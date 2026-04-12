from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from pyfoundinc import (
    Database,
    FrozenAdapterValue,
    Input,
    UnsupportedValueError,
    ValueAdapter,
    freeze,
    query,
    thaw,
)


@dataclass(frozen=True)
class Point:
    x: int
    y: int


class PointAdapter(ValueAdapter):
    def freeze(self, value: Point, freeze: Any) -> object:
        assert callable(freeze)
        return {"x": value.x, "y": value.y}

    def thaw(self, snapshot: Any, thaw: Any) -> Point:
        assert callable(thaw)
        data = cast(dict[str, Any], snapshot)
        return Point(
            x=thaw(data["x"]),
            y=thaw(data["y"]),
        )


@dataclass(frozen=True)
class Key:
    name: str

    def __repr__(self) -> str:
        raise RuntimeError("repr should not be used during freeze ordering")


class KeyAdapter(ValueAdapter):
    def freeze(self, value: Key, freeze: Any) -> object:
        assert callable(freeze)
        return {"name": value.name}

    def thaw(self, snapshot: Any, thaw: Any) -> Key:
        assert callable(thaw)
        data = cast(dict[str, Any], snapshot)
        return Key(name=thaw(data["name"]))


def test_freeze_rejects_cyclic_values() -> None:
    payload: list[object] = []
    payload.append(payload)

    with pytest.raises(UnsupportedValueError, match="Cyclic values"):
        freeze(payload)


def test_freeze_uses_canonical_sorting_without_repr() -> None:
    adapters = {Key: KeyAdapter()}
    snapshot = freeze({Key("b"): 1, Key("a"): 2}, adapters=adapters)
    round_trip = thaw(snapshot, adapters=adapters)
    assert round_trip == {Key("a"): 2, Key("b"): 1}


def test_adapter_round_trip_works_for_freeze_and_thaw() -> None:
    adapters = {Point: PointAdapter()}
    snapshot = freeze(Point(1, 2), adapters=adapters)

    assert isinstance(snapshot, FrozenAdapterValue)
    assert thaw(snapshot, adapters=adapters) == Point(1, 2)


def test_database_uses_adapters_for_boundary_values() -> None:
    adapters = {Point: PointAdapter()}
    payload = Input[Point]("payload")

    @query
    def total(db: Database) -> int:
        point = payload.read(db)
        assert isinstance(point, Point)
        return point.x + point.y

    db = Database(mode="checked", adapters=adapters)
    db.set(payload, Point(2, 3))
    assert db.get(total) == 5
    key, _ = db._query_key(total, (), {})
    assert db._records[key].last_decision == "executed"

    db.set(payload, Point(2, 3))
    assert db.get(total) == 5
    assert db._records[key].last_decision == "reused"
