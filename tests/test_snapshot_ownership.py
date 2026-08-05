from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

import pyinc.runtime as runtime_module
from pyinc import (
    CheckpointIntegrityError,
    Database,
    InMemoryArtifactStore,
    Input,
    Resource,
    ValueAdapter,
    query,
)
from pyinc.value import (
    FrozenAdapterValue,
    FrozenDict,
    FrozenGraph,
    FrozenList,
    FrozenRecord,
    FrozenRef,
    FrozenSet,
    detach_snapshot,
    fingerprint_snapshot,
    serialize_snapshot,
    thaw,
)

_MODES = ("strict", "checked", "fast")


def _replace_items(value: FrozenList, *items: Any) -> None:
    object.__setattr__(value, "items", tuple(items))


def _sequence(value: Any) -> tuple[Any, ...]:
    return tuple(cast(Any, value))


def test_detach_snapshot_clones_every_wrapper_and_preserves_graph_topology() -> None:
    tree = FrozenRecord(
        "OwnedRecord",
        (
            (
                "mapping",
                FrozenDict(
                    (
                        (
                            FrozenSet("frozenset", (0,)),
                            FrozenAdapterValue(
                                "tests:OwnedAdapter",
                                FrozenList((FrozenSet("frozenset", (1,)),)),
                            ),
                        ),
                    )
                ),
            ),
        ),
    )
    detached_tree = detach_snapshot(tree)

    assert detached_tree is not tree
    assert isinstance(detached_tree, FrozenRecord)
    detached_mapping = detached_tree["mapping"]
    original_mapping = tree["mapping"]
    assert detached_mapping is not original_mapping
    detached_key, detached_adapter = detached_mapping.entries[0]
    original_key, original_adapter = original_mapping.entries[0]
    assert detached_key is not original_key
    assert detached_adapter is not original_adapter
    assert detached_adapter.payload is not original_adapter.payload
    assert detached_adapter.payload[0] is not original_adapter.payload[0]
    assert serialize_snapshot(detached_tree) == serialize_snapshot(tree)

    graph = FrozenGraph(
        nodes=(
            FrozenList((FrozenRef(1), FrozenRef(1), FrozenRef(0))),
            FrozenDict((("back", FrozenRef(0)),)),
        ),
        root=FrozenRef(0),
    )
    detached_graph = detach_snapshot(graph)

    assert isinstance(detached_graph, FrozenGraph)
    assert detached_graph is not graph
    assert detached_graph.nodes[0] is not graph.nodes[0]
    assert detached_graph.nodes[1] is not graph.nodes[1]
    assert detached_graph.root is not graph.root
    assert detached_graph.nodes[0].items[0] is not graph.nodes[0].items[0]
    assert serialize_snapshot(detached_graph) == serialize_snapshot(graph)
    assert fingerprint_snapshot(detached_graph) == fingerprint_snapshot(graph)

    materialized = thaw(detached_graph)
    assert materialized[0] is materialized[1]
    assert materialized[2] is materialized
    assert materialized[0]["back"] is materialized


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("operation", ["set", "set_many"])
def test_input_boundaries_detach_nested_frozen_values(
    mode: str,
    operation: str,
) -> None:
    payload = Input[Any](f"owned-input-{mode}-{operation}")

    @query
    def observe(db: Database, nonce: int) -> tuple[int, ...]:
        del nonce
        return tuple(payload.read(db)["items"])

    retained_inner = FrozenList((1, 2))
    retained_outer = FrozenDict((("items", retained_inner),))
    db = Database(mode=mode)
    if operation == "set":
        db.set(payload, retained_outer)
    else:
        db.set_many([(payload, retained_outer)])

    _replace_items(retained_inner, 99)

    fresh = Database(mode=mode)
    fresh.set(payload, FrozenDict((("items", FrozenList((1, 2))),)))
    assert db.get(observe, 1) == fresh.get(observe, 1) == (1, 2)

    object.__setattr__(retained_outer, "entries", (("items", FrozenList((77,))),))
    assert db.get(observe, 2) == fresh.get(observe, 2) == (1, 2)

    input_key = db._input_records[payload]
    record = db._records[input_key]
    assert fingerprint_snapshot(record.snapshot) == record.digest


@pytest.mark.parametrize("mode", _MODES)
def test_query_argument_snapshot_is_owned_and_reexecutes_like_fresh(mode: str) -> None:
    trigger = Input[int](f"owned-argument-trigger-{mode}")

    @query
    def render(db: Database, values: Any, *, options: Any) -> tuple[int, ...]:
        trigger.read(db)
        return (*values, *options["tail"])

    retained = FrozenList((1, 2))
    retained_tail = FrozenList((3,))
    retained_options = FrozenDict((("tail", retained_tail),))
    db = Database(mode=mode)
    db.set(trigger, 0)
    assert db.get(render, retained, options=retained_options) == (1, 2, 3)
    key, _unused = db._query_key(render, (retained,), {"options": retained_options})
    call_snapshot = db._call_snapshots()[key]

    _replace_items(retained, 99)
    _replace_items(retained_tail, 88)

    assert fingerprint_snapshot(call_snapshot) == key.args_digest
    db.set(trigger, 1)
    with db._state_lock, db._request_scope():
        db._ensure_query(render, key, call_snapshot)
    warm_result = cast(tuple[int, ...], db._thaw_value(db._records[key].snapshot))

    fresh = Database(mode=mode)
    fresh.set(trigger, 1)
    assert (
        warm_result
        == fresh.get(
            render,
            FrozenList((1, 2)),
            options=FrozenDict((("tail", FrozenList((3,))),)),
        )
        == (1, 2, 3)
    )


@pytest.mark.parametrize("mode", _MODES)
def test_query_result_boundary_detaches_caller_retained_frozen_value(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained: list[FrozenList] = []
    original_freeze = cast(Any, runtime_module).freeze

    def capturing_freeze(value: Any, *, adapters: Any = None) -> Any:
        if type(value) is FrozenList and value.items == ("query-result", 1):
            retained.append(value)
        return original_freeze(value, adapters=adapters)

    monkeypatch.setattr(runtime_module, "freeze", capturing_freeze)

    @query
    def produce(db: Database) -> FrozenList:
        del db
        return FrozenList(("query-result", 1))

    @query
    def consume(db: Database, nonce: int) -> tuple[Any, ...]:
        del nonce
        produced: Any = db.get(produce)
        return tuple(produced)

    db = Database(mode=mode)
    assert _sequence(db.get(produce)) == ("query-result", 1)
    _replace_items(retained[-1], "query-result", 99)

    fresh = Database(mode=mode)
    assert db.get(consume, 1) == fresh.get(consume, 1) == ("query-result", 1)


@dataclass(frozen=True)
class _FrozenTextResource(Resource[str, FrozenList, FrozenList]):
    def label(self, path: str) -> str:
        return f"owned-frozen-text[{path}]"

    def probe(self, path: str) -> FrozenList:
        return FrozenList(("probe", Path(path).read_text(encoding="utf-8")))

    def load(self, db: Database, path: str) -> FrozenList:
        del db
        return FrozenList(("value", Path(path).read_text(encoding="utf-8")))


def _capture_frozen_resource_values(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, list[FrozenList]]:
    retained: dict[str, list[FrozenList]] = {"probe": [], "value": []}
    original_freeze = cast(Any, runtime_module).freeze

    def capturing_freeze(value: Any, *, adapters: Any = None) -> Any:
        if type(value) is FrozenList and value.items and value.items[0] in retained:
            retained[cast(str, value.items[0])].append(value)
        return original_freeze(value, adapters=adapters)

    monkeypatch.setattr(runtime_module, "freeze", capturing_freeze)
    return retained


@pytest.mark.parametrize("mode", _MODES)
def test_resource_result_boundary_detaches_retained_frozen_value(
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained = _capture_frozen_resource_values(monkeypatch)
    source = tmp_path / f"resource-result-{mode}.txt"
    source.write_text("old", encoding="utf-8")
    resource = _FrozenTextResource()
    db = Database(mode=mode)

    assert _sequence(db.read_resource(resource, str(source))) == ("value", "old")
    _replace_items(retained["value"][-1], "value", "corrupt")

    fresh = Database(mode=mode)
    warm_result = _sequence(db.read_resource(resource, str(source)))
    fresh_result = _sequence(fresh.read_resource(resource, str(source)))
    assert warm_result == fresh_result == ("value", "old")


@pytest.mark.parametrize("mode", _MODES)
def test_resource_probe_boundary_cannot_be_mutated_into_future_hit(
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained = _capture_frozen_resource_values(monkeypatch)
    source = tmp_path / f"resource-probe-{mode}.txt"
    source.write_text("old", encoding="utf-8")
    resource = _FrozenTextResource()
    db = Database(mode=mode)

    assert _sequence(db.read_resource(resource, str(source))) == ("value", "old")
    _replace_items(retained["probe"][-1], "probe", "new")
    source.write_text("new", encoding="utf-8")

    fresh = Database(mode=mode)
    warm_result = _sequence(db.read_resource(resource, str(source)))
    fresh_result = _sequence(fresh.read_resource(resource, str(source)))
    assert warm_result == fresh_result == ("value", "new")


@dataclass(frozen=True)
class _FailingFrozenProbeResource(Resource[str, str, FrozenList]):
    def label(self, path: str) -> str:
        return f"owned-failing-probe[{path}]"

    def probe(self, path: str) -> FrozenList:
        return FrozenList(("probe", Path(path).read_text(encoding="utf-8")))

    def load(self, db: Database, path: str) -> str:
        del db, path
        raise RuntimeError("expected load failure")


@pytest.mark.parametrize("mode", _MODES)
def test_failed_resource_probe_record_detaches_retained_frozen_value(
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained = _capture_frozen_resource_values(monkeypatch)
    source = tmp_path / f"resource-failure-{mode}.txt"
    source.write_text("failed", encoding="utf-8")
    resource = _FailingFrozenProbeResource()
    db = Database(mode=mode)

    with pytest.raises(RuntimeError, match="expected load failure"):
        db.read_resource(resource, str(source))
    resource_key = next(key for key in db._records if key.kind == "resource")
    expected = serialize_snapshot(FrozenList(("probe", "failed")))

    _replace_items(retained["probe"][-1], "probe", "corrupt")

    assert serialize_snapshot(db._records[resource_key].probe) == expected


@dataclass(frozen=True)
class _AdaptedFrozenPayload:
    payload: FrozenList


class _AdaptedFrozenPayloadAdapter(ValueAdapter):
    def freeze(self, value: _AdaptedFrozenPayload, freeze: Any) -> Any:
        del freeze
        return value.payload

    def thaw(self, snapshot: Any, thaw: Any) -> _AdaptedFrozenPayload:
        return _AdaptedFrozenPayload(FrozenList(tuple(thaw(snapshot))))


@pytest.mark.parametrize("mode", _MODES)
def test_adapter_payload_is_detached_recursively_at_input_boundary(mode: str) -> None:
    adapted = Input[_AdaptedFrozenPayload](f"owned-adapter-{mode}")
    retained = FrozenList((1, 2))
    adapter = _AdaptedFrozenPayloadAdapter()
    db = Database(mode=mode, adapters={_AdaptedFrozenPayload: adapter})
    db.set(adapted, _AdaptedFrozenPayload(retained))

    _replace_items(retained, 99)

    @query
    def observe(db: Database) -> tuple[int, ...]:
        value = adapted.read(db)
        if isinstance(value, FrozenAdapterValue):
            return tuple(value.payload)
        return tuple(value.payload)

    fresh = Database(mode=mode, adapters={_AdaptedFrozenPayload: adapter})
    fresh.set(adapted, _AdaptedFrozenPayload(FrozenList((1, 2))))
    assert db.get(observe) == fresh.get(observe) == (1, 2)


@pytest.mark.parametrize("mode", _MODES)
def test_frozen_graph_input_is_owned_and_preserves_cycle(mode: str) -> None:
    payload = Input[Any](f"owned-graph-{mode}")

    @query
    def inspect_cycle(db: Database) -> tuple[int, bool]:
        value = payload.read(db)
        return len(value), value[0] is value

    retained_node = FrozenList((FrozenRef(0),))
    retained_graph = FrozenGraph((retained_node,), FrozenRef(0))
    db = Database(mode=mode)
    db.set(payload, retained_graph)

    _replace_items(retained_node, 99)
    object.__setattr__(retained_graph.root, "index", 99)

    fresh = Database(mode=mode)
    fresh.set(payload, FrozenGraph((FrozenList((FrozenRef(0),)),), FrozenRef(0)))
    assert db.get(inspect_cycle) == fresh.get(inspect_cycle) == (1, True)


@dataclass(frozen=True)
class _FrozenParameterResource(Resource[FrozenList, int, tuple[int, ...]]):
    trace_path: str

    def label(self, parameter: FrozenList) -> str:
        return "owned-frozen-parameter"

    def probe(self, parameter: FrozenList) -> tuple[int, ...]:
        with Path(self.trace_path).open("a", encoding="utf-8") as stream:
            stream.write(f"{parameter[0]}\n")
        return tuple(parameter)

    def load(self, db: Database, parameter: FrozenList) -> int:
        del db
        return cast(int, parameter[0])


@pytest.mark.parametrize("mode", _MODES)
def test_resource_registry_never_reuses_caller_parameter_under_old_digest(
    mode: str,
    tmp_path: Path,
) -> None:
    trace = tmp_path / f"resource-parameter-{mode}.log"
    resource = _FrozenParameterResource(str(trace))
    retained = FrozenList((1,))
    db = Database(mode=mode)

    assert db.read_resource(resource, retained) == 1
    key = next(key for key in db._records if key.kind == "resource")
    _replace_items(retained, 99)

    assert db._maybe_changed_after(key, -1)
    observed_parameters = trace.read_text(encoding="utf-8").splitlines()
    assert observed_parameters[-1] == "1"
    assert "99" not in observed_parameters

    store = InMemoryArtifactStore()
    db.save_checkpoint(store)
    assert store.get(key.args_digest) == serialize_snapshot(FrozenList((1,)))


@pytest.mark.parametrize("mode", _MODES)
def test_checkpoint_persists_owned_frozen_resource_parameter(
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained: list[FrozenList] = []
    original_freeze = cast(Any, runtime_module).freeze

    def capturing_freeze(value: Any, *, adapters: Any = None) -> Any:
        if type(value) is FrozenList and value.items == (1,):
            retained.append(value)
        return original_freeze(value, adapters=adapters)

    monkeypatch.setattr(runtime_module, "freeze", capturing_freeze)
    trace = tmp_path / f"checkpoint-resource-parameter-{mode}.log"
    resource = _FrozenParameterResource(str(trace))

    @query
    def read_parameter(db: Database) -> int:
        return db.read_resource(resource, FrozenList((1,)))

    writer = Database(mode=mode)
    assert writer.get(read_parameter) == 1
    resource_key = next(key for key in writer._records if key.kind == "resource")
    _replace_items(retained[0], 99)

    store = InMemoryArtifactStore()
    checkpoint = writer.save_checkpoint(store)
    assert store.get(resource_key.args_digest) == serialize_snapshot(FrozenList((1,)))

    loaded = Database(mode=mode, store=store)
    loaded.load_checkpoint(checkpoint)
    fresh = Database(mode=mode)
    assert loaded.get(read_parameter) == fresh.get(read_parameter) == 1
    assert loaded.inspect(read_parameter).last_recompute == "reused"


@pytest.mark.parametrize("mode", _MODES)
def test_checkpoint_warm_matches_fresh_after_input_wrapper_alias_mutation(mode: str) -> None:
    payload = Input[Any](f"owned-checkpoint-input-{mode}")

    @query
    def first_item(db: Database) -> int:
        return cast(int, payload.read(db)[0])

    retained = FrozenList((1,))
    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    writer.set(payload, retained)
    _replace_items(retained, 99)
    assert writer.get(first_item) == 1
    checkpoint = writer.save_checkpoint()

    loaded = Database(mode=mode, store=store)
    loaded.set(payload, FrozenList((1,)))
    loaded.load_checkpoint(checkpoint)
    fresh = Database(mode=mode)
    fresh.set(payload, FrozenList((1,)))
    assert loaded.get(first_item) == fresh.get(first_item) == 1


def test_checkpoint_save_rejects_owned_result_digest_drift() -> None:
    @query
    def frozen_result(db: Database) -> FrozenList:
        del db
        return FrozenList((1,))

    store = InMemoryArtifactStore()
    db = Database(store=store)
    db.get(frozen_result)
    query_key = next(key for key in db._records if key.kind == "query")
    _replace_items(db._records[query_key].snapshot, 99)

    with pytest.raises(CheckpointIntegrityError, match="no longer matches"):
        db.save_checkpoint()


def test_checkpoint_save_rejects_owned_call_snapshot_digest_drift() -> None:
    @query
    def first_item(db: Database, values: Any) -> int:
        del db
        return cast(int, values[0])

    store = InMemoryArtifactStore()
    db = Database(store=store)
    db.get(first_item, FrozenList((1,)))
    query_key = next(key for key in db._records if key.kind == "query")
    call_snapshot = db._call_snapshots()[query_key]
    _replace_items(call_snapshot[0][0], 99)

    with pytest.raises(CheckpointIntegrityError, match="call no longer matches"):
        db.save_checkpoint()


@pytest.mark.parametrize("mode", _MODES)
def test_checkpoint_probe_hint_cannot_be_mutated_into_future_hit(
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retained = _capture_frozen_resource_values(monkeypatch)
    source = tmp_path / f"checkpoint-probe-{mode}.txt"
    source.write_text("old", encoding="utf-8")
    resource = _FrozenTextResource()

    @query
    def read_value(db: Database) -> str:
        return cast(str, db.read_resource(resource, str(source))[1])

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    assert writer.get(read_value) == "old"
    _replace_items(retained["probe"][-1], "probe", "new")
    source.write_text("new", encoding="utf-8")
    checkpoint = writer.save_checkpoint()

    loaded = Database(mode=mode, store=store)
    loaded.load_checkpoint(checkpoint)
    fresh = Database(mode=mode)
    assert loaded.get(read_value) == fresh.get(read_value) == "new"
