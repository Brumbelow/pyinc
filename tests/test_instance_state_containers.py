from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from pyinc import Database, Resource, UnsupportedValueError, query

_MODES = ("strict", "checked", "fast")


class _SneakyState(dict[str, object]):
    def __init__(self, marker: object) -> None:
        super().__init__(marker=marker)
        self.current = marker

    def __getitem__(self, key: str) -> object:
        if key == "marker":
            return self.current
        return super().__getitem__(key)


class _Policy:
    def __call__(self, left: object, right: object) -> bool:
        return left == right


class _Adapted:
    def __init__(self, value: int) -> None:
        self.value = value


class _Adapter:
    def freeze(self, value: _Adapted, freeze_value: Any) -> Any:
        return freeze_value(value.value + int(self.__dict__["marker"] is not None))

    def thaw(self, snapshot: Any, thaw_value: Any) -> _Adapted:
        return _Adapted(int(thaw_value(snapshot)))


@dataclass(frozen=True)
class _ConfiguredResource(Resource[str, int, int]):
    marker: object

    def label(self, key: str) -> str:
        return f"state-container[{key}]"

    def probe(self, key: str) -> int:
        del key
        return id(self.__dict__["marker"])

    def load(self, db: Database, key: str) -> int:
        del db, key
        return id(self.__dict__["marker"])


@pytest.mark.parametrize("mode", _MODES)
def test_policy_instance_dict_subclass_is_rejected_before_caching(mode: str) -> None:
    policy = _Policy()
    policy.__dict__ = _SneakyState(tuple([1]))

    @query(key=f"policy-state-dict-subclass-{mode}", eq=policy)
    def value(db: Database) -> int:
        del db
        return 1

    database = Database(mode=mode)
    with pytest.raises(UnsupportedValueError, match="cannot be fingerprinted"):
        database.get(value)
    assert database.statistics().node_count == 0


@pytest.mark.parametrize("mode", _MODES)
def test_adapter_instance_dict_subclass_is_rejected_at_database_boundary(mode: str) -> None:
    adapter = _Adapter()
    adapter.__dict__ = _SneakyState(tuple([1]))
    with pytest.raises(UnsupportedValueError, match="exact dict"):
        Database(mode=mode, adapters={_Adapted: adapter})


@pytest.mark.parametrize("mode", _MODES)
def test_resource_instance_dict_subclass_is_rejected_before_caching(mode: str) -> None:
    marker = tuple([1])
    resource = _ConfiguredResource(marker)
    object.__setattr__(resource, "__dict__", _SneakyState(marker))

    @query(key=f"resource-state-dict-subclass-{mode}")
    def value(db: Database) -> int:
        return resource.read(db, "key")

    database = Database(mode=mode)
    with pytest.raises(UnsupportedValueError, match="exact dict"):
        database.get(value)
    assert database.statistics().node_count == 0
