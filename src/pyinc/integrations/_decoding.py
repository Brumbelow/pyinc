"""Reuse of decoded query payloads, across requests and within one, and the
refusal a high-level entrypoint owes a query body.

Two memos, with different lifetimes and different reasons to be sound.

`decoded` keys on payload *identity*. A layer-3 entrypoint is a pure function of
the payloads it reads, and the kernel already decides when a payload is stale.
While a query's value stands, the kernel hands back the very same payload
object. When the value changes, it hands back a different one. So a decode keyed
on the identity of the payloads it was built from is valid for as long as those
values are, and nothing here reasons about invalidation. The memo holds a
reference to every payload it keys on, which makes `id()` safe: while an entry
refers to its payload, that payload stays alive and its address stays taken.
The memo is bounded so a long-running process cannot grow it without limit.

Payload identity is stable only in `strict` mode. `checked` and `fast` thaw at
the boundary (`Database._expose_snapshot`) and hand back a fresh object per
call. Every lookup would miss, and every miss would pin one more payload tree
and decoded tree until the bound reset the whole cache. So outside `strict` the
memo is skipped.

The memo is keyed per database through a weak reference, so a dropped database
releases every payload and decoded value it pinned. The entry bound applies
per database.

Two threads can use the integrations on one database at the same time when they
drive it directly, outside a `WorkspaceSession` (whose lock serializes its
methods). So every read and write of the memo holds `_CACHES_LOCK`, and a
decode two threads raced to compute is stored once. Both get back the one
stored first. The lock is never held while a decode runs. A decode reads
queries, and a thread that holds the database's lock, or a session's, may be
the next one to ask for this lock. A child forked while another thread held the
lock gets a new one.

`once_per_request` keys on the call itself. It lives only for the span a caller
declares with `request_scope`, on the thread that declared it. A
`WorkspaceSession` holds its lock for the whole of each public method, and its
inputs cannot change while it is held. So an entrypoint asked the same question
twice inside one method must answer the same both times. Outside such a span
the memo does not exist, so a caller driving the integrations directly around a
file edit still sees the edit. A session that rewrites the mirror inside one of
its own methods calls `request_inputs_changed` when it does. Only the thread
that opened a span sees it, so its memo needs no lock.
"""

from __future__ import annotations

import os
import threading
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from pyinc.errors import CompositionError
from pyinc.runtime import _current_thread_token

if TYPE_CHECKING:
    from pyinc.runtime import Database

_T = TypeVar("_T")

# The bound is an entry count, whatever each entry's size in bytes. An entry
# holds one decoded value, and a workspace-level entry is a whole analysis. The
# total stays in hand because those values share substructure with the query
# results they were decoded from, which the kernel retains anyway. Past the
# limit the whole cache is cleared at once, with no per-entry eviction, which
# keeps lookups a single dict hit.
_MAX_ENTRIES = 8192

_CACHES: weakref.WeakKeyDictionary[
    Database, dict[tuple[Any, ...], tuple[tuple[Any, ...], Any]]
] = weakref.WeakKeyDictionary()
# Held for every read and write of `_CACHES` and of the dicts it holds, and for
# nothing else. Unguarded, two threads could each find no dict for a database
# and install one of their own, and the second install dropped every entry the
# first thread stored. The weak reference's callback drops a collected
# database's entry. It runs on whatever thread collects the database, possibly
# one that already holds this lock, so it runs without the lock. It deletes only
# the collected database's key, in a single dict operation.
_CACHES_LOCK = threading.Lock()


def _new_caches_lock_in_child() -> None:
    """Give a forked child a lock of its own for the memo.

    A thread that held the lock when another thread forked does not exist in
    the child, so the child would wait on the lock forever, even for a new
    database. The memo itself is whole at the fork: each step taken under the
    lock leaves it consistent.
    """

    global _CACHES_LOCK
    _CACHES_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_new_caches_lock_in_child)


@dataclass
class _Request:
    """One `request_scope` span: the database it promises about, and its memo.

    It travels in a `ContextVar`. A context copied while it is open keeps a
    reference to it after the span, and its promise, have closed. Every
    `threading.Thread` started on a free-threaded 3.14 build copies one, and
    so does `asyncio.to_thread`. So a span belongs to the thread that opened it
    and ends when it closes, as the kernel's own request does. When it ends it
    lets go of the database and the memo. A copied context can live as long as
    its thread, and a pool's worker thread lives as long as the pool. So a
    request that kept them would keep the database and every memoized value
    alive with it.
    """

    # None once the request has ended.
    db: Database | None
    memo: dict[Any, Any] = field(default_factory=dict)
    owner: object = field(default_factory=_current_thread_token)
    ended: bool = False


_REQUEST: ContextVar[_Request | None] = ContextVar("pyinc_integration_request", default=None)


def _live_request() -> _Request | None:
    request = _REQUEST.get()
    if request is None or request.ended or request.owner is not _current_thread_token():
        return None
    return request


_MISSING = object()


def _lookup(
    cache: dict[tuple[Any, ...], tuple[tuple[Any, ...], Any]],
    key: tuple[Any, ...],
    sources: tuple[Any, ...],
) -> Any:
    """Return the value ``cache`` holds for ``sources``, or ``_MISSING``."""

    entry = cache.get(key)
    if entry is None:
        return _MISSING
    held, value = entry
    if all(left is right for left, right in zip(held, sources, strict=True)):
        return value
    return _MISSING


def decoded(
    db: Database, kind: str, sources: tuple[Any, ...], decode: Callable[[], _T]
) -> _T:
    """Return ``decode()`` for these ``sources``, reusing an earlier result.

    ``sources`` must name every value the decode reads, and ``kind`` keeps two
    decoders that read the same payload from colliding.
    """

    if db.mode != "strict":
        return decode()
    key = (kind, *(id(source) for source in sources))
    with _CACHES_LOCK:
        cache = _CACHES.get(db)
        if cache is None:
            cache = {}
            _CACHES[db] = cache
        held_value = _lookup(cache, key, sources)
    if held_value is not _MISSING:
        return held_value  # type: ignore[no-any-return]
    value = decode()
    with _CACHES_LOCK:
        # Another thread may have stored this decode while ours ran. Answer
        # with the one already stored, so every caller gets the same object.
        held_value = _lookup(cache, key, sources)
        if held_value is not _MISSING:
            return held_value  # type: ignore[no-any-return]
        if len(cache) >= _MAX_ENTRIES:
            cache.clear()
        cache[key] = (sources, value)
    return value


@contextmanager
def request_scope(db: Database) -> Iterator[None]:
    """Declare that ``db``'s inputs cannot change for the duration.

    Repeated entrypoint calls inside the span answer from the first one.
    """

    request = _Request(db)
    token = _REQUEST.set(request)
    try:
        yield
    finally:
        # Ended first, so nothing reads the database or the memo once they go.
        request.ended = True
        request.db = None
        request.memo = {}
        _REQUEST.reset(token)


def request_inputs_changed() -> None:
    """Opening-thread scope: memo reset and kernel-span refresh.

    A caller that mutates what the integrations read part-way through its own
    request has broken the promise `request_scope` makes and must say so.
    Saying so reaches the kernel too. When the caller also holds a
    `Database.request_span`, the span rolls onto a fresh request, so the
    kernel's own once-per-request work (resource validation above all)
    re-runs against the moved inputs.
    """

    request = _live_request()
    # A live request always holds its database. The second test is for the
    # type checker.
    if request is not None and request.db is not None:
        request.memo.clear()
        request.db.request_inputs_changed()


def once_per_request(
    db: Database, kind: str, args: tuple[Any, ...], compute: Callable[[], _T]
) -> _T:
    """Return ``compute()``, answering from this request if it already ran."""

    request = _live_request()
    if request is None or request.db is not db:
        return compute()
    memo = request.memo
    key = (kind, args)
    if key in memo:
        return memo[key]  # type: ignore[no-any-return]
    value = compute()
    memo[key] = value
    return value


def _reject_in_query(db: Database, name: str) -> None:
    """Raise ``CompositionError`` if ``db`` is part-way through a query execution.

    High-level entrypoints belong outside the query graph. The message names
    the entrypoint and no payload query: most of them have no same-stem query
    to name, and one that names nothing beats one that names the wrong thing.
    """

    if db._current_frame() is not None:
        raise CompositionError(
            f"{name}() is a high-level entrypoint and cannot be called from "
            f"inside a query body. Read the payload query it composes with "
            f"db.get(), or call {name}() outside the query."
        )
