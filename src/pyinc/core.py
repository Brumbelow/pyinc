from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import (
    TYPE_CHECKING,
    Any,
    Concatenate,
    Generic,
    ParamSpec,
    TypeVar,
    overload,
)

from .value import semantic_equal

if TYPE_CHECKING:
    from .runtime import Database

P = ParamSpec("P")
T = TypeVar("T")
EqFn = Callable[[Any, Any], bool]
CutoffFn = Callable[[Any], Any]


@dataclass(frozen=True)
class Input(Generic[T]):
    name: str
    eq: EqFn | None = None
    cutoff: CutoffFn | None = None

    def __post_init__(self) -> None:
        if self.eq is not None and self.cutoff is not None:
            raise ValueError("Input() accepts either eq= or cutoff=, but not both.")

    def read(self, db: Database) -> T:
        return db._read_input(self)


class Query(Generic[P, T]):
    def __init__(
        self,
        fn: Callable[Concatenate[Database, P], T],
        *,
        eq: EqFn | None = None,
        cutoff: CutoffFn | None = None,
    ) -> None:
        if eq is not None and cutoff is not None:
            raise ValueError("@query accepts either eq= or cutoff=, but not both.")
        self.fn = fn
        self.eq = eq
        self.cutoff = cutoff
        self.query_id = f"{fn.__module__}:{fn.__qualname__}"
        self.__name__ = fn.__name__
        self.__doc__ = fn.__doc__
        self.__module__ = fn.__module__
        self.__wrapped__ = fn
        wraps(fn)(self)

    def __call__(self, db: Database, *args: P.args, **kwargs: P.kwargs) -> T:
        return db.get(self, *args, **kwargs)

    def compare(self, old_value: Any, new_value: Any) -> bool:
        if self.cutoff is not None:
            return semantic_equal(self.cutoff(old_value), self.cutoff(new_value))
        comparator = self.eq or semantic_equal
        return comparator(old_value, new_value)


@overload
def query(
    fn: Callable[Concatenate[Database, P], T],
    *,
    eq: EqFn | None = None,
    cutoff: CutoffFn | None = None,
) -> Query[P, T]: ...


@overload
def query(
    fn: None = None,
    *,
    eq: EqFn | None = None,
    cutoff: CutoffFn | None = None,
) -> Callable[[Callable[Concatenate[Database, P], T]], Query[P, T]]: ...


def query(
    fn: Callable[Concatenate[Database, P], T] | None = None,
    *,
    eq: EqFn | None = None,
    cutoff: CutoffFn | None = None,
) -> Query[P, T] | Callable[[Callable[Concatenate[Database, P], T]], Query[P, T]]:
    def decorate(wrapped: Callable[Concatenate[Database, P], T]) -> Query[P, T]:
        return Query(wrapped, eq=eq, cutoff=cutoff)

    if fn is None:
        return decorate
    return decorate(fn)
