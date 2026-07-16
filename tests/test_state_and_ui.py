import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

from pycroppdf.main_window import PDFViewer
from pycroppdf.state import (
    UndoSnapshotStore,
    clone_crop_info,
    remap_crop_info_after_deletions,
    remap_page_indices_after_deletions,
)
from pycroppdf.widgets import ThumbnailWidget
from pycroppdf.workers import SaveWorker


class StateHelpersTests(unittest.TestCase):
    def test_undo_snapshot_store_enforces_count_and_disk_limits(self):
        document = fitz.open()
        document.new_page(width=300, height=400)

        count_store = UndoSnapshotStore(max_entries=2, max_disk_bytes=1024 * 1024)
        count_paths = []
        try:
            for _ in range(3):
                path, size = count_store.write_document(document)
                count_paths.append(path)
                count_store.append({"pdf_path": path, "pdf_size": size})
            self.assertEqual(len(count_store.entries), 2)
            self.assertFalse(os.path.exists(count_paths[0]))
            self.assertTrue(all(os.path.exists(path) for path in count_paths[1:]))
        finally:
            count_store.close()

        disk_store = UndoSnapshotStore(max_entries=10, max_disk_bytes=1)
        disk_paths = []
        try:
            disk_store.append({"pdf_path": None, "pdf_size": 0, "kind": "too-old"})
            path, size = disk_store.write_document(document)
            disk_paths.append(path)
            disk_store.append({"pdf_path": path, "pdf_size": size})
            disk_store.append({"pdf_path": None, "pdf_size": 0, "kind": "metadata"})
            self.assertEqual(len(disk_store.entries), 3)
            self.assertTrue(os.path.exists(disk_paths[0]))

            path, size = disk_store.write_document(document)
            disk_paths.append(path)
            disk_store.append({"pdf_path": path, "pdf_size": size})
            self.assertEqual(len(disk_store.entries), 2)
            self.assertFalse(os.path.exists(disk_paths[0]))
            self.assertTrue(os.path.exists(disk_paths[1]))
            self.assertEqual(disk_store.entries[0]["kind"], "metadata")
        finally:
            disk_store.close()
            document.close()

    def test_crop_rectangles_and_page_indices_remap_after_deletions(self):
        crop_info = {
            "view_mode": "odd_even",
            "rects": {
                0: QRectF(1, 2, 30, 40),
                1: QRectF(5, 6, 30, 40),
                2: QRectF(9, 10, 30, 40),
                3: QRectF(13, 14, 30, 40),
            },
            "image_dims": [(100, 200), (110, 210), (120, 220), (130, 230)],
        }

        remapped = remap_crop_info_after_deletions(crop_info, {1})

        self.assertEqual(list(remapped["rects"]), [0, 1, 2])
        self.assertEqual(remapped["rects"][1], QRectF(9, 10, 30, 40))
        self.assertEqual(remapped["image_dims"], [(100, 200), (120, 220), (130, 230)])
        self.assertEqual(crop_info["rects"][1], QRectF(5, 6, 30, 40))
        self.assertEqual(remap_page_indices_after_deletions({0, 2, 3}, {1}), {0, 1, 2})

    def test_save_worker_preserves_crop_and_whiteout_original_pages_after_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "source.pdf")
            output_path = os.path.join(directory, "output.pdf")
            manifest_path = os.path.join(directory, "output.json")
            document = fitz.open()
            for _ in range(4):
                document.new_page(width=600, height=800)
            document.save(source_path)
            document.delete_page(1)
            pdf_bytes = document.tobytes()
            document.close()

            original_crop = {
                "view_mode": "all",
                "rects": {
                    0: QRectF(10, 10, 500, 700),
                    1: QRectF(10, 10, 500, 700),
                    2: QRectF(10, 10, 500, 700),
                    3: QRectF(10, 10, 500, 700),
                },
                "image_dims": [(600, 800)] * 4,
            }
            crop_info = remap_crop_info_after_deletions(original_crop, {1})
            worker = SaveWorker(
                pdf_bytes,
                output_path,
                crop_info=crop_info,
                source_path=source_path,
                manifest_path=manifest_path,
                page_map=[0, 2, 3],
                original_page_count=4,
                whiteouts=[
                    {"original_page": 2, "rect": [1, 2, 3, 4]},
                    {"original_page": 3, "rect": [5, 6, 7, 8]},
                ],
            )

            worker.run()

            with open(manifest_path, encoding="utf-8") as file_handle:
                manifest = json.load(file_handle)
            self.assertEqual(manifest["deleted_original_pages"], [2])
            self.assertEqual(
                [(item["output_page"], item["original_page"]) for item in manifest["crops"]],
                [(1, 1), (2, 3), (3, 4)],
            )
            self.assertEqual(
                [(item["output_page"], item["original_page"]) for item in manifest["whiteouts"]],
                [(2, 3)],
            )


class ViewerInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.show_maximized_patch = patch("pycroppdf.main_window.PDFViewer.showMaximized")
        self.show_maximized_patch.start()
        self.viewer = PDFViewer()

    def tearDown(self):
        self.viewer.threadpool.waitForDone()
        self.viewer.close()
        self.viewer.deleteLater()
        self.app.processEvents()
        self.show_maximized_patch.stop()

    def _wait_for_processing(self, timeout=20):
        deadline = time.time() + timeout
        while self.viewer.is_processing and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertFalse(self.viewer.is_processing)

    def test_ctrl_and_shift_thumbnail_selection(self):
        self.viewer.handleThumbnailSelection(2, Qt.KeyboardModifier.NoModifier)
        self.viewer.handleThumbnailSelection(4, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.viewer.selected_pages, {2, 4})

        self.viewer.handleThumbnailSelection(6, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(self.viewer.selected_pages, {4, 5, 6})

        self.viewer.handleThumbnailSelection(
            2,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        )
        self.assertEqual(self.viewer.selected_pages, {2, 3, 4, 5, 6})

    def test_thumbnail_preview_click_does_not_select_and_checkbox_can_deselect(self):
        thumbnail = ThumbnailWidget(2, QImage(80, 120, QImage.Format.Format_RGB888))
        previews = []
        selections = []
        thumbnail.previewRequested.connect(previews.append)
        thumbnail.selectionRequested.connect(
            lambda page_num, modifiers, toggle: selections.append((page_num, modifiers, toggle))
        )

        QTest.mouseClick(thumbnail.label, Qt.MouseButton.LeftButton)
        self.assertEqual(previews, [2])
        self.assertEqual(selections, [])

        thumbnail.checkbox.click()
        self.assertEqual(selections[-1][0], 2)
        self.assertTrue(selections[-1][2])
        thumbnail.deleteLater()

    def test_processing_cursor_is_entered_and_left_once(self):
        with (
            patch.object(QApplication, "setOverrideCursor") as set_cursor,
            patch.object(QApplication, "restoreOverrideCursor") as restore_cursor,
        ):
            self.viewer.setUIProcessing(True)
            self.viewer.setUIProcessing(True)
            self.viewer.setUIProcessing(False)

        set_cursor.assert_called_once()
        restore_cursor.assert_called_once()

    def test_cover_render_finishes_and_undo_restores_document(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "source.pdf")
            document = fitz.open()
            for page_number in range(2):
                page = document.new_page(width=300, height=400)
                page.insert_text((40, 40), f"Page {page_number + 1}")
            document.save(source_path)
            document.close()

            self.viewer.loadPDF(source_path)
            self._wait_for_processing()
            self.viewer.applyCover(QRectF(10, 10, 40, 30), [0, 1])
            self._wait_for_processing()

            self.viewer.applyCover(QRectF(60, 60, 30, 20), [0])
            self._wait_for_processing()

            self.assertEqual(
                [operation["output_page"] for operation in self.viewer.cover_operations],
                [1, 2, 1],
            )
            self.assertEqual(
                self.viewer.statusBar().currentMessage(),
                "Cover added to page 1; existing covers remain.",
            )
            self.assertIsNone(QApplication.overrideCursor())

            self.viewer.undo()
            self._wait_for_processing()
            self.assertEqual(
                [operation["output_page"] for operation in self.viewer.cover_operations],
                [1, 2],
            )

            self.viewer.undo()
            self._wait_for_processing()
            self.assertEqual(self.viewer.cover_operations, [])
            self.assertIsNone(QApplication.overrideCursor())

    def test_cover_empty_scope_is_reported_without_starting_an_operation(self):
        document = fitz.open()
        document.new_page(width=300, height=400)
        self.viewer.pdf_doc = document
        self.viewer.page_map = [0]
        self.viewer.images = [QImage(300, 400, QImage.Format.Format_RGB888)]
        self.viewer.cover_scope_combo.setCurrentIndex(
            self.viewer.cover_scope_combo.findData("even")
        )

        with patch.object(self.viewer, "applyCover") as apply_cover:
            self.viewer.handleCoverRequest(QRectF(10, 10, 20, 20))

        apply_cover.assert_not_called()
        self.assertEqual(
            self.viewer.statusBar().currentMessage(),
            "The selected Cover scope contains no pages.",
        )

    def test_edit_operations_ignore_thumbnail_selection_and_follow_view_scope(self):
        document = fitz.open()
        for _ in range(3):
            document.new_page(width=300, height=400)
        self.viewer.pdf_doc = document
        self.viewer.page_map = [0, 1, 2]
        self.viewer.images = [QImage(300, 400, QImage.Format.Format_RGB888) for _ in range(3)]
        self.viewer.view_mode = "all"
        self.viewer.selected_pages = {1}
        self.viewer.single_view.setSelection(QRectF(10, 10, 200, 300))

        with patch.object(self.viewer, "reloadImages"):
            self.viewer.cropSelection()
        self.assertEqual(set(self.viewer.active_crop_info["rects"]), {0, 1, 2})
        self.assertIsNone(self.viewer.undo_stack[-1]["pdf_path"])

        self.viewer.active_crop_info = None
        self.viewer.selected_pages = {0, 2}
        self.assertEqual(self.viewer._target_pages_for_rectangle_request(), [0, 1, 2])

        self.viewer.cover_scope_combo.setCurrentIndex(self.viewer.cover_scope_combo.findData("odd"))
        self.assertEqual(self.viewer._target_pages_for_rectangle_request(), [0, 2])
        with patch.object(self.viewer, "applyCover") as apply_cover:
            self.viewer.handleCoverRequest(QRectF(10, 10, 20, 20))
        apply_cover.assert_called_once_with(QRectF(10, 10, 20, 20), [0, 2])
        self.assertEqual(
            self.viewer.statusBar().currentMessage(),
            "Adding cover to 2 odd pages; existing covers remain.",
        )

        self.viewer.cover_scope_combo.setCurrentIndex(
            self.viewer.cover_scope_combo.findData("even")
        )
        self.assertEqual(self.viewer._target_pages_for_rectangle_request(), [1])

        self.viewer.cover_scope_combo.setCurrentIndex(self.viewer.cover_scope_combo.findData("all"))

        with patch.object(self.viewer, "applyCover") as apply_cover:
            self.viewer.handleCoverRequest(QRectF(10, 10, 20, 20))
        apply_cover.assert_called_once_with(QRectF(10, 10, 20, 20), [0, 1, 2])

        self.viewer.preview_page_num = 1
        self.assertEqual(self.viewer._target_pages_for_rectangle_request(), [0, 1, 2])
        self.viewer.cover_page_override_checkbox.setChecked(True)
        self.assertEqual(self.viewer._target_pages_for_rectangle_request(), [1])
        self.assertFalse(self.viewer.cover_scope_combo.isEnabled())
        with patch.object(self.viewer, "applyCover") as apply_cover:
            self.viewer.handleCoverRequest(QRectF(15, 15, 25, 25))
        apply_cover.assert_called_once_with(QRectF(15, 15, 25, 25), [1])
        self.assertEqual(
            self.viewer.statusBar().currentMessage(),
            "Adding cover to page 2; existing covers remain.",
        )
        self.viewer.cover_page_override_checkbox.setChecked(False)
        self.assertTrue(self.viewer.cover_scope_combo.isEnabled())
        self.viewer.single_view.setSelection(QRectF(10, 10, 200, 300))
        with patch.object(self.viewer, "reloadImages"):
            self.viewer.cropSelection()
        self.assertEqual(set(self.viewer.active_crop_info["rects"]), {0, 1, 2})

        self.viewer.active_crop_info = None
        self.viewer.showStackView()
        self.viewer.single_view.setSelection(QRectF(10, 10, 200, 300))
        self.viewer.togglePagePreview(1)
        self.viewer.crop_page_override_checkbox.setChecked(True)
        self.viewer.single_view.setSelection(QRectF(20, 20, 160, 240))
        with patch.object(self.viewer, "reloadImages"):
            self.viewer.cropSelection()
        self.assertEqual(set(self.viewer.active_crop_info["rects"]), {0, 1, 2})

    def test_undo_restores_document_crop_mapping_covers_and_selection(self):
        document = fitz.open()
        for _ in range(3):
            document.new_page(width=600, height=800)
        self.viewer.pdf_doc = document
        self.viewer.page_map = [0, 1, 2]
        self.viewer.active_crop_info = {
            "view_mode": "all",
            "rects": {2: QRectF(10, 10, 500, 700)},
            "image_dims": [(600, 800)] * 3,
        }
        self.viewer.cover_operations = [{"original_page": 3, "rect": [1, 2, 3, 4]}]
        self.viewer.rotation_operations = [{"original_page": 1, "angle": 1.5}]
        self.viewer.selected_pages = {1, 2}
        self.viewer.selection_anchor = 1
        self.viewer.preview_page_num = 2
        self.viewer.cover_page_override_checkbox.setChecked(True)
        snapshot = self.viewer.pushUndo()
        snapshot_path = snapshot["pdf_path"]
        self.assertNotIn("pdf_bytes", snapshot)
        self.assertTrue(os.path.isfile(snapshot_path))

        self.viewer.pdf_doc.delete_page(1)
        self.viewer.page_map = [0, 2]
        self.viewer.active_crop_info = None
        self.viewer.cover_operations = []
        self.viewer.rotation_operations = []
        self.viewer.selected_pages = set()
        self.viewer.preview_page_num = None
        self.viewer.cover_page_override_checkbox.setChecked(False)

        with patch.object(self.viewer, "reloadImages"):
            self.viewer.undo()

        self.assertFalse(os.path.exists(snapshot_path))

        self.assertEqual(len(self.viewer.pdf_doc), 3)
        self.assertEqual(self.viewer.page_map, [0, 1, 2])
        self.assertEqual(self.viewer.active_crop_info["rects"][2], QRectF(10, 10, 500, 700))
        self.assertEqual(self.viewer.cover_operations[0]["original_page"], 3)
        self.assertEqual(self.viewer.rotation_operations, [{"original_page": 1, "angle": 1.5}])
        self.assertEqual(self.viewer.selected_pages, {1, 2})
        self.assertEqual(self.viewer.selection_anchor, 1)
        self.assertEqual(self.viewer.preview_page_num, 2)
        self.assertTrue(self.viewer.cover_page_override_checkbox.isChecked())

    def test_cover_failure_restores_document_and_does_not_leave_undo_entry(self):
        document = fitz.open()
        document.new_page(width=300, height=400)
        self.viewer.pdf_doc = document
        self.viewer.page_map = [0]
        self.viewer.original_page_count = 1
        self.viewer.images = [QImage(300, 400, QImage.Format.Format_RGB888)]

        with (
            patch.object(
                self.viewer,
                "_refresh_dirty_state",
                side_effect=RuntimeError("synthetic cover failure"),
            ),
            patch.object(QMessageBox, "critical"),
        ):
            self.viewer.applyCover(QRectF(20, 20, 40, 30), [0])
        self._wait_for_processing()

        self.assertEqual(self.viewer.cover_operations, [])
        self.assertEqual(self.viewer.pdf_doc[0].get_drawings(), [])
        self.assertEqual(self.viewer.undo_stack, [])
        self.assertFalse(self.viewer.is_dirty)

    def test_page_deletion_failure_restores_document_and_page_state(self):
        document = fitz.open()
        for _ in range(3):
            document.new_page(width=300, height=400)
        self.viewer.pdf_doc = document
        self.viewer.page_map = [0, 1, 2]
        self.viewer.original_page_count = 3
        self.viewer.images = [QImage(300, 400, QImage.Format.Format_RGB888) for _ in range(3)]
        self.viewer.selected_pages = {1}

        with (
            patch.object(
                self.viewer,
                "_stack_canvas_dimensions",
                side_effect=[(300, 400), RuntimeError("synthetic deletion failure")],
            ),
            patch.object(QMessageBox, "critical"),
        ):
            self.viewer.deleteSelectedPages()
        self._wait_for_processing()

        self.assertEqual(len(self.viewer.pdf_doc), 3)
        self.assertEqual(self.viewer.page_map, [0, 1, 2])
        self.assertEqual(self.viewer.selected_pages, {1})
        self.assertEqual(len(self.viewer.images), 3)
        self.assertEqual(self.viewer.undo_stack, [])

    def test_clone_crop_info_does_not_share_rectangles(self):
        original = {
            "view_mode": "all",
            "rects": {0: QRectF(1, 2, 3, 4)},
            "image_dims": [(100, 200)],
        }
        cloned = clone_crop_info(original)
        cloned["rects"][0].setLeft(99)

        self.assertEqual(original["rects"][0].left(), 1)


if __name__ == "__main__":
    unittest.main()
