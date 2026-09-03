from __future__ import annotations

import builtins
import dis
import functools
import hashlib
import importlib.machinery
import inspect
import io
import json
import os
import struct
import sys
import sysconfig
import threading
import time
import typing
import weakref
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager, suppress
from contextvars import ContextVar, copy_context
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from functools import cached_property
from pathlib import Path
from types import (
    BuiltinFunctionType,
    CodeType,
    FunctionType,
    GenericAlias,
    GetSetDescriptorType,
    MemberDescriptorType,
    MethodDescriptorType,
    MethodType,
    ModuleType,
    TracebackType,
    UnionType,
    WrapperDescriptorType,
)
from typing import TYPE_CHECKING, Any, ClassVar, Literal, ParamSpec, TypeVar, cast, overload

from ._path_identity import is_stdlib_path
from ._safe_fs import read_regular_file_following_links
from .errors import (
    AdapterContractError,
    CheckpointIntegrityError,
    CheckpointManifestError,
    CheckpointModeError,
    CheckpointVersionError,
    CycleError,
    InputKeyError,
    ReentrantDatabaseError,
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
    _AdapterRegistry,
    assert_not_mutated,
    collect_adapter_keys,
    deserialize_snapshot,
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
# are rejected loudly rather than silently reused. Version 8 marks the kernel
# rebuilding its own file-stat readings through a built-in adapter. A record
# saved by an earlier version froze such a reading field by field into a plain
# record instead, and the layout of the record holding it is otherwise
# identical, so nothing below this field distinguishes the two: a database
# holding the built-in would warm that stored encoding without re-freezing it
# and hand back a mapping where a fresh execution now produces the snapshot
# type. Registration also changes how a shared reading is stored, from a node
# of the shared-structure envelope to a value written inline, so the stored
# bytes of an unchanged value differ across the boundary as well. The adapter
# gate cannot catch either: it compares the adapter keys a record used, and a
# record written before the adapter existed names none. Version 7 adds the
# saving database's mode to the manifest root. The value a query computes and
# persists depends on that mode -- strict exposes frozen views and frozen call
# arguments where checked and fast thaw -- so a record saved under one mode can
# carry a value another mode would never compute; a version-6-or-earlier
# manifest names no mode at all and so cannot be attributed to one. Version 5
# and earlier are refused for a second reason that still stands: their records
# can predate the two soundness repairs version 6 marked -- captured-module
# identity was derived from a stat tuple a same-size rewrite can preserve, and a
# stat probe raising NotADirectoryError published no resource edge -- so such a
# record can carry a stale identity, or claim no dependencies for a read a fresh
# database re-derives.
_CHECKPOINT_MANIFEST_VERSION = 8
# Version of the snapshot/fingerprint encoding this kernel emits, mirrored from
# value._KERNEL_FINGERPRINT_PREFIX (b"K2;"). Recorded in the manifest and checked
# at load so a checkpoint from a differently-encoded kernel is never trusted.
_KERNEL_FINGERPRINT_VERSION = 2
_DEFAULT_SEMANTIC_EQUALITY_VERSION = 1
_MISSING_SNAPSHOT = object()
_EMPTY_CELL_OBSERVATION = object()
_UNBOUND_GLOBAL_OBSERVATION = object()
# Answer for a captured module attribute that has vanished. No target a memo
# recorded can be this object, so the guard falls through to the recompute that
# reports the missing attribute.
_MISSING_MODULE_ATTRIBUTE = object()
# Not a hexadecimal digest, so it can never equal a stored one and always
# forces the memo guard to fall through to a full recompute.
_UNREADABLE_RESOURCE_DIGEST = "unreadable-resource"
# Implementation digests of the kernel's own fixed adapters, held for the
# process. Such a digest folds only the adapter's class -- its two method
# bodies, its type payload, the bytes of the module they ship in -- and the
# interpreter build, none of which an in-contract process can move, so it cannot
# differ between two databases here. Re-deriving it per construction made every
# `Database()` walk two method bodies and hash a source file, which is a cost the
# design already decided to pay once rather than repeatedly.
#
# The key is the adapter key paired with the adapter's TYPE, deliberately not
# `id(adapter)`. An identity key would be sound only under an argument about
# lifetimes -- that no id recorded here can be recycled, which holds only
# because the kernel's registry is a module-level mapping keeping each singleton
# alive for the whole process -- and a key that needs that argument is a key
# whose soundness a later edit can silently remove. A type object is kept alive
# by the module that defines it, so the question does not arise. The pair is no
# coarser than identity would be for this set: an entry reaches this memo only
# when it IS the kernel's own entry for that key, and a fixed adapter holds no
# instance state, so two instances of one such class digest identically by
# construction.
#
# What this does NOT cover: a caller's adapter, including one registered for a
# type the kernel also adapts. Those are re-derived at every construction and at
# every trust boundary, because a caller's implementation and configuration are
# theirs to change between one database and the next.
#
# One consequence, stated because it is a widening: an out-of-contract in-process
# rewrite of a fixed adapter's own methods was already invisible to every
# database already built; it is now invisible to databases built afterwards too.
# A source-level change still moves the digest, because it needs a new process.
_FIXED_ADAPTER_IMPLEMENTATION_DIGESTS: dict[tuple[str, type[Any]], str] = {}


def _validated_store(store: Any, parameter: str) -> ArtifactStore:
    """Check an injected store against both halves of the store contract.

    A store reaches deep into the persistence path before it is first used,
    so an unusable one has to be refused where it is handed over rather than
    surfacing as an ``AttributeError`` or a silent no-op several calls later.
    The two checks catch different failures: the shape check rejects an object
    that is missing a method outright, and the identity check rejects a
    subclass that passes the shape check because it inherited the protocol's
    own unimplemented ``get``/``put``. Inheriting the ``contains`` default is
    intended and stays legal.

    ``parameter`` identifies the door the store came through rather than naming
    an argument -- call sites pass a phrase such as "The store passed to
    save_checkpoint()" -- so the raised message reads as a sentence about the
    call that was actually made.
    """
    if not isinstance(store, ArtifactStore):
        raise TypeError(
            f"{parameter} must implement the ArtifactStore protocol "
            f"(get, put, contains); got {type(store).__name__}."
        )
    for name in ("get", "put"):
        if getattr(type(store), name, None) is getattr(ArtifactStore, name):
            raise TypeError(
                f"{parameter} inherits ArtifactStore.{name}() without "
                f"implementing it; implement get() and put()."
            )
    return store


def _build_runtime_build_payload() -> tuple[Any, ...]:
    return (
        "runtime-build-v3",
        sys.implementation.name,
        getattr(sys.implementation, "cache_tag", None),
        tuple(sys.version_info),
        (
            "flags",
            # `hash_randomization` is deliberately excluded. It separates a
            # PYTHONHASHSEED=0 process from every other process while giving no
            # protection against the hazard it looks like it covers: two
            # default-seed processes carry the same flag and different hash
            # orders, so folding it cannot separate a hash-order-dependent body
            # from a stable one. All it buys is that a checkpoint written under
            # a pinned seed -- CI, the benchmark harness, the documentation
            # runner -- can never warm an ordinary process. Route hash-order
            # dependence through an `Input` or a `Resource`.
            #
            # The names come from `dir(sys.flags)` rather than a literal list,
            # so a flag a future interpreter adds is folded the day it appears;
            # the three `structseq` metadata attributes and the two sequence
            # methods are not flags and are excluded by name.
            tuple(
                (flag_name, getattr(sys.flags, flag_name))
                for flag_name in sorted(dir(sys.flags))
                if not flag_name.startswith("_")
                and flag_name
                not in {
                    "count",
                    "hash_randomization",
                    "index",
                    "n_fields",
                    "n_sequence_fields",
                    "n_unnamed_fields",
                }
            ),
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


def _stdlib_directory_prefix() -> str:
    """The directory this interpreter's standard library was installed in.

    Returned with a trailing separator, so a prefix test against it cannot
    match a sibling directory whose name merely begins the same way.
    `sysconfig` answers for every ordinary installation; the directory holding
    `os` is the fallback for an interpreter that reports no `stdlib` path.
    """

    directory = sysconfig.get_path("stdlib") or os.path.dirname(
        getattr(os, "__file__", "") or ""
    )
    if not directory:
        # Nothing to compare against: keep the narrower answer, so a module is
        # treated as the caller's rather than silently losing its constants.
        return "\x00 no stdlib directory"
    return os.path.join(os.path.abspath(directory), "")


_REFLECTIVE_NAMESPACE_BUILTINS = frozenset({"eval", "exec", "globals", "locals", "vars"})
_REFLECTIVE_ATTRIBUTE_BUILTINS = frozenset({"delattr", "getattr", "setattr"})

# The type-parameter classes this interpreter exposes. Named once so the memo
# observation and the chain-landing filter recognise the same set.
_TYPE_PARAMETER_TYPES = tuple(
    candidate
    for candidate in (
        getattr(typing, "TypeVar", None),
        getattr(typing, "ParamSpec", None),
        getattr(typing, "TypeVarTuple", None),
    )
    if isinstance(candidate, type)
)


def _is_type_alias(value: Any) -> bool:
    """True for a `type X = ...` alias, from typing or typing_extensions."""

    return type(value).__qualname__ == "TypeAliasType" and type(value).__module__ in {
        "typing",
        "typing_extensions",
    }


def _live_type_binding(value: type[Any]) -> Any:
    """The object the type's defining module currently binds under its name.

    Mirrors `_module_type_anchor_payload`'s resolution without its refusal.
    The payload refuses a type whose defining module no longer binds it, so an
    observation that pinned the type by identity alone would keep a stored
    fingerprint serving after the binding moved while every fresh computation
    refuses. Folded as an identity leaf, this is the type itself while the
    binding holds and whatever replaced it -- or None -- once it does not,
    which is exactly when the memo must stop answering.
    """

    module = sys.modules.get(value.__module__)
    if module is None:
        return None
    current: Any = vars(module).get(value.__qualname__.split(".", 1)[0])
    for part in value.__qualname__.split(".")[1:]:
        if not isinstance(current, type):
            return None
        current = vars(current).get(part)
    return current


def _sorted_state_entries(state: dict[Any, Any]) -> list[tuple[Any, Any]]:
    """Order an instance dictionary's entries, whatever its keys turn out to be.

    An instance dictionary is an ordinary dict, and nothing stops a caller
    writing a key into one that is not a string. Ordering the entries by their
    keys then asks an integer to compare against a string, which raises instead
    of answering -- and a walk that has been handed a value has to decide the
    order rather than let the keys decide whether there is one at all.

    So the plain order is attempted first, which keeps every ordinary
    dictionary on exactly the order, and the cost, it has today; a key set that
    does not order itself falls back to each key's type name and repr, which
    orders any two keys against each other. Attempted rather than checked in
    front for the reason the handle observation states beside its own catch,
    and because a check would refuse an all-integer dictionary that orders
    itself perfectly well and is folded today. The fallback order is internal:
    it is not a documented ordering and nothing outside this module may depend
    on which order it picks. It can genuinely differ between two processes --
    a repr may name an object's identity rather than its value, a frozenset's
    depends on the hash seed once it holds more than one member, and two keys
    whose type name and repr both tie keep the order the dictionary held them
    in. Nothing contracts on that order, which is what makes all of it
    acceptable; a caller who needs a stable fold gives the dictionary keys that
    order themselves.
    """

    try:
        return sorted(state.items())
    except TypeError:
        return sorted(
            state.items(),
            key=lambda entry: (type(entry[0]).__qualname__, repr(entry[0])),
        )


def _type_anchor_leaves(root: Any) -> tuple[Any, ...]:
    """Live-binding leaves for every anchored type an eager value resolves.

    Follows the shapes `_freeze_static_capture` resolves eagerly -- containers
    and the instance state of their subclasses, slices, scalar-subclass and
    pathlike instance state, frozen dataclass fields and extras, parameterized
    generics, unions, nested aliases and type parameters -- and contributes
    one `_live_type_binding` leaf per non-builtin type reached, because the
    payload anchors each of those types to its live module binding. An alias
    or a type parameter recurses only where its evaluate_* attribute is not a
    Python function: where one exists the payload folds the evaluator instead
    of the resolved value, and observing that evaluator as a definition
    already tracks the globals it resolves. Shapes the payload returns
    pre-frozen or refuses outright contribute nothing, mirroring the payload,
    which anchors no type there either.

    Each swept class and carrier type contributes its own leaf, and the sweep
    then follows that type's own definition closure -- its metaclass, its
    bases, the classes its body binds directly. Without that descent,
    rebinding one of those still answered from the memo where a fresh
    computation refuses. A class the body holds inside one of the immutable
    containers the payload accepts, rather than binding directly, is not
    followed here: the payload folds that attribute through the eager capture
    instead, so rebinding such a class leaves the memo answering with the
    stored fingerprint while a fresh computation either moves to a new one or
    refuses. Descent stops without a namespace walk at builtin and
    stdlib-rooted types, which the payload pins by name anchor and runtime
    build rather than by walking their contents.
    """

    leaves: list[Any] = []
    swept: builtins.set[int] = set()
    # Types need their own visited set: the arm below contributes a leaf on
    # every contact and returns before `swept` is ever consulted, so the id set
    # that stops the value walk repeating never receives a type.
    swept_types: builtins.set[int] = set()

    def sweep_instance_state(value: Any, exclude: frozenset[str] = frozenset()) -> None:
        try:
            state = object.__getattribute__(value, "__dict__")
        except (AttributeError, TypeError):
            return
        if not isinstance(state, dict):
            return
        for name, item in _sorted_state_entries(state):
            if name not in exclude:
                sweep(item)

    def sweep_type(cls: type[Any]) -> None:
        if cls.__module__ == "builtins":
            return
        leaves.append(_live_type_binding(cls))
        if cls.__module__.partition(".")[0] in sys.stdlib_module_names:
            return
        if id(cls) in swept_types:
            return
        swept_types.add(id(cls))
        # Mirrors _local_implementation_type_payload: the metaclass, then each
        # base, then the type-valued entries of the namespace, each of which
        # that payload anchors to its own live module binding.
        sweep(type(cls))
        for base in cls.__bases__:
            sweep(base)
        for _name, item in sorted(vars(cls).items()):
            if isinstance(item, type):
                sweep(item)

    def sweep(value: Any) -> None:
        if isinstance(value, type):
            sweep_type(value)
            return
        if id(value) in swept:
            return
        swept.add(id(value))
        if isinstance(value, slice):
            sweep(value.start)
            sweep(value.stop)
            sweep(value.step)
            return
        if isinstance(value, (str, bytes, int, float, complex)):
            sweep(type(value))
            sweep_instance_state(value)
            return
        if isinstance(value, os.PathLike):
            sweep(type(value))
            sweep_instance_state(value)
            return
        if isinstance(value, (tuple, frozenset)):
            for item in value:
                sweep(item)
            if type(value) not in (tuple, frozenset):
                sweep(type(value))
                sweep_instance_state(value)
            return
        if isinstance(value, GenericAlias):
            sweep(value.__origin__)
            for item in value.__args__:
                sweep(item)
            return
        if isinstance(value, UnionType):
            for item in typing.get_args(value):
                sweep(item)
            return
        if type(value).__module__ in {"typing", "types"}:
            origin = typing.get_origin(value)
            if origin is not None:
                sweep(origin)
                for item in typing.get_args(value):
                    sweep(item)
                return
        if _is_type_alias(value):
            if not isinstance(getattr(value, "evaluate_value", None), FunctionType):
                try:
                    part = getattr(value, "__value__", None)
                except Exception:
                    part = None
                sweep(part)
            return
        if _TYPE_PARAMETER_TYPES and isinstance(value, _TYPE_PARAMETER_TYPES):
            for evaluator_name, value_name in (
                ("evaluate_bound", "__bound__"),
                ("evaluate_constraints", "__constraints__"),
                ("evaluate_default", "__default__"),
            ):
                if isinstance(getattr(value, evaluator_name, None), FunctionType):
                    continue
                try:
                    part = getattr(value, value_name, None)
                except Exception:
                    part = None
                sweep(part)
            return
        if is_dataclass(value) and not isinstance(value, type):
            sweep(type(value))
            field_names: builtins.set[str] = builtins.set()
            for field_item in fields(value):
                field_names.add(field_item.name)
                try:
                    part = object.__getattribute__(value, field_item.name)
                except Exception:
                    part = None
                sweep(part)
            sweep_instance_state(value, exclude=frozenset(field_names))

    sweep(root)
    return tuple(leaves)


def _walk_reflective_code(code: CodeType) -> Iterator[CodeType]:
    yield code
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            yield from _walk_reflective_code(constant)


def _reflective_namespace_offenses(code: CodeType) -> tuple[str, ...]:
    """Names whose loads let *code* read a namespace it never captures.

    Capture fingerprinting is static: it resolves the names a code object
    references against the function's globals. globals()['NAME'],
    vars(module)['NAME'], getattr(module, 'NAME') and eval reach the same
    mutable state while referencing only the builtin, so those reads must be
    rejected rather than silently escaping identity. Only global-scope loads
    of the builtins count -- an attribute that happens to be named "globals"
    or "vars" is untouched -- and the getattr family (plus __dict__ attribute
    loads) is rejected only beside a handle that can produce a module
    namespace, because getattr on ordinary objects is legitimate and common.

    Three loads mark that handle. One is a reach for the module table: a
    "modules" attribute load, which survives whatever name sys was imported
    under and wherever the import sits, or the string "modules" beside a
    getattr-family builtin, which is how getattr spells the same reach
    without loading the attribute at all. The second is an "import_module"
    attribute load, which reaches importlib's own module builder and survives
    an alias and a body-scope import the same way. Neither is checked against
    the module it is read off, so an attribute merely named "modules" or
    "import_module" arms the rule beside an ordinary getattr too -- the
    over-rejection this conservative reading pays for. The third is a global
    load of the name importlib, which marks the module wherever that name is
    read, whatever is done with it afterwards. A callable lifted out of the
    module by a from-import loads neither the name nor an attribute, so it is
    an ordinary global load and this rule does not read it.

    Reaching a module namespace is not itself an offense. A plain
    sys.modules[...] subscript, and an import_module(...) call, with no
    reflective builtin beside them stay accepted, deliberately: the handle
    marks where a reflective read could start, and it is the builtin beside
    it that is refused.

    A function's __globals__ is its defining module's namespace by another
    spelling, so loading that attribute is an offense on its own: the walk
    that folds a captured function stops at the function and never follows it.

    Results are cached on the code object, which hashes its own constants: a
    code object carrying a constant that is not hashable -- a slice literal on
    interpreters before 3.12 -- is scanned uncached instead.
    """

    try:
        return _cached_reflective_namespace_offenses(code)
    except TypeError:
        return _scan_reflective_namespace_offenses(code)


@functools.lru_cache(maxsize=2048)
def _cached_reflective_namespace_offenses(code: CodeType) -> tuple[str, ...]:
    return _scan_reflective_namespace_offenses(code)


def _scan_reflective_namespace_offenses(code: CodeType) -> tuple[str, ...]:
    """Scan *code* and its nested code objects for the offending loads."""

    global_loads: set[str] = set()
    attribute_loads: set[str] = set()
    string_constants: set[str] = set()
    for item in _walk_reflective_code(code):
        string_constants.update(
            constant for constant in item.co_consts if isinstance(constant, str)
        )
        for instruction in dis.get_instructions(item):
            argval = instruction.argval
            if not isinstance(argval, str):
                continue
            if instruction.opname in {"LOAD_GLOBAL", "LOAD_NAME"}:
                global_loads.add(argval)
            elif instruction.opname in {"LOAD_ATTR", "LOAD_METHOD"}:
                attribute_loads.add(argval)
    attribute_builtins = global_loads & _REFLECTIVE_ATTRIBUTE_BUILTINS
    offenses = global_loads & _REFLECTIVE_NAMESPACE_BUILTINS
    if "__globals__" in attribute_loads:
        offenses = offenses | {"__globals__"}
    # The attribute load carries the module table under any import alias, and
    # getattr's string argument spells the same access without loading the
    # attribute at all -- both are the handle the rule below keys on, so
    # aliasing sys and getattr(sys, "modules") reach it the same way a plain
    # sys.modules access does.
    module_table = "modules" in attribute_loads or (
        bool(attribute_builtins) and "modules" in string_constants
    )
    # import_module builds the same namespace without going through the
    # table, and its attribute load survives an aliased or body-scope
    # importlib for the same reason the modules load survives an aliased sys.
    namespace_handle = (
        "importlib" in global_loads
        or "import_module" in attribute_loads
        or module_table
    )
    if namespace_handle:
        offenses = offenses | attribute_builtins
        if "__dict__" in attribute_loads:
            offenses = offenses | {"__dict__"}
    return tuple(sorted(offenses))


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

    def __post_init__(self) -> None:
        # The public key boundaries already refuse `str` subclasses, but node
        # identity decides record equality, manifest validation and every
        # rendered label, so the node table refuses one itself rather than
        # trusting every internal path that builds a key.
        if (
            type(self.kind) is not str
            or type(self.identity) is not str
            or type(self.args_digest) is not str
            or type(self.label) is not str
        ):
            raise TypeError("NodeKey fields must be exactly str.")


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


@dataclass
class ExecutionFrame:
    key: NodeKey
    dependencies: set[NodeKey] = field(default_factory=set)
    boundary_fingerprints: list[str] = field(default_factory=list)
    boundary_values: list[Any] = field(default_factory=list)
    untracked_reasons: list[str] = field(default_factory=list)
    checkpointable: bool = True
    # The thread the execution runs on, bound where the frame is built. A
    # thread spawned inside the body inherits the frame but not the ident, so
    # this is what tells a descendant apart from the executing thread itself.
    thread_ident: int = field(default_factory=threading.get_ident)
    # Set when the execution leaves. A spawned thread keeps the stack it
    # inherited for as long as it runs, so whether an execution is still live
    # has to be the frame's own property rather than the stack's shape.
    completed: bool = False


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
    """Delivered to observers when a subscribed query's stored value moves.

    Fires on a cold execution and on a re-execution that advanced the node's
    `changed_at`. `"reused"` and `"backdated"` decisions do not fire, and
    neither does a re-execution on a node marked untracked that re-landed a
    byte-identical value -- it keeps the `changed_at` it had. So `decision` is
    always `"executed"`.
    """

    query_id: str
    args_digest: str
    decision: str
    changed_at: int
    verified_at: int


ObserverCallback = Callable[[QueryChangeEvent], None]
ObserverErrorHook = Callable[[Exception], None]
_PendingObserverEvent = tuple[NodeKey, tuple[int, ...], QueryChangeEvent]


def _default_observer_error_hook(exc: Exception) -> None:
    sys.stderr.write(f"pyinc: observer callback raised {type(exc).__qualname__}: {exc}\n")


_ACTIVE_GUARDS: ContextVar[tuple[Database, ...]] = ContextVar("pyinc_active_guards", default=())
_GUARD_INSTALLED = False
_GUARD_INSTALL_LOCK = threading.Lock()

# How a refused call names the position it was made from. Keyed by the
# boundary states that are not "outside", which is the only state that allows
# everything.
_BOUNDARY_REJECTION_REASONS: Mapping[str, str] = {
    "inside": "inside a query body",
    "hook": "inside a resource hook",
    "descendant": "from a thread spawned inside a query execution",
}


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

        original_thread_start = threading.Thread.start

        def guarded_thread_start(thread: threading.Thread) -> None:
            """Start `thread` inside the spawning boundary's context, if there is one.

            A thread started inside a query body belongs to that execution:
            whatever it reads flows back into the result the query stores, so
            the frame the guard consults has to be visible from it. Threads
            start with an empty context otherwise, which is why the frame is
            invisible to them by default.

            A resource hook counts too, and not only when a query is running
            above it. A `read_resource` made at top level holds the state lock
            across the whole hook while opening no execution at all, so a child
            that inherited nothing passed every check and then blocked on that
            lock until its own parent returned -- which, if the parent joins it,
            is never. The hook depth is the only thing that says where such a
            child stands.

            Threads started anywhere else -- every thread in a process that is
            not inside a query or a hook at that instant -- are left exactly as
            they were, at the cost of one scan of an almost always empty tuple.
            """
            inside = any(
                db._current_frame() is not None or db._resource_hook_depth.get() > 0
                for db in _ACTIVE_GUARDS.get()
            )
            if inside and not getattr(thread, "_pyinc_context_bound", False):
                # A fresh snapshot per spawn: a Context may not be entered
                # twice, and the child must not share one with a sibling.
                spawning_context = copy_context()
                original_run = thread.run

                def run_in_spawning_context() -> None:
                    spawning_context.run(original_run)

                # Rebound on the instance, so Thread subclasses and Timer --
                # which define their own run() -- are covered without touching
                # the class.
                thread.run = run_in_spawning_context  # type: ignore[method-assign]
                thread._pyinc_context_bound = True  # type: ignore[attr-defined]
            original_thread_start(thread)

        builtins.open = guarded_open
        io.open = guarded_io_open
        os.getenv = guarded_getenv  # type: ignore[assignment]
        os.listdir = guarded_listdir
        os.scandir = guarded_scandir
        os.environ = guarded_environ  # type: ignore[assignment]  # noqa: B003
        Path.iterdir = guarded_path_iterdir  # type: ignore[assignment, method-assign]
        threading.Thread.start = guarded_thread_start  # type: ignore[assignment, method-assign]
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

    Calling `unsubscribe()` detaches exactly the registration that produced
    this handle -- equal callbacks and duplicate registrations each hold their
    own -- and no change committed after it returns reaches that registration.
    Repeated unsubscribes are no-ops. Like `observe`, `unsubscribe` is
    outside-only: called from a query body, a resource hook, or a thread
    spawned inside a running execution it raises `ReentrantDatabaseError`.
    Subscriptions do not keep the observed node alive under LRU eviction; if
    the node is evicted and later re-executed, the callback fires as normal.
    """

    __slots__ = ("_database", "_key", "_callback", "_token", "_active")

    def __init__(
        self,
        database: Database,
        key: NodeKey,
        callback: ObserverCallback,
        token: int,
    ) -> None:
        self._database = database
        self._key = key
        self._callback = callback
        self._token = token
        self._active = True

    def unsubscribe(self) -> None:
        self._database._reject_inside_query("Subscription.unsubscribe()")
        with self._database._state_lock:
            if not self._active:
                return
            self._active = False
            self._database._unregister_observer(self._key, self._token)


class Database:
    # The two instance-dictionary names a query handle carries its annotations
    # under. They are folded and observed through the annotation vocabulary
    # rather than the ambient-capture one, so an annotation naming a
    # module-anchored type stays an anchor instead of becoming a namespace
    # walk.
    _QUERY_HANDLE_ANNOTATION_NAMES: ClassVar[frozenset[str]] = frozenset(
        {"__annotate__", "__annotations__"}
    )
    # Instance-dictionary names the generic walk over a handle skips because
    # something else owns them: the contract fields the payloads beside the
    # handle fold directly, and `__wrapped__`, which the fold and the
    # observation each give an arm of their own. It stays on this list rather
    # than joining the walk: folded unconditionally, a handle's reference to
    # its own function would carry that function's defining module into the
    # ambient-capture route, which refuses a module with no stable source
    # identity -- every query defined in `__main__` among them.
    _QUERY_HANDLE_SIBLING_NAMES: ClassVar[frozenset[str]] = frozenset(
        {"fn", "eq", "cutoff", "key", "__wrapped__"}
    )
    # Instance-dictionary names `_query_handle_state_payload` folds by hand or
    # leaves to one of those siblings, so its walk over the rest of the handle
    # skips them. Class attributes rather than module constants: every module
    # constant this file defines is folded again for every query whose
    # annotations anchor to a pyinc type.
    _QUERY_HANDLE_CONTRACT_NAMES: ClassVar[frozenset[str]] = (
        _QUERY_HANDLE_ANNOTATION_NAMES
        | _QUERY_HANDLE_SIBLING_NAMES
        | frozenset({"__doc__", "__module__", "__name__", "__qualname__", "__type_params__"})
    )
    # Held here, not as a module constant: `_module_constants_payload` folds
    # this module's own module-level bindings when a query captures it, and an
    # installation path has no business inside a fingerprint. A class attribute
    # is skipped by that fold, and this value is only ever compared against.
    _STDLIB_DIRECTORY_PREFIX: ClassVar[str] = _stdlib_directory_prefix()

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
        if max_query_nodes is not None and (
            type(max_query_nodes) is not int or max_query_nodes <= 0
        ):
            raise ValueError("max_query_nodes must be a positive integer or None.")
        self.mode = mode
        self.max_query_nodes = max_query_nodes
        # Function-scope so this module keeps importing nothing from the
        # resource module at import time -- that module imports this one.
        from .resources import BUILTIN_ADAPTERS

        # The kernel's adapters for its own value types come first, so a caller
        # registering their own adapter for one of those types replaces the
        # entry instead of colliding with it. The replacement is a caller
        # adapter in every respect; `_partition_adapters_by_kernel_membership`
        # decides that by membership, below.
        #
        # Because the built-in entries are always present, that partition needs
        # their implementation digests at every construction -- derived on the
        # first construction in the process and read back after it, see
        # `_FIXED_ADAPTER_IMPLEMENTATION_DIGESTS`. That derivation is un-guarded
        # on purpose: a built-in that stopped fingerprinting cleanly would raise
        # out of `Database(...)` itself rather than be demoted to a caller
        # adapter and pay the full per-boundary cost silently. What keeps that
        # honest is a test, not a guard --
        # `test_the_builtin_file_stat_adapter_digests_cleanly`.
        self._adapters = {**BUILTIN_ADAPTERS, **dict(adapters or {})}
        # The registry is fixed for this database's lifetime, so the key-indexed
        # view every boundary exposure needs is built once here rather than per
        # exposure.
        self._view_adapter_registry = _AdapterRegistry(self._adapters)
        # Per-adapter-key implementation digests read from a loaded checkpoint's
        # manifest; the warm gate compares these against the live registry.
        self._checkpoint_adapter_digests: dict[str, str] = {}
        self._store = (
            _validated_store(store, "The store passed to Database(...)")
            if store is not None
            else None
        )
        self._revision = 0
        self._records: dict[NodeKey, NodeRecord] = {}
        # Both registries are keyed by the input's key string, which is the
        # whole of an input's identity: one entry per distinct key, however many
        # `Input` objects name it. `_inputs_by_key` holds the first object
        # registered under each key, as the comparand for the policy check.
        self._input_records: dict[str, NodeKey] = {}
        self._inputs_by_key: dict[str, Any] = {}
        self._query_records: set[NodeKey] = set()
        self._query_last_used: dict[NodeKey, int] = {}
        self._query_touch_counter = 0
        self._execution_stack: ContextVar[tuple[ExecutionFrame, ...]] = ContextVar(
            "pyinc_execution_stack",
            default=(),
        )
        self._allow_raw_reads: ContextVar[bool] = ContextVar("pyinc_allow_raw_reads", default=False)
        # How deep the calling context stands inside this database's resource
        # hooks; zero means outside them. The boundary predicate reads it rather
        # than an argument, so a probe -- which is handed no database and can
        # still hold one -- is covered exactly as a load is.
        self._resource_hook_depth: ContextVar[int] = ContextVar(
            "pyinc_resource_hook_depth", default=0
        )
        self._request_token: ContextVar[int | None] = ContextVar(
            "pyinc_request_token", default=None
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
        self._wrapped_callable_stack: ContextVar[tuple[int, ...]] = ContextVar(
            "pyinc_wrapped_callable_stack", default=()
        )
        self._query_handle_stack: ContextVar[tuple[int, ...]] = ContextVar(
            "pyinc_query_handle_stack", default=()
        )
        self._request_counter = 0
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
        self._query_fingerprint_memo: weakref.WeakKeyDictionary[
            Any,
            tuple[
                tuple[Any, ...],
                Any,
                str,
                tuple[tuple[ModuleType, Any], ...],
                tuple[tuple[Any, str], ...],
                tuple[tuple[ModuleType, tuple[str, ...], Any], ...],
                tuple[Any, ...],
            ],
        ] = weakref.WeakKeyDictionary()
        self._fingerprint_module_collector: ContextVar[dict[int, ModuleType] | None] = ContextVar(
            "pyinc_fingerprint_module_collector", default=None
        )
        self._fingerprint_attribute_collector: ContextVar[
            list[tuple[ModuleType, tuple[str, ...], Any]] | None
        ] = ContextVar("pyinc_fingerprint_attribute_collector", default=None)
        self._fingerprint_resource_collector: ContextVar[list[tuple[Any, str]] | None] = ContextVar(
            "pyinc_fingerprint_resource_collector", default=None
        )
        # Plain instance state, not a ContextVar like the request slots beside
        # it, and deliberately so: the only cross-thread effect is one request
        # replacing another's cache, which costs the loser fresh reads and can
        # never hand anyone a digest from a request that has already ended.
        # Degrading toward re-reading is the safe direction, so the weaker
        # container buys simplicity without opening a staleness class.
        self._request_resource_digests: dict[int, tuple[Any, str]] | None = None
        self._fingerprint_cacheable: ContextVar[bool] = ContextVar(
            "pyinc_fingerprint_cacheable", default=True
        )
        self._resource_registry: dict[NodeKey, tuple[Any, Any]] = {}
        self._call_snapshot_registry: dict[NodeKey, Any] = {}
        # Token-keyed so equal callbacks and duplicate
        # registrations each own one slot; insertion order is delivery order.
        self._observers: dict[NodeKey, dict[int, ObserverCallback]] = {}
        self._observer_token_counter = 0
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
        # Which registered adapters this database treats as fixed, and the
        # implementation digests it can therefore take once instead of at every
        # trust boundary. Runs here rather than beside the registry itself
        # because it fingerprints, and the fingerprint walk reads the request
        # slots above -- moved up beside `_adapters` it raises on
        # `_type_fingerprint_stack`.
        self._partition_adapters_by_kernel_membership()
        # Digest of each registered adapter's instance configuration, taken
        # once at construction. Mutating a registered adapter afterwards
        # violates the value-boundary law; every top-level request re-derives
        # the digest of each adapter named here and raises AdapterContractError
        # when one moved. An adapter whose configuration cannot be digested
        # contributes no entry: drift there is undetectable in-process and
        # enforcement falls back to the documented law (the checkpoint boundary
        # still refuses trust there), while every other adapter in the same
        # registry stays checked.
        self._registered_adapter_digests: dict[str, str] = (
            self._digestable_adapter_configuration_digests()
        )
        _install_guards_once()

    def _partition_adapters_by_kernel_membership(self) -> None:
        """Split the registry into the kernel's own fixed adapters and the rest.

        Fixed means one of the kernel's own entries, still the object the kernel
        put there -- decided by membership, same adapted type AND same adapter
        object, never by inspecting an adapter for signs of statelessness. A
        caller who registers their own adapter for one of those types replaces
        the entry, so their adapter lands on the non-fixed side and is treated
        exactly like any other caller adapter: full configuration verification
        at request scope, implementation digest re-derived at every trust
        boundary.

        A fixed adapter carries no instance state and its implementation lives
        in this package, so nothing an in-contract process can do moves its
        implementation digest. Those digests are therefore taken once per
        process, and the trust boundary reads them back instead of re-deriving
        them at each of its call sites -- the difference between a dict lookup
        and a fingerprint walk over two method bodies per boundary crossing.
        The same argument is why the derivation is memoized across databases
        rather than repeated per construction; see
        `_FIXED_ADAPTER_IMPLEMENTATION_DIGESTS`.
        """

        # Function-scope so this module keeps importing nothing from the
        # resource module at import time.
        from .resources import BUILTIN_ADAPTERS

        self._non_static_adapters: dict[type[Any], ValueAdapter] = {
            value_type: adapter
            for value_type, adapter in self._adapters.items()
            if BUILTIN_ADAPTERS.get(value_type) is not adapter
        }
        self._static_adapter_digests: dict[str, str] = {}
        for value_type, adapter in self._adapters.items():
            if value_type in self._non_static_adapters:
                continue
            key = _adapter_key(value_type)
            memo_key = (key, type(adapter))
            digest = _FIXED_ADAPTER_IMPLEMENTATION_DIGESTS.get(memo_key)
            if digest is None:
                # Still un-guarded, and still for the reason the merge states: a
                # fixed adapter that stopped fingerprinting cleanly raises out of
                # every construction in a fresh process, because nothing is
                # memoized until a derivation succeeds.
                digest = self._adapter_implementation_digest(adapter)
                _FIXED_ADAPTER_IMPLEMENTATION_DIGESTS[memo_key] = digest
            self._static_adapter_digests[key] = digest

    @property
    def revision(self) -> int:
        self._reject_inside_query("db.revision")
        with self._state_lock:
            return self._revision

    def statistics(self) -> DatabaseStatistics:
        self._reject_inside_query("db.statistics()")
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
        self._reject_inside_query("db.reset_statistics()")
        with self._state_lock:
            for key in self._stats:
                self._stats[key] = 0
            self._query_timings.clear()

    def query_profile(self) -> tuple[QueryProfile, ...]:
        self._reject_inside_query("db.query_profile()")
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
        self._reject_inside_query("db.dependency_graph()")
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
        # Ahead of the type check as well as the lock: a refusal that ran after
        # the key was resolved could leave a registration behind for an input
        # the caller was never allowed to declare here. The body extends the
        # same rule to the whole call. Every step that can fail -- the policy
        # check, the freeze, the caller's comparator, the store write -- runs
        # before the input is registered, so a `set` that raises for any reason
        # leaves the registry, the records, the counters and the revision
        # exactly as it found them, and the key stays free for whatever `set`
        # eventually declares it.
        self._reject_inside_query("db.set()")
        from .core import Input

        if not isinstance(input_key, Input):
            raise TypeError("db.set() expects an Input instance.")
        with self._state_lock:
            self._validate_input_registration(input_key)
            # Frozen but not published: the bytes reach the store on commit, so
            # a comparator that raises after a successful freeze cannot strand
            # an object nothing references.
            snapshot = freeze(value, adapters=self._view_adapter_registry)
            digest = fingerprint_snapshot(snapshot)
            # Resolved, not registered: an already-declared key comes back from
            # the registry and an undeclared one is built without being stored,
            # so a call that still fails has claimed nothing.
            node_key = self._input_node_key(input_key)
            record = self._records.get(node_key)
            equal = record is not None and self._compare_input_snapshots(
                input_key, record.snapshot, snapshot
            )
            # Commit, store first: publishing the bytes is the last step that
            # can fail, and it fails before the registration exists.
            self._persist_snapshot(snapshot)
            self._commit_input_registration(input_key)
            if record is not None and equal:
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
        # Ahead of the materialization below: draining the caller's iterable is
        # itself observable, and a refused call must leave it unpulled.
        self._reject_inside_query("db.set_many()")
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
                if not isinstance(input_key, Input):
                    raise TypeError("db.set_many() expects (Input, value) pairs.")
                if input_key.key in seen_keys:
                    raise InputKeyError(
                        f"db.set_many() received duplicate input key {input_key.key!r}."
                    )
                seen_keys.add(input_key.key)
                self._validate_input_registration(input_key)
                raw_pairs.append((input_key, value))

            # Freeze every value before running any user comparator. Neither
            # phase mutates database records, revisions, or statistics, and
            # nothing reaches the store until the whole batch is accepted --
            # so a comparator that raises leaves no unreferenced bytes behind.
            pending: list[tuple[Any, NodeKey, Any, str]] = []
            for input_key, value in raw_pairs:
                snapshot = freeze(value, adapters=self._view_adapter_registry)
                digest = fingerprint_snapshot(snapshot)
                node_key = self._input_node_key(input_key)
                pending.append((input_key, node_key, snapshot, digest))

            decisions: list[tuple[bool, Any, NodeKey, Any, str]] = []
            request_id = self._current_request_id()
            for input_key, node_key, snapshot, digest in pending:
                record = self._records.get(node_key)
                equal = record is not None and self._compare_input_snapshots(
                    input_key, record.snapshot, snapshot
                )
                decisions.append((equal, input_key, node_key, snapshot, digest))

            # Commit registrations and record changes only after every freeze
            # and comparator has succeeded. The store writes lead, being the
            # last step that can fail: a store that refuses one of the frozen
            # values leaves the batch entirely undeclared.
            for _input_key, _node_key, pending_snapshot, _digest in pending:
                self._persist_snapshot(pending_snapshot)
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
        # Before the lock, always: a thread spawned inside a query cannot be
        # told to wait for a lock its own parent is holding.
        self._reject_reentrant_read("db.get()")
        from .core import Query

        if not isinstance(query, Query):
            raise TypeError("db.get() expects a @query-decorated callable.")
        with self._state_lock, self._request_scope() as pending:
            key, call_snapshot = self._query_key(query, args, kwargs)
            had_record = key in self._records
            try:
                if not had_record and self._checkpoint_query_records:
                    self._try_warm_from_checkpoint(query, key, call_snapshot)
                self._ensure_query(query, key, call_snapshot)
            except Exception as exc:
                if not had_record:
                    self._discard_uncommitted_query(key)
                frame_now = self._current_frame()
                if not (
                    isinstance(exc, CycleError)
                    and frame_now is not None
                    and frame_now.key == key
                ):
                    # A caller that catches this raise returns a value built
                    # from a failure the graph does not describe: no edge to
                    # the node that raised is published, and nothing recorded
                    # for it describes the exception, so the caller's own
                    # answer can neither be re-derived nor re-verified from
                    # records. It is marked untracked instead -- re-executed
                    # on every request, kept out of checkpoints. The mark
                    # lands on the frame this call is unwinding into, which is
                    # the caller's: a node's own frame, where one was pushed at
                    # all, is popped before its exception reaches here. A
                    # caller that re-raises writes no record, so the reason is
                    # only ever read off a query that really did catch.
                    #
                    # The single exception above is a node refused for
                    # re-entering itself: the frame that would be marked is the
                    # very node this request names, so the refusal happened
                    # before any work started and nothing was read into a
                    # discarded frame -- and that request stays pinned to the
                    # registration this execution already owns. A cycle that
                    # reaches back through another query is not that shape:
                    # the branch that reached back read whatever it read first,
                    # and its frame is discarded with those reads in it.
                    self._mark_frame_impure(f"caught exception from sub-query '{key.label}'")
                raise
            self._record_dependency(key)
            result = cast(T, self._expose_boundary_snapshot(self._records[key].snapshot))
        self._dispatch_events(pending)
        return result

    def explain(self, query: _core.Query[P, Any], *args: P.args, **kwargs: P.kwargs) -> str:
        # Checked here rather than left to the inspect below, so the refusal
        # names the call the caller actually made.
        self._reject_inside_query("db.explain()")
        from .core import Query

        if not isinstance(query, Query):
            raise TypeError("db.explain() expects a @query-decorated callable.")
        return format_explanation(self.inspect(query, *args, **kwargs))

    def inspect(
        self, query: _core.Query[P, Any], *args: P.args, **kwargs: P.kwargs
    ) -> InspectionNode:
        self._reject_inside_query("db.inspect()")
        from .core import Query

        if not isinstance(query, Query):
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
        self._reject_inside_query("db.inspect_fresh()")
        from .core import Query

        if not isinstance(query, Query):
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
        self._reject_reentrant_read("db.request_span()")
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
        self._reject_inside_query("db.request_inputs_changed()")
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
        # A declared change is the one thing that may move a resource's
        # configuration inside a request, so the once-per-request digests go
        # with it and the span's next read re-reads.
        if self._request_resource_digests is not None:
            self._request_resource_digests.clear()
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
        self._span_epoch_seen.set(self._span_epoch)

    def observe(
        self,
        callback: ObserverCallback,
        query: _core.Query[P, Any],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Subscription:
        """Register `callback` to fire whenever the query node's value moves.

        Observer callbacks fire once per value move committed by a top-level
        `get` / `inspect` / `inspect_fresh` / `explain` call or by the calls
        inside a `request_span`: a cold execution, or a re-execution that
        advanced the node's `changed_at`. Backdated and reused decisions do not
        fire, and neither does a re-execution on a node marked untracked that
        re-landed a byte-identical value: such a node re-runs on every request
        and keeps the `changed_at` it had, so it announces nothing new.

        Each call to `observe` is its own registration with its own
        `Subscription` handle: registering the same callable twice delivers
        twice, and each handle detaches only its own registration. An event's
        recipients are the subscriptions that existed when the change committed
        and still exist when delivery begins.

        Callbacks run after the request scope completes and the kernel lock is
        released, so a callback may safely call back into the database.
        Exceptions from a callback are routed to the `observer_error_hook`
        (default: a one-line stderr log) and do not suppress sibling callbacks
        or corrupt kernel state. Both `observe` and `Subscription.unsubscribe`
        are outside-only and raise `ReentrantDatabaseError` from a query body, a
        resource hook, or a thread spawned inside a running execution.
        """
        # Registration is per call, and a query body runs only when the kernel
        # decides to execute it, so registering from one would make the
        # subscriber list a function of cache history.
        self._reject_inside_query("db.observe()")
        from .core import Query

        if not isinstance(query, Query):
            raise TypeError("db.observe() expects a @query-decorated callable.")
        if not callable(callback):
            raise TypeError("db.observe() expects a callable as its first argument.")
        with self._state_lock:
            key, _ = self._query_key(query, args, kwargs)
            self._observer_token_counter += 1
            token = self._observer_token_counter
            self._observers.setdefault(key, {})[token] = callback
        return Subscription(self, key, callback, token)

    def report_untracked_read(self, reason: str) -> None:
        # Before the lock, which this method takes ahead of everything else: a
        # spawned thread blocks there and never reaches the frame check below.
        # That check would let it through in any case -- a descendant inherits
        # its parent's frame.
        self._reject_reentrant_read("db.report_untracked_read()")
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

        The manifest records this database's mode, and only a database running
        that same mode can load the resulting checkpoint.

        Raises ``TypeError`` if a store passed here does not implement the
        ``ArtifactStore`` protocol.
        Raises ``ValueError`` if no ``ArtifactStore`` is available (either
        passed directly or configured via ``Database(store=...)``).
        """
        # Ahead of resolving the store, which is the first step towards the
        # cross-process lock a filesystem store takes to publish an object.
        self._reject_inside_query("db.save_checkpoint()")
        # A store configured at construction was validated there; one handed
        # over here is a fresh injection and gets the same check.
        _store = (
            _validated_store(store, "The store passed to save_checkpoint()")
            if store is not None
            else self._store
        )
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

        Where a record was skipped because the store holds *different* bytes
        under its digest, persisting the re-executed value back into that store
        raises the store's collision error rather than writing over it: the
        content address is already bound to bytes that disagree, and corruption
        surfaces loudly instead of being silently recomputed around on every
        run. A database with no store of its own -- one handed a store here and
        nowhere else -- reads through it without writing back.

        Raises ``TypeError`` if a store passed here does not implement the
        ``ArtifactStore`` protocol.
        Raises ``ValueError`` if no ``ArtifactStore`` is available.
        Raises ``KeyError`` if *key* is not found in the store.
        Raises ``CheckpointModeError`` if the checkpoint was saved by a database
        running a different mode.
        """
        self._reject_inside_query("db.load_checkpoint()")
        # A store handed over here outlives the call -- it serves every later
        # snapshot read -- so it is checked before the key is even looked up.
        _store = (
            _validated_store(store, "The store passed to load_checkpoint()")
            if store is not None
            else self._store
        )
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
            if dep_record.changed_at > record.verified_at:
                return True
        return False

    def _save_checkpoint_locked(self, store: ArtifactStore) -> str:
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
            and not self._record_is_stale_for_save(record)
            and (
                key.args_digest in self._checkpoint_snapshot_cache
                or (key.kind == "query" and key in self._call_snapshots())
                or (key.kind == "resource" and key in self._resource_objects())
            )
        }
        # A manifest is closed over its persisted query/resource dependencies.
        # If a stale child is omitted, every parent that references it is omitted
        # too, so a manifest never contains dangling dependency records.
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
                    self._persist_snapshot_to(call_snapshot, store)
            elif key.kind == "resource":
                resource_pair = self._resource_objects().get(key)
                if resource_pair is not None:
                    _resource, parameter = resource_pair
                    self._persist_snapshot_to(self._freeze_value(parameter), store)
                else:
                    parameter_snapshot = self._checkpoint_snapshot_cache.get(key.args_digest)
                    if parameter_snapshot is not None:
                        self._persist_snapshot_to(parameter_snapshot, store)
            deps: list[dict[str, Any]] = []
            for dep_key in record.dependencies:
                dep_record = self._records.get(dep_key)
                if dep_record is None:
                    continue
                if dep_key.kind == "input":
                    input_key = self._input_ident_for_key(dep_key)
                    input_obj = self._inputs_by_key[input_key]
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
            "mode": self.mode,
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
        if not isinstance(key, str) or not key.startswith("ck") or not self._is_digest(key[2:]):
            raise CheckpointIntegrityError(
                "Checkpoint keys must be 'ck' followed by a lowercase SHA-256 digest."
            )
        with self._allow_raw_reads_scope():
            manifest_bytes = store.get(key)
        if manifest_bytes is None:
            raise KeyError(f"Checkpoint key {key!r} not found in the ArtifactStore.")
        if not isinstance(manifest_bytes, bytes):
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
            isinstance(value, str)
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
        # Mode is a bare str alias, so the range is checked explicitly here, the
        # same way Database.__init__ checks the constructor argument.
        manifest_mode = manifest["mode"]
        if manifest_mode not in ("strict", "checked", "fast"):
            raise malformed("field 'mode' must be one of 'strict', 'checked', 'fast'.")
        if manifest_mode != self.mode:
            raise CheckpointModeError(
                f"Checkpoint {key!r} was saved in mode {manifest_mode!r}, but this "
                f"database runs in mode {self.mode!r}; refusing to load. A checkpoint "
                f"warms only a database running in the mode that saved it."
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
        if not isinstance(payload, bytes) or hashlib.sha256(payload).hexdigest() != digest:
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
        # The root call snapshot is materialized per mode to obtain the
        # arguments passed to the query, and an adapted value is reconstructed
        # through its adapter on every one of those paths. A changed adapter
        # can therefore alter a fresh execution's inputs even when the saved
        # result itself uses only native values.
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
        if snapshot is _MISSING_SNAPSHOT:
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
        if snapshot is _MISSING_SNAPSHOT:
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
        return record.digest == expected_digest

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
            return record.digest == expected_digest
        # Prefer warming the dep's subtree from the checkpoint (no execution:
        # resources come back via probe hints). If the subtree can't be warmed
        # -- e.g. it reaches a resource unresolvable from the pinned captures --
        # verify the dep by re-execution instead.
        if self._warm_checkpoint_dep_query(dep_key):
            return self._records[dep_key].digest == expected_digest
        return self._execute_to_verify_query_dep(dep, dep_key, expected_digest)

    def _execute_to_verify_query_dep(
        self, dep: dict[str, Any], dep_key: NodeKey, expected_digest: str
    ) -> bool:
        """Verify a query dep by re-executing its pinned code against live state.

        Used when a query dep cannot be warmed from the checkpoint. Recovers the
        dep's call snapshot from the store (content-addressed by its args_digest;
        missing/corrupt ⇒ degrade to warm refusal), runs the pinned Query live --
        so its resources are probed against the real world -- and compares the
        resulting digest to the manifest's expectation. Equal ⇒ verified and now
        live (downstream warming can reuse it); different ⇒ refuse.
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
        except Exception:
            self._discard_uncommitted_query(dep_key)
            return False
        record = self._records.get(dep_key)
        return record is not None and record.digest == expected_digest

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
            return record.digest == expected_digest
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
        resource, parameter = resolved
        self._resource_objects()[dep_key] = (resource, parameter)
        try:
            self._refresh_resource(resource, parameter, dep_key)
        except Exception:
            if dep_key not in self._records:
                self._resource_objects().pop(dep_key, None)
            return False
        record = self._records.get(dep_key)
        return record is not None and record.digest == expected_digest

    def _resolve_checkpoint_resource(self, dep_key: NodeKey) -> tuple[Any, Any] | None:
        """Resolve (resource object, parameter) for a checkpoint resource dep.

        The object comes from the warm root's pinned captures (matched on the
        resource's content identity); the parameter is thawed from the store,
        content-addressed by the dep's args_digest. Any missing piece ⇒ None,
        which the caller treats as "cannot verify from the checkpoint".
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
        parameter = self._thaw_value(parameter_snapshot)
        live_parameter_type_digest = fingerprint_snapshot(
            (
                "resource-parameter-types-v3",
                self._resource_configuration_type_payload(parameter),
            )
        )
        if live_parameter_type_digest != parameter_type_digest:
            return None
        # Round-trip guard: the resource must be re-probed/loaded with a parameter
        # structurally identical to the one it was keyed by. Thawing is lossy for
        # values with no reconstructor -- a frozen dataclass parameter thaws to a
        # plain dict -- so re-freeze the thawed parameter and require it to hash
        # back to this dep's args_digest (computed the same way in _resource_key).
        # A mismatch means we would drive the resource with a different-shaped
        # parameter (probe/load raising, or a stale value under this dep_key);
        # refuse so the caller re-executes live with the real parameter instead.
        if fingerprint_snapshot(self._freeze_value(parameter)) != dep_key.args_digest:
            return None
        return resource, parameter

    def _load_snapshot_from_store(self, digest: str) -> Snapshot | object:
        if digest in self._checkpoint_snapshot_cache:
            return self._checkpoint_snapshot_cache[digest]
        store = self._store or self._checkpoint_load_store
        if store is None:
            return _MISSING_SNAPSHOT
        return self._read_validated_snapshot(store, digest)

    def _persist_snapshot_to(self, snapshot: Snapshot, store: ArtifactStore) -> None:
        """Publish the snapshot's serialized bytes under its content address.

        The single verifying persist: every path that writes a snapshot goes
        through here. Presence is never evidence -- a digest already in the
        store is compared byte for byte against what this snapshot serializes
        to, so a save can no longer report success against a store holding
        bytes the database could never warm from.

        Raw filesystem I/O runs under the raw-read allow scope so a
        `FileSystemArtifactStore` used while a query frame is active is not
        rejected by the global guard.
        """
        digest = fingerprint_snapshot(snapshot)
        payload = serialize_snapshot(snapshot)
        with self._allow_raw_reads_scope():
            if store.get(digest) != payload:
                # Missing (None) or present-but-different: put either publishes
                # the bytes or raises the store's own collision error. The
                # authoritative comparison is the one put makes, so this read is
                # a filter rather than a trust decision -- a stale answer here
                # costs a redundant put and changes no outcome.
                store.put(digest, payload)

    def _find_input_node_by_key(self, input_key: str) -> NodeKey | None:
        return self._input_records.get(input_key)

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
        self._reject_reentrant_read("db.read_input()")
        from .core import Input

        if not isinstance(input_key, Input):
            raise TypeError("db.read_input() expects an Input instance.")
        with self._state_lock:
            # A read resolves; it never declares. The policy check still runs,
            # because two Inputs naming one key under different notions of
            # "changed" is a programming error wherever it surfaces -- but it
            # validates without mutating, so a read of a key nothing has set
            # leaves that key free for the `set` that eventually declares it.
            self._validate_input_registration(input_key)
            key = self._find_input_node_by_key(input_key.key)
            record = self._records.get(key) if key is not None else None
            if key is None or record is None:
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
        self._reject_reentrant_read("db.read_resource()")
        with self._state_lock, self._request_scope() as pending:
            key = self._resource_key(resource, parameter)
            outcome = _RefreshOutcome()
            try:
                self._refresh_resource(resource, parameter, key, outcome)
                self._record_dependency(key)
                result = self._expose_boundary_snapshot(self._records[key].snapshot)
            except Exception as exc:
                # A load that raised is still an observation: when it left a
                # failure record behind, the reader depends on it exactly as it
                # would on a value, so the edge is recorded before unwinding.
                if key in self._records:
                    self._record_dependency(key)
                else:
                    self._resource_objects().pop(key, None)
                    if isinstance(exc, ReentrantDatabaseError):
                        # A hook refused on a resource this database has never
                        # loaded leaves nothing at all: no record to depend on
                        # and no stored probe to retire. A body that catches
                        # the refusal would otherwise commit an answer with no
                        # edge to anything and be reused from then on -- still
                        # answering with its fallback once the hook is rewritten
                        # to stop reading the database, where a fresh database
                        # returns the value. The untracked mark is what is left
                        # to force it to derive its answer again.
                        self._mark_frame_impure(f"caught refusal from resource '{key.label}'")
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
        raw_reads_token = self._allow_raw_reads.set(False)
        try:
            # The guard covers the whole query boundary, not just the body:
            # materializing arguments runs adapter thaws and freezing the
            # result runs adapter freezes, and an ambient read in either
            # smuggles untracked state into the stored snapshot.
            with self._guard_untracked_reads():
                query_args, query_kwargs = self._materialize_call(
                    call_snapshot,
                    record_boundaries=self.mode == "checked",
                    frame=frame,
                )
                t0 = time.perf_counter_ns()
                result = query.fn(self, *query_args, **query_kwargs)
                elapsed = time.perf_counter_ns() - t0
                if self.mode == "checked":
                    for before, value in zip(
                        frame.boundary_fingerprints, frame.boundary_values, strict=True
                    ):
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
                value_moved = True
            else:
                record = previous
                previous_changed_at = previous.changed_at
                previous_digest = previous.digest
                if query.eq is None and query.cutoff is None:
                    # Both operands are canonical freeze outputs: the fresh
                    # snapshot from _freeze_value above, the previous one from
                    # an earlier freeze or a validated checkpoint load. The
                    # decision is the one canonical relation, snapshots_equal
                    # -- equality of the canonical encodings of the stored
                    # snapshots, the same decision in every mode, with no thaw
                    # and no second opinion. A NaN normalizes to a single
                    # canonical encoding, so it is reflexive here by
                    # construction rather than through a digest side channel.
                    # The digest test in front is a filter, not a verdict: a
                    # digest is sha256 of that same encoding and a query
                    # record's snapshot is never written without the matching
                    # digest beside it (store loads re-derive and check the
                    # pair on the way in), so unequal digests prove unequal
                    # encodings and settle the changed case without encoding
                    # anything. Equal digests decide nothing on their own --
                    # the byte comparison still runs, so no collision is ever
                    # trusted.
                    equal = (
                        not impure
                        and digest == previous_digest
                        and snapshots_equal(previous.snapshot, snapshot)
                    )
                else:
                    old_value = self._policy_operand(previous.snapshot)
                    new_value = self._policy_operand(snapshot)
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
                record.snapshot = snapshot
                record.digest = digest
                if equal:
                    record.changed_at = previous_changed_at
                    decision = "backdated"
                    value_moved = False
                elif impure and digest == previous_digest:
                    # `equal` was forced above, not observed: an untracked
                    # read skips the comparison entirely. When the re-run
                    # then lands a byte-identical snapshot there is no new
                    # value to propagate, so keep the old changed_at and
                    # leave the revision alone -- otherwise a stable impure
                    # leaf churns the counter on every warm request. This
                    # digest short-circuit applies only to the forced case:
                    # when a comparison actually ran and said unequal (a
                    # custom eq policy may, even for identical snapshots),
                    # the bump below stands.
                    record.changed_at = previous_changed_at
                    decision = "executed"
                    value_moved = False
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
                    value_moved = True
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
                # Delivery follows the value, not the decision: a re-run
                # that kept the previous changed_at landed nothing new to
                # announce, while a cold execution always did.
                if value_moved:
                    self._enqueue_observer_event(query, key, record)
            self._query_timings.setdefault(key, _TimingAggregate()).add(elapsed)
        finally:
            # First, before the tokens go back: a thread spawned inside this
            # execution holds a snapshot of the stack that still contains this
            # frame, and the flag is the only thing that tells it the
            # execution it descended from is over.
            frame.completed = True
            self._allow_raw_reads.reset(raw_reads_token)
            self._execution_stack.reset(token)

    def _enqueue_observer_event(self, query: Any, key: NodeKey, record: NodeRecord) -> None:
        subscribed = self._observers.get(key)
        if not subscribed:
            return
        pending = self._pending_events.get()
        if pending is None:
            return
        # The recipients of an event are the subscriptions that existed
        # when the change committed, minus any that end before delivery
        # starts -- membership is re-checked once at dispatch entry.
        pending.append(
            (
                key,
                tuple(subscribed),
                QueryChangeEvent(
                    query_id=query.key,
                    args_digest=key.args_digest,
                    decision="executed",
                    changed_at=record.changed_at,
                    verified_at=record.verified_at,
                ),
            )
        )

    def _unregister_observer(self, key: NodeKey, token: int) -> None:
        with self._state_lock:
            callbacks = self._observers.get(key)
            if callbacks is None:
                return
            if callbacks.pop(token, None) is None:
                return
            if not callbacks:
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
        # Filtered once, before the delivery loop: a subscription removed
        # during dispatch still receives events already snapshotted here,
        # and one added after a change never receives that change.
        with self._state_lock:
            snapshots: list[tuple[QueryChangeEvent, tuple[ObserverCallback, ...]]] = []
            for key, tokens, event in events:
                live = self._observers.get(key, {})
                snapshots.append(
                    (event, tuple(live[token] for token in tokens if token in live))
                )
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
                self._ensure_query(query_obj, key, call_snapshot)
        elif key.kind == "resource":
            resource_pair = self._resource_objects().get(key)
            if resource_pair is None:
                return True
            resource, parameter = resource_pair
            outcome = _RefreshOutcome()
            try:
                self._refresh_resource(resource, parameter, key, outcome)
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
        return self._records[key].is_untracked or self._records[key].changed_at > revision

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
        atomic = callable(getattr(resource, "probe_and_load", None))
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
                with self._resource_hook_scope():
                    early_probe = resource.probe(parameter)
            except Exception:
                pass
            else:
                early_probe_snapshot = freeze(early_probe, adapters=self._adapters)
                if self._reuse_on_probe_hit(record, early_probe_snapshot, current_request):
                    return
                if record is None and self._restore_from_probe_hint(
                    key, early_probe_snapshot, current_request
                ):
                    return
        if atomic:
            try:
                with self._resource_hook_scope():
                    probe, loaded_value = resource.probe_and_load(self, parameter)
            except ReentrantDatabaseError:
                # A hook that read back into the database observed nothing about
                # the outside world, so there is no failure to record. Writing a
                # failure record would store a probe for this refusal and let a
                # later read match on it, which would turn a hook that has to be
                # rewritten into a value the graph quietly carries. The refresh
                # above still marks an existing record unconfirmed, which is
                # what retires the probe its last real observation stored.
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
            with self._resource_hook_scope():
                probe = resource.probe(parameter)
            loaded_value = None
        probe_snapshot = freeze(probe, adapters=self._adapters)
        if self._reuse_on_probe_hit(record, probe_snapshot, current_request):
            return
        if record is None and self._restore_from_probe_hint(key, probe_snapshot, current_request):
            return
        if not atomic:
            try:
                with self._resource_hook_scope():
                    loaded_value = resource.load(self, parameter)
            except ReentrantDatabaseError:
                # As above: a refused read is not an observation of the world,
                # so it writes no failure record for a later read to match on.
                raise
            except Exception as exc:
                outcome.failure_recorded = self._record_resource_failure(
                    key, record, probe_snapshot, exc, current_request
                )
                raise
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
            if snapshot is not _MISSING_SNAPSHOT and self._adapter_keys_trusted(
                collect_adapter_keys(snapshot)
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
            with self._resource_hook_scope():
                return freeze(resource.probe(parameter), adapters=self._adapters)
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
        key = NodeKey(
            kind="query",
            identity=f"{query.key}:{query_fingerprint}",
            args_digest=args_digest,
            label=f"{query.key}[{args_digest[:12]}] {query.__name__}()",
        )
        self._query_objects()[key.identity] = query
        self._call_snapshots()[key] = call_snapshot
        return key, call_snapshot

    def _validate_input_registration(self, input_key: Any) -> None:
        """Refuse a key already declared under a different equality policy.

        The registration family's one validation, called once per call by
        every path that can declare or resolve an input -- so
        `_commit_input_registration` is the pure mutation it reads as, and the
        read path can run the check without registering anything.
        """
        existing = self._inputs_by_key.get(input_key.key)
        if existing is input_key:
            # The registered object measured against itself: there is no second
            # policy here to disagree with, so the two digests below could only
            # ever agree. Skipping them keeps a repeated `set` of a long-lived
            # `Input` off the policy-fingerprinting path entirely.
            return
        policy_digest = self._input_policy_digest(input_key)
        if existing is not None and self._input_policy_digest(existing) != policy_digest:
            raise InputKeyError(
                f"Input key {input_key.key!r} is already registered with a conflicting "
                "equality/cutoff policy."
            )

    def _prospective_input_key(self, input_key: Any) -> NodeKey:
        """The node key the input would take, built without declaring anything.

        A pure function of one read of the key string, so the identity the node
        is addressed by and the identity its label spells are the same string by
        construction -- which is what lets the registry hold a single node per
        key however many `Input` objects name it.
        """
        identity = input_key.key
        return NodeKey(
            kind="input",
            identity=identity,
            args_digest="",
            label=f"input[{identity}]",
        )

    def _input_node_key(self, input_key: Any) -> NodeKey:
        """The registered node key for this input, or the one it would take.

        A key already in the registry is served from it rather than rebuilt, so
        setting an input a second time costs a lookup. An unregistered key is
        constructed and not stored, leaving it free for whatever `set` finally
        declares it.
        """
        registered = self._input_records.get(input_key.key)
        if registered is not None:
            return registered
        return self._prospective_input_key(input_key)

    def _commit_input_registration(self, input_key: Any) -> NodeKey:
        """Declare the input. Called only once a write has already succeeded.

        Idempotent and unconditional: validation happened before the caller
        committed to the write, so nothing here refuses and nothing here can
        fail part way. Re-declaring a key already registered keeps the first
        `Input` object as the comparand and adds nothing, so the registry is
        sized by distinct keys rather than by how often they are set.
        """
        key = self._input_records.get(input_key.key)
        if key is not None:
            return key
        key = self._prospective_input_key(input_key)
        self._input_records[input_key.key] = key
        self._inputs_by_key[input_key.key] = input_key
        return key

    def _resource_key(self, resource: Any, parameter: Any) -> NodeKey:
        frozen_parameter = self._freeze_value(parameter)
        parameter_digest = fingerprint_snapshot(frozen_parameter)
        resource_identity = fingerprint_snapshot(self._resource_identity_payload(resource))
        parameter_type_digest = fingerprint_snapshot(
            (
                "resource-parameter-types-v3",
                self._resource_configuration_type_payload(parameter),
            )
        )
        label = resource.label(parameter)
        # Exactness before emptiness, as at the input and query key boundaries:
        # this label becomes the node's label, so a subclass would decide the
        # rendering and the emptiness check for it.
        if type(label) is not str:
            if isinstance(label, str):
                raise TypeError(
                    "Resource.label() must return exactly str; got str subclass "
                    f"{type(label).__qualname__}."
                )
            raise TypeError("Resource.label() must return a string.")
        if not label:
            raise ValueError("Resource.label() must return a non-empty string.")
        key = NodeKey(
            kind="resource",
            identity=(
                f"{type(resource).__module__}:{type(resource).__qualname__}:"
                f"{resource_identity}:{parameter_type_digest}"
            ),
            args_digest=parameter_digest,
            label=label,
        )
        self._resource_objects()[key] = (resource, parameter)
        return key

    def _materialize_call(
        self, call_snapshot: Any, *, record_boundaries: bool, frame: ExecutionFrame
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        if self.mode == "strict":
            envelope = self._strict_snapshot_view(
                call_snapshot, adapters=self._view_adapter_registry
            )
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
    def _strict_snapshot_view(
        cls, snapshot: Any, *, adapters: _AdapterRegistry | None = None
    ) -> Any:
        """Expose a snapshot through rebuilt immutable container interfaces.

        Every `Frozen*` shell is rebuilt, graph or not: frozen dataclass
        setters refuse plain writes, but `object.__setattr__` bypasses them,
        so a view aliasing the stored snapshot would let a caller corrupt the
        record it came from. Leaf values and all-leaf tuples are shared —
        nothing reflective can rebind their contents.

        With *adapters* supplied, an adapted value is reconstructed through
        its registered adapter, so a caller boundary hands back the type the
        adapter builds rather than the kernel's internal wrapper. The adapter
        is handed the already-rebuilt payload and the rebuild callable, never
        the stored snapshot: the no-aliasing invariant above must not depend
        on an adapter cooperating. Callers with no registry — the structural
        validators — keep the wrapper.
        """

        if type(snapshot) is not FrozenGraph:
            return cls._detached_snapshot_view(snapshot, adapters=adapters)

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
                if adapters is None:
                    return FrozenAdapterValue(value.adapter_key, resolve(value.payload))
                adapter = adapters.for_key(value.adapter_key)
                if adapter is None:
                    raise UnsupportedValueError(
                        f"Cannot thaw adapted snapshot for {value.adapter_key!r} "
                        "without the matching adapter registry."
                    )
                return adapter.thaw(resolve(value.payload), resolve)
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
    def _detached_snapshot_view(
        cls, value: Any, *, adapters: _AdapterRegistry | None = None
    ) -> Any:
        detach = cls._detached_snapshot_view
        if type(value) is FrozenList:
            return FrozenList(tuple(detach(item, adapters=adapters) for item in value.items))
        if type(value) is FrozenDict:
            return FrozenDict(
                tuple(
                    (detach(key, adapters=adapters), detach(item, adapters=adapters))
                    for key, item in value.entries
                )
            )
        if type(value) is FrozenSet:
            return FrozenSet(
                value.kind, tuple(detach(item, adapters=adapters) for item in value.items)
            )
        if type(value) is FrozenRecord:
            return FrozenRecord(
                value.type_name,
                tuple((key, detach(item, adapters=adapters)) for key, item in value.entries),
            )
        if type(value) is FrozenAdapterValue:
            if adapters is None:
                return FrozenAdapterValue(
                    value.adapter_key, detach(value.payload, adapters=adapters)
                )
            adapter = adapters.for_key(value.adapter_key)
            if adapter is None:
                raise UnsupportedValueError(
                    f"Cannot thaw adapted snapshot for {value.adapter_key!r} "
                    "without the matching adapter registry."
                )
            return adapter.thaw(
                detach(value.payload, adapters=adapters),
                lambda item: detach(item, adapters=adapters),
            )
        if type(value) is FrozenGraph:
            return cls._strict_snapshot_view(value, adapters=adapters)
        if type(value) is tuple:
            detached = tuple(detach(item, adapters=adapters) for item in value)
            if all(item is original for item, original in zip(detached, value, strict=True)):
                return value
            return detached
        return value

    def _expose_snapshot(
        self,
        snapshot: Any,
        *,
        record_boundaries: bool = False,
        frame: ExecutionFrame | None = None,
    ) -> Any:
        # Exposure always detaches: the stored snapshot itself is never handed
        # out, so nothing done to what came back can reach the record. Callers
        # never see the FrozenGraph envelope either -- a graph-shaped result is
        # rebuilt into shared/cyclic Frozen* views, exactly as _materialize_call
        # does for call arguments.
        if self.mode == "strict":
            exposed = self._strict_snapshot_view(snapshot, adapters=self._view_adapter_registry)
        else:
            exposed = self._thaw_value(snapshot)
        if record_boundaries and frame is not None:
            frame.boundary_fingerprints.append(self._fingerprint_value(exposed))
            frame.boundary_values.append(exposed)
        return exposed

    def _policy_operand(self, snapshot: Any) -> Any:
        """Detach a stored snapshot before a user eq=/cutoff= policy sees it.

        A policy operand is exposed exactly as a caller-boundary value is,
        without the checked-mode mutation bookkeeping: strict rebuilds every
        Frozen* shell -- _strict_snapshot_view exists precisely because
        object.__setattr__ bypasses frozen-dataclass setters, and it handles
        FrozenGraph cycles, so a cyclic result is comparable without
        re-freezing -- while checked and fast thaw, which already allocates
        fresh containers. Either way the operand shares no mutable shell with
        the record it came from, and either way an adapted value reaches the
        policy as the type its adapter rebuilds rather than as the kernel's
        internal wrapper.
        """

        return self._expose_snapshot(snapshot)

    def _expose_boundary_snapshot(self, snapshot: Any) -> Any:
        frame = self._current_frame()
        return self._expose_snapshot(
            snapshot,
            record_boundaries=self.mode == "checked" and frame is not None,
            frame=frame,
        )

    def _record_dependency(self, key: NodeKey) -> None:
        frame = self._current_frame()
        if frame is None:
            return
        frame.dependencies.add(key)

    def _mark_frame_uncheckpointable(self) -> None:
        frame = self._current_frame()
        if frame is None:
            return
        frame.checkpointable = False

    def _mark_frame_impure(self, reason: str) -> None:
        """Mark the running execution as resting on state no record describes.

        The untracked mark ``db.report_untracked_read`` records, plus the
        checkpoint exclusion, for a failure the kernel itself could not record.
        """
        frame = self._current_frame()
        if frame is None:
            # Nothing is executing -- a top-level caller is about to see this,
            # and it stores no answer that could be reused.
            return
        frame.untracked_reasons.append(reason)
        # Both halves: the reason is what forces re-execution, while the
        # checkpoint eligibility filter reads `checkpointable` on its own.
        self._mark_frame_uncheckpointable()

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

    def _resource_objects(self) -> dict[NodeKey, tuple[Any, Any]]:
        return self._resource_registry

    def _call_snapshots(self) -> dict[NodeKey, Any]:
        return self._call_snapshot_registry

    @contextmanager
    def _allow_raw_reads_scope(self) -> Iterator[None]:
        token = self._allow_raw_reads.set(True)
        try:
            yield
        finally:
            self._allow_raw_reads.reset(token)

    @contextmanager
    def _resource_hook_scope(self) -> Iterator[None]:
        """Run a resource hook: raw reads permitted, reads of this database refused.

        Observing the outside world is the whole job, so the raw-read allowance
        the plain scope grants is exactly what a hook needs and is delegated to
        unchanged. What the depth adds is the other half: a hook that reads back
        into the database hides that read behind the resource node, where no
        edge records it and an unchanged probe skips the hook that made it
        entirely. The depth is what the boundary predicate sees, so the refusal
        reaches a `probe` too -- it is handed no database and can still hold
        one.

        The hook also registers on `_ACTIVE_GUARDS` for its extent, which is how
        `guarded_thread_start` finds it: a `read_resource` made at top level
        opens no execution, so without this the spawn hook would scan an empty
        tuple and a thread started from inside such a hook would inherit
        nothing. Registering changes no raw read, whether or not a query is
        running above: `_raise_if_guarded` refuses only where a live frame has
        no raw-read permission, and a hook always has that permission. Under a
        query the entry is a duplicate of one already there, which costs that
        scan a second identical check and nothing else.
        """
        token = self._resource_hook_depth.set(self._resource_hook_depth.get() + 1)
        try:
            with self._guard_untracked_reads(), self._allow_raw_reads_scope():
                yield
        finally:
            self._resource_hook_depth.reset(token)

    @contextmanager
    def _guard_untracked_reads(self) -> Iterator[None]:
        stack = _ACTIVE_GUARDS.get()
        token = _ACTIVE_GUARDS.set(stack + (self,))
        try:
            yield
        finally:
            _ACTIVE_GUARDS.reset(token)

    def _boundary_state(self) -> Literal["outside", "inside", "hook", "descendant"]:
        """Where the calling thread stands relative to this database's executions.

        `"outside"` — no execution of this database is live on the calling
        context, so every entry point is open. `"inside"` — this very thread is
        running that execution, which is a query body calling back into the
        database it was handed. `"descendant"` — the live execution belongs to
        another thread and this one inherited it by being spawned inside it.
        `"hook"` — the caller is inside one of this database's resource hooks,
        which is the most specific of the four and so is answered first: a hook
        usually runs under a query frame, and it is the hook, not the frame,
        that decides what the caller may do.
        """
        if self._resource_hook_depth.get() > 0:
            return "hook"
        frame = self._current_frame()
        if frame is None:
            return "outside"
        if frame.thread_ident == threading.get_ident():
            return "inside"
        return "descendant"

    def _reject_inside_query(self, name: str) -> None:
        """Refuse `name` unless the caller is outside every execution of this database.

        `name` is the public spelling of the entry point, e.g. `"db.set()"`.
        """
        state = self._boundary_state()
        if state == "outside":
            return
        raise ReentrantDatabaseError(f"{name} is not allowed {_BOUNDARY_REJECTION_REASONS[state]}.")

    def _reject_reentrant_read(self, name: str) -> None:
        """Refuse `name` from a descendant thread or from inside a resource hook.

        The in-query read surface itself stays open: a query body reading its
        own inputs and resources is the point of the frame. What cannot be
        served is the same call from a thread spawned inside that body: it
        would block on the state lock the executing thread is still holding,
        and where that thread is waiting for the child, neither comes back.
        Refusing turns a hang into a diagnosable error.

        A hook is refused for a different reason. It would not hang -- the lock
        is reentrant and the call would answer -- but the answer would be
        invisible to the graph: the resource node records the probe and the
        value, never what the hook read to produce them, so a warm request that
        skips the hook on an unchanged probe reuses a value no fresh database
        would have produced. Database-derived values reach a resource through
        its key instead, which the reading query passes in and declares.
        """
        state = self._boundary_state()
        if state in ("descendant", "hook"):
            raise ReentrantDatabaseError(
                f"{name} is not allowed {_BOUNDARY_REJECTION_REASONS[state]}."
            )

    def _query_fingerprint(self, query: Any) -> str:
        cached = self._query_fingerprint_memo.get(query)
        runtime_build = self._runtime_build_payload()
        definition_observation = self._query_definition_observation(query)
        if (
            cached is not None
            and cached[0] == runtime_build
            and self._definition_observation_matches(cached[1], definition_observation)
            # Cheapest arm first: re-resolving a chain is a lookup per path
            # segment, where a module stamp hashes a file and a resource digest
            # re-runs identity(). Every arm but the last is a pure read, so
            # among those the order only decides what a mismatch costs before
            # it is found. The resource arm goes last because it can refuse,
            # and a moved resource means the resource redefined itself only
            # once nothing else about the query has moved.
            and all(
                self._resolve_module_path_target(module, path) is expected
                for module, path, expected in cached[5]
            )
            # Reads the recorded targets, which the arm above has just proved
            # are the objects those chains still name.
            and self._definition_observation_matches(
                cached[6], self._module_function_target_observation(cached[5])
            )
            and all(
                self._module_observation_stamp(module) == expected for module, expected in cached[3]
            )
            and self._resource_identities_hold(cached[4])
        ):
            return cached[2]

        modules: dict[int, ModuleType] = {}
        resources: list[tuple[Any, str]] = []
        attributes: list[tuple[ModuleType, tuple[str, ...], Any]] = []
        collector_token = self._fingerprint_module_collector.set(modules)
        resource_token = self._fingerprint_resource_collector.set(resources)
        attribute_token = self._fingerprint_attribute_collector.set(attributes)
        cacheable_token = self._fingerprint_cacheable.set(True)
        try:
            result = fingerprint_snapshot(
                (
                    "query-v3",
                    self._code_fingerprint(query.fn),
                    self._policy_definition_payload(query.eq),
                    self._policy_definition_payload(query.cutoff),
                    self._query_handle_state_payload(query, set()),
                )
            )
        finally:
            cacheable = self._fingerprint_cacheable.get()
            self._fingerprint_cacheable.reset(cacheable_token)
            self._fingerprint_attribute_collector.reset(attribute_token)
            self._fingerprint_resource_collector.reset(resource_token)
            self._fingerprint_module_collector.reset(collector_token)
        module_observations = tuple(
            (module, self._module_observation_stamp(module))
            for _module_id, module in sorted(modules.items(), key=lambda item: item[1].__name__)
        )
        # One pair per resource object: a resource reached from several slots
        # of one walk is stored once and digests identically every time, so
        # the repeats only cost the guard re-reads.
        deduped_resources: dict[int, tuple[Any, str]] = {}
        for observed_resource, observed_digest in resources:
            deduped_resources.setdefault(id(observed_resource), (observed_resource, observed_digest))
        # One entry per (module, path) chain: the same chain folded from several
        # slots of one walk reads the same live target every time, so keeping
        # the first record loses nothing and the guard re-resolves it once. The
        # records hold every module they name, so no id can be freed and reused
        # while this loop keys on one.
        deduped_attributes: dict[
            tuple[int, tuple[str, ...]], tuple[ModuleType, tuple[str, ...], Any]
        ] = {}
        for observed_module, observed_path, observed_target in attributes:
            deduped_attributes.setdefault(
                (id(observed_module), observed_path),
                (observed_module, observed_path, observed_target),
            )
        attribute_records = tuple(deduped_attributes.values())
        if cacheable:
            self._query_fingerprint_memo[query] = (
                runtime_build,
                definition_observation,
                result,
                module_observations,
                tuple(deduped_resources.values()),
                attribute_records,
                self._module_function_target_observation(attribute_records),
            )
        else:
            self._query_fingerprint_memo.pop(query, None)
        return result

    def _query_handle_state_payload(self, query: Any, seen_functions: builtins.set[int]) -> Any:
        """Fold the Query handle's own mutable surface into query identity.

        The handle is a plain object: functools.wraps copies function metadata
        onto it at decoration time, nothing prevents later assignment, and a
        query body may read attributes off its own handle. Any of them moving
        has to move identity exactly as the equivalent function attribute does,
        which is what makes writing one a supported way to reparameterize a
        query instead of a change the stored records cannot see.

        The contract fields are excluded: `fn`, `eq` and `cutoff` are folded by
        the payloads beside this one, and `key` names the node this fingerprint
        is spliced into. Routing them through the ambient-capture digest would
        also refuse the callable policy objects the policy payload accepts, and
        fold the query's own function a second time.

        `__wrapped__` is conditional rather than excluded. While it still points
        at `fn`, a marker stands in for it and the sibling payload owns it; once
        it points anywhere else it is folded like any other attribute, so
        rebinding it moves identity exactly as rebinding it on a plain captured
        function does.

        Annotations and type parameters take the annotation vocabulary the
        function metadata payload uses, not the ambient-capture one: an
        annotation naming a module-anchored type is pinned by that anchor,
        where the capture digest would walk the type's whole namespace. Where
        the handle still carries the function's own evaluator or its own
        annotations dictionary -- the objects functools.wraps copied across --
        a marker stands in for their content, which that same metadata payload
        has already folded; only a handle given annotations of its own is
        folded here.

        A query held on another query's handle is folded as the dependency it
        is, which puts a reference cycle within reach. Such a cycle is marked
        rather than refused: the contact that entered the handle folds
        everything the repeat would fold again, so eliding the back edge loses
        nothing, and both handles stay writable the way this fold exists to
        support.
        """

        handle_id = id(query)
        stack = self._query_handle_stack.get()
        if handle_id in stack:
            return ("recursive-query-handle", query.key)
        token = self._query_handle_stack.set(stack + (handle_id,))
        try:
            state = vars(query)
            # Before the sorts below, which compare names against each other:
            # a handle whose dictionary was given a non-string key answers with
            # the kernel's own refusal rather than a TypeError out of sorted().
            if any(not isinstance(name, str) for name in state):
                raise UnsupportedValueError(
                    f"Query handle {query.key!r} has invalid custom state."
                )
            # Both carriers are folded, never one instead of the other: which
            # of them functools.wraps copies depends on the interpreter, and a
            # handle can be given the other one afterwards.
            annotate = state.get("__annotate__")
            if annotate is None:
                annotate_payload: Any = None
            elif annotate is getattr(query.fn, "__annotate__", None):
                annotate_payload = ("evaluator-of-function",)
            elif isinstance(annotate, FunctionType):
                annotate_payload = (
                    "lazy-annotations",
                    self._annotation_evaluator_payload(annotate, set()),
                )
            else:
                raise UnsupportedValueError(
                    f"Query handle {query.key!r} has an invalid annotation evaluator."
                )
            eager = state.get("__annotations__")
            if eager is None:
                eager_payload: Any = None
            elif eager is self._function_own_annotations(query.fn):
                eager_payload = ("annotations-of-function",)
            elif isinstance(eager, dict) and all(isinstance(name, str) for name in eager):
                eager_payload = tuple(
                    (name, self._freeze_annotation_capture(item, set()))
                    for name, item in sorted(eager.items())
                )
            else:
                raise UnsupportedValueError(
                    f"Query handle {query.key!r} has invalid annotations."
                )
            annotations_payload = (annotate_payload, eager_payload)
            # functools.wraps points __wrapped__ at the function this handle
            # already folds, and a marker says so. Rebound, it is an ordinary
            # entry: a body can call whatever it points at now, so what it
            # points at has to be folded like any other attribute. It cannot
            # simply join the walk below -- the marker is what keeps a query
            # defined in a module with no stable source identity from being
            # refused for carrying a reference to its own function.
            wrapped = state.get("__wrapped__")
            wrapped_payload: Any = (
                ("wrapped-is-fn",)
                if wrapped is query.fn
                else self._query_handle_entry_payload(
                    query, "__wrapped__", wrapped, seen_functions
                )
            )
            type_parameters = state.get("__type_params__", ())
            if not isinstance(type_parameters, tuple):
                raise UnsupportedValueError(
                    f"Query handle {query.key!r} has invalid type parameters."
                )
            return (
                "query-handle-v3",
                state.get("__name__"),
                state.get("__qualname__"),
                state.get("__module__"),
                state.get("__doc__"),
                annotations_payload,
                wrapped_payload,
                tuple(self._freeze_annotation_capture(item, set()) for item in type_parameters),
                tuple(
                    (name, self._query_handle_entry_payload(query, name, item, seen_functions))
                    for name, item in sorted(state.items())
                    if name not in self._QUERY_HANDLE_CONTRACT_NAMES
                ),
            )
        finally:
            self._query_handle_stack.reset(token)

    @staticmethod
    def _function_own_annotations(fn: Any) -> Any:
        """The annotations dictionary a function holds, or None if reading fails.

        Only ever compared by identity, against what a query handle carries
        under the same name. A read that raises -- an annotation naming
        something unresolvable, under a lazy evaluator -- answers None, which
        no dictionary on a handle can be, so the handle's own copy is folded
        rather than treated as the function's.
        """

        try:
            return fn.__annotations__
        except Exception:
            return None

    def _query_handle_entry_payload(
        self, query: Any, name: str, value: Any, seen_functions: builtins.set[int]
    ) -> Any:
        """Fold one attribute written on a query handle."""

        try:
            return self._captured_dependency_digest(
                f"handle[{name}]", value, seen_functions, owner=query.fn
            )
        except UnsupportedValueError as exc:
            # The digest refuses in the vocabulary of an ambient capture, which
            # is not what this is: nothing read it out of an enclosing scope,
            # someone wrote it on the handle. The remedy is the same one, and
            # it stops short of naming the capture-set preview -- that reports
            # what the body closes over, never what the handle carries.
            raise UnsupportedValueError(
                f"Query {query.key!r} holds unsupported state {name!r} of type "
                f"{type(value).__module__}.{type(value).__qualname__} on its handle. "
                "Move mutable state behind Input/Resource nodes or use an immutable value."
            ) from exc

    def _resource_identities_hold(self, recorded: tuple[tuple[Any, str], ...]) -> bool:
        """Re-read every captured resource's identity, refusing an unstable one.

        A resource that distinguishes itself only by its own state -- the
        default ``identity()`` hands back the instance -- has no stable
        identity if a probe or a load mutates that state. Nothing else about
        the query has moved by the time this arm is reached, so a digest that
        has moved says the resource redefined itself between two reads: every
        warm request would cold-execute and leave the record it replaced
        behind. A resource that defines its own ``identity()`` and returns
        something different is reparameterizing itself deliberately, and
        keeps re-fingerprinting the way it always has.
        """
        for resource, expected in recorded:
            digest = self._resource_identity_digest(resource)
            if digest == expected:
                continue
            if digest == _UNREADABLE_RESOURCE_DIGEST:
                # A resource that has become unreadable forces the full
                # recompute rather than serving a fingerprint nothing
                # checked. That is a degradation, not a redefinition.
                return False
            # A resource that hands back itself is one that never said what
            # distinguishes it, so its own state is all there is to compare --
            # which is asked here by identity rather than by class so that the
            # question costs this module no new knowledge of the resource
            # layer it is read from. Asking it with an isinstance check would
            # take a third deferred import of that layer into a module already
            # carrying two, each one deferred only because importing the two
            # modules into each other at load time is what they cannot do.
            if self._resource_configuration(resource) is resource:
                raise UnsupportedValueError(
                    f"Resource {type(resource).__module__}:{type(resource).__qualname__} "
                    "changed its own state between two reads, so it has no stable "
                    "identity. Keep observation state out of the resource, or define "
                    "identity() to return the configuration that distinguishes it."
                )
            return False
        return True

    def _resource_identity_digest(self, resource: Any) -> str:
        """Re-read a captured resource's configuration for the memo guard.

        A resource's ``identity()`` runs user code and hands back a fresh
        object every call, so the reference observation that gates the rest of
        the memo cannot see it. This re-runs the read and digests its value
        instead, which is what makes a resource-folding fingerprint memoizable
        at all. Any failure answers with a value no stored digest can equal, so
        a resource that has become unreadable forces the full recompute rather
        than serving a fingerprint nothing checked.
        """

        # Re-read at most once per request. That is exactly the scope the
        # kernel already gives resource validation -- a span declares that the
        # world holds still until it closes, and a caller changing it mid-span
        # must say so, which rolls the request and clears this cache -- so
        # reusing a digest inside one request introduces no consistency class
        # the kernel did not already have. Outside a request the cache does
        # not exist and every read is fresh.
        cache = self._request_resource_digests
        if cache is not None:
            entry = cache.get(id(resource))
            # The resource object is kept beside its digest: an id freed and
            # reused by another object must not answer from this cache.
            if entry is not None and entry[0] is resource:
                return entry[1]
        try:
            configuration = self._resource_configuration(resource)
            digest = fingerprint_snapshot(
                (
                    freeze(configuration, adapters=self._adapters),
                    self._resource_configuration_type_payload(configuration),
                )
            )
        except Exception:
            return _UNREADABLE_RESOURCE_DIGEST
        if cache is not None:
            cache[id(resource)] = (resource, digest)
        return digest

    def _query_definition_observation(self, query: Any) -> Any:
        """Observe the live query definition for memoized-fingerprint reuse.

        The observation records object *references* — per entry, not per
        container, because a `__kwdefaults__` dict or a closure cell mutated in
        place keeps its identity while changing the definition. Storing the
        references in the memo pins their addresses, so identity comparison is
        collision-free: any rebinding introduces an object that cannot be
        identical to a still-pinned one, and a spurious mismatch only costs a
        fingerprint recompute. It traverses the same slots as
        `_function_definition_payload` folds into the fingerprint — function
        metadata, captured class bodies, and captured instance and policy
        state — plus the query handle's own instance dictionary, which
        `_query_handle_state_payload` folds beside it, pinning each entry's
        reference where the payload folds its value; modules stay leaves, and
        so do the types the payload pins by module anchor rather than by a
        namespace walk.

        A module is covered by three memo arms instead, because this walk never
        enters one: `_module_observation_stamp` re-derives its file bytes,
        import metadata and, outside the runtime-pinned modules, its
        module-level constants; each statically accessed attribute chain is
        re-resolved and its target compared by identity; and the definitions
        behind the chain landings whose payloads read one live
        -- functions, wraps-decorated callable objects, query handles, inputs,
        type aliases, type parameters and resources, whose globals, defaults,
        policies, evaluators, instance and handle state the payload folds live
        -- are observed by `_module_function_target_observation`. Where a chain
        lands on a class or a frozen dataclass instance instead -- named
        directly, or held inside an immutable container the payload accepts,
        such as a tuple, a NamedTuple or a frozenset -- no arm follows anything
        inside that landing: the memo compares the object the chain resolved to
        by identity while the payload folds what is inside it, so neither a
        member written in place nor a binding one of those members reads is
        observed. Where such a container carries a class, the rebinding this
        walk misses is one the payload refuses outright once the class stops
        being its module's live binding, so the memo answers on while a fresh
        computation raises. Landings the payload refuses from the start instead
        of folding -- a plain object that is not one of those callables, a
        mutable dataclass, a dict, a list -- raise when the fingerprint is
        built and carry nothing stale.
        What a fold reads out of a resource's `identity()` needs no walk on
        either route: it is gated by the recorded configuration digests the
        memo carries alongside this observation. Everything else a fold reads
        off a resource -- its type, its probe and load and identity methods,
        and what those read -- is observed as the ordinary instance it is, by
        this walk where a slot reaches the resource directly and by the
        chain-landing arm where an attribute chain lands on it.
        """

        # The query arm of `observe_value` is this observation: it folds the
        # key, the policies, the function and the handle state, and it marks
        # the handle before descending, so a body that captures its own query
        # pins it by reference there instead of walking it a second time.
        observe_value, _observe_function = self._definition_observers()
        return observe_value(query)

    def _module_function_target_observation(
        self, records: tuple[tuple[ModuleType, tuple[str, ...], Any], ...]
    ) -> tuple[Any, ...]:
        """Observe the definitions behind chain landings folded from live code.

        `_module_attribute_payload` folds a function target through
        `_function_definition_payload`, which reads its defaults, closure and
        globals live. None of that moves the function's identity, and the
        module's constants payload carries none of it either, so the memo needs
        the same observation the query's own function gets. A wraps-decorated
        callable object is folded the same way and for the same reason -- its
        `__call__` definition and its instance state are read live while the
        landing object's own identity holds still -- so it is observed here
        too, which is what makes `import m; m.f` and `from m import f` reuse a
        stored fingerprint on the same conditions. A Query landing is the third
        such shape: the payload folds its function, its policies and its handle
        state live, none of which moves the handle the chain resolves to.

        Four more landings are folded from live definitions and belong here for
        exactly that reason: an Input, whose `eq` and `cutoff` policies are
        folded as definitions; a type alias and a type parameter, whose lazy
        evaluators resolve their globals when the payload calls them; and a
        resource, whose `probe`, `load` and `identity` methods are folded as
        the definitions they are. One observer family covers every record, in
        the order the memo stored them, so a value two chains share is folded
        on first contact the same way here as it was when the stored
        observation was built.
        """

        from .core import Input, Query

        def observed(target: Any) -> bool:
            # A Query is named before the wrapped-callable clause rather than
            # left to it: functools.wraps does put a __wrapped__ function on
            # every handle, so the clause would catch one by accident, and the
            # memo would lose the handle silently if that ever stopped being
            # true.
            if isinstance(target, (FunctionType, Query, Input)):
                return True
            # Mirrors `_module_attribute_payload`, which routes a module and a
            # class to their own branches before the resource and
            # wrapped-callable ones: a module is covered by the memo's other
            # arms, and a class landing is compared by identity alone, whether
            # or not it carries a __wrapped__ attribute or the methods a
            # resource is recognised by.
            if isinstance(target, (ModuleType, type)):
                return False
            if _is_type_alias(target) or isinstance(target, _TYPE_PARAMETER_TYPES):
                return True
            if self._is_resource_handle(target):
                return True
            return isinstance(getattr(target, "__wrapped__", None), FunctionType) and callable(
                target
            )

        targets = [target for _module, _path, target in records if observed(target)]
        if not targets:
            # Most queries reach no module function at all, and building the
            # observer family is the expensive half of this call.
            return ()
        observe_value, _observe_function = self._definition_observers()
        return tuple(observe_value(target) for target in targets)

    def _definition_observers(
        self,
    ) -> tuple[Callable[[Any], Any], Callable[[FunctionType], Any]]:
        """Build one observer family over a fresh shared `seen` set.

        The `seen` set is what makes an observation first-contact-complete: a
        value reached from two slots is folded once, by whichever slot arrives
        first, and pinned by reference afterwards. Callers that must compare
        two observations therefore have to build them from families of the same
        shape — one family per observation, over the same slots in the same
        order.
        """

        from .core import Input, Query

        seen: builtins.set[int] = set()
        parameter_types = _TYPE_PARAMETER_TYPES

        def observe_value(value: Any) -> Any:
            if isinstance(value, Query):
                if id(value) in seen:
                    return value
                # Marked before the walk below, which reaches this arm again
                # for a query held on another query's handle. Everything the
                # payload folds for a captured query is folded here on this
                # first contact, so a later one pins the handle by reference.
                seen.add(id(value))
                return (
                    value.key,
                    observe_value(value.eq),
                    observe_value(value.cutoff),
                    observe_value(value.fn),
                    observe_query_handle(value),
                )
            if isinstance(value, Input):
                return (value, value.key, observe_value(value.eq), observe_value(value.cutoff))
            if isinstance(value, FunctionType):
                return observe_function(value)
            if isinstance(value, ModuleType):
                # Modules are gated by three memo arms instead of a walk here:
                # the observation stamp over file content and namespace
                # constants, the attribute targets re-resolved by identity, and
                # the observed definitions behind chain-reached functions.
                return value
            if isinstance(value, type):
                return observe_type(value)
            if isinstance(value, MethodType):
                return (value, observe_value(value.__func__), observe_instance(value.__self__))
            if isinstance(value, GenericAlias):
                # Mirrors _freeze_annotation_capture: a parameterized generic
                # carries its definition in its origin and arguments, and it
                # exposes no instance dictionary for the walk below to read.
                return (
                    value,
                    observe_value(value.__origin__),
                    tuple(observe_value(item) for item in value.__args__),
                )
            if isinstance(value, UnionType):
                return (
                    value,
                    tuple(observe_value(item) for item in typing.get_args(value)),
                )
            if _is_type_alias(value):
                return observe_type_alias(value)
            if parameter_types and isinstance(value, parameter_types):
                return observe_type_parameter(value)
            if isinstance(value, (tuple, frozenset)):
                if id(value) in seen:
                    return value
                seen.add(id(value))
                items = tuple(observe_value(item) for item in value)
                if type(value) in {tuple, frozenset}:
                    return (value, items)
                # Mirrors the tuple- and frozenset-subclass arms of
                # _freeze_captured_immutable, which fold the subclass type and
                # its instance state alongside the elements.
                return (value, items, observe_type(type(value)), observe_state(value))
            if isinstance(value, slice):
                return (
                    value,
                    observe_value(value.start),
                    observe_value(value.stop),
                    observe_value(value.step),
                )
            if isinstance(value, (staticmethod, classmethod)):
                # Descriptors are builtin by type and anything but a leaf by
                # content: _type_definition_payload folds the function they
                # wrap, so the observation has to reach it too. Before the
                # builtins arm below, which would otherwise pin the wrapper and
                # see none of the definition behind it.
                return (value, observe_value(value.__func__))
            if isinstance(value, property):
                return (
                    value,
                    tuple(
                        observe_value(function)
                        for function in (value.fget, value.fset, value.fdel)
                    ),
                )
            if type(value).__module__ == "builtins":
                return value
            return observe_instance(value)

        def observe_type(cls: type[Any]) -> Any:
            if cls.__module__ == "builtins" or (
                cls.__module__.partition(".")[0] in sys.stdlib_module_names
            ):
                # Mirrors _type_definition_payload: builtin and stdlib types
                # are pinned by name anchor and runtime build, never by a
                # namespace walk, so their contents need no observation.
                return cls
            if id(cls) in seen:
                return cls
            seen.add(id(cls))
            return (
                cls,
                observe_type(type(cls)) if type(cls) is not type else type,
                tuple(observe_type(base) for base in cls.__bases__),
                tuple((name, observe_value(item)) for name, item in sorted(vars(cls).items())),
            )

        def observe_instance(value: Any) -> Any:
            if isinstance(value, ModuleType):
                return value
            if isinstance(value, type):
                return observe_type(value)
            if id(value) in seen:
                return value
            seen.add(id(value))
            fields_observation: Any = None
            if is_dataclass(value):
                fields_observation = tuple(
                    (item.name, observe_value(object.__getattribute__(value, item.name)))
                    for item in fields(value)
                )
            return (
                value,
                observe_type(type(value)),
                fields_observation,
                observe_state(value),
            )

        def observe_state(value: Any) -> Any:
            try:
                # The slot _static_instance_dict reads. Going through vars()
                # instead would follow a proxying __getattribute__ to a mapping
                # rebuilt on every read, which identity comparison never matches.
                state = object.__getattribute__(value, "__dict__")
            except (AttributeError, TypeError):
                return None
            # Only a concrete instance dictionary is observed: the payload
            # refuses ambient capture state that is not one, and pinning a
            # proxy would pin an object rebuilt on the next read.
            if not isinstance(state, dict):
                return None
            return tuple(
                (name, observe_value(item)) for name, item in _sorted_state_entries(state)
            )

        def observe_function(fn: FunctionType) -> Any:
            if id(fn) in seen:
                return fn
            seen.add(id(fn))
            code = fn.__code__
            fn_globals = fn.__globals__
            return (
                fn,
                code,
                fn.__defaults__,
                tuple(observe_value(value) for value in fn.__defaults__ or ()),
                fn.__kwdefaults__,
                tuple(
                    (name, observe_value(value))
                    for name, value in sorted((fn.__kwdefaults__ or {}).items())
                ),
                tuple(
                    (cell, observe_cell(cell)) for cell in fn.__closure__ or ()
                ),
                tuple(
                    (name, observe_value(fn_globals[name]))
                    if name in fn_globals
                    else (name, _UNBOUND_GLOBAL_OBSERVATION)
                    for name in sorted(set(code.co_names))
                ),
                tuple(
                    (name, observe_value(value))
                    for name, value in _sorted_state_entries(vars(fn))
                ),
                observe_metadata(fn),
            )

        def observe_cell(cell: Any) -> Any:
            try:
                contents = cell.cell_contents
            except ValueError:
                return _EMPTY_CELL_OBSERVATION
            return observe_value(contents)

        def observe_annotation(value: Any) -> Any:
            if isinstance(value, type):
                top_level = value.__module__.partition(".")[0]
                if (
                    value.__module__ == "builtins"
                    or top_level in sys.stdlib_module_names
                    or top_level == "pyinc"
                ):
                    # Mirrors _freeze_annotation_capture: an annotation type
                    # from builtins, the standard library or pyinc is pinned by
                    # module anchor and module identity, which no in-place
                    # namespace edit can move, so its body needs no walk.
                    return value
                return observe_type(value)
            if isinstance(value, GenericAlias) or (
                type(value).__module__ in {"typing", "types"}
                and typing.get_origin(value) is not None
            ):
                return (
                    value,
                    observe_annotation(typing.get_origin(value)),
                    tuple(observe_annotation(item) for item in typing.get_args(value)),
                )
            if _is_type_alias(value):
                return observe_type_alias(value)
            if parameter_types and isinstance(value, parameter_types):
                return observe_type_parameter(value)
            return observe_value(value)

        def observe_type_alias(value: Any) -> Any:
            # One fold for both slots that reach an alias, annotation and
            # ambient capture, because _freeze_annotation_capture and
            # _freeze_static_capture read the same evaluator and __value__.
            # Folding on first contact, whichever slot arrives first, is what
            # makes the shared `seen` set below safe. Nested values take
            # observe_value, which matches the ambient reading and never
            # observes less than the annotation one, so neither slot is
            # short-changed by the other having arrived first.
            if id(value) in seen:
                # A later contact returns the pinned object: the first one
                # already folded it, and both paths fold it the same way.
                return value
            seen.add(id(value))
            evaluator = getattr(value, "evaluate_value", None)
            if isinstance(evaluator, FunctionType):
                content: Any = observe_function(evaluator)
            else:
                # Without a Python evaluator the payload resolved __value__
                # eagerly and anchored the types it reached to their live
                # module bindings; the leaves carry the same sensitivity for
                # every swept class and carrier type, and for the definition
                # closure each of those types anchors in turn, so the memo
                # refuses when a fresh computation would.
                resolved = getattr(value, "__value__", None)
                content = (observe_value(resolved), _type_anchor_leaves(resolved))
            return (
                value,
                content,
                tuple(
                    observe_value(item) for item in getattr(value, "__type_params__", None) or ()
                ),
            )

        def observe_type_parameter(value: Any) -> Any:
            # The same single fold for a type parameter reached from either
            # slot: a bound, a constraint set and a default carry their content
            # in a lazy evaluator where one exists and in the resolved
            # attribute otherwise.
            if id(value) in seen:
                return value
            seen.add(id(value))
            parts: list[Any] = []
            for evaluator_name, value_name in (
                ("evaluate_bound", "__bound__"),
                ("evaluate_constraints", "__constraints__"),
                ("evaluate_default", "__default__"),
            ):
                evaluator = getattr(value, evaluator_name, None)
                if isinstance(evaluator, FunctionType):
                    parts.append(observe_function(evaluator))
                    continue
                try:
                    part = getattr(value, value_name, None)
                except Exception:
                    part = None
                # Same reason as the alias arm: an eagerly resolved bound,
                # constraint set or default was anchored by the payload, so
                # the observation carries the live-binding leaves beside it.
                parts.append(
                    (
                        tuple(observe_value(item) for item in part)
                        if isinstance(part, tuple)
                        else observe_value(part),
                        _type_anchor_leaves(part),
                    )
                )
            return (value, tuple(parts))

        def observe_query_handle(handle: Any) -> Any:
            # Mirrors `_query_handle_state_payload` arm for arm. Each
            # annotation carrier is pinned beside the function's own, because
            # that payload folds the handle's copy by content only where the
            # two references have come apart; pinning both is what notices
            # either of them being rebound. __wrapped__ gets the same treatment
            # for the same reason: a marker while it points at the function,
            # its definition once it points elsewhere, since the payload then
            # folds that definition live. Everything else is pinned per entry,
            # except the contract fields the payload leaves to a sibling --
            # the arms beside this one observe the key, the policies and the
            # function.
            state = vars(handle)
            annotate = state.get("__annotate__")
            if annotate is None:
                annotate_observation: Any = None
            else:
                function_annotate = getattr(handle.fn, "__annotate__", None)
                annotate_observation = (
                    (annotate, function_annotate)
                    if annotate is function_annotate
                    else (annotate, function_annotate, observe_value(annotate))
                )
            eager = state.get("__annotations__")
            if eager is None:
                eager_observation: Any = None
            else:
                function_annotations = self._function_own_annotations(handle.fn)
                if eager is function_annotations or not (
                    isinstance(eager, dict) and all(isinstance(name, str) for name in eager)
                ):
                    eager_observation = (eager, function_annotations)
                else:
                    eager_observation = (
                        eager,
                        function_annotations,
                        tuple(
                            (name, observe_annotation(item))
                            for name, item in sorted(eager.items())
                        ),
                    )
            wrapped = state.get("__wrapped__")
            try:
                entries = sorted(state.items())
            except TypeError as exc:
                # A handle dictionary given a name that is not a string is
                # refused by the payload, and this walk gets there first for
                # the query being keyed and for one its body closes over --
                # both are observed before their state is folded -- so it
                # answers with the same refusal instead of letting the sort's
                # TypeError out. This does not stand in for the payload's own
                # check: a handle reached through a module attribute chain is
                # folded without this walk ever observing it, because the walk
                # stops at the module. Caught rather than checked in front:
                # the sort is attempted, and a TypeError out of it -- from the
                # names, or from the values a tuple comparison falls through
                # to -- is answered as invalid custom state, while a check
                # would cost every observation of every handle on the memo
                # path this closure exists to serve.
                raise UnsupportedValueError(
                    f"Query handle {handle.key!r} has invalid custom state."
                ) from exc
            return (
                (annotate_observation, eager_observation),
                (wrapped, handle.fn)
                if wrapped is handle.fn
                else (wrapped, handle.fn, observe_value(wrapped)),
                tuple(
                    (name, observe_value(item))
                    for name, item in entries
                    if name not in self._QUERY_HANDLE_ANNOTATION_NAMES
                    and name not in self._QUERY_HANDLE_SIBLING_NAMES
                ),
            )

        def observe_metadata(fn: FunctionType) -> Any:
            # _function_metadata_payload folds annotation values as ambient
            # captures when the body reads its own annotations back, and as
            # annotation captures otherwise; the observation follows the switch.
            reflects_annotations = any(
                name in {"__annotations__", "get_annotations", "get_type_hints"}
                for code in self._walk_code_objects(fn.__code__)
                for name in code.co_names
            )
            observe_entry = observe_value if reflects_annotations else observe_annotation
            try:
                annotations = fn.__annotations__
            except Exception:
                annotate = getattr(fn, "__annotate__", None)
                annotations_observation: Any = (
                    observe_function(annotate)
                    if isinstance(annotate, FunctionType)
                    else annotate
                )
            else:
                annotations_observation = (
                    annotations,
                    tuple(
                        (name, observe_entry(item))
                        for name, item in sorted(annotations.items())
                    )
                    if isinstance(annotations, dict)
                    and all(isinstance(name, str) for name in annotations)
                    else annotations,
                )
            type_parameters = getattr(fn, "__type_params__", ())
            return (
                fn.__name__,
                fn.__qualname__,
                fn.__module__,
                fn.__doc__,
                annotations_observation,
                type_parameters,
                tuple(observe_annotation(item) for item in type_parameters or ()),
            )

        return observe_value, observe_function

    @classmethod
    def _definition_observation_matches(cls, expected: Any, current: Any) -> bool:
        """Compare observations by identity at the leaves, never by equality."""

        if expected is current:
            return True
        if type(expected) is tuple and type(current) is tuple:
            return len(expected) == len(current) and all(
                cls._definition_observation_matches(old, new)
                for old, new in zip(expected, current, strict=True)
            )
        return False

    def _code_fingerprint(self, fn: FunctionType) -> str:
        payload = (
            *self._runtime_build_payload(),
            self._function_definition_payload(fn, set()),
        )
        return fingerprint_snapshot(payload)

    def _runtime_build_payload(self) -> tuple[Any, ...]:
        """Interpreter and build identity shared by durable trust boundaries."""

        return _RUNTIME_BUILD_PAYLOAD

    def _reject_reflective_namespace_reads(self, fn: FunctionType) -> None:
        offenses = _reflective_namespace_offenses(fn.__code__)
        if offenses:
            raise UnsupportedValueError(
                f"Function {fn.__module__}.{fn.__qualname__} reads a namespace "
                f"reflectively ({', '.join(offenses)}). Reflective namespace "
                "reads bypass capture fingerprinting; access module attributes "
                "directly, or move mutable state behind Input/Resource nodes. "
                "Run pyinc.explain_query_captures(...) to inspect the capture "
                "set before the first db.get()."
            )

    def _function_definition_payload(
        self, fn: FunctionType, seen_functions: builtins.set[int]
    ) -> Any:
        self._reject_reflective_namespace_reads(fn)
        fn_id = id(fn)
        if fn_id in seen_functions:
            return ("recursive-function", fn.__module__, fn.__qualname__)
        seen_functions.add(fn_id)
        try:
            closure_vars = inspect.getclosurevars(fn)
            return (
                fn.__module__,
                fn.__qualname__,
                self._code_definition_payload(fn.__code__, fn.__module__),
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
                self._function_metadata_payload(fn, seen_functions),
            )
        finally:
            seen_functions.remove(fn_id)

    def _function_metadata_payload(
        self, fn: FunctionType, seen_functions: builtins.set[int]
    ) -> Any:
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
            if not isinstance(annotations, dict) or any(
                not isinstance(name, str) for name in annotations
            ):
                raise UnsupportedValueError(
                    f"Function {fn.__module__}.{fn.__qualname__} has invalid annotations."
                )
            reflects_annotations = any(
                name in {"__annotations__", "get_annotations", "get_type_hints"}
                for code in self._walk_code_objects(fn.__code__)
                for name in code.co_names
            )
            annotations_payload = tuple(
                (
                    name,
                    self._captured_dependency_digest(
                        f"annotation[{name}]",
                        value,
                        seen_functions,
                        owner=fn,
                    )
                    if reflects_annotations
                    else self._freeze_annotation_capture(value, set()),
                )
                for name, value in sorted(annotations.items())
            )
        state = vars(fn)
        if any(not isinstance(name, str) for name in state):
            raise UnsupportedValueError(
                f"Function {fn.__module__}.{fn.__qualname__} has invalid custom state."
            )
        type_parameters = getattr(fn, "__type_params__", ())
        if not isinstance(type_parameters, tuple):
            raise UnsupportedValueError(
                f"Function {fn.__module__}.{fn.__qualname__} has invalid type parameters."
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
                    self._captured_dependency_digest(
                        f"attribute[{name}]",
                        value,
                        seen_functions,
                        owner=fn,
                    ),
                )
                for name, value in sorted(state.items())
            ),
            tuple(self._freeze_annotation_capture(value, set()) for value in type_parameters),
        )

    def _annotation_evaluator_payload(
        self, evaluator: FunctionType, active_ids: builtins.set[int]
    ) -> Any:
        # The third route that folds a Python function's code, and it makes the
        # same static assumption the other two do: the names below are resolved
        # against the evaluator's globals. A reflective read reaches state none
        # of them names, so it is refused here as well.
        self._reject_reflective_namespace_reads(evaluator)
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
                self._code_definition_payload(evaluator.__code__, evaluator.__module__),
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

    def _code_location_payload(self, filename: str, module_name: str | None) -> Any:
        """Fold where a definition sits inside its package, not on this machine.

        `co_filename` is the absolute path the source file had when the module
        was imported, so folding it verbatim binds every identity to the
        checkout, container or virtualenv the code was installed into: two
        byte-identical trees at different prefixes then share no fingerprint at
        all. What the fold needs is which file INSIDE ITS PACKAGE the code was
        compiled from, and the import system already answers that without
        touching the filesystem -- the defining module's dotted name encodes
        its package position, namespace parents included, and the basename
        separates `pkg/mod.py` from `pkg/mod/__init__.py`.

        A `co_filename` that is not an absolute path is not a location at all
        -- `<string>` for exec'd and dataclass-generated code, `<stdin>`,
        `<frozen importlib._bootstrap>` -- and is folded verbatim, so generated
        code can never take a real module's identity. An absolute path whose
        basename is not the defining module's own file is code compiled with a
        filename of someone's choosing; it is folded verbatim beside the module
        name rather than being allowed to answer to that module's location.
        """

        if not os.path.isabs(filename):
            return ("code-origin-verbatim-v4", filename)
        module = sys.modules.get(module_name) if module_name is not None else None
        module_file = vars(module).get("__file__") if module is not None else None
        basename = os.path.basename(filename)
        if isinstance(module_file, str) and os.path.basename(module_file) == basename:
            return ("code-origin-module-v4", module_name, basename)
        return ("code-origin-foreign-v4", module_name, filename)

    def _code_definition_payload(self, code: CodeType, module_name: str | None) -> Any:
        """Return a refcount-independent, typed encoding of a code object, in the
        package position its defining module gives it.

        `module_name` is that module's dotted name -- `fn.__module__` at the
        outer call sites, inherited by every nested code constant -- and is what
        `_code_location_payload` folds in place of the absolute source path; it
        is required rather than defaulted so a call site that forgets to thread
        it is a type error rather than a silent fallback to that path.
        """
        return (
            "code-v3",
            code.co_argcount,
            code.co_posonlyargcount,
            code.co_kwonlyargcount,
            code.co_nlocals,
            code.co_stacksize,
            code.co_flags,
            code.co_code,
            tuple(self._code_constant_payload(value, module_name) for value in code.co_consts),
            tuple(code.co_names),
            tuple(code.co_varnames),
            tuple(code.co_freevars),
            tuple(code.co_cellvars),
            code.co_exceptiontable,
            code.co_linetable,
            self._code_location_payload(code.co_filename, module_name),
            code.co_name,
            code.co_qualname,
            code.co_firstlineno,
        )

    def _code_constant_payload(self, value: Any, module_name: str | None) -> Any:
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
            return ("float-bits", struct.pack("!d", value))
        if isinstance(value, complex):
            return (
                "complex-bits",
                struct.pack("!d", value.real),
                struct.pack("!d", value.imag),
            )
        if isinstance(value, str):
            return ("str", value)
        if isinstance(value, bytes):
            return ("bytes", value)
        if isinstance(value, tuple):
            return (
                "tuple",
                tuple(self._code_constant_payload(item, module_name) for item in value),
            )
        if isinstance(value, frozenset):
            items = tuple(self._code_constant_payload(item, module_name) for item in value)
            return (
                "frozenset",
                tuple(sorted(items, key=fingerprint_snapshot)),
            )
        if isinstance(value, slice):
            return (
                "slice",
                self._code_constant_payload(value.start, module_name),
                self._code_constant_payload(value.stop, module_name),
                self._code_constant_payload(value.step, module_name),
            )
        if isinstance(value, CodeType):
            return ("code", self._code_definition_payload(value, module_name))
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
            frozen = self._freeze_static_capture(owner, set())
        except UnsupportedValueError:
            if not allow_instance_state:
                raise UnsupportedValueError(
                    f"Bound policy owner {type(owner).__module__}."
                    f"{type(owner).__qualname__} is not snapshot-safe."
                ) from None
            frozen = self._policy_instance_state_payload(owner)
        return (
            "instance",
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
        try:
            state = vars(policy)
        except TypeError:
            state = {}
        return tuple(
            (name, self._freeze_static_capture(value, set()))
            for name, value in sorted(state.items())
        )

    def _input_policy_digest(self, input_obj: Any) -> str:
        return fingerprint_snapshot(
            (
                "input-policy-v3",
                self._runtime_build_payload(),
                self._policy_definition_payload(input_obj.eq),
                self._policy_definition_payload(input_obj.cutoff),
            )
        )

    def _current_adapter_digests(self) -> dict[str, str]:
        """Implementation digest of each registered adapter, keyed by adapted type.

        The registry is fixed, but a caller's adapter configuration may be
        instance state, so those digests are recomputed at each checkpoint trust
        boundary. The kernel's own fixed adapters are exempt: they carry no
        instance state and their implementations ship in this package, so their
        digests -- taken once per process -- cannot have moved, and this serves
        them from that memo. Every registered key still appears, so the
        map a checkpoint manifest is written from is unchanged.
        """
        digests = dict(self._static_adapter_digests)
        for value_type, adapter in self._non_static_adapters.items():
            digests[_adapter_key(value_type)] = self._adapter_implementation_digest(adapter)
        return digests

    def _digestable_adapter_configuration_digests(self) -> dict[str, str]:
        """Configuration digest of every adapter that can be digested at all.

        The in-process basis for the pinned-adapter-state law: an adapter's own
        instance state, digested through the same helper the implementation
        digest folds it with. Implementations stay out of this map; they are
        digested at the checkpoint boundary, where the code that froze a record
        and the code reading it come from different processes.

        Built one entry at a time, so an adapter whose configuration cannot be
        digested -- slot state, a state key that defeats the digest's sort --
        costs the check only its own entry. Construction succeeds either way;
        what such an adapter loses is the in-process check on itself.

        The kernel's own fixed adapters contribute nothing here. They hold no
        instance state, so there is no configuration for the check to catch
        moving; naming them would only make every top-level request re-derive a
        digest that cannot change. A registry holding nothing else therefore
        leaves this map empty, and the request-scope check returns on its first
        line.
        """
        digests: dict[str, str] = {}
        for value_type, adapter in self._non_static_adapters.items():
            try:
                digest = self._adapter_configuration_digest(adapter)
            except (UnsupportedValueError, TypeError, ValueError):
                continue
            digests[_adapter_key(value_type)] = digest
        return digests

    def _verify_registered_adapters(self) -> None:
        """Raise if a registered adapter's configuration moved since construction.

        Adapter instance configuration is contractually immutable for the
        registered lifetime. The configuration digests are taken once at
        construction; re-deriving them at each top-level request turns a silent
        warm-not-equal-fresh into a loud typed error without changing any cache
        key. Only the adapters the construction-time map names are re-derived:
        an adapter whose configuration could not be digested then is absent
        from it and is skipped, on its own, because drift there is undetectable
        in-process and the checkpoint boundary already refuses to trust it.
        """

        expected = self._registered_adapter_digests
        if not expected:
            return
        current: dict[str, str] = {}
        for value_type, adapter in self._adapters.items():
            key = _adapter_key(value_type)
            if key not in expected:
                continue
            try:
                current[key] = self._adapter_configuration_digest(adapter)
            except (UnsupportedValueError, TypeError, ValueError) as exc:
                raise AdapterContractError(
                    "A registered adapter is no longer fingerprintable, so its "
                    "configuration can no longer be checked against the digest "
                    f"taken at Database construction. Adapter key(s): {key}."
                ) from exc
        if current == expected:
            return
        moved = sorted(key for key in expected if expected[key] != current.get(key))
        raise AdapterContractError(
            "Adapter instance configuration changed after Database construction "
            f"for adapter key(s): {', '.join(moved)}. Registered adapters are "
            "immutable for the database's lifetime; build a new Database with "
            "the reconfigured adapter instead."
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
        return fingerprint_snapshot(payload)

    def _adapter_configuration_digest(self, adapter: ValueAdapter) -> str:
        """Fingerprint an adapter's instance configuration.

        Folds the adapter's own state through ``_adapter_state_payload``, the
        same helper the implementation digest folds it with, and nothing else:
        an adapter that carries its configuration where that helper refuses to
        look -- slot state -- is as unverifiable here as it is there.
        """
        return fingerprint_snapshot(
            ("adapter-configuration-v1", self._adapter_state_payload(adapter))
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
        try:
            state = vars(adapter)
        except TypeError:
            state = {}
        try:
            return tuple(
                (
                    name,
                    self._freeze_static_capture(value, set()),
                )
                for name, value in sorted(state.items())
            )
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
        if not self._checkpoint_adapter_digests and not self._non_static_adapters:
            # Fast path: nothing to distrust. With no checkpoint digests loaded,
            # every key reaching here belongs to a record this process froze
            # through this very registry -- a loaded checkpoint that had adapted
            # values would have brought their digests with it, and the manifest
            # validator refuses a record naming a key its manifest does not
            # declare, so no loaded record can reach here. So the only
            # question left is whether a live adapter has moved since it froze
            # those records, and the kernel's own fixed adapters cannot: they
            # hold no state and their code ships with this module. A caller's
            # adapter takes the full comparison below, because its configuration
            # is state the law asks the caller to leave alone rather than
            # something this process can vouch for.
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
            # Fold the captured query's full definition into the parent's
            # identity so a change to a dependency query's body moves the parent.
            return (
                "query",
                value.key,
                self._function_definition_payload(value.fn, seen_functions),
                self._policy_definition_payload(value.eq),
                self._policy_definition_payload(value.cutoff),
                self._query_handle_state_payload(value, seen_functions),
            )
        if isinstance(value, Input):
            return (
                "input",
                value.key,
                self._policy_definition_payload(value.eq),
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
        if isinstance(value, type):
            # Tested before the __wrapped__ probe: a class carrying a
            # __wrapped__ class attribute is still a class, and the class
            # treatment (full body payload) must win. type(value) here is the
            # metaclass, so no callable-object payload could substitute.
            if "<locals>" in value.__qualname__ and self._type_fingerprint_stack.get():
                return self._implementation_type_payload(value)
            return self._type_definition_payload(value)
        wrapped_function = getattr(value, "__wrapped__", None)
        if isinstance(wrapped_function, FunctionType) and callable(value):
            try:
                return self._wrapped_callable_payload(
                    name,
                    value,
                    wrapped_function,
                    seen_functions,
                    owner=owner,
                )
            except UnsupportedValueError as exc:
                raise UnsupportedValueError(
                    f"Query {owner.__module__}:{owner.__qualname__} captures unsupported ambient value "
                    f"{name!r} of type {type(value).__qualname__}. "
                    "Move mutable state behind Input/Resource nodes or use an immutable value. "
                    "Run pyinc.explain_query_captures(...) to inspect the capture set before the first db.get()."
                ) from exc
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

    def _wrapped_callable_payload(
        self,
        name: str,
        value: Any,
        wrapped_function: FunctionType,
        seen_functions: builtins.set[int],
        *,
        owner: FunctionType,
    ) -> Any:
        """Fingerprint a functools.wraps-style callable object by its behavior.

        The wrapped function alone is not the behavior: __call__ decides what
        runs and the instance state parameterizes it, exactly as for an
        eq=/cutoff= policy object. __wrapped__ stays in the payload as
        additive information, never as a substitute for the implementation.

        The state fold is the ambient-capture one rather than the policy one:
        functools.wraps writes __wrapped__ and the copied metadata into the
        instance dictionary, and the policy fold refuses a function held there,
        so every wraps-decorated callable would be rejected. This fold reaches
        the same verdict on what matters -- slot state and mutable containers
        are refused -- while folding a function-valued entry as the dependency
        it is.

        Reading instance state is also what puts a reference cycle within
        reach, and the ambient guard that catches one is a per-walk set that
        restarts whenever a nested value routes back through the dependency
        digest, as a captured callable held in this one's state does. This
        stack spans those restarts. It refuses rather than folding a marker,
        which is what the kernel already does with a cyclic ambient value.
        """

        call = type(value).__call__
        call_function = getattr(call, "__func__", call)
        if not isinstance(call_function, FunctionType):
            raise UnsupportedValueError(
                f"Captured callable {type(value).__module__}."
                f"{type(value).__qualname__} has a non-Python __call__ "
                "implementation that cannot be fingerprinted safely."
            )
        value_id = id(value)
        stack = self._wrapped_callable_stack.get()
        if value_id in stack:
            raise UnsupportedValueError(
                f"Captured callable {type(value).__module__}."
                f"{type(value).__qualname__} holds a reference cycle through "
                "its instance state and cannot be fingerprinted safely."
            )
        token = self._wrapped_callable_stack.set(stack + (value_id,))
        try:
            return (
                "wrapped-callable-v3",
                type(value).__module__,
                type(value).__qualname__,
                self._implementation_type_payload(type(value)),
                self._function_definition_payload(call_function, seen_functions),
                self._captured_instance_dict_payload(
                    name,
                    value,
                    seen_functions,
                    owner=owner,
                    active_ids=set(),
                ),
                self._function_definition_payload(wrapped_function, seen_functions),
            )
        finally:
            self._wrapped_callable_stack.reset(token)

    def _freeze_captured_immutable(
        self,
        name: str,
        value: Any,
        seen_functions: builtins.set[int],
        *,
        owner: FunctionType,
        active_ids: builtins.set[int],
    ) -> Any:
        """Encode immutable capture shapes while preserving nested dependencies."""

        from .core import Input, Query

        wrapped_function = getattr(value, "__wrapped__", None)
        if (
            isinstance(
                value,
                (
                    Query,
                    Input,
                    ModuleType,
                    FunctionType,
                    MethodType,
                    BuiltinFunctionType,
                    type,
                ),
            )
            or self._is_resource_handle(value)
            or (isinstance(wrapped_function, FunctionType) and callable(value))
        ):
            return (
                "captured-dependency",
                self._captured_dependency_digest(
                    name,
                    value,
                    seen_functions,
                    owner=owner,
                ),
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
                        f"{name}[member]",
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
                    for state_name, item in _sorted_state_entries(
                        self._static_instance_dict(value)
                    )
                    if state_name not in field_names
                )
                return (
                    "capture-frozen-dataclass",
                    self._type_definition_payload(type(value)),
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
        owner: FunctionType,
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
            for state_name, item in _sorted_state_entries(self._static_instance_dict(value))
            if not self._is_wraps_copied_annotations(value, state_name, item)
        )

    @staticmethod
    def _is_wraps_copied_annotations(value: Any, state_name: str, item: Any) -> bool:
        """Whether an instance-dict entry is the annotations dict `wraps` copied.

        Through 3.13 `functools.WRAPPER_ASSIGNMENTS` carries `__annotations__`,
        so `functools.wraps` binds the wrapped function's own annotations
        dictionary -- the same object, not a copy -- into the wrapper's instance
        dictionary. The capture walk has no dictionary arm, so it met one there
        and refused the whole callable. From 3.14 the assignment list carries
        `__annotate__` and `__type_params__` instead, both of which the walk
        already folds, so the entry never appears and the identical callable is
        accepted. Skipping it makes the older interpreters agree with 3.14
        rather than refuse for a reason that belongs to the interpreter.

        Nothing leaves identity with it: the same dictionary is read back out of
        the wrapped function by `_function_definition_payload`, which the
        callable payload already folds, so mutating it in place still moves the
        query. The test is `is` against that function's annotations, so a
        wrapper whose `__annotations__` was rebound to some other dictionary
        still meets the dictionary arm and is still refused.

        The name is tested first because reading a function's `__annotations__`
        materializes them on 3.14, and an entry that cannot be the copy must not
        force that.
        """

        if state_name != "__annotations__":
            return False
        wrapped = getattr(value, "__wrapped__", None)
        if not isinstance(wrapped, FunctionType):
            return False
        return item is wrapped.__annotations__

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
                elif isinstance(attribute, cached_property):
                    getter = attribute.func
                    if not isinstance(getter, FunctionType):
                        raise UnsupportedValueError(
                            f"Local implementation {value.__module__}."
                            f"{value.__qualname__} has a non-Python cached "
                            f"property function {name!r}."
                        )
                    functions.append(getter)
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
            attributes: list[tuple[str, Any]] = []
            for name, attribute in sorted(namespace.items()):
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
                        self._function_definition_payload(descriptor_function, set()),
                    )
                elif isinstance(attribute, cached_property):
                    # The fourth descriptor kind, unwrapped like the other
                    # three: the getter runs once per instance, and the
                    # definition behind it is what decides the value the
                    # instance then keeps.
                    getter = attribute.func
                    if not isinstance(getter, FunctionType):
                        raise UnsupportedValueError(
                            f"Local implementation {value.__module__}."
                            f"{value.__qualname__} has a non-Python cached "
                            f"property function {name!r}."
                        )
                    payload = (
                        "cached-property",
                        self._function_definition_payload(getter, set()),
                    )
                elif isinstance(attribute, property):
                    payload = (
                        "property",
                        tuple(
                            (
                                label,
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
                            self._freeze_static_capture(attribute, set()),
                        )
                    except UnsupportedValueError:
                        if is_local or name in referenced_names:
                            raise
                        continue
                attributes.append((name, payload))
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
                tuple(attributes),
            )
        finally:
            self._type_fingerprint_stack.reset(stack_token)
            seen_types.remove(type_id)

    def _local_dataclass_behavior_payload(self, value: type[Any]) -> Any:
        params = getattr(value, "__dataclass_params__", None)
        if params is None:
            return None
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
                getattr(params, name) if hasattr(params, name) else ("missing",),
            )
            for name in parameter_names
        )
        field_payloads = tuple(
            (
                item.name,
                getattr(getattr(item, "_field_type", None), "name", None),
                bool(item.init),
                bool(item.repr),
                item.hash,
                bool(item.compare),
                item.kw_only,
                self._freeze_annotation_capture(item.type, set()),
                freeze(dict(item.metadata), adapters=self._adapters),
                self._resource_configuration_type_payload(dict(item.metadata)),
                item.doc if hasattr(item, "doc") else ("missing",),
                self._dataclass_default_payload(item.default),
                self._dataclass_default_factory_payload(item.default_factory),
            )
            for item in fields(value)
        )
        return ("dataclass-behavior-v3", parameters, field_payloads)

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
                walk_function(value.fn)
            elif isinstance(value, Input):
                return
            elif self._is_resource_handle(value):
                identity = fingerprint_snapshot(self._resource_identity_payload(value))
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
            elif isinstance(value, type):
                # A class is callable, so before this branch existed a class
                # carrying a __wrapped__ attribute fell into the arm below and
                # had its wrapped function walked, while every other captured
                # class was skipped. No class is walked now, which is what
                # makes them uniform: a dep query reachable only through such a
                # class is no longer code-pinned, so a checkpoint warm refuses
                # it and re-executes rather than serving it.
                return
            else:
                wrapped_function = getattr(value, "__wrapped__", None)
                if isinstance(wrapped_function, FunctionType) and callable(value):
                    value_id = id(value)
                    if value_id in seen_values:
                        return
                    seen_values.add(value_id)
                    walk_function(wrapped_function)
                    call = type(value).__call__
                    call_function = getattr(call, "__func__", call)
                    if isinstance(call_function, FunctionType):
                        walk_function(call_function)
                    for item in self._static_instance_dict(value).values():
                        walk_value(item)

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
        """Pin the statically accessed behavior behind a captured module, and
        — for a module the runtime build identity pins — the constants those
        accesses land on.
        """

        base_identity = self._module_identity_payload(module)
        paths, dynamic = self._module_access_paths(owner, capture_name)
        # `_module_identity_payload` has already refused this module unless its
        # `__spec__` is a `ModuleSpec`, so the subscript cannot raise. One
        # predicate decides which branch a module takes and whether its
        # constants are folded, so the two answers cannot disagree.
        specification = vars(module)["__spec__"]
        if self._is_runtime_pinned_module(module, specification):
            return (
                "captured-stdlib-module-v4",
                module.__name__,
                base_identity,
                paths,
                dynamic,
                self._accessed_path_constants_payload(module, paths),
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
                    self._captured_module_path_payload(module, path, seen_functions, owner=owner)
                    for path in paths
                ),
            )
        finally:
            self._module_capture_stack.reset(token)

    def _accessed_path_constants_payload(
        self, module: ModuleType, paths: tuple[tuple[str, ...], ...]
    ) -> tuple[tuple[tuple[str, ...], Any], ...]:
        """Fold the constants a captured standard-library module's accessed
        paths name.

        The identity payload no longer folds a runtime-pinned namespace
        wholesale, so a constant the query's own code names would otherwise
        stop moving the fingerprint. Only the paths `_module_access_paths`
        already computed are read, and only values the constant payload folds
        are kept: a function or a class landing is pinned by name anchor and
        runtime build, exactly as before.

        Each folded landing is recorded for the memo, which re-resolves the
        chain and compares the target by identity. That is exact for these
        values -- every shape the constant payload accepts is immutable, so the
        same object always folds to the same payload, and the guard can only be
        too strict, never too lax.
        """

        folded: list[tuple[tuple[str, ...], Any]] = []
        for path in paths:
            target = self._resolve_module_path_target(module, path)
            try:
                payload = self._module_constant_payload(target, set())
            except UnsupportedValueError:
                continue
            self._record_module_path_target(module, path, target)
            folded.append((path, payload))
        return tuple(folded)

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
        *,
        owner: FunctionType,
    ) -> Any:
        current: Any = module
        steps: list[Any] = []
        for index, attribute_name in enumerate(path):
            if not isinstance(current, ModuleType):
                self._record_module_path_target(module, path, current)
                return (
                    tuple(steps),
                    self._module_attribute_payload(
                        current,
                        seen_functions,
                        owner=owner,
                        capture_name=".".join((module.__name__, *path[:index])),
                    ),
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
        self._record_module_path_target(module, path, current)
        return (
            tuple(steps),
            self._module_attribute_payload(
                current,
                seen_functions,
                owner=owner,
                capture_name=".".join((module.__name__, *path)),
            ),
        )

    def _record_module_path_target(
        self, module: ModuleType, path: tuple[str, ...], target: Any
    ) -> None:
        """Hand the memo the object whose payload this chain just folded.

        Recorded only while a fingerprint is being computed; outside that scope
        the collector is unset and the walk records nothing. ``target`` is the
        value the walk stopped on, which is exactly what
        ``_resolve_module_path_target`` re-derives for the memo guard.
        """

        collector = self._fingerprint_attribute_collector.get()
        if collector is not None:
            collector.append((module, path, target))

    def _module_attribute_payload(
        self,
        value: Any,
        seen_functions: builtins.set[int],
        *,
        owner: FunctionType,
        capture_name: str,
    ) -> Any:
        from .core import Input, Query

        if isinstance(value, Query):
            return (
                "query",
                value.key,
                self._function_definition_payload(value.fn, seen_functions),
                self._policy_definition_payload(value.eq),
                self._policy_definition_payload(value.cutoff),
                self._query_handle_state_payload(value, seen_functions),
            )
        if isinstance(value, Input):
            return (
                "input",
                value.key,
                self._policy_definition_payload(value.eq),
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
                definition,
            )
        if isinstance(value, BuiltinFunctionType):
            return self._builtin_function_payload(value)
        if isinstance(value, type):
            return ("type", self._type_definition_payload(value))
        if self._is_resource_handle(value):
            return ("resource", self._resource_identity_payload(value))
        wrapped_function = getattr(value, "__wrapped__", None)
        if isinstance(wrapped_function, FunctionType) and callable(value):
            # Same acceptance and the same implementation/state sensitivity
            # whether captured as `from m import f` (the digest path) or
            # `import m; m.f` (this path); this route still folds its own
            # module-path envelope around the shared payload.
            try:
                return (
                    "wrapped-callable",
                    self._wrapped_callable_payload(
                        capture_name,
                        value,
                        wrapped_function,
                        seen_functions,
                        owner=owner,
                    ),
                )
            except UnsupportedValueError as exc:
                # The shared payload refuses in its own vocabulary -- slot
                # state, a mutable member, a non-Python __call__, a cycle --
                # and one of those refusals is raised by a nested digest that
                # frames the capture as a direct one. This route re-frames all
                # of them around the module attribute the query actually named,
                # and keeps the remedy the digest arm gives the same value.
                raise UnsupportedValueError(
                    f"Query {owner.__module__}:{owner.__qualname__} captures module attribute "
                    f"{capture_name!r} of type {type(value).__module__}."
                    f"{type(value).__qualname__}, which cannot be fingerprinted safely. "
                    "Move mutable state behind Input/Resource nodes or use an immutable value. "
                    "Run pyinc.explain_query_captures(...) to inspect the capture set before the first db.get()."
                ) from exc
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

        self._reject_reflective_namespace_reads(function)
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
            self._code_definition_payload(function.__code__, function.__module__),
            tuple(
                self._captured_dependency_digest(
                    f"default[{index}]",
                    item,
                    seen_functions,
                    owner=function,
                )
                for index, item in enumerate(function.__defaults__ or ())
            ),
            tuple(
                (
                    name,
                    self._captured_dependency_digest(
                        f"kwdefault[{name}]",
                        item,
                        seen_functions,
                        owner=function,
                    ),
                )
                for name, item in sorted((function.__kwdefaults__ or {}).items())
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
            if self._fingerprint_module_collector.get() is not None:
                self._fingerprint_cacheable.set(False)
            # Retain snapshot-safe initialized state so a changed source module
            # cannot hide behind a mutable binding. In-process mutation after a
            # memoized identity remains under the documented module-patch limit.
            return (
                "source-pinned-mutable-module-global",
                type(value).__module__,
                type(value).__qualname__,
                frozen_value,
            )

    def _source_pinned_type_payload(self, value: type[Any]) -> Any:
        if self._fingerprint_module_collector.get() is not None:
            self._fingerprint_cacheable.set(False)
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

    @staticmethod
    def _is_runtime_pinned_module(
        module: ModuleType, specification: importlib.machinery.ModuleSpec
    ) -> bool:
        """True for a module the runtime build identity already pins.

        A built-in or frozen module has no source file of its own, and a module
        installed with the standard library arrives with the interpreter: the
        runtime build payload every fingerprint folds already names the
        implementation, the version and the flags such a module came with, so
        its namespace tells an identity nothing the build identity has not said
        -- while carrying values the interpreter rebuilds per process. A
        distribution installed beside the standard library is the caller's code
        however deep it sits, which is what the last clause excludes.
        """

        origin = specification.origin
        if origin in {"built-in", "frozen"}:
            return True
        if module.__name__.partition(".")[0] not in sys.stdlib_module_names:
            return False
        if not isinstance(origin, str):
            return False
        # On Windows an origin may carry `/` for `os.sep` -- the path finder
        # produces one whenever the `sys.path` entry it joined was spelled that
        # way -- and may differ in case from `sysconfig`'s answer. Either
        # difference would otherwise put a third-party module on the pinned
        # side, where a namespace write to it stops being detected. Both tests
        # therefore run on a normalised copy; `os.path.normcase` is the identity
        # on POSIX, so no behaviour moves there.
        normalised = os.path.normcase(origin.replace(os.altsep or os.sep, os.sep))
        if not normalised.startswith(os.path.normcase(Database._STDLIB_DIRECTORY_PREFIX)):
            return False
        # A distribution installed into the base interpreter's own
        # site-packages sits UNDER the standard library's directory and is
        # still the caller's code. Naming both spellings keeps the test the
        # same on every platform.
        return not any(
            f"{os.sep}{directory}{os.sep}" in normalised
            for directory in ("site-packages", "dist-packages")
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
          publicly promised surface;
        * outside the modules the runtime build identity pins, the
          module-level stable constants, read live by
          `_module_constants_payload` — so a namespace write to one of them
          moves this payload without any file changing. A module that identity
          pins contributes none of them here; the constants a capturing
          query's own code reads off such a module are folded beside the
          capture, by `_accessed_path_constants_payload`.

        The behavior behind statically accessed attribute chains is folded
        elsewhere, by `_captured_module_path_payload`. Before reusing a digest
        that folded any of this, the memo re-derives the constants inside
        `_module_observation_stamp`, re-resolves each chain and compares its
        target by identity, and observes the definitions behind every landing
        `_module_function_target_observation` keeps -- the enumeration lives
        there, beside the filter that decides it. A chain that lands on a class
        or a frozen dataclass instance -- named directly, or held inside a
        tuple, a NamedTuple or a frozenset the payload accepts -- is where that
        stops: what is inside the landing is folded by the payload and compared
        here only through the resolved target's identity, so a member written
        in place, and equally a module binding one of those members reads,
        moves the fold and nothing the memo checks. Shapes the payload refuses
        instead of folding -- a plain object that is not one of those
        callables, a mutable dataclass, a dict, a list -- raise when the
        fingerprint is built. Such state belongs in an `Input` or a `Resource`.
        """
        collector = self._fingerprint_module_collector.get()
        if collector is not None:
            collector.setdefault(id(module), module)
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
            all_tuple = tuple(sorted(all_attr))
        else:
            raise UnsupportedValueError(f"Captured module {module_name!r} has an unsafe __all__.")

        # Elided for the modules the runtime build identity already pins: a
        # standard-library namespace holds values CPython rebuilds per process
        # -- `tokenize.ContStr` and its seven siblings are regexes joined from
        # a set -- so folding them would make this payload process-varying.
        constants_payload = (
            ()
            if self._is_runtime_pinned_module(module, specification)
            else self._module_constants_payload(module)
        )

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
        # The read reports rather than waits: a module file someone replaced
        # with a pipe or a device has no bytes to hash and never will, and that
        # report is refused here on the same terms a failed read always was.
        with self._allow_raw_reads_scope():
            try:
                content = read_regular_file_following_links(Path(file_path))
            except OSError as exc:
                raise UnsupportedValueError(
                    f"Captured module {module_name!r} file cannot be read safely."
                ) from exc
            if content is None:
                raise UnsupportedValueError(
                    f"Captured module {module_name!r} file cannot be read safely."
                )
            digest = hashlib.sha256(content).hexdigest()
        file_identity = ("file-sha256", import_identity, digest)
        return (version_digest, file_identity, all_tuple, constants_payload)

    @staticmethod
    def _resolve_module_path_target(module: ModuleType, path: tuple[str, ...]) -> Any:
        """Resolve the object a static module-attribute chain currently names.

        Walks exactly as `_captured_module_path_payload` does: attribute by
        attribute through module namespaces, stopping at the first non-module
        value (whose payload is what the fingerprint folded). A vanished
        attribute resolves to a sentinel no recorded target can be.
        """

        current: Any = module
        for attribute_name in path:
            if not isinstance(current, ModuleType):
                return current
            namespace = vars(current)
            if attribute_name not in namespace:
                return _MISSING_MODULE_ATTRIBUTE
            current = namespace[attribute_name]
        return current

    def _module_observation_stamp(self, module: ModuleType) -> Any:
        """Return the invalidation token for a memoized module identity.

        Re-derives what `_module_identity_payload` folded: import metadata,
        `__version__`, `__all__`, the file bytes, and, outside the modules the
        runtime build identity pins, the module-level constants. Anything that
        payload reads and this does not would be a change the memo could hide.
        """

        namespace = vars(module)
        specification = namespace.get("__spec__")
        if not isinstance(specification, importlib.machinery.ModuleSpec):
            return ("invalid-spec", module.__name__)
        version = namespace.get("__version__")
        try:
            version_payload = self._module_constant_payload(version, set())
        except UnsupportedValueError:
            version_payload = ("unsafe-version", type(version).__qualname__)
        all_attr = namespace.get("__all__")
        all_payload = (
            tuple(sorted(all_attr))
            if isinstance(all_attr, (list, tuple))
            and type(all_attr) in {list, tuple}
            and all(type(item) is str for item in all_attr)
            else None
        )
        origin = specification.origin
        if origin in {"built-in", "frozen"}:
            source_observation: Any = ("runtime-module", origin)
        else:
            file_path = namespace.get("__file__")
            if not isinstance(file_path, str):
                source_observation = ("missing-file",)
            else:
                # Observed by content, never by stat identity: the stamp gates
                # reuse of a memoized fingerprint, so it carries the same
                # collision risk the identity payload does. A file that cannot
                # be read is reported as such rather than refused -- the token
                # says what was observed, and a token that will not match is
                # what sends the request back to the identity payload, which is
                # where an unreadable module file is refused, once.
                with self._allow_raw_reads_scope():
                    try:
                        content = read_regular_file_following_links(Path(file_path))
                    except OSError:
                        content = None
                    source_observation = (
                        ("unreadable-file",)
                        if content is None
                        else ("file-sha256", hashlib.sha256(content).hexdigest())
                    )
        return (
            module.__name__,
            sys.modules.get(module.__name__) is module,
            specification.name,
            sys.modules.get(specification.name) is module,
            origin,
            namespace.get("__loader__") is specification.loader,
            namespace.get("__package__"),
            namespace.get("__cached__"),
            version_payload,
            all_payload,
            source_observation,
            # Guarded by the same expression `_module_identity_payload` uses,
            # so the stamp folds exactly what the identity folded: nothing for
            # a module the runtime build identity pins, and every module-level
            # constant otherwise, which a namespace write moves without
            # touching the file bytes above.
            (
                ()
                if self._is_runtime_pinned_module(module, specification)
                else self._module_constants_payload(module)
            ),
        )

    def _module_constants_payload(self, module: ModuleType) -> tuple[tuple[str, Any], ...]:
        """Fold the module-level constants a captured module contributes.

        The single read behind both the identity payload and the observation
        stamp, so what a fingerprint folds and what the memo guard re-derives
        cannot drift apart. Names whose values are not stable constants are
        skipped rather than refused: functions, modules and types are reached
        through their own payloads, and anything else the constant payload
        cannot fold is left to whichever chain reaches it.

        A runtime-pinned module's constants on an owner's accessed paths are
        folded separately, by `_accessed_path_constants_payload`, and
        re-checked by the memo's chain arm, which re-resolves each landing and
        compares it by identity rather than re-deriving it through this read.
        """

        stable_constants: list[tuple[str, Any]] = []
        for name, item in sorted(vars(module).items()):
            if name.startswith("__") or name in {"__all__", "__version__"}:
                continue
            if isinstance(item, (FunctionType, ModuleType, type)):
                continue
            try:
                constant_payload = self._module_constant_payload(item, set())
            except UnsupportedValueError:
                continue
            stable_constants.append((name, constant_payload))
        return tuple(stable_constants)

    def _module_constant_payload(self, value: Any, active_ids: builtins.set[int]) -> Any:
        if type(value) in (str, bytes, int, bool, type(None)):
            return value
        if type(value) is float:
            return ("float-bits", struct.pack(">d", value))
        if type(value) is complex:
            return (
                "complex-bits",
                struct.pack(">d", value.real),
                struct.pack(">d", value.imag),
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

    @staticmethod
    def _resource_configuration(resource: Any) -> Any:
        """Read the configuration a resource distinguishes itself by."""

        resource_identity = getattr(resource, "identity", None)
        return resource_identity() if callable(resource_identity) else resource

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
            configuration = self._resource_configuration(resource)
            try:
                frozen_configuration = freeze(configuration, adapters=self._adapters)
            except UnsupportedValueError as exc:
                raise UnsupportedValueError(
                    f"Resource {type(resource).__module__}:{type(resource).__qualname__} must be snapshot-safe "
                    "or define identity()."
                ) from exc
            collector = self._fingerprint_resource_collector.get()
            if collector is not None:
                # The configuration this fold just read, recorded so the memo
                # guard can re-read it and compare. The configuration *type*
                # payload rides along because two configurations can freeze
                # alike while their classes carry different behavior, and the
                # fold below folds both.
                collector.append(
                    (
                        resource,
                        fingerprint_snapshot(
                            (
                                frozen_configuration,
                                self._resource_configuration_type_payload(configuration),
                            )
                        ),
                    )
                )
            return (
                "resource-v3",
                self._runtime_build_payload(),
                type(resource).__module__,
                type(resource).__qualname__,
                self._implementation_type_payload(type(resource)),
                frozen_configuration,
                self._resource_configuration_type_payload(configuration),
                self._resource_method_payload(resource, "probe"),
                self._resource_method_payload(resource, "load"),
                self._resource_method_payload(resource, "probe_and_load"),
                self._resource_method_payload(resource, "identity"),
            )
        finally:
            self._resource_fingerprint_stack.reset(token)

    def _resource_configuration_type_payload(self, configuration: Any) -> Any:
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
                for name, item in _sorted_state_entries(self._static_instance_dict(value))
                if name not in excluded
            )

        def adapter_for(value: Any) -> tuple[type[Any], ValueAdapter] | None:
            for candidate in type(value).__mro__:
                adapter = self._adapters.get(candidate)
                if adapter is not None:
                    return candidate, adapter
            return None

        def encode(value: Any) -> Any:
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
            if type(value) in {
                FrozenList,
                FrozenDict,
                FrozenSet,
                FrozenRecord,
                FrozenAdapterValue,
                FrozenGraph,
                FrozenRef,
            }:
                return ("frozen-snapshot", type(value).__qualname__)

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
                    items.sort(key=lambda item: fingerprint_snapshot(item[0]))
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

        return encode(configuration)

    def _resource_method_payload(self, resource: Any, method_name: str) -> Any:
        method = getattr(resource, method_name, None)
        if method is None:
            return (method_name, "missing")
        fn = getattr(method, "__func__", method)
        if isinstance(fn, FunctionType):
            return (method_name, self._function_definition_payload(fn, set()))
        if isinstance(fn, BuiltinFunctionType):
            return (method_name, self._builtin_function_payload(fn))
        if callable(method):
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
                return ("float-bits", struct.pack(">d", value))
            if type(value) is complex:
                return (
                    "complex-bits",
                    struct.pack(">d", value.real),
                    struct.pack(">d", value.imag),
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
                    for name, item in _sorted_state_entries(self._static_instance_dict(value))
                    if name not in field_names
                )
                return ("frozen-dataclass", type_payload, field_payload, extra_state)
        raise UnsupportedValueError("Unsupported ambient capture.")

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
        if not isinstance(state, dict):
            raise UnsupportedValueError(
                "Ambient capture instance state is not a concrete dictionary."
            )
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
            for name, item in _sorted_state_entries(self._static_instance_dict(value))
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
        return all(callable(getattr(value, name, None)) for name in ("label", "probe", "load"))

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
        self._verify_registered_adapters()
        self._request_counter += 1
        token = self._request_token.set(self._request_counter)
        pending: list[_PendingObserverEvent] = []
        events_token = self._pending_events.set(pending)
        failures: list[NodeKey] = []
        failures_token = self._request_failures.set(failures)
        # Lives for exactly this request, so a resource's configuration is
        # re-read once per request rather than once per memo guard.
        self._request_resource_digests = {}
        try:
            yield pending
        finally:
            self._request_resource_digests = None
            self._pending_events.reset(events_token)
            self._release_failure_exceptions(failures)
            self._request_failures.reset(failures_token)
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
        # The innermost execution still running. On the thread that owns the
        # stack this is its top: a frame is marked completed in the same
        # finally that pops it. A thread spawned inside a query keeps the
        # stack it inherited for its whole life, so completed frames are what
        # it has to look past.
        for frame in reversed(self._execution_stack.get()):
            if not frame.completed:
                return frame
        return None

    def _freeze_value(self, value: Any) -> Snapshot:
        # The database's own registry, not the raw map -- see the note on the
        # sibling helpers below. This one is on the warm request path too: every
        # query key freezes its arguments through here.
        snapshot = freeze(value, adapters=self._view_adapter_registry)
        if self._store is not None:
            self._persist_snapshot(snapshot)
        return snapshot

    def _persist_snapshot(self, snapshot: Snapshot) -> None:
        """Write the snapshot's serialized bytes to the configured ArtifactStore.
        The write-through path resolves the database's own store and hands it to
        `_persist_snapshot_to`, so it verifies present bytes exactly as the
        checkpoint save path does -- one body, one behaviour."""
        store = self._store
        if store is None:
            return
        self._persist_snapshot_to(snapshot, store)

    # These helpers hand the value layer the key-indexed registry this database
    # built once, not the raw map. Handed a map, the value layer builds a fresh
    # registry per call -- deriving an adapter key per entry -- and freezing,
    # exposing and fingerprinting all run on the warm request path, so a registry
    # that merely stopped being empty was costing every warm request repeated
    # rebuilds of a table that cannot change. The registry is fixed for the
    # database's lifetime, which is what makes reusing it identical rather than
    # merely cheaper: `_adapters` is assigned once at construction and nothing
    # writes to it afterwards.
    def _thaw_value(self, value: Any) -> Any:
        return thaw(value, adapters=self._view_adapter_registry)

    def _fingerprint_value(self, value: Any) -> str:
        return fingerprint(value, adapters=self._view_adapter_registry)

    def _semantic_equal(self, left: Any, right: Any) -> bool:
        return semantic_equal(left, right, adapters=self._view_adapter_registry)

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
            left=self._thaw_value(previous),
            right=self._thaw_value(snapshot),
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
            # The caller chooses WHICH token stands for the value; whether two
            # tokens are the same observation is the kernel's one relation, so
            # the tokens are frozen and compared canonically -- the numeric
            # tower stays separated and a NaN token is equal to itself.
            return snapshots_equal(
                self._freeze_cutoff_token(cutoff(left)),
                self._freeze_cutoff_token(cutoff(right)),
            )
        if eq is None:
            return self._semantic_equal(left, right)
        return eq(left, right)

    def _freeze_cutoff_token(self, value: Any) -> Snapshot:
        try:
            return self._freeze_value(value)
        except UnsupportedValueError as exc:
            raise UnsupportedValueError(
                "Cutoff functions must return snapshot-safe values."
            ) from exc
