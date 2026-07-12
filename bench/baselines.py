"""Fixed comparison baselines for the release benchmark."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from typing import ParamSpec, Protocol, TypeVar, cast

FIXED_COMPARATORS: tuple[str, ...] = ("full", "naive", "joblib")

_P = ParamSpec("_P")
_R = TypeVar("_R")


class Memory(Protocol):
    def cache(self, func: Callable[_P, _R]) -> Callable[_P, _R]:
        raise NotImplementedError


def required_comparators() -> tuple[str, ...]:
    """Return the release comparator set, failing if joblib is unavailable."""
    if importlib.util.find_spec("joblib") is None:
        raise RuntimeError(
            "the benchmark requires joblib; install the fixed comparator set with "
            "`python -m pip install -e '.[bench]'`"
        )
    return FIXED_COMPARATORS


def make_joblib_memory(cache_dir: str) -> Memory:
    """Build the required joblib cache without importing it from shipped code."""
    import joblib  # type: ignore[import-untyped]

    return cast(Memory, joblib.Memory(location=cache_dir, verbose=0))
