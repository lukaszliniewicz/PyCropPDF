"""Explicit packaged-runtime checks used before publishing standalone builds."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import traceback
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz
from PyQt6.QtCore import QRectF
from PyQt6.QtWidgets import QApplication, QMessageBox

from . import main_window
from .main_window import PDFViewer
from .preview_images import CachedImages
from .provenance import sha256_file
from .raster_cache import DEFAULT_DECODED_BUDGET
from .workers import RenderAllPagesWorker


def _wait_for_previews(app: QApplication, viewer: PDFViewer, errors: list[str]) -> None:
    deadline = time.monotonic() + 90
    while viewer.is_processing and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    if viewer.is_processing or not viewer.threadpool.waitForDone(5000):
        raise RuntimeError("The packaged preview worker did not finish.")
    if errors or viewer._previews_incomplete:
        raise RuntimeError(f"The packaged preview failed: {errors}")


def _preview_runtime(report_path: Path) -> dict[str, object]:
    app = QApplication.instance() or QApplication([sys.argv[0]])
    if not isinstance(app, QApplication):
        raise RuntimeError("The preview check requires a QApplication.")
    errors: list[str] = []
    requests: list[tuple[int, ...]] = []

    def record_worker(*args, **kwargs) -> RenderAllPagesWorker:
        worker = RenderAllPagesWorker(*args, **kwargs)
        requests.append(worker.page_numbers)
        return worker

    def record_error(*args: object, **_kwargs: object) -> QMessageBox.StandardButton:
        errors.append(str(args[2]) if len(args) > 2 else "Preview error")
        return QMessageBox.StandardButton.Ok

    with (
        tempfile.TemporaryDirectory(prefix="pycroppdf-runtime-check-") as temporary,
        patch.object(main_window, "RenderAllPagesWorker", side_effect=record_worker),
        patch.object(QMessageBox, "critical", side_effect=record_error),
    ):
        source = Path(temporary) / "preview-check.pdf"
        with fitz.open() as document:
            for page_number in range(4):
                page = document.new_page(width=240, height=320)
                for line in range(8):
                    page.insert_text(
                        (30, 60 + line * 25), f"Packaged preview page {page_number + 1}"
                    )
            document.save(source)
        source_hash = sha256_file(str(source))
        viewer = PDFViewer()
        cache_root = viewer.preview_cache.root
        try:
            viewer.showNormal()
            viewer.resize(800, 600)
            viewer.show()
            if not viewer.loadPDF(str(source)):
                raise RuntimeError("The packaged window could not open the fixture.")
            _wait_for_previews(app, viewer, errors)
            images = viewer.images
            if not isinstance(images, CachedImages) or images.ready_pages != frozenset(range(4)):
                raise RuntimeError("The packaged preview cache is incomplete.")
            unchanged, before = images[0], images[3]
            if unchanged is None or before is None:
                raise RuntimeError("The packaged page images are unavailable.")
            viewer.togglePagePreview(3)
            app.processEvents()
            if viewer.preview_page_num != 3:
                raise RuntimeError("The packaged window could not preview a page.")
            viewer.applyCover(QRectF(45, 45, 60, 60), [3])
            _wait_for_previews(app, viewer, errors)
            if viewer.images[3] == before or viewer.images[0] != unchanged:
                raise RuntimeError("The packaged partial refresh changed the wrong pixels.")
            if requests != [(0, 1, 2, 3), (3,)]:
                raise RuntimeError(f"Unexpected packaged render requests: {requests}")
            if viewer.source_sha256 != source_hash or sha256_file(str(source)) != source_hash:
                raise RuntimeError("The packaged check changed the source PDF.")
            peak = viewer.preview_cache.peak_decoded_bytes
            if peak > DEFAULT_DECODED_BUDGET:
                raise RuntimeError("The packaged preview exceeded its cache budget.")
            app.processEvents()
            if not viewer.grab().save(str(report_path.with_suffix(".png"))):
                raise RuntimeError("The packaged window screenshot could not be saved.")
            result: dict[str, object] = {
                "passed": True,
                "pages": 4,
                "qt_platform": app.platformName(),
                "render_requests": requests,
                "source_unchanged": True,
                "partial_refresh_pixels_verified": True,
                "peak_cache_bytes": peak,
                "window_size": [viewer.width(), viewer.height()],
                "screenshot": report_path.with_suffix(".png").name,
            }
        finally:
            if viewer.is_processing:
                viewer.cancelOperation()
                viewer.threadpool.waitForDone(10000)
                app.processEvents()
            with patch.object(viewer, "_confirm_discard_changes", return_value=True):
                viewer.close()
            viewer.deleteLater()
            app.processEvents()
        if cache_root.exists():
            raise RuntimeError("The packaged check did not remove its preview cache.")
        result["cache_removed_on_close"] = True
        return result


def check_preview_runtime(report_name: str) -> None:
    """Exercise the real window, spawned rendering, and a local partial edit."""
    report_path = Path(report_name).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = _preview_runtime(report_path)
    except Exception:
        report_path.write_text(
            json.dumps({"passed": False, "traceback": traceback.format_exc()}, indent=2) + "\n",
            encoding="utf-8",
        )
        raise
    report_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
