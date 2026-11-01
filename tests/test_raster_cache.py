from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

import pycroppdf.raster_cache as raster_cache_module
from pycroppdf.raster_cache import (
    DEFAULT_DECODED_BUDGET,
    RasterCache,
    RasterCacheError,
    RasterData,
)


def _raster(width: int, height: int, value: int, *, stride: int | None = None) -> RasterData:
    row_stride = width * 3 if stride is None else stride
    return RasterData(
        width=width,
        height=height,
        stride=row_stride,
        samples=bytes([value]) * (row_stride * height),
    )


def _write_page(
    cache: RasterCache,
    generation: int,
    page: int,
    value: int,
    *,
    dimensions: tuple[int, int] = (1, 1),
    thumbnail_dimensions: tuple[int, int] = (1, 1),
) -> None:
    cache.write_page(
        generation,
        page,
        logical_dimensions=(dimensions[0] * 2, dimensions[1] * 2),
        pixels=_raster(*dimensions, value),
        thumbnail=_raster(*thumbnail_dimensions, value + 1),
    )


def test_initial_state_private_root_and_idempotent_close(tmp_path):
    cache = RasterCache(directory=tmp_path)
    root = cache.root

    assert cache.generation == 0
    assert cache.page_count == 0
    assert cache.ready_pages == frozenset()
    assert cache.closed is False
    assert cache.decoded_bytes == 0
    assert cache.peak_decoded_bytes == 0
    assert cache.decoded_entry_count == 0
    assert cache.disk_bytes == 0
    assert root.is_dir()
    if os.name == "posix":
        assert root.stat().st_mode & 0o777 == 0o700

    cache.close()
    cache.close()
    assert cache.closed is True
    assert cache.root == root
    assert not root.exists()


def test_default_budget_is_five_hundred_mib_and_constructor_validates(tmp_path):
    cache = RasterCache(directory=tmp_path)
    assert DEFAULT_DECODED_BUDGET == 500 * 1024 * 1024
    assert cache._max_decoded_bytes == DEFAULT_DECODED_BUDGET
    assert cache.memo_budget_bytes == 125 * 1024 * 1024
    cache.close()

    larger_cache = RasterCache(1024 * 1024 * 1024, directory=tmp_path)
    assert larger_cache.memo_budget_bytes == 128 * 1024 * 1024
    larger_cache.close()

    with pytest.raises(ValueError, match="negative"):
        RasterCache(-1, directory=tmp_path)
    with pytest.raises(TypeError, match="integer"):
        RasterCache(1.5, directory=tmp_path)


def test_under_budget_write_read_and_preserve_do_no_spill_work(tmp_path, monkeypatch):
    cache = RasterCache(directory=tmp_path)
    generation = cache.begin_generation(1)
    calls: list[str] = []
    real_compress = raster_cache_module.zlib.compress
    real_crc32 = raster_cache_module.zlib.crc32
    real_write = raster_cache_module._write_private_file

    def tracked_compress(data: bytes, *, level: int) -> bytes:
        calls.append("compress")
        return real_compress(data, level=level)

    def tracked_crc32(data: bytes) -> int:
        calls.append("crc32")
        return real_crc32(data)

    def tracked_write(root: Path, data: bytes) -> Path:
        calls.append("write")
        return real_write(root, data)

    monkeypatch.setattr(raster_cache_module.zlib, "compress", tracked_compress)
    monkeypatch.setattr(raster_cache_module.zlib, "crc32", tracked_crc32)
    monkeypatch.setattr(raster_cache_module, "_write_private_file", tracked_write)

    pixels = _raster(2, 1, 10, stride=8)
    thumbnail = _raster(1, 1, 11)
    info = cache.write_page(
        generation,
        0,
        logical_dimensions=(40, 20),
        pixels=pixels,
        thumbnail=thumbnail,
    )
    assert info is not None
    assert (info.logical_width, info.logical_height) == (40, 20)
    assert (info.width, info.height, info.stride) == (2, 1, 8)
    assert (info.thumbnail_width, info.thumbnail_height, info.thumbnail_stride) == (1, 1, 3)
    assert cache.read_page(0, generation=generation) is pixels
    assert cache.read_page(0, generation=generation, cache=False) is pixels

    order_before_stream = tuple(cache._decoded)
    assert cache.read_page(0, generation=generation, thumbnail=True, cache=False) is thumbnail
    assert tuple(cache._decoded) == order_before_stream

    next_generation = cache.begin_generation(2, preserve={0: 0, 1: 0})
    assert next_generation == generation + 1
    assert cache.read_page(0, generation=next_generation) is pixels
    assert cache.read_page(1, generation=next_generation, thumbnail=True) is thumbnail
    assert cache.decoded_bytes == len(pixels.samples) + len(thumbnail.samples)
    assert cache.decoded_entry_count == 2
    assert cache.disk_bytes == 0
    assert list(cache.root.iterdir()) == []
    assert calls == []
    cache.close()


def test_exact_budget_keeps_both_new_buffers_resident_without_files(tmp_path):
    cache = RasterCache(6, directory=tmp_path)
    generation = cache.begin_generation(1)
    pixels = _raster(1, 1, 40)
    thumbnail = _raster(1, 1, 41)

    cache.write_page(
        generation,
        0,
        logical_dimensions=(2, 2),
        pixels=pixels,
        thumbnail=thumbnail,
    )

    assert cache.read_page(0, generation=generation) is pixels
    assert cache.read_page(0, generation=generation, thumbnail=True) is thumbnail
    assert cache.decoded_bytes == 6
    assert cache.decoded_entry_count == 2
    assert cache.peak_decoded_bytes == 6
    assert cache.disk_bytes == 0
    assert list(cache.root.iterdir()) == []
    cache.close()


def test_lru_spills_promotes_and_reuses_backing_without_rewriting(tmp_path, monkeypatch):
    cache = RasterCache(6, directory=tmp_path)
    generation = cache.begin_generation(3)
    real_write = raster_cache_module._write_private_file
    write_calls: list[bytes] = []

    def tracked_write(root: Path, data: bytes) -> Path:
        write_calls.append(data)
        return real_write(root, data)

    monkeypatch.setattr(raster_cache_module, "_write_private_file", tracked_write)
    _write_page(cache, generation, 0, 10)
    assert cache.decoded_bytes == 6
    assert cache._pages[0].full_path is None
    assert cache._pages[0].thumbnail_path is None

    _write_page(cache, generation, 1, 20)
    full_zero_path = cache._pages[0].full_path
    thumb_zero_path = cache._pages[0].thumbnail_path
    assert full_zero_path is not None and full_zero_path.exists()
    assert thumb_zero_path is not None and thumb_zero_path.exists()
    assert cache.decoded_bytes == 6
    assert cache.decoded_entry_count == 2

    _write_page(cache, generation, 2, 30)
    assert cache.decoded_bytes == 6
    assert cache.peak_decoded_bytes == 6
    calls_after_spills = len(write_calls)

    full_zero = cache.read_page(0, generation=generation)
    assert full_zero is not None and set(full_zero.samples) == {10}
    order_after_promotion = tuple(cache._decoded)
    assert cache.read_page(0, generation=generation, cache=False) is full_zero
    assert tuple(cache._decoded) == order_after_promotion
    assert cache.read_page(0, generation=generation) is full_zero

    thumbnail_zero = cache.read_page(0, generation=generation, thumbnail=True)
    assert thumbnail_zero is not None and set(thumbnail_zero.samples) == {11}
    calls_after_promotion = len(write_calls)
    assert calls_after_promotion == calls_after_spills + 2

    # Evict the promoted full buffer, then promote it again from its original file.
    assert cache.read_page(1, generation=generation) is not None
    assert cache._pages[0].full_path == full_zero_path
    assert cache._pages[0].thumbnail_path == thumb_zero_path
    assert len(write_calls) == calls_after_promotion
    assert cache.decoded_bytes <= 6
    assert cache.disk_bytes > 0
    cache.close()


def test_cache_zero_and_oversized_reads_use_spill_without_retention(tmp_path, monkeypatch):
    cache = RasterCache(0, directory=tmp_path)
    generation = cache.begin_generation(1)
    _write_page(cache, generation, 0, 5, dimensions=(2, 1))
    record = cache._pages[0]
    assert record.full_path is not None and record.full_path.exists()
    assert record.thumbnail_path is not None and record.thumbnail_path.exists()
    assert cache.decoded_bytes == 0
    assert cache.decoded_entry_count == 0
    assert len(list(cache.root.iterdir())) == 2

    full_reads = 0
    real_read = raster_cache_module._read_exact_file

    def counted_read(path: Path, expected_size: int) -> bytes:
        nonlocal full_reads
        if path == record.full_path:
            full_reads += 1
        return real_read(path, expected_size)

    monkeypatch.setattr(raster_cache_module, "_read_exact_file", counted_read)
    first = cache.read_page(0, generation=generation, cache=False)
    assert first is not None and set(first.samples) == {5}
    assert cache.decoded_bytes == 0
    assert cache.read_page(0, generation=generation) == first
    assert cache.read_page(0, generation=generation) == first
    assert full_reads == 3
    assert cache.decoded_bytes == 0
    assert cache.decoded_entry_count == 0
    cache.close()


def test_five_hundred_pages_stay_within_tiny_residency_budget(tmp_path):
    cache = RasterCache(3, directory=tmp_path)
    generation = cache.begin_generation(500)
    for page in range(500):
        _write_page(cache, generation, page, page % 200, dimensions=(2, 1))
        assert cache.decoded_bytes == 3
        assert cache.decoded_entry_count == 1
        full = cache.read_page(page, generation=generation)
        thumbnail = cache.read_page(page, generation=generation, thumbnail=True)
        assert full is not None and len(full.samples) == 6
        assert thumbnail is not None and len(thumbnail.samples) == 3
        assert cache.decoded_bytes == 3
        assert cache.decoded_entry_count == 1

    assert cache.page_count == 500
    assert len(cache.ready_pages) == 500
    assert cache.peak_decoded_bytes == 3
    assert cache.disk_bytes > 0
    cache.close()


def test_generation_remap_reuses_buffers_once_and_full_invalidation_cleans(tmp_path):
    cache = RasterCache(64, directory=tmp_path)
    first_generation = cache.begin_generation(3)
    _write_page(cache, first_generation, 0, 10)
    _write_page(cache, first_generation, 2, 30)
    assert cache.write_asset(first_generation, "overlay", b"old bundle")
    old_record = cache._pages[0]
    assert cache.decoded_bytes == 12
    assert cache.disk_bytes == 0

    second_generation = cache.begin_generation(5, preserve={1: 0, 3: 1, 4: 0})
    assert second_generation == first_generation + 1
    assert cache.page_count == 5
    assert cache.ready_pages == frozenset({1, 4})
    assert cache.info(0, generation=first_generation) is None
    assert cache.read_page(0, generation=first_generation) is None
    info_one = cache.info(1)
    info_four = cache.info(4)
    assert info_one is not None and (info_one.generation, info_one.page) == (second_generation, 1)
    assert info_four is not None and (info_four.generation, info_four.page) == (
        second_generation,
        4,
    )
    assert (info_one.logical_width, info_one.logical_height) == (2, 2)
    assert (info_one.width, info_one.height, info_one.stride) == (1, 1, 3)
    assert cache._pages[1].full_buffer is old_record.full_buffer
    assert cache._pages[1].full_buffer is cache._pages[4].full_buffer
    assert cache._pages[1].thumbnail_buffer is cache._pages[4].thumbnail_buffer
    assert cache.read_page(1, generation=second_generation) is cache.read_page(
        4, generation=second_generation
    )
    assert cache.decoded_bytes == 6
    assert cache.decoded_entry_count == 2
    assert cache.read_asset(second_generation, "overlay") is None
    assert cache.disk_bytes == 0
    assert list(cache.root.iterdir()) == []

    third_generation = cache.begin_generation(2)
    assert third_generation == second_generation + 1
    assert cache.ready_pages == frozenset()
    assert cache.decoded_bytes == 0
    assert cache.decoded_entry_count == 0
    assert cache.disk_bytes == 0
    assert list(cache.root.iterdir()) == []
    cache.close()


def test_generation_validation_and_stale_access_behavior(tmp_path):
    cache = RasterCache(directory=tmp_path)
    generation = cache.begin_generation(1)
    with pytest.raises(ValueError, match="page_count"):
        cache.begin_generation(-1)
    with pytest.raises(IndexError):
        cache.begin_generation(1, preserve={1: 0})
    with pytest.raises(IndexError):
        cache.begin_generation(1, preserve={0: 1})
    with pytest.raises(IndexError):
        cache.info(1)
    with pytest.raises(IndexError):
        cache.read_page(1)
    assert (
        cache.write_page(
            generation - 1,
            0,
            logical_dimensions=(2, 2),
            pixels=_raster(1, 1, 1),
            thumbnail=_raster(1, 1, 1),
        )
        is None
    )
    cache.close()
    with pytest.raises(RuntimeError, match="closed"):
        cache.begin_generation(1)
    assert cache.info(0) is None
    assert cache.read_page(0) is None
    assert cache.read_asset(generation, "asset") is None


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_missing_or_corrupt_spilled_buffer_invalidates_aliases_and_siblings(tmp_path, damage):
    cache = RasterCache(0, directory=tmp_path)
    generation = cache.begin_generation(3)
    _write_page(cache, generation, 0, 9)
    _write_page(cache, generation, 2, 29)
    cache.begin_generation(3, preserve={0: 0, 1: 0, 2: 2})
    damaged_record = cache._pages[0]
    damaged_path = damaged_record.full_path
    sibling_path = damaged_record.thumbnail_path
    survivor_paths = {cache._pages[2].full_path, cache._pages[2].thumbnail_path}
    assert damaged_path is not None and sibling_path is not None
    assert all(path is not None for path in survivor_paths)

    if damage == "missing":
        damaged_path.unlink()
    else:
        damaged_path.write_bytes(b"not compressed raster")

    with pytest.raises(RasterCacheError):
        cache.read_page(0, thumbnail=False, cache=False)

    assert cache.ready_pages == frozenset({2})
    assert cache.decoded_bytes == 0
    assert cache.decoded_entry_count == 0
    assert not sibling_path.exists()
    assert all(path is not None and path.exists() for path in survivor_paths)
    assert cache.disk_bytes == sum(
        path.stat().st_size for path in survivor_paths if path is not None
    )
    assert cache.info(1) is None
    cache.close()


def test_crc32_detects_a_different_valid_zlib_stream(tmp_path):
    cache = RasterCache(0, directory=tmp_path)
    generation = cache.begin_generation(2)
    _write_page(cache, generation, 0, 13)
    _write_page(cache, generation, 1, 37)
    path_zero = cache._pages[0].full_path
    path_one = cache._pages[1].full_path
    assert path_zero is not None and path_one is not None
    stream_zero = path_zero.read_bytes()
    stream_one = path_one.read_bytes()
    assert raster_cache_module.zlib.decompress(stream_zero) == bytes([13]) * 3
    assert raster_cache_module.zlib.decompress(stream_one) == bytes([37]) * 3

    path_zero.write_bytes(stream_one)
    with pytest.raises(RasterCacheError, match="Could not read raster page"):
        cache.read_page(0, generation=generation)

    assert cache.ready_pages == frozenset({1})
    assert cache.info(0) is None
    assert cache._pages[1].full_path == path_one
    assert path_one.exists()
    cache.close()


def test_corruption_invalidates_only_memos_with_lost_dependency_versions(tmp_path):
    cache = RasterCache(12, directory=tmp_path)
    generation = cache.begin_generation(4)
    _write_page(cache, generation, 0, 13)
    _write_page(cache, generation, 2, 37)
    _write_page(cache, generation, 3, 57)
    versions = cache.page_versions(generation)
    assert versions is not None and versions[0] is not None and versions[2] is not None
    remapped = cache.begin_generation(4, preserve={0: 0, 1: 0, 2: 2, 3: 3})
    assert cache.write_memo(remapped, (800, 0), b"o", page_versions=(versions[0],))
    assert cache.write_memo(remapped, (800, 2), b"l", page_versions=(versions[2],))
    damaged_path = cache._pages[0].full_path
    sibling_path = cache._pages[0].thumbnail_path
    survivor_paths = {
        path
        for page in (2, 3)
        for path in (cache._pages[page].full_path, cache._pages[page].thumbnail_path)
        if path is not None
    }
    assert damaged_path is not None and sibling_path is not None
    assert damaged_path.exists() and sibling_path.exists()
    damaged_path.write_bytes(b"invalid spill")

    with pytest.raises(RasterCacheError):
        cache.read_page(0, generation=remapped)

    assert cache.ready_pages == frozenset({2, 3})
    assert cache.page_versions(remapped) == (
        None,
        None,
        versions[2],
        versions[3],
    )
    assert cache.read_memo(remapped, (800, 0)) is None
    assert cache.read_memo(remapped, (800, 2)) == b"l"
    assert not sibling_path.exists()
    assert all(path.exists() for path in survivor_paths)
    assert cache.disk_bytes == sum(path.stat().st_size for path in survivor_paths)
    cache.close()


def test_page_versions_are_monotonic_and_survive_remap_and_aliases(tmp_path):
    cache = RasterCache(64, directory=tmp_path)
    first_generation = cache.begin_generation(4)
    assert cache.page_versions(first_generation) == (None, None, None, None)
    _write_page(cache, first_generation, 0, 10)
    _write_page(cache, first_generation, 2, 30)
    versions = cache.page_versions(first_generation)
    assert versions is not None
    version_zero = versions[0]
    version_two = versions[2]
    assert version_zero is not None and version_two is not None
    assert version_zero < version_two
    assert versions == (version_zero, None, version_two, None)

    next_generation = cache.begin_generation(5, preserve={0: 0, 1: 0, 3: 2, 4: 0})
    assert cache.page_versions(first_generation) is None
    assert cache.page_versions(next_generation) == (
        version_zero,
        version_zero,
        None,
        version_two,
        version_zero,
    )
    _write_page(cache, next_generation, 0, 70)
    changed_versions = cache.page_versions(next_generation)
    assert changed_versions is not None
    assert changed_versions[0] is not None and changed_versions[0] > version_two
    assert changed_versions[1] == version_zero
    assert changed_versions[3] == version_two

    cache.close()
    assert cache.page_versions(next_generation) is None


def test_metadata_view_readers_finish_while_page_write_spill_is_blocked(tmp_path, monkeypatch):
    cache = RasterCache(0, directory=tmp_path)
    generation = cache.begin_generation(1)
    entered = threading.Event()
    release = threading.Event()
    reader_started = threading.Event()
    read_complete = threading.Event()
    write_result = []
    metadata_result = []
    errors = []
    real_write = raster_cache_module._write_private_file
    first_call = True

    def blocked_write(root: Path, data: bytes) -> Path:
        nonlocal first_call
        if first_call:
            first_call = False
            entered.set()
            if not release.wait(5):
                raise TimeoutError("metadata/write barrier timed out")
        return real_write(root, data)

    monkeypatch.setattr(raster_cache_module, "_write_private_file", blocked_write)

    def writer() -> None:
        try:
            write_result.append(
                cache.write_page(
                    generation,
                    0,
                    logical_dimensions=(2, 2),
                    pixels=_raster(1, 1, 21),
                    thumbnail=_raster(1, 1, 22),
                )
            )
        except BaseException as error:
            errors.append(error)

    def read_metadata() -> None:
        reader_started.set()
        try:
            metadata_result.append(
                (
                    cache.generation,
                    cache.page_count,
                    cache.closed,
                    cache.ready_pages,
                    cache.info(0),
                    cache.page_versions(generation),
                    cache.write_asset(generation, "overlay", b"frame"),
                    cache.read_asset(generation, "overlay"),
                )
            )
        except BaseException as error:
            errors.append(error)
        finally:
            read_complete.set()

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    assert entered.wait(5)
    metadata_thread = threading.Thread(target=read_metadata)
    metadata_thread.start()
    try:
        assert reader_started.wait(5)
        assert read_complete.wait(5), "metadata readers waited for the blocked raster spill"
    finally:
        release.set()
    writer_thread.join(5)
    metadata_thread.join(5)

    assert not writer_thread.is_alive()
    assert not metadata_thread.is_alive()
    assert errors == []
    assert metadata_result == [(generation, 1, False, frozenset(), None, (None,), True, b"frame")]
    assert len(write_result) == 1 and write_result[0] is not None
    assert cache.page_versions(generation) == (1,)
    assert cache.read_asset(generation, "overlay") == b"frame"
    cache.close()


def test_metadata_view_readers_finish_while_cold_spill_read_is_blocked(tmp_path, monkeypatch):
    cache = RasterCache(0, directory=tmp_path)
    generation = cache.begin_generation(1)
    _write_page(cache, generation, 0, 17)
    expected_info = cache.info(0)
    expected_versions = cache.page_versions(generation)
    entered = threading.Event()
    release = threading.Event()
    metadata_started = threading.Event()
    read_complete = threading.Event()
    read_result = []
    metadata_result = []
    errors = []
    real_read = raster_cache_module._read_exact_file

    def blocked_read(path: Path, expected_size: int) -> bytes:
        entered.set()
        if not release.wait(5):
            raise TimeoutError("metadata/read barrier timed out")
        return real_read(path, expected_size)

    monkeypatch.setattr(raster_cache_module, "_read_exact_file", blocked_read)

    def read_page() -> None:
        try:
            read_result.append(cache.read_page(0, generation=generation))
        except BaseException as error:
            errors.append(error)

    def read_metadata() -> None:
        metadata_started.set()
        try:
            metadata_result.append(
                (
                    cache.generation,
                    cache.page_count,
                    cache.ready_pages,
                    cache.info(0),
                    cache.page_versions(generation),
                )
            )
        except BaseException as error:
            errors.append(error)
        finally:
            read_complete.set()

    reader_thread = threading.Thread(target=read_page)
    reader_thread.start()
    assert entered.wait(5)
    metadata_thread = threading.Thread(target=read_metadata)
    metadata_thread.start()
    try:
        assert metadata_started.wait(5)
        assert read_complete.wait(5), "metadata readers waited for the blocked cold read"
    finally:
        release.set()
    reader_thread.join(5)
    metadata_thread.join(5)

    assert not reader_thread.is_alive()
    assert not metadata_thread.is_alive()
    assert errors == []
    assert metadata_result == [(generation, 1, frozenset({0}), expected_info, expected_versions)]
    assert read_result[0] is not None
    cache.close()


def test_memo_budget_lru_retain_discard_and_validation(tmp_path):
    cache = RasterCache(100, directory=tmp_path)
    generation = cache.begin_generation(2)
    _write_page(cache, generation, 0, 10)
    _write_page(cache, generation, 1, 30)
    versions = cache.page_versions(generation)
    assert versions is not None
    version_zero = versions[0]
    version_one = versions[1]
    assert version_zero is not None and version_one is not None
    assert cache.memo_budget_bytes == 25

    assert cache.write_memo(generation, (0,), b"a" * 10, page_versions=(version_zero,))
    assert cache.write_memo(generation, (1,), b"b" * 10, page_versions=(version_one,))
    assert cache.read_memo(generation, (0,)) == b"a" * 10
    assert cache.write_memo(generation, (), b"empty-key", page_versions=())
    assert cache.read_memo(generation, (0,)) == b"a" * 10
    assert cache.read_memo(generation, (1,)) is None
    assert cache.read_memo(generation, ()) == b"empty-key"
    assert cache.decoded_bytes == 12 + len(b"a" * 10) + len(b"empty-key")
    assert cache.decoded_entry_count == 6
    assert cache.disk_bytes == 0

    assert cache.retain_memos(generation, {(0,), ()})
    assert cache.read_memo(generation, (1,)) is None
    assert cache.discard_memo(generation, ()) is True
    assert cache.discard_memo(generation, ()) is False
    assert cache.read_memo(generation, ()) is None
    with pytest.raises(ValueError, match="tuples of integers"):
        cache.read_memo(generation, [0])  # type: ignore[arg-type]
    assert cache.write_memo(generation, (0,), b"x", page_versions=(version_one + 100,)) is False
    layout_key = (1280, 720, 8, version_zero)
    assert cache.write_memo(generation, layout_key, b"layout", page_versions=(version_zero,))
    assert cache.read_memo(generation, layout_key) == b"layout"
    assert cache.write_memo(generation, (0,), b"x" * 26, page_versions=(version_zero,)) is False
    assert cache.write_memo(generation, (4, 900), b"no-deps", page_versions=())
    with pytest.raises(ValueError, match="page_versions"):
        cache.write_memo(generation, (0,), b"bad", page_versions=[version_zero])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="tuples of integers"):
        cache.write_memo(generation, [0], b"bad", page_versions=(version_zero,))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="page_versions"):
        cache.write_memo(generation, (0,), b"bad", page_versions=(True,))
    with pytest.raises(TypeError, match="bytes"):
        cache.write_memo(generation, (0,), bytearray(b"bad"), page_versions=(version_zero,))  # type: ignore[arg-type]

    assert cache.retain_memos(generation - 1, set()) is False
    assert cache.read_memo(generation - 1, (0,)) is None
    assert cache.discard_memo(generation - 1, (0,)) is False
    assert cache.write_memo(generation - 1, (0,), b"stale", page_versions=(version_zero,)) is False
    cache.close()
    assert cache.read_memo(generation, (0,)) is None
    assert cache.discard_memo(generation, (0,)) is False
    assert cache.retain_memos(generation, {(0,)}) is False
    assert cache.write_memo(generation, (0,), b"closed", page_versions=(version_zero,)) is False


def test_memos_follow_page_versions_alias_remaps_and_full_invalidation(tmp_path):
    cache = RasterCache(128, directory=tmp_path)
    generation = cache.begin_generation(3)
    _write_page(cache, generation, 0, 11)
    _write_page(cache, generation, 2, 31)
    versions = cache.page_versions(generation)
    assert versions is not None and versions[0] is not None and versions[2] is not None
    version_zero = versions[0]
    version_two = versions[2]
    assert cache.write_memo(generation, (0,), b"zero", page_versions=(version_zero,))
    assert cache.write_memo(generation, (2,), b"two", page_versions=(version_two,))

    remapped = cache.begin_generation(4, preserve={0: 0, 1: 0, 2: 2})
    assert cache.page_versions(remapped) == (version_zero, version_zero, version_two, None)
    assert cache.read_memo(remapped, (0,)) == b"zero"
    assert cache.read_memo(remapped, (2,)) == b"two"
    assert cache.write_memo(
        remapped,
        (0, 1),
        b"alias pair",
        page_versions=(version_zero, version_zero),
    )
    assert cache.read_memo(remapped, (0, 1)) == b"alias pair"

    assert cache.retain_memos(remapped, {(0, 1), (2,)})
    assert cache.read_memo(remapped, (0,)) is None
    assert cache.read_memo(remapped, (2,)) == b"two"
    _write_page(cache, remapped, 1, 71)
    changed = cache.page_versions(remapped)
    assert changed is not None and changed[1] is not None
    assert changed[1] > version_two
    assert cache.read_memo(remapped, (0, 1)) == b"alias pair"
    assert cache.read_memo(remapped, (2,)) == b"two"

    _write_page(cache, remapped, 2, 91)
    changed_again = cache.page_versions(remapped)
    assert changed_again is not None and changed_again[2] is not None
    assert changed_again[2] > changed[1]
    assert cache.read_memo(remapped, (2,)) is None
    assert cache.read_memo(remapped, (0, 1)) == b"alias pair"
    assert cache.write_memo(remapped, (999, 1280, 720), b"generation-local", page_versions=())

    invalidated = cache.begin_generation(3)
    assert cache.page_versions(invalidated) == (None, None, None)
    assert cache.read_memo(invalidated, (2,)) is None
    assert cache.read_memo(invalidated, (999, 1280, 720)) is None
    assert cache.decoded_entry_count == 0
    cache.close()


def test_memo_bytes_share_total_budget_and_force_readable_raw_spills(tmp_path):
    cache = RasterCache(64, directory=tmp_path)
    generation = cache.begin_generation(9)
    for page in range(9):
        _write_page(cache, generation, page, page + 10)
    versions = cache.page_versions(generation)
    assert versions is not None and versions[0] is not None
    assert cache.decoded_bytes == 54
    assert cache.memo_budget_bytes == 16

    assert cache.write_memo(generation, (0,), b"m" * 16, page_versions=(versions[0],))
    assert cache.decoded_bytes == 64
    assert cache.peak_decoded_bytes == 64
    assert cache.decoded_entry_count == 17
    assert cache._pages[0].full_path is not None
    assert cache._pages[0].thumbnail_path is not None
    assert cache.disk_bytes > 0
    assert cache.read_memo(generation, (0,)) == b"m" * 16
    assert cache.read_page(0, generation=generation, cache=False) == _raster(1, 1, 10)
    assert cache.read_page(0, generation=generation, thumbnail=True, cache=False) == _raster(
        1, 1, 11
    )
    assert cache.decoded_bytes == 64
    cache.close()


@pytest.mark.parametrize("fail_call", [1, 2])
def test_memo_induced_spill_failure_keeps_raw_pages_and_accounting(
    tmp_path,
    monkeypatch,
    fail_call,
):
    cache = RasterCache(64, directory=tmp_path)
    generation = cache.begin_generation(9)
    for page in range(9):
        _write_page(cache, generation, page, page + 10)
    versions = cache.page_versions(generation)
    assert versions is not None and versions[0] is not None
    page_zero = cache.read_page(0, generation=generation)
    assert page_zero is not None

    real_write = raster_cache_module._write_private_file
    write_count = 0

    def fail_spill(root: Path, data: bytes) -> Path:
        nonlocal write_count
        write_count += 1
        if write_count == fail_call:
            raise OSError("memo pressure spill failed")
        return real_write(root, data)

    monkeypatch.setattr(raster_cache_module, "_write_private_file", fail_spill)
    with pytest.raises(RasterCacheError, match="spill raster"):
        cache.write_memo(generation, (0,), b"m" * 16, page_versions=(versions[0],))

    assert cache.read_memo(generation, (0,)) is None
    assert cache.ready_pages == frozenset(range(9))
    expected_raw_bytes = 54 - 3 * (fail_call - 1)
    assert cache.decoded_bytes == expected_raw_bytes
    assert cache.decoded_entry_count == 18 - (fail_call - 1)
    assert cache.read_page(0, generation=generation) is page_zero
    assert cache.disk_bytes > 0 if fail_call > 1 else cache.disk_bytes == 0
    cache.close()


def test_page_replacement_releases_stale_memo_budget_before_raw_admission(tmp_path):
    cache = RasterCache(64, directory=tmp_path)
    generation = cache.begin_generation(9)
    for page in range(9):
        _write_page(cache, generation, page, page + 10)
    versions = cache.page_versions(generation)
    assert versions is not None and versions[0] is not None
    assert cache.write_memo(
        generation, (700, 0, 1280, 720), b"m" * 16, page_versions=(versions[0],)
    )
    assert cache.decoded_bytes == 64
    assert cache._pages[0].full_path is not None

    _write_page(cache, generation, 0, 99)

    assert cache.read_memo(generation, (700, 0, 1280, 720)) is None
    assert cache.decoded_bytes == 54
    assert cache.peak_decoded_bytes == 64
    assert cache.decoded_entry_count == 18
    assert cache._pages[1].full_path is None
    assert cache._pages[1].thumbnail_path is None
    assert cache.read_page(0, generation=generation) == _raster(1, 1, 99)
    cache.close()


def test_candidate_file_failure_preserves_replaced_page(tmp_path, monkeypatch):
    cache = RasterCache(0, directory=tmp_path)
    generation = cache.begin_generation(1)
    _write_page(cache, generation, 0, 5)
    old_info = cache.info(0)
    old_data = cache.read_page(0, generation=generation, cache=False)
    old_paths = {cache._pages[0].full_path, cache._pages[0].thumbnail_path}
    old_disk_bytes = cache.disk_bytes
    original_write = raster_cache_module._write_private_file
    calls = 0

    def fail_second_candidate_write(root: Path, data: bytes) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second staged raster write failed")
        return original_write(root, data)

    monkeypatch.setattr(raster_cache_module, "_write_private_file", fail_second_candidate_write)
    with pytest.raises(RasterCacheError, match="spill raster"):
        cache.write_page(
            generation,
            0,
            logical_dimensions=(4, 2),
            pixels=_raster(2, 1, 22),
            thumbnail=_raster(1, 1, 23),
        )

    assert calls == 2
    assert cache.info(0) == old_info
    assert cache.read_page(0, generation=generation, cache=False) == old_data
    assert cache.disk_bytes == old_disk_bytes
    assert {cache._pages[0].full_path, cache._pages[0].thumbnail_path} == old_paths
    assert len(list(cache.root.iterdir())) == 2
    cache.close()


def test_eviction_write_failure_preserves_old_page_and_budget(tmp_path, monkeypatch):
    cache = RasterCache(6, directory=tmp_path)
    generation = cache.begin_generation(2)
    _write_page(cache, generation, 0, 5)
    old_info = cache.info(0)
    old_full = cache.read_page(0, generation=generation)
    old_thumbnail = cache.read_page(0, generation=generation, thumbnail=True)
    assert old_full is not None and old_thumbnail is not None

    def fail_write(_root: Path, _data: bytes) -> Path:
        raise OSError("eviction spill failed")

    monkeypatch.setattr(raster_cache_module, "_write_private_file", fail_write)
    with pytest.raises(RasterCacheError, match="spill raster"):
        _write_page(cache, generation, 1, 25)

    assert cache.ready_pages == frozenset({0})
    assert cache.info(0) == old_info
    assert cache.read_page(0, generation=generation) is old_full
    assert cache.read_page(0, generation=generation, thumbnail=True) is old_thumbnail
    assert cache.decoded_bytes == 6
    assert cache.decoded_entry_count == 2
    assert cache.disk_bytes == 0
    assert list(cache.root.iterdir()) == []
    cache.close()


def test_promotion_spill_failure_keeps_source_and_old_pages_readable(tmp_path, monkeypatch):
    cache = RasterCache(6, directory=tmp_path)
    generation = cache.begin_generation(2)
    _write_page(cache, generation, 0, 5)
    _write_page(cache, generation, 1, 25)
    spilled_full = cache._pages[0].full_path
    assert spilled_full is not None and spilled_full.exists()
    old_disk_bytes = cache.disk_bytes
    old_resident = cache.read_page(1, generation=generation)
    assert old_resident is not None

    def fail_write(_root: Path, _data: bytes) -> Path:
        raise OSError("promotion eviction spill failed")

    with monkeypatch.context() as patcher:
        patcher.setattr(raster_cache_module, "_write_private_file", fail_write)
        with pytest.raises(RasterCacheError, match="spill raster"):
            cache.read_page(0, generation=generation)

    assert cache.ready_pages == frozenset({0, 1})
    assert cache._pages[0].full_path == spilled_full
    assert cache._pages[0].full_path is not None and cache._pages[0].full_path.exists()
    assert cache.read_page(1, generation=generation) is old_resident
    assert cache.read_page(0, generation=generation, cache=False) is not None
    assert cache.decoded_bytes == 6
    assert cache.decoded_entry_count == 2
    assert cache.disk_bytes == old_disk_bytes
    cache.close()


def test_assets_are_immutable_in_memory_replaceable_and_cleared(tmp_path, monkeypatch):
    cache = RasterCache(directory=tmp_path)
    generation = cache.begin_generation(0)
    first = b"first bundle"
    assert cache.write_asset(generation, "overlay", first)
    assert cache.read_asset(generation, "overlay") is first
    assert cache.disk_bytes == 0
    assert list(cache.root.iterdir()) == []

    def fail_write(_root: Path, _data: bytes) -> Path:
        raise AssertionError("assets must not use private files")

    monkeypatch.setattr(raster_cache_module, "_write_private_file", fail_write)
    replacement = b"replacement bundle"
    assert cache.write_asset(generation, "overlay", replacement)
    assert cache.read_asset(generation, "overlay") is replacement
    with pytest.raises(TypeError, match="bytes"):
        cache.write_asset(generation, "overlay", bytearray(b"invalid"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Asset keys"):
        cache.write_asset(generation, "Uppercase", b"bad key")
    with pytest.raises(ValueError, match="Asset keys"):
        cache.read_asset(generation, "bad/key")
    assert cache.read_asset(generation, "overlay") is replacement

    next_generation = cache.begin_generation(1)
    assert cache.read_asset(next_generation, "overlay") is None
    assert cache.write_asset(generation, "overlay", b"stale") is False
    assert cache.read_asset(generation, "invalid/key") is None
    assert cache.disk_bytes == 0
    assert list(cache.root.iterdir()) == []
    cache.close()


def test_generation_switch_serializes_after_an_in_progress_spill(tmp_path, monkeypatch):
    cache = RasterCache(6, directory=tmp_path)
    generation = cache.begin_generation(2)
    _write_page(cache, generation, 0, 5)
    entered = threading.Event()
    release = threading.Event()
    request_started = threading.Event()
    write_result = []
    generation_result = []
    errors = []
    real_write = raster_cache_module._write_private_file
    first_call = True

    def blocked_write(root: Path, data: bytes) -> Path:
        nonlocal first_call
        if first_call:
            first_call = False
            entered.set()
            if not release.wait(5):
                raise TimeoutError("spill barrier timed out")
        return real_write(root, data)

    monkeypatch.setattr(raster_cache_module, "_write_private_file", blocked_write)

    def writer() -> None:
        try:
            write_result.append(
                cache.write_page(
                    generation,
                    1,
                    logical_dimensions=(2, 2),
                    pixels=_raster(1, 1, 21),
                    thumbnail=_raster(1, 1, 22),
                )
            )
        except BaseException as error:
            errors.append(error)

    def advance_generation() -> None:
        request_started.set()
        try:
            generation_result.append(cache.begin_generation(2))
        except BaseException as error:
            errors.append(error)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    assert entered.wait(5)
    generation_thread = threading.Thread(target=advance_generation)
    generation_thread.start()
    assert request_started.wait(5)
    release.set()
    writer_thread.join(5)
    generation_thread.join(5)

    assert not writer_thread.is_alive()
    assert not generation_thread.is_alive()
    assert errors == []
    assert len(write_result) == 1 and write_result[0] is not None
    assert generation_result == [generation + 1]
    assert cache.ready_pages == frozenset()
    assert cache.decoded_bytes == 0
    assert cache.decoded_entry_count == 0
    assert cache.disk_bytes == 0
    assert list(cache.root.iterdir()) == []
    assert cache.read_page(1, generation=generation) is None
    cache.close()


def test_close_serializes_after_an_in_progress_write_and_stales_late_access(
    tmp_path,
    monkeypatch,
):
    cache = RasterCache(0, directory=tmp_path)
    generation = cache.begin_generation(1)
    entered = threading.Event()
    release = threading.Event()
    close_started = threading.Event()
    write_result = []
    errors = []
    real_write = raster_cache_module._write_private_file
    first_call = True

    def blocked_write(root: Path, data: bytes) -> Path:
        nonlocal first_call
        if first_call:
            first_call = False
            entered.set()
            if not release.wait(5):
                raise TimeoutError("write barrier timed out")
        return real_write(root, data)

    monkeypatch.setattr(raster_cache_module, "_write_private_file", blocked_write)

    def writer() -> None:
        try:
            write_result.append(
                cache.write_page(
                    generation,
                    0,
                    logical_dimensions=(2, 2),
                    pixels=_raster(1, 1, 21),
                    thumbnail=_raster(1, 1, 22),
                )
            )
        except BaseException as error:
            errors.append(error)

    def close_cache() -> None:
        close_started.set()
        try:
            cache.close()
        except BaseException as error:
            errors.append(error)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    assert entered.wait(5)
    close_thread = threading.Thread(target=close_cache)
    close_thread.start()
    assert close_started.wait(5)
    release.set()
    writer_thread.join(5)
    close_thread.join(5)

    assert not writer_thread.is_alive()
    assert not close_thread.is_alive()
    assert errors == []
    assert len(write_result) == 1 and write_result[0] is not None
    assert cache.closed is True
    assert not cache.root.exists()
    assert cache.ready_pages == frozenset()
    assert cache.decoded_bytes == 0
    assert cache.decoded_entry_count == 0
    assert cache.disk_bytes == 0
    assert cache.read_page(0, generation=generation) is None
    assert cache.write_asset(generation, "overlay", b"late") is False


def test_write_and_asset_staleness_after_generation_change(tmp_path):
    cache = RasterCache(0, directory=tmp_path)
    old_generation = cache.begin_generation(1)
    cache.begin_generation(1)

    assert (
        cache.write_page(
            old_generation,
            0,
            logical_dimensions=(2, 2),
            pixels=_raster(1, 1, 1),
            thumbnail=_raster(1, 1, 2),
        )
        is None
    )
    assert cache.info(0, generation=old_generation) is None
    assert cache.read_page(0, generation=old_generation) is None
    assert cache.write_asset(old_generation, "overlay", b"stale") is False
    assert cache.read_asset(old_generation, "overlay") is None
    assert cache.ready_pages == frozenset()
    assert cache.disk_bytes == 0
    assert list(cache.root.iterdir()) == []
    cache.close()


def test_write_validation_rejects_invalid_rasters_without_publishing(tmp_path):
    cache = RasterCache(directory=tmp_path)
    generation = cache.begin_generation(1)
    with pytest.raises(ValueError, match="positive RGB888"):
        cache.write_page(
            generation,
            0,
            logical_dimensions=(2, 2),
            pixels=RasterData(2, 1, 5, b"bad"),
            thumbnail=_raster(1, 1, 1),
        )
    assert cache.ready_pages == frozenset()
    assert cache.decoded_bytes == 0
    assert cache.disk_bytes == 0
    cache.close()
