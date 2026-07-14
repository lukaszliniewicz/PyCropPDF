"""Provenance helpers for PyCropPDF derivatives."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = 2


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Return the SHA-256 digest for an in-memory source snapshot."""
    return hashlib.sha256(data).hexdigest()


def build_manifest(
    source_path: str,
    output_path: str,
    page_map: list[int],
    original_page_count: int,
    crops: list[dict[str, Any]] | None = None,
    whiteouts: list[dict[str, Any]] | None = None,
    redactions: list[dict[str, Any]] | None = None,
    source_sha256: str | None = None,
    output_sha256: str | None = None,
) -> dict[str, Any]:
    mapped_pages = {int(page) for page in page_map}
    deleted_pages = [
        page + 1 for page in range(int(original_page_count)) if page not in mapped_pages
    ]
    return {
        "schema": "pycroppdf.provenance",
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "path": os.path.abspath(source_path),
            "sha256": source_sha256 or sha256_file(source_path),
            "page_count": int(original_page_count),
        },
        "output": {
            "path": os.path.abspath(output_path),
            "sha256": output_sha256 or sha256_file(output_path),
            "page_count": len(page_map),
        },
        "page_map": [
            {"output_page": output_page + 1, "original_page": original_page + 1}
            for output_page, original_page in enumerate(page_map)
        ],
        "deleted_original_pages": deleted_pages,
        "crops": list(crops or []),
        "whiteouts": list(whiteouts or []),
        "redactions": list(redactions or []),
    }


def write_manifest(path: str, manifest: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w", encoding="utf-8", newline="\n") as file_handle:
        json.dump(manifest, file_handle, indent=2, ensure_ascii=False)
        file_handle.write("\n")
    os.replace(temporary_path, path)
