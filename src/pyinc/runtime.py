from __future__ import annotations

import _thread
import builtins
import concurrent.futures
import concurrent.futures.process
import concurrent.futures.thread
import dis
import functools
import hashlib
import importlib.machinery
import inspect
import io
import json
import multiprocessing.pool
import multiprocessing.process
import os
import struct
import subprocess
import sys
import sysconfig
import threading
import time
import typing
import weakref
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from dataclasses import Field as DataclassField
from pathlib import Path
from types import (
    BuiltinFunctionType,
    CodeType,
    FunctionType,
    GenericAlias,
    GetSetDescriptorType,
    MappingProxyType,
    MemberDescriptorType,
    MethodDescriptorType,
    MethodType,
    ModuleType,
    TracebackType,
    UnionType,
    WrapperDescriptorType,
)
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast, overload

from ._path_identity import is_stdlib_path
from .errors import (
    CheckpointIntegrityError,
    CheckpointManifestError,
    CheckpointModeError,
    CheckpointVersionError,
    CycleError,
    InputKeyError,
    QueryConcurrencyError,
    QueryContextError,
    ResourceDependencyError,
    UnsupportedValueError,
    UntrackedReadError,
)
from .explain import InspectionNode, format_explanation
from .store import ArtifactStore
from .value import (
    FrozenAdapterValue,
    FrozenDict,
    FrozenGraph,
    FrozenList,
    FrozenRecord,
    FrozenRef,
    FrozenSet,
    Snapshot,
    ValueAdapter,
    _adapter_key,
    assert_not_mutated,
    collect_adapter_keys,
    deserialize_snapshot,
    detach_snapshot,
    fingerprint,
    fingerprint_snapshot,
    freeze,
    semantic_equal,
    serialize_snapshot,
    snapshots_equal,
    thaw,
)

if TYPE_CHECKING:
    import pyinc.core as _core
    import pyinc.resources as _resources


Mode = str
DefaultT = TypeVar("DefaultT")
P = ParamSpec("P")
T = TypeVar("T")
ResourceKeyT = TypeVar("ResourceKeyT")
ResourceValueT = TypeVar("ResourceValueT")
ResourceProbeT = TypeVar("ResourceProbeT")

# Durable checkpoint manifest schema version. Bumped whenever the identity, the
# record layout, or the meaning of a recorded field changes, so stale manifests
# are rejected loudly rather than silently reused. Version 7 binds every
# semantic change/probe decision to the deep typed snapshot relation, rejects
# resource hooks that observe database-managed state, rejects query-time
# administration, and refuses fallbacks derived from an unrecorded child-query
# failure. A version-6 record may have
# backdated Python-equal numeric types, restored a checkpoint probe hint across
# such a transition, omitted a dependency read inside a resource hook, or cached
# a caught child failure without any edge that could invalidate it, or a result
# derived from an in-query database mutation or administrative operation.
_CHECKPOINT_MANIFEST_VERSION = 7
# Version of the snapshot/fingerprint encoding this kernel emits, mirrored from
# value._KERNEL_FINGERPRINT_PREFIX (b"K2;"). Recorded in the manifest and checked
# at load so a checkpoint from a differently-encoded kernel is never trusted.
_KERNEL_FINGERPRINT_VERSION = 2
_DEFAULT_SEMANTIC_EQUALITY_VERSION = 2
_MISSING_SNAPSHOT = object()


def _build_runtime_build_payload() -> tuple[Any, ...]:
    return (
        "runtime-build-v3",
        sys.implementation.name,
        getattr(sys.implementation, "cache_tag", None),
        tuple(sys.version_info),
        (
            "flags",
            tuple(sys.flags),
            sys.flags.optimize,
            sys.flags.debug,
            sys.flags.dont_write_bytecode,
            sys.flags.hash_randomization,
            sys.flags.utf8_mode,
            sys.flags.isolated,
            sys.flags.no_site,
            getattr(sys.flags, "safe_path", 0),
            getattr(sys.flags, "int_max_str_digits", -1),
            getattr(sys.flags, "gil", 1),
            sys.platform,
            os.name,
            sys.byteorder,
        ),
        (
            "abi",
            sys.api_version,
            getattr(sys, "abiflags", ""),
            getattr(sys.implementation, "_multiarch", None),
            sysconfig.get_platform(),
            sysconfig.get_config_var("SOABI"),
            sysconfig.get_config_var("EXT_SUFFIX"),
            struct.calcsize("P") * 8,
            sys.version,
        ),
    )


_RUNTIME_BUILD_PAYLOAD = _build_runtime_build_payload()


class _ProcessIdentityToken:
    """Opaque process-incarnation state excluded from captured module constants."""

    __slots__ = ("nonce",)

    def __init__(self) -> None:
        self.nonce = os.urandom(16)


# Definition objects owned permanently by code/module objects can use this
# process token. Directly replaceable definition sites use the stronger
# per-Database generation registry initialized below.
_PROCESS_IDENTITY_TOKEN = _ProcessIdentityToken()
_DEFINITION_IDENTITY_LOCK = threading.RLock()
_DEFINITION_IDENTITY_GENERATION = [0]
_DEFINITION_IDENTITY_SITES: weakref.WeakKeyDictionary[FunctionType, dict[str, tuple[Any, int]]] = (
    weakref.WeakKeyDictionary()
)


class _StateIdentityOwner:
    """Identity-site state tied to one policy, adapter, or resource object."""

    __slots__ = ("generation", "owner_ref", "owner_strong", "sites")

    def __init__(self, owner: object, generation: int) -> None:
        self.generation = generation
        self.owner_ref: weakref.ReferenceType[object] | None = None
        self.owner_strong: object | None = None
        self.sites: dict[str, tuple[Any, int]] = {}
        try:
            self.owner_ref = weakref.ref(owner)
        except TypeError:
            # Slot-only configuration owners cannot be weak-referenced. Keeping
            # the uncommon owner alive is the only way to prevent allocator
            # reuse from impersonating it later in this process.
            self.owner_strong = owner

    def owns(self, owner: object) -> bool:
        current = self.owner_ref() if self.owner_ref is not None else self.owner_strong
        return current is owner


_STATE_IDENTITY_OWNERS: dict[int, _StateIdentityOwner] = {}


def _canonical_record_key(entry: dict[str, Any]) -> tuple[str, str, str, str]:
    """Stable, total sort key for a manifest record, independent of dict order."""
    return (
        str(entry.get("kind", "")),
        str(entry.get("identity", "")),
        str(entry.get("args_digest", "")),
        str(entry.get("label", "")),
    )


def _canonical_dep_key(dep: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    """Stable, total sort key for a manifest dependency entry."""
    return (
        str(dep.get("kind", "")),
        str(dep.get("key", "")),
        str(dep.get("policy_digest", "")),
        str(dep.get("identity", "")),
        str(dep.get("args_digest", "")),
        str(dep.get("label", "")),
    )


@dataclass(frozen=True)
class NodeKey:
    kind: str
    identity: str
    args_digest: str
    label: str = field(compare=False)


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
    checkpoint_loaded: bool = False
    failure: str | None = None
    # The exception the failing load raised, kept only so the reads that follow
    # it *within the same request* re-raise it instead of re-running the load.
    # `failure_traceback` is the chain captured at that raise, restored on every
    # re-raise so the object's traceback stays bounded and points at the load.
    # A traceback pins its frames and every local in them, so both are dropped
    # when the request that produced them ends: nothing outside that request may
    # re-raise them, and a permanently failing node must not pin a load frame
    # (and whatever it allocated) until the next successful load.
    failure_exc: BaseException | None = None
    failure_traceback: TracebackType | None = None
    # True once an observation of this node raised without being recorded (an
    # unprobeable failure, or a freeze that failed after the load). The stored
    # probe then describes a world that has since been contradicted, so it may
    # no longer prove "unchanged" -- see `_refresh_resource`.
    probe_unconfirmed: bool = False
    # False once this record's value derived from an exception the graph could
    # not describe, which makes it reproducible only by re-running it. Such a
    # record is omitted from checkpoints exactly as a failure record is.
    checkpointable: bool = True

    @property
    def is_untracked(self) -> bool:
        return bool(self.untracked_reasons)

    @property
    def is_failed(self) -> bool:
        return self.failure is not None


@dataclass
class _RefreshOutcome:
    """Whether a raising resource refresh left the record describing that attempt.

    ``_maybe_changed_after`` may only let a record's ``changed_at`` decide when
    the refresh it just ran actually (re)wrote that record. A refresh that raises
    without recording anything leaves whatever the record said before, and a
    stale "unchanged" there is a from-scratch consistency violation.
    """

    failure_recorded: bool = False


@dataclass(frozen=True)
class _ResourceRegistration:
    """A resource object and the owned snapshot of the parameter that keyed it."""

    resource: Any
    parameter_snapshot: Snapshot
    parameter_type_digest: str


@dataclass
class _ResourceHookContext:
    """One active Resource hook and any database-read violation it caught."""

    database: Database
    resource_type: str
    hook_name: str
    violation: ResourceDependencyError | QueryConcurrencyError | None = None


@dataclass
class ExecutionFrame:
    key: NodeKey
    dependencies: set[NodeKey] = field(default_factory=set)
    boundary_fingerprints: list[str] = field(default_factory=list)
    boundary_values: list[Any] = field(default_factory=list)
    untracked_reasons: list[str] = field(default_factory=list)
    checkpointable: bool = True
    concurrency_violation: QueryConcurrencyError | None = None


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
    min_ns: int
    max_ns: int
    last_ns: int


@dataclass
class _TimingAggregate:
    count: int = 0
    total_ns: int = 0
    min_ns: int = 0
    max_ns: int = 0
    last_ns: int = 0

    def add(self, elapsed_ns: int) -> None:
        self.count += 1
        self.total_ns += elapsed_ns
        self.last_ns = elapsed_ns
        if self.count == 1:
            self.min_ns = elapsed_ns
            self.max_ns = elapsed_ns
        else:
            self.min_ns = min(self.min_ns, elapsed_ns)
            self.max_ns = max(self.max_ns, elapsed_ns)


@dataclass(frozen=True)
class QueryChangeEvent:
    """Delivered to observers when a subscribed query's result changes.

    Fires only when a query with a prior stored value recomputes to a value
    that is different under its equality policy. Cold execution, unchanged
    untracked re-execution, `"reused"`, and `"backdated"` do not fire.
    """

    query_id: str
    args_digest: str
    decision: str
    changed_at: int
    verified_at: int


ObserverCallback = Callable[[QueryChangeEvent], None]
ObserverErrorHook = Callable[[Exception], None]


@dataclass(frozen=True, eq=False)
class _ObserverRegistration:
    """One subscription, kept distinct even when callbacks compare equal."""

    callback: ObserverCallback


@dataclass(frozen=True)
class _PendingObserverEvent:
    """An event and the subscriptions that existed when it occurred."""

    event: QueryChangeEvent
    callbacks: tuple[ObserverCallback, ...]


def _default_observer_error_hook(exc: Exception) -> None:
    sys.stderr.write(f"pyinc: observer callback raised {type(exc).__qualname__}: {exc}\n")


_ACTIVE_GUARDS: ContextVar[tuple[Database, ...]] = ContextVar("pyinc_active_guards", default=())
_ACTIVE_QUERY_EXECUTIONS: ContextVar[tuple[Database, ...]] = ContextVar(
    "pyinc_active_query_executions", default=()
)
_ACTIVE_RESOURCE_HOOKS: ContextVar[tuple[_ResourceHookContext, ...]] = ContextVar(
    "pyinc_active_resource_hooks", default=()
)
_GUARD_INSTALLED = False
_GUARD_INSTALL_LOCK = threading.Lock()
_EXTERNAL_PROCESS_LAUNCH_DEPTH: ContextVar[int] = ContextVar(
    "pyinc_external_process_launch_depth", default=0
)


def _raise_if_guarded(message: str) -> None:
    """Raise `UntrackedReadError` if any active Database has a running query without raw-read permission."""
    for db in _ACTIVE_GUARDS.get():
        if db._current_frame() is not None and not db._allow_raw_reads.get():
            raise UntrackedReadError(message)


def _reject_query_context(operation: str) -> None:
    """Reject database administration while any query owns this context."""
    if not _ACTIVE_QUERY_EXECUTIONS.get():
        return
    raise QueryContextError(
        f"{operation} cannot be used while a query is executing. Queries may only "
        "compose queries, read Input or Resource values, and call "
        "Database.report_untracked_read()."
    )


def _reject_layer3_query_context(operation: str) -> None:
    """Reject a decoded integration entrypoint from live query execution."""
    if not _ACTIVE_QUERY_EXECUTIONS.get():
        return
    raise QueryContextError(
        f"{operation} is a Layer-3 integration entrypoint and cannot be used while "
        "a query is executing. Query authors must compose stable Layer-2 @query "
        "payload APIs from the entrypoint's integration module instead."
    )


def _reject_concurrent_launch(
    operation: str,
    *,
    allow_in_resource_hooks: bool = False,
) -> None:
    """Reject an enumerated worker/process launch from live query execution.

    Resource hooks may invoke the explicitly external-command APIs (for
    example ``subprocess.Popen``) as tracked I/O. Worker launches and process
    forks remain forbidden there because they can escape the current context
    or inherit a live ``Database`` and its lock.
    """
    resource_contexts = _ACTIVE_RESOURCE_HOOKS.get()
    if allow_in_resource_hooks and resource_contexts:
        return

    active_databases = _ACTIVE_QUERY_EXECUTIONS.get()
    if not active_databases and not resource_contexts:
        return

    existing: QueryConcurrencyError | None = None
    for database in active_databases:
        for frame in database._execution_stack.get():
            if frame.concurrency_violation is not None:
                existing = frame.concurrency_violation
                break
        if existing is not None:
            break
    if existing is None:
        for context in resource_contexts:
            if isinstance(context.violation, QueryConcurrencyError):
                existing = context.violation
                break

    if existing is None:
        if resource_contexts:
            message = (
                f"{operation} cannot be used from a Resource hook. Resource hooks "
                "may perform direct external I/O but cannot launch threads, workers, "
                "or processes that escape the current execution context."
            )
        else:
            message = (
                f"{operation} cannot be used while a query is executing. Queries "
                "must execute synchronously; launch concurrent work outside db.get()."
            )
        existing = QueryConcurrencyError(message)

    reason = f"forbidden concurrent launch: {operation}"
    seen_databases: set[int] = set()
    for database in active_databases:
        if id(database) in seen_databases:
            continue
        seen_databases.add(id(database))
        for frame in database._execution_stack.get():
            if frame.concurrency_violation is None:
                frame.concurrency_violation = existing
            if reason not in frame.untracked_reasons:
                frame.untracked_reasons.append(reason)
            frame.checkpointable = False
    for context in resource_contexts:
        if context.violation is None:
            context.violation = existing
    raise existing


def _raise_sticky_concurrency_violation(frame: ExecutionFrame) -> None:
    if frame.concurrency_violation is not None:
        raise frame.concurrency_violation


def _reject_cross_database_query_read(database: Database, operation: str) -> None:
    """Require query composition and managed reads to stay on their owning database."""
    active = _ACTIVE_QUERY_EXECUTIONS.get()
    if not active or active[-1] is database:
        return
    raise QueryContextError(
        f"{operation} cannot use a different Database while a query is executing. "
        "Query composition and Input or Resource reads must use the Database "
        "passed to the active query."
    )


def _reject_database_construction() -> None:
    """Apply hook-specific then query-specific precedence to Database construction."""
    hook_contexts = _ACTIVE_RESOURCE_HOOKS.get()
    if hook_contexts:
        hook_contexts[-1].database._reject_resource_hook_read("Database()")
    _reject_query_context("Database()")


def _install_guards_once() -> None:
    """Install raw-I/O and concurrent-launch guards once per process.

    The wrappers and audit hook consult per-context stacks to determine whether
    a ``Database`` query or Resource hook is active. Installation is idempotent
    and thread-safe; once installed, the guards stay in place for the life of
    the process.
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

        def guarded_launch(
            original: Callable[..., Any],
            operation: str,
            *,
            allow_in_resource_hooks: bool = False,
            external_process_scope: bool = False,
            allow_external_process_internal: bool = False,
        ) -> Callable[..., Any]:
            @functools.wraps(original)
            def guarded(*args: Any, **kwargs: Any) -> Any:
                if allow_external_process_internal and _EXTERNAL_PROCESS_LAUNCH_DEPTH.get() > 0:
                    return original(*args, **kwargs)
                _reject_concurrent_launch(
                    operation,
                    allow_in_resource_hooks=allow_in_resource_hooks,
                )
                if not external_process_scope:
                    return original(*args, **kwargs)
                depth = _EXTERNAL_PROCESS_LAUNCH_DEPTH.get()
                token = _EXTERNAL_PROCESS_LAUNCH_DEPTH.set(depth + 1)
                try:
                    return original(*args, **kwargs)
                finally:
                    _EXTERNAL_PROCESS_LAUNCH_DEPTH.reset(token)

            return guarded

        def guarded_open(*args: Any, **kwargs: Any) -> Any:
            _raise_if_guarded("Raw open() inside a query is untracked. Use FileResource.read().")
            return original_builtins_open(*args, **kwargs)

        def guarded_io_open(*args: Any, **kwargs: Any) -> Any:
            _raise_if_guarded("Raw open() inside a query is untracked. Use FileResource.read().")
            return original_io_open(*args, **kwargs)

        def guarded_getenv(key: str, default: str | None = None) -> str | None:
            _raise_if_guarded(
                "Raw os.getenv() inside a query is untracked. Use EnvResource.read()."
            )
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

        threading.Thread.start = guarded_launch(  # type: ignore[method-assign]
            threading.Thread.start,
            "threading.Thread.start()",
        )
        for alias_name in ("start_new_thread", "start_new", "start_joinable_thread"):
            original_alias = getattr(_thread, alias_name, None)
            if callable(original_alias):
                setattr(
                    _thread,
                    alias_name,
                    guarded_launch(original_alias, f"_thread.{alias_name}()"),
                )
        for alias_name in ("_start_new_thread", "_start_joinable_thread"):
            original_alias = getattr(threading, alias_name, None)
            if callable(original_alias):
                setattr(
                    threading,
                    alias_name,
                    guarded_launch(original_alias, f"threading.{alias_name}()"),
                )

        concurrent.futures.ThreadPoolExecutor.submit = guarded_launch(  # type: ignore[method-assign]
            concurrent.futures.ThreadPoolExecutor.submit,
            "ThreadPoolExecutor.submit()",
        )
        concurrent.futures.ProcessPoolExecutor.submit = guarded_launch(  # type: ignore[method-assign]
            concurrent.futures.ProcessPoolExecutor.submit,
            "ProcessPoolExecutor.submit()",
        )
        concurrent.futures.thread._WorkItem.__init__ = guarded_launch(  # type: ignore[method-assign]
            concurrent.futures.thread._WorkItem.__init__,
            "ThreadPoolExecutor.submit()",
        )
        concurrent.futures.process._WorkItem.__init__ = guarded_launch(  # type: ignore[method-assign]
            concurrent.futures.process._WorkItem.__init__,
            "ProcessPoolExecutor.submit()",
        )
        multiprocessing.process.BaseProcess.start = guarded_launch(  # type: ignore[method-assign]
            multiprocessing.process.BaseProcess.start,
            "multiprocessing.Process.start()",
        )
        multiprocessing.pool.Pool.__init__ = guarded_launch(  # type: ignore[method-assign]
            multiprocessing.pool.Pool.__init__,
            "multiprocessing.Pool()",
        )
        for method_name in (
            "apply",
            "apply_async",
            "map",
            "map_async",
            "starmap",
            "starmap_async",
            "imap",
            "imap_unordered",
        ):
            original_method = getattr(multiprocessing.pool.Pool, method_name)
            setattr(
                multiprocessing.pool.Pool,
                method_name,
                guarded_launch(original_method, f"multiprocessing.Pool.{method_name}()"),
            )
        for result_type_name in ("ApplyResult", "MapResult", "IMapIterator"):
            result_type = getattr(multiprocessing.pool, result_type_name)
            result_type.__init__ = guarded_launch(
                result_type.__init__,
                "multiprocessing.Pool submission",
            )

        subprocess.Popen.__init__ = guarded_launch(  # type: ignore[method-assign]
            subprocess.Popen.__init__,
            "subprocess.Popen()",
            allow_in_resource_hooks=True,
            external_process_scope=True,
        )

        for function_name in ("fork", "forkpty"):
            original_function = getattr(os, function_name, None)
            if callable(original_function):
                setattr(
                    os,
                    function_name,
                    guarded_launch(
                        original_function,
                        f"os.{function_name}()",
                        allow_external_process_internal=True,
                    ),
                )
        for function_name in (
            "execl",
            "execle",
            "execlp",
            "execlpe",
            "execv",
            "execve",
            "execvp",
            "execvpe",
        ):
            original_function = getattr(os, function_name, None)
            if callable(original_function):
                setattr(
                    os,
                    function_name,
                    guarded_launch(
                        original_function,
                        f"os.{function_name}()",
                        allow_external_process_internal=True,
                    ),
                )
        for function_name in (
            "posix_spawn",
            "posix_spawnp",
            "spawnl",
            "spawnle",
            "spawnlp",
            "spawnlpe",
            "spawnv",
            "spawnve",
            "spawnvp",
            "spawnvpe",
            "system",
            "popen",
            "startfile",
        ):
            original_function = getattr(os, function_name, None)
            if callable(original_function):
                setattr(
                    os,
                    function_name,
                    guarded_launch(
                        original_function,
                        f"os.{function_name}()",
                        allow_in_resource_hooks=True,
                        external_process_scope=function_name.startswith("spawn"),
                    ),
                )

        concurrency_audit_events: dict[str, tuple[str, bool, bool]] = {
            "_thread.start_joinable_thread": (
                "_thread.start_joinable_thread()",
                False,
                False,
            ),
            "_thread.start_new_thread": ("_thread.start_new_thread()", False, False),
            "os.exec": ("os.exec*()", False, True),
            "os.fork": ("os.fork()", False, True),
            "os.forkpty": ("os.forkpty()", False, True),
            "os.posix_spawn": ("os.posix_spawn()", True, False),
            "os.system": ("os.system()", True, False),
            "subprocess.Popen": ("subprocess.Popen()", True, False),
        }

        def concurrency_audit_hook(event: str, args: tuple[Any, ...]) -> None:
            del args
            policy = concurrency_audit_events.get(event)
            if policy is None:
                return
            operation, allow_in_resource_hooks, allow_external_internal = policy
            if allow_external_internal and _EXTERNAL_PROCESS_LAUNCH_DEPTH.get() > 0:
                return
            _reject_concurrent_launch(
                operation,
                allow_in_resource_hooks=allow_in_resource_hooks,
            )

        sys.addaudithook(concurrency_audit_hook)
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

    # The PEP 584 operators mirror `os._Environ` so `os.environ | {...}` keeps
    # working after the guard is installed. Both `|` directions build their dict
    # from `self`, so the reads go through the guarded `keys`/`__getitem__`;
    # `|=` only writes, matching the unguarded `__setitem__`.
    def __or__(self, other: object) -> dict[str, str]:
        if not isinstance(other, Mapping):
            return NotImplemented
        new = dict(self)
        new.update(other)
        return new

    def __ror__(self, other: object) -> dict[str, str]:
        if not isinstance(other, Mapping):
            return NotImplemented
        new = dict(other)
        new.update(self)
        return new

    def __ior__(  # type: ignore[misc]  # `|=` accepts pair iterables that `|` does not, as in os._Environ
        self, other: Mapping[str, str] | Iterable[tuple[str, str]]
    ) -> _GuardedEnviron:
        self.update(other)
        return self

    def __getattr__(self, name: str) -> Any:
        # `os._Environ` carries codec helpers beyond the mapping protocol
        # (encodekey/decodekey/encodevalue/decodevalue); expose exactly those.
        # Everything else stays hidden: `os._Environ` internals such as `_data`
        # hold the live environment as a plain attribute, so delegating unknown
        # names would hand queries an unchecked read path around the guard.
        if name in ("encodekey", "decodekey", "encodevalue", "decodevalue"):
            return getattr(self._wrapped, name)
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")


class Subscription:
    """Handle returned by `Database.observe(...)`.

    Calling `unsubscribe()` detaches this exact registration from future
    value changes; callback equality is not consulted. An event that already
    captured the registration is still delivered. Repeated unsubscribes are
    no-ops. Subscriptions do not keep the observed node alive under LRU
    eviction, and recreating an evicted node is a cold execution rather than
    a value-change event.
    """

    __slots__ = ("_database", "_key", "_registration", "_active")

    def __init__(
        self,
        database: Database,
        key: NodeKey,
        registration: _ObserverRegistration,
    ) -> None:
        self._database = database
        self._key = key
        self._registration = registration
        self._active = True

    def unsubscribe(self) -> None:
        self._database._reject_resource_hook_read("Subscription.unsubscribe()")
        _reject_query_context("Subscription.unsubscribe()")
        with self._database._state_lock:
            if not self._active:
                return
            self._active = False
            self._database._unregister_observer(self._key, self._registration)


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
        _reject_database_construction()
        if type(mode) is not str or mode not in {"strict", "checked", "fast"}:
            raise ValueError("mode must be one of: strict, checked, fast")
        if max_query_nodes is not None and (
            type(max_query_nodes) is not int or max_query_nodes <= 0
        ):
            raise ValueError("max_query_nodes must be a positive integer or None.")
        self._mode = mode
        self._max_query_nodes = max_query_nodes
        self._adapters = dict(adapters or {})
        # Per-adapter-key digests read from a loaded checkpoint's manifest; the
        # warm gate compares these against the live, lifetime-pinned registry.
        self._checkpoint_adapter_digests: dict[str, str] = {}
        self._store = store
        self._revision = 0
        self._records: dict[NodeKey, NodeRecord] = {}
        self._input_records: dict[Any, NodeKey] = {}
        self._inputs_by_key: dict[str, Any] = {}
        self._query_records: set[NodeKey] = set()
        self._query_last_used: dict[NodeKey, int] = {}
        self._query_touch_counter = 0
        self._execution_stack: ContextVar[tuple[ExecutionFrame, ...]] = ContextVar(
            "pyinc_execution_stack",
            default=(),
        )
        self._allow_raw_reads: ContextVar[bool] = ContextVar("pyinc_allow_raw_reads", default=False)
        self._request_token: ContextVar[int | None] = ContextVar(
            "pyinc_request_token", default=None
        )
        self._request_query_fingerprints: ContextVar[dict[int, tuple[Any, str]] | None] = (
            ContextVar("pyinc_request_query_fingerprints", default=None)
        )
        self._span_active: ContextVar[bool] = ContextVar("pyinc_span_active", default=False)
        self._span_epoch_seen: ContextVar[int] = ContextVar("pyinc_span_epoch_seen", default=0)
        self._policy_fingerprint_stack: ContextVar[tuple[int, ...]] = ContextVar(
            "pyinc_policy_fingerprint_stack", default=()
        )
        self._resource_fingerprint_stack: ContextVar[tuple[int, ...]] = ContextVar(
            "pyinc_resource_fingerprint_stack", default=()
        )
        self._type_fingerprint_stack: ContextVar[tuple[int, ...]] = ContextVar(
            "pyinc_type_fingerprint_stack", default=()
        )
        self._module_capture_stack: ContextVar[tuple[int, ...]] = ContextVar(
            "pyinc_module_capture_stack", default=()
        )
        self._request_counter = 0
        # Calls whose frozen arguments are not substitutive (notably NaN) must
        # never share a node merely because their canonical bytes match.  The
        # counter gives each such live call/resource registration an ephemeral
        # identity; these nodes are excluded from checkpoints below.
        self._non_substitutive_identity_counter = 0
        self._span_epoch = 0
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
        self._query_timings: dict[NodeKey, _TimingAggregate] = {}
        self._state_lock = threading.RLock()
        self._query_registry: dict[str, Any] = {}
        self._resource_registry: dict[NodeKey, _ResourceRegistration] = {}
        self._call_snapshot_registry: dict[NodeKey, Any] = {}
        self._observers: dict[NodeKey, list[_ObserverRegistration]] = {}
        self._observer_error_hook: ObserverErrorHook = (
            observer_error_hook if observer_error_hook is not None else _default_observer_error_hook
        )
        self._pending_events: ContextVar[list[_PendingObserverEvent] | None] = ContextVar(
            "pyinc_pending_events", default=None
        )
        # Resource nodes whose failure record holds this request's exception, so
        # the request scope can drop it (and the frames it pins) on the way out.
        self._request_failures: ContextVar[list[NodeKey] | None] = ContextVar(
            "pyinc_request_failures", default=None
        )
        # Scope-B: checkpoint records loaded from a durable store for cross-run reuse.
        self._checkpoint_query_records: dict[NodeKey, dict[str, Any]] = {}
        self._checkpoint_resource_probes: dict[NodeKey, tuple[Any, str]] = {}
        self._checkpoint_load_store: ArtifactStore | None = None
        self._checkpoint_snapshot_cache: dict[str, Snapshot] = {}
        # The transitive pinned-query set of the record currently being warmed.
        # Set at the warm root and consulted while warming its dependency queries
        # so an unpinned (non-code-pinnable) dep query is never served stale.
        self._checkpoint_root_pinned: builtins.set[str] | None = None
        # Companion object maps for the record currently being warmed, keyed by
        # the same identity strings the sets carry: query_id -> Query object (for
        # execute-to-verify) and resource identity -> resource object (for
        # probe-hint restoration). Set at the warm root, consulted transitively.
        self._checkpoint_root_pinned_query_objects: dict[str, Any] | None = None
        self._checkpoint_root_pinned_resources: dict[str, Any] | None = None
        _install_guards_once()
        # An adapter is part of this Database's value boundary, not mutable
        # request configuration.  Pin the complete implementation/state digest
        # once and reject a later change before it can make a warm value differ
        # from the same operation in a newly constructed Database.
        self._adapter_lifetime_digests = self._current_adapter_digests()

    @property
    def mode(self) -> Mode:
        self._reject_resource_hook_read("Database.mode")
        _reject_query_context("Database.mode")
        return self._mode

    @property
    def max_query_nodes(self) -> int | None:
        self._reject_resource_hook_read("Database.max_query_nodes")
        _reject_query_context("Database.max_query_nodes")
        return self._max_query_nodes

    @property
    def revision(self) -> int:
        self._reject_resource_hook_read("Database.revision")
        _reject_query_context("Database.revision")
        with self._state_lock:
            return self._revision

    def statistics(self) -> DatabaseStatistics:
        self._reject_resource_hook_read("Database.statistics()")
        _reject_query_context("Database.statistics()")
        with self._state_lock:
            resource_count = sum(1 for k in self._records if k.kind == "resource")
            return DatabaseStatistics(
                node_count=len(self._records),
                input_count=len(self._inputs_by_key),
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
        self._reject_resource_hook_read("Database.reset_statistics()")
        _reject_query_context("Database.reset_statistics()")
        with self._state_lock:
            for key in self._stats:
                self._stats[key] = 0
            self._query_timings.clear()

    def query_profile(self) -> tuple[QueryProfile, ...]:
        self._reject_resource_hook_read("Database.query_profile()")
        _reject_query_context("Database.query_profile()")
        with self._state_lock:
            profiles: list[QueryProfile] = []
            for key, timing in sorted(self._query_timings.items(), key=lambda item: item[0].label):
                profiles.append(
                    QueryProfile(
                        query_label=key.label,
                        execution_count=timing.count,
                        total_ns=timing.total_ns,
                        mean_ns=timing.total_ns // timing.count,
                        min_ns=timing.min_ns,
                        max_ns=timing.max_ns,
                        last_ns=timing.last_ns,
                    )
                )
            return tuple(profiles)

    def dependency_graph(self) -> tuple[DependencyGraphNode, ...]:
        self._reject_resource_hook_read("Database.dependency_graph()")
        _reject_query_context("Database.dependency_graph()")
        with self._state_lock:
            nodes: list[DependencyGraphNode] = []
            for key, record in self._records.items():
                dep_labels = tuple(
                    sorted(
                        self._records[dep].label
                        for dep in record.dependencies
                        if dep in self._records
                    )
                )
                nodes.append(
                    DependencyGraphNode(
                        label=record.label,
                        kind=key.kind,
                        changed_at=record.changed_at,
                        verified_at=record.verified_at,
                        last_decision=record.last_decision,
                        is_untracked=record.is_untracked,
                        dependency_labels=dep_labels,
                    )
                )
            return tuple(sorted(nodes, key=lambda n: n.label))

    def set(self, input_key: Any, value: Any) -> None:
        self._reject_resource_hook_read("Database.set()")
        _reject_query_context("Database.set()")
        from .core import Input

        if type(input_key) is not Input:
            raise TypeError("db.set() expects an Input instance.")
        with self._state_lock:
            # Registration is part of the commit. A value that cannot cross the
            # membrane, or a comparator that raises, must not leave a phantom
            # Input definition behind under this public key.
            self._validate_input_registration(input_key)
            node_key = self._prospective_input_key(input_key)
            snapshot = self._freeze_owned_snapshot(value)
            digest = fingerprint_snapshot(snapshot)
            record = self._records.get(node_key)
            equal = record is not None and self._compare_input_snapshots(
                input_key, record.snapshot, snapshot
            )
            self._commit_input_registration(input_key)
            if equal:
                assert record is not None
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
            # A set is a declared change: inside a span the request must move
            # so later gets re-derive from the new input instead of reusing
            # answers the span settled before it.
            self._roll_span_request()

    def set_many(self, updates: Iterable[tuple[Any, Any]]) -> None:
        self._reject_resource_hook_read("Database.set_many()")
        _reject_query_context("Database.set_many()")
        from .core import Input

        with self._state_lock:
            # Materialization is part of the transaction boundary: an iterator
            # that fails halfway through cannot leave registrations or counters
            # behind.
            materialized = list(updates)
            raw_pairs: list[tuple[Any, Any]] = []
            seen_keys: set[str] = set()
            for item in materialized:
                try:
                    input_key, value = item
                except (TypeError, ValueError) as exc:
                    raise TypeError(
                        "db.set_many() expects an iterable of (Input, value) pairs."
                    ) from exc
                if type(input_key) is not Input:
                    raise TypeError("db.set_many() expects (Input, value) pairs.")
                if input_key.key in seen_keys:
                    raise InputKeyError(
                        f"db.set_many() received duplicate input key {input_key.key!r}."
                    )
                seen_keys.add(input_key.key)
                self._validate_input_registration(input_key)
                raw_pairs.append((input_key, value))

            # Freeze every value before running any user comparator. Neither
            # phase mutates database records, revisions, or statistics.
            pending: list[tuple[Any, NodeKey, Any, str]] = []
            for input_key, value in raw_pairs:
                snapshot = self._freeze_owned_snapshot(value)
                digest = fingerprint_snapshot(snapshot)
                node_key = self._prospective_input_key(input_key)
                pending.append((input_key, node_key, snapshot, digest))

            decisions: list[tuple[bool, Any, NodeKey, Any, str]] = []
            request_id = self._current_request_id()
            for input_key, node_key, snapshot, digest in pending:
                record = self._records.get(node_key)
                equal = record is not None and self._compare_input_snapshots(
                    input_key, record.snapshot, snapshot
                )
                decisions.append((equal, input_key, node_key, snapshot, digest))

            # Commit registrations only after every freeze and comparator has
            # succeeded. Input mutation deliberately does not write the
            # external ArtifactStore: checkpoints persist the input frontier
            # transactionally before publishing their manifest. Keeping store
            # I/O out of this boundary makes set_many all-or-nothing even when
            # a store cannot provide a multi-object transaction.
            for input_key, _value in raw_pairs:
                self._commit_input_registration(input_key)

            changed = [decision for decision in decisions if not decision[0]]
            equal_count = len(decisions) - len(changed)
            for equal, _input_key, node_key, snapshot, digest in decisions:
                if equal:
                    record = self._records[node_key]
                    record.snapshot = snapshot
                    record.digest = digest
                    record.verified_at = self._revision
                    record.last_decision = "reused"
                    record.reason = "equal input update ignored"
                    record.checked_in_request = request_id

            self._stats["input_equal_ignores"] += equal_count

            if not changed:
                return

            # Phase 3: single revision bump, apply all changed inputs.
            self._revision += 1
            changed_at = self._revision
            for _equal, _input_key, node_key, snapshot, digest in changed:
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
            # A set is a declared change: inside a span the request must move
            # so later gets re-derive from the new inputs instead of reusing
            # answers the span settled before them.
            self._roll_span_request()

    def get(self, query: _core.Query[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        self._reject_resource_hook_read("Database.get()")
        _reject_cross_database_query_read(self, "Database.get()")
        from .core import Query

        if type(query) is not Query:
            raise TypeError("db.get() expects a @query-decorated callable.")
        with self._state_lock, self._request_scope() as pending:
            key: NodeKey | None = None
            had_record = False
            try:
                key, call_snapshot = self._query_key(query, args, kwargs)
                had_record = key in self._records
                if not had_record and self._checkpoint_query_records:
                    self._try_warm_from_checkpoint(query, key, call_snapshot)
                self._ensure_query(query, key, call_snapshot)
            except BaseException as error:
                self._mark_caught_query_failure(query, key, error)
                if key is not None and not had_record:
                    self._discard_uncommitted_query(key)
                raise
            assert key is not None
            self._record_dependency(key)
            result = cast(T, self._expose_boundary_snapshot(self._records[key].snapshot))
        self._dispatch_events(pending)
        return result

    def explain(self, query: _core.Query[P, Any], *args: P.args, **kwargs: P.kwargs) -> str:
        self._reject_resource_hook_read("Database.explain()")
        _reject_query_context("Database.explain()")
        from .core import Query

        if type(query) is not Query:
            raise TypeError("db.explain() expects a @query-decorated callable.")
        return format_explanation(self.inspect(query, *args, **kwargs))

    def inspect(
        self, query: _core.Query[P, Any], *args: P.args, **kwargs: P.kwargs
    ) -> InspectionNode:
        self._reject_resource_hook_read("Database.inspect()")
        _reject_query_context("Database.inspect()")
        from .core import Query

        if type(query) is not Query:
            raise TypeError("db.inspect() expects a @query-decorated callable.")
        with self._state_lock, self._request_scope() as pending:
            key, call_snapshot = self._query_key(query, args, kwargs)
            had_record = key in self._records
            try:
                if not had_record:
                    self._ensure_query(query, key, call_snapshot)
            except Exception:
                if not had_record:
                    self._discard_uncommitted_query(key)
                raise
            node = self._inspect_record(key)
        self._dispatch_events(pending)
        return node

    def inspect_fresh(
        self, query: _core.Query[P, Any], *args: P.args, **kwargs: P.kwargs
    ) -> InspectionNode:
        self._reject_resource_hook_read("Database.inspect_fresh()")
        _reject_query_context("Database.inspect_fresh()")
        from .core import Query

        if type(query) is not Query:
            raise TypeError("db.inspect_fresh() expects a @query-decorated callable.")
        with self._state_lock, self._request_scope() as pending:
            key, call_snapshot = self._query_key(query, args, kwargs)
            had_record = key in self._records
            try:
                self._ensure_query(query, key, call_snapshot)
            except Exception:
                if not had_record:
                    self._discard_uncommitted_query(key)
                raise
            node = self._inspect_record(key)
        self._dispatch_events(pending)
        return node

    @contextmanager
    def request_span(self) -> Iterator[None]:
        """Hold one request open across several top-level calls.

        ``get`` / ``inspect`` / ``inspect_fresh`` / ``read_resource`` calls
        inside the span join a single request instead of opening one each, so
        once-per-request work -- resource validation above all -- happens once
        for the whole batch. Entering the span declares that the world the
        database reads from does not change until it closes; a caller that
        changes it mid-span must say so with :meth:`request_inputs_changed`
        (``set`` and ``set_many`` declare their own changes). The request
        boundary moves with the span: failure exceptions retained for
        re-raising stay live to its end, and observer events are delivered
        when the outermost span closes -- cleanly or on an exception --
        exactly as they are for a single ``get``. Spans are reentrant -- an
        inner span, or one opened inside a ``get``, joins the enclosing
        request and its close does nothing.
        """
        self._reject_resource_hook_read("Database.request_span()")
        _reject_query_context("Database.request_span()")
        scope = self._request_scope()
        with self._state_lock:
            pending = scope.__enter__()
            span_token = self._span_active.set(True) if pending is not None else None
            epoch_token = (
                self._span_epoch_seen.set(self._span_epoch) if pending is not None else None
            )
        body_exc: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            body_exc = exc
            raise
        finally:
            with self._state_lock:
                if span_token is not None:
                    self._span_active.reset(span_token)
                if epoch_token is not None:
                    self._span_epoch_seen.reset(epoch_token)
                scope.__exit__(None, None, None)
            # Deliver outside the lock, exactly as a single get does. Work the
            # span committed keeps its notifications even when a later part of
            # the request fails, so delivery runs on the failure path too --
            # where it must never mask the propagating span-body exception.
            if body_exc is None:
                self._dispatch_events(pending)
            else:
                with suppress(Exception):
                    self._dispatch_events(pending)

    def request_inputs_changed(self) -> None:
        """Declare that the world outside the database changed mid-span.

        The declaration rolls any open :meth:`request_span` -- held by this
        thread or another -- onto a fresh request, so the span's next read of
        each node re-validates instead of answering from its earlier
        observation. Without a span open anywhere it changes nothing: every
        top-level call already opens its own request.

        Callers using `pyinc.integrations`' `request_scope` /
        `once_per_request` should call the integrations-level
        `request_inputs_changed()` instead: it clears that scope's memo and
        forwards here, whereas this method alone leaves the integrations memo
        answering from the old world.
        """
        self._reject_resource_hook_read("Database.request_inputs_changed()")
        _reject_query_context("Database.request_inputs_changed()")
        with self._state_lock:
            self._roll_span_request()

    def _roll_span_request(self) -> None:
        """Move any open span onto a fresh request id after a declared change.

        Callers hold the state lock. The change is declared instance-wide by
        bumping the span epoch: a span held by another thread catches up at
        its next request boundary, where the epoch is compared before any
        dedupe. A span on the calling thread moves immediately. Resetting the
        span's token and seen epoch at exit restores the pre-span values, so
        the intermediate ids need no bookkeeping.
        """
        self._span_epoch += 1
        self._sync_span_to_epoch()

    def _sync_span_to_epoch(self) -> None:
        """Roll this thread's open span forward when the epoch moved past it.

        Callers hold the state lock. Outside a span there is nothing to do:
        each top-level call mints a fresh request id no record can already
        carry. Inside one, a seen epoch behind the instance's means another
        thread committed a change since the span last synced, so the request
        id moves exactly as it does for a same-thread declaration and the
        span's next reads re-validate against the committed state.
        """
        if not self._span_active.get():
            return
        if self._span_epoch_seen.get() == self._span_epoch:
            return
        self._request_counter += 1
        self._request_token.set(self._request_counter)
        self._request_query_fingerprints.set({})
        self._span_epoch_seen.set(self._span_epoch)

    def observe(
        self,
        callback: ObserverCallback,
        query: _core.Query[P, Any],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Subscription:
        """Register `callback` to fire whenever the query node's value changes.

        Observer callbacks fire once per top-level `get` / `inspect` /
        `inspect_fresh` call in which a node with a prior stored value was
        re-executed and produced a policy-distinct value. Cold execution,
        unchanged untracked execution, backdating, and reuse do not fire.

        Callbacks run after the request scope completes and the kernel lock is
        released, so a callback may safely call back into the database. Each
        event captures its recipients when the value changes: a later
        subscriber cannot receive an earlier event, while unsubscribing does
        not retract an event already captured for that subscription.
        Exceptions from a callback are routed to the `observer_error_hook`
        (default: a one-line stderr log) and do not suppress sibling callbacks
        or corrupt kernel state.
        """
        self._reject_resource_hook_read("Database.observe()")
        _reject_query_context("Database.observe()")
        from .core import Query

        if type(query) is not Query:
            raise TypeError("db.observe() expects a @query-decorated callable.")
        if not callable(callback):
            raise TypeError("db.observe() expects a callable as its first argument.")
        with self._state_lock:
            key, _ = self._query_key(query, args, kwargs)
            registration = _ObserverRegistration(callback)
            self._observers.setdefault(key, []).append(registration)
        return Subscription(self, key, registration)

    def report_untracked_read(self, reason: str) -> None:
        self._reject_resource_hook_read("Database.report_untracked_read()")
        _reject_cross_database_query_read(self, "Database.report_untracked_read()")
        with self._state_lock:
            frame = self._current_frame()
            if frame is None:
                raise RuntimeError(
                    "db.report_untracked_read() must be called while a query is executing."
                )
            frame.untracked_reasons.append(reason)

    # ------------------------------------------------------------------
    # Scope-B: durable checkpoint save / load
    # ------------------------------------------------------------------

    def save_checkpoint(self, store: ArtifactStore | None = None) -> str:
        """Serialize all current node records to the ArtifactStore.

        Returns a checkpoint key that can be passed to :meth:`load_checkpoint`
        in a future process.  All snapshot values are also written under their
        ``fingerprint_snapshot`` digests so the store is self-contained.

        The returned key is content-addressed: the same database state always
        produces the same key.  Each subsequent call after mutations produces a
        fresh key.

        Inputs must be set before saving so that input digests are captured in
        the checkpoint's dependency records.

        Raises ``ValueError`` if no ``ArtifactStore`` is available (either
        passed directly or configured via ``Database(store=...)``), or if the
        store reports bytes already bound to a required content address do not
        match the checkpoint object being saved.
        """
        self._reject_resource_hook_read("Database.save_checkpoint()")
        _reject_query_context("Database.save_checkpoint()")
        _store = store if store is not None else self._store
        if _store is None:
            raise ValueError(
                "save_checkpoint() requires an ArtifactStore. "
                "Pass store= or construct Database(store=...) first."
            )
        with self._state_lock:
            return self._save_checkpoint_locked(_store)

    def load_checkpoint(self, key: str, store: ArtifactStore | None = None) -> None:
        """Load previously saved node records from the ArtifactStore.

        After loading, calls to :meth:`get` will verify dependencies and reuse
        cached results without re-executing queries whose inputs and resources
        are unchanged.  All ``Input`` values that the checkpoint depends on
        must be set before calling this method.

        Checkpoint records that cannot be verified (missing snapshot bytes,
        changed inputs, no live record for a resource dependency) are silently
        skipped; the affected queries re-execute on the next :meth:`get` call.
        A warmed record joins the loading database's own revision timeline, so
        the usual invalidation machinery governs it from then on.

        Raises ``ValueError`` if no ``ArtifactStore`` is available.
        Raises ``KeyError`` if *key* is not found in the store.
        """
        self._reject_resource_hook_read("Database.load_checkpoint()")
        _reject_query_context("Database.load_checkpoint()")
        _store = store if store is not None else self._store
        if _store is None:
            raise ValueError(
                "load_checkpoint() requires an ArtifactStore. "
                "Pass store= or construct Database(store=...) first."
            )
        with self._state_lock:
            self._load_checkpoint_locked(key, _store)

    def _record_is_stale_for_save(self, record: NodeRecord) -> bool:
        """True if *record*'s cached value is out of date w.r.t. its live deps.

        A checkpoint may only persist records whose snapshot matches what a fresh
        recomputation against the *current* graph would produce. When a dependency
        (typically an ``Input``) is mutated after this record last executed but
        before ``save_checkpoint`` -- a "dirty graph" with no intervening ``get``
        -- the record's snapshot is stale, yet the manifest would bake in the
        dep's *new* digest (``dep_record.digest`` is read live below), yielding a
        record that warms the stale value on reload and violates from-scratch
        consistency. Detect that here with the same timeline rule the warm gate
        uses (`_maybe_changed_after`): any dep that changed after this record was
        last verified -- or that is missing or untracked, and so can never be
        trusted at load -- makes the record unsafe to persist.

        Pure by design: this never executes a query or re-probes a resource, so a
        save never mutates the graph. Only directly-stale records are flagged;
        a record whose stale value is transitively caused by a stale *child* is
        left to the load path, where the omitted child fails re-verification
        (execute-to-verify / warm-dep) and the parent is refused rather than
        warmed stale (see the checkpoint dep-verification path).
        """
        for dep_key in record.dependencies:
            dep_record = self._records.get(dep_key)
            if dep_record is None:
                return True
            if dep_record.is_untracked:
                return True
            if not snapshots_equal(dep_record.snapshot, dep_record.snapshot):
                return True
            if dep_record.changed_at > record.verified_at:
                return True
        return False

    def _checkpoint_key_is_substitutive(self, key: NodeKey) -> bool:
        """Whether a query call/resource parameter can identify a durable node."""

        identity_snapshot: Any
        if key.kind == "query":
            call_snapshots = self._call_snapshots()
            if key in call_snapshots:
                identity_snapshot = call_snapshots[key]
            elif key.args_digest in self._checkpoint_snapshot_cache:
                identity_snapshot = self._checkpoint_snapshot_cache[key.args_digest]
            else:
                return False
        elif key.kind == "resource":
            registration = self._resource_objects().get(key)
            if registration is not None:
                identity_snapshot = registration.parameter_snapshot
            elif key.args_digest in self._checkpoint_snapshot_cache:
                identity_snapshot = self._checkpoint_snapshot_cache[key.args_digest]
            else:
                return False
        else:
            return False
        return snapshots_equal(identity_snapshot, identity_snapshot)

    def _save_checkpoint_locked(self, store: ArtifactStore) -> str:
        self._verify_adapter_lifetime()
        eligible = {
            key
            for key, record in self._records.items()
            if key.kind in ("query", "resource")
            # A failure record has no value to persist, and a reader that handled
            # the failure is only reproducible while the load keeps failing. Both
            # are omitted -- the dep-closure below drops every parent too -- so a
            # checkpoint never warms a result derived from an absent value.
            and not record.is_failed
            # The same exclusion for a failure the graph could not record: the
            # resource record whose probe an unrecorded raise contradicted (it
            # still holds the pre-failure probe and digest, which would verify
            # against a world that healed back into that state), and the reader
            # that consumed such a raise (its value is a handled failure no
            # record describes).
            and record.checkpointable
            and not record.probe_unconfirmed
            # A snapshot that is not equal to itself under the public typed
            # relation (notably NaN-containing values) is not substitutive.
            # Restoring it in another process could change observable behavior
            # such as hashing even when its canonical bytes are identical.
            and snapshots_equal(record.snapshot, record.snapshot)
            # Query arguments and resource parameters participate in the node
            # identity. Equal canonical bytes cannot identify a NaN-containing
            # call across a process boundary, and a non-substitutive probe can
            # never justify a resource hint restore.
            and self._checkpoint_key_is_substitutive(key)
            and (key.kind != "resource" or snapshots_equal(record.probe, record.probe))
            and not self._record_is_stale_for_save(record)
            and (
                key.args_digest in self._checkpoint_snapshot_cache
                or (key.kind == "query" and key in self._call_snapshots())
                or (key.kind == "resource" and key in self._resource_objects())
            )
        }
        # A manifest is closed over its persisted query/resource dependencies.
        # If a stale child is omitted, every parent that references it is omitted
        # too, so schema-v5 manifests never contain dangling dependency records.
        changed = True
        while changed:
            changed = False
            for key in tuple(eligible):
                record = self._records[key]
                if any(
                    dep.kind in ("query", "resource") and dep not in eligible
                    for dep in record.dependencies
                ):
                    eligible.remove(key)
                    changed = True

        records_list: list[dict[str, Any]] = []
        for key, record in self._records.items():
            if key not in eligible:
                continue
            self._require_snapshot_digest(
                record.snapshot,
                record.digest,
                subject=f"{record.label} value",
            )
            self._persist_snapshot_to(record.snapshot, store)
            # Persist what a fresh process needs to re-execute this leaf under its
            # own name, content-addressed by the digest already in the manifest:
            # a query's call snapshot (keyed by its args_digest) so it can be
            # re-run to verify, and a resource's frozen parameter (keyed by its
            # args_digest) so its object can be re-probed live. No manifest field
            # is added -- the digests already live on the record and its deps.
            if key.kind == "query":
                call_snapshot = self._call_snapshots().get(key)
                if call_snapshot is None:
                    call_snapshot = self._checkpoint_snapshot_cache.get(key.args_digest)
                if call_snapshot is not None:
                    self._require_snapshot_digest(
                        call_snapshot,
                        key.args_digest,
                        subject=f"{record.label} call",
                    )
                    self._persist_snapshot_to(call_snapshot, store)
            elif key.kind == "resource":
                registration = self._resource_objects().get(key)
                if registration is not None:
                    self._require_snapshot_digest(
                        registration.parameter_snapshot,
                        key.args_digest,
                        subject=f"{record.label} parameter",
                    )
                    self._persist_snapshot_to(registration.parameter_snapshot, store)
                else:
                    parameter_snapshot = self._checkpoint_snapshot_cache.get(key.args_digest)
                    if parameter_snapshot is not None:
                        self._require_snapshot_digest(
                            parameter_snapshot,
                            key.args_digest,
                            subject=f"{record.label} checkpoint parameter",
                        )
                        self._persist_snapshot_to(parameter_snapshot, store)
            deps: list[dict[str, Any]] = []
            for dep_key in record.dependencies:
                dep_record = self._records.get(dep_key)
                if dep_record is None:
                    continue
                if dep_key.kind == "input":
                    input_key = self._input_ident_for_key(dep_key)
                    input_obj = self._inputs_by_key[input_key]
                    self._require_snapshot_digest(
                        dep_record.snapshot,
                        dep_record.digest,
                        subject=f"{dep_record.label} value",
                    )
                    self._persist_snapshot_to(dep_record.snapshot, store)
                    deps.append(
                        {
                            "kind": "input",
                            "key": input_key,
                            "policy_digest": self._input_policy_digest(input_obj),
                            "label": dep_key.label,
                            "digest": dep_record.digest,
                        }
                    )
                elif dep_key.kind == "query":
                    deps.append(
                        {
                            "kind": "query",
                            "identity": dep_key.identity,
                            "query_id": self._query_id_for_key(dep_key),
                            "args_digest": dep_key.args_digest,
                            "label": dep_key.label,
                            "digest": dep_record.digest,
                        }
                    )
                elif dep_key.kind == "resource":
                    deps.append(
                        {
                            "kind": "resource",
                            "identity": dep_key.identity,
                            "args_digest": dep_key.args_digest,
                            "label": dep_key.label,
                            "digest": dep_record.digest,
                        }
                    )
            # Canonical, order-independent dep ordering so the manifest bytes (and
            # thus the checkpoint key) do not depend on set/dict iteration order.
            deps.sort(key=_canonical_dep_key)
            entry: dict[str, Any] = {
                "kind": key.kind,
                "identity": key.identity,
                "args_digest": key.args_digest,
                "label": key.label,
                "snapshot_digest": record.digest,
                "deps": deps,
                "is_untracked": record.is_untracked,
                # Adapter keys this record's snapshot uses (sorted for canonical
                # manifest bytes). The warm gate refuses the record unless every
                # one is still present with a matching implementation digest.
                "adapter_keys": sorted(collect_adapter_keys(record.snapshot)),
            }
            if key.kind == "query":
                entry["query_id"] = self._query_id_for_key(key)
            if key.kind == "resource" and record.probe is not None:
                try:
                    probe_snapshot = cast(Snapshot, record.probe)
                    entry["probe_bytes"] = serialize_snapshot(probe_snapshot).hex()
                except (UnsupportedValueError, TypeError):
                    # Probe hint is best-effort: if a resource's probe value
                    # can't be serialised, the checkpoint still records the
                    # snapshot digest and the resource will be re-probed on
                    # load instead of relying on the cached probe match.
                    pass
            records_list.append(entry)

        # Canonical record ordering keeps the manifest bytes independent of the
        # insertion order of self._records.
        records_list.sort(key=_canonical_record_key)
        # Trust anchor for the warm-time adapter gate: the implementation digest
        # each adapter key had when this checkpoint was written. Sorted by key so
        # the manifest bytes stay independent of registry iteration order.
        adapter_digests = self._current_adapter_digests()
        adapters_manifest = {key: adapter_digests[key] for key in sorted(adapter_digests)}
        manifest = {
            "pyinc_ckpt_version": _CHECKPOINT_MANIFEST_VERSION,
            "kernel_fingerprint_version": _KERNEL_FINGERPRINT_VERSION,
            "mode": self._mode,
            "adapters": adapters_manifest,
            "records": records_list,
        }
        manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
        # "ck" prefix ensures the checkpoint key never matches a snapshot digest
        # (snapshot digests are 64 hex chars; this is 66 chars with "ck" prefix).
        checkpoint_key = "ck" + hashlib.sha256(manifest_bytes).hexdigest()
        with self._allow_raw_reads_scope():
            store.put(checkpoint_key, manifest_bytes)
        return checkpoint_key

    def _load_checkpoint_locked(self, key: str, store: ArtifactStore) -> None:
        self._verify_adapter_lifetime()
        if type(key) is not str or not key.startswith("ck") or not self._is_digest(key[2:]):
            raise CheckpointIntegrityError(
                "Checkpoint keys must be 'ck' followed by a lowercase SHA-256 digest."
            )
        with self._allow_raw_reads_scope():
            manifest_bytes = store.get(key)
        if manifest_bytes is None:
            raise KeyError(f"Checkpoint key {key!r} not found in the ArtifactStore.")
        if type(manifest_bytes) is not bytes:
            raise CheckpointIntegrityError(f"Checkpoint {key!r} manifest payload is not bytes.")
        # The manifest is the root of trust: re-derive its content address
        # from the fetched bytes before parsing anything out of them.
        recomputed_key = "ck" + hashlib.sha256(manifest_bytes).hexdigest()
        if recomputed_key != key:
            raise CheckpointIntegrityError(
                f"Checkpoint {key!r} failed integrity verification: stored manifest "
                f"bytes hash to {recomputed_key!r}, not the requested key."
            )

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for field_name, value in pairs:
                if field_name in result:
                    raise ValueError(f"duplicate JSON field {field_name!r}")
                result[field_name] = value
            return result

        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"), object_pairs_hook=unique_object)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            RecursionError,
            OverflowError,
        ) as exc:
            raise CheckpointManifestError(
                f"Checkpoint {key!r} manifest could not be decoded as JSON: {exc}"
            ) from exc

        queries, probes, adapters, snapshots = self._validate_checkpoint_manifest(
            key, manifest, store
        )
        # Commit staged checkpoint state only after the complete manifest and all
        # content-addressed payloads have passed validation.
        self._checkpoint_load_store = store
        self._checkpoint_query_records = queries
        self._checkpoint_resource_probes = probes
        self._checkpoint_adapter_digests = adapters
        self._checkpoint_snapshot_cache = snapshots

    @staticmethod
    def _is_digest(value: Any) -> bool:
        return (
            type(value) is str
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    def _validate_checkpoint_manifest(
        self, key: str, manifest: Any, store: ArtifactStore
    ) -> tuple[
        dict[NodeKey, dict[str, Any]],
        dict[NodeKey, tuple[Any, str]],
        dict[str, str],
        dict[str, Snapshot],
    ]:
        def malformed(message: str) -> CheckpointManifestError:
            return CheckpointManifestError(f"Checkpoint {key!r} {message}")

        if not isinstance(manifest, dict):
            raise malformed("manifest must be a JSON object.")
        if "pyinc_ckpt_version" not in manifest:
            raise malformed("manifest is missing 'pyinc_ckpt_version'.")
        version = manifest["pyinc_ckpt_version"]
        if type(version) is not int or version != _CHECKPOINT_MANIFEST_VERSION:
            raise CheckpointVersionError(
                f"Unsupported checkpoint version {version!r}; expected "
                f"{_CHECKPOINT_MANIFEST_VERSION}."
            )
        required_root = {
            "pyinc_ckpt_version",
            "kernel_fingerprint_version",
            "mode",
            "adapters",
            "records",
        }
        if set(manifest) != required_root:
            raise malformed(f"manifest fields must be exactly {sorted(required_root)!r}.")
        kernel_version = manifest["kernel_fingerprint_version"]
        if type(kernel_version) is not int or kernel_version != _KERNEL_FINGERPRINT_VERSION:
            raise CheckpointVersionError(
                f"Checkpoint {key!r} was written by kernel fingerprint version "
                f"{kernel_version!r}, but this kernel emits version "
                f"{_KERNEL_FINGERPRINT_VERSION}; refusing to load."
            )
        checkpoint_mode = manifest["mode"]
        if type(checkpoint_mode) is not str or checkpoint_mode not in {
            "strict",
            "checked",
            "fast",
        }:
            raise malformed("field 'mode' must be 'strict', 'checked', or 'fast'.")
        if checkpoint_mode != self._mode:
            raise CheckpointModeError(
                f"Checkpoint {key!r} was saved in {checkpoint_mode!r} mode and cannot "
                f"be loaded in {self._mode!r} mode."
            )

        raw_adapters = manifest["adapters"]
        if not isinstance(raw_adapters, dict):
            raise malformed("field 'adapters' must be an object.")
        adapters: dict[str, str] = {}
        for adapter_key, digest in raw_adapters.items():
            if not isinstance(adapter_key, str) or not adapter_key:
                raise malformed("adapter keys must be non-empty strings.")
            if not self._is_digest(digest):
                raise malformed(f"adapter {adapter_key!r} has a malformed digest.")
            adapters[adapter_key] = digest

        raw_records = manifest["records"]
        if not isinstance(raw_records, list):
            raise malformed("field 'records' must be an array.")
        records: dict[NodeKey, dict[str, Any]] = {}
        record_identities: set[tuple[str, str, str]] = set()
        record_labels: dict[tuple[str, str, str], str] = {}
        probe_snapshots: dict[NodeKey, tuple[Any, str]] = {}
        for index, record in enumerate(raw_records):
            if not isinstance(record, dict):
                raise malformed(f"record {index} must be an object.")
            kind = record.get("kind")
            common = {
                "kind",
                "identity",
                "args_digest",
                "label",
                "snapshot_digest",
                "deps",
                "is_untracked",
                "adapter_keys",
            }
            allowed = common | ({"query_id"} if kind == "query" else {"probe_bytes"})
            if (
                kind not in ("query", "resource")
                or set(record) - allowed
                or not common <= set(record)
            ):
                raise malformed(f"record {index} has invalid fields or kind.")
            identity = record["identity"]
            label = record["label"]
            args_digest = record["args_digest"]
            snapshot_digest = record["snapshot_digest"]
            if not isinstance(identity, str) or not identity:
                raise malformed(f"record {index} has an invalid identity.")
            if not isinstance(label, str) or not label:
                raise malformed(f"record {index} has an invalid label.")
            if not self._is_digest(args_digest) or not self._is_digest(snapshot_digest):
                raise malformed(f"record {index} has a malformed content address.")
            if not isinstance(record["is_untracked"], bool):
                raise malformed(f"record {index} field 'is_untracked' must be boolean.")
            adapter_keys = record["adapter_keys"]
            if (
                not isinstance(adapter_keys, list)
                or any(not isinstance(item, str) or not item for item in adapter_keys)
                or len(set(adapter_keys)) != len(adapter_keys)
                or any(item not in adapters for item in adapter_keys)
            ):
                raise malformed(f"record {index} has invalid adapter keys.")
            if kind == "query" and (
                not isinstance(record.get("query_id"), str) or not record["query_id"]
            ):
                raise malformed(f"query record {index} has an invalid query id.")
            identity_prefix, separator, implementation_digest = identity.rpartition(":")
            if (
                not separator
                or not identity_prefix
                or not self._is_digest(implementation_digest)
                or (kind == "query" and identity_prefix != record["query_id"])
            ):
                raise malformed(f"record {index} has an invalid implementation identity.")
            node_key = NodeKey(kind, identity, args_digest, label)
            record_identity = (kind, identity, args_digest)
            if record_identity in record_identities:
                raise malformed(f"contains duplicate record identity {node_key!r}.")
            record_identities.add(record_identity)
            record_labels[record_identity] = label
            self._validate_checkpoint_dependencies(key, index, record["deps"])
            records[node_key] = record

            probe_hex = record.get("probe_bytes")
            if probe_hex is not None:
                if not isinstance(probe_hex, str):
                    raise malformed(f"resource record {index} has invalid probe bytes.")
                try:
                    probe_payload = bytes.fromhex(probe_hex)
                    if probe_payload.hex() != probe_hex:
                        raise ValueError
                    probe_snapshot = deserialize_snapshot(probe_payload)
                    if serialize_snapshot(probe_snapshot) != probe_payload:
                        raise ValueError
                except (UnsupportedValueError, TypeError, ValueError) as exc:
                    raise malformed(f"resource record {index} has invalid probe bytes.") from exc
                probe_snapshots[node_key] = (probe_snapshot, snapshot_digest)

        # Validate dependency references and their recorded target digests only
        # after every record key has been collected.
        invalid: set[NodeKey] = set()
        for node_key, record in records.items():
            for dep in record["deps"]:
                if dep["kind"] == "input":
                    if dep["label"] != f"input[{dep['key']}]":
                        raise malformed(
                            f"record {node_key.label!r} has an invalid input dependency label."
                        )
                    live_key = self._find_input_node_by_key(dep["key"])
                    live_obj = self._inputs_by_key.get(dep["key"])
                    live_record = self._records.get(live_key) if live_key else None
                    if (
                        live_obj is None
                        or live_record is None
                        or self._input_policy_digest(live_obj) != dep["policy_digest"]
                        or live_record.digest != dep["digest"]
                    ):
                        invalid.add(node_key)
                    continue
                target = NodeKey(dep["kind"], dep["identity"], dep["args_digest"], dep["label"])
                target_record = records.get(target)
                if target_record is None:
                    raise malformed(f"record {node_key.label!r} has a dangling dependency.")
                target_identity = (
                    dep["kind"],
                    dep["identity"],
                    dep["args_digest"],
                )
                if dep["label"] != record_labels[target_identity]:
                    raise malformed(
                        f"record {node_key.label!r} has an inconsistent dependency label."
                    )
                if target_record["snapshot_digest"] != dep["digest"]:
                    raise malformed(
                        f"record {node_key.label!r} has an inconsistent dependency digest."
                    )
                if dep["kind"] == "query" and target_record.get("query_id") != dep["query_id"]:
                    raise malformed(
                        f"record {node_key.label!r} has an inconsistent query dependency."
                    )

        remaining_dependencies: dict[NodeKey, set[NodeKey]] = {
            node_key: {
                NodeKey(dep["kind"], dep["identity"], dep["args_digest"], dep["label"])
                for dep in record["deps"]
                if dep["kind"] != "input"
            }
            for node_key, record in records.items()
        }
        reverse_dependencies: dict[NodeKey, set[NodeKey]] = {
            node_key: set() for node_key in records
        }
        for node_key, dependencies in remaining_dependencies.items():
            for dependency in dependencies:
                reverse_dependencies[dependency].add(node_key)
        ready = [
            node_key
            for node_key, dependencies in remaining_dependencies.items()
            if not dependencies
        ]
        visited_count = 0
        while ready:
            dependency = ready.pop()
            visited_count += 1
            for parent in reverse_dependencies[dependency]:
                remaining_dependencies[parent].discard(dependency)
                if not remaining_dependencies[parent]:
                    ready.append(parent)
        if visited_count != len(records):
            raise malformed("manifest dependency graph contains a cycle.")

        snapshots: dict[str, Snapshot] = {}
        for node_key, record in records.items():
            for digest in (record["snapshot_digest"], record["args_digest"]):
                if digest in snapshots:
                    continue
                snapshot = self._read_validated_snapshot(store, digest)
                if snapshot is _MISSING_SNAPSHOT:
                    invalid.add(node_key)
                else:
                    snapshots[digest] = cast(Snapshot, snapshot)

        for node_key, record in records.items():
            result_snapshot = snapshots.get(record["snapshot_digest"])
            if (
                result_snapshot is not None
                and sorted(collect_adapter_keys(result_snapshot)) != record["adapter_keys"]
            ):
                raise malformed(f"record {node_key.label!r} has inconsistent adapter keys.")
            if node_key in invalid or node_key.kind != "query":
                continue
            call_snapshot = snapshots.get(record["args_digest"])
            if call_snapshot is None:
                continue
            if not self._is_query_call_snapshot(call_snapshot):
                raise malformed(f"query record {node_key.label!r} has an invalid call snapshot.")

        # A record whose child payload is unavailable is also unavailable. This
        # closure is computed before any checkpoint state is installed.
        changed = True
        while changed:
            changed = False
            for node_key, record in records.items():
                if node_key in invalid:
                    continue
                for dep in record["deps"]:
                    if dep["kind"] == "input":
                        continue
                    target = NodeKey(dep["kind"], dep["identity"], dep["args_digest"], dep["label"])
                    if target in invalid:
                        invalid.add(node_key)
                        changed = True
                        break

        query_records = {
            node_key: record
            for node_key, record in records.items()
            if node_key.kind == "query" and node_key not in invalid
        }
        resource_probes = {
            node_key: probe
            for node_key, probe in probe_snapshots.items()
            if node_key not in invalid
        }
        valid_digests = {
            digest
            for node_key, record in records.items()
            if node_key not in invalid
            for digest in (record["snapshot_digest"], record["args_digest"])
        }
        return (
            query_records,
            resource_probes,
            adapters,
            {digest: snapshots[digest] for digest in valid_digests},
        )

    def _validate_checkpoint_dependencies(
        self, checkpoint_key: str, record_index: int, deps: Any
    ) -> None:
        if not isinstance(deps, list):
            raise CheckpointManifestError(
                f"Checkpoint {checkpoint_key!r} record {record_index} deps must be an array."
            )
        seen: set[tuple[Any, ...]] = set()
        for dep_index, dep in enumerate(deps):
            if not isinstance(dep, dict):
                raise CheckpointManifestError(
                    f"Checkpoint {checkpoint_key!r} record {record_index} dependency "
                    f"{dep_index} must be an object."
                )
            kind = dep.get("kind")
            identity: tuple[Any, ...]
            if kind == "input":
                required = {"kind", "key", "policy_digest", "label", "digest"}
                valid = (
                    set(dep) == required
                    and isinstance(dep["key"], str)
                    and bool(dep["key"])
                    and isinstance(dep["label"], str)
                    and self._is_digest(dep["digest"])
                    and self._is_digest(dep["policy_digest"])
                )
                identity = (kind, dep.get("key"))
            elif kind in ("query", "resource"):
                required = {"kind", "identity", "args_digest", "label", "digest"}
                if kind == "query":
                    required.add("query_id")
                valid = (
                    set(dep) == required
                    and isinstance(dep["identity"], str)
                    and bool(dep["identity"])
                    and isinstance(dep["label"], str)
                    and self._is_digest(dep["args_digest"])
                    and self._is_digest(dep["digest"])
                    and (
                        kind != "query"
                        or (isinstance(dep["query_id"], str) and bool(dep["query_id"]))
                    )
                )
                identity = (kind, dep.get("identity"), dep.get("args_digest"))
            else:
                valid = False
                identity = (kind,)
            if not valid or identity in seen:
                raise CheckpointManifestError(
                    f"Checkpoint {checkpoint_key!r} record {record_index} has an invalid "
                    f"or duplicate dependency at index {dep_index}."
                )
            seen.add(identity)

    @classmethod
    def _is_query_call_snapshot(cls, snapshot: Any) -> bool:
        try:
            envelope = cls._strict_snapshot_view(snapshot)
        except (IndexError, TypeError, ValueError):
            return False
        if not (
            type(envelope) is tuple
            and len(envelope) == 2
            and type(envelope[0]) is tuple
            and type(envelope[1]) is FrozenDict
        ):
            return False
        return all(type(key) is str for key, _value in envelope[1].entries)

    def _read_validated_snapshot(self, store: ArtifactStore, digest: str) -> Snapshot | object:
        with self._allow_raw_reads_scope():
            payload = store.get(digest)
        if type(payload) is not bytes:
            return _MISSING_SNAPSHOT
        if hashlib.sha256(payload).hexdigest() != digest:
            return _MISSING_SNAPSHOT
        try:
            snapshot = deserialize_snapshot(payload)
        except (RecursionError, UnsupportedValueError, TypeError, ValueError):
            return _MISSING_SNAPSHOT
        if fingerprint_snapshot(snapshot) != digest:
            return _MISSING_SNAPSHOT
        return snapshot

    def _try_warm_from_checkpoint(self, query: Any, key: NodeKey, call_snapshot: Any) -> bool:
        """Try to warm *key* from the checkpoint. Returns True if the record was loaded."""
        ckpt = self._checkpoint_query_records.get(key)
        if ckpt is None:
            return False
        if ckpt.get("is_untracked"):
            return False
        if not snapshots_equal(call_snapshot, call_snapshot):
            return False
        # The root call snapshot is thawed to obtain the arguments passed to
        # the query. A changed adapter can therefore alter a fresh execution's
        # inputs even when the saved result itself uses only native values.
        if not self._adapter_keys_trusted(collect_adapter_keys(call_snapshot)):
            return False
        # An adapter whose implementation changed (or vanished) since the save
        # would thaw this record's snapshot into a value a fresh run would not
        # produce. Refuse and re-execute under the live adapter instead.
        if not self._adapter_keys_trusted(ckpt.get("adapter_keys", ())):
            return False
        # The root's transitive pinned-query set governs this warm and every
        # dependency query warmed beneath it. A dep query outside the set was
        # reached via a runtime import or dynamic dispatch, so its code is not
        # pinned into any identity here and it must not be served from the
        # checkpoint -- refuse and let a fresh execution re-derive it.
        pinned_query_objects, pinned_resource_objects = self._collect_pinned_capture_objects(
            query.fn
        )
        pinned_queries = builtins.set(pinned_query_objects)
        previous_pinned = self._checkpoint_root_pinned
        previous_query_objects = self._checkpoint_root_pinned_query_objects
        previous_resources = self._checkpoint_root_pinned_resources
        self._checkpoint_root_pinned = pinned_queries
        self._checkpoint_root_pinned_query_objects = pinned_query_objects
        self._checkpoint_root_pinned_resources = pinned_resource_objects
        try:
            if not self._checkpoint_deps_are_pinned(ckpt["deps"], pinned_queries):
                return False
            dependencies = self._verify_and_resolve_checkpoint_deps(ckpt["deps"])
        finally:
            self._checkpoint_root_pinned = previous_pinned
            self._checkpoint_root_pinned_query_objects = previous_query_objects
            self._checkpoint_root_pinned_resources = previous_resources
        if dependencies is None:
            return False
        snapshot = self._load_snapshot_from_store(ckpt["snapshot_digest"])
        if snapshot is _MISSING_SNAPSHOT or not snapshots_equal(snapshot, snapshot):
            return False
        # Normalise the warmed record onto this database's timeline: its old
        # changed_at belongs to the saving process and means nothing here.
        # changed_at == verified_at == the current revision, plus real edges,
        # lets the ordinary red/green machinery govern it. checked_in_request
        # stays unset so the get that warmed it still verifies its deps.
        self._records[key] = NodeRecord(
            key=key,
            label=key.label,
            snapshot=cast(Snapshot, snapshot),
            digest=ckpt["snapshot_digest"],
            changed_at=self._revision,
            verified_at=self._revision,
            dependencies=dependencies,
            last_decision="reused",
            last_recompute="reused",
            reason="restored from checkpoint",
            checked_in_request=-1,
        )
        self._query_records.add(key)
        self._query_objects()[key.identity] = query
        self._call_snapshots()[key] = call_snapshot
        return True

    def _warm_checkpoint_dep_query(self, dep_key: NodeKey) -> bool:
        """Warm a checkpoint query dep without having its Query callable."""
        if dep_key in self._records:
            return True
        ckpt = self._checkpoint_query_records.get(dep_key)
        if ckpt is None:
            return False
        if ckpt.get("is_untracked"):
            return False
        # Same adapter-trust gate as the root warm: a dep record frozen under a
        # since-changed adapter must not be served from the checkpoint.
        if not self._adapter_keys_trusted(ckpt.get("adapter_keys", ())):
            return False
        call_snapshot = self._load_snapshot_from_store(dep_key.args_digest)
        if (
            call_snapshot is _MISSING_SNAPSHOT
            or not self._is_query_call_snapshot(call_snapshot)
            or not snapshots_equal(call_snapshot, call_snapshot)
            or not self._adapter_keys_trusted(collect_adapter_keys(cast(Snapshot, call_snapshot)))
        ):
            return False
        # Apply the root's pinned-query gate transitively: a dep-of-a-dep reached
        # only via runtime import is not code-pinned and must not warm.
        pinned_queries = self._checkpoint_root_pinned
        if pinned_queries is not None and not self._checkpoint_deps_are_pinned(
            ckpt["deps"], pinned_queries
        ):
            return False
        dependencies = self._verify_and_resolve_checkpoint_deps(ckpt["deps"])
        if dependencies is None:
            return False
        snapshot = self._load_snapshot_from_store(ckpt["snapshot_digest"])
        if snapshot is _MISSING_SNAPSHOT or not snapshots_equal(snapshot, snapshot):
            return False
        # A dep warmed without its Query object is flagged checkpoint_loaded so
        # _maybe_changed_after re-verifies it transitively through its edges.
        self._records[dep_key] = NodeRecord(
            key=dep_key,
            label=dep_key.label,
            snapshot=cast(Snapshot, snapshot),
            digest=ckpt["snapshot_digest"],
            changed_at=self._revision,
            verified_at=self._revision,
            dependencies=dependencies,
            last_decision="reused",
            last_recompute="reused",
            reason="restored from checkpoint (dep)",
            checked_in_request=-1,
            checkpoint_loaded=True,
        )
        self._query_records.add(dep_key)
        return True

    def _checkpoint_deps_are_pinned(
        self, deps: list[dict[str, Any]], pinned_queries: builtins.set[str]
    ) -> bool:
        """True unless a query dep's ``query_id`` is outside the pinned set."""
        for dep in deps:
            if dep["kind"] == "query" and dep["query_id"] not in pinned_queries:
                return False
        return True

    def _verify_and_resolve_checkpoint_deps(
        self, deps: list[dict[str, Any]]
    ) -> builtins.set[NodeKey] | None:
        """Verify every checkpoint dep against live state and resolve its key.

        Returns the resolved dependency edges (as live ``NodeKey``s) when all
        deps verify, or ``None`` if any dep cannot be verified -- in which case
        the caller must refuse to warm and let the query re-execute.
        """
        resolved: set[NodeKey] = set()
        for dep in deps:
            if not self._verify_checkpoint_dep(dep):
                return None
            dep_key = self._resolve_checkpoint_dep_key(dep)
            if dep_key is None:
                return None
            resolved.add(dep_key)
        return resolved

    def _resolve_checkpoint_dep_key(self, dep: dict[str, Any]) -> NodeKey | None:
        """Rebuild the live ``NodeKey`` for a checkpoint dep, or ``None``.

        Input deps carry only a name, so they are resolved against the live
        input node; query and resource deps carry their full identity.
        """
        dep_kind = dep["kind"]
        if dep_kind == "input":
            return self._find_input_node_by_key(dep["key"])
        if dep_kind in ("query", "resource"):
            return NodeKey(
                kind=dep_kind,
                identity=dep["identity"],
                args_digest=dep["args_digest"],
                label=dep["label"],
            )
        return None

    def _verify_checkpoint_dep(self, dep: dict[str, Any]) -> bool:
        dep_kind = dep["kind"]
        if dep_kind == "input":
            return self._verify_checkpoint_input_dep(dep)
        if dep_kind == "query":
            return self._verify_checkpoint_query_dep(dep)
        if dep_kind == "resource":
            return self._verify_checkpoint_resource_dep(dep)
        return False

    def _verify_checkpoint_input_dep(self, dep: dict[str, Any]) -> bool:
        input_key = self._find_input_node_by_key(dep["key"])
        if input_key is None:
            return False
        input_obj = self._inputs_by_key.get(dep["key"])
        if input_obj is None or self._input_policy_digest(input_obj) != dep["policy_digest"]:
            return False
        record = self._records.get(input_key)
        if record is None:
            return False
        if not self._adapter_keys_trusted(collect_adapter_keys(record.snapshot)):
            return False
        expected_digest: str = dep["digest"]
        return self._checkpoint_record_matches(record, expected_digest)

    def _checkpoint_record_matches(self, record: NodeRecord, expected_digest: str) -> bool:
        """Compare a live/restored record with checkpoint bytes substitutively."""
        expected_snapshot = self._load_snapshot_from_store(expected_digest)
        if expected_snapshot is _MISSING_SNAPSHOT:
            return False
        expected = cast(Snapshot, expected_snapshot)
        if not self._adapter_keys_trusted(collect_adapter_keys(expected)):
            return False
        return snapshots_equal(record.snapshot, expected)

    def _verify_checkpoint_query_dep(self, dep: dict[str, Any]) -> bool:
        dep_key = NodeKey(
            kind="query",
            identity=dep["identity"],
            args_digest=dep["args_digest"],
            label=dep["label"],
        )
        expected_digest: str = dep["digest"]
        checkpoint_record = self._checkpoint_query_records.get(dep_key)
        if checkpoint_record is not None and not self._adapter_keys_trusted(
            checkpoint_record.get("adapter_keys", ())
        ):
            # Re-freezing a live result under a changed adapter can reproduce
            # the old bytes while thawing those bytes has different semantics.
            # The digest therefore cannot validate a native parent result.
            return False
        record = self._records.get(dep_key)
        if record is not None:
            return self._checkpoint_record_matches(record, expected_digest)
        # Prefer warming the dep's subtree from the checkpoint (no execution:
        # resources come back via probe hints). If the subtree can't be warmed
        # -- e.g. it reaches a resource unresolvable from the pinned captures --
        # verify the dep by re-execution instead.
        if self._warm_checkpoint_dep_query(dep_key):
            return self._checkpoint_record_matches(self._records[dep_key], expected_digest)
        return self._execute_to_verify_query_dep(dep, dep_key, expected_digest)

    def _execute_to_verify_query_dep(
        self, dep: dict[str, Any], dep_key: NodeKey, expected_digest: str
    ) -> bool:
        """Verify a query dep by re-executing its pinned code against live state.

        Used when a query dep cannot be warmed from the checkpoint. Recovers the
        dep's call snapshot from the store (content-addressed by its args_digest;
        missing/corrupt ⇒ degrade to warm refusal), runs the pinned Query live --
        so its resources are probed against the real world -- and compares the
        resulting snapshot to the manifest's expected snapshot under the typed,
        substitutive relation. Equal means verified and live; different means
        the parent must refuse checkpoint reuse.
        """
        pinned_objects = self._checkpoint_root_pinned_query_objects
        if pinned_objects is None:
            return False
        query_obj = pinned_objects.get(dep["query_id"])
        if query_obj is None:
            return False
        # The pinned map is keyed by bare query_id (first-wins), so a root that
        # captures two same-query_id queries with divergent bodies (a factory
        # twin) can hand back the wrong object. Registering it under the saved
        # identity would execute the wrong body live and poison the request via
        # the checked_in_request short-circuit. Refuse unless the live object's
        # full identity matches the dep's -- mirroring the identity match that
        # _resolve_checkpoint_resource applies to pinned resources. On refusal
        # the parent re-executes and binds the correct object via _query_key.
        live_identity = f"{query_obj.key}:{self._query_fingerprint(query_obj)}"
        if live_identity != dep_key.identity:
            return False
        # Never re-run an impure (untracked) leaf as a warm-verification step:
        # an untracked record is never trusted; let the parent re-execute it.
        ckpt = self._checkpoint_query_records.get(dep_key)
        if ckpt is not None and ckpt.get("is_untracked"):
            return False
        call_snapshot = self._load_snapshot_from_store(dep["args_digest"])
        if call_snapshot is _MISSING_SNAPSHOT:
            return False
        # The call snapshot carries this dep's arguments; an adapted argument
        # thawed under a since-changed adapter would re-run the pinned query with
        # the wrong input. Refuse unless every adapter it uses is still trusted.
        if not self._adapter_keys_trusted(collect_adapter_keys(call_snapshot)):
            return False
        # Register the pinned object and restored call snapshot so the executed
        # dep becomes a fully live node: downstream reuse and future transitive
        # re-verification both look it up here.
        self._query_objects()[dep_key.identity] = query_obj
        self._call_snapshots()[dep_key] = call_snapshot
        try:
            self._ensure_query(query_obj, dep_key, call_snapshot)
        except (ResourceDependencyError, QueryConcurrencyError):
            self._discard_uncommitted_query(dep_key)
            raise
        except Exception:
            self._discard_uncommitted_query(dep_key)
            return False
        record = self._records.get(dep_key)
        return record is not None and self._checkpoint_record_matches(record, expected_digest)

    def _verify_checkpoint_resource_dep(self, dep: dict[str, Any]) -> bool:
        dep_key = NodeKey(
            kind="resource",
            identity=dep["identity"],
            args_digest=dep["args_digest"],
            label=dep["label"],
        )
        expected_digest: str = dep["digest"]
        expected_snapshot = self._load_snapshot_from_store(expected_digest)
        if expected_snapshot is _MISSING_SNAPSHOT or not self._adapter_keys_trusted(
            collect_adapter_keys(cast(Snapshot, expected_snapshot))
        ):
            # As with query dependencies, equal frozen bytes are not semantic
            # evidence when the adapter that thaws them has changed.
            return False
        record = self._records.get(dep_key)
        if record is not None:
            return snapshots_equal(record.snapshot, cast(Snapshot, expected_snapshot))
        # No live record: resolve the resource object from the root's pinned
        # captures (identity match), thaw its parameter from the store, and probe
        # LIVE via _refresh_resource. That takes the checkpoint probe-hint fast
        # path when the probe still matches (snapshot restored from the store) or
        # a full live load otherwise; either way the resulting record's digest
        # reflects live state, so the compare below is sound. If the resource
        # can't be resolved, refuse -- a query-level execute-to-verify may still
        # re-establish it by re-running the reader.
        resolved = self._resolve_checkpoint_resource(dep_key)
        if resolved is None:
            return False
        resource, parameter, registration = resolved
        self._resource_objects()[dep_key] = registration
        try:
            self._refresh_resource(resource, parameter, dep_key)
        except (ResourceDependencyError, QueryConcurrencyError):
            if dep_key not in self._records:
                self._resource_objects().pop(dep_key, None)
            raise
        except Exception:
            if dep_key not in self._records:
                self._resource_objects().pop(dep_key, None)
            return False
        record = self._records.get(dep_key)
        return record is not None and snapshots_equal(
            record.snapshot, cast(Snapshot, expected_snapshot)
        )

    def _resolve_checkpoint_resource(
        self, dep_key: NodeKey
    ) -> tuple[Any, Any, _ResourceRegistration] | None:
        """Resolve (resource object, parameter) for a checkpoint resource dep.

        The object comes from the warm root's pinned captures (matched on the
        resource's content identity); the parameter is rebuilt from the owned
        snapshot in the store, content-addressed by the dep's args_digest. Any
        missing or non-substitutive piece ⇒ None, which the caller treats as
        "cannot verify from the checkpoint".
        """
        pinned = self._checkpoint_root_pinned_resources
        if pinned is None:
            return None
        base_identity, separator, parameter_type_digest = dep_key.identity.rpartition(":")
        if not separator or not self._is_digest(parameter_type_digest):
            return None
        resource = pinned.get(base_identity)
        if resource is None:
            return None
        parameter_snapshot = self._load_snapshot_from_store(dep_key.args_digest)
        if parameter_snapshot is _MISSING_SNAPSHOT:
            return None
        # A resource parameter that thaws through an adapter must do so under the
        # same implementation that froze it; a changed thaw could hand the
        # resource a different-shaped parameter. The round-trip guard below also
        # catches a changed freeze, but only the digest check catches a thaw-only
        # change, so gate here explicitly.
        if not self._adapter_keys_trusted(collect_adapter_keys(parameter_snapshot)):
            return None
        registration = _ResourceRegistration(
            resource=resource,
            parameter_snapshot=cast(Snapshot, parameter_snapshot),
            parameter_type_digest=parameter_type_digest,
        )
        materialized, parameter = self._materialize_resource_parameter(dep_key, registration)
        if not materialized:
            return None
        return resource, parameter, registration

    def _load_snapshot_from_store(self, digest: str) -> Snapshot | object:
        if digest in self._checkpoint_snapshot_cache:
            return self._checkpoint_snapshot_cache[digest]
        store = self._store or self._checkpoint_load_store
        if store is None:
            return _MISSING_SNAPSHOT
        return self._read_validated_snapshot(store, digest)

    def _persist_snapshot_to(self, snapshot: Snapshot, store: ArtifactStore) -> None:
        """Put one checkpoint object, validating any existing content address.

        ``contains`` establishes presence only. The store's idempotent ``put``
        contract is what proves that bytes already bound to the digest are the
        canonical payload, so checkpoint save must always exercise it.
        """
        digest = fingerprint_snapshot(snapshot)
        with self._allow_raw_reads_scope():
            payload = serialize_snapshot(snapshot)
            store.put(digest, payload)

    def _find_input_node_by_key(self, input_key: str) -> NodeKey | None:
        input_obj = self._inputs_by_key.get(input_key)
        if input_obj is None:
            return None
        return self._input_records.get(input_obj)

    def _input_ident_for_key(self, key: NodeKey) -> str:
        return key.identity

    def _query_id_for_key(self, key: NodeKey) -> str:
        query_obj = self._query_objects().get(key.identity)
        if query_obj is not None:
            return str(query_obj.key)
        # No live Query object (e.g. a checkpoint-warmed dep re-saved): recover
        # the query_id from the identity, which is "<query_id>:<code_fingerprint>"
        # where the code fingerprint is a colon-free hex digest.
        return key.identity.rsplit(":", 1)[0]

    def read_input(self, input_key: _core.Input[T]) -> T:
        self._reject_resource_hook_read("Database.read_input()")
        _reject_cross_database_query_read(self, "Database.read_input()")
        from .core import Input

        if type(input_key) is not Input:
            raise TypeError("db.read_input() expects an Input instance.")
        with self._state_lock:
            key = self._input_key(input_key)
            record = self._records.get(key)
            if record is None:
                raise KeyError(f"Input {input_key.key!r} has not been set.")
            self._record_dependency(key)
            return cast(T, self._expose_boundary_snapshot(record.snapshot))

    @overload
    def read_resource(
        self,
        resource: _resources.Resource[ResourceKeyT, ResourceValueT, ResourceProbeT],
        parameter: ResourceKeyT,
    ) -> ResourceValueT: ...

    @overload
    def read_resource(self, resource: Any, parameter: Any) -> Any: ...

    def read_resource(self, resource: Any, parameter: Any) -> Any:
        self._reject_resource_hook_read("Database.read_resource()")
        _reject_cross_database_query_read(self, "Database.read_resource()")
        with self._state_lock, self._request_scope() as pending:
            key = self._resource_key(resource, parameter)
            outcome = _RefreshOutcome()
            try:
                self._refresh_resource(resource, parameter, key, outcome)
                self._record_dependency(key)
                result = self._expose_boundary_snapshot(self._records[key].snapshot)
                from .resources import FileStatResource, _file_stat_public_value

                if isinstance(resource, FileStatResource):
                    result = _file_stat_public_value(result)
            except (ResourceDependencyError, QueryConcurrencyError):
                if key not in self._records:
                    self._resource_objects().pop(key, None)
                raise
            except Exception:
                # A load that raised is still an observation: when it left a
                # failure record behind, the reader depends on it exactly as it
                # would on a value, so the edge is recorded before unwinding.
                if key in self._records:
                    self._record_dependency(key)
                else:
                    self._resource_objects().pop(key, None)
                if not outcome.failure_recorded:
                    # Nothing in the graph describes the exception this reader is
                    # about to see, so whatever it returns cannot be re-derived
                    # from records at load time. A failure record is excluded from
                    # checkpoints for that reason; a reader of an *unrecordable*
                    # raise has to be excluded for it too, and with it -- through
                    # the save-time dependency closure -- everything above it.
                    self._mark_frame_uncheckpointable()
                raise
        self._dispatch_events(pending)
        return result

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
        if not snapshots_equal(call_snapshot, call_snapshot):
            self._execute_query(
                query,
                key,
                call_snapshot,
                previous=existing,
                reason="non-substitutive query arguments",
            )
            self._mark_query_used(key)
            return
        if not snapshots_equal(existing.snapshot, existing.snapshot):
            self._execute_query(
                query,
                key,
                call_snapshot,
                previous=existing,
                reason="non-substitutive cached result",
            )
            self._mark_query_used(key)
            return
        if existing.is_untracked:
            self._execute_query(
                query,
                key,
                call_snapshot,
                previous=existing,
                reason="untracked dependency",
            )
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

    def _execute_query(
        self,
        query: Any,
        key: NodeKey,
        call_snapshot: Any,
        previous: NodeRecord | None,
        reason: str,
    ) -> None:
        frame = ExecutionFrame(key=key)
        stack = self._execution_stack.get()
        token = self._execution_stack.set(stack + (frame,))
        query_executions = _ACTIVE_QUERY_EXECUTIONS.get()
        query_execution_token = _ACTIVE_QUERY_EXECUTIONS.set(query_executions + (self,))
        raw_reads_token = self._allow_raw_reads.set(False)
        try:
            # The guard covers the whole query boundary, not just the body:
            # materializing arguments runs adapter thaws and freezing the
            # result runs adapter freezes, and an ambient read in either
            # smuggles untracked state into the stored snapshot.
            with self._guard_untracked_reads():
                query_args, query_kwargs = self._materialize_call(
                    call_snapshot,
                    record_boundaries=self._mode == "checked",
                    frame=frame,
                )
                _raise_sticky_concurrency_violation(frame)
                t0 = time.perf_counter_ns()
                result = query.fn(self, *query_args, **query_kwargs)
                _raise_sticky_concurrency_violation(frame)
                elapsed = time.perf_counter_ns() - t0
                if self._mode == "checked":
                    for before, value in zip(
                        frame.boundary_fingerprints, frame.boundary_values, strict=True
                    ):
                        assert_not_mutated(before, self._fingerprint_value(value))
                    _raise_sticky_concurrency_violation(frame)
                snapshot = self._freeze_value(result)
                _raise_sticky_concurrency_violation(frame)
            digest = fingerprint_snapshot(snapshot)
            impure = bool(frame.untracked_reasons)
            value_changed = False

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
                previous_snapshot = previous.snapshot
                if query.eq is None and query.cutoff is None:
                    # Both operands are canonical freeze outputs.  The typed
                    # structural relation is mode-independent, distinguishes
                    # Python-equal numeric representations, and deliberately
                    # refuses NaN rather than letting a digest backdate it.
                    equal = False if impure else snapshots_equal(previous_snapshot, snapshot)
                else:
                    # Policy code is user code. Give it owned operands even in
                    # strict mode, where non-boundary exposure otherwise returns
                    # the canonical Frozen* snapshot itself.
                    old_value = self._expose_snapshot(detach_snapshot(previous_snapshot))
                    new_value = self._expose_snapshot(detach_snapshot(snapshot))
                    equal = (
                        False
                        if impure
                        else self._compare_values(
                            eq=query.eq,
                            cutoff=query.cutoff,
                            left=old_value,
                            right=new_value,
                        )
                    )
                    _raise_sticky_concurrency_violation(frame)
                record.snapshot = snapshot
                record.digest = digest
                if equal:
                    record.changed_at = previous_changed_at
                    decision = "backdated"
                elif impure and snapshots_equal(previous_snapshot, snapshot):
                    # `equal` was forced above, not observed: an untracked
                    # read skips the comparison entirely. When the re-run
                    # then lands a substitutively equal snapshot there is no new
                    # value to propagate, so keep the old changed_at and
                    # leave the revision alone -- otherwise a stable impure
                    # leaf churns the counter on every warm request. This
                    # structural short-circuit applies only to the forced case:
                    # when a comparison actually ran and said unequal (a
                    # custom eq policy may, even for identical snapshots),
                    # the bump below stands.
                    record.changed_at = previous_changed_at
                    decision = "executed"
                else:
                    # A recompute that lands a new value is a change in the
                    # graph exactly as an input set or a resource reload is,
                    # so it moves the revision the same way. Without the bump
                    # an untracked leaf's new value lands at the revision its
                    # grandparent was verified at: the direct parent re-runs
                    # (untracked forces that), but its own changed_at then
                    # fails the strictly-greater check and every ancestor
                    # above it keeps a value a fresh database never produces.
                    self._revision += 1
                    record.changed_at = self._revision
                    decision = "executed"
                    value_changed = True
            self._query_records.add(key)
            record.verified_at = self._revision
            record.dependencies = frame.dependencies
            record.last_decision = decision
            record.last_recompute = decision
            record.reason = reason
            record.untracked_reasons = list(frame.untracked_reasons)
            record.checkpointable = frame.checkpointable
            record.checked_in_request = self._current_request_id()
            if decision == "backdated":
                self._stats["query_backdates"] += 1
            else:
                self._stats["query_executions"] += 1
            if value_changed:
                self._enqueue_observer_event(query, key, record)
            self._query_timings.setdefault(key, _TimingAggregate()).add(elapsed)
        finally:
            self._allow_raw_reads.reset(raw_reads_token)
            _ACTIVE_QUERY_EXECUTIONS.reset(query_execution_token)
            self._execution_stack.reset(token)

    def _enqueue_observer_event(self, query: Any, key: NodeKey, record: NodeRecord) -> None:
        registrations = self._observers.get(key)
        if not registrations:
            return
        pending = self._pending_events.get()
        if pending is None:
            return
        pending.append(
            _PendingObserverEvent(
                event=QueryChangeEvent(
                    query_id=query.key,
                    args_digest=key.args_digest,
                    decision="executed",
                    changed_at=record.changed_at,
                    verified_at=record.verified_at,
                ),
                callbacks=tuple(registration.callback for registration in registrations),
            )
        )

    def _unregister_observer(self, key: NodeKey, registration: _ObserverRegistration) -> None:
        with self._state_lock:
            registrations = self._observers.get(key)
            if registrations is None:
                return
            index = next(
                (
                    index
                    for index, candidate in enumerate(registrations)
                    if candidate is registration
                ),
                None,
            )
            if index is None:
                return
            del registrations[index]
            if not registrations:
                del self._observers[key]
                if key not in self._query_records:
                    self._call_snapshots().pop(key, None)
                    self._query_timings.pop(key, None)
                    if not any(
                        item.identity == key.identity for item in self._call_snapshots()
                    ) and not any(item.identity == key.identity for item in self._query_records):
                        self._query_objects().pop(key.identity, None)

    def _dispatch_events(self, events: list[_PendingObserverEvent] | None) -> None:
        if not events:
            return
        for pending in events:
            for callback in pending.callbacks:
                try:
                    callback(pending.event)
                except Exception as exc:
                    with suppress(Exception):
                        self._observer_error_hook(exc)

    def _maybe_changed_after(self, key: NodeKey, revision: int) -> bool:
        record = self._records.get(key)
        if record is None:
            return True
        if key.kind == "query":
            query_obj = self._query_objects().get(key.identity)
            call_snapshot = self._call_snapshots().get(key)
            if query_obj is None or call_snapshot is None:
                # A checkpoint-warmed record has no live Query object to re-run,
                # so re-verify it transitively through its own edges instead of
                # trusting it. Anything else with no Query object is treated as
                # changed (we cannot prove it is not).
                if not record.checkpoint_loaded:
                    return True
                if self._verify_checkpoint_loaded_record(record):
                    return True
            else:
                # A digest cannot prove that a NaN-containing call is the same
                # computation. Force the caller to rebuild the dependency with
                # its current live arguments instead of rechecking an old
                # ephemeral call node.
                if not snapshots_equal(call_snapshot, call_snapshot):
                    return True
                try:
                    self._ensure_query(query_obj, key, call_snapshot)
                except Exception:
                    # Verification happens before the parent's body runs. A
                    # child failure here must make the dependency dirty so the
                    # parent executes and observes that failure at its ordinary
                    # call site, where its own exception handler can decide the
                    # public result.
                    return True
        elif key.kind == "resource":
            registration = self._resource_objects().get(key)
            if registration is None:
                return True
            materialized, parameter = self._materialize_resource_parameter(key, registration)
            if not materialized:
                # Some accepted parameter shapes (notably dataclasses and
                # PathLike values) do not reconstruct with their original type.
                # Without an exact owned live value, force the parent to execute
                # and supply its parameter again instead of probing a different
                # value under this node's old digest.
                return True
            outcome = _RefreshOutcome()
            try:
                self._refresh_resource(registration.resource, parameter, key, outcome)
            except (ResourceDependencyError, QueryConcurrencyError):
                raise
            except Exception:
                # A refresh that raises must not escape a dependent's
                # verification pass: with a failure record describing *this*
                # attempt the probe comparison below decides, and the dependent
                # re-reads inside its own body where its own handler can see the
                # exception. When nothing was recorded -- an unobservable probe,
                # or a freeze that failed after a successful load -- the record
                # still describes an older world, so its changed_at may not be
                # trusted: report changed and let the dependent re-read.
                if not outcome.failure_recorded:
                    return True
        current = self._records[key]
        return (
            current.is_untracked
            or not snapshots_equal(current.snapshot, current.snapshot)
            or current.changed_at > revision
        )

    def _verify_checkpoint_loaded_record(self, record: NodeRecord) -> bool:
        """Re-verify a checkpoint-warmed record that has no live Query object.

        Walks its dependency edges: if every dep is unchanged since the record
        was last verified, the record is still good (bump ``verified_at`` and
        report unchanged). If any dep changed, report changed so the parent
        re-executes and re-keys this child against live state.
        """
        for dep_key in sorted(record.dependencies, key=lambda item: item.label):
            if self._maybe_changed_after(dep_key, record.verified_at):
                return True
        record.verified_at = self._revision
        return False

    def _refresh_resource(
        self,
        resource: Any,
        parameter: Any,
        key: NodeKey,
        outcome: _RefreshOutcome | None = None,
    ) -> None:
        """Bring this resource node up to date, raising what its load raised.

        An observation that raises without being recorded leaves the record
        describing a world that has just been contradicted. Reporting the node as
        changed for that one refresh is not enough: the record keeps its old
        probe, so once the world returns to the state it describes -- an undo, a
        branch switch back -- the probe matches again and the record claims
        "unchanged" across an interval it never observed. Dependents that
        consumed the exception then stay green on a value no fresh ``Database``
        produces. Mark it here instead, so the stored probe stops deciding
        anything until a real observation rewrites the record.

        Marking alone only repairs the resource's *direct* readers. Reporting the
        node as changed makes each direct reader re-execute and see the exception,
        but a reader that handles it returns at the current revision, so its own
        ``changed_at`` does not move and its parents never learn that anything
        happened -- they keep a value derived from the pre-failure world forever.
        The transition into "unconfirmed" is a change in the graph exactly as a
        recorded failure is, so it moves the revision too, and the reader's
        re-execution then lands on a revision its parents have not verified past.
        The bump is guarded on the mark not already being set: one bump per
        transition, not one per request, so a permanently unprobeable resource
        settles at a fixed revision instead of churning it on every ``get()``.
        Every path that rewrites the record from a real observation -- a
        successful load and a recordable failure alike -- clears the mark, so a
        resource that heals and breaks again bumps again.
        """
        outcome = outcome if outcome is not None else _RefreshOutcome()
        try:
            self._observe_resource(resource, parameter, key, outcome)
        except (ResourceDependencyError, QueryConcurrencyError):
            raise
        except Exception:
            if not outcome.failure_recorded:
                record = self._records.get(key)
                if record is not None:
                    if not record.probe_unconfirmed:
                        self._revision += 1
                    record.probe_unconfirmed = True
            raise

    def _observe_resource(
        self,
        resource: Any,
        parameter: Any,
        key: NodeKey,
        outcome: _RefreshOutcome,
    ) -> None:
        record = self._records.get(key)
        current_request = self._current_request_id()
        if record is not None and record.checked_in_request == current_request:
            if not record.is_failed:
                return
            if record.failure_exc is not None:
                # A resource is observed at most once per request; a failure is
                # settled for the request exactly as a value is. Re-raising the
                # exception that this request's load produced keeps a fan-out of
                # readers at one load instead of one per reader, and the object
                # is never older than the observation the request already made.
                outcome.failure_recorded = True
                raise record.failure_exc.with_traceback(record.failure_traceback)
        with self._resource_hook_scope(resource, "probe_and_load"):
            atomic_hook = getattr(resource, "probe_and_load", None)
        atomic = callable(atomic_hook)
        if atomic and (
            (record is not None and not record.is_failed and not record.probe_unconfirmed)
            or (record is None and key in self._checkpoint_resource_probes)
        ):
            # A record (or checkpoint hint) that could answer this request makes
            # a standalone probe worth taking before the combined read: for the
            # built-in file resources that is read-plus-hash with no decode. The
            # standalone result only ever answers "unchanged" against a stored
            # atomic pair and is discarded on a miss, so every stored
            # (probe, value) pair still originates from one observed read. A
            # probe that raises is not an observation; fall through and let the
            # combined read decide, exactly as it would have without the
            # attempt.
            try:
                with self._resource_hook_scope(resource, "probe"), self._allow_raw_reads_scope():
                    early_probe = resource.probe(parameter)
            except (ResourceDependencyError, QueryConcurrencyError):
                raise
            except Exception:
                pass
            else:
                with self._resource_hook_scope(resource, "probe"):
                    early_probe_snapshot = self._freeze_owned_snapshot(early_probe)
                if self._reuse_on_probe_hit(record, early_probe_snapshot, current_request):
                    return
                if record is None and self._restore_from_probe_hint(
                    key, early_probe_snapshot, current_request
                ):
                    return
        if atomic:
            try:
                with (
                    self._resource_hook_scope(resource, "probe_and_load"),
                    self._allow_raw_reads_scope(),
                ):
                    probe, loaded_value = cast(Callable[..., tuple[Any, Any]], atomic_hook)(
                        self, parameter
                    )
            except (ResourceDependencyError, QueryConcurrencyError):
                raise
            except Exception as exc:
                outcome.failure_recorded = self._record_resource_failure(
                    key,
                    record,
                    self._observe_failure_probe(resource, parameter),
                    exc,
                    current_request,
                )
                raise
        else:
            with self._resource_hook_scope(resource, "probe"), self._allow_raw_reads_scope():
                probe = resource.probe(parameter)
            loaded_value = None
        probe_hook_name = "probe_and_load" if atomic else "probe"
        with self._resource_hook_scope(resource, probe_hook_name):
            probe_snapshot = self._freeze_owned_snapshot(probe)
        if self._reuse_on_probe_hit(record, probe_snapshot, current_request):
            return
        if record is None and self._restore_from_probe_hint(key, probe_snapshot, current_request):
            return
        if not atomic:
            try:
                with self._resource_hook_scope(resource, "load"), self._allow_raw_reads_scope():
                    loaded_value = resource.load(self, parameter)
            except (ResourceDependencyError, QueryConcurrencyError):
                raise
            except Exception as exc:
                outcome.failure_recorded = self._record_resource_failure(
                    key, record, probe_snapshot, exc, current_request
                )
                raise
        value_hook_name = "probe_and_load" if atomic else "load"
        with self._resource_hook_scope(resource, value_hook_name):
            snapshot = self._freeze_value(loaded_value)
        digest = fingerprint_snapshot(snapshot)
        if record is None:
            changed_at = self._revision
        else:
            self._revision += 1
            changed_at = self._revision
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
                probe=probe_snapshot,
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
        record.probe = probe_snapshot
        record.checked_in_request = current_request
        record.failure = None
        record.failure_exc = None
        record.failure_traceback = None
        record.probe_unconfirmed = False

    def _reuse_on_probe_hit(
        self,
        record: NodeRecord | None,
        probe_snapshot: Any,
        current_request: int,
    ) -> bool:
        """Answer this request from the record when its probe is unchanged.

        A failure record must never take the probe-hit early exit as if it had
        a value: it holds no snapshot to reuse. The first read of each request
        re-runs the load on an unchanged failing probe, which is what keeps the
        exception a live one; the rest of the request re-raises it earlier. A
        record whose probe was contradicted by an unrecorded raise is excluded
        for the same reason its changed_at is: matching a probe the node has
        since failed to confirm proves nothing about the interval between.
        """
        if (
            record is not None
            and not record.is_failed
            and not record.probe_unconfirmed
            and snapshots_equal(record.snapshot, record.snapshot)
            and snapshots_equal(record.probe, probe_snapshot)
        ):
            record.verified_at = self._revision
            record.last_decision = "reused"
            record.reason = "resource probe unchanged"
            record.checked_in_request = current_request
            self._stats["resource_probe_hits"] += 1
            return True
        return False

    def _restore_from_probe_hint(
        self,
        key: NodeKey,
        probe_snapshot: Any,
        current_request: int,
    ) -> bool:
        """Scope-B: restore a recordless resource from its checkpoint probe hint.

        When the hint's probe matches, the snapshot comes from the store without
        performing a full load. The hint is a FROZEN probe, so callers compare
        the live probe's frozen form: a live value and a thawed snapshot differ
        in shape (a frozen-dataclass probe thaws to a dict) and would never
        match.
        """
        hint = self._checkpoint_resource_probes.get(key)
        if hint is None:
            return False
        expected_probe_snapshot, expected_digest = hint
        if snapshots_equal(probe_snapshot, expected_probe_snapshot) and self._adapter_keys_trusted(
            collect_adapter_keys(expected_probe_snapshot)
        ):
            snapshot = self._load_snapshot_from_store(expected_digest)
            # An adapter whose implementation changed (or vanished) since the
            # save would thaw this restored snapshot into a value a fresh load
            # never produces. The probe can stay stable while the adapter code
            # moves, so gate the restore just like every other thaw-into-live
            # path; on distrust fall through to the full load, which re-freezes
            # a fresh load under the live adapter.
            if (
                snapshot is not _MISSING_SNAPSHOT
                and snapshots_equal(snapshot, snapshot)
                and self._adapter_keys_trusted(collect_adapter_keys(snapshot))
            ):
                self._records[key] = NodeRecord(
                    key=key,
                    label=key.label,
                    snapshot=cast(Snapshot, snapshot),
                    digest=expected_digest,
                    changed_at=self._revision,
                    verified_at=self._revision,
                    last_decision="reused",
                    last_recompute="reused",
                    reason="restored from checkpoint",
                    probe=probe_snapshot,
                    checked_in_request=current_request,
                )
                self._stats["resource_probe_hits"] += 1
                return True
        return False

    def _observe_failure_probe(self, resource: Any, parameter: Any) -> Any:
        """Frozen probe observed alongside a load that raised.

        Returns ``_MISSING_SNAPSHOT`` when the probe itself cannot be observed: a
        resource that cannot even be probed models its failures partially and is
        outside the contract, so it gets no record at all.

        The base ``Resource`` supplies ``probe_and_load``, so every resource takes
        the atomic branch and this observation happens at a *later* instant than
        the load that raised: ``inspect()`` can show a failed node whose probe
        already describes a healed world. That is self-correcting rather than
        sticky -- a failure record never takes the probe-unchanged early exit, so
        the next request re-runs the load and succeeds. Overriding
        ``probe_and_load`` to observe both from one read is what removes the gap.
        """
        try:
            with self._resource_hook_scope(resource, "probe"), self._allow_raw_reads_scope():
                return self._freeze_owned_snapshot(resource.probe(parameter))
        except (ResourceDependencyError, QueryConcurrencyError):
            raise
        except Exception:
            return _MISSING_SNAPSHOT

    def _record_resource_failure(
        self,
        key: NodeKey,
        record: NodeRecord | None,
        probe_snapshot: Any,
        exc: BaseException,
        current_request: int,
    ) -> bool:
        """Record that this resource's load raised, carrying the observed probe.

        A failed load is an observation, not the absence of one, so the node keeps
        a record and the ordinary probe comparison decides when dependents must
        re-run. The changed_at discipline matches the success path: an unchanged
        failing probe keeps dependents green, while a changed probe or a
        transition between success and failure bumps the revision.

        Returns whether a record was written. ``False`` means the node still
        describes an older world, which callers must treat as "changed" rather
        than trusting the record's ``changed_at``.
        """
        if probe_snapshot is _MISSING_SNAPSHOT:
            return False
        failure = f"{type(exc).__name__}: {exc}"
        if record is None:
            changed_at = self._revision
        elif (
            record.is_failed
            and not record.probe_unconfirmed
            and snapshots_equal(record.probe, probe_snapshot)
        ):
            changed_at = record.changed_at
        else:
            self._revision += 1
            changed_at = self._revision
        # Outside a request nothing can re-raise this exception, so holding it
        # (and the load frame its traceback pins) would buy nothing.
        pending = self._request_failures.get()
        retained = exc if pending is not None else None
        retained_traceback = exc.__traceback__ if pending is not None else None
        if record is None:
            self._records[key] = NodeRecord(
                key=key,
                label=key.label,
                snapshot=None,
                digest="",
                changed_at=changed_at,
                verified_at=self._revision,
                last_decision="failed",
                last_recompute="failed",
                reason=f"resource load failed: {failure}",
                probe=probe_snapshot,
                checked_in_request=current_request,
                failure=failure,
                failure_exc=retained,
                failure_traceback=retained_traceback,
            )
        else:
            record.snapshot = None
            record.digest = ""
            record.changed_at = changed_at
            record.verified_at = self._revision
            record.last_decision = "failed"
            record.last_recompute = "failed"
            record.reason = f"resource load failed: {failure}"
            record.probe = probe_snapshot
            record.checked_in_request = current_request
            record.failure = failure
            record.failure_exc = retained
            record.failure_traceback = retained_traceback
            record.probe_unconfirmed = False
        if pending is not None:
            pending.append(key)
        return True

    def _release_failure_exceptions(self, keys: list[NodeKey]) -> None:
        """Drop the exceptions this request stored on its failure records.

        Only reads *within* the request that produced one may re-raise it, so the
        request boundary is where the traceback -- and every frame and local it
        keeps alive -- stops being useful. Records are never evicted, so leaving
        them attached would pin one load frame per permanently failing node.
        """
        for key in keys:
            record = self._records.get(key)
            if record is not None:
                record.failure_exc = None
                record.failure_traceback = None

    def _query_key(
        self, query: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> tuple[NodeKey, Any]:
        # Freeze the complete call as one graph. Besides retaining the existing
        # flat snapshot for tree-shaped calls, this preserves aliases and cycles
        # shared between positional and keyword values.
        call_snapshot = self._freeze_value((args, kwargs))
        args_digest = fingerprint_snapshot(call_snapshot)
        query_fingerprint = self._query_fingerprint(query)
        identity = f"{query.key}:{query_fingerprint}"
        identity = self._ephemeral_identity_for_non_substitutive(identity, call_snapshot)
        key = NodeKey(
            kind="query",
            identity=identity,
            args_digest=args_digest,
            label=f"{query.key}[{args_digest[:12]}] {query.__name__}()",
        )
        self._query_objects()[key.identity] = query
        self._call_snapshots()[key] = call_snapshot
        return key, call_snapshot

    def _ephemeral_identity_for_non_substitutive(self, identity: str, snapshot: Any) -> str:
        """Make a content-colliding, non-substitutive value unique to this call."""

        if snapshots_equal(snapshot, snapshot):
            return identity
        self._non_substitutive_identity_counter += 1
        return f"{identity}:non-substitutive:{self._non_substitutive_identity_counter}"

    def _input_key(self, input_key: Any) -> NodeKey:
        key = self._input_records.get(input_key)
        if key is None:
            self._validate_input_registration(input_key)
            key = self._register_input(input_key)
        return key

    def _validate_input_registration(self, input_key: Any) -> None:
        policy_digest = self._input_policy_digest(input_key)
        existing = self._inputs_by_key.get(input_key.key)
        if (
            existing is not None
            and existing is not input_key
            and self._input_policy_digest(existing) != policy_digest
        ):
            raise InputKeyError(
                f"Input key {input_key.key!r} is already registered with a conflicting "
                "equality/cutoff policy."
            )

    def _prospective_input_key(self, input_key: Any) -> NodeKey:
        return NodeKey(
            kind="input",
            identity=input_key.key,
            args_digest="",
            label=f"input[{input_key.key}]",
        )

    def _register_input(self, input_key: Any) -> NodeKey:
        key = self._input_records.get(input_key)
        if key is not None:
            return key
        self._validate_input_registration(input_key)
        return self._commit_input_registration(input_key)

    def _commit_input_registration(self, input_key: Any) -> NodeKey:
        key = self._input_records.get(input_key)
        if key is not None:
            return key
        existing = self._inputs_by_key.get(input_key.key)
        if existing is not None:
            key = self._input_records[existing]
            self._input_records[input_key] = key
            return key
        key = self._prospective_input_key(input_key)
        self._input_records[input_key] = key
        self._inputs_by_key[input_key.key] = input_key
        return key

    def _resource_key(self, resource: Any, parameter: Any) -> NodeKey:
        frozen_parameter = self._freeze_value(parameter)
        parameter_digest = fingerprint_snapshot(frozen_parameter)
        resource_identity = self._fingerprint_identity_payload(
            self._resource_identity_payload(resource),
            subject=(
                f"Resource {type(resource).__module__}.{type(resource).__qualname__} identity"
            ),
        )
        parameter_type_digest = self._fingerprint_identity_payload(
            (
                "resource-parameter-types-v3",
                self._resource_configuration_type_payload(parameter),
            ),
            subject="Resource parameter type identity",
        )
        with self._resource_hook_scope(resource, "label"):
            label = resource.label(parameter)
            if not isinstance(label, str):
                raise TypeError("Resource.label() must return a string.")
            if not label:
                raise ValueError("Resource.label() must return a non-empty string.")
        identity = (
            f"{type(resource).__module__}:{type(resource).__qualname__}:"
            f"{resource_identity}:{parameter_type_digest}"
        )
        identity = self._ephemeral_identity_for_non_substitutive(identity, frozen_parameter)
        key = NodeKey(
            kind="resource",
            identity=identity,
            args_digest=parameter_digest,
            label=label,
        )
        self._resource_objects()[key] = _ResourceRegistration(
            resource=resource,
            parameter_snapshot=frozen_parameter,
            parameter_type_digest=parameter_type_digest,
        )
        return key

    def _materialize_resource_parameter(
        self,
        key: NodeKey,
        registration: _ResourceRegistration,
    ) -> tuple[bool, Any]:
        """Rebuild an owned live parameter only when its exact identity survives."""

        snapshot_candidate = detach_snapshot(registration.parameter_snapshot)
        if self._resource_parameter_matches_registration(
            key,
            registration,
            snapshot_candidate,
        ):
            # An inbound Frozen* value already has the hook-visible type the
            # caller supplied. Use the detached shell directly instead of
            # thawing it into a different Python container type.
            return True, snapshot_candidate
        try:
            # Give adapter thaw hooks their own copy too: a hook must never gain a
            # reflective mutation path back into the registry's canonical value.
            parameter = self._thaw_value(detach_snapshot(registration.parameter_snapshot))
        except Exception:
            return False, None
        if not self._resource_parameter_matches_registration(key, registration, parameter):
            return False, None
        return True, parameter

    def _resource_parameter_matches_registration(
        self,
        key: NodeKey,
        registration: _ResourceRegistration,
        parameter: Any,
    ) -> bool:
        try:
            parameter_type_digest = self._fingerprint_identity_payload(
                (
                    "resource-parameter-types-v3",
                    self._resource_configuration_type_payload(parameter),
                ),
                subject="Resource parameter type identity",
            )
            if parameter_type_digest != registration.parameter_type_digest:
                return False
            round_trip = self._freeze_owned_snapshot(parameter)
        except Exception:
            return False
        return snapshots_equal(round_trip, registration.parameter_snapshot) and (
            fingerprint_snapshot(round_trip) == key.args_digest
        )

    def _materialize_call(
        self, call_snapshot: Any, *, record_boundaries: bool, frame: ExecutionFrame
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        if self._mode == "strict":
            envelope = self._strict_snapshot_view(call_snapshot)
            if not self._is_materialized_call_envelope(envelope, kwargs_type=FrozenDict):
                raise UnsupportedValueError("Invalid query call snapshot.")
            frozen_args, frozen_kwargs = envelope
            args = frozen_args
            kwargs = dict(frozen_kwargs.entries)
            return args, kwargs

        envelope = self._thaw_value(call_snapshot)
        if not self._is_materialized_call_envelope(envelope, kwargs_type=dict):
            raise UnsupportedValueError("Invalid query call snapshot.")
        args, kwargs = envelope
        if record_boundaries:
            boundary_values = (*args, *kwargs.values())
            frame.boundary_fingerprints.extend(
                self._fingerprint_value(value) for value in boundary_values
            )
            frame.boundary_values.extend(boundary_values)
        return args, kwargs

    @staticmethod
    def _is_materialized_call_envelope(envelope: Any, *, kwargs_type: type[Any]) -> bool:
        if not (
            type(envelope) is tuple
            and len(envelope) == 2
            and type(envelope[0]) is tuple
            and type(envelope[1]) is kwargs_type
        ):
            return False
        if kwargs_type is FrozenDict:
            return all(type(key) is str for key, _value in envelope[1].entries)
        return all(type(key) is str for key in envelope[1])

    @classmethod
    def _strict_snapshot_view(cls, snapshot: Any) -> Any:
        """Expose a snapshot through rebuilt immutable container interfaces.

        Every `Frozen*` shell is rebuilt, graph or not: frozen dataclass
        setters refuse plain writes, but `object.__setattr__` bypasses them,
        so a view aliasing the stored snapshot would let a caller corrupt the
        record it came from. Leaf values and all-leaf tuples are shared —
        nothing reflective can rebind their contents.
        """

        if type(snapshot) is not FrozenGraph:
            return cls._detached_snapshot_view(snapshot)

        shells: list[Any] = []
        for node in snapshot.nodes:
            if type(node) is FrozenList:
                shells.append(FrozenList(()))
            elif type(node) is FrozenDict:
                shells.append(FrozenDict(()))
            elif type(node) is FrozenSet:
                shells.append(FrozenSet(node.kind, ()))
            elif type(node) is FrozenRecord:
                shells.append(FrozenRecord(node.type_name, ()))
            else:
                raise TypeError("FrozenGraph contains an unsupported node.")

        def resolve(value: Any) -> Any:
            if type(value) is FrozenRef:
                return shells[value.index]
            if type(value) is FrozenList:
                return FrozenList(tuple(resolve(item) for item in value.items))
            if type(value) is FrozenDict:
                return FrozenDict(
                    tuple((resolve(key), resolve(item)) for key, item in value.entries)
                )
            if type(value) is FrozenSet:
                return FrozenSet(value.kind, tuple(resolve(item) for item in value.items))
            if type(value) is FrozenRecord:
                return FrozenRecord(
                    value.type_name,
                    tuple((key, resolve(item)) for key, item in value.entries),
                )
            if type(value) is FrozenAdapterValue:
                return FrozenAdapterValue(value.adapter_key, resolve(value.payload))
            if type(value) is tuple:
                return tuple(resolve(item) for item in value)
            return value

        for shell, node in zip(shells, snapshot.nodes, strict=True):
            if type(node) is FrozenList:
                object.__setattr__(shell, "items", tuple(resolve(item) for item in node.items))
            elif type(node) is FrozenDict:
                object.__setattr__(
                    shell,
                    "entries",
                    tuple((resolve(key), resolve(item)) for key, item in node.entries),
                )
            elif type(node) is FrozenSet:
                object.__setattr__(shell, "items", tuple(resolve(item) for item in node.items))
            else:
                object.__setattr__(
                    shell,
                    "entries",
                    tuple((key, resolve(item)) for key, item in node.entries),
                )
        return resolve(snapshot.root)

    @classmethod
    def _detached_snapshot_view(cls, value: Any) -> Any:
        detach = cls._detached_snapshot_view
        if type(value) is FrozenList:
            return FrozenList(tuple(detach(item) for item in value.items))
        if type(value) is FrozenDict:
            return FrozenDict(tuple((detach(key), detach(item)) for key, item in value.entries))
        if type(value) is FrozenSet:
            return FrozenSet(value.kind, tuple(detach(item) for item in value.items))
        if type(value) is FrozenRecord:
            return FrozenRecord(
                value.type_name,
                tuple((key, detach(item)) for key, item in value.entries),
            )
        if type(value) is FrozenAdapterValue:
            return FrozenAdapterValue(value.adapter_key, detach(value.payload))
        if type(value) is FrozenGraph:
            return cls._strict_snapshot_view(value)
        if type(value) is tuple:
            detached = tuple(detach(item) for item in value)
            if all(item is original for item, original in zip(detached, value, strict=True)):
                return value
            return detached
        return value

    def _expose_snapshot(
        self,
        snapshot: Any,
        *,
        boundary: bool = False,
        record_boundaries: bool = False,
        frame: ExecutionFrame | None = None,
    ) -> Any:
        # Strict mode exposes the adapter envelope without calling ``thaw``, but
        # that envelope is still meaningful only under the adapter state that
        # produced it. Enforce the same lifetime pin in every mode.
        self._verify_adapter_lifetime()
        if self._mode == "strict":
            # Callers never see the FrozenGraph envelope: a graph-shaped result
            # is rebuilt into shared/cyclic Frozen* views at the boundary,
            # exactly as _materialize_call does for call arguments. Non-boundary
            # exposure feeds the equality/cutoff comparison and keeps the raw
            # snapshot -- a cyclic view cannot be re-frozen for that check.
            exposed = self._strict_snapshot_view(snapshot) if boundary else snapshot
        else:
            exposed = self._thaw_value(snapshot)
        if boundary and record_boundaries and frame is not None:
            frame.boundary_fingerprints.append(self._fingerprint_value(exposed))
            frame.boundary_values.append(exposed)
        return exposed

    def _expose_boundary_snapshot(self, snapshot: Any) -> Any:
        frame = self._current_frame()
        return self._expose_snapshot(
            snapshot,
            boundary=True,
            record_boundaries=self._mode == "checked" and frame is not None,
            frame=frame,
        )

    def _record_dependency(self, key: NodeKey) -> None:
        frame = self._current_frame()
        if frame is None:
            return
        frame.dependencies.add(key)

    def _mark_caught_query_failure(
        self,
        query: Any,
        key: NodeKey | None,
        error: BaseException,
    ) -> None:
        """Conservatively invalidate a query that may handle a child failure.

        A query publishes its dependency edges only after it returns. If a
        nested query raises before publishing, its caller can catch the error
        and otherwise publish a dependency-free fallback that remains cached
        after the child heals. Mark each caller frame while the exception
        unwinds; the eventual successful catcher then re-executes on every
        request and is omitted from checkpoints.

        A direct same-key recursive call is the one exception. ``CycleError``
        is a deterministic property of that active frame, and preserving the
        existing cached fallback keeps the supported self-cycle pattern useful.
        Indirect cycles still propagate through distinct keys and take the
        conservative path.
        """
        frame = self._current_frame()
        if frame is None:
            return
        if key == frame.key and isinstance(error, CycleError):
            return
        query_key = getattr(query, "key", "<unknown>")
        reason = f"caught failure from child query {query_key!r} before it published"
        if reason not in frame.untracked_reasons:
            frame.untracked_reasons.append(reason)
        frame.checkpointable = False

    def _mark_frame_uncheckpointable(self) -> None:
        frame = self._current_frame()
        if frame is None:
            return
        frame.checkpointable = False

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
                self._inspect_record(dependency)
                for dependency in sorted(record.dependencies, key=lambda item: item.label)
            ),
        )

    def _query_objects(self) -> dict[str, Any]:
        return self._query_registry

    def _resource_objects(self) -> dict[NodeKey, _ResourceRegistration]:
        return self._resource_registry

    def _call_snapshots(self) -> dict[NodeKey, Any]:
        return self._call_snapshot_registry

    @contextmanager
    def _resource_hook_scope(self, resource: Any, hook_name: str) -> Iterator[None]:
        """Run one Resource hook under the no-database-dependencies contract.

        The violation is sticky for the complete hook call. A hook cannot catch
        ``ResourceDependencyError`` and publish a value whose provenance omits
        the attempted database read.
        """
        context = _ResourceHookContext(
            database=self,
            resource_type=f"{type(resource).__module__}.{type(resource).__qualname__}",
            hook_name=hook_name,
        )
        stack = _ACTIVE_RESOURCE_HOOKS.get()
        token = _ACTIVE_RESOURCE_HOOKS.set(stack + (context,))
        try:
            yield
        except BaseException as exc:
            if context.violation is not None and exc is not context.violation:
                raise context.violation from exc
            raise
        else:
            if context.violation is not None:
                raise context.violation
        finally:
            _ACTIVE_RESOURCE_HOOKS.reset(token)

    def _reject_resource_hook_read(self, operation: str) -> None:
        contexts = _ACTIVE_RESOURCE_HOOKS.get()
        if not contexts:
            return
        active = contexts[-1]
        error = active.violation
        if error is None:
            error = ResourceDependencyError(
                f"Resource hook {active.resource_type}.{active.hook_name}() cannot call "
                f"{operation}. Resource hooks must observe external state directly; "
                "compose Input, Query, and Resource reads in a @query instead."
            )
        reason = (
            f"Resource hook {active.resource_type}.{active.hook_name}() attempted "
            f"the forbidden read {operation}."
        )
        for context in contexts:
            if context.violation is None:
                context.violation = error
            frame = context.database._current_frame()
            if frame is not None:
                if reason not in frame.untracked_reasons:
                    frame.untracked_reasons.append(reason)
                frame.checkpointable = False
        raise error

    @contextmanager
    def _allow_raw_reads_scope(self) -> Iterator[None]:
        token = self._allow_raw_reads.set(True)
        try:
            yield
        finally:
            self._allow_raw_reads.reset(token)

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

    def _query_fingerprint(self, query: Any) -> str:
        request_memo = self._request_query_fingerprints.get()
        if request_memo is not None:
            cached = request_memo.get(id(query))
            if cached is not None and cached[0] is query:
                return cached[1]

        result = self._fingerprint_identity_payload(
            (
                "query-v4",
                self._query_handle_payload(query),
                self._code_fingerprint(query.fn),
                self._policy_definition_payload(query.eq),
                self._policy_definition_payload(query.cutoff),
            ),
            subject=f"Query {query.key!r} definition",
        )
        if request_memo is not None:
            # The query is pinned by this entry for the request's lifetime, so
            # its id cannot be recycled into a false hit. No cached definition
            # digest crosses a request boundary: every new request observes the
            # full supported definition again before it can reuse a query record.
            request_memo[id(query)] = (query, result)
        return result

    @staticmethod
    def _fingerprint_identity_payload(payload: Any, *, subject: str) -> str:
        """Fingerprint only payloads that form a substitutive identity.

        Canonical bytes deliberately normalize NaN payloads, so a digest alone
        cannot prove equality under the kernel's typed relation. Definition and
        configuration identities have no safe "always changed" fallback: refuse
        them rather than publish a stable key for non-substitutive state.
        """

        if not snapshots_equal(payload, payload):
            raise UnsupportedValueError(
                f"{subject} contains a non-substitutive value such as NaN and "
                "cannot be fingerprinted safely."
            )
        return fingerprint_snapshot(payload)

    @staticmethod
    def _query_handle_payload(query: Any) -> tuple[Any, ...]:
        """Return the complete immutable public state copied onto a Query handle."""

        return (
            "query-handle-v1",
            query.key,
            query.__module__,
            query.__name__,
            query.__qualname__,
            query.__doc__,
        )

    def _captured_query_public_surface_payload(
        self,
        query: Any,
        seen_functions: builtins.set[int],
    ) -> tuple[Any, ...]:
        """Pin identity-observable state exposed by a captured Query handle."""

        fn = cast(FunctionType, query.fn)
        return (
            "captured-query-public-surface-v1",
            tuple(
                (
                    name,
                    self._state_site_incarnation(query, f"query.{name}", value),
                    value
                    if type(value) in {str, type(None)}
                    else ("callable", type(value).__module__, type(value).__qualname__),
                )
                for name, value in (
                    ("key", query.key),
                    ("__module__", query.__module__),
                    ("__name__", query.__name__),
                    ("__qualname__", query.__qualname__),
                    ("__doc__", query.__doc__),
                    ("fn", fn),
                    ("eq", query.eq),
                    ("cutoff", query.cutoff),
                )
            ),
            self._captured_function_public_surface_payload(
                fn,
                identity_owner=query,
                site_prefix="query.fn",
                seen_functions=seen_functions,
            ),
        )

    @staticmethod
    def _function_metadata_containers(
        fn: FunctionType,
    ) -> tuple[
        tuple[Any, ...] | None,
        dict[str, Any] | None,
        dict[str, Any],
        tuple[Any, ...],
    ]:
        """Return function metadata only when its public containers are exact.

        FunctionType accepts tuple/dict subclasses for these attributes on
        supported CPython versions. Their iteration can expose one value to the
        fingerprint while overridden public lookup exposes another, so the
        container implementation is part of the trust boundary.
        """

        defaults = fn.__defaults__
        if defaults is not None and type(defaults) is not tuple:
            raise UnsupportedValueError(
                f"Function {fn.__module__}.{fn.__qualname__} must use an exact "
                "tuple for __defaults__."
            )
        kwdefaults = fn.__kwdefaults__
        if kwdefaults is not None and (
            type(kwdefaults) is not dict or any(type(name) is not str for name in kwdefaults)
        ):
            raise UnsupportedValueError(
                f"Function {fn.__module__}.{fn.__qualname__} must use an exact "
                "dict with exact str keys for __kwdefaults__."
            )
        state = fn.__dict__
        if type(state) is not dict or any(type(name) is not str for name in state):
            raise UnsupportedValueError(
                f"Function {fn.__module__}.{fn.__qualname__} must use an exact "
                "dict with exact str keys for __dict__."
            )
        type_parameters = getattr(fn, "__type_params__", ())
        if type(type_parameters) is not tuple:
            raise UnsupportedValueError(
                f"Function {fn.__module__}.{fn.__qualname__} must use an exact "
                "tuple for __type_params__."
            )
        return defaults, kwdefaults, state, type_parameters

    def _captured_function_public_surface_payload(
        self,
        fn: FunctionType,
        *,
        identity_owner: object,
        site_prefix: str,
        seen_functions: builtins.set[int],
    ) -> tuple[Any, ...]:
        defaults, kwdefaults, state, type_parameters = self._function_metadata_containers(fn)
        annotation_function = getattr(fn, "__annotate__", None)
        if annotation_function is not None and not isinstance(annotation_function, FunctionType):
            raise UnsupportedValueError(
                f"Function {fn.__module__}.{fn.__qualname__} has a non-Python "
                "__annotate__ evaluator."
            )
        annotations = fn.__annotations__
        if type(annotations) is not dict or any(type(name) is not str for name in annotations):
            raise UnsupportedValueError(
                f"Function {fn.__module__}.{fn.__qualname__} must use an exact "
                "dict with exact str keys for __annotations__."
            )
        return (
            "captured-function-public-surface-v1",
            tuple(
                (
                    name,
                    self._observable_site_incarnation(
                        identity_owner, f"{site_prefix}.{name}", value
                    ),
                    value,
                )
                for name, value in (
                    ("__name__", fn.__name__),
                    ("__qualname__", fn.__qualname__),
                    ("__module__", fn.__module__),
                    ("__doc__", fn.__doc__),
                )
            ),
            self._observable_site_incarnation(
                identity_owner, f"{site_prefix}.__code__", fn.__code__
            ),
            self._observable_site_incarnation(
                identity_owner, f"{site_prefix}.__defaults__", defaults
            ),
            self._observable_site_incarnation(
                identity_owner, f"{site_prefix}.__kwdefaults__", kwdefaults
            ),
            self._observable_site_incarnation(
                identity_owner, f"{site_prefix}.__annotations__", annotations
            ),
            tuple(
                (
                    name,
                    self._observable_site_incarnation(
                        identity_owner,
                        f"{site_prefix}.__annotations__[{name}]",
                        value,
                    ),
                    self._captured_dependency_digest(
                        f"public-annotation[{name}]",
                        value,
                        seen_functions,
                        owner=fn,
                    ),
                )
                for name, value in annotations.items()
            ),
            self._observable_site_incarnation(identity_owner, f"{site_prefix}.__dict__", state),
            self._observable_site_incarnation(
                identity_owner,
                f"{site_prefix}.__type_params__",
                type_parameters,
            ),
            tuple(
                (
                    index,
                    self._observable_site_incarnation(
                        identity_owner,
                        f"{site_prefix}.__type_params__[{index}]",
                        value,
                    ),
                    self._captured_dependency_digest(
                        f"public-type-parameter[{index}]",
                        value,
                        seen_functions,
                        owner=fn,
                    ),
                )
                for index, value in enumerate(type_parameters)
            ),
            (
                (
                    "annotation-function",
                    self._observable_site_incarnation(
                        identity_owner,
                        f"{site_prefix}.__annotate__",
                        annotation_function,
                    ),
                    self._captured_function_public_surface_payload(
                        annotation_function,
                        identity_owner=identity_owner,
                        site_prefix=f"{site_prefix}.__annotate__",
                        seen_functions=seen_functions | {id(fn)},
                    ),
                )
                if annotation_function is not None and annotation_function is not fn
                else (
                    "recursive-annotation-function",
                    fn.__module__,
                    fn.__qualname__,
                )
                if annotation_function is fn
                else None
            ),
            self._observable_site_incarnation(
                identity_owner, f"{site_prefix}.__closure__", fn.__closure__
            ),
        )

    def _code_fingerprint(self, fn: FunctionType) -> str:
        payload = (
            *self._runtime_build_payload(),
            self._function_definition_payload(fn, set()),
        )
        return self._fingerprint_identity_payload(
            payload,
            subject=f"Function {fn.__module__}.{fn.__qualname__} definition",
        )

    def _runtime_build_payload(self) -> tuple[Any, ...]:
        """Interpreter and build identity shared by durable trust boundaries."""

        return _RUNTIME_BUILD_PAYLOAD

    def _function_definition_payload(
        self, fn: FunctionType, seen_functions: builtins.set[int]
    ) -> Any:
        defaults, kwdefaults, _state, _type_parameters = self._function_metadata_containers(fn)
        fn_id = id(fn)
        if fn_id in seen_functions:
            return ("recursive-function", fn.__module__, fn.__qualname__)
        seen_functions.add(fn_id)
        try:
            closure_vars = inspect.getclosurevars(fn)
            return (
                fn.__module__,
                fn.__qualname__,
                self._code_definition_payload(fn.__code__),
                tuple(
                    (
                        self._definition_access_incarnation(fn, f"default[{index}]", value),
                        self._captured_dependency_digest(
                            f"default[{index}]",
                            value,
                            seen_functions,
                            owner=fn,
                        ),
                    )
                    for index, value in enumerate(defaults or ())
                ),
                tuple(
                    (
                        name,
                        (
                            self._definition_access_incarnation(fn, f"kwdefault[{name}]", value),
                            self._captured_dependency_digest(
                                f"kwdefault[{name}]",
                                value,
                                seen_functions,
                                owner=fn,
                            ),
                        ),
                    )
                    for name, value in (kwdefaults or {}).items()
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
                self._function_metadata_payload(fn, seen_functions),
            )
        finally:
            seen_functions.remove(fn_id)

    def _function_metadata_payload(
        self, fn: FunctionType, seen_functions: builtins.set[int]
    ) -> Any:
        _defaults, _kwdefaults, state, type_parameters = self._function_metadata_containers(fn)
        try:
            annotations = fn.__annotations__
        except Exception as exc:
            annotation_function = getattr(fn, "__annotate__", None)
            if not isinstance(annotation_function, FunctionType):
                raise UnsupportedValueError(
                    f"Function {fn.__module__}.{fn.__qualname__} annotations "
                    "cannot be fingerprinted safely."
                ) from exc
            annotations_payload: Any = (
                "lazy-annotations",
                self._annotation_evaluator_payload(annotation_function, set()),
            )
        else:
            if type(annotations) is not dict or any(type(name) is not str for name in annotations):
                raise UnsupportedValueError(
                    f"Function {fn.__module__}.{fn.__qualname__} must use an exact "
                    "dict with exact str keys for __annotations__."
                )
            reflects_annotations = any(
                name in {"__annotations__", "get_annotations", "get_type_hints"}
                for code in self._walk_code_objects(fn.__code__)
                for name in code.co_names
            )
            annotations_payload = tuple(
                (
                    name,
                    (
                        self._definition_access_incarnation(fn, f"annotation[{name}]", value),
                        self._captured_dependency_digest(
                            f"annotation[{name}]",
                            value,
                            seen_functions,
                            owner=fn,
                        ),
                    )
                    if reflects_annotations
                    else self._freeze_annotation_capture(value, set()),
                )
                for name, value in annotations.items()
            )
        reflects_type_parameters = any(
            "__type_params__" in code.co_names for code in self._walk_code_objects(fn.__code__)
        )
        return (
            "function-metadata-v3",
            fn.__name__,
            fn.__qualname__,
            fn.__module__,
            fn.__doc__,
            annotations_payload,
            tuple(
                (
                    name,
                    (
                        self._definition_access_incarnation(fn, f"attribute[{name}]", value),
                        self._captured_dependency_digest(
                            f"attribute[{name}]",
                            value,
                            seen_functions,
                            owner=fn,
                        ),
                    ),
                )
                for name, value in state.items()
            ),
            (
                self._definition_access_incarnation(fn, "__type_params__", type_parameters),
                tuple(
                    (
                        self._definition_access_incarnation(
                            fn,
                            f"type-parameter[{index}]",
                            value,
                        ),
                        self._captured_dependency_digest(
                            f"type-parameter[{index}]",
                            value,
                            seen_functions,
                            owner=fn,
                        ),
                    )
                    for index, value in enumerate(type_parameters)
                ),
            )
            if reflects_type_parameters
            else tuple(self._freeze_annotation_capture(value, set()) for value in type_parameters),
        )

    def _definition_access_incarnation(
        self, fn: FunctionType, site: str, value: Any
    ) -> tuple[Any, ...]:
        """Pin identity for a definition object directly available to Python.

        Python exposes identity through more than an enumerable set of builtins:
        protocol methods and extension callables can reveal it too. Defaults,
        reflected annotations, and function attributes therefore carry a
        process-local site generation in addition to their structural payload.
        The registry retains the previous value until it can compare the next
        one by identity, so allocator address reuse cannot erase a replacement.
        A different process safely declines checkpoint reuse for this shape.
        """

        return self._definition_site_incarnation(fn, site, value)

    def _definition_site_incarnation(
        self, owner: FunctionType, site: str, value: Any
    ) -> tuple[Any, ...]:
        del self
        with _DEFINITION_IDENTITY_LOCK:
            sites = _DEFINITION_IDENTITY_SITES.get(owner)
            if sites is None:
                sites = {}
                _DEFINITION_IDENTITY_SITES[owner] = sites
            previous = sites.get(site)
            if previous is not None and previous[0] is value:
                generation = previous[1]
            else:
                _DEFINITION_IDENTITY_GENERATION[0] += 1
                generation = _DEFINITION_IDENTITY_GENERATION[0]
                sites[site] = (value, generation)
        return (
            "process-definition-site-incarnation",
            _PROCESS_IDENTITY_TOKEN.nonce,
            os.getpid(),
            generation,
        )

    @staticmethod
    def _state_identity_owner(owner: object) -> _StateIdentityOwner:
        """Return process-stable identity state for a public configuration owner."""

        owner_id = id(owner)
        with _DEFINITION_IDENTITY_LOCK:
            current = _STATE_IDENTITY_OWNERS.get(owner_id)
            if current is not None and current.owns(owner):
                return current
            _DEFINITION_IDENTITY_GENERATION[0] += 1
            current = _StateIdentityOwner(owner, _DEFINITION_IDENTITY_GENERATION[0])
            if current.owner_ref is not None:
                expected_generation = current.generation

                def discard(
                    _reference: weakref.ReferenceType[object],
                    *,
                    expected_generation: int = expected_generation,
                    expected_id: int = owner_id,
                    registry: dict[int, _StateIdentityOwner] = _STATE_IDENTITY_OWNERS,
                    lock: _thread.RLock = _DEFINITION_IDENTITY_LOCK,
                ) -> None:
                    # Module globals may already be cleared when weakrefs fire
                    # during interpreter shutdown. Keep the registry and lock
                    # alive with the callback, without retaining the owner
                    # state through the callback closure.
                    with lock:
                        registered = registry.get(expected_id)
                        if (
                            registered is not None
                            and registered.generation == expected_generation
                        ):
                            del registry[expected_id]

                current.owner_ref = weakref.ref(owner, discard)
            _STATE_IDENTITY_OWNERS[owner_id] = current
            return current

    def _state_owner_incarnation(self, owner: object) -> tuple[Any, ...]:
        state = self._state_identity_owner(owner)
        return (
            "process-state-owner-incarnation",
            _PROCESS_IDENTITY_TOKEN.nonce,
            os.getpid(),
            state.generation,
        )

    def _state_site_incarnation(self, owner: object, site: str, value: Any) -> tuple[Any, ...]:
        """Pin one recursively observable value within a stable state owner."""

        state = self._state_identity_owner(owner)
        with _DEFINITION_IDENTITY_LOCK:
            previous = state.sites.get(site)
            if previous is not None and previous[0] is value:
                generation = previous[1]
            else:
                _DEFINITION_IDENTITY_GENERATION[0] += 1
                generation = _DEFINITION_IDENTITY_GENERATION[0]
                state.sites[site] = (value, generation)
        return (
            "process-state-site-incarnation",
            _PROCESS_IDENTITY_TOKEN.nonce,
            os.getpid(),
            state.generation,
            generation,
        )

    def _observable_site_incarnation(self, owner: object, site: str, value: Any) -> tuple[Any, ...]:
        if isinstance(owner, FunctionType):
            return self._definition_site_incarnation(owner, site, value)
        return self._state_site_incarnation(owner, site, value)

    def _annotation_evaluator_payload(
        self, evaluator: FunctionType, active_ids: builtins.set[int]
    ) -> Any:
        evaluator_id = id(evaluator)
        if evaluator_id in active_ids:
            return ("recursive-annotation-evaluator", evaluator.__qualname__)
        active_ids.add(evaluator_id)
        try:
            closure_vars = inspect.getclosurevars(evaluator)
            return (
                "annotation-evaluator-v3",
                evaluator.__module__,
                evaluator.__qualname__,
                self._code_definition_payload(evaluator.__code__),
                tuple(
                    (
                        scope,
                        name,
                        self._freeze_annotation_capture(value, active_ids),
                    )
                    for scope, mapping in (
                        ("nonlocal", closure_vars.nonlocals),
                        ("global", closure_vars.globals),
                    )
                    for name, value in sorted(mapping.items())
                ),
                tuple(sorted(closure_vars.unbound)),
            )
        finally:
            active_ids.remove(evaluator_id)

    def _freeze_annotation_capture(self, value: Any, active_ids: builtins.set[int]) -> Any:
        if value is Ellipsis:
            return ("ellipsis",)
        if type(value) in (str, bytes, int, float, bool, type(None), complex):
            return self._freeze_static_capture(value, set())
        if isinstance(value, type):
            if "<locals>" in value.__qualname__:
                raise UnsupportedValueError(
                    f"Local annotation type {value.__module__}."
                    f"{value.__qualname__} cannot be fingerprinted safely."
                )
            top_level_module = value.__module__.partition(".")[0]
            if value.__module__ == "builtins":
                return ("annotation-type", value.__module__, value.__qualname__)
            if top_level_module in sys.stdlib_module_names or top_level_module == "pyinc":
                return (
                    "annotation-type",
                    self._module_type_anchor_payload(value),
                )
            return ("annotation-type", self._type_definition_payload(value))
        if isinstance(value, ModuleType):
            return (
                "annotation-module",
                value.__name__,
                self._module_identity_payload(value),
            )
        if isinstance(value, GenericAlias):
            return (
                "annotation-generic-alias",
                self._freeze_annotation_capture(value.__origin__, active_ids),
                tuple(self._freeze_annotation_capture(item, active_ids) for item in value.__args__),
            )
        if isinstance(value, UnionType):
            return (
                "annotation-union",
                tuple(
                    self._freeze_annotation_capture(item, active_ids)
                    for item in typing.get_args(value)
                ),
            )
        if type(value).__qualname__ == "ForwardRef" and type(value).__module__ in {
            "annotationlib",
            "typing",
        }:
            argument = getattr(value, "__forward_arg__", None)
            module = getattr(value, "__forward_module__", None)
            if not isinstance(argument, str) or (
                module is not None and not isinstance(module, str)
            ):
                raise UnsupportedValueError("Invalid forward annotation metadata.")
            return ("annotation-forward-reference", argument, module)
        if type(value).__qualname__ == "TypeAliasType" and type(value).__module__ in {
            "typing",
            "typing_extensions",
        }:
            alias_name = getattr(value, "__name__", None)
            alias_module = getattr(value, "__module__", None)
            parameters = getattr(value, "__type_params__", ())
            if (
                not isinstance(alias_name, str)
                or not isinstance(alias_module, str)
                or not isinstance(parameters, tuple)
            ):
                raise UnsupportedValueError("Invalid type-alias metadata.")
            evaluator = getattr(value, "evaluate_value", None)
            if isinstance(evaluator, FunctionType):
                alias_value: Any = self._annotation_evaluator_payload(evaluator, active_ids)
            else:
                alias_value = self._freeze_annotation_capture(value.__value__, active_ids)
            return (
                "annotation-type-alias",
                alias_module,
                alias_name,
                tuple(self._freeze_annotation_capture(item, active_ids) for item in parameters),
                alias_value,
            )
        parameter_types = tuple(
            candidate
            for candidate in (
                getattr(typing, "TypeVar", None),
                getattr(typing, "ParamSpec", None),
                getattr(typing, "TypeVarTuple", None),
            )
            if isinstance(candidate, type)
        )
        if parameter_types and isinstance(value, parameter_types):
            parameter_name = getattr(value, "__name__", None)
            if not isinstance(parameter_name, str):
                raise UnsupportedValueError("Annotation parameter has no stable name.")
            parameter_id = id(value)
            if parameter_id in active_ids:
                return ("recursive-annotation-parameter", parameter_name)
            active_ids.add(parameter_id)
            try:
                parts: list[Any] = []
                for evaluator_name, value_name in (
                    ("evaluate_bound", "__bound__"),
                    ("evaluate_constraints", "__constraints__"),
                    ("evaluate_default", "__default__"),
                ):
                    evaluator = getattr(value, evaluator_name, None)
                    if isinstance(evaluator, FunctionType):
                        parts.append(self._annotation_evaluator_payload(evaluator, active_ids))
                    else:
                        try:
                            part = getattr(value, value_name, None)
                        except Exception:
                            part = ("unresolved", value_name)
                        if isinstance(part, tuple):
                            parts.append(
                                tuple(
                                    self._freeze_annotation_capture(item, active_ids)
                                    for item in part
                                )
                            )
                        elif part is None or part is getattr(typing, "NoDefault", object()):
                            parts.append(None)
                        else:
                            parts.append(self._freeze_annotation_capture(part, active_ids))
                return (
                    "annotation-parameter",
                    type(value).__qualname__,
                    parameter_name,
                    tuple(parts),
                    bool(getattr(value, "__covariant__", False)),
                    bool(getattr(value, "__contravariant__", False)),
                    bool(getattr(value, "__infer_variance__", False)),
                )
            finally:
                active_ids.remove(parameter_id)
        typing_origin = (
            typing.get_origin(value) if type(value).__module__ in {"typing", "types"} else None
        )
        if typing_origin is not None:
            return (
                "annotation-typing-alias",
                self._freeze_annotation_capture(typing_origin, active_ids),
                tuple(
                    self._freeze_annotation_capture(item, active_ids)
                    for item in typing.get_args(value)
                ),
            )
        if type(value).__module__ == "typing":
            bindings = tuple(sorted(name for name, item in vars(typing).items() if item is value))
            if bindings:
                return ("annotation-typing-singleton", bindings)
        if type(value) is tuple:
            return tuple(self._freeze_annotation_capture(item, active_ids) for item in value)
        raise UnsupportedValueError(
            f"Unsupported annotation value {type(value).__module__}.{type(value).__qualname__}."
        )

    def _code_definition_payload(self, code: CodeType) -> Any:
        """Return a refcount-independent, typed encoding of a code object."""
        return (
            "code-v3",
            code.co_argcount,
            code.co_posonlyargcount,
            code.co_kwonlyargcount,
            code.co_nlocals,
            code.co_stacksize,
            code.co_flags,
            code.co_code,
            tuple(self._code_constant_payload(value) for value in code.co_consts),
            tuple(code.co_names),
            tuple(code.co_varnames),
            tuple(code.co_freevars),
            tuple(code.co_cellvars),
            code.co_exceptiontable,
            code.co_linetable,
            code.co_filename,
            code.co_name,
            code.co_qualname,
            code.co_firstlineno,
        )

    def _code_constant_payload(self, value: Any) -> Any:
        if value is None:
            return ("none",)
        if value is Ellipsis:
            return ("ellipsis",)
        if value is NotImplemented:
            return ("not-implemented",)
        if isinstance(value, bool):
            return ("bool", value)
        if isinstance(value, int):
            return ("int", value)
        if isinstance(value, float):
            return (
                "float-bits",
                struct.pack("!d", value),
                self._definition_object_incarnation(value),
            )
        if isinstance(value, complex):
            return (
                "complex-bits",
                struct.pack("!d", value.real),
                struct.pack("!d", value.imag),
                self._definition_object_incarnation(value),
            )
        if isinstance(value, str):
            return ("str", value)
        if isinstance(value, bytes):
            return ("bytes", value)
        if isinstance(value, tuple):
            return (
                "tuple",
                tuple(self._code_constant_payload(item) for item in value),
            )
        if isinstance(value, frozenset):
            items = tuple(self._code_constant_payload(item) for item in value)
            return (
                "frozenset",
                tuple(sorted(items, key=fingerprint_snapshot)),
            )
        if isinstance(value, slice):
            return (
                "slice",
                self._code_constant_payload(value.start),
                self._code_constant_payload(value.stop),
                self._code_constant_payload(value.step),
            )
        if isinstance(value, CodeType):
            return ("code", self._code_definition_payload(value))
        raise TypeError(
            f"Unsupported code constant {type(value).__module__}.{type(value).__qualname__}."
        )

    def _policy_definition_payload(self, policy: Any) -> Any:
        if policy is None:
            return (
                "default-semantic-equality-v3",
                _DEFAULT_SEMANTIC_EQUALITY_VERSION,
                _KERNEL_FINGERPRINT_VERSION,
            )
        policy_id = id(policy)
        stack = self._policy_fingerprint_stack.get()
        if policy_id in stack:
            return (
                "recursive-policy",
                getattr(policy, "__module__", type(policy).__module__),
                getattr(policy, "__qualname__", type(policy).__qualname__),
            )
        token = self._policy_fingerprint_stack.set(stack + (policy_id,))
        try:
            fn = getattr(policy, "__func__", policy)
            if isinstance(fn, FunctionType):
                try:
                    definition = self._function_definition_payload(fn, set())
                except (UnsupportedValueError, TypeError, ValueError) as exc:
                    raise UnsupportedValueError(
                        f"Equality/cutoff policy {fn.__module__}.{fn.__qualname__} "
                        "cannot be fingerprinted because one of its captures is not "
                        "snapshot-safe."
                    ) from exc
                bound_owner = getattr(policy, "__self__", None)
                if bound_owner is None:
                    return ("function", definition)
                return (
                    "bound-function",
                    definition,
                    self._policy_bound_owner_payload(bound_owner, allow_instance_state=True),
                )
            if isinstance(fn, BuiltinFunctionType):
                return (
                    "builtin",
                    fn.__module__,
                    fn.__qualname__,
                    self._policy_bound_owner_payload(getattr(fn, "__self__", None)),
                )
            if isinstance(fn, (MethodDescriptorType, WrapperDescriptorType)):
                owner_type = getattr(fn, "__objclass__", None)
                if not isinstance(owner_type, type):
                    raise UnsupportedValueError(
                        "Equality/cutoff method descriptor has no defining type."
                    )
                return (
                    "method-descriptor",
                    self._type_definition_payload(owner_type),
                    fn.__name__,
                )
            self._reject_callable_object_wrapper(
                policy,
                subject="Equality/cutoff policy",
            )
            call = policy.__call__ if callable(policy) else None
            call_fn = getattr(call, "__func__", call)
            if isinstance(call_fn, FunctionType):
                try:
                    definition = self._function_definition_payload(call_fn, set())
                    state = self._policy_instance_state_payload(policy)
                except (UnsupportedValueError, TypeError, ValueError) as exc:
                    policy_name = f"{type(policy).__module__}.{type(policy).__qualname__}"
                    raise UnsupportedValueError(
                        f"Equality/cutoff policy {policy_name} cannot be fingerprinted "
                        "because its implementation or instance state is not "
                        "snapshot-safe."
                    ) from exc
                return (
                    "callable",
                    type(policy).__module__,
                    type(policy).__qualname__,
                    self._state_owner_incarnation(policy),
                    self._implementation_type_payload(type(policy)),
                    definition,
                    state,
                )
            policy_name = f"{type(policy).__module__}.{type(policy).__qualname__}"
            raise UnsupportedValueError(
                f"Equality/cutoff policy {policy_name} uses a non-Python callable "
                "implementation whose state cannot be fingerprinted safely."
            )
        finally:
            self._policy_fingerprint_stack.reset(token)

    def _policy_bound_owner_payload(self, owner: Any, *, allow_instance_state: bool = False) -> Any:
        if owner is None:
            return ("none",)
        if isinstance(owner, ModuleType):
            return (
                "module",
                owner.__name__,
                self._module_identity_payload(owner),
            )
        if isinstance(owner, type):
            return self._type_definition_payload(owner)
        try:
            frozen = self._freeze_captured_immutable_payload(
                "bound-policy-owner",
                owner,
                set(),
                owner=owner,
                active_ids=set(),
            )
        except UnsupportedValueError:
            if not allow_instance_state:
                raise UnsupportedValueError(
                    f"Bound policy owner {type(owner).__module__}."
                    f"{type(owner).__qualname__} is not snapshot-safe."
                ) from None
            frozen = self._policy_instance_state_payload(owner)
        return (
            "instance",
            self._state_owner_incarnation(owner),
            self._implementation_type_payload(type(owner)),
            frozen,
        )

    def _policy_instance_state_payload(self, policy: Any) -> Any:
        slots = tuple(
            slot
            for cls in type(policy).__mro__
            for slot in (
                (cls.__dict__.get("__slots__"),)
                if isinstance(cls.__dict__.get("__slots__"), str)
                else cls.__dict__.get("__slots__", ())
            )
            if slot not in {"__dict__", "__weakref__"}
        )
        if slots:
            raise UnsupportedValueError(
                f"Policy {type(policy).__module__}.{type(policy).__qualname__} "
                "uses slot state that cannot be fingerprinted safely."
            )
        state = self._static_instance_dict(policy)
        if not state:
            return ()
        structural = tuple(
            (name, self._freeze_static_capture(value, set())) for name, value in state.items()
        )
        graph = freeze(tuple(state.items()))
        identity = tuple(
            (
                name,
                self._freeze_captured_immutable(
                    f"policy-state.{name}",
                    value,
                    set(),
                    owner=policy,
                    active_ids=set(),
                ),
            )
            for name, value in state.items()
        )
        return ("policy-state-v2", structural, graph, identity)

    def _input_policy_digest(self, input_obj: Any) -> str:
        return self._fingerprint_identity_payload(
            (
                "input-policy-v3",
                self._runtime_build_payload(),
                self._policy_definition_payload(input_obj.eq),
                self._policy_definition_payload(input_obj.cutoff),
            ),
            subject=f"Input {input_obj.key!r} policy",
        )

    def _current_adapter_digests(self) -> dict[str, str]:
        """Complete digest of each registered adapter, keyed by adapted type.

        The digest covers implementation and instance configuration.  The
        registry itself is fixed, but callers can still hold and mutate an
        adapter object, so lifetime checks recompute this small map.
        """
        return {
            _adapter_key(value_type): self._adapter_implementation_digest(adapter)
            for value_type, adapter in self._adapters.items()
        }

    def _verify_adapter_lifetime(self) -> None:
        """Reject adapter configuration that moved after construction."""
        if not self._adapters:
            return
        current = self._current_adapter_digests()
        if current == self._adapter_lifetime_digests:
            return
        changed = sorted(
            key
            for key in set(current) | set(self._adapter_lifetime_digests)
            if current.get(key) != self._adapter_lifetime_digests.get(key)
        )
        names = ", ".join(changed)
        raise UnsupportedValueError(
            "ValueAdapter configuration changed after Database construction"
            f" ({names}). Construct a new Database for the new adapter state."
        )

    def _adapter_implementation_digest(self, adapter: ValueAdapter) -> str:
        """Fingerprint an adapter's ``freeze``/``thaw`` implementation.

        Both methods' code is folded in via the same definition-payload machinery
        that pins query bodies, so a checkpoint record frozen under one adapter is
        refused under a changed one -- even a change to ``thaw`` alone, which
        leaves the stored payload (and its digest) untouched. Non-Python methods
        are identified by their public callable identity. A Python method whose
        captures cannot be pinned is rejected instead of silently weakening the
        checkpoint trust boundary to the adapter class name.
        """
        try:
            payload: Any = (
                type(adapter).__module__,
                type(adapter).__qualname__,
                self._runtime_build_payload(),
                self._implementation_type_payload(type(adapter)),
                self._adapter_state_payload(adapter),
                self._adapter_method_payload(adapter, "freeze"),
                self._adapter_method_payload(adapter, "thaw"),
            )
        except UnsupportedValueError as exc:
            raise UnsupportedValueError(
                f"Adapter {type(adapter).__module__}."
                f"{type(adapter).__qualname__} cannot be fingerprinted safely: {exc}"
            ) from exc
        return self._fingerprint_identity_payload(
            payload,
            subject=(f"Adapter {type(adapter).__module__}.{type(adapter).__qualname__} identity"),
        )

    def _adapter_state_payload(self, adapter: ValueAdapter) -> Any:
        slots = tuple(
            slot
            for cls in type(adapter).__mro__
            for slot in (
                (cls.__dict__.get("__slots__"),)
                if isinstance(cls.__dict__.get("__slots__"), str)
                else cls.__dict__.get("__slots__", ())
            )
            if slot not in {"__dict__", "__weakref__"}
        )
        if slots:
            raise UnsupportedValueError(
                f"Adapter {type(adapter).__module__}.{type(adapter).__qualname__} "
                "uses slot state that cannot be fingerprinted safely."
            )
        state = self._static_instance_dict(adapter)
        if not state:
            return ()
        try:
            structural = tuple(
                (name, self._freeze_static_capture(value, set())) for name, value in state.items()
            )
            graph = freeze(tuple(state.items()))
            identity = tuple(
                (
                    name,
                    self._freeze_captured_immutable(
                        f"adapter-state.{name}",
                        value,
                        set(),
                        owner=adapter,
                        active_ids=set(),
                    ),
                )
                for name, value in state.items()
            )
            return ("adapter-state-v2", structural, graph, identity)
        except UnsupportedValueError as exc:
            raise UnsupportedValueError(
                f"Adapter {type(adapter).__module__}.{type(adapter).__qualname__} "
                "has instance state that is not snapshot-safe."
            ) from exc

    def _adapter_method_payload(self, adapter: ValueAdapter, method_name: str) -> Any:
        method = getattr(adapter, method_name, None)
        fn = getattr(method, "__func__", method)
        if isinstance(fn, FunctionType):
            try:
                definition = self._function_definition_payload(fn, set())
            except (UnsupportedValueError, TypeError, ValueError) as exc:
                adapter_name = f"{type(adapter).__module__}.{type(adapter).__qualname__}"
                raise UnsupportedValueError(
                    f"Adapter {adapter_name}.{method_name} cannot be fingerprinted "
                    "for checkpoint reuse because one of its captures is not "
                    "snapshot-safe."
                ) from exc
            return (method_name, definition)
        return (
            method_name,
            getattr(method, "__module__", type(adapter).__module__),
            getattr(method, "__qualname__", type(adapter).__qualname__),
        )

    def _adapter_keys_trusted(self, adapter_keys: Iterable[str]) -> bool:
        """True iff every adapter key was frozen by an implementation this process
        still carries, byte-identical.

        A key absent from the live registry, or one whose implementation digest
        has moved since the checkpoint, is untrusted: the caller must refuse the
        warm so the record re-executes and any adapted payload is re-frozen and
        re-thawed under the live adapter.
        """
        self._verify_adapter_lifetime()
        if not self._checkpoint_adapter_digests and not self._adapters:
            # Fast path: no adapters anywhere means nothing to distrust.
            return True
        try:
            current = self._current_adapter_digests()
        except (UnsupportedValueError, TypeError, ValueError):
            # The live adapter can still be used for fresh execution, but its
            # implementation cannot be proven identical to the checkpoint's.
            return False
        for adapter_key in adapter_keys:
            expected = self._checkpoint_adapter_digests.get(adapter_key)
            live = current.get(adapter_key)
            if expected is None or live is None or live != expected:
                return False
        return True

    @staticmethod
    def _reject_callable_object_wrapper(value: Any, *, subject: str) -> None:
        """Reject opaque callable instances that claim a wrapped function.

        A Python function decorated with ``functools.wraps`` is still a
        ``FunctionType`` and is fingerprinted through the function path. A
        callable object is different: ``__wrapped__`` says nothing about its
        ``__call__`` implementation or instance state, so treating it as the
        wrapped function would make the query identity incomplete.
        """

        if isinstance(value, (FunctionType, MethodType, BuiltinFunctionType)):
            return
        if not callable(value):
            return
        wrapped_function = getattr(value, "__wrapped__", None)
        if not isinstance(wrapped_function, FunctionType):
            return
        raise UnsupportedValueError(
            f"{subject} uses callable object "
            f"{type(value).__module__}.{type(value).__qualname__} with __wrapped__. "
            "Callable-object wrappers are unsupported because __wrapped__ does not "
            "identify their __call__ implementation or instance state; capture an "
            "ordinary Python function or bound method instead."
        )

    def _captured_dependency_digest(
        self,
        name: str,
        value: Any,
        seen_functions: builtins.set[int],
        *,
        owner: FunctionType,
    ) -> Any:
        return (
            "captured-site-v1",
            self._definition_site_incarnation(owner, f"capture[{name}]", value),
            self._captured_dependency_payload(
                name,
                value,
                seen_functions,
                owner=owner,
            ),
        )

    def _captured_dependency_payload(
        self,
        name: str,
        value: Any,
        seen_functions: builtins.set[int],
        *,
        owner: FunctionType,
    ) -> Any:
        from .core import Input, Query

        if value is Database:
            # Query bodies may name the public constructor only to receive the
            # query-context contract error. Fingerprint the exact runtime module
            # instead of recursively walking Database's process-global locks and
            # ContextVars, which are intentionally not snapshot values.
            return (
                "database-type-v1",
                self._module_identity_payload(sys.modules[__name__]),
                value.__qualname__,
            )
        if isinstance(value, Query):
            # Fold the captured query's full definition into the parent's
            # identity so a change to a dependency query's body moves the parent.
            return (
                "query-v2",
                self._query_handle_payload(value),
                self._captured_query_public_surface_payload(value, seen_functions),
                self._function_definition_payload(cast(FunctionType, value.fn), seen_functions),
                self._policy_definition_payload(value.eq),
                self._policy_definition_payload(value.cutoff),
            )
        if isinstance(value, Input):
            return (
                "input",
                self._state_site_incarnation(value, "input.key", value.key),
                value.key,
                self._state_site_incarnation(value, "input.eq", value.eq),
                self._policy_definition_payload(value.eq),
                self._state_site_incarnation(value, "input.cutoff", value.cutoff),
                self._policy_definition_payload(value.cutoff),
            )
        if self._is_resource_handle(value):
            return ("resource", self._resource_identity_payload(value))
        if isinstance(value, ModuleType):
            if name == "@pytest_ar" and value.__name__ == "_pytest.assertion.rewrite":
                # Pytest injects this implementation detail into rewritten
                # functions that contain assertions. Its source identity pins
                # the instrumentation without making assertion formatting
                # helpers part of the query's application dependency graph.
                return (
                    "pytest-assertion-rewrite",
                    self._module_identity_payload(value),
                )
            return self._captured_module_payload(
                value,
                capture_name=name,
                owner=owner,
                seen_functions=seen_functions,
            )
        if isinstance(value, FunctionType):
            defining_module = sys.modules.get(value.__module__)
            if defining_module is None:
                raise UnsupportedValueError(
                    f"Function {value.__module__}.{value.__qualname__} has no "
                    "loaded defining module."
                )
            try:
                definition = self._function_definition_payload(value, seen_functions)
            except UnsupportedValueError:
                definition = self._source_pinned_function_payload(value, seen_functions)
            return (
                "function",
                self._module_identity_payload(defining_module),
                self._captured_function_public_surface_payload(
                    value,
                    identity_owner=value,
                    site_prefix="captured-function",
                    seen_functions=seen_functions,
                ),
                definition,
            )
        if isinstance(value, MethodType):
            return self._bound_python_method_payload(
                value,
                capture_name=name,
                owner=owner,
                seen_functions=seen_functions,
            )
        if isinstance(value, BuiltinFunctionType):
            return self._builtin_function_payload(value)
        self._reject_callable_object_wrapper(
            value,
            subject=f"Query capture {name!r}",
        )
        if isinstance(value, type):
            if "<locals>" in value.__qualname__ and self._type_fingerprint_stack.get():
                return self._implementation_type_payload(value)
            return self._type_definition_payload(value)
        try:
            return (
                "value",
                self._freeze_captured_immutable(
                    name,
                    value,
                    seen_functions,
                    owner=owner,
                    active_ids=set(),
                ),
            )
        except UnsupportedValueError as exc:
            raise UnsupportedValueError(
                f"Query {owner.__module__}:{owner.__qualname__} captures unsupported ambient value "
                f"{name!r} of type {type(value).__qualname__}. "
                "Move mutable state behind Input/Resource nodes or use an immutable value. "
                "Run pyinc.explain_query_captures(...) to inspect the capture set before the first db.get()."
            ) from exc

    def _freeze_captured_immutable(
        self,
        name: str,
        value: Any,
        seen_functions: builtins.set[int],
        *,
        owner: object,
        active_ids: builtins.set[int],
    ) -> Any:
        """Encode immutable capture shapes while preserving nested dependencies."""

        return (
            "captured-value-site-v1",
            self._observable_site_incarnation(owner, f"capture-value[{name}]", value),
            self._freeze_captured_immutable_payload(
                name,
                value,
                seen_functions,
                owner=owner,
                active_ids=active_ids,
            ),
        )

    def _freeze_captured_immutable_payload(
        self,
        name: str,
        value: Any,
        seen_functions: builtins.set[int],
        *,
        owner: object,
        active_ids: builtins.set[int],
    ) -> Any:
        """Encode one already-incarnated value and recurse through public state."""

        from .core import Input, Query

        if isinstance(value, type):
            return ("captured-type", self._type_definition_payload(value))
        if isinstance(
            value,
            (
                Query,
                Input,
                ModuleType,
                FunctionType,
                MethodType,
                BuiltinFunctionType,
            ),
        ) or self._is_resource_handle(value):
            if not isinstance(owner, FunctionType):
                raise UnsupportedValueError(
                    "Managed handles and behavior objects are not supported inside "
                    "policy, adapter, or resource configuration state."
                )
            return (
                "captured-dependency",
                self._captured_dependency_digest(
                    name,
                    value,
                    seen_functions,
                    owner=owner,
                ),
            )
        self._reject_callable_object_wrapper(
            value,
            subject=f"Nested query capture {name!r}",
        )
        if type(value) is inspect.Signature:
            with self._capture_guard(value, active_ids):

                def signature_value(label: str, item: Any) -> Any:
                    if item is inspect.Signature.empty:
                        return ("empty",)
                    return self._freeze_captured_immutable(
                        f"{name}.{label}",
                        item,
                        seen_functions,
                        owner=owner,
                        active_ids=active_ids,
                    )

                return (
                    "capture-inspect-signature-v1",
                    tuple(
                        (
                            parameter.name,
                            int(parameter.kind),
                            signature_value(
                                f"parameter[{parameter.name}].default",
                                parameter.default,
                            ),
                            signature_value(
                                f"parameter[{parameter.name}].annotation",
                                parameter.annotation,
                            ),
                        )
                        for parameter in value.parameters.values()
                    ),
                    signature_value("return_annotation", value.return_annotation),
                )
        if isinstance(value, slice):
            return (
                "capture-slice",
                self._freeze_captured_immutable(
                    f"{name}.start",
                    value.start,
                    seen_functions,
                    owner=owner,
                    active_ids=active_ids,
                ),
                self._freeze_captured_immutable(
                    f"{name}.stop",
                    value.stop,
                    seen_functions,
                    owner=owner,
                    active_ids=active_ids,
                ),
                self._freeze_captured_immutable(
                    f"{name}.step",
                    value.step,
                    seen_functions,
                    owner=owner,
                    active_ids=active_ids,
                ),
            )
        if isinstance(value, tuple):
            with self._capture_guard(value, active_ids):
                items = tuple(
                    self._freeze_captured_immutable(
                        f"{name}[{index}]",
                        item,
                        seen_functions,
                        owner=owner,
                        active_ids=active_ids,
                    )
                    for index, item in enumerate(value)
                )
                if type(value) is tuple:
                    return ("capture-tuple", items)
                return (
                    "capture-tuple-subclass",
                    self._type_definition_payload(type(value)),
                    items,
                    self._captured_instance_dict_payload(
                        name,
                        value,
                        seen_functions,
                        owner=owner,
                        active_ids=active_ids,
                    ),
                )
        if isinstance(value, frozenset):
            with self._capture_guard(value, active_ids):
                items = tuple(
                    self._freeze_captured_immutable(
                        f"{name}[member:{id(item)}]",
                        item,
                        seen_functions,
                        owner=owner,
                        active_ids=active_ids,
                    )
                    for item in value
                )
                ordered = tuple(sorted(items, key=fingerprint_snapshot))
                if type(value) is frozenset:
                    return ("capture-frozenset", ordered)
                return (
                    "capture-frozenset-subclass",
                    self._type_definition_payload(type(value)),
                    ordered,
                    self._captured_instance_dict_payload(
                        name,
                        value,
                        seen_functions,
                        owner=owner,
                        active_ids=active_ids,
                    ),
                )
        if type(value) is FrozenList:
            with self._capture_guard(value, active_ids):
                return (
                    "capture-frozen-list",
                    tuple(
                        self._freeze_captured_immutable(
                            f"{name}.items[{index}]",
                            item,
                            seen_functions,
                            owner=owner,
                            active_ids=active_ids,
                        )
                        for index, item in enumerate(value.items)
                    ),
                )
        if type(value) is FrozenDict:
            with self._capture_guard(value, active_ids):
                return (
                    "capture-frozen-dict",
                    tuple(
                        (
                            self._freeze_captured_immutable(
                                f"{name}.entries[{index}].key",
                                key,
                                seen_functions,
                                owner=owner,
                                active_ids=active_ids,
                            ),
                            self._freeze_captured_immutable(
                                f"{name}.entries[{index}].value",
                                item,
                                seen_functions,
                                owner=owner,
                                active_ids=active_ids,
                            ),
                        )
                        for index, (key, item) in enumerate(value.entries)
                    ),
                )
        if type(value) is FrozenSet:
            with self._capture_guard(value, active_ids):
                return (
                    "capture-frozen-set",
                    value.kind,
                    tuple(
                        self._freeze_captured_immutable(
                            f"{name}.items[{index}]",
                            item,
                            seen_functions,
                            owner=owner,
                            active_ids=active_ids,
                        )
                        for index, item in enumerate(value.items)
                    ),
                )
        if type(value) is FrozenRecord:
            with self._capture_guard(value, active_ids):
                return (
                    "capture-frozen-record",
                    value.type_name,
                    tuple(
                        (
                            field_name,
                            self._freeze_captured_immutable(
                                f"{name}.entries[{index}].{field_name}",
                                item,
                                seen_functions,
                                owner=owner,
                                active_ids=active_ids,
                            ),
                        )
                        for index, (field_name, item) in enumerate(value.entries)
                    ),
                )
        if type(value) is FrozenAdapterValue:
            with self._capture_guard(value, active_ids):
                return (
                    "capture-frozen-adapter-value",
                    value.adapter_key,
                    self._freeze_captured_immutable(
                        f"{name}.payload",
                        value.payload,
                        seen_functions,
                        owner=owner,
                        active_ids=active_ids,
                    ),
                )
        if type(value) is FrozenRef:
            return ("capture-frozen-ref", value.index)
        if type(value) is FrozenGraph:
            with self._capture_guard(value, active_ids):
                return (
                    "capture-frozen-graph",
                    tuple(
                        self._freeze_captured_immutable(
                            f"{name}.nodes[{index}]",
                            item,
                            seen_functions,
                            owner=owner,
                            active_ids=active_ids,
                        )
                        for index, item in enumerate(value.nodes)
                    ),
                    self._freeze_captured_immutable(
                        f"{name}.root",
                        value.root,
                        seen_functions,
                        owner=owner,
                        active_ids=active_ids,
                    ),
                )
        if is_dataclass(value) and not isinstance(value, type):
            params = getattr(type(value), "__dataclass_params__", None)
            if params is None or not params.frozen:
                raise UnsupportedValueError(
                    "Mutable dataclass values cannot be captured ambiently."
                )
            field_names = {item.name for item in fields(value)}
            unsupported_slots = self._instance_slots(type(value)) - field_names
            if unsupported_slots:
                raise UnsupportedValueError(
                    f"Frozen dataclass {type(value).__module__}."
                    f"{type(value).__qualname__} has non-field slot state that "
                    "cannot be fingerprinted safely."
                )
            with self._capture_guard(value, active_ids):
                field_payload = tuple(
                    (
                        item.name,
                        self._freeze_captured_immutable(
                            f"{name}.{item.name}",
                            object.__getattribute__(value, item.name),
                            seen_functions,
                            owner=owner,
                            active_ids=active_ids,
                        ),
                    )
                    for item in fields(value)
                )
                extra_state = tuple(
                    (
                        state_name,
                        self._freeze_captured_immutable(
                            f"{name}.{state_name}",
                            item,
                            seen_functions,
                            owner=owner,
                            active_ids=active_ids,
                        ),
                    )
                    for state_name, item in self._static_instance_dict(value).items()
                    if state_name not in field_names
                )
                return (
                    "capture-frozen-dataclass",
                    self._type_definition_payload(type(value)),
                    self._definition_object_incarnation(value),
                    field_payload,
                    extra_state,
                )
        return self._freeze_static_capture(value, active_ids)

    def _captured_instance_dict_payload(
        self,
        name: str,
        value: Any,
        seen_functions: builtins.set[int],
        *,
        owner: object,
        active_ids: builtins.set[int],
    ) -> Any:
        slots = self._instance_slots(type(value))
        if slots:
            raise UnsupportedValueError(
                f"Ambient capture {type(value).__module__}."
                f"{type(value).__qualname__} uses slot state that cannot be "
                "fingerprinted safely."
            )
        return tuple(
            (
                state_name,
                self._freeze_captured_immutable(
                    f"{name}.{state_name}",
                    item,
                    seen_functions,
                    owner=owner,
                    active_ids=active_ids,
                ),
            )
            for state_name, item in self._static_instance_dict(value).items()
        )

    def _bound_python_method_payload(
        self,
        method: MethodType,
        *,
        capture_name: str,
        owner: FunctionType,
        seen_functions: builtins.set[int],
    ) -> Any:
        function = method.__func__
        if not isinstance(function, FunctionType):
            raise UnsupportedValueError(
                f"Bound method capture {capture_name!r} has a non-Python function."
            )
        bound_owner = method.__self__
        if isinstance(bound_owner, ModuleType):
            owner_payload: Any = self._captured_module_payload(
                bound_owner,
                capture_name=f"{capture_name}.__self__",
                owner=owner,
                seen_functions=seen_functions,
            )
        elif isinstance(bound_owner, type):
            owner_payload = (
                "type",
                self._implementation_type_payload(bound_owner),
            )
        else:
            owner_payload = (
                "instance",
                self._implementation_type_payload(type(bound_owner)),
                self._freeze_captured_immutable(
                    f"{capture_name}.__self__",
                    bound_owner,
                    seen_functions,
                    owner=owner,
                    active_ids=set(),
                ),
            )
        try:
            definition = self._function_definition_payload(function, seen_functions)
        except UnsupportedValueError:
            definition = self._source_pinned_function_payload(function, seen_functions)
        return (
            "bound-python-method",
            definition,
            owner_payload,
        )

    def _builtin_function_payload(self, function: BuiltinFunctionType) -> Any:
        owner = getattr(function, "__self__", None)
        if owner is None:
            owner_payload: Any = ("none",)
        elif isinstance(owner, ModuleType):
            owner_payload = (
                "module",
                owner.__name__,
                self._module_identity_payload(owner),
            )
        elif isinstance(owner, type):
            owner_payload = ("type", self._type_definition_payload(owner))
        else:
            owner_payload = ("value", self._freeze_static_capture(owner, set()))
        return (
            "builtin",
            function.__module__,
            function.__qualname__,
            owner_payload,
        )

    def _type_definition_payload(self, value: type[Any]) -> Any:
        if value.__module__ == "builtins":
            return ("builtin-type", value.__module__, value.__qualname__)
        if "<locals>" in value.__qualname__:
            raise UnsupportedValueError(
                f"Captured local type {value.__module__}.{value.__qualname__} "
                "cannot be fingerprinted safely. Define it at module scope or "
                "move its behavior behind an Input or Resource."
            )
        if value.__module__.partition(".")[0] in sys.stdlib_module_names:
            return (
                "stdlib-type-v3",
                self._runtime_build_payload(),
                self._module_type_anchor_payload(value),
            )
        return self._local_implementation_type_payload(value, set())

    def _implementation_dependency_type_payload(
        self, value: type[Any], seen_types: builtins.set[int]
    ) -> Any:
        if value is ValueAdapter:
            # Concrete adapters may explicitly inherit the public Protocol.
            # Its typing machinery is not adapter behavior and contains
            # runtime-owned mutable bookkeeping that cannot be frozen.  The
            # protocol identity and the common build payload pin that base;
            # the concrete type and its hooks are fingerprinted in full.
            return (
                "value-adapter-protocol-v1",
                value.__module__,
                value.__qualname__,
                self._runtime_build_payload(),
            )
        if value.__module__ == "builtins":
            return ("builtin-type", value.__module__, value.__qualname__)
        if value.__module__.partition(".")[0] in sys.stdlib_module_names:
            return (
                "stdlib-type-v3",
                self._runtime_build_payload(),
                self._module_type_anchor_payload(value),
            )
        return self._local_implementation_type_payload(value, seen_types)

    def _module_type_anchor_payload(self, value: type[Any]) -> Any:
        module = sys.modules.get(value.__module__)
        if module is None:
            raise UnsupportedValueError(
                f"Captured type {value.__module__}.{value.__qualname__} has no "
                "loaded defining module."
            )
        current: Any = vars(module).get(value.__qualname__.split(".", 1)[0])
        for part in value.__qualname__.split(".")[1:]:
            if not isinstance(current, type):
                current = None
                break
            current = vars(current).get(part)
        if current is not value:
            raise UnsupportedValueError(
                f"Captured type {value.__module__}.{value.__qualname__} is not "
                "the live module binding and cannot be fingerprinted safely."
            )
        return (
            "module-type-anchor",
            value.__module__,
            value.__qualname__,
            self._module_identity_payload(module),
        )

    def _implementation_type_payload(self, value: type[Any]) -> Any:
        """Pin a behavior-bearing type, including factory-local implementations."""

        if value.__module__ == "builtins":
            return ("builtin-type", value.__module__, value.__qualname__)
        return self._local_implementation_type_payload(value, set())

    def _local_implementation_type_payload(
        self, value: type[Any], seen_types: builtins.set[int]
    ) -> Any:
        type_id = id(value)
        fingerprint_stack = self._type_fingerprint_stack.get()
        if type_id in fingerprint_stack:
            return ("recursive-type", value.__module__, value.__qualname__)
        if type_id in seen_types:
            return ("recursive-type", value.__module__, value.__qualname__)
        if value.__module__ == "builtins":
            return ("builtin-type", value.__module__, value.__qualname__)
        seen_types.add(type_id)
        stack_token = self._type_fingerprint_stack.set(fingerprint_stack + (type_id,))
        try:
            is_local = "<locals>" in value.__qualname__
            namespace = vars(value)
            dataclass_generated_names = (
                {
                    "__init__",
                    "__repr__",
                    "__eq__",
                    "__setattr__",
                    "__delattr__",
                    "__hash__",
                    "__replace__",
                }
                if "__dataclass_fields__" in namespace
                else set()
            )

            def generated_dataclass_method(name: str, attribute: Any) -> bool:
                if name not in dataclass_generated_names or not isinstance(attribute, FunctionType):
                    return False
                wrapped = getattr(attribute, "__wrapped__", None)
                return (
                    attribute.__code__.co_filename == "<string>"
                    or (
                        isinstance(wrapped, FunctionType)
                        and wrapped.__code__.co_filename == "<string>"
                    )
                    or (name == "__replace__" and attribute.__module__ == "dataclasses")
                )

            functions: list[FunctionType] = []
            for name, attribute in namespace.items():
                if generated_dataclass_method(name, attribute):
                    continue
                if isinstance(attribute, FunctionType):
                    functions.append(attribute)
                elif isinstance(attribute, (staticmethod, classmethod)):
                    descriptor_function = attribute.__func__
                    if not isinstance(descriptor_function, FunctionType):
                        raise UnsupportedValueError(
                            f"Local implementation {value.__module__}."
                            f"{value.__qualname__} has a non-Python descriptor "
                            f"function {name!r}."
                        )
                    functions.append(descriptor_function)
                elif isinstance(attribute, property):
                    for property_function in (
                        attribute.fget,
                        attribute.fset,
                        attribute.fdel,
                    ):
                        if property_function is None:
                            continue
                        if not isinstance(property_function, FunctionType):
                            raise UnsupportedValueError(
                                f"Local implementation {value.__module__}."
                                f"{value.__qualname__} has a non-Python property "
                                f"function {name!r}."
                            )
                        functions.append(property_function)
            referenced_names = {
                name
                for function in functions
                for code in self._walk_code_objects(function.__code__)
                for name in code.co_names
            }
            automatic = {
                "__dict__",
                "__weakref__",
                "__annotations__",
                "__annotate_func__",
                "__dataclass_fields__",
                "__dataclass_params__",
                "__annotations_cache__",
                "__orig_bases__",
                "__parameters__",
            }
            attributes: list[tuple[str, Any, Any]] = []
            for name, attribute in namespace.items():
                payload: Any
                if name in {"__module__", "__qualname__"}:
                    continue
                if generated_dataclass_method(name, attribute):
                    continue
                if name in automatic:
                    continue
                if isinstance(attribute, FunctionType):
                    payload = (
                        "function",
                        self._state_site_incarnation(
                            value, f"type-attribute[{name}].__code__", attribute.__code__
                        ),
                        self._captured_function_public_surface_payload(
                            attribute,
                            identity_owner=value,
                            site_prefix=f"type-attribute[{name}]",
                            seen_functions=set(),
                        ),
                        self._function_definition_payload(attribute, set()),
                    )
                elif isinstance(attribute, (staticmethod, classmethod)):
                    descriptor_function = attribute.__func__
                    if not isinstance(descriptor_function, FunctionType):
                        raise UnsupportedValueError(
                            f"Local implementation {value.__module__}."
                            f"{value.__qualname__} has a non-Python descriptor "
                            f"function {name!r}."
                        )
                    payload = (
                        type(attribute).__name__,
                        self._state_site_incarnation(
                            value,
                            f"type-attribute[{name}].__func__.__code__",
                            descriptor_function.__code__,
                        ),
                        self._captured_function_public_surface_payload(
                            descriptor_function,
                            identity_owner=value,
                            site_prefix=f"type-attribute[{name}].__func__",
                            seen_functions=set(),
                        ),
                        self._function_definition_payload(descriptor_function, set()),
                    )
                elif isinstance(attribute, property):
                    payload = (
                        "property",
                        tuple(
                            (
                                label,
                                self._captured_function_public_surface_payload(
                                    cast(FunctionType, function),
                                    identity_owner=value,
                                    site_prefix=f"type-attribute[{name}].{label}",
                                    seen_functions=set(),
                                )
                                if function is not None
                                else None,
                                self._function_definition_payload(
                                    cast(FunctionType, function), set()
                                )
                                if function is not None
                                else None,
                            )
                            for label, function in (
                                ("get", attribute.fget),
                                ("set", attribute.fset),
                                ("delete", attribute.fdel),
                            )
                        ),
                    )
                elif isinstance(attribute, type):
                    payload = (
                        "nested-type",
                        self._implementation_dependency_type_payload(attribute, seen_types),
                    )
                elif isinstance(attribute, (MemberDescriptorType, GetSetDescriptorType)):
                    payload = (
                        "descriptor",
                        type(attribute).__module__,
                        type(attribute).__qualname__,
                        name,
                    )
                else:
                    try:
                        payload = (
                            "value",
                            self._freeze_captured_immutable(
                                f"type-attribute[{name}]",
                                attribute,
                                set(),
                                owner=value,
                                active_ids=set(),
                            ),
                        )
                    except UnsupportedValueError:
                        if is_local or name in referenced_names:
                            raise
                        continue
                attributes.append(
                    (
                        name,
                        self._state_site_incarnation(value, f"type-attribute[{name}]", attribute),
                        payload,
                    )
                )
            try:
                public_annotations = value.__annotations__
            except Exception as exc:
                raise UnsupportedValueError(
                    f"Type {value.__module__}.{value.__qualname__} annotations "
                    "cannot be fingerprinted safely."
                ) from exc
            if type(public_annotations) is not dict or any(
                type(name) is not str for name in public_annotations
            ):
                raise UnsupportedValueError(
                    f"Type {value.__module__}.{value.__qualname__} must use an exact "
                    "dict with exact str keys for __annotations__."
                )
            annotation_evaluator = namespace.get("__annotate_func__")
            evaluator_identity = (
                (
                    self._captured_function_public_surface_payload(
                        annotation_evaluator,
                        identity_owner=value,
                        site_prefix="type.__annotate_func__",
                        seen_functions=set(),
                    )
                )
                if isinstance(annotation_evaluator, FunctionType)
                else None
            )
            annotation_identity = (
                self._state_site_incarnation(value, "type.__annotations__", public_annotations),
                evaluator_identity,
                tuple(
                    (
                        name,
                        self._state_site_incarnation(
                            value, f"type-annotations[{name}]", annotation
                        ),
                        self._freeze_captured_immutable(
                            f"type-annotations[{name}]",
                            annotation,
                            set(),
                            owner=value,
                            active_ids=set(),
                        ),
                    )
                    for name, annotation in public_annotations.items()
                ),
            )
            namespace_identity = tuple(
                (
                    name,
                    self._state_site_incarnation(value, f"type-namespace[{name}]", attribute),
                )
                for name, attribute in namespace.items()
            )
            generated_method_identity = tuple(
                (
                    name,
                    self._state_site_incarnation(
                        value, f"generated-type-method[{name}]", attribute
                    ),
                    self._captured_function_public_surface_payload(
                        attribute,
                        identity_owner=value,
                        site_prefix=f"generated-type-method[{name}]",
                        seen_functions=set(),
                    ),
                )
                for name, attribute in namespace.items()
                if generated_dataclass_method(name, attribute)
            )
            return (
                "implementation-type-v3",
                value.__module__,
                value.__qualname__,
                (
                    "local-type-anchor",
                    value.__module__,
                    value.__qualname__,
                )
                if "<locals>" in value.__qualname__
                else self._module_type_anchor_payload(value),
                self._implementation_dependency_type_payload(type(value), seen_types),
                tuple(
                    self._implementation_dependency_type_payload(base, seen_types)
                    for base in value.__bases__
                ),
                self._local_dataclass_behavior_payload(value),
                namespace_identity,
                annotation_identity,
                generated_method_identity,
                tuple(attributes),
            )
        finally:
            self._type_fingerprint_stack.reset(stack_token)
            seen_types.remove(type_id)

    def _local_dataclass_behavior_payload(self, value: type[Any]) -> Any:
        params = getattr(value, "__dataclass_params__", None)
        if params is None:
            return None
        dataclass_fields = vars(value).get("__dataclass_fields__")
        if type(dataclass_fields) is not dict or any(
            type(name) is not str for name in dataclass_fields
        ):
            raise UnsupportedValueError(
                f"Dataclass {value.__module__}.{value.__qualname__} must use an "
                "exact __dataclass_fields__ dict with exact str keys."
            )
        if any(type(item) is not DataclassField for item in dataclass_fields.values()):
            raise UnsupportedValueError(
                f"Dataclass {value.__module__}.{value.__qualname__} has an invalid Field object."
            )

        def public_value(site: str, item: Any) -> Any:
            if item is MISSING:
                return ("missing",)
            if isinstance(item, FunctionType):
                return (
                    "function",
                    self._captured_function_public_surface_payload(
                        item,
                        identity_owner=value,
                        site_prefix=site,
                        seen_functions=set(),
                    ),
                    self._function_definition_payload(item, set()),
                )
            if isinstance(item, BuiltinFunctionType):
                return ("builtin", self._builtin_function_payload(item))
            return self._freeze_captured_immutable(
                site,
                item,
                set(),
                owner=value,
                active_ids=set(),
            )

        parameter_names = (
            "init",
            "repr",
            "eq",
            "order",
            "unsafe_hash",
            "frozen",
            "match_args",
            "kw_only",
            "slots",
            "weakref_slot",
        )
        parameters = tuple(
            (
                name,
                (
                    self._state_site_incarnation(
                        value,
                        f"dataclass-params.{name}",
                        parameter_value,
                    ),
                    public_value(f"dataclass-params.{name}", parameter_value),
                )
                if (parameter_value := getattr(params, name, MISSING)) is not MISSING
                else ("missing",),
            )
            for name in parameter_names
        )
        field_payloads: list[Any] = []
        for field_name, item in dataclass_fields.items():
            if type(item.name) is not str or item.name != field_name:
                raise UnsupportedValueError(
                    f"Dataclass {value.__module__}.{value.__qualname__} has an "
                    "inconsistent Field name."
                )
            metadata = item.metadata
            if type(metadata) not in {dict, MappingProxyType}:
                raise UnsupportedValueError(
                    f"Dataclass {value.__module__}.{value.__qualname__} Field "
                    f"{field_name!r} must use an exact dict or mappingproxy for metadata."
                )
            public_attributes = tuple(
                (
                    attribute_name,
                    self._state_site_incarnation(
                        value,
                        f"dataclass-field[{field_name}].{attribute_name}",
                        attribute_value,
                    ),
                    public_value(
                        f"dataclass-field[{field_name}].{attribute_name}",
                        attribute_value,
                    ),
                )
                for attribute_name in (
                    "name",
                    "type",
                    "default",
                    "default_factory",
                    "init",
                    "repr",
                    "hash",
                    "compare",
                    "kw_only",
                    "doc",
                )
                if (attribute_value := getattr(item, attribute_name, MISSING)) is not MISSING
            )
            metadata_identity = (
                self._state_site_incarnation(
                    value,
                    f"dataclass-field[{field_name}].metadata",
                    metadata,
                ),
                tuple(
                    (
                        public_value(
                            f"dataclass-field[{field_name}].metadata[{index}].key",
                            key,
                        ),
                        public_value(
                            f"dataclass-field[{field_name}].metadata[{index}].value",
                            metadata_value,
                        ),
                    )
                    for index, (key, metadata_value) in enumerate(metadata.items())
                ),
            )
            field_payloads.append(
                (
                    item.name,
                    self._state_site_incarnation(
                        value,
                        f"dataclass-field[{field_name}]",
                        item,
                    ),
                    (
                        self._state_site_incarnation(
                            value,
                            f"dataclass-field[{field_name}]._field_type",
                            item._field_type,
                        ),
                        type(item._field_type).__module__,
                        type(item._field_type).__qualname__,
                        getattr(item._field_type, "name", None),
                    ),
                    public_attributes,
                    metadata_identity,
                    getattr(getattr(item, "_field_type", None), "name", None),
                    bool(item.init),
                    bool(item.repr),
                    item.hash,
                    bool(item.compare),
                    item.kw_only,
                    self._freeze_annotation_capture(item.type, set()),
                    freeze(dict(metadata), adapters=self._adapters),
                    self._resource_configuration_type_payload(dict(metadata)),
                    item.doc if hasattr(item, "doc") else ("missing",),
                    self._dataclass_default_payload(item.default),
                    self._dataclass_default_factory_payload(item.default_factory),
                )
            )
        return (
            "dataclass-behavior-v4",
            self._state_site_incarnation(value, "__dataclass_params__", params),
            self._state_site_incarnation(
                value,
                "__dataclass_fields__",
                dataclass_fields,
            ),
            parameters,
            tuple(field_payloads),
        )

    def _dataclass_default_payload(self, value: Any) -> Any:
        if value is MISSING:
            return ("missing",)
        return ("value", self._freeze_static_capture(value, set()))

    def _dataclass_default_factory_payload(self, factory: Any) -> Any:
        if factory is MISSING:
            return ("missing",)
        if isinstance(factory, FunctionType):
            return ("function", self._function_definition_payload(factory, set()))
        if isinstance(factory, BuiltinFunctionType):
            return self._builtin_function_payload(factory)
        if isinstance(factory, type):
            return ("type", self._implementation_type_payload(factory))
        if callable(factory):
            return ("callable", self._policy_definition_payload(factory))
        raise UnsupportedValueError(f"Dataclass default factory {factory!r} is not callable.")

    @staticmethod
    def _walk_code_objects(code: CodeType) -> tuple[CodeType, ...]:
        nested = tuple(item for item in code.co_consts if isinstance(item, CodeType))
        return (code, *(child for item in nested for child in Database._walk_code_objects(item)))

    def _collect_pinned_captures(
        self, fn: FunctionType
    ) -> tuple[builtins.set[str], builtins.set[str]]:
        """Collect the code-pinned query_ids and resource identities of *fn*.

        A thin view over :meth:`_collect_pinned_capture_objects`: the query set
        drives the warm-time gate (a dep query outside it was reached via a
        runtime import / dynamic dispatch and must not be served stale); the
        resource set is the identity space the resource gate resolves against.
        """
        query_objects, resource_objects = self._collect_pinned_capture_objects(fn)
        return builtins.set(query_objects), builtins.set(resource_objects)

    def _collect_pinned_capture_objects(
        self, fn: FunctionType
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Collect the code-pinned query and resource *objects* reachable from *fn*.

        Walks the same capture set as ``_function_definition_payload``
        (defaults, kwdefaults, closure nonlocals, globals), recursing through
        captured functions, bound methods, queries, and immutable container
        shapes. Returns ``(query_id -> Query object, resource identity ->
        resource object)``; a query or resource reached only via a runtime import
        or dynamic dispatch is *not* captured and never appears here. The maps
        let the warm path re-run a pinned leaf (execute-to-verify) and re-probe a
        pinned resource (probe-hint) by their manifest identities.
        """
        from .core import Input, Query

        query_objects: dict[str, Any] = {}
        resource_objects: dict[str, Any] = {}
        seen_functions: set[int] = set()
        seen_values: set[int] = set()

        def walk_function(target: FunctionType) -> None:
            fn_id = id(target)
            if fn_id in seen_functions:
                return
            seen_functions.add(fn_id)
            closure_vars = inspect.getclosurevars(target)
            values: list[Any] = list(target.__defaults__ or ())
            values.extend((target.__kwdefaults__ or {}).values())
            values.extend(closure_vars.nonlocals.values())
            values.extend(closure_vars.globals.values())
            values.extend(vars(target).values())
            for value in values:
                walk_value(value)

        def walk_value(value: Any) -> None:
            if isinstance(value, Query):
                query_objects.setdefault(value.key, value)
                walk_function(cast(FunctionType, value.fn))
            elif isinstance(value, Input):
                return
            elif self._is_resource_handle(value):
                identity = self._fingerprint_identity_payload(
                    self._resource_identity_payload(value),
                    subject=(
                        f"Resource {type(value).__module__}.{type(value).__qualname__} identity"
                    ),
                )
                resource_objects.setdefault(
                    f"{type(value).__module__}:{type(value).__qualname__}:{identity}",
                    value,
                )
            elif isinstance(value, FunctionType):
                walk_function(value)
            elif isinstance(value, MethodType):
                function = value.__func__
                if isinstance(function, FunctionType):
                    walk_function(function)
                walk_value(value.__self__)
            elif isinstance(value, slice):
                for item in (value.start, value.stop, value.step):
                    walk_value(item)
            elif isinstance(value, (tuple, frozenset)):
                value_id = id(value)
                if value_id in seen_values:
                    return
                seen_values.add(value_id)
                for item in value:
                    walk_value(item)
                if type(value) not in {tuple, frozenset}:
                    for item in self._static_instance_dict(value).values():
                        walk_value(item)
            elif is_dataclass(value) and not isinstance(value, type):
                params = getattr(type(value), "__dataclass_params__", None)
                if params is None or not params.frozen:
                    return
                value_id = id(value)
                if value_id in seen_values:
                    return
                seen_values.add(value_id)
                field_names = {item.name for item in fields(value)}
                for item in fields(value):
                    walk_value(object.__getattribute__(value, item.name))
                for state_name, item in self._static_instance_dict(value).items():
                    if state_name not in field_names:
                        walk_value(item)
            else:
                self._reject_callable_object_wrapper(
                    value,
                    subject="Checkpoint query capture",
                )

        walk_function(fn)
        return query_objects, resource_objects

    def _captured_module_payload(
        self,
        module: ModuleType,
        *,
        capture_name: str,
        owner: FunctionType,
        seen_functions: builtins.set[int],
    ) -> Any:
        """Pin the statically accessed behavior behind a captured module."""

        base_identity = self._module_identity_payload(module)
        paths, dynamic = self._module_access_paths(owner, capture_name)
        if module.__name__.partition(".")[0] in sys.stdlib_module_names:
            return (
                "captured-stdlib-module-v3",
                module.__name__,
                base_identity,
                paths,
                dynamic,
            )
        if dynamic or not paths:
            raise UnsupportedValueError(
                f"Query {owner.__module__}:{owner.__qualname__} uses captured "
                f"module {capture_name!r} dynamically. Access module attributes "
                "directly so their behavior can be fingerprinted."
            )

        module_id = id(module)
        stack = self._module_capture_stack.get()
        if module_id in stack:
            return (
                "recursive-captured-module",
                module.__name__,
                base_identity,
                paths,
            )
        token = self._module_capture_stack.set(stack + (module_id,))
        try:
            return (
                "captured-module-v3",
                module.__name__,
                base_identity,
                tuple(
                    self._captured_module_path_payload(module, path, seen_functions)
                    for path in paths
                ),
            )
        finally:
            self._module_capture_stack.reset(token)

    def _module_access_paths(
        self, owner: FunctionType, capture_name: str
    ) -> tuple[tuple[tuple[str, ...], ...], bool]:
        paths: builtins.set[tuple[str, ...]] = builtins.set()
        dynamic = False
        for code in self._walk_code_objects(owner.__code__):
            instructions = tuple(dis.get_instructions(code))
            for index, instruction in enumerate(instructions):
                if (
                    instruction.opname
                    not in {
                        "LOAD_DEREF",
                        "LOAD_GLOBAL",
                        "LOAD_NAME",
                    }
                    or instruction.argval != capture_name
                ):
                    continue
                path: list[str] = []
                cursor = index + 1
                while cursor < len(instructions) and instructions[cursor].opname in {
                    "LOAD_ATTR",
                    "LOAD_METHOD",
                }:
                    attribute = instructions[cursor].argval
                    if not isinstance(attribute, str):
                        dynamic = True
                        break
                    path.append(attribute)
                    cursor += 1
                if path:
                    paths.add(tuple(path))
                else:
                    dynamic = True
        return tuple(sorted(paths)), dynamic

    def _captured_module_path_payload(
        self,
        module: ModuleType,
        path: tuple[str, ...],
        seen_functions: builtins.set[int],
    ) -> Any:
        current: Any = module
        steps: list[Any] = []
        for index, attribute_name in enumerate(path):
            if not isinstance(current, ModuleType):
                return (
                    tuple(steps),
                    self._module_attribute_payload(current, seen_functions),
                    ("remaining-attributes", path[index:]),
                )
            namespace = vars(current)
            if attribute_name not in namespace:
                raise UnsupportedValueError(
                    f"Captured module {current.__name__!r} has no static "
                    f"attribute {attribute_name!r}."
                )
            steps.append(
                (
                    "module-attribute",
                    current.__name__,
                    self._module_identity_payload(current),
                    attribute_name,
                )
            )
            current = namespace[attribute_name]
        return (
            tuple(steps),
            self._module_attribute_payload(current, seen_functions),
        )

    def _module_attribute_payload(self, value: Any, seen_functions: builtins.set[int]) -> Any:
        from .core import Input, Query

        if isinstance(value, Query):
            return (
                "query-v2",
                self._query_handle_payload(value),
                self._captured_query_public_surface_payload(value, seen_functions),
                self._function_definition_payload(cast(FunctionType, value.fn), seen_functions),
                self._policy_definition_payload(value.eq),
                self._policy_definition_payload(value.cutoff),
            )
        if isinstance(value, Input):
            return (
                "input",
                self._state_site_incarnation(value, "input.key", value.key),
                value.key,
                self._state_site_incarnation(value, "input.eq", value.eq),
                self._policy_definition_payload(value.eq),
                self._state_site_incarnation(value, "input.cutoff", value.cutoff),
                self._policy_definition_payload(value.cutoff),
            )
        if isinstance(value, ModuleType):
            return (
                "module",
                value.__name__,
                self._module_identity_payload(value),
            )
        if isinstance(value, FunctionType):
            defining_module = sys.modules.get(value.__module__)
            if defining_module is None:
                raise UnsupportedValueError(
                    f"Function {value.__module__}.{value.__qualname__} has no "
                    "loaded defining module."
                )
            try:
                definition = self._function_definition_payload(value, seen_functions)
            except UnsupportedValueError:
                definition = self._source_pinned_function_payload(value, seen_functions)
            return (
                "function",
                self._module_identity_payload(defining_module),
                self._captured_function_public_surface_payload(
                    value,
                    identity_owner=value,
                    site_prefix="captured-module-function",
                    seen_functions=seen_functions,
                ),
                definition,
            )
        if isinstance(value, BuiltinFunctionType):
            return self._builtin_function_payload(value)
        if isinstance(value, type):
            return ("type", self._type_definition_payload(value))
        if self._is_resource_handle(value):
            return ("resource", self._resource_identity_payload(value))
        try:
            return ("value", self._freeze_static_capture(value, set()))
        except UnsupportedValueError as exc:
            raise UnsupportedValueError(
                f"Captured module attribute of type {type(value).__module__}."
                f"{type(value).__qualname__} cannot be fingerprinted safely."
            ) from exc

    def _source_pinned_function_payload(
        self, function: FunctionType, seen_functions: builtins.set[int]
    ) -> Any:
        """Pin a module attribute whose unrelated ambient globals are mutable."""

        defining_module = sys.modules.get(function.__module__)
        if defining_module is None:
            raise UnsupportedValueError(
                f"Function {function.__module__}.{function.__qualname__} has no "
                "loaded defining module."
            )
        closure_vars = inspect.getclosurevars(function)
        return (
            "source-pinned-function-v3",
            function.__module__,
            function.__qualname__,
            self._module_identity_payload(defining_module),
            self._code_definition_payload(function.__code__),
            tuple(
                (
                    self._definition_access_incarnation(function, f"default[{index}]", item),
                    self._captured_dependency_digest(
                        f"default[{index}]",
                        item,
                        seen_functions,
                        owner=function,
                    ),
                )
                for index, item in enumerate(function.__defaults__ or ())
            ),
            tuple(
                (
                    name,
                    (
                        self._definition_access_incarnation(function, f"kwdefault[{name}]", item),
                        self._captured_dependency_digest(
                            f"kwdefault[{name}]",
                            item,
                            seen_functions,
                            owner=function,
                        ),
                    ),
                )
                for name, item in (function.__kwdefaults__ or {}).items()
            ),
            tuple(
                (
                    name,
                    self._captured_dependency_digest(name, item, seen_functions, owner=function),
                )
                for name, item in sorted(closure_vars.nonlocals.items())
            ),
            tuple(
                (
                    name,
                    self._source_pinned_global_payload(
                        name,
                        item,
                        function=function,
                        seen_functions=seen_functions,
                    ),
                )
                for name, item in sorted(closure_vars.globals.items())
            ),
            self._function_metadata_payload(function, seen_functions),
        )

    def _source_pinned_global_payload(
        self,
        name: str,
        value: Any,
        *,
        function: FunctionType,
        seen_functions: builtins.set[int],
    ) -> Any:
        try:
            return self._captured_dependency_digest(
                name,
                value,
                seen_functions,
                owner=function,
            )
        except UnsupportedValueError:
            if isinstance(value, type) and "<locals>" not in value.__qualname__:
                return self._source_pinned_type_payload(value)
            if type(value) not in {dict, list, set}:
                raise UnsupportedValueError(
                    f"Source-pinned function {function.__module__}."
                    f"{function.__qualname__} has unsupported global {name!r} "
                    f"of type {type(value).__module__}."
                    f"{type(value).__qualname__}."
                ) from None
            try:
                frozen_value = freeze(value)
            except UnsupportedValueError as error:
                raise UnsupportedValueError(
                    f"Source-pinned mutable global {name!r} is not snapshot-safe."
                ) from error
            # Retain snapshot-safe initialized state so a changed source module
            # cannot hide behind a mutable binding.
            return (
                "source-pinned-mutable-module-global",
                type(value).__module__,
                type(value).__qualname__,
                frozen_value,
            )

    def _source_pinned_type_payload(self, value: type[Any]) -> Any:
        anchors: list[Any] = []
        for dependency in (type(value), *value.__mro__):
            if dependency.__module__ == "builtins":
                anchors.append(("builtin", dependency.__module__, dependency.__qualname__))
                continue
            defining_module = sys.modules.get(dependency.__module__)
            if defining_module is None:
                raise UnsupportedValueError(
                    f"Type {dependency.__module__}.{dependency.__qualname__} "
                    "has no loaded defining module."
                )
            anchors.append(
                (
                    dependency.__module__,
                    dependency.__qualname__,
                    self._module_identity_payload(defining_module),
                )
            )
        return (
            "source-pinned-module-type",
            value.__module__,
            value.__qualname__,
            tuple(anchors),
        )

    def _module_identity_payload(self, module: ModuleType) -> Any:
        """Compute a structural digest for a captured module.

        Name-only capture is not sufficient: a third-party version bump or a
        source-file edit changes `module.CONSTANT` without touching the
        module's name, which would silently reuse stale cache entries.
        The payload combines:

        * `__version__` (if the module exposes one — standard for third-party
          packages);
        * a digest of the bytes at `module.__file__`; frozen and built-in
          modules are pinned through the runtime-build identity;
        * a sorted `__all__` tuple when declared, capturing the module's
          publicly promised surface.

        In-process monkey-patch of module attributes is *not* covered and is
        explicitly listed in `docs/kernel-contract.md` as out of scope; users
        relying on such state must route it through `Input` / `Resource`.
        """
        namespace = vars(module)
        module_name = module.__name__
        if sys.modules.get(module_name) is not module:
            raise UnsupportedValueError(
                f"Captured module {module_name!r} is not its live sys.modules binding."
            )
        specification = namespace.get("__spec__")
        if not isinstance(specification, importlib.machinery.ModuleSpec):
            raise UnsupportedValueError(
                f"Captured module {module_name!r} has no trustworthy import "
                "spec or stable source identity."
            )
        specification_name = specification.name
        if specification_name != module_name and (
            sys.modules.get(specification_name) is not module
        ):
            raise UnsupportedValueError(
                f"Captured module {module_name!r} has no trustworthy import "
                "spec or stable source identity."
            )
        import_identity = (module_name, specification_name)
        origin = specification.origin
        loader = cast(Any, specification.loader)
        if origin not in {"built-in", "frozen"} and (
            namespace.get("__loader__") is not loader
            or namespace.get("__package__", object()) != specification.parent
        ):
            raise UnsupportedValueError(
                f"Captured module {module_name!r} has no trustworthy import "
                "metadata or stable source identity."
            )
        if (
            origin not in {"built-in", "frozen"}
            and specification.has_location
            and namespace.get("__cached__") != specification.cached
        ):
            raise UnsupportedValueError(
                f"Captured module {module_name!r} has no trustworthy import "
                "metadata or stable source identity."
            )
        if origin == "built-in":
            if (
                loader is not importlib.machinery.BuiltinImporter
                or specification_name not in sys.builtin_module_names
            ):
                raise UnsupportedValueError(
                    f"Captured module {module_name!r} has a spoofed built-in spec."
                )
        elif origin == "frozen":
            if loader is not importlib.machinery.FrozenImporter:
                raise UnsupportedValueError(
                    f"Captured module {module_name!r} has a spoofed frozen spec."
                )
        elif (
            not isinstance(origin, str)
            or not specification.has_location
            or specification.loader is None
        ):
            raise UnsupportedValueError(
                f"Captured module {module_name!r} has no stable source identity."
            )
        version = namespace.get("__version__")
        if version is None or type(version) in {
            str,
            bytes,
            int,
            float,
            bool,
            complex,
        }:
            version_digest: Any = self._module_constant_payload(version, set())
        else:
            raise UnsupportedValueError(
                f"Captured module {module_name!r} has an unsafe __version__."
            )

        all_attr = namespace.get("__all__")
        if all_attr is None:
            all_tuple = None
        elif isinstance(all_attr, (list, tuple)) and type(all_attr) in {
            list,
            tuple,
        }:
            if any(type(item) is not str for item in all_attr):
                raise UnsupportedValueError(
                    f"Captured module {module_name!r} has a non-string __all__."
                )
            all_tuple = tuple(all_attr)
        else:
            raise UnsupportedValueError(f"Captured module {module_name!r} has an unsafe __all__.")

        stable_constants: list[tuple[str, Any]] = []
        for name, item in namespace.items():
            if name.startswith("__") or name in {"__all__", "__version__"}:
                continue
            if isinstance(item, (FunctionType, ModuleType, type)):
                continue
            try:
                constant_payload = self._module_constant_payload(item, set())
            except UnsupportedValueError:
                continue
            stable_constants.append((name, constant_payload))
        constants_payload = tuple(stable_constants)

        if origin in {"built-in", "frozen"}:
            return (
                version_digest,
                ("runtime-module", origin, import_identity),
                all_tuple,
                constants_payload,
            )

        file_path = namespace.get("__file__")
        if not isinstance(file_path, str):
            raise UnsupportedValueError(
                f"Captured module {module_name!r} has no stable source identity."
            )
        if Path(file_path).resolve() != Path(origin).resolve():
            raise UnsupportedValueError(
                f"Captured module {module_name!r} file does not match its import spec."
            )

        # The identity is the bytes, hashed on every derivation. Stat-shaped
        # shortcuts (size, mtime, ctime, device, inode) are not collision-free:
        # a same-size rewrite inside one timestamp granule preserves all five.
        with self._allow_raw_reads_scope():
            try:
                digest = hashlib.sha256(Path(file_path).read_bytes()).hexdigest()
            except OSError as exc:
                raise UnsupportedValueError(
                    f"Captured module {module_name!r} file cannot be read safely."
                ) from exc
        file_identity = ("file-sha256", import_identity, digest)
        return (version_digest, file_identity, all_tuple, constants_payload)

    def _module_constant_payload(self, value: Any, active_ids: builtins.set[int]) -> Any:
        if type(value) in (str, bytes, int, bool, type(None)):
            return value
        if type(value) is float:
            return (
                "float-bits",
                struct.pack(">d", value),
                self._definition_object_incarnation(value),
            )
        if type(value) is complex:
            return (
                "complex-bits",
                struct.pack(">d", value.real),
                struct.pack(">d", value.imag),
                self._definition_object_incarnation(value),
            )
        if isinstance(value, range):
            return ("range", value.start, value.stop, value.step)
        if isinstance(value, slice):
            return (
                "slice",
                self._module_constant_payload(value.start, active_ids),
                self._module_constant_payload(value.stop, active_ids),
                self._module_constant_payload(value.step, active_ids),
            )
        if type(value) is tuple:
            with self._capture_guard(value, active_ids):
                return tuple(self._module_constant_payload(item, active_ids) for item in value)
        if type(value) is frozenset:
            with self._capture_guard(value, active_ids):
                items = tuple(self._module_constant_payload(item, active_ids) for item in value)
            return ("frozenset", tuple(sorted(items, key=fingerprint_snapshot)))
        raise UnsupportedValueError("Unsupported stable module constant.")

    def _resource_identity_payload(self, resource: Any) -> Any:
        resource_id = id(resource)
        stack = self._resource_fingerprint_stack.get()
        if resource_id in stack:
            return (
                "recursive-resource",
                type(resource).__module__,
                type(resource).__qualname__,
            )
        token = self._resource_fingerprint_stack.set(stack + (resource_id,))
        try:
            with self._resource_hook_scope(resource, "identity"):
                resource_identity = getattr(resource, "identity", None)
                self._reject_callable_object_wrapper(
                    resource_identity,
                    subject=(
                        f"Resource hook {type(resource).__module__}."
                        f"{type(resource).__qualname__}.identity"
                    ),
                )
                configuration = resource_identity() if callable(resource_identity) else resource
                try:
                    frozen_configuration = freeze(configuration, adapters=self._adapters)
                except UnsupportedValueError as exc:
                    raise UnsupportedValueError(
                        f"Resource {type(resource).__module__}:{type(resource).__qualname__} "
                        "must be snapshot-safe or define identity()."
                    ) from exc
                direct_configuration = self._identity_reachable_from_owner(resource, configuration)
                configuration_type_payload = self._resource_configuration_type_payload(
                    configuration,
                    identity_owner=resource,
                    identity_site="resource-configuration",
                    pin_root_identity=direct_configuration,
                )
            return (
                "resource-v3",
                self._runtime_build_payload(),
                type(resource).__module__,
                type(resource).__qualname__,
                self._state_owner_incarnation(resource),
                self._implementation_type_payload(type(resource)),
                frozen_configuration,
                configuration_type_payload,
                self._resource_method_payload(resource, "probe"),
                self._resource_method_payload(resource, "load"),
                self._resource_method_payload(resource, "probe_and_load"),
                self._resource_method_payload(resource, "identity"),
            )
        finally:
            self._resource_fingerprint_stack.reset(token)

    def _identity_reachable_from_owner(self, owner: object, target: object) -> bool:
        """Find an identity() result retained in snapshot-safe owner state."""

        seen: builtins.set[int] = set()

        def walk(value: Any) -> bool:
            if value is target:
                return True
            value_type = type(value)
            if value_type in {str, bytes, int, float, bool, type(None), complex, FrozenRef}:
                return False
            value_id = id(value)
            if value_id in seen:
                return False
            seen.add(value_id)
            if value_type in {list, tuple}:
                return any(walk(item) for item in value)
            if value_type is dict:
                return any(walk(key) or walk(item) for key, item in value.items())
            if value_type in {set, frozenset}:
                return any(walk(item) for item in value)
            if value_type is FrozenList:
                return any(walk(item) for item in value.items)
            if value_type is FrozenDict:
                return any(walk(key) or walk(item) for key, item in value.entries)
            if value_type is FrozenSet:
                return any(walk(item) for item in value.items)
            if value_type is FrozenRecord:
                return any(walk(item) for _name, item in value.entries)
            if value_type is FrozenAdapterValue:
                return walk(value.payload)
            if value_type is FrozenGraph:
                return any(walk(item) for item in value.nodes) or walk(value.root)
            if is_dataclass(value) and not isinstance(value, type):
                return any(
                    walk(object.__getattribute__(value, item.name)) for item in fields(value)
                )
            try:
                state = self._static_instance_dict(value)
            except UnsupportedValueError:
                return False
            return any(walk(item) for item in state.values())

        return walk(owner)

    def _resource_configuration_type_payload(
        self,
        configuration: Any,
        *,
        identity_owner: object | None = None,
        identity_site: str = "configuration",
        pin_root_identity: bool = False,
    ) -> Any:
        """Pin behavior erased by the ordinary boundary snapshot.

        ``freeze`` remains the value contract for resource configuration, but it
        deliberately normalizes scalar/container subclasses, paths, and
        dataclasses.  That is correct at a query boundary and insufficient for a
        durable resource identity: methods on one of those values can influence
        ``probe``/``load`` even when its normalized data is unchanged.  This
        companion payload mirrors the configuration shape and records every
        behavior-bearing implementation and adapter without changing K2.
        """

        active: dict[int, int] = {}
        identity_index = [0]

        def guarded(value: Any, build: Callable[[], Any]) -> Any:
            object_id = id(value)
            existing = active.get(object_id)
            if existing is not None:
                return ("configuration-cycle", existing)
            cycle_index = len(active)
            active[object_id] = cycle_index
            try:
                return build()
            finally:
                del active[object_id]

        def state_payload(value: Any, *, excluded: builtins.set[str]) -> Any:
            slots = self._instance_slots(type(value)) - excluded
            if slots:
                raise UnsupportedValueError(
                    f"Resource configuration {type(value).__module__}."
                    f"{type(value).__qualname__} uses non-field slot state that "
                    "cannot be fingerprinted safely."
                )
            return tuple(
                (
                    name,
                    freeze(item, adapters=self._adapters),
                    encode(item),
                )
                for name, item in self._static_instance_dict(value).items()
                if name not in excluded
            )

        def adapter_for(value: Any) -> tuple[type[Any], ValueAdapter] | None:
            for candidate in type(value).__mro__:
                adapter = self._adapters.get(candidate)
                if adapter is not None:
                    return candidate, adapter
            return None

        def encode(value: Any, *, pin_identity: bool = True) -> Any:
            index = identity_index[0]
            identity_index[0] += 1
            payload = encode_payload(value)
            if identity_owner is None or not pin_identity:
                return payload
            return (
                "configuration-site-v1",
                self._state_site_incarnation(
                    identity_owner,
                    f"{identity_site}[{index}]",
                    value,
                ),
                payload,
            )

        def encode_payload(value: Any) -> Any:
            if type(value) in (str, bytes, int, float, bool, type(None), complex):
                if type(value) is float:
                    return ("float-bits", struct.pack(">d", value))
                if type(value) is complex:
                    return (
                        "complex-bits",
                        struct.pack(">d", value.real),
                        struct.pack(">d", value.imag),
                    )
                return ("plain-value",)
            if type(value) is FrozenList:
                return guarded(
                    value,
                    lambda: (
                        "frozen-list",
                        tuple(encode(item) for item in value.items),
                    ),
                )
            if type(value) is FrozenDict:
                return guarded(
                    value,
                    lambda: (
                        "frozen-dict",
                        tuple((encode(key), encode(item)) for key, item in value.entries),
                    ),
                )
            if type(value) is FrozenSet:
                return guarded(
                    value,
                    lambda: (
                        "frozen-set",
                        value.kind,
                        tuple(encode(item) for item in value.items),
                    ),
                )
            if type(value) is FrozenRecord:
                return guarded(
                    value,
                    lambda: (
                        "frozen-record",
                        value.type_name,
                        tuple((name, encode(item)) for name, item in value.entries),
                    ),
                )
            if type(value) is FrozenAdapterValue:
                return guarded(
                    value,
                    lambda: (
                        "frozen-adapter-value",
                        value.adapter_key,
                        encode(value.payload),
                    ),
                )
            if type(value) is FrozenGraph:
                return guarded(
                    value,
                    lambda: (
                        "frozen-graph",
                        tuple(encode(item) for item in value.nodes),
                        encode(value.root),
                    ),
                )
            if type(value) is FrozenRef:
                return ("frozen-ref", value.index)

            adapter_match = adapter_for(value)
            if adapter_match is not None:
                adapted_type, adapter = adapter_match
                return (
                    "adapted-value",
                    self._implementation_type_payload(type(value)),
                    _adapter_key(adapted_type),
                    self._adapter_implementation_digest(adapter),
                )

            if isinstance(value, (str, bytes, int, float, bool, complex)):
                return guarded(
                    value,
                    lambda: (
                        "scalar-subclass",
                        self._implementation_type_payload(type(value)),
                        state_payload(value, excluded=set()),
                    ),
                )
            if isinstance(value, os.PathLike):
                if is_stdlib_path(value):
                    return (
                        "path",
                        self._implementation_type_payload(type(value)),
                    )
                return guarded(
                    value,
                    lambda: (
                        "pathlike",
                        self._implementation_type_payload(type(value)),
                        state_payload(value, excluded=set()),
                    ),
                )
            if isinstance(value, list):
                return guarded(
                    value,
                    lambda: (
                        "list",
                        self._implementation_type_payload(type(value))
                        if type(value) is not list
                        else None,
                        tuple(encode(item) for item in value),
                        state_payload(value, excluded=set()) if type(value) is not list else (),
                    ),
                )
            if isinstance(value, tuple):
                return guarded(
                    value,
                    lambda: (
                        "tuple",
                        self._implementation_type_payload(type(value))
                        if type(value) is not tuple
                        else None,
                        tuple(encode(item) for item in value),
                        state_payload(value, excluded=set()) if type(value) is not tuple else (),
                    ),
                )
            if isinstance(value, Mapping):

                def mapping_payload() -> Any:
                    items = [
                        (freeze(key, adapters=self._adapters), key, item)
                        for key, item in value.items()
                    ]
                    item_digests = [
                        fingerprint_snapshot(frozen_key) for frozen_key, _key, _item in items
                    ]
                    if len(set(item_digests)) != len(item_digests):
                        raise UnsupportedValueError(
                            "Resource configuration mapping keys collapse to "
                            "the same frozen identity."
                        )
                    return (
                        "mapping",
                        self._implementation_type_payload(type(value))
                        if type(value) is not dict
                        else None,
                        tuple(
                            (frozen_key, encode(key), encode(item))
                            for frozen_key, key, item in items
                        ),
                        state_payload(value, excluded=set()) if type(value) is not dict else (),
                    )

                return guarded(value, mapping_payload)
            if isinstance(value, (set, frozenset)):

                def set_payload() -> Any:
                    items = [(freeze(item, adapters=self._adapters), item) for item in value]
                    item_digests = [
                        fingerprint_snapshot(frozen_item) for frozen_item, _item in items
                    ]
                    if len(set(item_digests)) != len(item_digests):
                        raise UnsupportedValueError(
                            "Resource configuration set members collapse to "
                            "the same frozen identity."
                        )
                    items.sort(key=lambda item: fingerprint_snapshot(item[0]))
                    exact_type = set if isinstance(value, set) else frozenset
                    return (
                        "set" if exact_type is set else "frozenset",
                        self._implementation_type_payload(type(value))
                        if type(value) is not exact_type
                        else None,
                        tuple((frozen_item, encode(item)) for frozen_item, item in items),
                        state_payload(value, excluded=set())
                        if type(value) is not exact_type
                        else (),
                    )

                return guarded(value, set_payload)
            if is_dataclass(value) and not isinstance(value, type):
                field_names = {item.name for item in fields(value)}
                return guarded(
                    value,
                    lambda: (
                        "dataclass",
                        self._implementation_type_payload(type(value)),
                        tuple(
                            (
                                item.name,
                                encode(object.__getattribute__(value, item.name)),
                            )
                            for item in fields(value)
                        ),
                        state_payload(value, excluded=field_names),
                    ),
                )
            if isinstance(value, range):
                return ("range",)
            raise UnsupportedValueError(
                f"Resource configuration {type(value).__module__}."
                f"{type(value).__qualname__} has no implementation-aware "
                "identity encoding."
            )

        return encode(configuration, pin_identity=pin_root_identity)

    def _resource_method_payload(self, resource: Any, method_name: str) -> Any:
        with self._resource_hook_scope(resource, method_name):
            method = getattr(resource, method_name, None)
            if method is None:
                return (method_name, "missing")
            fn = getattr(method, "__func__", method)
            if isinstance(fn, FunctionType):
                return (method_name, self._function_definition_payload(fn, set()))
            if isinstance(fn, BuiltinFunctionType):
                return (method_name, self._builtin_function_payload(fn))
            if callable(method):
                self._reject_callable_object_wrapper(
                    method,
                    subject=(
                        f"Resource hook {type(resource).__module__}."
                        f"{type(resource).__qualname__}.{method_name}"
                    ),
                )
                return (
                    method_name,
                    "callable",
                    self._policy_definition_payload(method),
                )
            return (
                method_name,
                type(method).__module__,
                type(method).__qualname__,
            )

    def _freeze_static_capture(self, value: Any, active_ids: builtins.set[int]) -> Any:
        scalar_types = (str, bytes, int, float, bool, type(None), complex)
        if value is Ellipsis:
            return ("ellipsis",)
        if type(value) in scalar_types:
            if type(value) is float:
                return (
                    "float-bits",
                    struct.pack(">d", value),
                    self._definition_object_incarnation(value),
                )
            if type(value) is complex:
                return (
                    "complex-bits",
                    struct.pack(">d", value.real),
                    struct.pack(">d", value.imag),
                    self._definition_object_incarnation(value),
                )
            return value
        if isinstance(value, type):
            return ("type", self._type_definition_payload(value))
        if isinstance(value, GenericAlias):
            return (
                "generic-alias",
                self._freeze_static_capture(value.__origin__, active_ids),
                tuple(self._freeze_static_capture(item, active_ids) for item in value.__args__),
            )
        if isinstance(value, UnionType):
            return (
                "union-type",
                tuple(
                    self._freeze_static_capture(item, active_ids) for item in typing.get_args(value)
                ),
            )
        if type(value).__qualname__ == "ForwardRef" and type(value).__module__ in {
            "annotationlib",
            "typing",
        }:
            forward_argument = getattr(value, "__forward_arg__", None)
            forward_module = getattr(value, "__forward_module__", None)
            if not isinstance(forward_argument, str) or (
                forward_module is not None and not isinstance(forward_module, str)
            ):
                raise UnsupportedValueError("Forward annotation has invalid identity metadata.")
            return ("forward-reference", forward_argument, forward_module)
        if type(value).__qualname__ == "TypeAliasType" and type(value).__module__ in {
            "typing",
            "typing_extensions",
        }:
            alias_name = getattr(value, "__name__", None)
            alias_module = getattr(value, "__module__", None)
            alias_parameters = getattr(value, "__type_params__", ())
            if (
                not isinstance(alias_name, str)
                or not isinstance(alias_module, str)
                or not isinstance(alias_parameters, tuple)
            ):
                raise UnsupportedValueError("Type alias has invalid identity metadata.")
            evaluator = getattr(value, "evaluate_value", None)
            if isinstance(evaluator, FunctionType):
                alias_value: Any = (
                    "lazy",
                    self._function_definition_payload(evaluator, set()),
                )
            else:
                try:
                    evaluated_alias = value.__value__
                except Exception as exc:
                    raise UnsupportedValueError(
                        f"Type alias {alias_module}.{alias_name} cannot be fingerprinted safely."
                    ) from exc
                alias_value = self._freeze_static_capture(evaluated_alias, active_ids)
            return (
                "type-alias",
                alias_module,
                alias_name,
                tuple(self._freeze_static_capture(item, active_ids) for item in alias_parameters),
                alias_value,
            )
        typing_origin = (
            typing.get_origin(value) if type(value).__module__ in {"typing", "types"} else None
        )
        if typing_origin is not None:
            return (
                "typing-alias",
                self._freeze_static_capture(typing_origin, active_ids),
                tuple(
                    self._freeze_static_capture(item, active_ids) for item in typing.get_args(value)
                ),
            )
        parameter_types = tuple(
            candidate
            for candidate in (
                getattr(typing, "TypeVar", None),
                getattr(typing, "ParamSpec", None),
                getattr(typing, "TypeVarTuple", None),
            )
            if isinstance(candidate, type)
        )
        if parameter_types and isinstance(value, parameter_types):
            no_default = getattr(typing, "NoDefault", object())
            parameter_name = getattr(value, "__name__", None)
            if not isinstance(parameter_name, str):
                raise UnsupportedValueError("Typing parameter has no stable string name.")
            bound_evaluator = getattr(value, "evaluate_bound", None)
            if isinstance(bound_evaluator, FunctionType):
                bound_payload: Any = (
                    "lazy",
                    self._function_definition_payload(bound_evaluator, set()),
                )
            else:
                try:
                    parameter_bound = getattr(value, "__bound__", None)
                except Exception as exc:
                    raise UnsupportedValueError(
                        f"Typing parameter {parameter_name!r} has an unsafe bound."
                    ) from exc
                bound_payload = (
                    self._freeze_static_capture(parameter_bound, active_ids)
                    if parameter_bound is not None
                    else None
                )
            constraints_evaluator = getattr(value, "evaluate_constraints", None)
            if isinstance(constraints_evaluator, FunctionType):
                constraints_payload: Any = (
                    "lazy",
                    self._function_definition_payload(constraints_evaluator, set()),
                )
            else:
                try:
                    constraints = getattr(value, "__constraints__", ())
                except Exception as exc:
                    raise UnsupportedValueError(
                        f"Typing parameter {parameter_name!r} has unsafe constraints."
                    ) from exc
                constraints_payload = tuple(
                    self._freeze_static_capture(item, active_ids) for item in constraints
                )
            default_evaluator = getattr(value, "evaluate_default", None)
            if isinstance(default_evaluator, FunctionType):
                default_payload: Any = (
                    "lazy",
                    self._function_definition_payload(default_evaluator, set()),
                )
            else:
                try:
                    default = getattr(value, "__default__", no_default)
                except Exception as exc:
                    raise UnsupportedValueError(
                        f"Typing parameter {parameter_name!r} has an unsafe default."
                    ) from exc
                default_payload = (
                    ("no-default",)
                    if default is no_default
                    else self._freeze_static_capture(default, active_ids)
                )
            return (
                "typing-parameter",
                type(value).__module__,
                type(value).__qualname__,
                parameter_name,
                bound_payload,
                constraints_payload,
                default_payload,
                bool(getattr(value, "__covariant__", False)),
                bool(getattr(value, "__contravariant__", False)),
                bool(getattr(value, "__infer_variance__", False)),
            )
        if type(value).__module__ == "typing":
            bindings = tuple(sorted(name for name, item in vars(typing).items() if item is value))
            if bindings:
                return (
                    "typing-singleton",
                    bindings,
                    self._module_identity_payload(typing),
                )
        if isinstance(value, scalar_types):
            with self._capture_guard(value, active_ids):
                return (
                    "scalar-subclass",
                    self._type_definition_payload(type(value)),
                    self._static_scalar_base_value(value),
                    self._static_instance_dict_payload(value, active_ids),
                )
        if type(value) in {
            FrozenList,
            FrozenDict,
            FrozenSet,
            FrozenRecord,
            FrozenAdapterValue,
        }:
            if not snapshots_equal(value, value):
                return (
                    "non-substitutive-frozen-snapshot",
                    id(value),
                    serialize_snapshot(value),
                )
            return value
        if isinstance(value, os.PathLike):
            if is_stdlib_path(value):
                return (
                    "path",
                    self._type_definition_payload(type(value)),
                    os.fspath(value),
                )
            with self._capture_guard(value, active_ids):
                return (
                    "pathlike",
                    self._type_definition_payload(type(value)),
                    os.fspath(value),
                    self._static_instance_dict_payload(value, active_ids),
                )
        if isinstance(value, range):
            return ("range", value.start, value.stop, value.step)
        if isinstance(value, slice):
            return (
                "slice",
                self._freeze_static_capture(value.start, active_ids),
                self._freeze_static_capture(value.stop, active_ids),
                self._freeze_static_capture(value.step, active_ids),
            )
        if type(value) is tuple:
            with self._capture_guard(value, active_ids):
                return tuple(self._freeze_static_capture(item, active_ids) for item in value)
        if isinstance(value, tuple):
            with self._capture_guard(value, active_ids):
                return (
                    "tuple-subclass",
                    self._type_definition_payload(type(value)),
                    tuple(self._freeze_static_capture(item, active_ids) for item in value),
                    self._static_instance_dict_payload(value, active_ids),
                )
        if type(value) is frozenset:
            with self._capture_guard(value, active_ids):
                items = tuple(self._freeze_static_capture(item, active_ids) for item in value)
                return ("frozenset", tuple(sorted(items, key=fingerprint_snapshot)))
        if isinstance(value, frozenset):
            with self._capture_guard(value, active_ids):
                items = tuple(self._freeze_static_capture(item, active_ids) for item in value)
                return (
                    "frozenset-subclass",
                    self._type_definition_payload(type(value)),
                    tuple(sorted(items, key=fingerprint_snapshot)),
                    self._static_instance_dict_payload(value, active_ids),
                )
        if is_dataclass(value) and not isinstance(value, type):
            params = getattr(type(value), "__dataclass_params__", None)
            if params is None or not params.frozen:
                raise UnsupportedValueError(
                    "Mutable dataclass values cannot be captured ambiently."
                )
            type_payload = self._type_definition_payload(type(value))
            field_names = {item.name for item in fields(value)}
            unsupported_slots = self._instance_slots(type(value)) - field_names
            if unsupported_slots:
                raise UnsupportedValueError(
                    f"Frozen dataclass {type(value).__module__}."
                    f"{type(value).__qualname__} has non-field slot state that "
                    "cannot be fingerprinted safely."
                )
            with self._capture_guard(value, active_ids):
                field_payload = tuple(
                    (
                        item.name,
                        self._freeze_static_capture(
                            object.__getattribute__(value, item.name), active_ids
                        ),
                    )
                    for item in fields(value)
                )
                extra_state = tuple(
                    (
                        name,
                        self._freeze_static_capture(item, active_ids),
                    )
                    for name, item in self._static_instance_dict(value).items()
                    if name not in field_names
                )
                return (
                    "frozen-dataclass",
                    type_payload,
                    self._definition_object_incarnation(value),
                    field_payload,
                    extra_state,
                )
        raise UnsupportedValueError("Unsupported ambient capture.")

    @staticmethod
    def _definition_object_incarnation(value: Any) -> tuple[Any, ...]:
        """Pin process-local identity only when structural state is not substitutive."""

        value_type = type(value)
        non_reflexive = type(value) in {float, complex} and value != value
        equality_implementation: object = value_type.__eq__
        hash_implementation: object = value_type.__hash__
        identity_based = (
            is_dataclass(value)
            and not isinstance(value, type)
            and (equality_implementation is object.__eq__ or hash_implementation is object.__hash__)
        )
        if non_reflexive or identity_based:
            # The current object is strongly reachable through the function,
            # policy, adapter, or resource definition while that definition is
            # observed. A separate process receives a distinct token and safely
            # declines checkpoint reuse even if its allocator repeats an address.
            return (
                "process-local-incarnation",
                _PROCESS_IDENTITY_TOKEN.nonce,
                os.getpid(),
                id(value),
            )
        return ("structural-incarnation",)

    @staticmethod
    def _static_scalar_base_value(value: Any) -> Any:
        if isinstance(value, str):
            return ("str", str(value))
        if isinstance(value, bytes):
            return ("bytes", bytes(value))
        if isinstance(value, int):
            return ("int", int(value))
        if isinstance(value, float):
            return ("float", float(value))
        if isinstance(value, complex):
            return ("complex", complex(value))
        raise UnsupportedValueError("Unsupported scalar subclass capture.")

    @staticmethod
    def _instance_slots(value_type: type[Any]) -> builtins.set[str]:
        slots: builtins.set[str] = builtins.set()
        for cls in value_type.__mro__:
            declared = cls.__dict__.get("__slots__", ())
            if isinstance(declared, str):
                declared = (declared,)
            slots.update(slot for slot in declared if slot not in {"__dict__", "__weakref__"})
        return slots

    @staticmethod
    def _static_instance_dict(value: Any) -> dict[str, Any]:
        try:
            state = object.__getattribute__(value, "__dict__")
        except (AttributeError, TypeError):
            return {}
        if type(state) is not dict or any(type(name) is not str for name in state):
            raise UnsupportedValueError("Instance state must be an exact dict with exact str keys.")
        return state

    def _static_instance_dict_payload(self, value: Any, active_ids: builtins.set[int]) -> Any:
        slots = self._instance_slots(type(value))
        if slots:
            raise UnsupportedValueError(
                f"Ambient capture {type(value).__module__}."
                f"{type(value).__qualname__} uses slot state that cannot be "
                "fingerprinted safely."
            )
        return tuple(
            (name, self._freeze_static_capture(item, active_ids))
            for name, item in self._static_instance_dict(value).items()
        )

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
        for name in ("label", "probe", "load"):
            with self._resource_hook_scope(value, name):
                if not callable(getattr(value, name, None)):
                    return False
        return True

    @contextmanager
    def _request_scope(
        self,
    ) -> Iterator[list[_PendingObserverEvent] | None]:
        current = self._request_token.get()
        if current is not None:
            # A span's request id must reflect every change committed while
            # the span thread held no lock; catching up here, at the boundary
            # of each call joining the span, is what keeps a cross-thread
            # set from leaving the span's dedupe on stale answers.
            self._sync_span_to_epoch()
            yield None
            return
        self._request_counter += 1
        token = self._request_token.set(self._request_counter)
        fingerprints_token = self._request_query_fingerprints.set({})
        pending: list[_PendingObserverEvent] = []
        events_token = self._pending_events.set(pending)
        failures: list[NodeKey] = []
        failures_token = self._request_failures.set(failures)
        try:
            yield pending
        finally:
            self._pending_events.reset(events_token)
            self._release_failure_exceptions(failures)
            self._request_failures.reset(failures_token)
            self._request_query_fingerprints.reset(fingerprints_token)
            self._request_token.reset(token)
            self._evict_query_nodes_if_needed()

    def _mark_query_used(self, key: NodeKey) -> None:
        self._query_touch_counter += 1
        self._query_last_used[key] = self._query_touch_counter

    def _evict_query_nodes_if_needed(self) -> None:
        limit = self._max_query_nodes
        if limit is None:
            return
        while len(self._query_records) > limit:
            lru_key = min(
                self._query_records,
                key=lambda item: self._query_last_used.get(item, -1),
            )
            self._evict_query_record(lru_key)

    def _evict_query_record(self, key: NodeKey) -> None:
        self._stats["evictions"] += 1
        self._records.pop(key, None)
        self._query_records.discard(key)
        self._query_last_used.pop(key, None)
        self._call_snapshots().pop(key, None)
        self._query_timings.pop(key, None)
        if not any(item.identity == key.identity for item in self._query_records) and not any(
            item.identity == key.identity for item in self._call_snapshots()
        ):
            self._query_objects().pop(key.identity, None)

    def _discard_uncommitted_query(self, key: NodeKey) -> None:
        """Remove state created while a cold/warmed evaluation was failing."""
        if any(frame.key == key for frame in self._execution_stack.get()):
            # A nested same-key request may have failed with CycleError while
            # the outer evaluation catches it and continues. The outer frame
            # still owns this registration until it succeeds or unwinds.
            return
        self._records.pop(key, None)
        self._query_records.discard(key)
        self._query_last_used.pop(key, None)
        self._call_snapshots().pop(key, None)
        self._query_timings.pop(key, None)
        if not any(item.identity == key.identity for item in self._query_records) and not any(
            item.identity == key.identity for item in self._call_snapshots()
        ):
            self._query_objects().pop(key.identity, None)

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
        snapshot = self._freeze_owned_snapshot(value)
        if self._store is not None:
            self._persist_snapshot(snapshot)
        return snapshot

    def _freeze_owned_snapshot(self, value: Any) -> Snapshot:
        self._verify_adapter_lifetime()
        snapshot = detach_snapshot(freeze(value, adapters=self._adapters))
        self._verify_adapter_lifetime()
        return snapshot

    @staticmethod
    def _require_snapshot_digest(
        snapshot: Snapshot,
        expected_digest: str,
        *,
        subject: str,
    ) -> None:
        try:
            payload = serialize_snapshot(snapshot)
        except (OverflowError, RecursionError, TypeError, UnsupportedValueError, ValueError) as exc:
            raise CheckpointIntegrityError(
                f"Cannot save checkpoint: {subject} is not a valid canonical snapshot."
            ) from exc
        actual_digest = hashlib.sha256(payload).hexdigest()
        if actual_digest != expected_digest:
            raise CheckpointIntegrityError(
                f"Cannot save checkpoint: {subject} no longer matches its recorded digest."
            )

    def _persist_snapshot(self, snapshot: Snapshot) -> None:
        """Write the snapshot's serialized bytes to the configured ArtifactStore.
        Raw filesystem I/O runs under the raw-read allow scope so a `FileSystemArtifactStore`
        used while a query frame is active is not rejected by the global guard."""
        store = self._store
        if store is None:
            return
        digest = fingerprint_snapshot(snapshot)
        with self._allow_raw_reads_scope():
            if store.contains(digest):
                return
            payload = serialize_snapshot(snapshot)
            store.put(digest, payload)

    def _thaw_value(self, value: Any) -> Any:
        self._verify_adapter_lifetime()
        thawed = thaw(value, adapters=self._adapters)
        self._verify_adapter_lifetime()
        return thawed

    def _fingerprint_value(self, value: Any) -> str:
        self._verify_adapter_lifetime()
        digest = fingerprint(value, adapters=self._adapters)
        self._verify_adapter_lifetime()
        return digest

    def _semantic_equal(self, left: Any, right: Any) -> bool:
        self._verify_adapter_lifetime()
        equal = semantic_equal(left, right, adapters=self._adapters)
        self._verify_adapter_lifetime()
        return equal

    def _compare_input_snapshots(
        self, input_key: Any, previous: Snapshot, snapshot: Snapshot
    ) -> bool:
        if input_key.eq is None and input_key.cutoff is None:
            # Both operands are canonical freeze outputs, so the default
            # comparison reduces to comparing the stored snapshots directly --
            # the same decision _execute_query makes for a recomputed result,
            # with no thaw and no ValueAdapter hook on the default input path.
            # Thawing would drop FrozenRecord type identity and call a dict of
            # matching shape an equal update the caller never sees.
            return snapshots_equal(previous, snapshot)
        # An explicit policy is defined over the values the caller wrote, not
        # over their encodings, so it keeps the thawed operands.
        return self._compare_values(
            eq=input_key.eq,
            cutoff=input_key.cutoff,
            left=self._thaw_value(detach_snapshot(previous)),
            right=self._thaw_value(detach_snapshot(snapshot)),
        )

    def _compare_values(
        self,
        *,
        eq: Callable[[Any, Any], bool] | None,
        cutoff: Callable[[Any], Any] | None,
        left: Any,
        right: Any,
    ) -> bool:
        if cutoff is not None:
            return snapshots_equal(
                self._freeze_cutoff_token(cutoff(left)),
                self._freeze_cutoff_token(cutoff(right)),
            )
        if eq is None:
            return self._semantic_equal(left, right)
        return eq(left, right)

    def _freeze_cutoff_token(self, value: Any) -> Snapshot:
        try:
            return self._freeze_owned_snapshot(value)
        except UnsupportedValueError as exc:
            raise UnsupportedValueError(
                "Cutoff functions must return snapshot-safe values."
            ) from exc


# Install before callers can cache an unguarded Python-level launch method.
# Database construction retains the idempotent call as a defensive check for
# unusual module-loading environments.
_install_guards_once()
