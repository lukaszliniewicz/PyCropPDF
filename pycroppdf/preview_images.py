"""Qt raster adapters and bounded, streaming preview composition."""

from __future__ import annotations

import math
import struct
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass

from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QImage, QPainter, QTransform

from .raster_cache import RasterCache, RasterCacheError, RasterData
from .raster_geometry import DEFAULT_MAX_RASTER_SIDE

OVERLAY_ASSET = "overlays"
_BUNDLE_HEADER = struct.Struct(">4sIII")
_IMAGE_HEADER = struct.Struct(">4sIIII")
CacheErrorCallback = Callable[[int, int, str], None]


@dataclass(frozen=True)
class PreviewLayout:
    generation: int
    dimensions: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class PreviewFrame:
    generation: int
    logical_dimensions: tuple[int, int]
    has_even_pages: bool


def image_from_raster(data: RasterData) -> QImage:
    return QImage(
        data.samples, data.width, data.height, data.stride, QImage.Format.Format_RGB888
    ).copy()


def thumbnail_raster(image: QImage) -> RasterData:
    thumbnail = image.scaled(
        QSize(160, 240),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    ).convertToFormat(QImage.Format.Format_RGB888)
    buffer = thumbnail.constBits()
    assert buffer is not None
    return RasterData(
        thumbnail.width(),
        thumbnail.height(),
        thumbnail.bytesPerLine(),
        buffer.asstring(thumbnail.sizeInBytes()),
    )


class CachedImages:
    """Lazy page access; dimensions never require decoding a page image."""

    def __init__(
        self,
        cache: RasterCache,
        generation: int,
        dimensions: Sequence[tuple[int, int]],
        on_error: CacheErrorCallback | None = None,
    ) -> None:
        self.cache = cache
        self.generation = generation
        self.dimensions = tuple(dimensions)
        self.on_error = on_error

    def __len__(self) -> int:
        return len(self.dimensions)

    def __iter__(self) -> Iterator[QImage | None]:
        for page_num in range(len(self)):
            yield self[page_num]

    def __getitem__(self, page_num: int) -> QImage | None:
        if page_num < 0:
            page_num += len(self)
        if not 0 <= page_num < len(self):
            raise IndexError(page_num)
        return self._image(page_num, thumbnail=False)

    @property
    def ready_pages(self) -> frozenset[int]:
        if self.generation != self.cache.generation:
            return frozenset()
        return self.cache.ready_pages

    def has_page(self, page_num: int) -> bool:
        return self.cache.info(page_num, generation=self.generation) is not None

    def thumbnail(self, page_num: int) -> QImage | None:
        return self._image(page_num, thumbnail=True)

    def _image(self, page_num: int, *, thumbnail: bool) -> QImage | None:
        try:
            data = self.cache.read_page(page_num, generation=self.generation, thumbnail=thumbnail)
            return image_from_raster(data) if data is not None else None
        except RasterCacheError as error:
            if self.on_error is not None:
                self.on_error(self.generation, page_num, str(error))
            return None

    def without_pages(self, deleted_pages: Iterable[int]) -> CachedImages:
        deleted = set(deleted_pages)
        kept = [page for page in range(len(self)) if page not in deleted]
        generation = self.cache.begin_generation(len(kept), preserve=dict(enumerate(kept)))
        return CachedImages(
            self.cache, generation, [self.dimensions[page] for page in kept], self.on_error
        )


def rotated_dimensions(dimensions: tuple[int, int], angle: float) -> tuple[int, int]:
    if abs(angle) < 0.01:
        return dimensions
    width, height = dimensions
    matrix = QImage.trueMatrix(QTransform().rotate(angle), width, height)
    bounds = matrix.mapRect(QRectF(0, 0, width, height)).toAlignedRect()
    return bounds.width(), bounds.height()


def overlay_canvas(width: int, height: int) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    if image.isNull():
        raise MemoryError("The preview overlay could not be allocated.")
    image.fill(Qt.GlobalColor.transparent)
    return image


def draw_overlay_page(
    canvas: QImage,
    image: QImage,
    logical_dimensions: tuple[int, int],
    page_dimensions: tuple[int, int],
    scale: float = 1.0,
) -> None:
    width, height = page_dimensions
    canvas_width, canvas_height = logical_dimensions
    target = QRectF(
        ((canvas_width - width) // 2) * scale,
        ((canvas_height - height) // 2) * scale,
        width * scale,
        height * scale,
    )
    painter = QPainter(canvas)
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Darken)
        painter.drawImage(target, image)
    finally:
        painter.end()


class StreamingOverlay:
    """Preserve all-page ink while retaining only three bounded canvases."""

    def __init__(
        self, dimensions: Sequence[tuple[int, int]], *, opaque_pages: bool = False
    ) -> None:
        self.dimensions = tuple(dimensions)
        self.logical_dimensions = (
            max((width for width, _height in dimensions), default=0),
            max((height for _width, height in dimensions), default=0),
        )
        width, height = self.logical_dimensions
        if width <= 0 or height <= 0:
            raise ValueError("No page geometry is available for the overlay.")
        longest = max(width, height)
        self.scale = (
            1.0 if longest <= DEFAULT_MAX_RASTER_SIDE else (DEFAULT_MAX_RASTER_SIDE - 1) / longest
        )
        pixel_width = max(1, math.ceil(width * self.scale))
        pixel_height = max(1, math.ceil(height * self.scale))
        self._all_pages = overlay_canvas(pixel_width, pixel_height)
        self.odd_pages = overlay_canvas(pixel_width, pixel_height)
        self.even_pages = overlay_canvas(pixel_width, pixel_height)
        # Darken can be regrouped exactly for opaque, integer-aligned pages.
        # Fractional scaling and transient alpha-bearing rotation previews retain
        # the original per-page composition order.
        self._opaque_pages = opaque_pages and self.scale == 1.0
        self._all_dirty = False
        self.seen: set[int] = set()

    @property
    def all_pages(self) -> QImage:
        if self._all_dirty:
            self._all_pages.fill(Qt.GlobalColor.transparent)
            painter = QPainter(self._all_pages)
            try:
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Darken)
                painter.drawImage(0, 0, self.odd_pages)
                painter.drawImage(0, 0, self.even_pages)
            finally:
                painter.end()
            self._all_dirty = False
        return self._all_pages

    def add(self, page_num: int, image: QImage) -> None:
        if page_num in self.seen:
            raise ValueError("A page was added to the overlay more than once.")
        if self._opaque_pages and (
            image.hasAlphaChannel() or (image.width(), image.height()) != self.dimensions[page_num]
        ):
            raise ValueError("Grouped overlays require opaque pages at their logical size.")
        parity_canvas = self.odd_pages if page_num % 2 == 0 else self.even_pages
        canvases = (parity_canvas,) if self._opaque_pages else (self._all_pages, parity_canvas)
        for canvas in canvases:
            draw_overlay_page(
                canvas, image, self.logical_dimensions, self.dimensions[page_num], self.scale
            )
        self._all_dirty = self._opaque_pages
        self.seen.add(page_num)

    def merge_group(self, pages: Sequence[int], image: QImage) -> None:
        if (
            not self._opaque_pages
            or not pages
            or image.size() != self.odd_pages.size()
            or any(page % 2 != pages[0] % 2 or page in self.seen for page in pages)
        ):
            raise ValueError("The cached overlay group does not match the canvas.")
        canvas = self.odd_pages if pages[0] % 2 == 0 else self.even_pages
        painter = QPainter(canvas)
        try:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Darken)
            painter.drawImage(0, 0, image)
        finally:
            painter.end()
        self.seen.update(pages)
        self._all_dirty = True

    def encode(self) -> bytes:
        images = [
            encode_image(image) for image in (self.all_pages, self.odd_pages, self.even_pages)
        ]
        return _BUNDLE_HEADER.pack(b"PCO1", *(len(data) for data in images)) + b"".join(images)


def encode_image(image: QImage) -> bytes:
    """Transfer a bounded, lossless image inside this process, without a codec."""
    width, height = image.width(), image.height()
    if not 0 < width <= DEFAULT_MAX_RASTER_SIDE or not 0 < height <= DEFAULT_MAX_RASTER_SIDE:
        raise RasterCacheError("The cached image exceeds the raster limit.")
    image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    buffer = image.constBits()
    if buffer is None:
        raise RasterCacheError("The preview image has no pixels.")
    samples = buffer.asstring(image.sizeInBytes())
    return _IMAGE_HEADER.pack(b"PCI1", width, height, image.bytesPerLine(), len(samples)) + samples


def decode_image(data: bytes) -> QImage:
    if len(data) < _IMAGE_HEADER.size:
        raise RasterCacheError("The cached image is incomplete.")
    magic, width, height, stride, length = _IMAGE_HEADER.unpack_from(data)
    if magic != b"PCI1":
        raise RasterCacheError("The cached image format is unsupported.")
    if not 0 < width <= DEFAULT_MAX_RASTER_SIDE or not 0 < height <= DEFAULT_MAX_RASTER_SIDE:
        raise RasterCacheError("The cached image exceeds the raster limit.")
    if stride != width * 4 or length != stride * height or len(data) != _IMAGE_HEADER.size + length:
        raise RasterCacheError("The cached image has invalid lengths.")
    image = QImage(
        data[_IMAGE_HEADER.size :], width, height, stride, QImage.Format.Format_ARGB32_Premultiplied
    ).copy()
    if image.isNull():
        raise RasterCacheError("The cached image could not be allocated.")
    return image


def decode_overlay(data: bytes) -> tuple[QImage, QImage, QImage]:
    if len(data) < _BUNDLE_HEADER.size:
        raise RasterCacheError("The cached overlay is incomplete.")
    magic, *lengths = _BUNDLE_HEADER.unpack_from(data)
    if magic != b"PCO1":
        raise RasterCacheError("The cached overlay format is unsupported.")
    if sum(lengths) + _BUNDLE_HEADER.size != len(data):
        raise RasterCacheError("The cached overlay has invalid lengths.")
    images = []
    offset = _BUNDLE_HEADER.size
    for length in lengths:
        payload = data[offset : offset + length]
        offset += length
        images.append(decode_image(payload))
    if any(image.size() != images[0].size() for image in images[1:]):
        raise RasterCacheError("The cached overlay canvases have different sizes.")
    return images[0], images[1], images[2]
