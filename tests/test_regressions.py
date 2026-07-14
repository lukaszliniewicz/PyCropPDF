import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PyQt6.QtCore import QMimeData, QPointF, QRectF, Qt, QUrl
from PyQt6.QtGui import QDropEvent, QImage
from PyQt6.QtWidgets import QApplication, QMessageBox

from pycroppdf.main_window import PDFViewer
from pycroppdf.provenance import sha256_bytes
from pycroppdf.workers import RenderAllPagesWorker, SaveWorker, scene_rect_to_pdf_coords


class CoordinateRegressionTests(unittest.TestCase):
    def test_full_visible_selection_preserves_existing_cropbox_for_every_rotation(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                document = fitz.open()
                page = document.new_page(width=600, height=800)
                page.set_cropbox(fitz.Rect(100, 100, 500, 700))
                page.set_rotation(rotation)
                preview = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))

                translated = scene_rect_to_pdf_coords(
                    QRectF(0, 0, preview.width, preview.height),
                    (preview.width, preview.height),
                    (preview.width, preview.height),
                    page,
                    page.cropbox,
                )

                self.assertEqual(tuple(translated), tuple(page.cropbox))
                document.close()

    def test_crop_preview_maps_back_to_the_existing_pending_crop(self):
        document = fitz.open()
        page = document.new_page(width=600, height=800)
        pending_crop = fitz.Rect(100, 100, 500, 700)
        preview = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), clip=pending_crop)

        translated = scene_rect_to_pdf_coords(
            QRectF(0, 0, preview.width, preview.height),
            (preview.width, preview.height),
            (preview.width, preview.height),
            page,
            pending_crop,
        )

        self.assertEqual(tuple(translated), tuple(pending_crop))
        document.close()

    def test_visual_mask_on_a_cropped_input_uses_the_visible_page_location(self):
        document = fitz.open()
        page = document.new_page(width=600, height=800)
        page.set_cropbox(fitz.Rect(100, 100, 500, 700))
        preview = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
        mask_rect = scene_rect_to_pdf_coords(
            QRectF(0, 0, 150, 150),
            (preview.width, preview.height),
            (preview.width, preview.height),
            page,
            page.cropbox,
        )
        page.draw_rect(mask_rect, color=(1, 1, 1), fill=(1, 1, 1), width=0)

        self.assertEqual(tuple(page.get_drawings()[-1]["rect"]), (100.0, 100.0, 200.0, 200.0))
        document.close()


class SaveAndProvenanceRegressionTests(unittest.TestCase):
    def test_manifest_uses_hash_of_loaded_source_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "source.pdf")
            output_path = os.path.join(directory, "output.pdf")
            manifest_path = os.path.join(directory, "output.json")

            original = fitz.open()
            original.new_page().insert_text((72, 72), "original")
            original.save(source_path)
            original.close()
            with open(source_path, "rb") as source_file:
                loaded_source = source_file.read()

            replacement = fitz.open()
            replacement.new_page().insert_text((72, 72), "replacement")
            replacement.save(source_path)
            replacement.close()

            SaveWorker(
                loaded_source,
                output_path,
                source_path=source_path,
                manifest_path=manifest_path,
                page_map=[0],
                original_page_count=1,
                source_sha256=sha256_bytes(loaded_source),
            ).run()

            with open(manifest_path, encoding="utf-8") as manifest_file:
                manifest = json.load(manifest_file)
            self.assertEqual(manifest["source"]["sha256"], sha256_bytes(loaded_source))
            self.assertNotEqual(manifest["source"]["sha256"], _sha256_file(source_path))
            output = fitz.open(output_path)
            self.assertIn("original", output[0].get_text())
            output.close()

    def test_save_failure_does_not_replace_an_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output_path = os.path.join(directory, "output.pdf")
            with open(output_path, "wb") as output_file:
                output_file.write(b"previous output")

            document = fitz.open()
            document.new_page(width=300, height=400)
            errors = []
            worker = SaveWorker(
                document.tobytes(),
                output_path,
                crop_info={"rects": {0: (999, 999, 1000, 1000)}},
            )
            worker.signals.error.connect(errors.append)
            worker.run()
            document.close()

            with open(output_path, "rb") as output_file:
                self.assertEqual(output_file.read(), b"previous output")
            self.assertTrue(errors)

    def test_manifest_records_secure_redactions(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "source.pdf")
            output_path = os.path.join(directory, "output.pdf")
            manifest_path = os.path.join(directory, "output.json")
            document = fitz.open()
            document.new_page()
            document.save(source_path)
            source_bytes = document.tobytes()
            document.close()

            SaveWorker(
                source_bytes,
                output_path,
                source_path=source_path,
                manifest_path=manifest_path,
                page_map=[0],
                original_page_count=1,
                redactions=[{"original_page": 1, "rect": [1, 2, 3, 4]}],
                source_sha256=_sha256_file(source_path),
            ).run()

            with open(manifest_path, encoding="utf-8") as manifest_file:
                manifest = json.load(manifest_file)
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["redactions"][0]["output_page"], 1)


class RenderingRegressionTests(unittest.TestCase):
    def test_render_worker_can_limit_work_to_selected_pages(self):
        document = fitz.open()
        for page_number in range(3):
            page = document.new_page(width=200, height=300)
            page.insert_text((30, 30), f"Page {page_number + 1}")
        pdf_bytes = document.tobytes()
        document.close()

        results = []
        worker = RenderAllPagesWorker(pdf_bytes, 3, page_numbers=[1])
        worker.signals.result.connect(results.append)
        worker.run()

        self.assertEqual([page_num for page_num, _image in results], [1])

    def test_parallel_render_returns_each_page_once(self):
        document = fitz.open()
        for _ in range(3):
            document.new_page(width=200, height=300)
        pdf_bytes = document.tobytes()
        document.close()

        results = []
        worker = RenderAllPagesWorker(pdf_bytes, 3)
        worker.signals.result.connect(results.append)
        worker.run()

        self.assertEqual(sorted(page_num for page_num, _image in results), [0, 1, 2])


class ViewerRegressionTests(unittest.TestCase):
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

    def test_redaction_removes_text_while_visual_masks_remain_non_destructive(self):
        document = fitz.open()
        page = document.new_page(width=300, height=400)
        page.insert_text((100, 100), "SECRET")
        self.viewer.pdf_doc = document
        self.viewer.images = [QImage(300, 400, QImage.Format.Format_RGB888)]
        self.viewer.page_map = [0]
        self.viewer.original_page_count = 1

        with patch.object(self.viewer, "reloadImages"):
            self.viewer.applyWhiteout(QRectF(70, 70, 100, 60), [0])
        self.assertIn("SECRET", page.get_text())

        with patch.object(self.viewer, "reloadImages"):
            self.viewer.undo()
        with patch.object(self.viewer, "reloadImages") as reload_images:
            self.viewer.applyRedaction(QRectF(70, 70, 100, 60), [0])
        reload_images.assert_called_once_with((0,))
        self.assertNotIn("SECRET", self.viewer.pdf_doc[0].get_text())

    def test_odd_even_crop_positions_survive_resize_and_view_changes(self):
        self.viewer.images = [
            QImage(400, 600, QImage.Format.Format_RGB888),
            QImage(400, 600, QImage.Format.Format_RGB888),
        ]
        self.viewer.updateOverlay()

        self.viewer.odd_view.setSelection(QRectF(20, 30, 100, 200))
        self.viewer.even_view.setSelection(QRectF(240, 50, 100, 200))
        self.viewer.odd_view.setSelection(QRectF(40, 70, 120, 180))

        odd_rect = self.viewer.odd_view.getSelectionRect()
        even_rect = self.viewer.even_view.getSelectionRect()
        self.assertEqual(odd_rect.topLeft(), QPointF(40, 70))
        self.assertEqual(even_rect.topLeft(), QPointF(240, 50))
        self.assertEqual(odd_rect.size(), even_rect.size())

        self.viewer.setViewMode("all")
        self.viewer.single_view.setSelection(QRectF(5, 10, 80, 90))
        self.viewer.setViewMode("odd_even")

        odd_rect = self.viewer.odd_view.getSelectionRect()
        even_rect = self.viewer.even_view.getSelectionRect()
        self.assertEqual(odd_rect.topLeft(), QPointF(40, 70))
        self.assertEqual(even_rect.topLeft(), QPointF(240, 50))
        self.assertEqual(odd_rect.size(), even_rect.size())
        self.assertEqual(odd_rect.size(), QRectF(0, 0, 80, 90).size())

        self.viewer.togglePagePreview(1)
        self.assertEqual(self.viewer.single_view.getSelectionRect(), even_rect)
        self.viewer.single_view.setSelection(QRectF(260, 60, 70, 85))
        self.assertEqual(self.viewer.odd_view.getSelectionRect().topLeft(), QPointF(40, 70))
        self.assertEqual(self.viewer.even_view.getSelectionRect().topLeft(), QPointF(260, 60))
        self.assertEqual(
            self.viewer.odd_view.getSelectionRect().size(),
            self.viewer.even_view.getSelectionRect().size(),
        )

    def test_tool_and_menu_actions_have_icons(self):
        controls = (
            self.viewer.open_action,
            self.viewer.save_action,
            self.viewer.undo_action,
            self.viewer.crop_tool_btn,
            self.viewer.whiteout_btn,
            self.viewer.redact_btn,
            self.viewer.crop_btn,
            self.viewer.delete_btn,
            self.viewer.save_btn,
        )
        self.assertTrue(all(not control.icon().isNull() for control in controls))

    def test_crop_selection_preserves_existing_cropbox_and_pending_crop_preview(self):
        document = fitz.open()
        page = document.new_page(width=600, height=800)
        page.set_cropbox(fitz.Rect(100, 100, 500, 700))
        self.viewer.pdf_doc = document
        self.viewer.images = [QImage(600, 900, QImage.Format.Format_RGB888)]
        self.viewer.page_map = [0]
        self.viewer.original_page_count = 1
        self.viewer.view_mode = "all"
        self.viewer.selected_pages = {0}
        self.viewer.single_view.setSelection(QRectF(0, 0, 600, 900))

        with patch.object(self.viewer, "reloadImages"):
            self.viewer.cropSelection()
        self.assertEqual(self.viewer.active_crop_info["rects"][0], (100.0, 100.0, 500.0, 700.0))

        self.viewer.images = [QImage(600, 900, QImage.Format.Format_RGB888)]
        self.viewer.single_view.setSelection(QRectF(0, 0, 600, 900))
        with patch.object(self.viewer, "reloadImages"):
            self.viewer.cropSelection()
        self.assertEqual(self.viewer.active_crop_info["rects"][0], (100.0, 100.0, 500.0, 700.0))

    def test_invalid_load_keeps_the_current_document_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            valid_path = os.path.join(directory, "valid.pdf")
            invalid_path = os.path.join(directory, "invalid.pdf")
            document = fitz.open()
            document.new_page()
            document.save(valid_path)
            document.close()
            with open(invalid_path, "wb") as invalid_file:
                invalid_file.write(b"not a PDF")

            current_document = fitz.open(valid_path)
            self.viewer.pdf_doc = current_document
            self.viewer.pdf_path = valid_path
            self.viewer.original_pdf_path = valid_path
            self.viewer.images = [QImage(100, 100, QImage.Format.Format_RGB888)]
            self.viewer.updateActionState()
            with patch.object(QMessageBox, "critical"):
                self.assertFalse(self.viewer.loadPDF(invalid_path))

            self.assertIs(self.viewer.pdf_doc, current_document)
            self.assertFalse(current_document.is_closed)
            self.assertEqual(len(self.viewer.images), 1)
            self.assertTrue(self.viewer.save_btn.isEnabled())
            self.viewer.pdf_doc = None
            current_document.close()

    def test_drop_is_rejected_while_an_operation_is_running(self):
        self.viewer.is_processing = True
        mime_data = QMimeData()
        mime_data.setUrls([QUrl.fromLocalFile("C:/tmp/replacement.pdf")])
        event = QDropEvent(
            QPointF(1, 1),
            Qt.DropAction.CopyAction,
            mime_data,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        with patch.object(self.viewer, "loadPDF") as load_pdf:
            self.viewer.dropEvent(event)

        load_pdf.assert_not_called()
        self.assertFalse(event.isAccepted())
        self.viewer.is_processing = False

    def test_delete_all_pages_is_rejected(self):
        document = fitz.open()
        document.new_page()
        self.viewer.pdf_doc = document
        self.viewer.images = [QImage(100, 100, QImage.Format.Format_RGB888)]
        self.viewer.page_map = [0]
        self.viewer.selected_pages = {0}

        with patch.object(QMessageBox, "warning") as warning:
            self.viewer.deleteSelectedPages()

        self.assertEqual(len(document), 1)
        warning.assert_called_once()


def _sha256_file(path):
    with open(path, "rb") as source_file:
        return hashlib.sha256(source_file.read()).hexdigest()


if __name__ == "__main__":
    unittest.main()
