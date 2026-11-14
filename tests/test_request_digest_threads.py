"""A request keeps the resource digests it read to itself.

The memo guard reads each resource's identity once per request and keeps the
digest for the rest of that request. A request belongs to the thread that
opened it, so a digest kept by a span on one thread must not answer for a call
on another. The cell holds a span open on one thread while another thread
changes a resource's identity, and checks each answer against a fresh database.
"""

from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass, field
from typing import Any

from _rendezvous import run_in_threads

from pyinc import Database, QueryChangeEvent, Resource, query

_WAIT = 10.0


@dataclass
class _VersionedResource(Resource[str, str, tuple[str]]):
    """Declares its identity from a version a caller moves in place.

    The probe never moves, so only the identity arm of the memo guard sees a
    new version.
    """

    version: list[int] = field(default_factory=lambda: [1])

    def identity(self) -> Any:
        return ("version", self.version[0])

    def probe(self, key: str) -> tuple[str]:
        return ("present",)

    def load(self, db: Database, key: str) -> str:
        return f"{key}@{self.version[0]}"

    def label(self, key: str) -> str:
        return f"versioned[{key}]"


def _fresh_observed(observed: Any) -> tuple[str, list[str]]:
    fresh = Database()
    events: list[QueryChangeEvent] = []
    fresh.observe(events.append, observed)
    return fresh.get(observed), [event.decision for event in events]


def test_a_span_keeps_its_resource_digests_from_other_threads() -> None:
    resource = _VersionedResource()

    @query
    def observed_cell(db: Database) -> str:
        return resource.read(db, "cell")

    @query
    def declared_cell(db: Database) -> str:
        return resource.read(db, "cell")

    db = Database()
    assert db.get(observed_cell) == "cell@1"
    assert db.get(declared_cell) == "cell@1"

    span_read = threading.Event()
    declared = threading.Event()

    def hold_span() -> tuple[str, str, str]:
        with db.request_span():
            first = db.get(observed_cell)
            second = db.get(declared_cell)
            span_read.set()
            assert declared.wait(_WAIT)
            # The change was declared, so the span reads the world again.
            after = db.get(declared_cell)
        return first, second, after

    def change_identity() -> tuple[tuple[str, list[str]], tuple[str, list[str]]]:
        try:
            assert span_read.wait(_WAIT)
            # The span is its own thread's promise. This thread holds no
            # request, so it is free to change the world without declaring it.
            resource.version[0] = 2
            events: list[QueryChangeEvent] = []
            db.observe(events.append, observed_cell)
            warm = (db.get(observed_cell), [event.decision for event in events])
            fresh = _fresh_observed(observed_cell)
            resource.version[0] = 3
            db.request_inputs_changed()
        finally:
            declared.set()
        return warm, fresh

    held, changed = run_in_threads(hold_span, change_identity)
    assert held == ("cell@1", "cell@1", "cell@3")
    # The warm subscription sees the cold execution a fresh one sees.
    assert changed == (("cell@2", ["executed"]),) * 2
    assert Database().get(declared_cell) == "cell@3"


def _settled_database(cell: Any) -> Database:
    # On 3.14 the first fold of a resource class can add annotation cache
    # entries to a base class dictionary, so the first memo entry misses once.
    # A second read leaves an entry the span's guard then matches.
    db = Database()
    db.get(cell)
    db.get(cell)
    return db


def test_observe_in_a_span_catches_up_to_a_change_declared_on_another_thread() -> None:
    resource = _VersionedResource()

    @query
    def cell(db: Database) -> str:
        return resource.read(db, "cell")

    db = _settled_database(cell)
    span_read = threading.Event()
    declared = threading.Event()

    def hold_span() -> tuple[str, list[str]]:
        events: list[QueryChangeEvent] = []
        with db.request_span():
            db.get(cell)
            span_read.set()
            assert declared.wait(_WAIT)
            # The span still holds the digest it read before the change.
            db.observe(events.append, cell)
            after = db.get(cell)
        return after, [event.decision for event in events]

    def declare_change() -> None:
        try:
            assert span_read.wait(_WAIT)
            resource.version[0] = 2
            db.request_inputs_changed()
        finally:
            declared.set()

    held, _ = run_in_threads(hold_span, declare_change)
    assert held == ("cell@2", ["executed"])
    assert held == _fresh_observed(cell)


def test_observe_in_a_span_catches_up_to_a_change_declared_from_another_context() -> None:
    resource = _VersionedResource()

    @query
    def cell(db: Database) -> str:
        return resource.read(db, "cell")

    db = _settled_database(cell)
    events: list[QueryChangeEvent] = []
    with db.request_span():
        db.get(cell)
        resource.version[0] = 2
        # This context never saw the span open, so it holds no request.
        contextvars.Context().run(db.request_inputs_changed)
        db.observe(events.append, cell)
        after = db.get(cell)
    held = (after, [event.decision for event in events])
    assert held == ("cell@2", ["executed"])
    assert held == _fresh_observed(cell)
