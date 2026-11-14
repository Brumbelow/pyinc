"""The fold of a class walks a copy of the class dictionary.

The fold asks each attribute of a class what it is. An object can answer
`__class__` by running code, as a lazy proxy does, and that code can add an
attribute to the class being walked. On 3.14 another thread does the same when
it first reads the annotations of a class. A walk over the live dictionary then
raises RuntimeError. The attribute below adds one to its owner every time it is
asked, so the change lands inside the walk on every run.
"""

from __future__ import annotations

from typing import Any

from pyinc import Database, Resource, query


class _GrowsItsOwner:
    def __set_name__(self, owner: type[Any], name: str) -> None:
        self.owner = owner
        self.added = 0

    @property  # type: ignore[misc]
    def __class__(self) -> type[Any]:
        self.added += 1
        setattr(self.owner, f"added_{self.added}", self.added)
        return _GrowsItsOwner


class _Holder(Resource[str, str, tuple[str]]):
    grows = _GrowsItsOwner()

    def identity(self) -> Any:
        return ("holder",)

    def probe(self, key: str) -> tuple[str]:
        return ("present",)

    def load(self, db: Database, key: str) -> str:
        return key

    def label(self, key: str) -> str:
        return f"holder[{key}]"


_HOLDER = _Holder()


@query
def _reads_holder(db: Database) -> str:
    return _HOLDER.read(db, "k")


def test_a_class_that_gains_attributes_while_it_is_folded_still_answers() -> None:
    db = Database()
    assert db.get(_reads_holder) == "k"
    assert _Holder.grows.added > 0
    assert db.get(_reads_holder) == "k"
    assert Database().get(_reads_holder) == "k"
