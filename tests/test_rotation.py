import json
import math
import os
import tempfile
import unittest
from unittest.mock import patch

import fitz

from pycroppdf.rotation import (
    deskew_available,
    deskew_pdf_bytes,
    detect_page_deskew_angle,
    recommended_deskew_workers,
    rotate_pdf_bytes,
    transform_crop_rects_for_rotations,
)
from pycroppdf.workers import AutoDeskewWorker, SaveWorker


class PageRotationTests(unittest.TestCase):
    @staticmethod
    def _source_pdf():
        document = fitz.open()
        page = document.new_page(width=200, height=100)
        page.insert_text((20, 30), "VECTOR TEXT")
        page.set_cropbox(fitz.Rect(10, 5, 190, 95))
        page.insert_link(
            {
                "kind": fitz.LINK_URI,
                "from": fitz.Rect(15, 15, 60, 35),
                "uri": "https://example.com",
            }
        )
        page.add_text_annot((70, 30), "note")
        pdf_bytes = document.tobytes()
        document.close()
        return pdf_bytes

    def test_quarter_turn_uses_native_page_rotation(self):
        output = fitz.open("pdf", rotate_pdf_bytes(self._source_pdf(), {0: 90}))
        page = output[0]

        self.assertEqual(page.rotation, 90)
        self.assertEqual(page.mediabox, fitz.Rect(0, 0, 200, 100))
        self.assertIn("VECTOR TEXT", page.get_text())
        self.assertEqual(len(page.get_links()), 1)
        self.assertEqual(len(list(page.annots() or ())), 1)
        output.close()

    def test_fine_rotation_preserves_vector_text_and_page_objects(self):
        source = fitz.open("pdf", self._source_pdf())
        original_media = fitz.Rect(source[0].mediabox)
        original_link = fitz.Rect(source[0].get_links()[0]["from"])
        source.close()

        output = fitz.open("pdf", rotate_pdf_bytes(self._source_pdf(), {0: 5.0}))
        page = output[0]

        self.assertEqual(page.rotation, 0)
        self.assertGreater(page.mediabox.width, original_media.width)
        self.assertGreater(page.mediabox.height, original_media.height)
        self.assertIn("VECTOR TEXT", page.get_text())
        self.assertNotEqual(page.get_links()[0]["from"], original_link)
        self.assertEqual(len(list(page.annots() or ())), 1)
        output.close()

    def test_existing_vector_cover_follows_a_later_fine_rotation(self):
        document = fitz.open()
        page = document.new_page(width=200, height=100)
        cover_rect = fitz.Rect(20, 20, 60, 40)
        page.draw_rect(cover_rect, color=(0, 0, 0), fill=(0, 0, 0), width=0)
        source_bytes = document.tobytes()
        document.close()

        output = fitz.open("pdf", rotate_pdf_bytes(source_bytes, {0: 5.0}))
        rotated_cover = fitz.Rect(output[0].get_drawings()[0]["rect"])

        self.assertNotEqual(rotated_cover, cover_rect)
        self.assertTrue(output[0].mediabox.contains(rotated_cover))
        self.assertGreater(rotated_cover.width, cover_rect.width)
        self.assertGreater(rotated_cover.height, cover_rect.height)
        output.close()

    def test_pending_crop_follows_fine_rotation_and_quarter_turns_keep_pdf_coordinates(self):
        document = fitz.open("pdf", self._source_pdf())
        document.new_page(width=200, height=100).set_cropbox(fitz.Rect(10, 5, 190, 95))
        source_bytes = document.tobytes()
        document.close()
        crop_rects = {
            0: (30.0, 20.0, 150.0, 75.0),
            1: (40.0, 25.0, 160.0, 80.0),
        }

        transformed = transform_crop_rects_for_rotations(
            source_bytes,
            crop_rects,
            {0: 5.0, 1: 90.0},
        )
        output = fitz.open("pdf", rotate_pdf_bytes(source_bytes, {0: 5.0, 1: 90.0}))

        self.assertNotEqual(transformed[0], crop_rects[0])
        self.assertEqual(transformed[1], crop_rects[1])
        angle = math.radians(5.0)
        expected_width = 120.0 * math.cos(angle) + 55.0 * math.sin(angle)
        expected_height = 55.0 * math.cos(angle) + 120.0 * math.sin(angle)
        self.assertAlmostEqual(fitz.Rect(transformed[0]).width, expected_width, places=3)
        self.assertAlmostEqual(fitz.Rect(transformed[0]).height, expected_height, places=3)
        self.assertTrue(output[0].cropbox.contains(fitz.Rect(transformed[0])))
        self.assertTrue(output[1].cropbox.contains(fitz.Rect(transformed[1])))
        output.close()

    def test_fine_rotation_preserves_cropbox_on_an_already_rotated_page(self):
        document = fitz.open("pdf", self._source_pdf())
        document[0].set_rotation(90)
        source_bytes = document.tobytes()
        document.close()
        pending_crop = (30.0, 20.0, 150.0, 75.0)
        expected_crop = transform_crop_rects_for_rotations(
            source_bytes,
            {0: pending_crop},
            {0: 5.0},
        )[0]

        output = fitz.open("pdf", rotate_pdf_bytes(source_bytes, {0: 5.0}))

        self.assertEqual(output[0].rotation, 0)
        self.assertNotEqual(output[0].cropbox, output[0].mediabox)
        self.assertTrue(output[0].cropbox.contains(fitz.Rect(expected_crop)))
        angle = math.radians(5.0)
        self.assertAlmostEqual(
            fitz.Rect(expected_crop).width,
            55.0 * math.cos(angle) + 120.0 * math.sin(angle),
            places=3,
        )
        self.assertAlmostEqual(
            fitz.Rect(expected_crop).height,
            120.0 * math.cos(angle) + 55.0 * math.sin(angle),
            places=3,
        )
        self.assertIn("VECTOR TEXT", output[0].get_text())
        self.assertEqual(len(output[0].get_links()), 1)
        self.assertEqual(len(list(output[0].annots() or ())), 1)
        output.close()

    def test_deskew_applies_independent_detected_angles(self):
        with patch("pycroppdf.rotation.detect_page_deskew_angle", side_effect=[2.0, None]):
            document = fitz.open()
            document.new_page(width=200, height=100).insert_text((20, 30), "One")
            document.new_page(width=200, height=100).insert_text((20, 30), "Two")
            output_bytes, rotations, undetected = deskew_pdf_bytes(document.tobytes(), [0, 1])
            document.close()

        output = fitz.open("pdf", output_bytes)
        self.assertEqual(rotations, {0: 2.0})
        self.assertEqual(undetected, [1])
        self.assertGreater(output[0].mediabox.width, 200)
        self.assertEqual(output[1].mediabox, fitz.Rect(0, 0, 200, 100))
        output.close()

    def test_deskew_worker_count_uses_all_but_one_cpu_except_on_two_cores(self):
        self.assertEqual(recommended_deskew_workers(1, cpu_count=16), 1)
        self.assertEqual(recommended_deskew_workers(7, cpu_count=16), 1)
        self.assertEqual(recommended_deskew_workers(8, cpu_count=1), 1)
        self.assertEqual(recommended_deskew_workers(8, cpu_count=2), 2)
        self.assertEqual(recommended_deskew_workers(8, cpu_count=4), 3)
        self.assertEqual(recommended_deskew_workers(20, cpu_count=16), 15)

    def test_deskew_uses_parallel_detection_for_larger_page_sets(self):
        document = fitz.open()
        for page_number in range(4):
            document.new_page(width=200, height=100).insert_text(
                (20, 30), f"Page {page_number + 1}"
            )
        source_bytes = document.tobytes()
        document.close()

        detected = {0: 1.5, 1: None, 2: -1.0, 3: None}
        with patch(
            "pycroppdf.rotation._detect_deskew_angles_parallel",
            return_value=detected,
        ) as parallel_detection:
            output_bytes, rotations, undetected = deskew_pdf_bytes(
                source_bytes,
                range(4),
                max_workers=2,
            )

        parallel_detection.assert_called_once_with(source_bytes, [0, 1, 2, 3], 2)
        self.assertEqual(rotations, {0: 1.5, 2: -1.0})
        self.assertEqual(undetected, [1, 3])
        output = fitz.open("pdf", output_bytes)
        self.assertGreater(output[0].mediabox.width, 200)
        self.assertEqual(output[1].mediabox, fitz.Rect(0, 0, 200, 100))
        output.close()

    def test_auto_deskew_worker_returns_transformed_pending_crops(self):
        source_bytes = self._source_pdf()
        crop_rects = {0: (30.0, 20.0, 150.0, 75.0)}
        transformed = {0: (31.0, 21.0, 151.0, 76.0)}
        results = []
        worker = AutoDeskewWorker(source_bytes, [0], max_workers=1, crop_rects=crop_rects)
        worker.signals.result.connect(results.append)

        with (
            patch(
                "pycroppdf.workers.deskew_pdf_bytes",
                return_value=(source_bytes, {0: 2.0}, []),
            ),
            patch(
                "pycroppdf.workers.transform_crop_rects_for_rotations",
                return_value=transformed,
            ) as transform_crops,
        ):
            worker.run()

        transform_crops.assert_called_once_with(source_bytes, crop_rects, {0: 2.0})
        self.assertEqual(results[0]["crop_rects"], transformed)

    @unittest.skipUnless(deskew_available(), "optional deskew dependency is not installed")
    def test_optional_deskew_detects_a_known_text_skew(self):
        document = fitz.open()
        page = document.new_page(width=595, height=842)
        for y in range(80, 760, 28):
            page.insert_text(
                (70, y),
                "Synthetic line for automatic deskew angle verification.",
                fontsize=11,
            )
        skewed = rotate_pdf_bytes(document.tobytes(), {0: 3.0})
        document.close()

        rotated = fitz.open("pdf", skewed)
        detected = detect_page_deskew_angle(rotated[0])
        rotated.close()

        self.assertIsNotNone(detected)
        self.assertAlmostEqual(detected, -3.0, delta=0.3)

    @unittest.skipUnless(deskew_available(), "optional deskew dependency is not installed")
    def test_optional_parallel_deskew_runs_in_spawned_processes(self):
        document = fitz.open()
        for _page_number in range(3):
            page = document.new_page(width=595, height=842)
            for y in range(80, 760, 28):
                page.insert_text(
                    (70, y),
                    "Synthetic line for parallel automatic deskew verification.",
                    fontsize=11,
                )
        skewed = rotate_pdf_bytes(document.tobytes(), dict.fromkeys(range(3), 3.0))
        document.close()

        output_bytes, rotations, undetected = deskew_pdf_bytes(
            skewed,
            range(3),
            max_workers=2,
        )

        self.assertEqual(undetected, [])
        self.assertEqual(set(rotations), {0, 1, 2})
        self.assertTrue(all(abs(angle + 3.0) <= 0.3 for angle in rotations.values()))
        output = fitz.open("pdf", output_bytes)
        self.assertEqual(len(output), 3)
        output.close()


class RotationManifestTests(unittest.TestCase):
    def test_save_manifest_maps_rotation_to_original_page(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "source.pdf")
            output_path = os.path.join(directory, "output.pdf")
            manifest_path = os.path.join(directory, "output.json")
            document = fitz.open()
            document.new_page()
            document.new_page()
            document.save(source_path)
            document.delete_page(0)

            SaveWorker(
                document.tobytes(),
                output_path,
                source_path=source_path,
                manifest_path=manifest_path,
                page_map=[1],
                original_page_count=2,
                rotations=[{"original_page": 2, "angle": 1.5}],
            ).run()
            document.close()

            with open(manifest_path, encoding="utf-8") as manifest_file:
                manifest = json.load(manifest_file)
            self.assertEqual(manifest["schema_version"], 3)
            self.assertEqual(
                manifest["rotations"],
                [{"original_page": 2, "angle": 1.5, "output_page": 1}],
            )


if __name__ == "__main__":
    unittest.main()
