"""State helpers for undo snapshots and page-index remapping."""

from __future__ import annotations

from typing import Iterable

from PyQt6.QtCore import QRectF


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
