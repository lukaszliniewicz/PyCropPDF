from __future__ import annotations

import pymupdf as fitz
import pytest

from pycroppdf.raster_geometry import (
    BASE_RENDER_SCALE,
    DEFAULT_MAX_RASTER_SIDE,
    page_raster_geometry,
    visible_pdf_rect_to_visual_rect,
)

PAGE_SIZES = ((200, 300), (320, 240), (595, 842), (842, 595))
ROTATIONS = (0, 90, 180, 270)


def _explicit_visual_rect(
    pdf_rect: fitz.Rect,
    cropbox: fitz.Rect,
    rotation: int,
) -> fitz.Rect:
    x0 = pdf_rect.x0 - cropbox.x0
    y0 = pdf_rect.y0 - cropbox.y0
    x1 = pdf_rect.x1 - cropbox.x0
    y1 = pdf_rect.y1 - cropbox.y0
    width = cropbox.width
    height = cropbox.height
    if rotation == 0:
        return fitz.Rect(x0, y0, x1, y1)
    if rotation == 90:
        return fitz.Rect(height - y1, x0, height - y0, x1)
    if rotation == 180:
        return fitz.Rect(width - x1, height - y1, width - x0, height - y0)
    if rotation == 270:
        return fitz.Rect(y0, width - x1, y1, width - x0)
    raise AssertionError(f"Unexpected rotation {rotation}")


def _assert_rect_close(actual: fitz.Rect, expected: fitz.Rect) -> None:
    assert tuple(actual) == pytest.approx(tuple(expected), abs=0.0001)


@pytest.mark.parametrize("size", PAGE_SIZES)
@pytest.mark.parametrize("offset_crop", (False, True))
@pytest.mark.parametrize("rotation", ROTATIONS)
@pytest.mark.parametrize("pending_crop", (False, True))
def test_geometry_matches_actual_pixmaps_for_crop_offsets_and_rotations(
    size: tuple[int, int],
    offset_crop: bool,
    rotation: int,
    pending_crop: bool,
):
    width, height = size
    document = fitz.open()
    page = document.new_page(width=width, height=height)
    if offset_crop:
        page.set_cropbox(fitz.Rect(11, 17, width - 19, height - 23))
    page.set_rotation(rotation)
    source_cropbox = fitz.Rect(page.cropbox)
    source_mediabox = fitz.Rect(page.mediabox)
    source_rotation = page.rotation

    crop_rect = None
    expected_clip = None
    if pending_crop:
        crop_rect = fitz.Rect(
            source_cropbox.x0 + source_cropbox.width * 0.13,
            source_cropbox.y0 + source_cropbox.height * 0.17,
            source_cropbox.x1 - source_cropbox.width * 0.19,
            source_cropbox.y1 - source_cropbox.height * 0.23,
        )
        expected_clip = _explicit_visual_rect(crop_rect, source_cropbox, rotation)
        expected_clip.intersect(page.rect)
        _assert_rect_close(
            visible_pdf_rect_to_visual_rect(page, crop_rect),
            _explicit_visual_rect(crop_rect, source_cropbox, rotation),
        )

    geometry = page_raster_geometry(page, crop_rect)
    matrix = fitz.Matrix(BASE_RENDER_SCALE, BASE_RENDER_SCALE)
    pixmap = page.get_pixmap(matrix=matrix, clip=expected_clip)
    assert geometry.scale == BASE_RENDER_SCALE
    assert geometry.raster_dimensions == (pixmap.width, pixmap.height)
    assert geometry.logical_dimensions == (pixmap.width, pixmap.height)
    if expected_clip is None:
        assert geometry.clip is None
    else:
        assert geometry.clip == pytest.approx(tuple(float(value) for value in expected_clip))
    assert fitz.Rect(page.cropbox) == source_cropbox
    assert fitz.Rect(page.mediabox) == source_mediabox
    assert page.rotation == source_rotation
    document.close()


@pytest.mark.parametrize("rotation", ROTATIONS)
def test_capped_geometry_matches_pixmap_and_preserves_uncapped_logical_dimensions(rotation: int):
    document = fitz.open()
    page = document.new_page(width=4000, height=1200)
    page.set_rotation(rotation)
    source_cropbox = fitz.Rect(page.cropbox)
    source_mediabox = fitz.Rect(page.mediabox)

    geometry = page_raster_geometry(page, max_dimension=DEFAULT_MAX_RASTER_SIDE)
    pixmap = page.get_pixmap(matrix=fitz.Matrix(geometry.scale, geometry.scale))

    assert geometry.scale < BASE_RENDER_SCALE
    assert geometry.raster_dimensions == (pixmap.width, pixmap.height)
    assert max(geometry.raster_dimensions) <= DEFAULT_MAX_RASTER_SIDE
    assert max(geometry.logical_dimensions) > DEFAULT_MAX_RASTER_SIDE
    assert fitz.Rect(page.cropbox) == source_cropbox
    assert fitz.Rect(page.mediabox) == source_mediabox
    assert page.rotation == rotation
    document.close()


def test_geometry_rejects_invalid_crop_and_cap_without_mutating_page():
    document = fitz.open()
    page = document.new_page(width=300, height=200)
    page.set_cropbox(fitz.Rect(13, 17, 287, 183))
    page.set_rotation(90)
    cropbox_before = fitz.Rect(page.cropbox)
    mediabox_before = fitz.Rect(page.mediabox)
    rotation_before = page.rotation

    with pytest.raises(ValueError):
        page_raster_geometry(page, fitz.Rect(400, 400, 500, 500))
    with pytest.raises(ValueError):
        page_raster_geometry(page, max_dimension=1)
    with pytest.raises(ValueError):
        page_raster_geometry(page, fitz.Rect(float("nan"), 20, 30, 40))

    assert fitz.Rect(page.cropbox) == cropbox_before
    assert fitz.Rect(page.mediabox) == mediabox_before
    assert page.rotation == rotation_before
    document.close()
