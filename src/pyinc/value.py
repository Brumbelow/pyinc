from __future__ import annotations

import hashlib
import math
import os
import struct
import sys
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
_MAX_SNAPSHOT_DEPTH = 200


class ValueAdapter(Protocol):
    """Makes a foreign type snapshot-safe at the value boundary.

    Adapters extend the kernel's condition 1 boundary and inherit its laws:
    `freeze` and `thaw` are deterministic, side-effect-free, and read no
    ambient state (at query boundaries they run under the ambient-read
    guard); results are owned by the receiver; the round trip preserves
    semantics; and adapter instance configuration stays immutable for the
    registered lifetime. See the kernel contract's `ValueAdapter` entry.
    """

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
_FROZEN_TYPES = (
    FrozenList,
    FrozenDict,
    FrozenSet,
    FrozenRecord,
    FrozenAdapterValue,
    FrozenRef,
    FrozenGraph,
)
AdapterMap = Mapping[type[Any], ValueAdapter]


class _AdapterRegistry:
    def __init__(self, adapters: AdapterMap | None = None) -> None:
        self._adapters = dict(adapters or {})
        self._adapters_by_key = {
            _adapter_key(value_type): adapter for value_type, adapter in self._adapters.items()
        }
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
    # Graph-capable wrappers already passed through, and whether one of them was
    # reached a second time. Set on the optimistic pass, consumed by _freeze_root.
    wrapper_ids: set[int] = field(default_factory=set)
    saw_aliased_wrapper: bool = False
    # Route every graph-capable wrapper through the memo instead of passing it
    # through. Only the re-encoding pass sets this.
    refreeze_wrappers: bool = False


def freeze(value: Any, *, adapters: AdapterMap | _AdapterRegistry | None = None) -> Snapshot:
    registry = _coerce_registry(adapters)
    result = _freeze_root(value, registry)
    _validate_snapshot(result)
    return result


def _freeze_root(value: Any, registry: _AdapterRegistry) -> Snapshot:
    """Freeze one boundary value, re-encoding once if sibling wrappers alias.

    A wrapper reached twice is a back-edge only once every wrapper on the way
    registers in the memo, and the first pass deliberately does not register
    them: a tree wrapper has to keep inlining as a tree rather than becoming a
    node-table entry.
    Sharing whose lowest common ancestor is a raw tuple therefore cannot be
    seen until the aliased wrapper is met, by which point the first occurrence
    is already inlined. So the walk that finds it is the freeze itself, and the
    answer it produces is thrown away in favour of a second pass that routes
    wrappers through the graph machinery -- landing the snapshot the equivalent
    raw structure produces.
    """

    state = _FreezeState()
    snapshot = _freeze(value, registry, state)
    if state.saw_aliased_wrapper:
        retry = _FreezeState(refreeze_wrappers=True)
        return _finalize_snapshot(_freeze(value, registry, retry), retry)
    return _finalize_snapshot(snapshot, state)


def _finalize_snapshot(snapshot: Snapshot, state: _FreezeState) -> Snapshot:
    """Collapse a freeze state into its public canonical snapshot form."""

    if state.has_back_edge:
        return _canonicalize_graph(FrozenGraph(nodes=tuple(state.nodes), root=snapshot))
    if not state.nodes:
        # No memoization happened at all -- the walk already produced a
        # Database-owned snapshot (wrapper inputs were detached in _freeze),
        # so there are no refs to inline.
        return snapshot
    # Memoized but no back-edges: inline FrozenRefs so the public snapshot has the
    # same flat shape as v1 for tree-shaped inputs.
    return _inline_refs(snapshot, state.nodes)


def _freeze(value: Any, registry: _AdapterRegistry, state: _FreezeState) -> Snapshot:
    if type(value) in IMMUTABLE_SCALARS:
        if type(value) is float:
            return float.fromhex(value.hex())
        if type(value) is complex:
            return complex(
                float.fromhex(value.real.hex()),
                float.fromhex(value.imag.hex()),
            )
        return cast(Snapshot, value)
    if type(value) in _FROZEN_TYPES:
        # Both walks below read shell fields directly -- unpacking entry pairs
        # and iterating `items` -- so the field shapes have to be checked
        # first. Without this an inbound shell with a 3-element entry escapes
        # as a raw ValueError past every boundary handler, and one whose
        # `items` is a list gets rebuilt into a well-formed snapshot instead
        # of rejected.
        _validate_wrapper_shape(value)
        if state.refreeze_wrappers or _wrapper_aliases_structure(value):
            # A strict-mode boundary view rebuilds a FrozenGraph snapshot into
            # wrapper objects that genuinely share or cycle through each other.
            # Feeding one back across a boundary must restore the graph
            # encoding it came from; inlining the aliased wrappers as a tree
            # would either drop the sharing or recurse forever on a cycle.
            return _refreeze_wrapper(value, state)
        if type(value) in (FrozenList, FrozenDict, FrozenRecord) or (
            type(value) is FrozenSet and value.kind == "set"
        ):
            # Sibling wrappers can alias through a raw spine that carries no
            # memo slot of its own: a tuple holds none, and a memoized
            # container's slot sits one level above the wrappers it holds.
            # Only the four types a FrozenGraph node table can hold are worth
            # tracking; nothing else can be the target of a back-edge.
            if id(value) in state.wrapper_ids:
                state.saw_aliased_wrapper = True
            else:
                state.wrapper_ids.add(id(value))
                state.live_refs.append(value)
        return _detach_wrapper(value)
    adapter_match = registry.for_value(value)
    if adapter_match is not None:
        adapter_key, adapter = adapter_match
        with _active_guard(value, state):
            payload = adapter.freeze(value, lambda item: _freeze(item, registry, state))
            return FrozenAdapterValue(adapter_key, _freeze(payload, registry, state))
    if isinstance(value, IMMUTABLE_SCALARS):
        raise UnsupportedValueError(
            f"Scalar subclass {type(value).__qualname__} cannot cross cached "
            "boundaries without a ValueAdapter."
        )
    if isinstance(value, _FROZEN_TYPES):
        raise UnsupportedValueError(
            f"Snapshot wrapper subclass {type(value).__qualname__} is not supported."
        )
    if isinstance(value, list):
        return _freeze_via_memo(
            value,
            state,
            lambda: FrozenList(tuple(_freeze(item, registry, state) for item in value)),
        )
    if isinstance(value, frozenset):
        with _active_guard(value, state):
            return FrozenSet("frozenset", _freeze_unordered(value, registry, state))
    if isinstance(value, set):
        return _freeze_via_memo(
            value,
            state,
            lambda: FrozenSet("set", _freeze_unordered(value, registry, state)),
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


def _freeze_mapping(
    value: Mapping[Any, Any], registry: _AdapterRegistry, state: _FreezeState
) -> FrozenDict:
    frozen_items = tuple(
        sorted(
            (
                (
                    _freeze_hash_position(key, registry, state),
                    _freeze(item, registry, state),
                )
                for key, item in value.items()
            ),
            key=lambda item: _canonical_sort_key(item[0]),
        )
    )
    return FrozenDict(frozen_items)


def _freeze_dataclass(value: Any, registry: _AdapterRegistry, state: _FreezeState) -> FrozenRecord:
    frozen_items = cast(
        tuple[tuple[str, Any], ...],
        tuple(
            (field_def.name, _freeze(getattr(value, field_def.name), registry, state))
            for field_def in fields(value)
        ),
    )
    return FrozenRecord(type(value).__qualname__, frozen_items)


def _validate_wrapper_shape(value: Any, seen: set[int] | None = None) -> None:
    """Reject an inbound Frozen* shell whose fields are not the declared shapes.

    `_wrapper_aliases_structure` and `_detach_wrapper` both read these fields
    directly, so a malformed shell would otherwise escape as a raw
    `ValueError` -- which no boundary handler catches -- or be silently
    rebuilt into a well-formed snapshot by the clone.

    `_validate_snapshot` cannot serve here even though it owns the same rules:
    an inbound wrapper is allowed to be a genuinely cyclic or shared Python
    object graph, which is precisely what `_refreeze_wrapper` re-encodes,
    while the canonical grammar forbids exactly that. So this walk is
    visit-once rather than path-scoped and checks only the field shapes the
    two walks depend on; ordering, duplicate-key, and cycle rules stay with
    the canonical validator that runs on the result. The messages are kept
    identical to it so a caller sees a single contract.
    """

    value_type = type(value)
    if value_type not in _FROZEN_TYPES and value_type is not tuple:
        return
    if value_type is FrozenRef:
        return
    if seen is None:
        seen = set()
    object_id = id(value)
    if object_id in seen:
        return
    seen.add(object_id)
    if value_type is FrozenList:
        if type(value.items) is not tuple:
            raise UnsupportedValueError("FrozenList.items must be a tuple.")
        for item in value.items:
            _validate_wrapper_shape(item, seen)
        return
    if value_type is FrozenDict:
        if type(value.entries) is not tuple:
            raise UnsupportedValueError("FrozenDict.entries must be a tuple.")
        for entry in value.entries:
            if type(entry) is not tuple or len(entry) != 2:
                raise UnsupportedValueError("FrozenDict entries must be key/value pairs.")
            _validate_wrapper_shape(entry[0], seen)
            _validate_wrapper_shape(entry[1], seen)
        return
    if value_type is FrozenSet:
        if type(value.kind) is not str or value.kind not in {"set", "frozenset"}:
            raise UnsupportedValueError("FrozenSet.kind must be 'set' or 'frozenset'.")
        if type(value.items) is not tuple:
            raise UnsupportedValueError("FrozenSet.items must be a tuple.")
        for item in value.items:
            _validate_wrapper_shape(item, seen)
        return
    if value_type is FrozenRecord:
        if type(value.entries) is not tuple:
            raise UnsupportedValueError("FrozenRecord.entries must be a tuple.")
        for entry in value.entries:
            if type(entry) is not tuple or len(entry) != 2:
                raise UnsupportedValueError("FrozenRecord entries must be field/value pairs.")
            _validate_wrapper_shape(entry[1], seen)
        return
    if value_type is FrozenAdapterValue:
        _validate_wrapper_shape(value.payload, seen)
        return
    if value_type is FrozenGraph:
        if type(value.nodes) is not tuple or not value.nodes:
            raise UnsupportedValueError("FrozenGraph.nodes must be a non-empty tuple.")
        for node in value.nodes:
            _validate_wrapper_shape(node, seen)
        _validate_wrapper_shape(value.root, seen)
        return
    for item in value:
        _validate_wrapper_shape(item, seen)


def _wrapper_aliases_structure(value: Any) -> bool:
    """Report whether an already-frozen wrapper aliases any of its own parts.

    A plain tree wrapper is detached into an equal clone that keeps its tree
    shape, but a wrapper whose object graph revisits one of the four
    graph-capable container types (shared or cyclic) has to be re-encoded.
    Hash positions (mapping keys, set members) are frozen in isolated states
    and can never carry references, so they stay unvisited here exactly as
    they stay ref-free in `_freeze`.
    """

    seen_nodes: set[int] = set()
    active: set[int] = set()

    def walk(current: Any) -> bool:
        current_type = type(current)
        if current_type in (FrozenList, FrozenDict, FrozenRecord) or (
            current_type is FrozenSet and current.kind == "set"
        ):
            if id(current) in seen_nodes:
                return True
            seen_nodes.add(id(current))
            if current_type is FrozenList:
                return any(walk(item) for item in current.items)
            if current_type is FrozenDict:
                return any(walk(item) for _key, item in current.entries)
            if current_type is FrozenRecord:
                return any(walk(item) for _name, item in current.entries)
            return False
        if current_type is FrozenAdapterValue:
            if id(current) in active:
                return True
            active.add(id(current))
            try:
                return walk(current.payload)
            finally:
                active.discard(id(current))
        if current_type is tuple:
            if id(current) in active:
                return True
            active.add(id(current))
            try:
                return any(walk(item) for item in current)
            finally:
                active.discard(id(current))
        return False

    return walk(value)


def _refreeze_wrapper(value: Any, state: _FreezeState) -> Snapshot:
    """Re-encode an aliased wrapper graph through the raw-mutable memo machinery.

    Every graph-capable wrapper registers in `state` exactly as its raw
    counterpart would, so revisits become `FrozenRef` back-edges and
    `_finalize_snapshot` restores the canonical `FrozenGraph` -- the
    round-tripped snapshot fingerprints identically to the snapshot the view
    was built from.
    """

    value_type = type(value)
    if value_type in IMMUTABLE_SCALARS:
        return cast(Snapshot, value)
    if value_type is FrozenList:
        return _freeze_via_memo(
            value,
            state,
            lambda: FrozenList(tuple(_refreeze_wrapper(item, state) for item in value.items)),
        )
    if value_type is FrozenDict:
        # Keys were frozen in isolated states and carry no references, but
        # they can still be caller-held shells; detach them. Cloning is
        # structure-preserving, so the canonical entry order (keyed by each
        # key's fingerprint) is unchanged.
        return _freeze_via_memo(
            value,
            state,
            lambda: FrozenDict(
                tuple(
                    (_detach_wrapper(key), _refreeze_wrapper(item, state))
                    for key, item in value.entries
                )
            ),
        )
    if value_type is FrozenSet:
        if value.kind == "set":
            return _freeze_via_memo(
                value,
                state,
                lambda: FrozenSet("set", tuple(_detach_wrapper(item) for item in value.items)),
            )
        return _detach_wrapper(value)
    if value_type is FrozenRecord:
        return _freeze_via_memo(
            value,
            state,
            lambda: FrozenRecord(
                value.type_name,
                tuple((name, _refreeze_wrapper(item, state)) for name, item in value.entries),
            ),
        )
    if value_type is FrozenAdapterValue:
        with _active_guard(value, state):
            return FrozenAdapterValue(value.adapter_key, _refreeze_wrapper(value.payload, state))
    if value_type is tuple:
        with _active_guard(value, state):
            return tuple(_refreeze_wrapper(item, state) for item in value)
    if value_type in (FrozenGraph, FrozenRef):
        # A nested graph carries its own reference namespace, so neither this
        # pass nor the canonical renumbering that follows it descends into
        # one: without a detach here the caller's envelope, node table and all,
        # lands in the stored snapshot untouched.
        return _detach_wrapper(value)
    raise UnsupportedValueError(f"Unsupported snapshot value {value_type.__qualname__}.")


def _inline_refs(value: Snapshot, nodes: list[Any]) -> Snapshot:
    """Replace every FrozenRef with its target snapshot. Caller guarantees no back-edges exist."""
    if isinstance(value, FrozenRef):
        return _inline_refs(nodes[value.index], nodes)
    if isinstance(value, FrozenList):
        return FrozenList(tuple(_inline_refs(item, nodes) for item in value.items))
    if isinstance(value, FrozenDict):
        return FrozenDict(
            tuple((_inline_refs(k, nodes), _inline_refs(v, nodes)) for k, v in value.entries)
        )
    if isinstance(value, FrozenSet):
        return FrozenSet(value.kind, tuple(_inline_refs(item, nodes) for item in value.items))
    if isinstance(value, FrozenRecord):
        return FrozenRecord(
            value.type_name,
            tuple((k, _inline_refs(v, nodes)) for k, v in value.entries),
        )
    if isinstance(value, FrozenAdapterValue):
        return FrozenAdapterValue(value.adapter_key, _inline_refs(value.payload, nodes))
    if isinstance(value, tuple):
        return tuple(_inline_refs(item, nodes) for item in value)
    return value


def _detach_wrapper(value: Any, active: set[int] | None = None) -> Snapshot:
    """Deep-clone a snapshot so it shares no Frozen* shell with the caller.

    Every Frozen* type is a frozen dataclass, and ``object.__setattr__``
    rebinds its fields, so a stored snapshot sharing a shell with the caller
    would let the caller corrupt the record it came from. Leaf scalars and
    all-leaf tuples stay shared -- nothing reflective can rebind them --
    which is the same rule the strict boundary view applies on the way out.
    """

    value_type = type(value)
    if value_type not in _FROZEN_TYPES and value_type is not tuple:
        return cast(Snapshot, value)
    if value_type is FrozenRef:
        # A ref cell is a rebindable shell like every other one: handing the
        # caller's back leaves it holding a live index into the stored node
        # table, rewritable long after the snapshot was validated.
        return FrozenRef(value.index)
    if active is None:
        active = set()
    object_id = id(value)
    if object_id in active:
        raise UnsupportedValueError(
            "Snapshot wrappers may not contain direct Python object cycles."
        )
    active.add(object_id)
    try:
        if value_type is FrozenList:
            return FrozenList(tuple(_detach_wrapper(item, active) for item in value.items))
        if value_type is FrozenDict:
            return FrozenDict(
                tuple(
                    (_detach_wrapper(key, active), _detach_wrapper(item, active))
                    for key, item in value.entries
                )
            )
        if value_type is FrozenSet:
            return FrozenSet(
                value.kind, tuple(_detach_wrapper(item, active) for item in value.items)
            )
        if value_type is FrozenRecord:
            return FrozenRecord(
                value.type_name,
                tuple(
                    (name, _detach_wrapper(item, active)) for name, item in value.entries
                ),
            )
        if value_type is FrozenAdapterValue:
            return FrozenAdapterValue(
                value.adapter_key, _detach_wrapper(value.payload, active)
            )
        if value_type is FrozenGraph:
            return FrozenGraph(
                nodes=tuple(_detach_wrapper(node, active) for node in value.nodes),
                root=_detach_wrapper(value.root, active),
            )
        detached = tuple(_detach_wrapper(item, active) for item in value)
        if all(item is original for item, original in zip(detached, value, strict=True)):
            return cast(Snapshot, value)
        return detached
    finally:
        active.discard(object_id)


def _canonicalize_graph(graph: FrozenGraph) -> FrozenGraph:
    """Renumber graph nodes by deterministic first traversal from the root.

    Memo slots are allocated while live containers are visited.  Mapping and set
    contents are canonicalized only after their members have been frozen, so the
    allocation order can reflect insertion or hash iteration order.  Rewriting
    references from the already-canonical container traversal removes that
    incidental order while preserving sharing and cycles.
    """

    _validate_snapshot(graph)
    old_to_new: dict[int, int] = {}
    new_nodes: list[Any] = []

    def rewrite(value: Any) -> Any:
        if type(value) is FrozenRef:
            old_index = value.index
            existing = old_to_new.get(old_index)
            if existing is not None:
                return FrozenRef(existing)
            new_index = len(new_nodes)
            old_to_new[old_index] = new_index
            new_nodes.append(None)
            new_nodes[new_index] = rewrite(graph.nodes[old_index])
            return FrozenRef(new_index)
        if type(value) is FrozenList:
            return FrozenList(tuple(rewrite(item) for item in value.items))
        if type(value) is FrozenDict:
            return FrozenDict(tuple((rewrite(key), rewrite(item)) for key, item in value.entries))
        if type(value) is FrozenSet:
            return FrozenSet(value.kind, tuple(rewrite(item) for item in value.items))
        if type(value) is FrozenRecord:
            return FrozenRecord(
                value.type_name,
                tuple((name, rewrite(item)) for name, item in value.entries),
            )
        if type(value) is FrozenAdapterValue:
            return FrozenAdapterValue(value.adapter_key, rewrite(value.payload))
        if type(value) is FrozenGraph:
            # A nested graph is an already-frozen value with its own reference
            # namespace. Preserve it exactly; only this graph's node table is
            # being renumbered.
            return value
        if type(value) is tuple:
            return tuple(rewrite(item) for item in value)
        return value

    root = rewrite(graph.root)
    if len(old_to_new) != len(graph.nodes):
        raise UnsupportedValueError("FrozenGraph contains unreachable nodes.")
    return FrozenGraph(tuple(new_nodes), root)


def collect_adapter_keys(snapshot: Any) -> frozenset[str]:
    """Collect every adapter key a snapshot's ``FrozenAdapterValue``s depend on.

    Pure and registry-free: it walks the ``Snapshot`` union the same way
    ``_inline_refs`` does and returns the set of adapter keys reachable in it.
    The checkpoint path uses this to record, per manifest record, which adapter
    implementations a snapshot's fidelity rests on, so a record frozen under a
    since-changed (or now-missing) adapter is refused at warm time rather than
    thawed into a value a fresh run would not have produced.
    """
    keys: set[str] = set()
    _collect_adapter_keys(snapshot, keys)
    return frozenset(keys)


def _collect_adapter_keys(value: Any, keys: set[str]) -> None:
    if isinstance(value, FrozenAdapterValue):
        keys.add(value.adapter_key)
        _collect_adapter_keys(value.payload, keys)
        return
    if isinstance(value, FrozenList):
        for item in value.items:
            _collect_adapter_keys(item, keys)
        return
    if isinstance(value, FrozenDict):
        for key, item in value.entries:
            _collect_adapter_keys(key, keys)
            _collect_adapter_keys(item, keys)
        return
    if isinstance(value, FrozenSet):
        for item in value.items:
            _collect_adapter_keys(item, keys)
        return
    if isinstance(value, FrozenRecord):
        for _key, item in value.entries:
            _collect_adapter_keys(item, keys)
        return
    if isinstance(value, FrozenGraph):
        # Every memoized value is its own node, so walking all nodes (plus the
        # root) reaches each adapter value inline; a bare FrozenRef just points
        # back into this table and carries no key of its own.
        for node in value.nodes:
            _collect_adapter_keys(node, keys)
        _collect_adapter_keys(value.root, keys)
        return
    if isinstance(value, tuple):
        for item in value:
            _collect_adapter_keys(item, keys)
        return


def thaw(value: Any, *, adapters: AdapterMap | _AdapterRegistry | None = None) -> Any:
    _validate_snapshot(value)
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
        return {
            _thaw(key, registry, env): _thaw(item, registry, env) for key, item in value.entries
        }
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
        # Defensive only: adapted values are never legal graph nodes. `_freeze`
        # routes them through `_active_guard`, so a cyclic adapted value raises
        # before a graph is built, and `_validate_snapshot` rejects
        # `FrozenAdapterValue` as a `FrozenGraph` node. See the cycle carve-out
        # in docs/kernel-contract.md.
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


def _validate_snapshot(snapshot: Any) -> None:
    """Reject values outside the canonical K2 snapshot grammar."""

    active: set[int] = set()

    def encoded_digest(value: Any) -> str:
        buffer = bytearray(_KERNEL_FINGERPRINT_PREFIX)
        _encode_snapshot(value, buffer)
        return hashlib.sha256(buffer).hexdigest()

    def require_metadata_string(value: Any, description: str) -> str:
        if type(value) is not str or not value:
            raise UnsupportedValueError(f"{description} must be a non-empty string.")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise UnsupportedValueError(
                f"{description} must contain valid Unicode scalar values."
            ) from exc
        return value

    def walk(value: Any, depth: int, graph_size: int | None) -> None:
        if depth > _MAX_SNAPSHOT_DEPTH:
            raise UnsupportedValueError(
                f"Snapshot nesting exceeds the {_MAX_SNAPSHOT_DEPTH}-level limit."
            )
        if value is None or type(value) in (bool, int, bytes):
            return
        if type(value) is float:
            if math.isnan(value) and struct.pack(">d", value) != struct.pack(
                ">d", float.fromhex("nan")
            ):
                raise UnsupportedValueError(
                    "Snapshot NaN values must use the canonical bit pattern."
                )
            return
        if type(value) is complex:
            canonical_nan = struct.pack(">d", float.fromhex("nan"))
            if (math.isnan(value.real) and struct.pack(">d", value.real) != canonical_nan) or (
                math.isnan(value.imag) and struct.pack(">d", value.imag) != canonical_nan
            ):
                raise UnsupportedValueError(
                    "Snapshot complex NaNs must use the canonical bit pattern."
                )
            return
        if type(value) is str:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise UnsupportedValueError(
                    "Snapshot strings must contain valid Unicode scalar values."
                ) from exc
            return
        if type(value) is FrozenRef:
            if (
                graph_size is None
                or type(value.index) is not int
                or value.index < 0
                or value.index >= graph_size
            ):
                raise UnsupportedValueError(
                    "FrozenRef index is outside its FrozenGraph node table."
                )
            return

        object_id = id(value)
        if object_id in active:
            raise UnsupportedValueError(
                "Snapshot wrappers may not contain direct Python object cycles."
            )
        active.add(object_id)
        try:
            if type(value) is tuple:
                for item in value:
                    walk(item, depth + 1, graph_size)
                return
            if type(value) is FrozenList:
                if type(value.items) is not tuple:
                    raise UnsupportedValueError("FrozenList.items must be a tuple.")
                for item in value.items:
                    walk(item, depth + 1, graph_size)
                return
            if type(value) is FrozenDict:
                if type(value.entries) is not tuple:
                    raise UnsupportedValueError("FrozenDict.entries must be a tuple.")
                key_digests: list[str] = []
                for entry in value.entries:
                    if type(entry) is not tuple or len(entry) != 2:
                        raise UnsupportedValueError("FrozenDict entries must be key/value pairs.")
                    key, item = entry
                    walk(key, depth + 1, graph_size)
                    walk(item, depth + 1, graph_size)
                    key_digests.append(encoded_digest(key))
                if len(set(key_digests)) != len(key_digests):
                    raise UnsupportedValueError("FrozenDict contains duplicate frozen keys.")
                if key_digests != sorted(key_digests):
                    raise UnsupportedValueError("FrozenDict keys are not in canonical order.")
                return
            if type(value) is FrozenSet:
                if type(value.kind) is not str or value.kind not in {
                    "set",
                    "frozenset",
                }:
                    raise UnsupportedValueError("FrozenSet.kind must be 'set' or 'frozenset'.")
                if type(value.items) is not tuple:
                    raise UnsupportedValueError("FrozenSet.items must be a tuple.")
                item_digests: list[str] = []
                for item in value.items:
                    walk(item, depth + 1, graph_size)
                    item_digests.append(encoded_digest(item))
                if len(set(item_digests)) != len(item_digests):
                    raise UnsupportedValueError("FrozenSet contains duplicate frozen members.")
                if item_digests != sorted(item_digests):
                    raise UnsupportedValueError("FrozenSet members are not in canonical order.")
                return
            if type(value) is FrozenRecord:
                require_metadata_string(value.type_name, "FrozenRecord.type_name")
                if type(value.entries) is not tuple:
                    raise UnsupportedValueError("FrozenRecord.entries must be a tuple.")
                names: list[str] = []
                for entry in value.entries:
                    if type(entry) is not tuple or len(entry) != 2:
                        raise UnsupportedValueError(
                            "FrozenRecord entries must be field/value pairs."
                        )
                    field_name = require_metadata_string(entry[0], "FrozenRecord field names")
                    names.append(field_name)
                    walk(entry[1], depth + 1, graph_size)
                if len(set(names)) != len(names):
                    raise UnsupportedValueError("FrozenRecord contains duplicate field names.")
                return
            if type(value) is FrozenAdapterValue:
                require_metadata_string(value.adapter_key, "FrozenAdapterValue.adapter_key")
                walk(value.payload, depth + 1, graph_size)
                return
            if type(value) is FrozenGraph:
                if type(value.nodes) is not tuple or not value.nodes:
                    raise UnsupportedValueError("FrozenGraph.nodes must be a non-empty tuple.")
                node_count = len(value.nodes)
                for node in value.nodes:
                    if type(node) not in {
                        FrozenList,
                        FrozenDict,
                        FrozenSet,
                        FrozenRecord,
                    } or (type(node) is FrozenSet and node.kind != "set"):
                        raise UnsupportedValueError(
                            "FrozenGraph nodes must be mutable-container shells."
                        )
                    walk(node, depth + 1, node_count)
                walk(value.root, depth + 1, node_count)
                reachable: set[int] = set()
                pending = list(_snapshot_refs(value.root))
                while pending:
                    index = pending.pop()
                    if index in reachable:
                        continue
                    reachable.add(index)
                    pending.extend(_snapshot_refs(value.nodes[index]))
                if reachable != set(range(node_count)):
                    raise UnsupportedValueError("FrozenGraph contains unreachable nodes.")
                return
        finally:
            active.remove(object_id)
        raise UnsupportedValueError(f"Unsupported snapshot value {type(value).__qualname__}.")

    walk(snapshot, 0, None)


def _snapshot_refs(value: Any) -> tuple[int, ...]:
    """Collect references belonging to the nearest containing FrozenGraph."""

    refs: list[int] = []
    pending = [value]
    while pending:
        current = pending.pop()
        if type(current) is FrozenRef:
            refs.append(current.index)
        elif type(current) is FrozenGraph:
            continue
        elif type(current) is FrozenList:
            pending.extend(current.items)
        elif type(current) is FrozenDict:
            for key, item in current.entries:
                pending.extend((key, item))
        elif type(current) is FrozenSet:
            pending.extend(current.items)
        elif type(current) is FrozenRecord:
            pending.extend(item for _name, item in current.entries)
        elif type(current) is FrozenAdapterValue:
            pending.append(current.payload)
        elif type(current) is tuple:
            pending.extend(current)
    return tuple(refs)


def fingerprint_snapshot(snapshot: Any) -> str:
    buf = bytearray(_KERNEL_FINGERPRINT_PREFIX)
    _encode_snapshot(snapshot, buf)
    return hashlib.sha256(buf).hexdigest()


def serialize_snapshot(snapshot: Any) -> bytes:
    """Encode a snapshot to bytes. The byte form carries the kernel-version prefix
    so an `ArtifactStore` can refuse payloads from older kernel versions."""
    _validate_snapshot(snapshot)
    buf = bytearray(_KERNEL_FINGERPRINT_PREFIX)
    _encode_snapshot(snapshot, buf)
    return bytes(buf)


def deserialize_snapshot(payload: bytes) -> Snapshot:
    """Decode a snapshot from bytes produced by `serialize_snapshot`."""
    if not payload.startswith(_KERNEL_FINGERPRINT_PREFIX):
        raise UnsupportedValueError(
            f"Payload does not carry the expected kernel fingerprint version prefix {_KERNEL_FINGERPRINT_PREFIX!r}."
        )
    try:
        snapshot, offset = _decode_snapshot(memoryview(payload), len(_KERNEL_FINGERPRINT_PREFIX))
    except (IndexError, OverflowError, RecursionError, UnicodeError, ValueError) as exc:
        raise UnsupportedValueError("Payload contains an invalid snapshot encoding.") from exc
    if offset != len(payload):
        raise UnsupportedValueError("Payload contains trailing bytes after a complete snapshot.")
    _validate_snapshot(snapshot)
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
        try:
            body = str(value).encode("ascii")
        except ValueError as exc:
            # The grammar has no width limit; CPython's int-to-str conversion
            # does. Type the refusal like every other boundary rejection.
            raise UnsupportedValueError(
                f"Integer exceeds the {sys.get_int_max_str_digits()}-digit int-to-str "
                "conversion limit and cannot be encoded; raise the limit with "
                "sys.set_int_max_str_digits() or keep the value off the boundary."
            ) from exc
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
    tag = bytes(buf[offset : offset + 1])
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
        return (
            complex(
                float.fromhex(real_body.decode("ascii")),
                float.fromhex(imag_body.decode("ascii")),
            ),
            imag_end + 1,
        )
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
    if bytes(buf[offset : offset + len(expected)]) != expected:
        raise UnsupportedValueError(
            f"Expected {expected!r} at offset {offset}, got {bytes(buf[offset : offset + len(expected)])!r}."
        )


def _read_length_prefixed(buf: memoryview, offset: int) -> tuple[bytes, int]:
    """Read an `<int>:<bytes>` segment. Returns (body, offset_after_body)."""
    sep = bytes(buf).find(b":", offset)
    if sep == -1:
        raise UnsupportedValueError(f"Missing ':' length separator at offset {offset}.")
    length = int(bytes(buf[offset:sep]).decode("ascii"))
    end = sep + 1 + length
    return bytes(buf[sep + 1 : end]), end


def _read_length_prefixed_int(buf: memoryview, offset: int) -> tuple[int, int]:
    """Read an `<int>:` count prefix (no body bytes). Returns (count, offset_after_colon)."""
    sep = bytes(buf).find(b":", offset)
    if sep == -1:
        raise UnsupportedValueError(f"Missing ':' length separator at offset {offset}.")
    return int(bytes(buf[offset:sep]).decode("ascii")), sep + 1


def snapshots_equal(left: Any, right: Any) -> bool:
    return bool(left == right)


def semantic_equal(
    left: Any, right: Any, *, adapters: AdapterMap | _AdapterRegistry | None = None
) -> bool:
    return freeze(left, adapters=adapters) == freeze(right, adapters=adapters)


def assert_not_mutated(before: str, after: str) -> None:
    if before != after:
        raise MutationError("Query mutated one of its boundary inputs.")


def _freeze_unordered(
    values: Iterable[Any], registry: _AdapterRegistry, state: _FreezeState
) -> tuple[Any, ...]:
    snapshots = tuple(_freeze_hash_position(item, registry, state) for item in values)
    return tuple(sorted(snapshots, key=_canonical_sort_key))


def _freeze_hash_position(value: Any, registry: _AdapterRegistry, _state: _FreezeState) -> Snapshot:
    # Freeze hash-position values independently. Their live identity cannot
    # safely participate in a mutable graph, and isolating them prevents memo
    # node numbers allocated during mapping/set iteration from entering the
    # canonical ordering key.
    snapshot = _freeze_root(value, registry)
    _validate_snapshot(snapshot)
    if not _snapshot_thaws_hashably(snapshot, [], set()):
        raise UnsupportedValueError(
            "Values used as mapping keys or set members must remain hashable "
            "after thaw; register a ValueAdapter for this value."
        )
    return snapshot


def _snapshot_thaws_hashably(snapshot: Any, nodes: list[Any], active_refs: set[int]) -> bool:
    if snapshot is None or type(snapshot) in IMMUTABLE_SCALARS:
        return True
    if type(snapshot) is tuple:
        return all(_snapshot_thaws_hashably(item, nodes, active_refs) for item in snapshot)
    if type(snapshot) is FrozenSet:
        return snapshot.kind == "frozenset" and all(
            _snapshot_thaws_hashably(item, nodes, active_refs) for item in snapshot.items
        )
    if type(snapshot) is FrozenAdapterValue:
        # A tree payload is reconstructed wholly by the adapter and remains
        # hashable under its round-trip contract. A graph payload can expose
        # mutable shared/cyclic state after insertion into a dict/set, so its
        # hash stability cannot be proven here.
        return not _snapshot_contains_graph(snapshot.payload)
    if type(snapshot) is FrozenRef:
        index = snapshot.index
        if index in active_refs or index < 0 or index >= len(nodes):
            return False
        target = nodes[index]
        if target is None:
            return False
        active_refs.add(index)
        try:
            return _snapshot_thaws_hashably(target, nodes, active_refs)
        finally:
            active_refs.remove(index)
    return False


def _snapshot_contains_graph(snapshot: Any) -> bool:
    if type(snapshot) is FrozenGraph:
        return True
    if type(snapshot) is FrozenList:
        return any(_snapshot_contains_graph(item) for item in snapshot.items)
    if type(snapshot) is FrozenDict:
        return any(
            _snapshot_contains_graph(key) or _snapshot_contains_graph(item)
            for key, item in snapshot.entries
        )
    if type(snapshot) is FrozenSet:
        return any(_snapshot_contains_graph(item) for item in snapshot.items)
    if type(snapshot) is FrozenRecord:
        return any(_snapshot_contains_graph(item) for _name, item in snapshot.entries)
    if type(snapshot) is FrozenAdapterValue:
        return _snapshot_contains_graph(snapshot.payload)
    if type(snapshot) is tuple:
        return any(_snapshot_contains_graph(item) for item in snapshot)
    return False


def _canonical_sort_key(value: Any) -> str:
    return fingerprint_snapshot(value)


def _coerce_registry(
    adapters: AdapterMap | _AdapterRegistry | None,
) -> _AdapterRegistry:
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
        raise UnsupportedValueError(
            "Cyclic values cannot cross cached boundaries through this container type."
        )
    state.active_ids.add(object_id)
    try:
        yield
    finally:
        state.active_ids.discard(object_id)


def _adapter_key(value_type: type[Any]) -> str:
    return f"{value_type.__module__}:{value_type.__qualname__}"
