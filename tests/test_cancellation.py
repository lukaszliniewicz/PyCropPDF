from __future__ import annotations

import inspect
from concurrent.futures import Future
from unittest.mock import patch

import pymupdf as fitz
import pytest

import pycroppdf.processing as processing
import pycroppdf.rotation as rotation
import pycroppdf.workers as workers
from pycroppdf.processing import CancellationToken, OperationCancelled
from pycroppdf.rotation import deskew_pdf_bytes, rotate_page_content
from pycroppdf.workers import AutoDeskewWorker, RenderAllPagesWorker


def _pdf_bytes(page_count: int = 3) -> bytes:
    document = fitz.open()
    for page_number in range(page_count):
        page = document.new_page(width=200, height=150)
        page.insert_text((24, 36), f"Source page {page_number + 1}")
    data = document.tobytes()
    document.close()
    return data


def _observe(worker):
    observed = {"results": [], "errors": [], "cancelled": [], "finished": [], "progress": []}
    worker.signals.result.connect(observed["results"].append)
    worker.signals.error.connect(observed["errors"].append)
    worker.signals.cancelled.connect(lambda: observed["cancelled"].append(True))
    worker.signals.finished.connect(lambda: observed["finished"].append(True))
    worker.signals.progress.connect(lambda done, total: observed["progress"].append((done, total)))
    return observed


def test_render_cancellation_before_work_emits_cancelled_once():
    token = CancellationToken()
    token.cancel()
    worker = RenderAllPagesWorker(b"not a PDF", 2, cancel_token=token)
    observed = _observe(worker)

    with patch("pycroppdf.workers._render_page_from_document") as render_page:
        worker.run()

    render_page.assert_not_called()
    assert observed["results"] == []
    assert observed["errors"] == []
    assert observed["cancelled"] == [True]
    assert observed["finished"] == [True]


def test_render_cancellation_after_native_render_prevents_result_emission():
    token = CancellationToken()
    worker = RenderAllPagesWorker(_pdf_bytes(1), 1, cancel_token=token)
    observed = _observe(worker)
    render_page = workers._render_page_from_document

    def render_then_cancel(document, page_num, crop_rects):
        rendered = render_page(document, page_num, crop_rects)
        token.cancel()
        return rendered

    with patch("pycroppdf.workers._render_page_from_document", side_effect=render_then_cancel):
        worker.run()

    assert observed["results"] == []
    assert observed["errors"] == []
    assert observed["cancelled"] == [True]
    assert observed["finished"] == [True]


def test_unexpected_render_failure_is_reported_as_error_not_cancellation():
    worker = RenderAllPagesWorker(_pdf_bytes(1), 1)
    observed = _observe(worker)

    with patch(
        "pycroppdf.workers._render_page_from_document",
        side_effect=RuntimeError("native render failed"),
    ):
        worker.run()

    assert observed["results"] == []
    assert len(observed["errors"]) == 1
    assert "native render failed" in observed["errors"][0]
    assert observed["cancelled"] == []
    assert observed["finished"] == [True]


def test_local_render_can_emit_completed_page_then_cancel_without_more_results():
    token = CancellationToken()
    worker = RenderAllPagesWorker(_pdf_bytes(2), 2, cancel_token=token)
    observed = _observe(worker)
    worker.signals.progress.connect(lambda done, _total: token.cancel() if done == 1 else None)

    worker.run()

    assert [page_num for page_num, _image in observed["results"]] == [0]
    assert observed["errors"] == []
    assert observed["cancelled"] == [True]
    assert observed["finished"] == [True]
    assert observed["progress"] == [(0, 2), (1, 2)]


def test_parallel_render_waits_for_pool_cleanup_before_cancelled_and_finished():
    events = []
    submitted_pages = []
    submitted_futures = {}
    constructor_arguments = []
    retained_iterators = []
    real_iter_bounded_results = processing.iter_bounded_results

    def retain_render_iterator(*args, **kwargs):
        iterator = real_iter_bounded_results(*args, **kwargs)
        retained_iterators.append(iterator)
        return iterator

    class FakeExecutor:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            assert submitted_futures[1].cancelled()
            events.append("pool-exited")
            return False

        def submit(self, _function, page_num):
            submitted_pages.append(page_num)
            future = Future()
            submitted_futures[page_num] = future
            if page_num == 0:
                future.set_result((page_num, b"\0\0\0", 1, 1, 3))
            return future

    def make_executor(**kwargs):
        constructor_arguments.append(kwargs)
        return FakeExecutor()

    token = CancellationToken()
    worker = RenderAllPagesWorker(_pdf_bytes(3), 3, cancel_token=token)
    observed = _observe(worker)
    worker.signals.progress.connect(lambda done, _total: token.cancel() if done == 1 else None)
    worker.signals.cancelled.connect(lambda: events.append("cancelled"))
    worker.signals.finished.connect(lambda: events.append("finished"))

    with (
        patch("pycroppdf.workers.os.process_cpu_count", create=True, return_value=3),
        patch(
            "pycroppdf.workers.concurrent.futures.ProcessPoolExecutor",
            side_effect=make_executor,
        ),
        patch.object(workers, "iter_bounded_results", side_effect=retain_render_iterator),
    ):
        worker.run()

    assert len(constructor_arguments) == 1
    assert constructor_arguments[0]["mp_context"].get_start_method() == "spawn"
    assert constructor_arguments[0]["max_workers"] == 2
    assert submitted_pages == [0, 1]
    assert submitted_futures[1].cancelled()
    assert len(retained_iterators) == 1
    assert inspect.getgeneratorstate(retained_iterators[0]) == inspect.GEN_CLOSED
    assert [page_num for page_num, _image in observed["results"]] == [0]
    assert observed["errors"] == []
    assert observed["cancelled"] == [True]
    assert observed["finished"] == [True]
    assert events == ["pool-exited", "cancelled", "finished"]


def test_auto_deskew_cancellation_before_work_skips_deskew_call():
    token = CancellationToken()
    token.cancel()
    worker = AutoDeskewWorker(b"not a PDF", (0,), cancel_token=token)
    observed = _observe(worker)

    with patch("pycroppdf.workers.deskew_pdf_bytes") as deskew:
        worker.run()

    deskew.assert_not_called()
    assert observed["results"] == []
    assert observed["errors"] == []
    assert observed["cancelled"] == [True]
    assert observed["finished"] == [True]


def test_auto_deskew_checks_cancellation_after_detection_before_crop_transform():
    token = CancellationToken()
    worker = AutoDeskewWorker(_pdf_bytes(2), (0, 1), cancel_token=token)
    observed = _observe(worker)

    def complete_detection_then_cancel(_pdf_bytes, _page_numbers, **kwargs):
        assert kwargs["cancel_token"] is token
        kwargs["on_progress"](0, 2)
        kwargs["on_progress"](1, 2)
        token.cancel()
        return b"deskewed", {0: 1.0}, [1]

    with (
        patch(
            "pycroppdf.workers.deskew_pdf_bytes",
            side_effect=complete_detection_then_cancel,
        ) as deskew,
        patch("pycroppdf.workers.transform_crop_rects_for_rotations") as transform_crops,
    ):
        worker.run()

    assert deskew.call_args.kwargs["on_progress"] is not None
    transform_crops.assert_not_called()
    assert observed["results"] == []
    assert observed["errors"] == []
    assert observed["cancelled"] == [True]
    assert observed["finished"] == [True]
    assert observed["progress"] == [(0, 2), (1, 2)]


def test_serial_deskew_reports_detection_progress_including_undetected_pages():
    source_bytes = _pdf_bytes(3)
    token = CancellationToken()
    progress = []

    with patch(
        "pycroppdf.rotation.detect_page_deskew_angle",
        side_effect=[None, 1.25, None],
    ):
        result, rotations, undetected = deskew_pdf_bytes(
            source_bytes,
            range(3),
            max_workers=1,
            cancel_token=token,
            on_progress=lambda done, total: progress.append((done, total)),
        )

    assert result
    assert rotations == {1: 1.25}
    assert undetected == [0, 2]
    assert progress == [(0, 3), (1, 3), (2, 3), (3, 3)]


def test_parallel_deskew_reports_bounded_detection_progress_and_spawn_context():
    constructor_arguments = []
    submitted_pages = []
    angles = {0: None, 1: 0.5, 2: None}

    class FakeExecutor:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            return False

        def submit(self, _function, page_num):
            submitted_pages.append(page_num)
            future = Future()
            future.set_result((page_num, angles[page_num]))
            return future

    def make_executor(**kwargs):
        constructor_arguments.append(kwargs)
        return FakeExecutor()

    source_bytes = _pdf_bytes(3)
    progress = []
    with patch("pycroppdf.rotation.ProcessPoolExecutor", side_effect=make_executor):
        _output, rotations, undetected = deskew_pdf_bytes(
            source_bytes,
            range(3),
            max_workers=2,
            on_progress=lambda done, total: progress.append((done, total)),
        )

    assert constructor_arguments[0]["max_workers"] == 2
    assert constructor_arguments[0]["mp_context"].get_start_method() == "spawn"
    assert submitted_pages == [0, 1, 2]
    assert rotations == {1: 0.5}
    assert undetected == [0, 2]
    assert [done for done, _total in progress] == [0, 1, 2, 3]
    assert all(total == 3 for _done, total in progress)


def test_parallel_deskew_cancellation_stops_admission_and_returns_no_output():
    events = []
    submitted_pages = []
    submitted_futures = {}
    retained_iterators = []
    angles = {0: None, 1: None, 2: None}
    real_iter_bounded_results = processing.iter_bounded_results

    def retain_deskew_iterator(*args, **kwargs):
        iterator = real_iter_bounded_results(*args, **kwargs)
        retained_iterators.append(iterator)
        return iterator

    class FakeExecutor:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            assert submitted_futures[1].cancelled()
            events.append("pool-exited")
            return False

        def submit(self, _function, page_num):
            submitted_pages.append(page_num)
            future = Future()
            submitted_futures[page_num] = future
            if page_num == 0:
                future.set_result((page_num, angles[page_num]))
            return future

    token = CancellationToken()
    source_bytes = _pdf_bytes(3)
    progress = []

    def report_progress(done, total):
        progress.append((done, total))
        if done == 1:
            token.cancel()

    with (
        patch("pycroppdf.rotation.ProcessPoolExecutor", return_value=FakeExecutor()),
        patch.object(rotation, "iter_bounded_results", side_effect=retain_deskew_iterator),
        pytest.raises(OperationCancelled),
    ):
        deskew_pdf_bytes(
            source_bytes,
            range(3),
            max_workers=2,
            cancel_token=token,
            on_progress=report_progress,
        )

    assert events == ["pool-exited"]
    assert submitted_pages == [0, 1]
    assert submitted_futures[1].cancelled()
    assert len(retained_iterators) == 1
    assert inspect.getgeneratorstate(retained_iterators[0]) == inspect.GEN_CLOSED
    assert progress == [(0, 3), (1, 3)]
    with fitz.open("pdf", source_bytes) as original:
        assert len(original) == 3
        assert "Source page 1" in original[0].get_text()


def test_serial_deskew_cancellation_after_first_detection_returns_no_output():
    source_bytes = _pdf_bytes(3)
    token = CancellationToken()
    detected = []
    progress = []

    def detect_and_record(page):
        detected.append(page.number)
        return None

    def report_progress(done, total):
        progress.append((done, total))
        if done == 1:
            token.cancel()

    with (
        patch("pycroppdf.rotation.detect_page_deskew_angle", side_effect=detect_and_record),
        pytest.raises(OperationCancelled),
    ):
        deskew_pdf_bytes(
            source_bytes,
            range(3),
            max_workers=1,
            cancel_token=token,
            on_progress=report_progress,
        )

    assert detected == [0]
    assert progress == [(0, 3), (1, 3)]
    with fitz.open("pdf", source_bytes) as original:
        assert len(original) == 3
        assert "Source page 1" in original[0].get_text()


def test_deskew_cancellation_after_first_rotation_discards_private_document():
    source_bytes = _pdf_bytes(2)
    token = CancellationToken()
    real_rotate = rotate_page_content
    rotated_pages = []

    def rotate_then_cancel(document, page_num, angle):
        real_rotate(document, page_num, angle)
        rotated_pages.append(page_num)
        token.cancel()

    with (
        patch("pycroppdf.rotation.detect_page_deskew_angle", return_value=1.0),
        patch("pycroppdf.rotation.rotate_page_content", side_effect=rotate_then_cancel),
        pytest.raises(OperationCancelled),
    ):
        deskew_pdf_bytes(
            source_bytes,
            range(2),
            max_workers=1,
            cancel_token=token,
        )

    assert rotated_pages == [0]
    with fitz.open("pdf", source_bytes) as original:
        assert len(original) == 2
        assert "Source page 1" in original[0].get_text()
        assert "Source page 2" in original[1].get_text()
