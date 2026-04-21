from __future__ import annotations

import inspect as _inspect
import os
from dataclasses import dataclass, fields, is_dataclass
from types import BuiltinFunctionType, FunctionType, ModuleType
from typing import Any

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


def _classify_value_capture(value: Any, seen: set[int]) -> tuple[bool, str]:
    if isinstance(value, (str, bytes, int, float, bool, type(None), complex)):
        return True, ""
    if isinstance(value, (FrozenList, FrozenDict, FrozenSet, FrozenRecord, FrozenAdapterValue)):
        return True, ""
    if isinstance(value, os.PathLike):
        return True, ""
    if isinstance(value, range):
        return True, ""
    if isinstance(value, tuple):
        object_id = id(value)
        if object_id in seen:
            return False, "Cyclic ambient values are not supported."
        seen.add(object_id)
        try:
            for item in value:
                accepted, reason = _classify_value_capture(item, seen)
                if not accepted:
                    return False, reason
            return True, ""
        finally:
            seen.discard(object_id)
    if isinstance(value, frozenset):
        object_id = id(value)
        if object_id in seen:
            return False, "Cyclic ambient values are not supported."
        seen.add(object_id)
        try:
            for item in value:
                accepted, reason = _classify_value_capture(item, seen)
                if not accepted:
                    return False, reason
            return True, ""
        finally:
            seen.discard(object_id)
    if is_dataclass(value) and not isinstance(value, type):
        params = getattr(type(value), "__dataclass_params__", None)
        if params is None or not params.frozen:
            return False, "Mutable dataclass values cannot be captured ambiently."
        object_id = id(value)
        if object_id in seen:
            return False, "Cyclic ambient values are not supported."
        seen.add(object_id)
        try:
            for f in fields(value):
                accepted, reason = _classify_value_capture(getattr(value, f.name), seen)
                if not accepted:
                    return False, reason
            return True, ""
        finally:
            seen.discard(object_id)
    return False, "Unsupported ambient capture."


def _classify_capture(name: str, value: Any, origin: str) -> CaptureInfo:
    from .core import Input, Query

    type_name = type(value).__qualname__

    if isinstance(value, Query):
        return CaptureInfo(name=name, origin=origin, type_name=type_name, accepted=True, kind="query")
    if isinstance(value, Input):
        return CaptureInfo(name=name, origin=origin, type_name=type_name, accepted=True, kind="input")
    if _is_resource_handle(value):
        return CaptureInfo(name=name, origin=origin, type_name=type_name, accepted=True, kind="resource")
    if isinstance(value, ModuleType):
        return CaptureInfo(name=name, origin=origin, type_name=type_name, accepted=True, kind="module")
    if isinstance(value, FunctionType):
        return CaptureInfo(name=name, origin=origin, type_name=type_name, accepted=True, kind="function")
    if isinstance(value, BuiltinFunctionType):
        return CaptureInfo(name=name, origin=origin, type_name=type_name, accepted=True, kind="builtin")
    if isinstance(value, type):
        return CaptureInfo(name=name, origin=origin, type_name=type_name, accepted=True, kind="type")

    accepted, reason = _classify_value_capture(value, set())
    if accepted:
        return CaptureInfo(name=name, origin=origin, type_name=type_name, accepted=True, kind="value")
    return CaptureInfo(
        name=name,
        origin=origin,
        type_name=type_name,
        accepted=False,
        kind="rejected",
        rejection_reason=reason,
    )


def explain_query_captures(fn_or_query: Any) -> tuple[CaptureInfo, ...]:
    from .core import Query

    target = fn_or_query.fn if isinstance(fn_or_query, Query) else fn_or_query
    if not isinstance(target, FunctionType):
        raise TypeError(
            "explain_query_captures() expects a function or @query-decorated callable."
        )

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
    return tuple(results)
