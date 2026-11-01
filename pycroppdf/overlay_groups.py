"""Reuse bounded parity composites without retaining extra page images."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PyQt6.QtGui import QImage

from .preview_images import (
    StreamingOverlay,
    decode_image,
    draw_overlay_page,
    encode_image,
    image_from_raster,
    overlay_canvas,
)
from .raster_cache import RasterCache, RasterCacheError

PAGES_PER_PARITY_GROUP = 32
_MEMO_SCHEMA = 1


class GroupedOverlay:
    """Rebuild only dirty parity groups when opaque pages can be regrouped.

    Memo bytes share the page cache's budget. Only one scratch group canvas is
    allocated at a time, even when native rendering completes out of order.
    """

    def __init__(
        self,
        cache: RasterCache,
        generation: int,
        dimensions: Sequence[tuple[int, int]],
        requested: set[int],
        check_cancelled: Callable[[], None],
    ) -> None:
        self.cache = cache
        self.generation = generation
        self.dimensions = tuple(dimensions)
        self.check_cancelled = check_cancelled
        self.stream = StreamingOverlay(dimensions, opaque_pages=True)
        self._groups: dict[tuple[int, int], list[int]] = {}
        for page in range(len(dimensions)):
            self._groups.setdefault(self._group_id(page), []).append(page)
        width, height = self.stream.logical_dimensions
        # Allow a little header space as well as the four bytes per pixel.
        memo_bytes = len(self._groups) * (width * height * 4 + 64)
        if self.stream.scale != 1.0 or memo_bytes > cache.memo_budget_bytes:
            self._groups.clear()
        self._pending = {group: set(pages) for group, pages in self._groups.items()}
        keys = {
            group: key
            for group, pages in self._groups.items()
            if not requested.intersection(pages) and (key := self._key(group)) is not None
        }
        cache.retain_memos(generation, set(keys.values()))
        for group, key in keys.items():
            check_cancelled()
            data = cache.read_memo(generation, key)
            if data is None:
                continue
            try:
                image = decode_image(data)
                self.stream.merge_group(self._groups[group], image)
            except (RasterCacheError, ValueError):
                cache.discard_memo(generation, key)
                continue
            self._pending[group].clear()
        self.cached_pages = frozenset(self.stream.seen)

    @staticmethod
    def _group_id(page: int) -> tuple[int, int]:
        return page % 2, page // (2 * PAGES_PER_PARITY_GROUP)

    @property
    def logical_dimensions(self) -> tuple[int, int]:
        return self.stream.logical_dimensions

    def _key(
        self, group: tuple[int, int], versions: tuple[int | None, ...] | None = None
    ) -> tuple[int, ...] | None:
        if versions is None:
            versions = self.cache.page_versions(self.generation)
        if versions is None:
            return None
        values = [_MEMO_SCHEMA, *self.logical_dimensions, *group]
        for page in self._groups[group]:
            version = versions[page]
            if version is None:
                return None
            values.extend((page, *self.dimensions[page], version))
        return tuple(values)

    def add(self, page: int, image: QImage) -> None:
        self.stream.add(page, image)
        group = self._group_id(page)
        pending = self._pending.get(group)
        if pending is None:
            return
        pending.remove(page)
        if not pending:
            self._store_group(group)

    def _store_group(self, group: tuple[int, int]) -> None:
        self.check_cancelled()
        versions = self.cache.page_versions(self.generation)
        key = self._key(group, versions)
        if key is None or versions is None:
            return
        width, height = self.logical_dimensions
        canvas = overlay_canvas(width, height)
        dependencies = []
        try:
            for page in self._groups[group]:
                self.check_cancelled()
                pixels = self.cache.read_page(page, generation=self.generation, cache=False)
                version = versions[page]
                if pixels is None or version is None:
                    return
                draw_overlay_page(
                    canvas,
                    image_from_raster(pixels),
                    self.logical_dimensions,
                    self.dimensions[page],
                )
                del pixels
                dependencies.append(version)
            self.check_cancelled()
            if self.cache.page_versions(self.generation) != versions:
                return
            self.cache.write_memo(
                self.generation, key, encode_image(canvas), page_versions=tuple(dependencies)
            )
        except RasterCacheError:
            # Optional derived data must not prevent a usable preview. A corrupt
            # page is invalidated by the cache and the worker will offer Retry.
            return

    def encode(self) -> bytes:
        return self.stream.encode()
