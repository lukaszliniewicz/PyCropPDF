from __future__ import annotations

import weakref
from concurrent.futures import Future
from unittest.mock import patch

import pytest

from pycroppdf.processing import (
    CancellationToken,
    OperationCancelled,
    iter_bounded_results,
)


class ControlledExecutor:
    def __init__(self, *, retain_futures: bool = False) -> None:
        self.submitted_items = []
        self.future_refs = []
        self.future_by_item = {}
        self._sequence = {}
        self._functions = {}
        self._retain_futures = retain_futures

    def submit(self, function, item):
        future = Future()
        sequence = len(self.submitted_items)
        self.submitted_items.append(item)
        self.future_refs.append(weakref.ref(future))
        if self._retain_futures:
            self.future_by_item[item] = future
        self._sequence[future] = sequence
        self._functions[future] = (function, item)
        return future

    def complete_next(self, futures):
        future = min(futures, key=self._sequence.__getitem__)
        function, item = self._functions.pop(future)
        self._sequence.pop(future)
        try:
            result = function(item)
        except BaseException as error:
            future.set_exception(error)
        else:
            future.set_result(result)
        return {future}, set(futures) - {future}

    def cancel_all_waiting(self, futures):
        return set(), set(futures)


def test_bounded_results_admit_at_most_window_and_yield_each_item_once():
    max_pending = 4
    executor = ControlledExecutor()
    iterator = iter_bounded_results(
        executor,
        lambda item: item * 2,
        range(100),
        max_pending=max_pending,
    )
    results = []

    def complete_next(futures, **_kwargs):
        return executor.complete_next(futures)

    with patch("pycroppdf.processing.wait", side_effect=complete_next):
        for _ in range(100):
            results.append(next(iterator))
            yielded = len(results)
            assert len(executor.submitted_items) - yielded <= max_pending
            assert sum(reference() is not None for reference in executor.future_refs) <= max_pending

        with pytest.raises(StopIteration):
            next(iterator)

    assert results == [item * 2 for item in range(100)]
    assert executor.submitted_items == list(range(100))
    assert all(reference() is None for reference in executor.future_refs)


def test_closing_bounded_results_cancels_unconsumed_futures():
    executor = ControlledExecutor(retain_futures=True)
    iterator = iter_bounded_results(
        executor,
        lambda item: item,
        (1, 2, 3),
        max_pending=2,
    )

    with patch(
        "pycroppdf.processing.wait",
        side_effect=lambda futures, **_kwargs: executor.complete_next(futures),
    ):
        assert next(iterator) == 1
    assert executor.submitted_items == [1, 2]
    iterator.close()

    assert executor.future_by_item[2].cancelled()
    assert 3 not in executor.submitted_items


def test_task_exception_is_propagated_and_other_pending_work_is_cancelled():
    executor = ControlledExecutor(retain_futures=True)
    failure = ValueError("task failed")

    def task(item):
        if item == 1:
            raise failure
        return item

    iterator = iter_bounded_results(executor, task, (1, 2), max_pending=2)
    with (
        patch(
            "pycroppdf.processing.wait",
            side_effect=lambda futures, **_kwargs: executor.complete_next(futures),
        ),
        pytest.raises(ValueError) as caught,
    ):
        next(iterator)

    assert caught.value is failure
    assert executor.future_by_item[2].cancelled()


def test_cancelled_token_admits_no_task():
    executor = ControlledExecutor()
    token = CancellationToken()
    token.cancel()
    iterator = iter_bounded_results(
        executor,
        lambda item: item,
        range(10),
        max_pending=3,
        cancel_token=token,
    )

    with pytest.raises(OperationCancelled):
        next(iterator)

    assert executor.submitted_items == []


def test_cancellation_during_wait_cancels_all_admitted_work():
    executor = ControlledExecutor(retain_futures=True)
    token = CancellationToken()
    iterator = iter_bounded_results(
        executor,
        lambda item: item,
        range(10),
        max_pending=2,
        cancel_token=token,
    )

    def cancel_while_waiting(futures, *, timeout, return_when):
        assert timeout == 0.05
        assert return_when
        token.cancel()
        return executor.cancel_all_waiting(futures)

    with (
        patch("pycroppdf.processing.wait", side_effect=cancel_while_waiting),
        pytest.raises(OperationCancelled),
    ):
        next(iterator)

    assert executor.submitted_items == [0, 1]
    assert all(executor.future_by_item[item].cancelled() for item in (0, 1))


def test_bounded_results_reject_nonpositive_window():
    with pytest.raises(ValueError, match="positive"):
        next(iter_bounded_results(ControlledExecutor(), lambda item: item, (), max_pending=0))
