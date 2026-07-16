import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PyQt6.QtCore import QEvent, QMimeData, QPointF, QRectF, QSize, QSizeF, Qt, QUrl
from PyQt6.QtGui import QColor, QDropEvent, QImage, QKeyEvent
from PyQt6.QtWidgets import QApplication, QMessageBox, QToolButton

from pycroppdf.main_window import TOOLBAR_CONTROL_HEIGHT, PDFViewer
from pycroppdf.provenance import sha256_bytes
from pycroppdf.workers import (
    RenderAllPagesWorker,
    SaveWorker,
    pdf_rect_to_scene_coords,
    scene_rect_to_pdf_coords,
)


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

    def test_stack_and_page_scene_coordinates_round_trip_through_pdf_space(self):
        document = fitz.open()
        page = document.new_page(width=300, height=400)
        preview = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
        stack_canvas = (700, 900)
        stack_rect = QRectF(170, 190, 240, 300)

        pdf_rect = scene_rect_to_pdf_coords(
            stack_rect,
            (preview.width, preview.height),
            stack_canvas,
            page,
            page.cropbox,
        )
        page_rect = pdf_rect_to_scene_coords(
            pdf_rect,
            (preview.width, preview.height),
            (preview.width, preview.height),
            page,
            page.cropbox,
        )
        round_trip = scene_rect_to_pdf_coords(
            page_rect,
            (preview.width, preview.height),
            (preview.width, preview.height),
            page,
            page.cropbox,
        )

        for actual, expected in zip(round_trip, pdf_rect, strict=True):
            self.assertAlmostEqual(actual, expected, places=5)
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
            self.assertEqual(manifest["schema_version"], 3)
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

    def test_cover_is_non_destructive_and_redaction_is_not_exposed(self):
        document = fitz.open()
        page = document.new_page(width=300, height=400)
        page.insert_text((100, 100), "SECRET")
        self.viewer.pdf_doc = document
        self.viewer.images = [QImage(300, 400, QImage.Format.Format_RGB888)]
        self.viewer.page_map = [0]
        self.viewer.original_page_count = 1

        with patch.object(self.viewer, "reloadImages"):
            self.viewer.applyCover(QRectF(70, 70, 100, 60), [0])
        self.assertIn("SECRET", page.get_text())
        self.assertFalse(hasattr(self.viewer, "redact_btn"))
        self.assertFalse(hasattr(self.viewer, "applyRedaction"))

    def test_fast_save_uses_safe_minimal_garbage_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            document = fitz.open()
            document.new_page(width=300, height=400)
            self.viewer.pdf_doc = document
            self.viewer.pdf_path = os.path.join(directory, "source.pdf")
            self.viewer.original_pdf_path = self.viewer.pdf_path
            self.viewer.save_directory = directory
            self.viewer.save_filename = "output.pdf"

            with (
                patch("pycroppdf.main_window.SaveWorker") as save_worker,
                patch.object(self.viewer.threadpool, "start"),
            ):
                self.viewer.fast_save_action.setChecked(True)
                self.viewer.savePDF()
                fast_kwargs = save_worker.call_args.kwargs
                self.assertFalse(fast_kwargs["deflate"])
                self.assertEqual(fast_kwargs["garbage"], 1)

                self.viewer.setUIProcessing(False)
                self.viewer.fast_save_action.setChecked(False)
                self.viewer.savePDF()
                compressed_kwargs = save_worker.call_args.kwargs
                self.assertTrue(compressed_kwargs["deflate"])
                self.assertEqual(compressed_kwargs["garbage"], 2)

                self.viewer.setUIProcessing(False)

    def test_cover_uses_the_chosen_color(self):
        document = fitz.open()
        document.new_page(width=300, height=400)
        self.viewer.pdf_doc = document
        self.viewer.images = [QImage(300, 400, QImage.Format.Format_RGB888)]
        self.viewer.page_map = [0]
        self.viewer.original_page_count = 1

        self.viewer.onColorPicked(QColor(235, 70, 45))
        with patch.object(self.viewer, "reloadImages"):
            self.viewer.applyCover(QRectF(30, 30, 60, 60), [0])

        pixmap = self.viewer.pdf_doc[0].get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
        red, green, blue = pixmap.pixel(50, 50)[:3]
        self.assertGreater(red, 220)
        self.assertLess(green, 90)
        self.assertLess(blue, 70)
        self.assertEqual(self.viewer.cover_operations[-1]["color"], [0.9216, 0.2745, 0.1765])

    def test_cover_works_inside_a_crop_on_a_rotated_page_and_undo_preserves_crop(self):
        document = fitz.open()
        page = document.new_page(width=400, height=600)
        page.set_rotation(90)
        self.viewer.pdf_doc = document
        self.viewer.page_map = [0]
        self.viewer.original_page_count = 1
        self.viewer.images = [QImage(450, 300, QImage.Format.Format_RGB888)]
        crop_rect = (50.0, 60.0, 350.0, 510.0)
        self.viewer.active_crop_info = {
            "view_mode": "all",
            "rects": {0: crop_rect},
            "image_dims": [(450, 300)],
        }
        self.viewer.setViewMode("all")
        self.viewer._refresh_dirty_state()
        self.viewer.updateActionState()

        self.assertTrue(self.viewer.cover_tool_btn.isEnabled())
        self.viewer.rotation_angle_spin.setValue(5.0)
        self.viewer.previewRotation()
        self.assertFalse(self.viewer.cover_tool_btn.isEnabled())
        self.viewer.discardRotationPreview()
        self.assertTrue(self.viewer.cover_tool_btn.isEnabled())

        with patch.object(QMessageBox, "warning") as warning:
            self.viewer.cover_tool_btn.click()
        warning.assert_not_called()
        self.assertEqual(self.viewer.active_tool, "cover")
        self.assertIn("active crop remains applied", self.viewer.statusBar().currentMessage())

        scene_rect = QRectF(45, 30, 90, 60)
        expected_pdf_rect = self.viewer._scene_rect_to_pdf_rect(scene_rect, 0)
        self.viewer.cover_color = (0.0, 0.0, 0.0)
        with patch.object(self.viewer, "reloadImages"):
            self.viewer.handleCoverRequest(scene_rect)

        self.assertEqual(self.viewer.active_crop_info["rects"][0], crop_rect)
        self.assertEqual(len(self.viewer.cover_operations), 1)
        operation_rect = fitz.Rect(self.viewer.cover_operations[0]["rect"])
        for actual, expected in zip(operation_rect, expected_pdf_rect, strict=True):
            self.assertAlmostEqual(actual, expected, places=3)
        restored_scene_rect = self.viewer._pdf_rect_to_scene_rect(operation_rect, 0)
        for actual, expected in zip(
            (
                restored_scene_rect.x(),
                restored_scene_rect.y(),
                restored_scene_rect.width(),
                restored_scene_rect.height(),
            ),
            (scene_rect.x(), scene_rect.y(), scene_rect.width(), scene_rect.height()),
            strict=True,
        ):
            self.assertAlmostEqual(actual, expected, places=3)
        self.assertTrue(self.viewer.pdf_doc[0].get_drawings())

        with patch.object(self.viewer, "reloadImages"):
            self.viewer.undo()
        self.assertEqual(self.viewer.active_crop_info["rects"][0], crop_rect)
        self.assertEqual(self.viewer.cover_operations, [])
        self.assertEqual(self.viewer.pdf_doc[0].get_drawings(), [])

    def test_stack_crop_maps_to_page_preview_and_back_after_deletion(self):
        document = fitz.open()
        for width, height in ((400, 600), (300, 500), (200, 400)):
            document.new_page(width=width, height=height)
        self.viewer.pdf_doc = document
        self.viewer.images = [
            QImage(400, 600, QImage.Format.Format_RGB888),
            QImage(300, 500, QImage.Format.Format_RGB888),
            QImage(200, 400, QImage.Format.Format_RGB888),
        ]
        self.viewer.page_map = [0, 1, 2]
        self.viewer.original_page_count = 3
        self.viewer.updateOverlay()
        self.viewer.odd_view.setSelection(QRectF(60, 70, 120, 180))
        self.viewer.even_view.setSelection(QRectF(150, 110, 120, 180))

        self.viewer.togglePagePreview(1)
        preview_rect = self.viewer.single_view.getSelectionRect()
        stack_pdf_rect = self.viewer._scene_rect_to_pdf_rect(
            self.viewer.even_view.getSelectionRect(),
            1,
            self.viewer._stack_canvas_dimensions(),
        )
        preview_pdf_rect = self.viewer._scene_rect_to_pdf_rect(
            preview_rect,
            1,
            self.viewer._page_canvas_dimensions(1),
        )
        for actual, expected in zip(preview_pdf_rect, stack_pdf_rect, strict=True):
            self.assertAlmostEqual(actual, expected, places=5)

        self.viewer.selected_pages = {0}
        self.viewer.deleteSelectedPages()
        self.assertEqual(self.viewer.preview_page_num, 0)
        preview_rect = self.viewer.single_view.getSelectionRect()
        stack_pdf_rect = self.viewer._scene_rect_to_pdf_rect(
            self.viewer.odd_view.getSelectionRect(),
            0,
            self.viewer._stack_canvas_dimensions(),
        )
        preview_pdf_rect = self.viewer._scene_rect_to_pdf_rect(
            preview_rect,
            0,
            self.viewer._page_canvas_dimensions(0),
        )
        for actual, expected in zip(preview_pdf_rect, stack_pdf_rect, strict=True):
            self.assertAlmostEqual(actual, expected, places=5)

    def test_active_preview_and_page_selection_are_independent(self):
        document = fitz.open()
        for _ in range(3):
            document.new_page(width=300, height=400)
        self.viewer.pdf_doc = document
        self.viewer.images = [QImage(300, 400, QImage.Format.Format_RGB888) for _ in range(3)]
        self.viewer.page_map = [0, 1, 2]
        self.viewer.togglePagePreview(1)

        self.assertEqual(self.viewer.preview_page_num, 1)
        self.assertEqual(self.viewer.selected_pages, set())
        self.viewer.handleThumbnailSelection(1, Qt.KeyboardModifier.NoModifier, True)
        self.assertEqual(self.viewer.selected_pages, {1})
        self.viewer.handleThumbnailSelection(1, Qt.KeyboardModifier.NoModifier, True)
        self.assertEqual(self.viewer.selected_pages, set())
        self.assertEqual(self.viewer.preview_page_num, 1)
        self.viewer.view_stack_btn.click()
        self.assertIsNone(self.viewer.preview_page_num)

    def test_rotation_preview_requires_preview_and_can_be_discarded(self):
        document = fitz.open()
        document.new_page(width=400, height=600)
        self.viewer.pdf_doc = document
        self.viewer.images = [QImage(400, 600, QImage.Format.Format_RGB888)]
        self.viewer.setViewMode("all")

        with patch.object(self.viewer.threadpool, "start") as start:
            self.viewer.rotation_angle_spin.setValue(4.0)

        start.assert_not_called()
        self.assertEqual(self.viewer._rotation_preview_angle, 0.0)
        self.assertEqual(self.viewer.single_view.sceneRect().width(), 400)
        self.assertTrue(self.viewer.crop_btn.isEnabled())

        self.viewer.preview_rotation_btn.click()
        self.assertEqual(self.viewer._rotation_preview_angle, 4.0)
        self.assertEqual(self.viewer.pdf_doc[0].rotation, 0)
        self.assertGreater(self.viewer.single_view.sceneRect().width(), 400)
        self.assertFalse(self.viewer.crop_btn.isEnabled())

        self.viewer.rotation_angle_spin.setValue(5.0)
        self.assertEqual(self.viewer._rotation_preview_angle, 0.0)
        self.assertEqual(self.viewer.single_view.sceneRect().width(), 400)
        self.viewer.preview_rotation_btn.click()
        self.assertEqual(self.viewer._rotation_preview_angle, 5.0)

        self.viewer.discard_rotation_preview_btn.click()
        self.assertEqual(self.viewer._rotation_preview_angle, 0.0)
        self.assertEqual(self.viewer.rotation_angle_spin.value(), 0.0)
        self.assertEqual(self.viewer.single_view.sceneRect().width(), 400)
        self.assertTrue(self.viewer.crop_btn.isEnabled())

    def test_space_bar_pan_is_global_to_page_views(self):
        target = self.viewer.rotation_angle_spin
        QApplication.sendEvent(
            target,
            QKeyEvent(
                QEvent.Type.KeyPress,
                Qt.Key.Key_Space,
                Qt.KeyboardModifier.NoModifier,
            ),
        )
        self.assertTrue(all(view._pan for view in self._page_views()))
        QApplication.sendEvent(
            target,
            QKeyEvent(
                QEvent.Type.KeyRelease,
                Qt.Key.Key_Space,
                Qt.KeyboardModifier.NoModifier,
            ),
        )
        self.assertTrue(all(not view._pan for view in self._page_views()))

    def _page_views(self):
        return (self.viewer.single_view, self.viewer.odd_view, self.viewer.even_view)

    def test_odd_even_crop_positions_survive_resize_and_view_changes(self):
        document = fitz.open()
        document.new_page(width=400, height=600)
        document.new_page(width=400, height=600)
        self.viewer.pdf_doc = document
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

    def test_actions_use_custom_icons_and_auto_deskew_label(self):
        controls = (
            self.viewer.open_action,
            self.viewer.reload_original_action,
            self.viewer.save_action,
            self.viewer.undo_action,
            self.viewer.crop_tool_btn,
            self.viewer.cover_tool_btn,
            self.viewer.crop_btn,
            self.viewer.pick_cover_color_btn,
            self.viewer.preview_rotation_btn,
            self.viewer.discard_rotation_preview_btn,
            self.viewer.delete_btn,
            self.viewer.reload_original_btn,
            self.viewer.save_btn,
        )
        self.assertTrue(all(not control.icon().isNull() for control in controls))
        self.assertEqual(self.viewer.auto_deskew_btn.text(), "Auto deskew")
        image = self.viewer.cover_tool_btn.icon().pixmap(QSize(18, 18)).toImage()
        self.assertTrue(
            any(
                image.pixelColor(x, y).alpha() and image.pixelColor(x, y).red() > 230
                for x in range(image.width())
                for y in range(image.height())
            )
        )

    def test_sidebar_never_shows_a_horizontal_scrollbar(self):
        self.assertEqual(
            self.viewer.scroll_area.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )

    def test_rotation_scope_targets_stacks_and_previewed_pages(self):
        document = fitz.open()
        for _ in range(5):
            document.new_page()
        self.viewer.pdf_doc = document

        self.viewer.rotation_scope_combo.setCurrentIndex(
            self.viewer.rotation_scope_combo.findData("odd")
        )
        self.assertEqual(self.viewer._rotation_target_pages(), [0, 2, 4])
        self.viewer.rotation_scope_combo.setCurrentIndex(
            self.viewer.rotation_scope_combo.findData("even")
        )
        self.assertEqual(self.viewer._rotation_target_pages(), [1, 3])
        self.viewer.preview_page_num = 3
        self.viewer.crop_page_override_checkbox.setChecked(True)
        self.assertEqual(self.viewer._rotation_target_pages(), [1, 3])
        self.viewer.rotation_page_override_checkbox.setChecked(True)
        self.assertEqual(self.viewer._rotation_target_pages(), [3])

    def test_per_page_crop_override_can_differ_from_uniform_stack_size(self):
        document = fitz.open()
        document.new_page(width=400, height=600)
        document.new_page(width=400, height=600)
        self.viewer.pdf_doc = document
        self.viewer.images = [
            QImage(400, 600, QImage.Format.Format_RGB888),
            QImage(400, 600, QImage.Format.Format_RGB888),
        ]
        self.viewer.updateOverlay()

        self.viewer.odd_view.setSelection(QRectF(20, 30, 100, 200))
        self.viewer.even_view.setSelection(QRectF(240, 50, 70, 150))
        self.assertEqual(self.viewer.odd_view.getSelectionRect().size(), QSizeF(70, 150))
        self.assertEqual(self.viewer.even_view.getSelectionRect().size(), QSizeF(70, 150))

        self.viewer.togglePagePreview(0)
        self.viewer.crop_page_override_checkbox.setChecked(True)
        self.viewer.single_view.setSelection(QRectF(30, 40, 120, 180))

        self.assertIn(0, self.viewer.page_crop_overrides)
        self.assertEqual(self.viewer.odd_view.getSelectionRect().size(), QSizeF(70, 150))

        with patch.object(self.viewer, "reloadImages"):
            self.viewer.cropSelection()
        page_override = fitz.Rect(self.viewer.active_crop_info["rects"][0])
        stack_crop = fitz.Rect(self.viewer.active_crop_info["rects"][1])
        self.assertNotAlmostEqual(page_override.width, stack_crop.width)
        self.assertNotAlmostEqual(page_override.height, stack_crop.height)

    def test_edit_toolbars_share_one_contextual_row(self):
        document = fitz.open()
        document.new_page()
        self.viewer.pdf_doc = document
        self.viewer.images = [QImage(100, 100, QImage.Format.Format_RGB888)]
        self.viewer.updateActionState()

        self.assertFalse(self.viewer.crop_toolbar.isHidden())
        self.assertTrue(self.viewer.cover_toolbar.isHidden())
        self.assertTrue(self.viewer.rotation_toolbar.isHidden())

        self.viewer.cover_tool_btn.click()
        self.assertTrue(self.viewer.crop_toolbar.isHidden())
        self.assertFalse(self.viewer.cover_toolbar.isHidden())
        self.viewer.pick_cover_color_btn.click()
        self.assertTrue(all(view._tool == "pick_color" for view in self._page_views()))
        self.viewer.onColorPicked(QColor(20, 40, 60))
        self.assertTrue(all(view._tool == "cover" for view in self._page_views()))

        self.viewer.rotation_options_toggle_btn.click()
        self.assertEqual(self.viewer.active_tool, "rotate")
        self.assertTrue(self.viewer.crop_toolbar.isHidden())
        self.assertTrue(self.viewer.cover_toolbar.isHidden())
        self.assertFalse(self.viewer.rotation_toolbar.isHidden())

        self.viewer.crop_tool_btn.click()
        self.assertEqual(self.viewer.active_tool, "crop")
        self.assertFalse(self.viewer.crop_toolbar.isHidden())
        self.assertTrue(self.viewer.cover_toolbar.isHidden())
        self.assertTrue(self.viewer.rotation_toolbar.isHidden())

        toolbars = (
            self.viewer.crop_toolbar,
            self.viewer.cover_toolbar,
            self.viewer.rotation_toolbar,
        )
        self.assertEqual(sum(not toolbar.isHidden() for toolbar in toolbars), 1)

    def test_rotation_toolbar_and_preview_remain_available_with_an_active_crop(self):
        document = fitz.open()
        document.new_page(width=400, height=600)
        self.viewer.pdf_doc = document
        self.viewer.images = [QImage(300, 450, QImage.Format.Format_RGB888)]
        self.viewer.active_crop_info = {
            "view_mode": "all",
            "rects": {0: (50.0, 60.0, 350.0, 510.0)},
            "image_dims": [(400, 600)],
        }
        self.viewer.updateActionState()

        self.assertTrue(self.viewer.rotation_options_toggle_btn.isEnabled())
        with patch.object(QMessageBox, "warning") as warning:
            self.viewer.rotation_options_toggle_btn.click()
        warning.assert_not_called()
        self.assertEqual(self.viewer.active_tool, "rotate")
        self.assertFalse(self.viewer.rotation_toolbar.isHidden())
        self.assertTrue(self.viewer.rotate_left_btn.isEnabled())

        self.viewer.rotation_angle_spin.setValue(3.0)
        self.assertTrue(self.viewer.preview_rotation_btn.isEnabled())
        self.viewer.preview_rotation_btn.click()
        self.assertEqual(self.viewer._rotation_preview_angle, 3.0)
        self.assertIsNotNone(self.viewer.active_crop_info)

    def test_invalid_rotated_crop_result_does_not_replace_the_document(self):
        document = fitz.open()
        document.new_page(width=400, height=600)
        self.viewer.pdf_doc = document
        self.viewer.page_map = [0]
        self.viewer.original_page_count = 1
        self.viewer.active_crop_info = {
            "view_mode": "all",
            "rects": {0: (50.0, 60.0, 350.0, 510.0)},
            "image_dims": [(400, 600)],
        }
        self.viewer.pushUndo()
        self.viewer._rotation_undo_pushed = True
        self.viewer._operation_id = 1

        with patch.object(QMessageBox, "critical"):
            self.viewer.rotationFinished(
                1,
                {
                    "pdf_bytes": document.tobytes(),
                    "rotation_deltas": {0: 3.0},
                    "crop_rects": {1: (10.0, 10.0, 20.0, 20.0)},
                },
            )

        self.assertIs(self.viewer.pdf_doc, document)
        self.assertFalse(document.is_closed)
        self.assertEqual(self.viewer.rotation_operations, [])
        self.assertEqual(self.viewer.undo_stack, [])

    def test_page_only_controls_use_consistent_label(self):
        controls = (
            self.viewer.crop_page_override_checkbox,
            self.viewer.cover_page_override_checkbox,
            self.viewer.rotation_page_override_checkbox,
        )
        self.assertEqual({control.text() for control in controls}, {"This page only"})
        self.assertEqual(
            [
                self.viewer.cover_scope_combo.itemText(index)
                for index in range(self.viewer.cover_scope_combo.count())
            ],
            ["All", "Odd", "Even"],
        )

    def test_toolbar_controls_use_one_height(self):
        controls = (
            self.viewer.crop_tool_btn,
            self.viewer.cover_tool_btn,
            self.viewer.rotation_options_toggle_btn,
            self.viewer.view_stack_btn,
            self.viewer.delete_btn,
            self.viewer.undo_btn,
            self.viewer.reload_original_btn,
            self.viewer.save_btn,
            self.viewer.crop_btn,
            self.viewer.reset_crop_btn,
            self.viewer.crop_page_override_checkbox,
            self.viewer.cover_color_btn,
            self.viewer.pick_cover_color_btn,
            self.viewer.cover_scope_combo,
            self.viewer.cover_page_override_checkbox,
            self.viewer.cover_note_label,
            self.viewer.rotation_page_override_checkbox,
            self.viewer.rotation_scope_combo,
            self.viewer.rotate_left_btn,
            self.viewer.rotate_right_btn,
            self.viewer.rotation_angle_spin,
            self.viewer.preview_rotation_btn,
            self.viewer.discard_rotation_preview_btn,
            self.viewer.apply_rotation_btn,
            self.viewer.auto_deskew_btn,
        )
        self.assertEqual({control.height() for control in controls}, {TOOLBAR_CONTROL_HEIGHT})

    def test_primary_toolbar_keeps_save_visible_at_compact_width(self):
        document = fitz.open()
        document.new_page()
        self.viewer.pdf_doc = document
        self.viewer.images = [QImage(100, 100, QImage.Format.Format_RGB888)]
        self.viewer.updateActionState()
        self.viewer.rotation_options_toggle_btn.click()
        self.viewer.showNormal()
        self.viewer.resize(1100, 800)
        self.app.processEvents()

        self.assertTrue(self.viewer.save_btn.isVisible())
        for toolbar in (
            self.viewer.main_toolbar,
            self.viewer.crop_toolbar,
            self.viewer.rotation_toolbar,
        ):
            extension = toolbar.findChild(QToolButton, "qt_toolbar_ext_button")
            self.assertFalse(extension and extension.isVisible())

        self.viewer.cover_tool_btn.click()
        self.app.processEvents()
        self.assertTrue(self.viewer.save_btn.isVisible())
        extension = self.viewer.cover_toolbar.findChild(QToolButton, "qt_toolbar_ext_button")
        self.assertFalse(extension and extension.isVisible())

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

    def test_reload_original_discards_edits_and_resets_document_state(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "source.pdf")
            document = fitz.open()
            document.new_page().insert_text((72, 72), "ORIGINAL")
            document.save(source_path)
            document.close()

            with patch.object(self.viewer, "reloadImages"):
                self.assertTrue(self.viewer.loadPDF(source_path))
            self.viewer.pdf_doc[0].insert_text((72, 100), "SESSION EDIT")
            self.viewer.active_crop_info = {
                "view_mode": "all",
                "rects": {0: (20.0, 20.0, 500.0, 700.0)},
                "image_dims": [(600, 800)],
            }
            self.viewer.rotation_operations = [{"original_page": 1, "angle": 2.0}]
            self.viewer._refresh_dirty_state()

            with (
                patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.Discard,
                ) as question,
                patch.object(self.viewer, "reloadImages"),
            ):
                self.assertTrue(self.viewer.reloadOriginal())

            question.assert_called_once()
            self.assertIn("ORIGINAL", self.viewer.pdf_doc[0].get_text())
            self.assertNotIn("SESSION EDIT", self.viewer.pdf_doc[0].get_text())
            self.assertIsNone(self.viewer.active_crop_info)
            self.assertEqual(self.viewer.rotation_operations, [])
            self.assertEqual(self.viewer.cover_operations, [])
            self.assertEqual(self.viewer.page_map, [0])
            self.assertFalse(self.viewer.is_dirty)

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
