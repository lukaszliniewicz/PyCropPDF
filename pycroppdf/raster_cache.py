"""Private RAM-first raster storage with bounded decoded-byte residency."""

from __future__ import annotations

import os
import re
import tempfile
import uuid
import zlib
from collections import OrderedDict
from collections.abc import Collection, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock, RLock

DEFAULT_DECODED_BUDGET = 500 * 1024 * 1024
_ASSET_KEY = re.compile(r"[a-z0-9_-]{1,64}\Z")


class RasterCacheError(RuntimeError):
    """Raised when current raster data cannot be stored or recovered."""


@dataclass(frozen=True, slots=True)
class RasterData:
    """An RGB888 image buffer with an explicit row stride."""

    width: int
    height: int
    stride: int
    samples: bytes


@dataclass(frozen=True, slots=True)
class RasterInfo:
    """Pixel dimensions and logical geometry for one ready page."""

    generation: int
    page: int
    logical_width: int
    logical_height: int
    width: int
    height: int
    stride: int
    thumbnail_width: int
    thumbnail_height: int
    thumbnail_stride: int


@dataclass(frozen=True, slots=True)
class _SpillInfo:
    path: Path
    compressed_size: int
    checksum: int


@dataclass(eq=False, slots=True)
class _RasterBuffer:
    """One canonical full-page or thumbnail buffer shared by page aliases."""

    width: int
    height: int
    stride: int
    resident: RasterData | None
    spill: _SpillInfo | None = None

    @property
    def byte_size(self) -> int:
        return self.stride * self.height


@dataclass(frozen=True, slots=True)
class _PageRecord:
    info: RasterInfo
    full_buffer: _RasterBuffer
    thumbnail_buffer: _RasterBuffer
    version: int

    @property
    def full_path(self) -> Path | None:
        return None if self.full_buffer.spill is None else self.full_buffer.spill.path

    @property
    def thumbnail_path(self) -> Path | None:
        return None if self.thumbnail_buffer.spill is None else self.thumbnail_buffer.spill.path


@dataclass(frozen=True, slots=True)
class _MemoRecord:
    data: bytes
    page_versions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _MetadataView:
    generation: int
    page_count: int
    closed: bool
    page_info: tuple[RasterInfo | None, ...]
    page_versions: tuple[int | None, ...]
    ready_pages: frozenset[int]


def _write_private_file(root: Path, data: bytes) -> Path:
    """Write a complete unique file before making its final name visible."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=".pending-", dir=root)
    temporary_path = Path(temporary_name)
    final_path = root / f"raster-{uuid.uuid4().hex}.bin"
    try:
        with os.fdopen(descriptor, "wb") as file_handle:
            descriptor = -1
            written = file_handle.write(data)
            if written != len(data):
                raise OSError("Short write while storing private raster data.")
            file_handle.flush()
        os.replace(temporary_path, final_path)
        return final_path
    except BaseException:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        for path in (temporary_path, final_path):
            with suppress(OSError):
                path.unlink()
        raise


def _unlink_paths(paths: set[Path]) -> None:
    for path in paths:
        with suppress(OSError):
            path.unlink()


def _read_exact_file(path: Path, expected_size: int) -> bytes:
    """Read at most one byte beyond a recorded private-file size."""
    with path.open("rb") as file_handle:
        data = file_handle.read(expected_size + 1)
        if len(data) != expected_size or file_handle.read(1):
            raise RasterCacheError("Private file size does not match its metadata.")
        return data


class RasterCache:
    """Own page rasters in RAM first and spill evicted buffers privately.

    Pixel, LRU, memo, generation, and spill changes share one data lock.
    Metadata readers use a separate short lock and see the last committed view
    while a pixel read or spill is in progress. Mutations acquire the data lock
    before publishing through the metadata lock. Generation changes and close
    wait for active pixel work to finish before releasing its buffers/directory.
    """

    def __init__(
        self,
        max_decoded_bytes: int = DEFAULT_DECODED_BUDGET,
        *,
        directory: str | Path | None = None,
    ) -> None:
        if not isinstance(max_decoded_bytes, int) or isinstance(max_decoded_bytes, bool):
            raise TypeError("max_decoded_bytes must be an integer.")
        if max_decoded_bytes < 0:
            raise ValueError("max_decoded_bytes cannot be negative.")

        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="pycroppdf-previews-",
            dir=directory,
        )
        self._root = Path(self._temporary_directory.name)
        self._max_decoded_bytes = max_decoded_bytes
        self._lock = RLock()
        self._metadata_lock = Lock()
        self._generation = 0
        self._page_count = 0
        self._closed = False
        self._next_page_version = 1
        self._pages: dict[int, _PageRecord] = {}
        self._assets: dict[str, bytes] = {}
        self._decoded: OrderedDict[_RasterBuffer, None] = OrderedDict()
        self._raw_decoded_bytes = 0
        self._memos: OrderedDict[tuple[int, ...], _MemoRecord] = OrderedDict()
        self._memo_bytes = 0
        self._decoded_bytes = 0
        self._peak_decoded_bytes = 0
        self._metadata_view = _MetadataView(0, 0, False, (), (), frozenset())

    @property
    def root(self) -> Path:
        return self._root

    @property
    def generation(self) -> int:
        with self._metadata_lock:
            return self._metadata_view.generation

    @property
    def page_count(self) -> int:
        with self._metadata_lock:
            return self._metadata_view.page_count

    @property
    def closed(self) -> bool:
        with self._metadata_lock:
            return self._metadata_view.closed

    @property
    def ready_pages(self) -> frozenset[int]:
        with self._metadata_lock:
            return self._metadata_view.ready_pages

    def page_versions(self, generation: int) -> tuple[int | None, ...] | None:
        """Return a coherent snapshot of current per-page version tokens."""
        with self._metadata_lock:
            view = self._metadata_view
            if view.closed or generation != view.generation:
                return None
            return view.page_versions

    @property
    def decoded_bytes(self) -> int:
        with self._lock:
            return self._decoded_bytes

    @property
    def peak_decoded_bytes(self) -> int:
        with self._lock:
            return self._peak_decoded_bytes

    @property
    def decoded_entry_count(self) -> int:
        with self._lock:
            return len(self._decoded) + len(self._memos)

    @property
    def memo_budget_bytes(self) -> int:
        return min(128 * 1024 * 1024, self._max_decoded_bytes // 4)

    @property
    def disk_bytes(self) -> int:
        with self._lock:
            total = 0
            for path in self._referenced_paths():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
            return total

    def begin_generation(
        self,
        page_count: int,
        *,
        preserve: Mapping[int, int] | None = None,
    ) -> int:
        """Advance generation, reusing preserved buffers without spilling them."""
        with self._lock:
            self._ensure_open()
            if not isinstance(page_count, int) or isinstance(page_count, bool):
                raise TypeError("page_count must be an integer.")
            if page_count < 0:
                raise ValueError("page_count cannot be negative.")

            old_buffers = self._all_buffers()
            old_paths = self._referenced_paths()
            next_generation = self._generation + 1
            remapped: dict[int, _PageRecord] = {}
            if preserve is not None:
                for new_page, old_page in preserve.items():
                    if (
                        not isinstance(new_page, int)
                        or isinstance(new_page, bool)
                        or not 0 <= new_page < page_count
                    ):
                        raise IndexError(
                            f"Preserved page index {new_page!r} is outside the new document."
                        )
                    if (
                        not isinstance(old_page, int)
                        or isinstance(old_page, bool)
                        or not 0 <= old_page < self._page_count
                    ):
                        raise IndexError(
                            f"Preserved page index {old_page!r} is outside the current document."
                        )
                    old_record = self._pages.get(old_page)
                    if old_record is not None:
                        info = replace(
                            old_record.info,
                            generation=next_generation,
                            page=new_page,
                        )
                        remapped[new_page] = replace(old_record, info=info)

            self._generation = next_generation
            self._page_count = page_count
            self._pages = remapped
            live_buffers = self._all_buffers()
            self._prune_decoded(live_buffers)
            if remapped:
                self._prune_invalid_memos()
            else:
                self._clear_memos()
            self._release_unreferenced_buffers(old_buffers - live_buffers)
            self._publish_metadata_locked(clear_assets=True)
            _unlink_paths(old_paths - self._referenced_paths())
            return self._generation

    def write_page(
        self,
        generation: int,
        page: int,
        *,
        logical_dimensions: tuple[int, int],
        pixels: RasterData,
        thumbnail: RasterData,
    ) -> RasterInfo | None:
        """Publish a complete page while keeping decoded buffers RAM-resident if they fit."""
        with self._lock:
            if not self._is_current(generation):
                return None
            self._check_page_index(page)
            logical_width, logical_height = self._validate_dimensions(
                logical_dimensions,
                "logical_dimensions",
            )
            self._validate_raster(pixels, "pixels")
            self._validate_raster(thumbnail, "thumbnail")

            full_buffer = _RasterBuffer(
                width=pixels.width,
                height=pixels.height,
                stride=pixels.stride,
                resident=pixels,
            )
            thumbnail_buffer = _RasterBuffer(
                width=thumbnail.width,
                height=thumbnail.height,
                stride=thumbnail.stride,
                resident=thumbnail,
            )
            candidates = (full_buffer, thumbnail_buffer)
            candidate_set = set(candidates)
            candidate_paths: set[Path] = set()
            future_versions = {
                record.version
                for existing_page, record in self._pages.items()
                if existing_page != page
            }
            future_versions.add(self._next_page_version)
            future_memo_bytes = sum(
                len(record.data)
                for record in self._memos.values()
                if self._versions_are_live(record.page_versions, future_versions)
            )
            raw_budget = self._max_decoded_bytes - future_memo_bytes

            surviving_pages = {
                old_page: record for old_page, record in self._pages.items() if old_page != page
            }
            surviving_buffers = self._buffers_in(surviving_pages)
            plan = [
                buffer
                for buffer in self._decoded
                if buffer in surviving_buffers and buffer.resident is not None
            ]

            try:
                for buffer in candidates:
                    if buffer.byte_size > raw_budget:
                        buffer.spill = self._create_spill(buffer)
                        candidate_paths.add(buffer.spill.path)
                        buffer.resident = None
                    else:
                        plan.append(buffer)

                planned_bytes = sum(buffer.byte_size for buffer in plan)
                while planned_bytes > raw_budget:
                    victim = plan.pop(0)
                    planned_bytes -= victim.byte_size
                    if victim in candidate_set:
                        if victim.spill is None:
                            victim.spill = self._create_spill(victim)
                            candidate_paths.add(victim.spill.path)
                        victim.resident = None
                    else:
                        self._evict_buffer(victim)
            except BaseException:
                _unlink_paths(candidate_paths)
                raise

            info = RasterInfo(
                generation=generation,
                page=page,
                logical_width=logical_width,
                logical_height=logical_height,
                width=pixels.width,
                height=pixels.height,
                stride=pixels.stride,
                thumbnail_width=thumbnail.width,
                thumbnail_height=thumbnail.height,
                thumbnail_stride=thumbnail.stride,
            )
            version = self._next_page_version
            new_record = _PageRecord(
                info=info,
                full_buffer=full_buffer,
                thumbnail_buffer=thumbnail_buffer,
                version=version,
            )
            old_buffers = self._all_buffers()
            old_paths = self._referenced_paths()
            self._pages[page] = new_record
            self._next_page_version += 1
            live_buffers = self._all_buffers()
            self._prune_decoded(live_buffers)
            self._release_unreferenced_buffers(old_buffers - live_buffers)
            self._prune_invalid_memos()
            for buffer in candidates:
                if buffer.resident is not None:
                    self._add_decoded(buffer)
            self._publish_metadata_locked()
            _unlink_paths(old_paths - self._referenced_paths())
            candidate_paths.clear()
            return info

    def info(self, page: int, *, generation: int | None = None) -> RasterInfo | None:
        """Return metadata only when it belongs to the requested live generation."""
        with self._metadata_lock:
            view = self._metadata_view
            requested_generation = view.generation if generation is None else generation
            if view.closed or requested_generation != view.generation:
                return None
            self._check_metadata_page_index(page, view.page_count)
            return view.page_info[page]

    def read_page(
        self,
        page: int,
        *,
        generation: int | None = None,
        thumbnail: bool = False,
        cache: bool = True,
    ) -> RasterData | None:
        """Read a ready RGB buffer, optionally retaining it in the decoded LRU."""
        with self._lock:
            requested_generation = self._generation if generation is None else generation
            if self._closed or requested_generation != self._generation:
                return None
            self._check_page_index(page)
            record = self._pages.get(page)
            if record is None:
                return None

            buffer = record.thumbnail_buffer if thumbnail else record.full_buffer
            if buffer.resident is not None:
                if cache:
                    self._touch_decoded(buffer)
                return buffer.resident

            if buffer.spill is None:
                self._invalidate_buffer(buffer)
                raise RasterCacheError(f"Raster page {page} has no readable buffer backing.")

            try:
                data = self._read_spill(buffer)
            except (OSError, zlib.error, RasterCacheError) as error:
                self._invalidate_buffer(buffer)
                raise RasterCacheError(f"Could not read raster page {page}.") from error

            raw_budget = self._max_decoded_bytes - self._memo_bytes
            if not cache or data is None or len(data.samples) > raw_budget:
                return data

            while self._raw_decoded_bytes + len(data.samples) > raw_budget:
                victim = next(iter(self._decoded))
                self._evict_buffer(victim)
            buffer.resident = data
            self._add_decoded(buffer)
            return data

    def read_memo(self, generation: int, key: tuple[int, ...]) -> bytes | None:
        """Return a current derived result and mark it most recently used."""
        with self._lock:
            if not self._is_current(generation):
                return None
            self._validate_memo_key(key)
            record = self._memos.get(key)
            if record is None:
                return None
            if not self._memo_dependencies_live(record.page_versions):
                self._remove_memo(key)
                return None
            self._memos.move_to_end(key)
            return record.data

    def write_memo(
        self,
        generation: int,
        key: tuple[int, ...],
        data: bytes,
        *,
        page_versions: tuple[int, ...],
    ) -> bool:
        """Retain an immutable derived result while its page-version inputs are live."""
        with self._lock:
            if not self._is_current(generation):
                return False
            self._validate_memo_key(key)
            self._validate_page_versions(page_versions)
            if not isinstance(data, bytes):
                raise TypeError("Memo data must be bytes.")
            if not self._memo_dependencies_live(page_versions):
                return False

            memo_budget = self.memo_budget_bytes
            if memo_budget == 0 or len(data) > memo_budget:
                return False

            old_record = self._memos.get(key)
            planned_memo_bytes = self._memo_bytes
            if old_record is not None:
                planned_memo_bytes -= len(old_record.data)
            evicted_keys: list[tuple[int, ...]] = []
            for existing_key, existing_record in self._memos.items():
                if existing_key == key:
                    continue
                if planned_memo_bytes + len(data) <= memo_budget:
                    break
                planned_memo_bytes -= len(existing_record.data)
                evicted_keys.append(existing_key)

            new_memo_bytes = planned_memo_bytes + len(data)
            raw_budget = self._max_decoded_bytes - new_memo_bytes
            try:
                while self._raw_decoded_bytes > raw_budget:
                    victim = next(iter(self._decoded))
                    self._evict_buffer(victim)
            except RasterCacheError:
                raise

            for evicted_key in evicted_keys:
                self._remove_memo(evicted_key)
            if old_record is not None:
                self._remove_memo(key)
            self._memos[key] = _MemoRecord(data=data, page_versions=page_versions)
            self._memo_bytes += len(data)
            self._refresh_decoded_metrics()
            return True

    def retain_memos(
        self,
        generation: int,
        keys: Collection[tuple[int, ...]],
    ) -> bool:
        """Keep only requested keys whose version dependencies are still current."""
        with self._lock:
            if not self._is_current(generation):
                return False
            retained_keys: set[tuple[int, ...]] = set()
            for key in keys:
                self._validate_memo_key(key)
                retained_keys.add(key)
            for key, record in tuple(self._memos.items()):
                if key not in retained_keys or not self._memo_dependencies_live(
                    record.page_versions
                ):
                    self._remove_memo(key)
            return True

    def discard_memo(self, generation: int, key: tuple[int, ...]) -> bool:
        """Soft-invalidate one current-generation derived result."""
        with self._lock:
            if not self._is_current(generation):
                return False
            self._validate_memo_key(key)
            return self._remove_memo(key)

    def write_asset(self, generation: int, key: str, data: bytes) -> bool:
        """Replace one current-generation opaque in-memory asset."""
        with self._metadata_lock:
            view = self._metadata_view
            if view.closed or generation != view.generation:
                return False
            self._validate_asset_key(key)
            if not isinstance(data, bytes):
                raise TypeError("Asset data must be bytes.")
            self._assets[key] = data
            return True

    def read_asset(self, generation: int, key: str) -> bytes | None:
        """Return an immutable current-generation opaque asset."""
        with self._metadata_lock:
            view = self._metadata_view
            if view.closed or generation != view.generation:
                return None
            self._validate_asset_key(key)
            return self._assets.get(key)

    def close(self) -> None:
        """Clear all state and remove the private temporary directory."""
        with self._lock:
            if self._closed:
                return
            old_buffers = self._all_buffers()
            self._closed = True
            self._page_count = 0
            self._pages.clear()
            self._clear_memos()
            self._clear_decoded()
            self._release_unreferenced_buffers(old_buffers)
            self._publish_metadata_locked(clear_assets=True)
            self._temporary_directory.cleanup()

    def _is_current(self, generation: int) -> bool:
        return not self._closed and generation == self._generation

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Raster cache is closed.")

    def _check_page_index(self, page: int) -> None:
        if not isinstance(page, int) or isinstance(page, bool) or not 0 <= page < self._page_count:
            raise IndexError(f"Page index {page!r} is outside the current document.")

    @staticmethod
    def _check_metadata_page_index(page: int, page_count: int) -> None:
        if not isinstance(page, int) or isinstance(page, bool) or not 0 <= page < page_count:
            raise IndexError(f"Page index {page!r} is outside the current document.")

    @staticmethod
    def _validate_dimensions(dimensions: tuple[int, int], label: str) -> tuple[int, int]:
        if (
            not isinstance(dimensions, tuple)
            or len(dimensions) != 2
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
                for value in dimensions
            )
        ):
            raise ValueError(f"{label} must contain two positive integers.")
        return dimensions

    @staticmethod
    def _validate_raster(raster: RasterData, label: str) -> None:
        if not isinstance(raster, RasterData):
            raise TypeError(f"{label} must be RasterData.")
        if (
            not isinstance(raster.width, int)
            or isinstance(raster.width, bool)
            or raster.width <= 0
            or not isinstance(raster.height, int)
            or isinstance(raster.height, bool)
            or raster.height <= 0
            or not isinstance(raster.stride, int)
            or isinstance(raster.stride, bool)
            or raster.stride < raster.width * 3
            or not isinstance(raster.samples, bytes)
            or len(raster.samples) != raster.stride * raster.height
        ):
            raise ValueError(
                f"{label} must be a positive RGB888 raster with a valid stride and size."
            )

    @staticmethod
    def _validate_asset_key(key: str) -> None:
        if not isinstance(key, str) or _ASSET_KEY.fullmatch(key) is None:
            raise ValueError("Asset keys must match [a-z0-9_-]{1,64}.")

    @staticmethod
    def _validate_memo_key(key: tuple[int, ...]) -> None:
        if not isinstance(key, tuple) or any(
            not isinstance(page, int) or isinstance(page, bool) for page in key
        ):
            raise ValueError("Memo keys must be tuples of integers.")

    @staticmethod
    def _validate_page_versions(page_versions: tuple[int, ...]) -> None:
        if not isinstance(page_versions, tuple) or any(
            not isinstance(version, int) or isinstance(version, bool) for version in page_versions
        ):
            raise ValueError("Memo page_versions must be a tuple of integers.")

    @staticmethod
    def _decompress_bounded(compressed: bytes, expected_size: int) -> bytes:
        decompressor = zlib.decompressobj()
        samples = decompressor.decompress(compressed, expected_size + 1)
        if (
            len(samples) != expected_size
            or not decompressor.eof
            or decompressor.unconsumed_tail
            or decompressor.unused_data
        ):
            raise RasterCacheError("Compressed raster has an invalid size or stream boundary.")
        return samples

    def _create_spill(self, buffer: _RasterBuffer) -> _SpillInfo:
        data = buffer.resident
        if data is None:
            raise RasterCacheError("Cannot spill a raster buffer without resident data.")
        try:
            compressed = zlib.compress(data.samples, level=1)
            checksum = zlib.crc32(data.samples)
            path = _write_private_file(self._root, compressed)
        except (OSError, zlib.error) as error:
            raise RasterCacheError("Could not spill raster buffer data.") from error
        return _SpillInfo(path=path, compressed_size=len(compressed), checksum=checksum)

    def _read_spill(self, buffer: _RasterBuffer) -> RasterData:
        spill = buffer.spill
        if spill is None:
            raise RasterCacheError("Raster buffer has no spill file.")
        compressed = _read_exact_file(spill.path, spill.compressed_size)
        samples = self._decompress_bounded(compressed, buffer.byte_size)
        if zlib.crc32(samples) != spill.checksum:
            raise RasterCacheError("Raster CRC32 does not match its metadata.")
        return RasterData(
            width=buffer.width,
            height=buffer.height,
            stride=buffer.stride,
            samples=samples,
        )

    def _evict_buffer(self, buffer: _RasterBuffer) -> None:
        if buffer.resident is None:
            self._drop_decoded(buffer)
            return
        if buffer.spill is None:
            buffer.spill = self._create_spill(buffer)
        self._drop_decoded(buffer)
        buffer.resident = None

    def _add_decoded(self, buffer: _RasterBuffer) -> None:
        if buffer.resident is None or buffer in self._decoded:
            return
        self._decoded[buffer] = None
        self._raw_decoded_bytes += len(buffer.resident.samples)
        self._refresh_decoded_metrics()

    def _touch_decoded(self, buffer: _RasterBuffer) -> None:
        if buffer in self._decoded:
            self._decoded.move_to_end(buffer)

    def _drop_decoded(self, buffer: _RasterBuffer) -> None:
        if buffer in self._decoded:
            self._decoded.pop(buffer)
            if buffer.resident is not None:
                self._raw_decoded_bytes -= len(buffer.resident.samples)
                self._refresh_decoded_metrics()

    def _prune_decoded(self, live_buffers: set[_RasterBuffer]) -> None:
        retained: OrderedDict[_RasterBuffer, None] = OrderedDict()
        retained_bytes = 0
        for buffer in self._decoded:
            if buffer in live_buffers and buffer.resident is not None:
                retained[buffer] = None
                retained_bytes += len(buffer.resident.samples)
        self._decoded = retained
        self._raw_decoded_bytes = retained_bytes
        self._refresh_decoded_metrics()

    def _clear_decoded(self) -> None:
        for buffer in self._decoded:
            buffer.resident = None
        self._decoded.clear()
        self._raw_decoded_bytes = 0
        self._refresh_decoded_metrics()

    def _all_buffers(self) -> set[_RasterBuffer]:
        return self._buffers_in(self._pages)

    @staticmethod
    def _buffers_in(pages: Mapping[int, _PageRecord]) -> set[_RasterBuffer]:
        buffers: set[_RasterBuffer] = set()
        for record in pages.values():
            buffers.add(record.full_buffer)
            buffers.add(record.thumbnail_buffer)
        return buffers

    def _release_unreferenced_buffers(self, buffers: set[_RasterBuffer]) -> None:
        for buffer in buffers:
            self._drop_decoded(buffer)
            buffer.resident = None

    def _invalidate_buffer(self, damaged: _RasterBuffer) -> None:
        old_buffers = self._all_buffers()
        old_paths = self._referenced_paths()
        invalid_pages = {
            page
            for page, record in self._pages.items()
            if record.full_buffer is damaged or record.thumbnail_buffer is damaged
        }
        for page in invalid_pages:
            self._pages.pop(page, None)
        live_buffers = self._all_buffers()
        self._prune_decoded(live_buffers)
        self._prune_invalid_memos()
        self._release_unreferenced_buffers(old_buffers - live_buffers)
        self._publish_metadata_locked()
        _unlink_paths(old_paths - self._referenced_paths())

    def _referenced_paths(self) -> set[Path]:
        paths: set[Path] = set()
        for buffer in self._all_buffers():
            if buffer.spill is not None:
                paths.add(buffer.spill.path)
        return paths

    def _memo_dependencies_live(self, page_versions: tuple[int, ...]) -> bool:
        live_versions = {record.version for record in self._pages.values()}
        return self._versions_are_live(page_versions, live_versions)

    @staticmethod
    def _versions_are_live(page_versions: tuple[int, ...], live_versions: set[int]) -> bool:
        return all(version in live_versions for version in page_versions)

    def _prune_invalid_memos(self) -> None:
        live_versions = {record.version for record in self._pages.values()}
        for key, record in tuple(self._memos.items()):
            if not self._versions_are_live(record.page_versions, live_versions):
                self._remove_memo(key)

    def _clear_memos(self) -> None:
        self._memos.clear()
        self._memo_bytes = 0
        self._refresh_decoded_metrics()

    def _remove_memo(self, key: tuple[int, ...]) -> bool:
        record = self._memos.pop(key, None)
        if record is None:
            return False
        self._memo_bytes -= len(record.data)
        self._refresh_decoded_metrics()
        return True

    def _refresh_decoded_metrics(self) -> None:
        self._decoded_bytes = self._raw_decoded_bytes + self._memo_bytes
        self._peak_decoded_bytes = max(self._peak_decoded_bytes, self._decoded_bytes)

    def _publish_metadata_locked(self, *, clear_assets: bool = False) -> None:
        page_info: list[RasterInfo | None] = []
        page_versions: list[int | None] = []
        ready_pages: set[int] = set()
        for page in range(self._page_count):
            record = self._pages.get(page)
            page_info.append(None if record is None else record.info)
            page_versions.append(None if record is None else record.version)
            if record is not None:
                ready_pages.add(page)
        view = _MetadataView(
            generation=self._generation,
            page_count=self._page_count,
            closed=self._closed,
            page_info=tuple(page_info),
            page_versions=tuple(page_versions),
            ready_pages=frozenset(ready_pages),
        )
        with self._metadata_lock:
            if clear_assets:
                self._assets.clear()
            self._metadata_view = view
