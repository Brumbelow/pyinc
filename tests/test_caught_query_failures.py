from __future__ import annotations

import json
from typing import Any

import pytest

from pyinc import Database, InMemoryArtifactStore, Input, UnsupportedValueError, query
from pyinc.runtime import NodeRecord

_MODES = ("strict", "checked", "fast")


def _query_record(db: Database, query_obj: Any) -> NodeRecord:
    key, _call_snapshot = db._query_key(query_obj, (), {})
    return db._records[key]


@pytest.mark.parametrize("mode", _MODES)
def test_caught_cold_child_failure_heals_without_stale_fallback(mode: str) -> None:
    state = Input[str](f"caught-cold-child-state-{mode}")

    @query(key=f"caught-cold-child-{mode}")
    def child(db: Database) -> str:
        value = state.read(db)
        if value == "fail":
            raise RuntimeError("child failed")
        return value

    @query(key=f"caught-cold-parent-{mode}")
    def parent(db: Database) -> tuple[str, ...]:
        try:
            return ("value", child(db))
        except RuntimeError:
            return ("fallback",)

    warm_db = Database(mode=mode)
    warm_db.set(state, "fail")
    assert warm_db.get(parent) == ("fallback",)

    failed_record = _query_record(warm_db, parent)
    assert failed_record.dependencies == set()
    assert failed_record.untracked_reasons == [
        f"caught failure from child query {child.key!r} before it published"
    ]
    assert not failed_record.checkpointable

    warm_db.set(state, "healed")
    warm = warm_db.get(parent)

    fresh_db = Database(mode=mode)
    fresh_db.set(state, "healed")
    fresh = fresh_db.get(parent)

    assert warm == fresh == ("value", "healed")
    healed_record = _query_record(warm_db, parent)
    assert healed_record.untracked_reasons == []
    assert healed_record.checkpointable
    assert len(healed_record.dependencies) == 1


@pytest.mark.parametrize("mode", _MODES)
def test_child_success_failure_and_heal_are_handled_in_parent_body(mode: str) -> None:
    state = Input[str](f"caught-transition-state-{mode}")

    @query(key=f"caught-transition-child-{mode}")
    def child(db: Database) -> str:
        value = state.read(db)
        if value == "fail":
            raise LookupError("child failed")
        return value

    @query(key=f"caught-transition-parent-{mode}")
    def parent(db: Database) -> tuple[str, str]:
        try:
            return ("value", child(db))
        except LookupError:
            return ("fallback", "child failed")

    warm_db = Database(mode=mode)
    warm_db.set(state, "first")
    assert warm_db.get(parent) == ("value", "first")
    child_key, _call_snapshot = warm_db._query_key(child, (), {})
    child_record = warm_db._records[child_key]
    child_snapshot = child_record.snapshot
    child_digest = child_record.digest

    warm_db.set(state, "fail")
    warm_failure = warm_db.get(parent)

    fresh_failure_db = Database(mode=mode)
    fresh_failure_db.set(state, "fail")
    fresh_failure = fresh_failure_db.get(parent)

    assert warm_failure == fresh_failure == ("fallback", "child failed")
    failed_record = _query_record(warm_db, parent)
    assert failed_record.untracked_reasons == [
        f"caught failure from child query {child.key!r} before it published"
    ]
    assert not failed_record.checkpointable
    assert warm_db._records[child_key] is child_record
    assert child_record.snapshot == child_snapshot
    assert child_record.digest == child_digest

    warm_db.set(state, "healed")
    warm_healed = warm_db.get(parent)

    fresh_healed_db = Database(mode=mode)
    fresh_healed_db.set(state, "healed")
    fresh_healed = fresh_healed_db.get(parent)

    assert warm_healed == fresh_healed == ("value", "healed")


@pytest.mark.parametrize("mode", _MODES)
def test_failure_propagates_across_query_frames_and_deduplicates(mode: str) -> None:
    state = Input[str](f"caught-multiframe-state-{mode}")

    @query(key=f"caught-multiframe-leaf-{mode}")
    def leaf(db: Database) -> str:
        value = state.read(db)
        if value == "fail":
            raise RuntimeError("leaf failed")
        return value

    @query(key=f"caught-multiframe-bridge-{mode}")
    def bridge(db: Database) -> str:
        return leaf(db)

    @query(key=f"caught-multiframe-root-{mode}")
    def root(db: Database) -> int:
        caught = 0
        for _attempt in range(2):
            try:
                bridge(db)
            except RuntimeError:
                caught += 1
        return caught

    warm_db = Database(mode=mode)
    warm_db.set(state, "fail")
    assert warm_db.get(root) == 2

    root_record = _query_record(warm_db, root)
    assert root_record.dependencies == set()
    assert root_record.untracked_reasons == [
        f"caught failure from child query {bridge.key!r} before it published"
    ]
    assert not root_record.checkpointable
    assert all(query_obj is not leaf for query_obj in warm_db._query_registry.values())
    assert all(query_obj is not bridge for query_obj in warm_db._query_registry.values())

    warm_db.set(state, "healed")
    warm = warm_db.get(root)

    fresh_db = Database(mode=mode)
    fresh_db.set(state, "healed")
    fresh = fresh_db.get(root)

    assert warm == fresh == 0


@pytest.mark.parametrize("mode", _MODES)
def test_caught_child_keying_failure_rolls_back_and_marks_catcher(mode: str) -> None:
    @query(key=f"caught-keying-child-{mode}")
    def child(db: Database, value: object) -> int:
        del db, value
        return 1

    @query(key=f"caught-keying-parent-{mode}")
    def parent(db: Database) -> int:
        try:
            return child(db, object())
        except UnsupportedValueError:
            return 7

    db = Database(mode=mode)
    assert db.get(parent) == 7

    parent_record = _query_record(db, parent)
    assert parent_record.untracked_reasons == [
        f"caught failure from child query {child.key!r} before it published"
    ]
    assert not parent_record.checkpointable
    assert all(query_obj is not child for query_obj in db._query_registry.values())
    assert all(child.key not in key.label for key in db._call_snapshot_registry)


@pytest.mark.parametrize("mode", _MODES)
def test_caught_checkpoint_warm_failure_rolls_back_and_marks_catcher(mode: str) -> None:
    @query(key=f"caught-warm-child-{mode}")
    def child(db: Database) -> int:
        del db
        return 1

    class WarmFailureDatabase(Database):
        def _try_warm_from_checkpoint(
            self,
            query_obj: Any,
            key: Any,
            call_snapshot: Any,
        ) -> bool:
            del key, call_snapshot
            if query_obj is child:
                raise RuntimeError("warm failed")
            return False

    @query(key=f"caught-warm-parent-{mode}")
    def parent(db: Database) -> int:
        try:
            return child(db)
        except RuntimeError:
            return 7

    db = WarmFailureDatabase(mode=mode)
    db._checkpoint_query_records = {object(): {}}  # type: ignore[dict-item]
    assert db.get(parent) == 7

    parent_record = _query_record(db, parent)
    assert parent_record.untracked_reasons == [
        f"caught failure from child query {child.key!r} before it published"
    ]
    assert not parent_record.checkpointable
    assert all(query_obj is not child for query_obj in db._query_registry.values())
    assert all(child.key not in key.label for key in db._call_snapshot_registry)


@pytest.mark.parametrize("mode", _MODES)
def test_caught_failure_record_is_omitted_from_same_mode_checkpoint(mode: str) -> None:
    state = Input[str](f"caught-checkpoint-state-{mode}")

    @query(key=f"caught-checkpoint-child-{mode}")
    def child(db: Database) -> str:
        value = state.read(db)
        if value == "fail":
            raise RuntimeError("child failed")
        return value

    @query(key=f"caught-checkpoint-parent-{mode}")
    def parent(db: Database) -> tuple[str, ...]:
        try:
            return ("value", child(db))
        except RuntimeError:
            return ("fallback",)

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    writer.set(state, "fail")
    assert writer.get(parent) == ("fallback",)
    assert not _query_record(writer, parent).checkpointable

    checkpoint_key = writer.save_checkpoint()
    manifest_bytes = store.get(checkpoint_key)
    assert manifest_bytes is not None
    manifest = json.loads(manifest_bytes)
    assert all(record.get("query_id") != parent.key for record in manifest["records"])

    reader = Database(mode=mode, store=store)
    reader.set(state, "healed")
    reader.load_checkpoint(checkpoint_key)
    warm = reader.get(parent)

    fresh_db = Database(mode=mode)
    fresh_db.set(state, "healed")
    fresh = fresh_db.get(parent)

    assert warm == fresh == ("value", "healed")
    assert reader.inspect(parent).last_recompute == "executed"
