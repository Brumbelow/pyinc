from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias, TypeVar

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, rule, run_state_machine_as_test

from pyinc import (
    CheckpointModeError,
    Database,
    InMemoryArtifactStore,
    Input,
    Query,
    Resource,
    query,
)

Mode: TypeAlias = Literal["strict", "checked", "fast"]
Observation: TypeAlias = tuple[str, str, object]
ResultT = TypeVar("ResultT")

_MODES: tuple[Mode, ...] = ("strict", "checked", "fast")
_MAX_QUERY_NODES = 4

_CHOOSER = Input[str]("qa03-stateful-chooser")
_LEFT = Input[int]("qa03-stateful-left")
_RIGHT = Input[int]("qa03-stateful-right")
_BIAS = Input[int]("qa03-stateful-bias")


@dataclass(frozen=True)
class _MutableIntegerResource(Resource[str, int, tuple[str, str]]):
    """Read an externally mutable integer or raise a reproducible failure."""

    @staticmethod
    def _decode(path: str, raw: bytes) -> int:
        text = raw.decode("utf-8")
        kind, payload = text.split(":", 1)
        if kind == "fail":
            raise RuntimeError(f"external value {path} failed with code {payload}")
        if kind != "ok":
            raise RuntimeError(f"external value {path} has invalid kind {kind}")
        return int(payload)

    @staticmethod
    def _probe(raw: bytes) -> tuple[str, str]:
        return "present", hashlib.sha256(raw).hexdigest()

    def probe(self, key: str) -> tuple[str, str]:
        return self._probe(Path(key).read_bytes())

    def load(self, db: Database, key: str) -> int:
        del db
        return self._decode(key, Path(key).read_bytes())

    def probe_and_load(self, db: Database, key: str) -> tuple[tuple[str, str], int]:
        del db
        raw = Path(key).read_bytes()
        return self._probe(raw), self._decode(key, raw)

    def label(self, key: str) -> str:
        return f"qa03-mutable-integer[{key}]"


_MUTABLE_INTEGER = _MutableIntegerResource()


@query(key="qa03-stateful-left-branch")
def _left_branch(db: Database) -> int:
    return _LEFT.read(db)


@query(key="qa03-stateful-right-branch")
def _right_branch(db: Database) -> int:
    return _RIGHT.read(db)


@query(key="qa03-stateful-selected-branch")
def _selected_branch(db: Database) -> int:
    if _CHOOSER.read(db) == "left":
        return _left_branch(db)
    return _right_branch(db)


@query(key="qa03-stateful-resource-child")
def _resource_child(db: Database, path: str) -> int:
    return _MUTABLE_INTEGER.read(db, path)


@query(key="qa03-stateful-root")
def _root(db: Database, path: str, call_variant: int) -> tuple[int, str, int, int]:
    try:
        resource_kind = "value"
        resource_value = _resource_child(db, path)
    except RuntimeError:
        resource_kind = "fallback"
        resource_value = -1
    return (
        _selected_branch(db) + _BIAS.read(db),
        resource_kind,
        resource_value,
        call_variant,
    )


@query(key="qa03-stateful-eviction-filler")
def _eviction_filler(db: Database, slot: int) -> tuple[str, int]:
    del db
    return "filler", slot


def _capture(operation: Callable[[], object]) -> Observation:
    try:
        value = operation()
    except Exception as error:
        return "error", f"{type(error).__module__}.{type(error).__qualname__}", str(error)
    return "value", f"{type(value).__module__}.{type(value).__qualname__}", value


class _WarmFreshStateMachine(RuleBasedStateMachine):
    """Compare every evolving warm or restored observation with a cold model."""

    def __init__(self, mode: Mode) -> None:
        super().__init__()
        self.mode = mode
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self._temporary_directory.name) / "external-state.txt"
        self.state: dict[str, int | str] = {
            "chooser": "left",
            "left": 1,
            "right": 2,
            "bias": 0,
        }
        self.call_variant = 0
        self.store = InMemoryArtifactStore()
        self.database = self._new_database(store=self.store)
        self._install_inputs(self.database)

        # Every generated run contains the counterexample shapes even before
        # Hypothesis starts varying their order: a caught child/resource failure,
        # recovery, query-node eviction, same-mode restoration, and both cross-mode
        # refusal directions for this saver mode.
        self.path.write_text("fail:0", encoding="utf-8")
        self._assert_matches_fresh()

        failed_checkpoint = self.database.save_checkpoint()
        restored_failure = self._new_database(store=self.store)
        self._install_inputs(restored_failure)
        restored_failure.load_checkpoint(failed_checkpoint)
        self.database = restored_failure
        self._assert_matches_fresh()

        self.path.write_text("ok:7", encoding="utf-8")
        self._assert_matches_fresh()

        for slot in range(2):
            self._assert_query_matches_fresh(_eviction_filler, slot)
        assert self.database.statistics().evictions > 0
        self._assert_matches_fresh()

        self.last_checkpoint = self.database.save_checkpoint()
        restored = self._new_database(store=self.store)
        self._install_inputs(restored)
        restored.load_checkpoint(self.last_checkpoint)
        self.database = restored
        self._assert_matches_fresh()
        assert self.database.statistics().query_reuses > 0
        self._assert_cross_mode_rejection()

    def _new_database(
        self,
        *,
        mode: Mode | None = None,
        store: InMemoryArtifactStore | None = None,
    ) -> Database:
        return Database(
            mode=self.mode if mode is None else mode,
            max_query_nodes=_MAX_QUERY_NODES,
            store=store,
        )

    def _install_inputs(self, database: Database) -> None:
        database.set_many(
            (
                (_CHOOSER, self.state["chooser"]),
                (_LEFT, self.state["left"]),
                (_RIGHT, self.state["right"]),
                (_BIAS, self.state["bias"]),
            )
        )

    def _fresh_database(self, *, mode: Mode | None = None) -> Database:
        fresh = self._new_database(mode=mode)
        self._install_inputs(fresh)
        return fresh

    def _observe(self, database: Database) -> tuple[Observation, Observation]:
        direct = _capture(lambda: database.get(_resource_child, str(self.path)))
        root = _capture(lambda: database.get(_root, str(self.path), self.call_variant))
        return direct, root

    def _assert_matches_fresh(self) -> None:
        warm = self._observe(self.database)
        fresh = self._observe(self._fresh_database())
        assert warm == fresh

    def _assert_query_matches_fresh(
        self,
        query_obj: Query[[int], ResultT],
        argument: int,
    ) -> None:
        warm = _capture(lambda: self.database.get(query_obj, argument))
        fresh_database = self._fresh_database()
        fresh = _capture(lambda: fresh_database.get(query_obj, argument))
        assert warm == fresh

    def _assert_cross_mode_rejection(self) -> None:
        for other_mode in _MODES:
            if other_mode == self.mode:
                continue
            candidate = self._new_database(mode=other_mode, store=self.store)
            self._install_inputs(candidate)
            with pytest.raises(CheckpointModeError, match="cannot be loaded"):
                candidate.load_checkpoint(self.last_checkpoint)

    @rule(
        name=st.sampled_from(("left", "right", "bias")),
        value=st.integers(min_value=-8, max_value=8),
    )
    def update_one_input(self, name: str, value: int) -> None:
        input_key = {"left": _LEFT, "right": _RIGHT, "bias": _BIAS}[name]
        self.state[name] = value
        self.database.set(input_key, value)
        self._assert_matches_fresh()

    @rule(
        chooser=st.sampled_from(("left", "right")),
        left=st.integers(min_value=-8, max_value=8),
        right=st.integers(min_value=-8, max_value=8),
        bias=st.integers(min_value=-8, max_value=8),
    )
    def update_inputs_atomically(
        self,
        chooser: str,
        left: int,
        right: int,
        bias: int,
    ) -> None:
        self.state.update(chooser=chooser, left=left, right=right, bias=bias)
        self.database.set_many(((_CHOOSER, chooser), (_LEFT, left), (_RIGHT, right), (_BIAS, bias)))
        self._assert_matches_fresh()

    @rule()
    def rewire_selected_branch(self) -> None:
        chooser = "right" if self.state["chooser"] == "left" else "left"
        self.state["chooser"] = chooser
        self.database.set(_CHOOSER, chooser)
        self._assert_matches_fresh()

    @rule(
        fails=st.booleans(),
        value=st.integers(min_value=-8, max_value=8),
    )
    def mutate_external_resource(self, fails: bool, value: int) -> None:
        kind = "fail" if fails else "ok"
        self.path.write_text(f"{kind}:{value}", encoding="utf-8")
        self._assert_matches_fresh()

    @rule(slot=st.integers(min_value=0, max_value=12))
    def force_query_eviction(self, slot: int) -> None:
        self._assert_query_matches_fresh(_eviction_filler, slot)
        self._assert_matches_fresh()

    @rule(call_variant=st.integers(min_value=0, max_value=4))
    def change_query_call_identity(self, call_variant: int) -> None:
        self.call_variant = call_variant
        self._assert_matches_fresh()

    @rule()
    def save_checkpoint(self) -> None:
        self.last_checkpoint = self.database.save_checkpoint()
        self._assert_matches_fresh()

    @rule()
    def restore_checkpoint_into_new_database(self) -> None:
        restored = self._new_database(store=self.store)
        self._install_inputs(restored)
        restored.load_checkpoint(self.last_checkpoint)
        self.database = restored
        self._assert_matches_fresh()

    @rule()
    def reject_checkpoint_in_other_modes(self) -> None:
        self._assert_cross_mode_rejection()
        self._assert_matches_fresh()

    @rule()
    def replace_database_while_sharing_artifact_store(self) -> None:
        self.database = self._new_database(store=self.store)
        self._install_inputs(self.database)
        self._assert_matches_fresh()

    def teardown(self) -> None:
        self._temporary_directory.cleanup()


@pytest.mark.parametrize("mode", _MODES)
def test_stateful_warm_restored_results_match_fresh_model(mode: Mode) -> None:
    run_state_machine_as_test(  # type: ignore[no-untyped-call]
        lambda: _WarmFreshStateMachine(mode),
        settings=settings(
            max_examples=4,
            stateful_step_count=8,
            deadline=None,
        ),
    )
