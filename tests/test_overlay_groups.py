from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf as fitz
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

import pycroppdf.workers as workers
from pycroppdf.overlay_groups import GroupedOverlay
from pycroppdf.preview_images import (
    StreamingOverlay,
    decode_overlay,
    image_from_raster,
    thumbnail_raster,
)
from pycroppdf.processing import CancellationToken, OperationCancelled
from pycroppdf.raster_cache import RasterCache, RasterData


@pytest.fixture(scope="module", autouse=True)
def application():
    app = QApplication.instance() or QApplication([])
    yield app


def _image(page, dimensions):
    width, height = dimensions
    image = QImage(width, height, QImage.Format.Format_RGB888)
    image.fill(Qt.GlobalColor.white)
    image.setPixelColor(page % width, (page * 7) % height, QColor(page % 256, 80, 180))
    return image


def _write(cache, generation, page, image):
    pixels = RasterData(
        image.width(),
        image.height(),
        image.bytesPerLine(),
        image.constBits().asstring(image.sizeInBytes()),
    )
    cache.write_page(
        generation,
        page,
        logical_dimensions=(image.width(), image.height()),
        pixels=pixels,
        thumbnail=thumbnail_raster(image),
    )


def _build(cache, generation, dimensions, requested, images, order=None):
    overlay = GroupedOverlay(
        cache, generation, dimensions, set(requested), CancellationToken().raise_if_cancelled
    )
    for page in range(len(images)) if order is None else order:
        if page in overlay.cached_pages:
            continue
        if page in requested:
            _write(cache, generation, page, images[page])
            image = images[page]
        else:
            image = image_from_raster(cache.read_page(page, generation=generation, cache=False))
        overlay.add(page, image)
    return overlay


def _reference(images):
    overlay = StreamingOverlay([(image.width(), image.height()) for image in images])
    for page, image in enumerate(images):
        overlay.add(page, image)
    return decode_overlay(overlay.encode())


def test_one_page_edit_reuses_other_groups_without_reading_their_rasters(tmp_path):
    dimensions = [(40 - page % 3, 60 - page % 5) for page in range(128)]
    images = [_image(page, size) for page, size in enumerate(dimensions)]
    cache = RasterCache(512 * 1024, directory=tmp_path)
    try:
        generation = cache.begin_generation(len(images))
        first = _build(cache, generation, dimensions, set(range(128)), images, range(127, -1, -1))
        assert decode_overlay(first.encode()) == _reference(images)
        assert cache.disk_bytes > 0
        generation = cache.begin_generation(
            128, preserve={page: page for page in range(128) if page != 7}
        )
        images[7].setPixelColor(4, 9, QColor("black"))
        with patch.object(cache, "read_page", wraps=cache.read_page) as read:
            edited = _build(cache, generation, dimensions, {7}, images)
        assert len(edited.cached_pages) == 96
        read_pages = {call.args[0] for call in read.call_args_list}
        assert read_pages == set(range(1, 64, 2))
        assert read.call_count <= 64
        assert decode_overlay(edited.encode()) == _reference(images)
        assert cache.peak_decoded_bytes <= 512 * 1024
    finally:
        cache.close()


def test_deletion_and_crop_invalidate_position_parity_and_geometry_keys(tmp_path):
    dimensions = [(40, 60)] * 70
    images = [_image(page, size) for page, size in enumerate(dimensions)]
    cache = RasterCache(directory=tmp_path)
    try:
        generation = cache.begin_generation(70)
        _build(cache, generation, dimensions, set(range(70)), images)
        generation = cache.begin_generation(69, preserve={page: page + 1 for page in range(69)})
        images.pop(0)
        dimensions.pop(0)
        deleted = _build(cache, generation, dimensions, set(), images)
        assert not deleted.cached_pages
        assert decode_overlay(deleted.encode()) == _reference(images)
        generation = cache.begin_generation(
            69, preserve={page: page for page in range(69) if page != 3}
        )
        dimensions[3] = (60, 40)
        images[3] = _image(3, dimensions[3])
        cropped = _build(cache, generation, dimensions, {3}, images)
        assert not cropped.cached_pages  # The maximum canvas width changed.
        assert decode_overlay(cropped.encode()) == _reference(images)
    finally:
        cache.close()


@pytest.mark.parametrize(
    "budget,dimensions",
    [(0, [(40, 60)] * 5), (4096, [(40, 60)] * 5), (500 * 1024 * 1024, [(2400, 60)] * 5)],
)
def test_insufficient_budget_or_fractional_scaling_falls_back(budget, dimensions, tmp_path):
    # Capped pages have a smaller raster than their logical geometry.
    images = [
        _image(page, (min(width, 2048), height)) for page, (width, height) in enumerate(dimensions)
    ]
    cache = RasterCache(budget, directory=tmp_path)
    try:
        generation = cache.begin_generation(5)
        with patch.object(cache, "write_memo", wraps=cache.write_memo) as write_memo:
            overlay = GroupedOverlay(cache, generation, dimensions, set(range(5)), lambda: None)
            reference = StreamingOverlay(dimensions)
            for page, image in enumerate(images):
                overlay.add(page, image)
                reference.add(page, image)
        write_memo.assert_not_called()
        assert decode_overlay(overlay.encode()) == decode_overlay(reference.encode())
    finally:
        cache.close()


def test_malformed_optional_memo_is_rebuilt_and_cancellation_stops_group_work(tmp_path):
    dimensions = [(40, 60)] * 4
    images = [_image(page, size) for page, size in enumerate(dimensions)]
    cache = RasterCache(directory=tmp_path)
    try:
        generation = cache.begin_generation(4)
        _build(cache, generation, dimensions, set(range(4)), images)
        with patch.object(cache, "read_memo", return_value=b"incomplete"):
            rebuilt = _build(cache, generation, dimensions, set(), images)
        assert not rebuilt.cached_pages
        assert decode_overlay(rebuilt.encode()) == _reference(images)
        token = CancellationToken()
        token.cancel()
        with pytest.raises(OperationCancelled):
            GroupedOverlay(cache, generation, dimensions, set(), token.raise_if_cancelled)
    finally:
        cache.close()


def test_worker_partial_refresh_emits_each_page_once_and_renders_only_changed_page(tmp_path):
    with fitz.open() as document:
        for _ in range(70):
            document.new_page(width=40, height=60)
        pdf_bytes = document.tobytes()
    dimensions = [(60, 90)] * 70
    images = [_image(page, size) for page, size in enumerate(dimensions)]
    native_requests = []

    def render(_worker, page_numbers, _crop_rects, _max_dimension):
        native_requests.append(page_numbers)
        for page in reversed(page_numbers):
            image = images[page]
            yield (
                page,
                image.constBits().asstring(image.sizeInBytes()),
                image.width(),
                image.height(),
                image.bytesPerLine(),
            )

    cache = RasterCache(directory=tmp_path)
    errors, results, frames = [], [], []
    try:
        generation = cache.begin_generation(70)
        with patch.object(workers.RenderAllPagesWorker, "_render_results", render):
            worker = workers.RenderAllPagesWorker(pdf_bytes, 70, cache=cache, generation=generation)
            worker.signals.error.connect(errors.append)
            worker.run()
            assert not errors
            generation = cache.begin_generation(
                70, preserve={page: page for page in range(70) if page != 7}
            )
            images[7].setPixelColor(4, 9, QColor("black"))
            worker = workers.RenderAllPagesWorker(
                pdf_bytes, 70, page_numbers=[7], cache=cache, generation=generation
            )
            worker.signals.error.connect(errors.append)
            worker.signals.result.connect(results.append)
            worker.signals.preview_ready.connect(frames.append)
            with patch.object(cache, "read_page", wraps=cache.read_page) as read:
                worker.run()
        assert not errors
        assert native_requests == [tuple(range(70)), (7,)]
        assert {page for page, _info in results} == set(range(70))
        assert len(results) == 70
        assert all(info.generation == generation for _page, info in results)
        assert read.call_count <= 64
        assert all(call.args[0] % 2 == 1 and call.args[0] < 64 for call in read.call_args_list)
        assert frames and all(frame.generation == generation for frame in frames)
        assert decode_overlay(cache.read_asset(generation, "overlays")) == _reference(images)
    finally:
        cache.close()


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_native_cropped_rotated_pages_match_grouped_canvas_geometry(tmp_path, angle):
    with fitz.open() as document:
        page = document.new_page(width=400, height=300)
        page.draw_rect(fitz.Rect(90, 80, 130, 110), fill=(0, 0, 0))
        page.set_cropbox(fitz.Rect(37.25, 29.75, 396.5, 274.5))
        page.set_rotation(angle)
        pdf_bytes = document.tobytes()
    cache = RasterCache(directory=tmp_path)
    errors = []
    try:
        generation = cache.begin_generation(1)
        worker = workers.RenderAllPagesWorker(
            pdf_bytes,
            1,
            crop_info={"rects": {0: fitz.Rect(75.1, 63.4, 334.2, 226.8)}},
            cache=cache,
            generation=generation,
        )
        worker.signals.error.connect(errors.append)
        worker.run()
        assert not errors
        info = cache.info(0)
        assert (info.width, info.height) == (info.logical_width, info.logical_height)
        image = image_from_raster(cache.read_page(0, generation=generation))
        assert decode_overlay(cache.read_asset(generation, "overlays")) == _reference([image])
    finally:
        cache.close()
