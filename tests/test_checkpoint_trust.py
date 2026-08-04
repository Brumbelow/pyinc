"""Store-read integrity for the durable checkpoint path.

The checkpoint API (`Database.save_checkpoint` / `Database.load_checkpoint`)
trusts an `ArtifactStore` to return, for a given content-address, exactly the
bytes that were written under it. These tests simulate a store that breaks
that contract (bit-flipped bytes, truncation, a foreign kernel version, a
tampered manifest) and pin the kernel's response: snapshot-level corruption
is silently skipped and the affected query re-executes; manifest-level
corruption raises a loud `ValueError`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from pyinc import (
    CheckpointManifestError,
    Database,
    FileResource,
    FileSystemArtifactStore,
    InMemoryArtifactStore,
    Input,
    InputKeyError,
    UnsupportedValueError,
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
    # for 11. The bytes still decode cleanly -- only the content address
    # is wrong.
    digest = fingerprint_snapshot(freeze(11))
    store._items[digest] = serialize_snapshot(freeze(99))

    db2 = Database(store=store)
    db2.set(p, 0)
    db2.load_checkpoint(ck_key)
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

    db2 = Database(store=store)
    db2.set(p, 0)
    db2.load_checkpoint(ck_key)
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

    db2 = Database(store=store)
    db2.set(p, 0)
    db2.load_checkpoint(ck_key)
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

    # Trailing whitespace keeps the JSON parseable but changes the bytes,
    # so the recomputed content address no longer matches the requested key.
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
    """Structurally missing required fields must raise ValueError, not KeyError."""
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
# Warm-path soundness: a record restored from a checkpoint must take part in
# normal invalidation. It carries real dependency edges, its revisions are
# normalised onto the loading database's timeline, and its resources are
# re-probed against live state -- so an incremental read after a load still
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

    # Now change the input the query depends on. The warmed record must carry a
    # real edge to that input, so the next get re-executes against the new value
    # instead of serving the stale checkpointed result.
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
    # No live resource record exists in the fresh database, so the warm is
    # refused and the query re-executes, re-probing the (now changed) file.
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

    # A second request against unchanged state must still reuse. Normalising the
    # warmed record's revisions onto this database's timeline must not leave it
    # looking perpetually dirty (which would thrash into a re-execute).
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

    # Change the child's input. The parent has no live child to walk into, so it
    # must transitively re-verify the warmed child record and re-execute.
    db2.set(p, 9)
    assert db2.get(warm_parent) == 100
    assert db2.inspect(warm_parent).last_recompute == "executed"


def test_untracked_records_are_never_warmed() -> None:
    p = Input[int]("warm_untracked")

    @query
    def warm_untracked_query(db: Database) -> int:
        db.report_untracked_read("external_clock")
        return p.read(db) + 1

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    db1.set(p, 7)
    assert db1.get(warm_untracked_query) == 8
    assert db1.inspect(warm_untracked_query).is_untracked
    ck_key = db1.save_checkpoint()

    db2 = Database(store=store)
    db2.set(p, 7)
    db2.load_checkpoint(ck_key)
    # An untracked record is impure by definition; it must never be served from
    # a checkpoint. The warm is refused and the query re-executes.
    assert db2.get(warm_untracked_query) == 8
    assert db2.inspect(warm_untracked_query).last_recompute == "executed"


# ---------------------------------------------------------------------------
# Deterministic, code-complete identities (D1/D2/D3/D7).
#
# A checkpoint identity must be complete over the code it depends on and
# reproducible in a fresh process. If a captured dependency query's body
# changes, the parent's identity must move (transitive code pinning). Identities
# must not embed per-process addresses, must react to build flags, and manifests
# must be versioned strictly.
# ---------------------------------------------------------------------------


def _make_dep_child(value: int) -> Query[..., int]:
    # A query factory: every child shares a query_id (same qualname/module) but
    # carries a different captured body, standing in for a code change to a
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
    # The child's code is pinned into the parent's identity, so the parent no
    # longer matches the checkpointed record and re-executes against the new
    # child body instead of serving the stale value.
    assert db2.get(parent_v2) == 3
    assert db2.inspect(parent_v2).last_recompute == "executed"


def test_query_identity_pins_captured_query_code() -> None:
    parent_a = _make_dep_parent(_make_dep_child(1))
    parent_b = _make_dep_parent(_make_dep_child(2))

    db = Database()
    identity_a = db._query_key(parent_a, (), {})[0].identity
    identity_b = db._query_key(parent_b, (), {})[0].identity

    # Same parent source, same captured-child query_id, different child body:
    # the identities must diverge because the child's code is folded in.
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
    # -O changes captured-module behaviour invisibly to source digests, so the
    # build configuration is part of the identity.
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
    import importlib

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
    # Never cache bytecode: two same-second source rewrites can otherwise share a
    # .pyc and reimport the stale body, masking whether the warm gate fired.
    saved_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        write_child(1)
        purge()

        @query
        def runtime_import_parent(db: Database) -> int:
            # The child is obtained by a runtime import inside the body, so it is
            # never captured and cannot be code-pinned.
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
        # The dep query is not in the parent's pinned capture set, so the warm
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
    """A 3.0.0 manifest records dependency sets this kernel no longer means.

    3.0.0 recorded no dependencies for a reader whose resource read raised a
    caught exception, so warming such a record here would report "dependencies
    unchanged" for a node a fresh database re-derives. The record layout is
    unchanged, which is exactly why the manifest version has to carry the
    difference.
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

    Version-5 records may have been written by a kernel that derived captured-
    module identity from a stat tuple a same-size rewrite can preserve, and
    that dropped the resource edge for a stat probe raising NotADirectoryError.
    Both leave records a warm database would reuse while a fresh one
    re-derives, so the manifest version has to carry the difference.
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


# ---------------------------------------------------------------------------
# Reuse restoration (D5): execute-to-verify the frontier by re-execution and
# re-establish a resource's live record from its checkpoint probe hint.
#
# Stage 2 kept warming sound but dropped resource-rooted reuse in a fresh
# process (a resource dep with no live record was refused). These tests restore
# high reuse without giving back soundness: an unchanged resource is verified by
# a live probe (its snapshot restored from the store), and a query dep whose
# subtree cannot be warmed from the checkpoint is verified by re-execution, its
# call snapshot recovered from the store. Every degradation path lands on
# re-execution (a correct value), never on an error or a stale value.
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
    # Subtree A's changed probe fails verification and re-executes; subtree B is
    # unchanged and reuses; the root re-executes because A moved.
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
    # The frozen-dataclass probe is unchanged, so the probe hint matches (this
    # only holds once the hint compares frozen forms rather than a live probe
    # against a thawed dict) and the resource record is restored from the store.
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

    # Drop the persisted call snapshot for hidden_leaf(path): execute-to-verify
    # can no longer recover the leaf's args from the store and must degrade to
    # warm refusal (re-execution) -- never an error, never a stale value.
    call_snapshot_digest = fingerprint_snapshot(freeze(((str(data_file),), {})))
    assert call_snapshot_digest in store._items  # the call snapshot was persisted
    del store._items[call_snapshot_digest]

    db2 = Database(store=store)
    db2.load_checkpoint(ck_key)
    assert db2.get(wrapper) == "PAYLOAD"  # correct value, recomputed
    assert db2.inspect(wrapper).last_recompute == "executed"


_twin_input = Input[int]("twin_execute_verify_p")


def _make_twin_child(op: str) -> Query[..., int]:
    # Two children share a query_id (same module:qualname) but carry different
    # bodies -- the query-factory twin pattern. Their identities diverge on code.
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
    # The captured objects are freevars, walked in co_freevars order -- which
    # CPython sorts alphabetically. Name the wrong twin so it sorts first, so the
    # first-wins pinned-capture map binds it under the shared query_id.
    @query
    def twin_root(db: Database) -> int:
        _pin = a_wrong  # captured (walked first) but never called
        return z_right(db) + 100

    return twin_root


def test_twin_query_id_execute_to_verify_refuses_wrong_body() -> None:
    # Two twins share a query_id; the pinned-capture map is keyed by bare
    # query_id (first-wins), so the WRONG twin (mul) is the one the root pins.
    wrong_child = _make_twin_child("mul")  # p*2
    right_child = _make_twin_child("add")  # p+2, the body root actually calls
    assert wrong_child.key == right_child.key
    root = _make_twin_root(wrong_child, right_child)

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    db1.set(_twin_input, 2)
    # At p=2 the twins coincide (mul=4, add=4): the wrong body has a digest that
    # would pass execute-to-verify at the save state.
    assert db1.get(root) == 104  # add: 2+2=4 -> 104
    ck_key = db1.save_checkpoint()

    # Evict the child's result snapshot so it cannot warm and must take the
    # execute-to-verify path.
    child_digest = fingerprint_snapshot(freeze(4))
    assert child_digest in store._items
    del store._items[child_digest]

    db2 = Database(store=store)
    db2.set(_twin_input, 2)
    db2.load_checkpoint(ck_key)
    # Diverge BEFORE the first get, so the poisoned execute-to-verify and the
    # parent's re-execution land in the same request -- the only window where the
    # checked_in_request short-circuit would serve the wrong twin's value.
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
    # The frontier was verified by a live resource probe that hit its checkpoint
    # hint, not by re-executing any query.
    assert stats.query_executions == 0
    assert stats.resource_probe_hits >= 1
    assert stats.query_reuses >= 1


@dataclass(frozen=True)
class _RecordSpec:
    """A resource *parameter* shaped as a frozen dataclass.

    A frozen dataclass freezes to a ``FrozenRecord`` but *thaws* to a plain
    ``dict`` (it has no reconstructor). A checkpoint path that thaws the stored
    parameter and hands it back to the resource therefore hands ``load`` a dict,
    not the dataclass -- so a ``load`` that reaches for ``spec.name`` blows up.
    """

    name: str
    version: int


@dataclass(frozen=True)
class _SpecResource:
    """A custom resource keyed by a frozen-dataclass parameter.

    Its ``load`` reads the parameter's *attributes*, the natural shape for a
    hand-written resource. That is exactly what a thawed-to-dict parameter
    cannot satisfy.
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

    # Fresh process: no live records, so warming read_spec has to verify its
    # resource dep from the checkpoint. The dataclass parameter thaws to a plain
    # dict, so the probe-hint restoration cannot faithfully re-drive the resource:
    # it must refuse (resolve nothing, pre-create no shadow record) and let
    # read_spec re-execute against the real dataclass parameter.
    db2 = Database(store=store)
    db2.load_checkpoint(ck_key)
    # Never raises (before the guard, load(dict) explodes on spec.name) and hands
    # back the freshly-recomputed, correct value -- never a dict-parameter load.
    assert db2.get(read_spec) == "ALPHA:V3"
    # The resource-dep probe hint refused, so read_spec re-executed instead of
    # being served from the checkpoint.
    assert db2.inspect(read_spec).last_recompute == "executed"
    assert db2.statistics().query_executions == 1


# ---------------------------------------------------------------------------
# Adapter registry trust (A5): a ValueAdapter's key (module:qualname of the
# adapted TYPE) is process-independent, but its freeze/thaw *implementation* is
# not. A checkpoint records, per adapter key, a digest of the implementation
# that produced it; a warmed record whose snapshot uses an adapter whose
# implementation has since changed (or gone missing) is skipped and the query
# re-executes -- so a load can never thaw a stale payload under a divergent
# adapter and hand back a value a fresh run would not have produced.
# ---------------------------------------------------------------------------


class _Temperature:
    """A boundary value with no native snapshot form; it needs an adapter.

    A plain (non-dataclass) object so ``freeze`` refuses it outright unless an
    adapter is registered -- which makes the "no adapter" failure mode explicit.
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
    """An operational adapter whose mutable ambient state is not pinnable."""

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

    # The adapter digest no longer matches, so the warm is skipped and the query
    # re-executes -- never serving the stale, wrongly-thawed value.
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


def test_missing_adapter_errors_match_fresh_database() -> None:
    temp_in = Input[float]("adapter_missing")

    @query
    def read_temp(db: Database) -> _Temperature:
        return _Temperature(temp_in.read(db))

    # A fresh database with NO adapter cannot freeze the returned _Temperature,
    # so the query raises when it executes -- this is the reference failure mode.
    fresh = Database("checked")
    fresh.set(temp_in, 5.0)
    with pytest.raises(UnsupportedValueError) as fresh_exc:
        fresh.get(read_temp)

    store = InMemoryArtifactStore()
    db1 = Database("checked", store=store, adapters={_Temperature: _IdentityTempAdapter()})
    db1.set(temp_in, 5.0)
    assert db1.get(read_temp) == _Temperature(5.0)
    ck_key = db1.save_checkpoint()

    # Loading into a process without the adapter must fail identically: the warm
    # is skipped (the required adapter key is absent from this registry) and the
    # re-execution raises the same exception -- never a wrongly-thawed value.
    db2 = Database("checked", store=store)
    db2.set(temp_in, 5.0)
    db2.load_checkpoint(ck_key)
    with pytest.raises(UnsupportedValueError) as loaded_exc:
        db2.get(read_temp)

    assert type(loaded_exc.value) is type(fresh_exc.value)


@dataclass(frozen=True)
class _StableProbeTempResource:
    """A resource whose PROBE is a stable scalar but whose LOAD returns an
    adapter-wrapped value.

    The stable probe means a fresh process hits the probe-hint fast path in
    ``_refresh_resource`` and restores the RESULT snapshot straight from the
    store. That snapshot is an adapter payload, so a since-changed adapter would
    thaw it into a value a fresh run never produces unless the restore is gated.
    """

    def read(self, db: Database, degrees: float) -> _Temperature:
        return cast(_Temperature, db.read_resource(self, degrees))

    def label(self, degrees: float) -> str:
        return f"temp[{degrees}]"

    def probe(self, degrees: float) -> str:
        # A stable version string: it never changes across processes, so the
        # probe hint always matches and the result snapshot is restored directly.
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
    # payload stored the raw degrees (5.0); thawing it under the offset adapter
    # would yield _Temperature(4.0) -- a value the new adapter never produces.
    db2 = Database("checked", store=store, adapters={_Temperature: _OffsetTempAdapter()})
    db2.load_checkpoint(ck_key)

    # Ground truth: the offset adapter, fresh with no checkpoint, round-trips 5.0.
    reference = Database("checked", adapters={_Temperature: _OffsetTempAdapter()})
    assert reference.get(read_temp_resource) == _Temperature(5.0)

    # The probe is stable so the hint matches, but the adapter digest moved. The
    # ungated fast path would restore the stale snapshot and thaw it to
    # _Temperature(4.0); the gate must refuse it so the resource re-loads under
    # the live adapter and yields the fresh value.
    assert db2.get(read_temp_resource) == _Temperature(5.0)
    # The resource actually re-loaded (fell through to the full load path) rather
    # than being served from the probe-hint fast path.
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
# guarantees a fresh process depends on: a checkpoint round-trips through a
# brand-new store instance on the same directory; crashed-writer temp-file
# debris is never content-addressed; and a torn write is never served as a
# value (a read either misses or fails the A1 integrity check).
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

    # A brand-new store instance over the same directory shares no in-process
    # state with the saver -- only the bytes on disk.
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
    # root and inside each fan-out directory. Nothing is content-addressed to a
    # ".tmp-" name, so no get() can ever resolve one.
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
    #     that never reached the final name via os.replace. It must stay
    #     invisible: the real object still resolves and the debris never shadows
    #     it.
    partial = full_payload[: len(full_payload) // 2]
    (object_path.parent / ".tmp-crash").write_bytes(partial)
    assert store.get(digest) == full_payload

    # (b) A torn write that somehow landed under the FINAL name yields truncated
    #     bytes. The load-time content-address check (A1) refuses them, so the
    #     query re-executes to a correct value -- partial bytes are never served.
    object_path.write_bytes(full_payload[:-1])

    loader = FileSystemArtifactStore(tmp_path)
    db2 = Database(store=loader)
    db2.set(p, 0)
    db2.load_checkpoint(ck_key)
    assert db2.get(dur_partial_query) == 100
    assert db2.inspect(dur_partial_query).last_recompute == "executed"


# ---------------------------------------------------------------------------
# Code-identity determinism.
#
# A code object's identity must not depend on ambient runtime refcounts. The
# canonical typed encoder reads semantic code fields directly, so retaining a
# string constant elsewhere in the process cannot move the identity.
# ---------------------------------------------------------------------------


def _refcount_guard_helper(text: str) -> bool:
    # The regex-pattern literal is a const of THIS code object; passing it by
    # identity to re.fullmatch makes re._cache retain that exact object on first
    # use, nudging its refcount from 1 to 2. The identity must ignore that move.
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

    # Perturb ambient refcounts exactly as first regex use does: the helper's
    # pattern const is retained by re._cache, so its refcount crosses 1 -> 2.
    assert _refcount_guard_helper("7 refcount_guard ok") is True
    identity_after = db._query_key(refcount_guard_query, (), {})[0].identity

    # The typed code encoding is invariant to the refcount shift. Pin the
    # externally observable identity rather than an encoding implementation.
    assert identity_before == identity_after


# ---------------------------------------------------------------------------
# Dirty-graph save: a checkpoint may only persist records whose cached value
# matches what a fresh recomputation against the *current* graph would produce.
# If an input is mutated after a query computed but before save_checkpoint (no
# intervening get -- a "dirty graph"), that query's record is stale and must not
# warm on reload. Save omits it; the query re-executes to the correct value.
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

    # Reload must serve what a from-scratch database computes, not the stale
    # pre-set value baked at save. Pre-fix the stale record warms and serves 0.
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

    # The settled sibling must still warm from the checkpoint -- omitting the
    # stale record must not spill over and drop records that are still sound.
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

    # A settled save must still warm on reload -- guard against over-omission.
    assert reloaded.get(settled_reuse_q) == 12
    assert reloaded.inspect(settled_reuse_q).last_recompute == "reused"
