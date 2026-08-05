from __future__ import annotations

from typing import Any

import pytest

from pyinc import Database, InMemoryArtifactStore, Input, UnsupportedValueError, query
from pyinc.value import FrozenDict, FrozenSet, fingerprint_snapshot


class _RejectingStore:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, _digest: str) -> bytes | None:
        self.calls += 1
        raise OSError("store must not be consulted by input mutation")

    def put(self, _digest: str, _payload: bytes) -> None:
        self.calls += 1
        raise OSError("store must not be consulted by input mutation")

    def contains(self, _digest: str) -> bool:
        self.calls += 1
        raise OSError("store must not be consulted by input mutation")


def _ordered(values: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(sorted(values, key=fingerprint_snapshot))


def _colliding_dictionary() -> FrozenDict:
    keys = _ordered((1, 1.0))
    return FrozenDict(tuple((key, index) for index, key in enumerate(keys)))


def _database_state(db: Database, store: InMemoryArtifactStore) -> tuple[Any, ...]:
    return (
        dict(db._records),
        dict(db._input_records),
        dict(db._inputs_by_key),
        db.revision,
        dict(db._stats),
        store.keys(),
    )


@pytest.mark.parametrize("mode", ("strict", "checked", "fast"))
def test_failed_set_is_atomic_and_leaves_no_policy_registration(mode: str) -> None:
    store = InMemoryArtifactStore()
    db = Database(mode=mode, store=store)
    rejected = Input[object]("atomic-input", eq=lambda _left, _right: True)
    before = _database_state(db, store)

    with pytest.raises(UnsupportedValueError, match="collide after thaw"):
        db.set(rejected, _colliding_dictionary())

    assert _database_state(db, store) == before

    replacement = Input[int]("atomic-input", cutoff=lambda value: value % 2)
    db.set(replacement, 3)

    @query(key=f"atomic-input-reader-{mode}")
    def read_replacement(current: Database) -> int:
        return replacement.read(current)

    assert db.get(read_replacement) == 3
    checkpoint = db.save_checkpoint()
    loaded = Database(mode=mode, store=store)
    loaded.load_checkpoint(checkpoint)
    loaded.set(replacement, 3)
    assert loaded.get(read_replacement) == 3


@pytest.mark.parametrize("mode", ("strict", "checked", "fast"))
def test_failed_set_many_does_not_persist_earlier_values(mode: str) -> None:
    store = InMemoryArtifactStore()
    db = Database(mode=mode, store=store)
    first = Input[int]("atomic-many-first")
    second = Input[object]("atomic-many-second")
    before = _database_state(db, store)

    with pytest.raises(UnsupportedValueError, match="collide after thaw"):
        db.set_many(((first, 1), (second, _colliding_dictionary())))

    assert _database_state(db, store) == before


@pytest.mark.parametrize("mode", ("strict", "checked", "fast"))
def test_input_transactions_do_not_depend_on_nontransactional_store_io(mode: str) -> None:
    store = _RejectingStore()
    db = Database(mode=mode, store=store)
    first = Input[int]("store-independent-first")
    second = Input[int]("store-independent-second")

    db.set_many(((first, 1), (second, 2)))

    assert store.calls == 0
    assert db.revision == 1
    assert db._records[db._input_records[first]].snapshot == 1
    assert db._records[db._input_records[second]].snapshot == 2


@pytest.mark.parametrize("mode", ("strict", "checked", "fast"))
def test_query_results_with_thaw_colliding_public_keys_or_members_are_rejected(
    mode: str,
) -> None:
    dictionary = _colliding_dictionary()
    members = FrozenSet("set", _ordered((True, 1)))

    @query(key=f"colliding-query-result-{mode}")
    def invalid_result(_db: Database, kind: str) -> object:
        return dictionary if kind == "dict" else members

    store = InMemoryArtifactStore()
    db = Database(mode=mode, store=store)
    for kind in ("dict", "set"):
        with pytest.raises(UnsupportedValueError, match="collide after thaw"):
            db.get(invalid_result, kind)
    assert db._records == {}
    assert store.get(fingerprint_snapshot(dictionary)) is None
    assert store.get(fingerprint_snapshot(members)) is None
