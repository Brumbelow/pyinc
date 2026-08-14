from __future__ import annotations

import inspect as _inspect
import os
import sys
from dataclasses import dataclass, fields, is_dataclass
from types import BuiltinFunctionType, FunctionType, MethodType, ModuleType
from typing import Any, cast

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


def _unbound_capture_owner() -> None:
    """Stand-in for the query of a capture classified on its own.

    The kernel's payload builders take the owning query function to resolve
    attribute-access paths for module state held by a capture and to name the
    query when they reject. A capture classified outside a query has no such
    function; this one accesses nothing, so a non-stdlib module held by such a
    capture is reported as used dynamically.
    """


def _classify_capture(
    name: str, value: Any, origin: str, *, owner: FunctionType | None = None
) -> CaptureInfo:
    from .core import Input, Query
    from .runtime import Database

    type_name = type(value).__qualname__
    database = Database()
    kind = "value"
    owner_function = owner if owner is not None else cast(FunctionType, _unbound_capture_owner)
    try:
        if origin in {"annotation", "type_parameter"}:
            kind = "annotation"
            database._freeze_annotation_capture(value, set())
        elif origin == "annotation_evaluator" and isinstance(value, FunctionType):
            kind = "annotation"
            database._annotation_evaluator_payload(value, set())
        elif isinstance(value, Query):
            kind = "query"
            database._query_fingerprint(value)
        elif isinstance(value, Input):
            kind = "input"
            database._input_policy_digest(value)
        elif _is_resource_handle(value):
            kind = "resource"
            database._resource_identity_payload(value)
        elif isinstance(value, ModuleType):
            # A module's identity payload is half of what the kernel folds for
            # a captured module: beside it go the attribute paths the owning
            # query reads off the capture statically, and a non-stdlib module
            # reached any other way is refused there. Routing this through the
            # kernel's own capture arm keeps that verdict -- and the carve-outs
            # it makes -- this report's verdict too.
            kind = "module"
            database._captured_dependency_digest(name, value, set(), owner=owner_function)
        elif isinstance(value, FunctionType):
            kind = "function"
            database._function_definition_payload(value, set())
        elif isinstance(value, MethodType):
            # The kernel dispatches a bound method here, above its __wrapped__
            # probe, so a wraps-decorated method must be fingerprinted as the
            # method it is rather than falling to the callable branch below.
            kind = "method"
            database._bound_python_method_payload(
                value,
                capture_name=name,
                owner=owner_function,
                seen_functions=set(),
            )
        elif isinstance(value, BuiltinFunctionType):
            kind = "builtin"
            database._builtin_function_payload(value)
        elif isinstance(value, type):
            kind = "type"
            database._type_definition_payload(value)
        elif callable(value) and isinstance(getattr(value, "__wrapped__", None), FunctionType):
            # Last of the callable shapes, as in the kernel: functions, bound
            # methods and classes are dispatched above, so what reaches here is
            # a callable object whose behavior lives in __call__ and instance
            # state. The verdict is the kernel's own payload builder's rather
            # than a restatement of its rules.
            kind = "callable"
            database._wrapped_callable_payload(
                name,
                value,
                value.__wrapped__,
                set(),
                owner=owner_function,
            )
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


def _handle_state_entry(name: str, type_name: str, error: Exception | None) -> CaptureInfo:
    if error is None:
        return CaptureInfo(
            name=name,
            origin="handle",
            type_name=type_name,
            accepted=True,
            kind="handle",
        )
    return CaptureInfo(
        name=name,
        origin="handle",
        type_name=type_name,
        accepted=False,
        kind="rejected",
        rejection_reason=str(error) or type(error).__qualname__,
    )


def _classify_handle_state(query: Any) -> list[CaptureInfo]:
    """Report the state a query handle carries beyond its contract fields.

    The kernel folds a handle's own dictionary into query identity, so an
    attribute written on the handle can refuse a query whose captures are all
    clean, and a report that only ever looks at the function would call that
    query accepted. Every verdict here is the kernel's own: each entry outside
    the contract fields is folded by the builder the fold uses for one entry,
    and a refusal that builder cannot reach -- a handle given a non-string
    name, an annotation carrier or type parameters the fold rejects -- is
    reported against the handle itself by folding the whole of it.
    """

    from .runtime import Database

    database = Database()
    state = vars(query)
    results: list[CaptureInfo] = []
    for name in sorted(name for name in state if isinstance(name, str)):
        if name in Database._QUERY_HANDLE_CONTRACT_NAMES:
            continue
        value = state[name]
        error: Exception | None = None
        try:
            database._query_handle_entry_payload(query, name, value, set())
        except Exception as exc:
            error = exc
        results.append(_handle_state_entry(f"handle[{name}]", type(value).__qualname__, error))
    if all(item.accepted for item in results):
        try:
            database._query_handle_state_payload(query, set())
        except Exception as exc:
            results.append(_handle_state_entry("handle", type(query).__qualname__, exc))
    return results


def explain_query_captures(fn_or_query: Any) -> tuple[CaptureInfo, ...]:
    from .core import Query
    from .runtime import _reflective_namespace_offenses

    handle = fn_or_query if isinstance(fn_or_query, Query) else None
    target = handle.fn if handle is not None else fn_or_query
    if not isinstance(target, FunctionType):
        raise TypeError("explain_query_captures() expects a function or @query-decorated callable.")

    results: list[CaptureInfo] = []
    # Ahead of the capture set, and from the kernel's own detector: these loads
    # reach namespace state no entry below can describe, and the kernel refuses
    # them off the body before it folds a single capture. Reporting a clean
    # capture set for such a body would describe a query the kernel will not
    # accept.
    for offense in _reflective_namespace_offenses(target.__code__):
        results.append(
            CaptureInfo(
                name=f"reflective[{offense}]",
                origin="code",
                type_name="code",
                accepted=False,
                kind="rejected",
                rejection_reason=(
                    "Reflective namespace reads bypass capture fingerprinting; "
                    "access module attributes directly, or move mutable state "
                    "behind Input/Resource nodes."
                ),
            )
        )
    for index, value in enumerate(target.__defaults__ or ()):
        results.append(_classify_capture(f"default[{index}]", value, "default", owner=target))
    for default_name, value in sorted((target.__kwdefaults__ or {}).items()):
        results.append(_classify_capture(f"kwdefault[{default_name}]", value, "kwdefault", owner=target))

    closure_vars = _inspect.getclosurevars(target)
    for capture_name, value in sorted(closure_vars.nonlocals.items()):
        results.append(_classify_capture(capture_name, value, "closure", owner=target))
    for capture_name, value in sorted(closure_vars.globals.items()):
        results.append(_classify_capture(capture_name, value, "global", owner=target))

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
        results.append(_classify_capture(metadata_name, value, origin, owner=target))
    if handle is not None:
        results.extend(_classify_handle_state(handle))
    return tuple(results)
