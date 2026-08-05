from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from types import FunctionType
from typing import Any, ParamSpec, TypeVar, cast

import pytest

from pyinc import Database, InMemoryArtifactStore, Input, Query, UnsupportedValueError, query

_MODES = ("strict", "checked", "fast")
_P = ParamSpec("_P")
_T = TypeVar("_T")


def _wrapped_sum(*values: int) -> int:
    return sum(values)


class _StatefulCallableWrapper:
    def __init__(self, offset: int) -> None:
        self.__wrapped__ = _wrapped_sum
        self.offset = offset
        self.calls = 0

    def __call__(self, *values: int) -> int:
        self.calls += 1
        return self.__wrapped__(*values) + self.offset


def _transparent_decorator(fn: Callable[_P, _T]) -> Callable[_P, _T]:
    @wraps(fn)
    def decorated(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        return fn(*args, **kwargs)

    return decorated


@_transparent_decorator
def _decorated_increment(value: int) -> int:
    return value + 1


@dataclass(frozen=True)
class _DecoratedHelper:
    @_transparent_decorator
    def increment(self, value: int) -> int:
        return value + 1


class _CallableResource:
    __wrapped__ = _wrapped_sum

    def __call__(self, value: int) -> int:
        return value + 100

    def identity(self) -> tuple[str]:
        return ("callable-resource",)

    def probe(self, key: int) -> int:
        return key

    def load(self, db: Database, key: int) -> int:
        return key + 1

    def label(self, key: int) -> str:
        return f"callable-resource[{key}]"


class _CallableHookResource:
    def __init__(self, hook_name: str, hook: _StatefulCallableWrapper) -> None:
        setattr(self, hook_name, hook)

    def identity(self) -> tuple[str]:
        return ("callable-hook-resource",)

    def probe(self, key: int) -> int:
        return key

    def load(self, db: Database, key: int) -> int:
        return key

    def probe_and_load(self, db: Database, key: int) -> tuple[int, int]:
        return key, key

    def label(self, key: int) -> str:
        return f"callable-hook-resource[{key}]"


class _LegacyWrappedDatabase(Database):
    """Emit the pre-KER-04 identity solely to construct a hostile checkpoint."""

    def _captured_dependency_digest(
        self,
        name: str,
        value: Any,
        seen_functions: set[int],
        *,
        owner: FunctionType,
    ) -> Any:
        wrapped_function = getattr(value, "__wrapped__", None)
        if isinstance(wrapped_function, FunctionType) and callable(value):
            return (
                "wrapped-function",
                type(value).__module__,
                type(value).__qualname__,
                self._function_definition_payload(wrapped_function, seen_functions),
            )
        return super()._captured_dependency_digest(
            name,
            value,
            seen_functions,
            owner=owner,
        )


def _wrapper_query(
    wrapper: _StatefulCallableWrapper,
    *,
    key: str,
    nested: bool = False,
) -> Query[[], int]:
    capture: object = (wrapper,) if nested else wrapper

    @query(key=key)
    def value(db: Database) -> int:
        if isinstance(capture, tuple):
            selected = cast(_StatefulCallableWrapper, capture[0])
        else:
            selected = cast(_StatefulCallableWrapper, capture)
        return selected(10)

    return value


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("nested", [False, True], ids=["direct", "nested"])
def test_callable_object_wrappers_are_rejected_before_query_publication(
    mode: str,
    nested: bool,
) -> None:
    wrapper = _StatefulCallableWrapper(1)
    value = _wrapper_query(
        wrapper,
        key=f"callable-wrapper-rejected:{mode}:{nested}",
        nested=nested,
    )
    warm = Database(mode=mode)

    with pytest.raises(UnsupportedValueError, match="callable object|unsupported ambient value"):
        warm.get(value)

    wrapper.offset = 2
    with pytest.raises(UnsupportedValueError):
        warm.get(value)
    with pytest.raises(UnsupportedValueError):
        Database(mode=mode).get(value)

    assert wrapper.calls == 0
    assert warm.revision == 0
    assert warm.statistics().query_count == 0
    assert warm.statistics().query_executions == 0


@pytest.mark.parametrize("mode", _MODES)
def test_ordinary_decorated_functions_bound_methods_and_query_handles_remain_supported(
    mode: str,
) -> None:
    method = _DecoratedHelper().increment

    @query(key=f"decorated-child:{mode}")
    def child(db: Database) -> int:
        return _decorated_increment(1)

    @query(key=f"decorated-parent:{mode}")
    def parent(db: Database) -> int:
        return child(db) + method(1)

    warm = Database(mode=mode)
    assert warm.get(parent) == 4
    assert warm.get(parent) == 4
    assert Database(mode=mode).get(parent) == 4


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("policy_owner", ["query", "input"])
@pytest.mark.parametrize("policy_kind", ["eq", "cutoff"])
def test_callable_object_wrapper_policies_are_rejected_atomically(
    mode: str,
    policy_owner: str,
    policy_kind: str,
) -> None:
    wrapper = _StatefulCallableWrapper(0)
    db = Database(mode=mode)

    if policy_owner == "query":
        def raw_value(database: Database) -> int:
            return 1

        if policy_kind == "eq":
            value = Query(
                raw_value,
                key=f"callable-wrapper-policy:{mode}:{policy_kind}",
                eq=cast(Any, wrapper),
            )
        else:
            value = Query(
                raw_value,
                key=f"callable-wrapper-policy:{mode}:{policy_kind}",
                cutoff=cast(Any, wrapper),
            )
        with pytest.raises(UnsupportedValueError, match="Equality/cutoff policy"):
            db.get(value)
    else:
        if policy_kind == "eq":
            source = Input[int](
                f"callable-wrapper-policy:{mode}:{policy_kind}",
                eq=cast(Any, wrapper),
            )
        else:
            source = Input[int](
                f"callable-wrapper-policy:{mode}:{policy_kind}",
                cutoff=cast(Any, wrapper),
            )
        with pytest.raises(UnsupportedValueError, match="Equality/cutoff policy"):
            db.set(source, 1)

    assert wrapper.calls == 0
    assert db.revision == 0
    assert db.statistics().node_count == 0


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("hook_name", ["identity", "probe", "load", "probe_and_load"])
def test_callable_object_resource_hooks_are_rejected_but_resources_remain_supported(
    mode: str,
    hook_name: str,
) -> None:
    hook = _StatefulCallableWrapper(1)
    rejected = _CallableHookResource(hook_name, hook)
    db = Database(mode=mode)

    with pytest.raises(UnsupportedValueError, match=rf"Resource hook.*{hook_name}"):
        db.read_resource(rejected, 1)

    assert hook.calls == 0
    assert db.statistics().resource_count == 0
    assert db.read_resource(_CallableResource(), 1) == 2


@pytest.mark.parametrize("mode", _MODES)
def test_legacy_callable_wrapper_checkpoint_cannot_publish_stale_state(mode: str) -> None:
    wrapper = _StatefulCallableWrapper(1)
    value = _wrapper_query(
        wrapper,
        key=f"legacy-callable-wrapper-checkpoint:{mode}",
    )
    store = InMemoryArtifactStore()
    writer = _LegacyWrappedDatabase(mode=mode, store=store)

    assert writer.get(value) == 11
    checkpoint = writer.save_checkpoint()
    wrapper.offset = 2

    reader = Database(mode=mode, store=store)
    reader.load_checkpoint(checkpoint)
    assert reader._checkpoint_query_records

    with pytest.raises(UnsupportedValueError, match="callable object"):
        reader.get(value)
    with pytest.raises(UnsupportedValueError):
        Database(mode=mode).get(value)

    assert wrapper.calls == 1
    assert reader.revision == 0
    assert reader.statistics().query_count == 0
    assert reader.statistics().query_executions == 0
