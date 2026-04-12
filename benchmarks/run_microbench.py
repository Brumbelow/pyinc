from __future__ import annotations

import argparse
import ast
import gc
import importlib.util
import json
import math
import os
import platform
import statistics
import sys
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias

from pyfoundinc import (
    Database,
    DirectoryResource,
    FileResource,
    FileStatResource,
    Input,
    InspectionNode,
    query,
)
from pyfoundinc.integrations.python_source import (
    directory_analysis,
    directory_analysis_payload,
    file_analysis_payload,
    source_text,
)

if TYPE_CHECKING:
    from benchmarks.plain_python_source import directory_analysis as plain_directory_analysis
else:
    try:
        from benchmarks.plain_python_source import directory_analysis as plain_directory_analysis
    except ModuleNotFoundError as err:
        plain_module_path = Path(__file__).with_name("plain_python_source.py")
        plain_spec = importlib.util.spec_from_file_location("benchmarks.plain_python_source", plain_module_path)
        if plain_spec is None or plain_spec.loader is None:
            raise RuntimeError(f"unable to load plain benchmark baseline from {plain_module_path}") from err
        plain_module = importlib.util.module_from_spec(plain_spec)
        sys.modules.setdefault("benchmarks.plain_python_source", plain_module)
        plain_spec.loader.exec_module(plain_module)
        plain_directory_analysis = plain_module.directory_analysis

CleanupFn: TypeAlias = Callable[[], None]
ObserveFn: TypeAlias = Callable[[], Mapping[str, int]]
PrepareFn: TypeAlias = Callable[[int], None]
ScenarioRunner: TypeAlias = Callable[["BenchConfig", str], "ScenarioResult"]


DEFAULT_MODE_BY_SCENARIO = {
    "diamond_reuse": "strict",
    "dynamic_rewiring": "strict",
    "resource_reads": "strict",
    "large_boundary": "checked",
    "query_backdating": "strict",
    "backdating_chain": "strict",
    "rewiring_torture": "strict",
    "cutoff_economics": "strict",
    "resource_granularity": "checked",
    "lru_pressure": "strict",
    "source_analysis": "strict",
}

ALIASES = {
    "diamond": "diamond_reuse",
    "rewiring": "dynamic_rewiring",
    "files": "resource_reads",
    "large": "large_boundary",
    "backdating": "query_backdating",
}


@dataclass(frozen=True)
class BenchCall:
    db: Database
    invoke: Callable[[], object]
    inspect: Callable[[], InspectionNode]
    observe: ObserveFn | None = None


@dataclass(frozen=True)
class SimpleBenchCall:
    invoke: Callable[[], object]
    observe: ObserveFn | None = None


MeasuredCall: TypeAlias = BenchCall | SimpleBenchCall
PreparedSetup: TypeAlias = Callable[[], tuple[MeasuredCall, CleanupFn]]


@dataclass(frozen=True)
class BenchConfig:
    samples: int
    warmup: int
    rounds: int
    payload_size: int


@dataclass(frozen=True)
class SequenceFixture:
    call: MeasuredCall
    prepare: PrepareFn
    cleanup: CleanupFn


@dataclass(frozen=True)
class PhaseMetrics:
    count: int
    min_s: float
    median_s: float
    mean_s: float
    stdev_s: float
    p95_s: float
    max_s: float
    total_s: float
    ops_per_s: float

    def to_dict(self) -> dict[str, int | float]:
        return {
            "count": self.count,
            "min_s": self.min_s,
            "median_s": self.median_s,
            "mean_s": self.mean_s,
            "stdev_s": self.stdev_s,
            "p95_s": self.p95_s,
            "max_s": self.max_s,
            "total_s": self.total_s,
            "ops_per_s": self.ops_per_s,
        }


@dataclass(frozen=True)
class PhaseResult:
    name: str
    metrics: PhaseMetrics
    markers: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "metrics": self.metrics.to_dict(),
            "markers": self.markers,
        }


@dataclass(frozen=True)
class InvariantResult:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ComparisonResult:
    name: str
    candidate_phase: str
    baseline_phase: str
    speedup_ratio: float | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "candidate_phase": self.candidate_phase,
            "baseline_phase": self.baseline_phase,
            "speedup_ratio": self.speedup_ratio,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ScenarioResult:
    key: str
    suite: str
    title: str
    why: str
    mode: str
    parameters: dict[str, Any]
    phases: tuple[PhaseResult, ...]
    comparisons: tuple[ComparisonResult, ...]
    invariants: tuple[InvariantResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "suite": self.suite,
            "title": self.title,
            "why": self.why,
            "mode": self.mode,
            "parameters": self.parameters,
            "phases": [phase.to_dict() for phase in self.phases],
            "comparisons": [comparison.to_dict() for comparison in self.comparisons],
            "invariants": [invariant.to_dict() for invariant in self.invariants],
            "interpretation": self.interpretation(),
        }

    def interpretation(self) -> str:
        for comparison in self.comparisons:
            if comparison.speedup_ratio is not None:
                return comparison.detail
        passed = sum(1 for item in self.invariants if item.passed)
        return f"{passed} invariant checks passed."

    def vs_fresh(self, phase_name: str) -> float | None:
        for comparison in self.comparisons:
            if comparison.candidate_phase == phase_name and "fresh" in comparison.baseline_phase:
                return comparison.speedup_ratio
        return None


@dataclass(frozen=True)
class BenchReport:
    environment: dict[str, Any]
    config: dict[str, Any]
    results: tuple[ScenarioResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "config": self.config,
            "results": [result.to_dict() for result in self.results],
        }


@dataclass(frozen=True)
class ImplementationPhaseResult:
    implementation: str
    phase: PhaseResult

    def to_dict(self) -> dict[str, Any]:
        return {
            "implementation": self.implementation,
            "phase": self.phase.to_dict(),
        }


@dataclass(frozen=True)
class WorkloadOperationResult:
    name: str
    measurements: tuple[ImplementationPhaseResult, ...]
    comparison: ComparisonResult | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "measurements": [measurement.to_dict() for measurement in self.measurements],
            "comparison": None if self.comparison is None else self.comparison.to_dict(),
            "note": self.note,
        }

    def measurement(self, implementation: str) -> ImplementationPhaseResult | None:
        for measurement in self.measurements:
            if measurement.implementation == implementation:
                return measurement
        return None

    def speedup(self) -> float | None:
        if self.comparison is None:
            return None
        return self.comparison.speedup_ratio


@dataclass(frozen=True)
class WorkloadScenarioResult:
    key: str
    suite: str
    title: str
    why: str
    parameters: dict[str, Any]
    operations: tuple[WorkloadOperationResult, ...]
    invariants: tuple[InvariantResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "suite": self.suite,
            "title": self.title,
            "why": self.why,
            "parameters": self.parameters,
            "operations": [operation.to_dict() for operation in self.operations],
            "invariants": [invariant.to_dict() for invariant in self.invariants],
            "interpretation": self.interpretation(),
        }

    def interpretation(self) -> str:
        fastest = max(
            (operation for operation in self.operations if operation.speedup() is not None),
            key=lambda operation: operation.speedup() or 0.0,
            default=None,
        )
        if fastest is not None and fastest.note:
            return fastest.note
        passed = sum(1 for item in self.invariants if item.passed)
        return f"{passed} invariant checks passed."


@dataclass(frozen=True)
class WorkloadComparisonReport:
    environment: dict[str, Any]
    config: dict[str, Any]
    implementations: tuple[str, ...]
    results: tuple[WorkloadScenarioResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "config": self.config,
            "implementations": list(self.implementations),
            "results": [result.to_dict() for result in self.results],
        }


@dataclass(frozen=True)
class ScenarioSpec:
    key: str
    suite: str
    title: str
    why: str
    run: ScenarioRunner


def _noop() -> None:
    return None


@contextmanager
def _gc_suppressed() -> Any:
    was_enabled = gc.isenabled()
    gc.collect()
    if was_enabled:
        gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


def _timed(action: Callable[[], object]) -> float:
    with _gc_suppressed():
        started = time.perf_counter()
        action()
        return time.perf_counter() - started


def _percentile_index(size: int, percentile: float) -> int:
    return max(0, min(size - 1, math.ceil(size * percentile) - 1))


def _phase_metrics(times: list[float]) -> PhaseMetrics:
    if not times:
        raise ValueError("benchmark phase produced no samples")
    ordered = sorted(times)
    total = sum(times)
    return PhaseMetrics(
        count=len(times),
        min_s=ordered[0],
        median_s=statistics.median(ordered),
        mean_s=statistics.mean(times),
        stdev_s=statistics.stdev(times) if len(times) > 1 else 0.0,
        p95_s=ordered[_percentile_index(len(ordered), 0.95)],
        max_s=ordered[-1],
        total_s=total,
        ops_per_s=(len(times) / total) if total else float("inf"),
    )


def _query_call(
    db: Database,
    query_obj: Any,
    *args: Any,
    observe: ObserveFn | None = None,
) -> BenchCall:
    return BenchCall(
        db=db,
        invoke=lambda: db.get(query_obj, *args),
        inspect=lambda: db.inspect(query_obj, *args),
        observe=observe,
    )


def _record_markers(call: MeasuredCall, aggregate: Counter[str]) -> None:
    if call.observe is None:
        return
    aggregate.update(call.observe())


def _measure_cold(name: str, setup: PreparedSetup, *, rounds: int) -> PhaseResult:
    times: list[float] = []
    markers: Counter[str] = Counter()
    for _ in range(rounds):
        call, cleanup = setup()
        try:
            times.append(_timed(call.invoke))
            _record_markers(call, markers)
        finally:
            cleanup()
    return PhaseResult(name=name, metrics=_phase_metrics(times), markers=dict(sorted(markers.items())))


def _measure_cached(
    name: str,
    setup: PreparedSetup,
    config: BenchConfig,
) -> PhaseResult:
    times: list[float] = []
    markers: Counter[str] = Counter()
    for _ in range(config.rounds):
        call, cleanup = setup()
        try:
            call.invoke()
            for _ in range(config.warmup):
                call.invoke()
            for _ in range(config.samples):
                times.append(_timed(call.invoke))
                _record_markers(call, markers)
        finally:
            cleanup()
    return PhaseResult(name=name, metrics=_phase_metrics(times), markers=dict(sorted(markers.items())))


def _measure_sequence(
    name: str,
    setup: Callable[[], SequenceFixture],
    config: BenchConfig,
) -> PhaseResult:
    times: list[float] = []
    markers: Counter[str] = Counter()
    for _ in range(config.rounds):
        fixture = setup()
        try:
            fixture.call.invoke()
            for step in range(config.warmup):
                fixture.prepare(step)
                fixture.call.invoke()
            for step in range(config.warmup, config.warmup + config.samples):
                times.append(_timed(partial(_run_sequence_step, fixture, step)))
                _record_markers(fixture.call, markers)
        finally:
            fixture.cleanup()
    return PhaseResult(name=name, metrics=_phase_metrics(times), markers=dict(sorted(markers.items())))


def _measure_fresh_sequence(
    name: str,
    setup: Callable[[], SequenceFixture],
    config: BenchConfig,
) -> PhaseResult:
    times: list[float] = []
    markers: Counter[str] = Counter()
    total_steps = config.warmup + config.samples
    for _ in range(config.rounds):
        for step in range(total_steps):
            fixture = setup()
            try:
                if step < config.warmup:
                    _run_fresh_sequence_step(fixture, step)
                else:
                    times.append(_timed(partial(_run_fresh_sequence_step, fixture, step)))
                    _record_markers(fixture.call, markers)
            finally:
                fixture.cleanup()
    return PhaseResult(name=name, metrics=_phase_metrics(times), markers=dict(sorted(markers.items())))


def _measure_prepared(name: str, setup: PreparedSetup, config: BenchConfig) -> PhaseResult:
    times: list[float] = []
    markers: Counter[str] = Counter()
    for _ in range(config.rounds):
        for _ in range(config.warmup):
            call, cleanup = setup()
            try:
                call.invoke()
            finally:
                cleanup()
        for _ in range(config.samples):
            call, cleanup = setup()
            try:
                times.append(_timed(call.invoke))
                _record_markers(call, markers)
            finally:
                cleanup()
    return PhaseResult(name=name, metrics=_phase_metrics(times), markers=dict(sorted(markers.items())))


def _run_sequence_step(fixture: SequenceFixture, step: int) -> object:
    fixture.prepare(step)
    return fixture.call.invoke()


def _run_fresh_sequence_step(fixture: SequenceFixture, step: int) -> object:
    for previous_step in range(step + 1):
        fixture.prepare(previous_step)
    return fixture.call.invoke()


def _require(condition: bool, name: str, detail: str) -> InvariantResult:
    if not condition:
        raise RuntimeError(f"{name}: {detail}")
    return InvariantResult(name=name, passed=True, detail=detail)


def _find_node(root: InspectionNode, *needles: str) -> InspectionNode:
    if all(needle in root.label for needle in needles):
        return root
    for dependency in root.dependencies:
        try:
            return _find_node(dependency, *needles)
        except LookupError:
            continue
    raise LookupError(", ".join(needles))


def _decision_markers(node: InspectionNode, prefix: str) -> dict[str, int]:
    return {f"{prefix}_{node.last_decision}": 1}


def _merged_markers(*entries: Mapping[str, int]) -> dict[str, int]:
    merged: Counter[str] = Counter()
    for entry in entries:
        merged.update(entry)
    return dict(merged)


def _speedup(
    phases: Mapping[str, PhaseResult],
    name: str,
    candidate_phase: str,
    baseline_phase: str,
) -> ComparisonResult:
    return _phase_speedup(
        name,
        candidate_phase,
        phases[candidate_phase],
        baseline_phase,
        phases[baseline_phase],
    )


def _phase_speedup(
    name: str,
    candidate_label: str,
    candidate_phase: PhaseResult,
    baseline_label: str,
    baseline_phase: PhaseResult,
) -> ComparisonResult:
    candidate = candidate_phase.metrics.mean_s
    baseline = baseline_phase.metrics.mean_s
    ratio = None if candidate <= 0.0 else baseline / candidate
    if ratio is None:
        detail = f"{candidate_label} completed too quickly for a stable ratio against {baseline_label}."
    else:
        detail = f"{candidate_label} is {ratio:.2f}x faster than {baseline_label} by mean latency."
    return ComparisonResult(
        name=name,
        candidate_phase=candidate_label,
        baseline_phase=baseline_label,
        speedup_ratio=ratio,
        detail=detail,
    )


def _scenario_result(
    *,
    key: str,
    suite: str,
    title: str,
    why: str,
    mode: str,
    parameters: dict[str, Any],
    phases: Sequence[PhaseResult],
    invariants: Sequence[InvariantResult],
    comparison_pairs: Sequence[tuple[str, str, str]],
) -> ScenarioResult:
    ordered_phases = tuple(phases)
    phase_map = {phase.name: phase for phase in ordered_phases}
    comparisons = tuple(
        _speedup(phase_map, name, candidate_phase, baseline_phase)
        for name, candidate_phase, baseline_phase in comparison_pairs
    )
    return ScenarioResult(
        key=key,
        suite=suite,
        title=title,
        why=why,
        mode=mode,
        parameters=parameters,
        phases=ordered_phases,
        comparisons=comparisons,
        invariants=tuple(invariants),
    )


def _resolved_mode(scenario_key: str, override: str | None) -> str:
    return override or DEFAULT_MODE_BY_SCENARIO[scenario_key]


def _scale(payload_size: int, *, minimum: int, maximum: int, divisor: int) -> int:
    return max(minimum, min(maximum, max(minimum, payload_size // divisor)))


def _observe_root(call: BenchCall) -> dict[str, int]:
    return _decision_markers(call.inspect(), "root")


def _build_diamond(mode: str) -> tuple[BenchCall, Input[int]]:
    number = Input[int]("number")

    @query
    def left(db: Database) -> int:
        return number.read(db) + 1

    @query
    def right(db: Database) -> int:
        return number.read(db) + 2

    @query
    def root(db: Database) -> int:
        return left(db) * right(db)

    db = Database(mode=mode)
    db.set(number, 1)
    call = _query_call(db, root, observe=lambda: _observe_root(call))
    return call, number


def benchmark_diamond(config: BenchConfig, mode: str) -> ScenarioResult:
    call, number = _build_diamond(mode)
    baseline = call.invoke()
    call.db.set(number, 2)
    updated = call.invoke()
    invariants = (
        _require(updated != baseline, "diamond_changes_root", "input changes reach the root node"),
    )

    def setup_call() -> tuple[BenchCall, CleanupFn]:
        local_call, _ = _build_diamond(mode)
        return local_call, _noop

    def setup_delta() -> SequenceFixture:
        local_call, local_number = _build_diamond(mode)
        next_value = 2

        def prepare(_: int) -> None:
            nonlocal next_value
            local_call.db.set(local_number, next_value)
            next_value += 1

        return SequenceFixture(call=local_call, prepare=prepare, cleanup=_noop)

    phases = (
        _measure_cold("cold_full", setup_call, rounds=config.rounds),
        _measure_cached("warm_reuse", setup_call, config),
        _measure_sequence("incremental_delta", setup_delta, config),
        _measure_fresh_sequence("fresh_recompute", setup_delta, config),
    )
    return _scenario_result(
        key="diamond_reuse",
        suite="micro",
        title="Diamond Reuse",
        why="Shows classic fanout reuse versus fresh recomputation for a small dependency diamond.",
        mode=mode,
        parameters={},
        phases=phases,
        invariants=invariants,
        comparison_pairs=(
            ("warm_vs_cold", "warm_reuse", "cold_full"),
            ("incremental_vs_fresh", "incremental_delta", "fresh_recompute"),
        ),
    )


def _build_rewiring(mode: str) -> tuple[BenchCall, Input[str], Input[int], Input[int]]:
    chooser = Input[str]("chooser")
    left_input = Input[int]("left")
    right_input = Input[int]("right")

    @query
    def selected(db: Database) -> int:
        if chooser.read(db) == "left":
            return left_input.read(db)
        return right_input.read(db)

    @query
    def root(db: Database) -> int:
        return selected(db) + 1

    db = Database(mode=mode)
    db.set(chooser, "left")
    db.set(left_input, 1)
    db.set(right_input, 10)
    call = _query_call(db, root, observe=lambda: _observe_root(call))
    return call, chooser, left_input, right_input


def benchmark_rewiring(config: BenchConfig, mode: str) -> ScenarioResult:
    call, chooser, left_input, right_input = _build_rewiring(mode)
    invariants = []

    initial = call.invoke()
    invariants.append(
        _require(initial == 2, "rewiring_initial_branch", "the left branch is active for the baseline request"),
    )
    call.db.set(chooser, "right")
    call.db.set(right_input, 11)
    switched = call.invoke()
    invariants.append(
        _require(switched == 12, "rewiring_switches_branch", "switching the chooser swaps to the right branch"),
    )
    call.db.set(left_input, 999)
    reused = call.invoke()
    invariants.append(
        _require(
            reused == switched,
            "rewiring_drops_stale_edges",
            "mutating the inactive branch no longer affects the root",
        ),
    )

    def setup_call() -> tuple[BenchCall, CleanupFn]:
        local_call, _, _, _ = _build_rewiring(mode)
        return local_call, _noop

    def setup_delta() -> SequenceFixture:
        local_call, local_chooser, local_left, local_right = _build_rewiring(mode)
        current = "left"
        next_left = 2
        next_right = 20

        def prepare(_: int) -> None:
            nonlocal current, next_left, next_right
            current = "right" if current == "left" else "left"
            local_call.db.set(local_chooser, current)
            if current == "left":
                local_call.db.set(local_left, next_left)
                next_left += 1
            else:
                local_call.db.set(local_right, next_right)
                next_right += 1

        return SequenceFixture(call=local_call, prepare=prepare, cleanup=_noop)

    phases = (
        _measure_cold("cold_full", setup_call, rounds=config.rounds),
        _measure_cached("warm_reuse", setup_call, config),
        _measure_sequence("incremental_delta", setup_delta, config),
        _measure_fresh_sequence("fresh_recompute", setup_delta, config),
    )
    return _scenario_result(
        key="dynamic_rewiring",
        suite="micro",
        title="Dynamic Rewiring",
        why="Measures branch switching and confirms inactive dependencies stop invalidating the active path.",
        mode=mode,
        parameters={},
        phases=phases,
        invariants=invariants,
        comparison_pairs=(
            ("warm_vs_cold", "warm_reuse", "cold_full"),
            ("incremental_vs_fresh", "incremental_delta", "fresh_recompute"),
        ),
    )


def _build_file_resources(mode: str) -> tuple[BenchCall, Path, CleanupFn]:
    files = FileResource()
    directories = DirectoryResource()

    @query
    def digest(db: Database, filename: str) -> tuple[str, int]:
        parent = str(Path(filename).parent)
        entries = directories.read(db, parent)
        return files.read(db, filename), len(entries)

    tmpdir = tempfile.TemporaryDirectory()
    path = Path(tmpdir.name) / "sample.txt"
    path.write_text("alpha", encoding="utf-8")
    db = Database(mode=mode)
    call = _query_call(db, digest, str(path), observe=lambda: _observe_root(call))
    return call, path, tmpdir.cleanup


def benchmark_file_resources(config: BenchConfig, mode: str) -> ScenarioResult:
    call, path, cleanup = _build_file_resources(mode)
    try:
        baseline = call.invoke()
        path.write_text("beta", encoding="utf-8")
        updated = call.invoke()
        invariants = (
            _require(
                updated != baseline,
                "resource_reads_observe_file_updates",
                "tracked file content changes invalidate the digest",
            ),
        )
    finally:
        cleanup()

    def setup_call() -> tuple[BenchCall, CleanupFn]:
        local_call, _, local_cleanup = _build_file_resources(mode)
        return local_call, local_cleanup

    def setup_delta() -> SequenceFixture:
        local_call, local_path, local_cleanup = _build_file_resources(mode)

        def prepare(step: int) -> None:
            local_path.write_text(f"value-{step}", encoding="utf-8")

        return SequenceFixture(call=local_call, prepare=prepare, cleanup=local_cleanup)

    phases = (
        _measure_cold("cold_full", setup_call, rounds=config.rounds),
        _measure_cached("warm_reuse", setup_call, config),
        _measure_sequence("incremental_delta", setup_delta, config),
        _measure_fresh_sequence("fresh_recompute", setup_delta, config),
    )
    return _scenario_result(
        key="resource_reads",
        suite="micro",
        title="Resource Reads",
        why="Compares tracked file and directory reads against rebuilding the same resource-backed query from scratch.",
        mode=mode,
        parameters={},
        phases=phases,
        invariants=invariants,
        comparison_pairs=(
            ("warm_vs_cold", "warm_reuse", "cold_full"),
            ("incremental_vs_fresh", "incremental_delta", "fresh_recompute"),
        ),
    )


def _build_large_boundary(mode: str, payload_size: int) -> tuple[BenchCall, Input[list[int]], list[int]]:
    payload = Input[list[int]]("payload")

    @query
    def mirror(db: Database) -> object:
        return payload.read(db)

    db = Database(mode=mode)
    values = list(range(payload_size))
    db.set(payload, values)
    call = _query_call(db, mirror, observe=lambda: _observe_root(call))
    return call, payload, values


def benchmark_large_boundary(config: BenchConfig, mode: str) -> ScenarioResult:
    call, payload, values = _build_large_boundary(mode, config.payload_size)
    baseline = call.invoke()
    baseline_revision = call.db.revision

    call.db.set(payload, values)
    identical = call.invoke()
    equal_revision = call.db.revision
    call.db.set(payload, list(values))
    equal_value = call.invoke()
    values[0] += config.payload_size
    call.db.set(payload, values)
    delta = call.invoke()

    invariants = (
        _require(
            baseline_revision == equal_revision == call.db.revision - 1,
            "large_boundary_equal_updates_do_not_advance_revision",
            "identical and equal-content updates keep the same input revision",
        ),
        _require(identical == baseline == equal_value, "large_boundary_equal_values_match", "equal updates reuse the same value"),
        _require(delta != baseline, "large_boundary_delta_changes_value", "real deltas still invalidate the boundary"),
    )

    def setup_call() -> tuple[BenchCall, CleanupFn]:
        local_call, _, _ = _build_large_boundary(mode, config.payload_size)
        return local_call, _noop

    def setup_identical() -> SequenceFixture:
        local_call, local_payload, local_values = _build_large_boundary(mode, config.payload_size)

        def prepare(_: int) -> None:
            local_call.db.set(local_payload, local_values)

        return SequenceFixture(call=local_call, prepare=prepare, cleanup=_noop)

    def setup_equal() -> SequenceFixture:
        local_call, local_payload, local_values = _build_large_boundary(mode, config.payload_size)

        def prepare(_: int) -> None:
            local_call.db.set(local_payload, list(local_values))

        return SequenceFixture(call=local_call, prepare=prepare, cleanup=_noop)

    def setup_delta() -> SequenceFixture:
        local_call, local_payload, local_values = _build_large_boundary(mode, config.payload_size)
        next_index = 0
        next_value = config.payload_size

        def prepare(_: int) -> None:
            nonlocal next_index, next_value
            local_values[next_index % len(local_values)] = next_value
            next_index += 1
            next_value += 1
            local_call.db.set(local_payload, local_values)

        return SequenceFixture(call=local_call, prepare=prepare, cleanup=_noop)

    phases = (
        _measure_cold("cold_full", setup_call, rounds=config.rounds),
        _measure_cached("warm_reuse", setup_call, config),
        _measure_sequence("identical_update", setup_identical, config),
        _measure_sequence("equal_update", setup_equal, config),
        _measure_sequence("incremental_delta", setup_delta, config),
        _measure_fresh_sequence("fresh_recompute", setup_delta, config),
    )
    return _scenario_result(
        key="large_boundary",
        suite="micro",
        title="Large Boundary",
        why="Shows the cost of comparing large boundary values, plus the payoff when equal updates do not dirty the graph.",
        mode=mode,
        parameters={"payload_size": config.payload_size},
        phases=phases,
        invariants=invariants,
        comparison_pairs=(
            ("warm_vs_cold", "warm_reuse", "cold_full"),
            ("equal_vs_delta", "equal_update", "incremental_delta"),
            ("delta_vs_fresh", "incremental_delta", "fresh_recompute"),
        ),
    )


def _build_query_backdating(mode: str) -> tuple[BenchCall, Input[int], Any]:
    source = Input[int]("source")

    @query
    def middle(db: Database) -> int:
        return abs(source.read(db))

    @query
    def root(db: Database) -> int:
        return middle(db) * 10 + 5

    db = Database(mode=mode)
    db.set(source, 42)

    def observe() -> dict[str, int]:
        inspection = db.inspect(root)
        middle_node = _find_node(inspection, "middle")
        return _merged_markers(
            _decision_markers(inspection, "root"),
            _decision_markers(middle_node, "middle"),
        )

    call = _query_call(db, root, observe=observe)
    return call, source, middle


def benchmark_query_backdating(config: BenchConfig, mode: str) -> ScenarioResult:
    call, source, middle = _build_query_backdating(mode)
    baseline = call.invoke()
    changed_at = call.inspect().changed_at

    call.db.set(source, -42)
    updated = call.invoke()
    middle_node = _find_node(call.inspect(), "middle")
    root_node = call.inspect()
    call.db.set(source, 100)
    changed = call.invoke()

    invariants = (
        _require(updated == baseline, "query_backdating_preserves_equal_results", "equal recomputes preserve the root value"),
        _require(middle_node.last_decision == "backdated", "query_backdating_marks_middle", "the middle query backdates on equal recompute"),
        _require(root_node.last_decision == "reused", "query_backdating_skips_downstream", "downstream queries reuse after backdating"),
        _require(root_node.changed_at == changed_at, "query_backdating_preserves_changed_at", "reused downstream nodes keep changed_at stable"),
        _require(changed != baseline, "query_backdating_real_changes_still_invalidate", "real magnitude changes still reach the root"),
    )

    def setup_call() -> tuple[BenchCall, CleanupFn]:
        local_call, _, _ = _build_query_backdating(mode)
        return local_call, _noop

    def setup_backdate() -> SequenceFixture:
        local_call, local_source, _ = _build_query_backdating(mode)
        current = 42

        def prepare(_: int) -> None:
            nonlocal current
            current = -42 if current == 42 else 42
            local_call.db.set(local_source, current)

        return SequenceFixture(call=local_call, prepare=prepare, cleanup=_noop)

    def setup_real_change() -> SequenceFixture:
        local_call, local_source, _ = _build_query_backdating(mode)
        next_value = 100

        def prepare(_: int) -> None:
            nonlocal next_value
            local_call.db.set(local_source, next_value)
            next_value += 1

        return SequenceFixture(call=local_call, prepare=prepare, cleanup=_noop)

    phases = (
        _measure_cold("cold_full", setup_call, rounds=config.rounds),
        _measure_cached("warm_reuse", setup_call, config),
        _measure_sequence("backdate", setup_backdate, config),
        _measure_fresh_sequence("backdate_fresh", setup_backdate, config),
        _measure_sequence("real_change", setup_real_change, config),
        _measure_fresh_sequence("real_change_fresh", setup_real_change, config),
    )
    return _scenario_result(
        key="query_backdating",
        suite="micro",
        title="Query Backdating",
        why="Measures how much work the kernel avoids when equal recomputes backdate before downstream nodes execute.",
        mode=mode,
        parameters={},
        phases=phases,
        invariants=invariants,
        comparison_pairs=(
            ("warm_vs_cold", "warm_reuse", "cold_full"),
            ("backdate_vs_fresh", "backdate", "backdate_fresh"),
            ("real_change_vs_fresh", "real_change", "real_change_fresh"),
            ("backdate_vs_real_change", "backdate", "real_change"),
        ),
    )


def _build_backdating_chain(mode: str, payload_size: int) -> tuple[BenchCall, Input[int]]:
    source = Input[int]("source")
    fanout = _scale(payload_size, minimum=8, maximum=48, divisor=32)

    @query
    def canonical(db: Database) -> int:
        return abs(source.read(db))

    @query
    def bucket(db: Database) -> int:
        return canonical(db) % 23

    @query
    def branch(db: Database, index: int) -> str:
        return f"{index}:{bucket(db)}"

    @query
    def root(db: Database) -> tuple[str, ...]:
        return tuple(branch(db, index) for index in range(fanout))

    db = Database(mode=mode)
    db.set(source, 42)

    def observe() -> dict[str, int]:
        inspection = db.inspect(root)
        canonical_node = _find_node(inspection, "canonical")
        bucket_node = _find_node(inspection, "bucket")
        return _merged_markers(
            _decision_markers(inspection, "root"),
            _decision_markers(canonical_node, "canonical"),
            _decision_markers(bucket_node, "bucket"),
        )

    call = _query_call(db, root, observe=observe)
    return call, source


def benchmark_backdating_chain(config: BenchConfig, mode: str) -> ScenarioResult:
    call, source = _build_backdating_chain(mode, config.payload_size)
    baseline = call.invoke()
    call.db.set(source, -42)
    backdated_value = call.invoke()
    backdated_inspection = call.inspect()
    call.db.set(source, 43)
    changed_value = call.invoke()

    invariants = (
        _require(
            backdated_value == baseline,
            "backdating_chain_equal_result",
            "sign flips preserve the chain output after normalization",
        ),
        _require(
            _find_node(backdated_inspection, "canonical").last_decision == "backdated",
            "backdating_chain_marks_canonical",
            "the canonical normalization backdates on equal recompute",
        ),
        _require(
            backdated_inspection.last_decision == "reused",
            "backdating_chain_skips_root",
            "the fanout root reuses after a backdated upstream node",
        ),
        _require(
            changed_value != baseline,
            "backdating_chain_real_change",
            "real magnitude changes still reach the fanout root",
        ),
    )

    def setup_call() -> tuple[BenchCall, CleanupFn]:
        local_call, _ = _build_backdating_chain(mode, config.payload_size)
        return local_call, _noop

    def setup_backdate() -> SequenceFixture:
        local_call, local_source = _build_backdating_chain(mode, config.payload_size)
        current = 42

        def prepare(_: int) -> None:
            nonlocal current
            current = -42 if current == 42 else 42
            local_call.db.set(local_source, current)

        return SequenceFixture(call=local_call, prepare=prepare, cleanup=_noop)

    def setup_real_change() -> SequenceFixture:
        local_call, local_source = _build_backdating_chain(mode, config.payload_size)
        next_value = 43

        def prepare(_: int) -> None:
            nonlocal next_value
            local_call.db.set(local_source, next_value)
            next_value += 1

        return SequenceFixture(call=local_call, prepare=prepare, cleanup=_noop)

    phases = (
        _measure_cold("cold_full", setup_call, rounds=config.rounds),
        _measure_cached("warm_reuse", setup_call, config),
        _measure_sequence("backdate", setup_backdate, config),
        _measure_fresh_sequence("backdate_fresh", setup_backdate, config),
        _measure_sequence("real_change", setup_real_change, config),
        _measure_fresh_sequence("real_change_fresh", setup_real_change, config),
    )
    return _scenario_result(
        key="backdating_chain",
        suite="micro",
        title="Backdating Chain",
        why="Extends backdating across a deeper fanout so the report shows how equal recomputes stop wider downstream ripple.",
        mode=mode,
        parameters={"fanout": _scale(config.payload_size, minimum=8, maximum=48, divisor=32)},
        phases=phases,
        invariants=invariants,
        comparison_pairs=(
            ("warm_vs_cold", "warm_reuse", "cold_full"),
            ("backdate_vs_fresh", "backdate", "backdate_fresh"),
            ("real_change_vs_fresh", "real_change", "real_change_fresh"),
            ("backdate_vs_real_change", "backdate", "real_change"),
        ),
    )


def _build_rewiring_torture(
    mode: str,
    payload_size: int,
    *,
    initial_side: str,
) -> tuple[BenchCall, Input[str], tuple[Input[int], ...], tuple[Input[int], ...]]:
    chooser = Input[str]("chooser")
    width = _scale(payload_size, minimum=4, maximum=24, divisor=96)
    left_inputs = tuple(Input[int](f"left_{index}") for index in range(width))
    right_inputs = tuple(Input[int](f"right_{index}") for index in range(width))

    @query
    def left_sum(db: Database) -> int:
        return sum(input_key.read(db) for input_key in left_inputs)

    @query
    def right_sum(db: Database) -> int:
        return sum(input_key.read(db) for input_key in right_inputs)

    @query
    def active_sum(db: Database) -> int:
        if chooser.read(db) == "left":
            return left_sum(db)
        return right_sum(db)

    @query
    def root(db: Database) -> int:
        return active_sum(db) + 1

    db = Database(mode=mode)
    db.set(chooser, initial_side)
    for index, input_key in enumerate(left_inputs):
        db.set(input_key, index + 1)
    for index, input_key in enumerate(right_inputs):
        db.set(input_key, 100 + index)

    def observe() -> dict[str, int]:
        inspection = db.inspect(root)
        active_node = _find_node(inspection, "active_sum")
        return _merged_markers(
            _decision_markers(inspection, "root"),
            _decision_markers(active_node, "active"),
        )

    call = _query_call(db, root, observe=observe)
    return call, chooser, left_inputs, right_inputs


def benchmark_rewiring_torture(config: BenchConfig, mode: str) -> ScenarioResult:
    call, chooser, left_inputs, _ = _build_rewiring_torture(mode, config.payload_size, initial_side="left")
    baseline = call.invoke()
    call.db.set(chooser, "right")
    switched = call.invoke()
    for input_key in left_inputs:
        call.db.set(input_key, 999)
    inactive = call.invoke()

    invariants = (
        _require(switched != baseline, "rewiring_torture_switches_active_branch", "switching sides changes the active aggregate"),
        _require(
            inactive == switched,
            "rewiring_torture_ignores_inactive_churn",
            "churn on the inactive branch does not affect the root",
        ),
    )

    def setup_call() -> tuple[BenchCall, CleanupFn]:
        local_call, _, _, _ = _build_rewiring_torture(mode, config.payload_size, initial_side="left")
        return local_call, _noop

    def setup_switch() -> SequenceFixture:
        local_call, local_chooser, local_left, local_right = _build_rewiring_torture(
            mode,
            config.payload_size,
            initial_side="left",
        )
        current = "left"
        left_value = 1000
        right_value = 2000

        def prepare(_: int) -> None:
            nonlocal current, left_value, right_value
            current = "right" if current == "left" else "left"
            local_call.db.set(local_chooser, current)
            if current == "left":
                local_call.db.set(local_left[0], left_value)
                left_value += 1
            else:
                local_call.db.set(local_right[0], right_value)
                right_value += 1

        return SequenceFixture(call=local_call, prepare=prepare, cleanup=_noop)

    def setup_inactive_churn() -> SequenceFixture:
        local_call, _, local_left, _ = _build_rewiring_torture(mode, config.payload_size, initial_side="right")
        next_value = 5000

        def prepare(_: int) -> None:
            nonlocal next_value
            for input_key in local_left:
                local_call.db.set(input_key, next_value)
                next_value += 1

        return SequenceFixture(call=local_call, prepare=prepare, cleanup=_noop)

    phases = (
        _measure_cold("cold_full", setup_call, rounds=config.rounds),
        _measure_cached("warm_reuse", setup_call, config),
        _measure_sequence("branch_switch", setup_switch, config),
        _measure_fresh_sequence("branch_switch_fresh", setup_switch, config),
        _measure_sequence("inactive_churn", setup_inactive_churn, config),
        _measure_fresh_sequence("inactive_churn_fresh", setup_inactive_churn, config),
    )
    return _scenario_result(
        key="rewiring_torture",
        suite="micro",
        title="Rewiring Torture",
        why="Separates real branch switches from heavy churn on inactive branches so stale-edge dropping becomes visible in the numbers.",
        mode=mode,
        parameters={"branch_width": _scale(config.payload_size, minimum=4, maximum=24, divisor=96)},
        phases=phases,
        invariants=invariants,
        comparison_pairs=(
            ("warm_vs_cold", "warm_reuse", "cold_full"),
            ("switch_vs_fresh", "branch_switch", "branch_switch_fresh"),
            ("inactive_vs_fresh", "inactive_churn", "inactive_churn_fresh"),
        ),
    )


def _analysis_counts(source: str) -> tuple[int, int]:
    tree = ast.parse(source)
    imports = 0
    definitions = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions += 1
    return imports, definitions


def _comment_heavy_module(payload_size: int) -> str:
    item_count = _scale(payload_size, minimum=8, maximum=64, divisor=16)
    lines: list[str] = []
    for index in range(item_count):
        lines.append(f"import dep_{index}\n")
    for index in range(item_count):
        lines.append(f"def fn_{index}() -> int:\n")
        lines.append(f"    return {index}\n")
    return "".join(lines)


def _build_cutoff_economics(
    mode: str,
    payload_size: int,
    *,
    use_cutoff: bool,
) -> tuple[BenchCall, Path, CleanupFn]:
    files = FileResource()

    def ast_cutoff(source: str) -> tuple[str, str]:
        try:
            return ("ast", ast.dump(ast.parse(source), include_attributes=False))
        except SyntaxError:
            return ("source", source)

    if use_cutoff:

        @query(cutoff=ast_cutoff)
        def source_query(db: Database, path: str) -> str:
            return files.read(db, path)

    else:

        @query
        def source_query(db: Database, path: str) -> str:
            return files.read(db, path)

    @query
    def analysis_query(db: Database, path: str) -> tuple[int, int]:
        return _analysis_counts(source_query(db, path))

    tmpdir = tempfile.TemporaryDirectory()
    path = Path(tmpdir.name) / "module.py"
    path.write_text(_comment_heavy_module(payload_size), encoding="utf-8")
    db = Database(mode=mode)

    def observe() -> dict[str, int]:
        inspection = db.inspect(analysis_query, str(path))
        source_node = _find_node(inspection, "source_query")
        return _merged_markers(
            _decision_markers(inspection, "analysis"),
            _decision_markers(source_node, "source"),
        )

    call = _query_call(db, analysis_query, str(path), observe=observe)
    return call, path, tmpdir.cleanup


def benchmark_cutoff_economics(config: BenchConfig, mode: str) -> ScenarioResult:
    cutoff_call, cutoff_path, cutoff_cleanup = _build_cutoff_economics(mode, config.payload_size, use_cutoff=True)
    try:
        baseline = cutoff_call.invoke()
        cutoff_path.write_text(_comment_heavy_module(config.payload_size) + "# comment\n", encoding="utf-8")
        comment_only = cutoff_call.invoke()
        cutoff_inspection = cutoff_call.inspect()
        invariants = [
            _require(comment_only == baseline, "cutoff_economics_equal_output", "comment-only edits keep the semantic result stable"),
            _require(
                _find_node(cutoff_inspection, "source_query").last_decision == "backdated",
                "cutoff_economics_backdates_source",
                "the source layer backdates when the AST token is unchanged",
            ),
            _require(
                cutoff_inspection.last_decision == "reused",
                "cutoff_economics_reuses_analysis",
                "the expensive analysis layer reuses when the cutoff stops the invalidation early",
            ),
        ]
    finally:
        cutoff_cleanup()

    plain_call, plain_path, plain_cleanup = _build_cutoff_economics(mode, config.payload_size, use_cutoff=False)
    try:
        plain_call.invoke()
        plain_path.write_text(_comment_heavy_module(config.payload_size) + "# comment\n", encoding="utf-8")
        plain_call.invoke()
        plain_inspection = plain_call.inspect()
        invariants.append(
            _require(
                plain_inspection.last_decision == "backdated",
                "cutoff_economics_plain_analysis_backdates_late",
                "without cutoff the analysis still reruns before backdating",
            ),
        )
    finally:
        plain_cleanup()

    def setup_cutoff_call() -> tuple[BenchCall, CleanupFn]:
        local_call, _, local_cleanup = _build_cutoff_economics(mode, config.payload_size, use_cutoff=True)
        return local_call, local_cleanup

    def setup_cutoff_sequence() -> SequenceFixture:
        local_call, local_path, local_cleanup = _build_cutoff_economics(mode, config.payload_size, use_cutoff=True)
        source = _comment_heavy_module(config.payload_size)

        def prepare(step: int) -> None:
            suffix = "# comment\n" if step % 2 == 0 else ""
            local_path.write_text(source + suffix, encoding="utf-8")

        return SequenceFixture(call=local_call, prepare=prepare, cleanup=local_cleanup)

    def setup_plain_sequence() -> SequenceFixture:
        local_call, local_path, local_cleanup = _build_cutoff_economics(mode, config.payload_size, use_cutoff=False)
        source = _comment_heavy_module(config.payload_size)

        def prepare(step: int) -> None:
            suffix = "# comment\n" if step % 2 == 0 else ""
            local_path.write_text(source + suffix, encoding="utf-8")

        return SequenceFixture(call=local_call, prepare=prepare, cleanup=local_cleanup)

    phases = (
        _measure_cold("cold_full", setup_cutoff_call, rounds=config.rounds),
        _measure_cached("warm_reuse", setup_cutoff_call, config),
        _measure_sequence("with_cutoff_comment_edit", setup_cutoff_sequence, config),
        _measure_fresh_sequence("with_cutoff_comment_edit_fresh", setup_cutoff_sequence, config),
        _measure_sequence("without_cutoff_comment_edit", setup_plain_sequence, config),
        _measure_fresh_sequence("without_cutoff_comment_edit_fresh", setup_plain_sequence, config),
    )
    return _scenario_result(
        key="cutoff_economics",
        suite="micro",
        title="Cutoff Economics",
        why="Measures whether an AST-based cutoff is worth its own comparison cost by avoiding deeper re-analysis on equal edits.",
        mode=mode,
        parameters={"module_items": _scale(config.payload_size, minimum=8, maximum=64, divisor=16)},
        phases=phases,
        invariants=tuple(invariants),
        comparison_pairs=(
            ("warm_vs_cold", "warm_reuse", "cold_full"),
            ("with_cutoff_vs_fresh", "with_cutoff_comment_edit", "with_cutoff_comment_edit_fresh"),
            ("without_cutoff_vs_fresh", "without_cutoff_comment_edit", "without_cutoff_comment_edit_fresh"),
            ("cutoff_vs_plain", "with_cutoff_comment_edit", "without_cutoff_comment_edit"),
        ),
    )


def _build_resource_granularity(mode: str) -> tuple[BenchCall, Path, Path, CleanupFn]:
    files = FileResource()
    stats = FileStatResource()
    directories = DirectoryResource()

    @query
    def file_contents(db: Database, path: str) -> str:
        return files.read(db, path)

    @query
    def file_metadata(db: Database, path: str) -> tuple[bool, int | None, int | None]:
        snapshot = stats.read(db, path)
        if isinstance(snapshot, dict):
            return (
                bool(snapshot["exists"]),
                snapshot["size"],
                snapshot["mtime_ns"],
            )
        return snapshot.exists, snapshot.size, snapshot.mtime_ns

    @query
    def directory_listing(db: Database, path: str) -> tuple[str, ...]:
        return directories.read(db, path)

    @query
    def root(db: Database, path: str) -> tuple[object, object, object]:
        parent = str(Path(path).parent)
        return (
            file_contents(db, path),
            file_metadata(db, path),
            directory_listing(db, parent),
        )

    tmpdir = tempfile.TemporaryDirectory()
    workspace = Path(tmpdir.name)
    target = workspace / "target.txt"
    target.write_text("alpha", encoding="utf-8")
    db = Database(mode=mode)

    def observe() -> dict[str, int]:
        inspection = db.inspect(root, str(target))
        contents_node = _find_node(inspection, f"file[{target}]")
        metadata_node = _find_node(inspection, f"filestat[{target}]")
        listing_node = _find_node(inspection, f"dir[{workspace}]")
        return _merged_markers(
            _decision_markers(inspection, "root"),
            _decision_markers(contents_node, "file_resource"),
            _decision_markers(metadata_node, "stat_resource"),
            _decision_markers(listing_node, "directory_resource"),
        )

    call = _query_call(db, root, str(target), observe=observe)
    return call, target, workspace, tmpdir.cleanup


def benchmark_resource_granularity(config: BenchConfig, mode: str) -> ScenarioResult:
    call, target, workspace, cleanup = _build_resource_granularity(mode)
    try:
        call.invoke()
        original_stat = target.stat()
        target.write_text("bravo", encoding="utf-8")
        os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        call.invoke()
        content_inspection = call.inspect()
        extra = workspace / "extra.txt"
        extra.write_text("sidecar", encoding="utf-8")
        call.invoke()
        listing_inspection = call.inspect()
        invariants = (
            _require(
                _find_node(content_inspection, f"file[{target}]").last_decision == "executed",
                "resource_granularity_content_hits_file_resource",
                "same-stat content changes still invalidate FileResource reads",
            ),
            _require(
                _find_node(content_inspection, f"filestat[{target}]").last_decision == "reused",
                "resource_granularity_content_skips_filestat",
                "stable metadata keeps FileStatResource reused",
            ),
            _require(
                _find_node(content_inspection, f"dir[{workspace}]").last_decision == "reused",
                "resource_granularity_content_skips_directory",
                "content-only edits do not dirty DirectoryResource reads",
            ),
            _require(
                _find_node(listing_inspection, f"dir[{workspace}]").last_decision == "executed",
                "resource_granularity_listing_hits_directory",
                "listing changes invalidate DirectoryResource reads",
            ),
        )
    finally:
        cleanup()

    def setup_call() -> tuple[BenchCall, CleanupFn]:
        local_call, _, _, local_cleanup = _build_resource_granularity(mode)
        return local_call, local_cleanup

    def setup_content_only() -> SequenceFixture:
        local_call, local_target, _, local_cleanup = _build_resource_granularity(mode)
        original_stat = local_target.stat()
        values = ("alpha", "omega")
        current = 0

        def prepare(_: int) -> None:
            nonlocal current
            current = (current + 1) % len(values)
            local_target.write_text(values[current], encoding="utf-8")
            os.utime(local_target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

        return SequenceFixture(call=local_call, prepare=prepare, cleanup=local_cleanup)

    def setup_listing_change() -> SequenceFixture:
        local_call, _, local_workspace, local_cleanup = _build_resource_granularity(mode)
        extra = local_workspace / "extra.txt"

        def prepare(step: int) -> None:
            if step % 2 == 0:
                extra.write_text("sidecar", encoding="utf-8")
            elif extra.exists():
                extra.unlink()

        return SequenceFixture(call=local_call, prepare=prepare, cleanup=local_cleanup)

    phases = (
        _measure_cold("cold_full", setup_call, rounds=config.rounds),
        _measure_cached("warm_reuse", setup_call, config),
        _measure_sequence("content_only", setup_content_only, config),
        _measure_fresh_sequence("content_only_fresh", setup_content_only, config),
        _measure_sequence("listing_change", setup_listing_change, config),
        _measure_fresh_sequence("listing_change_fresh", setup_listing_change, config),
    )
    return _scenario_result(
        key="resource_granularity",
        suite="micro",
        title="Resource Granularity",
        why="Contrasts file content, file metadata, and directory listing resources under changes that should only invalidate one layer.",
        mode=mode,
        parameters={},
        phases=phases,
        invariants=invariants,
        comparison_pairs=(
            ("warm_vs_cold", "warm_reuse", "cold_full"),
            ("content_vs_fresh", "content_only", "content_only_fresh"),
            ("listing_vs_fresh", "listing_change", "listing_change_fresh"),
        ),
    )


def _build_lru_pressure(
    mode: str,
    payload_size: int,
    *,
    max_query_nodes: int | None,
    prime_cache: bool,
) -> tuple[BenchCall, PrepareFn]:
    working_set = _scale(payload_size, minimum=4, maximum=24, divisor=96)
    window = _scale(payload_size, minimum=64, maximum=2048, divisor=1)

    @query
    def window_sum(db: Database, value: int) -> int:
        return sum((value + offset) % 97 for offset in range(window))

    db = Database(mode=mode, max_query_nodes=max_query_nodes)
    current = 0

    def prepare(step: int) -> None:
        nonlocal current
        current = step % working_set

    def invoke() -> int:
        return db.get(window_sum, current)

    def inspect() -> InspectionNode:
        return db.inspect(window_sum, current)

    def observe() -> dict[str, int]:
        return _decision_markers(inspect(), "root")

    if prime_cache:
        for value in range(working_set):
            db.get(window_sum, value)
        current = 0

    return BenchCall(db=db, invoke=invoke, inspect=inspect, observe=observe), prepare


def benchmark_lru_pressure(config: BenchConfig, mode: str) -> ScenarioResult:
    @query
    def echo_number(db: Database, value: int) -> int:
        return value

    db = Database(mode=mode, max_query_nodes=2)
    key_one, _ = db._query_key(echo_number, (1,), {})
    db.get(echo_number, 1)
    db.get(echo_number, 2)
    db.get(echo_number, 3)
    invariants = (
        _require(
            key_one not in db._records,
            "lru_pressure_evicts_oldest_key",
            "bounded query storage prunes the oldest entry under churn",
        ),
    )

    working_set = _scale(config.payload_size, minimum=4, maximum=24, divisor=96)
    retained_nodes = working_set
    evicted_nodes = max(1, working_set // 2)

    def setup_call() -> tuple[BenchCall, CleanupFn]:
        local_call, _ = _build_lru_pressure(mode, config.payload_size, max_query_nodes=retained_nodes, prime_cache=False)
        return local_call, _noop

    def setup_retained() -> SequenceFixture:
        local_call, prepare = _build_lru_pressure(
            mode,
            config.payload_size,
            max_query_nodes=retained_nodes,
            prime_cache=True,
        )
        return SequenceFixture(call=local_call, prepare=prepare, cleanup=_noop)

    def setup_evicted() -> SequenceFixture:
        local_call, prepare = _build_lru_pressure(
            mode,
            config.payload_size,
            max_query_nodes=evicted_nodes,
            prime_cache=True,
        )
        return SequenceFixture(call=local_call, prepare=prepare, cleanup=_noop)

    def setup_fresh() -> SequenceFixture:
        local_call, prepare = _build_lru_pressure(mode, config.payload_size, max_query_nodes=None, prime_cache=False)
        return SequenceFixture(call=local_call, prepare=prepare, cleanup=_noop)

    phases = (
        _measure_cold("cold_full", setup_call, rounds=config.rounds),
        _measure_cached("warm_reuse", setup_call, config),
        _measure_sequence("retained_working_set", setup_retained, config),
        _measure_sequence("evicted_working_set", setup_evicted, config),
        _measure_fresh_sequence("fresh_recompute", setup_fresh, config),
    )
    return _scenario_result(
        key="lru_pressure",
        suite="micro",
        title="LRU Pressure",
        why="Shows how bounded query storage approaches fresh recomputation once the working set no longer fits in cache.",
        mode=mode,
        parameters={"working_set": working_set, "retained_nodes": retained_nodes, "evicted_nodes": evicted_nodes},
        phases=phases,
        invariants=invariants,
        comparison_pairs=(
            ("warm_vs_cold", "warm_reuse", "cold_full"),
            ("retained_vs_fresh", "retained_working_set", "fresh_recompute"),
            ("evicted_vs_fresh", "evicted_working_set", "fresh_recompute"),
            ("retained_vs_evicted", "retained_working_set", "evicted_working_set"),
        ),
    )


def _module_source(index: int, defs_per_file: int) -> str:
    lines = [f"import dep_{index}\n"]
    for inner in range(defs_per_file):
        lines.append(f"def fn_{index}_{inner}() -> int:\n")
        lines.append(f"    return {index + inner}\n")
    if index % 3 == 0:
        lines.append(f"class Box{index}:\n")
        lines.append("    pass\n")
    return "".join(lines)


SOURCE_ANALYSIS_OPERATIONS = (
    "initial_full",
    "no_change_repeat",
    "comment_only_edit",
    "semantic_edit",
)


@dataclass(frozen=True)
class SourceAnalysisWorkspace:
    root: Path
    target: Path
    baseline_source: str
    file_count: int
    definitions_per_file: int
    cleanup: CleanupFn


def _source_analysis_shape(payload_size: int) -> tuple[int, int]:
    file_count = _scale(payload_size, minimum=12, maximum=256, divisor=2)
    defs_per_file = _scale(payload_size, minimum=4, maximum=24, divisor=max(8, file_count // 8))
    return file_count, defs_per_file


def _source_analysis_parameters(payload_size: int) -> dict[str, int]:
    file_count, defs_per_file = _source_analysis_shape(payload_size)
    return {"file_count": file_count, "definitions_per_file": defs_per_file}


def _create_source_analysis_workspace(payload_size: int) -> SourceAnalysisWorkspace:
    file_count, defs_per_file = _source_analysis_shape(payload_size)
    tmpdir = tempfile.TemporaryDirectory()
    root = Path(tmpdir.name)
    target = root / "module_00.py"
    baseline_source = _module_source(0, defs_per_file)
    for index in range(file_count):
        (root / f"module_{index:02d}.py").write_text(_module_source(index, defs_per_file), encoding="utf-8")
    return SourceAnalysisWorkspace(
        root=root,
        target=target,
        baseline_source=baseline_source,
        file_count=file_count,
        definitions_per_file=defs_per_file,
        cleanup=tmpdir.cleanup,
    )


def _build_source_analysis(
    mode: str,
    payload_size: int,
) -> tuple[BenchCall, SourceAnalysisWorkspace]:
    workspace = _create_source_analysis_workspace(payload_size)
    db = Database(mode=mode)

    def invoke() -> tuple[object, ...]:
        return directory_analysis(db, workspace.root)

    def inspect() -> InspectionNode:
        return db.inspect(directory_analysis_payload, str(workspace.root))

    def observe() -> dict[str, int]:
        inspection = inspect()
        source_node = db.inspect(source_text, str(workspace.target))
        return _merged_markers(
            _decision_markers(inspection, "root"),
            {f"source_recompute_{source_node.last_recompute}": 1},
        )

    return BenchCall(db=db, invoke=invoke, inspect=inspect, observe=observe), workspace


def _build_plain_source_analysis(payload_size: int) -> tuple[SimpleBenchCall, SourceAnalysisWorkspace]:
    workspace = _create_source_analysis_workspace(payload_size)

    def invoke() -> tuple[object, ...]:
        return plain_directory_analysis(workspace.root)

    return SimpleBenchCall(invoke=invoke), workspace


def _plain_source_analysis_call_for_workspace(workspace: SourceAnalysisWorkspace) -> SimpleBenchCall:
    def invoke() -> tuple[object, ...]:
        return plain_directory_analysis(workspace.root)

    return SimpleBenchCall(invoke=invoke)


def _prepare_source_analysis_operation(
    call: MeasuredCall,
    workspace: SourceAnalysisWorkspace,
    operation: str,
) -> None:
    if operation == "initial_full":
        return
    call.invoke()
    if operation == "no_change_repeat":
        return
    if operation == "comment_only_edit":
        workspace.target.write_text(workspace.baseline_source + "# trailing comment\n", encoding="utf-8")
        return
    if operation == "semantic_edit":
        workspace.target.write_text(
            workspace.baseline_source.replace("import dep_0", "import dep_rewritten", 1),
            encoding="utf-8",
        )
        return
    raise ValueError(f"unknown source analysis operation: {operation}")


def _setup_incremental_source_analysis_operation(
    mode: str,
    payload_size: int,
    operation: str,
) -> tuple[BenchCall, CleanupFn]:
    call, workspace = _build_source_analysis(mode, payload_size)
    _prepare_source_analysis_operation(call, workspace, operation)
    return call, workspace.cleanup


def _setup_plain_source_analysis_operation(
    payload_size: int,
    operation: str,
) -> tuple[SimpleBenchCall, CleanupFn]:
    call, workspace = _build_plain_source_analysis(payload_size)
    _prepare_source_analysis_operation(call, workspace, operation)
    return call, workspace.cleanup


def _run_source_analysis_operation(
    call: MeasuredCall,
    workspace: SourceAnalysisWorkspace,
    operation: str,
) -> tuple[object, dict[str, int]]:
    _prepare_source_analysis_operation(call, workspace, operation)
    result = call.invoke()
    markers = {} if call.observe is None else dict(call.observe())
    return result, markers


def _source_analysis_operation_note(operation: str, speedup_ratio: float | None) -> str:
    if speedup_ratio is None:
        return f"{operation} did not produce a stable speedup ratio."
    if operation == "initial_full":
        return "Cold full analysis still carries the incremental engine's setup cost."
    if operation == "no_change_repeat":
        return "No-change repeats favor the incremental engine because the root query reuses."
    if operation == "comment_only_edit":
        return "Comment-only edits favor the incremental engine because the source query backdates and the directory result reuses."
    if operation == "semantic_edit":
        return "One-file semantic edits still require targeted recomputation, so the gap is smaller."
    return f"{operation} completed with a meaningful incremental/plain comparison."


def benchmark_source_analysis(config: BenchConfig, mode: str) -> ScenarioResult:
    call, workspace = _build_source_analysis(mode, config.payload_size)
    try:
        call.invoke()
        workspace.target.write_text(workspace.baseline_source + "# trailing comment\n", encoding="utf-8")
        call.invoke()
        comment_inspection = call.inspect()
        comment_source = call.db.inspect(source_text, str(workspace.target))
        comment_file = call.db.inspect(file_analysis_payload, str(workspace.target))
        workspace.target.write_text(
            workspace.baseline_source.replace("import dep_0", "import dep_rewritten", 1),
            encoding="utf-8",
        )
        call.invoke()
        semantic_file = call.db.inspect(file_analysis_payload, str(workspace.target))
        invariants = (
            _require(
                comment_source.last_recompute == "backdated",
                "source_analysis_comment_backdates_source",
                "comment-only file edits backdate the tracked source query",
            ),
            _require(
                comment_file.last_decision == "reused",
                "source_analysis_comment_reuses_file_analysis",
                "comment-only edits reuse the target file analysis payload",
            ),
            _require(
                comment_inspection.last_decision == "reused",
                "source_analysis_comment_reuses_directory",
                "comment-only edits reuse the top-level directory analysis payload",
            ),
            _require(
                semantic_file.last_recompute == "executed",
                "source_analysis_semantic_edit_executes_file_analysis",
                "semantic file edits still execute the affected file analysis",
            ),
        )
    finally:
        workspace.cleanup()

    def setup_call() -> tuple[BenchCall, CleanupFn]:
        local_call, local_workspace = _build_source_analysis(mode, config.payload_size)
        return local_call, local_workspace.cleanup

    def setup_comment_only() -> SequenceFixture:
        local_call, local_workspace = _build_source_analysis(mode, config.payload_size)

        def prepare(step: int) -> None:
            suffix = "# trailing comment\n" if step % 2 == 0 else ""
            local_workspace.target.write_text(local_workspace.baseline_source + suffix, encoding="utf-8")

        return SequenceFixture(call=local_call, prepare=prepare, cleanup=local_workspace.cleanup)

    def setup_semantic() -> SequenceFixture:
        local_call, local_workspace = _build_source_analysis(mode, config.payload_size)

        def prepare(step: int) -> None:
            replacement = f"dep_rewritten_{step}"
            local_workspace.target.write_text(
                local_workspace.baseline_source.replace("import dep_0", f"import {replacement}", 1),
                encoding="utf-8",
            )

        return SequenceFixture(call=local_call, prepare=prepare, cleanup=local_workspace.cleanup)

    phases = (
        _measure_cold("cold_full", setup_call, rounds=config.rounds),
        _measure_cached("warm_reuse", setup_call, config),
        _measure_sequence("comment_only_backdate", setup_comment_only, config),
        _measure_fresh_sequence("comment_only_backdate_fresh", setup_comment_only, config),
        _measure_sequence("semantic_edit", setup_semantic, config),
        _measure_fresh_sequence("semantic_edit_fresh", setup_semantic, config),
    )
    return _scenario_result(
        key="source_analysis",
        suite="workload",
        title="Python Source Analysis",
        why="Benchmarks the real reference workload: flat-directory source analysis with one-file edits versus fresh whole-directory recomputation.",
        mode=mode,
        parameters=_source_analysis_parameters(config.payload_size),
        phases=phases,
        invariants=invariants,
        comparison_pairs=(
            ("warm_vs_cold", "warm_reuse", "cold_full"),
            ("comment_vs_fresh", "comment_only_backdate", "comment_only_backdate_fresh"),
            ("semantic_vs_fresh", "semantic_edit", "semantic_edit_fresh"),
        ),
    )


def benchmark_source_analysis_plain(config: BenchConfig) -> ScenarioResult:
    def setup_operation(operation: str) -> tuple[SimpleBenchCall, CleanupFn]:
        return _setup_plain_source_analysis_operation(config.payload_size, operation)

    phases = tuple(
        _measure_prepared(operation, partial(setup_operation, operation), config)
        for operation in SOURCE_ANALYSIS_OPERATIONS
    )
    invariants = (
        _require(True, "plain_source_analysis_baseline", "plain baseline performs direct whole-directory analysis on every operation"),
    )
    return _scenario_result(
        key="source_analysis",
        suite="workload",
        title="Python Source Analysis",
        why="Benchmarks the plain stdlib baseline without incremental computation.",
        mode="plain",
        parameters=_source_analysis_parameters(config.payload_size),
        phases=phases,
        invariants=invariants,
        comparison_pairs=(),
    )


def benchmark_source_analysis_compare(config: BenchConfig, mode: str) -> WorkloadScenarioResult:
    invariants: list[InvariantResult] = []
    operations: list[WorkloadOperationResult] = []

    for operation in SOURCE_ANALYSIS_OPERATIONS:
        incremental_call, incremental_workspace = _build_source_analysis(mode, config.payload_size)
        try:
            incremental_result, incremental_markers = _run_source_analysis_operation(
                incremental_call,
                incremental_workspace,
                operation,
            )
            plain_call = _plain_source_analysis_call_for_workspace(incremental_workspace)
            plain_result = plain_call.invoke()
        finally:
            incremental_workspace.cleanup()

        invariants.append(
            _require(
                incremental_result == plain_result,
                f"source_analysis_{operation}_matches_plain",
                f"incremental and plain source analysis agree for {operation}",
            ),
        )
        if operation == "no_change_repeat":
            invariants.append(
                _require(
                    incremental_markers.get("root_reused", 0) > 0,
                    "source_analysis_no_change_reuses_root",
                    "no-change repeats reuse the directory analysis root",
                ),
            )
        if operation == "comment_only_edit":
            invariants.extend(
                [
                    _require(
                        incremental_markers.get("root_reused", 0) > 0,
                        "source_analysis_comment_edit_reuses_root",
                        "comment-only edits reuse the directory analysis root",
                    ),
                    _require(
                        incremental_markers.get("source_recompute_backdated", 0) > 0,
                        "source_analysis_comment_edit_backdates_source",
                        "comment-only edits backdate the tracked source query",
                    ),
                ],
            )
        if operation == "semantic_edit":
            invariants.extend(
                [
                    _require(
                        incremental_markers.get("root_executed", 0) > 0,
                        "source_analysis_semantic_edit_executes_root",
                        "semantic edits re-execute the directory analysis root",
                    ),
                    _require(
                        incremental_markers.get("source_recompute_executed", 0) > 0,
                        "source_analysis_semantic_edit_executes_source",
                        "semantic edits execute the tracked source query",
                    ),
                ],
            )

        incremental_phase = _measure_prepared(
            operation,
            partial(_setup_incremental_source_analysis_operation, mode, config.payload_size, operation),
            config,
        )
        plain_phase = _measure_prepared(
            operation,
            partial(_setup_plain_source_analysis_operation, config.payload_size, operation),
            config,
        )
        comparison = _phase_speedup("plain_vs_incremental", "incremental", incremental_phase, "plain", plain_phase)
        operations.append(
            WorkloadOperationResult(
                name=operation,
                measurements=(
                    ImplementationPhaseResult(implementation="incremental", phase=incremental_phase),
                    ImplementationPhaseResult(implementation="plain", phase=plain_phase),
                ),
                comparison=comparison,
                note=_source_analysis_operation_note(operation, comparison.speedup_ratio),
            ),
        )

    return WorkloadScenarioResult(
        key="source_analysis",
        suite="workload",
        title="Python Source Analysis",
        why="Compares the incremental integration against the plain stdlib baseline on the same workload operations.",
        parameters=_source_analysis_parameters(config.payload_size),
        operations=tuple(operations),
        invariants=tuple(invariants),
    )


SCENARIOS = (
    ScenarioSpec(
        key="diamond_reuse",
        suite="micro",
        title="Diamond Reuse",
        why="Shows fanout reuse in a minimal diamond graph.",
        run=benchmark_diamond,
    ),
    ScenarioSpec(
        key="dynamic_rewiring",
        suite="micro",
        title="Dynamic Rewiring",
        why="Shows dependency rewiring when active branches change.",
        run=benchmark_rewiring,
    ),
    ScenarioSpec(
        key="resource_reads",
        suite="micro",
        title="Resource Reads",
        why="Measures tracked file and directory reads.",
        run=benchmark_file_resources,
    ),
    ScenarioSpec(
        key="large_boundary",
        suite="micro",
        title="Large Boundary",
        why="Measures large equal-value boundary updates.",
        run=benchmark_large_boundary,
    ),
    ScenarioSpec(
        key="query_backdating",
        suite="micro",
        title="Query Backdating",
        why="Measures single-node backdating and downstream reuse.",
        run=benchmark_query_backdating,
    ),
    ScenarioSpec(
        key="backdating_chain",
        suite="micro",
        title="Backdating Chain",
        why="Measures deeper fanout backdating.",
        run=benchmark_backdating_chain,
    ),
    ScenarioSpec(
        key="rewiring_torture",
        suite="micro",
        title="Rewiring Torture",
        why="Separates active-branch work from inactive churn.",
        run=benchmark_rewiring_torture,
    ),
    ScenarioSpec(
        key="cutoff_economics",
        suite="micro",
        title="Cutoff Economics",
        why="Compares comment-only edits with and without cutoff.",
        run=benchmark_cutoff_economics,
    ),
    ScenarioSpec(
        key="resource_granularity",
        suite="micro",
        title="Resource Granularity",
        why="Compares file content, file metadata, and directory granularity.",
        run=benchmark_resource_granularity,
    ),
    ScenarioSpec(
        key="lru_pressure",
        suite="micro",
        title="LRU Pressure",
        why="Measures the cost of bounded query caches under churn.",
        run=benchmark_lru_pressure,
    ),
    ScenarioSpec(
        key="source_analysis",
        suite="workload",
        title="Python Source Analysis",
        why="Benchmarks the reference source-analysis integration.",
        run=benchmark_source_analysis,
    ),
)

SCENARIO_INDEX = {scenario.key: scenario for scenario in SCENARIOS}
PLAIN_WORKLOAD_RUNNERS = {
    "source_analysis": benchmark_source_analysis_plain,
}
COMPARE_WORKLOAD_RUNNERS = {
    "source_analysis": benchmark_source_analysis_compare,
}


def _selected_scenarios(suite: str, bench: str) -> tuple[str, ...]:
    if bench != "all":
        key = ALIASES.get(bench, bench)
        if key not in SCENARIO_INDEX:
            raise ValueError(f"unknown benchmark scenario: {bench}")
        return (key,)
    if suite == "micro":
        return tuple(scenario.key for scenario in SCENARIOS if scenario.suite == "micro")
    if suite == "workload":
        return tuple(scenario.key for scenario in SCENARIOS if scenario.suite == "workload")
    return tuple(scenario.key for scenario in SCENARIOS)


def _selected_scenario_specs(suite: str, bench: str) -> tuple[ScenarioSpec, ...]:
    return tuple(SCENARIO_INDEX[key] for key in _selected_scenarios(suite, bench))


def _build_environment() -> dict[str, Any]:
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "timestamp_utc": datetime.now(UTC).isoformat(),
    }


def _build_report_config(
    *,
    config: BenchConfig,
    suite: str,
    bench: str,
    mode_override: str | None,
    implementation: str,
    selected: Sequence[str],
) -> dict[str, Any]:
    return {
        "suite": suite,
        "bench": bench,
        "selected": list(selected),
        "samples": config.samples,
        "warmup": config.warmup,
        "rounds": config.rounds,
        "payload_size": config.payload_size,
        "mode_override": mode_override,
        "implementation": implementation,
    }


def run_benchmarks(
    *,
    config: BenchConfig,
    suite: str,
    bench: str,
    mode_override: str | None,
) -> BenchReport:
    selected = _selected_scenarios(suite, bench)
    results = tuple(
        SCENARIO_INDEX[scenario_key].run(config, _resolved_mode(scenario_key, mode_override))
        for scenario_key in selected
    )
    return BenchReport(
        environment=_build_environment(),
        config=_build_report_config(
            config=config,
            suite=suite,
            bench=bench,
            mode_override=mode_override,
            implementation="incremental",
            selected=selected,
        ),
        results=results,
    )


def run_plain_workload_benchmarks(
    *,
    config: BenchConfig,
    suite: str,
    bench: str,
) -> BenchReport:
    selected = _selected_scenarios(suite, bench)
    results = tuple(PLAIN_WORKLOAD_RUNNERS[scenario_key](config) for scenario_key in selected)
    return BenchReport(
        environment=_build_environment(),
        config=_build_report_config(
            config=config,
            suite=suite,
            bench=bench,
            mode_override=None,
            implementation="plain",
            selected=selected,
        ),
        results=results,
    )


def run_workload_comparison(
    *,
    config: BenchConfig,
    suite: str,
    bench: str,
    mode_override: str | None,
) -> WorkloadComparisonReport:
    selected = _selected_scenarios(suite, bench)
    results = tuple(
        COMPARE_WORKLOAD_RUNNERS[scenario_key](config, _resolved_mode(scenario_key, mode_override))
        for scenario_key in selected
    )
    return WorkloadComparisonReport(
        environment=_build_environment(),
        config=_build_report_config(
            config=config,
            suite=suite,
            bench=bench,
            mode_override=mode_override,
            implementation="compare",
            selected=selected,
        ),
        implementations=("incremental", "plain"),
        results=results,
    )


def render_json(report: BenchReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)


def render_compare_json(report: WorkloadComparisonReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True)


def render_table(report: BenchReport) -> str:
    rows: list[list[str]] = [
        ["scenario", "phase", "mode", "n", "mean_ms", "p95_ms", "ops/s", "vs_fresh", "markers"],
    ]
    for scenario in report.results:
        for phase in scenario.phases:
            rows.append(
                [
                    scenario.key,
                    phase.name,
                    scenario.mode,
                    str(phase.metrics.count),
                    f"{phase.metrics.mean_s * 1000:.3f}",
                    f"{phase.metrics.p95_s * 1000:.3f}",
                    f"{phase.metrics.ops_per_s:.1f}",
                    _format_ratio(scenario.vs_fresh(phase.name)),
                    _format_markers(phase.markers),
                ],
            )
    lines = [
        "pyfoundinc benchmarks",
        (
            "python="
            f"{report.environment['python_version']} "
            f"impl={report.environment['python_implementation']} "
            f"platform={report.environment['platform']}"
        ),
        (
            "suite="
            f"{report.config['suite']} "
            f"bench={report.config['bench']} "
            f"samples={report.config['samples']} "
            f"warmup={report.config['warmup']} "
            f"rounds={report.config['rounds']} "
            f"payload_size={report.config['payload_size']}"
        ),
        "",
        _render_table_rows(rows),
    ]
    return "\n".join(lines)


def render_compare_table(report: WorkloadComparisonReport) -> str:
    rows: list[list[str]] = [
        [
            "scenario",
            "operation",
            "inc_mean_ms",
            "inc_p95_ms",
            "plain_mean_ms",
            "plain_p95_ms",
            "speedup_pct",
            "incremental_markers",
        ],
    ]
    for scenario in report.results:
        for operation in scenario.operations:
            incremental = operation.measurement("incremental")
            plain = operation.measurement("plain")
            rows.append(
                [
                    scenario.key,
                    operation.name,
                    "-" if incremental is None else f"{incremental.phase.metrics.mean_s * 1000:.3f}",
                    "-" if incremental is None else f"{incremental.phase.metrics.p95_s * 1000:.3f}",
                    "-" if plain is None else f"{plain.phase.metrics.mean_s * 1000:.3f}",
                    "-" if plain is None else f"{plain.phase.metrics.p95_s * 1000:.3f}",
                    _format_percentage_ratio(operation.speedup()),
                    "-" if incremental is None else _format_markers(incremental.phase.markers),
                ],
            )
    lines = [
        "pyfoundinc workload comparison",
        (
            "python="
            f"{report.environment['python_version']} "
            f"impl={report.environment['python_implementation']} "
            f"platform={report.environment['platform']}"
        ),
        (
            "suite="
            f"{report.config['suite']} "
            f"bench={report.config['bench']} "
            f"implementations={','.join(report.implementations)} "
            f"samples={report.config['samples']} "
            f"warmup={report.config['warmup']} "
            f"rounds={report.config['rounds']} "
            f"payload_size={report.config['payload_size']}"
        ),
        "",
        _render_table_rows(rows),
    ]
    return "\n".join(lines)


def render_markdown(report: BenchReport) -> str:
    lines = [
        "# pyfoundinc benchmark report",
        "",
        (
            f"- Python: `{report.environment['python_version']}` "
            f"({report.environment['python_implementation']}) on `{report.environment['platform']}`"
        ),
        (
            f"- Config: `suite={report.config['suite']}` "
            f"`bench={report.config['bench']}` "
            f"`samples={report.config['samples']}` "
            f"`warmup={report.config['warmup']}` "
            f"`rounds={report.config['rounds']}` "
            f"`payload_size={report.config['payload_size']}`"
        ),
        "",
        "## Summary",
        "",
        "| scenario | phase | mode | mean_ms | p95_ms | ops/s | vs_fresh |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for scenario in report.results:
        for phase in scenario.phases:
            lines.append(
                "| "
                + " | ".join(
                    [
                        scenario.key,
                        phase.name,
                        scenario.mode,
                        f"{phase.metrics.mean_s * 1000:.3f}",
                        f"{phase.metrics.p95_s * 1000:.3f}",
                        f"{phase.metrics.ops_per_s:.1f}",
                        _format_ratio(scenario.vs_fresh(phase.name)),
                    ],
                )
                + " |"
            )
    lines.append("")

    grouped: dict[str, list[ScenarioResult]] = defaultdict(list)
    for result in report.results:
        grouped[result.suite].append(result)

    for suite in ("micro", "workload"):
        if suite not in grouped:
            continue
        lines.extend([f"## {suite.title()}", ""])
        for scenario in grouped[suite]:
            lines.extend(
                [
                    f"### {scenario.title}",
                    "",
                    f"Why this matters: {scenario.why}",
                    "",
                    f"Invariant checks: {len(scenario.invariants)} passed.",
                    "",
                    "| phase | mean_ms | p95_ms | total_ms | ops/s | vs_fresh | markers |",
                    "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
                ],
            )
            for phase in scenario.phases:
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            phase.name,
                            f"{phase.metrics.mean_s * 1000:.3f}",
                            f"{phase.metrics.p95_s * 1000:.3f}",
                            f"{phase.metrics.total_s * 1000:.3f}",
                            f"{phase.metrics.ops_per_s:.1f}",
                            _format_ratio(scenario.vs_fresh(phase.name)),
                            _format_markers(phase.markers),
                        ],
                    )
                    + " |"
                )
            lines.extend(
                [
                    "",
                    f"Interpretation: {scenario.interpretation()}",
                    "",
                ],
            )
    return "\n".join(lines).rstrip()


def render_compare_markdown(report: WorkloadComparisonReport) -> str:
    lines = [
        "# pyfoundinc workload comparison report",
        "",
        (
            f"- Python: `{report.environment['python_version']}` "
            f"({report.environment['python_implementation']}) on `{report.environment['platform']}`"
        ),
        (
            f"- Config: `suite={report.config['suite']}` "
            f"`bench={report.config['bench']}` "
            f"`implementation={report.config['implementation']}` "
            f"`samples={report.config['samples']}` "
            f"`warmup={report.config['warmup']}` "
            f"`rounds={report.config['rounds']}` "
            f"`payload_size={report.config['payload_size']}`"
        ),
        "",
        "## Summary",
        "",
        "| scenario | operation | incremental_mean_ms | plain_mean_ms | speedup_pct | incremental_markers |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for scenario in report.results:
        for operation in scenario.operations:
            incremental = operation.measurement("incremental")
            plain = operation.measurement("plain")
            lines.append(
                "| "
                + " | ".join(
                    [
                        scenario.key,
                        operation.name,
                        "-" if incremental is None else f"{incremental.phase.metrics.mean_s * 1000:.3f}",
                        "-" if plain is None else f"{plain.phase.metrics.mean_s * 1000:.3f}",
                        _format_percentage_ratio(operation.speedup()),
                        "-" if incremental is None else _format_markers(incremental.phase.markers),
                    ],
                )
                + " |"
            )
    lines.append("")
    lines.extend(["## Workload", ""])
    for scenario in report.results:
        lines.extend(
            [
                f"### {scenario.title}",
                "",
                f"Why this matters: {scenario.why}",
                "",
                f"Invariant checks: {len(scenario.invariants)} passed.",
                "",
                "| operation | incremental_mean_ms | incremental_p95_ms | plain_mean_ms | plain_p95_ms | speedup_pct | incremental_markers |",
                "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
            ],
        )
        for operation in scenario.operations:
            incremental = operation.measurement("incremental")
            plain = operation.measurement("plain")
            lines.append(
                "| "
                + " | ".join(
                    [
                        operation.name,
                        "-" if incremental is None else f"{incremental.phase.metrics.mean_s * 1000:.3f}",
                        "-" if incremental is None else f"{incremental.phase.metrics.p95_s * 1000:.3f}",
                        "-" if plain is None else f"{plain.phase.metrics.mean_s * 1000:.3f}",
                        "-" if plain is None else f"{plain.phase.metrics.p95_s * 1000:.3f}",
                        _format_percentage_ratio(operation.speedup()),
                        "-" if incremental is None else _format_markers(incremental.phase.markers),
                    ],
                )
                + " |"
            )
        lines.extend(["", f"Interpretation: {scenario.interpretation()}", ""])
    return "\n".join(lines).rstrip()


def _format_ratio(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}x"


def _format_percentage_ratio(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.0f}%"


def _format_markers(markers: Mapping[str, int]) -> str:
    if not markers:
        return "-"
    parts = [f"{key}={value}" for key, value in markers.items()]
    text = ", ".join(parts)
    return text if len(text) <= 48 else text[:45] + "..."


def _render_table_rows(rows: Sequence[Sequence[str]]) -> str:
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    rendered: list[str] = []
    for row_index, row in enumerate(rows):
        padded = "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))
        rendered.append(padded.rstrip())
        if row_index == 0:
            rendered.append("  ".join("-" * width for width in widths).rstrip())
    return "\n".join(rendered)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run pyfoundinc benchmark suites.")
    parser.add_argument("--samples", type=int, default=30, help="Measured iterations per phase and round.")
    parser.add_argument("--warmup", type=int, default=5, help="Untimed warmup iterations before each measured phase.")
    parser.add_argument("--rounds", type=int, default=3, help="Independent benchmark rounds per phase.")
    parser.add_argument(
        "--payload-size",
        type=int,
        default=512,
        help="Generic scale factor used by large-boundary and workload scenarios.",
    )
    parser.add_argument(
        "--suite",
        choices=["micro", "workload", "all"],
        default="all",
        help="Run the micro suite, the workload suite, or both.",
    )
    parser.add_argument(
        "--bench",
        default="all",
        help="Run a single scenario or the full selected suite. Supports old aliases like 'diamond' and 'backdating'.",
    )
    parser.add_argument(
        "--mode",
        choices=["strict", "checked", "fast"],
        default=None,
        help="Override the default mode for all selected scenarios.",
    )
    parser.add_argument(
        "--implementation",
        choices=["incremental", "plain", "compare"],
        default="incremental",
        help="Choose the incremental engine, the plain baseline, or a workload comparison report.",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json", "markdown"],
        default="table",
        help="Primary stdout format.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Optional path for writing the JSON report artifact.",
    )
    parser.add_argument(
        "--output-markdown",
        type=Path,
        default=None,
        help="Optional path for writing the Markdown report artifact.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Backward-compatible alias for --output-json.",
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.samples <= 0:
        parser.error("--samples must be a positive integer")
    if args.warmup < 0:
        parser.error("--warmup must be zero or a positive integer")
    if args.rounds <= 0:
        parser.error("--rounds must be a positive integer")
    if args.payload_size <= 0:
        parser.error("--payload-size must be a positive integer")
    if args.output is not None and args.output_json is not None:
        parser.error("use either --output or --output-json, not both")
    if args.bench != "all":
        key = ALIASES.get(args.bench, args.bench)
        if key not in SCENARIO_INDEX:
            parser.error(f"--bench must be one of: all, {', '.join(sorted(SCENARIO_INDEX | ALIASES))}")
    if args.implementation != "incremental":
        selected_specs = _selected_scenario_specs(args.suite, args.bench)
        if any(spec.suite != "workload" for spec in selected_specs):
            parser.error("--implementation plain/compare is only supported for workload scenarios")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)

    config = BenchConfig(
        samples=args.samples,
        warmup=args.warmup,
        rounds=args.rounds,
        payload_size=args.payload_size,
    )
    if args.implementation == "incremental":
        incremental_report = run_benchmarks(
            config=config,
            suite=args.suite,
            bench=args.bench,
            mode_override=args.mode,
        )
        if args.format == "json":
            stdout_payload = render_json(incremental_report)
        elif args.format == "markdown":
            stdout_payload = render_markdown(incremental_report)
        else:
            stdout_payload = render_table(incremental_report)
        output_json_payload = render_json(incremental_report)
        output_markdown_payload = render_markdown(incremental_report)
    elif args.implementation == "plain":
        plain_report = run_plain_workload_benchmarks(
            config=config,
            suite=args.suite,
            bench=args.bench,
        )
        if args.format == "json":
            stdout_payload = render_json(plain_report)
        elif args.format == "markdown":
            stdout_payload = render_markdown(plain_report)
        else:
            stdout_payload = render_table(plain_report)
        output_json_payload = render_json(plain_report)
        output_markdown_payload = render_markdown(plain_report)
    else:
        compare_report = run_workload_comparison(
            config=config,
            suite=args.suite,
            bench=args.bench,
            mode_override=args.mode,
        )
        if args.format == "json":
            stdout_payload = render_compare_json(compare_report)
        elif args.format == "markdown":
            stdout_payload = render_compare_markdown(compare_report)
        else:
            stdout_payload = render_compare_table(compare_report)
        output_json_payload = render_compare_json(compare_report)
        output_markdown_payload = render_compare_markdown(compare_report)
    print(stdout_payload)

    output_json = args.output_json or args.output
    if output_json is not None:
        _write_text(output_json, output_json_payload)
    if args.output_markdown is not None:
        _write_text(args.output_markdown, output_markdown_payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
