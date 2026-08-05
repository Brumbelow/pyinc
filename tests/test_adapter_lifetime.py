from __future__ import annotations

from typing import Any, cast

import pytest

from pyinc import (
    Database,
    FrozenAdapterValue,
    FrozenList,
    InMemoryArtifactStore,
    Input,
    UnsupportedValueError,
    query,
)


class _ScaledValue:
    def __init__(self, value: float) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _ScaledValue) and self.value == other.value


class _ScalingAdapter:
    def __init__(self, scale: float) -> None:
        self.scale = scale

    def freeze(self, value: _ScaledValue, freeze_value: Any) -> Any:
        return freeze_value(value.value * self.scale)

    def thaw(self, snapshot: Any, thaw_value: Any) -> _ScaledValue:
        return _ScaledValue(float(thaw_value(snapshot)) / self.scale)


class _AliasedStateAdapter:
    def __init__(self) -> None:
        shared = FrozenList((1,))
        self.a = shared
        self.b = shared

    def freeze(self, value: _ScaledValue, freeze_value: Any) -> Any:
        adjustment = 0.0 if self.a is self.b else 100.0
        return freeze_value(value.value + adjustment)

    def thaw(self, snapshot: Any, thaw_value: Any) -> _ScaledValue:
        adjustment = 0.0 if self.a is self.b else 100.0
        return _ScaledValue(float(thaw_value(snapshot)) - adjustment)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_adapter_configuration_change_is_rejected_before_warm_reuse(mode: str) -> None:
    source = Input[float](f"adapter-lifetime-{mode}")

    @query(key=f"adapter-lifetime-result-{mode}")
    def adapted(db: Database) -> _ScaledValue:
        return _ScaledValue(source.read(db))

    adapter = _ScalingAdapter(2.0)
    database = Database(mode=cast(Any, mode), adapters={_ScaledValue: adapter})
    database.set(source, 5.0)
    first = database.get(adapted)
    if mode == "strict":
        assert isinstance(first, FrozenAdapterValue)
        assert first.payload == 10.0
    else:
        assert first == _ScaledValue(5.0)

    adapter.scale = 5.0
    with pytest.raises(
        UnsupportedValueError,
        match="ValueAdapter configuration changed after Database construction",
    ):
        database.get(adapted)

    # The same configuration is valid when it is pinned by a new Database.
    fresh = Database(mode=cast(Any, mode), adapters={_ScaledValue: adapter})
    fresh.set(source, 5.0)
    current = fresh.get(adapted)
    if mode == "strict":
        assert isinstance(current, FrozenAdapterValue)
        assert current.payload == 25.0
    else:
        assert current == _ScaledValue(5.0)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_adapter_configuration_change_cannot_persist_or_restore_stale_state(mode: str) -> None:
    source = Input[float](f"adapter-checkpoint-lifetime-{mode}")

    @query(key=f"adapter-checkpoint-result-{mode}")
    def adapted(db: Database) -> _ScaledValue:
        return _ScaledValue(source.read(db))

    adapter = _ScalingAdapter(2.0)
    store = InMemoryArtifactStore()
    database = Database(
        mode=cast(Any, mode),
        adapters={_ScaledValue: adapter},
        store=store,
    )
    database.set(source, 5.0)
    database.get(adapted)
    checkpoint = database.save_checkpoint()
    revision = database.revision
    statistics = database.statistics()
    objects = dict(store.keys())

    adapter.scale = 5.0
    for operation in (
        lambda: database.set(source, 6.0),
        database.save_checkpoint,
        lambda: database.load_checkpoint(checkpoint),
    ):
        with pytest.raises(
            UnsupportedValueError,
            match="ValueAdapter configuration changed after Database construction",
        ):
            operation()

    assert database.revision == revision
    assert database.statistics() == statistics
    assert dict(store.keys()) == objects


def test_adapter_must_be_fingerprintable_when_database_is_constructed() -> None:
    class _SlottedAdapter:
        __slots__ = ("scale",)

        def __init__(self) -> None:
            self.scale = 2.0

        def freeze(self, value: _ScaledValue, freeze_value: Any) -> Any:
            return freeze_value(value.value * self.scale)

        def thaw(self, snapshot: Any, thaw_value: Any) -> _ScaledValue:
            return _ScaledValue(float(thaw_value(snapshot)) / self.scale)

    with pytest.raises(UnsupportedValueError, match="cannot be fingerprinted safely"):
        Database(adapters={_ScaledValue: _SlottedAdapter()})


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_adapter_alias_topology_change_is_rejected_before_warm_or_checkpoint_reuse(
    mode: str,
) -> None:
    source = Input[float](f"adapter-alias-topology-{mode}")

    @query(key=f"adapter-alias-topology-result-{mode}")
    def adapted(db: Database) -> _ScaledValue:
        return _ScaledValue(source.read(db))

    adapter = _AliasedStateAdapter()
    store = InMemoryArtifactStore()
    database = Database(
        mode=cast(Any, mode),
        adapters={_ScaledValue: adapter},
        store=store,
    )
    database.set(source, 1.0)
    database.get(adapted)
    checkpoint = database.save_checkpoint()

    adapter.b = FrozenList((1,))

    for operation in (database.get,):
        with pytest.raises(
            UnsupportedValueError,
            match="ValueAdapter configuration changed after Database construction",
        ):
            operation(adapted)
    with pytest.raises(
        UnsupportedValueError,
        match="ValueAdapter configuration changed after Database construction",
    ):
        database.load_checkpoint(checkpoint)

    fresh = Database(mode=cast(Any, mode), adapters={_ScaledValue: adapter})
    fresh.set(source, 1.0)
    result = fresh.get(adapted)
    if mode == "strict":
        assert isinstance(result, FrozenAdapterValue)
        assert result.payload == 101.0
    else:
        assert result == _ScaledValue(1.0)
