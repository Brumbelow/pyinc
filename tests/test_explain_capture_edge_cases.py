from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from pyinc import Database, FileResource
from pyinc.explain import (
    _classify_capture,
    _classify_instance_dict,
    _classify_value_capture,
    _instance_dict,
    _instance_slots,
    _is_resource_handle,
    _type_capture_rejection,
    explain_query_captures,
)
from pyinc.runtime import Database as RuntimeDatabase
from pyinc.value import (
    FrozenAdapterValue,
    FrozenDict,
    FrozenList,
    FrozenRecord,
    FrozenSet,
)


class _Outer:
    class Nested:
        pass


class _StatefulInt(int):
    metadata: object


class _SlottedString(str):
    __slots__ = ("metadata",)
    metadata: object


class _TupleSubclass(tuple[Any, ...]):
    metadata: object


class _FrozenSetSubclass(frozenset[Any]):
    metadata: object


class _SlottedFrozenSet(frozenset[Any]):
    __slots__ = ("metadata",)
    metadata: object


class _PathWithState(os.PathLike[str]):
    def __init__(self, path: str, metadata: object) -> None:
        self.path = path
        self.metadata = metadata

    def __fspath__(self) -> str:
        return self.path


class _SlottedPath(os.PathLike[str]):
    __slots__ = ("path",)

    def __init__(self, path: str) -> None:
        self.path = path

    def __fspath__(self) -> str:
        return self.path


class _ExtraSlot:
    __slots__ = ("metadata",)


@dataclass(frozen=True)
class _FrozenConfig:
    value: object


@dataclass
class _MutableConfig:
    value: object


@dataclass(frozen=True)
class _FrozenConfigWithExtraSlot(_ExtraSlot):
    value: object


def _non_dict_state(instance: object) -> tuple[()]:
    return ()


_NonDictState = type(
    "_NonDictState",
    (),
    {"__module__": __name__, "__dict__": property(_non_dict_state)},
)


def _annotation_evaluator(format: int) -> dict[str, object]:
    return {"return": int, "format": format}


def test_type_capture_rejection_distinguishes_stable_and_unbound_types() -> None:
    assert _type_capture_rejection(int) == ""
    assert _type_capture_rejection(_Outer.Nested) == ""

    ghost = type("MissingBinding", (), {"__module__": __name__})
    assert "live binding" in _type_capture_rejection(ghost)

    missing_module = type("MissingModule", (), {"__module__": "not_imported_for_test"})
    assert "live binding" in _type_capture_rejection(missing_module)

    class LocalType:
        pass

    assert "Local type definitions" in _type_capture_rejection(LocalType)


def test_instance_state_helpers_handle_slots_missing_dict_and_unsafe_state() -> None:
    class StringSlot:
        __slots__ = "value"

    class IgnoredSlots:
        __slots__ = ("__dict__", "__weakref__", "value")

    assert _instance_slots(StringSlot) == {"value"}
    assert _instance_slots(IgnoredSlots) == {"value"}
    assert _instance_dict(object()) == {}
    assert _instance_dict(_NonDictState()) == {}

    safe = _PathWithState("safe", (1, 2))
    unsafe = _PathWithState("unsafe", [1, 2])
    assert _classify_instance_dict(safe, set()) == (True, "")
    accepted, reason = _classify_instance_dict(unsafe, set())
    assert not accepted
    assert reason == "Unsupported ambient capture."


@pytest.mark.parametrize(
    "value",
    [
        "text",
        b"bytes",
        1,
        1.5,
        True,
        None,
        2j,
        int,
        range(1, 4),
        frozenset({1, 2}),
        Path("somewhere"),
        FrozenList(()),
        FrozenDict(()),
        FrozenSet("set", ()),
        FrozenRecord("Record", ()),
        FrozenAdapterValue("tests:Adapter", None),
    ],
)
def test_value_capture_classifier_accepts_snapshot_safe_values(value: object) -> None:
    assert _classify_value_capture(value, set()) == (True, "")


def test_scalar_subclass_capture_checks_type_slots_and_instance_state() -> None:
    safe = _StatefulInt(3)
    safe.metadata = ("stable",)
    assert _classify_value_capture(safe, set()) == (True, "")

    unsafe = _StatefulInt(4)
    unsafe.metadata = []
    accepted, reason = _classify_value_capture(unsafe, set())
    assert not accepted
    assert reason == "Unsupported ambient capture."

    slotted = _SlottedString("value")
    slotted.metadata = "state"
    accepted, reason = _classify_value_capture(slotted, set())
    assert not accepted
    assert reason == "Scalar subclass slot state cannot be fingerprinted safely."

    class LocalInt(int):
        pass

    accepted, reason = _classify_value_capture(LocalInt(1), set())
    assert not accepted
    assert "Local type definitions" in reason


def test_pathlike_capture_checks_type_slots_and_instance_state() -> None:
    assert _classify_value_capture(_PathWithState("safe", (1, 2)), set()) == (True, "")

    accepted, reason = _classify_value_capture(_PathWithState("unsafe", []), set())
    assert not accepted
    assert reason == "Unsupported ambient capture."

    accepted, reason = _classify_value_capture(_SlottedPath("unsafe"), set())
    assert not accepted
    assert reason == "Path-like slot state cannot be fingerprinted safely."

    class LocalPath(os.PathLike[str]):
        def __fspath__(self) -> str:
            return "local"

    accepted, reason = _classify_value_capture(LocalPath(), set())
    assert not accepted
    assert "Local type definitions" in reason


def test_tuple_subclass_capture_checks_contents_cycles_type_and_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe = _TupleSubclass((1, (2,)))
    safe.metadata = "stable"
    assert _classify_value_capture(safe, set()) == (True, "")

    unsafe = _TupleSubclass((1, []))
    accepted, reason = _classify_value_capture(unsafe, set())
    assert not accepted
    assert reason == "Unsupported ambient capture."

    stateful = _TupleSubclass((1,))
    stateful.metadata = []
    accepted, reason = _classify_value_capture(stateful, set())
    assert not accepted
    assert reason == "Unsupported ambient capture."

    accepted, reason = _classify_value_capture(safe, {id(safe)})
    assert not accepted
    assert reason == "Cyclic ambient values are not supported."

    class LocalTuple(tuple[Any, ...]):
        pass

    accepted, reason = _classify_value_capture(LocalTuple((1,)), set())
    assert not accepted
    assert "Local type definitions" in reason

    original = _instance_slots
    monkeypatch.setattr(
        "pyinc.explain._instance_slots",
        lambda value_type: {"metadata"} if value_type is _TupleSubclass else original(value_type),
    )
    accepted, reason = _classify_value_capture(_TupleSubclass((1,)), set())
    assert not accepted
    assert reason == "Tuple subclass slot state cannot be fingerprinted safely."


def test_frozenset_subclass_capture_checks_contents_cycles_type_and_state() -> None:
    safe = _FrozenSetSubclass({1, 2})
    safe.metadata = "stable"
    assert _classify_value_capture(safe, set()) == (True, "")

    stateful = _FrozenSetSubclass({1})
    stateful.metadata = []
    accepted, reason = _classify_value_capture(stateful, set())
    assert not accepted
    assert reason == "Unsupported ambient capture."

    accepted, reason = _classify_value_capture(safe, {id(safe)})
    assert not accepted
    assert reason == "Cyclic ambient values are not supported."

    slotted = _SlottedFrozenSet({1})
    slotted.metadata = "state"
    accepted, reason = _classify_value_capture(slotted, set())
    assert not accepted
    assert reason == "Frozenset subclass slot state cannot be fingerprinted safely."

    class LocalFrozenSet(frozenset[Any]):
        pass

    accepted, reason = _classify_value_capture(LocalFrozenSet({1}), set())
    assert not accepted
    assert "Local type definitions" in reason

    class UnsupportedMember:
        pass

    accepted, reason = _classify_value_capture(frozenset({UnsupportedMember()}), set())
    assert not accepted
    assert reason == "Unsupported ambient capture."


def test_frozen_dataclass_capture_checks_fields_cycles_slots_and_extra_state() -> None:
    assert _classify_value_capture(_FrozenConfig((1, 2)), set()) == (True, "")

    accepted, reason = _classify_value_capture(_MutableConfig(1), set())
    assert not accepted
    assert reason == "Mutable dataclass values cannot be captured ambiently."

    unsafe_field = _FrozenConfig([])
    accepted, reason = _classify_value_capture(unsafe_field, set())
    assert not accepted
    assert reason == "Unsupported ambient capture."

    accepted, reason = _classify_value_capture(unsafe_field, {id(unsafe_field)})
    assert not accepted
    assert reason == "Cyclic ambient values are not supported."

    accepted, reason = _classify_value_capture(_FrozenConfigWithExtraSlot(1), set())
    assert not accepted
    assert reason == "Frozen dataclass non-field slot state cannot be fingerprinted safely."

    safe_extra = _FrozenConfig(1)
    object.__setattr__(safe_extra, "metadata", (2, 3))
    assert _classify_value_capture(safe_extra, set()) == (True, "")

    unsafe_extra = _FrozenConfig(1)
    object.__setattr__(unsafe_extra, "metadata", [])
    accepted, reason = _classify_value_capture(unsafe_extra, set())
    assert not accepted
    assert reason == "Unsupported ambient capture."

    class LocalFrozen:
        pass

    LocalFrozenDataclass = dataclass(frozen=True)(LocalFrozen)
    accepted, reason = _classify_value_capture(LocalFrozenDataclass(), set())
    assert not accepted
    assert "Local type definitions" in reason


def test_value_classifier_rejects_unsupported_objects() -> None:
    accepted, reason = _classify_value_capture(object(), set())
    assert not accepted
    assert reason == "Unsupported ambient capture."


def test_capture_classifier_covers_builtin_type_annotation_evaluator_and_resource() -> None:
    builtin = _classify_capture("builtin", len, "attribute")
    assert builtin.accepted and builtin.kind == "builtin"

    captured_type = _classify_capture("type", _Outer.Nested, "attribute")
    assert captured_type.accepted and captured_type.kind == "type"

    annotation = _classify_capture("annotations", _annotation_evaluator, "annotation_evaluator")
    assert annotation.accepted and annotation.kind == "annotation"

    resource = _classify_capture("resource", FileResource(), "attribute")
    assert resource.accepted and resource.kind == "resource"
    assert _is_resource_handle(FileResource())
    assert not _is_resource_handle(object())


def test_capture_classifier_uses_exception_type_when_message_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyError(Exception):
        def __str__(self) -> str:
            return ""

    subject = object()
    original = RuntimeDatabase._freeze_static_capture

    def fail(self: RuntimeDatabase, value: object, active: set[int]) -> Any:
        # Only the capture under test raises. Building a database fingerprints
        # the kernel's own adapters, which freezes static captures of its own
        # before the classifier is reached, so a blanket failure here would
        # break the construction instead of being classified.
        if value is subject:
            raise EmptyError
        return original(self, value, active)

    monkeypatch.setattr(RuntimeDatabase, "_freeze_static_capture", fail)
    info = _classify_capture("value", subject, "attribute")
    assert not info.accepted
    assert info.rejection_reason.endswith("EmptyError")


def test_explain_reports_defaults_kwdefaults_attributes_and_type_parameters() -> None:
    def configured(
        db: Database,
        value: tuple[int, ...] = (1, 2),
        *,
        window: range = range(3),
    ) -> int:
        return value[0] + len(window)

    configured_metadata = cast(Any, configured)
    configured_metadata.helper = len
    configured_metadata.captured_type = _Outer.Nested
    configured_metadata.__type_params__ = (int,)

    infos = explain_query_captures(configured)
    by_name = {info.name: info for info in infos}
    assert by_name["default[0]"].origin == "default"
    assert by_name["kwdefault[window]"].origin == "kwdefault"
    assert by_name["attribute[helper]"].kind == "builtin"
    assert by_name["attribute[captured_type]"].kind == "type"
    assert by_name["type_parameter[0]"].kind == "annotation"


@pytest.mark.skipif(sys.version_info < (3, 14), reason="lazy annotations require Python 3.14")
def test_explain_falls_back_to_lazy_annotation_evaluator() -> None:
    namespace: dict[str, object] = {}
    code = compile(
        "def annotated(value: MissingType): return value",
        "<lazy-annotation-test>",
        "exec",
        dont_inherit=True,
    )
    exec(code, namespace)
    annotated = cast(Any, namespace["annotated"])

    infos = explain_query_captures(annotated)
    assert len(infos) == 1
    assert infos[0].name == "annotations"
    assert infos[0].origin == "annotation_evaluator"
    assert infos[0].kind == "annotation"
    assert infos[0].accepted


@pytest.mark.skipif(sys.version_info < (3, 14), reason="lazy annotations require Python 3.14")
def test_explain_ignores_nonfunction_lazy_annotation_evaluator() -> None:
    class BrokenAnnotations:
        def __call__(self, format: int) -> object:
            raise RuntimeError(format)

    def target() -> None:
        return None

    cast(Any, target).__annotate__ = BrokenAnnotations()
    assert explain_query_captures(target) == ()
