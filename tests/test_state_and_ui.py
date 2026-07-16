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
from PyQt6.QtWidgets import QApplication

from pycroppdf.main_window import PDFViewer
from pycroppdf.state import (
    clone_crop_info,
    remap_crop_info_after_deletions,
    remap_page_indices_after_deletions,
)
from pycroppdf.workers import SaveWorker


class StateHelpersTests(unittest.TestCase):
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

    def test_whiteout_render_finishes_and_undo_restores_document(self):
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
            self.viewer.applyWhiteout(QRectF(10, 10, 40, 30), [0])
            self._wait_for_processing()

            self.assertEqual(len(self.viewer.whiteout_operations), 1)
            self.assertIsNone(QApplication.overrideCursor())

            self.viewer.undo()
            self._wait_for_processing()
            self.assertEqual(self.viewer.whiteout_operations, [])
            self.assertIsNone(QApplication.overrideCursor())

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
        self.assertIsNone(self.viewer.undo_stack[-1]["pdf_bytes"])

        self.viewer.active_crop_info = None
        self.viewer.selected_pages = {0, 2}
        self.assertEqual(self.viewer._target_pages_for_rectangle_request(), [0, 1, 2])

        with patch.object(self.viewer, "applyWhiteout") as apply_whiteout:
            self.viewer.handleWhiteoutRequest(QRectF(10, 10, 20, 20))
        apply_whiteout.assert_called_once_with(QRectF(10, 10, 20, 20), [0, 1, 2])

        with patch.object(self.viewer, "applyRedaction") as apply_redaction:
            self.viewer.handleRedactionRequest(QRectF(20, 20, 30, 30))
        apply_redaction.assert_called_once_with(QRectF(20, 20, 30, 30), [0, 1, 2])

        self.viewer.preview_page_num = 1
        self.assertEqual(self.viewer._target_pages_for_rectangle_request(), [0, 1, 2])
        self.viewer.single_view.setSelection(QRectF(10, 10, 200, 300))
        with patch.object(self.viewer, "reloadImages"):
            self.viewer.cropSelection()
        self.assertEqual(set(self.viewer.active_crop_info["rects"]), {0, 1, 2})

    def test_undo_restores_document_crop_mapping_whiteouts_and_selection(self):
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
        self.viewer.whiteout_operations = [{"original_page": 3, "rect": [1, 2, 3, 4]}]
        self.viewer.selected_pages = {1, 2}
        self.viewer.selection_anchor = 1
        self.viewer.preview_page_num = 2
        self.viewer.pushUndo()

        self.viewer.pdf_doc.delete_page(1)
        self.viewer.page_map = [0, 2]
        self.viewer.active_crop_info = None
        self.viewer.whiteout_operations = []
        self.viewer.selected_pages = set()
        self.viewer.preview_page_num = None

        with patch.object(self.viewer, "reloadImages"):
            self.viewer.undo()

        self.assertEqual(len(self.viewer.pdf_doc), 3)
        self.assertEqual(self.viewer.page_map, [0, 1, 2])
        self.assertEqual(self.viewer.active_crop_info["rects"][2], QRectF(10, 10, 500, 700))
        self.assertEqual(self.viewer.whiteout_operations[0]["original_page"], 3)
        self.assertEqual(self.viewer.selected_pages, {1, 2})
        self.assertEqual(self.viewer.selection_anchor, 1)
        self.assertEqual(self.viewer.preview_page_num, 2)

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
