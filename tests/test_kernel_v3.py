from __future__ import annotations

import decimal
import functools
import hashlib
import importlib
import importlib.machinery
import json
import os
import string
import struct
import sys
import sysconfig
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import CodeType, FunctionType, MethodType, ModuleType
from typing import Any, NamedTuple, TypeVar, cast

import pytest

import pyinc.runtime as runtime_module
from pyinc import (
    BinaryFileResource,
    CheckpointManifestError,
    CheckpointVersionError,
    CycleError,
    Database,
    InMemoryArtifactStore,
    Input,
    InputKeyError,
    Query,
    Resource,
    UnsupportedValueError,
    query,
)


@dataclass(frozen=True)
class _ImmutableCaptureBox:
    value: Any


@dataclass(frozen=True)
class _BoundMethodOwner:
    base: int


class _ObservedConsts:
    SCALE = 2


def _observed_compute_one(value: int) -> int:
    return value + 1


def _observed_compute_two(value: int) -> int:
    return value + 2


class _ObservedPlain:
    compute = staticmethod(_observed_compute_one)


def _observed_static_source() -> int:
    return 3


def _observed_class_source() -> int:
    return 5


def _observed_property_source() -> int:
    return 7


def _observed_descriptor_replacement() -> int:
    return 11


class _ObservedStaticHolder:
    @staticmethod
    def read() -> int:
        return _observed_static_source() * 10


class _ObservedClassHolder:
    @classmethod
    def read(cls) -> int:
        return _observed_class_source() * 10


class _ObservedPropertyHolder:
    @property
    def read(self) -> int:
        return _observed_property_source() * 10


@dataclass(frozen=True)
class _ObservedBox:
    factor: int


@dataclass(frozen=True, slots=True)
class _ObservedSlottedBox:
    factor: int


_observed_box = _ObservedBox(2)
_observed_slotted_box = _ObservedSlottedBox(2)
_observed_alias = dict[str, _ObservedBox]


class _ObservedPair(NamedTuple):
    box: _ObservedBox
    tag: str


_observed_pair = _ObservedPair(_observed_box, "pair")
_observed_plain = (_observed_box, "plain")
_observed_members = frozenset({_observed_box})


class _ObservedResourceShape:
    """A plain class that answers the label/probe/load resource-handle probes."""

    MARKER = 1

    def label(self, key: int) -> str:
        return f"shaped[{key}]"

    def probe(self, key: int) -> int:
        return 0

    def load(self, db: Database, key: int) -> int:
        return 0


class _ObservedShapeHolder:
    nested = _ObservedResourceShape


@dataclass(frozen=True)
class _ObservedScaledResource(Resource[int, int, int]):
    scale: int = 2

    def label(self, key: int) -> str:
        return f"observed[{key}]"

    def probe(self, key: int) -> int:
        return self.scale

    def load(self, db: Database, key: int) -> int:
        return self.scale * key


class _ObservedResourceHolder:
    nested = _ObservedScaledResource(2)


class _ObservedPartsResource(Resource[int, int, int]):
    def __init__(self, scale: int) -> None:
        # A list, so a write into the configuration leaves every reference an
        # observation can pin identical and only re-reading identity() sees it.
        self.parts = [scale]

    def identity(self) -> tuple[str, tuple[int, ...]]:
        return ("observed-parts-resource", tuple(self.parts))

    def label(self, key: int) -> str:
        return f"parts[{key}]"

    def probe(self, key: int) -> int:
        return self.parts[0]

    def load(self, db: Database, key: int) -> int:
        return self.parts[0] * key


class _ObservedPartsHolder:
    """Class-body slot for a resource that a query also captures directly."""

    nested: Any = None


def _stdlib_dumps_replacement(*args: Any, **kwargs: Any) -> str:
    """Stands in for json.dumps while the stdlib fold-blindness pin runs."""

    return "replaced"


class _StdlibDecimalReplacement:
    """Stands in for decimal.Decimal while the stdlib fold-blindness pin runs."""

    def __init__(self, text: str) -> None:
        self.text = text

    def __mul__(self, other: int) -> str:
        return "replaced"


def _observed_documented() -> int:
    """ab"""

    return 1


def _observed_shared_source() -> int:
    return 3


class _ObservedDualDescriptorHolder:
    @staticmethod
    def scaled() -> int:
        return _observed_shared_source() * 10

    @property
    def offset(self) -> int:
        return _observed_shared_source() * 100


def _observed_cached_source() -> int:
    return 3


class _ObservedCachedHolder:
    @functools.cached_property
    def read(self) -> int:
        return _observed_cached_source() * 10


class _CountingEq:
    def __init__(self, tolerance: int) -> None:
        self.tolerance = tolerance

    def __call__(self, left: Any, right: Any) -> bool:
        return abs(int(left) - int(right)) <= self.tolerance


def _wrapped_base(value: int) -> int:
    return value


class _WrappedScaler:
    def __init__(self, k: int) -> None:
        self.k = k
        functools.wraps(_wrapped_base)(self)

    def __call__(self, value: int) -> int:
        return self.k * value


_wrapped_scaler = _WrappedScaler(2)


class _WrappedClass:
    __wrapped__ = _wrapped_base
    compute = staticmethod(_observed_compute_one)


class _UnsafeWrapped:
    def __init__(self) -> None:
        self.state = {"mutable": True}
        functools.wraps(_wrapped_base)(self)

    def __call__(self) -> int:
        return 1


_unsafe_wrapped = _UnsafeWrapped()


class _ReboundAnnotationsWrapped:
    def __init__(self) -> None:
        functools.wraps(_wrapped_base)(self)
        self.__annotations__ = {"value": "int"}

    def __call__(self) -> int:
        return 1


_rebound_annotations_wrapped = _ReboundAnnotationsWrapped()


class _CyclicWrapped:
    def __init__(self) -> None:
        self.cycle: Any = self
        functools.wraps(_wrapped_base)(self)

    def __call__(self) -> int:
        return 1


_cyclic_wrapped = _CyclicWrapped()


@functools.cache
def _wrapped_cache_decorated(value: int) -> int:
    return value * 2


def _handle_wrapped_one(db: Database) -> int:
    return 1


def _handle_wrapped_two(db: Database) -> int:
    return 2


def _memo_and_truth(db: Database, target: Any) -> tuple[str, str]:
    """The memoized fingerprint next to the recomputed truth for ``target``.

    Callers prime the memo, apply a mutation, then compare the two: a coherent
    memo either recomputes or was already equal; only a stale hit differs.
    """

    # Uncacheable queries are popped from the memo, which would make the
    # comparison below trivially true; only memoized queries can be probed.
    assert target in db._query_fingerprint_memo
    memoized = db._query_fingerprint(target)
    db._query_fingerprint_memo.pop(target, None)
    return memoized, db._query_fingerprint(target)


def _assert_warm_matches_fresh(db: Database, mode: str, target: Any, expected: Any) -> None:
    """Pin *db*'s warm answer for *target* against a from-scratch database.

    ``db`` has already answered *target* once and something the fingerprint
    covers has changed since. The execution counter is read before the warm
    call: a query whose identity moved with the change has no record to reuse
    and must execute, so a warm answer that merely repeated the stored one
    fails here instead of passing as agreement.
    """

    executions = db.statistics().query_executions
    warm = db.get(target)
    fresh = Database(mode=mode).get(target)
    assert warm == fresh == expected
    assert db.statistics().query_executions == executions + 1


def test_input_keys_are_nonempty_keyword_configured_and_unique_per_database() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        Input[int]("")
    with pytest.raises(TypeError):
        Input(  # type: ignore[call-arg]
            "positional-policy", cast(Any, lambda left, right: left == right)
        )

    first = Input[int]("stable-key")
    alias = Input[int]("stable-key")
    conflict = Input[int]("stable-key", cutoff=abs)
    db = Database()
    db.set(first, 1)
    assert alias.read(db) == 1
    assert db.statistics().input_count == 1
    with pytest.raises(InputKeyError, match="stable-key"):
        db.set(conflict, 2)
    assert first.read(db) == 1


def test_query_supports_explicit_keys_and_rejects_async_and_generator_functions() -> None:
    @query(key="project:answer")
    def answer(db: Database) -> int:
        return 42

    assert isinstance(answer, Query)
    assert answer.key == "project:answer"
    assert Database().get(answer) == 42

    with pytest.raises(TypeError, match="synchronous"):

        @query
        async def async_query(db: Database) -> int:
            return 1

    with pytest.raises(TypeError, match="non-generator"):

        @query
        def generator_query(db: Database) -> Any:
            yield 1


def test_query_metadata_copy_cannot_overwrite_its_contract() -> None:
    def raw(db: Database) -> int:
        """Query documentation."""
        return 7

    def comparator(left: Any, right: Any) -> bool:
        return bool(left == right)

    attributed = cast(Any, raw)
    attributed.key = "untrusted-key"
    attributed.eq = "not-callable"
    attributed.fn = object()

    wrapped = Query(raw, key="trusted-key", eq=comparator)

    assert wrapped.key == "trusted-key"
    assert wrapped.eq is comparator
    assert wrapped.fn is raw
    metadata = cast(Any, wrapped)
    assert metadata.__name__ == "raw"
    assert metadata.__doc__ == "Query documentation."
    assert metadata.__wrapped__ is raw


def test_query_identity_includes_comparator_policy() -> None:
    def make(comparator: Any) -> Query[[], int]:
        @query(key="policy-sensitive", eq=comparator)
        def value(db: Database) -> int:
            return 1

        return value

    first = make(lambda left, right: left == right)
    second = make(lambda left, right: abs(left) == abs(right))
    db = Database()
    first_key, _ = db._query_key(first, (), {})
    second_key, _ = db._query_key(second, (), {})
    assert first_key.identity != second_key.identity


def test_unpinnable_query_policy_capture_is_rejected() -> None:
    def make_policy(limit: int) -> Any:
        mutable_state = [limit]

        def compare(left: int, right: int) -> bool:
            return abs(left - right) <= mutable_state[0]

        return compare

    @query(key="unpinnable-policy", eq=make_policy(1))
    def value(db: Database) -> int:
        return 1

    with pytest.raises(UnsupportedValueError, match="Equality/cutoff policy"):
        Database().get(value)


def test_unpinnable_input_policy_is_rejected_before_registration() -> None:
    mutable_state = [1]

    def compare(left: int, right: int) -> bool:
        return abs(left - right) <= mutable_state[0]

    value = Input[int]("unpinnable-input-policy", eq=compare)
    db = Database()
    before = db.statistics()

    with pytest.raises(UnsupportedValueError, match="Equality/cutoff policy"):
        db.set(value, 1)

    assert db.statistics() == before
    assert db.revision == 0


def test_builtin_method_descriptor_policy_is_fingerprintable() -> None:
    text = Input[str]("descriptor-policy", cutoff=str.strip)
    alias = Input[str]("descriptor-policy", cutoff=str.strip)
    db = Database()

    db.set(text, " value ")
    db.set(alias, "value")

    assert text.read(db) == "value"
    assert db.statistics().input_equal_ignores == 1


def test_callable_and_bound_policy_state_changes_identity() -> None:
    class Policy:
        def __init__(self, limit: int) -> None:
            self.limit = limit

        def __call__(self, left: int, right: int) -> bool:
            return abs(left - right) <= self.limit

        def compare(self, left: int, right: int) -> bool:
            return abs(left - right) <= self.limit

    def template(db: Database) -> int:
        return 1

    callable_policy = Query(template, key="callable-policy", eq=Policy(1))
    changed_callable_policy = Query(template, key="callable-policy", eq=Policy(100))

    first_owner = Policy(1)
    second_owner = Policy(100)

    bound_policy = Query(template, key="bound-policy", eq=first_owner.compare)
    changed_bound_policy = Query(template, key="bound-policy", eq=second_owner.compare)

    db = Database()
    assert db._query_fingerprint(callable_policy) != db._query_fingerprint(changed_callable_policy)
    assert db._query_fingerprint(bound_policy) != db._query_fingerprint(changed_bound_policy)


def test_runtime_build_identity_covers_all_implementation_trust_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Adapter:
        def freeze(self, value: Any, freeze_value: Any) -> Any:
            return freeze_value(value)

        def thaw(self, snapshot: Any, thaw_value: Any) -> Any:
            return thaw_value(snapshot)

    @dataclass(frozen=True)
    class ConstantResource(Resource[str, str, str]):
        def probe(self, key: str) -> str:
            return key

        def load(self, db: Database, key: str) -> str:
            return key

        def label(self, key: str) -> str:
            return key

    @query(key="build-sensitive")
    def value(db: Database) -> int:
        return 1

    db = Database()
    input_value = Input[int]("build-sensitive-input")
    resource = ConstantResource()
    adapter = Adapter()

    monkeypatch.setattr(db, "_runtime_build_payload", lambda: ("build-a",))
    first = (
        db._query_fingerprint(value),
        db._input_policy_digest(input_value),
        db._resource_identity_payload(resource),
        db._adapter_implementation_digest(cast(Any, adapter)),
    )
    monkeypatch.setattr(db, "_runtime_build_payload", lambda: ("build-b",))
    second = (
        db._query_fingerprint(value),
        db._input_policy_digest(input_value),
        db._resource_identity_payload(resource),
        db._adapter_implementation_digest(cast(Any, adapter)),
    )

    assert all(left != right for left, right in zip(first, second, strict=True))


def test_runtime_build_payload_covers_full_release_and_abi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database()
    original = runtime_module._build_runtime_build_payload()
    assert db._runtime_build_payload() == original
    assert tuple(sys.version_info) in original
    flags = next(
        item for item in original if isinstance(item, tuple) and item and item[0] == "flags"
    )
    assert tuple(sys.flags) in flags
    abi = next(item for item in original if isinstance(item, tuple) and item and item[0] == "abi")
    assert struct.calcsize("P") * 8 in abi
    assert sys.version in abi

    original_get = sysconfig.get_config_var

    def changed_config(name: str) -> Any:
        if name == "SOABI":
            return "different-soabi"
        return original_get(name)

    monkeypatch.setattr(sysconfig, "get_config_var", changed_config)
    assert runtime_module._build_runtime_build_payload() != original
    assert db._runtime_build_payload() == original


def test_recursive_query_and_policy_captures_have_finite_identity() -> None:
    def same(left: int, right: int) -> bool:
        return recursive.key == "recursive" and left == right

    @query(key="recursive", eq=same)
    def recursive(db: Database, value: int) -> int:
        if value == 0:
            return 1
        return value * recursive(db, value - 1)

    assert Database().get(recursive, 5) == 120


def test_immutable_capture_can_contain_stable_type_objects() -> None:
    supported_types = (str, int, type(None))

    @query
    def type_names(db: Database) -> tuple[str, ...]:
        return tuple(item.__name__ for item in supported_types)

    assert Database().get(type_names) == ("str", "int", "NoneType")


def test_code_identity_canonically_encodes_slice_constants() -> None:
    def template() -> None:
        return None

    first_fn = FunctionType(
        template.__code__.replace(co_consts=(slice(1, 5, 2),)), globals(), "slice_query"
    )
    second_fn = FunctionType(
        template.__code__.replace(co_consts=(slice(1, 6, 2),)), globals(), "slice_query"
    )
    first = Query(first_fn, key="slice-query")
    second = Query(second_fn, key="slice-query")
    db = Database()
    first_key, _ = db._query_key(first, (), {})
    second_key, _ = db._query_key(second, (), {})
    assert first_key.identity != second_key.identity


def test_code_identity_recurses_into_nested_code_constants() -> None:
    def template(db: Database) -> str:
        def nested() -> str:
            return "alpha"

        return nested()

    nested = next(
        value
        for value in template.__code__.co_consts
        if isinstance(value, CodeType) and value.co_name == "nested"
    )
    changed_nested = nested.replace(
        co_consts=tuple("beta" if value == "alpha" else value for value in nested.co_consts)
    )
    changed_code = template.__code__.replace(
        co_consts=tuple(
            changed_nested if value is nested else value for value in template.__code__.co_consts
        )
    )
    changed = FunctionType(changed_code, globals(), template.__name__)

    first = Query(template, key="nested-code")
    second = Query(changed, key="nested-code")
    db = Database()
    assert db._query_key(first, (), {})[0].identity != db._query_key(second, (), {})[0].identity


def test_query_identity_includes_defaults_and_keyword_defaults() -> None:
    def template(db: Database, value: int = 1, *, scale: int = 2) -> int:
        return value * scale

    first_fn = FunctionType(template.__code__, globals(), template.__name__, (1,))
    first_fn.__kwdefaults__ = {"scale": 2}
    second_fn = FunctionType(template.__code__, globals(), template.__name__, (3,))
    second_fn.__kwdefaults__ = {"scale": 4}
    first = Query(first_fn, key="function-defaults")
    second = Query(second_fn, key="function-defaults")
    db = Database()
    assert db._query_key(first, (), {})[0].identity != db._query_key(second, (), {})[0].identity


def test_non_substitutive_cutoff_keeps_dependents_at_the_earlier_representative() -> None:
    """Pins the documented shape of consistency under a coarse policy.

    A cutoff that declares two values unchanged makes dependents consistent
    modulo that equivalence: they legitimately stay at results computed from
    the earlier representative, while a fresh database starts from the later
    one. Exact-value agreement requires a substitutive policy (condition 3).
    """
    coarse = Input[int]("congruence.value", cutoff=lambda _value: 0)

    @query
    def doubled(db: Database) -> int:
        return coarse.read(db) * 2

    db = Database(mode="checked")
    db.set(coarse, 1)
    assert db.get(doubled) == 2

    db.set(coarse, 2)
    assert db.get(doubled) == 2

    fresh = Database(mode="checked")
    fresh.set(coarse, 2)
    assert fresh.get(doubled) == 4


def test_live_kwdefault_mutation_changes_query_identity_between_requests() -> None:
    @query(key="live-kwdefault-mutation")
    def answer(db: Database, *, value: int = 1) -> int:
        return value

    db = Database()
    assert db.get(answer) == 1

    kwdefaults = answer.fn.__kwdefaults__
    assert kwdefaults is not None
    kwdefaults["value"] = 2
    try:
        fresh = Database()
        assert fresh.get(answer) == 2
        assert db.get(answer) == 2
    finally:
        kwdefaults["value"] = 1


def test_live_closure_cell_rebinding_changes_query_identity_between_requests() -> None:
    def make_query() -> tuple[Query[[], int], Any]:
        value = 1

        def rebind(new: int) -> None:
            nonlocal value
            value = new

        @query(key="live-closure-rebinding")
        def read_value(db: Database) -> int:
            return value

        return read_value, rebind

    read_value, rebind = make_query()
    db = Database()
    assert db.get(read_value) == 1

    rebind(2)
    fresh = Database()
    assert fresh.get(read_value) == 2
    assert db.get(read_value) == 2


_LIVE_GLOBAL_CAPTURE = 1


def test_live_captured_global_rebinding_changes_query_identity_between_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "_LIVE_GLOBAL_CAPTURE", 1)

    @query(key="live-global-rebinding")
    def read_global(db: Database) -> int:
        return _LIVE_GLOBAL_CAPTURE

    db = Database()
    assert db.get(read_global) == 1

    monkeypatch.setattr(module, "_LIVE_GLOBAL_CAPTURE", 2)
    fresh = Database()
    assert fresh.get(read_global) == 2
    assert db.get(read_global) == 2


def test_query_identity_includes_transitively_captured_functions() -> None:
    def make_helper(offset: int) -> Any:
        def helper() -> int:
            return offset

        return helper

    def make_query(helper: Any) -> Query[[], int]:
        @query(key="captured-helper")
        def captured(db: Database) -> int:
            return cast(int, helper())

        return captured

    first = make_query(make_helper(1))
    second = make_query(make_helper(2))
    db = Database()
    assert db._query_key(first, (), {})[0].identity != db._query_key(second, (), {})[0].identity


@pytest.mark.parametrize("shape", ("tuple", "frozenset", "frozen-dataclass"))
def test_query_identity_recurses_through_immutable_capture_shapes(
    shape: str,
) -> None:
    def make(offset: int) -> Query[[], int]:
        def helper() -> int:
            return offset

        if shape == "tuple":
            captured = (helper,)

            @query(key=f"immutable-capture:{shape}")
            def calculated(db: Database) -> int:
                return captured[0]()

        elif shape == "frozenset":
            frozen = frozenset({helper})

            @query(key=f"immutable-capture:{shape}")
            def calculated(db: Database) -> int:
                return next(iter(frozen))()

        else:
            boxed = _ImmutableCaptureBox(helper)

            @query(key=f"immutable-capture:{shape}")
            def calculated(db: Database) -> int:
                return cast(Callable[[], int], boxed.value)()

        return calculated

    first = make(2)
    second = make(3)
    db = Database()

    assert db.get(first) == 2
    assert db.get(second) == 3
    assert db._query_key(first, (), {})[0].identity != db._query_key(second, (), {})[0].identity


def test_query_identity_recurses_through_immutable_function_defaults() -> None:
    def make(offset: int) -> Query[[], int]:
        def helper() -> int:
            return offset

        def calculated(db: Database, captured: tuple[Any, ...] = (helper,)) -> int:
            return cast(Callable[[], int], captured[0])()

        return Query(calculated, key="immutable-default-capture")

    first = make(5)
    second = make(8)
    db = Database()

    assert db.get(first) == 5
    assert db.get(second) == 8
    assert db._query_key(first, (), {})[0].identity != db._query_key(second, (), {})[0].identity


def test_query_identity_and_pinned_walk_recurse_into_tuple_query() -> None:
    def make(value: int) -> tuple[Query[[], int], Query[[], int]]:
        @query(key="tuple-captured-child")
        def child(db: Database) -> int:
            return value

        children = (child,)

        @query(key="tuple-captured-parent")
        def parent(db: Database) -> int:
            return children[0](db)

        return parent, child

    first, first_child = make(13)
    second, _second_child = make(21)
    db = Database()

    assert db.get(first) == 13
    assert db.get(second) == 21
    assert db._query_key(first, (), {})[0].identity != db._query_key(second, (), {})[0].identity
    queries, _resources = db._collect_pinned_capture_objects(first.fn)
    assert queries[first_child.key] is first_child


def test_bound_python_method_identity_pins_function_and_owner() -> None:
    def make(delta: int, base: int) -> Query[[], int]:
        def calculate(owner: _BoundMethodOwner) -> int:
            return owner.base + delta

        bound = MethodType(calculate, _BoundMethodOwner(base))

        @query(key="bound-python-method")
        def calculated(db: Database) -> int:
            return cast(Callable[[], int], bound)()

        return calculated

    original = make(1, 10)
    changed_function = make(2, 10)
    changed_owner = make(1, 20)
    db = Database()

    assert db.get(original) == 11
    assert db.get(changed_function) == 12
    assert db.get(changed_owner) == 21
    identities = {
        db._query_key(candidate, (), {})[0].identity
        for candidate in (original, changed_function, changed_owner)
    }
    assert len(identities) == 3


def test_pinned_capture_walk_recurses_through_immutable_shapes_and_methods() -> None:
    resource = BinaryFileResource()

    @query(key="nested-pinned-child")
    def child(db: Database) -> int:
        return 1

    def pin(owner: _BoundMethodOwner) -> int:
        return owner.base

    bound = MethodType(pin, _BoundMethodOwner(4))
    nested = (
        frozenset({child}),
        _ImmutableCaptureBox(resource),
        bound,
    )

    @query(key="nested-pinned-parent")
    def parent(db: Database) -> int:
        _pin = nested
        return child(db) + cast(Callable[[], int], bound)()

    queries, resources = Database()._collect_pinned_capture_objects(parent.fn)

    assert queries[child.key] is child
    assert resource in resources.values()


def test_query_identity_includes_captured_function_custom_state() -> None:
    def make_query(factor: int) -> Query[[], int]:
        def helper() -> int:
            return cast(int, helper.factor)  # type: ignore[attr-defined]

        helper.factor = factor  # type: ignore[attr-defined]

        @query(key="captured-function-state")
        def calculated(db: Database) -> int:
            return helper()

        return calculated

    first = make_query(2)
    second = make_query(3)
    db = Database()

    assert db.get(first) == 2
    assert db.get(second) == 3
    assert db._query_key(first, (), {})[0].identity != db._query_key(second, (), {})[0].identity


def test_bound_builtin_owner_participates_in_query_identity() -> None:
    def make_query(text: str) -> Query[[], str]:
        uppercase = text.upper

        @query(key="bound-builtin-owner")
        def rendered(db: Database) -> str:
            return uppercase()

        return rendered

    first = make_query("left")
    second = make_query("right")
    db = Database()

    assert db.get(first) == "LEFT"
    assert db.get(second) == "RIGHT"
    assert db._query_key(first, (), {})[0].identity != db._query_key(second, (), {})[0].identity


def test_ambient_nan_payload_bits_participate_in_query_identity() -> None:
    def make_query(bits: str) -> Query[[], bytes]:
        value = struct.unpack(">d", bytes.fromhex(bits))[0]

        @query(key="ambient-nan-bits")
        def packed(db: Database) -> bytes:
            return struct.pack(">d", value)

        return packed

    first = make_query("7ff8000000000001")
    second = make_query("7ff8000000000002")
    db = Database()

    assert db.get(first) == bytes.fromhex("7ff8000000000001")
    assert db.get(second) == bytes.fromhex("7ff8000000000002")
    assert db._query_key(first, (), {})[0].identity != db._query_key(second, (), {})[0].identity


def test_evaluated_generic_annotations_are_fingerprintable() -> None:
    namespace: dict[str, Any] = {"Database": Database}
    exec(
        compile(
            "def raw(db: Database, values: list[int]) -> tuple[int, ...]:\n"
            "    return tuple(values)\n",
            "<evaluated-annotations>",
            "exec",
            dont_inherit=True,
        ),
        namespace,
    )
    raw = cast(FunctionType, namespace["raw"])
    calculated = Query(raw, key="evaluated-annotations")

    assert Database().get(calculated, [1, 2]) == (1, 2)


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 requires Python 3.12")
def test_pep_695_type_parameters_are_fingerprintable() -> None:
    namespace: dict[str, Any] = {"Database": Database}
    exec(
        compile(
            "def raw[T](db: Database, value: T) -> T:\n    return value\n",
            "<pep-695-annotations>",
            "exec",
            dont_inherit=True,
        ),
        namespace,
    )
    raw = cast(FunctionType, namespace["raw"])
    calculated = Query(raw, key="pep-695-annotations")

    assert Database().get(calculated, 5) == 5


def test_dynamic_module_without_stable_source_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("pyinc_dynamic_identity_test")
    module.answer = 1  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)

    @query(key="dynamic-module")
    def answer(db: Database) -> int:
        return cast(int, module.answer)

    with pytest.raises(UnsupportedValueError, match="stable source identity"):
        Database().get(answer)


def test_dynamic_module_cannot_spoof_builtin_or_file_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builtin_spoof = ModuleType("pyinc_spoofed_builtin")
    builtin_spoof.__spec__ = importlib.machinery.ModuleSpec(
        builtin_spoof.__name__, None, origin="built-in"
    )
    monkeypatch.setitem(sys.modules, builtin_spoof.__name__, builtin_spoof)

    with pytest.raises(UnsupportedValueError, match="spoofed built-in"):
        Database()._module_identity_payload(builtin_spoof)

    file_spoof = ModuleType("pyinc_spoofed_file")
    file_spoof.__file__ = runtime_module.__file__
    file_spoof.__spec__ = importlib.machinery.ModuleSpec(
        file_spoof.__name__, None, origin=runtime_module.__file__
    )
    file_spoof.__spec__.has_location = True
    monkeypatch.setitem(sys.modules, file_spoof.__name__, file_spoof)

    with pytest.raises(UnsupportedValueError, match="stable source identity"):
        Database()._module_identity_payload(file_spoof)


def test_source_loader_spec_does_not_erase_captured_module_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("pyinc_callable_source_spoof")
    specification = importlib.machinery.ModuleSpec(
        module.__name__,
        importlib.machinery.SourceFileLoader(module.__name__, runtime_module.__file__),
        origin=runtime_module.__file__,
    )
    specification.has_location = True
    module.__file__ = runtime_module.__file__
    module.__spec__ = specification
    module.__loader__ = specification.loader
    module.__package__ = specification.parent
    vars(module)["__cached__"] = specification.cached
    module.answer = lambda: 1  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)

    @query(key="captured-source-loader-module")
    def read_answer(db: Database) -> int:
        return cast(int, module.answer())

    first = Database()._query_fingerprint(read_answer)
    module.answer = lambda: 2  # type: ignore[attr-defined]
    second = Database()._query_fingerprint(read_answer)

    assert first != second
    assert Database().get(read_answer) == 2


def test_captured_module_pins_reexported_function_and_submodule_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper_name = "pyinc_module_reexport_helper"
    facade_name = "pyinc_module_reexport_facade"
    package_name = "pyinc_module_reexport_package"
    helper_path = tmp_path / f"{helper_name}.py"
    facade_path = tmp_path / f"{facade_name}.py"
    package_path = tmp_path / package_name
    package_path.mkdir()
    helper_path.write_text("def answer():\n    return 1\n", encoding="utf-8")
    facade_path.write_text(f"from {helper_name} import answer\n", encoding="utf-8")
    (package_path / "__init__.py").write_text("from . import sub\n", encoding="utf-8")
    submodule_path = package_path / "sub.py"
    submodule_path.write_text("def answer():\n    return 10\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    facade = importlib.import_module(facade_name)
    package = importlib.import_module(package_name)
    submodule = importlib.import_module(f"{package_name}.sub")

    @query(key="captured-module-reexports")
    def read_answers(db: Database) -> tuple[int, int]:
        return cast(int, facade.answer()), cast(int, package.sub.answer())

    store = InMemoryArtifactStore()
    writer = Database(store=store)
    assert writer.get(read_answers) == (1, 10)
    checkpoint = writer.save_checkpoint()

    helper_path.write_text("def answer():\n    return 2\n", encoding="utf-8")
    submodule_path.write_text("def answer():\n    return 20\n", encoding="utf-8")
    importlib.invalidate_caches()
    importlib.reload(sys.modules[helper_name])
    importlib.reload(facade)
    importlib.reload(submodule)

    reader = Database(store=store)
    reader.load_checkpoint(checkpoint)
    assert reader.get(read_answers) == (2, 20)
    assert reader.inspect(read_answers).last_recompute == "executed"

    for module_name in (
        f"{package_name}.sub",
        package_name,
        facade_name,
        helper_name,
    ):
        sys.modules.pop(module_name, None)


def test_source_pinned_module_function_keeps_safe_transitive_globals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper_name = "pyinc_source_pinned_helper"
    facade_name = "pyinc_source_pinned_facade"
    helper_path = tmp_path / f"{helper_name}.py"
    facade_path = tmp_path / f"{facade_name}.py"
    helper_path.write_text("def answer():\n    return 1\n", encoding="utf-8")
    facade_path.write_text(
        f"from {helper_name} import answer\n"
        "STATE = {}\n"
        "def indirect():\n"
        "    return answer() + len(STATE)\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    facade = importlib.import_module(facade_name)

    @query(key="source-pinned-transitive-global")
    def read_answer(db: Database) -> int:
        return cast(int, facade.indirect())

    store = InMemoryArtifactStore()
    writer = Database(store=store)
    assert writer.get(read_answer) == 1
    assert read_answer not in writer._query_fingerprint_memo
    checkpoint = writer.save_checkpoint()

    helper_path.write_text("def answer():\n    return 2\n", encoding="utf-8")
    importlib.invalidate_caches()
    importlib.reload(sys.modules[helper_name])
    importlib.reload(facade)

    assert writer.get(read_answer) == 2

    reader = Database(store=store)
    reader.load_checkpoint(checkpoint)
    assert reader.get(read_answer) == 2
    assert reader.inspect(read_answer).last_recompute == "executed"

    sys.modules.pop(facade_name, None)
    sys.modules.pop(helper_name, None)


def test_source_pinned_module_function_keeps_imported_mutable_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper_name = "pyinc_source_pinned_state_helper"
    facade_name = "pyinc_source_pinned_state_facade"
    helper_path = tmp_path / f"{helper_name}.py"
    facade_path = tmp_path / f"{facade_name}.py"
    helper_path.write_text("STATE = {'answer': 1}\n", encoding="utf-8")
    facade_path.write_text(
        f"from {helper_name} import STATE\ndef indirect():\n    return STATE['answer']\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    facade = importlib.import_module(facade_name)

    @query(key="source-pinned-imported-state")
    def read_answer(db: Database) -> int:
        return cast(int, facade.indirect())

    store = InMemoryArtifactStore()
    writer = Database(store=store)
    assert writer.get(read_answer) == 1
    checkpoint = writer.save_checkpoint()

    helper_path.write_text("STATE = {'answer': 2}\n", encoding="utf-8")
    importlib.invalidate_caches()
    importlib.reload(sys.modules[helper_name])
    importlib.reload(facade)

    reader = Database(store=store)
    reader.load_checkpoint(checkpoint)
    assert reader.get(read_answer) == 2
    assert reader.inspect(read_answer).last_recompute == "executed"

    sys.modules.pop(facade_name, None)
    sys.modules.pop(helper_name, None)


def test_module_identity_hashes_compiled_file_bytes_even_when_stat_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "extension.bin"
    path.write_bytes(b"first-payload")
    original_stat = path.stat()
    module = ModuleType("pyinc_compiled_identity_test")
    module.__file__ = str(path)
    specification = importlib.machinery.ModuleSpec(
        module.__name__,
        importlib.machinery.SourceFileLoader(module.__name__, str(path)),
        origin=str(path),
    )
    specification.has_location = True
    module.__spec__ = specification
    module.__loader__ = specification.loader
    module.__package__ = specification.parent
    vars(module)["__cached__"] = specification.cached
    monkeypatch.setitem(sys.modules, module.__name__, module)
    db = Database()

    first = db._module_identity_payload(module)
    path.write_bytes(b"other-payload")
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    second = db._module_identity_payload(module)

    assert path.stat().st_size == original_stat.st_size
    assert path.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert first != second


def test_module_identity_observes_rewritten_bytes_when_stat_identity_collides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A same-size rewrite can land inside one timestamp granule, leaving size,
    # mtime, ctime, device, and inode all unchanged. Freeze the stat answer for
    # this path to make that collision deterministic; the identity must come
    # from the bytes.
    path = tmp_path / "extension.bin"
    path.write_bytes(b"first-payload")
    frozen_stat = os.stat(path)
    real_stat = os.stat

    def stat_with_frozen_target(
        target: Any, *args: Any, **kwargs: Any
    ) -> os.stat_result:
        if isinstance(target, (str, os.PathLike)) and os.fspath(target) == str(path):
            return frozen_stat
        return real_stat(target, *args, **kwargs)

    module = ModuleType("pyinc_stat_collision_identity_test")
    module.__file__ = str(path)
    specification = importlib.machinery.ModuleSpec(
        module.__name__,
        importlib.machinery.SourceFileLoader(module.__name__, str(path)),
        origin=str(path),
    )
    specification.has_location = True
    module.__spec__ = specification
    module.__loader__ = specification.loader
    module.__package__ = specification.parent
    vars(module)["__cached__"] = specification.cached
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(os, "stat", stat_with_frozen_target)
    db = Database()

    first = db._module_identity_payload(module)
    path.write_bytes(b"other-payload")
    second = db._module_identity_payload(module)

    assert first != second


def test_frozen_local_dataclass_capture_is_rejected_instead_of_erasing_behavior() -> None:
    def make_query(multiplier: int) -> Query[[], int]:
        @dataclass(frozen=True)
        class Config:
            value: int

            def calculate(self) -> int:
                return self.value * multiplier

        config = Config(2)

        @query(key="local-frozen-config")
        def calculated(db: Database) -> int:
            return config.calculate()

        return calculated

    db = Database()
    with pytest.raises(UnsupportedValueError, match="unsupported ambient value"):
        db.get(make_query(2))
    with pytest.raises(UnsupportedValueError, match="unsupported ambient value"):
        db.get(make_query(3))
    assert db.statistics().query_count == 0


def test_local_type_capture_is_rejected_instead_of_colliding() -> None:
    def make_query(result: int) -> Query[[], int]:
        class Helper:
            @staticmethod
            def calculate() -> int:
                return result

        @query(key="captured-local-type")
        def calculated(db: Database) -> int:
            return Helper.calculate()

        return calculated

    first = make_query(1)
    second = make_query(2)
    db = Database()

    with pytest.raises(UnsupportedValueError, match="Captured local type"):
        db.get(first)
    with pytest.raises(UnsupportedValueError, match="Captured local type"):
        db.get(second)
    assert db.statistics().query_count == 0


def test_module_type_identity_pins_imported_base_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper_name = "pyinc_identity_helper_fixture"
    base_name = "pyinc_identity_base_fixture"
    config_name = "pyinc_identity_config_fixture"
    helper_path = tmp_path / f"{helper_name}.py"
    base_path = tmp_path / f"{base_name}.py"
    config_path = tmp_path / f"{config_name}.py"
    helper_path.write_text("def helper(value):\n    return value * 2\n")
    base_path.write_text(
        f"from {helper_name} import helper\n"
        "class Base:\n"
        "    def score(self, value):\n"
        "        return helper(value)\n"
    )
    config_path.write_text(f"from {base_name} import Base\nclass Config(Base):\n    pass\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)

    def purge() -> None:
        importlib.invalidate_caches()
        for module_name in (config_name, base_name, helper_name):
            sys.modules.pop(module_name, None)

    def load_config() -> type[Any]:
        purge()
        module = importlib.import_module(config_name)
        return cast(type[Any], module.Config)

    def make_query(config_type: type[Any]) -> Query[[], int]:
        @query(key="module-type-transitivity")
        def score(db: Database) -> int:
            return cast(int, config_type().score(2))

        return score

    try:
        first = make_query(load_config())
        db = Database()
        assert db.get(first) == 4
        first_identity = next(iter(db._query_records)).identity

        helper_path.write_text("def helper(value):\n    return value * 3\n")
        second = make_query(load_config())

        assert db.get(second) == 6
        assert Database().get(second) == 6
        assert first_identity != db._query_key(second, (), {})[0].identity
    finally:
        purge()


def test_set_many_failure_leaves_all_database_state_unchanged() -> None:
    first = Input[int]("first", eq=lambda left, right: True)

    def explode(left: int, right: int) -> bool:
        raise RuntimeError("comparator failed")

    second = Input[int]("second", eq=explode)
    db = Database()
    db.set(first, 1)
    db.set(second, 2)
    before_revision = db.revision
    before_statistics = db.statistics()

    with pytest.raises(RuntimeError, match="comparator failed"):
        db.set_many(((first, 99), (second, 100)))

    assert first.read(db) == 1
    assert second.read(db) == 2
    assert db.revision == before_revision
    assert db.statistics() == before_statistics


def test_set_many_rejects_duplicate_keys_before_mutating() -> None:
    value = Input[int]("value")
    db = Database()
    with pytest.raises(InputKeyError, match="duplicate"):
        db.set_many(((value, 1), (value, 2)))
    assert db.revision == 0
    assert db.statistics().node_count == 0


def test_caught_query_failure_does_not_publish_a_dependency_edge() -> None:
    @query
    def failing(db: Database) -> int:
        raise RuntimeError("boom")

    @query
    def catches_failure(db: Database) -> int:
        try:
            return failing(db)
        except RuntimeError:
            return 7

    db = Database()
    assert db.get(catches_failure) == 7
    assert db.inspect(catches_failure).dependencies == ()
    assert all("failing" not in key.label for key in db._call_snapshot_registry)
    assert all(query_obj is not failing for query_obj in db._query_registry.values())


def test_query_catching_its_own_cycle_keeps_committed_registries() -> None:
    @query
    def catches_cycle(db: Database) -> int:
        try:
            return catches_cycle(db)
        except CycleError:
            return 1

    db = Database()
    assert db.get(catches_cycle) == 1
    key = next(iter(db._query_records))
    assert key in db._records
    assert key in db._call_snapshot_registry
    assert db._query_registry[key.identity] is catches_cycle
    assert db.get(catches_cycle) == 1
    assert db.inspect(catches_cycle).last_decision == "reused"


def test_query_labels_do_not_call_argument_repr() -> None:
    @dataclass(frozen=True)
    class HostileRepr:
        value: int

        def __repr__(self) -> str:
            raise AssertionError("repr must not be called")

    @query
    def extract(db: Database, argument: HostileRepr) -> int:
        return cast(int, argument["value"])  # type: ignore[index]

    db = Database(mode="strict")
    assert db.get(extract, HostileRepr(3)) == 3
    label = db.inspect(extract, HostileRepr(3)).label
    assert extract.key in label
    assert "HostileRepr" not in label


def test_high_cardinality_query_state_stays_bounded_by_lru_limit() -> None:
    @query
    def identity(db: Database, value: int) -> int:
        return value

    limit = 16
    db = Database(max_query_nodes=limit)
    for value in range(1_000):
        assert db.get(identity, value) == value

    assert db.statistics().query_count == limit
    assert len(db._call_snapshot_registry) == limit
    assert len(db._query_timings) == limit
    assert len(db.query_profile()) == limit
    assert len(db._query_registry) == 1
    assert len(db._query_fingerprint_memo) == 1
    assert all(not isinstance(timing, list) for timing in db._query_timings.values())


def test_memoized_fingerprint_tracks_function_docstrings() -> None:
    @query(key="memo-fn-doc")
    def documented(db: Database) -> int:
        """Original docstring."""
        return 1

    db = Database()
    db._query_fingerprint(documented)
    documented.fn.__doc__ = "Changed docstring."
    memoized, truth = _memo_and_truth(db, documented)
    assert memoized == truth
    assert memoized == Database()._query_fingerprint(documented)


def test_memoized_fingerprint_tracks_in_place_annotation_mutation() -> None:
    @query(key="memo-fn-annotations")
    def annotated(db: Database, value: int) -> int:
        return value

    db = Database()
    db._query_fingerprint(annotated)
    annotated.fn.__annotations__["value"] = str
    memoized, truth = _memo_and_truth(db, annotated)
    assert memoized == truth
    assert memoized == Database()._query_fingerprint(annotated)


def test_memoized_fingerprint_tracks_name_and_type_params() -> None:
    @query(key="memo-fn-name")
    def named(db: Database) -> int:
        return 1

    db = Database()
    db._query_fingerprint(named)
    named.fn.__name__ = "renamed"
    memoized, truth = _memo_and_truth(db, named)
    assert memoized == truth

    db._query_fingerprint(named)
    named.fn.__qualname__ = "renamed_qualname"
    memoized, truth = _memo_and_truth(db, named)
    assert memoized == truth

    db._query_fingerprint(named)
    named.fn.__module__ = "renamed_module"
    memoized, truth = _memo_and_truth(db, named)
    assert memoized == truth

    db._query_fingerprint(named)
    cast(Any, named.fn).__type_params__ = (TypeVar("T"),)
    memoized, truth = _memo_and_truth(db, named)
    assert memoized == truth


def test_memoized_fingerprint_tracks_rebound_lazy_annotate() -> None:
    def make_evaluator(marker: int) -> Any:
        def __annotate__(format: int) -> dict[str, Any]:
            raise NameError(f"deferred {marker}")

        return __annotate__

    @query(key="memo-fn-annotate")
    def lazy(db: Database) -> int:
        return 1

    cast(Any, lazy.fn).__annotate__ = make_evaluator(1)
    db = Database()
    db._query_fingerprint(lazy)
    cast(Any, lazy.fn).__annotate__ = make_evaluator(2)
    memoized, truth = _memo_and_truth(db, lazy)
    assert memoized == truth


def test_memoized_fingerprint_tracks_captured_class_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @query(key="memo-class-attr")
    def scaled(db: Database) -> int:
        return _ObservedConsts.SCALE + 0

    db = Database()
    db._query_fingerprint(scaled)
    monkeypatch.setattr(_ObservedConsts, "SCALE", 3)
    memoized, truth = _memo_and_truth(db, scaled)
    assert memoized == truth
    assert memoized == Database()._query_fingerprint(scaled)


def test_memoized_fingerprint_tracks_method_rebinding_on_a_captured_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @query(key="memo-class-method")
    def computed(db: Database) -> int:
        return _ObservedPlain.compute(1)

    db = Database()
    db._query_fingerprint(computed)
    monkeypatch.setattr(_ObservedPlain, "compute", staticmethod(_observed_compute_two))
    memoized, truth = _memo_and_truth(db, computed)
    assert memoized == truth
    assert memoized == Database()._query_fingerprint(computed)


def test_memoized_fingerprint_tracks_frozen_dataclass_field_writes() -> None:
    @query(key="memo-dataclass-field")
    def boxed(db: Database) -> int:
        return _observed_box.factor

    db = Database()
    db._query_fingerprint(boxed)
    original = _observed_box.factor
    object.__setattr__(_observed_box, "factor", original + 1)
    try:
        memoized, truth = _memo_and_truth(db, boxed)
        assert memoized == truth
        assert memoized == Database()._query_fingerprint(boxed)
    finally:
        object.__setattr__(_observed_box, "factor", original)


def test_memoized_fingerprint_tracks_frozen_dataclass_defaults() -> None:
    @query(key="memo-dataclass-default")
    def defaulted(db: Database, box: _ObservedBox = _observed_box) -> int:
        return box.factor

    db = Database()
    db._query_fingerprint(defaulted)
    original = _observed_box.factor
    object.__setattr__(_observed_box, "factor", original + 5)
    try:
        memoized, truth = _memo_and_truth(db, defaulted)
        assert memoized == truth
    finally:
        object.__setattr__(_observed_box, "factor", original)


def test_memoized_fingerprint_tracks_frozen_dataclass_kwdefaults() -> None:
    @query(key="memo-dataclass-kwdefault")
    def defaulted(db: Database, *, box: _ObservedBox = _observed_box) -> int:
        return box.factor

    db = Database()
    db._query_fingerprint(defaulted)
    original = _observed_box.factor
    object.__setattr__(_observed_box, "factor", original + 7)
    try:
        memoized, truth = _memo_and_truth(db, defaulted)
        assert memoized == truth
    finally:
        object.__setattr__(_observed_box, "factor", original)


def test_memoized_fingerprint_reuses_the_memo_for_an_unchanged_definition() -> None:
    @query(key="memo-warm-reuse")
    def scaled(db: Database, kind: Any = _observed_alias) -> int:
        return _ObservedConsts.SCALE + _observed_box.factor + _ObservedPlain.compute(1)

    db = Database()
    first = db._query_fingerprint(scaled)
    entry = db._query_fingerprint_memo[scaled]
    assert db._query_fingerprint(scaled) == first
    # A recompute stores a freshly built memo entry, so entry identity tells a
    # reused observation apart from one that merely recomputed the same digest.
    assert db._query_fingerprint_memo[scaled] is entry


def test_memoized_fingerprint_tracks_annotation_class_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @query(key="memo-annotation-class")
    def annotated(db: Database, value: int) -> int:
        return value

    annotated.fn.__annotations__["value"] = _ObservedConsts
    db = Database()
    db._query_fingerprint(annotated)
    monkeypatch.setattr(_ObservedConsts, "SCALE", 4)
    memoized, truth = _memo_and_truth(db, annotated)
    assert memoized == truth
    assert memoized == Database()._query_fingerprint(annotated)


def test_memoized_fingerprint_tracks_input_policy_object_state() -> None:
    tolerant = Input[int]("memo-policy-input", eq=_CountingEq(0))

    @query(key="memo-policy-query")
    def read_tolerant(db: Database) -> int:
        return tolerant.read(db)

    db = Database()
    db.set(tolerant, 1)
    db._query_fingerprint(read_tolerant)
    cast(_CountingEq, tolerant.eq).tolerance = 5
    memoized, truth = _memo_and_truth(db, read_tolerant)
    assert memoized == truth
    assert memoized == Database()._query_fingerprint(read_tolerant)


def test_memoized_fingerprint_tracks_state_inside_a_captured_named_tuple() -> None:
    @query(key="memo-named-tuple")
    def paired(db: Database) -> int:
        return _observed_pair.box.factor

    db = Database()
    db._query_fingerprint(paired)
    original = _observed_box.factor
    object.__setattr__(_observed_box, "factor", original + 3)
    try:
        memoized, truth = _memo_and_truth(db, paired)
        assert memoized == truth
        assert memoized == Database()._query_fingerprint(paired)
    finally:
        object.__setattr__(_observed_box, "factor", original)


def test_memoized_fingerprint_tracks_state_inside_captured_tuples_and_frozensets() -> None:
    @query(key="memo-tuple-and-frozenset")
    def collected(db: Database) -> int:
        return _observed_plain[0].factor + len(_observed_members)

    db = Database()
    db._query_fingerprint(collected)
    original = _observed_box.factor
    object.__setattr__(_observed_box, "factor", original + 4)
    try:
        memoized, truth = _memo_and_truth(db, collected)
        assert memoized == truth
        assert memoized == Database()._query_fingerprint(collected)
    finally:
        object.__setattr__(_observed_box, "factor", original)


def _import_alias_helper_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module_name: str
) -> Any:
    """Import a real module whose alias and type parameter hold one class."""

    (tmp_path / f"{module_name}.py").write_text(
        "class Target:\n"
        "    SCALE = 2\n"
        "\n"
        "\n"
        "type Alias = Target\n"
        "\n"
        "\n"
        "def bounded[T: Target](value: T) -> T:\n"
        "    return value\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    return importlib.import_module(module_name)


@pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="type-alias and type-parameter syntax require Python 3.12",
)
def test_memoized_fingerprint_tracks_type_alias_and_type_parameter_annotations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_annotation_shape_helper"
    helper = _import_alias_helper_module(tmp_path, monkeypatch, module_name)
    try:

        @query(key="memo-annotation-alias")
        def aliased(db: Database, value: Any = None) -> int:
            return 1

        @query(key="memo-annotation-bound")
        def parameterized(db: Database) -> int:
            return 1

        # An alias and a type-parameter bound both keep their content behind
        # the annotated module's class, reached through the evaluators the
        # fingerprint folds rather than through the alias object itself.
        aliased.fn.__annotations__["value"] = helper.Alias
        cast(Any, parameterized.fn).__type_params__ = helper.bounded.__type_params__

        db = Database()
        db._query_fingerprint(aliased)
        db._query_fingerprint(parameterized)
        monkeypatch.setattr(helper.Target, "SCALE", 3)
        for target in (aliased, parameterized):
            memoized, truth = _memo_and_truth(db, target)
            assert memoized == truth
            assert memoized == Database()._query_fingerprint(target)
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="type-alias and type-parameter syntax require Python 3.12",
)
def test_memoized_fingerprint_tracks_an_alias_reached_from_body_and_annotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_annotation_shape_two_slots"
    helper = _import_alias_helper_module(tmp_path, monkeypatch, module_name)
    try:
        alias = helper.Alias

        @query(key="memo-alias-two-slots")
        def doubled(db: Database, value: Any = None) -> int:
            return len(str(alias))

        # The body reaches the alias before the annotation entry does, so the
        # first slot to arrive has to be the one that folds it; a shallow
        # first look would leave the second slot with nothing left to observe.
        doubled.fn.__annotations__["value"] = alias
        db = Database()
        db._query_fingerprint(doubled)
        monkeypatch.setattr(helper.Target, "SCALE", 3)
        memoized, truth = _memo_and_truth(db, doubled)
        assert memoized == truth
        assert memoized == Database()._query_fingerprint(doubled)
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="type-alias and type-parameter syntax require Python 3.12",
)
def test_memoized_fingerprint_tracks_alias_and_type_parameter_captures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_annotation_shape_captures"
    helper = _import_alias_helper_module(tmp_path, monkeypatch, module_name)
    try:
        alias = helper.Alias
        parameter = helper.bounded.__type_params__[0]

        # Neither query annotates with these objects: they are ordinary
        # captures, which the fingerprint folds through the same evaluators.
        @query(key="memo-alias-capture")
        def captures_alias(db: Database) -> int:
            return len(str(alias))

        @query(key="memo-parameter-capture")
        def captures_parameter(db: Database) -> int:
            return len(str(parameter))

        db = Database()
        db._query_fingerprint(captures_alias)
        db._query_fingerprint(captures_parameter)
        monkeypatch.setattr(helper.Target, "SCALE", 3)
        for target in (captures_alias, captures_parameter):
            memoized, truth = _memo_and_truth(db, target)
            assert memoized == truth
            assert memoized == Database()._query_fingerprint(target)
    finally:
        sys.modules.pop(module_name, None)


def test_memoized_fingerprint_tracks_annotation_types_a_reflecting_body_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @query(key="memo-annotation-reflected-type")
    def reflected(db: Database, store: Any = None) -> int:
        return len(_ObservedConsts.__annotations__)

    # A body that reads annotations back makes the fingerprint fold each
    # annotated value as an ambient capture, which pins the annotated type's
    # namespace; without that read the same type is pinned by module anchor
    # alone, and an added attribute would move neither side.
    reflected.fn.__annotations__["store"] = InMemoryArtifactStore
    db = Database()
    db._query_fingerprint(reflected)
    monkeypatch.setattr(InMemoryArtifactStore, "_observed_marker", 1, raising=False)
    memoized, truth = _memo_and_truth(db, reflected)
    assert memoized == truth
    assert memoized == Database()._query_fingerprint(reflected)


def test_memoized_fingerprint_tracks_instance_state_a_reflecting_body_reads() -> None:
    @query(key="memo-annotation-reflected-instance")
    def reflected(db: Database, box: Any = None) -> int:
        return len(_ObservedConsts.__annotations__)

    # The instance counterpart of the type case above. This shape exists only
    # on the reflecting side of that switch: without the read, an annotated
    # object is refused outright rather than folded field by field.
    reflected.fn.__annotations__["box"] = _observed_box
    db = Database()
    db._query_fingerprint(reflected)
    original = _observed_box.factor
    object.__setattr__(_observed_box, "factor", original + 6)
    try:
        memoized, truth = _memo_and_truth(db, reflected)
        assert memoized == truth
        assert memoized == Database()._query_fingerprint(reflected)
    finally:
        object.__setattr__(_observed_box, "factor", original)


def test_memoized_fingerprint_tracks_a_resource_shaped_class_in_a_captured_class_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @query(key="memo-resource-shaped-class")
    def shaped(db: Database) -> int:
        return _ObservedShapeHolder.nested.MARKER

    # A class answers the label/probe/load probes that recognize a resource
    # handle exactly as an instance does, but a class reached inside a
    # captured class body is folded through the type payload and never
    # through a resource identity, so the observation has to descend it.
    db = Database()
    db._query_fingerprint(shaped)
    monkeypatch.setattr(_ObservedResourceShape, "MARKER", 2)
    memoized, truth = _memo_and_truth(db, shaped)
    assert memoized == truth
    assert memoized == Database()._query_fingerprint(shaped)


def test_memoized_fingerprint_tracks_resource_configuration_folded_as_capture_state() -> None:
    @query(key="memo-resource-class-body-state")
    def configured(db: Database) -> int:
        return _ObservedResourceHolder.nested.scale

    # Reached inside a captured class body, a resource is folded field by
    # field like any other frozen dataclass and its identity() never runs, so
    # this query stays memoized and the memo has to see the write.
    db = Database()
    db._query_fingerprint(configured)
    nested = _ObservedResourceHolder.nested
    object.__setattr__(nested, "scale", 5)
    try:
        memoized, truth = _memo_and_truth(db, configured)
        assert memoized == truth
        assert memoized == Database()._query_fingerprint(configured)
    finally:
        object.__setattr__(nested, "scale", 2)


def test_resource_capturing_fingerprints_track_reconfiguration() -> None:
    class ScaledResource(Resource[int, int, int]):
        def __init__(self, scale: int) -> None:
            # The configuration lives in a list, so rebinding nothing and
            # writing into it in place leaves every reference the observation
            # pins identical. Holding it in a plain attribute would let the
            # observation catch the write and prove nothing about the digest.
            self.parts = [scale]

        def identity(self) -> tuple[str, tuple[int, ...]]:
            return ("scaled-resource", tuple(self.parts))

        def label(self, key: int) -> str:
            return f"scaled[{key}]"

        def probe(self, key: int) -> int:
            return self.parts[0]

        def load(self, db: Database, key: int) -> int:
            return self.parts[0] * key

    resource = ScaledResource(2)

    @query(key="memo-resource-config")
    def scaled(db: Database) -> int:
        return db.read_resource(resource, 10)

    db = Database()
    assert db.get(scaled) == 20
    assert scaled in db._query_fingerprint_memo

    db._query_fingerprint(scaled)
    resource.parts[0] = 3
    # identity() hands back a fresh object every call, so its value cannot be
    # pinned by reference, and this in-place write moves nothing the
    # observation holds. The memo carries a digest of the configuration the
    # fold read instead, and re-reading it is what catches the change.
    memoized, truth = _memo_and_truth(db, scaled)
    assert memoized == truth
    assert memoized == Database()._query_fingerprint(scaled)
    warm = db.get(scaled)
    fresh = Database().get(scaled)
    assert warm == fresh == 30


def test_memoized_fingerprint_reuses_the_memo_for_an_unchanged_resource_query() -> None:
    class SteadyResource(Resource[int, int, int]):
        def __init__(self, scale: int) -> None:
            self.parts = [scale]

        def identity(self) -> tuple[str, tuple[int, ...]]:
            return ("steady-resource", tuple(self.parts))

        def label(self, key: int) -> str:
            return f"steady[{key}]"

        def probe(self, key: int) -> int:
            return self.parts[0]

        def load(self, db: Database, key: int) -> int:
            return self.parts[0] * key

    resource = SteadyResource(2)

    @query(key="memo-resource-steady")
    def steady(db: Database) -> int:
        return db.read_resource(resource, 10)

    db = Database()
    first = db._query_fingerprint(steady)
    entry = db._query_fingerprint_memo[steady]
    assert db._query_fingerprint(steady) == first
    # The counterpart of the capture-free guard, for the one shape whose memo
    # is gated by a re-read rather than by a reference: a recompute stores a
    # freshly built entry, so entry identity separates a served memo from one
    # that recomputed the same digest. A configuration digest that failed to
    # reproduce itself would recompute here while every coherence pin stayed
    # green.
    assert db._query_fingerprint_memo[steady] is entry


def test_memoized_fingerprint_tracks_rebound_module_attributes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_rebound_attribute_module"
    (tmp_path / f"{module_name}.py").write_text("SCALE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key="memo-module-attribute")
        def scaled(db: Database) -> int:
            return cast(int, module.SCALE) * 10

        db = Database()
        db._query_fingerprint(scaled)
        monkeypatch.setattr(module, "SCALE", 5)
        memoized, truth = _memo_and_truth(db, scaled)
        assert memoized == truth
        assert memoized == Database()._query_fingerprint(scaled)
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_rebound_module_attribute_matches_fresh(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = f"pyinc_rebound_attribute_{mode}"
    (tmp_path / f"{module_name}.py").write_text("SCALE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key=f"module-attribute-fsc-{mode}")
        def scaled(db: Database) -> int:
            return cast(int, module.SCALE) * 10

        db = Database(mode=mode)
        assert db.get(scaled) == 10
        monkeypatch.setattr(module, "SCALE", 5)
        executions = db.statistics().query_executions
        warm = db.get(scaled)
        fresh = Database(mode=mode).get(scaled)
        assert warm == fresh == 50
        assert db.statistics().query_executions == executions + 1
    finally:
        sys.modules.pop(module_name, None)


def test_memoized_fingerprint_reuses_the_memo_for_an_unchanged_module_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_unchanged_attribute_module"
    (tmp_path / f"{module_name}.py").write_text(
        "SCALE = 1\ndef helper():\n    return 2\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key="memo-module-attribute-steady")
        def scaled(db: Database) -> int:
            return cast(int, module.SCALE) * cast(int, module.helper())

        db = Database()
        first = db._query_fingerprint(scaled)
        entry = db._query_fingerprint_memo[scaled]
        assert db._query_fingerprint(scaled) == first
        # The counterpart of the coherence pins above, for the guard arms that
        # re-resolve attribute chains, observe the function one of them reaches
        # and re-derive the module constants: any of them answering with a
        # fresh object every call would recompute here with every coherence pin
        # still green. A recompute stores a newly built entry, so entry
        # identity separates a served memo from one that rebuilt the same
        # digest.
        assert db._query_fingerprint_memo[scaled] is entry
    finally:
        sys.modules.pop(module_name, None)


def test_memoized_fingerprint_tracks_functions_behind_a_captured_staticmethod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @query(key="memo-descriptor-static")
    def read(db: Database) -> int:
        return _ObservedStaticHolder.read()

    db = Database()
    db._query_fingerprint(read)
    # The class body payload unwraps the descriptor and folds the function
    # inside it, globals and all. The descriptor object itself is untouched by
    # this rebinding, so an observation that stops at the wrapper sees nothing.
    monkeypatch.setattr(
        sys.modules[__name__], "_observed_static_source", _observed_descriptor_replacement
    )
    memoized, truth = _memo_and_truth(db, read)
    assert memoized == truth
    assert memoized == Database()._query_fingerprint(read)


def test_memoized_fingerprint_tracks_functions_behind_a_captured_classmethod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @query(key="memo-descriptor-class")
    def read(db: Database) -> int:
        return _ObservedClassHolder.read()

    db = Database()
    db._query_fingerprint(read)
    monkeypatch.setattr(
        sys.modules[__name__], "_observed_class_source", _observed_descriptor_replacement
    )
    memoized, truth = _memo_and_truth(db, read)
    assert memoized == truth
    assert memoized == Database()._query_fingerprint(read)


def test_memoized_fingerprint_tracks_functions_behind_a_captured_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @query(key="memo-descriptor-property")
    def read(db: Database) -> int:
        return _ObservedPropertyHolder().read

    db = Database()
    db._query_fingerprint(read)
    monkeypatch.setattr(
        sys.modules[__name__], "_observed_property_source", _observed_descriptor_replacement
    )
    memoized, truth = _memo_and_truth(db, read)
    assert memoized == truth
    assert memoized == Database()._query_fingerprint(read)


def test_memoized_fingerprint_tracks_constants_outside_the_captured_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_offchain_constant_module"
    (tmp_path / f"{module_name}.py").write_text("SCALE = 1\nOTHER = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key="memo-offchain-constant")
        def scaled(db: Database) -> int:
            return cast(int, module.SCALE) * 10

        db = Database()
        db._query_fingerprint(scaled)
        # OTHER sits on no access path, so no chain re-resolves it. The module
        # identity payload folds every stable constant the namespace holds, so
        # the write moves the fingerprint and only the module stamp can see it.
        monkeypatch.setattr(module, "OTHER", 9)
        memoized, truth = _memo_and_truth(db, scaled)
        assert memoized == truth
        assert memoized == Database()._query_fingerprint(scaled)
    finally:
        sys.modules.pop(module_name, None)


def test_memoized_fingerprint_tracks_constants_on_a_captured_stdlib_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @query(key="memo-stdlib-constant")
    def letters(db: Database) -> int:
        return len(string.ascii_lowercase)

    db = Database()
    db._query_fingerprint(letters)
    # A stdlib capture folds paths rather than the values behind them, but its
    # module identity payload still carries the namespace constants.
    monkeypatch.setattr(string, "PYINC_PROBE_CONSTANT", 5, raising=False)
    memoized, truth = _memo_and_truth(db, letters)
    assert memoized == truth
    assert memoized == Database()._query_fingerprint(letters)


def test_memoized_fingerprint_tracks_functions_a_captured_module_function_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_module_function_dependency"
    replacement_name = "pyinc_module_function_replacement"
    (tmp_path / f"{module_name}.py").write_text(
        "def inner():\n    return 3\n\n\ndef helper():\n    return inner() * 10\n",
        encoding="utf-8",
    )
    (tmp_path / f"{replacement_name}.py").write_text(
        "def inner():\n    return 4\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    replacement = importlib.import_module(replacement_name)
    try:

        @query(key="memo-module-function-global")
        def scaled(db: Database) -> int:
            return cast(int, module.helper())

        db = Database()
        db._query_fingerprint(scaled)
        # The chain names helper, whose identity this rebinding leaves alone,
        # and inner is a function rather than a constant, so neither the
        # re-resolved target nor the module stamp moves. What the fingerprint
        # folded is helper's globals, which is what the memo has to observe.
        monkeypatch.setattr(module, "inner", replacement.inner)
        memoized, truth = _memo_and_truth(db, scaled)
        assert memoized == truth
        assert memoized == Database()._query_fingerprint(scaled)
    finally:
        sys.modules.pop(module_name, None)
        sys.modules.pop(replacement_name, None)


def test_memoized_fingerprint_tracks_a_chain_landed_inputs_policy_globals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_module_input_policy"
    (tmp_path / f"{module_name}.py").write_text(
        "from pyinc import Input\n"
        "\n"
        "\n"
        "def tolerance() -> int:\n"
        "    return 3\n"
        "\n"
        "\n"
        "def wider_tolerance() -> int:\n"
        "    return 99\n"
        "\n"
        "\n"
        "def near(a: int, b: int) -> bool:\n"
        "    return abs(a - b) <= tolerance()\n"
        "\n"
        "\n"
        'READING = Input[int]("module-input-policy", eq=near)\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key="memo-module-input-policy")
        def reading(db: Database) -> int:
            return cast(int, module.READING.read(db))

        db = Database()
        db.set(module.READING, 1)
        db._query_fingerprint(reading)
        # The chain lands on an Input, whose eq policy is folded as a
        # definition: the policy's globals are read live while the Input the
        # chain resolves to holds still, and a function is not a constant the
        # module stamp carries.
        monkeypatch.setattr(module, "tolerance", module.wider_tolerance)
        memoized, truth = _memo_and_truth(db, reading)
        assert memoized == truth
        assert memoized == Database()._query_fingerprint(reading)
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="type-alias and type-parameter syntax require Python 3.12",
)
def test_memoized_fingerprint_agrees_with_truth_when_an_alias_target_is_rebound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_module_type_alias"
    (tmp_path / f"{module_name}.py").write_text(
        "class First:\n"
        "    marker = 1\n"
        "\n"
        "\n"
        "class Second:\n"
        "    marker = 2\n"
        "\n"
        "\n"
        "type ALIAS = First\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key="memo-module-type-alias")
        def aliased(db: Database) -> str:
            return cast(str, module.ALIAS.__name__)

        db = Database()
        db._query_fingerprint(aliased)
        # The chain lands on a type alias. The alias object never moves, and a
        # class is not a constant the module stamp carries, so what the memo
        # owes after this rebinding depends on what the interpreter exposes.
        monkeypatch.setattr(module, "First", module.Second)
        if isinstance(getattr(module.ALIAS, "evaluate_value", None), FunctionType):
            # The payload folds the lazy evaluator as a definition, and its
            # observation resolves the same global -- identity tracks the
            # rebinding.
            memoized, truth = _memo_and_truth(db, aliased)
            assert memoized == truth
            assert memoized == Database()._query_fingerprint(aliased)
        else:
            # Through 3.13 there is no evaluator to fold: the payload resolved
            # and cached __value__ at prime time, and the rebinding kills the
            # live module binding its anchor requires. A fresh computation
            # refuses, so the memo must refuse with it -- serving past the
            # refusal is the staleness the anchor exists to prevent.
            with pytest.raises(UnsupportedValueError, match="cannot be fingerprinted safely"):
                db._query_fingerprint(aliased)
            with pytest.raises(UnsupportedValueError, match="cannot be fingerprinted safely"):
                db.get(aliased)
            with pytest.raises(UnsupportedValueError, match="cannot be fingerprinted safely"):
                Database()._query_fingerprint(aliased)
    finally:
        sys.modules.pop(module_name, None)


def test_memoized_fingerprint_refuses_with_truth_when_a_parameter_bound_is_rebound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_module_type_parameter_rebound"
    (tmp_path / f"{module_name}.py").write_text(
        "from typing import TypeVar\n"
        "\n"
        "\n"
        "class Bound:\n"
        "    marker = 1\n"
        "\n"
        "\n"
        "class Second:\n"
        "    marker = 2\n"
        "\n"
        "\n"
        'PARAM = TypeVar("PARAM", bound=Bound)\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key="memo-module-type-parameter-rebound")
        def named(db: Database) -> str:
            return cast(str, module.PARAM.__name__)

        db = Database()
        db._query_fingerprint(named)
        # A runtime-constructed TypeVar stores its bound eagerly on every
        # interpreter -- evaluate_bound, where it exists at all, is not a
        # Python function -- so the payload anchors the bound class to its
        # live module binding. Rebinding that global makes every fresh
        # computation refuse; the memo must refuse with it rather than keep
        # serving the fingerprint it stored while the binding held.
        monkeypatch.setattr(module, "Bound", module.Second)
        with pytest.raises(UnsupportedValueError, match="cannot be fingerprinted safely"):
            db._query_fingerprint(named)
        with pytest.raises(UnsupportedValueError, match="cannot be fingerprinted safely"):
            Database()._query_fingerprint(named)
    finally:
        sys.modules.pop(module_name, None)


def test_memoized_fingerprint_tracks_a_chain_landed_type_parameters_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_module_type_parameter"
    (tmp_path / f"{module_name}.py").write_text(
        "from typing import TypeVar\n"
        "\n"
        "\n"
        "class Bound:\n"
        "    marker = 1\n"
        "\n"
        "\n"
        'PARAM = TypeVar("PARAM", bound=Bound)\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key="memo-module-type-parameter")
        def named(db: Database) -> str:
            return cast(str, module.PARAM.__name__)

        db = Database()
        db._query_fingerprint(named)
        # The chain lands on a type parameter, beside the alias above and for
        # the same reason: the bound is read off the parameter when the payload
        # asks for it -- through the lazy evaluator where the interpreter has
        # one and the resolved attribute otherwise -- and the class it names is
        # folded by its body. The parameter itself never moves, and a class is
        # not a constant the module stamp carries.
        monkeypatch.setattr(module.Bound, "marker", 9)
        memoized, truth = _memo_and_truth(db, named)
        assert memoized == truth
        assert memoized == Database()._query_fingerprint(named)
    finally:
        sys.modules.pop(module_name, None)


def test_memoized_fingerprint_tracks_a_chain_landed_resources_method_globals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_module_resource_methods"
    (tmp_path / f"{module_name}.py").write_text(
        "def factor() -> int:\n"
        "    return 2\n"
        "\n"
        "\n"
        "def larger_factor() -> int:\n"
        "    return 5\n"
        "\n"
        "\n"
        "class Scaled:\n"
        "    def identity(self) -> str:\n"
        '        return "scaled-resource"\n'
        "\n"
        "    def label(self, key: int) -> str:\n"
        '        return f"scaled[{key}]"\n'
        "\n"
        "    def probe(self, key: int) -> int:\n"
        "        return factor()\n"
        "\n"
        "    def load(self, db: object, key: int) -> int:\n"
        "        return factor() * key\n"
        "\n"
        "\n"
        "SCALED = Scaled()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key="memo-module-resource-methods")
        def scaled(db: Database) -> Any:
            return module.SCALED

        db = Database()
        db._query_fingerprint(scaled)
        # The chain lands on a resource, whose probe, load and identity
        # methods are folded as the definitions they are. The per-request
        # configuration digest covers what identity() returns and nothing of
        # what those method bodies read, so the rebinding moves the fresh
        # fingerprint while the resource itself holds still.
        monkeypatch.setattr(module, "factor", module.larger_factor)
        memoized, truth = _memo_and_truth(db, scaled)
        assert memoized == truth
        assert memoized == Database()._query_fingerprint(scaled)
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_rebound_global_behind_a_captured_module_function_matches_fresh(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = f"pyinc_module_function_limit_{mode}"
    (tmp_path / f"{module_name}.py").write_text(
        "LIMIT = 2\n\n\ndef helper():\n    return LIMIT * 10\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key=f"module-function-global-fsc-{mode}")
        def scaled(db: Database) -> int:
            return cast(int, module.helper())

        db = Database(mode=mode)
        assert db.get(scaled) == 20
        monkeypatch.setattr(module, "LIMIT", 7)
        executions = db.statistics().query_executions
        warm = db.get(scaled)
        fresh = Database(mode=mode).get(scaled)
        assert warm == fresh == 70
        assert db.statistics().query_executions == executions + 1
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_rebound_defaults_behind_a_captured_chain_matches_fresh(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = f"pyinc_module_function_defaults_{mode}"
    (tmp_path / f"{module_name}.py").write_text(
        "def helper(scale=2):\n    return scale * 10\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key=f"module-function-defaults-fsc-{mode}")
        def scaled(db: Database) -> int:
            return cast(int, module.helper())

        db = Database(mode=mode)
        assert db.get(scaled) == 20
        # The other half of what a chain landing on a function carries: its
        # defaults are read live from the same definition its globals are, so
        # rebinding them moves identity where a write inside a class the chain
        # lands on does not. The landing function is the same object either
        # way, which is what the memo compares.
        module.helper.__defaults__ = (7,)
        _assert_warm_matches_fresh(db, mode, scaled, 70)
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_captured_class_attribute_change_matches_fresh(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    @query(key=f"class-attr-fsc-{mode}")
    def scaled(db: Database) -> int:
        return _ObservedConsts.SCALE + 0

    db = Database(mode=mode)
    assert db.get(scaled) == 2
    monkeypatch.setattr(_ObservedConsts, "SCALE", 3)
    _assert_warm_matches_fresh(db, mode, scaled, 3)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_class_attribute_reached_from_body_and_annotation_matches_fresh(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    @query(key=f"class-attr-two-slots-fsc-{mode}")
    def scaled(db: Database, value: Any = None) -> int:
        return _ObservedConsts.SCALE + 0

    # One class in two slots: the body captures it and the annotation names
    # it. Whichever slot arrives first is the one that folds the class body,
    # because the second finds the class already seen.
    scaled.fn.__annotations__["value"] = _ObservedConsts
    db = Database(mode=mode)
    assert db.get(scaled) == 2
    monkeypatch.setattr(_ObservedConsts, "SCALE", 4)
    _assert_warm_matches_fresh(db, mode, scaled, 4)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_instance_state_reached_from_default_and_body_matches_fresh(mode: str) -> None:
    @query(key=f"instance-state-fsc-{mode}")
    def boxed(db: Database, item: _ObservedBox = _observed_box) -> int:
        return item.factor + _observed_box.factor

    db = Database(mode=mode)
    assert db.get(boxed) == 4
    original = _observed_box.factor
    object.__setattr__(_observed_box, "factor", 5)
    try:
        # The default value and the captured global are one instance, so a
        # field written in place has to be seen through whichever of the two
        # slots the walk reaches first.
        _assert_warm_matches_fresh(db, mode, boxed, 10)
    finally:
        object.__setattr__(_observed_box, "factor", original)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_slotted_instance_state_change_matches_fresh(mode: str) -> None:
    @query(key=f"slotted-instance-fsc-{mode}")
    def boxed(db: Database) -> int:
        return _observed_slotted_box.factor * 10

    db = Database(mode=mode)
    assert db.get(boxed) == 20
    original = _observed_slotted_box.factor
    object.__setattr__(_observed_slotted_box, "factor", 3)
    try:
        # A slotted instance carries no instance dictionary, so the state
        # observation finds nothing to read and the dataclass-field walk is
        # the only thing between this write and a warm answer.
        _assert_warm_matches_fresh(db, mode, boxed, 30)
    finally:
        object.__setattr__(_observed_slotted_box, "factor", original)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_captured_function_docstring_change_matches_fresh(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    @query(key=f"metadata-fsc-{mode}")
    def documented(db: Database) -> int:
        return len(_observed_documented.__doc__ or "")

    db = Database(mode=mode)
    assert db.get(documented) == 2
    monkeypatch.setattr(_observed_documented, "__doc__", "abcd")
    _assert_warm_matches_fresh(db, mode, documented, 4)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_function_metadata_reached_from_default_and_body_matches_fresh(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    @query(key=f"metadata-two-slots-fsc-{mode}")
    def documented(db: Database, fn: Any = _observed_documented) -> int:
        return len(_observed_documented.__doc__ or "")

    db = Database(mode=mode)
    assert db.get(documented) == 2
    # The same function object arrives as a default value and as a captured
    # global; only one of the two folds its metadata, and the docstring has
    # to move the verdict either way.
    monkeypatch.setattr(_observed_documented, "__doc__", "abcdef")
    _assert_warm_matches_fresh(db, mode, documented, 6)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_shared_policy_state_change_matches_fresh(mode: str) -> None:
    comparator = _CountingEq(0)
    tolerant = Input[int](f"policy-fsc-input-{mode}", eq=comparator)

    @query(key=f"policy-fsc-{mode}", eq=comparator)
    def read_tolerant(db: Database) -> int:
        return tolerant.read(db) + 1

    db = Database(mode=mode)
    db.set(tolerant, 1)
    assert db.get(read_tolerant) == 2
    # One comparator in two slots: the input's policy, reached through the
    # captured input, and the query's own policy. A policy decides what counts
    # as a change rather than what the query returns, so the value cannot move
    # here; what the change owes is the recompute the counter below checks and
    # agreement with a database that never held the earlier tolerance.
    comparator.tolerance = 5
    executions = db.statistics().query_executions
    warm = db.get(read_tolerant)
    fresh_db = Database(mode=mode)
    fresh_db.set(tolerant, 1)
    assert warm == fresh_db.get(read_tolerant) == 2
    assert db.statistics().query_executions == executions + 1


@pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="type-alias and type-parameter syntax require Python 3.12",
)
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_alias_reached_from_body_and_annotation_matches_fresh(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = f"pyinc_alias_verdict_helper_{mode}"
    helper = _import_alias_helper_module(tmp_path, monkeypatch, module_name)
    try:
        alias = helper.Alias

        @query(key=f"alias-two-slots-fsc-{mode}")
        def scaled(db: Database, value: Any = None) -> int:
            return cast(int, alias.__value__.SCALE) + 0

        # The alias is captured by the body and named by the annotation, and
        # the class it resolves to is reached only through its evaluator.
        scaled.fn.__annotations__["value"] = alias
        db = Database(mode=mode)
        assert db.get(scaled) == 2
        monkeypatch.setattr(helper.Target, "SCALE", 3)
        _assert_warm_matches_fresh(db, mode, scaled, 3)
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.skipif(
    sys.version_info < (3, 12),
    reason="type-alias and type-parameter syntax require Python 3.12",
)
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_type_parameter_reached_from_body_and_annotation_matches_fresh(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = f"pyinc_parameter_verdict_helper_{mode}"
    helper = _import_alias_helper_module(tmp_path, monkeypatch, module_name)
    try:
        parameter = helper.bounded.__type_params__[0]

        @query(key=f"parameter-two-slots-fsc-{mode}")
        def scaled(db: Database, value: Any = None) -> int:
            return cast(int, parameter.__bound__.SCALE) + 0

        # A bound carries its content behind the same class, reached from the
        # captured parameter and from the annotation that names it.
        scaled.fn.__annotations__["value"] = parameter
        db = Database(mode=mode)
        assert db.get(scaled) == 2
        monkeypatch.setattr(helper.Target, "SCALE", 4)
        _assert_warm_matches_fresh(db, mode, scaled, 4)
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_resource_configuration_change_matches_fresh(mode: str) -> None:
    resource = _ObservedPartsResource(2)

    @query(key=f"resource-config-fsc-{mode}")
    def scaled(db: Database) -> int:
        return db.read_resource(resource, 10)

    db = Database(mode=mode)
    assert db.get(scaled) == 20
    resource.parts[0] = 3
    _assert_warm_matches_fresh(db, mode, scaled, 30)
    # The resource edge re-probes on its own, so agreement on the value alone
    # would not say the configuration reached this query's identity. The
    # fingerprint is that identity: a warm database's answer for it has to
    # equal what a database that never saw the earlier configuration derives.
    assert db._query_fingerprint(scaled) == Database(mode=mode)._query_fingerprint(scaled)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_resource_configuration_read_in_the_body_matches_fresh(mode: str) -> None:
    resource = _ObservedPartsResource(2)

    @query(key=f"resource-capture-only-fsc-{mode}")
    def scaled(db: Database) -> int:
        return resource.parts[0] * 10

    db = Database(mode=mode)
    assert db.get(scaled) == 20
    # The resource is captured but never read through the database, so no
    # resource edge re-probes on this query's behalf. The configuration
    # reaches the verdict through the fingerprint alone, which is what the
    # recorded digest has to catch.
    resource.parts[0] = 3
    _assert_warm_matches_fresh(db, mode, scaled, 30)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_resource_reached_from_capture_and_class_body_matches_fresh(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    resource = _ObservedPartsResource(2)
    monkeypatch.setattr(_ObservedPartsHolder, "nested", resource)

    @query(key=f"resource-two-slots-fsc-{mode}")
    def scaled(db: Database) -> int:
        return db.read_resource(resource, 10) + len(_ObservedPartsHolder.nested.parts)

    db = Database(mode=mode)
    assert db.get(scaled) == 21
    # One resource in two slots: a direct capture, which folds it through
    # identity(), and a captured class body, which folds it as an ordinary
    # instance. Whichever the walk reaches first decides how the
    # configuration is recorded, and the write has to survive that choice.
    resource.parts[0] = 3
    _assert_warm_matches_fresh(db, mode, scaled, 31)
    assert db._query_fingerprint(scaled) == Database(mode=mode)._query_fingerprint(scaled)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_module_function_reached_from_chain_and_capture_matches_fresh(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = f"pyinc_module_two_slots_{mode}"
    replacement_name = f"pyinc_module_two_slots_replacement_{mode}"
    (tmp_path / f"{module_name}.py").write_text(
        "def inner():\n    return 3\n\n\ndef helper():\n    return inner() * 10\n",
        encoding="utf-8",
    )
    (tmp_path / f"{replacement_name}.py").write_text(
        "def inner():\n    return 4\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    replacement = importlib.import_module(replacement_name)
    try:
        captured = module.helper

        @query(key=f"module-two-slots-fsc-{mode}")
        def scaled(db: Database) -> int:
            return cast(int, module.helper()) + cast(int, captured())

        db = Database(mode=mode)
        assert db.get(scaled) == 60
        # One function in two slots: the chain names it and re-resolves it,
        # and the closure holds the same object directly. The rebinding below
        # moves neither reference and swaps no constant the module stamp
        # carries; what it changes is the globals behind that function.
        monkeypatch.setattr(module, "inner", replacement.inner)
        _assert_warm_matches_fresh(db, mode, scaled, 80)
    finally:
        sys.modules.pop(module_name, None)
        sys.modules.pop(replacement_name, None)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_rebound_function_on_a_captured_chain_matches_fresh(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = f"pyinc_chain_target_swap_{mode}"
    replacement_name = f"pyinc_chain_target_swap_replacement_{mode}"
    (tmp_path / f"{module_name}.py").write_text("def helper():\n    return 10\n", encoding="utf-8")
    (tmp_path / f"{replacement_name}.py").write_text(
        "def helper():\n    return 20\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    replacement = importlib.import_module(replacement_name)
    try:

        @query(key=f"chain-target-swap-fsc-{mode}")
        def scaled(db: Database) -> int:
            return cast(int, module.helper())

        db = Database(mode=mode)
        assert db.get(scaled) == 10
        # A function is no constant, so the module stamp carries nothing about
        # this rebinding; what the chain names is a different object now, and
        # only re-resolving it says so.
        monkeypatch.setattr(module, "helper", replacement.helper)
        _assert_warm_matches_fresh(db, mode, scaled, 20)
    finally:
        sys.modules.pop(module_name, None)
        sys.modules.pop(replacement_name, None)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_rebound_function_behind_a_captured_chain_matches_fresh(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = f"pyinc_chain_global_swap_{mode}"
    replacement_name = f"pyinc_chain_global_swap_replacement_{mode}"
    (tmp_path / f"{module_name}.py").write_text(
        "def inner():\n    return 3\n\n\ndef helper():\n    return inner() * 10\n",
        encoding="utf-8",
    )
    (tmp_path / f"{replacement_name}.py").write_text(
        "def inner():\n    return 4\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    replacement = importlib.import_module(replacement_name)
    try:

        @query(key=f"chain-global-swap-fsc-{mode}")
        def scaled(db: Database) -> int:
            return cast(int, module.helper())

        db = Database(mode=mode)
        assert db.get(scaled) == 30
        # The chain still names the same function and the swapped-in value is
        # a function rather than a constant, so neither the re-resolved target
        # nor the module stamp moves: the change lives in the globals the
        # fingerprint folded out of the function behind the chain.
        monkeypatch.setattr(module, "inner", replacement.inner)
        _assert_warm_matches_fresh(db, mode, scaled, 40)
    finally:
        sys.modules.pop(module_name, None)
        sys.modules.pop(replacement_name, None)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_constant_outside_the_captured_chain_matches_fresh(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = f"pyinc_offchain_verdict_{mode}"
    (tmp_path / f"{module_name}.py").write_text("SCALE = 1\nOTHER = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key=f"offchain-constant-fsc-{mode}")
        def scaled(db: Database) -> int:
            return cast(int, module.SCALE) * 10

        db = Database(mode=mode)
        assert db.get(scaled) == 10
        # OTHER sits on no access path, so the result cannot move with it.
        # The fingerprint folds every constant the captured module holds, so
        # the verdict still does: the warm read re-executes rather than
        # answering from a record keyed under the earlier namespace.
        monkeypatch.setattr(module, "OTHER", 9)
        _assert_warm_matches_fresh(db, mode, scaled, 10)
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_function_behind_a_captured_staticmethod_matches_fresh(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    @query(key=f"descriptor-fsc-{mode}")
    def read(db: Database) -> int:
        return _ObservedStaticHolder.read()

    db = Database(mode=mode)
    assert db.get(read) == 30
    monkeypatch.setattr(
        sys.modules[__name__], "_observed_static_source", _observed_descriptor_replacement
    )
    _assert_warm_matches_fresh(db, mode, read, 110)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_function_behind_two_captured_descriptors_matches_fresh(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    @query(key=f"descriptor-two-slots-fsc-{mode}")
    def read(db: Database) -> int:
        return _ObservedDualDescriptorHolder.scaled() + _ObservedDualDescriptorHolder().offset

    db = Database(mode=mode)
    assert db.get(read) == 330
    # One function behind two descriptors of the same captured class: a
    # staticmethod and a property, unwrapped from one class-body walk.
    monkeypatch.setattr(
        sys.modules[__name__], "_observed_shared_source", _observed_descriptor_replacement
    )
    _assert_warm_matches_fresh(db, mode, read, 1210)


def test_rebinding_inside_a_chain_landed_class_keeps_the_warm_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_chain_landed_class_module"
    (tmp_path / f"{module_name}.py").write_text(
        "class Consts:\n    SCALE = 2\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key="chain-landed-class-boundary")
        def scaled(db: Database) -> int:
            return cast(int, module.Consts.SCALE) + 0

        db = Database()
        assert db.get(scaled) == 2
        monkeypatch.setattr(module.Consts, "SCALE", 3)
        executions = db.statistics().query_executions
        # The documented boundary of the memo, stated as behavior: where a
        # chain lands on a class, the memo pins the landing object by
        # reference and follows nothing inside it, so a database that already
        # answered this query keeps answering from the earlier identity. Only
        # a database fingerprinting it for the first time sees the rebinding.
        assert db.get(scaled) == 2
        assert db.statistics().query_executions == executions
        assert Database().get(scaled) == 3
    finally:
        sys.modules.pop(module_name, None)


def test_rebinding_inside_a_chain_landed_class_moves_only_a_fresh_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_chain_landed_class_split_module"
    (tmp_path / f"{module_name}.py").write_text("class Model:\n    flag = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key="chain-landed-class-split")
        def flagged(db: Database) -> int:
            return cast(int, module.Model.flag) + 0

        db = Database()
        assert db.get(flagged) == 1
        before = db._query_fingerprint(flagged)
        monkeypatch.setattr(module.Model, "flag", 2)
        executions = db.statistics().query_executions
        # Both halves of the residual the boundary pin above states as
        # behavior, told apart by the fingerprint itself. The write is folded:
        # a database meeting this query for the first time computes a different
        # identity for it. What the memo cannot see is that the write happened,
        # because the class it re-resolves is the same object it recorded, so
        # the database that already answered keeps the identity it stored and
        # answers from the record filed under it.
        assert db.get(flagged) == 1
        assert db.statistics().query_executions == executions
        assert db._query_fingerprint(flagged) == before
        assert Database()._query_fingerprint(flagged) != before
        assert Database().get(flagged) == 2
    finally:
        sys.modules.pop(module_name, None)


def test_state_inside_a_chain_landed_instance_keeps_the_warm_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_chain_landed_instance_module"
    (tmp_path / f"{module_name}.py").write_text(
        "from dataclasses import dataclass\n"
        "\n"
        "\n"
        "@dataclass(frozen=True)\n"
        "class Holder:\n"
        "    scale: int\n"
        "\n"
        "\n"
        "instance = Holder(2)\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key="chain-landed-instance-boundary")
        def scaled(db: Database) -> int:
            return cast(int, module.instance.scale) + 0

        db = Database()
        assert db.get(scaled) == 2
        object.__setattr__(module.instance, "scale", 3)
        executions = db.statistics().query_executions
        # The instance half of the same boundary: the landing object is one
        # reference to the memo, whatever its fields do.
        assert db.get(scaled) == 2
        assert db.statistics().query_executions == executions
        assert Database().get(scaled) == 3
    finally:
        sys.modules.pop(module_name, None)


def test_state_inside_a_chain_landed_container_keeps_the_warm_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_chain_landed_tuple_module"
    (tmp_path / f"{module_name}.py").write_text(
        "from dataclasses import dataclass\n"
        "\n"
        "\n"
        "@dataclass(frozen=True)\n"
        "class Holder:\n"
        "    scale: int\n"
        "\n"
        "\n"
        "TABLE = (Holder(2),)\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key="chain-landed-tuple-boundary")
        def scaled(db: Database) -> int:
            return cast(int, module.TABLE[0].scale) * 10

        db = Database()
        assert db.get(scaled) == 20
        object.__setattr__(module.TABLE[0], "scale", 3)
        executions = db.statistics().query_executions
        # A tuple is an accepted landing, and the same boundary runs through
        # it: the payload folds what the tuple holds while the memo compares
        # the tuple by identity and follows nothing inside. So the residue is
        # not confined to a class or an instance a chain names directly -- it
        # reaches a frozen dataclass held in any immutable container the
        # payload accepts.
        assert db.get(scaled) == 20
        assert db.statistics().query_executions == executions
        assert Database().get(scaled) == 30
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.parametrize(
    "shape, source",
    [
        ("dict", "TABLE = {'scale': 2}\n"),
        ("list", "TABLE = [2]\n"),
        (
            "object",
            "class Holder:\n"
            "    def __init__(self):\n"
            "        self.scale = 2\n"
            "\n"
            "\n"
            "TABLE = Holder()\n",
        ),
        (
            "mutable_dataclass",
            "from dataclasses import dataclass\n"
            "\n"
            "\n"
            "@dataclass\n"
            "class Holder:\n"
            "    scale: int = 2\n"
            "\n"
            "\n"
            "TABLE = Holder()\n",
        ),
        (
            "slotted_object",
            "class Holder:\n"
            "    __slots__ = ('scale',)\n"
            "\n"
            "    def __init__(self):\n"
            "        self.scale = 2\n"
            "\n"
            "\n"
            "TABLE = Holder()\n",
        ),
    ],
)
def test_chain_landing_the_payload_cannot_pin_is_refused(
    shape: str, source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = f"pyinc_chain_landed_{shape}_module"
    (tmp_path / f"{module_name}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key=f"chain-landed-{shape}")
        def sized(db: Database) -> int:
            return len(str(module.TABLE))

        # The counterpart of the boundary pins above: these landings are
        # refused when the fingerprint is built rather than folded and left to
        # a memo that cannot follow them, so no stale answer comes from any of
        # them. The landings that are folded, and then compared only through
        # the landed object's identity, are what those pins document.
        with pytest.raises(UnsupportedValueError, match="cannot be fingerprinted safely"):
            Database().get(sized)
    finally:
        sys.modules.pop(module_name, None)


def test_rebinding_a_stdlib_chain_target_function_moves_no_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @query(key="stdlib-chain-target-function")
    def dumped(db: Database) -> str:
        return json.dumps({"a": 1})

    db = Database()
    assert db.get(dumped) == '{"a": 1}'
    before = db._query_fingerprint(dumped)
    monkeypatch.setattr(json, "dumps", _stdlib_dumps_replacement)
    executions = db.statistics().query_executions
    # The other residual, and a different one in kind from the chain-landed
    # writes above: a captured standard-library module folds the names read off
    # it rather than the behavior behind them, so nothing here is folded that
    # this rebinding could move. The identity holds in both directions -- the
    # memo keeps it and a database computing it from scratch arrives at the
    # same bytes -- which is why the last line is the cost of the limitation
    # rather than a disagreement between the two.
    assert db.get(dumped) == '{"a": 1}'
    assert db.statistics().query_executions == executions
    assert db._query_fingerprint(dumped) == before
    assert Database()._query_fingerprint(dumped) == before
    assert Database().get(dumped) == "replaced"


def test_rebinding_a_stdlib_chain_target_class_moves_no_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @query(key="stdlib-chain-target-class")
    def scaled(db: Database) -> str:
        return str(decimal.Decimal("2") * 3)

    db = Database()
    assert db.get(scaled) == "6"
    before = db._query_fingerprint(scaled)
    monkeypatch.setattr(decimal, "Decimal", _StdlibDecimalReplacement)
    executions = db.statistics().query_executions
    # The class half of the same limitation. A class reached through a chain on
    # a non-stdlib module is folded and only the memo cannot follow it; here
    # the fold never reaches the class at all, so the rebinding is invisible to
    # a fresh fingerprint too.
    assert db.get(scaled) == "6"
    assert db.statistics().query_executions == executions
    assert db._query_fingerprint(scaled) == before
    assert Database()._query_fingerprint(scaled) == before
    assert Database().get(scaled) == "replaced"


def test_declared_mid_span_resource_reconfiguration_reaches_the_next_request() -> None:
    class SpanResource(Resource[int, int, int]):
        def __init__(self, scale: int) -> None:
            # A list, so the observation pins the container by reference and an
            # in-place write moves nothing it can see. Only re-reading the
            # configuration catches this.
            self.parts = [scale]

        def identity(self) -> tuple[str, tuple[int, ...]]:
            return ("span-resource", tuple(self.parts))

        def label(self, key: int) -> str:
            return f"span[{key}]"

        def probe(self, key: int) -> int:
            return self.parts[0]

        def load(self, db: Database, key: int) -> int:
            return self.parts[0] * key

    resource = SpanResource(2)

    @query(key="memo-resource-span")
    def scaled(db: Database) -> int:
        return db.read_resource(resource, 10)

    db = Database()
    with db.request_span():
        assert db.get(scaled) == 20
        before = db._query_fingerprint(scaled)
        resource.parts[0] = 3
        # A span holds the world still, so the configuration digest is read
        # once for the whole request; declaring the change is what releases
        # that hold, and the next read inside the span must see it.
        db.request_inputs_changed()
        assert db._query_fingerprint(scaled) != before
        assert db.get(scaled) == 30


def test_memoized_fingerprint_still_serves_capture_free_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    original = Database._code_fingerprint

    def counting(self: Database, fn: FunctionType) -> str:
        nonlocal calls
        calls += 1
        return original(self, fn)

    monkeypatch.setattr(Database, "_code_fingerprint", counting)

    @query
    def plain(db: Database, value: int) -> int:
        return value

    db = Database()
    for item in range(5):
        assert db.get(plain, item) == item
    assert plain in db._query_fingerprint_memo
    assert calls == 1


def test_binary_file_resource_reads_bytes_from_one_observation(tmp_path: Path) -> None:
    path = tmp_path / "payload.bin"
    path.write_bytes(b"\x00\xffpayload")
    resource = BinaryFileResource()

    @query
    def content(db: Database) -> bytes:
        return resource.read(db, path)

    assert Database().get(content) == b"\x00\xffpayload"


def test_resource_implementation_participates_in_identity() -> None:
    def make_resource(variant: int) -> Resource[str, int, tuple[int]]:
        @dataclass(frozen=True)
        class ConstantResource(Resource[str, int, tuple[int]]):
            def probe(self, key: str) -> tuple[int]:
                return (variant,)

            def load(self, db: Database, key: str) -> int:
                return variant

            def label(self, key: str) -> str:
                return f"constant[{key}]"

        return ConstantResource()

    db = Database()
    first = db._resource_key(make_resource(1), "key")
    second = db._resource_key(make_resource(2), "key")
    assert first.identity != second.identity


def test_matching_resource_implementations_share_an_identity() -> None:
    def make_resource(variant: int) -> Resource[str, int, tuple[int]]:
        @dataclass(frozen=True)
        class ConstantResource(Resource[str, int, tuple[int]]):
            def probe(self, key: str) -> tuple[int]:
                return (variant,)

            def load(self, db: Database, key: str) -> int:
                return variant

            def label(self, key: str) -> str:
                return f"constant[{key}]"

        return ConstantResource()

    db = Database()
    # The control the inequality pins rest on. Each call defines its own class
    # object, so two resources built the same way share nothing but what they
    # are made of; an identity that folded anything unique to the class object
    # would separate these two and still separate the differing pair below,
    # leaving the difference in behavior unattributable -- and a stored record
    # unreachable from the process that reads it back.
    assert (
        db._resource_key(make_resource(1), "key").identity
        == db._resource_key(make_resource(1), "key").identity
    )
    assert (
        db._resource_key(make_resource(1), "key").identity
        != db._resource_key(make_resource(2), "key").identity
    )


def test_behavior_helper_implementations_participate_in_trust_identities() -> None:
    def make_resource(multiplier: int) -> Resource[int, int, int]:
        @dataclass(frozen=True)
        class CalculatedResource(Resource[int, int, int]):
            def calculate(self, value: int) -> int:
                return value * multiplier

            def probe(self, key: int) -> int:
                return key

            def load(self, db: Database, key: int) -> int:
                return self.calculate(key)

            def label(self, key: int) -> str:
                return f"calculated[{key}]"

        return CalculatedResource()

    def make_policy(multiplier: int) -> Any:
        class Policy:
            def normalize(self, value: int) -> int:
                return value * multiplier

            def __call__(self, left: int, right: int) -> bool:
                return self.normalize(left) == self.normalize(right)

        return Policy()

    def make_adapter(multiplier: int) -> Any:
        class Adapter:
            def normalize(self, value: int) -> int:
                return value * multiplier

            def freeze(self, value: int, freeze_value: Any) -> Any:
                return freeze_value(self.normalize(value))

            def thaw(self, snapshot: Any, thaw_value: Any) -> Any:
                return thaw_value(snapshot)

        return Adapter()

    def template(db: Database) -> int:
        return 1

    db = Database()
    first_resource = db._resource_key(make_resource(2), 4)
    second_resource = db._resource_key(make_resource(3), 4)
    first_policy = Query(template, key="helper-policy", eq=make_policy(2))
    second_policy = Query(template, key="helper-policy", eq=make_policy(3))
    first_adapter = db._adapter_implementation_digest(cast(Any, make_adapter(2)))
    second_adapter = db._adapter_implementation_digest(cast(Any, make_adapter(3)))

    assert first_resource.identity != second_resource.identity
    assert db._query_fingerprint(first_policy) != db._query_fingerprint(second_policy)
    assert first_adapter != second_adapter


def test_callable_resource_hook_implementation_participates_in_identity() -> None:
    def make_resource(multiplier: int) -> Any:
        class Loader:
            def __call__(self, db: Database, key: int) -> int:
                return key * multiplier

        class CallableHookResource:
            def __init__(self) -> None:
                self.load = Loader()

            def identity(self) -> tuple[str]:
                return ("callable-hook",)

            def probe(self, key: int) -> int:
                return key

            def label(self, key: int) -> str:
                return "callable-hook"

        return CallableHookResource()

    first = make_resource(2)
    second = make_resource(3)
    db = Database()

    assert db._resource_key(first, 2).identity != db._resource_key(second, 2).identity
    assert db.read_resource(first, 2) == 4
    assert db.read_resource(second, 2) == 6


def test_nested_resource_configuration_behavior_participates_in_identity() -> None:
    def make_resource(multiplier: int) -> Resource[int, int, int]:
        @dataclass(frozen=True)
        class Config:
            offset: int

            def calculate(self, value: int) -> int:
                return value * multiplier + self.offset

        @dataclass(frozen=True)
        class CalculatedResource(Resource[int, int, int]):
            config: Config

            def identity(self) -> dict[str, Config]:
                return {"config": self.config}

            def probe(self, key: int) -> int:
                return key

            def load(self, db: Database, key: int) -> int:
                return self.config.calculate(key)

            def label(self, key: int) -> str:
                return f"configured[{key}]"

        return CalculatedResource(Config(offset=1))

    first = make_resource(2)
    second = make_resource(3)
    db = Database()
    first_key = db._resource_key(first, 2)
    second_key = db._resource_key(second, 2)

    assert first_key.identity != second_key.identity
    assert db.read_resource(first, 2) == 5
    assert db.read_resource(second, 2) == 7


def test_local_dataclass_options_and_metadata_participate_in_resource_identity() -> None:
    def make_resource(*, equality: bool, factor: int) -> Resource[int, int, int]:
        from dataclasses import field as dataclass_field

        @dataclass(frozen=True, eq=equality)  # type: ignore[literal-required]
        class Config:
            value: int = dataclass_field(metadata={"factor": factor})

        @dataclass(frozen=True)
        class ConfiguredResource(Resource[int, int, int]):
            config: Config

            def identity(self) -> Config:
                return self.config

            def probe(self, key: int) -> int:
                return key

            def load(self, db: Database, key: int) -> int:
                metadata_factor = next(iter(self.config.__dataclass_fields__.values())).metadata[
                    "factor"
                ]
                equality_bonus = int(self.config == type(self.config)(self.config.value))
                return cast(int, metadata_factor) * key + equality_bonus

            def label(self, key: int) -> str:
                return "dataclass-options"

        return ConfiguredResource(Config(1))

    first = make_resource(equality=True, factor=2)
    second = make_resource(equality=False, factor=3)
    db = Database()

    assert db._resource_key(first, 2).identity != db._resource_key(second, 2).identity
    assert db.read_resource(first, 2) == 5
    assert db.read_resource(second, 2) == 6


def test_local_metaclass_behavior_participates_in_resource_identity() -> None:
    def make_resource(multiplier: int) -> Resource[int, int, int]:
        class ConfigMeta(type):
            def __getattr__(cls, name: str) -> int:
                if name == "factor":
                    return multiplier
                raise AttributeError(name)

        @dataclass(frozen=True)
        class Config(metaclass=ConfigMeta):
            value: int

        @dataclass(frozen=True)
        class ConfiguredResource(Resource[int, int, int]):
            config: Config

            def identity(self) -> Config:
                return self.config

            def probe(self, key: int) -> int:
                return key

            def load(self, db: Database, key: int) -> int:
                return type(self.config).factor * key

            def label(self, key: int) -> str:
                return "metaclass-options"

        return ConfiguredResource(Config(1))

    first = make_resource(2)
    second = make_resource(3)
    db = Database()

    assert db._resource_key(first, 2).identity != db._resource_key(second, 2).identity
    assert db.read_resource(first, 2) == 4
    assert db.read_resource(second, 2) == 6


def test_failed_high_cardinality_resource_reads_do_not_leak_registry_entries() -> None:
    @dataclass(frozen=True)
    class FailingResource(Resource[int, int, int]):
        def probe(self, key: int) -> int:
            raise RuntimeError(f"probe failed for {key}")

        def load(self, db: Database, key: int) -> int:
            raise AssertionError("load must not run")

        def label(self, key: int) -> str:
            return f"failing[{key}]"

    db = Database()
    resource = FailingResource()
    for key in range(100):
        with pytest.raises(RuntimeError, match="probe failed"):
            db.read_resource(resource, key)

    assert not db._resource_registry
    assert db.statistics().resource_count == 0


def test_resource_parameter_behavior_participates_in_node_identity() -> None:
    @dataclass(frozen=True)
    class CalculatingResource(Resource[Any, int, int]):
        def identity(self) -> tuple[str]:
            return ("parameter-behavior",)

        def probe(self, key: Any) -> int:
            return cast(int, key.value)

        def load(self, db: Database, key: Any) -> int:
            return cast(int, key.calculate())

        def label(self, key: Any) -> str:
            return "parameter-behavior"

    def make_key(multiplier: int) -> Any:
        @dataclass(frozen=True)
        class Key:
            value: int

            def calculate(self) -> int:
                return self.value * multiplier

        return Key(2)

    resource = CalculatingResource()
    first = make_key(2)
    second = make_key(3)
    db = Database()

    assert db._resource_key(resource, first).identity != db._resource_key(resource, second).identity
    assert db.read_resource(resource, first) == 4
    assert db.read_resource(resource, second) == 6


def test_mutable_resource_probe_is_snapshotted_before_storage() -> None:
    class MutableProbeResource(Resource[str, int, list[int]]):
        def __init__(self) -> None:
            self.state = [10]

        def identity(self) -> tuple[str]:
            return ("mutable-probe",)

        def probe(self, key: str) -> list[int]:
            return self.state

        def load(self, db: Database, key: str) -> int:
            return self.state[0]

        def label(self, key: str) -> str:
            return "mutable-probe"

    resource = MutableProbeResource()
    db = Database()
    assert db.read_resource(resource, "key") == 10

    resource.state[0] = 20
    assert db.read_resource(resource, "key") == 20


def test_resource_labels_are_validated_but_do_not_define_node_identity() -> None:
    class LabeledResource(Resource[str, int, str]):
        def __init__(self) -> None:
            self.current_label: Any = "first"

        def identity(self) -> tuple[str]:
            return ("stable-label-resource",)

        def probe(self, key: str) -> str:
            return key

        def load(self, db: Database, key: str) -> int:
            return 1

        def label(self, key: str) -> str:
            return cast(str, self.current_label)

    resource = LabeledResource()
    store = InMemoryArtifactStore()
    db = Database(store=store)
    first_key = db._resource_key(resource, "key")
    assert db.read_resource(resource, "key") == 1

    resource.current_label = "second"
    second_key = db._resource_key(resource, "key")
    assert first_key == second_key
    assert len(db._resource_registry) == 1
    checkpoint = db.save_checkpoint()
    Database(store=store).load_checkpoint(checkpoint)

    resource.current_label = ""
    with pytest.raises(ValueError, match="non-empty"):
        db.read_resource(resource, "other")
    resource.current_label = 123
    with pytest.raises(TypeError, match="string"):
        db.read_resource(resource, "other")


def test_resource_configuration_and_parameter_preserve_nan_identity_bits() -> None:
    @dataclass(frozen=True)
    class FloatResource(Resource[float, bytes, bytes]):
        configured: float

        def identity(self) -> float:
            return self.configured

        def probe(self, key: float) -> bytes:
            return struct.pack(">d", key)

        def load(self, db: Database, key: float) -> bytes:
            return struct.pack(">d", self.configured) + struct.pack(">d", key)

        def label(self, key: float) -> str:
            return "nan-resource"

    first_nan = struct.unpack(">d", bytes.fromhex("7ff8000000000001"))[0]
    second_nan = struct.unpack(">d", bytes.fromhex("7ff8000000000002"))[0]
    first = FloatResource(first_nan)
    second = FloatResource(second_nan)
    db = Database()

    assert (
        db._resource_key(first, first_nan).identity != db._resource_key(second, second_nan).identity
    )
    assert db.read_resource(first, first_nan) == bytes.fromhex("7ff80000000000017ff8000000000001")
    assert db.read_resource(second, second_nan) == bytes.fromhex("7ff80000000000027ff8000000000002")


def test_checkpoint_schema_v4_can_warm_a_none_result() -> None:
    @query(key="none-result")
    def none_result(db: Database) -> None:
        return None

    store = InMemoryArtifactStore()
    writer = Database(store=store)
    assert writer.get(none_result) is None
    checkpoint = writer.save_checkpoint()

    reader = Database(store=store)
    reader.load_checkpoint(checkpoint)
    assert reader.get(none_result) is None
    assert reader.inspect(none_result).last_recompute == "reused"


def test_checkpoint_validation_is_complete_before_replacing_staged_state() -> None:
    @query(key="manifest-validation")
    def value(db: Database) -> int:
        return 3

    store = InMemoryArtifactStore()
    writer = Database(store=store)
    assert writer.get(value) == 3
    good_key = writer.save_checkpoint()

    reader = Database(store=store)
    reader.load_checkpoint(good_key)
    staged_before = dict(reader._checkpoint_query_records)
    manifest = json.loads(cast(bytes, store.get(good_key)).decode("utf-8"))
    manifest["records"].append(dict(manifest["records"][0]))
    malformed_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    malformed_key = "ck" + hashlib.sha256(malformed_bytes).hexdigest()
    store.put(malformed_key, malformed_bytes)

    with pytest.raises(CheckpointManifestError, match="duplicate record"):
        reader.load_checkpoint(malformed_key)
    assert reader._checkpoint_query_records == staged_before

    manifest["records"].pop()
    record = manifest["records"][0]
    record["deps"] = [
        {
            "kind": "query",
            "identity": record["identity"],
            "query_id": record["query_id"],
            "args_digest": record["args_digest"],
            "label": record["label"],
            "digest": record["snapshot_digest"],
        }
    ]
    cyclic_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    cyclic_key = "ck" + hashlib.sha256(cyclic_bytes).hexdigest()
    store.put(cyclic_key, cyclic_bytes)
    with pytest.raises(CheckpointManifestError, match="contains a cycle"):
        reader.load_checkpoint(cyclic_key)
    assert reader._checkpoint_query_records == staged_before

    record["deps"] = []
    manifest["pyinc_ckpt_version"] = 3
    old_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    old_key = "ck" + hashlib.sha256(old_bytes).hexdigest()
    store.put(old_key, old_bytes)
    with pytest.raises(CheckpointVersionError, match="Unsupported checkpoint version"):
        reader.load_checkpoint(old_key)
    assert reader._checkpoint_query_records == staged_before


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_wrapped_callable_instance_state_moves_query_identity(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    @query(key=f"wrapped-callable-state-{mode}")
    def scaled(db: Database) -> int:
        return _wrapped_scaler(10)

    db = Database(mode=mode)
    assert db.get(scaled) == 20
    monkeypatch.setattr(_wrapped_scaler, "k", 3)
    executions = db.statistics().query_executions
    warm = db.get(scaled)
    fresh = Database(mode=mode).get(scaled)
    assert warm == fresh == 30
    assert db.statistics().query_executions == executions + 1


def test_wrapped_callable_fingerprint_moves_with_instance_and_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @query(key="wrapped-callable-fingerprint")
    def scaled(db: Database) -> int:
        return _wrapped_scaler(10)

    before = Database()._query_fingerprint(scaled)
    monkeypatch.setattr(_wrapped_scaler, "k", 3)
    after_state = Database()._query_fingerprint(scaled)
    assert before != after_state

    def other_call(self: _WrappedScaler, value: int) -> int:
        return self.k * value + 1

    monkeypatch.setattr(_WrappedScaler, "__call__", other_call)
    after_call = Database()._query_fingerprint(scaled)
    assert after_call not in {before, after_state}

    # Memo path: a reused database must agree with the recomputed truth.
    db = Database()
    db._query_fingerprint(scaled)
    monkeypatch.setattr(_wrapped_scaler, "k", 7)
    memoized, truth = _memo_and_truth(db, scaled)
    assert memoized == truth


def test_class_carrying_wrapped_attribute_is_fingerprinted_as_a_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @query(key="wrapped-class-body")
    def computed(db: Database) -> int:
        return _WrappedClass.compute(1)

    before = Database()._query_fingerprint(computed)
    monkeypatch.setattr(_WrappedClass, "compute", staticmethod(_observed_compute_two))
    after = Database()._query_fingerprint(computed)
    assert before != after


def test_wrapped_callable_with_unsafe_state_is_rejected() -> None:
    @query(key="wrapped-callable-unsafe")
    def broken(db: Database) -> int:
        return _unsafe_wrapped()

    with pytest.raises(UnsupportedValueError, match="captures unsupported ambient value"):
        Database().get(broken)


def test_wrapped_callable_identity_moves_with_the_wrapped_annotations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Through 3.13 functools.wraps binds the wrapped function's own annotations
    # dictionary into the wrapper's instance dictionary; from 3.14 it leaves
    # __annotate__ there instead. The instance-state fold skips that copy on the
    # versions that place it, so what keeps the annotations inside identity on
    # every interpreter is the wrapped function's own definition payload, and
    # mutating the shared dictionary in place has to move the query.
    @query(key="wrapped-callable-shared-annotations")
    def scaled(db: Database) -> int:
        return _wrapped_scaler(10)

    before = Database()._query_fingerprint(scaled)
    monkeypatch.setitem(_wrapped_base.__annotations__, "value", "float")
    assert Database()._query_fingerprint(scaled) != before


def test_wrapped_callable_with_rebound_annotations_is_rejected() -> None:
    # The instance-state fold skips __annotations__ only while it is the very
    # object functools.wraps copied off the wrapped function. A wrapper that
    # rebinds the attribute to a dictionary of its own is holding mutable state
    # no fold can track, and it is refused like any other captured dictionary.
    @query(key="wrapped-callable-rebound-annotations")
    def broken(db: Database) -> int:
        return _rebound_annotations_wrapped()

    with pytest.raises(UnsupportedValueError, match="captures unsupported ambient value"):
        Database().get(broken)


def test_wrapped_callable_holding_a_reference_cycle_is_rejected() -> None:
    # Folding the instance state is what puts a cycle within reach, and the
    # kernel refuses cyclic ambient values rather than folding a marker for
    # them; reaching the cycle through the callable arm must reach the same
    # verdict instead of recursing.
    @query(key="wrapped-callable-cycle")
    def broken(db: Database) -> int:
        return _cyclic_wrapped()

    with pytest.raises(UnsupportedValueError, match="captures unsupported ambient value"):
        Database().get(broken)


def test_capturing_a_cache_decorated_function_is_rejected() -> None:
    @query(key="wrapped-cache-decorated")
    def broken(db: Database) -> int:
        return _wrapped_cache_decorated(3)

    # A cache-decorated function is a callable object carrying __wrapped__, and
    # its cache is state no fold can read. The module-attribute route already
    # refused the identical object; the direct capture agrees with it now
    # instead of folding the wrapped function and calling the rest invisible.
    with pytest.raises(UnsupportedValueError, match="captures unsupported ambient value"):
        Database().get(broken)


def test_capturing_a_local_class_carrying_wrapped_is_rejected() -> None:
    class _LocalWrappedClass:
        __wrapped__ = _wrapped_base
        marker = 1

    @query(key="wrapped-local-class")
    def broken(db: Database) -> int:
        return _LocalWrappedClass.marker

    # Fingerprinted as the class it is, so the local-type refusal governs it;
    # the __wrapped__ attribute no longer routes it past that check.
    with pytest.raises(UnsupportedValueError, match="Captured local type"):
        Database().get(broken)


def test_module_attribute_wrapped_callable_refusal_names_the_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_wrapped_module_refusal"
    (tmp_path / f"{module_name}.py").write_text(
        "import functools\n"
        "\n"
        "def base(value):\n"
        "    return value\n"
        "\n"
        "class Unsafe:\n"
        "    def __init__(self):\n"
        "        self.state = {'mutable': True}\n"
        "        functools.wraps(base)(self)\n"
        "    def __call__(self):\n"
        "        return 1\n"
        "\n"
        "unsafe = Unsafe()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key="wrapped-module-attribute-unsafe")
        def broken(db: Database) -> int:
            return cast(int, module.unsafe())

        # The shared payload refuses in its own vocabulary; this route says
        # which module attribute the query named, so the message identifies
        # something the reader can go and look at.
        with pytest.raises(UnsupportedValueError) as raised:
            Database().get(broken)
        message = str(raised.value)
        assert f"captures module attribute '{module_name}.unsafe'" in message
        assert "Unsafe" in message
        assert "Move mutable state behind Input/Resource nodes" in message
        assert "explain_query_captures" in message
    finally:
        sys.modules.pop(module_name, None)


def test_module_attribute_and_direct_capture_agree_on_wrapped_callables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_wrapped_module_attribute"
    (tmp_path / f"{module_name}.py").write_text(
        "import functools\n"
        "\n"
        "def base(value):\n"
        "    return value\n"
        "\n"
        "class Scaler:\n"
        "    def __init__(self, k):\n"
        "        self.k = k\n"
        "        functools.wraps(base)(self)\n"
        "    def __call__(self, value):\n"
        "        return self.k * value\n"
        "\n"
        "scaler = Scaler(2)\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:
        # The same callable object reached two ways: `import m; m.f` below and
        # the closure capture a `from m import f` produces. The two routes fold
        # different envelopes, so their fingerprints differ; what has to agree
        # is that both accept it and both move when its state does.
        captured_scaler = module.scaler

        @query(key="wrapped-module-attribute")
        def scaled(db: Database) -> int:
            return cast(int, module.scaler(10))

        @query(key="wrapped-direct-capture")
        def scaled_direct(db: Database) -> int:
            return cast(int, captured_scaler(10))

        db = Database()
        direct_db = Database()
        assert db.get(scaled) == 20
        assert direct_db.get(scaled_direct) == 20
        before = Database()._query_fingerprint(scaled)
        before_direct = Database()._query_fingerprint(scaled_direct)
        monkeypatch.setattr(module.scaler, "k", 3)
        assert Database()._query_fingerprint(scaled) != before
        assert Database()._query_fingerprint(scaled_direct) != before_direct

        executions = db.statistics().query_executions
        assert db.get(scaled) == Database().get(scaled) == 30
        assert db.statistics().query_executions == executions + 1

        direct_executions = direct_db.statistics().query_executions
        assert direct_db.get(scaled_direct) == Database().get(scaled_direct) == 30
        assert direct_db.statistics().query_executions == direct_executions + 1
    finally:
        sys.modules.pop(module_name, None)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_function_behind_a_captured_cached_property_matches_fresh(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    @query(key=f"cached-property-fsc-{mode}")
    def read(db: Database) -> int:
        return _ObservedCachedHolder().read

    db = Database(mode=mode)
    assert db.get(read) == 30
    monkeypatch.setattr(
        sys.modules[__name__], "_observed_cached_source", _observed_descriptor_replacement
    )
    _assert_warm_matches_fresh(db, mode, read, 110)


def test_captured_cached_property_folds_the_function_behind_the_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @query(key="cached-property-fingerprint")
    def read(db: Database) -> int:
        return _ObservedCachedHolder().read

    before = Database()._query_fingerprint(read)
    monkeypatch.setattr(
        sys.modules[__name__], "_observed_cached_source", _observed_descriptor_replacement
    )
    after = Database()._query_fingerprint(read)
    assert before != after


def test_memoized_fingerprint_tracks_functions_behind_a_captured_cached_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @query(key="memo-descriptor-cached-property")
    def read(db: Database) -> int:
        return _ObservedCachedHolder().read

    db = Database()
    db._query_fingerprint(read)
    monkeypatch.setattr(
        sys.modules[__name__], "_observed_cached_source", _observed_descriptor_replacement
    )
    memoized, truth = _memo_and_truth(db, read)
    assert memoized == truth
    assert memoized == Database()._query_fingerprint(read)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_query_handle_attribute_change_matches_fresh(mode: str) -> None:
    # A query body may read attributes off its own handle. Writing one is a
    # supported way to reparameterize the query: identity moves with the
    # attribute, so the stored record no longer answers and the new value is
    # recomputed rather than served stale.
    @query(key=f"handle-state-{mode}")
    def selfread(db: Database) -> int:
        return int(cast(Any, selfread).threshold)

    cast(Any, selfread).threshold = 1
    db = Database(mode=mode)
    assert db.get(selfread) == 1
    cast(Any, selfread).threshold = 5
    _assert_warm_matches_fresh(db, mode, selfread, 5)


def test_query_handle_doc_and_attributes_move_the_fingerprint() -> None:
    @query(key="handle-fingerprint")
    def documented(db: Database) -> int:
        """Handle docstring."""

        return 1

    before = Database()._query_fingerprint(documented)
    cast(Any, documented).__doc__ = "Changed handle docstring."
    after_doc = Database()._query_fingerprint(documented)
    assert before != after_doc

    cast(Any, documented).threshold = 9
    after_attr = Database()._query_fingerprint(documented)
    assert after_attr not in {before, after_doc}

    # Memo path: a reused database must agree with the recomputed truth.
    db = Database()
    db._query_fingerprint(documented)
    cast(Any, documented).threshold = 10
    memoized, truth = _memo_and_truth(db, documented)
    assert memoized == truth
    assert memoized == Database()._query_fingerprint(documented)


def test_captured_query_handle_state_moves_the_parent_fingerprint() -> None:
    @query(key="handle-helper")
    def helper(db: Database) -> int:
        return 1

    @query(key="handle-parent")
    def parent(db: Database) -> int:
        return helper(db) + 1

    db = Database()
    db._query_fingerprint(parent)
    before = Database()._query_fingerprint(parent)
    cast(Any, helper).__doc__ = "Rebound helper documentation."
    after = Database()._query_fingerprint(parent)
    assert before != after

    memoized, truth = _memo_and_truth(db, parent)
    assert memoized == truth
    assert memoized == after


def test_module_reached_query_handle_state_moves_the_parent_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_module_query_handle"
    (tmp_path / f"{module_name}.py").write_text(
        "from pyinc import Database, query\n"
        "\n"
        "\n"
        '@query(key="module-handle-child")\n'
        "def child(db: Database) -> int:\n"
        "    return 1\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key="module-handle-parent")
        def parent(db: Database) -> int:
            return cast(int, module.child(db)) + 1

        db = Database()
        db._query_fingerprint(parent)
        before = Database()._query_fingerprint(parent)
        # The chain names child, whose object identity this write leaves alone,
        # and a Query is not one of the module constants the stamp folds, so
        # neither of those memo arms moves: the handle fold is what sees it,
        # and the observation has to follow it through the same chain.
        monkeypatch.setattr(module.child, "__doc__", "Rebound child documentation.")
        after = Database()._query_fingerprint(parent)
        assert before != after

        memoized, truth = _memo_and_truth(db, parent)
        assert memoized == truth
        assert memoized == after
    finally:
        sys.modules.pop(module_name, None)


def test_query_handle_annotations_of_its_own_move_the_fingerprint() -> None:
    @query(key="handle-annotations")
    def annotated(db: Database, value: int) -> int:
        return value

    db = Database()
    db._query_fingerprint(annotated)
    before = Database()._query_fingerprint(annotated)
    # The handle starts out carrying the function's own annotations, which the
    # function payload folds; giving it annotations of its own is what this
    # fold has to see, on either carrier the interpreter uses.
    cast(Any, annotated).__annotations__ = {"value": str, "return": int}
    with_eager = Database()._query_fingerprint(annotated)
    assert with_eager != before
    memoized, truth = _memo_and_truth(db, annotated)
    assert memoized == truth
    assert memoized == with_eager

    # Their content, not merely their presence: a fold that noticed only that
    # the handle had annotations of its own would answer the same here.
    cast(Any, annotated).__annotations__ = {"value": bytes, "return": int}
    with_other_eager = Database()._query_fingerprint(annotated)
    assert with_other_eager not in {before, with_eager}
    memoized, truth = _memo_and_truth(db, annotated)
    assert memoized == truth
    assert memoized == with_other_eager

    def evaluate(format: int) -> dict[str, Any]:
        return {"value": bytes, "return": int}

    cast(Any, annotated).__annotate__ = evaluate
    with_evaluator = Database()._query_fingerprint(annotated)
    assert with_evaluator not in {before, with_eager}
    memoized, truth = _memo_and_truth(db, annotated)
    assert memoized == truth
    assert memoized == with_evaluator


def test_reflective_annotation_evaluator_on_a_query_handle_is_rejected() -> None:
    @query(key="handle-reflective-annotate")
    def annotated(db: Database) -> int:
        return 1

    def evaluate(format: int) -> dict[str, Any]:
        return {"return": globals()["_ObservedConsts"]}

    # An annotation evaluator is folded by resolving the names its code
    # references against its globals, exactly as any other captured function
    # is, so a read that names none of them escapes the fold here too. The
    # same function on any other handle attribute is refused through the
    # function-definition route; this is the third route to that fold.
    cast(Any, annotated).__annotate__ = evaluate
    with pytest.raises(UnsupportedValueError, match=r"reads a namespace reflectively \(globals\)"):
        Database()._query_fingerprint(annotated)


def test_rebound_wrapped_function_on_a_query_handle_matches_fresh() -> None:
    # functools.wraps points __wrapped__ at the decorated function, and a body
    # can call whatever it points at now. Rebinding it is the same kind of
    # write as rebinding any other attribute on the handle, so it moves
    # identity the same way -- exactly as rebinding __wrapped__ on a plain
    # captured function does.
    @query(key="handle-wrapped-rebind")
    def reader(db: Database) -> int:
        return int(cast(Any, reader).__wrapped__(db))

    cast(Any, reader).__wrapped__ = _handle_wrapped_one
    db = Database()
    assert db.get(reader) == 1

    before = Database()._query_fingerprint(reader)
    cast(Any, reader).__wrapped__ = _handle_wrapped_two
    after = Database()._query_fingerprint(reader)
    assert after != before

    memoized, truth = _memo_and_truth(db, reader)
    assert memoized == truth
    assert memoized == after
    _assert_warm_matches_fresh(db, "strict", reader, 2)


def test_memoized_fingerprint_tracks_metadata_behind_a_rebound_wrapped_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @query(key="memo-handle-wrapped")
    def reader(db: Database) -> int:
        return int(cast(Any, reader).__wrapped__(db))

    cast(Any, reader).__wrapped__ = _handle_wrapped_one
    db = Database()
    db._query_fingerprint(reader)
    before = Database()._query_fingerprint(reader)
    # Once __wrapped__ points somewhere other than the query's own function,
    # the payload folds that function's definition live. The handle's
    # reference to it does not move when its metadata does, and the module
    # stamp carries no function metadata either, so the observation has to
    # follow the function itself rather than pin it by reference.
    monkeypatch.setattr(_handle_wrapped_one, "__doc__", "Rebound helper documentation.")
    truth = Database()._query_fingerprint(reader)
    assert truth != before
    memoized, recomputed = _memo_and_truth(db, reader)
    assert memoized == recomputed == truth


def test_query_handles_holding_a_reference_cycle_have_finite_identity() -> None:
    @query(key="handle-cycle-first")
    def first(db: Database) -> int:
        return 1

    @query(key="handle-cycle-second")
    def second(db: Database) -> int:
        return 2

    @query(key="handle-cycle-solo")
    def solo(db: Database) -> int:
        return 3

    # A query held on another query's handle is folded as the dependency it is,
    # so a pair holding each other -- or a handle holding itself -- is a cycle
    # the fold has to survive. The repeat is marked, and what it would have
    # folded is still folded by the contact that entered the handle: writing an
    # attribute on either side of the cycle still moves the other's identity.
    cast(Any, first).peer = second
    cast(Any, second).peer = first
    cast(Any, solo).mine = solo

    before_first = Database()._query_fingerprint(first)
    cast(Any, second).threshold = 5
    assert Database()._query_fingerprint(first) != before_first

    before_solo = Database()._query_fingerprint(solo)
    cast(Any, solo).threshold = 7
    assert Database()._query_fingerprint(solo) != before_solo


def test_query_handle_with_unsafe_attribute_is_rejected() -> None:
    @query(key="handle-unsafe")
    def broken(db: Database) -> int:
        return 1

    cast(Any, broken).state = {"mutable": True}
    # Named as what it is -- state written on the handle, not a value the body
    # closed over -- and carrying the same remedy the capture refusals give.
    with pytest.raises(
        UnsupportedValueError,
        match=r"Query 'handle-unsafe' holds unsupported state 'state' of type builtins.dict",
    ):
        Database().get(broken)


def test_query_handle_with_a_non_string_attribute_name_is_refused() -> None:
    @query(key="handle-non-string-name")
    def direct(db: Database) -> int:
        return 1

    @query(key="handle-non-string-name-child")
    def child(db: Database) -> int:
        return 1

    @query(key="handle-non-string-name-parent")
    def parent(db: Database) -> int:
        return child(db) + 1

    # A handle dictionary given a name that is not a string is reached by the
    # memo observation before it is reached by the fold -- for the query being
    # keyed and for one its body captures alike -- so the observation has to
    # answer the fold's refusal rather than leave it to the fold. Otherwise the
    # observation's own sort compares that name against a string first and the
    # caller sees a raw TypeError.
    cast(dict[Any, Any], direct.__dict__)[7] = 1
    cast(dict[Any, Any], child.__dict__)[7] = 1
    routes = (
        (direct, "handle-non-string-name"),
        (parent, "handle-non-string-name-child"),
    )
    for handle, key in routes:
        with pytest.raises(
            UnsupportedValueError,
            match=rf"Query handle '{key}' has invalid custom state\.",
        ):
            Database().get(handle)


def test_module_reached_query_handle_with_a_non_string_name_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_module_query_handle_non_string_name"
    (tmp_path / f"{module_name}.py").write_text(
        "from pyinc import Database, query\n"
        "\n"
        "\n"
        '@query(key="module-handle-non-string-name-child")\n'
        "def child(db: Database) -> int:\n"
        "    return 1\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key="module-handle-non-string-name-parent")
        def parent(db: Database) -> int:
            return cast(int, module.child(db)) + 1

        # The route the memo observation cannot answer for: a module is a leaf
        # of the definition observation, so a handle reached through a module
        # attribute chain is folded without that walk ever reaching it. The
        # refusal here has to come out of the fold's own guard; take that guard
        # away and the sort behind it hands the caller a raw TypeError.
        cast(dict[Any, Any], module.child.__dict__)[7] = 1
        with pytest.raises(
            UnsupportedValueError,
            match=r"Query handle 'module-handle-non-string-name-child' has invalid custom state\.",
        ):
            Database().get(parent)
    finally:
        sys.modules.pop(module_name, None)


_REFLECTIVE_FIXTURE_SOURCE = '''\
"""Five reflective reads of one mutable module global, plus a benign getattr."""

import sys

CONFIG_MODE = "A"


def via_globals():
    return globals()["CONFIG_MODE"]


def via_vars():
    return vars(sys.modules[__name__])["CONFIG_MODE"]


def via_getattr():
    return getattr(sys.modules[__name__], "CONFIG_MODE")


def via_dict():
    return sys.modules[__name__].__dict__["CONFIG_MODE"]


def via_eval():
    return eval("CONFIG_MODE")


def benign_getattr(target):
    return getattr(target, "value", None)
'''


@pytest.mark.parametrize(
    "shape", ["via_globals", "via_vars", "via_getattr", "via_dict", "via_eval"]
)
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_reflective_namespace_reads_are_rejected(
    shape: str, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = f"pyinc_reflective_{shape}_{mode}"
    (tmp_path / f"{module_name}.py").write_text(_REFLECTIVE_FIXTURE_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    reader = getattr(module, shape)

    @query(key=f"reflective-{shape}-{mode}")
    def read_config(db: Database) -> str:
        return cast(str, reader())

    with pytest.raises(UnsupportedValueError, match="reads a namespace reflectively"):
        Database(mode=mode).get(read_config)


def test_direct_module_attribute_reads_stay_accepted_and_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_reflective_direct_control"
    (tmp_path / f"{module_name}.py").write_text(_REFLECTIVE_FIXTURE_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)

    @query(key="reflective-direct-control")
    def read_config(db: Database) -> str:
        return cast(str, module.CONFIG_MODE)

    db = Database()
    assert db.get(read_config) == "A"
    monkeypatch.setattr(module, "CONFIG_MODE", "B")
    assert db.get(read_config) == Database().get(read_config) == "B"


def test_benign_getattr_on_ordinary_objects_stays_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_reflective_benign_control"
    (tmp_path / f"{module_name}.py").write_text(_REFLECTIVE_FIXTURE_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    benign = module.benign_getattr

    @query(key="reflective-benign-control")
    def probed(db: Database) -> Any:
        return benign(_ObservedBox(3))

    assert Database().get(probed) is None


_UNCONDITIONAL_REFLECTIVE_SOURCE = '''\
"""A locals() read and an exec call, each an offense on the builtin alone."""

CONFIG_MODE = "A"


def via_locals():
    return locals().get("mode", CONFIG_MODE)


def via_exec():
    scope = {"CONFIG_MODE": CONFIG_MODE}
    exec("value = CONFIG_MODE", scope)
    return scope["value"]
'''


@pytest.mark.parametrize(
    "shape, offense", [("via_locals", "locals"), ("via_exec", "exec")]
)
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_locals_and_exec_reads_are_rejected(
    shape: str, offense: str, mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = f"pyinc_unconditional_reflective_{shape}_{mode}"
    (tmp_path / f"{module_name}.py").write_text(
        _UNCONDITIONAL_REFLECTIVE_SOURCE, encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:
        reader = getattr(module, shape)

        @query(key=f"unconditional-reflective-{shape}-{mode}")
        def read_config(db: Database) -> str:
            return cast(str, reader())

        # Neither of these needs the module-namespace handle the getattr family
        # is judged beside: the builtin's own load is the offense. What gets
        # read through the namespace either one hands back is chosen while the
        # body runs, so the static walk that resolves names has nothing to
        # resolve. Both spellings are refused in every mode, so no mode trades
        # the refusal for a cheaper fingerprint.
        with pytest.raises(UnsupportedValueError, match=rf"reflectively \({offense}"):
            Database(mode=mode).get(read_config)
    finally:
        sys.modules.pop(module_name, None)


_FUNCTION_SCOPE_IMPORT_SOURCE = '''\
"""Two getattr namespace reads whose module handle is imported at function scope."""

CONFIG_MODE = "A"


def via_function_scope_import():
    import sys

    return getattr(sys.modules[__name__], "CONFIG_MODE")


def via_function_scope_importlib():
    import importlib

    return getattr(importlib.import_module(__name__), "CONFIG_MODE")
'''


def test_function_scope_import_of_sys_is_reached_through_the_module_table_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_reflective_function_scope_import"
    (tmp_path / f"{module_name}.py").write_text(_FUNCTION_SCOPE_IMPORT_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:
        reader = module.via_function_scope_import

        @query(key="reflective-function-scope-import")
        def read_config(db: Database) -> str:
            return cast(str, reader())

        # An import inside the body binds sys as a local, so no global load of
        # the name exists to key on -- but the module table is still reached
        # by an attribute load, and that is what the rule reads. The import's
        # scope makes no difference to this spelling.
        with pytest.raises(UnsupportedValueError, match="reads a namespace reflectively"):
            Database().get(read_config)
    finally:
        sys.modules.pop(module_name, None)


def test_function_scope_importlib_is_reached_through_the_import_module_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_reflective_function_scope_importlib"
    (tmp_path / f"{module_name}.py").write_text(_FUNCTION_SCOPE_IMPORT_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:
        reader = module.via_function_scope_importlib

        @query(key="reflective-function-scope-importlib")
        def read_config(db: Database) -> str:
            return cast(str, reader())

        # An import inside the body binds importlib as a local, so no global
        # load of the name exists to key on -- but the module builder is
        # still reached by an attribute load, and that is what the rule
        # reads. The import's scope makes no difference to this spelling, as
        # it makes none to the module table.
        with pytest.raises(UnsupportedValueError, match="reads a namespace reflectively"):
            Database().get(read_config)
    finally:
        sys.modules.pop(module_name, None)


_ALIASED_IMPORTLIB_SOURCE = '''\
"""One mutable module global, reached twice through an aliased importlib."""

import importlib as _il

CONFIG_MODE = "A"


def via_aliased_importlib_getattr():
    return getattr(_il.import_module(__name__), "CONFIG_MODE")


def via_aliased_import_module_attribute():
    return _il.import_module(__name__).CONFIG_MODE
'''


def test_getattr_through_an_aliased_importlib_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_reflective_aliased_importlib"
    (tmp_path / f"{module_name}.py").write_text(_ALIASED_IMPORTLIB_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:
        reader = module.via_aliased_importlib_getattr

        @query(key="reflective-aliased-importlib")
        def read_config(db: Database) -> str:
            return cast(str, reader())

        # Aliasing importlib on import changes the name the reading code
        # loads and nothing else: the call still loads import_module as an
        # attribute, and that load is the handle the rule keys on, exactly as
        # the modules load is for an aliased sys.
        with pytest.raises(UnsupportedValueError, match="reads a namespace reflectively"):
            Database().get(read_config)
    finally:
        sys.modules.pop(module_name, None)


def test_import_module_alone_stays_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_reflective_import_module_alone"
    (tmp_path / f"{module_name}.py").write_text(_ALIASED_IMPORTLIB_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:
        reader = module.via_aliased_import_module_attribute

        @query(key="reflective-import-module-alone")
        def read_config(db: Database) -> str:
            return cast(str, reader())

        # Building a module handle marks a handle; it is the reflective
        # builtin beside it that is refused. Calling import_module and
        # reading an attribute off what comes back uses none of those
        # builtins, so this shape is accepted and its read escapes capture
        # identity -- importlib is a standard-library module, whose captured
        # payload folds the names read off it rather than the state behind
        # them. The boundary is recorded here so a later change to the rule
        # has to answer for it deliberately.
        assert Database().get(read_config) == "A"
    finally:
        sys.modules.pop(module_name, None)


_FROM_IMPORTED_IMPORT_MODULE_SOURCE = '''\
"""One mutable module global, reached through a from-imported import_module."""

from importlib import import_module

CONFIG_MODE = "A"


def via_from_imported_import_module():
    return getattr(import_module(__name__), "CONFIG_MODE")
'''


def test_from_imported_import_module_stays_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_reflective_from_imported_import_module"
    (tmp_path / f"{module_name}.py").write_text(
        _FROM_IMPORTED_IMPORT_MODULE_SOURCE, encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:
        reader = module.via_from_imported_import_module

        @query(key="reflective-from-imported-import-module")
        def read_config(db: Database) -> str:
            return cast(str, reader())

        # A from-import lifts the callable out of importlib, so the reading
        # code loads neither the name importlib nor any attribute this rule
        # reads: the call is an ordinary global load and the getattr beside it
        # is never armed. This is the rule's documented boundary rather than an
        # oversight. Closing it means keying on the bare global name
        # import_module, which arms the handle for any function that loads a
        # global of that name beside an ordinary getattr -- whatever the
        # callable behind the name actually is, and however unrelated to a
        # module namespace. Recorded here so a later change to the rule has to
        # answer for it deliberately.
        assert Database().get(read_config) == "A"
    finally:
        sys.modules.pop(module_name, None)


_MODULE_TABLE_FIXTURE_SOURCE = '''\
"""One mutable module global, reached through the module table three ways."""

import sys
import sys as _aliased_sys

CONFIG_MODE = "A"


def via_getattr_on_sys():
    return getattr(sys, "modules")[__name__].CONFIG_MODE


def via_getattr_on_aliased_sys():
    return getattr(_aliased_sys, "modules")[__name__].CONFIG_MODE


def via_aliased_module_table_subscript():
    return _aliased_sys.modules[__name__].CONFIG_MODE
'''


@pytest.mark.parametrize("shape", ["via_getattr_on_sys", "via_getattr_on_aliased_sys"])
def test_getattr_of_the_module_table_is_rejected(
    shape: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = f"pyinc_reflective_module_table_{shape}"
    (tmp_path / f"{module_name}.py").write_text(_MODULE_TABLE_FIXTURE_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:
        reader = getattr(module, shape)

        @query(key=f"reflective-module-table-{shape}")
        def read_config(db: Database) -> str:
            return cast(str, reader())

        # getattr(sys, "modules") is the getattr-family spelling of the very
        # handle the rule keys on: it loads no attribute named modules, so the
        # string it passes instead is what marks the reach. Aliasing sys on
        # import changes the name the reading code loads and nothing else.
        with pytest.raises(UnsupportedValueError, match="reads a namespace reflectively"):
            Database().get(read_config)
    finally:
        sys.modules.pop(module_name, None)


def test_module_table_subscripting_alone_stays_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_reflective_module_table_subscript"
    (tmp_path / f"{module_name}.py").write_text(_MODULE_TABLE_FIXTURE_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:
        reader = module.via_aliased_module_table_subscript

        @query(key="reflective-module-table-subscript")
        def read_config(db: Database) -> str:
            return cast(str, reader())

        # Reaching the module table marks a handle; it is the reflective
        # builtin beside it that is refused. Subscripting the table and
        # reading an attribute off what comes back uses none of those
        # builtins, so this shape is accepted and its read escapes capture
        # identity -- sys is a standard-library module, whose captured payload
        # folds the names read off it rather than the state behind them. The
        # boundary is recorded here so a later change to the rule has to
        # answer for it deliberately.
        assert Database().get(read_config) == "A"
    finally:
        sys.modules.pop(module_name, None)


_FUNCTION_GLOBALS_FIXTURE_SOURCE = '''\
"""A helper function beside the mutable module state its __globals__ reaches."""

SECRET = {"v": 1}


def helper():
    return 1
'''


def test_reading_a_captured_functions_globals_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_reflective_function_globals"
    (tmp_path / f"{module_name}.py").write_text(
        _FUNCTION_GLOBALS_FIXTURE_SOURCE, encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    try:

        @query(key="reflective-function-globals")
        def read_secret(db: Database) -> int:
            return cast(int, module.helper.__globals__["SECRET"]["v"])

        # An attribute chain that lands on a function folds that function's
        # own definition and stops there. __globals__ carries the whole
        # defining module dictionary past the landing, so the mutable state
        # behind it reaches the answer while moving nothing the fingerprint
        # sees -- the same escape globals() opens from inside the module, and
        # refused for the same reason.
        with pytest.raises(
            UnsupportedValueError, match=r"reads a namespace reflectively \(__globals__\)"
        ):
            Database().get(read_secret)
    finally:
        sys.modules.pop(module_name, None)
