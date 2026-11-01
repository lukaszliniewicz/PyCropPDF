from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import Future
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import pymupdf as fitz
import pytest
from PyQt6.QtCore import QRectF

from pycroppdf.workers import (
    LOCAL_RENDER_PAGE_LIMIT,
    MAX_RENDER_WORKERS,
    RenderAllPagesWorker,
    SaveWorker,
    _render_page_from_document,
    rect_to_tuple,
)


def _pdf_with_markers() -> bytes:
    document = fitz.open()
    page = document.new_page(width=320, height=240)
    page.draw_rect(fitz.Rect(65, 60, 85, 80), fill=(1, 0, 0), color=None, width=0)
    page.draw_rect(fitz.Rect(117, 115, 145, 145), fill=(0, 1, 0), color=None, width=0)
    page.draw_rect(fitz.Rect(175, 80, 200, 100), fill=(0, 0, 1), color=None, width=0)
    page.set_cropbox(fitz.Rect(37, 29, 277, 209))
    source = document.tobytes()
    document.close()
    return source


def _explicit_visual_rect(rect: fitz.Rect, cropbox: fitz.Rect, rotation: int) -> fitz.Rect:
    x0 = rect.x0 - cropbox.x0
    y0 = rect.y0 - cropbox.y0
    x1 = rect.x1 - cropbox.x0
    y1 = rect.y1 - cropbox.y0
    width = cropbox.width
    height = cropbox.height
    match rotation:
        case 0:
            return fitz.Rect(x0, y0, x1, y1)
        case 90:
            return fitz.Rect(height - y1, x0, height - y0, x1)
        case 180:
            return fitz.Rect(width - x1, height - y1, width - x0, height - y0)
        case 270:
            return fitz.Rect(y0, width - x1, y1, width - x0)
        case _:
            raise AssertionError(f"Unexpected page rotation: {rotation}")


def _explicit_visual_point(
    point: tuple[float, float], cropbox: fitz.Rect, rotation: int
) -> tuple[float, float]:
    x = point[0] - cropbox.x0
    y = point[1] - cropbox.y0
    if rotation == 0:
        return x, y
    if rotation == 90:
        return cropbox.height - y, x
    if rotation == 180:
        return cropbox.width - x, cropbox.height - y
    if rotation == 270:
        return y, cropbox.width - x
    raise AssertionError(f"Unexpected page rotation: {rotation}")


def _pixel(samples: bytes, width: int, stride: int, x: int, y: int) -> tuple[int, int, int]:
    assert 0 <= x < width
    assert y >= 0
    start = y * stride + x * 3
    return tuple(samples[start : start + 3])


def test_pending_crop_renders_in_cropbox_space_for_every_page_rotation():
    source = _pdf_with_markers()
    pending_crop = fitz.Rect(60, 55, 205, 170)
    marker_points = {
        (75, 70): (255, 0, 0),
        (130, 130): (0, 255, 0),
        (188, 90): (0, 0, 255),
    }

    for rotation in (0, 90, 180, 270):
        document = fitz.open("pdf", source)
        page = document[0]
        page.set_rotation(rotation)
        matrix = fitz.Matrix(1.5, 1.5)

        full_reference = page.get_pixmap(matrix=matrix)
        no_crop_result = _render_page_from_document(document, 0, {})
        full_crop_result = _render_page_from_document(document, 0, {0: tuple(page.cropbox)})
        for result in (no_crop_result, full_crop_result):
            _page_num, samples, width, height, stride = result
            assert (width, height, stride) == (
                full_reference.width,
                full_reference.height,
                full_reference.stride,
            )
            assert samples == full_reference.samples

        subcrop_reference_rect = _explicit_visual_rect(pending_crop, page.cropbox, rotation)
        subcrop_reference = page.get_pixmap(matrix=matrix, clip=subcrop_reference_rect)
        _page_num, samples, width, height, stride = _render_page_from_document(
            document, 0, {0: tuple(pending_crop)}
        )
        assert (width, height, stride) == (
            subcrop_reference.width,
            subcrop_reference.height,
            subcrop_reference.stride,
        )
        assert samples == subcrop_reference.samples

        for point, color in marker_points.items():
            visual_x, visual_y = _explicit_visual_point(point, page.cropbox, rotation)
            pixel_x = int((visual_x - subcrop_reference_rect.x0) * 1.5)
            pixel_y = int((visual_y - subcrop_reference_rect.y0) * 1.5)
            assert _pixel(samples, width, stride, pixel_x, pixel_y) == color
        document.close()


def test_rect_to_tuple_handles_qrectf_and_pdf_rect_with_fixed_float_coordinates():
    assert rect_to_tuple(QRectF(5, 6, 7, 8)) == (5.0, 6.0, 12.0, 14.0)
    assert rect_to_tuple(fitz.Rect(1, 2, 3, 4)) == (1.0, 2.0, 3.0, 4.0)
    assert rect_to_tuple((1, 2, 3, 4)) == (1.0, 2.0, 3.0, 4.0)


def _three_page_pdf() -> bytes:
    document = fitz.open()
    for page_number in range(3):
        page = document.new_page(width=160, height=120)
        page.insert_text((20, 30), f"Page {page_number + 1}")
    source = document.tobytes()
    document.close()
    return source


def test_render_pool_uses_spawn_and_preserves_worker_limits():
    constructor_kwargs = []

    class FakeExecutor:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            return False

        def submit(self, _task, page_num):
            future = Future()
            future.set_result((page_num, b"\0\0\0", 1, 1, 3))
            return future

    def make_executor(**kwargs):
        constructor_kwargs.append(kwargs)
        return FakeExecutor()

    worker = RenderAllPagesWorker(_three_page_pdf(), 3)
    results = []
    errors = []
    finished = []
    worker.signals.result.connect(results.append)
    worker.signals.error.connect(errors.append)
    worker.signals.finished.connect(lambda: finished.append(True))

    with (
        patch("pycroppdf.workers.os.process_cpu_count", create=True, return_value=2),
        patch(
            "pycroppdf.workers.concurrent.futures.ProcessPoolExecutor",
            side_effect=make_executor,
        ) as executor_constructor,
    ):
        worker.run()

    executor_constructor.assert_called_once()
    kwargs = constructor_kwargs[0]
    assert kwargs["mp_context"].get_start_method() == "spawn"
    assert kwargs["max_workers"] == 1
    assert kwargs["max_workers"] <= MAX_RENDER_WORKERS
    assert kwargs["max_workers"] <= 3
    assert sorted(page_num for page_num, _image in results) == [0, 1, 2]
    assert errors == []
    assert finished == [True]

    with patch("pycroppdf.workers.concurrent.futures.ProcessPoolExecutor") as constructor:
        local_worker = RenderAllPagesWorker(_three_page_pdf(), LOCAL_RENDER_PAGE_LIMIT)
        local_worker.run()
        constructor.assert_not_called()


def test_render_worker_completes_real_spawn_pool_in_guarded_subprocess():
    repository_root = Path(__file__).resolve().parents[1]
    script = dedent(
        """
        import pymupdf as fitz
        from pycroppdf.workers import RenderAllPagesWorker

        def main():
            document = fitz.open()
            for page_number in range(3):
                page = document.new_page(width=160, height=120)
                page.insert_text((20, 30), f"Page {page_number + 1}")
            pdf_bytes = document.tobytes()
            document.close()

            results = []
            errors = []
            finished = []
            worker = RenderAllPagesWorker(pdf_bytes, 3)
            worker.signals.result.connect(results.append)
            worker.signals.error.connect(errors.append)
            worker.signals.finished.connect(lambda: finished.append(True))
            worker.run()

            assert errors == [], errors
            assert sorted(page_num for page_num, _image in results) == [0, 1, 2]
            assert all(not image.isNull() for _page_num, image in results)
            assert finished == [True]

        if __name__ == "__main__":
            main()
        """
    )
    with tempfile.TemporaryDirectory() as directory:
        script_path = Path(directory, "render_spawn_check.py")
        script_path.write_text(script, encoding="utf-8")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, (str(repository_root), environment.get("PYTHONPATH")))
        )
        completed = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=repository_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"


def _pdf_bytes(text: str = "source") -> bytes:
    document = fitz.open()
    page = document.new_page(width=200, height=150)
    page.insert_text((24, 36), text)
    data = document.tobytes()
    document.close()
    return data


def _save_worker(
    save_path: str,
    source_path: str | None,
    manifest_path: str,
) -> SaveWorker:
    return SaveWorker(
        _pdf_bytes(),
        save_path,
        source_path=source_path,
        manifest_path=manifest_path,
        page_map=[0],
        original_page_count=1,
    )


def _run_and_assert_manifest_alias_rejected(
    worker: SaveWorker,
    watched_files: dict[str, bytes],
    missing_paths: tuple[str, ...],
    temp_root: str,
) -> None:
    errors = []
    results = []
    finished = []
    worker.signals.error.connect(errors.append)
    worker.signals.result.connect(results.append)
    worker.signals.finished.connect(lambda: finished.append(True))

    worker.run()

    assert len(errors) == 1
    assert "must not alias" in errors[0]
    assert results == []
    assert finished == [True]
    for path, expected_contents in watched_files.items():
        assert Path(path).read_bytes() == expected_contents
    for path in missing_paths:
        assert not Path(path).exists()
    assert not any(
        path.name.startswith(".pycroppdf-") for path in Path(temp_root).rglob("*") if path.is_file()
    )


def test_manifest_equal_to_output_is_rejected_before_writing():
    with tempfile.TemporaryDirectory() as directory:
        source_path = str(Path(directory, "source.pdf"))
        output_path = str(Path(directory, "output.pdf"))
        source_contents = b"preserve source"
        output_contents = b"preserve output"
        Path(source_path).write_bytes(source_contents)
        Path(output_path).write_bytes(output_contents)

        _run_and_assert_manifest_alias_rejected(
            _save_worker(output_path, source_path, output_path),
            {source_path: source_contents, output_path: output_contents},
            (),
            directory,
        )


def test_manifest_equal_to_source_is_rejected_before_creating_output():
    with tempfile.TemporaryDirectory() as directory:
        source_path = str(Path(directory, "source.pdf"))
        output_path = str(Path(directory, "output.pdf"))
        source_contents = b"preserve source"
        Path(source_path).write_bytes(source_contents)

        _run_and_assert_manifest_alias_rejected(
            _save_worker(output_path, source_path, source_path),
            {source_path: source_contents},
            (output_path,),
            directory,
        )


def test_manifest_path_normalizes_dotdot_before_alias_check():
    with tempfile.TemporaryDirectory() as directory:
        output_path = str(Path(directory, "output.pdf"))
        alias_path = os.path.join(directory, "unused", "..", "output.pdf")
        contents = b"preserve output"
        Path(output_path).write_bytes(contents)

        _run_and_assert_manifest_alias_rejected(
            _save_worker(output_path, None, alias_path),
            {output_path: contents},
            (),
            directory,
        )


def test_manifest_symlinked_parent_alias_is_rejected():
    with tempfile.TemporaryDirectory() as directory:
        real_parent = Path(directory, "real")
        alias_parent = Path(directory, "alias")
        real_parent.mkdir()
        try:
            alias_parent.symlink_to(real_parent, target_is_directory=True)
        except (NotImplementedError, OSError) as error:
            pytest.skip(f"Directory symlinks are unavailable: {error}")

        source_path = str(real_parent / "source.pdf")
        manifest_alias = str(alias_parent / "source.pdf")
        output_path = str(real_parent / "output.pdf")
        contents = b"preserve source"
        Path(source_path).write_bytes(contents)

        _run_and_assert_manifest_alias_rejected(
            _save_worker(output_path, source_path, manifest_alias),
            {source_path: contents},
            (output_path,),
            directory,
        )


def test_manifest_hardlink_alias_is_rejected():
    with tempfile.TemporaryDirectory() as directory:
        source_path = str(Path(directory, "source.pdf"))
        manifest_path = str(Path(directory, "manifest-alias.pdf"))
        output_path = str(Path(directory, "output.pdf"))
        contents = b"preserve source"
        Path(source_path).write_bytes(contents)
        try:
            os.link(source_path, manifest_path)
        except (NotImplementedError, OSError) as error:
            pytest.skip(f"Hard links are unavailable: {error}")

        _run_and_assert_manifest_alias_rejected(
            _save_worker(output_path, source_path, manifest_path),
            {source_path: contents, manifest_path: contents},
            (output_path,),
            directory,
        )


def test_distinct_paths_write_valid_pdf_and_manifest():
    with tempfile.TemporaryDirectory() as directory:
        source_path = str(Path(directory, "source.pdf"))
        output_path = str(Path(directory, "output.pdf"))
        manifest_path = str(Path(directory, "output.json"))
        source_bytes = _pdf_bytes("source")
        Path(source_path).write_bytes(source_bytes)
        results = []
        errors = []
        finished = []
        worker = SaveWorker(
            source_bytes,
            output_path,
            source_path=source_path,
            manifest_path=manifest_path,
            page_map=[0],
            original_page_count=1,
        )
        worker.signals.result.connect(results.append)
        worker.signals.error.connect(errors.append)
        worker.signals.finished.connect(lambda: finished.append(True))
        worker.run()

        assert errors == []
        assert len(results) == 1
        assert finished == [True]
        with fitz.open(output_path) as output_document:
            assert len(output_document) == 1
            assert "source" in output_document[0].get_text()
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        assert manifest["source"]["sha256"] == hashlib.sha256(source_bytes).hexdigest()
        assert (
            manifest["output"]["sha256"]
            == hashlib.sha256(Path(output_path).read_bytes()).hexdigest()
        )


def test_distinct_manifest_allows_overwriting_loaded_source_snapshot():
    with tempfile.TemporaryDirectory() as directory:
        source_path = str(Path(directory, "source.pdf"))
        manifest_path = str(Path(directory, "source.json"))
        original_bytes = _pdf_bytes("original snapshot")
        edited_bytes = _pdf_bytes("edited replacement")
        Path(source_path).write_bytes(original_bytes)
        original_digest = hashlib.sha256(original_bytes).hexdigest()
        results = []
        errors = []
        finished = []
        worker = SaveWorker(
            edited_bytes,
            source_path,
            source_path=source_path,
            manifest_path=manifest_path,
            page_map=[0],
            original_page_count=1,
            source_sha256=original_digest,
        )
        worker.signals.result.connect(results.append)
        worker.signals.error.connect(errors.append)
        worker.signals.finished.connect(lambda: finished.append(True))
        worker.run()

        assert errors == []
        assert len(results) == 1
        assert finished == [True]
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        assert manifest["source"]["sha256"] == original_digest
        assert (
            manifest["output"]["sha256"]
            == hashlib.sha256(Path(source_path).read_bytes()).hexdigest()
        )
        with fitz.open(source_path) as saved_document:
            assert "edited replacement" in saved_document[0].get_text()
