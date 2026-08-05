from __future__ import annotations

from typing import Any

import pytest

from pyinc import Database, FrozenList, InMemoryArtifactStore, Input, query


def _mutate_operand(value: Any) -> None:
    if type(value) is list:
        value[0] = 999
        return
    if type(value) is FrozenList:
        object.__setattr__(value, "items", (999,))
        return
    raise AssertionError(f"unexpected policy operand {type(value).__qualname__}")


def _mutating_eq(left: Any, right: Any) -> bool:
    _mutate_operand(left)
    _mutate_operand(right)
    return True


def _mutating_cutoff(value: Any) -> int:
    _mutate_operand(value)
    return 0


def _raising_mutating_eq(left: Any, right: Any) -> bool:
    _mutate_operand(left)
    _mutate_operand(right)
    raise RuntimeError("comparison failed")


def _contents(value: Any) -> tuple[int, ...]:
    return tuple(value)


@pytest.mark.parametrize("mode", ("strict", "checked", "fast"))
@pytest.mark.parametrize("policy", ("eq", "cutoff"))
def test_query_policies_receive_detached_operands_warm_fresh_and_checkpoint(
    mode: str, policy: str
) -> None:
    source = Input[int](f"detached-query-policy-source-{mode}-{policy}")

    def calculate(db: Database) -> list[int]:
        return [source.read(db)]

    if policy == "eq":
        value = query(key=f"detached-query-policy-{mode}-{policy}", eq=_mutating_eq)(calculate)
    else:
        value = query(key=f"detached-query-policy-{mode}-{policy}", cutoff=_mutating_cutoff)(
            calculate
        )

    store = InMemoryArtifactStore()
    warm = Database(mode=mode, store=store)
    warm.set(source, 1)
    assert _contents(warm.get(value)) == (1,)
    checkpoint = warm.save_checkpoint()

    loaded = Database(mode=mode, store=store)
    loaded.set(source, 1)
    loaded.load_checkpoint(checkpoint)

    warm.set(source, 2)
    loaded.set(source, 2)
    warm_value = _contents(warm.get(value))
    loaded_value = _contents(loaded.get(value))

    fresh = Database(mode=mode)
    fresh.set(source, 2)
    fresh_value = _contents(fresh.get(value))
    assert warm_value == loaded_value == fresh_value == (2,)


@pytest.mark.parametrize("mode", ("strict", "checked", "fast"))
def test_input_comparator_mutation_and_failure_leave_the_stored_value_untouched(
    mode: str,
) -> None:
    source = Input[list[int]](
        f"detached-input-policy-{mode}",
        eq=_raising_mutating_eq,
    )
    store = InMemoryArtifactStore()
    db = Database(mode=mode, store=store)
    db.set(source, [1])
    revision_before = db.revision
    statistics_before = db.statistics()
    store_before = store.keys()

    with pytest.raises(RuntimeError, match="comparison failed"):
        db.set(source, [2])

    assert _contents(source.read(db)) == (1,)
    assert db.revision == revision_before
    assert db.statistics() == statistics_before
    assert store.keys() == store_before
