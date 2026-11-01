import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from pycroppdf.main_window import PDFViewer


class OverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _page(width=40, height=60):
        image = QImage(width, height, QImage.Format.Format_RGB888)
        image.fill(QColor("white"))
        return image

    def test_unique_marks_survive_one_hundred_pages_in_either_order(self):
        first = self._page()
        first.setPixelColor(2, 3, QColor("black"))
        last = self._page()
        last.setPixelColor(35, 55, QColor("black"))
        pages = [first, *[self._page() for _ in range(98)], last]

        forward = PDFViewer._compose_images(pages).toImage()
        reverse = PDFViewer._compose_images(reversed(pages)).toImage()

        self.assertEqual(forward.pixelColor(2, 3), QColor("black"))
        self.assertEqual(forward.pixelColor(35, 55), QColor("black"))
        self.assertEqual(forward, reverse)

    def test_mixed_page_sizes_remain_centered_on_a_shared_canvas(self):
        small = self._page(20, 30)
        small.setPixelColor(0, 0, QColor("black"))
        large = self._page(40, 60)
        large.setPixelColor(39, 59, QColor("black"))

        combined = PDFViewer._compose_images([small, large], (40, 60)).toImage()

        self.assertEqual(combined.pixelColor(10, 15), QColor("black"))
        self.assertEqual(combined.pixelColor(39, 59), QColor("black"))
        self.assertEqual(combined.pixelColor(0, 0), QColor("white"))

    def test_empty_or_missing_previews_do_not_produce_a_pixmap(self):
        self.assertIsNone(PDFViewer._compose_images([None, QImage()]))
