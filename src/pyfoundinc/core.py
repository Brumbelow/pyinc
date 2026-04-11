from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable, Generic, TypeVar

from .value import semantic_equal

T = TypeVar("T")
EqFn = Callable[[Any, Any], bool]


@dataclass(frozen=True)
class Input(Generic[T]):
    name: str
    eq: EqFn | None = None

    def read(self, db: "Database") -> T:
        return db._read_input(self)


class Query(Generic[T]):
    def __init__(self, fn: Callable[..., T], *, eq: EqFn | None = None) -> None:
        self.fn = fn
        self.eq = eq
        self.query_id = f"{fn.__module__}:{fn.__qualname__}"
        self.__name__ = fn.__name__
        self.__doc__ = fn.__doc__
        self.__module__ = fn.__module__
        self.__wrapped__ = fn
        wraps(fn)(self)

    def __call__(self, db: "Database", *args: Any, **kwargs: Any) -> T:
        return db.get(self, *args, **kwargs)

    def compare(self, old_value: Any, new_value: Any) -> bool:
        comparator = self.eq or semantic_equal
        return comparator(old_value, new_value)


def query(fn: Callable[..., T] | None = None, *, eq: EqFn | None = None) -> Query[T] | Callable[[Callable[..., T]], Query[T]]:
    if fn is None:
        return lambda wrapped: Query(wrapped, eq=eq)
    return Query(fn, eq=eq)


from .runtime import Database  # noqa: E402
