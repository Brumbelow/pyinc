"""Hold a race open until both threads have reached it, without sleeping.

A race test has to put two threads inside one window at once. Before the fix,
the second thread walks into the window. After it, a lock keeps the second
thread out, so it can only queue on that lock. `Rendezvous.point` holds the
first thread that reaches it until either has happened. `Rendezvous.lock`
wraps the lock under test so that queueing on it counts as arriving. The
outcome is the same on every run: no sleep decides it, and both versions of
the code release the first thread.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

#: Long enough that only a missing second thread reaches it.
_ARRIVAL_TIMEOUT = 10.0


class ReportingLock:
    """A lock that calls ``on_wait`` before a thread blocks on it.

    The owner of a wrapped `threading.RLock` re-enters without blocking, so a
    reentrant acquire is not reported.
    """

    def __init__(self, on_wait: Callable[[], None], inner: Any | None = None) -> None:
        self._inner = threading.Lock() if inner is None else inner
        self._on_wait = on_wait

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._inner.acquire(False):
            return True
        if not blocking:
            return False
        self._on_wait()
        return bool(self._inner.acquire(True, timeout))

    def release(self) -> None:
        self._inner.release()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *_exc_info: object) -> None:
        self.release()


class Rendezvous:
    """The first thread at `point` waits there for a second thread.

    The second thread arrives by reaching `point` itself or by blocking on a
    lock that `lock` wrapped. Later calls to `point` pass straight through.
    """

    def __init__(self) -> None:
        self._arrived = threading.Event()
        self._count_lock = threading.Lock()
        self._count = 0

    def point(self) -> None:
        with self._count_lock:
            self._count += 1
            first = self._count == 1
        if not first:
            self._arrived.set()
        elif not self._arrived.wait(_ARRIVAL_TIMEOUT):
            raise AssertionError("no second thread reached the race window")

    def lock(self, inner: Any | None = None) -> ReportingLock:
        return ReportingLock(self._arrived.set, inner)


def run_in_threads(*targets: Callable[[], object]) -> list[object]:
    """Run each target on its own thread and return their results in order.

    An exception raised on a thread is re-raised here, so a failure inside the
    race fails the test instead of vanishing with the thread.
    """

    results: list[object] = [None] * len(targets)
    errors: list[BaseException] = []

    def run(index: int, target: Callable[[], object]) -> None:
        try:
            results[index] = target()
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=run, args=(index, target)) for index, target in enumerate(targets)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_ARRIVAL_TIMEOUT * 3)
    assert not any(thread.is_alive() for thread in threads), "a racing thread did not finish"
    if errors:
        raise errors[0]
    return results
