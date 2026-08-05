from __future__ import annotations

import inspect as _inspect
import os
import sys
from dataclasses import dataclass, fields, is_dataclass
from types import BuiltinFunctionType, FunctionType, ModuleType
from typing import Any

from ._path_identity import is_stdlib_path
from .value import (
    FrozenAdapterValue,
    FrozenDict,
    FrozenList,
    FrozenRecord,
    FrozenSet,
)


@dataclass(frozen=True)
class InspectionNode:
    label: str
    kind: str
    changed_at: int
    verified_at: int
    last_decision: str
    last_recompute: str
    reason: str
    untracked_reasons: tuple[str, ...] = ()
    dependencies: tuple[InspectionNode, ...] = ()

    @property
    def is_untracked(self) -> bool:
        return bool(self.untracked_reasons)


@dataclass(frozen=True)
class CaptureInfo:
    """Classification of one statically discoverable function capture."""

    name: str
    origin: str
    type_name: str
    accepted: bool
    kind: str = ""
    rejection_reason: str = ""


def format_explanation(root: InspectionNode) -> str:
    lines: list[str] = []

    def walk(current: InspectionNode, depth: int) -> None:
        indent = "  " * depth
        lines.append(
            f"{indent}- {current.label}: {current.last_decision}"
            f" [last_recompute={current.last_recompute}]"
            f" (changed_at={current.changed_at}, verified_at={current.verified_at})"
        )
        if current.reason:
            lines.append(f"{indent}  reason: {current.reason}")
        for item in current.untracked_reasons:
            lines.append(f"{indent}  untracked: {item}")
        for dependency in current.dependencies:
            walk(dependency, depth + 1)

    walk(root, 0)
    return "\n".join(lines)


def _is_resource_handle(value: Any) -> bool:
    return all(callable(getattr(value, name, None)) for name in ("label", "probe", "load"))


def _type_capture_rejection(value_type: type[Any]) -> str:
    if value_type.__module__ == "builtins":
        return ""
    if "<locals>" in value_type.__qualname__:
        return "Local type definitions cannot be fingerprinted safely."
    module = sys.modules.get(value_type.__module__)
    current: Any = (
        vars(module).get(value_type.__qualname__.split(".", 1)[0]) if module is not None else None
    )
    for part in value_type.__qualname__.split(".")[1:]:
        current = vars(current).get(part) if isinstance(current, type) else None
    if current is not value_type:
        return "Type is not the live binding in its defining module."
    return ""


def _instance_slots(value_type: type[Any]) -> set[str]:
    slots: set[str] = set()
    for cls in value_type.__mro__:
        declared = cls.__dict__.get("__slots__", ())
        if isinstance(declared, str):
            declared = (declared,)
        slots.update(slot for slot in declared if slot not in {"__dict__", "__weakref__"})
    return slots


def _instance_dict(value: Any) -> dict[str, Any]:
    try:
        state = object.__getattribute__(value, "__dict__")
    except (AttributeError, TypeError):
        return {}
    return state if isinstance(state, dict) else {}


def _classify_instance_dict(value: Any, seen: set[int]) -> tuple[bool, str]:
    for item in _instance_dict(value).values():
        accepted, reason = _classify_value_capture(item, seen)
        if not accepted:
            return False, reason
    return True, ""


def _classify_value_capture(value: Any, seen: set[int]) -> tuple[bool, str]:
    scalar_types = (str, bytes, int, float, bool, type(None), complex)
    if type(value) in scalar_types:
        return True, ""
    if isinstance(value, type):
        reason = _type_capture_rejection(value)
        return not reason, reason
    if isinstance(value, scalar_types):
        reason = _type_capture_rejection(type(value))
        if reason:
            return False, reason
        if _instance_slots(type(value)):
            return False, "Scalar subclass slot state cannot be fingerprinted safely."
        return _classify_instance_dict(value, seen)
    if type(value) in {
        FrozenList,
        FrozenDict,
        FrozenSet,
        FrozenRecord,
        FrozenAdapterValue,
    }:
        return True, ""
    if isinstance(value, os.PathLike):
        reason = _type_capture_rejection(type(value))
        if reason:
            return False, reason
        if is_stdlib_path(value):
            return True, ""
        if _instance_slots(type(value)):
            return False, "Path-like slot state cannot be fingerprinted safely."
        return _classify_instance_dict(value, seen)
    if isinstance(value, range):
        return True, ""
    if isinstance(value, tuple):
        if type(value) is not tuple:
            reason = _type_capture_rejection(type(value))
            if reason:
                return False, reason
            if _instance_slots(type(value)):
                return False, "Tuple subclass slot state cannot be fingerprinted safely."
        object_id = id(value)
        if object_id in seen:
            return False, "Cyclic ambient values are not supported."
        seen.add(object_id)
        try:
            for item in value:
                accepted, reason = _classify_value_capture(item, seen)
                if not accepted:
                    return False, reason
            if type(value) is not tuple:
                return _classify_instance_dict(value, seen)
            return True, ""
        finally:
            seen.discard(object_id)
    if isinstance(value, frozenset):
        if type(value) is not frozenset:
            reason = _type_capture_rejection(type(value))
            if reason:
                return False, reason
            if _instance_slots(type(value)):
                return False, "Frozenset subclass slot state cannot be fingerprinted safely."
        object_id = id(value)
        if object_id in seen:
            return False, "Cyclic ambient values are not supported."
        seen.add(object_id)
        try:
            for item in value:
                accepted, reason = _classify_value_capture(item, seen)
                if not accepted:
                    return False, reason
            if type(value) is not frozenset:
                return _classify_instance_dict(value, seen)
            return True, ""
        finally:
            seen.discard(object_id)
    if is_dataclass(value) and not isinstance(value, type):
        params = getattr(type(value), "__dataclass_params__", None)
        if params is None or not params.frozen:
            return False, "Mutable dataclass values cannot be captured ambiently."
        reason = _type_capture_rejection(type(value))
        if reason:
            return False, reason
        field_names = {item.name for item in fields(value)}
        if _instance_slots(type(value)) - field_names:
            return False, "Frozen dataclass non-field slot state cannot be fingerprinted safely."
        object_id = id(value)
        if object_id in seen:
            return False, "Cyclic ambient values are not supported."
        seen.add(object_id)
        try:
            for f in fields(value):
                accepted, reason = _classify_value_capture(
                    object.__getattribute__(value, f.name), seen
                )
                if not accepted:
                    return False, reason
            for name, item in _instance_dict(value).items():
                if name in field_names:
                    continue
                accepted, reason = _classify_value_capture(item, seen)
                if not accepted:
                    return False, reason
            return True, ""
        finally:
            seen.discard(object_id)
    return False, "Unsupported ambient capture."


def _classify_capture(name: str, value: Any, origin: str) -> CaptureInfo:
    from .core import Input, Query
    from .runtime import Database

    type_name = type(value).__qualname__
    database = Database()
    kind = "value"
    try:
        if origin in {"annotation", "type_parameter"}:
            kind = "annotation"
            database._freeze_annotation_capture(value, set())
        elif origin == "annotation_evaluator" and isinstance(value, FunctionType):
            kind = "annotation"
            database._annotation_evaluator_payload(value, set())
        elif type(value) is Query:
            kind = "query"
            database._query_fingerprint(value)
        elif type(value) is Input:
            kind = "input"
            database._input_policy_digest(value)
        elif _is_resource_handle(value):
            kind = "resource"
            database._resource_identity_payload(value)
        elif isinstance(value, ModuleType):
            kind = "module"
            database._module_identity_payload(value)
        elif isinstance(value, FunctionType):
            kind = "function"
            database._function_definition_payload(value, set())
        elif isinstance(value, BuiltinFunctionType):
            kind = "builtin"
            database._builtin_function_payload(value)
        elif isinstance(value, type):
            kind = "type"
            database._type_definition_payload(value)
        else:
            database._freeze_static_capture(value, set())
    except Exception as exc:
        reason = str(exc) or type(exc).__qualname__
        if reason.startswith("Captured local type"):
            reason = reason.replace("Captured local type", "Local type", 1)
        return CaptureInfo(
            name=name,
            origin=origin,
            type_name=type_name,
            accepted=False,
            kind="rejected",
            rejection_reason=reason,
        )
    return CaptureInfo(
        name=name,
        origin=origin,
        type_name=type_name,
        accepted=True,
        kind=kind,
    )


def explain_query_captures(fn_or_query: Any) -> tuple[CaptureInfo, ...]:
    """Classify captures discoverable through static function inspection.

    Dynamic namespace and reflection paths such as ``globals()[name]`` are not
    resolved by this preflight and therefore do not appear in its result.
    """

    from .core import Query

    target = fn_or_query.fn if type(fn_or_query) is Query else fn_or_query
    if not isinstance(target, FunctionType):
        raise TypeError("explain_query_captures() expects a function or @query-decorated callable.")

    results: list[CaptureInfo] = []
    for index, value in enumerate(target.__defaults__ or ()):
        results.append(_classify_capture(f"default[{index}]", value, "default"))
    for default_name, value in sorted((target.__kwdefaults__ or {}).items()):
        results.append(_classify_capture(f"kwdefault[{default_name}]", value, "kwdefault"))

    closure_vars = _inspect.getclosurevars(target)
    for capture_name, value in sorted(closure_vars.nonlocals.items()):
        results.append(_classify_capture(capture_name, value, "closure"))
    for capture_name, value in sorted(closure_vars.globals.items()):
        results.append(_classify_capture(capture_name, value, "global"))

    try:
        annotations = target.__annotations__
    except Exception:
        annotation_function = getattr(target, "__annotate__", None)
        metadata: list[tuple[str, Any, str]] = (
            [("annotations", annotation_function, "annotation_evaluator")]
            if isinstance(annotation_function, FunctionType)
            else []
        )
    else:
        metadata = [
            (f"annotation[{name}]", value, "annotation")
            for name, value in sorted(annotations.items())
        ]
    metadata.extend(
        (f"attribute[{name}]", value, "attribute") for name, value in sorted(vars(target).items())
    )
    metadata.extend(
        (f"type_parameter[{index}]", value, "type_parameter")
        for index, value in enumerate(getattr(target, "__type_params__", ()))
    )
    for metadata_name, value, origin in metadata:
        results.append(_classify_capture(metadata_name, value, origin))
    return tuple(results)
