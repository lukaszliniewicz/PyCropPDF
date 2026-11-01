from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QImage, QTransform
from PyQt6.QtWidgets import QApplication

from pycroppdf.preview_images import (
    CachedImages,
    StreamingOverlay,
    decode_image,
    decode_overlay,
    encode_image,
    image_from_raster,
    rotated_dimensions,
    thumbnail_raster,
)
from pycroppdf.raster_cache import RasterCache, RasterCacheError, RasterData


@pytest.fixture(scope="module", autouse=True)
def application():
    app = QApplication.instance() or QApplication([])
    yield app


def _page(width=40, height=60):
    image = QImage(width, height, QImage.Format.Format_RGB888)
    image.fill(QColor("white"))
    return image


def _raster(image):
    image = image.convertToFormat(QImage.Format.Format_RGB888)
    return RasterData(
        image.width(),
        image.height(),
        image.bytesPerLine(),
        image.constBits().asstring(image.sizeInBytes()),
    )


def test_streamed_hundred_page_markers_survive_eviction_in_both_orders(tmp_path, application):
    assert QApplication.instance() is application
    cache = RasterCache(8000, directory=tmp_path)
    generation = cache.begin_generation(100)
    try:
        for page in range(100):
            image = _page()
            image.setPixelColor(page % 40, page // 40, QColor("black"))
            cache.write_page(
                generation,
                page,
                logical_dimensions=(40, 60),
                pixels=_raster(image),
                thumbnail=thumbnail_raster(image),
            )
            cache.read_page(page)
        assert cache.decoded_entry_count == 1
        assert cache.peak_decoded_bytes <= 8000
        overlays = []
        for pages in (range(100), range(99, -1, -1)):
            overlay = StreamingOverlay([(40, 60)] * 100)
            for page in pages:
                overlay.add(page, image_from_raster(cache.read_page(page, cache=False)))
            overlays.append(decode_overlay(overlay.encode()))
        assert overlays[0] == overlays[1]
        for page in range(100):
            x, y = page % 40, page // 40
            assert overlays[0][0].pixelColor(x, y) == QColor("black")
            parity = 1 if page % 2 == 0 else 2
            assert overlays[0][parity].pixelColor(x, y) == QColor("black")
    finally:
        cache.close()


def test_streamed_mixed_sizes_use_logical_centering_and_capped_canvases():
    small, large = _page(20, 30), _page(40, 60)
    small.setPixelColor(0, 0, QColor("black"))
    large.setPixelColor(39, 59, QColor("black"))
    overlay = StreamingOverlay([(20, 30), (40, 60)])
    overlay.add(1, large)
    overlay.add(0, small)
    assert overlay.all_pages.pixelColor(10, 15) == QColor("black")
    assert overlay.all_pages.pixelColor(39, 59) == QColor("black")
    assert overlay.all_pages.pixelColor(0, 0) == QColor("white")

    capped = StreamingOverlay([(6000, 1800), (893, 1263)])
    assert capped.logical_dimensions == (6000, 1800)
    assert max(capped.all_pages.width(), capped.all_pages.height()) <= 2048
    capped.add(0, large)
    assert decode_overlay(capped.encode())[0].size() == capped.all_pages.size()


@pytest.mark.parametrize("dimensions", [(40, 60), (893, 1263), (6000, 1800)])
@pytest.mark.parametrize("angle", [0, 0.1, 4, -12.5, 90, 180])
def test_rotated_logical_dimensions_match_qimage_transform(dimensions, angle):
    image = _page(*dimensions)
    actual = image.transformed(
        QTransform().rotate(angle), Qt.TransformationMode.SmoothTransformation
    )
    assert rotated_dimensions(dimensions, angle) == (actual.width(), actual.height())


def test_lazy_images_keep_metadata_after_eviction_and_reject_old_generation(tmp_path):
    cache = RasterCache(0, directory=tmp_path)
    generation = cache.begin_generation(2)
    image = _page()
    for page in range(2):
        cache.write_page(
            generation,
            page,
            logical_dimensions=(6000, 9000),
            pixels=_raster(image),
            thumbnail=thumbnail_raster(image),
        )
    images = CachedImages(cache, generation, [(6000, 9000)] * 2)
    assert images[0] == images[-1]
    assert images.dimensions == ((6000, 9000),) * 2
    assert cache.decoded_bytes == 0
    remapped = images.without_pages({0})
    assert len(remapped) == 1 and remapped.has_page(0)
    assert images[0] is None and not images.ready_pages
    cache.close()
    assert remapped[0] is None


def test_corrupt_cached_page_reports_once_and_can_be_regenerated(tmp_path):
    errors = []
    cache = RasterCache(0, directory=tmp_path)
    generation = cache.begin_generation(1)
    image = _page()
    cache.write_page(
        generation,
        0,
        logical_dimensions=(40, 60),
        pixels=_raster(image),
        thumbnail=thumbnail_raster(image),
    )
    images = CachedImages(cache, generation, [(40, 60)], lambda *args: errors.append(args))
    cache._pages[0].full_path.unlink()
    assert images[0] is None and images[0] is None
    assert len(errors) == 1 and errors[0][:2] == (generation, 0)
    assert not images.has_page(0)
    cache.close()


def test_overlay_bundle_rejects_incomplete_or_unbounded_payloads():
    import struct

    with pytest.raises(RasterCacheError):
        decode_overlay(b"truncated")
    payload = struct.pack(">4sIIII", b"PCI1", 9000, 1, 36000, 36000)
    with pytest.raises(RasterCacheError, match="raster limit"):
        decode_overlay(
            struct.pack(">4sIII", b"PCO1", len(payload), len(payload), len(payload)) + payload * 3
        )


def test_raw_snapshot_preserves_premultiplied_pixels_without_a_codec():
    image = _page(893, 1263).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    image.setPixelColor(3, 5, QColor(10, 90, 220, 73))
    data = encode_image(image)
    assert len(data) == 20 + 893 * 1263 * 4
    decoded = decode_image(data)
    assert decoded == image
    image.fill(Qt.GlobalColor.black)
    assert decoded != image


@pytest.mark.parametrize(
    "header,payload",
    [
        ((b"bad!", 1, 1, 4, 4), b"\0" * 4),
        ((b"PCI1", 0, 1, 0, 0), b""),
        ((b"PCI1", 1, 1, 8, 8), b"\0" * 8),
        ((b"PCI1", 1, 1, 4, 3), b"\0" * 3),
        ((b"PCI1", 1, 1, 4, 4), b"\0" * 5),
    ],
)
def test_raw_snapshot_rejects_invalid_format_stride_or_lengths(header, payload):
    import struct

    with pytest.raises(RasterCacheError):
        decode_image(struct.pack(">4sIIII", *header) + payload)


def test_opaque_specialisation_matches_general_composition_and_rejects_alpha():
    images = [_page(21, 33), _page(40, 60), _page(31, 41)]
    dimensions = [(image.width(), image.height()) for image in images]
    general = StreamingOverlay(dimensions)
    opaque = StreamingOverlay(dimensions, opaque_pages=True)
    for page, image in enumerate(images):
        image.setPixelColor(page, page + 1, QColor(30 + page * 50, 100, 20))
        general.add(page, image)
        opaque.add(page, image)
        assert decode_overlay(general.encode()) == decode_overlay(opaque.encode())
    alpha = _page().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    with pytest.raises(ValueError, match="opaque"):
        StreamingOverlay([(40, 60)], opaque_pages=True).add(0, alpha)
