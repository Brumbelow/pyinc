"""A forked child can take the locks another thread of its parent held.

`os.fork` copies only the thread that calls it. A lock that some other thread
held at that moment stays held in the child, and no thread is left there to
release it, so the child's first wait on it never ends. Each cell parks a
thread inside the lock, forks, and has the child take the same lock under an
alarm. A child that waits is killed by the alarm and fails the cell.
"""

from __future__ import annotations

import gc
import os
import signal
import sys
import threading
import traceback
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from pyinc import Database, InMemoryArtifactStore
from pyinc import store as store_module
from pyinc.integrations import _decoding

posix_fork = pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork does not exist here")
# Python 3.12 and later warn on every fork from a process with several threads,
# and these cells fork from one on purpose.
fork_with_threads = pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded:DeprecationWarning"
)

# Long enough that only a child waiting on a lock reaches it.
_CHILD_ALARM_SECONDS = 10


@contextmanager
def _held_by_another_thread(lock: Any) -> Iterator[None]:
    """Hold ``lock`` on a parked thread for the duration of the block."""

    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with lock:
            entered.set()
            release.wait()

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert entered.wait(10), "the holder thread never took the lock"
        yield
    finally:
        release.set()
        holder.join()


def _exit_code_of_child(call: Callable[[], object]) -> int:
    """Fork, run ``call`` in the child under an alarm, and return its exit code.

    The code is 0 when ``call`` returned, 1 when it raised, and minus the
    signal number when a signal killed the child, as the alarm does.
    """

    # Every caller is already skipped on Windows. This tells a type check run
    # for Windows that nothing below runs there.
    if sys.platform == "win32":
        pytest.skip("os.fork does not exist on Windows")
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child never reports coverage
        try:
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
            signal.alarm(_CHILD_ALARM_SECONDS)
            call()
        except BaseException:
            traceback.print_exc()
            os._exit(1)
        os._exit(0)
    _pid, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


@posix_fork
@fork_with_threads
def test_a_forked_child_decodes_while_another_thread_held_the_memo_lock() -> None:
    payload = object()

    def decode_twice() -> None:
        db = Database(mode="strict")
        assert _decoding.decoded(db, "kind", (payload,), lambda: "decoded") == "decoded"
        # The second call answers from the memo, under the child's own lock.
        assert _decoding.decoded(db, "kind", (payload,), lambda: "again") == "decoded"

    with _held_by_another_thread(_decoding._CACHES_LOCK):
        exit_code = _exit_code_of_child(decode_twice)

    assert exit_code == 0


@posix_fork
@fork_with_threads
@pytest.mark.parametrize("operation", ["put", "keys"])
def test_a_forked_child_uses_a_store_while_another_thread_held_its_lock(operation: str) -> None:
    store = InMemoryArtifactStore()
    store.put("a" * 64, b"parent")

    def use_store() -> None:
        if operation == "put":
            store.put("b" * 64, b"child")
            assert store.get("b" * 64) == b"child"
        else:
            assert dict(store.keys()) == {"a" * 64: b"parent"}

    with _held_by_another_thread(store._lock):
        exit_code = _exit_code_of_child(use_store)

    assert exit_code == 0


def test_the_stores_listed_for_a_fork_are_not_kept_alive_by_the_list() -> None:
    store = InMemoryArtifactStore()
    assert store in store_module._LIVE_STORES
    store_ref = weakref.ref(store)

    del store
    gc.collect()

    assert store_ref() is None
