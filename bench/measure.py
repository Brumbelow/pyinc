"""Scenario result record + timing/memory measurement.

Kept separate from ``harness`` so ``scenarios`` can import the result type and
``measure`` without importing ``harness`` (which imports ``scenarios``) — this
breaks what would otherwise be an import cycle.
"""

from __future__ import annotations

import time
import tracemalloc
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ScenarioResult:
    target: str
    scenario: str
    engine: str
    seconds: float
    peak_kib: float
    graph_size: int
    node_count: int
    correct: bool


def measure(fn: Callable[[], object]) -> tuple[object, float, float]:
    """Run ``fn`` once; return (result, seconds, peak KiB)."""
    tracemalloc.start()
    start = time.perf_counter()
    value = fn()
    elapsed = time.perf_counter() - start
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return value, elapsed, peak / 1024.0
