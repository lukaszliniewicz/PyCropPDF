"""Small, dependency-free primitives for cooperative background processing."""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterable
from concurrent.futures import FIRST_COMPLETED, Executor, Future, wait
from threading import Event
from typing import TypeVar

TInput = TypeVar("TInput")
TResult = TypeVar("TResult")


class OperationCancelled(Exception):
    """Raised when a cooperative operation observes its cancellation token."""


class CancellationToken:
    """Thread-safe cancellation state shared by a caller and its worker."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise OperationCancelled("Operation was cancelled.")


def iter_bounded_results(
    executor: Executor,
    function: Callable[[TInput], TResult],
    items: Iterable[TInput],
    *,
    max_pending: int,
    cancel_token: CancellationToken | None = None,
) -> Generator[TResult, None, None]:
    """Yield task results in completion order with bounded future admission.

    The executor belongs to the caller. Closing this iterator or observing
    cancellation cancels futures that have not yet been consumed.
    """
    if max_pending <= 0:
        raise ValueError("max_pending must be a positive integer.")

    item_iterator = iter(items)
    pending: set[Future[TResult]] = set()
    exhausted = False
    try:
        while pending or not exhausted:
            while not exhausted and len(pending) < max_pending:
                if cancel_token is not None:
                    cancel_token.raise_if_cancelled()
                try:
                    item = next(item_iterator)
                except StopIteration:
                    exhausted = True
                    break
                if cancel_token is not None:
                    cancel_token.raise_if_cancelled()
                pending.add(executor.submit(function, item))
                del item

            if not pending:
                break

            done, _not_done = wait(
                pending,
                timeout=0.05,
                return_when=FIRST_COMPLETED,
            )
            if cancel_token is not None:
                cancel_token.raise_if_cancelled()

            while done:
                future = done.pop()
                pending.remove(future)
                result = future.result()
                del future
                if cancel_token is not None:
                    cancel_token.raise_if_cancelled()
                yield result
                del result
    finally:
        for future in pending:
            future.cancel()
        pending.clear()
