import copy
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf as fitz
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

from pycroppdf.main_window import PDFViewer
from pycroppdf.preview_images import PreviewFrame, PreviewLayout
from pycroppdf.provenance import sha256_bytes
from pycroppdf.raster_cache import RasterInfo
from pycroppdf.rotation import rotate_pdf_bytes
from pycroppdf.workers import AutoDeskewWorker


class OperationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch.object(PDFViewer, "showMaximized"):
            self.viewer = PDFViewer()
        document = fitz.open()
        for index in range(2):
            document.new_page(width=240, height=320).insert_text(
                (30, 60), f"Original page {index + 1}"
            )
        self.viewer.pdf_doc = document
        self.viewer.page_map = [0, 1]
        self.viewer.original_page_count = 2
        self.viewer.source_sha256 = sha256_bytes(document.tobytes(no_new_id=True))
        self.viewer.images = []
        for page in document:
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
            self.viewer.images.append(
                QImage(
                    pixmap.samples,
                    pixmap.width,
                    pixmap.height,
                    pixmap.stride,
                    QImage.Format.Format_RGB888,
                ).copy()
            )
        self.viewer.updateThumbnails()
        self.viewer.updateOverlay()
        self.viewer.updateActionState()
        self.viewer.resize(800, 600)
        self.viewer.show()
        self.viewer.activateWindow()
        self.app.processEvents()

    def tearDown(self):
        if self.viewer.is_processing:
            self.viewer.cancelOperation()
            self.viewer.threadpool.waitForDone()
            self.viewer.setUIProcessing(False)
        with patch.object(self.viewer, "_confirm_discard_changes", return_value=True):
            self.viewer.close()
        self.viewer.deleteLater()
        self.app.processEvents()

    def _render_without_dispatch(self):
        with patch.object(self.viewer.threadpool, "start") as start:
            self.viewer.reloadImages()
        return start.call_args.args[0]

    def _deskew_without_dispatch(self):
        worker = AutoDeskewWorker(
            self.viewer.pdf_doc.tobytes(no_new_id=True), [0, 1], max_workers=1
        )
        with patch.object(self.viewer.threadpool, "start"):
            self.viewer._start_rotation_worker(worker, "Detecting skew...")
        return worker

    def _state(self):
        return {
            "pdf": self.viewer.pdf_doc.tobytes(no_new_id=True),
            "page_map": list(self.viewer.page_map),
            "source_sha256": self.viewer.source_sha256,
            "crops": copy.deepcopy(self.viewer.active_crop_info),
            "covers": copy.deepcopy(self.viewer.cover_operations),
            "rotations": copy.deepcopy(self.viewer.rotation_operations),
            "selected": set(self.viewer.selected_pages),
            "dirty": self.viewer.is_dirty,
            "undo_count": len(self.viewer.undo_stack),
        }

    def test_cancel_render_preserves_edits_and_retry_restores_coordinate_tools(self):
        self.viewer.active_crop_info = {
            "rects": {0: (20.0, 20.0, 220.0, 280.0)},
            "image_dims": [(360, 480)] * 2,
            "view_mode": "all",
        }
        self.viewer.is_dirty = True
        self.viewer.selected_pages = {1}
        before = self._state()
        worker = self._render_without_dispatch()

        self.viewer.cancel_operation_btn.click()
        self.assertTrue(worker.cancel_token.is_cancelled)
        self.assertTrue(self.viewer.is_processing)
        self.assertFalse(self.viewer.cancel_operation_btn.isEnabled())
        worker.run()

        self.assertFalse(self.viewer.is_processing)
        self.assertEqual(self._state(), before)
        self.assertTrue(self.viewer.retry_previews_btn.isEnabled())
        self.assertFalse(self.viewer.crop_tool_btn.isEnabled())
        self.assertFalse(self.viewer.cover_tool_btn.isEnabled())
        self.assertFalse(self.viewer.single_view.isEnabled())
        self.assertTrue(self.viewer.save_btn.isEnabled())

        with patch.object(self.viewer.threadpool, "start") as start:
            self.viewer.retry_previews_btn.click()
        retry_worker = start.call_args.args[0]
        retry_worker.run()

        self.assertFalse(self.viewer.is_processing)
        self.assertFalse(self.viewer._previews_incomplete)
        self.assertTrue(self.viewer.crop_tool_btn.isEnabled())
        self.assertTrue(self.viewer.single_view.isEnabled())
        self.assertTrue(self.viewer.retry_previews_btn.isHidden())
        self.assertIn("Ready", self.viewer.statusBar().currentMessage())
        self.assertEqual(self._state(), before)

    def test_cancelled_deskew_discards_queued_result_and_only_its_reserved_undo(self):
        self.viewer.pushUndo(include_pdf=False)
        before = self._state()
        original_document = self.viewer.pdf_doc
        worker = self._deskew_without_dispatch()
        operation_id = self.viewer._operation_id
        self.assertEqual(len(self.viewer.undo_stack), before["undo_count"])
        self.assertIsNotNone(self.viewer._rotation_pending_snapshot)
        self.viewer.cancelOperation()

        late_result = {
            "pdf_bytes": rotate_pdf_bytes(before["pdf"], {0: 90}),
            "rotation_deltas": {0: 90},
            "undetected_pages": [],
        }
        worker.signals.result.emit(late_result)
        worker.signals.cancelled.emit()
        worker.signals.finished.emit()
        worker.signals.result.emit(late_result)

        self.assertIs(self.viewer.pdf_doc, original_document)
        self.assertEqual(self._state(), before)
        self.assertEqual(self.viewer._cancelled_operation_id, operation_id)
        self.assertFalse(self.viewer.is_processing)
        self.assertTrue(self.viewer.crop_tool_btn.isEnabled())
        self.assertIn("document was not changed", self.viewer.statusBar().currentMessage())

    def test_old_worker_cannot_change_a_retry_generation(self):
        old_worker = self._render_without_dispatch()
        self.viewer.cancelOperation()
        old_worker.run()
        retry_worker = self._render_without_dispatch()
        current_id = self.viewer._operation_id
        current_images = self.viewer.images
        old_worker.signals.progress.emit(10, 10)
        old_worker.signals.result.emit(
            (0, RasterInfo(old_worker.generation, 0, 360, 480, 1, 1, 3, 1, 1, 3))
        )
        # Even a payload naming the current generation must be rejected when
        # delivered by an earlier operation's signal connection.
        old_worker.signals.prepared.emit(
            PreviewLayout(retry_worker.generation, ((9000, 9000),) * 2)
        )
        old_worker.signals.preview_ready.emit(
            PreviewFrame(retry_worker.generation, (9000, 9000), True)
        )
        # A current operation ID cannot admit an obsolete generation either.
        self.viewer.previewLayoutPrepared(
            current_id, PreviewLayout(old_worker.generation, ((9000, 9000),) * 2)
        )
        self.viewer.previewReady(
            current_id, PreviewFrame(old_worker.generation, (9000, 9000), True)
        )
        old_worker.signals.cancelled.emit()
        old_worker.signals.finished.emit()

        self.assertTrue(self.viewer.is_processing)
        self.assertEqual(self.viewer._operation_id, current_id)
        self.assertEqual(self.viewer.operation_progress.value(), 0)
        self.assertEqual(self.viewer.pages_rendered, 0)
        self.assertIs(self.viewer.images, current_images)
        self.assertIsNone(self.viewer._preview_frame)
        self.assertFalse(any(self.viewer._page_preview_ready(page) for page in range(2)))
        retry_worker.run()
        self.assertFalse(self.viewer.is_processing)
        self.assertFalse(self.viewer._previews_incomplete)

    def test_cancel_error_and_no_angle_preserve_full_undo_history(self):
        for outcome in ("cancel", "error", "no-angle"):
            with self.subTest(outcome=outcome):
                while len(self.viewer.undo_stack) < self.viewer.undo_store.max_entries:
                    self.viewer.pushUndo(include_pdf=False)
                before = self._state()
                original_entries = tuple(map(id, self.viewer.undo_stack))
                worker = self._deskew_without_dispatch()

                if outcome == "cancel":
                    self.viewer.cancelOperation()
                    worker.run()
                elif outcome == "error":
                    with patch.object(QMessageBox, "critical"):
                        worker.signals.error.emit("Expected detection failure")
                    worker.signals.finished.emit()
                else:
                    worker.signals.result.emit({"rotation_deltas": {}, "undetected_pages": [0, 1]})
                    worker.signals.finished.emit()

                self.assertEqual(self._state(), before)
                self.assertEqual(tuple(map(id, self.viewer.undo_stack)), original_entries)

    def test_cancelling_at_undo_disk_limit_preserves_the_existing_snapshot_file(self):
        self.viewer.undo_store.max_disk_bytes = 1
        previous = self.viewer.pushUndo()
        before = self._state()
        worker = self._deskew_without_dispatch()
        self.viewer.cancelOperation()
        worker.run()

        self.assertEqual(self._state(), before)
        self.assertIs(self.viewer.undo_stack[0], previous)
        self.assertTrue(os.path.isfile(previous["pdf_path"]))

    def test_successful_edit_evicts_oldest_undo_only_at_commit(self):
        while len(self.viewer.undo_stack) < self.viewer.undo_store.max_entries:
            self.viewer.pushUndo(include_pdf=False)
        original_entries = tuple(map(id, self.viewer.undo_stack))
        worker = self._deskew_without_dispatch()
        self.assertEqual(tuple(map(id, self.viewer.undo_stack)), original_entries)

        rotated = rotate_pdf_bytes(self.viewer.pdf_doc.tobytes(no_new_id=True), {0: 90})
        with patch.object(self.viewer.threadpool, "start") as start:
            worker.signals.result.emit(
                {"pdf_bytes": rotated, "rotation_deltas": {0: 90}, "undetected_pages": []}
            )
            worker.signals.finished.emit()
        start.call_args.args[0].run()

        self.assertEqual(len(self.viewer.undo_stack), self.viewer.undo_store.max_entries)
        self.assertEqual(tuple(map(id, self.viewer.undo_stack[:-1])), original_entries[1:])
        self.assertTrue(os.path.isfile(self.viewer.undo_stack[-1]["pdf_path"]))
        self.assertEqual(self.viewer.pdf_doc[0].rotation, 90)

    def test_render_error_offers_retry_and_is_not_reported_as_cancelled(self):
        worker = self._render_without_dispatch()
        self.viewer.cancelOperation()
        with patch.object(QMessageBox, "critical") as critical:
            worker.signals.error.emit("unexpected native failure")
        worker.signals.finished.emit()

        critical.assert_called_once()
        self.assertFalse(self.viewer.is_processing)
        self.assertTrue(self.viewer.retry_previews_btn.isEnabled())
        self.assertTrue(self.viewer.save_btn.isEnabled())
        self.assertIn("failed", self.viewer.statusBar().currentMessage())
        self.assertNotIn("cancelled", self.viewer.statusBar().currentMessage())
        self._render_without_dispatch().run()

    def test_escape_requests_cancellation(self):
        worker = self._render_without_dispatch()
        self.app.processEvents()
        QTest.keyClick(self.viewer, Qt.Key.Key_Escape)
        self.assertTrue(worker.cancel_token.is_cancelled)
        worker.run()
        self.assertFalse(self.viewer.is_processing)

    def test_close_cancels_then_respects_the_unsaved_changes_confirmation(self):
        self.viewer.is_dirty = True
        worker = self._render_without_dispatch()
        with (
            patch.object(self.viewer, "_confirm_discard_changes", return_value=False) as confirm,
            patch.object(QMessageBox, "information") as information,
        ):
            self.assertFalse(self.viewer.close())
            self.assertTrue(worker.cancel_token.is_cancelled)
            self.assertFalse(self.viewer.pdf_doc.is_closed)
            worker.run()
            self.app.processEvents()
            confirm.assert_called_once()
            information.assert_not_called()
            self.assertTrue(self.viewer.isVisible())
            self.assertFalse(self.viewer.pdf_doc.is_closed)
            self.assertTrue(self.viewer.retry_previews_btn.isEnabled())
            self.assertIn("cancelled", self.viewer.statusBar().currentMessage())

    def test_late_cancel_does_not_roll_back_committed_deskew(self):
        worker = self._deskew_without_dispatch()
        rotated = rotate_pdf_bytes(self.viewer.pdf_doc.tobytes(), {0: 90})
        with patch.object(self.viewer.threadpool, "start") as start:
            worker.signals.result.emit(
                {"pdf_bytes": rotated, "rotation_deltas": {0: 90}, "undetected_pages": []}
            )
            self.assertFalse(self.viewer.cancelOperation())
            worker.signals.finished.emit()

        self.assertEqual(self.viewer.pdf_doc[0].rotation, 90)
        self.assertEqual(len(self.viewer.undo_stack), 1)
        render_worker = start.call_args.args[0]
        self.viewer.cancelOperation()
        render_worker.run()
        self.assertEqual(self.viewer.pdf_doc[0].rotation, 90)
        self.assertEqual(len(self.viewer.undo_stack), 1)
        self.assertTrue(self.viewer.undo_btn.isEnabled())

    def test_save_remains_non_cancellable(self):
        self.viewer._processing_kind = "save"
        self.viewer.setUIProcessing(True)
        self.assertFalse(self.viewer.cancelOperation())
        self.assertFalse(self.viewer.cancel_operation_action.isEnabled())
        self.assertTrue(self.viewer.cancel_operation_btn.isHidden())
        self.viewer.processingCancelled(self.viewer._operation_id)
        self.assertFalse(self.viewer._previews_incomplete)
        with patch.object(QMessageBox, "information") as information:
            self.assertFalse(self.viewer.close())
        information.assert_called_once()
        self.assertFalse(self.viewer._close_after_processing)
        self.viewer.setUIProcessing(False)
