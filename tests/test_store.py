from __future__ import annotations

import os
from pathlib import Path

import pytest

from pyinc import (
    ArtifactStore,
    Database,
    FileSystemArtifactStore,
    InMemoryArtifactStore,
    Input,
    deserialize_snapshot,
    freeze,
    query,
    serialize_snapshot,
)
from pyinc.value import fingerprint_snapshot  # not re-exported from pyinc

# ---------------------------------------------------------------------------
# Group A: InMemoryArtifactStore protocol
# ---------------------------------------------------------------------------


def test_in_memory_store_round_trips_payload() -> None:
    store = InMemoryArtifactStore()
    payload = serialize_snapshot(freeze({"a": 1}))
    digest = fingerprint_snapshot(freeze({"a": 1}))

    store.put(digest, payload)

    assert store.get(digest) == payload
    assert store.contains(digest) is True


def test_in_memory_store_returns_none_for_missing_digest() -> None:
    store = InMemoryArtifactStore()
    assert store.get("0" * 64) is None
    assert store.contains("0" * 64) is False


def test_in_memory_store_idempotent_put_with_same_bytes() -> None:
    store = InMemoryArtifactStore()
    payload = b"K2;N;"
    digest = "abc"
    store.put(digest, payload)
    store.put(digest, payload)  # idempotent — must not raise
    assert store.get(digest) == payload


def test_in_memory_store_collision_raises_value_error() -> None:
    store = InMemoryArtifactStore()
    digest = "abc"
    store.put(digest, b"first")
    with pytest.raises(ValueError, match="collision"):
        store.put(digest, b"second")


def test_in_memory_store_satisfies_artifact_store_protocol() -> None:
    store: ArtifactStore = InMemoryArtifactStore()
    assert hasattr(store, "get")
    assert hasattr(store, "put")
    assert hasattr(store, "contains")


# ---------------------------------------------------------------------------
# Group B: FileSystemArtifactStore
# ---------------------------------------------------------------------------


def test_filesystem_store_writes_under_fanout_layout(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path)
    digest = "ab" + "c" * 62
    store.put(digest, b"K2;N;")

    expected_path = tmp_path / "objects" / "ab" / ("c" * 62)
    assert expected_path.exists()
    assert expected_path.read_bytes() == b"K2;N;"


def test_filesystem_store_get_returns_none_for_missing_digest(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path)
    assert store.get("0" * 64) is None


def test_filesystem_store_round_trip(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path)
    payload = serialize_snapshot(freeze([1, 2, 3]))
    digest = fingerprint_snapshot(freeze([1, 2, 3]))

    store.put(digest, payload)

    retrieved = store.get(digest)
    assert retrieved == payload
    assert deserialize_snapshot(retrieved) == freeze([1, 2, 3])


def test_filesystem_store_idempotent_put_same_bytes(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path)
    digest = "ab" + "c" * 62
    store.put(digest, b"K2;N;")
    store.put(digest, b"K2;N;")
    assert store.get(digest) == b"K2;N;"


def test_filesystem_store_collision_raises_value_error(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path)
    digest = "ab" + "c" * 62
    store.put(digest, b"first")
    with pytest.raises(ValueError, match="collision"):
        store.put(digest, b"different")


def test_filesystem_store_persists_across_instances(tmp_path: Path) -> None:
    digest = fingerprint_snapshot(freeze({"key": "value"}))
    payload = serialize_snapshot(freeze({"key": "value"}))

    writer = FileSystemArtifactStore(tmp_path)
    writer.put(digest, payload)

    reader = FileSystemArtifactStore(tmp_path)
    assert reader.get(digest) == payload


# ---------------------------------------------------------------------------
# Group C: Database integration
# ---------------------------------------------------------------------------


def test_database_writes_input_snapshot_to_store() -> None:
    payload = Input[dict[str, int]]("p")
    store = InMemoryArtifactStore()
    db = Database(store=store)

    db.set(payload, {"x": 1})

    digest = fingerprint_snapshot(freeze({"x": 1}))
    assert store.contains(digest)
    assert deserialize_snapshot(store.get(digest)) == freeze({"x": 1})  # type: ignore[arg-type]


def test_database_writes_query_result_snapshot_to_store() -> None:
    payload = Input[int]("seed")
    store = InMemoryArtifactStore()

    @query
    def double(db: Database) -> int:
        return payload.read(db) * 2

    db = Database(store=store)
    db.set(payload, 21)

    assert db.get(double) == 42

    # Both the input snapshot (21) and the result snapshot (42) are persisted.
    assert store.contains(fingerprint_snapshot(freeze(21)))
    assert store.contains(fingerprint_snapshot(freeze(42)))


def test_database_with_no_store_writes_nothing() -> None:
    payload = Input[int]("p")

    @query
    def echo(db: Database) -> int:
        return payload.read(db)

    db = Database()  # store=None default
    db.set(payload, 7)
    assert db.get(echo) == 7  # No errors despite no store.


def test_database_lru_eviction_does_not_remove_from_store() -> None:
    p1 = Input[int]("a")
    p2 = Input[int]("b")
    store = InMemoryArtifactStore()

    @query
    def q1(db: Database) -> int:
        return p1.read(db)

    @query
    def q2(db: Database) -> int:
        return p2.read(db)

    db = Database(store=store, max_query_nodes=1)
    db.set(p1, 100)
    db.set(p2, 200)

    assert db.get(q1) == 100
    assert db.get(q2) == 200  # evicts q1's memo

    # Both result snapshots remain in the store.
    assert store.contains(fingerprint_snapshot(freeze(100)))
    assert store.contains(fingerprint_snapshot(freeze(200)))


def test_database_filesystem_store_writes_through_raw_open_guard(
    tmp_path: Path,
) -> None:
    payload = Input[str]("p")
    store = FileSystemArtifactStore(tmp_path / "store")
    db = Database(store=store)

    db.set(payload, "hello")

    digest = fingerprint_snapshot(freeze("hello"))
    assert store.get(digest) is not None


# ---------------------------------------------------------------------------
# Group D: Phase 1 (mutable graphs) × Phase 2 (storage) composition
# ---------------------------------------------------------------------------


def test_filesystem_store_round_trips_cyclic_graph(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path)
    payload_obj: list[object] = []
    payload_obj.append(payload_obj)

    snapshot = freeze(payload_obj)
    bytes_payload = serialize_snapshot(snapshot)
    digest = fingerprint_snapshot(snapshot)

    store.put(digest, bytes_payload)
    retrieved = store.get(digest)
    assert retrieved == bytes_payload
    assert deserialize_snapshot(retrieved) == snapshot


def test_database_persists_shared_identity_input_to_store() -> None:
    payload = Input[tuple[dict[str, int], dict[str, int]]]("p")
    store = InMemoryArtifactStore()
    db = Database(store=store)

    shared = {"x": 1}
    db.set(payload, (shared, shared))

    digest = fingerprint_snapshot(freeze((shared, shared)))
    assert store.contains(digest)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_database_with_store_under_each_mode_round_trips(
    mode: str, tmp_path: Path
) -> None:
    payload = Input[dict[str, int]]("p")
    store = FileSystemArtifactStore(tmp_path)

    @query
    def echo_x(db: Database) -> int:
        return payload.read(db)["x"]

    db = Database(mode=mode, store=store)
    db.set(payload, {"x": 7})
    assert db.get(echo_x) == 7

    digest = fingerprint_snapshot(freeze({"x": 7}))
    retrieved = store.get(digest)
    assert retrieved is not None
    assert deserialize_snapshot(retrieved) == freeze({"x": 7})


# ---------------------------------------------------------------------------
# Group E: Atomic write behavior
# ---------------------------------------------------------------------------


def test_filesystem_store_atomic_write_uses_temporary_file(tmp_path: Path) -> None:
    """The temporary file must be in the same directory as the target so that
    `os.replace` is guaranteed to be atomic across all common filesystems."""
    store = FileSystemArtifactStore(tmp_path)
    digest = "ab" + "c" * 62
    store.put(digest, b"K2;N;")

    target_dir = tmp_path / "objects" / "ab"
    # No temp files should remain after the put completes.
    leftover = [
        name
        for name in os.listdir(target_dir)
        if name.startswith(".tmp-") or name.startswith("tmp")
    ]
    assert leftover == []


# ---------------------------------------------------------------------------
# Group F: Scope-B checkpoint API (save_checkpoint / load_checkpoint)
# ---------------------------------------------------------------------------


def test_checkpoint_save_requires_store() -> None:
    db = Database()
    with pytest.raises(ValueError, match="ArtifactStore"):
        db.save_checkpoint()


def test_checkpoint_load_requires_store() -> None:
    db = Database()
    with pytest.raises(ValueError, match="ArtifactStore"):
        db.load_checkpoint("ck" + "0" * 64)


def test_checkpoint_load_missing_key_raises_key_error() -> None:
    store = InMemoryArtifactStore()
    db = Database(store=store)
    with pytest.raises(KeyError):
        db.load_checkpoint("ck" + "0" * 64)


def test_checkpoint_basic_round_trip() -> None:
    p = Input[int]("ckp_num")

    @query
    def ckp_doubled(db: Database) -> int:
        return p.read(db) * 2

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    db1.set(p, 21)
    assert db1.get(ckp_doubled) == 42

    ck_key = db1.save_checkpoint()
    assert ck_key.startswith("ck")

    db2 = Database(store=store)
    db2.set(p, 21)
    db2.load_checkpoint(ck_key)
    assert db2.get(ckp_doubled) == 42

    node = db2.inspect(ckp_doubled)
    assert node.last_recompute == "reused"


def test_checkpoint_invalidated_by_changed_input() -> None:
    p = Input[int]("ckp_seed")

    @query
    def ckp_tripled(db: Database) -> int:
        return p.read(db) * 3

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    db1.set(p, 7)
    assert db1.get(ckp_tripled) == 21
    ck_key = db1.save_checkpoint()

    db2 = Database(store=store)
    db2.set(p, 8)  # different input
    db2.load_checkpoint(ck_key)
    assert db2.get(ckp_tripled) == 24  # re-executed with new input

    node = db2.inspect(ckp_tripled)
    assert node.last_recompute == "executed"


def test_checkpoint_key_is_content_addressed() -> None:
    p = Input[int]("ckp_x")

    @query
    def ckp_identity(db: Database) -> int:
        return p.read(db)

    store = InMemoryArtifactStore()

    db1 = Database(store=store)
    db1.set(p, 5)
    db1.get(ckp_identity)
    key1 = db1.save_checkpoint()

    db2 = Database(store=store)
    db2.set(p, 5)
    db2.get(ckp_identity)
    key2 = db2.save_checkpoint()

    assert key1 == key2


def test_checkpoint_filesystem_store_cross_instance(tmp_path: Path) -> None:
    p = Input[str]("ckp_text")

    @query
    def ckp_upper(db: Database) -> str:
        return p.read(db).upper()

    store = FileSystemArtifactStore(tmp_path)
    db1 = Database(store=store)
    db1.set(p, "hello")
    assert db1.get(ckp_upper) == "HELLO"
    ck_key = db1.save_checkpoint()

    store2 = FileSystemArtifactStore(tmp_path)
    db2 = Database(store=store2)
    db2.set(p, "hello")
    db2.load_checkpoint(ck_key)
    assert db2.get(ckp_upper) == "HELLO"
    assert db2.inspect(ckp_upper).last_recompute == "reused"


def test_checkpoint_chain_of_queries() -> None:
    p = Input[int]("ckp_base")

    @query
    def ckp_step1(db: Database) -> int:
        return p.read(db) + 1

    @query
    def ckp_step2(db: Database) -> int:
        return ckp_step1(db) * 10

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    db1.set(p, 4)
    assert db1.get(ckp_step2) == 50  # (4+1)*10
    ck_key = db1.save_checkpoint()

    db2 = Database(store=store)
    db2.set(p, 4)
    db2.load_checkpoint(ck_key)
    assert db2.get(ckp_step2) == 50

    assert db2.inspect(ckp_step2).last_recompute == "reused"
    assert db2.inspect(ckp_step1).last_recompute == "reused"


def test_checkpoint_store_passed_to_save_and_load_directly() -> None:
    p = Input[int]("ckp_direct")

    @query
    def ckp_sq(db: Database) -> int:
        return p.read(db) ** 2

    store = InMemoryArtifactStore()
    db1 = Database()  # no store configured
    db1.set(p, 6)
    db1.get(ckp_sq)
    ck_key = db1.save_checkpoint(store=store)

    db2 = Database()  # no store configured
    db2.set(p, 6)
    db2.load_checkpoint(ck_key, store=store)
    assert db2.get(ckp_sq) == 36
    assert db2.inspect(ckp_sq).last_recompute == "reused"
