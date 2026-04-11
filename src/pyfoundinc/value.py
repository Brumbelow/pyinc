from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
import os
import pickle
from types import NoneType
from typing import Any

from .errors import MutationError, UnsupportedValueError


@dataclass(frozen=True)
class FrozenList(Sequence[Any]):
    items: tuple[Any, ...]

    def __getitem__(self, index: int) -> Any:
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
    items: frozenset[Any]

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
    | tuple[Any, ...]
)

IMMUTABLE_SCALARS = (str, bytes, int, float, bool, NoneType, complex)


def freeze(value: Any) -> Snapshot:
    if isinstance(value, IMMUTABLE_SCALARS):
        return value
    if isinstance(value, FrozenList | FrozenDict | FrozenSet | FrozenRecord):
        return value
    if isinstance(value, tuple):
        return tuple(freeze(item) for item in value)
    if isinstance(value, list):
        return FrozenList(tuple(freeze(item) for item in value))
    if isinstance(value, frozenset):
        return frozenset(freeze(item) for item in value)
    if isinstance(value, set):
        return FrozenSet(frozenset(freeze(item) for item in value))
    if isinstance(value, Mapping):
        frozen_items = tuple(
            sorted(
                ((freeze(key), freeze(item)) for key, item in value.items()),
                key=_sort_key,
            )
        )
        return FrozenDict(frozen_items)
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if is_dataclass(value) and not isinstance(value, type):
        frozen_items = tuple((field.name, freeze(getattr(value, field.name))) for field in fields(value))
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


def thaw(value: Any) -> Any:
    if isinstance(value, FrozenList):
        return [thaw(item) for item in value.items]
    if isinstance(value, FrozenDict):
        return {thaw(key): thaw(item) for key, item in value.entries}
    if isinstance(value, FrozenSet):
        return {thaw(item) for item in value.items}
    if isinstance(value, FrozenRecord):
        return {key: thaw(item) for key, item in value.entries}
    if isinstance(value, tuple):
        return tuple(thaw(item) for item in value)
    if isinstance(value, frozenset):
        return frozenset(thaw(item) for item in value)
    return value


def fingerprint(value: Any) -> str:
    snapshot = freeze(value)
    return fingerprint_snapshot(snapshot)


def fingerprint_snapshot(snapshot: Any) -> str:
    return pickle.dumps(snapshot, protocol=5).hex()


def snapshots_equal(left: Any, right: Any) -> bool:
    return left == right


def semantic_equal(left: Any, right: Any) -> bool:
    return freeze(left) == freeze(right)


def assert_not_mutated(before: str, after: str) -> None:
    if before != after:
        raise MutationError("Query mutated one of its boundary inputs.")


def _sort_key(item: tuple[Any, Any]) -> tuple[str, str]:
    key, _ = item
    return (type(key).__qualname__, repr(key))
