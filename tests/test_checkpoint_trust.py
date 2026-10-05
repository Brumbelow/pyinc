"""Store integrity for the durable checkpoint path.

The checkpoint API (`Database.save_checkpoint` / `Database.load_checkpoint`)
trusts an `ArtifactStore` to return, for a given content-address, the bytes
that were written under it. These tests simulate a store that breaks that
contract (bit-flipped bytes, truncation, a foreign kernel version, a tampered
manifest) and pin the kernel's response on both sides of the store.

Reading: snapshot-level corruption is silently skipped and the affected query
re-executes. Manifest-level corruption raises a loud `ValueError`.

Writing: when a present digest's stored bytes disagree with what is being
persisted, the store raises its collision error. A save therefore reports
success only against a store it can warm from. A value re-executed after a
skipped load also raises when it is written back, so the corruption surfaces
as an error. Otherwise every run would recompute around it.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import textwrap
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, cast

import pytest

import pyinc
from pyinc import (
    BUILTIN_ADAPTERS,
    AdapterContractError,
    CheckpointError,
    CheckpointManifestError,
    CheckpointModeError,
    CheckpointVersionError,
    Database,
    FileResource,
    FileStatResource,
    FileStatSnapshot,
    FileSystemArtifactStore,
    FrozenRecord,
    InMemoryArtifactStore,
    Input,
    InputKeyError,
    PyIncError,
    UnsupportedValueError,
    UntrackedReadError,
    freeze,
    query,
    serialize_snapshot,
)
from pyinc.core import Query  # internal: introspecting query identities
from pyinc.runtime import _CHECKPOINT_MANIFEST_VERSION  # internal: manifest schema
from pyinc.value import fingerprint_snapshot  # not re-exported from pyinc


class _BehavioralList(list[int]):
    def score(self) -> int:
        return self[0] * 2


# ---------------------------------------------------------------------------
# Snapshot-level corruption: skipped and re-executed, never surfaced.
# ---------------------------------------------------------------------------


def test_bitflipped_snapshot_bytes_are_skipped_and_reexecuted() -> None:
    p = Input[int]("trust_bitflip")

    @query
    def trust_bitflip_query(db: Database) -> int:
        return p.read(db) + 11

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    db1.set(p, 0)
    assert db1.get(trust_bitflip_query) == 11
    ck_key = db1.save_checkpoint()

    # Plant the serialization of a DIFFERENT valid value under the digest
    # for 11. The bytes still decode cleanly. Only the content address is
    # wrong.
    digest = fingerprint_snapshot(freeze(11))
    store._items[digest] = serialize_snapshot(freeze(99))

    # The loader reads from the store and never writes back to it. This test
    # pins the load side: verification refuses the planted bytes and the query
    # re-executes. Persisting the recomputed value into a store that holds other
    # bytes under that digest raises the store's collision error, which is
    # pinned separately. Passing the store to load_checkpoint makes every
    # snapshot read verify against it.
    db2 = Database()
    db2.set(p, 0)
    db2.load_checkpoint(ck_key, store=store)
    assert db2.get(trust_bitflip_query) == 11
    assert db2.inspect(trust_bitflip_query).last_recompute == "executed"


def test_truncated_snapshot_bytes_are_skipped() -> None:
    p = Input[int]("trust_trunc")

    @query
    def trust_trunc_query(db: Database) -> int:
        return p.read(db) + 22

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    db1.set(p, 0)
    assert db1.get(trust_trunc_query) == 22
    ck_key = db1.save_checkpoint()

    digest = fingerprint_snapshot(freeze(22))
    original = store._items[digest]
    store._items[digest] = original[:-1]

    # Read-only loader, as above. Load refuses the truncated bytes and the
    # query re-executes. The recomputed value stays out of the store, because
    # publishing it over the truncated bytes raises the collision error, a
    # separate behaviour with its own test.
    db2 = Database()
    db2.set(p, 0)
    db2.load_checkpoint(ck_key, store=store)
    assert db2.get(trust_trunc_query) == 22
    assert db2.inspect(trust_trunc_query).last_recompute == "executed"


def test_wrong_kernel_prefix_snapshot_is_skipped() -> None:
    p = Input[int]("trust_prefix")

    @query
    def trust_prefix_query(db: Database) -> int:
        return p.read(db) + 33

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    db1.set(p, 0)
    assert db1.get(trust_prefix_query) == 33
    ck_key = db1.save_checkpoint()

    digest = fingerprint_snapshot(freeze(33))
    original = store._items[digest]
    store._items[digest] = b"K9;" + original[3:]

    # Read-only loader, as above. Load refuses the foreign kernel prefix and
    # the query re-executes. With no write-back store, the re-executed value
    # never meets the planted bytes at `put`. That refusal has its own test.
    db2 = Database()
    db2.set(p, 0)
    db2.load_checkpoint(ck_key, store=store)
    assert db2.get(trust_prefix_query) == 33
    assert db2.inspect(trust_prefix_query).last_recompute == "executed"


def test_missing_snapshot_key_reexecutes() -> None:
    p = Input[int]("trust_missing")

    @query
    def trust_missing_query(db: Database) -> int:
        return p.read(db) + 44

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    db1.set(p, 0)
    assert db1.get(trust_missing_query) == 44
    ck_key = db1.save_checkpoint()

    digest = fingerprint_snapshot(freeze(44))
    del store._items[digest]

    db2 = Database(store=store)
    db2.set(p, 0)
    db2.load_checkpoint(ck_key)
    assert db2.get(trust_missing_query) == 44
    assert db2.inspect(trust_missing_query).last_recompute == "executed"


def test_reexecuted_value_refuses_to_overwrite_wrong_stored_bytes() -> None:
    """The composed case the tests above keep apart: one store serves the load
    and takes the write-back."""
    p = Input[int]("trust_writeback")

    @query
    def trust_writeback_query(db: Database) -> int:
        return p.read(db) + 88

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    db1.set(p, 0)
    assert db1.get(trust_writeback_query) == 88
    ck_key = db1.save_checkpoint()

    digest = fingerprint_snapshot(freeze(88))
    planted = serialize_snapshot(freeze(99))
    store._items[digest] = planted

    db2 = Database(store=store)
    db2.set(p, 0)
    # The load itself succeeds. It runs outside the raises block, so a refusal
    # here fails the test with an error.
    db2.load_checkpoint(ck_key)

    # The victim digest in the match is the execution witness. Only a
    # re-execution that produced 88 can publish under that address, so the test
    # passes only if the record was skipped at load, the query body ran, and the
    # persist of its result was attempted. If load-skipped digests were ever
    # exempt from the persist, nothing would raise and this would fail.
    # (`query_executions` cannot be the witness: it increments after the
    # persist, so the raise comes first.)
    with pytest.raises(ValueError, match=f"Digest collision.*{digest}"):
        db2.get(trust_writeback_query)

    # The refusal leaves the planted bytes in place.
    assert store._items[digest] == planted


# ---------------------------------------------------------------------------
# Save-side store trust: a digest that is present but holds the wrong bytes
# makes the save fail loudly. Success here would return a key for a store the
# database provably cannot warm from.
# ---------------------------------------------------------------------------


def _single_record_snapshot_digest(store: InMemoryArtifactStore, ck_key: str) -> str:
    manifest_bytes = store.get(ck_key)
    assert manifest_bytes is not None
    records = json.loads(manifest_bytes)["records"]
    assert len(records) == 1
    return cast(str, records[0]["snapshot_digest"])


def test_save_checkpoint_raises_on_preseeded_wrong_bytes_in_memory() -> None:
    p = Input[int]("preseed_mem")

    @query
    def preseed_mem_query(db: Database) -> int:
        return p.read(db) + 55

    clean = InMemoryArtifactStore()
    db = Database(store=clean)
    db.set(p, 0)
    assert db.get(preseed_mem_query) == 55
    # A clean save reveals the digest that content-addresses the record.
    victim = _single_record_snapshot_digest(clean, db.save_checkpoint())

    hostile = InMemoryArtifactStore()
    hostile.put(victim, b"wrong bytes")

    with pytest.raises(ValueError, match="Digest collision"):
        db.save_checkpoint(store=hostile)

    # The refusal leaves the corrupt bytes in place.
    assert hostile.get(victim) == b"wrong bytes"


def test_save_checkpoint_raises_on_preseeded_wrong_bytes_on_disk(tmp_path: Path) -> None:
    p = Input[int]("preseed_disk")

    @query
    def preseed_disk_query(db: Database) -> int:
        return p.read(db) + 66

    clean = InMemoryArtifactStore()
    db = Database(store=clean)
    db.set(p, 0)
    assert db.get(preseed_disk_query) == 66
    victim = _single_record_snapshot_digest(clean, db.save_checkpoint())

    hostile = FileSystemArtifactStore(tmp_path / "hostile")
    object_path = hostile.root / "objects" / victim[:2] / victim[2:]
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(b"wrong bytes")

    with pytest.raises(ValueError, match="Digest collision"):
        db.save_checkpoint(store=hostile)

    assert object_path.read_bytes() == b"wrong bytes"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_clean_save_and_load_warm_matches_fresh_per_mode(mode: str) -> None:
    p = Input[int]("roundtrip_seed")

    @query
    def roundtrip_child(db: Database) -> int:
        return p.read(db) + 1

    @query
    def roundtrip_parent(db: Database) -> int:
        return roundtrip_child(db) * 10

    store = InMemoryArtifactStore()
    saver = Database(store=store, mode=mode)
    saver.set(p, 4)
    fresh = saver.get(roundtrip_parent)
    assert fresh == 50
    ck_key = saver.save_checkpoint()

    warm_db = Database(store=store, mode=mode)
    warm_db.set(p, 4)
    warm_db.load_checkpoint(ck_key)
    before = warm_db.statistics()
    warm = warm_db.get(roundtrip_parent)
    after = warm_db.statistics()

    assert warm == fresh
    # Witnesses against a vacuous pass: the warm request executed nothing and
    # reused at least one record.
    executions_during_warm = after.query_executions - before.query_executions
    assert executions_during_warm == 0
    assert after.query_reuses - before.query_reuses >= 1
    assert warm_db.inspect(roundtrip_parent).last_recompute == "reused"
    assert warm_db.inspect(roundtrip_child).last_recompute == "reused"


# ---------------------------------------------------------------------------
# Manifest-level corruption: the manifest is the root of trust, so any
# failure to verify it is loud.
# ---------------------------------------------------------------------------


def test_tampered_manifest_raises_value_error() -> None:
    p = Input[int]("trust_tamper")

    @query
    def trust_tamper_query(db: Database) -> int:
        return p.read(db) + 55

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    db1.set(p, 0)
    assert db1.get(trust_tamper_query) == 55
    ck_key = db1.save_checkpoint()

    # Trailing whitespace keeps the JSON parseable but changes the bytes, so
    # the recomputed content address differs from the requested key.
    store._items[ck_key] = store._items[ck_key] + b" "

    db2 = Database(store=store)
    db2.set(p, 0)
    with pytest.raises(ValueError, match="integrity"):
        db2.load_checkpoint(ck_key)


def test_malformed_manifest_json_raises_value_error() -> None:
    store = InMemoryArtifactStore()
    db = Database(store=store)

    bogus = b"{not valid json"
    key = "ck" + hashlib.sha256(bogus).hexdigest()
    store.put(key, bogus)

    with pytest.raises(ValueError) as exc_info:
        db.load_checkpoint(key)
    assert not isinstance(exc_info.value, json.JSONDecodeError)


def test_deeply_nested_manifest_json_raises_typed_checkpoint_error() -> None:
    """A malformed manifest raises a typed error, never a raw RecursionError."""
    store = InMemoryArtifactStore()
    db = Database(store=store)

    bogus = (b'{"a":' * 200_000) + b"1" + (b"}" * 200_000)
    key = "ck" + hashlib.sha256(bogus).hexdigest()
    store.put(key, bogus)

    with pytest.raises(CheckpointManifestError, match="could not be decoded"):
        db.load_checkpoint(key)


def test_unsupported_manifest_version_raises_value_error() -> None:
    store = InMemoryArtifactStore()
    db = Database(store=store)

    manifest = {"pyinc_ckpt_version": 999, "records": []}
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    key = "ck" + hashlib.sha256(manifest_bytes).hexdigest()
    store.put(key, manifest_bytes)

    with pytest.raises(ValueError, match="Unsupported checkpoint version"):
        db.load_checkpoint(key)


def test_manifest_missing_required_field_raises_value_error() -> None:
    """Structurally missing required fields raise ValueError, never KeyError."""
    store = InMemoryArtifactStore()
    db = Database(store=store)

    manifest = {
        "pyinc_ckpt_version": _CHECKPOINT_MANIFEST_VERSION,
        "kernel_fingerprint_version": 2,
        "records": [{"kind": "query"}],
    }
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    key = "ck" + hashlib.sha256(manifest_bytes).hexdigest()
    store.put(key, manifest_bytes)

    with pytest.raises(ValueError) as exc_info:
        db.load_checkpoint(key)
    assert not isinstance(exc_info.value, KeyError)


def test_manifest_rejects_structurally_invalid_query_call_snapshot() -> None:
    @query(key="invalid-call-snapshot")
    def calculated(db: Database) -> int:
        return 7

    store = InMemoryArtifactStore()
    writer = Database(store=store)
    assert writer.get(calculated) == 7
    checkpoint = writer.save_checkpoint()
    manifest = json.loads(store._items[checkpoint].decode("utf-8"))

    cyclic: list[Any] = []
    cyclic.append(cyclic)
    for invalid_call_snapshot in (freeze(123), freeze(cyclic)):
        invalid_digest = fingerprint_snapshot(invalid_call_snapshot)
        store.put(invalid_digest, serialize_snapshot(invalid_call_snapshot))
        manifest["records"][0]["args_digest"] = invalid_digest
        malformed_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
        malformed_key = "ck" + hashlib.sha256(malformed_bytes).hexdigest()
        store.put(malformed_key, malformed_bytes)

        reader = Database(store=store)
        with pytest.raises(CheckpointManifestError, match="invalid call snapshot"):
            reader.load_checkpoint(malformed_key)
        assert not reader._checkpoint_query_records


def test_checkpoint_accepts_graph_query_call_snapshot() -> None:
    @query(key="graph-call-snapshot")
    def inspect_graph(db: Database, positional: list[Any], *, keyword: list[Any]) -> bool:
        return positional is keyword and positional[0] is positional

    cyclic: list[Any] = []
    cyclic.append(cyclic)
    store = InMemoryArtifactStore()
    writer = Database(mode="checked", store=store)
    assert writer.get(inspect_graph, cyclic, keyword=cyclic) is True
    checkpoint = writer.save_checkpoint()

    reader = Database(mode="checked", store=store)
    reader.load_checkpoint(checkpoint)
    equivalent: list[Any] = []
    equivalent.append(equivalent)

    assert reader.get(inspect_graph, equivalent, keyword=equivalent) is True
    assert reader.inspect(inspect_graph, equivalent, keyword=equivalent).last_recompute == "reused"
    assert reader.statistics().query_executions == 0


# ---------------------------------------------------------------------------
# Warm-path soundness: a record restored from a checkpoint takes part in
# normal invalidation. It carries real dependency edges, its revisions are
# normalised onto the loading database's timeline, and its resources are
# re-probed against live state. An incremental read after a load therefore
# equals a fresh recomputation.
# ---------------------------------------------------------------------------


def test_input_change_after_load_reexecutes() -> None:
    p = Input[int]("warm_input_change")

    @query
    def warm_input_query(db: Database) -> int:
        return p.read(db) + 1

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    db1.set(p, 10)
    assert db1.get(warm_input_query) == 11
    ck_key = db1.save_checkpoint()

    db2 = Database(store=store)
    db2.set(p, 10)
    db2.load_checkpoint(ck_key)
    # The first get warms the record from the checkpoint (reused, unchanged).
    assert db2.get(warm_input_query) == 11
    assert db2.inspect(warm_input_query).last_recompute == "reused"

    # Change the input the query depends on. The warmed record carries a real
    # edge to that input, so the next get re-executes against the new value.
    db2.set(p, 20)
    assert db2.get(warm_input_query) == 21
    assert db2.inspect(warm_input_query).last_recompute == "executed"


def test_file_change_between_runs_reexecutes(tmp_path: Path) -> None:
    data_file = tmp_path / "content.txt"
    data_file.write_text("alpha")
    resource = FileResource()

    @query
    def warm_file_query(db: Database) -> str:
        return "value:" + resource.read(db, str(data_file))

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    assert db1.get(warm_file_query) == "value:alpha"
    ck_key = db1.save_checkpoint()

    # The file changes on disk between the save and the load.
    data_file.write_text("omega")

    db2 = Database(store=store)
    db2.load_checkpoint(ck_key)
    # The fresh database has no live resource record, so the warm is refused.
    # The query re-executes and re-probes the changed file.
    assert db2.get(warm_file_query) == "value:omega"
    assert db2.inspect(warm_file_query).last_recompute == "executed"


def test_second_request_after_load_still_reuses() -> None:
    p = Input[int]("warm_stable")

    @query
    def warm_stable_query(db: Database) -> int:
        return p.read(db) * 5

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    db1.set(p, 3)
    assert db1.get(warm_stable_query) == 15
    ck_key = db1.save_checkpoint()

    db2 = Database(store=store)
    db2.set(p, 3)
    db2.load_checkpoint(ck_key)

    assert db2.get(warm_stable_query) == 15
    assert db2.inspect(warm_stable_query).last_decision == "reused"
    reuses_after_first = db2.statistics().query_reuses

    # A second request against unchanged state still reuses. Normalising the
    # warmed record's revisions onto this database's timeline must leave it
    # clean, or every request would thrash into a re-execute.
    assert db2.get(warm_stable_query) == 15
    assert db2.inspect(warm_stable_query).last_decision == "reused"
    assert db2.statistics().query_reuses > reuses_after_first
    assert db2.statistics().query_executions == 0


def test_warmed_dependency_can_be_resaved_to_a_fresh_store() -> None:
    @query(key="portable-resave-child")
    def child(db: Database, value: int) -> int:
        return value * 2

    @query(key="portable-resave-parent")
    def parent(db: Database) -> int:
        return child(db, 3) + 1

    first_store = InMemoryArtifactStore()
    writer = Database(store=first_store)
    assert writer.get(parent) == 7
    first_checkpoint = writer.save_checkpoint()

    warmed = Database(store=first_store)
    warmed.load_checkpoint(first_checkpoint)
    assert warmed.get(parent) == 7
    assert warmed.statistics().query_executions == 0

    second_store = InMemoryArtifactStore()
    second_checkpoint = warmed.save_checkpoint(second_store)
    assert second_checkpoint in second_store._items

    reader = Database(store=second_store)
    reader.load_checkpoint(second_checkpoint)
    assert reader.get(parent) == 7
    assert reader.inspect(parent).last_recompute == "reused"
    assert reader.statistics().query_executions == 0


def test_checkpoint_resource_parameter_type_mismatch_cannot_poison_request() -> None:
    @dataclass(frozen=True)
    class BehavioralResource:
        def identity(self) -> tuple[str]:
            return ("behavioral-parameter",)

        def read(self, db: Database, parameter: list[int]) -> int:
            return cast(int, db.read_resource(self, parameter))

        def label(self, parameter: list[int]) -> str:
            return "behavioral-parameter"

        def probe(self, parameter: list[int]) -> tuple[int, ...]:
            return tuple(parameter)

        def load(self, db: Database, parameter: list[int]) -> int:
            return parameter.score() if isinstance(parameter, _BehavioralList) else 0

    resource = BehavioralResource()

    @query(key="behavioral-parameter-root")
    def root(db: Database) -> int:
        return resource.read(db, _BehavioralList([2]))

    store = InMemoryArtifactStore()
    writer = Database(store=store)
    assert writer.get(root) == 4
    checkpoint = writer.save_checkpoint()

    reader = Database(store=store)
    reader.load_checkpoint(checkpoint)
    assert reader.get(root) == 4
    assert Database().get(root) == 4


def test_transitive_dep_change_invalidates_warmed_parent_without_live_child() -> None:
    p = Input[int]("warm_transitive")

    @query
    def warm_child(db: Database) -> int:
        return p.read(db) + 1

    @query
    def warm_parent(db: Database) -> int:
        return warm_child(db) * 10

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    db1.set(p, 4)
    assert db1.get(warm_parent) == 50
    ck_key = db1.save_checkpoint()

    db2 = Database(store=store)
    db2.set(p, 4)
    db2.load_checkpoint(ck_key)
    # Warm the parent, which transitively warms the child as a dependency. The
    # child is restored without a live Query object of its own.
    assert db2.get(warm_parent) == 50
    assert db2.inspect(warm_parent).last_recompute == "reused"

    # Change the child's input. With no live child to walk into, the parent
    # re-verifies the warmed child record transitively and re-executes.
    db2.set(p, 9)
    assert db2.get(warm_parent) == 100
    assert db2.inspect(warm_parent).last_recompute == "executed"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_untracked_records_are_never_warmed(mode: str) -> None:
    p = Input[int]("warm_untracked")

    @query
    def warm_untracked_query(db: Database) -> int:
        db.report_untracked_read("external_clock")
        return p.read(db) + 1

    store = InMemoryArtifactStore()
    db1 = Database(store=store, mode=mode)
    db1.set(p, 7)
    assert db1.get(warm_untracked_query) == 8
    assert db1.inspect(warm_untracked_query).is_untracked
    ck_key = db1.save_checkpoint()

    db2 = Database(store=store, mode=mode)
    db2.set(p, 7)
    db2.load_checkpoint(ck_key)
    # An untracked record is impure by definition, so a checkpoint never serves
    # it. The warm is refused and the query re-executes.
    assert db2.get(warm_untracked_query) == 8
    assert db2.inspect(warm_untracked_query).last_recompute == "executed"


# ---------------------------------------------------------------------------
# Deterministic, code-complete identities (D1/D2/D3/D7).
#
# A checkpoint identity covers all the code it depends on and is reproducible
# in a fresh process. When a captured dependency query's body changes, the
# parent's identity moves (transitive code pinning). Identities hold no
# per-process addresses and react to build flags. Manifests are versioned
# strictly.
# ---------------------------------------------------------------------------


def _make_dep_child(value: int) -> Query[..., int]:
    # A query factory. Every child shares a query_id (same qualname and module)
    # but captures a different body. This stands in for a code change to a
    # dependency query that keeps its name across runs.
    @query
    def dep_child(db: Database) -> int:
        return value

    return dep_child


def _make_dep_parent(child: Query[..., int]) -> Query[..., int]:
    @query
    def dep_parent(db: Database) -> int:
        return child(db) + 1

    return dep_parent


def _pyinc_src_dir() -> str:
    import pyinc

    return str(Path(pyinc.__file__).resolve().parent.parent)


def test_dep_query_code_change_between_runs_reexecutes() -> None:
    child_v1 = _make_dep_child(1)
    parent = _make_dep_parent(child_v1)

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    assert db1.get(parent) == 2
    ck_key = db1.save_checkpoint()

    # A future process rebuilds the same-query_id child with a changed body.
    child_v2 = _make_dep_child(2)
    parent_v2 = _make_dep_parent(child_v2)

    db2 = Database(store=store)
    db2.load_checkpoint(ck_key)
    # The child's code is pinned into the parent's identity, so the parent's
    # identity moves away from the checkpointed record. The parent re-executes
    # against the new child body.
    assert db2.get(parent_v2) == 3
    assert db2.inspect(parent_v2).last_recompute == "executed"


def test_query_identity_pins_captured_query_code() -> None:
    parent_a = _make_dep_parent(_make_dep_child(1))
    parent_b = _make_dep_parent(_make_dep_child(2))

    db = Database()
    identity_a = db._query_key(parent_a, (), {})[0].identity
    identity_b = db._query_key(parent_b, (), {})[0].identity

    # Same parent source, same captured-child query_id, different child body.
    # The identities diverge because the child's code is folded in.
    assert identity_a != identity_b


def test_code_fingerprint_stable_across_processes(tmp_path: Path) -> None:
    script = tmp_path / "fp_script.py"
    script.write_text(
        textwrap.dedent(
            """
            from pyinc import Database, Input, query

            gauge = Input[int]("gauge")

            @query
            def fp_child(db: Database) -> int:
                return 7

            @query
            def fp_parent(db: Database) -> int:
                return gauge.read(db) + fp_child(db)

            db = Database()
            key, _ = db._query_key(fp_parent, (), {})
            print(key.identity)
            """
        )
    )
    env = {**os.environ, "PYTHONPATH": _pyinc_src_dir()}
    first = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout
    second = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout

    assert first.strip() != ""
    # Two independent processes (each with its own randomized hash seed and
    # object addresses) must compute byte-identical identities.
    assert first == second


def test_optimize_flag_changes_identity(tmp_path: Path) -> None:
    script = tmp_path / "opt_script.py"
    script.write_text(
        textwrap.dedent(
            """
            from pyinc import Database, query

            @query
            def opt_leaf(db: Database) -> int:
                return 41

            @query
            def opt_top(db: Database) -> int:
                return opt_leaf(db) + 1

            db = Database()
            key, _ = db._query_key(opt_top, (), {})
            print(key.identity)
            """
        )
    )
    env = {**os.environ, "PYTHONPATH": _pyinc_src_dir()}
    normal = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout
    optimized = subprocess.run(
        [sys.executable, "-O", str(script)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout

    assert normal.strip() != ""
    # -O changes captured-module behaviour without changing source digests, so
    # the build configuration is part of the identity.
    assert normal != optimized


def test_duplicate_input_keys_are_rejected_before_checkpointing() -> None:
    first = Input[int]("dup_name", eq=lambda old, new: old == new)
    second = Input[int]("dup_name", cutoff=lambda value: value)
    db = Database(store=InMemoryArtifactStore())
    db.set(first, 1)
    with pytest.raises(InputKeyError, match="dup_name"):
        db.set(second, 2)
    assert first.read(db) == 1


def test_runtime_imported_dep_query_refuses_warm_and_recomputes(
    tmp_path: Path,
) -> None:
    mod_name = "pyinc_ckpt_runtime_child"
    child_path = tmp_path / f"{mod_name}.py"

    def write_child(value: int) -> None:
        child_path.write_text(
            textwrap.dedent(
                f"""
                from pyinc import Database, query

                @query
                def imported_child(db: Database) -> int:
                    return {value}
                """
            )
        )

    def purge() -> None:
        importlib.invalidate_caches()
        sys.modules.pop(mod_name, None)

    sys.path.insert(0, str(tmp_path))
    # Disable bytecode caching. Two rewrites in the same second can share a .pyc
    # and reimport the stale body, which would hide whether the warm gate fired.
    saved_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        write_child(1)
        purge()

        @query
        def runtime_import_parent(db: Database) -> int:
            # The body imports the child at runtime, so the child is never
            # captured and cannot be code-pinned.
            module = importlib.import_module(mod_name)
            child_value: int = module.imported_child(db)
            return child_value + 1000

        store = InMemoryArtifactStore()
        db1 = Database(store=store)
        assert db1.get(runtime_import_parent) == 1001
        ck_key = db1.save_checkpoint()

        # The child's module changes between save and load.
        write_child(2)
        purge()

        db2 = Database(store=store)
        db2.load_checkpoint(ck_key)
        # The dep query is outside the parent's pinned capture set, so the warm
        # is refused and the parent re-executes against the changed module.
        assert db2.get(runtime_import_parent) == 1002
        assert db2.inspect(runtime_import_parent).last_recompute == "executed"
    finally:
        sys.dont_write_bytecode = saved_dont_write_bytecode
        purge()
        with suppress(ValueError):
            sys.path.remove(str(tmp_path))


def test_v2_manifest_rejected_loudly() -> None:
    store = InMemoryArtifactStore()
    db = Database(store=store)

    manifest = {
        "pyinc_ckpt_version": 2,
        "kernel_fingerprint_version": 2,
        "records": [],
    }
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    key = "ck" + hashlib.sha256(manifest_bytes).hexdigest()
    store.put(key, manifest_bytes)

    with pytest.raises(ValueError, match="Unsupported checkpoint version"):
        db.load_checkpoint(key)


def test_v4_manifest_rejected_loudly() -> None:
    """A 3.0.0 manifest records dependency sets whose meaning has since changed.

    3.0.0 recorded no dependencies for a reader whose resource read raised a
    caught exception. Warming such a record here would report "dependencies
    unchanged" for a node a fresh database re-derives. The record layout is the
    same, so the manifest version has to carry the difference.
    """
    store = InMemoryArtifactStore()
    db = Database(store=store)

    manifest = {
        "pyinc_ckpt_version": 4,
        "kernel_fingerprint_version": 2,
        "records": [],
    }
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    key = "ck" + hashlib.sha256(manifest_bytes).hexdigest()
    store.put(key, manifest_bytes)

    with pytest.raises(ValueError, match="Unsupported checkpoint version"):
        db.load_checkpoint(key)


def test_v5_manifest_rejected_loudly() -> None:
    """A 3.1.x manifest can carry records this kernel would trust unsoundly.

    The kernel that wrote version-5 records may have had two flaws. It derived
    captured-module identity from a stat tuple that a same-size rewrite can
    preserve. It dropped the resource edge for a stat probe raising
    NotADirectoryError. Both leave records a warm database would reuse while a
    fresh one re-derives, so the manifest version has to carry the difference.
    """
    store = InMemoryArtifactStore()
    db = Database(store=store)

    manifest = {
        "pyinc_ckpt_version": 5,
        "kernel_fingerprint_version": 2,
        "records": [],
    }
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    key = "ck" + hashlib.sha256(manifest_bytes).hexdigest()
    store.put(key, manifest_bytes)

    with pytest.raises(ValueError, match="Unsupported checkpoint version"):
        db.load_checkpoint(key)


def test_v6_manifest_rejected_loudly() -> None:
    """A version-6 manifest predates the manifest's save-mode field.

    The value a query computes and persists depends on the database mode, so a
    record carries a value only a database in the saving mode would produce.
    Version 6 records no mode, so its records cannot be attributed to any mode,
    and the manifest version has to carry the difference.
    """
    store = InMemoryArtifactStore()
    db = Database(store=store)

    manifest = {
        "pyinc_ckpt_version": 6,
        "kernel_fingerprint_version": 2,
        "records": [],
    }
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    key = "ck" + hashlib.sha256(manifest_bytes).hexdigest()
    store.put(key, manifest_bytes)

    with pytest.raises(ValueError, match="Unsupported checkpoint version"):
        db.load_checkpoint(key)


def test_v7_manifest_rejected_loudly() -> None:
    """A version-7 record predates the kernel's built-in file-stat adapter.

    Such a record froze a stat reading field by field into a plain record. This
    kernel freezes it through an adapter. The record gives no sign of the
    difference: the adapter gate reads the keys a record used, and a record
    written before the adapter names none. A database holding the built-in
    would warm that encoding without re-freezing it and return a shape no fresh
    execution produces. The manifest version has to carry the difference.
    """
    store = InMemoryArtifactStore()
    db = Database(store=store)

    manifest = {
        "pyinc_ckpt_version": 7,
        "kernel_fingerprint_version": 2,
        "records": [],
    }
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    key = "ck" + hashlib.sha256(manifest_bytes).hexdigest()
    store.put(key, manifest_bytes)

    with pytest.raises(ValueError, match="Unsupported checkpoint version"):
        db.load_checkpoint(key)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize(
    "reader",
    [
        "cwd",
        pytest.param(
            "byte-environment",
            marks=pytest.mark.skipif(
                not os.supports_bytes_environ, reason="POSIX byte environment"
            ),
        ),
    ],
)
def test_v8_guard_records_report_checkpoint_version_error(
    mode: str, reader: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    @query(key=f"v8-guard-record-{reader}")
    def observation(db: Database) -> str | bytes:
        if reader == "cwd":
            return os.getcwd()
        if sys.platform != "win32":
            return os.getenvb(b"PYINC_CHECKPOINT_UPGRADE", b"")
        return b""

    store = InMemoryArtifactStore()
    saver = Database(mode, store=store)
    legacy_value = "legacy-cwd" if reader == "cwd" else b"legacy-env"

    def legacy_read(*args: Any, **kwargs: Any) -> str | bytes:
        return legacy_value

    with monkeypatch.context() as legacy:
        legacy.setattr(os, "getcwd" if reader == "cwd" else "getenvb", legacy_read)
        assert saver.get(observation) == legacy_value
        manifest = json.loads(cast(bytes, store.get(saver.save_checkpoint())))
        assert len(manifest["records"]) == 1
        manifest["pyinc_ckpt_version"] = 8
        manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
        key = "ck" + hashlib.sha256(manifest_bytes).hexdigest()
        store.put(key, manifest_bytes)

    fresh = Database(mode)
    with pytest.raises(UntrackedReadError):
        fresh.get(observation)

    loader = Database(mode, store=store)
    with pytest.raises(
        CheckpointVersionError, match=rf"version 8; expected {_CHECKPOINT_MANIFEST_VERSION}"
    ):
        loader.load_checkpoint(key)
    assert loader._checkpoint_query_records == {}
    assert loader._checkpoint_resource_probes == {}
    assert loader._checkpoint_adapter_digests == {}
    assert loader.statistics().node_count == 0


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_pre_adapter_checkpoints_are_refused_not_warmed(mode: str, tmp_path: Path) -> None:
    """A manifest from before the built-in adapter is refused.

    The manifest is built the way the pre-adapter kernel built it: schema
    version 7, with an `adapters` map that omits the file-stat key, because
    that kernel had no such adapter. Its records are this kernel's own. The
    record layout never moved, so only the version field tells the two apart.
    Without that check, the load would warm a stat reading in an encoding this
    kernel has stopped producing.

    The refusal is complete: the loader raises, stages nothing, and the next
    request executes for itself.
    """
    target = tmp_path / "watched.txt"
    target.write_text("hello", encoding="utf-8")
    stats = FileStatResource()

    @query(key=f"pre-adapter-refusal-{mode}")
    def watched_size(db: Database) -> Any:
        return stats.read(db, str(target)).size

    store = InMemoryArtifactStore()
    saver = Database(mode, store=store)
    assert saver.get(watched_size) == 5
    manifest = json.loads(cast(bytes, store.get(saver.save_checkpoint())).decode("utf-8"))

    manifest["pyinc_ckpt_version"] = 7
    manifest["adapters"] = {}
    pre_adapter_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    pre_adapter_key = "ck" + hashlib.sha256(pre_adapter_bytes).hexdigest()
    store.put(pre_adapter_key, pre_adapter_bytes)

    loader = Database(mode, store=store)
    with pytest.raises(CheckpointVersionError, match="Unsupported checkpoint version"):
        loader.load_checkpoint(pre_adapter_key)

    assert loader._checkpoint_query_records == {}
    assert loader._checkpoint_adapter_digests == {}
    before = loader.statistics()
    assert loader.get(watched_size) == 5
    after = loader.statistics()
    # Witness that the request after the refusal ran a real execution, with no
    # warm slipping past the raise.
    assert after.query_executions - before.query_executions == 1
    assert after.query_reuses - before.query_reuses == 0


def test_a_manifest_hiding_a_records_adapter_keys_is_refused(tmp_path: Path) -> None:
    """A record may name only adapter keys its own manifest declares.

    This keeps the warm gate's cheap path sound. That path trusts every key it
    is given when no checkpoint digests are loaded and the registry holds only
    the kernel's fixed adapters, because such a key can only come from a record
    this process froze. A manifest with an empty `adapters` map whose records
    still name a key would break that reasoning. The manifest validator refuses
    that shape before any record is staged. The save path cannot write it,
    because it writes the map from the whole registry.
    """
    target = tmp_path / "watched.txt"
    target.write_text("hello", encoding="utf-8")
    stats = FileStatResource()

    @query(key="undeclared-adapter-keys")
    def watched_size(db: Database) -> Any:
        return stats.read(db, str(target)).size

    store = InMemoryArtifactStore()
    saver = Database("checked", store=store)
    assert saver.get(watched_size) == 5
    manifest = json.loads(cast(bytes, store.get(saver.save_checkpoint())).decode("utf-8"))
    assert [record["adapter_keys"] for record in manifest["records"]].count(
        ["pyinc.resources:FileStatSnapshot"]
    ) == 1

    manifest["adapters"] = {}
    hidden_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    hidden_key = "ck" + hashlib.sha256(hidden_bytes).hexdigest()
    store.put(hidden_key, hidden_bytes)

    loader = Database("checked", store=store)
    with pytest.raises(CheckpointManifestError, match="invalid adapter keys"):
        loader.load_checkpoint(hidden_key)
    assert loader._checkpoint_query_records == {}


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_file_stat_records_round_trip_through_a_checkpoint_per_mode(
    mode: str, tmp_path: Path
) -> None:
    """The adapted stat reading warms to what a fresh run computes.

    This guards the sibling refusal above. A checkpoint this kernel writes
    still warms in a database of the same mode, through the adapter. The
    reading itself sits in the dependency graph as well as in the returned
    value.
    """
    target = tmp_path / "watched.txt"
    target.write_text("hello", encoding="utf-8")
    stats = FileStatResource()

    @query(key=f"filestat-round-trip-child-{mode}")
    def observed(db: Database) -> Any:
        return stats.read(db, str(target))

    @query(key=f"filestat-round-trip-parent-{mode}")
    def described(db: Database) -> Any:
        snapshot = observed(db)
        return (type(snapshot) is FileStatSnapshot, snapshot.exists, snapshot.size)

    store = InMemoryArtifactStore()
    saver = Database(mode, store=store)
    fresh = saver.get(described)
    assert fresh == (True, True, 5)
    ck_key = saver.save_checkpoint()

    warm_db = Database(mode, store=store)
    warm_db.load_checkpoint(ck_key)
    before = warm_db.statistics()
    warm = warm_db.get(described)
    after = warm_db.statistics()

    assert warm == fresh
    executions_during_warm = after.query_executions - before.query_executions
    assert executions_during_warm == 0
    assert after.query_reuses - before.query_reuses >= 1
    # The reading keeps its declared type on the warm side, as well as inside
    # the query that computed the tuple above.
    assert type(warm_db.get(observed)) is FileStatSnapshot


def test_saved_manifest_records_the_database_mode() -> None:
    """The save side records the mode that the load side must match."""

    @query(key="manifest-records-mode")
    def moded(db: Database) -> int:
        return 11

    store = InMemoryArtifactStore()
    db = Database("checked", store=store)
    assert db.get(moded) == 11
    checkpoint = db.save_checkpoint()

    manifest = json.loads(cast(bytes, store.get(checkpoint)).decode("utf-8"))
    assert manifest["mode"] == "checked"
    assert set(manifest) == {
        "pyinc_ckpt_version",
        "kernel_fingerprint_version",
        "mode",
        "adapters",
        "records",
    }


# ---------------------------------------------------------------------------
# Cross-mode loads: a checkpoint warms only a database running the mode that
# saved it.
#
# The value a query computes can depend on its mode, so a persisted record
# belongs to one mode and warms no other. The refusal keys on the mode mismatch
# itself. All six ordered cross-mode pairs are refused, including pairs whose
# answers coincide today. A narrower rule would encode today's coincidence and
# break as soon as it stops holding.
#
# The saving mode is part of the manifest bytes, so the checkpoint key differs
# per mode. A key re-derived under a second mode is absent from the store, so
# that load raises a plain `KeyError` before it reaches the refusal. These
# tests hand the loading database the key the saving database returned. A
# caller who carries a key between runs does the same, and it is the only way
# to test the refusal.
# ---------------------------------------------------------------------------

_MODES = ("strict", "checked", "fast")

_CROSS_MODE_PAIRS = [
    (save_mode, load_mode) for save_mode in _MODES for load_mode in _MODES if save_mode != load_mode
]


@query(key="cross-mode-argument-shape")
def _observed_argument_shape(db: Database, xs: object) -> str:
    """Report the type of the object the database hands this query.

    It takes an argument and is type-sensitive on purpose. Most queries answer
    the same in every mode, so a test built on one would pass with or without
    the refusal. This one persists a different string on each side of the
    strict boundary, so a cross-mode warm shows in the value itself.
    """
    return type(xs).__name__


def _shape_argument() -> list[int]:
    # A fresh list per call, so each database gets its own.
    return [1, 2, 3]


def test_argument_shape_query_answers_differently_per_mode() -> None:
    """The witness query the cross-mode tests use tells modes apart.

    Strict mode rebuilds every boundary value as a snapshot view before a query
    sees it. Checked and fast thaw it back into a plain `list`. Pinning that
    here keeps the refusal tests below meaningful.
    """
    answers = {
        mode: Database(mode=mode).get(_observed_argument_shape, _shape_argument())
        for mode in _MODES
    }
    assert answers == {"strict": "FrozenList", "checked": "list", "fast": "list"}


@pytest.mark.parametrize(("save_mode", "load_mode"), _CROSS_MODE_PAIRS)
def test_cross_mode_checkpoint_load_refuses_loudly(save_mode: str, load_mode: str) -> None:
    store = InMemoryArtifactStore()
    saver = Database(mode=save_mode, store=store)
    persisted = saver.get(_observed_argument_shape, _shape_argument())
    checkpoint = saver.save_checkpoint()

    loader = Database(mode=load_mode, store=store)
    # Internal: the staging a load commits onto the database, consulted by every
    # later get. Compared by identity so an equal rebind still shows.
    staging_before = loader._checkpoint_query_records
    with pytest.raises(CheckpointModeError, match="refusing to load"):
        loader.load_checkpoint(checkpoint)

    # The refusal lands before the commit. The loader keeps the empty staging it
    # was constructed with and never adopts the checkpoint's store.
    assert loader._checkpoint_query_records is staging_before
    assert loader._checkpoint_query_records == {}
    assert loader._checkpoint_load_store is None

    # The next request computes its own answer and ignores what the other mode
    # persisted.
    fresh = Database(mode=load_mode).get(_observed_argument_shape, _shape_argument())
    executions_before = loader.statistics().query_executions
    answer = loader.get(_observed_argument_shape, _shape_argument())
    assert loader.statistics().query_executions - executions_before == 1
    assert answer == fresh
    # It matches the refused checkpoint's value only where the two modes agree
    # anyway. For the four pairs that straddle the strict boundary, it differs
    # from the string the saver persisted.
    assert (answer == persisted) is (fresh == persisted)


def test_cross_mode_refusal_is_catchable_as_the_checkpoint_family() -> None:
    """The refusal a real cross-mode load raises is catchable by family.

    The class's bases are pinned separately. This pins the object
    `load_checkpoint` raises, which a caller writing `except CheckpointError`
    or `except ValueError` around a load depends on.
    """
    store = InMemoryArtifactStore()
    saver = Database(mode="strict", store=store)
    assert saver.get(_observed_argument_shape, _shape_argument()) == "FrozenList"
    checkpoint = saver.save_checkpoint()

    loader = Database(mode="fast", store=store)
    with pytest.raises(CheckpointError, match="refusing to load"):
        loader.load_checkpoint(checkpoint)
    # The first attempt staged nothing, so the second sees the same database
    # and refuses on the same terms.
    with pytest.raises(ValueError, match="refusing to load"):
        loader.load_checkpoint(checkpoint)
    assert loader._checkpoint_query_records == {}


@pytest.mark.parametrize("mode", _MODES)
def test_same_mode_checkpoint_load_warms_and_matches_fresh(mode: str) -> None:
    """The diagonal keeps warming, measured with the query that exposes the defect."""
    store = InMemoryArtifactStore()
    saver = Database(mode=mode, store=store)
    persisted = saver.get(_observed_argument_shape, _shape_argument())
    checkpoint = saver.save_checkpoint()

    loader = Database(mode=mode, store=store)
    loader.load_checkpoint(checkpoint)
    before = loader.statistics()
    warm = loader.get(_observed_argument_shape, _shape_argument())
    after = loader.statistics()

    fresh = Database(mode=mode).get(_observed_argument_shape, _shape_argument())
    assert warm == fresh == persisted
    # Witnesses: the warm request executed nothing and reused a record, so only
    # a load that warmed a record satisfies the equality above.
    assert after.query_executions - before.query_executions == 0
    assert after.query_reuses - before.query_reuses >= 1


class _CheckpointConsts:
    SCALE = 2


def test_captured_class_attribute_change_rekeys_the_saved_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @query(key="checkpoint-class-attr")
    def scaled(db: Database) -> int:
        return _CheckpointConsts.SCALE + 0

    store = InMemoryArtifactStore()
    saver = Database(store=store)
    assert saver.get(scaled) == 2
    monkeypatch.setattr(_CheckpointConsts, "SCALE", 3)
    executions = saver.statistics().query_executions
    assert saver.get(scaled) == 3
    assert saver.statistics().query_executions == executions + 1
    checkpoint = saver.save_checkpoint()

    loaded = Database(store=store)
    loaded.load_checkpoint(checkpoint)
    # A record is filed under the identity its database derived when it ran. A
    # saving database that answered from a fingerprint predating the change
    # would write the earlier identity into the manifest, and this load would
    # miss it. Reuse is the witness that the saved identity matches what a
    # loading database derives from the same live class.
    assert loaded.get(scaled) == Database().get(scaled) == 3
    assert loaded.inspect(scaled).last_recompute == "reused"


def test_record_saved_after_a_captured_class_change_is_not_warmed_into_the_old_world(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limit = Input[int]("checkpoint_class_attr_limit")

    @query(key="checkpoint-class-attr-window")
    def scaled(db: Database) -> int:
        return limit.read(db) * _CheckpointConsts.SCALE

    store = InMemoryArtifactStore()
    saver = Database(store=store)
    saver.set(limit, 10)
    assert saver.get(scaled) == 20

    with monkeypatch.context() as patched:
        patched.setattr(_CheckpointConsts, "SCALE", 3)
        saver.set(limit, 20)
        # The input change forces this execution whatever the captured class
        # holds, so the value saved below is a value of the changed world.
        assert saver.get(scaled) == 60
        checkpoint = saver.save_checkpoint()

    # The class is back to its value before that execution, so the record must
    # be unreachable from here. Only its identity records which capture produced
    # it. A database that never held the changed class must recompute, never
    # warm the value that class produced.
    loaded = Database(store=store)
    loaded.set(limit, 20)
    loaded.load_checkpoint(checkpoint)
    scratch = Database()
    scratch.set(limit, 20)
    assert loaded.get(scaled) == scratch.get(scaled) == 40
    assert loaded.inspect(scaled).last_recompute == "executed"


# ---------------------------------------------------------------------------
# Reuse restoration (D5): execute-to-verify the frontier by re-execution and
# re-establish a resource's live record from its checkpoint probe hint.
#
# Stage 2 kept warming sound but dropped resource-rooted reuse in a fresh
# process: a resource dep with no live record was refused. These tests restore
# high reuse and keep soundness. A live probe verifies an unchanged resource,
# whose snapshot is restored from the store. Re-execution verifies a query dep
# whose subtree the checkpoint cannot warm, with its call snapshot recovered
# from the store. Every degradation path ends in re-execution and a correct
# value, never an error or a stale value.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ProbeStamp:
    """A resource probe shaped as a frozen dataclass.

    A frozen dataclass freezes to a ``FrozenRecord`` but *thaws* to a plain
    ``dict``, so a live-probe-vs-thawed-hint comparison never matches. The hint
    check must compare frozen forms.
    """

    present: bool
    digest: str


@dataclass(frozen=True)
class _StampResource:
    def read(self, db: Database, path: str) -> str:
        return cast(str, db.read_resource(self, path))

    def label(self, path: str) -> str:
        return f"stamp[{path}]"

    def probe(self, path: str) -> _ProbeStamp:
        file_path = Path(path)
        if not file_path.exists():
            return _ProbeStamp(False, "")
        return _ProbeStamp(True, hashlib.sha256(file_path.read_bytes()).hexdigest())

    def load(self, db: Database, path: str) -> str:
        return Path(path).read_text()


def test_unchanged_file_downstream_reuses_after_leaf_verification(
    tmp_path: Path,
) -> None:
    data_file = tmp_path / "leaf.txt"
    data_file.write_text("one two three")
    resource = FileResource()

    @query
    def leaf_words(db: Database) -> int:
        return len(resource.read(db, str(data_file)).split())

    @query
    def downstream(db: Database) -> int:
        return leaf_words(db) * 100

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    assert db1.get(downstream) == 300
    ck_key = db1.save_checkpoint()

    # Fresh-process semantics: a brand-new database with no live records.
    db2 = Database(store=store)
    db2.load_checkpoint(ck_key)
    assert db2.get(downstream) == 300
    # The unchanged file re-establishes the resource-backed leaf's live record
    # from its probe hint, so the downstream query reuses without re-executing.
    assert db2.inspect(downstream).last_recompute == "reused"
    assert db2.inspect(leaf_words).last_recompute == "reused"
    assert db2.statistics().query_executions == 0


def test_changed_file_reexecutes_only_affected_subtree(tmp_path: Path) -> None:
    file_a = tmp_path / "a.txt"
    file_b = tmp_path / "b.txt"
    file_a.write_text("aa")  # 2 chars
    file_b.write_text("bbb")  # 3 chars
    resource = FileResource()

    @query
    def size_a(db: Database) -> int:
        return len(resource.read(db, str(file_a)))

    @query
    def size_b(db: Database) -> int:
        return len(resource.read(db, str(file_b)))

    @query
    def combined(db: Database) -> int:
        return size_a(db) + size_b(db)

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    assert db1.get(combined) == 5
    ck_key = db1.save_checkpoint()

    # Change only subtree A between save and load.
    file_a.write_text("aaaa")  # now 4 chars

    db2 = Database(store=store)
    db2.load_checkpoint(ck_key)
    assert db2.get(combined) == 7  # 4 + 3
    # Subtree A's changed probe fails verification and A re-executes. Subtree B
    # is unchanged and reuses. The root re-executes because A moved.
    assert db2.inspect(size_a).last_recompute == "executed"
    assert db2.inspect(size_b).last_recompute == "reused"
    assert db2.inspect(combined).last_recompute == "executed"


def test_tuple_nested_resource_is_pinned_for_probe_hint_reuse(tmp_path: Path) -> None:
    file_a = tmp_path / "pa.txt"
    file_b = tmp_path / "pb.txt"
    file_a.write_text("aa")  # 2 chars
    file_b.write_text("bbbb")  # 4 chars
    # The resource is captured inside a tuple. Identity encoding and the pinned
    # capture walk both recurse through that immutable shape, so probe-hint
    # restoration can resolve it without executing the parameterized leaves.
    hidden = (FileResource(),)

    @query
    def measure(db: Database, path: str) -> int:
        return len(hidden[0].read(db, path))

    @query
    def measure_total(db: Database) -> int:
        return measure(db, str(file_a)) + measure(db, str(file_b))

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    assert db1.get(measure_total) == 6
    ck_key = db1.save_checkpoint()

    db2 = Database(store=store)
    db2.load_checkpoint(ck_key)
    # Files unchanged: both resource-backed leaves and the downstream total reuse.
    assert db2.get(measure_total) == 6
    assert db2.inspect(measure_total).last_recompute == "reused"
    assert db2.statistics().query_executions == 0


def test_resource_probe_hint_reuses_with_frozen_dataclass_probe(
    tmp_path: Path,
) -> None:
    data_file = tmp_path / "stamped.txt"
    data_file.write_text("hello")
    resource = _StampResource()

    @query
    def read_stamped(db: Database) -> str:
        return resource.read(db, str(data_file)).upper()

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    assert db1.get(read_stamped) == "HELLO"
    ck_key = db1.save_checkpoint()

    db2 = Database(store=store)
    db2.load_checkpoint(ck_key)
    assert db2.get(read_stamped) == "HELLO"
    # The frozen-dataclass probe is unchanged, so the probe hint matches and the
    # resource record is restored from the store. The match holds only because
    # the hint compares frozen forms. A live dataclass probe compared with a
    # thawed dict would miss.
    assert db2.inspect(read_stamped).last_recompute == "reused"
    assert db2.statistics().query_executions == 0
    assert db2.statistics().resource_probe_hits >= 1


def test_missing_args_snapshot_degrades_to_reexecution(tmp_path: Path) -> None:
    data_file = tmp_path / "m.txt"
    data_file.write_text("payload")
    hidden = (FileResource(),)

    @query
    def hidden_leaf(db: Database, path: str) -> str:
        return hidden[0].read(db, path)

    @query
    def wrapper(db: Database) -> str:
        return hidden_leaf(db, str(data_file)).upper()

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    assert db1.get(wrapper) == "PAYLOAD"
    ck_key = db1.save_checkpoint()

    # Drop the persisted call snapshot for hidden_leaf(path). Execute-to-verify
    # then cannot recover the leaf's args from the store, so it degrades to warm
    # refusal and re-execution, never an error or a stale value.
    call_snapshot_digest = fingerprint_snapshot(freeze(((str(data_file),), {})))
    assert call_snapshot_digest in store._items  # the call snapshot was persisted
    del store._items[call_snapshot_digest]

    db2 = Database(store=store)
    db2.load_checkpoint(ck_key)
    assert db2.get(wrapper) == "PAYLOAD"  # correct value, recomputed
    assert db2.inspect(wrapper).last_recompute == "executed"


_twin_input = Input[int]("twin_execute_verify_p")


def _make_twin_child(op: str) -> Query[..., int]:
    # The query-factory twin pattern: two children share a query_id (same
    # module:qualname) but carry different bodies. Their identities diverge on
    # code.
    if op == "mul":

        @query
        def twin_child(db: Database) -> int:
            return _twin_input.read(db) * 2

    else:

        @query
        def twin_child(db: Database) -> int:
            return _twin_input.read(db) + 2

    return twin_child


def _make_twin_root(a_wrong: Query[..., int], z_right: Query[..., int]) -> Query[..., int]:
    # The captured objects are freevars, walked in co_freevars order, which
    # CPython sorts alphabetically. The wrong twin's name sorts first, so the
    # first-wins pinned-capture map binds it under the shared query_id.
    @query
    def twin_root(db: Database) -> int:
        _pin = a_wrong  # captured (walked first) but never called
        return z_right(db) + 100

    return twin_root


def test_twin_query_id_execute_to_verify_refuses_wrong_body() -> None:
    # Two twins share a query_id. The pinned-capture map is keyed by bare
    # query_id (first wins), so the root pins the WRONG twin (mul).
    wrong_child = _make_twin_child("mul")  # p*2
    right_child = _make_twin_child("add")  # p+2, the body the root calls
    assert wrong_child.key == right_child.key
    root = _make_twin_root(wrong_child, right_child)

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    db1.set(_twin_input, 2)
    # At p=2 the twins coincide (mul=4, add=4): the wrong body has a digest that
    # would pass execute-to-verify at the save state.
    assert db1.get(root) == 104  # add: 2+2=4 -> 104
    ck_key = db1.save_checkpoint()

    # Evict the child's result snapshot, which forces the execute-to-verify
    # path.
    child_digest = fingerprint_snapshot(freeze(4))
    assert child_digest in store._items
    del store._items[child_digest]

    db2 = Database(store=store)
    db2.set(_twin_input, 2)
    db2.load_checkpoint(ck_key)
    # Diverge BEFORE the first get, so the poisoned execute-to-verify and the
    # parent's re-execution land in the same request. That is the only window
    # where the checked_in_request short-circuit would serve the wrong twin's
    # value.
    db2.set(_twin_input, 5)
    reloaded = db2.get(root)

    fresh = Database()
    fresh.set(_twin_input, 5)
    fresh_value = fresh.get(root)

    # add at p=5 is 7 -> 107. Before the identity guard, execute-to-verify bound
    # and ran the mul twin (5*2=10 -> 110) and the parent reused it.
    assert fresh_value == 107
    assert reloaded == fresh_value == 107


def test_statistics_reflect_frontier_verification(tmp_path: Path) -> None:
    data_file = tmp_path / "s.txt"
    data_file.write_text("alpha beta")
    resource = FileResource()

    @query
    def stat_leaf(db: Database) -> str:
        return resource.read(db, str(data_file))

    @query
    def stat_top(db: Database) -> str:
        return stat_leaf(db) + "!"

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    assert db1.get(stat_top) == "alpha beta!"
    ck_key = db1.save_checkpoint()

    db2 = Database(store=store)
    db2.load_checkpoint(ck_key)
    assert db2.get(stat_top) == "alpha beta!"

    stats = db2.statistics()
    # A live resource probe that hit its checkpoint hint verified the frontier,
    # with zero query executions.
    assert stats.query_executions == 0
    assert stats.resource_probe_hits >= 1
    assert stats.query_reuses >= 1


@dataclass(frozen=True)
class _RecordSpec:
    """A resource *parameter* shaped as a frozen dataclass.

    A frozen dataclass freezes to a ``FrozenRecord`` but *thaws* to a plain
    ``dict``, because it has no reconstructor. A checkpoint path that thaws the
    stored parameter and hands it back to the resource gives ``load`` a dict. A
    ``load`` that reaches for ``spec.name`` then fails.
    """

    name: str
    version: int


@dataclass(frozen=True)
class _SpecResource:
    """A custom resource keyed by a frozen-dataclass parameter.

    Its ``load`` reads the parameter's *attributes*, the natural shape for a
    hand-written resource. A thawed-to-dict parameter cannot satisfy it.
    """

    def read(self, db: Database, spec: _RecordSpec) -> str:
        return cast(str, db.read_resource(self, spec))

    def label(self, spec: _RecordSpec) -> str:
        return f"spec[{spec.name}]"

    def probe(self, spec: _RecordSpec) -> tuple[str, int]:
        return (spec.name, spec.version)

    def load(self, db: Database, spec: _RecordSpec) -> str:
        return f"{spec.name}:v{spec.version}"


def test_dataclass_parameter_resource_probe_hint_refuses_and_reexecutes() -> None:
    resource = _SpecResource()

    @query
    def read_spec(db: Database) -> str:
        return resource.read(db, _RecordSpec("alpha", 3)).upper()

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    assert db1.get(read_spec) == "ALPHA:V3"
    ck_key = db1.save_checkpoint()

    # Fresh process with no live records, so warming read_spec has to verify its
    # resource dep from the checkpoint. The dataclass parameter thaws to a plain
    # dict, so probe-hint restoration cannot re-drive the resource faithfully. It
    # refuses (resolves nothing, pre-creates no shadow record) and lets read_spec
    # re-execute against the real dataclass parameter.
    db2 = Database(store=store)
    db2.load_checkpoint(ck_key)
    # Returns the freshly recomputed, correct value from a dataclass-parameter
    # load. Before the guard, load(dict) raised on spec.name.
    assert db2.get(read_spec) == "ALPHA:V3"
    # The resource-dep probe hint refused, so read_spec re-executed.
    assert db2.inspect(read_spec).last_recompute == "executed"
    assert db2.statistics().query_executions == 1


@dataclass(frozen=True)
class _Point:
    """A dataclass-valued query result for the round trip below."""

    x: int
    y: int


@pytest.mark.parametrize("mode", _MODES)
def test_dataclass_value_round_trips_a_checkpoint_without_its_class(
    mode: str, tmp_path: Path
) -> None:
    """A checkpointed dataclass comes back as data, in every mode.

    The first request, the reload and a fresh database all leave the class
    unreconstructed. A caller holds the snapshot shape its mode exposes. Strict
    keeps the `FrozenRecord` view. Checked and fast return the owned thawed
    dict.
    """
    point = Input[_Point]("checkpoint_dataclass_point")

    @query
    def point_value(db: Database) -> Any:
        return point.read(db)

    # One store directory throughout. The loader reads back what the saver
    # wrote, at the same path, with no edits in between.
    saver = Database(mode=mode, store=FileSystemArtifactStore(tmp_path))
    saver.set(point, _Point(5, 6))
    saved = saver.get(point_value)
    checkpoint = saver.save_checkpoint()

    loader = Database(mode=mode, store=FileSystemArtifactStore(tmp_path))
    loader.set(point, _Point(5, 6))
    loader.load_checkpoint(checkpoint)
    before = loader.statistics()
    reloaded = loader.get(point_value)
    after = loader.statistics()

    fresh = Database(mode=mode)
    fresh.set(point, _Point(5, 6))
    fresh_value = fresh.get(point_value)

    # Witnesses that the load warmed the record: the request that produced
    # `reloaded` executed no query and reused one.
    assert after.query_executions - before.query_executions == 0
    assert after.query_reuses - before.query_reuses >= 1

    # The value survives the round trip. The class is lost, and no adapter is
    # registered to bring it back.
    assert reloaded == saved
    assert reloaded == fresh_value
    assert not isinstance(reloaded, _Point)

    # What the caller holds is mode-dependent, and neither shape is the class.
    if mode == "strict":
        assert reloaded == FrozenRecord(type_name="_Point", entries=(("x", 5), ("y", 6)))
    else:
        assert type(reloaded) is dict
        assert reloaded == {"x": 5, "y": 6}


# ---------------------------------------------------------------------------
# Adapter registry trust (A5): a ValueAdapter's key (module:qualname of the
# adapted TYPE) is process-independent, but its freeze/thaw *implementation*
# can vary between processes. A checkpoint records, per adapter key, a digest
# of the implementation that produced it. A warmed record whose snapshot uses
# an adapter whose implementation has since changed or gone missing is skipped,
# and the query re-executes. A load therefore never thaws a stale payload under
# a divergent adapter or returns a value a fresh run would not produce.
# ---------------------------------------------------------------------------


class _Temperature:
    """A boundary value with no native snapshot form, so it needs an adapter.

    A plain (non-dataclass) object, so ``freeze`` refuses it outright unless an
    adapter is registered. This makes the "no adapter" failure mode explicit.
    """

    def __init__(self, degrees: float) -> None:
        self.degrees = degrees

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Temperature) and other.degrees == self.degrees

    def __hash__(self) -> int:
        return hash(self.degrees)

    def __repr__(self) -> str:
        return f"_Temperature({self.degrees})"


class _IdentityTempAdapter:
    """Stores a _Temperature by its raw degrees. A faithful round-trip."""

    def freeze(self, value: Any, recurse: Callable[[Any], Any]) -> Any:
        return recurse(value.degrees)

    def thaw(self, snapshot: Any, recurse: Callable[[Any], Any]) -> Any:
        return _Temperature(recurse(snapshot))


class _OffsetTempAdapter:
    """A behaviourally different adapter under the SAME value-type key.

    It stores ``degrees + 1`` and reverses that on thaw, so it round-trips
    correctly on its OWN payloads but returns a wrong value if handed a payload
    frozen by ``_IdentityTempAdapter`` (which stored the raw degrees).
    """

    def freeze(self, value: Any, recurse: Callable[[Any], Any]) -> Any:
        return recurse(value.degrees + 1)

    def thaw(self, snapshot: Any, recurse: Callable[[Any], Any]) -> Any:
        return _Temperature(recurse(snapshot) - 1)


class _ThawOffsetTempAdapter:
    """Keeps argument identity stable while changing its thawed value."""

    def freeze(self, value: Any, recurse: Callable[[Any], Any]) -> Any:
        return recurse(value.degrees)

    def thaw(self, snapshot: Any, recurse: Callable[[Any], Any]) -> Any:
        return _Temperature(recurse(snapshot) + 10)


_MUTABLE_ADAPTER_OFFSETS = {"freeze": 1.0, "thaw": 1.0}


class _MutableCaptureTempAdapter:
    """An operational adapter with unpinnable mutable ambient state."""

    def freeze(self, value: Any, recurse: Callable[[Any], Any]) -> Any:
        return recurse(value.degrees + _MUTABLE_ADAPTER_OFFSETS["freeze"])

    def thaw(self, snapshot: Any, recurse: Callable[[Any], Any]) -> Any:
        return _Temperature(recurse(snapshot) - _MUTABLE_ADAPTER_OFFSETS["thaw"])


@dataclass(frozen=True)
class _ConfiguredTempAdapter:
    offset: float

    def freeze(self, value: Any, recurse: Callable[[Any], Any]) -> Any:
        return recurse(value.degrees + self.offset)

    def thaw(self, snapshot: Any, recurse: Callable[[Any], Any]) -> Any:
        return _Temperature(recurse(snapshot) - self.offset)


def test_checkpoint_with_same_adapter_reuses() -> None:
    temp_in = Input[float]("adapter_same")

    @query
    def read_temp(db: Database) -> _Temperature:
        return _Temperature(temp_in.read(db))

    store = InMemoryArtifactStore()
    db1 = Database("checked", store=store, adapters={_Temperature: _IdentityTempAdapter()})
    db1.set(temp_in, 5.0)
    assert db1.get(read_temp) == _Temperature(5.0)
    ck_key = db1.save_checkpoint()

    # Same adapter implementation under the same key: the recorded adapter digest
    # matches, so the warmed record is trusted and reused without re-executing.
    db2 = Database("checked", store=store, adapters={_Temperature: _IdentityTempAdapter()})
    db2.set(temp_in, 5.0)
    db2.load_checkpoint(ck_key)
    assert db2.get(read_temp) == _Temperature(5.0)
    assert db2.inspect(read_temp).last_recompute == "reused"


def test_checkpoint_rejects_adapter_key_declaration_that_omits_snapshot_usage() -> None:
    @query(key="adapter-declaration-tamper")
    def read_temp(db: Database) -> _Temperature:
        return _Temperature(5.0)

    store = InMemoryArtifactStore()
    writer = Database("checked", store=store, adapters={_Temperature: _IdentityTempAdapter()})
    assert writer.get(read_temp) == _Temperature(5.0)
    checkpoint = writer.save_checkpoint()
    manifest = json.loads(store._items[checkpoint].decode("utf-8"))
    manifest["records"][0]["adapter_keys"] = []
    malformed_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    malformed_key = "ck" + hashlib.sha256(malformed_bytes).hexdigest()
    store.put(malformed_key, malformed_bytes)

    reader = Database("checked", store=store, adapters={_Temperature: _OffsetTempAdapter()})
    with pytest.raises(CheckpointManifestError, match="inconsistent adapter keys"):
        reader.load_checkpoint(malformed_key)


def test_modified_adapter_implementation_skips_warm() -> None:
    temp_in = Input[float]("adapter_modified")

    @query
    def read_temp(db: Database) -> _Temperature:
        return _Temperature(temp_in.read(db))

    store = InMemoryArtifactStore()
    db1 = Database("checked", store=store, adapters={_Temperature: _IdentityTempAdapter()})
    db1.set(temp_in, 5.0)
    assert db1.get(read_temp) == _Temperature(5.0)
    ck_key = db1.save_checkpoint()

    # A fresh process registers a behaviourally different adapter under the same
    # key. Thawing the checkpointed payload under it would yield _Temperature(4.0)
    # (5 - 1), a value the new adapter would never produce from a fresh run.
    db2 = Database("checked", store=store, adapters={_Temperature: _OffsetTempAdapter()})
    db2.set(temp_in, 5.0)
    db2.load_checkpoint(ck_key)

    # What the new adapter yields fresh, with no checkpoint in play, is the
    # ground truth the load must match.
    reference = Database("checked", adapters={_Temperature: _OffsetTempAdapter()})
    reference.set(temp_in, 5.0)
    assert reference.get(read_temp) == _Temperature(5.0)

    # The adapter digest differs now, so the warm is skipped and the query
    # re-executes, never serving the stale, wrongly thawed value.
    assert db2.get(read_temp) == _Temperature(5.0)
    assert db2.inspect(read_temp).last_recompute == "executed"


def test_modified_query_argument_adapter_skips_root_warm() -> None:
    @query(key="adapted-root-argument")
    def read_argument(db: Database, value: _Temperature) -> float:
        return value.degrees

    store = InMemoryArtifactStore()
    writer = Database("checked", store=store, adapters={_Temperature: _IdentityTempAdapter()})
    assert writer.get(read_argument, _Temperature(1.0)) == 1.0
    checkpoint = writer.save_checkpoint()

    loaded = Database("checked", store=store, adapters={_Temperature: _ThawOffsetTempAdapter()})
    loaded.load_checkpoint(checkpoint)

    fresh = Database("checked", adapters={_Temperature: _ThawOffsetTempAdapter()})
    assert fresh.get(read_argument, _Temperature(1.0)) == 11.0
    assert loaded.get(read_argument, _Temperature(1.0)) == 11.0
    assert loaded.inspect(read_argument, _Temperature(1.0)).last_recompute == "executed"


def test_modified_query_argument_adapter_inside_graph_skips_root_warm() -> None:
    @query(key="adapted-root-graph-argument")
    def read_argument(
        db: Database,
        value: _Temperature,
        left: list[int],
        right: list[int],
    ) -> float:
        return value.degrees + int(left is right)

    shared = [1]
    store = InMemoryArtifactStore()
    writer = Database(
        "checked",
        store=store,
        adapters={_Temperature: _IdentityTempAdapter()},
    )
    assert writer.get(read_argument, _Temperature(1.0), shared, shared) == 2.0
    checkpoint = writer.save_checkpoint()

    loaded = Database(
        "checked",
        store=store,
        adapters={_Temperature: _ThawOffsetTempAdapter()},
    )
    loaded.load_checkpoint(checkpoint)
    equivalent = [1]

    assert loaded.get(read_argument, _Temperature(1.0), equivalent, equivalent) == 12.0
    assert (
        loaded.inspect(read_argument, _Temperature(1.0), equivalent, equivalent).last_recompute
        == "executed"
    )


def test_modified_query_argument_adapter_skips_descendant_warm() -> None:
    @query(key="adapted-child-argument")
    def child(db: Database, value: _Temperature) -> float:
        return value.degrees

    @query(key="adapted-child-root")
    def root(db: Database) -> float:
        return child(db, _Temperature(1.0))

    store = InMemoryArtifactStore()
    writer = Database("checked", store=store, adapters={_Temperature: _IdentityTempAdapter()})
    assert writer.get(root) == 1.0
    checkpoint = writer.save_checkpoint()

    loaded = Database("checked", store=store, adapters={_Temperature: _ThawOffsetTempAdapter()})
    loaded.load_checkpoint(checkpoint)
    fresh = Database("checked", adapters={_Temperature: _ThawOffsetTempAdapter()})

    assert fresh.get(root) == 11.0
    assert loaded.get(root) == 11.0
    assert loaded.inspect(root).last_recompute == "executed"


def test_modified_descendant_result_adapter_skips_parent_warm() -> None:
    @query(key="adapted-child-result")
    def child(db: Database) -> _Temperature:
        return _Temperature(1.0)

    @query(key="adapted-result-root")
    def root(db: Database) -> float:
        return child(db).degrees

    store = InMemoryArtifactStore()
    writer = Database("checked", store=store, adapters={_Temperature: _IdentityTempAdapter()})
    assert writer.get(root) == 1.0
    checkpoint = writer.save_checkpoint()

    loaded = Database("checked", store=store, adapters={_Temperature: _ThawOffsetTempAdapter()})
    loaded.load_checkpoint(checkpoint)
    fresh = Database("checked", adapters={_Temperature: _ThawOffsetTempAdapter()})

    assert fresh.get(root) == 11.0
    assert loaded.get(root) == 11.0
    assert loaded.inspect(root).last_recompute == "executed"


def test_modified_input_adapter_skips_dependent_warm() -> None:
    adapted_input = Input[_Temperature]("adapted-input")

    @query(key="adapted-input-reader")
    def read_input(db: Database) -> float:
        return adapted_input.read(db).degrees

    store = InMemoryArtifactStore()
    writer = Database("checked", store=store, adapters={_Temperature: _IdentityTempAdapter()})
    writer.set(adapted_input, _Temperature(1.0))
    assert writer.get(read_input) == 1.0
    checkpoint = writer.save_checkpoint()

    loaded = Database("checked", store=store, adapters={_Temperature: _ThawOffsetTempAdapter()})
    loaded.set(adapted_input, _Temperature(1.0))
    loaded.load_checkpoint(checkpoint)
    fresh = Database("checked", adapters={_Temperature: _ThawOffsetTempAdapter()})
    fresh.set(adapted_input, _Temperature(1.0))

    assert fresh.get(read_input) == 11.0
    assert loaded.get(read_input) == 11.0
    assert loaded.inspect(read_input).last_recompute == "executed"


def test_checkpoint_save_rejects_adapter_with_unpinnable_capture() -> None:
    temp_in = Input[float]("adapter_unpinnable_save")

    @query
    def read_temp(db: Database) -> _Temperature:
        return _Temperature(temp_in.read(db))

    db = Database(
        "checked",
        store=InMemoryArtifactStore(),
        adapters={_Temperature: _MutableCaptureTempAdapter()},
    )
    db.set(temp_in, 5.0)
    assert db.get(read_temp) == _Temperature(5.0)

    with pytest.raises(UnsupportedValueError, match="cannot be fingerprinted"):
        db.save_checkpoint()


def test_checkpoint_save_rejects_adapter_with_mixed_slot_state() -> None:
    class MixedSlotAdapter:
        __slots__ = ("offset", "__dict__")

        def __init__(self, offset: float) -> None:
            self.offset = offset

        def freeze(self, value: _Temperature, freeze_value: Any) -> Any:
            return freeze_value(value.degrees + self.offset)

        def thaw(self, snapshot: Any, thaw_value: Any) -> _Temperature:
            return _Temperature(float(thaw_value(snapshot)) - self.offset)

    temp_in = Input[float]("adapter_mixed_slots")

    @query
    def read_temp(db: Database) -> _Temperature:
        return _Temperature(temp_in.read(db))

    db = Database(
        "checked",
        store=InMemoryArtifactStore(),
        adapters={_Temperature: MixedSlotAdapter(1.0)},
    )
    db.set(temp_in, 5.0)
    assert db.get(read_temp) == _Temperature(5.0)

    with pytest.raises(UnsupportedValueError, match="slot state"):
        db.save_checkpoint()


def test_unpinnable_live_adapter_safely_misses_checkpoint() -> None:
    temp_in = Input[float]("adapter_unpinnable_warm")

    @query
    def read_temp(db: Database) -> _Temperature:
        return _Temperature(temp_in.read(db))

    store = InMemoryArtifactStore()
    saved = Database("checked", store=store, adapters={_Temperature: _IdentityTempAdapter()})
    saved.set(temp_in, 5.0)
    assert saved.get(read_temp) == _Temperature(5.0)
    checkpoint = saved.save_checkpoint()

    loaded = Database(
        "checked",
        store=store,
        adapters={_Temperature: _MutableCaptureTempAdapter()},
    )
    loaded.set(temp_in, 5.0)
    loaded.load_checkpoint(checkpoint)

    assert loaded.get(read_temp) == _Temperature(5.0)
    assert loaded.inspect(read_temp).last_recompute == "executed"


def test_changed_adapter_instance_configuration_skips_warm() -> None:
    temp_in = Input[float]("adapter_configuration")

    @query
    def read_temp(db: Database) -> _Temperature:
        return _Temperature(temp_in.read(db))

    store = InMemoryArtifactStore()
    saved = Database("checked", store=store, adapters={_Temperature: _ConfiguredTempAdapter(1.0)})
    saved.set(temp_in, 5.0)
    assert saved.get(read_temp) == _Temperature(5.0)
    checkpoint = saved.save_checkpoint()

    loaded = Database("checked", store=store, adapters={_Temperature: _ConfiguredTempAdapter(2.0)})
    loaded.set(temp_in, 5.0)
    loaded.load_checkpoint(checkpoint)

    assert loaded.get(read_temp) == _Temperature(5.0)
    assert loaded.inspect(read_temp).last_recompute == "executed"


def test_mutated_adapter_database_raises_while_a_reloaded_database_reexecutes() -> None:
    temp_in = Input[float]("adapter_mutation_workflow")

    @query
    def read_temp(db: Database) -> _Temperature:
        return _Temperature(temp_in.read(db))

    store = InMemoryArtifactStore()
    adapter = _ConfiguredTempAdapter(1.0)
    saver = Database("checked", store=store, adapters={_Temperature: adapter})
    saver.set(temp_in, 5.0)
    assert saver.get(read_temp) == _Temperature(5.0)
    checkpoint = saver.save_checkpoint()

    # Violating the immutability law on the SAVING database raises loudly.
    object.__setattr__(adapter, "offset", 2.0)
    with pytest.raises(AdapterContractError):
        saver.get(read_temp)

    # A database built with the new configuration from the start refuses the
    # warm record and re-executes. This is the load-side half the sibling test
    # pins.
    loaded = Database("checked", store=store, adapters={_Temperature: _ConfiguredTempAdapter(2.0)})
    loaded.set(temp_in, 5.0)
    loaded.load_checkpoint(checkpoint)
    assert loaded.get(read_temp) == _Temperature(5.0)
    assert loaded.inspect(read_temp).last_recompute == "executed"


def test_the_builtin_file_stat_adapter_checkpoints_cleanly(tmp_path: Path) -> None:
    target = tmp_path / "watched.txt"
    target.write_text("hello", encoding="utf-8")
    stat_resource = FileStatResource()

    @query
    def watched_size(db: Database) -> Any:
        return stat_resource.read(db, target).size

    store = InMemoryArtifactStore()
    db = Database(
        "checked",
        store=store,
        adapters={FileStatSnapshot: BUILTIN_ADAPTERS[FileStatSnapshot]},
    )
    assert db.get(watched_size) == 5

    # A stateless adapter defined in a real module fingerprints cleanly, so the
    # save succeeds and the manifest carries the adapter key the warm gate
    # compares against. An adapter with slot state or an unpinnable capture
    # raises here, as the sibling tests above pin.
    key = db.save_checkpoint()
    manifest = json.loads(cast(bytes, store.get(key)).decode("utf-8"))
    assert list(manifest["adapters"]) == ["pyinc.resources:FileStatSnapshot"]

    loader = Database(
        "checked",
        store=store,
        adapters={FileStatSnapshot: BUILTIN_ADAPTERS[FileStatSnapshot]},
    )
    loader.load_checkpoint(key)
    before = loader.statistics()
    warm = loader.get(watched_size)
    after = loader.statistics()

    assert warm == 5
    assert after.query_executions - before.query_executions == 0
    assert after.query_reuses - before.query_reuses >= 1


def test_missing_adapter_errors_match_fresh_database() -> None:
    temp_in = Input[float]("adapter_missing")

    @query
    def read_temp(db: Database) -> _Temperature:
        return _Temperature(temp_in.read(db))

    # A fresh database with NO adapter cannot freeze the returned _Temperature,
    # so the query raises when it executes. This is the reference failure mode.
    fresh = Database("checked")
    fresh.set(temp_in, 5.0)
    with pytest.raises(UnsupportedValueError) as fresh_exc:
        fresh.get(read_temp)

    store = InMemoryArtifactStore()
    db1 = Database("checked", store=store, adapters={_Temperature: _IdentityTempAdapter()})
    db1.set(temp_in, 5.0)
    assert db1.get(read_temp) == _Temperature(5.0)
    ck_key = db1.save_checkpoint()

    # Loading into a process without the adapter fails the same way. The warm
    # is skipped because this registry lacks the required adapter key, and the
    # re-execution raises the same exception, never a wrongly thawed value.
    db2 = Database("checked", store=store)
    db2.set(temp_in, 5.0)
    db2.load_checkpoint(ck_key)
    with pytest.raises(UnsupportedValueError) as loaded_exc:
        db2.get(read_temp)

    assert type(loaded_exc.value) is type(fresh_exc.value)


@dataclass(frozen=True)
class _StableProbeTempResource:
    """A resource with a stable scalar PROBE and an adapter-wrapped LOAD result.

    With a stable probe, a fresh process hits the probe-hint fast path in
    ``_refresh_resource`` and restores the RESULT snapshot straight from the
    store. That snapshot is an adapter payload. Without a gate on the restore, a
    since-changed adapter would thaw it into a value a fresh run never produces.
    """

    def read(self, db: Database, degrees: float) -> _Temperature:
        return cast(_Temperature, db.read_resource(self, degrees))

    def label(self, degrees: float) -> str:
        return f"temp[{degrees}]"

    def probe(self, degrees: float) -> str:
        # A version string that stays the same across processes, so the probe
        # hint always matches and the result snapshot is restored directly.
        return "v1"

    def load(self, db: Database, degrees: float) -> _Temperature:
        return _Temperature(degrees)


def test_adapter_change_with_stable_probe_reloads_resource_result() -> None:
    resource = _StableProbeTempResource()

    @query
    def read_temp_resource(db: Database) -> _Temperature:
        return resource.read(db, 5.0)

    store = InMemoryArtifactStore()
    db1 = Database("checked", store=store, adapters={_Temperature: _IdentityTempAdapter()})
    assert db1.get(read_temp_resource) == _Temperature(5.0)
    ck_key = db1.save_checkpoint()

    # Fresh process, behaviourally different adapter under the same key. The v1
    # payload stored the raw degrees (5.0). Thawing it under the offset adapter
    # would yield _Temperature(4.0), a value the new adapter never produces.
    db2 = Database("checked", store=store, adapters={_Temperature: _OffsetTempAdapter()})
    db2.load_checkpoint(ck_key)

    # Ground truth: the offset adapter, fresh with no checkpoint, round-trips 5.0.
    reference = Database("checked", adapters={_Temperature: _OffsetTempAdapter()})
    assert reference.get(read_temp_resource) == _Temperature(5.0)

    # The probe is stable so the hint matches, but the adapter digest moved. The
    # ungated fast path would restore the stale snapshot and thaw it to
    # _Temperature(4.0). The gate refuses it, so the resource re-loads under the
    # live adapter and yields the fresh value.
    assert db2.get(read_temp_resource) == _Temperature(5.0)
    # The resource re-loaded through the full load path, bypassing the
    # probe-hint fast path.
    assert db2.statistics().resource_loads == 1
    assert db2.statistics().resource_probe_hits == 0
    assert db2.inspect(read_temp_resource).last_recompute == "executed"


def test_modified_resource_result_adapter_skips_parent_warm() -> None:
    resource = _StableProbeTempResource()

    @query(key="adapted-resource-parent")
    def read_temp_degrees(db: Database) -> float:
        return resource.read(db, 1.0).degrees

    store = InMemoryArtifactStore()
    writer = Database("checked", store=store, adapters={_Temperature: _IdentityTempAdapter()})
    assert writer.get(read_temp_degrees) == 1.0
    checkpoint = writer.save_checkpoint()

    loaded = Database("checked", store=store, adapters={_Temperature: _ThawOffsetTempAdapter()})
    loaded.load_checkpoint(checkpoint)
    fresh = Database("checked", adapters={_Temperature: _ThawOffsetTempAdapter()})

    assert fresh.get(read_temp_degrees) == 11.0
    assert loaded.get(read_temp_degrees) == 11.0
    assert loaded.inspect(read_temp_degrees).last_recompute == "executed"


# ---------------------------------------------------------------------------
# FileSystemArtifactStore durability.
#
# The disk-backed store writes objects atomically (tempfile in the target dir
# plus os.replace) under a two-level fan-out. These tests pin the durability
# guarantees a fresh process depends on. A checkpoint round-trips through a
# brand-new store instance on the same directory. Temp-file debris from a
# crashed writer is never content-addressed. A torn write is never served as a
# value: a read either misses or fails the A1 integrity check.
# ---------------------------------------------------------------------------


def test_checkpoint_round_trip_across_store_instances_and_processes(
    tmp_path: Path,
) -> None:
    p = Input[int]("dur_round_trip")

    @query
    def dur_round_trip_query(db: Database) -> int:
        return p.read(db) * 3

    saver_store = FileSystemArtifactStore(tmp_path)
    db1 = Database(store=saver_store)
    db1.set(p, 7)
    assert db1.get(dur_round_trip_query) == 21
    ck_key = db1.save_checkpoint()

    # A brand-new store instance over the same directory shares only the bytes
    # on disk with the saver.
    loader_store = FileSystemArtifactStore(tmp_path)
    db2 = Database(store=loader_store)
    db2.set(p, 7)
    db2.load_checkpoint(ck_key)
    assert db2.get(dur_round_trip_query) == 21
    assert db2.inspect(dur_round_trip_query).last_recompute == "reused"


def test_leftover_tmp_files_are_ignored(tmp_path: Path) -> None:
    p = Input[int]("dur_tmp_junk")

    @query
    def dur_tmp_junk_query(db: Database) -> int:
        return p.read(db) + 5

    store = FileSystemArtifactStore(tmp_path)
    db1 = Database(store=store)
    db1.set(p, 10)
    assert db1.get(dur_tmp_junk_query) == 15
    ck_key = db1.save_checkpoint()

    # Seed the store with crashed-writer debris: leftover ".tmp-" files (the
    # prefix FileSystemArtifactStore hands to tempfile.mkstemp) at the objects
    # root and inside each fan-out directory. Content addresses never use a
    # ".tmp-" name, so get() can never resolve one.
    objects = tmp_path / "objects"
    (objects / ".tmp-root-junk").write_bytes(b"not an object")
    for fanout in objects.iterdir():
        if fanout.is_dir():
            (fanout / ".tmp-partial").write_bytes(b"half written")

    loader = FileSystemArtifactStore(tmp_path)
    db2 = Database(store=loader)
    db2.set(p, 10)
    db2.load_checkpoint(ck_key)
    assert db2.get(dur_tmp_junk_query) == 15
    assert db2.inspect(dur_tmp_junk_query).last_recompute == "reused"


def test_partial_object_write_never_visible(tmp_path: Path) -> None:
    p = Input[int]("dur_partial")

    @query
    def dur_partial_query(db: Database) -> int:
        return p.read(db) + 100

    store = FileSystemArtifactStore(tmp_path)
    db1 = Database(store=store)
    db1.set(p, 0)
    assert db1.get(dur_partial_query) == 100
    ck_key = db1.save_checkpoint()

    # The value snapshot is content-addressed by fingerprint_snapshot(freeze(100)).
    digest = fingerprint_snapshot(freeze(100))
    object_path = store._path_for(digest)
    assert object_path.exists()
    full_payload = object_path.read_bytes()

    # (a) A crashed atomic write leaves a ".tmp-" file holding a partial payload
    #     that never reached the final name via os.replace. It stays invisible:
    #     the real object still resolves and the debris never shadows it.
    partial = full_payload[: len(full_payload) // 2]
    (object_path.parent / ".tmp-crash").write_bytes(partial)
    assert store.get(digest) == full_payload

    # (b) A torn write that somehow landed under the FINAL name yields truncated
    #     bytes. The load-time content-address check (A1) refuses them, so the
    #     query re-executes to a correct value. Partial bytes are never served.
    object_path.write_bytes(full_payload[:-1])

    #     The loader reads through this store and never writes back to it.
    #     This test pins that torn bytes are refused and the query re-executes.
    #     Publishing the recomputed value over the torn bytes raises the
    #     store's collision error, which has its own test.
    loader = FileSystemArtifactStore(tmp_path)
    db2 = Database()
    db2.set(p, 0)
    db2.load_checkpoint(ck_key, store=loader)
    assert db2.get(dur_partial_query) == 100
    assert db2.inspect(dur_partial_query).last_recompute == "executed"


# ---------------------------------------------------------------------------
# Code-identity determinism.
#
# A code object's identity is independent of ambient runtime refcounts. The
# canonical typed encoder reads semantic code fields directly, so retaining a
# string constant elsewhere in the process cannot move the identity.
# ---------------------------------------------------------------------------


def _refcount_guard_helper(text: str) -> bool:
    # The regex-pattern literal is a const of THIS code object. Passing it by
    # identity to re.fullmatch makes re._cache retain that object on first use,
    # moving its refcount from 1 to 2. The identity must ignore that move.
    return re.fullmatch(r"\d+ refcount_guard \w+", text) is not None


def test_code_fingerprint_ignores_ambient_refcount_changes() -> None:
    @query
    def refcount_guard_query(db: Database) -> bool:
        return _refcount_guard_helper("7 refcount_guard ok")

    db = Database()
    # Drop any cached compilation so the captured helper's pattern const starts at
    # refcount 1 (held only by its co_consts) for the first keying.
    re.purge()
    identity_before = db._query_key(refcount_guard_query, (), {})[0].identity

    # Perturb ambient refcounts the way first regex use does: re._cache retains
    # the helper's pattern const, so its refcount crosses 1 -> 2.
    assert _refcount_guard_helper("7 refcount_guard ok") is True
    identity_after = db._query_key(refcount_guard_query, (), {})[0].identity

    # The typed code encoding is invariant to the refcount shift. The test pins
    # the externally observable identity and leaves the encoding free to change.
    assert identity_before == identity_after


# ---------------------------------------------------------------------------
# Dirty-graph save: a checkpoint persists only records whose cached value
# matches what a fresh recomputation against the *current* graph would produce.
# If an input changes after a query computed and before save_checkpoint, with
# no get in between (a "dirty graph"), that query's record is stale and stays
# cold on reload. Save omits it, and the query re-executes to the correct value.
# ---------------------------------------------------------------------------


def test_dirty_graph_save_reloads_fresh_not_stale() -> None:
    b = Input[int]("dirty_reload_bias")

    @query
    def dirty_reload_q(db: Database) -> int:
        return b.read(db)

    store = InMemoryArtifactStore()
    saver = Database(store=store)
    saver.set(b, 0)
    assert saver.get(dirty_reload_q) == 0  # compute against b == 0
    saver.set(b, 1)  # mutate with NO intervening get -> dirty graph
    ck_key = saver.save_checkpoint()  # dirty save

    reloaded = Database(store=store)
    reloaded.set(b, 1)
    reloaded.load_checkpoint(ck_key)

    fresh = Database()
    fresh.set(b, 1)

    # Reload serves what a from-scratch database computes. Before the fix, the
    # stale record baked at save warmed and served 0.
    assert fresh.get(dirty_reload_q) == 1
    assert reloaded.get(dirty_reload_q) == 1
    assert reloaded.inspect(dirty_reload_q).last_recompute == "executed"


def test_dirty_save_does_not_degrade_settled_records() -> None:
    settled_in = Input[int]("dirty_mix_settled")
    dirty_in = Input[int]("dirty_mix_dirty")

    @query
    def settled_q(db: Database) -> int:
        return settled_in.read(db) + 100

    @query
    def dirty_q(db: Database) -> int:
        return dirty_in.read(db) + 200

    store = InMemoryArtifactStore()
    saver = Database(store=store)
    saver.set(settled_in, 10)
    saver.set(dirty_in, 20)
    assert saver.get(settled_q) == 110
    assert saver.get(dirty_q) == 220
    # Dirty only the dirty_q subtree; settled_q's subtree is untouched.
    saver.set(dirty_in, 99)
    ck_key = saver.save_checkpoint()

    reloaded = Database(store=store)
    reloaded.set(settled_in, 10)
    reloaded.set(dirty_in, 99)
    reloaded.load_checkpoint(ck_key)

    # The settled sibling still warms from the checkpoint. The save omits only
    # the stale record and keeps every record that is still sound.
    assert reloaded.get(settled_q) == 110
    assert reloaded.inspect(settled_q).last_recompute == "reused"

    # The dirtied subtree re-executes to the fresh value, never the stale 220.
    fresh = Database()
    fresh.set(dirty_in, 99)
    assert reloaded.get(dirty_q) == fresh.get(dirty_q) == 299
    assert reloaded.inspect(dirty_q).last_recompute == "executed"


def test_settled_save_reuse_unchanged() -> None:
    b = Input[int]("settled_reuse_bias")

    @query
    def settled_reuse_q(db: Database) -> int:
        return b.read(db) + 7

    store = InMemoryArtifactStore()
    saver = Database(store=store)
    saver.set(b, 5)
    assert saver.get(settled_reuse_q) == 12  # settle before save
    ck_key = saver.save_checkpoint()

    reloaded = Database(store=store)
    reloaded.set(b, 5)
    reloaded.load_checkpoint(ck_key)

    # A settled save still warms on reload. This guards against over-omission.
    assert reloaded.get(settled_reuse_q) == 12
    assert reloaded.inspect(settled_reuse_q).last_recompute == "reused"


# ---------------------------------------------------------------------------
# Query handle state: a body may read attributes off its own handle, and
# writing one is a supported way to reparameterize the query. Identity moves
# with the write. A checkpoint saved before the write then misses, and the body
# re-executes against the current state. An untouched handle still warms the
# same records.
# ---------------------------------------------------------------------------


def test_query_handle_attribute_change_invalidates_checkpointed_records() -> None:
    @query(key="checkpoint-handle-attr")
    def selfread(db: Database) -> int:
        return int(cast(Any, selfread).threshold)

    store = InMemoryArtifactStore()
    cast(Any, selfread).threshold = 1
    saver = Database(store=store)
    assert saver.get(selfread) == 1
    saved_identity = saver._query_key(selfread, (), {})[0].identity
    checkpoint = saver.save_checkpoint()

    cast(Any, selfread).threshold = 2
    loaded = Database(store=store)
    loaded.load_checkpoint(checkpoint)
    # The write moves the node the stored record is keyed by, so the record is
    # unreachable. The value below therefore cannot be the saved 1 dressed up
    # as a recomputation.
    assert loaded._query_key(selfread, (), {})[0].identity != saved_identity

    value = loaded.get(selfread)
    assert value == Database().get(selfread) == 2
    assert loaded.inspect(selfread).last_recompute == "executed"


def test_query_handle_attribute_the_body_never_reads_invalidates_records() -> None:
    @query(key="checkpoint-handle-attr-unread")
    def stamped(db: Database) -> int:
        return 42

    store = InMemoryArtifactStore()
    saver = Database(store=store)
    assert saver.get(stamped) == 42
    saved_identity = saver._query_key(stamped, (), {})[0].identity
    checkpoint = saver.save_checkpoint()

    # This body reads nothing off its handle, so the write below cannot reach
    # the query through the capture the test above relies on. Here the handle
    # fold moves identity. It covers the whole handle, including parts no body
    # reads, and the moved identity makes the stored record unreachable. The
    # write cannot change the answer, so the assertions below check the
    # identity and the execution counter. For a query shaped like this one,
    # those are what show a miss.
    cast(Any, stamped).threshold = 2
    loaded = Database(store=store)
    loaded.load_checkpoint(checkpoint)
    assert loaded._query_key(stamped, (), {})[0].identity != saved_identity
    assert loaded.get(stamped) == 42
    assert loaded.inspect(stamped).last_recompute == "executed"
    assert loaded.statistics().query_executions == 1


def test_unchanged_query_handle_attribute_still_warms_from_a_checkpoint() -> None:
    @query(key="checkpoint-handle-attr-stable")
    def selfread(db: Database) -> int:
        return int(cast(Any, selfread).threshold)

    store = InMemoryArtifactStore()
    cast(Any, selfread).threshold = 3
    saver = Database(store=store)
    assert saver.get(selfread) == 3
    saved_identity = saver._query_key(selfread, (), {})[0].identity
    checkpoint = saver.save_checkpoint()

    loaded = Database(store=store)
    loaded.load_checkpoint(checkpoint)
    # Control for the misses above: queries that carry handle state keep their
    # checkpoints. An untouched handle keys the same node, and the stored record
    # answers without running the body. So the misses come from the write, and
    # this query shape still warms.
    assert loaded._query_key(selfread, (), {})[0].identity == saved_identity
    assert loaded.get(selfread) == 3
    assert loaded.inspect(selfread).last_recompute == "reused"
    assert loaded.statistics().query_executions == 0


def test_reverting_a_query_handle_attribute_restores_its_records() -> None:
    @query(key="checkpoint-handle-attr-revert")
    def selfread(db: Database) -> int:
        return int(cast(Any, selfread).threshold)

    store = InMemoryArtifactStore()
    cast(Any, selfread).threshold = 1
    saver = Database(store=store)
    assert saver.get(selfread) == 1
    saved_identity = saver._query_key(selfread, (), {})[0].identity
    checkpoint = saver.save_checkpoint()

    cast(Any, selfread).threshold = 2
    rebound = Database(store=store)
    rebound.load_checkpoint(checkpoint)
    assert rebound._query_key(selfread, (), {})[0].identity != saved_identity
    assert rebound.get(selfread) == 2
    assert rebound.inspect(selfread).last_recompute == "executed"

    cast(Any, selfread).threshold = 1
    reverted = Database(store=store)
    reverted.load_checkpoint(checkpoint)
    # The write above left the saved record unaddressed but intact. Writing the
    # attribute back rebuilds the same identity byte for byte, so the
    # checkpoint's record is reachable again and answers without running the
    # body. A handle attribute therefore reparameterizes a query reversibly,
    # and everything stored under it survives.
    assert reverted._query_key(selfread, (), {})[0].identity == saved_identity
    assert reverted.get(selfread) == 1
    assert reverted.inspect(selfread).last_recompute == "reused"
    assert reverted.statistics().query_executions == 0


def test_reflective_queries_stay_rejected_after_a_checkpoint_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_checkpoint_reflective"
    (tmp_path / f"{module_name}.py").write_text(
        'CONFIG_MODE = "A"\n\n\ndef reader():\n    return globals()["CONFIG_MODE"]\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    reader = module.reader

    anchor = Input[int]("reflective-durability-anchor")

    @query(key="checkpoint-reflective")
    def read_config(db: Database) -> str:
        return cast(str, reader())

    store = InMemoryArtifactStore()
    saver = Database(store=store)
    saver.set(anchor, 1)
    # A refused query never reaches a stored record. The durable claim is that
    # loading a checkpoint keeps the refusal in force on the far side.
    with pytest.raises(UnsupportedValueError):
        saver.get(read_config)
    checkpoint = saver.save_checkpoint()

    loaded = Database(store=store)
    loaded.set(anchor, 1)
    loaded.load_checkpoint(checkpoint)
    with pytest.raises(UnsupportedValueError):
        loaded.get(read_config)


# ---------------------------------------------------------------------------
# The checkpoint error taxonomy. Each cause a load can fail for has its own
# class, so a caller can catch one cause on its own. The classes nest where the
# causes do: catching CheckpointManifestError also catches its subclass
# CheckpointIntegrityError. A mode mismatch is its own cause. Like its
# siblings, it is catchable as a checkpoint failure, as a pyinc error, and as a
# ValueError.
# ---------------------------------------------------------------------------


def test_checkpoint_mode_error_is_public_and_catchable() -> None:
    assert issubclass(CheckpointModeError, CheckpointError)
    assert issubclass(CheckpointModeError, PyIncError)
    assert issubclass(CheckpointModeError, ValueError)
    assert "CheckpointModeError" in pyinc.__all__
    with pytest.raises(CheckpointError):
        raise CheckpointModeError("mode mismatch")


# ---------------------------------------------------------------------------
# Input keys and the durability of what they write. A key that renders one way
# as node identity and another way as a node label writes a checkpoint whose
# input dependency labels fail the load-side invariant. The save would report
# success and every load of it would fail. The key boundary refuses such a key
# outright, and the caller writes the plain-string spelling it names.
# ---------------------------------------------------------------------------


class _CheckpointKey(str, Enum):  # noqa: UP042 - the pre-StrEnum mixin idiom is under test
    SEED = "checkpoint_enum_seed"


def test_enum_keys_cannot_write_checkpoints_and_value_spelling_round_trips() -> None:
    """The unloadable-checkpoint shape is unconstructible, and `.value` warms.

    A `str`-mixin Enum member is stored unchanged as node identity, while the
    node label is formatted from it. The two render differently, so the saved
    manifest carries an input dependency label the loader rejects. A
    successful save used to produce state no database could read back. The key
    is now refused where it is written. The `member.value` spelling the
    refusal names is a plain string and round-trips through a checkpoint.
    """
    with pytest.raises(InputKeyError, match="exactly str") as raised:
        Input[int](cast(Any, _CheckpointKey.SEED))
    assert "key.value" in str(raised.value)

    seed = Input[int](_CheckpointKey.SEED.value)
    assert type(seed.key) is str

    @query
    def enum_value_keyed(db: Database) -> int:
        return seed.read(db) + 1

    store = InMemoryArtifactStore()
    saver = Database(store=store)
    saver.set(seed, 4)
    fresh = saver.get(enum_value_keyed)
    assert fresh == 5
    checkpoint = saver.save_checkpoint()

    loader = Database(store=store)
    loader.set(seed, 4)
    loader.load_checkpoint(checkpoint)
    before = loader.statistics()
    warm = loader.get(enum_value_keyed)
    after = loader.statistics()

    # Witnesses that the load warmed a record: the warm request executed no
    # query and reused at least one record.
    assert warm == fresh
    assert after.query_executions - before.query_executions == 0
    assert after.query_reuses - before.query_reuses >= 1
    assert loader.inspect(enum_value_keyed).last_recompute == "reused"
