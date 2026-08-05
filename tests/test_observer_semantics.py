from __future__ import annotations

from typing import Literal, TypeAlias

import pytest

from pyinc import (
    Database,
    InMemoryArtifactStore,
    Input,
    QueryChangeEvent,
    Subscription,
    freeze,
    query,
    serialize_snapshot,
)

Mode: TypeAlias = Literal["strict", "checked", "fast"]
_MODES: tuple[Mode, ...] = ("strict", "checked", "fast")


class _EqualCallback:
    def __init__(self, label: str, calls: list[str]) -> None:
        self.label = label
        self.calls = calls

    def __call__(self, _event: QueryChangeEvent) -> None:
        self.calls.append(self.label)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _EqualCallback)


def _observable(value: object) -> bytes:
    return serialize_snapshot(freeze(value))


@pytest.mark.parametrize("mode", _MODES)
def test_observer_requires_a_prior_policy_distinct_value(mode: Mode) -> None:
    source = Input[int](f"observer-value-change-{mode}")

    @query(key=f"observer-value-change-result-{mode}")
    def observed(db: Database) -> list[int]:
        return [source.read(db)]

    db = Database(mode=mode)
    db.set(source, 1)
    events: list[QueryChangeEvent] = []
    db.observe(events.append, observed)

    assert _observable(db.get(observed)) == _observable([1])
    assert events == []
    db.inspect_fresh(observed)
    db.set(source, 1)
    assert _observable(db.get(observed)) == _observable([1])
    assert events == []

    db.set(source, 2)
    assert _observable(db.get(observed)) == _observable([2])
    assert len(events) == 1
    assert events[0].query_id == observed.key
    assert events[0].decision == "executed"
    assert events[0].changed_at == events[0].verified_at

    assert _observable(db.get(observed)) == _observable([2])
    db.inspect_fresh(observed)
    assert len(events) == 1


@pytest.mark.parametrize("mode", _MODES)
def test_unchanged_untracked_execution_is_not_a_value_change(mode: Mode) -> None:
    source = Input[int](f"observer-untracked-{mode}")

    @query(key=f"observer-untracked-result-{mode}")
    def observed(db: Database) -> int:
        db.report_untracked_read("test observation")
        return source.read(db)

    db = Database(mode=mode)
    db.set(source, 1)
    events: list[QueryChangeEvent] = []
    db.observe(events.append, observed)

    assert db.get(observed) == 1
    assert db.get(observed) == 1
    assert events == []

    db.set(source, 2)
    assert db.get(observed) == 2
    assert len(events) == 1
    assert db.get(observed) == 2
    assert len(events) == 1


@pytest.mark.parametrize("mode", _MODES)
def test_unsubscribe_uses_subscription_identity_not_callback_equality(mode: Mode) -> None:
    source = Input[int](f"observer-registration-{mode}")

    @query(key=f"observer-registration-result-{mode}")
    def observed(db: Database) -> int:
        return source.read(db)

    db = Database(mode=mode)
    db.set(source, 1)
    assert db.get(observed) == 1

    calls: list[str] = []
    first = _EqualCallback("first", calls)
    second = _EqualCallback("second", calls)
    assert first == second
    first_subscription = db.observe(first, observed)
    second_subscription = db.observe(second, observed)

    second_subscription.unsubscribe()
    second_subscription.unsubscribe()
    db.set(source, 2)
    assert db.get(observed) == 2
    assert calls == ["first"]

    first_subscription.unsubscribe()
    db.set(source, 3)
    assert db.get(observed) == 3
    assert calls == ["first"]


@pytest.mark.parametrize("mode", _MODES)
def test_event_recipient_snapshot_excludes_late_subscribers(mode: Mode) -> None:
    source = Input[int](f"observer-late-{mode}")

    @query(key=f"observer-late-result-{mode}")
    def observed(db: Database) -> int:
        return source.read(db)

    db = Database(mode=mode)
    db.set(source, 1)
    assert db.get(observed) == 1
    early_events: list[QueryChangeEvent] = []
    late_events: list[QueryChangeEvent] = []
    db.observe(early_events.append, observed)

    with db.request_span():
        db.set(source, 2)
        assert db.get(observed) == 2
        db.observe(late_events.append, observed)
        db.set(source, 3)
        assert db.get(observed) == 3
        assert early_events == late_events == []

    assert len(early_events) == 2
    assert len(late_events) == 1
    assert late_events[0] == early_events[1]


@pytest.mark.parametrize("mode", _MODES)
def test_unsubscribe_after_occurrence_keeps_already_captured_event(mode: Mode) -> None:
    source = Input[int](f"observer-captured-{mode}")

    @query(key=f"observer-captured-result-{mode}")
    def observed(db: Database) -> int:
        return source.read(db)

    db = Database(mode=mode)
    db.set(source, 1)
    assert db.get(observed) == 1
    events: list[QueryChangeEvent] = []
    subscription = db.observe(events.append, observed)

    with db.request_span():
        db.set(source, 2)
        assert db.get(observed) == 2
        subscription.unsubscribe()
        assert events == []

    assert len(events) == 1
    db.set(source, 3)
    assert db.get(observed) == 3
    assert len(events) == 1


@pytest.mark.parametrize("mode", _MODES)
def test_unsubscribe_during_dispatch_keeps_the_complete_captured_batch(mode: Mode) -> None:
    source = Input[int](f"observer-dispatch-{mode}")

    @query(key=f"observer-dispatch-result-{mode}")
    def observed(db: Database) -> int:
        return source.read(db)

    db = Database(mode=mode)
    db.set(source, 1)
    assert db.get(observed) == 1

    calls: list[str] = []
    subscriptions: list[Subscription] = []

    def unsubscribe_both(_event: QueryChangeEvent) -> None:
        calls.append("first")
        for subscription in subscriptions:
            subscription.unsubscribe()

    def sibling(_event: QueryChangeEvent) -> None:
        calls.append("second")

    subscriptions.extend(
        (
            db.observe(unsubscribe_both, observed),
            db.observe(sibling, observed),
        )
    )

    with db.request_span():
        db.set(source, 2)
        assert db.get(observed) == 2
        db.set(source, 3)
        assert db.get(observed) == 3
        assert calls == []

    assert calls == ["first", "second", "first", "second"]
    db.set(source, 4)
    assert db.get(observed) == 4
    assert calls == ["first", "second", "first", "second"]


@pytest.mark.parametrize("mode", _MODES)
def test_checkpoint_warm_observer_history_matches_fresh(mode: Mode) -> None:
    source = Input[int](f"observer-checkpoint-{mode}")

    @query(key=f"observer-checkpoint-result-{mode}")
    def observed(db: Database) -> list[int]:
        return [source.read(db)]

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    writer.set(source, 1)
    writer_events: list[QueryChangeEvent] = []
    writer.observe(writer_events.append, observed)
    assert _observable(writer.get(observed)) == _observable([1])
    assert writer_events == []

    with writer.request_span():
        writer.set(source, 2)
        assert _observable(writer.get(observed)) == _observable([2])
        checkpoint = writer.save_checkpoint()
        assert writer_events == []
    assert len(writer_events) == 1

    warmed = Database(mode=mode)
    warmed.set(source, 2)
    warmed_events: list[QueryChangeEvent] = []
    warmed.observe(warmed_events.append, observed)
    warmed.load_checkpoint(checkpoint, store)
    assert _observable(warmed.get(observed)) == _observable([2])
    assert warmed_events == []
    assert warmed.statistics().query_executions == 0

    fresh = Database(mode=mode)
    fresh.set(source, 2)
    assert _observable(fresh.get(observed)) == _observable([2])
    fresh_events: list[QueryChangeEvent] = []
    fresh.observe(fresh_events.append, observed)

    for value in (2, 3, 3, 4):
        warmed.set(source, value)
        fresh.set(source, value)
        assert _observable(warmed.get(observed)) == _observable(fresh.get(observed))
        assert warmed_events == fresh_events

    assert len(warmed_events) == 2
