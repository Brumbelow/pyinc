from __future__ import annotations

import inspect as _inspect
from dataclasses import dataclass
from types import BuiltinFunctionType, FunctionType, MethodType, ModuleType
from typing import Any, cast


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


def _unbound_capture_owner() -> None:
    """Stand-in for the query of a capture classified on its own.

    The kernel's payload builders take the owning query function to resolve
    attribute-access paths for module state held by a capture and to name the
    query when they reject. A capture classified outside a query has no such
    function; this one accesses nothing, so a non-stdlib module held by such a
    capture is reported as used dynamically.
    """


def _capture_kind(value: Any) -> str:
    """The kind a capture is reported as, in the order the kernel dispatches it.

    `Database._captured_dependency_digest` tests the same shapes in the same
    order, so the label names the arm whose verdict the report gives.
    """
    from .core import Input, Query
    from .runtime import _is_guarded_name

    if _is_guarded_name(value):
        # A wrapper the ambient-read guard installed in place of a
        # standard-library callable (`from os import getcwd` once a Database
        # exists): the kernel pins it by the name it guards before any other
        # arm sees it.
        return "guarded"
    if isinstance(value, Query):
        return "query"
    if isinstance(value, Input):
        return "input"
    if _is_resource_handle(value):
        return "resource"
    if isinstance(value, ModuleType):
        return "module"
    if isinstance(value, FunctionType):
        return "function"
    if isinstance(value, MethodType):
        # Above the __wrapped__ probe, as in the kernel, so a wraps-decorated
        # method is the method it is rather than a callable object.
        return "method"
    if isinstance(value, BuiltinFunctionType):
        return "builtin"
    if isinstance(value, type):
        return "type"
    if callable(value) and isinstance(getattr(value, "__wrapped__", None), FunctionType):
        # Last of the callable shapes: what reaches here is a callable object
        # whose behavior lives in __call__ and instance state.
        return "callable"
    return "value"


def _classify_capture(
    name: str, value: Any, origin: str, *, owner: FunctionType | None = None
) -> CaptureInfo:
    """Report one capture with the verdict of the arm the kernel folds it with.

    The kernel folds a query function's defaults, closure cells, globals and
    custom attributes with `_captured_dependency_digest`, while the function
    itself is on the stack of functions being folded; its annotations as
    annotations, unless the body reads them back, when they are folded as the
    other captures are. Each verdict here is that call's, made the same way,
    so the report accepts what the kernel accepts -- a function the kernel
    pins by its source, a container or a frozen dataclass holding a callable
    -- and refuses what it refuses. Two arms are called one level down, at
    the payload builder the kernel's digest wraps, because the digest only
    reframes their refusals around the capture's name: the report keeps the
    builder's own reason, such as a mutable dataclass's.
    """
    from .runtime import Database

    type_name = type(value).__qualname__
    database = Database()
    kind = "value"
    owner_function = owner if owner is not None else cast(FunctionType, _unbound_capture_owner)
    # The query function is being folded while its captures are.
    seen_functions = {id(owner)} if owner is not None else set()
    try:
        if origin == "type_parameter" or (
            origin == "annotation"
            and not (owner is not None and Database._reads_its_own_annotations(owner))
        ):
            kind = "annotation"
            database._freeze_annotation_capture(value, set())
        elif origin == "annotation_evaluator" and isinstance(value, FunctionType):
            kind = "annotation"
            database._annotation_evaluator_payload(value, set())
        else:
            kind = "annotation" if origin == "annotation" else _capture_kind(value)
            if kind == "value":
                database._freeze_captured_immutable(
                    name, value, seen_functions, owner=owner_function, active_ids=set()
                )
            elif kind == "callable":
                database._wrapped_callable_payload(
                    name, value, value.__wrapped__, seen_functions, owner=owner_function
                )
            else:
                database._captured_dependency_digest(
                    name, value, seen_functions, owner=owner_function
                )
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
            results.append(_handle_state_entry("handle[*]", type(query).__qualname__, exc))
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
