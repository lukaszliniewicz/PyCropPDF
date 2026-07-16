"""Vector-preserving PDF page rotation and optional skew detection."""

from __future__ import annotations

import math
import multiprocessing
import os
from collections.abc import Iterable, Mapping
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from typing import Any

import fitz

DESKEW_MAX_ANGLE = 10.0
DESKEW_RENDER_MAX_PIXELS = 1400
MIN_PARALLEL_DESKEW_PAGES = 8
ANGLE_EPSILON = 0.01

_DESKEW_PROCESS_DOCUMENT: fitz.Document | None = None


def normalize_angle(angle: float) -> float:
    """Normalize an angle to the interval [-180, 180)."""
    normalized = (float(angle) + 180.0) % 360.0 - 180.0
    return 0.0 if abs(normalized) < ANGLE_EPSILON else normalized


@lru_cache(maxsize=1)
def _deskew_dependencies() -> tuple[Any, Any]:
    """Import and cache the complete optional deskew runtime."""
    import numpy as np
    from deskew import determine_skew

    return np, determine_skew


def deskew_available() -> bool:
    """Return whether the optional deskew runtime can actually be imported."""
    try:
        _deskew_dependencies()
    except Exception:  # optional binary packages can fail with more than ImportError
        return False
    return True


def page_has_interactive_objects(page: fitz.Page) -> bool:
    """Return whether arbitrary rotation may affect interactive page objects."""
    return bool(page.first_annot or page.first_link or page.first_widget)


def pages_with_interactive_objects(
    document: fitz.Document, page_numbers: Iterable[int]
) -> list[int]:
    """Return page indices containing annotations, links, or form widgets."""
    return [
        page_num
        for page_num in sorted({int(number) for number in page_numbers})
        if page_has_interactive_objects(document[page_num])
    ]


def _transformed_rect(rect: fitz.Rect, matrix: fitz.Matrix) -> fitz.Rect:
    transformed = fitz.Rect(rect * matrix)
    if transformed.is_empty or not all(math.isfinite(value) for value in transformed):
        raise ValueError("Rotation produced an invalid page rectangle.")
    return transformed


def _capture_page_objects(page: fitz.Page) -> tuple[list[dict], list[dict], list[dict]]:
    annotations = [
        {
            "xref": annotation.xref,
            "rect": fitz.Rect(annotation.rect),
            "rotation": max(0, int(annotation.rotation)),
            "popup_rect": fitz.Rect(annotation.popup_rect) if annotation.has_popup else None,
        }
        for annotation in (page.annots() or ())
    ]

    links = [dict(link) for link in page.get_links()]
    widgets = [
        {"xref": widget.xref, "rect": fitz.Rect(widget.rect)} for widget in (page.widgets() or ())
    ]
    return annotations, links, widgets


def _restore_page_object_positions(
    document: fitz.Document,
    page_num: int,
    matrix: fitz.Matrix,
    clockwise_degrees: float,
    annotations: list[dict],
    links: list[dict],
    widgets: list[dict],
) -> None:
    page = document[page_num]
    for annotation_state in annotations:
        annotation = page.load_annot(annotation_state["xref"])
        if annotation is None:
            continue
        annotation.set_rect(_transformed_rect(annotation_state["rect"], matrix))
        popup_rect = annotation_state.get("popup_rect")
        if popup_rect is not None:
            annotation.set_popup(_transformed_rect(popup_rect, matrix))
        annotation.set_rotation(round(annotation_state["rotation"] + clockwise_degrees))
        updated_page = annotation.update()
        if updated_page is not None:
            page = updated_page

    for link_state in links:
        if not link_state.get("xref"):
            continue
        link_state["from"] = _transformed_rect(link_state["from"], matrix)
        page.update_link(link_state)

    for widget_state in widgets:
        widget = page.load_widget(widget_state["xref"])
        if widget is None:
            continue
        widget.rect = _transformed_rect(widget_state["rect"], matrix)
        widget.update()


def _set_transformed_page_boxes(
    page: fitz.Page,
    media_box: fitz.Rect,
    crop_box: fitz.Rect,
    optional_boxes: Mapping[str, fitz.Rect],
    matrix: fitz.Matrix,
) -> None:
    transformed_media = _transformed_rect(media_box, matrix)
    new_media = fitz.Rect(0, 0, transformed_media.width, transformed_media.height)
    page.set_mediabox(new_media)
    page.set_cropbox(_transformed_rect(crop_box, matrix))

    setters = {
        "artbox": page.set_artbox,
        "bleedbox": page.set_bleedbox,
        "trimbox": page.set_trimbox,
    }
    for name, original_box in optional_boxes.items():
        transformed = _transformed_rect(original_box, matrix)
        transformed.intersect(new_media)
        if not transformed.is_empty:
            setters[name](transformed)


def rotate_page_content(
    document: fitz.Document,
    page_num: int,
    clockwise_degrees: float,
) -> None:
    """Rotate one page while preserving its vector page content.

    Exact quarter turns use the PDF page rotation field. Other angles are baked
    into the content stream and the page boxes are expanded to avoid clipping.
    """
    clockwise_degrees = normalize_angle(clockwise_degrees)
    if clockwise_degrees == 0:
        return

    page = document[int(page_num)]
    quarter_turns = round(clockwise_degrees / 90.0)
    if math.isclose(clockwise_degrees, quarter_turns * 90.0, abs_tol=ANGLE_EPSILON):
        page.set_rotation((page.rotation + quarter_turns * 90) % 360)
        return

    if page.rotation:
        page.remove_rotation()
        page = document.reload_page(page)

    annotations, links, widgets = _capture_page_objects(page)
    old_transformation = fitz.Matrix(page.transformation_matrix)
    media_box = fitz.Rect(page.mediabox)
    crop_box = fitz.Rect(page.cropbox)
    optional_boxes = {
        "artbox": fitz.Rect(page.artbox),
        "bleedbox": fitz.Rect(page.bleedbox),
        "trimbox": fitz.Rect(page.trimbox),
    }
    original_contents = page.read_contents()

    rotation = fitz.Matrix(clockwise_degrees)
    rotated_media = _transformed_rect(media_box, rotation)
    translation = fitz.Matrix(1, 0, 0, 1, -rotated_media.x0, -rotated_media.y0)
    visual_matrix = rotation * translation

    _set_transformed_page_boxes(page, media_box, crop_box, optional_boxes, visual_matrix)
    new_transformation = fitz.Matrix(page.transformation_matrix)
    pdf_matrix = old_transformation * visual_matrix * ~new_transformation
    pdf_crop = fitz.Rect(crop_box * ~old_transformation)

    prefix = (
        "q "
        f"{pdf_matrix.a:g} {pdf_matrix.b:g} {pdf_matrix.c:g} {pdf_matrix.d:g} "
        f"{pdf_matrix.e:g} {pdf_matrix.f:g} cm\n"
        f"{pdf_crop.x0:g} {pdf_crop.y0:g} {pdf_crop.width:g} {pdf_crop.height:g} re W n\n"
    ).encode("ascii")
    new_contents = prefix + original_contents + b"\nQ\n"
    contents_xref = document.get_new_xref()
    document.update_object(contents_xref, "<<>>")
    document.update_stream(contents_xref, new_contents)
    page.set_contents(contents_xref)

    _restore_page_object_positions(
        document,
        int(page_num),
        visual_matrix,
        clockwise_degrees,
        annotations,
        links,
        widgets,
    )


def rotate_pdf_bytes(
    pdf_bytes: bytes,
    rotations: Mapping[int, float],
) -> bytes:
    """Return a PDF snapshot with the requested per-page rotation deltas."""
    document = fitz.open("pdf", pdf_bytes)
    try:
        for page_num, angle in sorted(rotations.items()):
            page_num = int(page_num)
            if page_num < 0 or page_num >= len(document):
                raise ValueError(f"Page {page_num + 1} is outside the document.")
            rotate_page_content(document, page_num, float(angle))
        return document.tobytes(garbage=2, deflate=True)
    finally:
        document.close()


def detect_page_deskew_angle(page: fitz.Page) -> float | None:
    """Return the suggested clockwise correction angle for one page."""
    try:
        np, determine_skew = _deskew_dependencies()
    except Exception as error:  # pragma: no cover - depends on optional installation
        raise RuntimeError(
            "Automatic deskew is unavailable because its optional runtime could not be loaded. "
            "Install or reinstall pycroppdf[deskew]."
        ) from error

    page_rect = page.rect
    longest_side = max(page_rect.width, page_rect.height, 1.0)
    scale = min(2.0, max(0.5, DESKEW_RENDER_MAX_PIXELS / longest_side))
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale),
        colorspace=fitz.csGRAY,
        alpha=False,
        annots=False,
    )
    grayscale = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.stride)[
        :, : pixmap.width
    ]
    detected = determine_skew(
        grayscale,
        min_angle=-DESKEW_MAX_ANGLE,
        max_angle=DESKEW_MAX_ANGLE,
        min_deviation=0.2,
    )
    if detected is None or abs(float(detected)) < ANGLE_EPSILON:
        return None

    # ``deskew`` returns the counter-clockwise image correction. PyMuPDF page
    # rotation and this module's UI convention use positive clockwise angles.
    return normalize_angle(-float(detected))


def recommended_deskew_workers(page_count: int, cpu_count: int | None = None) -> int:
    """Use all but one detected CPU, except use both CPUs on two-core systems."""
    page_count = max(0, int(page_count))
    if page_count < MIN_PARALLEL_DESKEW_PAGES:
        return 1

    if cpu_count is None:
        available_cpu_count = getattr(os, "process_cpu_count", os.cpu_count)
        cpu_count = available_cpu_count() or 1
    cpu_count = max(1, int(cpu_count))
    usable_cpus = cpu_count if cpu_count <= 2 else cpu_count - 1
    return min(page_count, usable_cpus)


def _initialize_deskew_process(pdf_bytes: bytes) -> None:
    """Open one process-local document instead of sending it with every task."""
    global _DESKEW_PROCESS_DOCUMENT
    _DESKEW_PROCESS_DOCUMENT = fitz.open("pdf", pdf_bytes)


def _detect_page_deskew_task(page_num: int) -> tuple[int, float | None]:
    if _DESKEW_PROCESS_DOCUMENT is None:  # pragma: no cover - process setup guard
        raise RuntimeError("The deskew worker process was not initialized.")
    return page_num, detect_page_deskew_angle(_DESKEW_PROCESS_DOCUMENT[page_num])


def _detect_deskew_angles_parallel(
    pdf_bytes: bytes,
    page_numbers: list[int],
    worker_count: int,
) -> dict[int, float | None]:
    """Detect page angles concurrently using isolated PyMuPDF documents."""
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(
        max_workers=worker_count,
        mp_context=context,
        initializer=_initialize_deskew_process,
        initargs=(pdf_bytes,),
    ) as executor:
        return dict(executor.map(_detect_page_deskew_task, page_numbers))


def deskew_pdf_bytes(
    pdf_bytes: bytes,
    page_numbers: Iterable[int],
    *,
    max_workers: int | None = None,
) -> tuple[bytes, dict[int, float], list[int]]:
    """Detect and apply skew corrections to the requested pages."""
    document = fitz.open("pdf", pdf_bytes)
    applied: dict[int, float] = {}
    undetected: list[int] = []
    try:
        targets = sorted({int(page_num) for page_num in page_numbers})
        for page_num in targets:
            if page_num < 0 or page_num >= len(document):
                raise ValueError(f"Page {page_num + 1} is outside the document.")

        worker_count = (
            recommended_deskew_workers(len(targets))
            if max_workers is None
            else min(len(targets), max(1, int(max_workers)))
        )
        if worker_count > 1:
            detected_angles = _detect_deskew_angles_parallel(
                pdf_bytes,
                targets,
                worker_count,
            )
        else:
            detected_angles = {
                page_num: detect_page_deskew_angle(document[page_num]) for page_num in targets
            }

        for page_num in targets:
            angle = detected_angles[page_num]
            if angle is None:
                undetected.append(page_num)
                continue
            rotate_page_content(document, page_num, angle)
            applied[page_num] = angle
        return document.tobytes(garbage=2, deflate=True), applied, undetected
    finally:
        document.close()
