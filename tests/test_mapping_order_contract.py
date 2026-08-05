from __future__ import annotations

import pytest

from pyinc import Database, InMemoryArtifactStore, Input, query

Mode = str

_CANONICAL_KEYS = ("m", "a", "z")
_MAPPING_INPUT = Input[dict[str, int]]("contract.mapping-order")


@query(key="contract.mapping-result")
def _mapping_result(db: Database) -> dict[str, int]:
    source = _MAPPING_INPUT.read(db)
    return {"z": source["z"], "m": source["m"], "a": source["a"]}


@query(key="contract.mapping-iteration")
def _mapping_iteration(db: Database) -> tuple[str, ...]:
    return tuple(_mapping_result(db))


def _orders(db: Database) -> tuple[tuple[str, ...], ...]:
    mapping_result: dict[str, int] = db.get(_mapping_result)
    return (
        tuple(_MAPPING_INPUT.read(db)),
        tuple(mapping_result),
        db.get(_mapping_iteration),
    )


@pytest.mark.parametrize("mode", ("strict", "checked", "fast"))
def test_mapping_boundaries_use_canonical_iteration_order_warm_fresh_and_checkpoint(
    mode: Mode,
) -> None:
    original = {"z": 3, "a": 1, "m": 2}
    reordered = {"m": 2, "z": 3, "a": 1}
    expected = (_CANONICAL_KEYS, _CANONICAL_KEYS, _CANONICAL_KEYS)
    store = InMemoryArtifactStore()

    writer = Database(mode=mode, store=store)
    writer.set(_MAPPING_INPUT, original)
    assert _orders(writer) == expected

    writer.set(_MAPPING_INPUT, reordered)
    assert _orders(writer) == expected
    assert writer.inspect(_mapping_iteration).last_decision == "reused"
    checkpoint = writer.save_checkpoint()

    warmed = Database(mode=mode, store=store)
    warmed.set(_MAPPING_INPUT, reordered)
    warmed.load_checkpoint(checkpoint)

    fresh = Database(mode=mode)
    fresh.set(_MAPPING_INPUT, reordered)

    assert _orders(warmed) == _orders(fresh) == expected
    assert warmed.inspect(_mapping_iteration).last_decision == "reused"
