from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from inspect import isasyncgenfunction, iscoroutinefunction, isgeneratorfunction
from types import FunctionType
from typing import (
    TYPE_CHECKING,
    Any,
    Concatenate,
    Generic,
    ParamSpec,
    TypeVar,
    overload,
)

from .errors import InputKeyError
from .value import semantic_equal

if TYPE_CHECKING:
    from .runtime import Database

P = ParamSpec("P")
T = TypeVar("T")
EqFn = Callable[[Any, Any], bool]
CutoffFn = Callable[[Any], Any]


@dataclass(frozen=True, eq=False)
class Input(Generic[T]):
    key: str
    eq: EqFn | None = field(default=None, kw_only=True)
    cutoff: CutoffFn | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        # Exactness is decided before emptiness: the key is stored as node
        # identity and formatted into node labels, so a subclass would let its
        # own equality, formatting and truthiness decide what the kernel
        # records -- including whether `not self.key` sees an empty key at all.
        if type(self.key) is not str:
            if isinstance(self.key, str):
                raise InputKeyError(
                    "Input key must be exactly str; got str subclass "
                    f"{type(self.key).__qualname__}. Pass the plain string "
                    "(for Enum keys, use key.value)."
                )
            raise InputKeyError("Input key must be a non-empty string.")
        if not self.key:
            raise InputKeyError("Input key must be a non-empty string.")
        if self.eq is not None and self.cutoff is not None:
            raise ValueError("Input() accepts either eq= or cutoff=, but not both.")
        if self.eq is not None and not callable(self.eq):
            raise TypeError("Input eq= must be callable.")
        if self.cutoff is not None and not callable(self.cutoff):
            raise TypeError("Input cutoff= must be callable.")

    def read(self, db: Database) -> T:
        return db.read_input(self)


class Query(Generic[P, T]):
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
        # Same exactness-then-emptiness order as `Input`, for the same reason:
        # the key is formatted into query identities, node labels and the
        # checkpoint manifest's query ids. The derived default is a plain
        # string by construction, so only an explicit `key=` reaches the guard.
        if type(query_key) is not str:
            if isinstance(query_key, str):
                raise ValueError(
                    "Query key must be exactly str; got str subclass "
                    f"{type(query_key).__qualname__}. Pass the plain string "
                    "(for Enum keys, use key.value)."
                )
            raise ValueError("Query key must be a non-empty string.")
        if not query_key:
            raise ValueError("Query key must be a non-empty string.")
        self.fn = fn
        self.eq = eq
        self.cutoff = cutoff
        self.key = query_key
        # Copy descriptive callable metadata without merging the function's
        # arbitrary attribute dictionary into the query contract.
        wraps(fn, updated=())(self)

    def __call__(self, db: Database, *args: P.args, **kwargs: P.kwargs) -> T:
        return db.get(self, *args, **kwargs)

    def compare(self, old_value: Any, new_value: Any) -> bool:
        """Decide equivalence of two exposed values under this query's policy.

        Registry-free by contract: values needing a ValueAdapter cannot be
        compared here -- the runtime performs its own comparison over stored
        snapshots and never calls this helper. The default policy is the
        kernel's canonical relation via ``semantic_equal``.

        The cutoff arm decides by that same relation over frozen tokens, but
        it freezes without a Database, so it differs from the kernel's cutoff
        comparison in what it rejects rather than in what it decides: a token
        needing a ValueAdapter raises ``UnsupportedValueError`` here even
        where the kernel, holding the registry, would compare it, and the
        failure carries freeze's own message instead of the kernel's "Cutoff
        functions must return snapshot-safe values." No token frozen here
        reaches a configured ArtifactStore.
        """
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
