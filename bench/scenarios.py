"""Scenario runner: correctness-gated measurement.

For every (workload, scenario) the runner first establishes correctness — the
incremental result must be byte-for-byte identical to a fresh, cache-disabled
recomputation — and only then records timing for each comparison implementation.
A correctness mismatch raises :class:`CorrectnessError`; no timing is ever emitted
without a passing correctness assertion.
"""

from __future__ import annotations

import hashlib
import importlib
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyinc import Database, InMemoryArtifactStore

from .adapters import WORKLOADS, KernelWorkload, RunMetrics, WorkloadFactory

IMPLEMENTATIONS = ("pyinc_incremental", "fresh_full", "naive_cache", "joblib_memory")
_INPUT_EDIT_SCENARIOS = frozenset({"presentation_edit", "semantic_edit", "high_fanout_edit"})

_NA = -1


class CorrectnessError(AssertionError):
    """Raised when an incremental result diverges from fresh recomputation."""


@dataclass(frozen=True)
class BenchRecord:
    workload: str
    scenario: str
    implementation: str
    correctness: str
    repetitions: int
    median_ns: int
    p95_ns: int
    query_executions: int
    query_reuses: int
    query_backdates: int
    output_writes: int
    output_deletes: int
    graph_nodes: int
    graph_edges: int
    checkpoint_bytes: int
    output_digest: str


def _uses_checkpoint(cls: WorkloadFactory) -> bool:
    return bool(getattr(cls, "uses_checkpoint", False))


def _make_db(cls: WorkloadFactory, store: InMemoryArtifactStore | None = None) -> Database:
    if store is not None:
        return Database(store=store)
    if _uses_checkpoint(cls):
        return Database(store=InMemoryArtifactStore())
    return Database()


def _build_incremental(
    cls: WorkloadFactory, base: Path, scenario: str
) -> Callable[[], RunMetrics]:
    """Set up state to just before the measured operation and return a thunk that
    performs exactly that operation (so only it is timed)."""
    workload = cls(base)
    if scenario == "cold":
        workload.seed()
        db = _make_db(cls)
        return lambda: workload.run_pyinc(db)

    workload.seed()
    if scenario == "checkpoint_restore":
        store = InMemoryArtifactStore()
        db1 = _make_db(cls, store)
        workload.run_pyinc(db1)
        key = db1.save_checkpoint()
        db2 = _make_db(cls, store)
        db2.load_checkpoint(key)
        return lambda: workload.run_pyinc(db2)

    db = _make_db(cls)
    workload.run_pyinc(db)  # cold (setup; not timed)
    db.reset_statistics()  # isolate the measured operation's stats from the cold run
    if scenario == "output_tamper":
        workload.tamper()
    elif scenario == "full_recompute":
        return lambda: workload.run_pyinc(_make_db(cls))
    else:
        workload.mutate(scenario)
    return lambda: workload.run_pyinc(db)


def _build_fresh(cls: WorkloadFactory, base: Path, scenario: str) -> Callable[[], str]:
    workload = cls(base)
    workload.seed()
    if scenario in _INPUT_EDIT_SCENARIOS:
        workload.mutate(scenario)
    return workload.run_fresh


def _build_naive(cls: WorkloadFactory, base: Path, scenario: str) -> Callable[[], str | None]:
    workload = cls(base)
    workload.seed()
    if scenario in _INPUT_EDIT_SCENARIOS:
        workload.mutate(scenario)
    return workload.run_naive


def _joblib_kernel_total(shared: int, leaves: tuple[int, ...]) -> int:
    return sum(leaf * shared for leaf in leaves)


def _build_joblib(cls: WorkloadFactory, base: Path, scenario: str) -> Callable[[], str | None] | None:
    """Return a joblib.Memory-backed callable where arg-based memoization can
    represent the scenario (the kernel workload), else ``None`` (N/A)."""
    try:
        joblib = importlib.import_module("joblib")
    except ImportError:
        return None
    if cls is not KernelWorkload:
        return None  # arg-based memoization cannot represent file-tree generation

    workload = KernelWorkload(base)
    workload.seed()
    if scenario in _INPUT_EDIT_SCENARIOS:
        workload.mutate(scenario)
    memory = joblib.Memory(location=str(base / "joblib"), verbose=0)
    cached = memory.cache(_joblib_kernel_total)

    def run() -> str | None:
        shared, leaves = workload.state_tuple()
        return hashlib.sha256(repr(cached(shared, leaves)).encode()).hexdigest()

    return run


def _percentile(samples: list[int], pct: float) -> int:
    if len(samples) == 1:
        return samples[0]
    ordered = sorted(samples)
    rank = pct / 100 * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return int(ordered[low] + (ordered[high] - ordered[low]) * frac)


def _time(thunk: Callable[[], Any], *, warmup: int, repetitions: int) -> tuple[int, int]:
    for _ in range(warmup):
        thunk()
    samples: list[int] = []
    for _ in range(repetitions):
        start = time.perf_counter_ns()
        thunk()
        samples.append(time.perf_counter_ns() - start)
    return int(statistics.median(samples)), _percentile(samples, 95.0)


def run_scenario(
    cls: WorkloadFactory,
    scenario: str,
    base: Path,
    *,
    warmup: int,
    repetitions: int,
) -> list[BenchRecord]:
    # --- correctness gate (runs before any timing) ---
    metrics = _build_incremental(cls, base / "auth_inc", scenario)()
    fresh_digest = _build_fresh(cls, base / "auth_fresh", scenario)()
    if metrics.digest != fresh_digest:
        raise CorrectnessError(
            f"{cls.name}/{scenario}: incremental digest {metrics.digest[:12]} != "
            f"fresh {fresh_digest[:12]}"
        )
    naive_check = _build_naive(cls, base / "auth_naive", scenario)()
    if naive_check is not None and naive_check != fresh_digest:
        raise CorrectnessError(f"{cls.name}/{scenario}: naive baseline diverged from fresh")

    records: list[BenchRecord] = []

    def emit(impl: str, median: int, p95: int, met: RunMetrics | None, correctness: str) -> None:
        records.append(
            BenchRecord(
                workload=cls.name,
                scenario=scenario,
                implementation=impl,
                correctness=correctness,
                repetitions=repetitions,
                median_ns=median,
                p95_ns=p95,
                query_executions=met.query_executions if met else _NA,
                query_reuses=met.query_reuses if met else _NA,
                query_backdates=met.query_backdates if met else _NA,
                output_writes=met.writes if met else _NA,
                output_deletes=met.deletes if met else _NA,
                graph_nodes=met.graph_nodes if met else _NA,
                graph_edges=met.graph_edges if met else _NA,
                checkpoint_bytes=met.checkpoint_bytes if met else _NA,
                output_digest=fresh_digest,
            )
        )

    # pyinc_incremental — rebuild setup each repetition so only the measured op is timed.
    inc_median, inc_p95 = _time(
        lambda: _build_incremental(cls, _fresh_dir(base, "inc"), scenario)(),
        warmup=warmup,
        repetitions=repetitions,
    )
    emit("pyinc_incremental", inc_median, inc_p95, metrics, "pass")

    fresh_median, fresh_p95 = _time(
        lambda: _build_fresh(cls, _fresh_dir(base, "fresh"), scenario)(),
        warmup=warmup,
        repetitions=repetitions,
    )
    emit("fresh_full", fresh_median, fresh_p95, None, "pass")

    naive_median, naive_p95 = _time(
        lambda: _build_naive(cls, _fresh_dir(base, "naive"), scenario)(),
        warmup=warmup,
        repetitions=repetitions,
    )
    emit("naive_cache", naive_median, naive_p95, None, "pass")

    joblib_builder = _build_joblib(cls, base / "joblib_probe", scenario)
    if joblib_builder is None:
        emit("joblib_memory", _NA, _NA, None, "n/a")
    else:
        jb_median, jb_p95 = _time(
            lambda: _build_joblib(cls, _fresh_dir(base, "joblib"), scenario)(),  # type: ignore[misc]
            warmup=warmup,
            repetitions=repetitions,
        )
        emit("joblib_memory", jb_median, jb_p95, None, "pass")

    return records


_counter = {"n": 0}


def _fresh_dir(base: Path, tag: str) -> Path:
    _counter["n"] += 1
    path = base / f"{tag}_{_counter['n']}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_all(base: Path, *, warmup: int, repetitions: int) -> list[BenchRecord]:
    records: list[BenchRecord] = []
    for cls in WORKLOADS:
        for scenario in cls.scenarios:
            records.extend(
                run_scenario(cls, scenario, base / cls.name / scenario, warmup=warmup, repetitions=repetitions)
            )
    return records
