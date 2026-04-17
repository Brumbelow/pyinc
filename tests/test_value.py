from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from pyinc import (
    Database,
    FrozenAdapterValue,
    Input,
    MutationError,
    UnsupportedValueError,
    ValueAdapter,
    freeze,
    query,
    thaw,
)
from pyinc.value import (
    FrozenDict,
    FrozenList,
    FrozenRecord,
    FrozenSet,
    _AdapterRegistry,
    assert_not_mutated,
    fingerprint,
    fingerprint_snapshot,
    semantic_equal,
)


@dataclass(frozen=True)
class Point:
    x: int
    y: int


class PointAdapter(ValueAdapter):
    def freeze(self, value: Point, freeze: Any) -> object:
        assert callable(freeze)
        return {"x": value.x, "y": value.y}

    def thaw(self, snapshot: Any, thaw: Any) -> Point:
        assert callable(thaw)
        data = cast(dict[str, Any], snapshot)
        return Point(
            x=thaw(data["x"]),
            y=thaw(data["y"]),
        )


@dataclass(frozen=True)
class Key:
    name: str

    def __repr__(self) -> str:
        raise RuntimeError("repr should not be used during freeze ordering")


class KeyAdapter(ValueAdapter):
    def freeze(self, value: Key, freeze: Any) -> object:
        assert callable(freeze)
        return {"name": value.name}

    def thaw(self, snapshot: Any, thaw: Any) -> Key:
        assert callable(thaw)
        data = cast(dict[str, Any], snapshot)
        return Key(name=thaw(data["name"]))


def test_freeze_rejects_cyclic_values() -> None:
    payload: list[object] = []
    payload.append(payload)

    with pytest.raises(UnsupportedValueError, match="Cyclic values"):
        freeze(payload)


def test_freeze_uses_canonical_sorting_without_repr() -> None:
    adapters = {Key: KeyAdapter()}
    snapshot = freeze({Key("b"): 1, Key("a"): 2}, adapters=adapters)
    round_trip = thaw(snapshot, adapters=adapters)
    assert round_trip == {Key("a"): 2, Key("b"): 1}


def test_adapter_round_trip_works_for_freeze_and_thaw() -> None:
    adapters = {Point: PointAdapter()}
    snapshot = freeze(Point(1, 2), adapters=adapters)

    assert isinstance(snapshot, FrozenAdapterValue)
    assert thaw(snapshot, adapters=adapters) == Point(1, 2)


def test_database_uses_adapters_for_boundary_values() -> None:
    adapters = {Point: PointAdapter()}
    payload = Input[Point]("payload")

    @query
    def total(db: Database) -> int:
        point = payload.read(db)
        assert isinstance(point, Point)
        return point.x + point.y

    db = Database(mode="checked", adapters=adapters)
    db.set(payload, Point(2, 3))
    assert db.get(total) == 5
    key, _ = db._query_key(total, (), {})
    assert db._records[key].last_decision == "executed"

    db.set(payload, Point(2, 3))
    assert db.get(total) == 5
    assert db._records[key].last_decision == "reused"


# ---------------------------------------------------------------------------
# Group A: Freeze/thaw round-trips
# ---------------------------------------------------------------------------


def test_freeze_thaw_round_trip_list() -> None:
    value = [1, "a", True, None]
    frozen = freeze(value)
    assert isinstance(frozen, FrozenList)
    assert thaw(frozen) == value


def test_freeze_thaw_round_trip_dict() -> None:
    value = {"b": 2, "a": 1}
    frozen = freeze(value)
    assert isinstance(frozen, FrozenDict)
    thawed = thaw(frozen)
    assert thawed == value


def test_freeze_thaw_round_trip_set() -> None:
    value = {3, 1, 2}
    frozen = freeze(value)
    assert isinstance(frozen, FrozenSet)
    assert frozen.kind == "set"
    assert thaw(frozen) == value


def test_freeze_thaw_round_trip_frozenset() -> None:
    value = frozenset([3, 1, 2])
    frozen = freeze(value)
    assert isinstance(frozen, FrozenSet)
    assert frozen.kind == "frozenset"
    assert thaw(frozen) == value


def test_freeze_thaw_round_trip_tuple() -> None:
    value = (1, [2, 3], {"k": "v"})
    frozen = freeze(value)
    assert isinstance(frozen, tuple)
    thawed = thaw(frozen)
    assert thawed == value


def test_freeze_thaw_round_trip_nested_combinations() -> None:
    value: dict[str, Any] = {"items": [1, {"nested": True}, (3, 4)], "meta": None}
    thawed = thaw(freeze(value))
    assert thawed == value


def test_freeze_thaw_round_trip_dataclass() -> None:
    @dataclass(frozen=True)
    class Config:
        name: str
        values: tuple[int, ...]

    frozen = freeze(Config("x", (1, 2)))
    assert isinstance(frozen, FrozenRecord)
    assert "Config" in frozen.type_name
    # Dataclasses thaw to dicts.
    thawed = thaw(frozen)
    assert thawed == {"name": "x", "values": (1, 2)}


# ---------------------------------------------------------------------------
# Group B: Container protocol operations
# ---------------------------------------------------------------------------


def test_frozen_list_indexing_iteration_len() -> None:
    fl = FrozenList(items=(10, 20, 30))
    assert fl[0] == 10
    assert fl[2] == 30
    assert len(fl) == 3
    assert list(fl) == [10, 20, 30]


def test_frozen_dict_lookup_iteration_keyerror() -> None:
    fd = FrozenDict(entries=(("a", 1), ("b", 2)))
    assert fd["a"] == 1
    assert fd["b"] == 2
    assert list(fd) == ["a", "b"]
    assert len(fd) == 2
    with pytest.raises(KeyError):
        fd["missing"]


def test_frozen_set_contains_iteration_len() -> None:
    fs = FrozenSet(kind="set", items=(1, 2, 3))
    assert 1 in fs
    assert 4 not in fs
    assert len(fs) == 3
    assert set(fs) == {1, 2, 3}


def test_frozen_record_lookup_iteration_keyerror() -> None:
    fr = FrozenRecord(type_name="Cfg", entries=(("x", 1), ("y", 2)))
    assert fr["x"] == 1
    assert fr["y"] == 2
    assert list(fr) == ["x", "y"]
    assert len(fr) == 2
    with pytest.raises(KeyError):
        fr["z"]


# ---------------------------------------------------------------------------
# Group C: Edge cases
# ---------------------------------------------------------------------------


def test_freeze_empty_containers() -> None:
    assert freeze([]) == FrozenList(items=())
    assert freeze({}) == FrozenDict(entries=())
    assert freeze(set()) == FrozenSet(kind="set", items=())
    assert freeze(()) == ()

    # Round-trip empties.
    assert thaw(freeze([])) == []
    assert thaw(freeze({})) == {}
    assert thaw(freeze(set())) == set()
    assert thaw(freeze(())) == ()


def test_freeze_single_element_containers() -> None:
    assert isinstance(freeze([42]), FrozenList)
    assert thaw(freeze([42])) == [42]

    assert isinstance(freeze({"k": "v"}), FrozenDict)
    assert thaw(freeze({"k": "v"})) == {"k": "v"}

    assert isinstance(freeze({99}), FrozenSet)
    assert thaw(freeze({99})) == {99}


def test_freeze_deeply_nested_five_levels() -> None:
    value: list[Any] = [{"a": [{"b": (1, [2, 3])}]}]
    thawed = thaw(freeze(value))
    assert thawed == value


def test_freeze_already_frozen_values_pass_through() -> None:
    fl = FrozenList(items=(1, 2))
    fd = FrozenDict(entries=(("a", 1),))
    fs = FrozenSet(kind="set", items=(1,))
    fr = FrozenRecord(type_name="R", entries=(("x", 1),))
    fa = FrozenAdapterValue(adapter_key="test:T", payload="data")

    # Already-frozen values hit the early return — identity preserved.
    assert freeze(fl) is fl
    assert freeze(fd) is fd
    assert freeze(fs) is fs
    assert freeze(fr) is fr
    assert freeze(fa) is fa


# ---------------------------------------------------------------------------
# Group D: Type conversion and rejection
# ---------------------------------------------------------------------------


def test_freeze_pathlike_converts_to_str() -> None:
    result = freeze(Path("/tmp/test.txt"))
    assert isinstance(result, str)
    assert result == "/tmp/test.txt"


def test_freeze_range_converts_to_tuple() -> None:
    result = freeze(range(1, 10, 2))
    assert result == ("range", 1, 10, 2)


def test_freeze_rejects_iterator_and_generator() -> None:
    with pytest.raises(UnsupportedValueError, match="Iterators and generators"):
        freeze(iter([1, 2]))

    def gen() -> Any:
        yield 1

    with pytest.raises(UnsupportedValueError, match="Iterators and generators"):
        freeze(gen())


def test_freeze_rejects_unsupported_types() -> None:
    with pytest.raises(UnsupportedValueError, match="Unsupported boundary value"):
        freeze(object())

    with pytest.raises(UnsupportedValueError, match="Unsupported boundary value"):
        freeze(lambda: None)


# ---------------------------------------------------------------------------
# Group E: Equality, fingerprint, mutation check
# ---------------------------------------------------------------------------


def test_semantic_equal_same_structure() -> None:
    assert semantic_equal([1, 2], [1, 2])
    assert semantic_equal({"a": 1, "b": [2]}, {"a": 1, "b": [2]})
    assert semantic_equal((1, 2, 3), (1, 2, 3))
    assert not semantic_equal([1, 2], [1, 3])


def test_semantic_equal_different_container_types() -> None:
    # list freezes to FrozenList, tuple stays tuple — not equal.
    assert not semantic_equal([1, 2], (1, 2))
    # set vs frozenset — different kind field.
    assert not semantic_equal({1, 2}, frozenset({1, 2}))


def test_fingerprint_determinism() -> None:
    # Same value → same digest.
    assert fingerprint({"a": [1, 2], "b": 3}) == fingerprint({"a": [1, 2], "b": 3})
    # Dict ordering doesn't matter (freeze sorts entries).
    assert fingerprint({"b": 3, "a": [1, 2]}) == fingerprint({"a": [1, 2], "b": 3})
    # Different values → different digests.
    assert fingerprint([1, 2]) != fingerprint([1, 3])


CANONICAL_FINGERPRINTS: dict[str, tuple[Any, str]] = {
    "none": (None, "28859e345f55ce45426724100617830af221bdf3e01f4333ee34ba58d2df4429"),
    "true": (True, "2b37f1bd8ab8bcc2250382d39155fa1b139c784a9a0e475c63ed1439951bd4bd"),
    "false": (False, "3b6dac53d9671a502027e128bb59ad0d4ab2d3ecf950f82c82374dadc6001f25"),
    "int-zero": (0, "fd1b3408926f09f933b81c68493c2a2a180f5dcabedc5c9c81da0f89e6a5cb29"),
    "int-one": (1, "abca15a9056c7cb1e0aadc77e02e1507845304b010bee10bd5b83ad66db4f210"),
    "int-neg-one": (-1, "58187bf57d9c055e9674352317ab812235d3c5f9dc18a5d8d9c51e7a52345790"),
    "float-1.5": (1.5, "9900351fd014bde471ca0191fa198cadba1ed1b7e360f61939cb2e6a8f914a28"),
    "str-empty": ("", "4a44ca33cb2ab92e0983a99b93ed982e7681f376253b3d3574c2a76b9e433c83"),
    "str-hello": ("hello", "660503a64caf47b9ee37c4dad85a47c4e90ff958c115dcc0f7b7b51622f740e7"),
    "bytes-empty": (b"", "14c3b16893b53fc99a2f05056511e16d23254bf6c01ce52b5dc9cbc8bb6acb5a"),
    "bytes-deadbeef": (b"\xde\xad\xbe\xef", "15fba55305f5c7181328b3ec23196790181eb93c35e9e323065b25896713620d"),
    "tuple-1-2": ((1, 2), "99725380f4eff3c51ec4d11882adb8e6d12fe2c6763f4ed0a72c0516e70f4c9e"),
    "frozenlist-1-2": (FrozenList((1, 2)), "9dccbf0605900933a327ce55f18c69592cd0b74ce652a47f7268e8b727820201"),
    "frozendict-a1-b2": (FrozenDict((("a", 1), ("b", 2))), "95bbdb13cbbf32404273f0cb62c337910aab7e467babe5ae7d954db5ac557a17"),
    "frozenset-set-1-2": (FrozenSet("set", (1, 2)), "6dadb07a5bd7e26b0c36e060b073c297d6c959b99c5163ae1a123fcc3f644398"),
    "frozenset-frozenset-1-2": (FrozenSet("frozenset", (1, 2)), "02591c74d3248987533119c463a27065fe166f59f1b63b91330883079e33c621"),
    "frozenrecord-point-x1-y2": (FrozenRecord("Point", (("x", 1), ("y", 2))), "c248ff9e7917f1d6ef963123e2d12761bc5ddfd457e2ffb2e00633c284ae435e"),
    "frozenadapter-mod-pt-1": (FrozenAdapterValue("mod:Pt", 1), "dc304df151d02c2a33476bafbdda649e22c4592877e3893614152704b2d1b47a"),
    "complex-1+2j": (complex(1, 2), "7db3a1d42f7e302819d565050789c5f34ebb613d399a753252165227ba6236eb"),
}


def test_fingerprint_snapshot_pins_exact_bytes_for_canonical_values() -> None:
    for label, (value, expected_digest) in CANONICAL_FINGERPRINTS.items():
        actual = fingerprint_snapshot(value)
        assert actual == expected_digest, (
            f"fingerprint_snapshot drift for {label!r}: got {actual}, expected {expected_digest}"
        )


def test_fingerprint_snapshot_distinguishes_shapes() -> None:
    # bool vs int
    assert fingerprint_snapshot(False) != fingerprint_snapshot(0)
    assert fingerprint_snapshot(True) != fingerprint_snapshot(1)
    # FrozenList vs raw tuple
    assert fingerprint_snapshot(FrozenList((1, 2))) != fingerprint_snapshot((1, 2))
    # FrozenSet "set" vs "frozenset"
    assert fingerprint_snapshot(FrozenSet("set", (1, 2))) != fingerprint_snapshot(FrozenSet("frozenset", (1, 2)))
    # range-as-tuple vs raw tuple with same head
    assert fingerprint_snapshot(("range", 1, 10, 2)) != fingerprint_snapshot((1, 10, 2))
    # FrozenRecord differing type_name
    assert fingerprint_snapshot(FrozenRecord("Point", (("x", 1),))) != fingerprint_snapshot(
        FrozenRecord("Other", (("x", 1),))
    )
    # FrozenAdapterValue differing adapter_key
    assert fingerprint_snapshot(FrozenAdapterValue("a", 1)) != fingerprint_snapshot(FrozenAdapterValue("b", 1))
    # string length-prefix must defeat concatenation collisions
    assert fingerprint_snapshot(("ab", "cd")) != fingerprint_snapshot(("a", "bcd"))
    # bytes vs str with matching ASCII content
    assert fingerprint_snapshot("ab") != fingerprint_snapshot(b"ab")


def test_fingerprint_snapshot_rejects_non_snapshot_values() -> None:
    class NotASnapshot:
        pass

    with pytest.raises(TypeError, match="unsupported snapshot value"):
        fingerprint_snapshot(NotASnapshot())


def test_fingerprint_snapshot_equivalence_under_freeze_normalization() -> None:
    # freeze() sorts dict entries; two insertion-order dicts yield the same snapshot.
    left = freeze({"b": 1, "a": 2})
    right = freeze({"a": 2, "b": 1})
    assert fingerprint_snapshot(left) == fingerprint_snapshot(right)

    # Sets freeze by canonical sort — ordering of input items does not affect digest.
    assert fingerprint_snapshot(freeze({1, 2, 3})) == fingerprint_snapshot(freeze({3, 2, 1}))


def test_assert_not_mutated_matching_passes_different_raises() -> None:
    assert_not_mutated("abc123", "abc123")  # No exception.
    with pytest.raises(MutationError):
        assert_not_mutated("abc123", "xyz789")


# ---------------------------------------------------------------------------
# Group F: Adapter registry
# ---------------------------------------------------------------------------


def test_adapter_registry_mro_resolution() -> None:
    class Base:
        pass

    class Child(Base):
        pass

    class BaseAdapter:
        def freeze(self, value: Any, freeze_fn: Any) -> Any:
            return "frozen"

        def thaw(self, snapshot: Any, thaw_fn: Any) -> Any:
            return Base()

    registry = _AdapterRegistry({Base: BaseAdapter()})

    # Child resolves to Base adapter via MRO.
    result = registry.for_value(Child())
    assert result is not None
    assert result[1] is not None

    # Base itself resolves.
    result = registry.for_value(Base())
    assert result is not None


def test_thaw_without_matching_adapter_raises() -> None:
    frozen = FrozenAdapterValue(adapter_key="nonexistent:Type", payload="data")
    with pytest.raises(UnsupportedValueError, match="Cannot thaw adapted snapshot"):
        thaw(frozen)


def test_adapter_registry_for_value_returns_none_for_unknown_type() -> None:
    registry = _AdapterRegistry({Point: PointAdapter()})
    assert registry.for_value("a string") is None
    assert registry.for_value(42) is None
