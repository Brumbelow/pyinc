"""Memoized decoding of query payloads into the integration value types.

A layer-3 entrypoint like :func:`scope_tree` is a pure function of the payloads
it reads: given the same payload objects it builds the same dataclasses. The
kernel already decides when a payload is stale — while a query's value stands it
hands back the very same payload object, and when the value changes it hands
back a different one. So a decode keyed on the *identity* of the payloads it was
built from is valid for exactly as long as those values are, and nothing here
has to reason about invalidation.

This matters because the entrypoints are called many times per request from
different places (a reference scan, a symbol lookup, a diagnostic walk) and each
call used to rebuild the whole tree of dataclasses again.

The cache holds a reference to every payload it keys on, which is what makes
`id()` safe to key on: an entry's payload cannot be collected and have its
address reused while the entry still refers to it. It is bounded so a
long-running process that has walked many workspaces cannot grow it without
limit.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

_T = TypeVar("_T")

# Entries are small (one decoded value each) but unbounded growth in a
# long-lived server is not acceptable; past the limit the cache starts over
# rather than evicting one at a time, which keeps lookups a single dict hit.
_MAX_ENTRIES = 8192

_CACHE: dict[tuple[Any, ...], tuple[tuple[Any, ...], Any]] = {}


def decoded(kind: str, sources: tuple[Any, ...], decode: Callable[[], _T]) -> _T:
    """Return ``decode()`` for these ``sources``, reusing an earlier result.

    ``sources`` must name every value the decode reads, and ``kind`` keeps two
    decoders that read the same payload from colliding.
    """

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
