from __future__ import annotations

import builtins
import inspect
import io
import marshal
import os
import sys
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from types import BuiltinFunctionType, FunctionType, ModuleType
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast, overload
from unittest import mock

from .errors import CycleError, UnsupportedValueError, UntrackedReadError
from .explain import InspectionNode, format_explanation
from .value import (
    FrozenAdapterValue,
    FrozenDict,
    FrozenList,
    FrozenRecord,
    FrozenSet,
    Snapshot,
    ValueAdapter,
    assert_not_mutated,
    fingerprint,
    fingerprint_snapshot,
    freeze,
    semantic_equal,
    thaw,
)

if TYPE_CHECKING:
    from .core import Input, Query


Mode = str
DefaultT = TypeVar("DefaultT")
P = ParamSpec("P")
T = TypeVar("T")


@dataclass(frozen=True)
class NodeKey:
    kind: str
    identity: str
    args_digest: str
    label: str


@dataclass
class NodeRecord:
    key: NodeKey
    label: str
    snapshot: Any
    digest: str
    changed_at: int
    verified_at: int
    dependencies: set[NodeKey] = field(default_factory=set)
    last_decision: str = "pending"
    last_recompute: str = "never"
    reason: str = ""
    untracked_reasons: list[str] = field(default_factory=list)
    probe: Any = None
    checked_in_request: int = -1

    @property
    def is_untracked(self) -> bool:
        return bool(self.untracked_reasons)


@dataclass
class ExecutionFrame:
    key: NodeKey
    dependencies: set[NodeKey] = field(default_factory=set)
    boundary_fingerprints: list[str] = field(default_factory=list)
    boundary_values: list[Any] = field(default_factory=list)
    untracked_reasons: list[str] = field(default_factory=list)


class _GuardedEnviron(MutableMapping[str, str]):
    def __init__(self, wrapped: MutableMapping[str, str], check_read: Callable[[], None]) -> None:
        self._wrapped = wrapped
        self._check_read = check_read

    def __getitem__(self, key: str) -> str:
        self._check_read()
        return self._wrapped[key]

    def __setitem__(self, key: str, value: str) -> None:
        self._wrapped[key] = value

    def __delitem__(self, key: str) -> None:
        del self._wrapped[key]

    def __iter__(self) -> Iterator[str]:
        self._check_read()
        return iter(self._wrapped)

    def __len__(self) -> int:
        self._check_read()
        return len(self._wrapped)

    @overload
    def get(self, key: str, default: None = None) -> str | None: ...

    @overload
    def get(self, key: str, default: str = ...) -> str: ...

    @overload
    def get(self, key: str, default: DefaultT) -> str | DefaultT: ...

    def get(self, key: str, default: DefaultT | None = None) -> str | DefaultT | None:
        self._check_read()
        return self._wrapped.get(key, default)

    def keys(self) -> Any:
        self._check_read()
        return self._wrapped.keys()

    def items(self) -> Any:
        self._check_read()
        return self._wrapped.items()

    def values(self) -> Any:
        self._check_read()
        return self._wrapped.values()

    def copy(self) -> dict[str, str]:
        self._check_read()
        return dict(self._wrapped)

    def __contains__(self, key: object) -> bool:
        self._check_read()
        return key in self._wrapped


class Database:
    def __init__(
        self,
        mode: Mode = "strict",
        *,
        adapters: Mapping[type[Any], ValueAdapter] | None = None,
        max_query_nodes: int | None = None,
    ) -> None:
        if mode not in {"strict", "checked", "fast"}:
            raise ValueError("mode must be one of: strict, checked, fast")
        if max_query_nodes is not None and max_query_nodes <= 0:
            raise ValueError("max_query_nodes must be a positive integer or None.")
        self.mode = mode
        self.max_query_nodes = max_query_nodes
        self._adapters = dict(adapters or {})
        self._revision = 0
        self._records: dict[NodeKey, NodeRecord] = {}
        self._input_records: dict[Any, NodeKey] = {}
        self._query_records: set[NodeKey] = set()
        self._query_last_used: dict[NodeKey, int] = {}
        self._query_touch_counter = 0
        self._execution_stack: ContextVar[tuple[ExecutionFrame, ...]] = ContextVar(
            "pyinc_execution_stack",
            default=(),
        )
        self._allow_raw_reads: ContextVar[bool] = ContextVar("pyinc_allow_raw_reads", default=False)
        self._request_token: ContextVar[int | None] = ContextVar("pyinc_request_token", default=None)
        self._request_counter = 0

    @property
    def revision(self) -> int:
        return self._revision

    def set(self, input_key: Any, value: Any) -> None:
        from .core import Input

        if not isinstance(input_key, Input):
            raise TypeError("db.set() expects an Input instance.")
        snapshot = self._freeze_value(value)
        digest = fingerprint_snapshot(snapshot)
        node_key = self._input_key(input_key)
        record = self._records.get(node_key)
        if record is not None and self._compare_values(
            eq=input_key.eq,
            cutoff=input_key.cutoff,
            left=self._thaw_value(record.snapshot),
            right=self._thaw_value(snapshot),
        ):
            record.snapshot = snapshot
            record.digest = digest
            record.verified_at = self._revision
            record.last_decision = "reused"
            record.reason = "equal input update ignored"
            record.checked_in_request = self._current_request_id()
            return
        self._revision += 1
        changed_at = self._revision
        if record is None:
            self._records[node_key] = NodeRecord(
                key=node_key,
                label=node_key.label,
                snapshot=snapshot,
                digest=digest,
                changed_at=changed_at,
                verified_at=changed_at,
                last_decision="executed",
                last_recompute="executed",
                reason="input set",
                checked_in_request=self._current_request_id(),
            )
        else:
            record.snapshot = snapshot
            record.digest = digest
            record.changed_at = changed_at
            record.verified_at = changed_at
            record.last_decision = "executed"
            record.last_recompute = "executed"
            record.reason = "input changed"
            record.checked_in_request = self._current_request_id()

    def get(self, query: Query[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        from .core import Query

        if not isinstance(query, Query):
            raise TypeError("db.get() expects a @query-decorated callable.")
        with self._request_scope():
            key, call_snapshot = self._query_key(query, args, kwargs)
            self._record_dependency(key)
            self._ensure_query(query, key, call_snapshot)
            return cast(T, self._expose_boundary_snapshot(self._records[key].snapshot))

    def explain(self, query: Query[P, Any], *args: P.args, **kwargs: P.kwargs) -> str:
        from .core import Query

        if not isinstance(query, Query):
            raise TypeError("db.explain() expects a @query-decorated callable.")
        return format_explanation(self.inspect(query, *args, **kwargs))

    def inspect(self, query: Query[P, Any], *args: P.args, **kwargs: P.kwargs) -> InspectionNode:
        from .core import Query

        if not isinstance(query, Query):
            raise TypeError("db.inspect() expects a @query-decorated callable.")
        with self._request_scope():
            key, call_snapshot = self._query_key(query, args, kwargs)
            if key not in self._records:
                self._ensure_query(query, key, call_snapshot)
            return self._inspect_record(key)

    def report_untracked_read(self, reason: str) -> None:
        frame = self._current_frame()
        if frame is None:
            raise RuntimeError("db.report_untracked_read() must be called while a query is executing.")
        frame.untracked_reasons.append(reason)

    def _read_input(self, input_key: Input[T]) -> T:
        key = self._input_key(input_key)
        record = self._records.get(key)
        if record is None:
            raise KeyError(f"Input {input_key.name!r} has not been set.")
        self._record_dependency(key)
        return cast(T, self._expose_boundary_snapshot(record.snapshot))

    def _read_resource(self, resource: Any, parameter: Any) -> Any:
        key = self._resource_key(resource, parameter)
        self._refresh_resource(resource, parameter, key)
        self._record_dependency(key)
        return self._expose_boundary_snapshot(self._records[key].snapshot)

    def _ensure_query(self, query: Any, key: NodeKey, call_snapshot: Any) -> None:
        if any(frame.key == key for frame in self._execution_stack.get()):
            raise CycleError(f"Cycle detected while evaluating {key.label}.")
        existing = self._records.get(key)
        current_request = self._current_request_id()
        if existing is None:
            self._execute_query(query, key, call_snapshot, previous=None, reason="cold execute")
            self._mark_query_used(key)
            return
        if existing.checked_in_request == current_request:
            existing.last_decision = "reused"
            existing.reason = "already checked in current request"
            self._mark_query_used(key)
            return
        if existing.is_untracked:
            self._execute_query(query, key, call_snapshot, previous=existing, reason="untracked dependency")
            self._mark_query_used(key)
            return

        dirty_reason = None
        for dependency in sorted(existing.dependencies, key=lambda item: item.label):
            if self._maybe_changed_after(dependency, existing.verified_at):
                dirty_reason = f"dependency changed: {dependency.label}"
                break
        if dirty_reason is None:
            existing.verified_at = self._revision
            existing.last_decision = "reused"
            existing.reason = "dependencies unchanged"
            existing.checked_in_request = current_request
            self._mark_query_used(key)
            return
        self._execute_query(query, key, call_snapshot, previous=existing, reason=dirty_reason)
        self._mark_query_used(key)

    def _execute_query(self, query: Any, key: NodeKey, call_snapshot: Any, previous: NodeRecord | None, reason: str) -> None:
        frame = ExecutionFrame(key=key)
        stack = self._execution_stack.get()
        token = self._execution_stack.set(stack + (frame,))
        try:
            query_args, query_kwargs = self._materialize_call(
                call_snapshot,
                record_boundaries=self.mode == "checked",
                frame=frame,
            )
            with self._guard_untracked_reads():
                result = query.fn(self, *query_args, **query_kwargs)
            if self.mode == "checked":
                for before, value in zip(frame.boundary_fingerprints, frame.boundary_values, strict=True):
                    assert_not_mutated(before, self._fingerprint_value(value))
            snapshot = self._freeze_value(result)
            digest = fingerprint_snapshot(snapshot)
            impure = bool(frame.untracked_reasons)

            if previous is None:
                record = NodeRecord(
                    key=key,
                    label=key.label,
                    snapshot=snapshot,
                    digest=digest,
                    changed_at=self._revision,
                    verified_at=self._revision,
                    last_recompute="executed",
                )
                self._records[key] = record
                self._query_records.add(key)
                previous_changed_at = self._revision
                decision = "executed"
            else:
                record = previous
                previous_changed_at = previous.changed_at
                old_value = self._expose_snapshot(previous.snapshot)
                new_value = self._expose_snapshot(snapshot)
                equal = False if impure else self._compare_values(
                    eq=query.eq,
                    cutoff=query.cutoff,
                    left=old_value,
                    right=new_value,
                )
                record.snapshot = snapshot
                record.digest = digest
                if equal:
                    record.changed_at = previous_changed_at
                    decision = "backdated"
                else:
                    record.changed_at = self._revision
                    decision = "executed"
            self._query_records.add(key)
            record.verified_at = self._revision
            record.dependencies = frame.dependencies
            record.last_decision = decision
            record.last_recompute = decision
            record.reason = reason
            record.untracked_reasons = list(frame.untracked_reasons)
            record.checked_in_request = self._current_request_id()
        finally:
            self._execution_stack.reset(token)

    def _maybe_changed_after(self, key: NodeKey, revision: int) -> bool:
        record = self._records.get(key)
        if record is None:
            return True
        if key.kind == "query":
            query = record.key.identity
            query_obj = self._query_objects().get(query)
            call_snapshot = self._call_snapshots().get(key)
            if query_obj is None or call_snapshot is None:
                return True
            self._ensure_query(query_obj, key, call_snapshot)
        elif key.kind == "resource":
            resource_pair = self._resource_objects().get(key)
            if resource_pair is None:
                return True
            resource, parameter = resource_pair
            self._refresh_resource(resource, parameter, key)
        return self._records[key].is_untracked or self._records[key].changed_at > revision

    def _refresh_resource(self, resource: Any, parameter: Any, key: NodeKey) -> None:
        with self._allow_raw_reads_scope():
            probe = resource.probe(parameter)
        record = self._records.get(key)
        current_request = self._current_request_id()
        if record is not None and record.checked_in_request == current_request:
            return
        if record is not None and record.probe == probe:
            record.verified_at = self._revision
            record.last_decision = "reused"
            record.reason = "resource probe unchanged"
            record.checked_in_request = current_request
            return
        if record is None:
            changed_at = self._revision
        else:
            self._revision += 1
            changed_at = self._revision
        with self._allow_raw_reads_scope():
            loaded_value = resource.load(self, parameter)
        snapshot = self._freeze_value(loaded_value)
        digest = fingerprint_snapshot(snapshot)
        if record is None:
            self._records[key] = NodeRecord(
                key=key,
                label=key.label,
                snapshot=snapshot,
                digest=digest,
                changed_at=changed_at,
                verified_at=self._revision,
                last_decision="executed",
                last_recompute="executed",
                reason="resource loaded",
                probe=probe,
                checked_in_request=current_request,
            )
            return
        record.snapshot = snapshot
        record.digest = digest
        record.changed_at = changed_at
        record.verified_at = self._revision
        record.last_decision = "executed"
        record.last_recompute = "executed"
        record.reason = "resource probe changed"
        record.probe = probe
        record.checked_in_request = current_request

    def _query_key(self, query: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[NodeKey, Any]:
        call_snapshot = (self._freeze_value(args), self._freeze_value(kwargs))
        args_digest = fingerprint_snapshot(call_snapshot)
        code_fingerprint = self._code_fingerprint(query.fn)
        key = NodeKey(
            kind="query",
            identity=f"{query.query_id}:{code_fingerprint}",
            args_digest=args_digest,
            label=f"{query.query_id}{args}",
        )
        self._query_objects()[key.identity] = query
        self._call_snapshots()[key] = call_snapshot
        return key, call_snapshot

    def _input_key(self, input_key: Any) -> NodeKey:
        key = self._input_records.get(input_key)
        if key is None:
            key = NodeKey(
                kind="input",
                identity=f"{id(input_key)}:{input_key.name}",
                args_digest="",
                label=f"input[{input_key.name}]",
            )
            self._input_records[input_key] = key
        return key

    def _resource_key(self, resource: Any, parameter: Any) -> NodeKey:
        frozen_parameter = self._freeze_value(parameter)
        parameter_digest = fingerprint_snapshot(frozen_parameter)
        resource_identity = fingerprint_snapshot(self._resource_identity_payload(resource))
        key = NodeKey(
            kind="resource",
            identity=f"{type(resource).__module__}:{type(resource).__qualname__}:{resource_identity}",
            args_digest=parameter_digest,
            label=resource.label(parameter),
        )
        self._resource_objects()[key] = (resource, parameter)
        return key

    def _materialize_call(self, call_snapshot: Any, *, record_boundaries: bool, frame: ExecutionFrame) -> tuple[tuple[Any, ...], dict[str, Any]]:
        frozen_args, frozen_kwargs = call_snapshot
        args = tuple(self._expose_snapshot(item, boundary=True, record_boundaries=record_boundaries, frame=frame) for item in frozen_args)
        kwargs = {
            key: self._expose_snapshot(value, boundary=True, record_boundaries=record_boundaries, frame=frame)
            for key, value in frozen_kwargs.entries
        }
        return args, kwargs

    def _expose_snapshot(self, snapshot: Any, *, boundary: bool = False, record_boundaries: bool = False, frame: ExecutionFrame | None = None) -> Any:
        exposed = snapshot if self.mode == "strict" else self._thaw_value(snapshot)
        if boundary and record_boundaries and frame is not None:
            frame.boundary_fingerprints.append(self._fingerprint_value(exposed))
            frame.boundary_values.append(exposed)
        return exposed

    def _expose_boundary_snapshot(self, snapshot: Any) -> Any:
        frame = self._current_frame()
        return self._expose_snapshot(
            snapshot,
            boundary=True,
            record_boundaries=self.mode == "checked" and frame is not None,
            frame=frame,
        )

    def _record_dependency(self, key: NodeKey) -> None:
        frame = self._current_frame()
        if frame is None:
            return
        frame.dependencies.add(key)

    def _inspect_record(self, key: NodeKey) -> InspectionNode:
        record = self._records[key]
        return InspectionNode(
            label=record.label,
            kind=record.key.kind,
            changed_at=record.changed_at,
            verified_at=record.verified_at,
            last_decision=record.last_decision,
            last_recompute=record.last_recompute,
            reason=record.reason,
            untracked_reasons=tuple(record.untracked_reasons),
            dependencies=tuple(
                self._inspect_record(dependency) for dependency in sorted(record.dependencies, key=lambda item: item.label)
            ),
        )

    def _query_objects(self) -> dict[str, Any]:
        if not hasattr(self, "_query_registry"):
            self._query_registry: dict[str, Any] = {}
        return self._query_registry

    def _resource_objects(self) -> dict[NodeKey, tuple[Any, Any]]:
        if not hasattr(self, "_resource_registry"):
            self._resource_registry: dict[NodeKey, tuple[Any, Any]] = {}
        return self._resource_registry

    def _call_snapshots(self) -> dict[NodeKey, Any]:
        if not hasattr(self, "_call_snapshot_registry"):
            self._call_snapshot_registry: dict[NodeKey, Any] = {}
        return self._call_snapshot_registry

    @contextmanager
    def _allow_raw_reads_scope(self) -> Iterator[None]:
        token = self._allow_raw_reads.set(True)
        try:
            yield
        finally:
            self._allow_raw_reads.reset(token)

    @contextmanager
    def _allow_raw_open(self) -> Iterator[None]:
        # Backward-compatible alias for custom resources using the previous helper.
        with self._allow_raw_reads_scope():
            yield

    @contextmanager
    def _guard_untracked_reads(self) -> Iterator[None]:
        original_builtins_open = builtins.open
        original_io_open = io.open
        original_os_getenv = os.getenv
        original_os_listdir = os.listdir
        original_os_scandir = os.scandir
        original_path_iterdir = Path.iterdir
        original_environ = os.environ

        def check_env_read() -> None:
            self._ensure_tracked_read("Raw os.environ access inside a query is untracked. Use EnvResource.read().")

        guarded_environ = _GuardedEnviron(original_environ, check_env_read)

        def guarded_open(*args: Any, **kwargs: Any) -> Any:
            self._ensure_tracked_read("Raw open() inside a query is untracked. Use FileResource.read().")
            return original_builtins_open(*args, **kwargs)

        def guarded_io_open(*args: Any, **kwargs: Any) -> Any:
            self._ensure_tracked_read("Raw open() inside a query is untracked. Use FileResource.read().")
            return original_io_open(*args, **kwargs)

        def guarded_getenv(key: str, default: str | None = None) -> str | None:
            self._ensure_tracked_read("Raw os.getenv() inside a query is untracked. Use EnvResource.read().")
            return original_os_getenv(key, default)

        def guarded_listdir(*args: Any, **kwargs: Any) -> Any:
            self._ensure_tracked_read("Raw os.listdir() inside a query is untracked. Use DirectoryResource.read().")
            return original_os_listdir(*args, **kwargs)

        def guarded_scandir(*args: Any, **kwargs: Any) -> Any:
            self._ensure_tracked_read("Raw os.scandir() inside a query is untracked. Use DirectoryResource.read().")
            return original_os_scandir(*args, **kwargs)

        def guarded_path_iterdir(path_obj: Path) -> Any:
            self._ensure_tracked_read("Raw Path.iterdir() inside a query is untracked. Use DirectoryResource.read().")
            return original_path_iterdir(path_obj)

        with (
            mock.patch("builtins.open", guarded_open),
            mock.patch("io.open", guarded_io_open),
            mock.patch("os.getenv", guarded_getenv),
            mock.patch("os.listdir", guarded_listdir),
            mock.patch("os.scandir", guarded_scandir),
            mock.patch("os.environ", guarded_environ),
            mock.patch("pathlib.Path.iterdir", guarded_path_iterdir),
        ):
            yield

    def _ensure_tracked_read(self, message: str) -> None:
        if self._current_frame() is not None and not self._allow_raw_reads.get():
            raise UntrackedReadError(message)

    def _code_fingerprint(self, fn: FunctionType) -> str:
        payload = (
            sys.implementation.name,
            getattr(sys.implementation, "cache_tag", None),
            tuple(sys.version_info[:3]),
            self._function_definition_payload(fn, set()),
        )
        return fingerprint_snapshot(payload)

    def _function_definition_payload(self, fn: FunctionType, seen_functions: builtins.set[int]) -> Any:
        fn_id = id(fn)
        if fn_id in seen_functions:
            return ("recursive-function", fn.__module__, fn.__qualname__)
        seen_functions.add(fn_id)
        try:
            closure_vars = inspect.getclosurevars(fn)
            return (
                fn.__module__,
                fn.__qualname__,
                marshal.dumps(fn.__code__),
                tuple(
                    self._captured_dependency_digest(
                        f"default[{index}]",
                        value,
                        seen_functions,
                        owner=fn,
                    )
                    for index, value in enumerate(fn.__defaults__ or ())
                ),
                tuple(
                    (
                        name,
                        self._captured_dependency_digest(
                            f"kwdefault[{name}]",
                            value,
                            seen_functions,
                            owner=fn,
                        ),
                    )
                    for name, value in sorted((fn.__kwdefaults__ or {}).items())
                ),
                tuple(
                    (
                        scope_name,
                        name,
                        self._captured_dependency_digest(name, value, seen_functions, owner=fn),
                    )
                    for scope_name, mapping in (
                        ("nonlocal", closure_vars.nonlocals),
                        ("global", closure_vars.globals),
                    )
                    for name, value in sorted(mapping.items())
                ),
            )
        finally:
            seen_functions.remove(fn_id)

    def _captured_dependency_digest(
        self,
        name: str,
        value: Any,
        seen_functions: builtins.set[int],
        *,
        owner: FunctionType,
    ) -> Any:
        from .core import Input, Query

        if isinstance(value, Query):
            return ("query", value.query_id)
        if isinstance(value, Input):
            return ("input", value.name, id(value))
        if self._is_resource_handle(value):
            return ("resource", self._resource_identity_payload(value))
        if isinstance(value, ModuleType):
            return ("module", value.__name__)
        if isinstance(value, FunctionType):
            return ("function", self._function_definition_payload(value, seen_functions))
        if isinstance(value, BuiltinFunctionType):
            return ("builtin", value.__module__, value.__qualname__)
        if isinstance(value, type):
            return ("type", value.__module__, value.__qualname__)
        try:
            return ("value", self._freeze_static_capture(value, set()))
        except UnsupportedValueError as exc:
            raise UnsupportedValueError(
                f"Query {owner.__module__}:{owner.__qualname__} captures unsupported ambient value "
                f"{name!r} of type {type(value).__qualname__}. "
                "Move mutable state behind Input/Resource nodes or use an immutable value."
            ) from exc

    def _resource_identity_payload(self, resource: Any) -> Any:
        resource_identity = getattr(resource, "identity", None)
        if callable(resource_identity):
            return (
                type(resource).__module__,
                type(resource).__qualname__,
                self._freeze_value(resource_identity()),
            )
        try:
            return (
                type(resource).__module__,
                type(resource).__qualname__,
                self._freeze_value(resource),
            )
        except UnsupportedValueError as exc:
            raise UnsupportedValueError(
                f"Resource {type(resource).__module__}:{type(resource).__qualname__} must be snapshot-safe "
                "or define identity()."
            ) from exc

    def _freeze_static_capture(self, value: Any, active_ids: builtins.set[int]) -> Any:
        if isinstance(value, (str, bytes, int, float, bool, type(None), complex)):
            return value
        if isinstance(value, (FrozenList, FrozenDict, FrozenSet, FrozenRecord, FrozenAdapterValue)):
            return value
        if isinstance(value, os.PathLike):
            return os.fspath(value)
        if isinstance(value, range):
            return ("range", value.start, value.stop, value.step)
        if isinstance(value, tuple):
            with self._capture_guard(value, active_ids):
                return tuple(self._freeze_static_capture(item, active_ids) for item in value)
        if isinstance(value, frozenset):
            with self._capture_guard(value, active_ids):
                items = tuple(self._freeze_static_capture(item, active_ids) for item in value)
                return ("frozenset", tuple(sorted(items, key=fingerprint_snapshot)))
        if is_dataclass(value) and not isinstance(value, type):
            params = getattr(type(value), "__dataclass_params__", None)
            if params is None or not params.frozen:
                raise UnsupportedValueError("Mutable dataclass values cannot be captured ambiently.")
            with self._capture_guard(value, active_ids):
                return FrozenRecord(
                    type(value).__qualname__,
                    tuple(
                        (field.name, self._freeze_static_capture(getattr(value, field.name), active_ids))
                        for field in fields(value)
                    ),
                )
        raise UnsupportedValueError("Unsupported ambient capture.")

    @contextmanager
    def _capture_guard(self, value: Any, active_ids: builtins.set[int]) -> Iterator[None]:
        object_id = id(value)
        if object_id in active_ids:
            raise UnsupportedValueError("Cyclic ambient values are not supported.")
        active_ids.add(object_id)
        try:
            yield
        finally:
            active_ids.remove(object_id)

    def _is_resource_handle(self, value: Any) -> bool:
        return all(callable(getattr(value, name, None)) for name in ("label", "probe", "load"))

    @contextmanager
    def _request_scope(self) -> Iterator[None]:
        current = self._request_token.get()
        if current is not None:
            yield
            return
        self._request_counter += 1
        token = self._request_token.set(self._request_counter)
        try:
            yield
        finally:
            self._request_token.reset(token)
            self._evict_query_nodes_if_needed()

    def _mark_query_used(self, key: NodeKey) -> None:
        self._query_touch_counter += 1
        self._query_last_used[key] = self._query_touch_counter

    def _evict_query_nodes_if_needed(self) -> None:
        limit = self.max_query_nodes
        if limit is None:
            return
        while len(self._query_records) > limit:
            lru_key = min(self._query_records, key=lambda item: self._query_last_used.get(item, -1))
            self._evict_query_record(lru_key)

    def _evict_query_record(self, key: NodeKey) -> None:
        self._records.pop(key, None)
        self._query_records.discard(key)
        self._query_last_used.pop(key, None)
        self._call_snapshots().pop(key, None)

    def _current_request_id(self) -> int:
        current = self._request_token.get()
        if current is None:
            return -1
        return current

    def _current_frame(self) -> ExecutionFrame | None:
        stack = self._execution_stack.get()
        if not stack:
            return None
        return stack[-1]

    def _freeze_value(self, value: Any) -> Snapshot:
        return freeze(value, adapters=self._adapters)

    def _thaw_value(self, value: Any) -> Any:
        return thaw(value, adapters=self._adapters)

    def _fingerprint_value(self, value: Any) -> str:
        return fingerprint(value, adapters=self._adapters)

    def _semantic_equal(self, left: Any, right: Any) -> bool:
        return semantic_equal(left, right, adapters=self._adapters)

    def _compare_values(
        self,
        *,
        eq: Callable[[Any, Any], bool] | None,
        cutoff: Callable[[Any], Any] | None,
        left: Any,
        right: Any,
    ) -> bool:
        if cutoff is not None:
            return self._freeze_cutoff_token(cutoff(left)) == self._freeze_cutoff_token(cutoff(right))
        if eq is None:
            return self._semantic_equal(left, right)
        return eq(left, right)

    def _freeze_cutoff_token(self, value: Any) -> Snapshot:
        try:
            return self._freeze_value(value)
        except UnsupportedValueError as exc:
            raise UnsupportedValueError("Cutoff functions must return snapshot-safe values.") from exc
