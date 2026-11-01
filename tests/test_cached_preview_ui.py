from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf as fitz
import pytest
from PyQt6.QtCore import QPoint, QRectF, Qt
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget

import pycroppdf.workers as workers
from pycroppdf.main_window import PDFViewer
from pycroppdf.raster_cache import RasterCache
from pycroppdf.thumbnails import THUMBNAIL_PIXMAP_BUDGET, ThumbnailView


@pytest.fixture
def viewer():
    app = QApplication.instance() or QApplication([])
    with patch.object(PDFViewer, "showMaximized"):
        window = PDFViewer()
    document = fitz.open()
    document.new_page(width=240, height=320).insert_text((30, 60), "Page one")
    document.new_page(width=180, height=240).insert_text((30, 60), "Page two")
    window.pdf_doc = document
    window.page_map = [0, 1]
    window.original_page_count = 2
    window.resize(800, 600)
    window.show()
    app.processEvents()
    yield window
    window.threadpool.waitForDone()
    with patch.object(window, "_confirm_discard_changes", return_value=True):
        window.close()
    window.deleteLater()
    app.processEvents()


def _start(viewer, pages=None, *, retry=False):
    with patch.object(viewer.threadpool, "start") as start:
        viewer.retryPreviews() if retry else viewer.reloadImages(pages)
    return start.call_args.args[0]


def _run(worker):
    with patch.object(QMessageBox, "critical") as critical:
        worker.run()
    critical.assert_not_called()


def test_progressive_thumbnail_preview_cancel_retry_only_renders_missing_pages(viewer):
    source = viewer.pdf_doc.tobytes(no_new_id=True)
    worker = _start(viewer)
    original_images = viewer.images
    observations = []
    native_reads = []
    document_length = fitz.Document.__len__

    def cancel_after_first(result):
        page, _info = result
        if page == 0:
            observations.append((viewer.is_processing, viewer.scroll_area.pages.rowCount()))
            with (
                patch.object(viewer, "_set_preview_crop_selection") as crop_selection,
                patch.object(
                    fitz.Document,
                    "__len__",
                    autospec=True,
                    side_effect=document_length,
                ) as page_count,
            ):
                viewer.togglePagePreview(0)
            native_reads.append((crop_selection.call_count, page_count.call_count))
            observations.append(viewer.preview_page_num)
            viewer.cancelOperation()

    worker.signals.result.connect(cancel_after_first)
    _run(worker)
    assert observations == [(True, 2), 0]
    assert native_reads == [(0, 0)]
    assert viewer.images.ready_pages == {0}
    assert not viewer.is_processing and viewer._previews_incomplete
    assert not viewer.crop_btn.isEnabled()
    assert viewer.pdf_doc.tobytes(no_new_id=True) == source

    retry = _start(viewer, retry=True)
    with patch.object(
        workers, "_render_page_from_document", wraps=workers._render_page_from_document
    ) as render:
        _run(retry)
    assert [call.args[1] for call in render.call_args_list] == [1]
    assert not original_images.ready_pages
    assert original_images[0] is None
    assert viewer.images.ready_pages == {0, 1}
    assert not viewer.is_processing and not viewer._previews_incomplete
    assert viewer.crop_btn.isEnabled()
    assert viewer.preview_page_num == 0
    assert viewer.single_view.sceneRect() == QRectF(0, 0, 360, 480)
    assert (
        viewer.single_view.transform().m22() * 480 > viewer.single_view.viewport().height() * 0.85
    )
    assert viewer.pdf_doc.tobytes(no_new_id=True) == source


def test_partial_cover_preserves_untouched_raster_and_updates_composite(viewer):
    _run(_start(viewer))
    untouched_pixels = viewer.preview_cache.read_page(1, cache=False)
    before = viewer.images[1]
    covered_before = viewer.images[0]
    with patch.object(viewer.threadpool, "start") as start:
        viewer.applyCover(QRectF(45, 45, 60, 60), [0])
    with patch.object(
        workers, "_render_page_from_document", wraps=workers._render_page_from_document
    ) as render:
        _run(start.call_args.args[0])
    assert [call.args[1] for call in render.call_args_list] == [0]
    assert viewer.preview_cache.read_page(1, cache=False) is untouched_pixels
    assert viewer.preview_cache.disk_bytes == 0
    assert viewer.images[1] == before
    covered_after = viewer.images[0]
    assert covered_after != covered_before
    changed = next(
        (x, y)
        for y in range(45, 105)
        for x in range(45, 105)
        if covered_before.pixelColor(x, y) != covered_after.pixelColor(x, y)
    )
    assert viewer.odd_view_pixmap_cache.toImage().pixelColor(*changed) == covered_after.pixelColor(
        *changed
    )
    assert viewer.cover_operations[-1]["original_page"] == 1
    assert not viewer._previews_incomplete


def test_deletion_remaps_cache_without_native_render_and_rebuilds_parity(viewer):
    _run(_start(viewer))
    kept = viewer.images[1]
    kept_pixels = viewer.preview_cache.read_page(1, cache=False)
    old_images = viewer.images
    viewer.selected_pages = {0}
    with patch.object(viewer.threadpool, "start") as start:
        viewer.deleteSelectedPages()
    with patch.object(workers, "_render_page_from_document") as render:
        _run(start.call_args.args[0])
    render.assert_not_called()
    assert viewer.page_map == [1]
    assert viewer.images[0] == kept
    assert viewer.preview_cache.read_page(0, cache=False) is kept_pixels
    assert viewer.preview_cache.disk_bytes == 0
    assert old_images[1] is None
    assert viewer.even_view_pixmap_cache is None
    assert viewer.odd_view.sceneRect() == QRectF(0, 0, 270, 360)
    assert not viewer._previews_incomplete


def test_large_rotated_page_uses_exact_scene_geometry_with_zero_decoded_budget(viewer):
    viewer.pdf_doc.close()
    viewer.pdf_doc = fitz.open()
    page = viewer.pdf_doc.new_page(width=4000, height=1200)
    page.set_cropbox(fitz.Rect(37.25, 29.75, 3968.5, 1174.5))
    page.set_rotation(90)
    viewer.pdf_doc.new_page(width=200, height=300)
    viewer.preview_cache.close()
    viewer.preview_cache = RasterCache(0)
    _run(_start(viewer))
    info = viewer.preview_cache.info(0)
    assert max(info.width, info.height) <= 2048
    assert max(info.logical_width, info.logical_height) > 2048
    assert viewer.preview_cache.decoded_bytes == 0
    expected = fitz.Rect(150, 100, 1150, 900)
    scene = viewer._pdf_rect_to_scene_rect(expected, 0)
    assert tuple(viewer._scene_rect_to_pdf_rect(scene, 0)) == pytest.approx(
        tuple(expected), abs=0.001
    )
    viewer.togglePagePreview(0)
    assert viewer.single_view.sceneRect().size().width() == info.logical_width
    assert viewer.single_view.sceneRect().size().height() == info.logical_height
    scene = viewer._pdf_rect_to_scene_rect(expected, 0)
    assert tuple(viewer._scene_rect_to_pdf_rect(scene, 0)) == pytest.approx(
        tuple(expected), abs=0.001
    )
    assert viewer.preview_cache.decoded_bytes == 0


def test_corrupt_preview_offers_retry_and_restores_only_damaged_page(viewer):
    viewer.preview_cache.close()
    viewer.preview_cache = RasterCache(0)
    _run(_start(viewer))
    viewer.preview_cache._pages[0].full_path.unlink()
    viewer.togglePagePreview(0)
    QApplication.processEvents()
    assert viewer._previews_incomplete and viewer.retry_previews_btn.isEnabled()
    with patch.object(
        workers, "_render_page_from_document", wraps=workers._render_page_from_document
    ) as render:
        _run(_start(viewer, retry=True))
    assert [call.args[1] for call in render.call_args_list] == [0]
    assert not viewer._previews_incomplete


def test_fitting_document_and_partial_edit_never_write_preview_files(viewer):
    with patch(
        "pycroppdf.raster_cache._write_private_file",
        side_effect=AssertionError("A fitting document must remain in RAM"),
    ) as spill:
        _run(_start(viewer))
        unchanged = viewer.preview_cache.read_page(1, cache=False)
        assert list(viewer.preview_cache.root.iterdir()) == []
        with patch.object(viewer.threadpool, "start") as start:
            viewer.applyCover(QRectF(45, 45, 60, 60), [0])
        _run(start.call_args.args[0])
        assert viewer.preview_cache.read_page(1, cache=False) is unchanged
        assert list(viewer.preview_cache.root.iterdir()) == []
        spill.assert_not_called()


def test_five_hundred_thumbnail_rows_keep_widget_and_pixmap_counts_bounded(viewer):
    view = viewer.scroll_area
    image = QImage(160, 240, QImage.Format.Format_RGB888)
    image.fill(Qt.GlobalColor.white)
    viewer.images = [image] * 500
    viewer.updateThumbnails()
    QApplication.processEvents()
    initial_widgets = len(view.findChildren(QWidget))
    assert view.pages.rowCount() == 500
    for page in range(500):
        view.pages.thumbnail(page)
    assert view.pages.pixmap_bytes <= THUMBNAIL_PIXMAP_BUDGET
    assert len(view.findChildren(QWidget)) == initial_widgets < 20
    view.scrollTo(view.pages.index(499), ThumbnailView.ScrollHint.PositionAtCenter)
    QApplication.processEvents()
    assert view.visualRect(view.pages.index(499)).intersects(view.viewport().rect())


def test_thumbnail_keyboard_checks_without_panning_and_enter_previews(viewer):
    _run(_start(viewer))
    view = viewer.scroll_area
    view.setCurrentIndex(view.pages.index(1))
    view.setFocus()
    QApplication.processEvents()
    QTest.keyClick(view, Qt.Key.Key_Space)
    assert viewer.selected_pages == {1}
    assert not any(
        page_view._pan for page_view in (viewer.single_view, viewer.odd_view, viewer.even_view)
    )
    QTest.keyClick(view, Qt.Key.Key_Return)
    assert viewer.preview_page_num == 1 and viewer.selected_pages == {1}
    QTest.keyClick(view, Qt.Key.Key_Space)
    assert not viewer.selected_pages
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(40, 80))
    assert viewer.preview_page_num == 0 and not viewer.selected_pages
