from __future__ import annotations

from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest

from pyinc import (
    Database,
    FileResource,
    InMemoryArtifactStore,
    Input,
    QueryContextError,
    Resource,
    ResourceDependencyError,
    Subscription,
    query,
)
from pyinc.action import Action, Output, action

_MODES = ("strict", "checked", "fast")
_FORBIDDEN_OPERATIONS = (
    "mode",
    "max_query_nodes",
    "revision",
    "statistics",
    "reset_statistics",
    "query_profile",
    "dependency_graph",
    "set",
    "set_many",
    "explain",
    "inspect",
    "inspect_fresh",
    "request_span",
    "request_inputs_changed",
    "observe",
    "save_checkpoint",
    "load_checkpoint",
    "unsubscribe",
)
_ALLOWED_DATABASE_SURFACE = {
    "get",
    "read_input",
    "read_resource",
    "report_untracked_read",
}
_FORBIDDEN_DATABASE_SURFACE = set(_FORBIDDEN_OPERATIONS) - {"unsubscribe"}

_STATE_INPUT = Input[int]("query-context-state")
_FILES = FileResource()


@query(key="query-context-observed")
def _observed(db: Database) -> int:
    return _STATE_INPUT.read(db)


@query(key="query-context-child")
def _child(db: Database) -> int:
    return _STATE_INPUT.read(db)


class _Hostile:
    def __init__(self) -> None:
        self.touched = False

    def _fail(self) -> NoReturn:
        self.touched = True
        raise AssertionError("hostile argument was evaluated")

    def __iter__(self) -> Iterator[Any]:
        return self._fail()

    def __hash__(self) -> int:
        return self._fail()

    def __repr__(self) -> str:
        return self._fail()

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        return self._fail()

    def get(self, _key: str) -> Any:
        return self._fail()

    def put(self, _key: str, _payload: bytes) -> None:
        self._fail()

    def contains(self, _key: str) -> bool:
        return self._fail()


_QUERY_CONTEXT_TARGET: Database | None = None
_QUERY_CONTEXT_SUBSCRIPTION: Subscription | None = None
_QUERY_CONTEXT_HOSTILE: _Hostile | None = None
_QUERY_CONTEXT_ACTION: Action | None = None
_QUERY_CONTEXT_ACTION_ROOT: Path | None = None


def _dynamic(name: str) -> Any:
    return globals()[name]


def _action_attempt(operation: str) -> Any:
    @query(key=f"query-context-action-{operation}")
    def invoke(db: Database) -> object:
        selected = cast(Action, _dynamic("_QUERY_CONTEXT_ACTION"))
        root = cast(Path, _dynamic("_QUERY_CONTEXT_ACTION_ROOT"))
        if operation == "outputs":
            return selected.outputs(db)
        if operation == "plan":
            return selected.plan(db, root=root)
        return selected.reconcile(db, root=root)

    return invoke


def _invoke_forbidden(target: Database, operation: str) -> None:
    hostile = cast(_Hostile, _dynamic("_QUERY_CONTEXT_HOSTILE"))
    if operation == "mode":
        _ = target.mode
    elif operation == "max_query_nodes":
        _ = target.max_query_nodes
    elif operation == "revision":
        _ = target.revision
    elif operation == "statistics":
        target.statistics()
    elif operation == "reset_statistics":
        target.reset_statistics()
    elif operation == "query_profile":
        target.query_profile()
    elif operation == "dependency_graph":
        target.dependency_graph()
    elif operation == "set":
        target.set(hostile, hostile)
    elif operation == "set_many":
        target.set_many(cast(Any, hostile))
    elif operation == "explain":
        target.explain(cast(Any, hostile), hostile)
    elif operation == "inspect":
        target.inspect(cast(Any, hostile), hostile)
    elif operation == "inspect_fresh":
        target.inspect_fresh(cast(Any, hostile), hostile)
    elif operation == "request_span":
        with target.request_span():
            raise AssertionError("request_span entered during a query")
    elif operation == "request_inputs_changed":
        target.request_inputs_changed()
    elif operation == "observe":
        target.observe(cast(Any, hostile), cast(Any, hostile), hostile)
    elif operation == "save_checkpoint":
        target.save_checkpoint(cast(Any, hostile))
    elif operation == "load_checkpoint":
        target.load_checkpoint(cast(Any, hostile), cast(Any, hostile))
    elif operation == "unsubscribe":
        subscription = cast(Subscription, _dynamic("_QUERY_CONTEXT_SUBSCRIPTION"))
        subscription.unsubscribe()
    else:  # pragma: no cover - the parametrization is the closed public inventory
        raise AssertionError(operation)


def _attempt_query(operation: str, target_kind: str) -> Any:
    @query(key=f"query-context-attempt-{operation}-{target_kind}")
    def attempt(db: Database) -> int:
        target = db if target_kind == "same" else cast(Any, _dynamic("_QUERY_CONTEXT_TARGET"))
        _invoke_forbidden(target, operation)
        return 0

    return attempt


def _caught_query(operation: str, target_kind: str) -> Any:
    @query(key=f"query-context-caught-{operation}-{target_kind}")
    def caught(db: Database) -> int:
        target = db if target_kind == "same" else cast(Any, _dynamic("_QUERY_CONTEXT_TARGET"))
        with suppress(QueryContextError):
            _invoke_forbidden(target, operation)
        return _STATE_INPUT.read(db)

    return caught


def _state_without_request_count(db: Database) -> tuple[Any, ...]:
    statistics = db.statistics()
    statistic_values = tuple(
        getattr(statistics, name)
        for name in statistics.__dataclass_fields__
        if name != "total_requests"
    )
    records = tuple(
        sorted(
            (
                key.kind,
                key.identity,
                key.args_digest,
                record.digest,
                record.changed_at,
                record.verified_at,
                record.last_decision,
                tuple(sorted(dep.label for dep in record.dependencies)),
            )
            for key, record in db._records.items()
        )
    )
    observers = tuple(
        sorted(
            (key.label, tuple(id(callback) for callback in callbacks))
            for key, callbacks in db._observers.items()
        )
    )
    return (
        db.revision,
        statistic_values,
        records,
        tuple(sorted(key.label for key in db._query_records)),
        tuple(sorted(db._query_registry)),
        tuple(sorted(key.label for key in db._resource_registry)),
        tuple(sorted(key.label for key in db._call_snapshot_registry)),
        observers,
        db._span_epoch,
        tuple(sorted(key.label for key in db._checkpoint_query_records)),
        tuple(sorted(key.label for key in db._checkpoint_resource_probes)),
    )


def test_public_database_surface_has_an_explicit_query_context_policy() -> None:
    surface = {
        name
        for name, value in vars(Database).items()
        if not name.startswith("_") and (callable(value) or isinstance(value, property))
    }
    assert surface == _ALLOWED_DATABASE_SURFACE | _FORBIDDEN_DATABASE_SURFACE
    subscription_surface = {
        name
        for name, value in vars(Subscription).items()
        if not name.startswith("_") and callable(value)
    }
    assert subscription_surface == {"unsubscribe"}


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("target_kind", ("same", "cross"))
@pytest.mark.parametrize("operation", _FORBIDDEN_OPERATIONS)
def test_administration_is_rejected_before_arguments_or_state_are_touched(
    mode: str, target_kind: str, operation: str
) -> None:
    active = Database(mode=mode)
    other = Database(mode=mode)
    active.set(_STATE_INPUT, 11)
    other.set(_STATE_INPUT, 11)
    target = active if target_kind == "same" else other
    subscription = target.observe(lambda _event: None, _observed)
    hostile = _Hostile()
    globals()["_QUERY_CONTEXT_TARGET"] = target
    globals()["_QUERY_CONTEXT_SUBSCRIPTION"] = subscription
    globals()["_QUERY_CONTEXT_HOSTILE"] = hostile
    state_before = _state_without_request_count(target)
    target_requests_before = target._request_counter

    with pytest.raises(QueryContextError, match="while a query is executing"):
        active.get(_attempt_query(operation, target_kind))

    assert hostile.touched is False
    assert subscription._active is True
    assert _state_without_request_count(target) == state_before
    if target_kind == "cross":
        assert target._request_counter == target_requests_before
    subscription.unsubscribe()


@pytest.mark.parametrize("mode", _MODES)
def test_constructor_rejection_precedes_hostile_configuration(mode: str) -> None:
    hostile = _Hostile()
    globals()["_QUERY_CONTEXT_HOSTILE"] = hostile

    @query(key=f"query-context-constructor-{mode}")
    def constructs(_db: Database) -> int:
        Database(mode=cast(Any, _dynamic("_QUERY_CONTEXT_HOSTILE")))
        return 0

    with pytest.raises(QueryContextError, match=r"Database\(\)"):
        Database(mode=mode).get(constructs)
    assert hostile.touched is False


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("operation", ("outputs", "plan", "reconcile"))
def test_actions_are_rejected_inside_queries_before_desired_or_filesystem_work(
    tmp_path: Path, mode: str, operation: str
) -> None:
    desired_calls = 0

    @action(tool=f"query-context-action-{operation}-{mode}")
    def emit(_db: Database) -> list[Output]:
        nonlocal desired_calls
        desired_calls += 1
        return [Output.text("owned.txt", "owned")]

    root = tmp_path / "root"
    globals()["_QUERY_CONTEXT_ACTION"] = emit
    globals()["_QUERY_CONTEXT_ACTION_ROOT"] = root

    with pytest.raises(QueryContextError, match=f"Action\\.{operation}"):
        Database(mode=mode).get(_action_attempt(operation))

    assert desired_calls == 0
    assert not root.exists()


@pytest.mark.parametrize("mode", _MODES)
def test_same_database_composition_and_managed_reads_remain_allowed(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "allowed.txt"
    path.write_text("tracked", encoding="utf-8")
    db = Database(mode=mode)
    db.set(_STATE_INPUT, 7)

    @query(key=f"query-context-allowed-{mode}")
    def allowed(db: Database) -> tuple[int, int, str]:
        db.report_untracked_read("explicit test observation")
        return db.get(_child), db.read_input(_STATE_INPUT), db.read_resource(_FILES, str(path))

    assert db.get(allowed) == (7, 7, "tracked")


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("read_kind", ("query", "input", "resource", "report"))
def test_cross_database_managed_reads_fail_before_target_work(
    mode: str, read_kind: str, tmp_path: Path
) -> None:
    path = tmp_path / "cross.txt"
    path.write_text("tracked", encoding="utf-8")
    active = Database(mode=mode)
    target = Database(mode=mode)
    active.set(_STATE_INPUT, 1)
    target.set(_STATE_INPUT, 2)
    globals()["_QUERY_CONTEXT_TARGET"] = target
    requests_before = target._request_counter
    statistics_before = target.statistics()

    @query(key=f"query-context-cross-read-{read_kind}-{mode}")
    def cross_read(_db: Database) -> Any:
        other = cast(Any, _dynamic("_QUERY_CONTEXT_TARGET"))
        if read_kind == "query":
            return other.get(_child)
        if read_kind == "input":
            return other.read_input(_STATE_INPUT)
        if read_kind == "resource":
            return other.read_resource(_FILES, str(path))
        other.report_untracked_read("wrong database")
        return 0

    with pytest.raises(QueryContextError, match="different Database"):
        active.get(cross_read)
    assert target._request_counter == requests_before
    assert target.statistics() == statistics_before


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("target_kind", ("same", "cross"))
@pytest.mark.parametrize("operation", _FORBIDDEN_OPERATIONS)
def test_caught_administration_errors_remain_warm_fresh_consistent(
    mode: str, target_kind: str, operation: str
) -> None:
    warm = Database(mode=mode)
    other = Database(mode=mode)
    warm.set(_STATE_INPUT, 1)
    other.set(_STATE_INPUT, 10)
    target = warm if target_kind == "same" else other
    subscription = target.observe(lambda _event: None, _observed)
    globals()["_QUERY_CONTEXT_TARGET"] = target
    globals()["_QUERY_CONTEXT_SUBSCRIPTION"] = subscription
    globals()["_QUERY_CONTEXT_HOSTILE"] = _Hostile()
    caught = _caught_query(operation, target_kind)
    assert warm.get(caught) == 1

    warm.set(_STATE_INPUT, 2)
    fresh = Database(mode=mode)
    fresh.set(_STATE_INPUT, 2)
    fresh_other = Database(mode=mode)
    fresh_other.set(_STATE_INPUT, 10)
    fresh_target = fresh if target_kind == "same" else fresh_other
    fresh_subscription = fresh_target.observe(lambda _event: None, _observed)
    globals()["_QUERY_CONTEXT_TARGET"] = fresh_target
    globals()["_QUERY_CONTEXT_SUBSCRIPTION"] = fresh_subscription
    globals()["_QUERY_CONTEXT_HOSTILE"] = _Hostile()
    assert warm.get(caught) == fresh.get(caught) == 2
    subscription.unsubscribe()
    fresh_subscription.unsubscribe()


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("operation", _FORBIDDEN_OPERATIONS)
def test_caught_administration_errors_are_sound_after_same_mode_checkpoint(
    mode: str, operation: str
) -> None:
    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    writer.set(_STATE_INPUT, 3)
    subscription = writer.observe(lambda _event: None, _observed)
    globals()["_QUERY_CONTEXT_SUBSCRIPTION"] = subscription
    globals()["_QUERY_CONTEXT_HOSTILE"] = _Hostile()
    caught = _caught_query(operation, "same")
    assert writer.get(caught) == 3
    checkpoint = writer.save_checkpoint()

    warmed = Database(mode=mode, store=store)
    warmed.set(_STATE_INPUT, 3)
    warmed_subscription = warmed.observe(lambda _event: None, _observed)
    globals()["_QUERY_CONTEXT_SUBSCRIPTION"] = warmed_subscription
    globals()["_QUERY_CONTEXT_HOSTILE"] = _Hostile()
    warmed.load_checkpoint(checkpoint)
    assert warmed.get(caught) == 3
    warmed.set(_STATE_INPUT, 4)

    fresh = Database(mode=mode)
    fresh.set(_STATE_INPUT, 4)
    fresh_subscription = fresh.observe(lambda _event: None, _observed)
    globals()["_QUERY_CONTEXT_SUBSCRIPTION"] = fresh_subscription
    globals()["_QUERY_CONTEXT_HOSTILE"] = _Hostile()
    assert warmed.get(caught) == fresh.get(caught) == 4
    subscription.unsubscribe()
    warmed_subscription.unsubscribe()
    fresh_subscription.unsubscribe()


@dataclass(frozen=True)
class _AdministrativeHookResource(Resource[str, str, str]):
    operation: str

    def identity(self) -> tuple[str, str]:
        return "query-context-hook", self.operation

    def label(self, key: str) -> str:
        return f"query-context-hook[{key}]"

    def probe(self, key: str) -> str:
        if self.operation == "statistics":
            cast(Any, _dynamic("_QUERY_CONTEXT_TARGET")).statistics()
        else:
            Database(mode=cast(Any, _dynamic("_QUERY_CONTEXT_HOSTILE")))
        return key

    def load(self, _db: Database, key: str) -> str:
        return key


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("operation", ("statistics", "construct"))
def test_resource_hook_error_precedes_query_context_error(mode: str, operation: str) -> None:
    db = Database(mode=mode)
    globals()["_QUERY_CONTEXT_TARGET"] = db
    hostile = _Hostile()
    globals()["_QUERY_CONTEXT_HOSTILE"] = hostile
    resource = _AdministrativeHookResource(operation)

    @query(key=f"query-context-hook-precedence-{operation}-{mode}")
    def reads(db: Database) -> str:
        return resource.read(db, "key")

    with pytest.raises(ResourceDependencyError, match="Resource hook"):
        db.get(reads)
    assert hostile.touched is False


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("policy_kind", ("eq", "cutoff"))
def test_query_policies_cannot_administer_the_database(mode: str, policy_kind: str) -> None:
    source = Input[int](f"query-context-policy-source-{mode}-{policy_kind}")
    globals()["_QUERY_CONTEXT_TARGET"] = None

    def policy(*values: int) -> Any:
        target = cast(Any, _dynamic("_QUERY_CONTEXT_TARGET"))
        target.statistics()
        return values[0] if policy_kind == "cutoff" else values[0] == values[1]

    decorated = query(eq=policy) if policy_kind == "eq" else query(cutoff=policy)

    @decorated
    def value(db: Database) -> int:
        return source.read(db)

    db = Database(mode=mode)
    globals()["_QUERY_CONTEXT_TARGET"] = db
    db.set(source, 1)
    assert db.get(value) == 1
    key, _ = db._query_key(value, (), {})
    digest_before = db._records[key].digest
    db.set(source, 2)
    with pytest.raises(QueryContextError, match="Database.statistics"):
        db.get(value)
    assert db._records[key].digest == digest_before
