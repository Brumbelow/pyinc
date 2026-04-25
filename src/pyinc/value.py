from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, is_dataclass
from types import NoneType
from typing import Any, Protocol, cast, overload

from .errors import MutationError, UnsupportedValueError

FreezeFn = Callable[[Any], "Snapshot"]
ThawFn = Callable[[Any], Any]

_KERNEL_FINGERPRINT_VERSION = 2
_KERNEL_FINGERPRINT_PREFIX = b"K2;"


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


@dataclass(frozen=True)
class FrozenRef:
    """Opaque pointer into a `FrozenGraph.nodes` table."""

    index: int


@dataclass(frozen=True)
class FrozenGraph:
    """Memoized snapshot of a value graph with shared or cyclic references."""

    nodes: tuple[Any, ...]
    root: Any


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
    | FrozenRef
    | FrozenGraph
    | tuple[Any, ...]
)

IMMUTABLE_SCALARS = (str, bytes, int, float, bool, NoneType, complex)
_FROZEN_TYPES = (FrozenList, FrozenDict, FrozenSet, FrozenRecord, FrozenAdapterValue, FrozenRef, FrozenGraph)
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


@dataclass
class _FreezeState:
    memo: dict[int, int] = field(default_factory=dict)
    nodes: list[Any] = field(default_factory=list)
    has_back_edge: bool = False
    active_ids: set[int] = field(default_factory=set)
    # Strong refs to every memoized value. Without this, a value freed mid-freeze
    # can have its id() reused by a later allocation, causing a spurious memo hit.
    live_refs: list[Any] = field(default_factory=list)


def freeze(value: Any, *, adapters: AdapterMap | _AdapterRegistry | None = None) -> Snapshot:
    registry = _coerce_registry(adapters)
    state = _FreezeState()
    snapshot = _freeze(value, registry, state)
    if state.has_back_edge:
        return FrozenGraph(nodes=tuple(state.nodes), root=snapshot)
    if not state.nodes:
        # No memoization happened at all — preserve the snapshot as-is so already-frozen
        # values pass through with identity intact.
        return snapshot
    # Memoized but no back-edges: inline FrozenRefs so the public snapshot has the
    # same flat shape as v1 for tree-shaped inputs.
    return _inline_refs(snapshot, state.nodes)


def _freeze(value: Any, registry: _AdapterRegistry, state: _FreezeState) -> Snapshot:
    if isinstance(value, IMMUTABLE_SCALARS):
        return value
    if isinstance(value, _FROZEN_TYPES):
        return value
    adapter_match = registry.for_value(value)
    if adapter_match is not None:
        adapter_key, adapter = adapter_match
        with _active_guard(value, state):
            payload = adapter.freeze(value, lambda item: _freeze(item, registry, state))
            return FrozenAdapterValue(adapter_key, _freeze(payload, registry, state))
    if isinstance(value, list):
        return _freeze_via_memo(
            value, state, lambda: FrozenList(tuple(_freeze(item, registry, state) for item in value))
        )
    if isinstance(value, frozenset):
        with _active_guard(value, state):
            return FrozenSet("frozenset", _freeze_unordered(value, registry, state))
    if isinstance(value, set):
        return _freeze_via_memo(
            value, state, lambda: FrozenSet("set", _freeze_unordered(value, registry, state))
        )
    if isinstance(value, Mapping):
        return _freeze_via_memo(value, state, lambda: _freeze_mapping(value, registry, state))
    if isinstance(value, tuple):
        with _active_guard(value, state):
            return tuple(_freeze(item, registry, state) for item in value)
    if isinstance(value, os.PathLike):
        return cast(str | bytes, os.fspath(value))
    if is_dataclass(value) and not isinstance(value, type):
        return _freeze_via_memo(value, state, lambda: _freeze_dataclass(value, registry, state))
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


def _freeze_via_memo(value: Any, state: _FreezeState, build: Callable[[], Snapshot]) -> Snapshot:
    obj_id = id(value)
    if obj_id in state.memo:
        state.has_back_edge = True
        return FrozenRef(state.memo[obj_id])
    idx = len(state.nodes)
    state.memo[obj_id] = idx
    state.nodes.append(None)
    state.live_refs.append(value)
    state.active_ids.add(obj_id)
    try:
        result = build()
    finally:
        state.active_ids.discard(obj_id)
    state.nodes[idx] = result
    return FrozenRef(idx)


def _freeze_mapping(value: Mapping[Any, Any], registry: _AdapterRegistry, state: _FreezeState) -> FrozenDict:
    frozen_items = tuple(
        sorted(
            ((_freeze(key, registry, state), _freeze(item, registry, state)) for key, item in value.items()),
            key=lambda item: _canonical_sort_key(item[0]),
        )
    )
    return FrozenDict(frozen_items)


def _freeze_dataclass(value: Any, registry: _AdapterRegistry, state: _FreezeState) -> FrozenRecord:
    frozen_items = cast(
        tuple[tuple[str, Any], ...],
        tuple((field_def.name, _freeze(getattr(value, field_def.name), registry, state)) for field_def in fields(value)),
    )
    return FrozenRecord(type(value).__qualname__, frozen_items)


def _inline_refs(value: Snapshot, nodes: list[Any]) -> Snapshot:
    """Replace every FrozenRef with its target snapshot. Caller guarantees no back-edges exist."""
    if isinstance(value, FrozenRef):
        return _inline_refs(nodes[value.index], nodes)
    if isinstance(value, FrozenList):
        return FrozenList(tuple(_inline_refs(item, nodes) for item in value.items))
    if isinstance(value, FrozenDict):
        return FrozenDict(tuple((_inline_refs(k, nodes), _inline_refs(v, nodes)) for k, v in value.entries))
    if isinstance(value, FrozenSet):
        return FrozenSet(value.kind, tuple(_inline_refs(item, nodes) for item in value.items))
    if isinstance(value, FrozenRecord):
        return FrozenRecord(
            value.type_name, tuple((k, _inline_refs(v, nodes)) for k, v in value.entries)
        )
    if isinstance(value, FrozenAdapterValue):
        return FrozenAdapterValue(value.adapter_key, _inline_refs(value.payload, nodes))
    if isinstance(value, tuple):
        return tuple(_inline_refs(item, nodes) for item in value)
    return value


def thaw(value: Any, *, adapters: AdapterMap | _AdapterRegistry | None = None) -> Any:
    registry = _coerce_registry(adapters)
    if isinstance(value, FrozenGraph):
        env: list[Any] = [_allocate_shell(node, registry) for node in value.nodes]
        for i, node in enumerate(value.nodes):
            _fill_shell(env[i], node, registry, env)
        return _thaw(value.root, registry, env)
    return _thaw(value, registry, None)


def _thaw(value: Any, registry: _AdapterRegistry, env: list[Any] | None) -> Any:
    if isinstance(value, FrozenRef):
        if env is None:
            raise UnsupportedValueError("FrozenRef encountered outside a FrozenGraph context.")
        return env[value.index]
    if isinstance(value, FrozenAdapterValue):
        adapter = registry.for_key(value.adapter_key)
        if adapter is None:
            raise UnsupportedValueError(
                f"Cannot thaw adapted snapshot for {value.adapter_key!r} without the matching adapter registry."
            )
        return adapter.thaw(value.payload, lambda item: _thaw(item, registry, env))
    if isinstance(value, FrozenList):
        return [_thaw(item, registry, env) for item in value.items]
    if isinstance(value, FrozenDict):
        return {_thaw(key, registry, env): _thaw(item, registry, env) for key, item in value.entries}
    if isinstance(value, FrozenSet):
        thawed_items = tuple(_thaw(item, registry, env) for item in value.items)
        if value.kind == "frozenset":
            return frozenset(thawed_items)
        return set(thawed_items)
    if isinstance(value, FrozenRecord):
        return {key: _thaw(item, registry, env) for key, item in value.entries}
    if isinstance(value, tuple):
        return tuple(_thaw(item, registry, env) for item in value)
    return value


def _allocate_shell(node: Any, registry: _AdapterRegistry) -> Any:
    """Allocate an empty container shell for two-pass thaw of a graph node."""
    if isinstance(node, FrozenList):
        return []
    if isinstance(node, FrozenSet):
        if node.kind == "set":
            return set()
        # Frozensets are immutable and cannot participate in cycles, so they are
        # never genuine back-edge targets. They reach this path only via memoized
        # adapter values; thaw eagerly during fill.
        return None
    if isinstance(node, FrozenDict):
        return {}
    if isinstance(node, FrozenRecord):
        return {}
    if isinstance(node, FrozenAdapterValue):
        # Adapter cycles are not supported in v2.0.0. The shell is None and any
        # back-reference into this node will raise during fill.
        return None
    raise UnsupportedValueError(
        f"Cannot allocate shell for node of type {type(node).__qualname__}."
    )


def _fill_shell(shell: Any, node: Any, registry: _AdapterRegistry, env: list[Any]) -> None:
    if isinstance(node, FrozenList):
        for item in node.items:
            shell.append(_thaw(item, registry, env))
        return
    if isinstance(node, FrozenDict):
        for key, item in node.entries:
            shell[_thaw(key, registry, env)] = _thaw(item, registry, env)
        return
    if isinstance(node, FrozenSet):
        if node.kind == "set":
            for item in node.items:
                shell.add(_thaw(item, registry, env))
        return
    if isinstance(node, FrozenRecord):
        for key, item in node.entries:
            shell[key] = _thaw(item, registry, env)
        return
    if isinstance(node, FrozenAdapterValue):
        adapter = registry.for_key(node.adapter_key)
        if adapter is None:
            raise UnsupportedValueError(
                f"Cannot thaw adapted snapshot for {node.adapter_key!r} without the matching adapter registry."
            )
        thawed = adapter.thaw(node.payload, lambda item: _thaw(item, registry, env))
        env_idx = env.index(shell) if shell is not None else None
        if env_idx is not None:
            env[env_idx] = thawed
        return


def fingerprint(value: Any, *, adapters: AdapterMap | _AdapterRegistry | None = None) -> str:
    snapshot = freeze(value, adapters=adapters)
    return fingerprint_snapshot(snapshot)


def fingerprint_snapshot(snapshot: Any) -> str:
    buf = bytearray(_KERNEL_FINGERPRINT_PREFIX)
    _encode_snapshot(snapshot, buf)
    return hashlib.sha256(buf).hexdigest()


def serialize_snapshot(snapshot: Any) -> bytes:
    """Encode a snapshot to bytes. The byte form carries the kernel-version prefix
    so an `ArtifactStore` can refuse payloads from older kernel versions."""
    buf = bytearray(_KERNEL_FINGERPRINT_PREFIX)
    _encode_snapshot(snapshot, buf)
    return bytes(buf)


def deserialize_snapshot(payload: bytes) -> Snapshot:
    """Decode a snapshot from bytes produced by `serialize_snapshot`."""
    if not payload.startswith(_KERNEL_FINGERPRINT_PREFIX):
        raise UnsupportedValueError(
            f"Payload does not carry the expected kernel fingerprint version prefix {_KERNEL_FINGERPRINT_PREFIX!r}."
        )
    snapshot, offset = _decode_snapshot(memoryview(payload), len(_KERNEL_FINGERPRINT_PREFIX))
    if offset != len(payload):
        raise UnsupportedValueError("Payload contains trailing bytes after a complete snapshot.")
    return snapshot


def _encode_snapshot(value: Any, buf: bytearray) -> None:
    if value is None:
        buf += b"N;"
        return
    if value is True:
        buf += b"T;"
        return
    if value is False:
        buf += b"F;"
        return
    if isinstance(value, int):
        body = str(value).encode("ascii")
        buf += b"i"
        buf += str(len(body)).encode("ascii")
        buf += b":"
        buf += body
        buf += b";"
        return
    if isinstance(value, float):
        body = value.hex().encode("ascii")
        buf += b"f"
        buf += str(len(body)).encode("ascii")
        buf += b":"
        buf += body
        buf += b";"
        return
    if isinstance(value, complex):
        real_body = value.real.hex().encode("ascii")
        imag_body = value.imag.hex().encode("ascii")
        buf += b"c"
        buf += str(len(real_body)).encode("ascii")
        buf += b":"
        buf += real_body
        buf += b","
        buf += str(len(imag_body)).encode("ascii")
        buf += b":"
        buf += imag_body
        buf += b";"
        return
    if isinstance(value, str):
        body = value.encode("utf-8")
        buf += b"s"
        buf += str(len(body)).encode("ascii")
        buf += b":"
        buf += body
        buf += b";"
        return
    if isinstance(value, bytes):
        buf += b"b"
        buf += str(len(value)).encode("ascii")
        buf += b":"
        buf += value
        buf += b";"
        return
    if isinstance(value, FrozenList):
        buf += b"L"
        buf += str(len(value.items)).encode("ascii")
        buf += b":"
        for item in value.items:
            _encode_snapshot(item, buf)
        buf += b";"
        return
    if isinstance(value, FrozenDict):
        buf += b"D"
        buf += str(len(value.entries)).encode("ascii")
        buf += b":"
        for key, item in value.entries:
            _encode_snapshot(key, buf)
            _encode_snapshot(item, buf)
        buf += b";"
        return
    if isinstance(value, FrozenSet):
        kind_body = value.kind.encode("utf-8")
        buf += b"S"
        buf += str(len(kind_body)).encode("ascii")
        buf += b":"
        buf += kind_body
        buf += b","
        buf += str(len(value.items)).encode("ascii")
        buf += b":"
        for item in value.items:
            _encode_snapshot(item, buf)
        buf += b";"
        return
    if isinstance(value, FrozenRecord):
        name_body = value.type_name.encode("utf-8")
        buf += b"R"
        buf += str(len(name_body)).encode("ascii")
        buf += b":"
        buf += name_body
        buf += b","
        buf += str(len(value.entries)).encode("ascii")
        buf += b":"
        for key, item in value.entries:
            key_body = key.encode("utf-8")
            buf += str(len(key_body)).encode("ascii")
            buf += b":"
            buf += key_body
            _encode_snapshot(item, buf)
        buf += b";"
        return
    if isinstance(value, FrozenAdapterValue):
        key_body = value.adapter_key.encode("utf-8")
        buf += b"A"
        buf += str(len(key_body)).encode("ascii")
        buf += b":"
        buf += key_body
        buf += b","
        _encode_snapshot(value.payload, buf)
        buf += b";"
        return
    if isinstance(value, FrozenRef):
        body = str(value.index).encode("ascii")
        buf += b"r"
        buf += str(len(body)).encode("ascii")
        buf += b":"
        buf += body
        buf += b";"
        return
    if isinstance(value, FrozenGraph):
        buf += b"G"
        buf += str(len(value.nodes)).encode("ascii")
        buf += b":"
        for node in value.nodes:
            _encode_snapshot(node, buf)
        _encode_snapshot(value.root, buf)
        buf += b";"
        return
    if isinstance(value, tuple):
        buf += b"t"
        buf += str(len(value)).encode("ascii")
        buf += b":"
        for item in value:
            _encode_snapshot(item, buf)
        buf += b";"
        return
    raise TypeError(
        f"fingerprint_snapshot: unsupported snapshot value of type {type(value).__qualname__!r}; "
        "all inputs must be produced by freeze()."
    )


def _decode_snapshot(buf: memoryview, offset: int) -> tuple[Snapshot, int]:
    """Inverse of `_encode_snapshot`. Returns the decoded snapshot and the new offset."""
    tag = bytes(buf[offset:offset + 1])
    if tag == b"N":
        _expect(buf, offset + 1, b";")
        return None, offset + 2
    if tag == b"T":
        _expect(buf, offset + 1, b";")
        return True, offset + 2
    if tag == b"F":
        _expect(buf, offset + 1, b";")
        return False, offset + 2
    if tag == b"i":
        body, end = _read_length_prefixed(buf, offset + 1)
        _expect(buf, end, b";")
        return int(body.decode("ascii")), end + 1
    if tag == b"f":
        body, end = _read_length_prefixed(buf, offset + 1)
        _expect(buf, end, b";")
        return float.fromhex(body.decode("ascii")), end + 1
    if tag == b"c":
        real_body, real_end = _read_length_prefixed(buf, offset + 1)
        _expect(buf, real_end, b",")
        imag_body, imag_end = _read_length_prefixed(buf, real_end + 1)
        _expect(buf, imag_end, b";")
        return complex(float.fromhex(real_body.decode("ascii")), float.fromhex(imag_body.decode("ascii"))), imag_end + 1
    if tag == b"s":
        body, end = _read_length_prefixed(buf, offset + 1)
        _expect(buf, end, b";")
        return body.decode("utf-8"), end + 1
    if tag == b"b":
        body, end = _read_length_prefixed(buf, offset + 1)
        _expect(buf, end, b";")
        return bytes(body), end + 1
    if tag == b"L":
        count_body, count_end = _read_length_prefixed_int(buf, offset + 1)
        cursor = count_end
        items: list[Snapshot] = []
        for _ in range(count_body):
            item, cursor = _decode_snapshot(buf, cursor)
            items.append(item)
        _expect(buf, cursor, b";")
        return FrozenList(tuple(items)), cursor + 1
    if tag == b"D":
        count_body, count_end = _read_length_prefixed_int(buf, offset + 1)
        cursor = count_end
        entries: list[tuple[Any, Any]] = []
        for _ in range(count_body):
            key, cursor = _decode_snapshot(buf, cursor)
            val, cursor = _decode_snapshot(buf, cursor)
            entries.append((key, val))
        _expect(buf, cursor, b";")
        return FrozenDict(tuple(entries)), cursor + 1
    if tag == b"S":
        kind_body, kind_end = _read_length_prefixed(buf, offset + 1)
        _expect(buf, kind_end, b",")
        count_body, count_end = _read_length_prefixed_int(buf, kind_end + 1)
        cursor = count_end
        items_s: list[Any] = []
        for _ in range(count_body):
            item, cursor = _decode_snapshot(buf, cursor)
            items_s.append(item)
        _expect(buf, cursor, b";")
        return FrozenSet(kind_body.decode("utf-8"), tuple(items_s)), cursor + 1
    if tag == b"R":
        name_body, name_end = _read_length_prefixed(buf, offset + 1)
        _expect(buf, name_end, b",")
        count_body, count_end = _read_length_prefixed_int(buf, name_end + 1)
        cursor = count_end
        entries_r: list[tuple[str, Any]] = []
        for _ in range(count_body):
            key_body, key_end = _read_length_prefixed(buf, cursor)
            cursor = key_end
            val, cursor = _decode_snapshot(buf, cursor)
            entries_r.append((key_body.decode("utf-8"), val))
        _expect(buf, cursor, b";")
        return FrozenRecord(name_body.decode("utf-8"), tuple(entries_r)), cursor + 1
    if tag == b"A":
        key_body, key_end = _read_length_prefixed(buf, offset + 1)
        _expect(buf, key_end, b",")
        payload, payload_end = _decode_snapshot(buf, key_end + 1)
        _expect(buf, payload_end, b";")
        return FrozenAdapterValue(key_body.decode("utf-8"), payload), payload_end + 1
    if tag == b"r":
        body, end = _read_length_prefixed(buf, offset + 1)
        _expect(buf, end, b";")
        return FrozenRef(int(body.decode("ascii"))), end + 1
    if tag == b"G":
        count_body, count_end = _read_length_prefixed_int(buf, offset + 1)
        cursor = count_end
        nodes: list[Any] = []
        for _ in range(count_body):
            node, cursor = _decode_snapshot(buf, cursor)
            nodes.append(node)
        root, cursor = _decode_snapshot(buf, cursor)
        _expect(buf, cursor, b";")
        return FrozenGraph(tuple(nodes), root), cursor + 1
    if tag == b"t":
        count_body, count_end = _read_length_prefixed_int(buf, offset + 1)
        cursor = count_end
        items_t: list[Any] = []
        for _ in range(count_body):
            item, cursor = _decode_snapshot(buf, cursor)
            items_t.append(item)
        _expect(buf, cursor, b";")
        return tuple(items_t), cursor + 1
    raise UnsupportedValueError(f"Unknown snapshot tag {tag!r} at offset {offset}.")


def _expect(buf: memoryview, offset: int, expected: bytes) -> None:
    if bytes(buf[offset:offset + len(expected)]) != expected:
        raise UnsupportedValueError(
            f"Expected {expected!r} at offset {offset}, got {bytes(buf[offset:offset + len(expected)])!r}."
        )


def _read_length_prefixed(buf: memoryview, offset: int) -> tuple[bytes, int]:
    """Read an `<int>:<bytes>` segment. Returns (body, offset_after_body)."""
    sep = bytes(buf).find(b":", offset)
    if sep == -1:
        raise UnsupportedValueError(f"Missing ':' length separator at offset {offset}.")
    length = int(bytes(buf[offset:sep]).decode("ascii"))
    end = sep + 1 + length
    return bytes(buf[sep + 1:end]), end


def _read_length_prefixed_int(buf: memoryview, offset: int) -> tuple[int, int]:
    """Read an `<int>:` count prefix (no body bytes). Returns (count, offset_after_colon)."""
    sep = bytes(buf).find(b":", offset)
    if sep == -1:
        raise UnsupportedValueError(f"Missing ':' length separator at offset {offset}.")
    return int(bytes(buf[offset:sep]).decode("ascii")), sep + 1


def snapshots_equal(left: Any, right: Any) -> bool:
    return bool(left == right)


def semantic_equal(left: Any, right: Any, *, adapters: AdapterMap | _AdapterRegistry | None = None) -> bool:
    return freeze(left, adapters=adapters) == freeze(right, adapters=adapters)


def assert_not_mutated(before: str, after: str) -> None:
    if before != after:
        raise MutationError("Query mutated one of its boundary inputs.")


def _freeze_unordered(values: Iterable[Any], registry: _AdapterRegistry, state: _FreezeState) -> tuple[Any, ...]:
    snapshots = tuple(_freeze(item, registry, state) for item in values)
    return tuple(sorted(snapshots, key=_canonical_sort_key))


def _canonical_sort_key(value: Any) -> str:
    return fingerprint_snapshot(value)


def _coerce_registry(adapters: AdapterMap | _AdapterRegistry | None) -> _AdapterRegistry:
    if isinstance(adapters, _AdapterRegistry):
        return adapters
    return _AdapterRegistry(adapters)


@contextmanager
def _active_guard(value: Any, state: _FreezeState) -> Iterator[None]:
    """Reject in-flight cycles for non-memoized container types (tuple, frozenset, adapter)
    where Python cannot naturally construct a self-reference but a hand-built cycle would
    otherwise infinitely recurse."""
    object_id = id(value)
    if object_id in state.active_ids:
        raise UnsupportedValueError("Cyclic values cannot cross cached boundaries through this container type.")
    state.active_ids.add(object_id)
    try:
        yield
    finally:
        state.active_ids.discard(object_id)


def _adapter_key(value_type: type[Any]) -> str:
    return f"{value_type.__module__}:{value_type.__qualname__}"
