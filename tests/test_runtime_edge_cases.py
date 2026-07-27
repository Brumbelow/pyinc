from __future__ import annotations

import copy
import gc
import hashlib
import json
import os
import sys
import typing
import weakref
from dataclasses import MISSING, dataclass, field
from pathlib import Path
from types import FunctionType
from typing import Any, cast

import pytest

from pyinc import (
    CheckpointIntegrityError,
    CheckpointManifestError,
    CheckpointVersionError,
    Database,
    DirectoryResource,
    FileResource,
    InMemoryArtifactStore,
    Input,
    InputKeyError,
    UnsupportedValueError,
    UntrackedReadError,
    freeze,
    query,
    serialize_snapshot,
)
from pyinc.resources import Resource
from pyinc.runtime import _MISSING_SNAPSHOT, ExecutionFrame, NodeKey, NodeRecord
from pyinc.value import (
    FrozenAdapterValue,
    FrozenDict,
    FrozenGraph,
    FrozenList,
    FrozenRecord,
    FrozenRef,
    FrozenSet,
    fingerprint_snapshot,
)

_DIGEST = "a" * 64
_IMPLEMENTATION_DIGEST = "b" * 64


def _tallied(key: str) -> str:
    calls = Path(f"{key}.calls")
    return calls.read_text(encoding="utf-8") if calls.exists() else ""


class _RuntimeType:
    pass


class _RichRuntimeType:
    __slots__ = ("slot",)

    class Nested:
        pass

    constant = (1, 2)

    @staticmethod
    def static(value: int) -> int:
        return value + 1

    @classmethod
    def create(cls) -> _RichRuntimeType:
        return cls()

    @property
    def measured(self) -> int:
        return 1

    @measured.setter
    def measured(self, value: int) -> None:
        self.slot = value

    @measured.deleter
    def measured(self) -> None:
        self.slot = 0


class _StatefulStr(str):
    metadata: object


class _StatefulBytes(bytes):
    metadata: object


class _StatefulInt(int):
    metadata: object


class _StatefulFloat(float):
    metadata: object


class _StatefulComplex(complex):
    metadata: object


class _SlottedString(str):
    __slots__ = ("metadata",)
    metadata: object


class _TupleSubclass(tuple[Any, ...]):
    metadata: object


class _FrozenSetSubclass(frozenset[Any]):
    metadata: object


class _RuntimePath(os.PathLike[str]):
    def __init__(self, path: str) -> None:
        self.path = path

    def __fspath__(self) -> str:
        return self.path


@dataclass(frozen=True)
class _FrozenRuntimeConfig:
    value: object


@dataclass
class _MutableRuntimeConfig:
    value: object


def _non_dict_state(instance: object) -> tuple[()]:
    return ()


_NonDictState = type(
    "_NonDictState",
    (),
    {"__module__": __name__, "__dict__": property(_non_dict_state)},
)


class _PolicyCallable:
    def __init__(self, expected: int = 1) -> None:
        self.expected = expected

    def __call__(self, left: int, right: int) -> bool:
        return left == right == self.expected

    def compare(self, left: int, right: int) -> bool:
        return left == right


class _SlottedPolicy:
    __slots__ = ("expected",)

    def __init__(self) -> None:
        self.expected = 1

    def __call__(self, left: int, right: int) -> bool:
        return left == right


class _RuntimeAdapter:
    def __init__(self, offset: int = 0) -> None:
        self.offset = offset

    def freeze(self, value: _RuntimeType, freeze_value: Any) -> object:
        return freeze_value(self.offset)

    def thaw(self, snapshot: Any, thaw_value: Any) -> _RuntimeType:
        thaw_value(snapshot)
        return _RuntimeType()


class _SlottedAdapter:
    __slots__ = ("offset",)

    def __init__(self) -> None:
        self.offset = 1

    def freeze(self, value: _RuntimeType, freeze_value: Any) -> object:
        return freeze_value(self.offset)

    def thaw(self, snapshot: Any, thaw_value: Any) -> _RuntimeType:
        thaw_value(snapshot)
        return _RuntimeType()


class _UnsafeStateAdapter(_RuntimeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.values: list[int] = []


_UNSAFE_ADAPTER_CAPTURE: dict[str, int] = {"value": 1}


class _UnsafeMethodAdapter:
    def freeze(self, value: _RuntimeType, freeze_value: Any) -> object:
        return freeze_value(_UNSAFE_ADAPTER_CAPTURE["value"])

    def thaw(self, snapshot: Any, thaw_value: Any) -> _RuntimeType:
        thaw_value(snapshot)
        return _RuntimeType()


class _BuiltinMethodAdapter:
    freeze = len
    thaw = len


@dataclass(frozen=True)
class _InvalidLabelResource(Resource[str, str, str]):
    label_value: object

    def probe(self, key: str) -> str:
        return key

    def load(self, db: Database, key: str) -> str:
        return key

    def label(self, key: str) -> Any:
        return self.label_value


@dataclass(frozen=True)
class _UnloadableResource(Resource[str, str, str]):
    def probe(self, key: str) -> str:
        return f"probe:{key}"

    def load(self, db: Database, key: str) -> str:
        raise RuntimeError(f"cannot load {key}")

    def label(self, key: str) -> str:
        return f"unloadable[{key}]"


@dataclass(frozen=True)
class _TallyingFailingResource(Resource[str, str, tuple[str, ...]]):
    """Never loads, appending one character per call to ``<key>.calls``.

    The tally lives beside the resource's own key because a query's capture set
    may not contain mutable state -- a counter attribute or module global is
    rejected before the first ``get()``.
    """

    def _tally(self, key: str, event: str) -> None:
        with open(f"{key}.calls", "a", encoding="utf-8") as handle:
            handle.write(event)

    def probe(self, key: str) -> tuple[str, ...]:
        self._tally(key, "p")
        return ("missing",)

    def load(self, db: Database, key: str) -> str:
        self._tally(key, "l")
        raise FileNotFoundError(key)

    def label(self, key: str) -> str:
        return f"tallying[{key}]"


class _LoadPayload:
    """Stands in for whatever a load allocates before it raises."""

    def __init__(self) -> None:
        self.buffer = bytearray(4 * 1024 * 1024)


class _PayloadError(RuntimeError):
    def __init__(self, payload: weakref.ref[_LoadPayload]) -> None:
        super().__init__("payload load failed")
        self.payload = payload


@dataclass(frozen=True)
class _AllocatingFailingResource(Resource[str, str, str]):
    """Allocates in the frame that raises, reachable only through its traceback."""

    def probe(self, key: str) -> str:
        return f"probe:{key}"

    def load(self, db: Database, key: str) -> str:
        payload = _LoadPayload()
        raise _PayloadError(weakref.ref(payload))

    def label(self, key: str) -> str:
        return f"allocating[{key}]"


@dataclass(frozen=True)
class _UnprobeableResource(Resource[str, str, str]):
    def probe(self, key: str) -> str:
        raise RuntimeError(f"cannot probe {key}")

    def load(self, db: Database, key: str) -> str:
        raise AssertionError("load must not run")

    def label(self, key: str) -> str:
        return f"unprobeable[{key}]"


@dataclass(frozen=True)
class _RichDataclass:
    count: int = 1
    values: list[int] = field(default_factory=list)


class _NonBytesStore:
    def get(self, digest: str) -> Any:
        return "not bytes"

    def put(self, digest: str, payload: bytes) -> None:
        return None

    def contains(self, digest: str) -> bool:
        return True


def _store_snapshot(store: InMemoryArtifactStore, value: object) -> tuple[object, str]:
    snapshot = freeze(value)
    digest = fingerprint_snapshot(snapshot)
    store.put(digest, serialize_snapshot(snapshot))
    return snapshot, digest


def _query_record(args_digest: str, snapshot_digest: str) -> dict[str, Any]:
    return {
        "kind": "query",
        "identity": f"query-id:{_IMPLEMENTATION_DIGEST}",
        "args_digest": args_digest,
        "label": "query-label",
        "snapshot_digest": snapshot_digest,
        "deps": [],
        "is_untracked": False,
        "adapter_keys": [],
        "query_id": "query-id",
    }


def _resource_record(args_digest: str, snapshot_digest: str) -> dict[str, Any]:
    return {
        "kind": "resource",
        "identity": f"resource-id:{_IMPLEMENTATION_DIGEST}",
        "args_digest": args_digest,
        "label": "resource-label",
        "snapshot_digest": snapshot_digest,
        "deps": [],
        "is_untracked": False,
        "adapter_keys": [],
    }


def _manifest(
    records: list[dict[str, Any]] | None = None,
    adapters: dict[object, object] | None = None,
) -> dict[str, Any]:
    return {
        "pyinc_ckpt_version": 4,
        "kernel_fingerprint_version": 2,
        "adapters": adapters or {},
        "records": records or [],
    }


def test_database_public_validation_errors_are_specific() -> None:
    with pytest.raises(ValueError, match="strict, checked, fast"):
        Database(mode="unknown")

    db = Database()
    with pytest.raises(TypeError, match="db.set"):
        db.set(cast(Any, "input"), 1)
    with pytest.raises(TypeError, match="db.get"):
        db.get(cast(Any, lambda: None))
    with pytest.raises(TypeError, match="db.explain"):
        db.explain(cast(Any, lambda: None))
    with pytest.raises(TypeError, match="db.inspect"):
        db.inspect(cast(Any, lambda: None))
    with pytest.raises(RuntimeError, match="while a query is executing"):
        db.report_untracked_read("outside")
    with pytest.raises(ValueError, match="requires an ArtifactStore"):
        db.save_checkpoint()
    with pytest.raises(ValueError, match="requires an ArtifactStore"):
        db.load_checkpoint("ck" + _DIGEST)


def test_set_many_rejects_malformed_pairs_and_duplicate_keys_transactionally() -> None:
    db = Database()
    value = Input[int]("set-many-edge")

    with pytest.raises(TypeError, match="iterable of .* pairs"):
        db.set_many([cast(Any, (value, 1, 2))])
    with pytest.raises(TypeError, match="expects .* pairs"):
        db.set_many([(cast(Any, "not-input"), 1)])
    with pytest.raises(InputKeyError, match="duplicate input key"):
        db.set_many([(value, 1), (value, 2)])
    assert db.revision == 0


def test_resource_labels_must_be_nonempty_strings() -> None:
    db = Database()
    with pytest.raises(TypeError, match="must return a string"):
        _InvalidLabelResource(1).read(db, "value")
    with pytest.raises(ValueError, match="non-empty string"):
        _InvalidLabelResource("").read(db, "value")


def test_failed_resource_loads_are_recorded_only_when_the_probe_is_total() -> None:
    db = Database()

    partial = _UnprobeableResource()
    with pytest.raises(RuntimeError, match="cannot probe"):
        partial.read(db, "value")
    assert not db._resource_registry
    assert db.statistics().resource_count == 0

    total = _UnloadableResource()
    with pytest.raises(RuntimeError, match="cannot load"):
        total.read(db, "value")
    record = db._records[db._resource_key(total, "value")]
    assert record.is_failed
    assert record.failure == "RuntimeError: cannot load value"
    assert record.probe == "probe:value"
    assert record.snapshot is None
    assert record.digest == ""
    assert record.checked_in_request == db._request_counter
    assert record.failure_exc is None
    assert db.statistics().resource_count == 1


def test_failing_resource_loads_once_per_request_across_a_fan_out(tmp_path: Path) -> None:
    resource = _TallyingFailingResource()
    target = str(tmp_path / "target")

    @query
    def reader(db: Database, key: str, index: int) -> str:
        try:
            return resource.read(db, key)
        except FileNotFoundError:
            return f"<default-{index}>"

    @query
    def fan_out(db: Database, key: str) -> tuple[str, ...]:
        return tuple(reader(db, key, index) for index in range(20))

    db = Database()
    assert db.get(fan_out, target)[0] == "<default-0>"
    # One load per request, however many readers observe it. The probe either
    # side of it is probe_and_load's default implementation followed by the
    # second observation taken alongside the failure.
    assert _tallied(target) == "plp"
    revision = db.revision
    executions = db.statistics().query_executions

    for _ in range(10):
        assert len(db.get(fan_out, target)) == 20

    assert _tallied(target) == "plp" * 11
    assert db.revision == revision
    assert db.statistics().query_executions == executions


def test_repeated_failing_reads_within_one_query_body_load_once(tmp_path: Path) -> None:
    resource = _TallyingFailingResource()
    target = str(tmp_path / "target")

    @query
    def read_repeatedly(db: Database, key: str, times: int) -> int:
        handled = 0
        for _ in range(times):
            try:
                resource.read(db, key)
            except FileNotFoundError:
                handled += 1
        return handled

    db = Database()
    assert db.get(read_repeatedly, target, 50) == 50
    assert _tallied(target) == "plp"


def test_failing_load_exception_is_reused_only_inside_its_own_request() -> None:
    db = Database()
    resource = _AllocatingFailingResource()

    with db._request_scope():
        with pytest.raises(_PayloadError) as first:
            resource.read(db, "value")
        record = db._records[db._resource_key(resource, "value")]
        assert record.failure_exc is first.value
        with pytest.raises(_PayloadError) as second:
            resource.read(db, "value")
        assert second.value is first.value

    assert record.failure_exc is None
    assert record.failure_traceback is None
    with pytest.raises(_PayloadError) as later:
        resource.read(db, "value")
    assert later.value is not first.value


def test_failing_load_frames_are_released_when_the_request_ends() -> None:
    db = Database()
    resource = _AllocatingFailingResource()

    payload: weakref.ref[_LoadPayload] | None = None
    try:
        resource.read(db, "value")
    except _PayloadError as exc:
        payload = exc.payload

    record = db._records[db._resource_key(resource, "value")]
    assert record.is_failed
    # The exception serves re-reads inside its own request and nothing else, so
    # the record stops pinning the load frame (and its 4 MiB local) once that
    # request is over. Resource records are never evicted; this one would
    # otherwise hold the frame until a load finally succeeded.
    assert record.failure_exc is None
    assert record.failure_traceback is None
    gc.collect()
    assert payload is not None
    assert payload() is None


def test_checkpoints_omit_failed_resource_records_and_their_readers(tmp_path: Path) -> None:
    files = FileResource()
    missing = tmp_path / "missing.txt"
    present = tmp_path / "present.txt"
    present.write_text("here", encoding="utf-8")

    @query(key="checkpoint-optional-read")
    def read_optional(db: Database, filename: str) -> str:
        try:
            return files.read(db, filename)
        except FileNotFoundError:
            return "<default>"

    store = InMemoryArtifactStore()
    db = Database(store=store)
    assert db.get(read_optional, str(missing)) == "<default>"
    assert db.get(read_optional, str(present)) == "here"

    checkpoint = db.save_checkpoint()
    manifest = json.loads(cast(bytes, store.get(checkpoint)).decode("utf-8"))
    saved = {(entry["identity"], entry["args_digest"]) for entry in manifest["records"]}
    missing_key, _ = db._query_key(read_optional, (str(missing),), {})
    present_key, _ = db._query_key(read_optional, (str(present),), {})
    assert (present_key.identity, present_key.args_digest) in saved
    assert (missing_key.identity, missing_key.args_digest) not in saved
    assert not any(str(missing) in entry["label"] for entry in manifest["records"])

    warmed = Database(store=store)
    warmed.load_checkpoint(checkpoint)
    missing.write_text("late", encoding="utf-8")
    assert warmed.get(read_optional, str(missing)) == "late"


def test_checkpoints_omit_readers_of_an_unrecordable_failure(tmp_path: Path) -> None:
    directories = DirectoryResource()
    path = tmp_path / "listing"
    path.mkdir()
    (path / "a.txt").write_text("a", encoding="utf-8")

    @query(key="checkpoint-optional-listing")
    def names(db: Database, dirname: str) -> tuple[str, ...]:
        try:
            return directories.read(db, dirname)
        except NotADirectoryError:
            return ("<caught>",)

    store = InMemoryArtifactStore()
    db = Database(store=store)
    assert db.get(names, str(path)) == ("a.txt",)

    # Neither the load nor the probe survives a directory replaced by a file, so
    # the resource record keeps the listing it last saw while the reader is
    # correctly re-executed and caches the handled failure. Persisting that pair
    # would warm a value the record cannot re-derive.
    (path / "a.txt").unlink()
    path.rmdir()
    path.write_text("not a directory", encoding="utf-8")
    assert db.get(names, str(path)) == ("<caught>",)

    checkpoint = db.save_checkpoint()
    manifest = json.loads(cast(bytes, store.get(checkpoint)).decode("utf-8"))
    saved = {(entry["identity"], entry["args_digest"]) for entry in manifest["records"]}
    reader_key, _ = db._query_key(names, (str(path),), {})
    assert (reader_key.identity, reader_key.args_digest) not in saved
    assert not any(entry["kind"] == "resource" for entry in manifest["records"])

    path.unlink()
    path.mkdir()
    (path / "a.txt").write_text("a", encoding="utf-8")

    warmed = Database(store=store)
    warmed.load_checkpoint(checkpoint)
    assert warmed.get(names, str(path)) == Database().get(names, str(path)) == ("a.txt",)
    assert warmed.statistics().query_executions == 1


def test_materialized_call_validation_covers_strict_and_checked_modes() -> None:
    key = NodeKey("query", "identity", _DIGEST, "query")
    frame = ExecutionFrame(key)

    strict = Database(mode="strict")
    with pytest.raises(UnsupportedValueError, match="Invalid query call snapshot"):
        strict._materialize_call(1, record_boundaries=False, frame=frame)

    checked = Database(mode="checked")
    with pytest.raises(UnsupportedValueError, match="Invalid query call snapshot"):
        checked._materialize_call(1, record_boundaries=True, frame=frame)

    call = freeze(((1,), {"named": 2}))
    args, kwargs = checked._materialize_call(call, record_boundaries=True, frame=frame)
    assert args == (1,)
    assert kwargs == {"named": 2}
    assert len(frame.boundary_values) == 2
    assert len(frame.boundary_fingerprints) == 2

    assert not Database._is_materialized_call_envelope(1, kwargs_type=dict)
    assert not Database._is_materialized_call_envelope(((1,), {1: 2}), kwargs_type=dict)
    assert not Database._is_materialized_call_envelope(
        ((1,), FrozenDict(((1, 2),))), kwargs_type=FrozenDict
    )


def test_strict_snapshot_view_resolves_every_graph_wrapper() -> None:
    graph = FrozenGraph(
        nodes=(
            FrozenList(
                (
                    FrozenRef(1),
                    FrozenRef(2),
                    FrozenRef(3),
                    FrozenAdapterValue("adapter", (FrozenRef(3),)),
                )
            ),
            FrozenDict((("key", FrozenRef(3)),)),
            FrozenSet("set", (1,)),
            FrozenRecord("Record", (("parent", FrozenRef(0)),)),
        ),
        root=FrozenRef(0),
    )
    exposed = Database._strict_snapshot_view(graph)
    assert isinstance(exposed, FrozenList)
    assert isinstance(exposed[0], FrozenDict)
    assert isinstance(exposed[1], FrozenSet)
    assert isinstance(exposed[2], FrozenRecord)
    assert isinstance(exposed[3], FrozenAdapterValue)
    assert exposed[2]["parent"] is exposed

    invalid = FrozenGraph(((),), FrozenRef(0))
    with pytest.raises(TypeError, match="unsupported node"):
        Database._strict_snapshot_view(invalid)

    nested = FrozenGraph(
        nodes=(
            FrozenList(
                (
                    FrozenList((1,)),
                    FrozenDict((("key", 2),)),
                    FrozenSet("set", (3,)),
                    FrozenRecord("Nested", (("value", 4),)),
                )
            ),
        ),
        root=FrozenRef(0),
    )
    nested_view = Database._strict_snapshot_view(nested)
    assert [type(item) for item in nested_view] == [
        FrozenList,
        FrozenDict,
        FrozenSet,
        FrozenRecord,
    ]


def test_static_capture_covers_supported_scalar_typing_and_container_shapes() -> None:
    db = Database()
    capture = db._freeze_static_capture

    assert capture(Ellipsis, set()) == ("ellipsis",)
    assert capture(1.5, set())[0] == "float-bits"
    assert capture(1 + 2j, set())[0] == "complex-bits"
    assert capture(_RuntimeType, set())[0] == "type"
    assert capture(list[int], set())[0] == "generic-alias"
    assert capture(int | str, set())[0] == "union-type"
    typing_list = typing.__dict__["List"][int]
    assert capture(typing_list, set())[0] == "typing-alias"
    assert capture(typing.ForwardRef("_RuntimeType", module=__name__), set())[0] == (
        "forward-reference"
    )
    assert capture(typing.TypeVar("RuntimeT"), set())[0] == "typing-parameter"
    assert capture(typing.NoReturn, set())[0] == "typing-singleton"

    scalars: list[Any] = [
        _StatefulStr("value"),
        _StatefulBytes(b"value"),
        _StatefulInt(1),
        _StatefulFloat(1.5),
        _StatefulComplex(1 + 2j),
    ]
    for scalar in scalars:
        scalar.metadata = "safe"
        assert capture(scalar, set())[0] == "scalar-subclass"

    assert capture(FrozenList(()), set()) == FrozenList(())
    assert capture(Path("path"), set())[0] == "path"
    assert capture(_RuntimePath("path"), set())[0] == "pathlike"
    assert capture(range(1, 5, 2), set()) == ("range", 1, 5, 2)
    assert capture(slice(1, 5, 2), set())[0] == "slice"
    assert capture((1, 2), set()) == (1, 2)

    tuple_subclass = _TupleSubclass((1, 2))
    tuple_subclass.metadata = "safe"
    assert capture(tuple_subclass, set())[0] == "tuple-subclass"
    assert capture(frozenset({1, 2}), set())[0] == "frozenset"
    frozen_subclass = _FrozenSetSubclass({1, 2})
    frozen_subclass.metadata = "safe"
    assert capture(frozen_subclass, set())[0] == "frozenset-subclass"
    assert capture(_FrozenRuntimeConfig((1, 2)), set())[0] == "frozen-dataclass"


def test_static_capture_rejects_mutable_slotted_cyclic_and_invalid_state() -> None:
    db = Database()
    with pytest.raises(UnsupportedValueError, match="Mutable dataclass"):
        db._freeze_static_capture(_MutableRuntimeConfig(1), set())

    slotted = _SlottedString("value")
    slotted.metadata = "unsafe"
    with pytest.raises(UnsupportedValueError, match="slot state"):
        db._freeze_static_capture(slotted, set())

    value = (1, 2)
    with pytest.raises(UnsupportedValueError, match="Cyclic ambient values"):
        db._freeze_static_capture(value, {id(value)})

    with pytest.raises(UnsupportedValueError, match="not a concrete dictionary"):
        db._static_instance_dict(_NonDictState())
    with pytest.raises(UnsupportedValueError, match="Unsupported scalar subclass"):
        db._static_scalar_base_value(object())
    with pytest.raises(UnsupportedValueError, match="Unsupported ambient capture"):
        db._freeze_static_capture(object(), set())


def test_annotation_capture_covers_types_modules_aliases_parameters_and_singletons() -> None:
    db = Database()
    capture = db._freeze_annotation_capture

    assert capture(Ellipsis, set()) == ("ellipsis",)
    assert capture("RuntimeType", set()) == "RuntimeType"
    assert capture(int, set()) == ("annotation-type", "builtins", "int")
    assert capture(Path, set())[0] == "annotation-type"
    assert capture(_RuntimeType, set())[0] == "annotation-type"
    assert capture(os, set())[0] == "annotation-module"
    assert capture(list[int], set())[0] == "annotation-generic-alias"
    assert capture(int | str, set())[0] == "annotation-union"
    assert capture(typing.ForwardRef("_RuntimeType", module=__name__), set())[0] == (
        "annotation-forward-reference"
    )
    typing_list = typing.__dict__["List"][int]
    assert capture(typing_list, set())[0] == "annotation-typing-alias"
    assert capture(typing.NoReturn, set())[0] == "annotation-typing-singleton"
    assert capture((int, "RuntimeType"), set())[0][0] == "annotation-type"

    constrained = typing.TypeVar("constrained", int, str)
    assert capture(constrained, set())[0] == "annotation-parameter"
    bounded = typing.TypeVar("bounded", bound=int)
    assert capture(bounded, set())[0] == "annotation-parameter"
    assert capture(bounded, {id(bounded)}) == (
        "recursive-annotation-parameter",
        "bounded",
    )

    type_alias_type = getattr(typing, "TypeAliasType", None)
    if type_alias_type is not None:
        alias = type_alias_type("RuntimeAlias", list[int])
        assert capture(alias, set())[0] == "annotation-type-alias"

    class LocalAnnotation:
        pass

    with pytest.raises(UnsupportedValueError, match="Local annotation type"):
        capture(LocalAnnotation, set())
    with pytest.raises(UnsupportedValueError, match="Unsupported annotation value"):
        capture(object(), set())


@pytest.mark.skipif(sys.version_info < (3, 14), reason="lazy annotations require Python 3.14")
def test_annotation_evaluator_and_function_metadata_cover_lazy_and_invalid_annotations() -> None:
    db = Database()

    namespace: dict[str, object] = {}
    code = compile(
        "def lazy(value: MissingType): return value",
        "<runtime-lazy-annotation-test>",
        "exec",
        dont_inherit=True,
    )
    exec(code, namespace)
    lazy = cast(Any, namespace["lazy"])
    assert db._function_metadata_payload(lazy, set())[0] == "function-metadata-v3"
    assert db._annotation_evaluator_payload(lazy.__annotate__, {id(lazy.__annotate__)}) == (
        "recursive-annotation-evaluator",
        lazy.__annotate__.__qualname__,
    )

    def invalid_annotations() -> None:
        return None

    invalid_annotations.__annotations__ = {cast(Any, 1): int}
    with pytest.raises(UnsupportedValueError, match="invalid annotations"):
        db._function_metadata_payload(cast(FunctionType, invalid_annotations), set())

    class BrokenAnnotations:
        def __call__(self, format: int) -> object:
            raise RuntimeError(format)

    def broken() -> None:
        return None

    broken_metadata = cast(Any, broken)
    broken_metadata.__annotate__ = BrokenAnnotations()
    with pytest.raises(UnsupportedValueError, match="cannot be fingerprinted safely"):
        db._function_metadata_payload(cast(FunctionType, broken), set())

    def reflects_annotations() -> object:
        return reflects_annotations.__annotations__

    reflects_annotations.__annotations__ = {"return": int}
    assert db._function_metadata_payload(cast(FunctionType, reflects_annotations), set())[0] == (
        "function-metadata-v3"
    )


def test_code_constant_payload_covers_all_constant_kinds_and_rejection() -> None:
    db = Database()
    values = (
        None,
        Ellipsis,
        NotImplemented,
        True,
        1,
        1.5,
        1 + 2j,
        "value",
        b"value",
        (1, "value"),
        frozenset({1, 2}),
        slice(1, 2, 1),
        test_code_constant_payload_covers_all_constant_kinds_and_rejection.__code__,
    )
    assert all(db._code_constant_payload(value) for value in values)
    with pytest.raises(TypeError, match="Unsupported code constant"):
        db._code_constant_payload(object())


def test_policy_payload_covers_functions_builtins_descriptors_callable_state_and_owners() -> None:
    db = Database()

    def equal(left: int, right: int) -> bool:
        return left == right

    assert db._policy_definition_payload(None)[0] == "default-semantic-equality-v3"
    assert db._policy_definition_payload(equal)[0] == "function"
    policy = _PolicyCallable()
    assert db._policy_definition_payload(policy)[0] == "callable"
    assert db._policy_definition_payload(policy.compare)[0] == "bound-function"
    assert db._policy_definition_payload(len)[0] == "builtin"
    assert db._policy_definition_payload(str.__eq__)[0] == "method-descriptor"

    token = db._policy_fingerprint_stack.set((id(policy),))
    try:
        assert db._policy_definition_payload(policy)[0] == "recursive-policy"
    finally:
        db._policy_fingerprint_stack.reset(token)

    assert db._policy_bound_owner_payload(None) == ("none",)
    assert db._policy_bound_owner_payload(os)[0] == "module"
    assert db._policy_bound_owner_payload(int)[0] == "builtin-type"
    assert db._policy_bound_owner_payload(policy, allow_instance_state=True)[0] == "instance"

    with pytest.raises(UnsupportedValueError, match="not snapshot-safe"):
        db._policy_bound_owner_payload([])
    assert db._policy_bound_owner_payload([], allow_instance_state=True)[0] == "instance"
    assert db._policy_instance_state_payload(object()) == ()
    with pytest.raises(UnsupportedValueError, match="slot state"):
        db._policy_instance_state_payload(_SlottedPolicy())
    with pytest.raises(UnsupportedValueError, match="non-Python callable"):
        db._policy_definition_payload(object())


def test_adapter_fingerprints_cover_state_methods_and_trust_failures() -> None:
    adapter = _RuntimeAdapter(offset=2)
    db = Database(adapters={_RuntimeType: adapter})
    digest = db._adapter_implementation_digest(adapter)
    adapter_key = next(iter(db._current_adapter_digests()))
    assert db._current_adapter_digests() == {adapter_key: digest}

    db._checkpoint_adapter_digests = {adapter_key: digest}
    assert db._adapter_keys_trusted([adapter_key])
    db._checkpoint_adapter_digests = {adapter_key: "0" * 64}
    assert not db._adapter_keys_trusted([adapter_key])
    assert not db._adapter_keys_trusted(["missing:Adapter"])

    assert (
        Database()._adapter_method_payload(cast(Any, _BuiltinMethodAdapter()), "freeze")[0]
        == "freeze"
    )
    assert Database()._adapter_state_payload(cast(Any, object())) == ()

    with pytest.raises(UnsupportedValueError, match="slot state"):
        Database()._adapter_state_payload(cast(Any, _SlottedAdapter()))
    with pytest.raises(UnsupportedValueError, match="instance state"):
        Database()._adapter_state_payload(cast(Any, _UnsafeStateAdapter()))
    with pytest.raises(UnsupportedValueError, match="cannot be fingerprinted"):
        Database()._adapter_method_payload(cast(Any, _UnsafeMethodAdapter()), "freeze")

    unsafe_db = Database(adapters={_RuntimeType: cast(Any, _SlottedAdapter())})
    unsafe_db._checkpoint_adapter_digests = {adapter_key: digest}
    assert not unsafe_db._adapter_keys_trusted([adapter_key])


def test_captured_immutable_and_type_payloads_cover_behavior_bearing_shapes() -> None:
    db = Database()

    def owner() -> None:
        return None

    def captured(value: object) -> tuple[Any, ...]:
        return cast(
            tuple[Any, ...],
            db._freeze_captured_immutable(
                "value",
                value,
                set(),
                owner=cast(FunctionType, owner),
                active_ids=set(),
            ),
        )

    assert captured(slice(1, 2, 1))[0] == "capture-slice"
    assert captured((1, 2))[0] == "capture-tuple"
    tuple_subclass = _TupleSubclass((1, 2))
    tuple_subclass.metadata = "safe"
    assert captured(tuple_subclass)[0] == "capture-tuple-subclass"
    assert captured(frozenset({1, 2}))[0] == "capture-frozenset"
    frozen_subclass = _FrozenSetSubclass({1, 2})
    frozen_subclass.metadata = "safe"
    assert captured(frozen_subclass)[0] == "capture-frozenset-subclass"
    assert captured(_FrozenRuntimeConfig(1))[0] == "capture-frozen-dataclass"
    with pytest.raises(UnsupportedValueError, match="Mutable dataclass"):
        captured(_MutableRuntimeConfig(1))

    rich_payload = db._implementation_type_payload(_RichRuntimeType)
    assert rich_payload[0] == "implementation-type-v3"
    dataclass_payload = db._implementation_type_payload(_RichDataclass)
    assert dataclass_payload[0] == "implementation-type-v3"

    seen = {_RichRuntimeType}
    assert db._local_implementation_type_payload(_RichRuntimeType, {id(_RichRuntimeType)})[0] == (
        "recursive-type"
    )
    assert seen == {_RichRuntimeType}


def test_type_anchor_and_dataclass_default_helpers_cover_failure_and_factory_kinds() -> None:
    db = Database()
    ghost = type("MissingBinding", (), {"__module__": __name__})
    with pytest.raises(UnsupportedValueError, match="live module binding"):
        db._module_type_anchor_payload(ghost)
    missing_module = type("MissingModule", (), {"__module__": "missing_runtime_module"})
    with pytest.raises(UnsupportedValueError, match="no loaded defining module"):
        db._module_type_anchor_payload(missing_module)

    def factory() -> int:
        return 1

    assert db._dataclass_default_payload(MISSING) == ("missing",)
    assert db._dataclass_default_payload(1) == ("value", 1)
    assert db._dataclass_default_factory_payload(MISSING) == ("missing",)
    assert db._dataclass_default_factory_payload(factory)[0] == "function"
    assert db._dataclass_default_factory_payload(len)[0] == "builtin"
    assert db._dataclass_default_factory_payload(list)[0] == "type"
    assert db._dataclass_default_factory_payload(_PolicyCallable())[0] == "callable"
    with pytest.raises(UnsupportedValueError, match="is not callable"):
        db._dataclass_default_factory_payload(1)


def test_static_state_helpers_cover_missing_dict_and_string_slots() -> None:
    class StringSlots:
        __slots__ = "value"

    db = Database()
    assert db._instance_slots(StringSlots) == {"value"}
    assert db._static_instance_dict(object()) == {}


def test_checkpoint_public_load_rejects_invalid_missing_and_nonbytes_manifests() -> None:
    store = InMemoryArtifactStore()
    db = Database(store=store)

    with pytest.raises(CheckpointIntegrityError, match="lowercase SHA-256"):
        db.load_checkpoint("not-a-checkpoint")
    with pytest.raises(KeyError, match="not found"):
        db.load_checkpoint("ck" + _DIGEST)

    nonbytes_db = Database(store=cast(Any, _NonBytesStore()))
    with pytest.raises(CheckpointIntegrityError, match="payload is not bytes"):
        nonbytes_db.load_checkpoint("ck" + _DIGEST)

    duplicate = b'{"field":1,"field":2}'
    duplicate_key = "ck" + hashlib.sha256(duplicate).hexdigest()
    store.put(duplicate_key, duplicate)
    with pytest.raises(CheckpointManifestError, match="duplicate JSON field"):
        db.load_checkpoint(duplicate_key)


def test_checkpoint_digest_and_call_snapshot_shape_helpers() -> None:
    assert Database._is_digest(_DIGEST)
    assert not Database._is_digest(1)
    assert not Database._is_digest("a" * 63)
    assert not Database._is_digest("A" * 64)

    valid = freeze(((), {"named": 1}))
    assert Database._is_query_call_snapshot(valid)
    assert not Database._is_query_call_snapshot(1)
    assert not Database._is_query_call_snapshot(((), FrozenDict(((1, 2),))))
    invalid_graph = FrozenGraph(((),), FrozenRef(0))
    assert not Database._is_query_call_snapshot(invalid_graph)


def test_checkpoint_manifest_root_and_record_validation_errors() -> None:
    db = Database()
    store = InMemoryArtifactStore()

    def rejects(manifest: object, message: str) -> None:
        with pytest.raises((CheckpointManifestError, CheckpointVersionError), match=message):
            db._validate_checkpoint_manifest("checkpoint", manifest, store)

    rejects([], "JSON object")
    rejects({}, "missing 'pyinc_ckpt_version'")

    extra_root = _manifest()
    extra_root["extra"] = True
    rejects(extra_root, "fields must be exactly")

    wrong_kernel = _manifest()
    wrong_kernel["kernel_fingerprint_version"] = 1
    rejects(wrong_kernel, "kernel fingerprint version")

    adapters_not_object = _manifest()
    adapters_not_object["adapters"] = []
    rejects(adapters_not_object, "adapters.*object")
    rejects(_manifest(adapters={1: _DIGEST}), "adapter keys")
    rejects(_manifest(adapters={"adapter": "bad"}), "malformed digest")

    records_not_array = _manifest()
    records_not_array["records"] = {}
    rejects(records_not_array, "records.*array")
    rejects(_manifest(records=[cast(Any, 1)]), "record 0 must be an object")

    valid_record = _query_record(_DIGEST, _DIGEST)
    mutations: list[tuple[str, object, str]] = [
        ("kind", "invalid", "invalid fields or kind"),
        ("identity", "", "invalid identity"),
        ("label", "", "invalid label"),
        ("args_digest", "bad", "malformed content address"),
        ("is_untracked", 1, "must be boolean"),
        ("adapter_keys", ["missing"], "invalid adapter keys"),
        ("query_id", "", "invalid query id"),
        ("identity", "query-id:bad", "invalid implementation identity"),
    ]
    for field_name, value, message in mutations:
        record = copy.deepcopy(valid_record)
        record[field_name] = value
        rejects(_manifest(records=[record]), message)

    rejects(
        _manifest(records=[copy.deepcopy(valid_record), copy.deepcopy(valid_record)]),
        "duplicate record identity",
    )


def test_checkpoint_dependency_validation_rejects_bad_and_duplicate_entries() -> None:
    db = Database()

    with pytest.raises(CheckpointManifestError, match="deps must be an array"):
        db._validate_checkpoint_dependencies("checkpoint", 0, {})
    with pytest.raises(CheckpointManifestError, match="must be an object"):
        db._validate_checkpoint_dependencies("checkpoint", 0, [1])
    with pytest.raises(CheckpointManifestError, match="invalid or duplicate"):
        db._validate_checkpoint_dependencies("checkpoint", 0, [{"kind": "unknown"}])

    input_dep = {
        "kind": "input",
        "key": "value",
        "policy_digest": _DIGEST,
        "label": "input[value]",
        "digest": _DIGEST,
    }
    db._validate_checkpoint_dependencies("checkpoint", 0, [input_dep])
    with pytest.raises(CheckpointManifestError, match="invalid or duplicate"):
        db._validate_checkpoint_dependencies("checkpoint", 0, [input_dep, input_dep])

    query_dep = {
        "kind": "query",
        "identity": f"query:{_IMPLEMENTATION_DIGEST}",
        "args_digest": _DIGEST,
        "label": "query",
        "digest": _DIGEST,
        "query_id": "query",
    }
    resource_dep = {
        "kind": "resource",
        "identity": f"resource:{_IMPLEMENTATION_DIGEST}",
        "args_digest": _DIGEST,
        "label": "resource",
        "digest": _DIGEST,
    }
    db._validate_checkpoint_dependencies("checkpoint", 0, [query_dep, resource_dep])


def test_checkpoint_manifest_accepts_valid_query_and_resource_records() -> None:
    store = InMemoryArtifactStore()
    _call, args_digest = _store_snapshot(store, ((), {}))
    _result, result_digest = _store_snapshot(store, 7)
    _probe, probe_digest = _store_snapshot(store, ("present",))

    query_record = _query_record(args_digest, result_digest)
    resource_record = _resource_record(args_digest, result_digest)
    resource_record["probe_bytes"] = store.get(probe_digest).hex()  # type: ignore[union-attr]

    queries, probes, adapters, snapshots = Database()._validate_checkpoint_manifest(
        "checkpoint", _manifest([query_record, resource_record]), store
    )
    assert len(queries) == 1
    assert len(probes) == 1
    assert adapters == {}
    assert set(snapshots) == {args_digest, result_digest}


def test_checkpoint_manifest_rejects_invalid_probe_and_input_dependency_label() -> None:
    store = InMemoryArtifactStore()
    _call, args_digest = _store_snapshot(store, ((), {}))
    _result, result_digest = _store_snapshot(store, 7)
    db = Database()

    resource_record = _resource_record(args_digest, result_digest)
    resource_record["probe_bytes"] = 1
    with pytest.raises(CheckpointManifestError, match="invalid probe bytes"):
        db._validate_checkpoint_manifest("checkpoint", _manifest([resource_record]), store)

    query_record = _query_record(args_digest, result_digest)
    query_record["deps"] = [
        {
            "kind": "input",
            "key": "value",
            "policy_digest": _DIGEST,
            "label": "wrong-label",
            "digest": _DIGEST,
        }
    ]
    with pytest.raises(CheckpointManifestError, match="invalid input dependency label"):
        db._validate_checkpoint_manifest("checkpoint", _manifest([query_record]), store)

    resource_record = _resource_record(args_digest, result_digest)
    resource_record["probe_bytes"] = "AA"
    with pytest.raises(CheckpointManifestError, match="invalid probe bytes"):
        db._validate_checkpoint_manifest("checkpoint", _manifest([resource_record]), store)

    resource_record["probe_bytes"] = "00"
    with pytest.raises(CheckpointManifestError, match="invalid probe bytes"):
        db._validate_checkpoint_manifest("checkpoint", _manifest([resource_record]), store)


def test_checkpoint_manifest_rejects_dangling_and_inconsistent_dependency_records() -> None:
    store = InMemoryArtifactStore()
    _call, args_digest = _store_snapshot(store, ((), {}))
    _result, result_digest = _store_snapshot(store, 7)
    db = Database()

    target = _query_record(args_digest, result_digest)
    target["identity"] = f"target:{_IMPLEMENTATION_DIGEST}"
    target["query_id"] = "target"
    target["label"] = "target-label"

    parent = _query_record(args_digest, result_digest)
    parent["identity"] = f"parent:{'c' * 64}"
    parent["query_id"] = "parent"
    parent["label"] = "parent-label"

    dep = {
        "kind": "query",
        "identity": target["identity"],
        "query_id": target["query_id"],
        "args_digest": target["args_digest"],
        "label": target["label"],
        "digest": target["snapshot_digest"],
    }

    dangling_parent = copy.deepcopy(parent)
    dangling_parent["deps"] = [dep]
    with pytest.raises(CheckpointManifestError, match="dangling dependency"):
        db._validate_checkpoint_manifest("checkpoint", _manifest([dangling_parent]), store)

    for field_name, value, message in (
        ("label", "different-label", "inconsistent dependency label"),
        ("digest", _DIGEST, "inconsistent dependency digest"),
        ("query_id", "different-query", "inconsistent query dependency"),
    ):
        bad_dep = copy.deepcopy(dep)
        bad_dep[field_name] = value
        bad_parent = copy.deepcopy(parent)
        bad_parent["deps"] = [bad_dep]
        with pytest.raises(CheckpointManifestError, match=message):
            db._validate_checkpoint_manifest(
                "checkpoint",
                _manifest([copy.deepcopy(target), bad_parent]),
                store,
            )

    input_parent = copy.deepcopy(parent)
    input_parent["deps"] = [
        {
            "kind": "input",
            "key": "missing",
            "policy_digest": _DIGEST,
            "label": "input[missing]",
            "digest": _DIGEST,
        }
    ]
    queries, _probes, _adapters, _snapshots = db._validate_checkpoint_manifest(
        "checkpoint", _manifest([input_parent]), store
    )
    assert queries == {}


def test_validated_snapshot_reads_cover_missing_hash_mismatch_invalid_and_valid_payloads() -> None:
    db = Database()
    store = InMemoryArtifactStore()
    assert db._read_validated_snapshot(store, _DIGEST) is _MISSING_SNAPSHOT

    store._items[_DIGEST] = b"wrong bytes"
    assert db._read_validated_snapshot(store, _DIGEST) is _MISSING_SNAPSHOT

    malformed = b"K2;X;"
    malformed_digest = hashlib.sha256(malformed).hexdigest()
    store._items[malformed_digest] = malformed
    assert db._read_validated_snapshot(store, malformed_digest) is _MISSING_SNAPSHOT

    snapshot, digest = _store_snapshot(store, {"value": 1})
    assert db._read_validated_snapshot(store, digest) == snapshot


def test_stale_record_detection_covers_missing_untracked_and_newer_dependencies() -> None:
    db = Database()
    root_key = NodeKey("query", "root", _DIGEST, "root")
    dep_key = NodeKey("query", "dep", _DIGEST, "dep")
    root = NodeRecord(root_key, "root", 1, _DIGEST, 1, 1, dependencies={dep_key})
    assert db._record_is_stale_for_save(root)

    dep = NodeRecord(dep_key, "dep", 1, _DIGEST, 1, 1, untracked_reasons=["raw read"])
    db._records[dep_key] = dep
    assert db._record_is_stale_for_save(root)

    dep.untracked_reasons.clear()
    dep.changed_at = 2
    assert db._record_is_stale_for_save(root)

    dep.changed_at = 1
    assert not db._record_is_stale_for_save(root)


def test_checkpoint_dependency_resolution_handles_unknown_and_missing_inputs() -> None:
    db = Database()
    assert db._find_input_node_by_key("missing") is None
    assert db._resolve_checkpoint_dep_key({"kind": "unknown"}) is None
    assert not db._verify_checkpoint_dep({"kind": "unknown"})
    assert not db._verify_checkpoint_input_dep(
        {
            "kind": "input",
            "key": "missing",
            "policy_digest": _DIGEST,
            "digest": _DIGEST,
        }
    )


def test_inspection_failures_discard_cold_query_state() -> None:
    @query(key="inspect-failure-edge")
    def inspect_failure(db: Database) -> int:
        raise RuntimeError("inspect failed")

    @query(key="inspect-fresh-failure-edge")
    def inspect_fresh_failure(db: Database) -> int:
        raise RuntimeError("inspect fresh failed")

    db = Database()
    with pytest.raises(RuntimeError, match="inspect failed"):
        db.inspect(inspect_failure)
    with pytest.raises(RuntimeError, match="inspect fresh failed"):
        db.inspect_fresh(inspect_fresh_failure)
    assert db._query_records == set()


def test_runtime_internal_cleanup_and_missing_registration_branches() -> None:
    db = Database()
    key = NodeKey("query", "identity", _DIGEST, "query")
    record = NodeRecord(key, "query", 1, _DIGEST, 1, 1)

    def callback(event: object) -> None:
        return None

    def other_callback(event: object) -> None:
        return None

    db._observers[key] = [callback]
    db._enqueue_observer_event(cast(Any, type("Q", (), {"key": "query"})()), key, record)

    missing_key = NodeKey("query", "missing", _DIGEST, "missing")
    db._unregister_observer(missing_key, callback)
    db._unregister_observer(key, other_callback)

    db._query_objects()[key.identity] = object()
    db._call_snapshots()[key] = ((), FrozenDict(()))
    db._unregister_observer(key, callback)
    assert key.identity not in db._query_objects()

    value = Input[int]("register-twice")
    assert db._register_input(value) == db._register_input(value)

    query_record = NodeRecord(key, "query", 1, _DIGEST, 1, 1)
    db._records[key] = query_record
    assert db._maybe_changed_after(key, 0)

    resource_key = NodeKey("resource", "resource", _DIGEST, "resource")
    db._records[resource_key] = NodeRecord(resource_key, "resource", 1, _DIGEST, 1, 1)
    assert db._maybe_changed_after(resource_key, 0)

    assert db._load_snapshot_from_store(_DIGEST) is _MISSING_SNAPSHOT
    db._persist_snapshot(freeze(1))


def test_ensure_tracked_read_honors_query_frames_and_raw_read_scope() -> None:
    db = Database()
    key = NodeKey("query", "identity", _DIGEST, "query")
    token = db._execution_stack.set((ExecutionFrame(key),))
    try:
        with pytest.raises(UntrackedReadError, match="untracked"):
            db._ensure_tracked_read("untracked read")
        with db._allow_raw_reads_scope():
            db._ensure_tracked_read("allowed")
    finally:
        db._execution_stack.reset(token)


def test_default_observer_hook_is_used_when_callback_raises(
    capsys: pytest.CaptureFixture[str],
) -> None:
    @query
    def observed(db: Database) -> int:
        return 1

    def fail(event: object) -> None:
        raise RuntimeError("observer failed")

    db = Database()
    db.observe(fail, observed)
    assert db.get(observed) == 1
    assert "observer callback raised RuntimeError: observer failed" in capsys.readouterr().err
