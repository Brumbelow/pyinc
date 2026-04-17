from __future__ import annotations

import hashlib
import math
import os
import struct
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from types import NoneType
from typing import Any, Protocol, cast, overload

from .errors import MutationError, UnsupportedValueError

FreezeFn = Callable[[Any], "Snapshot"]
ThawFn = Callable[[Any], Any]


class ValueAdapter(Protocol):
    def freeze(self, value: Any, freeze: FreezeFn) -> Any:
        """Convert a live value into a snapshot-safe payload."""

    def thaw(self, snapshot: Any, thaw: ThawFn) -> Any:
        """Reconstruct an exposed value from a frozen payload."""


@dataclass(frozen=True)
class FrozenList(Sequence[Any]):
    items: tuple[Any, ...]

    @overload
    def __getitem__(self, index: int) -> Any: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[Any]: ...

    def __getitem__(self, index: int | slice) -> Any:
        return self.items[index]

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[Any]:
        return iter(self.items)


@dataclass(frozen=True)
class FrozenDict(Mapping[Any, Any]):
    entries: tuple[tuple[Any, Any], ...]

    def __getitem__(self, key: Any) -> Any:
        for current_key, value in self.entries:
            if current_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[Any]:
        return (key for key, _ in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class FrozenSet:
    kind: str
    items: tuple[Any, ...]

    def __contains__(self, value: Any) -> bool:
        return value in self.items

    def __iter__(self) -> Iterator[Any]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)


@dataclass(frozen=True)
class FrozenRecord(Mapping[str, Any]):
    type_name: str
    entries: tuple[tuple[str, Any], ...]

    def __getitem__(self, key: str) -> Any:
        for current_key, value in self.entries:
            if current_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self.entries)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class FrozenAdapterValue:
    adapter_key: str
    payload: Any


Snapshot = (
    str
    | bytes
    | int
    | float
    | bool
    | None
    | complex
    | FrozenList
    | FrozenDict
    | FrozenSet
    | FrozenRecord
    | FrozenAdapterValue
    | tuple[Any, ...]
)

IMMUTABLE_SCALARS = (str, bytes, int, float, bool, NoneType, complex)
AdapterMap = Mapping[type[Any], ValueAdapter]


class _AdapterRegistry:
    def __init__(self, adapters: AdapterMap | None = None) -> None:
        self._adapters = dict(adapters or {})
        self._adapters_by_key = {_adapter_key(value_type): adapter for value_type, adapter in self._adapters.items()}
        if len(self._adapters_by_key) != len(self._adapters):
            raise ValueError("Adapter registry contains duplicate type identifiers.")

    def for_value(self, value: Any) -> tuple[str, ValueAdapter] | None:
        for candidate in type(value).__mro__:
            adapter = self._adapters.get(candidate)
            if adapter is not None:
                return _adapter_key(candidate), adapter
        return None

    def for_key(self, adapter_key: str) -> ValueAdapter | None:
        return self._adapters_by_key.get(adapter_key)


def freeze(value: Any, *, adapters: AdapterMap | _AdapterRegistry | None = None) -> Snapshot:
    registry = _coerce_registry(adapters)
    return _freeze(value, registry, set())


def _freeze(value: Any, registry: _AdapterRegistry, active_ids: set[int]) -> Snapshot:
    if isinstance(value, IMMUTABLE_SCALARS):
        return value
    if isinstance(value, FrozenList | FrozenDict | FrozenSet | FrozenRecord | FrozenAdapterValue):
        return value
    adapter_match = registry.for_value(value)
    if adapter_match is not None:
        adapter_key, adapter = adapter_match
        with _freeze_guard(value, active_ids):
            payload = adapter.freeze(value, lambda item: _freeze(item, registry, active_ids))
            return FrozenAdapterValue(adapter_key, _freeze(payload, registry, active_ids))
    if isinstance(value, tuple):
        with _freeze_guard(value, active_ids):
            return tuple(_freeze(item, registry, active_ids) for item in value)
    if isinstance(value, list):
        with _freeze_guard(value, active_ids):
            return FrozenList(tuple(_freeze(item, registry, active_ids) for item in value))
    if isinstance(value, frozenset):
        with _freeze_guard(value, active_ids):
            return FrozenSet("frozenset", _freeze_unordered(value, registry, active_ids))
    if isinstance(value, set):
        with _freeze_guard(value, active_ids):
            return FrozenSet("set", _freeze_unordered(value, registry, active_ids))
    if isinstance(value, Mapping):
        with _freeze_guard(value, active_ids):
            frozen_items = tuple(
                sorted(
                    ((_freeze(key, registry, active_ids), _freeze(item, registry, active_ids)) for key, item in value.items()),
                    key=lambda item: _canonical_sort_key(item[0]),
                )
            )
            return FrozenDict(frozen_items)
    if isinstance(value, os.PathLike):
        return cast(str | bytes, os.fspath(value))
    if is_dataclass(value) and not isinstance(value, type):
        with _freeze_guard(value, active_ids):
            frozen_items = cast(
                tuple[tuple[str, Any], ...],
                tuple((field.name, _freeze(getattr(value, field.name), registry, active_ids)) for field in fields(value)),
            )
            return FrozenRecord(type(value).__qualname__, frozen_items)
    if isinstance(value, range):
        return ("range", value.start, value.stop, value.step)
    if isinstance(value, Iterator):
        raise UnsupportedValueError("Iterators and generators cannot cross cached boundaries.")
    if isinstance(value, Iterable) and not isinstance(value, Sequence):
        raise UnsupportedValueError(
            f"Unsupported iterable boundary value {type(value).__qualname__}; "
            "materialize it to a list, tuple, or dict first."
        )
    raise UnsupportedValueError(
        f"Unsupported boundary value {type(value).__qualname__}; "
        "register an adapter or return a snapshot-safe value."
    )


def thaw(value: Any, *, adapters: AdapterMap | _AdapterRegistry | None = None) -> Any:
    registry = _coerce_registry(adapters)
    return _thaw(value, registry)


def _thaw(value: Any, registry: _AdapterRegistry) -> Any:
    if isinstance(value, FrozenAdapterValue):
        adapter = registry.for_key(value.adapter_key)
        if adapter is None:
            raise UnsupportedValueError(
                f"Cannot thaw adapted snapshot for {value.adapter_key!r} without the matching adapter registry."
            )
        return adapter.thaw(value.payload, lambda item: _thaw(item, registry))
    if isinstance(value, FrozenList):
        return [_thaw(item, registry) for item in value.items]
    if isinstance(value, FrozenDict):
        return {_thaw(key, registry): _thaw(item, registry) for key, item in value.entries}
    if isinstance(value, FrozenSet):
        thawed_items = tuple(_thaw(item, registry) for item in value.items)
        if value.kind == "frozenset":
            return frozenset(thawed_items)
        return set(thawed_items)
    if isinstance(value, FrozenRecord):
        return {key: _thaw(item, registry) for key, item in value.entries}
    if isinstance(value, tuple):
        return tuple(_thaw(item, registry) for item in value)
    return value


def fingerprint(value: Any, *, adapters: AdapterMap | _AdapterRegistry | None = None) -> str:
    snapshot = freeze(value, adapters=adapters)
    return fingerprint_snapshot(snapshot)


def fingerprint_snapshot(snapshot: Any) -> str:
    return hashlib.sha256(_canonical_bytes(snapshot)).hexdigest()


_LEN = struct.Struct(">Q")


def _canonical_bytes(value: Any) -> bytes:
    if value is None:
        return b"N"
    if isinstance(value, bool):
        return b"B" + (b"\x01" if value else b"\x00")
    if isinstance(value, int):
        magnitude = abs(value)
        data = magnitude.to_bytes((magnitude.bit_length() + 7) // 8 or 1, "big")
        sign = b"\x01" if value < 0 else b"\x00"
        return b"I" + sign + _LEN.pack(len(data)) + data
    if isinstance(value, float):
        if math.isnan(value):  # NaN canonicalization
            payload = struct.pack(">Q", 0x7FF8000000000000)
        elif value == 0.0:  # -0.0 → +0.0
            payload = struct.pack(">d", 0.0)
        else:
            payload = struct.pack(">d", value)
        return b"F" + payload
    if isinstance(value, complex):
        return b"C" + _canonical_bytes(value.real) + _canonical_bytes(value.imag)
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        return b"s" + _LEN.pack(len(encoded)) + encoded
    if isinstance(value, bytes):
        return b"b" + _LEN.pack(len(value)) + value
    if isinstance(value, FrozenList):
        return b"L" + _LEN.pack(len(value.items)) + b"".join(_canonical_bytes(item) for item in value.items)
    if isinstance(value, FrozenDict):
        return b"D" + _LEN.pack(len(value.entries)) + b"".join(
            _canonical_bytes(key) + _canonical_bytes(item) for key, item in value.entries
        )
    if isinstance(value, FrozenSet):
        kind = b"\x01" if value.kind == "frozenset" else b"\x00"
        return b"S" + kind + _LEN.pack(len(value.items)) + b"".join(_canonical_bytes(item) for item in value.items)
    if isinstance(value, FrozenRecord):
        name = value.type_name.encode("utf-8")
        entries = b"".join(
            _canonical_bytes(key) + _canonical_bytes(item) for key, item in value.entries
        )
        return b"R" + _LEN.pack(len(name)) + name + _LEN.pack(len(value.entries)) + entries
    if isinstance(value, FrozenAdapterValue):
        key = value.adapter_key.encode("utf-8")
        return b"A" + _LEN.pack(len(key)) + key + _canonical_bytes(value.payload)
    if isinstance(value, tuple):
        return b"t" + _LEN.pack(len(value)) + b"".join(_canonical_bytes(item) for item in value)
    raise UnsupportedValueError(
        f"Cannot canonicalize snapshot value of type {type(value).__qualname__}; "
        "expected a frozen/immutable snapshot node."
    )


def snapshots_equal(left: Any, right: Any) -> bool:
    return bool(left == right)


def semantic_equal(left: Any, right: Any, *, adapters: AdapterMap | _AdapterRegistry | None = None) -> bool:
    return freeze(left, adapters=adapters) == freeze(right, adapters=adapters)


def assert_not_mutated(before: str, after: str) -> None:
    if before != after:
        raise MutationError("Query mutated one of its boundary inputs.")


def _freeze_unordered(values: Iterable[Any], registry: _AdapterRegistry, active_ids: set[int]) -> tuple[Any, ...]:
    snapshots = tuple(_freeze(item, registry, active_ids) for item in values)
    return tuple(sorted(snapshots, key=_canonical_sort_key))


def _canonical_sort_key(value: Any) -> bytes:
    return _canonical_bytes(value)


def _coerce_registry(adapters: AdapterMap | _AdapterRegistry | None) -> _AdapterRegistry:
    if isinstance(adapters, _AdapterRegistry):
        return adapters
    return _AdapterRegistry(adapters)


@contextmanager
def _freeze_guard(value: Any, active_ids: set[int]) -> Iterator[None]:
    object_id = id(value)
    if object_id in active_ids:
        raise UnsupportedValueError("Cyclic values cannot cross cached boundaries.")
    active_ids.add(object_id)
    try:
        yield
    finally:
        active_ids.remove(object_id)


def _adapter_key(value_type: type[Any]) -> str:
    return f"{value_type.__module__}:{value_type.__qualname__}"
