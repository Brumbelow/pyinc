from __future__ import annotations

import os
import random
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from pyinc import (
    Database,
    FrozenAdapterValue,
    FrozenGraph,
    FrozenRef,
    Input,
    MutationError,
    UnsupportedValueError,
    ValueAdapter,
    deserialize_snapshot,
    freeze,
    query,
    serialize_snapshot,
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
    snapshots_equal,
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


def test_freeze_handles_self_referential_list_via_frozen_graph() -> None:
    payload: list[object] = []
    payload.append(payload)

    snapshot = freeze(payload)
    assert isinstance(snapshot, FrozenGraph)
    assert len(snapshot.nodes) == 1
    assert snapshot.root == FrozenRef(0)
    assert snapshot.nodes[0] == FrozenList(items=(FrozenRef(0),))

    thawed = thaw(snapshot)
    assert isinstance(thawed, list)
    assert thawed[0] is thawed


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


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_database_uses_adapters_for_boundary_values(mode: str) -> None:
    adapters = {Point: PointAdapter()}
    payload = Input[Point]("payload")

    @query
    def total(db: Database) -> int:
        point = payload.read(db)
        assert isinstance(point, Point)
        return point.x + point.y

    db = Database(mode=mode, adapters=adapters)
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


def test_frozen_mapping_order_is_canonical_and_stable() -> None:
    """Pin the canonical entry order literally, not by recomputing the digest.

    The order is documented as fixed for the byte grammar's lifetime, so these
    sequences are the contract rather than an observation: a failure here means
    the order moved, which invalidates every stored record and checkpoint.
    Neither insertion nor sorted order is what the rule produces, and both
    refutations are asserted so a change to either could not pass unnoticed.
    """

    frozen = cast(FrozenDict, freeze({"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}))
    canonical = ["two", "three", "one", "four", "five"]
    assert [key for key, _ in frozen.entries] == canonical
    assert list(thaw(frozen)) == canonical

    # Not insertion order.
    assert list(thaw(freeze({"b": 1, "a": 2}))) == ["a", "b"]

    # Not sorted order either.
    assert list(thaw(freeze({letter: index for index, letter in enumerate("abcdefgh")}))) == [
        "a",
        "h",
        "e",
        "d",
        "c",
        "f",
        "g",
        "b",
    ]


def test_frozen_set_member_order_is_canonical_and_stable() -> None:
    """Pin the canonical member order literally, on the snapshot that holds it.

    Sets order members by the same digest rule mappings order keys by, so a set
    of the mapping pin's five keys stores the identical sequence -- asserted here
    so the two sides of one rule cannot drift apart on one of them only. A
    failure means the order moved: STOP rather than re-pin, as above.

    The thawed value deliberately carries no order assertion. `thaw` rebuilds an
    ordinary `set`, whose iteration order is Python's and varies between
    processes, so there is no sequence there to pin -- the snapshot and
    `strict`'s view of it are the only places this order exists.
    """

    words = ("one", "two", "three", "four", "five")
    canonical = ("two", "three", "one", "four", "five")

    frozen = cast(FrozenSet, freeze(set(words)))
    assert frozen.kind == "set"
    assert frozen.items == canonical

    frozen_immutable = cast(FrozenSet, freeze(frozenset(words)))
    assert frozen_immutable.kind == "frozenset"
    assert frozen_immutable.items == canonical

    # The same rule, on the same five strings, through the mapping path.
    mapping = cast(FrozenDict, freeze(dict.fromkeys(words, 1)))
    assert tuple(key for key, _ in mapping.entries) == canonical

    # Content survives thaw; order is not part of what a set carries.
    assert thaw(frozen) == set(words)


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


def test_freeze_already_frozen_values_are_detached_clones() -> None:
    fl = FrozenList(items=(1, 2))
    fd = FrozenDict(entries=(("a", 1),))
    fs = FrozenSet(kind="set", items=(1,))
    fr = FrozenRecord(type_name="R", entries=(("x", 1),))
    fa = FrozenAdapterValue(adapter_key="test:T", payload="data")
    fref = FrozenRef(index=0)
    fg = FrozenGraph(nodes=(FrozenList(items=(FrozenRef(0),)),), root=FrozenRef(0))

    # Frozen* shells are frozen dataclasses whose fields object.__setattr__ can
    # rebind, so a stored snapshot must never share a shell with the caller:
    # freeze hands back an equal, identically fingerprinted clone.
    for wrapper in (fl, fd, fs, fr, fa, fg):
        clone = freeze(wrapper)
        assert clone is not wrapper
        assert clone == wrapper
        assert fingerprint_snapshot(clone) == fingerprint_snapshot(wrapper)
    with pytest.raises(UnsupportedValueError, match="FrozenRef index"):
        freeze(fref)


def test_freeze_clones_reach_every_nested_shell() -> None:
    inner = FrozenList((1,))
    outer = FrozenDict((("k", FrozenRecord("R", (("x", inner),))),))
    clone = cast(FrozenDict, freeze(outer))

    cloned_record = clone.entries[0][1]
    assert cloned_record is not outer.entries[0][1]
    assert cloned_record["x"] is not inner
    assert cloned_record["x"] == inner


def test_freeze_clones_graph_envelopes_and_their_node_tables() -> None:
    shared = [1]
    fg = cast(FrozenGraph, freeze((shared, shared)))
    clone = cast(FrozenGraph, freeze(fg))

    assert clone is not fg
    assert clone == fg
    assert fingerprint_snapshot(clone) == fingerprint_snapshot(fg)
    assert all(
        node is not original
        for node, original in zip(clone.nodes, fg.nodes, strict=True)
    )


def test_freeze_rejects_hand_built_wrapper_cycles_without_recursing() -> None:
    # The cycle spine must be a kind="frozenset" FrozenSet: it is the one
    # wrapper shape _wrapper_aliases_structure never descends (its walk covers
    # only the four graph-capable types, FrozenAdapterValue, and tuples), so
    # this cycle reaches _freeze's pass-through branch and, since freeze
    # detaches pre-frozen wrappers, _detach_wrapper's own guard. Do NOT
    # "simplify" the spine to a FrozenList: that shape is intercepted by the
    # aliasing detection at value.py:266 and re-routed through
    # _refreeze_wrapper, whose _active_guard raises a DIFFERENT message
    # ("Cyclic values cannot cross cached boundaries through this container
    # type.", value.py:1597-1599) and never reaches the detach.
    shell = FrozenSet("frozenset", ())
    holder = FrozenAdapterValue("test:T", shell)
    object.__setattr__(shell, "items", (holder,))
    with pytest.raises(UnsupportedValueError, match="object cycles"):
        freeze(holder)


def test_freeze_detach_shares_leaf_tuples_and_clones_tuples_holding_shells() -> None:
    leaf_tuple = (1, "a")
    mixed_tuple = (1, FrozenList((2,)))
    clone = cast(FrozenList, freeze(FrozenList((leaf_tuple, mixed_tuple))))

    # An all-leaf tuple is shared: it is immutable and holds no rebindable
    # shell, so the detach returns it unchanged rather than reallocating.
    assert clone.items[0] is leaf_tuple
    # A tuple holding a shell must be rebuilt, or the shell stays aliased.
    assert clone.items[1] is not mixed_tuple
    assert clone.items[1] == mixed_tuple
    assert clone.items[1][1] is not mixed_tuple[1]


def test_freeze_rejects_malformed_wrapper_shells_with_the_kernel_error() -> None:
    # _detach_wrapper reads shell fields directly (unpacking entry pairs,
    # iterating items), so the input grammar has to be checked before the
    # clone walk. Otherwise a malformed shell either escapes as a raw
    # ValueError -- which none of runtime.py's UnsupportedValueError boundary
    # handlers catch -- or gets silently normalized into a well-formed
    # snapshot, which would let an invalid wrapper enter the store.
    cases: list[tuple[Any, str]] = [
        (
            FrozenDict(entries=cast(Any, (("a", 1, 2),))),
            "FrozenDict entries must be key/value pairs.",
        ),
        (
            FrozenRecord("R", cast(Any, (("x",),))),
            "FrozenRecord entries must be field/value pairs.",
        ),
        (FrozenList(items=cast(Any, [1, 2])), "FrozenList.items must be a tuple."),
        (FrozenList(items=cast(Any, "ab")), "FrozenList.items must be a tuple."),
        (FrozenSet(kind="set", items=cast(Any, [1])), "FrozenSet.items must be a tuple."),
        (FrozenDict(entries=cast(Any, [("a", 1)])), "FrozenDict.entries must be a tuple."),
        (FrozenRecord("R", cast(Any, [("x", 1)])), "FrozenRecord.entries must be a tuple."),
    ]
    for wrapper, message in cases:
        with pytest.raises(UnsupportedValueError, match=re.escape(message)):
            freeze(wrapper)


def test_freeze_rejects_malformed_wrapper_shells_nested_in_raw_containers() -> None:
    # The same guard has to fire for a wrapper reached through a raw spine;
    # inlining used to rebuild these into well-formed snapshots silently.
    with pytest.raises(
        UnsupportedValueError, match=re.escape("FrozenList.items must be a tuple.")
    ):
        freeze([FrozenList(items=cast(Any, [1, 2]))])
    with pytest.raises(
        UnsupportedValueError,
        match=re.escape("FrozenDict entries must be key/value pairs."),
    ):
        freeze({"k": FrozenDict(entries=cast(Any, (("a", 1, 2),)))})


def test_freeze_detaches_hash_positions() -> None:
    # Mapping keys and set members are frozen through their own _freeze_root,
    # so the detach guarantee has to hold on that boundary too.
    member = FrozenSet("frozenset", (1,))

    as_set_member = cast(FrozenSet, freeze({member}))
    assert as_set_member.items[0] is not member
    assert as_set_member.items[0] == member

    as_dict_key = cast(FrozenDict, freeze({member: "v"}))
    stored_key = as_dict_key.entries[0][0]
    assert stored_key is not member
    assert stored_key == member


def test_freeze_detaches_graph_ref_cells() -> None:
    # A FrozenRef is a frozen dataclass like every other shell, so handing one
    # back by identity leaves the caller holding a live index into the stored
    # node table -- rebindable long after the snapshot was validated.
    ref = FrozenRef(0)
    graph = FrozenGraph(nodes=(FrozenList((ref,)),), root=ref)
    stored = cast(FrozenGraph, freeze(graph))

    assert stored.root is not ref
    assert cast(FrozenList, stored.nodes[0]).items[0] is not ref
    before = fingerprint_snapshot(stored)
    assert before == fingerprint_snapshot(graph)

    object.__setattr__(ref, "index", 7)
    assert fingerprint_snapshot(stored) == before


# ---------------------------------------------------------------------------
# Group D: Type conversion and rejection
# ---------------------------------------------------------------------------


def test_freeze_pathlike_converts_to_str() -> None:
    path = Path("/tmp/test.txt")
    result = freeze(path)
    assert isinstance(result, str)
    assert result == os.fspath(path)


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
    "none": (None, "bc84a14378e233c608ccc17f177919a6595f9d6331b9a5c5d8ffd0eade02bb87"),
    "true": (True, "d84c15ea4d5e0080d8dbabc5ce68dbd742fbbd0499d4df57ef079c53d8871b45"),
    "false": (
        False,
        "3f05aff300785adb8cdb12bb9993f3b494b7e1de18042e42a5f5e863e49cc5f8",
    ),
    "int-zero": (0, "1f5fe91ed5208d6477e3d13cca2812d9c1c42defb7dab70d784f45f65565b8fd"),
    "int-one": (1, "ec187eb76b8d83f7e32bd0b232f69735d1a65a8a78529b0654411717920e53cf"),
    "int-neg-one": (
        -1,
        "2d47fa0bc94ee71387d24889f9db1d68fb766b5c62d76709386c07ae630b94c6",
    ),
    "float-1.5": (
        1.5,
        "dd96a811716c46538b4197ff3950edf3f49ae9d60768708c746a752dadf10c56",
    ),
    "str-empty": (
        "",
        "482975b5aa2860a276737cba6db568490b691439406fbb367f3b604bb5210ba3",
    ),
    "str-hello": (
        "hello",
        "fc322c5025dd198cb3fb82b6bcd16933f825fbd6991e27c11f52159ce3196aff",
    ),
    "bytes-empty": (
        b"",
        "37591557e2971f9d22a13734870a227c1f09fa2db90410e374d5398b41458998",
    ),
    "bytes-deadbeef": (
        b"\xde\xad\xbe\xef",
        "2e7517169495c8c68748b8a7e4b5d65edda8e3f147186cbcb91f558ac5d1c1c8",
    ),
    "tuple-1-2": (
        (1, 2),
        "5c7369e88f8c1ac8aa9c6d40293d9bca89c7eefc7be95171520a7c26a420e17f",
    ),
    "frozenlist-1-2": (
        FrozenList((1, 2)),
        "cda055499f3951671946049d2351b5fd9cd819ddbadb4cd29196263ab2e3fea0",
    ),
    "frozendict-a1-b2": (
        FrozenDict((("a", 1), ("b", 2))),
        "a2314b9c7615be9706387bd492849f94b88a62c02a6f9d9efd7f65bc5947b4f4",
    ),
    "frozenset-set-1-2": (
        FrozenSet("set", (1, 2)),
        "ee2eaaecaa7809412de0bfa62e9c0911dc43d6ae94f5de8d56241764023b817e",
    ),
    "frozenset-frozenset-1-2": (
        FrozenSet("frozenset", (1, 2)),
        "a4fd14bb2105db0058518e6646536c1ff566c058e2d52da333485a0507c355b2",
    ),
    "frozenrecord-point-x1-y2": (
        FrozenRecord("Point", (("x", 1), ("y", 2))),
        "54d491b759c0fce6fd77da828a7a8b4dc0f2d8954109834a479b3ba5030758ed",
    ),
    "frozenadapter-mod-pt-1": (
        FrozenAdapterValue("mod:Pt", 1),
        "af0c41f8d3007f00c9a7ea5933f30f1b45fd4bef98a5f5d828fae6964a19da16",
    ),
    "complex-1+2j": (
        complex(1, 2),
        "7d5cd6c5ec8d30c14912b51d052e79e2e88752d5d815f43559a7cdc5a09eaa45",
    ),
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
    assert fingerprint_snapshot(FrozenSet("set", (1, 2))) != fingerprint_snapshot(
        FrozenSet("frozenset", (1, 2))
    )
    # range-as-tuple vs raw tuple with same head
    assert fingerprint_snapshot(("range", 1, 10, 2)) != fingerprint_snapshot((1, 10, 2))
    # FrozenRecord differing type_name
    assert fingerprint_snapshot(FrozenRecord("Point", (("x", 1),))) != fingerprint_snapshot(
        FrozenRecord("Other", (("x", 1),))
    )
    # FrozenAdapterValue differing adapter_key
    assert fingerprint_snapshot(FrozenAdapterValue("a", 1)) != fingerprint_snapshot(
        FrozenAdapterValue("b", 1)
    )
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


# ---------------------------------------------------------------------------
# Group G: Mutable graph support — shared identity and cycles via FrozenGraph
# ---------------------------------------------------------------------------


def test_freeze_preserves_shared_identity_in_list() -> None:
    inner = [1, 2, 3]
    payload = [inner, inner]

    snapshot = freeze(payload)
    assert isinstance(snapshot, FrozenGraph)

    thawed = thaw(snapshot)
    assert isinstance(thawed, list)
    assert thawed[0] is thawed[1]
    assert thawed[0] == [1, 2, 3]


def test_freeze_preserves_shared_identity_in_dict_value() -> None:
    shared = {"k": "v"}
    payload = {"a": shared, "b": shared}

    snapshot = freeze(payload)
    assert isinstance(snapshot, FrozenGraph)

    thawed = thaw(snapshot)
    assert isinstance(thawed, dict)
    assert thawed["a"] is thawed["b"]


def test_freeze_handles_mutual_reference_cycle() -> None:
    a: list[Any] = []
    b: list[Any] = []
    a.append(b)
    b.append(a)

    snapshot = freeze(a)
    assert isinstance(snapshot, FrozenGraph)

    thawed = thaw(snapshot)
    assert isinstance(thawed, list)
    # Walk the cycle: a → b → a
    assert thawed[0][0] is thawed


def test_freeze_handles_deep_mutual_graph_round_trips() -> None:
    nodes: list[list[Any]] = [[] for _ in range(10)]
    for i in range(10):
        nodes[i].append(nodes[(i + 1) % 10])

    snapshot = freeze(nodes[0])
    assert isinstance(snapshot, FrozenGraph)

    thawed = thaw(snapshot)
    cur = thawed
    for _ in range(20):
        cur = cur[0]
    # 20 hops on a 10-cycle starting from thawed-nodes[0] returns to thawed-nodes[0].
    assert cur is thawed


def test_freeze_pure_tree_does_not_wrap_in_frozen_graph() -> None:
    # Plain tree — no shared subtrees, no cycles. Existing flat shape preserved
    # so the common case stays zero-overhead.
    snapshot = freeze([1, [2, [3]]])
    assert isinstance(snapshot, FrozenList)


def test_freeze_reencodes_shared_wrapper_structure_as_the_raw_frozen_graph() -> None:
    # A strict-mode boundary view rebuilds a graph snapshot into wrapper
    # objects that genuinely alias each other. Feeding such a view back into
    # freeze must restore the graph encoding the view came from, so the
    # round-trip lands the exact snapshot the raw structure produces.
    inner_raw = [1, 2]
    original = freeze([inner_raw, inner_raw])
    assert isinstance(original, FrozenGraph)

    inner = FrozenList((1, 2))
    shared = FrozenList((inner, inner))
    snapshot = freeze(shared)

    assert isinstance(snapshot, FrozenGraph)
    assert snapshot == original
    assert fingerprint_snapshot(snapshot) == fingerprint_snapshot(original)


def test_freeze_reencodes_wrappers_shared_through_a_tuple_spine() -> None:
    # Sibling wrappers whose lowest common ancestor is a raw tuple alias each
    # other exactly as they would under a list spine, and must land the same
    # graph encoding: a tuple carries no memo slot of its own to notice it.
    inner_raw = [1]
    original = freeze((inner_raw, inner_raw))
    assert isinstance(original, FrozenGraph)

    inner = FrozenList((1,))
    snapshot = freeze((inner, inner))

    assert isinstance(snapshot, FrozenGraph)
    assert snapshot == original
    assert fingerprint_snapshot(snapshot) == fingerprint_snapshot(original)


def test_freeze_reencodes_wrappers_shared_through_nested_tuple_spines() -> None:
    inner_raw = [1]
    original = freeze(((inner_raw,), ("tag", (inner_raw,))))

    inner = FrozenList((1,))
    snapshot = freeze(((inner,), ("tag", (inner,))))

    assert isinstance(snapshot, FrozenGraph)
    assert snapshot == original
    assert fingerprint_snapshot(snapshot) == fingerprint_snapshot(original)


def test_freeze_reencodes_wrappers_shared_through_mixed_list_and_tuple_spines() -> None:
    inner_raw = [1]
    original = freeze([(inner_raw,), {"key": inner_raw}])

    inner = FrozenList((1,))
    snapshot = freeze([(inner,), {"key": inner}])

    assert isinstance(snapshot, FrozenGraph)
    assert snapshot == original
    assert fingerprint_snapshot(snapshot) == fingerprint_snapshot(original)


def test_freeze_tuple_of_unshared_wrappers_stays_a_plain_tuple() -> None:
    items = (FrozenList((1,)), FrozenList((2,)))
    snapshot = cast(tuple[Any, ...], freeze(items))

    assert type(snapshot) is tuple
    assert snapshot == items
    # The raw tuple spine used to leak its wrapper elements by identity
    # (freeze((w,))[0] is w); ownership now requires clones.
    assert snapshot[0] is not items[0]
    assert snapshot[0] == items[0]

    # Nested spines leaked identically (freeze(((w,),))[0][0] was w), so the
    # detach must recurse through them rather than stopping at the outer tuple.
    nested = cast(tuple[Any, ...], freeze(((items[0],),)))
    assert type(nested) is tuple
    assert nested[0][0] is not items[0]
    assert nested[0][0] == items[0]


def test_freeze_reencodes_cyclic_wrapper_structure_as_the_raw_frozen_graph() -> None:
    raw: list[Any] = []
    raw.append(raw)
    original = freeze(raw)
    assert isinstance(original, FrozenGraph)

    shell = FrozenList(())
    object.__setattr__(shell, "items", (shell,))
    snapshot = freeze(shell)

    assert isinstance(snapshot, FrozenGraph)
    assert snapshot == original
    assert fingerprint_snapshot(snapshot) == fingerprint_snapshot(original)


def test_frozen_graph_digest_is_deterministic_across_construction_orders() -> None:
    a: list[Any] = []
    a.append(a)
    digest_a = fingerprint(a)

    b: list[Any] = []
    b.append(b)
    digest_b = fingerprint(b)

    assert digest_a == digest_b


@pytest.mark.parametrize("shared_factory", [list, set])
def test_frozen_graph_node_numbers_ignore_mapping_insertion_order(
    shared_factory: Any,
) -> None:
    first_left = shared_factory([1])
    second_left = shared_factory([2])
    left: dict[str, Any] = {}
    for key, value in (
        ("a", first_left),
        ("b", second_left),
        ("aa", first_left),
        ("bb", second_left),
    ):
        left[key] = value

    first_right = shared_factory([1])
    second_right = shared_factory([2])
    right: dict[str, Any] = {}
    for key, value in (
        ("bb", second_right),
        ("aa", first_right),
        ("b", second_right),
        ("a", first_right),
    ):
        right[key] = value

    left_snapshot = freeze(left)
    right_snapshot = freeze(right)

    assert left == right
    assert left_snapshot == right_snapshot
    assert fingerprint_snapshot(left_snapshot) == fingerprint_snapshot(right_snapshot)
    thawed = cast(dict[str, Any], thaw(left_snapshot))
    assert thawed["a"] is thawed["aa"]
    assert thawed["b"] is thawed["bb"]
    assert thawed["a"] is not thawed["b"]


def test_frozen_graph_node_numbers_ignore_mapping_order_with_cycles() -> None:
    def cyclic(marker: int) -> list[Any]:
        value: list[Any] = []
        value.extend((value, marker))
        return value

    first_left = cyclic(1)
    second_left = cyclic(2)
    left = {
        "a": first_left,
        "b": second_left,
        "aa": first_left,
        "bb": second_left,
    }

    first_right = cyclic(1)
    second_right = cyclic(2)
    right: dict[str, Any] = {}
    for key, value in (
        ("bb", second_right),
        ("aa", first_right),
        ("b", second_right),
        ("a", first_right),
    ):
        right[key] = value

    left_snapshot = freeze(left)
    right_snapshot = freeze(right)

    assert left_snapshot == right_snapshot
    thawed = cast(dict[str, list[Any]], thaw(left_snapshot))
    assert thawed["a"] is thawed["aa"]
    assert thawed["a"][0] is thawed["a"]
    assert thawed["b"][0] is thawed["b"]


def test_adapted_hash_positions_are_isolated_from_graph_node_order() -> None:
    class AdaptedKey:
        def __init__(self, name: str, payload: Any) -> None:
            self.name = name
            self.payload = payload

        def __hash__(self) -> int:
            return 0

        def __eq__(self, other: object) -> bool:
            return isinstance(other, AdaptedKey) and self.name == other.name

    class AdaptedKeyAdapter:
        def freeze(self, value: AdaptedKey, _freeze_value: Any) -> Any:
            return {"name": value.name, "payload": value.payload}

        def thaw(self, snapshot: Any, thaw_value: Any) -> AdaptedKey:
            data = cast(Any, snapshot)
            return AdaptedKey(
                cast(str, thaw_value(data["name"])),
                thaw_value(data["payload"]),
            )

    adapters = {AdaptedKey: AdaptedKeyAdapter()}
    left: set[AdaptedKey] = set()
    right: set[AdaptedKey] = set()
    for name in ("a", "b", "c"):
        left.add(AdaptedKey(name, [name]))
    for name in ("c", "b", "a"):
        right.add(AdaptedKey(name, [name]))

    assert freeze(left, adapters=adapters) == freeze(right, adapters=adapters)

    # A hash position freezes its value on its own, and a payload that shares
    # or cycles is refused there: the encoding holds such a payload as a node
    # and cannot hand it back to `thaw` whole. The refusal names the payload
    # rather than the position, which is the more specific of the two answers
    # available for the same value.
    shared: list[Any] = []
    shared_payload = [shared, shared]
    with pytest.raises(UnsupportedValueError, match="cannot hand back whole"):
        freeze({AdaptedKey("shared", shared_payload)}, adapters=adapters)

    cyclic_payload: list[Any] = []
    cyclic_payload.append(cyclic_payload)
    with pytest.raises(UnsupportedValueError, match="cannot hand back whole"):
        freeze({AdaptedKey("cyclic", cyclic_payload)}, adapters=adapters)


def test_strict_view_of_tuple_shared_list_refreezes_to_the_stored_snapshot() -> None:
    @query
    def shared_pair(db: Database) -> object:
        items = [1]
        return (items, items)

    raw = [1]
    expected = freeze((raw, raw))

    db = Database(mode="strict")
    view = cast(Any, db.get(shared_pair))
    assert view[0] is view[1]
    assert freeze(view) == expected
    assert fingerprint(view) == fingerprint_snapshot(expected)


def test_strict_view_of_tuple_shared_list_round_trips_through_set_and_arguments() -> None:
    @query
    def shared_pair(db: Database) -> object:
        items = [1]
        return (items, items)

    @query
    def width(db: Database, payload: object) -> int:
        return len(cast(Any, payload))

    raw = [1]
    db = Database(mode="strict")
    view = db.get(shared_pair)

    # As a query argument: the view keys the very node the raw value keyed.
    db.get(width, (raw, raw))
    executions = db.statistics().query_executions
    assert db.get(width, view) == 2
    assert db.statistics().query_executions == executions

    # Through db.set: re-encoding the view is an equal update, not a change.
    stored = Input[object]("shared-pair")
    db.set(stored, (raw, raw))
    ignores = db.statistics().input_equal_ignores
    db.set(stored, view)
    assert db.statistics().input_equal_ignores == ignores + 1


def test_freeze_of_aliased_wrappers_detaches_every_member_shell() -> None:
    member = FrozenSet("frozenset", (2,))
    shared = FrozenList((member,))
    snapshot = cast(FrozenGraph, freeze((shared, shared)))

    assert isinstance(snapshot, FrozenGraph)
    node = cast(FrozenList, snapshot.nodes[0])
    assert node is not shared
    # The re-encode pass used to hand the frozenset member through by
    # identity; the caller could then rebind its items inside the stored graph.
    assert node.items[0] is not member
    assert node.items[0] == member


def test_freeze_of_aliased_dict_detaches_its_keys() -> None:
    key = FrozenSet("frozenset", (3,))
    shared = FrozenDict(((key, 1),))
    snapshot = cast(FrozenGraph, freeze((shared, shared)))

    node = cast(FrozenDict, snapshot.nodes[0])
    assert node is not shared
    stored_key = node.entries[0][0]
    assert stored_key is not key
    assert stored_key == key


def test_freeze_of_aliased_wrappers_detaches_nested_graph_envelopes() -> None:
    # A nested FrozenGraph is its own reference namespace, so neither the
    # re-encode pass nor the canonical renumbering that follows it descends
    # into one -- both used to carry the caller's envelope straight into the
    # stored snapshot.
    spine = [1]
    inner = cast(FrozenGraph, freeze((spine, spine)))
    shared = FrozenList((inner,))
    snapshot = cast(FrozenGraph, freeze((shared, shared)))

    stored = cast(FrozenList, snapshot.nodes[0]).items[0]
    assert stored is not inner
    assert stored == inner
    assert cast(FrozenGraph, stored).nodes[0] is not inner.nodes[0]


def test_freeze_of_aliased_dict_preserves_canonical_entry_order() -> None:
    raw = {1: "a", 2: "b"}
    shared = cast(FrozenDict, freeze(raw))
    snapshot = cast(FrozenGraph, freeze((shared, shared)))
    baseline = freeze((raw, raw))
    assert fingerprint_snapshot(snapshot) == fingerprint_snapshot(baseline)


def test_freeze_of_aliased_wrappers_rejects_malformed_nested_shells() -> None:
    # The re-encode pass clones through _detach_wrapper as well, and that walk
    # is shape-trusting by design: the guard has to fire in _freeze before the
    # aliasing decision, not somewhere inside the two walks it protects.
    shared = FrozenList((FrozenList(items=cast(Any, [1, 2])),))
    with pytest.raises(
        UnsupportedValueError, match=re.escape("FrozenList.items must be a tuple.")
    ):
        freeze((shared, shared))


# ---------------------------------------------------------------------------
# Group H: Serialize/deserialize and K2 fingerprint prefix
# ---------------------------------------------------------------------------


def test_kernel_fingerprint_prefix_is_k2() -> None:
    # The serialized byte form (which is also the digest input) must carry a
    # version prefix so older durable caches cannot be silently accepted.
    payload = serialize_snapshot(freeze("hello"))
    assert payload.startswith(b"K2;")


def test_serialize_deserialize_round_trip_scalars() -> None:
    for value in (
        None,
        True,
        False,
        0,
        1,
        -1,
        1.5,
        "",
        "hello",
        b"",
        b"\xde\xad",
        complex(1, 2),
    ):
        payload = serialize_snapshot(freeze(value))
        assert deserialize_snapshot(payload) == freeze(value)


def test_serialize_deserialize_round_trip_containers() -> None:
    samples: list[Any] = [
        [1, 2, 3],
        {"b": 2, "a": 1},
        {1, 2, 3},
        frozenset({4, 5}),
        (1, "two", True),
    ]
    for value in samples:
        snapshot = freeze(value)
        payload = serialize_snapshot(snapshot)
        assert deserialize_snapshot(payload) == snapshot


def test_serialize_deserialize_round_trip_dataclass() -> None:
    @dataclass(frozen=True)
    class Cfg:
        name: str
        values: tuple[int, ...]

    snapshot = freeze(Cfg("x", (1, 2)))
    assert deserialize_snapshot(serialize_snapshot(snapshot)) == snapshot


def test_serialize_deserialize_round_trip_adapter_value() -> None:
    adapters = {Point: PointAdapter()}
    snapshot = freeze(Point(1, 2), adapters=adapters)
    assert deserialize_snapshot(serialize_snapshot(snapshot)) == snapshot


def test_serialize_deserialize_round_trip_frozen_graph_with_cycle() -> None:
    payload: list[Any] = []
    payload.append(payload)
    snapshot = freeze(payload)
    assert isinstance(snapshot, FrozenGraph)

    bytes_payload = serialize_snapshot(snapshot)
    decoded = deserialize_snapshot(bytes_payload)
    assert decoded == snapshot


def test_deserialize_rejects_payload_without_k2_prefix() -> None:
    with pytest.raises(UnsupportedValueError, match="kernel fingerprint version"):
        deserialize_snapshot(b"K1;N;")
    with pytest.raises(UnsupportedValueError, match="kernel fingerprint version"):
        deserialize_snapshot(b"N;")
    with pytest.raises(UnsupportedValueError, match="kernel fingerprint version"):
        deserialize_snapshot(b"")


def test_deserialize_rejects_trailing_garbage() -> None:
    payload = serialize_snapshot(freeze(42))
    with pytest.raises(UnsupportedValueError, match="trailing"):
        deserialize_snapshot(payload + b"junk")


def test_snapshot_validation_rejects_invalid_graphs_and_excessive_depth() -> None:
    invalid = FrozenGraph(nodes=(), root=FrozenRef(0))
    with pytest.raises(UnsupportedValueError, match="non-empty"):
        freeze(invalid)
    with pytest.raises(UnsupportedValueError, match="non-empty"):
        serialize_snapshot(invalid)
    with pytest.raises(UnsupportedValueError, match="non-empty"):
        thaw(invalid)

    deep_payload = b"K2;" + b"t1:" * 3_000 + b"N;" + b";" * 3_000
    with pytest.raises(UnsupportedValueError, match="invalid snapshot encoding"):
        deserialize_snapshot(deep_payload)


def test_snapshot_metadata_requires_exact_unicode_strings() -> None:
    class StringSubclass(str):
        pass

    invalid_metadata = (
        FrozenSet(cast(Any, StringSubclass("set")), ()),
        FrozenRecord(cast(Any, StringSubclass("Record")), ()),
        FrozenRecord("Record", ((cast(Any, StringSubclass("field")), 1),)),
        FrozenAdapterValue(cast(Any, StringSubclass("module:Type")), 1),
        FrozenRecord("\ud800", ()),
        FrozenRecord("Record", (("\ud800", 1),)),
        FrozenAdapterValue("module:\ud800", 1),
    )

    for snapshot in invalid_metadata:
        with pytest.raises(UnsupportedValueError):
            serialize_snapshot(snapshot)


def test_freeze_rejects_values_that_thaw_unhashably_in_hash_positions() -> None:
    @dataclass(frozen=True)
    class Key:
        value: int

    invalid_values = (
        {Key(1): "value"},
        {Key(1)},
        frozenset({Key(1)}),
        {(Key(1),): "value"},
        {frozenset({Key(1)}): "value"},
        {FrozenList((1,)): "value"},
        {FrozenDict((("key", 1),)): "value"},
        {FrozenSet("set", (1,)): "value"},
        {FrozenGraph((FrozenList(()),), FrozenRef(0)): "value"},
    )

    for value in invalid_values:
        with pytest.raises(UnsupportedValueError, match="remain hashable"):
            freeze(value)

    adapted = freeze({Point(1, 2)}, adapters={Point: PointAdapter()})
    assert thaw(adapted, adapters={Point: PointAdapter()}) == {Point(1, 2)}


def test_scalar_subclasses_require_an_adapter_at_boundaries() -> None:
    class StatefulInt(int):
        factor: int

        def __new__(cls, value: int, factor: int) -> StatefulInt:
            instance = super().__new__(cls, value)
            instance.factor = factor
            return instance

    with pytest.raises(UnsupportedValueError, match="Scalar subclass"):
        freeze(StatefulInt(2, 3))


def test_integers_past_the_int_to_str_limit_are_rejected_as_boundary_values() -> None:
    # freeze accepts the value -- the K2 grammar has no width limit -- but the
    # encoder cannot render it, so the rejection is typed like every other one.
    huge = 10**5000

    with pytest.raises(UnsupportedValueError, match="digit"):
        fingerprint_snapshot(freeze(huge))
    with pytest.raises(UnsupportedValueError, match="digit"):
        fingerprint(huge)
    with pytest.raises(UnsupportedValueError, match="digit"):
        serialize_snapshot(freeze(huge))


def test_nan_payloads_are_canonicalized_and_prefrozen_payloads_are_validated() -> None:
    first = struct.unpack(">d", bytes.fromhex("7ff8000000000001"))[0]
    second = struct.unpack(">d", bytes.fromhex("7ff8000000000002"))[0]
    canonical_first = freeze(first)
    canonical_second = freeze(second)

    assert struct.pack(">d", cast(float, canonical_first)) == struct.pack(
        ">d", cast(float, canonical_second)
    )
    with pytest.raises(UnsupportedValueError, match="canonical bit pattern"):
        freeze(FrozenList((first,)))


def test_semantic_equality_of_canonical_snapshots_matches_snapshot_equality() -> None:
    """freeze is encoding-preserving on its own outputs.

    For canonical snapshots ``a`` and ``b``, re-freezing never changes the
    canonical encoding, so ``semantic_equal(a, b)`` coincides with
    ``snapshots_equal(a, b)``. The runtime's default backdate decision is
    exactly ``snapshots_equal`` on the stored snapshots (no digest fallback),
    so this reduction is what makes the one relation one.
    """

    @dataclass(frozen=True)
    class Sample:
        tag: str
        payload: Any

    adapters = {Point: PointAdapter()}
    rng = random.Random(20260801)

    scalar_pool: tuple[object, ...] = (
        0,
        1,
        -7,
        3.5,
        -0.0,
        0.0,
        1e300,
        float("inf"),
        float("-inf"),
        float("nan"),
        complex(2, -0.0),
        complex(0, 1.5),
        True,
        False,
        None,
        "",
        "a",
        "é☃",
        b"",
        b"\x00\xff",
    )
    key_pool: tuple[object, ...] = (1, 1.0, 2, "k", "k2", True, (1, 2), frozenset({3}), b"k")

    def build(depth: int) -> object:
        kinds = ["scalar", "scalar", "dataclass", "adapter"]
        if depth < 3:
            kinds += ["list", "tuple", "dict", "set", "frozenset", "shared", "cycle"]
        kind = rng.choice(kinds)
        if kind == "scalar":
            return rng.choice(scalar_pool)
        if kind == "dataclass":
            return Sample(rng.choice(["p", "q"]), build(depth + 1))
        if kind == "adapter":
            return Point(rng.randint(0, 2), rng.randint(0, 2))
        if kind == "list":
            return [build(depth + 1) for _ in range(rng.randint(0, 3))]
        if kind == "tuple":
            return tuple(build(depth + 1) for _ in range(rng.randint(0, 3)))
        if kind == "dict":
            return {rng.choice(key_pool): build(depth + 1) for _ in range(rng.randint(0, 3))}
        if kind == "set":
            return {rng.choice(key_pool) for _ in range(rng.randint(0, 3))}
        if kind == "frozenset":
            return frozenset(rng.choice(key_pool) for _ in range(rng.randint(0, 2)))
        if kind == "shared":
            inner = [build(depth + 1)]
            return [inner, inner, {"s": inner}]
        assert kind == "cycle"
        loop: list[object] = [rng.randint(0, 3)]
        loop.append(loop)
        return {"c": loop, "o": build(depth + 1)}

    compared = 0
    for _ in range(600):
        left_value = build(0)
        right_value = left_value if rng.random() < 0.3 else build(0)
        try:
            left = freeze(left_value, adapters=adapters)
            right = freeze(right_value, adapters=adapters)
        except UnsupportedValueError as refusal:
            # The generator can place an adapted value inside shared or cyclic
            # structure, where its mapping payload becomes a node of its own
            # and the freeze refuses it. There is no snapshot for the property
            # to compare, so the pair is skipped rather than asserted on.
            assert "cannot hand back whole" in str(refusal)
            continue
        compared += 1
        assert fingerprint_snapshot(freeze(left, adapters=adapters)) == fingerprint_snapshot(left)
        assert semantic_equal(left, right, adapters=adapters) == snapshots_equal(left, right)
    # The refusals must not be what the loop mostly does: the seeded generator
    # yields well over four hundred comparable pairs, and a floor well under
    # that catches a change that quietly empties the property.
    assert compared >= 400


_TOWER_PAIRS: tuple[tuple[object, object], ...] = (
    (1, 1.0),
    (True, 1),
    (False, 0),
    (0.0, -0.0),
    (1, 1 + 0j),
)


def test_canonical_relation_separates_the_numeric_tower() -> None:
    for left, right in _TOWER_PAIRS:
        assert not semantic_equal(left, right), (left, right)
        assert not semantic_equal([left], [right])
        assert not semantic_equal((left,), (right,))
        assert not semantic_equal({"k": left}, {"k": right})
        assert not semantic_equal({left}, {right})
        assert not semantic_equal(frozenset({left}), frozenset({right}))


def test_canonical_relation_separates_nested_dataclass_fields() -> None:
    @dataclass(frozen=True)
    class Holder:
        x: Any

    for left, right in _TOWER_PAIRS:
        assert not semantic_equal(Holder(left), Holder(right)), (left, right)


def test_canonical_relation_makes_nan_reflexive() -> None:
    nan = float("nan")
    assert semantic_equal(nan, nan)
    assert semantic_equal((1.0, nan), (1.0, nan))
    assert semantic_equal([nan], [nan])
    assert snapshots_equal(freeze([nan]), freeze([nan]))


def test_canonical_relation_still_equates_equal_values() -> None:
    assert semantic_equal([1, 2], [1, 2])
    assert semantic_equal({"a": 1.5}, {"a": 1.5})
    assert not semantic_equal([1, 2], [1, 3])
    assert not semantic_equal([1, 2], (1, 2))


def test_canonical_relation_refuses_values_that_are_not_snapshots() -> None:
    """The relation is defined by the encoding, so it has no `==` fallback.

    A value the encoder cannot describe gets no verdict at all -- answering
    `False` for it would be an equality claim the encoding never made.
    """

    with pytest.raises(TypeError):
        snapshots_equal(object(), object())
    with pytest.raises(UnsupportedValueError):
        semantic_equal(object(), object())


_UNHASHABLE_THAW_ARMS = (
    pytest.param(
        FrozenDict(entries=((FrozenDict(entries=(("k", 1),)), "value"),)),
        "cannot be a dictionary key",
        id="mapping-key",
    ),
    pytest.param(
        FrozenSet(kind="set", items=(FrozenDict(entries=(("k", 1),)),)),
        "cannot be a set member",
        id="set-member",
    ),
    pytest.param(
        FrozenGraph(
            nodes=(
                FrozenList(items=(1, 2)),
                FrozenDict(entries=((FrozenRef(index=0), "value"),)),
            ),
            root=FrozenRef(index=1),
        ),
        "cannot be a dictionary key",
        id="mapping-key-through-a-shared-node",
    ),
    pytest.param(
        FrozenGraph(
            nodes=(
                FrozenList(items=(1, 2)),
                FrozenSet(kind="set", items=(FrozenRef(index=0),)),
            ),
            root=FrozenRef(index=1),
        ),
        "cannot be a set member",
        id="set-member-through-a-shared-node",
    ),
)


@pytest.mark.parametrize(("snapshot", "expected"), _UNHASHABLE_THAW_ARMS)
def test_thawing_a_container_where_a_hashable_value_belongs_is_refused(
    snapshot: Any, expected: str
) -> None:
    """A key or member that thaws into a container is the encoding's business.

    Both positions accept whatever the snapshot puts there, and a mapping or a
    list arriving in one of them is discovered only when the reconstructed
    container refuses to take it. That refusal is the interpreter's, in wording
    that has moved between releases and that says nothing about the snapshot it
    came out of, so the boundary answers in its own words instead and names the
    container that cannot go where it was put. Both encodings reach it: written
    inline, and lifted into a shared node the reference resolves to.
    """

    with pytest.raises(UnsupportedValueError, match=expected):
        thaw(snapshot)


def test_thawing_keeps_every_key_and_member_a_hashable_value_can_hold() -> None:
    """The refusal is for the position, not for the shape reaching it.

    A tuple key holding scalars, a frozen set member, and the same containers
    in the value position are all hashable or unconstrained, and each still
    thaws to the value it encodes.
    """

    mapping = {("a", 1): [1, 2], frozenset({1, 2}): "member"}
    assert thaw(freeze(mapping)) == mapping
    members = {1, ("a", 2), frozenset({3})}
    assert thaw(freeze(members)) == members


# ---------------------------------------------------------------------------
# Group L: adapted payloads the shared-structure encoding cannot return whole
# ---------------------------------------------------------------------------


class _AdaptedHolder:
    """A boundary value an adapter carries, wrapping one container of its own."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner


class _MappingPayloadAdapter(ValueAdapter):
    def freeze(self, value: _AdaptedHolder, freeze: Any) -> object:
        return {"payload": value.inner}

    def thaw(self, snapshot: Any, thaw: Any) -> _AdaptedHolder:
        return _AdaptedHolder(thaw(snapshot["payload"]))


class _ListPayloadAdapter(ValueAdapter):
    def freeze(self, value: _AdaptedHolder, freeze: Any) -> object:
        return [value.inner]

    def thaw(self, snapshot: Any, thaw: Any) -> _AdaptedHolder:
        return _AdaptedHolder(thaw(snapshot[0]))


class _TuplePayloadAdapter(ValueAdapter):
    """A payload written inline that still reaches a container of its own."""

    def freeze(self, value: _AdaptedHolder, freeze: Any) -> object:
        return (value.inner,)

    def thaw(self, snapshot: Any, thaw: Any) -> _AdaptedHolder:
        return _AdaptedHolder(thaw(snapshot[0]))


_PAYLOAD_SHAPES: dict[str, type[ValueAdapter]] = {
    "mapping": _MappingPayloadAdapter,
    "list": _ListPayloadAdapter,
    "tuple": _TuplePayloadAdapter,
}


def _placed(adapted: _AdaptedHolder, placement: str) -> Any:
    """Return the adapted value alone, inside shared structure, or in a cycle."""

    if placement == "alone":
        return adapted
    if placement == "shared-container":
        box = [adapted]
        return {"left": box, "right": box}
    holder: list[Any] = [adapted]
    holder.append(holder)
    return holder


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("shape", ["mapping", "list", "tuple"])
@pytest.mark.parametrize("placement", ["alone", "shared-container", "cyclic"])
def test_an_adapted_payload_comes_back_whole_or_is_refused(
    mode: str, shape: str, placement: str
) -> None:
    """An adapted value is returned intact, or the freeze refuses it.

    An adapter's `thaw` is handed the payload as the snapshot holds it. Alone,
    every payload shape is written into the value itself and comes back whole
    in every mode. Inside shared or cyclic structure the encoding lifts the
    containers it memoizes into nodes of their own, and a payload that reaches
    one of them can no longer be handed back: a mapping or list payload is
    itself the node, and a tuple payload -- inline only as far as its own
    elements -- carries a reference to one. Every such placement is refused at
    the freeze rather than resolved into a value that depends on the mode and
    on where the adapted value sat.
    """

    source = Input[int]("adapted-payload-source")

    @query
    def result(db: Database) -> Any:
        return _placed(_AdaptedHolder({"n": source.read(db)}), placement)

    db = Database(mode=mode, adapters={_AdaptedHolder: _PAYLOAD_SHAPES[shape]()})
    db.set(source, 1)

    if placement == "alone":
        adapted = db.get(result)
        assert isinstance(adapted, _AdaptedHolder)
        assert dict(adapted.inner) == {"n": 1}
        return

    requests = db.statistics().total_requests
    with pytest.raises(UnsupportedValueError, match="cannot hand back whole") as refusal:
        db.get(result)
    assert "_AdaptedHolder" in str(refusal.value)
    # Nothing was kept that a second request could be served from: the refusal
    # repeats rather than a stored value standing in for it, and the database
    # holds no record of the query that was refused.
    with pytest.raises(UnsupportedValueError, match="cannot hand back whole"):
        db.get(result)
    statistics = db.statistics()
    assert statistics.total_requests == requests + 2
    assert statistics.query_count == 0
    assert statistics.query_reuses == 0
