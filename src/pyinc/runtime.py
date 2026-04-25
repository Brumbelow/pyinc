from __future__ import annotations

import builtins
import hashlib
import inspect
import io
import marshal
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from types import BuiltinFunctionType, FunctionType, ModuleType
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast, overload

from .errors import CycleError, UnsupportedValueError, UntrackedReadError
from .store import ArtifactStore
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
    serialize_snapshot,
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


@dataclass(frozen=True)
class DatabaseStatistics:
    node_count: int
    input_count: int
    query_count: int
    resource_count: int
    query_executions: int
    query_reuses: int
    query_backdates: int
    resource_loads: int
    resource_probe_hits: int
    input_sets: int
    input_equal_ignores: int
    evictions: int
    total_requests: int


@dataclass(frozen=True)
class DependencyGraphNode:
    label: str
    kind: str
    changed_at: int
    verified_at: int
    last_decision: str
    is_untracked: bool
    dependency_labels: tuple[str, ...]


@dataclass(frozen=True)
class QueryProfile:
    query_label: str
    execution_count: int
    total_ns: int
    mean_ns: int


@dataclass(frozen=True)
class QueryChangeEvent:
    """Delivered to observers when a subscribed query's result changes.

    Fires only on the `"executed"` decision (cold execute or true recompute
    that produced a new value). `"reused"` and `"backdated"` decisions do
    not fire — the stored value did not move.
    """

    query_id: str
    args_digest: str
    decision: str
    changed_at: int
    verified_at: int


ObserverCallback = Callable[[QueryChangeEvent], None]
ObserverErrorHook = Callable[[Exception], None]


def _default_observer_error_hook(exc: Exception) -> None:
    sys.stderr.write(f"pyinc: observer callback raised {type(exc).__qualname__}: {exc}\n")


_ACTIVE_GUARDS: ContextVar[tuple[Database, ...]] = ContextVar(
    "pyinc_active_guards", default=()
)
_GUARD_INSTALLED = False
_GUARD_INSTALL_LOCK = threading.Lock()


def _raise_if_guarded(message: str) -> None:
    """Raise `UntrackedReadError` if any active Database has a running query without raw-read permission."""
    for db in _ACTIVE_GUARDS.get():
        if db._current_frame() is not None and not db._allow_raw_reads.get():
            raise UntrackedReadError(message)


def _install_guards_once() -> None:
    """Install global wrappers around raw I/O entry points exactly once per process.

    The wrappers consult `_ACTIVE_GUARDS` (a `ContextVar`) to determine whether
    any `Database` currently has a query frame on the calling context without
    raw-read permission. Installation is idempotent and thread-safe; once
    installed, the wrappers stay in place for the life of the process.
    """
    global _GUARD_INSTALLED
    if _GUARD_INSTALLED:
        return
    with _GUARD_INSTALL_LOCK:
        if _GUARD_INSTALLED:
            return

        original_builtins_open = builtins.open
        original_io_open = io.open
        original_os_getenv = os.getenv
        original_os_listdir = os.listdir
        original_os_scandir = os.scandir
        original_path_iterdir = Path.iterdir
        original_environ = os.environ

        def guarded_open(*args: Any, **kwargs: Any) -> Any:
            _raise_if_guarded("Raw open() inside a query is untracked. Use FileResource.read().")
            return original_builtins_open(*args, **kwargs)

        def guarded_io_open(*args: Any, **kwargs: Any) -> Any:
            _raise_if_guarded("Raw open() inside a query is untracked. Use FileResource.read().")
            return original_io_open(*args, **kwargs)

        def guarded_getenv(key: str, default: str | None = None) -> str | None:
            _raise_if_guarded("Raw os.getenv() inside a query is untracked. Use EnvResource.read().")
            return original_os_getenv(key, default)

        def guarded_listdir(*args: Any, **kwargs: Any) -> Any:
            _raise_if_guarded(
                "Raw os.listdir() inside a query is untracked. Use DirectoryResource.read()."
            )
            return original_os_listdir(*args, **kwargs)

        def guarded_scandir(*args: Any, **kwargs: Any) -> Any:
            _raise_if_guarded(
                "Raw os.scandir() inside a query is untracked. Use DirectoryResource.read()."
            )
            return original_os_scandir(*args, **kwargs)

        def guarded_path_iterdir(path_obj: Path) -> Any:
            _raise_if_guarded(
                "Raw Path.iterdir() inside a query is untracked. Use DirectoryResource.read()."
            )
            return original_path_iterdir(path_obj)

        guarded_environ = _GuardedEnviron(
            original_environ,
            lambda: _raise_if_guarded(
                "Raw os.environ access inside a query is untracked. Use EnvResource.read()."
            ),
        )

        builtins.open = guarded_open
        io.open = guarded_io_open
        os.getenv = guarded_getenv  # type: ignore[assignment]
        os.listdir = guarded_listdir
        os.scandir = guarded_scandir
        os.environ = guarded_environ  # type: ignore[assignment]  # noqa: B003
        Path.iterdir = guarded_path_iterdir  # type: ignore[assignment, method-assign]
        _GUARD_INSTALLED = True


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


class Subscription:
    """Handle returned by `Database.observe(...)`.

    Calling `unsubscribe()` detaches the callback from the subscribed
    query node. Repeated unsubscribes are no-ops. Subscriptions do not
    keep the observed node alive under LRU eviction; if the node is
    evicted and later re-executed, the callback fires as normal.
    """

    __slots__ = ("_database", "_key", "_callback", "_active")

    def __init__(self, database: Database, key: NodeKey, callback: ObserverCallback) -> None:
        self._database = database
        self._key = key
        self._callback = callback
        self._active = True

    def unsubscribe(self) -> None:
        if not self._active:
            return
        self._active = False
        self._database._unregister_observer(self._key, self._callback)


class Database:
    def __init__(
        self,
        mode: Mode = "strict",
        *,
        adapters: Mapping[type[Any], ValueAdapter] | None = None,
        max_query_nodes: int | None = None,
        observer_error_hook: ObserverErrorHook | None = None,
        store: ArtifactStore | None = None,
    ) -> None:
        if mode not in {"strict", "checked", "fast"}:
            raise ValueError("mode must be one of: strict, checked, fast")
        if max_query_nodes is not None and max_query_nodes <= 0:
            raise ValueError("max_query_nodes must be a positive integer or None.")
        self.mode = mode
        self.max_query_nodes = max_query_nodes
        self._adapters = dict(adapters or {})
        self._store = store
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
        self._stats: dict[str, int] = {
            "query_executions": 0,
            "query_reuses": 0,
            "query_backdates": 0,
            "resource_loads": 0,
            "resource_probe_hits": 0,
            "input_sets": 0,
            "input_equal_ignores": 0,
            "evictions": 0,
        }
        self._query_timings: dict[str, list[int]] = {}
        self._module_identity_cache: dict[tuple[str, str, int, int], Any] = {}
        self._state_lock = threading.RLock()
        self._observers: dict[NodeKey, list[ObserverCallback]] = {}
        self._observer_error_hook: ObserverErrorHook = (
            observer_error_hook if observer_error_hook is not None else _default_observer_error_hook
        )
        self._pending_events: ContextVar[list[tuple[NodeKey, QueryChangeEvent]] | None] = ContextVar(
            "pyinc_pending_events", default=None
        )
        _install_guards_once()

    @property
    def revision(self) -> int:
        return self._revision

    def statistics(self) -> DatabaseStatistics:
        resource_count = sum(1 for k in self._records if k.kind == "resource")
        return DatabaseStatistics(
            node_count=len(self._records),
            input_count=len(self._input_records),
            query_count=len(self._query_records),
            resource_count=resource_count,
            query_executions=self._stats["query_executions"],
            query_reuses=self._stats["query_reuses"],
            query_backdates=self._stats["query_backdates"],
            resource_loads=self._stats["resource_loads"],
            resource_probe_hits=self._stats["resource_probe_hits"],
            input_sets=self._stats["input_sets"],
            input_equal_ignores=self._stats["input_equal_ignores"],
            evictions=self._stats["evictions"],
            total_requests=self._request_counter,
        )

    def reset_statistics(self) -> None:
        for key in self._stats:
            self._stats[key] = 0
        self._query_timings.clear()

    def query_profile(self) -> tuple[QueryProfile, ...]:
        profiles: list[QueryProfile] = []
        for identity, timings in sorted(self._query_timings.items()):
            total_ns = sum(timings)
            count = len(timings)
            profiles.append(QueryProfile(
                query_label=identity,
                execution_count=count,
                total_ns=total_ns,
                mean_ns=total_ns // count,
            ))
        return tuple(profiles)

    def dependency_graph(self) -> tuple[DependencyGraphNode, ...]:
        nodes: list[DependencyGraphNode] = []
        for key, record in self._records.items():
            dep_labels = tuple(
                sorted(self._records[dep].label for dep in record.dependencies if dep in self._records)
            )
            nodes.append(DependencyGraphNode(
                label=record.label,
                kind=key.kind,
                changed_at=record.changed_at,
                verified_at=record.verified_at,
                last_decision=record.last_decision,
                is_untracked=record.is_untracked,
                dependency_labels=dep_labels,
            ))
        return tuple(sorted(nodes, key=lambda n: n.label))

    def set(self, input_key: Any, value: Any) -> None:
        from .core import Input

        if not isinstance(input_key, Input):
            raise TypeError("db.set() expects an Input instance.")
        with self._state_lock:
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
                self._stats["input_equal_ignores"] += 1
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
            self._stats["input_sets"] += 1

    def set_many(self, updates: Iterable[tuple[Any, Any]]) -> None:
        from .core import Input

        with self._state_lock:
            # Phase 1: collect and validate all updates, compute snapshots.
            pending: list[tuple[Any, Any, NodeKey, Any, str]] = []  # (input_key, value, node_key, snapshot, digest)
            for input_key, value in updates:
                if not isinstance(input_key, Input):
                    raise TypeError("db.set_many() expects (Input, value) pairs.")
                snapshot = self._freeze_value(value)
                digest = fingerprint_snapshot(snapshot)
                node_key = self._input_key(input_key)
                pending.append((input_key, value, node_key, snapshot, digest))

            # Phase 2: determine which inputs actually changed.
            changed: list[tuple[Any, NodeKey, Any, str]] = []
            equal_count = 0
            request_id = self._current_request_id()
            for input_key, _value, node_key, snapshot, digest in pending:
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
                    record.checked_in_request = request_id
                    equal_count += 1
                else:
                    changed.append((input_key, node_key, snapshot, digest))

            self._stats["input_equal_ignores"] += equal_count

            if not changed:
                return

            # Phase 3: single revision bump, apply all changed inputs.
            self._revision += 1
            changed_at = self._revision
            for _input_key, node_key, snapshot, digest in changed:
                record = self._records.get(node_key)
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
                        checked_in_request=request_id,
                    )
                else:
                    record.snapshot = snapshot
                    record.digest = digest
                    record.changed_at = changed_at
                    record.verified_at = changed_at
                    record.last_decision = "executed"
                    record.last_recompute = "executed"
                    record.reason = "input changed"
                    record.checked_in_request = request_id
                self._stats["input_sets"] += 1

    def get(self, query: Query[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        from .core import Query

        if not isinstance(query, Query):
            raise TypeError("db.get() expects a @query-decorated callable.")
        with self._state_lock, self._request_scope() as pending:
            key, call_snapshot = self._query_key(query, args, kwargs)
            self._record_dependency(key)
            self._ensure_query(query, key, call_snapshot)
            result = cast(T, self._expose_boundary_snapshot(self._records[key].snapshot))
        self._dispatch_events(pending)
        return result

    def explain(self, query: Query[P, Any], *args: P.args, **kwargs: P.kwargs) -> str:
        from .core import Query

        if not isinstance(query, Query):
            raise TypeError("db.explain() expects a @query-decorated callable.")
        return format_explanation(self.inspect(query, *args, **kwargs))

    def inspect(self, query: Query[P, Any], *args: P.args, **kwargs: P.kwargs) -> InspectionNode:
        from .core import Query

        if not isinstance(query, Query):
            raise TypeError("db.inspect() expects a @query-decorated callable.")
        with self._state_lock, self._request_scope() as pending:
            key, call_snapshot = self._query_key(query, args, kwargs)
            if key not in self._records:
                self._ensure_query(query, key, call_snapshot)
            node = self._inspect_record(key)
        self._dispatch_events(pending)
        return node

    def inspect_fresh(self, query: Query[P, Any], *args: P.args, **kwargs: P.kwargs) -> InspectionNode:
        from .core import Query

        if not isinstance(query, Query):
            raise TypeError("db.inspect_fresh() expects a @query-decorated callable.")
        with self._state_lock, self._request_scope() as pending:
            key, call_snapshot = self._query_key(query, args, kwargs)
            self._ensure_query(query, key, call_snapshot)
            node = self._inspect_record(key)
        self._dispatch_events(pending)
        return node

    def observe(
        self,
        callback: ObserverCallback,
        query: Query[P, Any],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Subscription:
        """Register `callback` to fire whenever the query node's value changes.

        Observer callbacks fire once per top-level `get` / `inspect` /
        `inspect_fresh` call in which the node was re-executed and produced a
        new value (decision `"executed"`). Backdated and reused decisions do
        not fire — by definition the stored value did not move.

        Callbacks run after the request scope completes and the kernel lock is
        released, so a callback may safely call back into the database.
        Exceptions from a callback are routed to the `observer_error_hook`
        (default: a one-line stderr log) and do not suppress sibling callbacks
        or corrupt kernel state.
        """
        from .core import Query

        if not isinstance(query, Query):
            raise TypeError("db.observe() expects a @query-decorated callable.")
        if not callable(callback):
            raise TypeError("db.observe() expects a callable as its first argument.")
        with self._state_lock:
            key, _ = self._query_key(query, args, kwargs)
            self._observers.setdefault(key, []).append(callback)
        return Subscription(self, key, callback)

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
            self._stats["query_reuses"] += 1
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
            self._stats["query_reuses"] += 1
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
                t0 = time.perf_counter_ns()
                result = query.fn(self, *query_args, **query_kwargs)
                elapsed = time.perf_counter_ns() - t0
            self._query_timings.setdefault(key.label, []).append(elapsed)
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
            if decision == "backdated":
                self._stats["query_backdates"] += 1
            else:
                self._stats["query_executions"] += 1
                self._enqueue_observer_event(query, key, record)
        finally:
            self._execution_stack.reset(token)

    def _enqueue_observer_event(self, query: Any, key: NodeKey, record: NodeRecord) -> None:
        if key not in self._observers:
            return
        pending = self._pending_events.get()
        if pending is None:
            return
        pending.append((
            key,
            QueryChangeEvent(
                query_id=query.query_id,
                args_digest=key.args_digest,
                decision="executed",
                changed_at=record.changed_at,
                verified_at=record.verified_at,
            ),
        ))

    def _unregister_observer(self, key: NodeKey, callback: ObserverCallback) -> None:
        with self._state_lock:
            callbacks = self._observers.get(key)
            if callbacks is None:
                return
            try:
                callbacks.remove(callback)
            except ValueError:
                return
            if not callbacks:
                del self._observers[key]

    def _dispatch_events(
        self, events: list[tuple[NodeKey, QueryChangeEvent]] | None
    ) -> None:
        if not events:
            return
        with self._state_lock:
            snapshots = [
                (event, tuple(self._observers.get(key, ())))
                for key, event in events
            ]
        for event, callbacks in snapshots:
            for callback in callbacks:
                try:
                    callback(event)
                except Exception as exc:
                    with suppress(Exception):
                        self._observer_error_hook(exc)

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
        atomic = hasattr(resource, "probe_and_load")
        if atomic:
            with self._allow_raw_reads_scope():
                probe, loaded_value = resource.probe_and_load(self, parameter)
        else:
            with self._allow_raw_reads_scope():
                probe = resource.probe(parameter)
            loaded_value = None
        record = self._records.get(key)
        current_request = self._current_request_id()
        if record is not None and record.checked_in_request == current_request:
            return
        if record is not None and record.probe == probe:
            record.verified_at = self._revision
            record.last_decision = "reused"
            record.reason = "resource probe unchanged"
            record.checked_in_request = current_request
            self._stats["resource_probe_hits"] += 1
            return
        if record is None:
            changed_at = self._revision
        else:
            self._revision += 1
            changed_at = self._revision
        if not atomic:
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
            self._stats["resource_loads"] += 1
            return
        record.snapshot = snapshot
        record.digest = digest
        record.changed_at = changed_at
        record.verified_at = self._revision
        record.last_decision = "executed"
        record.last_recompute = "executed"
        record.reason = "resource probe changed"
        self._stats["resource_loads"] += 1
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
        stack = _ACTIVE_GUARDS.get()
        token = _ACTIVE_GUARDS.set(stack + (self,))
        try:
            yield
        finally:
            _ACTIVE_GUARDS.reset(token)

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
            return ("module", value.__name__, self._module_identity_payload(value))
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
                "Move mutable state behind Input/Resource nodes or use an immutable value. "
                "Run pyinc.explain_query_captures(...) to inspect the capture set before the first db.get()."
            ) from exc

    def _module_identity_payload(self, module: ModuleType) -> Any:
        """Compute a structural digest for a captured module.

        Name-only capture is not sufficient: a third-party version bump or a
        source-file edit changes `module.CONSTANT` without touching the
        module's name, which would silently reuse stale cache entries.
        The payload combines:

        * `__version__` (if the module exposes one — standard for third-party
          packages);
        * a digest of `module.__file__` — `sha256` of the bytes for `.py`
          sources, `(path, size, mtime_ns)` for compiled or namespace-pkg
          files, `None` for frozen / built-in modules (which are pinned via
          `sys.version_info` in the outer code fingerprint);
        * a sorted `__all__` tuple when declared, capturing the module's
          publicly promised surface.

        In-process monkey-patch of module attributes is *not* covered and is
        explicitly listed in `docs/kernel-contract.md` as out of scope; users
        relying on such state must route it through `Input` / `Resource`.
        """
        version = getattr(module, "__version__", None)
        version_digest: str | None = str(version) if version is not None else None

        all_attr = getattr(module, "__all__", None)
        if isinstance(all_attr, (list, tuple)):
            all_tuple: tuple[str, ...] | None = tuple(sorted(str(item) for item in all_attr))
        else:
            all_tuple = None

        file_path = getattr(module, "__file__", None)
        if not isinstance(file_path, str):
            return (version_digest, None, all_tuple)

        with self._allow_raw_reads_scope():
            try:
                stat_result = os.stat(file_path)
            except OSError:
                return (version_digest, None, all_tuple)

            cache_key = (module.__name__, file_path, stat_result.st_mtime_ns, stat_result.st_size)
            cached = self._module_identity_cache.get(cache_key)
            if cached is None:
                if file_path.endswith(".py"):
                    try:
                        raw = Path(file_path).read_bytes()
                    except OSError:
                        cached = ("stat", file_path, stat_result.st_mtime_ns, stat_result.st_size)
                    else:
                        cached = ("source-sha256", hashlib.sha256(raw).hexdigest())
                else:
                    cached = ("stat", file_path, stat_result.st_mtime_ns, stat_result.st_size)
                self._module_identity_cache[cache_key] = cached

        return (version_digest, cached, all_tuple)

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
    def _request_scope(
        self,
    ) -> Iterator[list[tuple[NodeKey, QueryChangeEvent]] | None]:
        current = self._request_token.get()
        if current is not None:
            yield None
            return
        self._request_counter += 1
        token = self._request_token.set(self._request_counter)
        pending: list[tuple[NodeKey, QueryChangeEvent]] = []
        events_token = self._pending_events.set(pending)
        try:
            yield pending
        finally:
            self._pending_events.reset(events_token)
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
        self._stats["evictions"] += 1
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
        snapshot = freeze(value, adapters=self._adapters)
        if self._store is not None:
            self._persist_snapshot(snapshot)
        return snapshot

    def _persist_snapshot(self, snapshot: Snapshot) -> None:
        """Write the snapshot's serialized bytes to the configured ArtifactStore.
        Raw filesystem I/O runs under the raw-read allow scope so a `FileSystemArtifactStore`
        used while a query frame is active is not rejected by the global guard."""
        store = self._store
        if store is None:
            return
        digest = fingerprint_snapshot(snapshot)
        if store.contains(digest):
            return
        payload = serialize_snapshot(snapshot)
        with self._allow_raw_reads_scope():
            store.put(digest, payload)

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
