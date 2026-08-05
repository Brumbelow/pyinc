from __future__ import annotations

import json
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias, cast

import pytest

from pyinc import (
    ArtifactStore,
    Database,
    FileSystemArtifactStore,
    InMemoryArtifactStore,
    Input,
    Resource,
    freeze,
    query,
    serialize_snapshot,
)
from pyinc.value import fingerprint_snapshot

Mode: TypeAlias = Literal["strict", "checked", "fast"]
StoreKind: TypeAlias = Literal["memory", "filesystem", "custom"]
ObjectRole: TypeAlias = Literal[
    "query-result",
    "query-call",
    "resource-result",
    "resource-parameter",
]

_MODES: tuple[Mode, ...] = ("strict", "checked", "fast")
_STORE_KINDS: tuple[StoreKind, ...] = ("memory", "filesystem", "custom")
_OBJECT_ROLES: tuple[ObjectRole, ...] = (
    "query-result",
    "query-call",
    "resource-result",
    "resource-parameter",
)


class _DefaultContainsStore(ArtifactStore):
    """Minimal explicit implementation that inherits the protocol default."""

    def __init__(self) -> None:
        self._items: dict[str, bytes] = {}

    def get(self, digest: str) -> bytes | None:
        return self._items.get(digest)

    def put(self, digest: str, payload: bytes) -> None:
        existing = self._items.get(digest)
        if existing is not None and existing != payload:
            raise ValueError(f"Digest collision in custom store for {digest!r}.")
        self._items[digest] = payload


class _RecordingStore:
    def __init__(self, wrapped: ArtifactStore) -> None:
        self.wrapped = wrapped
        self.put_keys: list[str] = []
        self.contains_keys: list[str] = []

    def get(self, digest: str) -> bytes | None:
        return self.wrapped.get(digest)

    def put(self, digest: str, payload: bytes) -> None:
        self.put_keys.append(digest)
        self.wrapped.put(digest, payload)

    def contains(self, digest: str) -> bool:
        self.contains_keys.append(digest)
        return self.wrapped.contains(digest)


class _ContainsTrapStore(_DefaultContainsStore):
    def contains(self, digest: str) -> bool:
        raise AssertionError(f"checkpoint save consulted contains({digest!r})")


@dataclass(frozen=True)
class _NumberResource(Resource[int, int, int]):
    def probe(self, key: int) -> int:
        return key

    def load(self, db: Database, key: int) -> int:
        return key * 10

    def label(self, key: int) -> str:
        return f"number[{key}]"


_NUMBER_RESOURCE = _NumberResource()


def _store(kind: StoreKind, tmp_path: Path) -> ArtifactStore:
    if kind == "memory":
        return InMemoryArtifactStore()
    if kind == "filesystem":
        return FileSystemArtifactStore(tmp_path / "artifact-store")
    return _DefaultContainsStore()


def _observable(value: object) -> bytes:
    return serialize_snapshot(freeze(value))


def test_artifact_store_contains_has_a_working_default() -> None:
    store = _DefaultContainsStore()

    assert isinstance(store, ArtifactStore)
    assert store.contains("missing") is False
    store.put("present", b"payload")
    assert store.contains("present") is True


def test_in_memory_keys_are_read_only_and_detached() -> None:
    store = InMemoryArtifactStore()
    store.put("first", b"one")
    observed = store.keys()

    with pytest.raises(TypeError):
        cast(MutableMapping[str, bytes], observed)["injected"] = b"two"

    store.put("later", b"three")
    assert dict(observed) == {"first": b"one"}
    assert store.get("injected") is None
    assert dict(store.keys()) == {"first": b"one", "later": b"three"}


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("store_kind", _STORE_KINDS)
def test_checkpoint_save_rejects_a_corrupt_preseeded_result(
    mode: Mode,
    store_kind: StoreKind,
    tmp_path: Path,
) -> None:
    source = Input[int](f"corrupt-preseed-{mode}-{store_kind}")

    @query(key=f"corrupt-preseed-result-{mode}-{store_kind}")
    def result(db: Database) -> list[int]:
        return [source.read(db) + 1]

    writer = Database(mode=mode)
    writer.set(source, 41)
    assert _observable(writer.get(result)) == _observable([42])

    expected_snapshot = freeze([42])
    expected_digest = fingerprint_snapshot(expected_snapshot)
    backing = _store(store_kind, tmp_path)
    backing.put(expected_digest, serialize_snapshot(freeze([99])))
    recording = _RecordingStore(backing)

    with pytest.raises(ValueError, match="[Cc]ollision"):
        writer.save_checkpoint(recording)

    assert expected_digest in recording.put_keys
    assert not any(key.startswith("ck") for key in recording.put_keys)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("store_kind", _STORE_KINDS)
def test_equal_preseed_is_idempotently_put_and_checkpoint_warms_like_fresh(
    mode: Mode,
    store_kind: StoreKind,
    tmp_path: Path,
) -> None:
    source = Input[int](f"equal-preseed-{mode}-{store_kind}")

    @query(key=f"equal-preseed-result-{mode}-{store_kind}")
    def result(db: Database) -> list[int]:
        return [source.read(db) + 1]

    writer = Database(mode=mode)
    writer.set(source, 41)
    expected = writer.get(result)
    expected_snapshot = freeze([42])
    expected_digest = fingerprint_snapshot(expected_snapshot)

    backing = _store(store_kind, tmp_path)
    backing.put(expected_digest, serialize_snapshot(expected_snapshot))
    recording = _RecordingStore(backing)
    checkpoint = writer.save_checkpoint(recording)

    assert expected_digest in recording.put_keys
    assert checkpoint in recording.put_keys

    loaded = Database(mode=mode)
    loaded.set(source, 41)
    loaded.load_checkpoint(checkpoint, recording)
    warmed_value = loaded.get(result)

    fresh = Database(mode=mode)
    fresh.set(source, 41)
    fresh_value = fresh.get(result)

    assert _observable(warmed_value) == _observable(fresh_value) == _observable(expected)
    assert loaded.inspect(result).last_recompute == "reused"
    assert loaded.statistics().query_executions == 0


@pytest.mark.parametrize("mode", _MODES)
def test_checkpoint_save_puts_every_referenced_object_without_contains(mode: Mode) -> None:
    @query(key=f"complete-object-write-{mode}")
    def composed(db: Database, value: int) -> list[int]:
        return [_NUMBER_RESOURCE.read(db, value), value]

    writer = Database(mode=mode)
    expected = writer.get(composed, 4)
    store = _ContainsTrapStore()
    checkpoint = writer.save_checkpoint(store)

    manifest_bytes = store.get(checkpoint)
    assert manifest_bytes is not None
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    records = manifest["records"]
    assert {record["kind"] for record in records} == {"query", "resource"}
    for record in records:
        assert store.get(record["snapshot_digest"]) is not None
        assert store.get(record["args_digest"]) is not None

    loaded = Database(mode=mode)
    loaded.load_checkpoint(checkpoint, store)
    warmed_value = loaded.get(composed, 4)

    fresh = Database(mode=mode)
    fresh_value = fresh.get(composed, 4)

    assert _observable(warmed_value) == _observable(fresh_value) == _observable(expected)
    assert loaded.inspect(composed, 4).last_recompute == "reused"
    assert loaded.statistics().query_executions == 0


@pytest.mark.parametrize("object_role", _OBJECT_ROLES)
def test_checkpoint_save_rejects_corruption_in_each_referenced_object(
    object_role: ObjectRole,
) -> None:
    @query(key=f"corrupt-referenced-object-{object_role}")
    def composed(db: Database, value: int) -> list[int]:
        return [_NUMBER_RESOURCE.read(db, value), value]

    writer = Database(mode="checked")
    assert writer.get(composed, 4) == [40, 4]

    source = InMemoryArtifactStore()
    source_checkpoint = writer.save_checkpoint(source)
    manifest_bytes = source.get(source_checkpoint)
    assert manifest_bytes is not None
    records = json.loads(manifest_bytes.decode("utf-8"))["records"]
    query_record = next(record for record in records if record["kind"] == "query")
    resource_record = next(record for record in records if record["kind"] == "resource")
    corrupt_digest = {
        "query-result": query_record["snapshot_digest"],
        "query-call": query_record["args_digest"],
        "resource-result": resource_record["snapshot_digest"],
        "resource-parameter": resource_record["args_digest"],
    }[object_role]

    destination = InMemoryArtifactStore()
    destination.put(corrupt_digest, b"wrong bytes for this content address")

    with pytest.raises(ValueError, match="[Cc]ollision"):
        writer.save_checkpoint(destination)

    stored_objects = destination.keys()
    assert not any(key.startswith("ck") for key in stored_objects)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("store_kind", _STORE_KINDS)
def test_warmed_checkpoint_resave_rejects_corrupt_destination_state(
    mode: Mode,
    store_kind: StoreKind,
    tmp_path: Path,
) -> None:
    @query(key=f"warmed-resave-corruption-{mode}-{store_kind}")
    def result(db: Database, value: int) -> list[int]:
        return [value * 2]

    source_store = InMemoryArtifactStore()
    writer = Database(mode=mode)
    expected = writer.get(result, 21)
    source_checkpoint = writer.save_checkpoint(source_store)

    warmed = Database(mode=mode)
    warmed.load_checkpoint(source_checkpoint, source_store)
    assert _observable(warmed.get(result, 21)) == _observable(expected)
    assert warmed.statistics().query_executions == 0

    expected_snapshot = freeze([42])
    expected_digest = fingerprint_snapshot(expected_snapshot)
    destination = _store(store_kind, tmp_path)
    destination.put(expected_digest, serialize_snapshot(freeze([99])))
    recording = _RecordingStore(destination)

    with pytest.raises(ValueError, match="[Cc]ollision"):
        warmed.save_checkpoint(recording)

    assert expected_digest in recording.put_keys
    assert not any(key.startswith("ck") for key in recording.put_keys)
