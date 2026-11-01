"""Pure PyMuPDF geometry for logical and capped preview rasters."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

import pymupdf as fitz

BASE_RENDER_SCALE = 1.5
DEFAULT_MAX_RASTER_SIDE = 2048


@dataclass(frozen=True, slots=True)
class RasterGeometry:
    logical_dimensions: tuple[int, int]
    raster_dimensions: tuple[int, int]
    scale: float
    clip: tuple[float, float, float, float] | None


def visible_pdf_rect_to_visual_rect(page: fitz.Page, pdf_rect: fitz.Rect) -> fitz.Rect:
    """Map an unrotated CropBox-space rectangle into displayed page coordinates."""
    crop_position = page.cropbox_position
    local_rect = fitz.Rect(
        pdf_rect.x0 - crop_position.x,
        pdf_rect.y0 - crop_position.y,
        pdf_rect.x1 - crop_position.x,
        pdf_rect.y1 - crop_position.y,
    )
    return fitz.Rect(local_rect * page.rotation_matrix)


def page_raster_geometry(
    page: fitz.Page,
    crop_rect: fitz.Rect | Iterable[float] | None = None,
    *,
    max_dimension: int | None = None,
) -> RasterGeometry:
    """Calculate preview geometry without rendering or changing the page."""
    page_rect = fitz.Rect(page.rect)
    _validate_rect(page_rect, "Visible page rectangle")

    visual_rect = page_rect
    clip: tuple[float, float, float, float] | None = None
    if crop_rect is not None:
        pending_crop = fitz.Rect(crop_rect)
        _validate_rect(pending_crop, "Pending crop rectangle")
        pending_crop.intersect(page.cropbox)
        _validate_rect(pending_crop, "Pending crop rectangle")
        visual_rect = visible_pdf_rect_to_visual_rect(page, pending_crop)
        _validate_rect(visual_rect, "Displayed crop rectangle")
        visual_rect.intersect(page_rect)
        _validate_rect(visual_rect, "Displayed crop rectangle")
        clip = (
            float(visual_rect.x0),
            float(visual_rect.y0),
            float(visual_rect.x1),
            float(visual_rect.y1),
        )

    logical_bounds = (visual_rect * fitz.Matrix(BASE_RENDER_SCALE, BASE_RENDER_SCALE)).irect
    logical_dimensions = (int(logical_bounds.width), int(logical_bounds.height))
    if logical_dimensions[0] <= 0 or logical_dimensions[1] <= 0:
        raise ValueError("Logical raster dimensions must be positive.")

    scale = BASE_RENDER_SCALE
    if max_dimension is not None:
        if not isinstance(max_dimension, int) or isinstance(max_dimension, bool):
            raise TypeError("max_dimension must be an integer or None.")
        if max_dimension < 2:
            raise ValueError("max_dimension must be at least 2.")
        longest_side = max(visual_rect.width, visual_rect.height)
        scale = min(BASE_RENDER_SCALE, (max_dimension - 1) / longest_side)

    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("Raster scale must be finite and positive.")
    raster_bounds = (visual_rect * fitz.Matrix(scale, scale)).irect
    raster_dimensions = (int(raster_bounds.width), int(raster_bounds.height))
    if raster_dimensions[0] <= 0 or raster_dimensions[1] <= 0:
        raise ValueError("Raster dimensions must be positive.")
    return RasterGeometry(
        logical_dimensions=logical_dimensions,
        raster_dimensions=raster_dimensions,
        scale=float(scale),
        clip=clip,
    )


def _validate_rect(rect: fitz.Rect, description: str) -> None:
    coordinates = (rect.x0, rect.y0, rect.x1, rect.y1)
    if (
        not all(math.isfinite(value) for value in coordinates)
        or rect.is_empty
        or rect.width <= 0
        or rect.height <= 0
    ):
        raise ValueError(f"{description} must be finite and have positive dimensions.")
