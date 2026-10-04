"""Reuse of decoded query payloads, across requests and within one, and the
refusal a high-level entrypoint owes a query body.

Two memos, with different lifetimes and different reasons to be sound.

`decoded` keys on payload *identity*. A layer-3 entrypoint is a pure function of
the payloads it reads, and the kernel already decides when a payload is stale:
while a query's value stands it hands back the very same payload object, and
when the value changes it hands back a different one. So a decode keyed on the
identity of the payloads it was built from is valid for exactly as long as those
values are, and nothing here reasons about invalidation. It holds a reference to
every payload it keys on, which is what makes `id()` safe: an entry's payload
cannot be collected and its address reused while the entry still refers to it.
It is bounded so a long-running process cannot grow it without limit.

Payload identity is only stable in `strict` mode. `checked` and `fast` thaw at
the boundary (`Database._expose_snapshot`), handing back a fresh object per
call, so every lookup would miss and every miss would pin one more payload tree
and decoded tree until the bound reset the whole cache. Off `strict` the memo is
skipped outright.

The memo is keyed per database through a weak reference, so a dropped database
releases every payload and decoded value it pinned; the entry bound applies
per database.

Two threads can use the integrations on one database at the same time when they
drive it directly rather than through a `WorkspaceSession`, whose lock
serializes its methods. So every read and write of the memo holds
`_CACHES_LOCK`, and a decode two threads raced to compute is stored once: the
one stored first is the one both get back. The lock is never held while a
decode runs. A decode reads queries, and a thread that holds the database's
lock, or a session's, may be the next one to ask for this lock.

`once_per_request` keys on the call itself, and lives only for the span a caller
declares with `request_scope`, on the thread that declared it. A `WorkspaceSession` holds its lock for the whole of
each public method and its inputs cannot change while it is held, so an
entrypoint asked the same question twice inside one method must answer the same
both times. Outside such a span the memo does not exist, so a caller driving the
integrations directly around a file edit still sees the edit. A session that
does rewrite the mirror inside one of its own methods calls
`request_inputs_changed` when it does. Only the thread that opened a span ever
sees it, so its memo needs no lock.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from pyinc.errors import CompositionError

if TYPE_CHECKING:
    from pyinc.runtime import Database

_T = TypeVar("_T")

# The bound is on entry count, not on bytes: an entry holds one decoded value,
# and a workspace-level entry is a whole analysis rather than a small object.
# What keeps the total in hand is that those values share substructure with the
# query results they were decoded from, which the kernel retains anyway. Past
# the limit the cache is cleared wholesale rather than evicted one entry at a
# time, which keeps lookups a single dict hit.
_MAX_ENTRIES = 8192

_CACHES: weakref.WeakKeyDictionary[
    Database, dict[tuple[Any, ...], tuple[tuple[Any, ...], Any]]
] = weakref.WeakKeyDictionary()
# Held for every read and write of `_CACHES` and of the dicts it holds, and for
# nothing else. Unguarded, two threads could each find no dict for a database
# and install one of their own, and the second install dropped every entry the
# first thread stored. The weak reference's callback, which drops a collected
# database's entry, runs on whatever thread collects it, possibly one that
# already holds this lock, so it does not take the lock. It deletes one key in
# a single dict operation, and no key a live database owns.
_CACHES_LOCK = threading.Lock()


@dataclass
class _Request:
    """One `request_scope` span: the database it promises about, and its memo.

    It travels in a `ContextVar`, so a context copied while it is open -- every
    `threading.Thread` started on a free-threaded 3.14 build, `asyncio.to_thread`
    -- keeps a reference to it after the span has closed and its promise with
    it. So a span belongs to the thread that opened it and ends when it closes,
    as the kernel's own request does.
    """

    db: Database
    memo: dict[Any, Any] = field(default_factory=dict)
    thread_ident: int = field(default_factory=threading.get_ident)
    ended: bool = False


_REQUEST: ContextVar[_Request | None] = ContextVar("pyinc_integration_request", default=None)


def _live_request() -> _Request | None:
    request = _REQUEST.get()
    if request is None or request.ended or request.thread_ident != threading.get_ident():
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
        request.ended = True
        _REQUEST.reset(token)


def request_inputs_changed() -> None:
    """Drop what this request has memoized, because its inputs just moved.

    A caller that mutates what the integrations read part-way through its own
    request has broken the promise `request_scope` makes and must say so.
    Saying so reaches the kernel too: when the caller also holds a
    `Database.request_span`, the span rolls onto a fresh request, so the
    kernel's own once-per-request work -- resource validation above all --
    re-runs against the moved inputs.
    """

    request = _live_request()
    if request is not None:
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
