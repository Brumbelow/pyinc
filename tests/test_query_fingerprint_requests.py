from __future__ import annotations

import gc
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from types import FunctionType
from typing import Any, cast

import pytest

from pyinc import (
    Database,
    FileResource,
    FrozenList,
    InMemoryArtifactStore,
    Input,
    Query,
    Resource,
    query,
)

_MODES = ("strict", "checked", "fast")
_GLOBAL_IDENTITY_CAPTURE: object = tuple([0])
_REPLACEABLE_CAPTURE_TYPE: type[Any] = type(
    "_REPLACEABLE_CAPTURE_TYPE", (), {"__module__": __name__}
)
_MUTATION_CASES = (
    "frozen-default",
    "annotation",
    "resource-configuration",
    "input-policy-state",
)


@dataclass(frozen=True)
class _FrozenBox:
    value: int


@dataclass(frozen=True, eq=False, slots=True, weakref_slot=True)
class _IdentityBox:
    value: int


@dataclass(frozen=True, slots=True, weakref_slot=True)
class _StructuralBox:
    value: int


@dataclass(frozen=True, slots=True)
class _NestedHolder:
    child: _StructuralBox


class _PolicyWithAliases:
    def __init__(self, value: FrozenList) -> None:
        self.a = value
        self.b = value

    def __call__(self, left: Any, right: Any) -> bool:
        return bool(left == right)


class _TypeWithMarker:
    marker = tuple([1])


@dataclass(frozen=True, slots=True)
class _AnnotationHolder:
    child: FrozenList


class _TypeWithAnnotation:
    field: _AnnotationHolder


@dataclass(frozen=True)
class _NestedIdentityResource(Resource[str, int, int]):
    holder: _NestedHolder
    return_nested_identity: bool = False

    def identity(self) -> object:
        if self.return_nested_identity:
            return self.holder.child
        return self

    def probe(self, key: str) -> int:
        return 0

    def load(self, db: Database, key: str) -> int:
        return id(self.holder.child)

    def label(self, key: str) -> str:
        return f"nested-identity[{key}]"


class _ConfiguredResource(Resource[str, int, str]):
    def __init__(self, value: int) -> None:
        self.value = value

    def identity(self) -> object:
        return ("configured-resource", self.value)

    def probe(self, key: str) -> str:
        return key

    def load(self, db: Database, key: str) -> int:
        return self.value

    def label(self, key: str) -> str:
        return f"configured[{key}]"


class _InputPolicy:
    def __init__(self, marker: int) -> None:
        self.marker = marker

    def __call__(self, left: Any, right: Any) -> bool:
        return bool(left == right)


def _definition_mutation_case(name: str) -> tuple[Query[Any, int], Callable[[], None]]:
    if name == "frozen-default":
        default = _FrozenBox(1)

        def raw_default(db: Database, selected: _FrozenBox = default) -> int:
            return selected.value

        def mutate_default() -> None:
            object.__setattr__(default, "value", 2)

        return Query(raw_default, key=f"query-fingerprint:{name}"), mutate_default

    elif name == "annotation":

        def raw_annotation(db: Database) -> int:
            value = raw_annotation.__annotations__["marker"]
            if type(value) is not int:
                raise TypeError("marker annotation must be an integer")
            return value

        raw_annotation.__annotations__["marker"] = 1

        def mutate_annotation() -> None:
            raw_annotation.__annotations__["marker"] = 2

        return Query(raw_annotation, key=f"query-fingerprint:{name}"), mutate_annotation

    elif name == "resource-configuration":
        resource = _ConfiguredResource(1)

        def raw_resource(db: Database) -> int:
            return resource.read(db, "stable")

        def mutate_resource() -> None:
            resource.value = 2

        return Query(raw_resource, key=f"query-fingerprint:{name}"), mutate_resource

    elif name == "input-policy-state":
        policy = _InputPolicy(1)
        source = Input[int]("query-fingerprint-policy", eq=policy)

        def raw_policy(db: Database) -> int:
            current = cast(_InputPolicy, source.eq)
            return current.marker

        def mutate_policy() -> None:
            policy.marker = 2

        return Query(raw_policy, key=f"query-fingerprint:{name}"), mutate_policy

    raise AssertionError(f"unknown mutation case: {name}")


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("case", _MUTATION_CASES)
def test_in_place_query_definition_mutation_matches_fresh(case: str, mode: str) -> None:
    value, mutate = _definition_mutation_case(case)
    warm = Database(mode=mode)

    old_identity = warm._query_fingerprint(value)
    assert warm.get(value) == 1

    mutate()

    new_identity = warm._query_fingerprint(value)
    warm_result = warm.get(value)
    fresh_result = Database(mode=mode).get(value)

    assert new_identity != old_identity
    assert warm_result == fresh_result == 2
    assert warm.statistics().query_executions == 2


@pytest.mark.parametrize("mode", _MODES)
def test_checkpoint_cannot_reuse_an_observed_pre_mutation_fingerprint(mode: str) -> None:
    value, mutate = _definition_mutation_case("frozen-default")
    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    assert writer.get(value) == 1
    checkpoint = writer.save_checkpoint()

    reader = Database(mode=mode, store=store)
    reader.load_checkpoint(checkpoint)
    subscription = reader.observe(lambda event: None, value)

    mutate()

    warm_result = reader.get(value)
    fresh_result = Database(mode=mode).get(value)

    assert warm_result == fresh_result == 2
    assert reader.inspect(value).last_recompute == "executed"
    subscription.unsubscribe()


def test_query_fingerprint_is_computed_once_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raw(db: Database) -> int:
        return 1

    value = Query(raw, key="query-fingerprint:request-lifetime")
    db = Database()
    original = db._code_fingerprint
    calls = 0

    def counting_fingerprint(fn: Any) -> str:
        nonlocal calls
        calls += 1
        return original(fn)

    monkeypatch.setattr(db, "_code_fingerprint", counting_fingerprint)

    with db.request_span():
        assert db.get(value) == 1
        assert db.get(value) == 1
        assert db.inspect(value).kind == "query"
        assert calls == 1

    assert db._request_query_fingerprints.get() is None
    assert db.get(value) == 1
    assert calls == 2

    with db.request_span():
        assert db.get(value) == 1
        assert calls == 3
        db.request_inputs_changed()
        assert db.get(value) == 1
        assert calls == 4

    assert db._request_query_fingerprints.get() is None


def test_query_fingerprint_outside_request_is_never_memoized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raw(db: Database) -> int:
        return 1

    value = Query(raw, key="query-fingerprint:outside-request")
    db = Database()
    original = db._code_fingerprint
    calls = 0

    def counting_fingerprint(fn: Any) -> str:
        nonlocal calls
        calls += 1
        return original(fn)

    monkeypatch.setattr(db, "_code_fingerprint", counting_fingerprint)

    assert db._query_fingerprint(value) == db._query_fingerprint(value)
    assert calls == 2
    assert db._request_query_fingerprints.get() is None


def test_failed_request_discards_query_fingerprint_memo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raw(db: Database) -> int:
        return 1

    value = Query(raw, key="query-fingerprint:failed-request")
    db = Database()
    original = db._code_fingerprint
    calls = 0

    def counting_fingerprint(fn: Any) -> str:
        nonlocal calls
        calls += 1
        return original(fn)

    monkeypatch.setattr(db, "_code_fingerprint", counting_fingerprint)

    with pytest.raises(RuntimeError, match="request failed"), db.request_span():
        assert db.get(value) == 1
        raise RuntimeError("request failed")

    assert calls == 1
    assert db._request_query_fingerprints.get() is None
    assert db.get(value) == 1
    assert calls == 2


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("shape", ("tuple", "identity-dataclass", "nan"))
def test_equal_state_default_replacement_moves_identity(shape: str, mode: str) -> None:
    if shape == "tuple":
        old: object = tuple([1])
        new: object = tuple([1])
    elif shape == "identity-dataclass":
        old = _IdentityBox(1)
        new = _IdentityBox(1)
    else:
        old = float("nan")
        new = float("nan")

    def raw(db: Database, selected: object = old) -> int:
        return id(selected)

    value = Query(raw, key=f"query-fingerprint:replacement:{shape}")
    warm = Database(mode=mode)
    old_fingerprint = warm._query_fingerprint(value)
    assert warm.get(value) == id(old)

    raw.__defaults__ = (new,)
    new_fingerprint = warm._query_fingerprint(value)

    assert new_fingerprint != old_fingerprint
    assert warm.get(value) == Database(mode=mode).get(value) == id(new)


@pytest.mark.parametrize("mode", _MODES)
def test_annotation_insertion_order_moves_query_identity(mode: str) -> None:
    def raw(db: Database) -> tuple[str, ...]:
        return tuple(raw.__annotations__)

    raw.__annotations__ = {"a": 1, "b": 2}
    value = Query(raw, key="query-fingerprint:annotation-order")
    warm = Database(mode=mode)
    old_fingerprint = warm._query_fingerprint(value)
    assert warm.get(value) == ("a", "b")

    raw.__annotations__ = {"b": 2, "a": 1}
    new_fingerprint = warm._query_fingerprint(value)

    assert new_fingerprint != old_fingerprint
    assert warm.get(value) == Database(mode=mode).get(value) == ("b", "a")


@pytest.mark.parametrize("mode", _MODES)
def test_function_attribute_insertion_order_moves_query_identity(mode: str) -> None:
    def raw(db: Database) -> tuple[str, ...]:
        return tuple(raw.__dict__)

    raw.a = 1  # type: ignore[attr-defined]
    raw.b = 2  # type: ignore[attr-defined]
    value = Query(raw, key="query-fingerprint:function-state-order")
    warm = Database(mode=mode)
    old_fingerprint = warm._query_fingerprint(value)
    assert warm.get(value) == ("a", "b")

    del raw.a  # type: ignore[attr-defined]
    raw.a = 1  # type: ignore[attr-defined]
    new_fingerprint = warm._query_fingerprint(value)

    assert new_fingerprint != old_fingerprint
    assert warm.get(value) == Database(mode=mode).get(value) == ("b", "a")


@pytest.mark.parametrize("mode", _MODES)
def test_input_policy_state_order_moves_query_identity(mode: str) -> None:
    policy = _InputPolicy(1)
    dynamic_policy = cast(Any, policy)
    dynamic_policy.a = 1
    dynamic_policy.b = 2
    source = Input[int](f"query-fingerprint:policy-order:{mode}", eq=policy)

    def raw(db: Database) -> tuple[str, ...]:
        current = cast(_InputPolicy, source.eq)
        return tuple(current.__dict__)

    value = Query(raw, key="query-fingerprint:policy-state-order")
    warm = Database(mode=mode)
    old_fingerprint = warm._query_fingerprint(value)
    assert warm.get(value) == ("marker", "a", "b")

    del dynamic_policy.a
    dynamic_policy.a = 1
    new_fingerprint = warm._query_fingerprint(value)

    assert new_fingerprint != old_fingerprint
    assert warm.get(value) == Database(mode=mode).get(value) == ("marker", "b", "a")


@pytest.mark.parametrize("mode", _MODES)
def test_definition_order_change_invalidates_checkpoint(mode: str) -> None:
    def raw(db: Database) -> tuple[str, ...]:
        return tuple(raw.__annotations__)

    raw.__annotations__ = {"a": 1, "b": 2}
    value = Query(raw, key="query-fingerprint:checkpoint-order")
    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    assert writer.get(value) == ("a", "b")
    checkpoint = writer.save_checkpoint()

    raw.__annotations__ = {"b": 2, "a": 1}
    warmed = Database(mode=mode, store=store)
    warmed.load_checkpoint(checkpoint)

    assert warmed.get(value) == Database(mode=mode).get(value) == ("b", "a")
    assert warmed.inspect(value).last_recompute == "executed"


def _identity_capture_case(
    shape: str, initial: object
) -> tuple[Query[Any, int], Callable[[object], None]]:
    if shape == "nonlocal":
        captured = initial

        def raw(db: Database) -> int:
            return id(captured)

        def replace(value: object) -> None:
            nonlocal captured
            captured = value

        return Query(raw, key="query-fingerprint:nonlocal-identity"), replace

    if shape == "global":
        global _GLOBAL_IDENTITY_CAPTURE
        _GLOBAL_IDENTITY_CAPTURE = initial

        def raw(db: Database) -> int:
            return id(_GLOBAL_IDENTITY_CAPTURE)

        def replace(value: object) -> None:
            global _GLOBAL_IDENTITY_CAPTURE
            _GLOBAL_IDENTITY_CAPTURE = value

        return Query(raw, key="query-fingerprint:global-identity"), replace

    raise AssertionError(f"unknown identity capture shape: {shape}")


def _weak_identity_capture_case(
    shape: str, initial: _IdentityBox
) -> tuple[Query[Any, int], Callable[[object | None], None]]:
    if shape == "default":

        def raw_default(db: Database, selected: object = initial) -> int:
            return weakref.getweakrefcount(selected)

        def replace(value: object | None) -> None:
            raw_default.__defaults__ = (value,)

        return Query(raw_default, key="query-fingerprint:default-incarnation"), replace

    if shape == "nonlocal":
        captured: object | None = initial

        def raw_nonlocal(db: Database) -> int:
            return weakref.getweakrefcount(captured)

        def replace(value: object | None) -> None:
            nonlocal captured
            captured = value

        return Query(raw_nonlocal, key="query-fingerprint:nonlocal-incarnation"), replace

    if shape == "global":
        global _GLOBAL_IDENTITY_CAPTURE
        _GLOBAL_IDENTITY_CAPTURE = initial

        def raw_global(db: Database) -> int:
            return weakref.getweakrefcount(_GLOBAL_IDENTITY_CAPTURE)

        def replace(value: object | None) -> None:
            global _GLOBAL_IDENTITY_CAPTURE
            _GLOBAL_IDENTITY_CAPTURE = value

        return Query(raw_global, key="query-fingerprint:global-incarnation"), replace

    raise AssertionError(f"unknown weak identity capture shape: {shape}")


def _clone_function(function: FunctionType) -> FunctionType:
    clone = FunctionType(
        function.__code__,
        function.__globals__,
        name=function.__name__,
        argdefs=function.__defaults__,
        closure=function.__closure__,
    )
    clone.__kwdefaults__ = dict(function.__kwdefaults__ or {})
    clone.__annotations__ = dict(function.__annotations__)
    clone.__dict__.update(function.__dict__)
    clone.__module__ = function.__module__
    clone.__qualname__ = function.__qualname__
    clone.__doc__ = function.__doc__
    return clone


def _special_identity_capture_case(
    shape: str,
) -> tuple[object, object, Query[Any, int], Callable[[object], None]]:
    if shape == "function":

        def helper() -> int:
            return 1

        old: object = helper
        new: object = _clone_function(cast(FunctionType, helper))
    elif shape == "query":

        def leaf(db: Database) -> int:
            return 1

        old = Query(leaf, key="query-fingerprint:replaceable-leaf")
        new = Query(leaf, key="query-fingerprint:replaceable-leaf")
    elif shape == "input":
        old = Input[int]("query-fingerprint:replaceable-input")
        new = Input[int]("query-fingerprint:replaceable-input")
    elif shape == "resource":
        old = FileResource()
        new = FileResource()
    elif shape == "type":
        global _REPLACEABLE_CAPTURE_TYPE
        old = _REPLACEABLE_CAPTURE_TYPE
        new = type("_REPLACEABLE_CAPTURE_TYPE", (), {"__module__": __name__})

        def parent_type(db: Database) -> int:
            return id(_REPLACEABLE_CAPTURE_TYPE)

        def replace_type(value: object) -> None:
            global _REPLACEABLE_CAPTURE_TYPE
            _REPLACEABLE_CAPTURE_TYPE = cast(type[Any], value)

        return old, new, Query(parent_type, key="query-fingerprint:type-site"), replace_type
    else:
        raise AssertionError(f"unknown special identity capture shape: {shape}")

    captured = old

    def parent(db: Database) -> int:
        return id(captured)

    def replace(value: object) -> None:
        nonlocal captured
        captured = value

    return old, new, Query(parent, key=f"query-fingerprint:{shape}-site"), replace


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("shape", ("global", "nonlocal"))
def test_equal_state_capture_replacement_matches_warm_fresh_and_checkpoint(
    shape: str, mode: str
) -> None:
    old = tuple([1])
    new = tuple([1])
    value, replace = _identity_capture_case(shape, old)
    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    try:
        old_fingerprint = writer._query_fingerprint(value)
        assert writer.get(value) == id(old)
        checkpoint = writer.save_checkpoint()

        replace(new)
        new_fingerprint = writer._query_fingerprint(value)
        assert new_fingerprint != old_fingerprint
        assert writer.get(value) == Database(mode=mode).get(value) == id(new)

        reader = Database(mode=mode, store=store)
        reader.load_checkpoint(checkpoint)
        assert reader.get(value) == Database(mode=mode).get(value) == id(new)
        assert reader.inspect(value).last_recompute == "executed"
    finally:
        if shape == "global":
            replace(tuple([0]))


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("shape", ("default", "global", "nonlocal"))
def test_definition_site_generation_prevents_allocator_reuse_alias(shape: str, mode: str) -> None:
    old = _IdentityBox(1)
    old_address = id(old)
    watch = weakref.ref(old)
    value, replace = _weak_identity_capture_case(shape, old)
    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    try:
        old_fingerprint = writer._query_fingerprint(value)
        assert writer.get(value) == 1
        checkpoint = writer.save_checkpoint()

        # Dropping the language-level reference used to let CPython recycle
        # this address immediately. The fingerprint registry must retain the
        # old object until it can compare the replacement by identity.
        replace(None)
        del old
        gc.collect()
        retained = watch()
        assert retained is not None
        assert id(retained) == old_address
        del retained

        replacement = _IdentityBox(1)
        replace(replacement)
        new_fingerprint = writer._query_fingerprint(value)
        gc.collect()

        assert watch() is None
        assert new_fingerprint != old_fingerprint
        assert writer.get(value) == Database(mode=mode).get(value) == 0

        reader = Database(mode=mode, store=store)
        reader.load_checkpoint(checkpoint)
        assert reader.get(value) == Database(mode=mode).get(value) == 0
        assert reader.inspect(value).last_recompute == "executed"
    finally:
        if shape == "global":
            replace(tuple([0]))


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("shape", ("function", "query", "input", "resource", "type"))
def test_special_capture_replacement_matches_warm_fresh_and_checkpoint(
    shape: str, mode: str
) -> None:
    old, new, value, replace = _special_identity_capture_case(shape)
    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    try:
        old_fingerprint = writer._query_fingerprint(value)
        assert writer.get(value) == id(old)
        checkpoint = writer.save_checkpoint()

        replace(new)
        new_fingerprint = writer._query_fingerprint(value)

        assert new_fingerprint != old_fingerprint
        assert writer.get(value) == Database(mode=mode).get(value) == id(new)

        reader = Database(mode=mode, store=store)
        reader.load_checkpoint(checkpoint)
        assert reader.get(value) == Database(mode=mode).get(value) == id(new)
        assert reader.inspect(value).last_recompute == "executed"
    finally:
        if shape == "type":
            replace(old)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("shape", ("dataclass", "frozen-list"))
def test_nested_capture_replacement_matches_warm_fresh_and_checkpoint(
    shape: str, mode: str
) -> None:
    old = _StructuralBox(1)
    new = _StructuralBox(1)
    if shape == "dataclass":
        container: object = _NestedHolder(old)

        def replace() -> None:
            object.__setattr__(container, "child", new)

        def selected() -> _StructuralBox:
            return cast(_NestedHolder, container).child

    else:
        container = FrozenList((old,))

        def replace() -> None:
            object.__setattr__(container, "items", (new,))

        def selected() -> _StructuralBox:
            return cast(_StructuralBox, container[0])

    @query(key=f"query-fingerprint:nested-capture:{shape}")
    def parent(db: Database) -> int:
        return id(selected())

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    old_fingerprint = writer._query_fingerprint(parent)
    assert writer.get(parent) == id(old)
    checkpoint = writer.save_checkpoint()

    replace()

    assert writer._query_fingerprint(parent) != old_fingerprint
    assert writer.get(parent) == Database(mode=mode).get(parent) == id(new)
    reader = Database(mode=mode, store=store)
    reader.load_checkpoint(checkpoint)
    assert reader.get(parent) == id(new)
    assert reader.inspect(parent).last_recompute == "executed"


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("handle", ("query", "input"))
def test_policy_alias_topology_matches_warm_fresh_and_checkpoint(handle: str, mode: str) -> None:
    shared = FrozenList((1,))
    policy = _PolicyWithAliases(shared)
    if handle == "query":

        @query(key="query-fingerprint:aliased-policy-child", eq=policy)
        def child(db: Database) -> int:
            return 1

        @query(key="query-fingerprint:aliased-query-policy-parent")
        def parent(db: Database) -> bool:
            current = cast(_PolicyWithAliases, child.eq)
            return current.a is current.b

    else:
        source = Input[int]("query-fingerprint:aliased-input-policy", eq=policy)

        @query(key="query-fingerprint:aliased-input-policy-parent")
        def parent(db: Database) -> bool:
            current = cast(_PolicyWithAliases, source.eq)
            return current.a is current.b

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    assert writer.get(parent) is True
    checkpoint = writer.save_checkpoint()

    policy.b = FrozenList((1,))

    assert writer.get(parent) is Database(mode=mode).get(parent) is False
    reader = Database(mode=mode, store=store)
    reader.load_checkpoint(checkpoint)
    assert reader.get(parent) is False
    assert reader.inspect(parent).last_recompute == "executed"


@pytest.mark.parametrize("mode", _MODES)
def test_captured_type_attribute_identity_matches_warm_fresh_and_checkpoint(
    mode: str,
) -> None:
    original = _TypeWithMarker.marker

    @query(key="query-fingerprint:type-marker-parent")
    def parent(db: Database) -> int:
        return id(_TypeWithMarker.marker)

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    try:
        assert writer.get(parent) == id(original)
        checkpoint = writer.save_checkpoint()
        _TypeWithMarker.marker = tuple([1])

        assert writer.get(parent) == Database(mode=mode).get(parent) == id(_TypeWithMarker.marker)
        reader = Database(mode=mode, store=store)
        reader.load_checkpoint(checkpoint)
        assert reader.get(parent) == id(_TypeWithMarker.marker)
        assert reader.inspect(parent).last_recompute == "executed"
    finally:
        _TypeWithMarker.marker = original


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("identity_shape", ("self", "nested"))
def test_nested_resource_configuration_matches_warm_fresh_and_checkpoint(
    identity_shape: str, mode: str
) -> None:
    old = _StructuralBox(1)
    new = _StructuralBox(1)
    holder = _NestedHolder(old)
    resource = _NestedIdentityResource(
        holder,
        return_nested_identity=identity_shape == "nested",
    )

    @query(key=f"query-fingerprint:nested-resource:{identity_shape}")
    def parent(db: Database) -> int:
        return resource.read(db, "stable")

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    assert writer.get(parent) == id(old)
    checkpoint = writer.save_checkpoint()

    object.__setattr__(holder, "child", new)

    assert writer.get(parent) == Database(mode=mode).get(parent) == id(new)
    reader = Database(mode=mode, store=store)
    reader.load_checkpoint(checkpoint)
    assert reader.get(parent) == id(new)
    assert reader.inspect(parent).last_recompute == "executed"


@pytest.mark.parametrize("mode", _MODES)
def test_nested_type_annotation_identity_matches_warm_fresh_and_checkpoint(
    mode: str,
) -> None:
    old = FrozenList((1,))
    new = FrozenList((1,))
    holder = _AnnotationHolder(old)
    annotations = _TypeWithAnnotation.__annotations__
    original = annotations["field"]
    annotations["field"] = holder

    @query(key="query-fingerprint:nested-type-annotation")
    def parent(db: Database) -> int:
        current = cast(_AnnotationHolder, _TypeWithAnnotation.__annotations__["field"])
        return id(current.child)

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    try:
        assert writer.get(parent) == id(old)
        checkpoint = writer.save_checkpoint()
        object.__setattr__(holder, "child", new)

        assert writer.get(parent) == Database(mode=mode).get(parent) == id(new)
        reader = Database(mode=mode, store=store)
        reader.load_checkpoint(checkpoint)
        assert reader.get(parent) == id(new)
        assert reader.inspect(parent).last_recompute == "executed"
    finally:
        annotations["field"] = original
