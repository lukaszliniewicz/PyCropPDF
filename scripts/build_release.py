#!/usr/bin/env python3
"""Build a Windows executable or Linux AppImage and its release notices."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import TypedDict
from urllib.parse import urlsplit, urlunsplit

REPO = Path(__file__).resolve().parents[1]
ZIP_EPOCH_MIN = 315_532_800  # 1980-01-01 00:00:00 UTC
ZIP_EPOCH_MAX = 4_354_819_198  # 2107-12-31 23:59:58 UTC
LICENSE_NAMES = {
    "license",
    "licenses",
    "copying",
    "copyright",
    "notice",
    "authors",
}
LICENSE_FILE_RE = re.compile(
    r"^(?:licenses?|copying|copyright|notice|authors)(?:[._-].*)?$", re.IGNORECASE
)
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$")


class ReleaseError(RuntimeError):
    """Raised when the local release contract cannot be met."""


class PackageRecord(TypedDict):
    name: str
    version: str
    license_expression: str | None
    license: str | None
    project_urls: list[dict[str, str]]
    license_text_files: list[str]
    warnings: list[str]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_version() -> str:
    pyproject_path = REPO / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        version = str(data["project"]["version"])
    except (KeyError, OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseError(f"cannot read project version from {pyproject_path}") from exc
    if not VERSION_RE.fullmatch(version):
        raise ReleaseError(f"project version is unsafe for release filenames: {version!r}")
    return version


def target_platform() -> tuple[str, str]:
    machine = platform.machine().lower()
    if machine not in {"amd64", "x86_64"}:
        raise ReleaseError(f"only x86_64 builds are supported (found {machine or 'unknown'})")
    if sys.platform == "win32":
        return "windows", "x86_64"
    if sys.platform.startswith("linux"):
        return "linux", "x86_64"
    raise ReleaseError(f"only Windows and Linux builds are supported (found {sys.platform})")


def git_output(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def source_state() -> tuple[str, bool, int]:
    commit = git_output("rev-parse", "HEAD")
    dirty = bool(git_output("status", "--porcelain", "--untracked-files=all"))
    commit_epoch_text = git_output("show", "-s", "--format=%ct", "HEAD")
    try:
        commit_epoch = int(commit_epoch_text)
    except ValueError as exc:
        raise ReleaseError("git returned an invalid commit timestamp") from exc
    return commit, dirty, commit_epoch


def normalize_package_name(name: str) -> str:
    normalized = re.sub(r"[-_.]+", "-", name).lower()
    if not normalized or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized):
        raise ReleaseError(f"installed distribution has an unsafe package name: {name!r}")
    return normalized


def is_license_file(path: PurePosixPath) -> bool:
    if any(
        part.casefold() in LICENSE_NAMES or LICENSE_FILE_RE.fullmatch(part)
        for part in path.parts[:-1]
    ):
        return True
    return bool(LICENSE_FILE_RE.fullmatch(path.name))


def safe_relative_path(raw_path: str) -> PurePosixPath:
    if (
        not raw_path
        or "\\" in raw_path
        or "\x00" in raw_path
        or any(ord(character) < 32 for character in raw_path)
        or ":" in raw_path
        or raw_path.startswith("/")
    ):
        raise ReleaseError(f"unsafe relative path: {raw_path!r}")
    raw_parts = raw_path.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ReleaseError(f"unsafe relative path: {raw_path!r}")
    return PurePosixPath(*raw_parts)


def safe_project_url(raw_url: str) -> str | None:
    """Remove URL credentials, query parameters, and fragments from public metadata."""
    parts = urlsplit(raw_url.strip())
    if not parts.scheme or not parts.netloc:
        return None
    hostname = parts.hostname or ""
    if not hostname:
        return None
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{hostname}:{port}" if port is not None else hostname
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def metadata_value(metadata: importlib.metadata.PackageMetadata, key: str) -> str | None:
    values = metadata.get_all(key, [])
    return values[0] if values else None


def project_urls(metadata: importlib.metadata.PackageMetadata) -> list[dict[str, str]]:
    urls: list[dict[str, str]] = []
    for raw in metadata.get_all("Project-URL", []):
        label, separator, value = raw.partition(",")
        url = value.strip() if separator else label.strip()
        title = label.strip() if separator else "Project"
        safe_url = safe_project_url(url)
        if safe_url:
            urls.append({"label": title, "url": safe_url})
    home_page = metadata_value(metadata, "Home-page")
    if home_page:
        safe_url = safe_project_url(home_page)
        if safe_url:
            urls.append({"label": "Home-page", "url": safe_url})
    return sorted(urls, key=lambda item: (item["label"].casefold(), item["url"]))


def distribution_name_version(distribution: importlib.metadata.Distribution) -> tuple[str, str]:
    name = metadata_value(distribution.metadata, "Name")
    version = distribution.version
    if not name or not version:
        raise ReleaseError("an installed distribution has missing name or version metadata")
    return name, version


def supplemental_files(source_root: Path) -> list[tuple[Path, PurePosixPath]]:
    """Return regular files below a non-symlink license root without following links."""
    try:
        root_mode = source_root.lstat().st_mode
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(root_mode):
        raise ReleaseError("packaging/licenses must be a real directory when present")

    resolved_root = source_root.resolve(strict=True)
    pending: list[tuple[Path, PurePosixPath]] = [(source_root, PurePosixPath())]
    files: list[tuple[Path, PurePosixPath]] = []
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as scan:
            entries = sorted(scan, key=lambda entry: entry.name)
        for entry in entries:
            relative = PurePosixPath(*prefix.parts, entry.name)
            relative = safe_relative_path(relative.as_posix())
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                pending.append((Path(entry.path), relative))
            elif stat.S_ISREG(mode):
                source = Path(entry.path)
                if not source.resolve(strict=True).is_relative_to(resolved_root):
                    raise ReleaseError(
                        f"supplemental license path escapes packaging/licenses: {relative}"
                    )
                files.append((source, relative))
    return sorted(files, key=lambda item: item[1].as_posix())


def copy_supplemental_notices(destination: Path) -> list[str]:
    copied_paths: list[str] = []
    for source, relative in supplemental_files(REPO / "packaging" / "licenses"):
        relative_target = PurePosixPath("supplemental", *relative.parts)
        target = destination.joinpath(*relative_target.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(target):
            if not target.is_file() or sha256_file(target) != sha256_file(source):
                raise ReleaseError(f"conflicting supplemental license path: {relative}")
        else:
            shutil.copyfile(source, target)
        copied_paths.append(relative_target.as_posix())
    return copied_paths


def collect_notices(destination: Path) -> list[PackageRecord]:
    """Copy application and installed-package license texts into a self-contained tree."""
    if os.path.lexists(destination):
        raise ReleaseError(f"refusing to reuse existing notice directory: {destination}")
    destination.mkdir(parents=True)
    shutil.copyfile(REPO / "LICENSE", destination / "LICENSE")

    packages_by_name: dict[str, PackageRecord] = {}
    distributions = list(importlib.metadata.distributions())
    for distribution in distributions:
        name, version = distribution_name_version(distribution)
        normalized_name = normalize_package_name(name)
        metadata = distribution.metadata
        license_expression = metadata_value(metadata, "License-Expression")
        license_value = metadata_value(metadata, "License")
        urls = project_urls(metadata)
        package = packages_by_name.get(normalized_name)
        if package is None:
            package = PackageRecord(
                name=name,
                version=version,
                license_expression=license_expression,
                license=license_value,
                project_urls=urls,
                license_text_files=[],
                warnings=[],
            )
            packages_by_name[normalized_name] = package
        elif (
            package["version"] != version
            or package["license_expression"] != license_expression
            or package["license"] != license_value
            or package["project_urls"] != urls
        ):
            raise ReleaseError(
                f"installed distributions disagree on metadata for {normalized_name!r}"
            )

        package_files: list[str] = []
        file_records = distribution.files or ()
        root = Path(str(distribution.locate_file(""))).resolve()
        for record in file_records:
            raw_path = str(record)
            try:
                relative_path = safe_relative_path(raw_path)
            except ReleaseError:
                if is_license_file(PurePosixPath(raw_path.replace("\\", "/"))):
                    raise
                continue
            if not is_license_file(relative_path):
                continue

            source = Path(str(distribution.locate_file(str(record))))
            if not source.is_file():
                continue
            resolved_source = source.resolve(strict=True)
            if not resolved_source.is_relative_to(root):
                raise ReleaseError(
                    f"installed-distribution license path escapes its package root: {raw_path!r}"
                )

            relative_name = PurePosixPath("packages", normalized_name, *relative_path.parts)
            target = destination.joinpath(*relative_name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if sha256_file(target) != sha256_file(resolved_source):
                    raise ReleaseError(f"conflicting installed license text at {relative_name}")
            else:
                shutil.copyfile(resolved_source, target)
            package_files.append(relative_name.as_posix())

        package_files = sorted(set(package_files))
        package["license_text_files"] = sorted(
            set(package["license_text_files"]) | set(package_files)
        )

    packages = list(packages_by_name.values())
    for package in packages:
        warning = (
            []
            if package["license_text_files"]
            else ["No recorded license text file was found in this distribution."]
        )
        package["warnings"] = warning
        if warning:
            print(
                f"WARNING: {package['name']} {package['version']}: {warning[0]}",
                file=sys.stderr,
            )
    packages.sort(key=lambda item: (str(item["name"]).casefold(), str(item["version"])))
    copy_supplemental_notices(destination)
    (destination / "packages.json").write_text(
        json.dumps({"packages": packages}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    readme = (
        "# Release notices\n\n"
        "`LICENSE` is the PyCropPDF application license. Files under `packages/` are "
        "license and notice texts found in the installed build environment.\n\n"
        "The package inventory describes that installed environment and can include "
        "build or development tools that are not shipped in the application. Third-party "
        "terms apply to their respective packages; the project's MIT license does not "
        "replace those terms. A missing-text warning is recorded per package in "
        "`packages.json`. Files under `supplemental/`, when present, contain additional "
        "upstream license terms and provenance notes supplied with the release. This "
        "bundle does not assert legal compliance.\n"
    )
    (destination / "README.md").write_text(readme, encoding="utf-8")
    return packages


def zip_timestamp(commit_epoch: int) -> tuple[int, int, int, int, int, int]:
    raw_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if raw_epoch is None:
        raw_epoch_value = commit_epoch
    else:
        try:
            raw_epoch_value = int(raw_epoch)
        except ValueError as exc:
            raise ReleaseError("SOURCE_DATE_EPOCH must be an integer Unix timestamp") from exc
    bounded_epoch = min(max(raw_epoch_value, ZIP_EPOCH_MIN), ZIP_EPOCH_MAX)
    utc = time.gmtime(bounded_epoch)
    return (
        utc.tm_year,
        utc.tm_mon,
        utc.tm_mday,
        utc.tm_hour,
        utc.tm_min,
        utc.tm_sec - utc.tm_sec % 2,
    )


def make_notices_zip(notices_dir: Path, destination: Path, commit_epoch: int) -> None:
    timestamp = zip_timestamp(commit_epoch)
    files = sorted(path for path in notices_dir.rglob("*") if path.is_file())
    with zipfile.ZipFile(
        destination,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=False,
    ) as archive:
        for source in files:
            relative = source.relative_to(notices_dir)
            archive_name = PurePosixPath(*relative.parts).as_posix()
            info = zipfile.ZipInfo(archive_name, date_time=timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            with source.open("rb") as file_handle:
                archive.writestr(info, file_handle.read(), compress_type=zipfile.ZIP_DEFLATED)


def require_build_environment() -> None:
    for distribution_name in ("pycroppdf", "deskew", "pyinstaller"):
        try:
            importlib.metadata.version(distribution_name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ReleaseError(
                f"the active Python environment is missing required distribution {distribution_name}"
            ) from exc


def resolve_appimagetool(value: str) -> Path:
    resolved = shutil.which(value)
    if not resolved:
        raise ReleaseError(f"cannot find executable appimagetool: {value}")
    return Path(resolved).resolve(strict=True)


def resolve_runtime(value: str) -> Path:
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ReleaseError(f"AppImage runtime is not a file: {value}")
    return path


def output_names(version: str, system_name: str) -> dict[str, str]:
    if system_name == "windows":
        application = f"PyCropPDF-{version}-windows-x86_64.exe"
    else:
        application = f"PyCropPDF-{version}-x86_64.AppImage"
    return {
        "application": application,
        "notices": f"PyCropPDF-{version}-{system_name}-x86_64-notices.zip",
        "metadata": f"PyCropPDF-{version}-{system_name}-x86_64-build.json",
    }


def refuse_existing_outputs(output_dir: Path, names: dict[str, str]) -> None:
    existing = [output_dir / name for name in names.values() if os.path.lexists(output_dir / name)]
    if existing:
        listed = ", ".join(str(path) for path in existing)
        raise ReleaseError(f"refusing to overwrite existing release artifacts: {listed}")


def copy_appimage_resources(appdir: Path) -> None:
    resource_dir = REPO / "packaging" / "appimage"
    for filename in ("AppRun", "PyCropPDF.desktop", "PyCropPDF.svg"):
        source = resource_dir / filename
        if not source.is_file():
            raise ReleaseError(f"required AppImage resource is missing: {source.relative_to(REPO)}")
        shutil.copyfile(source, appdir / filename)
    (appdir / "AppRun").chmod(0o755)


def publish_artifacts(staged: Iterable[tuple[Path, Path]]) -> None:
    published: list[tuple[Path, int, int]] = []
    try:
        for source, destination in staged:
            with destination.open("xb") as output_handle:
                identity = os.fstat(output_handle.fileno())
                published.append((destination, identity.st_dev, identity.st_ino))
                with source.open("rb") as input_handle:
                    shutil.copyfileobj(input_handle, output_handle)
                os.chmod(destination, stat.S_IMODE(source.stat().st_mode))
    except Exception:
        for path, device, inode in reversed(published):
            try:
                current = path.stat()
                if current.st_dev == device and current.st_ino == inode:
                    path.unlink()
            except FileNotFoundError:
                pass
        raise


def build_release(args: argparse.Namespace) -> list[Path]:
    system_name, architecture = target_platform()
    if system_name == "linux" and (not args.appimagetool or not args.runtime):
        raise ReleaseError("Linux builds require both --appimagetool and --runtime")
    if system_name == "windows" and (args.appimagetool or args.runtime):
        raise ReleaseError("--appimagetool and --runtime are only valid for Linux builds")

    version = project_version()
    names = output_names(version, system_name)
    output_dir = Path(args.output).expanduser()
    if not output_dir.is_absolute():
        output_dir = REPO / output_dir
    output_dir = output_dir.resolve()
    refuse_existing_outputs(output_dir, names)
    require_build_environment()

    appimagetool = resolve_appimagetool(args.appimagetool) if system_name == "linux" else None
    runtime = resolve_runtime(args.runtime) if system_name == "linux" else None
    source_commit, source_dirty, commit_epoch = source_state()
    lock_path = REPO / "uv.lock"
    if not lock_path.is_file():
        raise ReleaseError("uv.lock is required to record the build input hash")
    lock_sha256 = sha256_file(lock_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    refuse_existing_outputs(output_dir, names)

    build_parent = REPO / "build"
    build_parent.mkdir(parents=True, exist_ok=True)
    published_paths = [output_dir / name for name in names.values()]
    with tempfile.TemporaryDirectory(prefix="pycroppdf-release-", dir=build_parent) as temp_dir:
        work_dir = Path(temp_dir)
        notices_dir = work_dir / "notices"
        packages = collect_notices(notices_dir)
        staged_notices = work_dir / names["notices"]
        make_notices_zip(notices_dir, staged_notices, commit_epoch)

        pyinstaller_dist = work_dir / "dist"
        common_command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--additional-hooks-dir",
            str(REPO / "packaging" / "pyinstaller_hooks"),
            "--hidden-import",
            "deskew",
            "--copy-metadata",
            "pycroppdf",
            "--add-data",
            f"{notices_dir}{os.pathsep}licenses",
            "--distpath",
            str(pyinstaller_dist),
            "--workpath",
            str(work_dir / "work"),
            "--specpath",
            str(work_dir / "spec"),
        ]
        if system_name == "windows":
            executable_stem = names["application"][: -len(".exe")]
            command = [
                *common_command,
                "--onefile",
                "--noconsole",
                "--name",
                executable_stem,
                str(REPO / "run.py"),
            ]
        else:
            command = [
                *common_command,
                "--onedir",
                "--name",
                "PyCropPDF",
                str(REPO / "run.py"),
            ]
        subprocess.run(command, check=True, timeout=1200)

        if system_name == "windows":
            built_application = pyinstaller_dist / names["application"]
        else:
            built_directory = pyinstaller_dist / "PyCropPDF"
            if not built_directory.is_dir():
                raise ReleaseError("PyInstaller did not produce the expected Linux onedir output")
            appdir = work_dir / "AppDir"
            executable = built_directory / "PyCropPDF"
            if not executable.is_file():
                raise ReleaseError("PyInstaller output is missing its PyCropPDF executable")
            executable.chmod(executable.stat().st_mode | 0o111)
            target_directory = appdir / "usr" / "bin" / "PyCropPDF"
            target_directory.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(built_directory, target_directory, symlinks=True)
            (target_directory / "PyCropPDF").chmod(0o755)
            copy_appimage_resources(appdir)
            built_application = work_dir / names["application"]
            appimage_env = os.environ.copy()
            appimage_env.update(
                {
                    "ARCH": "x86_64",
                    "VERSION": version,
                    "SOURCE_DATE_EPOCH": str(commit_epoch),
                    "APPIMAGE_EXTRACT_AND_RUN": "1",
                }
            )
            assert appimagetool is not None and runtime is not None
            subprocess.run(
                [
                    str(appimagetool),
                    "--runtime-file",
                    str(runtime),
                    str(appdir),
                    str(built_application),
                ],
                check=True,
                timeout=1200,
                env=appimage_env,
            )
            built_application.chmod(0o755)

        if not built_application.is_file():
            raise ReleaseError("release executable was not produced")
        metadata: dict[str, object] = {
            "project_version": version,
            "source_commit": source_commit,
            "source_tree_dirty": source_dirty,
            "uv_lock_sha256": lock_sha256,
            "python_version": platform.python_version(),
            "platform": system_name,
            "architecture": architecture,
            "packages": [
                {"name": package["name"], "version": package["version"]} for package in packages
            ],
            "artifact": {
                "name": names["application"],
                "sha256": sha256_file(built_application),
            },
        }
        if system_name == "linux":
            assert appimagetool is not None and runtime is not None
            metadata["appimagetool_sha256"] = sha256_file(appimagetool)
            metadata["runtime_sha256"] = sha256_file(runtime)

        staged_metadata = work_dir / names["metadata"]
        staged_metadata.write_text(
            json.dumps(metadata, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        refuse_existing_outputs(output_dir, names)
        publish_artifacts(
            [
                (built_application, output_dir / names["application"]),
                (staged_notices, output_dir / names["notices"]),
                (staged_metadata, output_dir / names["metadata"]),
            ]
        )
    return published_paths


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a versioned Windows executable or Linux AppImage with notices."
    )
    parser.add_argument(
        "--output",
        default="dist/release",
        metavar="DIR",
        help="release output directory (default: dist/release, relative to the repository)",
    )
    parser.add_argument(
        "--appimagetool",
        metavar="PATH",
        help="appimagetool executable or command name (required for Linux)",
    )
    parser.add_argument(
        "--runtime",
        metavar="PATH",
        help="AppImage runtime file (required for Linux)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        artifacts = build_release(args)
    except (ReleaseError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"release build failed: {exc}", file=sys.stderr)
        return 1
    for path in artifacts:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
