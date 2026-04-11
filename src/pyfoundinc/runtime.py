from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import builtins
import io
import marshal
import os
import sys
from types import FunctionType
from typing import Any
from unittest import mock

from .errors import CycleError, UntrackedReadError
from .explain import format_explanation
from .value import (
    ValueAdapter,
    assert_not_mutated,
    fingerprint,
    fingerprint_snapshot,
    freeze,
    thaw,
    semantic_equal,
)


Mode = str


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


class Database:
    def __init__(
        self,
        mode: Mode = "strict",
        *,
        adapters: Mapping[type[Any], ValueAdapter] | None = None,
    ) -> None:
        if mode not in {"strict", "checked", "fast"}:
            raise ValueError("mode must be one of: strict, checked, fast")
        self.mode = mode
        self._adapters = dict(adapters or {})
        self._revision = 0
        self._records: dict[NodeKey, NodeRecord] = {}
        self._input_records: dict[Any, NodeKey] = {}
        self._active_frame: ContextVar[ExecutionFrame | None] = ContextVar("pyfoundinc_active_frame", default=None)
        self._allow_open: ContextVar[bool] = ContextVar("pyfoundinc_allow_open", default=False)
        self._request_token: ContextVar[int | None] = ContextVar("pyfoundinc_request_token", default=None)
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
        comparator = input_key.eq or self._semantic_equal
        if record is not None and comparator(self._thaw_value(record.snapshot), self._thaw_value(snapshot)):
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

    def get(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        from .core import Query

        if not isinstance(query, Query):
            raise TypeError("db.get() expects a @query-decorated callable.")
        with self._request_scope():
            key, call_snapshot = self._query_key(query, args, kwargs)
            self._record_dependency(key)
            self._ensure_query(query, key, call_snapshot)
            return self._expose_boundary_snapshot(self._records[key].snapshot)

    def explain(self, query: Any, *args: Any, **kwargs: Any) -> str:
        from .core import Query

        if not isinstance(query, Query):
            raise TypeError("db.explain() expects a @query-decorated callable.")
        with self._request_scope():
            key, call_snapshot = self._query_key(query, args, kwargs)
            self._ensure_query(query, key, call_snapshot)
            return format_explanation(self, key)

    def report_untracked_read(self, reason: str) -> None:
        frame = self._active_frame.get()
        if frame is None:
            raise RuntimeError("db.report_untracked_read() must be called while a query is executing.")
        frame.untracked_reasons.append(reason)

    def _read_input(self, input_key: Any) -> Any:
        key = self._input_key(input_key)
        record = self._records.get(key)
        if record is None:
            raise KeyError(f"Input {input_key.name!r} has not been set.")
        self._record_dependency(key)
        return self._expose_boundary_snapshot(record.snapshot)

    def _read_resource(self, resource: Any, parameter: Any) -> Any:
        key = self._resource_key(resource, parameter)
        self._record_dependency(key)
        self._refresh_resource(resource, parameter, key)
        return self._expose_boundary_snapshot(self._records[key].snapshot)

    def _ensure_query(self, query: Any, key: NodeKey, call_snapshot: Any) -> None:
        existing = self._records.get(key)
        current_request = self._current_request_id()
        if existing is None:
            self._execute_query(query, key, call_snapshot, previous=None, reason="cold execute")
            return
        if existing.checked_in_request == current_request:
            existing.last_decision = "reused"
            existing.reason = "already checked in current request"
            return
        if existing.is_untracked:
            self._execute_query(query, key, call_snapshot, previous=existing, reason="untracked dependency")
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
            return
        self._execute_query(query, key, call_snapshot, previous=existing, reason=dirty_reason)

    def _execute_query(self, query: Any, key: NodeKey, call_snapshot: Any, previous: NodeRecord | None, reason: str) -> None:
        current_frame = self._active_frame.get()
        if current_frame is not None and current_frame.key == key:
            raise CycleError(f"Cycle detected while evaluating {key.label}.")
        frame = ExecutionFrame(key=key)
        token = self._active_frame.set(frame)
        try:
            query_args, query_kwargs = self._materialize_call(
                call_snapshot,
                record_boundaries=self.mode == "checked",
                frame=frame,
            )
            with self._guard_untracked_open():
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
                previous_changed_at = self._revision
                decision = "executed"
            else:
                record = previous
                previous_changed_at = previous.changed_at
                old_value = self._expose_snapshot(previous.snapshot)
                new_value = self._expose_snapshot(snapshot)
                equal = False if impure else self._compare_values(query.eq, old_value, new_value)
                record.snapshot = snapshot
                record.digest = digest
                if equal:
                    record.changed_at = previous_changed_at
                    decision = "backdated"
                else:
                    record.changed_at = self._revision
                    decision = "executed"
            record.verified_at = self._revision
            record.dependencies = frame.dependencies
            record.last_decision = decision
            record.last_recompute = decision
            record.reason = reason
            record.untracked_reasons = list(frame.untracked_reasons)
            record.checked_in_request = self._current_request_id()
        finally:
            self._active_frame.reset(token)

    def _maybe_changed_after(self, key: NodeKey, revision: int) -> bool:
        record = self._records.get(key)
        if record is None:
            return True
        if key.kind == "query":
            query = record.key.identity
            query_obj = self._query_objects()[query]
            call_snapshot = self._call_snapshots()[key]
            self._ensure_query(query_obj, key, call_snapshot)
        elif key.kind == "resource":
            resource, parameter = self._resource_objects()[key]
            self._refresh_resource(resource, parameter, key)
        return self._records[key].is_untracked or self._records[key].changed_at > revision

    def _refresh_resource(self, resource: Any, parameter: Any, key: NodeKey) -> None:
        with self._allow_raw_open():
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
        with self._allow_raw_open():
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
        key = NodeKey(
            kind="resource",
            identity=f"{type(resource).__module__}:{type(resource).__qualname__}",
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
        if self.mode == "strict":
            exposed = snapshot
        else:
            exposed = self._thaw_value(snapshot)
        if boundary and record_boundaries and frame is not None:
            frame.boundary_fingerprints.append(self._fingerprint_value(exposed))
            frame.boundary_values.append(exposed)
        return exposed

    def _expose_boundary_snapshot(self, snapshot: Any) -> Any:
        frame = self._active_frame.get()
        return self._expose_snapshot(
            snapshot,
            boundary=True,
            record_boundaries=self.mode == "checked" and frame is not None,
            frame=frame,
        )

    def _record_dependency(self, key: NodeKey) -> None:
        frame = self._active_frame.get()
        if frame is None:
            return
        frame.dependencies.add(key)

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
    def _allow_raw_open(self) -> Any:
        token = self._allow_open.set(True)
        try:
            yield
        finally:
            self._allow_open.reset(token)

    @contextmanager
    def _guard_untracked_open(self) -> Any:
        original_builtins_open = builtins.open
        original_io_open = io.open

        def guarded_open(*args: Any, **kwargs: Any) -> Any:
            if self._active_frame.get() is not None and not self._allow_open.get():
                raise UntrackedReadError("Raw open() inside a query is untracked. Use FileResource.read().")
            return original_builtins_open(*args, **kwargs)

        def guarded_io_open(*args: Any, **kwargs: Any) -> Any:
            if self._active_frame.get() is not None and not self._allow_open.get():
                raise UntrackedReadError("Raw open() inside a query is untracked. Use FileResource.read().")
            return original_io_open(*args, **kwargs)

        with mock.patch("builtins.open", guarded_open), mock.patch("io.open", guarded_io_open):
            yield

    def _code_fingerprint(self, fn: FunctionType) -> str:
        closure = None
        if fn.__closure__:
            closure = tuple(self._closure_digest(cell.cell_contents) for cell in fn.__closure__)
        payload = (
            sys.implementation.name,
            getattr(sys.implementation, "cache_tag", None),
            tuple(sys.version_info[:3]),
            fn.__module__,
            fn.__qualname__,
            marshal.dumps(fn.__code__),
            fn.__defaults__,
            fn.__kwdefaults__,
            closure,
        )
        return marshal.dumps(payload).hex()

    def _closure_digest(self, value: Any) -> tuple[str, str]:
        query_id = getattr(value, "query_id", None)
        if query_id is not None:
            return ("query", query_id)
        if isinstance(value, (str, bytes, int, float, bool, type(None), complex, tuple, frozenset, range)):
            return ("value", self._fingerprint_value(value))
        if isinstance(value, os.PathLike):
            return ("value", self._fingerprint_value(value))
        return ("identity", f"{type(value).__module__}:{type(value).__qualname__}:{id(value)}")

    @contextmanager
    def _request_scope(self) -> Any:
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

    def _current_request_id(self) -> int:
        current = self._request_token.get()
        if current is None:
            return -1
        return current

    def _freeze_value(self, value: Any) -> Any:
        return freeze(value, adapters=self._adapters)

    def _thaw_value(self, value: Any) -> Any:
        return thaw(value, adapters=self._adapters)

    def _fingerprint_value(self, value: Any) -> str:
        return fingerprint(value, adapters=self._adapters)

    def _semantic_equal(self, left: Any, right: Any) -> bool:
        return semantic_equal(left, right, adapters=self._adapters)

    def _compare_values(self, comparator: Callable[[Any, Any], bool] | None, left: Any, right: Any) -> bool:
        if comparator is None:
            return self._semantic_equal(left, right)
        return comparator(left, right)
