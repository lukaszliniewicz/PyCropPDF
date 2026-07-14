import json
import os
import tempfile
import unittest

import fitz

from pycroppdf.provenance import build_manifest, write_manifest
from pycroppdf.workers import SaveWorker


class ProvenanceTests(unittest.TestCase):
    def test_manifest_records_page_mapping_deletions_and_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "source.pdf")
            output_path = os.path.join(directory, "output.pdf")
            manifest_path = os.path.join(directory, "output.json")
            document = fitz.open()
            for _ in range(3):
                document.new_page()
            document.save(source_path)
            document.close()
            with open(source_path, "rb") as source, open(output_path, "wb") as output:
                output.write(source.read())

            manifest = build_manifest(
                source_path,
                output_path,
                page_map=[0, 2],
                original_page_count=3,
                crops=[{"output_page": 1, "original_page": 1, "rect": [1, 2, 3, 4]}],
            )
            write_manifest(manifest_path, manifest)

            with open(manifest_path, encoding="utf-8") as file_handle:
                saved = json.load(file_handle)
            self.assertEqual(saved["page_map"][1]["original_page"], 3)
            self.assertEqual(saved["deleted_original_pages"], [2])
            self.assertEqual(len(saved["source"]["sha256"]), 64)
            self.assertEqual(saved["crops"][0]["rect"], [1, 2, 3, 4])

    def test_save_worker_writes_manifest_for_deleted_page_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "source.pdf")
            output_path = os.path.join(directory, "output.pdf")
            manifest_path = os.path.join(directory, "output.json")
            document = fitz.open()
            for _ in range(3):
                document.new_page()
            document.save(source_path)
            document.delete_page(1)
            pdf_bytes = document.tobytes()
            document.close()

            worker = SaveWorker(
                pdf_bytes,
                output_path,
                source_path=source_path,
                manifest_path=manifest_path,
                page_map=[0, 2],
                original_page_count=3,
                whiteouts=[{"original_page": 3, "rect": [1, 2, 3, 4]}],
            )
            worker.run()

            with open(manifest_path, encoding="utf-8") as file_handle:
                saved = json.load(file_handle)
            self.assertEqual(saved["deleted_original_pages"], [2])
            self.assertEqual(saved["whiteouts"][0]["output_page"], 2)


if __name__ == "__main__":
    unittest.main()
