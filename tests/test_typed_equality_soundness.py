from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from pyinc import (
    Database,
    FrozenList,
    InMemoryArtifactStore,
    Input,
    Query,
    freeze,
    query,
    semantic_equal,
)
from pyinc.resources import Resource

MODES = ("strict", "checked", "fast")
NUMERIC_TRANSITIONS = (
    pytest.param("int-float", 1, 1.0, id="int-float"),
    pytest.param("bool-int", True, 1, id="bool-int"),
    pytest.param("signed-zero", 0.0, -0.0, id="signed-zero"),
)
_PREFROZEN_NAN = cast(FrozenList, freeze([float("nan")]))


def _numeric_observable(value: object) -> tuple[str, str]:
    if type(value) is float:
        return ("float", value.hex())
    return (type(value).__name__, repr(value))


def _set_input(db: Database, input_value: Input[Any], value: Any, entrypoint: str) -> None:
    if entrypoint == "set":
        db.set(input_value, value)
    else:
        db.set_many([(input_value, value)])


@dataclass(frozen=True)
class _TypedProbeResource(Resource[str, str, object]):
    def _state(self, path: str) -> dict[str, object]:
        return cast(dict[str, object], json.loads(Path(path).read_text(encoding="utf-8")))

    def probe(self, path: str) -> object:
        return self._state(path)["probe"]

    def load(self, db: Database, path: str) -> str:
        state = self._state(path)
        failure = state["failure"]
        if failure is not None:
            raise RuntimeError(str(failure))
        return str(state["value"])

    def probe_and_load(self, db: Database, path: str) -> tuple[object, str]:
        state = self._state(path)
        failure = state["failure"]
        if failure is not None:
            raise RuntimeError(str(failure))
        return state["probe"], str(state["value"])

    def label(self, path: str) -> str:
        return f"typed-probe[{path}]"


class _MutableNanResource(Resource[None, FrozenList, FrozenList]):
    def __init__(self, *, probe: FrozenList, value: FrozenList) -> None:
        self.current_probe = probe
        self.current_value = value

    def identity(self) -> object:
        return "mutable-nan-resource"

    def probe(self, parameter: None) -> FrozenList:
        return self.current_probe

    def load(self, db: Database, parameter: None) -> FrozenList:
        return self.current_value

    def probe_and_load(self, db: Database, parameter: None) -> tuple[FrozenList, FrozenList]:
        return self.current_probe, self.current_value

    def label(self, parameter: None) -> str:
        return "mutable-nan-resource"


def _nan_wrapper(value: float) -> FrozenList:
    return FrozenList((value,))


def _nan_hash(value: object) -> int:
    items = cast(Any, value)
    return hash(items[0])


def _write_resource_state(
    path: Path, *, probe: object, value: str, failure: str | None = None
) -> None:
    path.write_text(
        json.dumps({"probe": probe, "value": value, "failure": failure}),
        encoding="utf-8",
    )


@pytest.mark.parametrize(("_case", "old_value", "new_value"), NUMERIC_TRANSITIONS)
def test_semantic_equal_rejects_python_equal_numeric_representations(
    _case: str, old_value: object, new_value: object
) -> None:
    assert old_value == new_value
    assert not semantic_equal(old_value, new_value)


def test_query_compare_uses_typed_relation_for_default_and_cutoff() -> None:
    def calculate(db: Database) -> object:
        return None

    default = Query(calculate)
    token = Query(calculate, cutoff=lambda value: value[0])

    for old_value, new_value in ((1, 1.0), (True, 1), (0.0, -0.0)):
        assert not default.compare(old_value, new_value)
        assert not token.compare((old_value, "old"), (new_value, "new"))

    nan_token = Query(calculate, cutoff=lambda _value: _PREFROZEN_NAN)
    assert not default.compare(_PREFROZEN_NAN, _PREFROZEN_NAN)
    assert not nan_token.compare("old", "new")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("case", "old_value", "new_value"), NUMERIC_TRANSITIONS)
def test_default_query_numeric_transition_matches_fresh(
    mode: str, case: str, old_value: object, new_value: object
) -> None:
    stage = Input[int](f"ker01.query.{mode}.{case}")

    @query
    def numeric_value(db: Database) -> object:
        return old_value if stage.read(db) == 0 else new_value

    @query
    def observe_numeric_value(db: Database) -> tuple[str, str]:
        return _numeric_observable(numeric_value(db))

    warm = Database(mode=mode)
    warm.set(stage, 0)
    warm.get(observe_numeric_value)
    warm.set(stage, 1)

    fresh = Database(mode=mode)
    fresh.set(stage, 1)
    expected = fresh.get(observe_numeric_value)

    assert warm.get(observe_numeric_value) == expected == _numeric_observable(new_value)
    assert warm.inspect(numeric_value).last_recompute == "executed"
    assert warm.inspect(observe_numeric_value).last_recompute == "executed"


@pytest.mark.parametrize("mode", MODES)
def test_prefrozen_nan_query_rechecks_dependents_and_matches_fresh(mode: str) -> None:
    stage = Input[int](f"ker01.query.nan.{mode}")

    @query
    def nan_items(db: Database) -> FrozenList:
        stage.read(db)
        return _PREFROZEN_NAN

    @query
    def describe_nan_items(db: Database) -> tuple[int, bool]:
        items = nan_items(db)
        return len(items), math.isnan(cast(float, next(iter(items))))

    warm = Database(mode=mode)
    warm.set(stage, 0)
    warm.get(describe_nan_items)
    warm.set(stage, 1)

    fresh = Database(mode=mode)
    fresh.set(stage, 1)

    assert warm.get(describe_nan_items) == fresh.get(describe_nan_items) == (1, True)
    assert warm.inspect(nan_items).last_recompute == "executed"
    assert warm.inspect(describe_nan_items).last_recompute == "backdated"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("entrypoint", ("set", "set_many"))
@pytest.mark.parametrize(("case", "old_value", "new_value"), NUMERIC_TRANSITIONS)
def test_input_numeric_transition_matches_fresh(
    mode: str,
    entrypoint: str,
    case: str,
    old_value: object,
    new_value: object,
) -> None:
    source = Input[object](f"ker01.input.{entrypoint}.{mode}.{case}")

    @query
    def observe_input(db: Database) -> tuple[str, str]:
        return _numeric_observable(source.read(db))

    warm = Database(mode=mode)
    _set_input(warm, source, old_value, entrypoint)
    warm.get(observe_input)
    _set_input(warm, source, new_value, entrypoint)

    fresh = Database(mode=mode)
    _set_input(fresh, source, new_value, entrypoint)

    assert warm.get(observe_input) == fresh.get(observe_input) == _numeric_observable(new_value)
    assert warm.statistics().input_equal_ignores == 0


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("entrypoint", ("set", "set_many"))
def test_nan_input_update_is_not_ignored(mode: str, entrypoint: str) -> None:
    source = Input[object](f"ker01.input.nan.{entrypoint}.{mode}")

    @query
    def observe_nan_input(db: Database) -> bool:
        return math.isnan(cast(float, source.read(db)))

    warm = Database(mode=mode)
    _set_input(warm, source, float("nan"), entrypoint)
    warm.get(observe_nan_input)
    backdates = warm.statistics().query_backdates
    _set_input(warm, source, float("nan"), entrypoint)

    fresh = Database(mode=mode)
    _set_input(fresh, source, float("nan"), entrypoint)

    assert warm.get(observe_nan_input) == fresh.get(observe_nan_input) is True
    assert warm.statistics().query_backdates == backdates + 1
    assert warm.inspect(observe_nan_input).last_recompute == "backdated"
    assert warm.statistics().input_equal_ignores == 0


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("case", "old_token", "new_token"), NUMERIC_TRANSITIONS)
def test_query_cutoff_token_transition_matches_fresh(
    mode: str, case: str, old_token: object, new_token: object
) -> None:
    stage = Input[int](f"ker01.query-cutoff.{mode}.{case}")

    @query(cutoff=lambda value: value[0])
    def tokenized(db: Database) -> tuple[object, str]:
        return (old_token, "old") if stage.read(db) == 0 else (new_token, "new")

    @query
    def tokenized_payload(db: Database) -> str:
        return tokenized(db)[1]

    warm = Database(mode=mode)
    warm.set(stage, 0)
    warm.get(tokenized_payload)
    warm.set(stage, 1)

    fresh = Database(mode=mode)
    fresh.set(stage, 1)

    assert warm.get(tokenized_payload) == fresh.get(tokenized_payload) == "new"
    assert warm.inspect(tokenized).last_recompute == "executed"


@pytest.mark.parametrize("mode", MODES)
def test_prefrozen_nan_query_cutoff_token_matches_fresh(mode: str) -> None:
    stage = Input[int](f"ker01.query-cutoff.nan.{mode}")

    @query(cutoff=lambda _value: _PREFROZEN_NAN)
    def tokenized(db: Database) -> str:
        return "old" if stage.read(db) == 0 else "new"

    @query
    def tokenized_payload(db: Database) -> str:
        return tokenized(db)

    warm = Database(mode=mode)
    warm.set(stage, 0)
    warm.get(tokenized_payload)
    warm.set(stage, 1)

    fresh = Database(mode=mode)
    fresh.set(stage, 1)

    assert warm.get(tokenized_payload) == fresh.get(tokenized_payload) == "new"
    assert warm.inspect(tokenized).last_recompute == "executed"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("entrypoint", ("set", "set_many"))
@pytest.mark.parametrize(("case", "old_token", "new_token"), NUMERIC_TRANSITIONS)
def test_input_cutoff_token_transition_matches_fresh(
    mode: str,
    entrypoint: str,
    case: str,
    old_token: object,
    new_token: object,
) -> None:
    source = Input[tuple[object, str]](
        f"ker01.input-cutoff.{entrypoint}.{mode}.{case}", cutoff=lambda value: value[0]
    )

    @query
    def input_payload(db: Database) -> str:
        return source.read(db)[1]

    warm = Database(mode=mode)
    _set_input(warm, source, (old_token, "old"), entrypoint)
    warm.get(input_payload)
    _set_input(warm, source, (new_token, "new"), entrypoint)

    fresh = Database(mode=mode)
    _set_input(fresh, source, (new_token, "new"), entrypoint)

    assert warm.get(input_payload) == fresh.get(input_payload) == "new"
    assert warm.statistics().input_equal_ignores == 0


@pytest.mark.parametrize("mode", MODES)
def test_prefrozen_nan_input_cutoff_token_matches_fresh(mode: str) -> None:
    source = Input[str](f"ker01.input-cutoff.nan.{mode}", cutoff=lambda _value: _PREFROZEN_NAN)

    @query
    def input_payload(db: Database) -> str:
        return source.read(db)

    warm = Database(mode=mode)
    warm.set(source, "old")
    warm.get(input_payload)
    warm.set(source, "new")

    fresh = Database(mode=mode)
    fresh.set(source, "new")

    assert warm.get(input_payload) == fresh.get(input_payload) == "new"
    assert warm.statistics().input_equal_ignores == 0


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("case", "old_probe", "new_probe"), NUMERIC_TRANSITIONS)
def test_resource_success_probe_transition_matches_fresh(
    tmp_path: Path,
    mode: str,
    case: str,
    old_probe: object,
    new_probe: object,
) -> None:
    state_path = tmp_path / f"success-{case}.json"
    resource = _TypedProbeResource()

    @query
    def read_value(db: Database, path: str) -> str:
        return resource.read(db, path)

    _write_resource_state(state_path, probe=old_probe, value="old")
    warm = Database(mode=mode)
    assert warm.get(read_value, str(state_path)) == "old"

    _write_resource_state(state_path, probe=new_probe, value="new")
    fresh = Database(mode=mode)

    assert warm.get(read_value, str(state_path)) == fresh.get(read_value, str(state_path)) == "new"
    assert warm.inspect(read_value, str(state_path)).last_decision == "executed"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("case", "old_probe", "new_probe"), NUMERIC_TRANSITIONS)
def test_resource_failure_probe_transition_matches_fresh(
    tmp_path: Path,
    mode: str,
    case: str,
    old_probe: object,
    new_probe: object,
) -> None:
    state_path = tmp_path / f"failure-{case}.json"
    resource = _TypedProbeResource()

    @query
    def read_failure(db: Database, path: str) -> str:
        try:
            resource.read(db, path)
        except RuntimeError as exc:
            return str(exc)
        raise AssertionError("resource unexpectedly succeeded")

    _write_resource_state(state_path, probe=old_probe, value="unused", failure="old")
    warm = Database(mode=mode)
    assert warm.get(read_failure, str(state_path)) == "old"

    _write_resource_state(state_path, probe=new_probe, value="unused", failure="new")
    fresh = Database(mode=mode)

    assert (
        warm.get(read_failure, str(state_path)) == fresh.get(read_failure, str(state_path)) == "new"
    )
    assert warm.inspect(read_failure, str(state_path)).last_decision == "executed"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("entrypoint", ("set", "set_many"))
@pytest.mark.parametrize(("case", "old_value", "new_value"), NUMERIC_TRANSITIONS)
def test_checkpoint_does_not_preserve_stale_input_update(
    mode: str,
    entrypoint: str,
    case: str,
    old_value: object,
    new_value: object,
) -> None:
    source = Input[object](f"ker01.checkpoint-input.{entrypoint}.{mode}.{case}")

    @query
    def observe_input(db: Database) -> tuple[str, str]:
        return _numeric_observable(source.read(db))

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    _set_input(writer, source, old_value, entrypoint)
    writer.get(observe_input)
    _set_input(writer, source, new_value, entrypoint)
    checkpoint = writer.save_checkpoint()

    warmed = Database(mode=mode, store=store)
    _set_input(warmed, source, new_value, entrypoint)
    warmed.load_checkpoint(checkpoint)

    fresh = Database(mode=mode)
    _set_input(fresh, source, new_value, entrypoint)

    assert warmed.get(observe_input) == fresh.get(observe_input) == _numeric_observable(new_value)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("case", "old_value", "new_value"), NUMERIC_TRANSITIONS)
def test_checkpoint_does_not_preserve_stale_query_backdate(
    mode: str, case: str, old_value: object, new_value: object
) -> None:
    stage = Input[int](f"ker01.checkpoint-query.{mode}.{case}")

    @query
    def numeric_value(db: Database) -> object:
        return old_value if stage.read(db) == 0 else new_value

    @query
    def observe_numeric_value(db: Database) -> tuple[str, str]:
        return _numeric_observable(numeric_value(db))

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    writer.set(stage, 0)
    writer.get(observe_numeric_value)
    writer.set(stage, 1)
    writer.get(numeric_value)
    checkpoint = writer.save_checkpoint()

    warmed = Database(mode=mode, store=store)
    warmed.set(stage, 1)
    warmed.load_checkpoint(checkpoint)

    fresh = Database(mode=mode)
    fresh.set(stage, 1)

    assert (
        warmed.get(observe_numeric_value)
        == fresh.get(observe_numeric_value)
        == _numeric_observable(new_value)
    )


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("case", "old_token", "new_token"), NUMERIC_TRANSITIONS)
def test_checkpoint_does_not_preserve_stale_query_cutoff_backdate(
    mode: str, case: str, old_token: object, new_token: object
) -> None:
    stage = Input[int](f"ker01.checkpoint-query-cutoff.{mode}.{case}")

    @query(cutoff=lambda value: value[0])
    def tokenized(db: Database) -> tuple[object, str]:
        return (old_token, "old") if stage.read(db) == 0 else (new_token, "new")

    @query
    def tokenized_payload(db: Database) -> str:
        return tokenized(db)[1]

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    writer.set(stage, 0)
    writer.get(tokenized_payload)
    writer.set(stage, 1)
    writer.get(tokenized)
    checkpoint = writer.save_checkpoint()

    warmed = Database(mode=mode, store=store)
    warmed.set(stage, 1)
    warmed.load_checkpoint(checkpoint)

    fresh = Database(mode=mode)
    fresh.set(stage, 1)

    assert warmed.get(tokenized_payload) == fresh.get(tokenized_payload) == "new"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("entrypoint", ("set", "set_many"))
@pytest.mark.parametrize(("case", "old_token", "new_token"), NUMERIC_TRANSITIONS)
def test_checkpoint_does_not_preserve_stale_input_cutoff_update(
    mode: str,
    entrypoint: str,
    case: str,
    old_token: object,
    new_token: object,
) -> None:
    source = Input[tuple[object, str]](
        f"ker01.checkpoint-input-cutoff.{entrypoint}.{mode}.{case}",
        cutoff=lambda value: value[0],
    )

    @query
    def input_payload(db: Database) -> str:
        return source.read(db)[1]

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    _set_input(writer, source, (old_token, "old"), entrypoint)
    writer.get(input_payload)
    _set_input(writer, source, (new_token, "new"), entrypoint)
    checkpoint = writer.save_checkpoint()

    warmed = Database(mode=mode, store=store)
    _set_input(warmed, source, (new_token, "new"), entrypoint)
    warmed.load_checkpoint(checkpoint)

    fresh = Database(mode=mode)
    _set_input(fresh, source, (new_token, "new"), entrypoint)

    assert warmed.get(input_payload) == fresh.get(input_payload) == "new"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(("case", "old_probe", "new_probe"), NUMERIC_TRANSITIONS)
def test_checkpoint_probe_hint_rejects_typed_numeric_collision(
    tmp_path: Path,
    mode: str,
    case: str,
    old_probe: object,
    new_probe: object,
) -> None:
    state_path = tmp_path / f"checkpoint-probe-{case}.json"
    resource = _TypedProbeResource()

    @query
    def read_value(db: Database, path: str) -> str:
        return resource.read(db, path)

    store = InMemoryArtifactStore()
    _write_resource_state(state_path, probe=old_probe, value="old")
    writer = Database(mode=mode, store=store)
    assert writer.get(read_value, str(state_path)) == "old"
    checkpoint = writer.save_checkpoint()

    _write_resource_state(state_path, probe=new_probe, value="new")
    warmed = Database(mode=mode, store=store)
    warmed.load_checkpoint(checkpoint)
    fresh = Database(mode=mode)

    assert (
        warmed.get(read_value, str(state_path)) == fresh.get(read_value, str(state_path)) == "new"
    )


@pytest.mark.parametrize("mode", MODES)
def test_dependency_free_nan_result_and_parent_reexecute_each_request(mode: str) -> None:
    @query
    def nan_value(db: Database) -> float:
        return float("nan")

    @query
    def observes_nan(db: Database) -> bool:
        return math.isnan(nan_value(db))

    warm = Database(mode=mode)
    assert warm.get(observes_nan) is True
    first_stats = warm.statistics()
    assert warm.get(observes_nan) is True

    fresh = Database(mode=mode)
    assert fresh.get(observes_nan) is True
    second_stats = warm.statistics()
    assert second_stats.query_executions == first_stats.query_executions + 1
    assert second_stats.query_backdates == first_stats.query_backdates + 1
    assert warm.inspect(nan_value).last_recompute == "executed"


@pytest.mark.parametrize("mode", MODES)
def test_nan_query_arguments_get_ephemeral_typed_identities(mode: str) -> None:
    old_nan = float("nan")
    new_nan = float("nan")
    old_argument = _nan_wrapper(old_nan)
    new_argument = _nan_wrapper(new_nan)

    @query
    def hash_argument(db: Database, value: FrozenList) -> int:
        return _nan_hash(value)

    warm = Database(mode=mode)
    old_result = warm.get(hash_argument, old_argument)
    warm_result = warm.get(hash_argument, new_argument)
    fresh_result = Database(mode=mode).get(hash_argument, new_argument)

    assert old_result == hash(old_nan)
    assert warm_result == fresh_result == hash(new_nan)
    identities = {
        key.identity
        for key in warm._records
        if key.kind == "query" and "hash_argument" in key.label
    }
    assert len(identities) == 2


@pytest.mark.parametrize("mode", MODES)
def test_nan_input_snapshot_forces_parent_reverification(mode: str) -> None:
    old_nan = float("nan")
    new_nan = float("nan")
    source = Input[FrozenList](f"ker01.input.non-substitutive.{mode}")

    @query
    def hash_input(db: Database) -> int:
        return _nan_hash(source.read(db))

    warm = Database(mode=mode)
    warm.set(source, _nan_wrapper(old_nan))
    assert warm.get(hash_input) == hash(old_nan)
    warm.set(source, _nan_wrapper(new_nan))

    fresh = Database(mode=mode)
    fresh.set(source, _nan_wrapper(new_nan))
    assert warm.get(hash_input) == fresh.get(hash_input) == hash(new_nan)


@pytest.mark.parametrize("mode", MODES)
def test_nan_input_dependency_is_not_restored_from_checkpoint(mode: str) -> None:
    old_nan = float("nan")
    new_nan = float("nan")
    old_value = _nan_wrapper(old_nan)
    new_value = _nan_wrapper(new_nan)
    source = Input[FrozenList](f"ker01.checkpoint-input.nan.{mode}")

    @query
    def hash_input(db: Database) -> int:
        return _nan_hash(source.read(db))

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    writer.set(source, old_value)
    assert writer.get(hash_input) == hash(old_nan)
    checkpoint = writer.save_checkpoint()

    warmed = Database(mode=mode, store=store)
    warmed.set(source, new_value)
    warmed.load_checkpoint(checkpoint)
    fresh = Database(mode=mode)
    fresh.set(source, new_value)

    assert warmed.get(hash_input) == fresh.get(hash_input) == hash(new_nan)
    assert warmed.inspect(hash_input).last_recompute == "executed"


@pytest.mark.parametrize("mode", MODES)
def test_nan_query_argument_is_not_restored_from_checkpoint(mode: str) -> None:
    old_nan = float("nan")
    new_nan = float("nan")
    old_argument = _nan_wrapper(old_nan)
    new_argument = _nan_wrapper(new_nan)

    @query
    def hash_argument(db: Database, value: FrozenList) -> int:
        return _nan_hash(value)

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    assert writer.get(hash_argument, old_argument) == hash(old_nan)
    checkpoint = writer.save_checkpoint()

    warmed = Database(mode=mode, store=store)
    warmed.load_checkpoint(checkpoint)
    fresh = Database(mode=mode)

    assert (
        warmed.get(hash_argument, new_argument)
        == fresh.get(hash_argument, new_argument)
        == hash(new_nan)
    )


@pytest.mark.parametrize("mode", MODES)
def test_nan_resource_value_and_probe_do_not_restore_or_reuse(mode: str) -> None:
    old_probe_nan = float("nan")
    new_probe_nan = float("nan")
    old_value_nan = float("nan")
    new_value_nan = float("nan")
    resource = _MutableNanResource(
        probe=_nan_wrapper(old_probe_nan),
        value=_nan_wrapper(old_value_nan),
    )

    @query
    def hash_resource(db: Database) -> int:
        return _nan_hash(resource.read(db, None))

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    assert writer.get(hash_resource) == hash(old_value_nan)
    checkpoint = writer.save_checkpoint()

    resource.current_probe = _nan_wrapper(new_probe_nan)
    resource.current_value = _nan_wrapper(new_value_nan)
    warmed = Database(mode=mode, store=store)
    warmed.load_checkpoint(checkpoint)
    fresh = Database(mode=mode)

    assert warmed.get(hash_resource) == fresh.get(hash_resource) == hash(new_value_nan)
    assert warmed.inspect(hash_resource).last_recompute == "executed"
