"""Background workers and coordinate conversion for PDF rendering and saving."""

from __future__ import annotations

import concurrent.futures
import os
import tempfile
import traceback
from collections.abc import Iterable
from contextlib import suppress

import fitz
from PyQt6.QtCore import QObject, QRectF, QRunnable, pyqtSignal
from PyQt6.QtGui import QImage

from .provenance import build_manifest, sha256_file, write_manifest
from .rotation import deskew_pdf_bytes, recommended_deskew_workers, rotate_pdf_bytes

MAX_RENDER_WORKERS = 4
LOCAL_RENDER_PAGE_LIMIT = 2
_RENDER_PDF_BYTES: bytes | None = None
_RENDER_CROP_RECTS: dict[int, tuple[float, float, float, float]] = {}
_RENDER_DOCUMENT: fitz.Document | None = None


class WorkerSignals(QObject):
    """Signals emitted by a worker running outside the GUI thread."""

    finished = pyqtSignal()
    error = pyqtSignal(str)
    result = pyqtSignal(object)


def rect_to_tuple(rect: fitz.Rect | Iterable[float]) -> tuple[float, float, float, float]:
    """Return a PDF rectangle as a picklable, immutable tuple."""
    if all(hasattr(rect, attribute) for attribute in ("x", "y", "width", "height")):
        normalized = fitz.Rect(
            rect.x(), rect.y(), rect.x() + rect.width(), rect.y() + rect.height()
        )
    else:
        normalized = fitz.Rect(rect)
    return tuple(float(value) for value in normalized)


def _validate_rect(rect: fitz.Rect, description: str) -> fitz.Rect:
    if rect.is_empty or rect.width <= 0 or rect.height <= 0:
        raise ValueError(f"{description} does not intersect the visible page area.")
    return rect


def _visible_pdf_rect_to_visual_rect(page: fitz.Page, pdf_rect: fitz.Rect) -> fitz.Rect:
    """Convert an unrotated CropBox-relative PDF rectangle into displayed coordinates."""
    crop_position = page.cropbox_position
    local_rect = fitz.Rect(
        pdf_rect.x0 - crop_position.x,
        pdf_rect.y0 - crop_position.y,
        pdf_rect.x1 - crop_position.x,
        pdf_rect.y1 - crop_position.y,
    )
    return fitz.Rect(local_rect * page.rotation_matrix)


def scene_rect_to_pdf_coords(
    scene_rect,
    page_image_dims: tuple[int, int],
    canvas_dims: tuple[int, int],
    page: fitz.Page,
    visible_pdf_rect: fitz.Rect | Iterable[float] | None = None,
) -> fitz.Rect:
    """Map a selection in an overlay scene to an unrotated PDF page rectangle.

    The display is a rasterized, potentially rotated view of the page's current
    CropBox. ``visible_pdf_rect`` is the exact unrotated PDF rectangle rendered
    into the raster; it is the current CropBox for an untouched page and an
    active crop preview for a pending crop. Keeping this mapping explicit avoids
    losing a pre-existing CropBox or reinterpreting a cropped preview as a full
    page.
    """
    image_width, image_height = page_image_dims
    canvas_width, canvas_height = canvas_dims
    if image_width <= 0 or image_height <= 0:
        raise ValueError("The selected page has no renderable preview.")

    visible_rect = fitz.Rect(visible_pdf_rect or page.cropbox)
    visible_rect.intersect(page.cropbox)
    _validate_rect(visible_rect, "The rendered page area")
    visual_rect = _visible_pdf_rect_to_visual_rect(page, visible_rect)
    _validate_rect(visual_rect, "The rendered page area")

    x_offset = (canvas_width - image_width) // 2
    y_offset = (canvas_height - image_height) // 2
    scale_x = visual_rect.width / image_width
    scale_y = visual_rect.height / image_height
    selected_visual_rect = fitz.Rect(
        visual_rect.x0 + (scene_rect.x() - x_offset) * scale_x,
        visual_rect.y0 + (scene_rect.y() - y_offset) * scale_y,
        visual_rect.x0 + (scene_rect.x() - x_offset + scene_rect.width()) * scale_x,
        visual_rect.y0 + (scene_rect.y() - y_offset + scene_rect.height()) * scale_y,
    )
    selected_visual_rect.intersect(visual_rect)
    _validate_rect(selected_visual_rect, "The selection")

    local_pdf_rect = fitz.Rect(selected_visual_rect * page.derotation_matrix)
    crop_position = page.cropbox_position
    selected_pdf_rect = fitz.Rect(
        local_pdf_rect.x0 + crop_position.x,
        local_pdf_rect.y0 + crop_position.y,
        local_pdf_rect.x1 + crop_position.x,
        local_pdf_rect.y1 + crop_position.y,
    )
    selected_pdf_rect.intersect(visible_rect)
    return _validate_rect(selected_pdf_rect, "The selection")


def pdf_rect_to_scene_coords(
    pdf_rect: fitz.Rect | Iterable[float],
    page_image_dims: tuple[int, int],
    canvas_dims: tuple[int, int],
    page: fitz.Page,
    visible_pdf_rect: fitz.Rect | Iterable[float] | None = None,
) -> QRectF:
    """Map an unrotated PDF rectangle into an overlay or page-preview scene.

    This is the inverse of :func:`scene_rect_to_pdf_coords`.  Keeping both
    directions in one place prevents a stack rectangle from being reused as
    page-preview pixels when the stack canvas is larger than an individual
    page.
    """
    image_width, image_height = page_image_dims
    canvas_width, canvas_height = canvas_dims
    if image_width <= 0 or image_height <= 0:
        raise ValueError("The selected page has no renderable preview.")

    visible_rect = fitz.Rect(visible_pdf_rect or page.cropbox)
    visible_rect.intersect(page.cropbox)
    _validate_rect(visible_rect, "The rendered page area")

    selected_pdf_rect = fitz.Rect(pdf_rect)
    selected_pdf_rect.intersect(visible_rect)
    _validate_rect(selected_pdf_rect, "The selection")

    visual_rect = _visible_pdf_rect_to_visual_rect(page, visible_rect)
    crop_position = page.cropbox_position
    local_selected_rect = fitz.Rect(
        selected_pdf_rect.x0 - crop_position.x,
        selected_pdf_rect.y0 - crop_position.y,
        selected_pdf_rect.x1 - crop_position.x,
        selected_pdf_rect.y1 - crop_position.y,
    )
    selected_visual_rect = fitz.Rect(local_selected_rect * page.rotation_matrix)

    scale_x = image_width / visual_rect.width
    scale_y = image_height / visual_rect.height
    x_offset = (canvas_width - image_width) // 2
    y_offset = (canvas_height - image_height) // 2
    return QRectF(
        x_offset + (selected_visual_rect.x0 - visual_rect.x0) * scale_x,
        y_offset + (selected_visual_rect.y0 - visual_rect.y0) * scale_y,
        selected_visual_rect.width * scale_x,
        selected_visual_rect.height * scale_y,
    )


def _initialise_render_process(
    pdf_bytes: bytes,
    crop_rects: dict[int, tuple[float, float, float, float]],
) -> None:
    """Store the immutable render inputs once per process."""
    global _RENDER_PDF_BYTES, _RENDER_CROP_RECTS, _RENDER_DOCUMENT
    _RENDER_PDF_BYTES = pdf_bytes
    _RENDER_CROP_RECTS = crop_rects
    _RENDER_DOCUMENT = fitz.open("pdf", pdf_bytes)


def _render_page_from_document(
    document: fitz.Document,
    page_num: int,
    crop_rects: dict[int, tuple[float, float, float, float]],
):
    page = document[page_num]
    clip_rect = None
    crop_rect = crop_rects.get(page_num)
    if crop_rect:
        clip_rect = fitz.Rect(crop_rect)
        clip_rect.intersect(page.cropbox)
        _validate_rect(clip_rect, f"Crop rectangle for page {page_num + 1}")

    pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), clip=clip_rect)
    return page_num, pix.samples, pix.width, pix.height, pix.stride


def _render_page_task(page_num: int):
    """Render one page using the process-local PDF source."""
    if _RENDER_PDF_BYTES is None or _RENDER_DOCUMENT is None:
        raise RuntimeError("Render process was not initialized.")
    return _render_page_from_document(_RENDER_DOCUMENT, page_num, _RENDER_CROP_RECTS)


def _image_from_render_result(result):
    page_num, samples, width, height, stride = result
    image = QImage(samples, width, height, stride, QImage.Format.Format_RGB888).copy()
    return page_num, image


class RenderAllPagesWorker(QRunnable):
    """Render all previews with a bounded pool of PDF rendering processes."""

    def __init__(
        self,
        pdf_bytes: bytes,
        num_pages: int,
        crop_info: dict | None = None,
        page_numbers: Iterable[int] | None = None,
    ):
        super().__init__()
        self.signals = WorkerSignals()
        self.pdf_bytes = pdf_bytes
        self.num_pages = num_pages
        self.crop_info = crop_info or {}
        requested_pages = range(num_pages) if page_numbers is None else page_numbers
        self.page_numbers = tuple(sorted({int(page_num) for page_num in requested_pages}))
        if any(page_num < 0 or page_num >= num_pages for page_num in self.page_numbers):
            raise ValueError("A requested preview page is outside the document.")

    def run(self) -> None:
        try:
            crop_rects = {
                int(page_num): rect_to_tuple(rect)
                for page_num, rect in self.crop_info.get("rects", {}).items()
                if rect
            }
            if not self.page_numbers:
                return

            if len(self.page_numbers) <= LOCAL_RENDER_PAGE_LIMIT:
                document = fitz.open("pdf", self.pdf_bytes)
                try:
                    for page_num in self.page_numbers:
                        result = _render_page_from_document(document, page_num, crop_rects)
                        self.signals.result.emit(_image_from_render_result(result))
                finally:
                    document.close()
                return

            process_cpu_count = getattr(os, "process_cpu_count", os.cpu_count)
            cpu_count = process_cpu_count() or 1
            max_workers = min(len(self.page_numbers), MAX_RENDER_WORKERS, max(1, cpu_count - 1))
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=max_workers,
                initializer=_initialise_render_process,
                initargs=(self.pdf_bytes, crop_rects),
            ) as executor:
                futures = [
                    executor.submit(_render_page_task, page_num) for page_num in self.page_numbers
                ]
                for future in concurrent.futures.as_completed(futures):
                    self.signals.result.emit(_image_from_render_result(future.result()))
        except Exception:
            self.signals.error.emit(traceback.format_exc())
        finally:
            self.signals.finished.emit()


class RotatePagesWorker(QRunnable):
    """Apply manual page rotations outside the GUI thread."""

    def __init__(self, pdf_bytes: bytes, rotations: dict[int, float]):
        super().__init__()
        self.signals = WorkerSignals()
        self.pdf_bytes = pdf_bytes
        self.rotations = {int(page_num): float(angle) for page_num, angle in rotations.items()}

    def run(self) -> None:
        try:
            rotated_pdf = rotate_pdf_bytes(self.pdf_bytes, self.rotations)
            self.signals.result.emit(
                {
                    "pdf_bytes": rotated_pdf,
                    "rotation_deltas": dict(self.rotations),
                    "undetected_pages": [],
                }
            )
        except Exception:
            self.signals.error.emit(traceback.format_exc())
        finally:
            self.signals.finished.emit()


class AutoDeskewWorker(QRunnable):
    """Detect and apply per-page skew corrections outside the GUI thread."""

    def __init__(
        self,
        pdf_bytes: bytes,
        page_numbers: Iterable[int],
        max_workers: int | None = None,
    ):
        super().__init__()
        self.signals = WorkerSignals()
        self.pdf_bytes = pdf_bytes
        self.page_numbers = tuple(sorted({int(page_num) for page_num in page_numbers}))
        self.max_workers = (
            recommended_deskew_workers(len(self.page_numbers))
            if max_workers is None
            else max(1, int(max_workers))
        )

    def run(self) -> None:
        try:
            deskewed_pdf, rotations, undetected = deskew_pdf_bytes(
                self.pdf_bytes,
                self.page_numbers,
                max_workers=self.max_workers,
            )
            self.signals.result.emit(
                {
                    "pdf_bytes": deskewed_pdf,
                    "rotation_deltas": rotations,
                    "undetected_pages": undetected,
                    "worker_count": self.max_workers,
                }
            )
        except Exception:
            self.signals.error.emit(traceback.format_exc())
        finally:
            self.signals.finished.emit()


def _resolve_operations(
    operations: Iterable[dict],
    page_map: list[int],
) -> list[dict]:
    output_page_by_original = {
        original_page: output_page + 1 for output_page, original_page in enumerate(page_map)
    }
    resolved_operations = []
    for operation in operations:
        original_page = int(operation.get("original_page") or 0)
        output_page = output_page_by_original.get(original_page - 1)
        if output_page is None:
            continue
        resolved = dict(operation)
        resolved["output_page"] = output_page
        resolved_operations.append(resolved)
    return resolved_operations


def _temporary_output_path(directory: str, suffix: str) -> str:
    file_descriptor, path = tempfile.mkstemp(prefix=".pycroppdf-", suffix=suffix, dir=directory)
    os.close(file_descriptor)
    os.unlink(path)
    return path


class SaveWorker(QRunnable):
    """Save an edited PDF and its provenance manifest without partial outputs."""

    def __init__(
        self,
        pdf_bytes: bytes,
        save_path: str,
        crop_info: dict | None = None,
        deflate: bool = False,
        garbage: int = 2,
        source_path: str | None = None,
        manifest_path: str | None = None,
        page_map: Iterable[int] | None = None,
        original_page_count: int | None = None,
        rotations: Iterable[dict] | None = None,
        whiteouts: Iterable[dict] | None = None,
        redactions: Iterable[dict] | None = None,
        source_sha256: str | None = None,
    ):
        super().__init__()
        self.signals = WorkerSignals()
        self.pdf_bytes = pdf_bytes
        self.save_path = save_path
        self.crop_info = crop_info or {}
        self.deflate = deflate
        self.garbage = int(garbage)
        if self.garbage not in range(5):
            raise ValueError("PDF garbage collection must be between 0 and 4.")
        self.source_path = source_path
        self.manifest_path = manifest_path
        self.page_map = list(page_map or [])
        self.original_page_count = original_page_count
        self.rotations = list(rotations or [])
        self.whiteouts = list(whiteouts or [])
        self.redactions = list(redactions or [])
        self.source_sha256 = source_sha256

    def run(self) -> None:
        document = None
        pdf_temporary_path = None
        manifest_temporary_path = None
        pdf_saved = False
        try:
            output_directory = os.path.dirname(os.path.abspath(self.save_path))
            if not os.path.isdir(output_directory):
                raise ValueError(f"Save directory does not exist: {output_directory}")

            document = fitz.open("pdf", self.pdf_bytes)
            output_page_count = len(document)
            applied_crops = []
            crop_rects = self.crop_info.get("rects", {})
            for page_num, page in enumerate(document):
                crop_rect = crop_rects.get(page_num)
                if not crop_rect:
                    continue
                crop_rect = fitz.Rect(rect_to_tuple(crop_rect))
                crop_rect.intersect(page.cropbox)
                _validate_rect(crop_rect, f"Crop rectangle for page {page_num + 1}")
                page.set_cropbox(crop_rect)
                original_page = (
                    self.page_map[page_num] if page_num < len(self.page_map) else page_num
                )
                applied_crops.append(
                    {
                        "output_page": page_num + 1,
                        "original_page": original_page + 1,
                        "rect": [round(float(value), 3) for value in crop_rect],
                    }
                )

            pdf_temporary_path = _temporary_output_path(output_directory, ".pdf")
            document.save(
                pdf_temporary_path,
                garbage=self.garbage,
                deflate=self.deflate,
            )
            document.close()
            document = None

            if self.manifest_path:
                if not self.source_path:
                    raise ValueError("Cannot write provenance without the loaded source snapshot.")
                source_sha256 = self.source_sha256
                if source_sha256 is None:
                    if not os.path.isfile(self.source_path):
                        raise ValueError("Cannot hash the original source for provenance.")
                    source_sha256 = sha256_file(self.source_path)
                resolved_page_map = self.page_map or list(
                    range(int(self.original_page_count or output_page_count))
                )
                manifest = build_manifest(
                    self.source_path,
                    self.save_path,
                    resolved_page_map,
                    int(self.original_page_count or len(resolved_page_map)),
                    crops=applied_crops,
                    rotations=_resolve_operations(self.rotations, resolved_page_map),
                    whiteouts=_resolve_operations(self.whiteouts, resolved_page_map),
                    redactions=_resolve_operations(self.redactions, resolved_page_map),
                    source_sha256=source_sha256,
                    output_sha256=sha256_file(pdf_temporary_path),
                )
                manifest_directory = os.path.dirname(os.path.abspath(self.manifest_path))
                os.makedirs(manifest_directory, exist_ok=True)
                manifest_temporary_path = _temporary_output_path(manifest_directory, ".json")
                write_manifest(manifest_temporary_path, manifest)

            os.replace(pdf_temporary_path, self.save_path)
            pdf_temporary_path = None
            pdf_saved = True
            if manifest_temporary_path:
                os.replace(manifest_temporary_path, self.manifest_path)
                manifest_temporary_path = None
            self.signals.result.emit(
                {
                    "pdf_path": self.save_path,
                    "manifest_path": self.manifest_path,
                    "manifest_written": bool(self.manifest_path),
                }
            )
        except Exception:
            error = traceback.format_exc()
            if pdf_saved:
                self.signals.result.emit(
                    {
                        "pdf_path": self.save_path,
                        "manifest_path": self.manifest_path,
                        "manifest_written": False,
                        "manifest_error": error,
                    }
                )
            else:
                self.signals.error.emit(error)
        finally:
            if document is not None:
                document.close()
            for temporary_path in (pdf_temporary_path, manifest_temporary_path):
                if temporary_path and os.path.exists(temporary_path):
                    with suppress(OSError):
                        os.remove(temporary_path)
            self.signals.finished.emit()
