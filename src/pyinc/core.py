from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from inspect import isasyncgenfunction, iscoroutinefunction, isgeneratorfunction
from types import FunctionType
from typing import (
    TYPE_CHECKING,
    Any,
    Concatenate,
    Generic,
    ParamSpec,
    TypeVar,
    cast,
    final,
    overload,
)

from ._runtime_types import register_core_types
from .errors import InputKeyError
from .value import semantic_equal

if TYPE_CHECKING:
    from .runtime import Database

P = ParamSpec("P")
T = TypeVar("T")
EqFn = Callable[[Any, Any], bool]
CutoffFn = Callable[[Any], Any]


@final
@dataclass(frozen=True, eq=False)
class Input(Generic[T]):
    key: str
    eq: EqFn | None = field(default=None, kw_only=True)
    cutoff: CutoffFn | None = field(default=None, kw_only=True)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        del cls, kwargs
        raise TypeError("Input handles cannot be subclassed.")

    def __post_init__(self) -> None:
        if type(self.key) is not str or not self.key:
            raise InputKeyError("Input key must be a non-empty exact str.")
        if self.eq is not None and self.cutoff is not None:
            raise ValueError("Input() accepts either eq= or cutoff=, but not both.")
        if self.eq is not None and not callable(self.eq):
            raise TypeError("Input eq= must be callable.")
        if self.cutoff is not None and not callable(self.cutoff):
            raise TypeError("Input cutoff= must be callable.")

    def read(self, db: Database) -> T:
        return db.read_input(self)


@final
class Query(Generic[P, T]):
    """An immutable handle for one query definition.

    The callable and policy objects remain observable through read-only
    properties so the runtime can fingerprint their complete definitions on
    each request.  The handle itself has no instance dictionary and cannot
    acquire state that would be invisible to a capturing query.
    """

    __slots__ = (
        "_cutoff",
        "_doc",
        "_eq",
        "_fn",
        "_key",
        "_module",
        "_name",
        "_qualname",
        "__weakref__",
    )

    def __init_subclass__(cls, **kwargs: Any) -> None:
        del cls, kwargs
        raise TypeError("Query handles cannot be subclassed.")

    def __init__(
        self,
        fn: Callable[Concatenate[Database, P], T],
        *,
        key: str | None = None,
        eq: EqFn | None = None,
        cutoff: CutoffFn | None = None,
    ) -> None:
        if not isinstance(fn, FunctionType):
            raise TypeError("@query can decorate Python functions only.")
        if iscoroutinefunction(fn) or isgeneratorfunction(fn) or isasyncgenfunction(fn):
            raise TypeError("@query requires a synchronous, non-generator function.")
        if eq is not None and cutoff is not None:
            raise ValueError("@query accepts either eq= or cutoff=, but not both.")
        if eq is not None and not callable(eq):
            raise TypeError("@query eq= must be callable.")
        if cutoff is not None and not callable(cutoff):
            raise TypeError("@query cutoff= must be callable.")
        query_key = key if key is not None else f"{fn.__module__}:{fn.__qualname__}"
        if type(query_key) is not str or not query_key:
            raise ValueError("Query key must be a non-empty exact str.")
        object.__setattr__(self, "_fn", fn)
        object.__setattr__(self, "_eq", eq)
        object.__setattr__(self, "_cutoff", cutoff)
        object.__setattr__(self, "_key", query_key)
        object.__setattr__(self, "_module", fn.__module__)
        object.__setattr__(self, "_name", fn.__name__)
        object.__setattr__(self, "_qualname", fn.__qualname__)
        object.__setattr__(self, "_doc", fn.__doc__)

    def __getattribute__(self, name: str) -> Any:
        if name == "__module__":
            return object.__getattribute__(self, "_module")
        if name == "__name__":
            return object.__getattribute__(self, "_name")
        if name == "__qualname__":
            return object.__getattribute__(self, "_qualname")
        if name == "__doc__":
            return object.__getattribute__(self, "_doc")
        if name == "__wrapped__":
            return object.__getattribute__(self, "_fn")
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("Query handles are immutable.")

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError("Query handles are immutable.")

    @property
    def fn(self) -> Callable[Concatenate[Database, P], T]:
        return cast(
            Callable[..., T],
            object.__getattribute__(self, "_fn"),
        )

    @property
    def eq(self) -> EqFn | None:
        return cast(EqFn | None, object.__getattribute__(self, "_eq"))

    @property
    def cutoff(self) -> CutoffFn | None:
        return cast(CutoffFn | None, object.__getattribute__(self, "_cutoff"))

    @property
    def key(self) -> str:
        return cast(str, object.__getattribute__(self, "_key"))

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
    key: str | None = None,
    eq: EqFn | None = None,
    cutoff: CutoffFn | None = None,
) -> Query[P, T]: ...


@overload
def query(
    fn: None = None,
    *,
    key: str | None = None,
    eq: EqFn | None = None,
    cutoff: CutoffFn | None = None,
) -> Callable[[Callable[Concatenate[Database, P], T]], Query[P, T]]: ...


def query(
    fn: Callable[Concatenate[Database, P], T] | None = None,
    *,
    key: str | None = None,
    eq: EqFn | None = None,
    cutoff: CutoffFn | None = None,
) -> Query[P, T] | Callable[[Callable[Concatenate[Database, P], T]], Query[P, T]]:
    def decorate(wrapped: Callable[Concatenate[Database, P], T]) -> Query[P, T]:
        return Query(wrapped, key=key, eq=eq, cutoff=cutoff)

    if fn is None:
        return decorate
    return decorate(fn)


register_core_types(Input, Query)
