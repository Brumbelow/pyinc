"""Reuse of decoded query payloads, across requests and within one.

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

`once_per_request` keys on the call itself, and lives only for the span a caller
declares with `request_scope`. A `WorkspaceSession` holds its lock for the whole of
each public method and its inputs cannot change while it is held, so an
entrypoint asked the same question twice inside one method must answer the same
both times. Outside such a span the memo does not exist, so a caller driving the
integrations directly around a file edit still sees the edit. A session that
does rewrite the mirror inside one of its own methods calls
`request_inputs_changed` when it does.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, TypeVar

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

_CACHE: dict[tuple[Any, ...], tuple[tuple[Any, ...], Any]] = {}

_REQUEST: ContextVar[tuple[Database, dict[Any, Any]] | None] = ContextVar(
    "pyinc_integration_request", default=None
)


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
    entry = _CACHE.get(key)
    if entry is not None:
        held, value = entry
        if all(left is right for left, right in zip(held, sources, strict=True)):
            return value  # type: ignore[no-any-return]
    value = decode()
    if len(_CACHE) >= _MAX_ENTRIES:
        _CACHE.clear()
    _CACHE[key] = (sources, value)
    return value


@contextmanager
def request_scope(db: Database) -> Iterator[None]:
    """Declare that ``db``'s inputs cannot change for the duration.

    Repeated entrypoint calls inside the span answer from the first one.
    """

    token = _REQUEST.set((db, {}))
    try:
        yield
    finally:
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

    scope = _REQUEST.get()
    if scope is not None:
        scope[1].clear()
        scope[0].request_inputs_changed()


def once_per_request(
    db: Database, kind: str, args: tuple[Any, ...], compute: Callable[[], _T]
) -> _T:
    """Return ``compute()``, answering from this request if it already ran."""

    scope = _REQUEST.get()
    if scope is None or scope[0] is not db:
        return compute()
    memo = scope[1]
    key = (kind, args)
    if key in memo:
        return memo[key]  # type: ignore[no-any-return]
    value = compute()
    memo[key] = value
    return value
