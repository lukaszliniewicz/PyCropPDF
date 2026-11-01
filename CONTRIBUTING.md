# Development and static-check policy

Use Python 3.11 or later. Install [uv](https://docs.astral.sh/uv/getting-started/installation/),
then install the checked-in dependency resolution:

```sh
uv sync --locked --extra dev --extra deskew
uv run --no-sync python scripts/check.py
uv run --no-sync python -m pytest -q
```

The equivalent static-check command in an already activated development environment is
`python scripts/check.py`. It runs from the repository root regardless of the caller's
working directory and uses that environment's Python for every tool. It stops at the
first failure and never edits source or the type baseline.

## Required checks

| Tool | Policy |
| --- | --- |
| Ruff | All selected lint rules and formatting must pass. The rules in `pyproject.toml` include correctness, imports, simplifications, and common performance mistakes. |
| Basedpyright | Check all application modules and the check scripts in `standard` mode, targeting Python 3.11. New diagnostics fail. Import cycles and unnecessary type-ignore comments are errors. |
| Vulture | Scan application code, tests, entry-point wrappers, and the check scripts at 60% confidence, including unused functions and methods. Qt callbacks and documented compatibility aliases have explicit, type-checked references in `scripts/vulture_allowlist.py`. |
| Import Linter | Reject cycles inside `pycroppdf` and imports against the declared layers. Adding a module requires placing it in the layer contract. |

The application layers, from highest to lowest, are the CLI entry point, runtime
diagnostics, the window, workers/widgets/thumbnails/icons/state, overlay groups/
rotation/provenance, preview adapters, and pure raster-cache/raster-geometry/processing
helpers. Modules on the same layer are
independent. A lower layer must not import a higher layer. These contracts preserve
the existing boundaries; future extractions should update them deliberately.

Tests are linted and scanned for references by Vulture. They are not included in the
initial type-check scope because their dynamic Qt fixtures and mocks need separate
typing work. Every application module is included. Generated files, review artifacts,
virtual environments, and builds are excluded.

## Existing type debt

`.basedpyright/baseline.json` currently records 60 legacy diagnostics (23 in the window,
31 in widgets, and 6 in rotation), down from the initial 113. A passing check means
**no diagnostics beyond that inventory**, not
that the application is fully typed. Do not add errors to the baseline just to pass a
change. Fix issues in code being changed when practical, and prefer explicit types and
state guards to broad suppressions.

Run `uv run --no-sync basedpyright --pythonpath .venv/bin/python` on Linux/macOS (or
`uv run --no-sync basedpyright --pythonpath .venv/Scripts/python.exe` on Windows) to
remove resolved baseline entries automatically, then commit the updated baseline.
The checker and CI set `CI=true`, which makes Basedpyright require an up-to-date baseline
without rewriting it. Deliberate adoption of a new diagnostic rule may require
`basedpyright --writebaseline`; review that diff and explain the additional debt.
Do not disable a diagnostic across the project to hide an isolated failure.

## Dependency updates and verification

Package metadata uses compatibility ranges; `uv.lock` records the exact development
and build resolution, including optional extras and platform markers. Update them with:

```sh
uv lock --upgrade
uv sync --locked --extra dev --extra deskew
uv run --no-sync python scripts/check.py
uv run --no-sync python -m pytest -q
```

Review upstream release notes and Python/platform support before changing a major
version constraint. Verify the optional deskew path as well as the core install. A
passing source test suite does not qualify a packaged executable: rebuild and smoke-test
Windows and Linux artifacts before a release. The Windows build needs a Windows host.
Use `uv sync --locked --extra build --extra deskew` for the locked build dependencies.

CI runs the static policy on Linux and tests on Linux/Windows with Python 3.11/3.13.
GUI layout still needs visual review, including small windows and increased display
scaling. Static checks and headless tests do not establish visual quality.

## Standalone release builds

The manually dispatched **Release builds** workflow builds the exact selected commit
on Windows and Ubuntu 22.04 with the locked build/deskew dependencies. It verifies
pinned AppImage tools, collects installed dependency notices, and saves build records
with the source commit, lockfile hash, Python/package versions, and binary hash.
Artifacts include the Windows executable, Linux AppImage, platform-specific notices,
and build records. The workflow does not publish a release automatically.

Both packaged executables must pass `--check-preview-runtime REPORT.json` and
`--check-deskew-workers`. The first opens the actual window, renders a four-page
fixture through spawned workers, opens a page preview, applies a partial edit, checks
pixels/source preservation/cache cleanup, and saves a screenshot. Linux uses Xvfb
with the xcb plugin; Windows uses its native platform plugin. These are automated
runtime checks, not a claim of manual interaction or screen-reader qualification.

For a local build, install `uv sync --locked --extra build --extra deskew`, then run
`uv run --no-sync python scripts/build_release.py`. On Linux, supply verified
`--appimagetool PATH --runtime PATH`; the pinned versions and SHA-256 values are in
the workflow. Build output defaults to `dist/release`; existing outputs are preserved.

Before publication, require green source CI and both packaged checks for the exact
release commit. Download the workflow artifacts, verify their recorded hashes, inspect
the screenshots, and attach the verified binaries/notices/build records plus a
`SHA256SUMS` file to a draft release. Publish only after checking the tag target and
uploaded asset hashes. Do not substitute a local rebuild for the checked artifacts.
