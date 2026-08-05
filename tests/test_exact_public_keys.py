from __future__ import annotations

import pytest

from pyinc import Database, Input, InputKeyError, query


class _StatefulString(str):
    state: int

    def __new__(cls, value: str, state: int) -> _StatefulString:
        instance = super().__new__(cls, value)
        instance.state = state
        return instance


def test_input_rejects_stateful_string_subclass_key() -> None:
    with pytest.raises(InputKeyError, match="exact str"):
        Input[int](_StatefulString("same-spelling", 1))


def test_query_rejects_stateful_string_subclass_key() -> None:
    with pytest.raises(ValueError, match="exact str"):

        @query(key=_StatefulString("same-spelling", 1))
        def keyed_query(_db: Database) -> int:
            return 1


def test_plain_string_keys_remain_exact_and_operational() -> None:
    source = Input[int]("plain-input")

    @query(key="plain-query")
    def read_source(db: Database) -> int:
        return source.read(db)

    db = Database()
    db.set(source, 3)
    assert type(source.key) is str
    assert type(read_source.key) is str
    assert db.get(read_source) == 3
