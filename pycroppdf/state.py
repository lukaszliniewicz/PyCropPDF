"""State helpers for undo snapshots and page-index remapping."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import suppress
from typing import Any

from PyQt6.QtCore import QRectF

MAX_UNDO_SNAPSHOTS = 10
MAX_UNDO_DISK_BYTES = 512 * 1024 * 1024


class UndoSnapshotStore:
    """Own disk-backed PDF snapshots and their bounded undo history."""

    def __init__(
        self,
        max_entries: int = MAX_UNDO_SNAPSHOTS,
        max_disk_bytes: int = MAX_UNDO_DISK_BYTES,
    ) -> None:
        self.max_entries = max(1, int(max_entries))
        self.max_disk_bytes = max(0, int(max_disk_bytes))
        self.entries: list[dict[str, Any]] = []
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="pycroppdf-undo-")

    def write_document(self, document: Any) -> tuple[str, int]:
        """Write the current PDF state without retaining another in-memory copy."""
        file_descriptor, path = tempfile.mkstemp(
            prefix="snapshot-",
            suffix=".pdf",
            dir=self._temporary_directory.name,
        )
        os.close(file_descriptor)
        try:
            document.save(path, garbage=0, deflate=False, no_new_id=True)
            with suppress(OSError):
                os.chmod(path, 0o600)
            return path, os.path.getsize(path)
        except Exception:
            with suppress(OSError):
                os.remove(path)
            raise

    @staticmethod
    def read_document(snapshot: Mapping[str, Any]) -> bytes:
        """Read a snapshot for reopening as a memory-backed active document."""
        path = snapshot.get("pdf_path")
        if not path:
            raise ValueError("The undo snapshot has no PDF document state.")
        with open(os.fspath(path), "rb") as snapshot_file:
            return snapshot_file.read()

    def append(self, snapshot: dict[str, Any]) -> None:
        self.entries.append(snapshot)
        self._enforce_limits()

    def pop(self, index: int = -1) -> dict[str, Any]:
        return self.entries.pop(index)

    def release(self, snapshot: Mapping[str, Any]) -> None:
        path = snapshot.get("pdf_path")
        if path:
            with suppress(OSError):
                os.remove(os.fspath(path))

    def discard(self, snapshot: dict[str, Any]) -> None:
        for index, entry in enumerate(self.entries):
            if entry is snapshot:
                self.entries.pop(index)
                break
        self.release(snapshot)

    def clear(self) -> None:
        for snapshot in self.entries:
            self.release(snapshot)
        self.entries.clear()

    def close(self) -> None:
        self.clear()
        self._temporary_directory.cleanup()

    def _enforce_limits(self) -> None:
        while len(self.entries) > self.max_entries:
            self.release(self.entries.pop(0))

        total_disk_bytes = sum(
            max(0, int(snapshot.get("pdf_size", 0))) for snapshot in self.entries
        )
        document_snapshot_count = sum(bool(snapshot.get("pdf_path")) for snapshot in self.entries)
        while document_snapshot_count > 1 and total_disk_bytes > self.max_disk_bytes:
            removed_index = next(
                index for index, snapshot in enumerate(self.entries) if snapshot.get("pdf_path")
            )
            removed_entries = self.entries[: removed_index + 1]
            del self.entries[: removed_index + 1]
            total_disk_bytes -= sum(
                max(0, int(snapshot.get("pdf_size", 0))) for snapshot in removed_entries
            )
            document_snapshot_count -= sum(
                bool(snapshot.get("pdf_path")) for snapshot in removed_entries
            )
            for snapshot in removed_entries:
                self.release(snapshot)


def clone_crop_info(crop_info: dict | None) -> dict | None:
    """Return an independent copy of crop state and its rectangle values."""
    if not crop_info:
        return None

    cloned = dict(crop_info)
    cloned["rects"] = {}
    for page_num, rect in crop_info.get("rects", {}).items():
        if isinstance(rect, QRectF):
            cloned["rects"][int(page_num)] = QRectF(rect)
        else:
            cloned["rects"][int(page_num)] = tuple(float(value) for value in rect)
    cloned["image_dims"] = [
        (int(width), int(height)) for width, height in crop_info.get("image_dims", [])
    ]
    return cloned


def remap_page_indices_after_deletions(
    page_indices: Iterable[int],
    deleted_pages: Iterable[int],
) -> set[int]:
    """Map output-page indices onto the document after pages are deleted."""
    deleted = sorted({int(page_num) for page_num in deleted_pages})
    deleted_set = set(deleted)
    remapped = set()
    for page_num in page_indices:
        page_num = int(page_num)
        if page_num in deleted_set:
            continue
        remapped.add(page_num - sum(deleted_page < page_num for deleted_page in deleted))
    return remapped


def remap_crop_info_after_deletions(
    crop_info: dict | None,
    deleted_pages: Iterable[int],
) -> dict | None:
    """Preserve per-page crop boxes and dimensions after page deletion."""
    cloned = clone_crop_info(crop_info)
    if not cloned:
        return None

    deleted = sorted({int(page_num) for page_num in deleted_pages})
    deleted_set = set(deleted)
    original_rects = cloned.get("rects", {})
    original_dims = cloned.get("image_dims", [])

    cloned["rects"] = {
        new_page_num: original_rects[old_page_num]
        for new_page_num, old_page_num in enumerate(
            page_num for page_num in range(len(original_dims)) if page_num not in deleted_set
        )
        if old_page_num in original_rects
    }
    cloned["image_dims"] = [
        dimensions
        for page_num, dimensions in enumerate(original_dims)
        if page_num not in deleted_set
    ]
    return cloned


def remap_page_mapping_after_deletions(
    values: Mapping[int, object],
    deleted_pages: Iterable[int],
) -> dict[int, object]:
    """Preserve page-keyed values while removing and renumbering pages."""
    deleted = sorted({int(page_num) for page_num in deleted_pages})
    deleted_set = set(deleted)
    return {
        int(page_num) - sum(deleted_page < int(page_num) for deleted_page in deleted): value
        for page_num, value in values.items()
        if int(page_num) not in deleted_set
    }
