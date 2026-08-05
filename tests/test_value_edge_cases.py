from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

import pytest

from pyinc import UnsupportedValueError, deserialize_snapshot, freeze, serialize_snapshot, thaw
from pyinc.value import (
    FrozenAdapterValue,
    FrozenDict,
    FrozenGraph,
    FrozenList,
    FrozenRecord,
    FrozenRef,
    FrozenSet,
    _active_guard,
    _adapter_key,
    _AdapterRegistry,
    _allocate_shell,
    _canonicalize_graph,
    _coerce_registry,
    _collect_adapter_keys,
    _fill_shell,
    _FreezeState,
    _snapshot_contains_graph,
    _snapshot_refs,
    _snapshot_thaws_hashably,
    _thaw,
    collect_adapter_keys,
    fingerprint_snapshot,
    snapshots_equal,
)


@dataclass(frozen=True)
class _AdaptedValue:
    value: int


class _AdaptedValueAdapter:
    def freeze(self, value: _AdaptedValue, freeze_value: Any) -> object:
        return freeze_value(value.value)

    def thaw(self, snapshot: Any, thaw_value: Any) -> _AdaptedValue:
        return _AdaptedValue(thaw_value(snapshot))


class _IterableOnly:
    def __iter__(self) -> Iterator[int]:
        return iter((1, 2, 3))


def _invalid_order(values: tuple[Any, ...]) -> tuple[Any, ...]:
    ordered = tuple(sorted(values, key=fingerprint_snapshot))
    return tuple(reversed(ordered))


def _canonical_order(values: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(sorted(values, key=fingerprint_snapshot))


def test_adapter_registry_rejects_duplicate_type_identifiers() -> None:
    first = type("Duplicate", (), {"__module__": "duplicate", "__qualname__": "Duplicate"})
    second = type("Duplicate", (), {"__module__": "duplicate", "__qualname__": "Duplicate"})
    adapter = _AdaptedValueAdapter()
    with pytest.raises(ValueError, match="duplicate type identifiers"):
        _AdapterRegistry({first: adapter, second: adapter})


def test_freeze_rejects_snapshot_wrapper_subclasses_and_iterable_only_values() -> None:
    class FrozenListSubclass(FrozenList):
        pass

    with pytest.raises(UnsupportedValueError, match="Snapshot wrapper subclass"):
        freeze(FrozenListSubclass(()))
    with pytest.raises(UnsupportedValueError, match="materialize"):
        freeze(_IterableOnly())


def test_collect_adapter_keys_walks_every_snapshot_container() -> None:
    snapshot = FrozenGraph(
        nodes=(
            FrozenList(
                (
                    FrozenAdapterValue("list", 1),
                    FrozenDict(
                        ((FrozenAdapterValue("dict-key", 2), FrozenAdapterValue("dict-value", 3)),)
                    ),
                    FrozenSet("set", (FrozenAdapterValue("set", 4),)),
                    FrozenRecord("Record", (("field", FrozenAdapterValue("record", 5)),)),
                    (FrozenAdapterValue("tuple", 6),),
                )
            ),
        ),
        root=FrozenAdapterValue("root", FrozenRef(0)),
    )
    assert collect_adapter_keys(snapshot) == frozenset(
        {"list", "dict-key", "dict-value", "set", "record", "tuple", "root"}
    )

    keys: set[str] = set()
    _collect_adapter_keys(1, keys)
    assert keys == set()


def test_internal_thaw_reports_a_reference_without_a_graph() -> None:
    with pytest.raises(UnsupportedValueError, match="outside a FrozenGraph"):
        _thaw(FrozenRef(0), _AdapterRegistry(), None)


def test_graph_shell_allocation_covers_each_supported_node_kind() -> None:
    assert _allocate_shell(FrozenList(()), _AdapterRegistry()) == []
    assert _allocate_shell(FrozenSet("set", ()), _AdapterRegistry()) == set()
    assert _allocate_shell(FrozenSet("frozenset", ()), _AdapterRegistry()) is None
    assert _allocate_shell(FrozenDict(()), _AdapterRegistry()) == {}
    assert _allocate_shell(FrozenRecord("Record", ()), _AdapterRegistry()) == {}
    assert _allocate_shell(FrozenAdapterValue("adapter", 1), _AdapterRegistry()) is None
    with pytest.raises(UnsupportedValueError, match="Cannot allocate shell"):
        _allocate_shell((), _AdapterRegistry())


def test_graph_shell_filling_covers_sets_records_and_adapters() -> None:
    registry = _AdapterRegistry({_AdaptedValue: _AdaptedValueAdapter()})

    list_shell: list[object] = []
    _fill_shell(list_shell, FrozenList((1,)), registry, [])
    assert list_shell == [1]

    dict_shell: dict[object, object] = {}
    _fill_shell(dict_shell, FrozenDict((("key", 2),)), registry, [])
    assert dict_shell == {"key": 2}

    set_shell: set[object] = set()
    _fill_shell(set_shell, FrozenSet("set", (3,)), registry, [])
    assert set_shell == {3}
    _fill_shell(None, FrozenSet("frozenset", (4,)), registry, [])

    record_shell: dict[str, object] = {}
    _fill_shell(record_shell, FrozenRecord("Record", (("field", 5),)), registry, [])
    assert record_shell == {"field": 5}

    missing = FrozenAdapterValue("missing:Adapter", 1)
    with pytest.raises(UnsupportedValueError, match="without the matching adapter"):
        _fill_shell(None, missing, registry, [None])

    adapted = FrozenAdapterValue(_adapter_key(_AdaptedValue), 6)
    empty_environment: list[object] = [None]
    _fill_shell(None, adapted, registry, empty_environment)
    assert empty_environment == [None]

    placeholder: dict[object, object] = {}
    environment: list[object] = [placeholder]
    _fill_shell(placeholder, adapted, registry, environment)
    assert environment == [_AdaptedValue(6)]

    _fill_shell(None, 1, registry, [])


def test_snapshot_validation_rejects_deep_direct_cycle_and_noncanonical_scalars() -> None:
    nested: object = None
    for _ in range(202):
        nested = (nested,)
    with pytest.raises(UnsupportedValueError, match="nesting exceeds"):
        serialize_snapshot(cast(Any, nested))

    cyclic = FrozenList(())
    object.__setattr__(cyclic, "items", (cyclic,))
    with pytest.raises(UnsupportedValueError, match="direct Python object cycles"):
        serialize_snapshot(cyclic)

    noncanonical_nan = struct.unpack(">d", bytes.fromhex("7ff8000000000001"))[0]
    with pytest.raises(UnsupportedValueError, match="complex NaNs"):
        serialize_snapshot(complex(noncanonical_nan, 0))
    with pytest.raises(UnsupportedValueError, match="valid Unicode"):
        serialize_snapshot("\ud800")


@pytest.mark.parametrize(
    "snapshot",
    [
        FrozenList(cast(Any, [])),
        FrozenDict(cast(Any, [])),
        FrozenDict(cast(Any, (("key", 1, 2),))),
        FrozenDict((("key", 1), ("key", 2))),
        FrozenSet("invalid", ()),
        FrozenSet("set", cast(Any, [])),
        FrozenSet("set", (1, 1)),
        FrozenRecord("Record", cast(Any, [])),
        FrozenRecord("Record", cast(Any, (("field", 1, 2),))),
        FrozenRecord("Record", (("field", 1), ("field", 2))),
        FrozenGraph((FrozenSet("frozenset", ()),), FrozenRef(0)),
        FrozenGraph((FrozenList(()), FrozenList(())), FrozenRef(0)),
        object(),
    ],
)
def test_snapshot_validation_rejects_malformed_wrapper_shapes(snapshot: object) -> None:
    with pytest.raises(UnsupportedValueError):
        serialize_snapshot(cast(Any, snapshot))


def test_snapshot_validation_rejects_noncanonical_dict_and_set_order() -> None:
    first, second = _invalid_order((1, 2))
    with pytest.raises(UnsupportedValueError, match="keys are not in canonical order"):
        serialize_snapshot(FrozenDict(((first, "first"), (second, "second"))))
    with pytest.raises(UnsupportedValueError, match="members are not in canonical order"):
        serialize_snapshot(FrozenSet("set", (first, second)))


@pytest.mark.parametrize(
    ("left", "right"),
    (
        (1, 1.0),
        (True, 1),
        (0.0, -0.0),
        ((1,), (1.0,)),
        (FrozenSet("frozenset", (1,)), FrozenSet("frozenset", (1.0,))),
    ),
)
def test_snapshot_validation_rejects_builtin_keys_and_members_that_collide_after_thaw(
    left: object, right: object
) -> None:
    ordered = _canonical_order((left, right))
    dictionary = FrozenDict(tuple((key, str(index)) for index, key in enumerate(ordered)))

    with pytest.raises(UnsupportedValueError, match="collide after thaw"):
        serialize_snapshot(dictionary)
    with pytest.raises(UnsupportedValueError, match="collide after thaw"):
        freeze(dictionary)
    for kind in ("set", "frozenset"):
        with pytest.raises(UnsupportedValueError, match="collide after thaw"):
            serialize_snapshot(FrozenSet(kind, ordered))


def test_thaw_rejects_adapter_keys_and_members_that_collapse_cardinality() -> None:
    class CollidingAdapter:
        def freeze(self, value: _AdaptedValue, freeze_value: Any) -> object:
            return freeze_value(value.value)

        def thaw(self, snapshot: Any, thaw_value: Any) -> int:
            thaw_value(snapshot)
            return 1

    adapter_key = _adapter_key(_AdaptedValue)
    keys = _canonical_order(
        (FrozenAdapterValue(adapter_key, 1), FrozenAdapterValue(adapter_key, 2))
    )
    adapters = {_AdaptedValue: CollidingAdapter()}

    dictionary = FrozenDict(tuple((key, index) for index, key in enumerate(keys)))
    with pytest.raises(UnsupportedValueError, match="keys collide after thaw"):
        thaw(dictionary, adapters=adapters)
    for kind in ("set", "frozenset"):
        with pytest.raises(UnsupportedValueError, match="members collide after thaw"):
            thaw(FrozenSet(kind, keys), adapters=adapters)


def test_snapshot_validation_rejects_keys_that_become_unhashable() -> None:
    unhashable_key = FrozenDict((("nested", 1),))
    snapshot = FrozenDict(((unhashable_key, "value"),))

    with pytest.raises(UnsupportedValueError, match="remain hashable after thaw"):
        serialize_snapshot(snapshot)


def test_snapshot_reference_collection_stops_at_nested_graph_boundaries() -> None:
    nested_graph = FrozenGraph((FrozenList((FrozenRef(0),)),), FrozenRef(0))
    snapshot = FrozenList(
        (
            FrozenRef(1),
            FrozenDict(((FrozenRef(2), FrozenRef(3)),)),
            FrozenSet("set", (FrozenRef(4),)),
            FrozenRecord("Record", (("field", FrozenRef(5)),)),
            FrozenAdapterValue("adapter", FrozenRef(6)),
            (FrozenRef(7),),
            nested_graph,
        )
    )
    assert set(_snapshot_refs(snapshot)) == set(range(1, 8))


@pytest.mark.parametrize(
    "payload",
    [
        b"K2;X;",
        b"K2;N:",
        b"K2;s1x",
        b"K2;t1x",
        b"K2;i1:a;",
    ],
)
def test_deserialize_rejects_malformed_tags_delimiters_and_lengths(payload: bytes) -> None:
    with pytest.raises(UnsupportedValueError):
        deserialize_snapshot(payload)


def test_snapshot_equality_helper_returns_a_real_bool() -> None:
    assert snapshots_equal(FrozenList((1,)), FrozenList((1,))) is True
    assert snapshots_equal(FrozenList((1,)), FrozenList((2,))) is False


def test_snapshot_equality_helper_does_not_short_circuit_same_nan_wrapper() -> None:
    snapshot = FrozenList((float("nan"),))

    assert snapshots_equal(snapshot, snapshot) is False


def test_snapshot_hashability_analysis_covers_refs_and_nested_shapes() -> None:
    assert _snapshot_thaws_hashably(None, [], set())
    assert _snapshot_thaws_hashably((1, FrozenSet("frozenset", (2,))), [], set())
    assert not _snapshot_thaws_hashably((1, FrozenList(())), [], set())
    assert _snapshot_thaws_hashably(FrozenSet("frozenset", (1,)), [], set())
    assert not _snapshot_thaws_hashably(FrozenSet("set", (1,)), [], set())
    assert _snapshot_thaws_hashably(FrozenAdapterValue("adapter", 1), [], set())
    graph = FrozenGraph((FrozenList((FrozenRef(0),)),), FrozenRef(0))
    assert not _snapshot_thaws_hashably(FrozenAdapterValue("adapter", graph), [], set())

    nodes: list[object] = [(1,), None]
    assert _snapshot_thaws_hashably(FrozenRef(0), nodes, set())
    assert not _snapshot_thaws_hashably(FrozenRef(0), nodes, {0})
    assert not _snapshot_thaws_hashably(FrozenRef(-1), nodes, set())
    assert not _snapshot_thaws_hashably(FrozenRef(2), nodes, set())
    assert not _snapshot_thaws_hashably(FrozenRef(1), nodes, set())


@pytest.mark.parametrize(
    "snapshot",
    [
        FrozenGraph((FrozenList((FrozenRef(0),)),), FrozenRef(0)),
        FrozenList((FrozenGraph((FrozenList((FrozenRef(0),)),), FrozenRef(0)),)),
        FrozenDict((("key", FrozenGraph((FrozenList((FrozenRef(0),)),), FrozenRef(0))),)),
        FrozenSet("set", (FrozenGraph((FrozenList((FrozenRef(0),)),), FrozenRef(0)),)),
        FrozenRecord(
            "Record", (("field", FrozenGraph((FrozenList((FrozenRef(0),)),), FrozenRef(0))),)
        ),
        FrozenAdapterValue("adapter", FrozenGraph((FrozenList((FrozenRef(0),)),), FrozenRef(0))),
        (FrozenGraph((FrozenList((FrozenRef(0),)),), FrozenRef(0)),),
    ],
)
def test_snapshot_contains_graph_finds_graphs_in_every_container(snapshot: object) -> None:
    assert _snapshot_contains_graph(snapshot)
    assert not _snapshot_contains_graph(1)


def test_active_guard_rejects_reentrant_nonmemoized_values_and_cleans_up() -> None:
    value = (1, 2)
    state = _FreezeState()
    with (
        _active_guard(value, state),
        pytest.raises(UnsupportedValueError, match="Cyclic values"),
        _active_guard(value, state),
    ):
        pass
    assert state.active_ids == set()


def test_registry_coercion_preserves_an_existing_registry() -> None:
    registry = _AdapterRegistry()
    assert _coerce_registry(registry) is registry


def test_graph_canonicalization_rewrites_all_nested_snapshot_kinds() -> None:
    nested_graph = FrozenGraph((FrozenList((FrozenRef(0),)),), FrozenRef(0))
    graph = FrozenGraph(
        nodes=(
            FrozenList(
                (
                    FrozenRef(1),
                    FrozenRef(2),
                    FrozenRef(3),
                    FrozenAdapterValue("adapter", (nested_graph,)),
                )
            ),
            FrozenDict((("key", FrozenRef(3)),)),
            FrozenSet("set", (3,)),
            FrozenRecord("Record", (("field", FrozenRef(0)),)),
        ),
        root=FrozenRef(0),
    )
    canonical = _canonicalize_graph(graph)
    assert canonical.root == FrozenRef(0)
    assert len(canonical.nodes) == 4
