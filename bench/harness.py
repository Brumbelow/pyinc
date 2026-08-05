"""Scenario orchestration, release-gate validation, and artifact writing."""

from __future__ import annotations

import csv
import json
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import labels
from .baselines import required_comparators
from .measure import ScenarioResult, WorkMetrics

ALL_TARGETS: tuple[str, ...] = ("synthetic", "calc", "codegen", "action")
REPETITIONS = 5
ROWS_PER_REPETITION = 67

_TARGET_MATRIX: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "synthetic": (
        ("cold", ("pyinc", "full", "naive", "joblib")),
        ("unchanged", ("pyinc", "full", "naive", "joblib")),
        ("localized_semantic_edit", ("pyinc", "full", "naive", "joblib")),
        ("high_fanout_shared_edit", ("pyinc", "full", "naive", "joblib")),
        ("checkpoint_restore", ("pyinc",)),
    ),
    "calc": (
        ("cold", ("pyinc", "full", "naive")),
        ("unchanged", ("pyinc", "full", "naive")),
        ("unreferenced_file_edit", ("pyinc", "full", "naive")),
        ("comment_only_referenced_edit", ("pyinc", "full", "naive")),
        ("localized_semantic_edit", ("pyinc", "full", "naive")),
        ("high_fanout_shared_edit", ("pyinc", "full", "naive")),
        ("removed_emitted_artifact", ("pyinc", "full", "naive")),
        ("tampered_generated_output", ("pyinc", "full", "naive")),
        ("checkpoint_restore", ("pyinc",)),
    ),
    "codegen": (
        ("cold", ("pyinc", "full")),
        ("unchanged", ("pyinc", "full")),
        ("comment_only_referenced_edit", ("pyinc", "full")),
        ("localized_semantic_edit", ("pyinc", "full")),
        ("high_fanout_shared_edit", ("pyinc", "full")),
        ("removed_emitted_artifact", ("pyinc", "full")),
        ("tampered_generated_output", ("pyinc", "full")),
        ("checkpoint_restore", ("pyinc",)),
    ),
    "action": (
        ("cold", ("pyinc", "full")),
        ("unchanged", ("pyinc", "full")),
        ("high_fanout_shared_edit", ("pyinc", "full")),
        ("removed_emitted_artifact", ("pyinc", "full")),
        ("tampered_generated_output", ("pyinc", "full")),
    ),
}


def _expected_rows(targets: Sequence[str]) -> tuple[tuple[str, str, str], ...]:
    rows: list[tuple[str, str, str]] = []
    for target in targets:
        try:
            matrix = _TARGET_MATRIX[target]
        except KeyError as error:
            raise KeyError(f"unknown bench target: {target!r}") from error
        rows.extend(
            (target, scenario, engine) for scenario, engines in matrix for engine in engines
        )
    return tuple(rows)


EXPECTED_ROW_KEYS = _expected_rows(ALL_TARGETS)
INTENTIONAL_STALE: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("synthetic", "high_fanout_shared_edit", "naive"),
        ("calc", "tampered_generated_output", "naive"),
    }
)
NODE_CEILINGS: dict[str, int] = {
    "synthetic": 16,
    "calc": 24,
    "codegen": 40,
    "action": 8,
}

CountExpectation = int | tuple[int, int]


@dataclass(frozen=True)
class WorkExpectation:
    query_executions: CountExpectation
    query_reuses_fixture: int
    query_backdates: CountExpectation
    resource_loads: CountExpectation
    memo_nodes: CountExpectation
    memo_node_delta: CountExpectation
    dep_graph_edges: CountExpectation
    dep_graph_edge_delta: CountExpectation


def _work(
    query_executions: CountExpectation,
    query_reuses_fixture: int,
    query_backdates: CountExpectation,
    resource_loads: CountExpectation,
    memo_nodes: CountExpectation,
    memo_node_delta: CountExpectation,
    dep_graph_edges: CountExpectation,
    dep_graph_edge_delta: CountExpectation,
) -> WorkExpectation:
    return WorkExpectation(
        query_executions,
        query_reuses_fixture,
        query_backdates,
        resource_loads,
        memo_nodes,
        memo_node_delta,
        dep_graph_edges,
        dep_graph_edge_delta,
    )


# These envelopes are the reviewed deterministic-work contract for the release
# harness. Every expectation field is enforced absolutely. The separate
# call-level query-reuse value only seeds fixtures; the five-run gate checks its
# same-path determinism without presenting it as a cross-path envelope. Codegen
# removal has an execution range
# because digest-sorted verification can execute either 8 or 12 nodes. Those
# ceilings remain far below a cold/full graph, so deterministic
# over-recomputation fails the release gate.
PYINC_WORK_EXPECTATIONS: dict[tuple[str, str], WorkExpectation] = {
    ("synthetic", "cold"): _work(7, 0, 0, 0, 14, 7, 18, 18),
    ("synthetic", "unchanged"): _work(0, 7, 0, 0, 14, 0, 18, 0),
    ("synthetic", "localized_semantic_edit"): _work(2, 11, 0, 0, 14, 0, 18, 0),
    ("synthetic", "high_fanout_shared_edit"): _work(7, 1, 0, 0, 14, 0, 18, 0),
    ("synthetic", "checkpoint_restore"): _work(1, 6, 0, 0, 14, 7, 18, 18),
    ("calc", "cold"): _work(15, 23, 0, 2, 17, 17, 22, 22),
    ("calc", "unchanged"): _work(0, 38, 0, 0, 17, 0, 22, 0),
    ("calc", "unreferenced_file_edit"): _work(0, 38, 0, 0, 17, 0, 22, 0),
    ("calc", "comment_only_referenced_edit"): _work(1, 37, 1, 1, 17, 0, 22, 0),
    ("calc", "localized_semantic_edit"): _work(5, 38, 5, 1, 17, 0, 22, 0),
    ("calc", "high_fanout_shared_edit"): _work(7, 42, 4, 1, 17, 0, 22, 0),
    ("calc", "removed_emitted_artifact"): _work(4, 28, 4, 1, 17, 0, 22, 0),
    ("calc", "tampered_generated_output"): _work(0, 29, 0, 0, 17, 0, 22, 0),
    ("calc", "checkpoint_restore"): _work(0, 3, 0, 0, 15, 15, 19, 19),
    ("codegen", "cold"): _work(29, 175, 0, 1, 30, 30, 43, 43),
    ("codegen", "unchanged"): _work(0, 204, 0, 0, 30, 0, 43, 0),
    ("codegen", "comment_only_referenced_edit"): _work(1, 203, 10, 1, 30, 0, 43, 0),
    ("codegen", "localized_semantic_edit"): _work(6, 205, 10, 1, 30, 0, 43, 0),
    ("codegen", "high_fanout_shared_edit"): _work(6, 216, 14, 1, 30, 0, 43, 0),
    ("codegen", "removed_emitted_artifact"): _work((8, 12), 160, 7, 1, 30, 0, 40, -3),
    ("codegen", "tampered_generated_output"): _work(0, 154, 0, 0, 30, 0, 40, 0),
    ("codegen", "checkpoint_restore"): _work(0, 37, 0, 0, 24, 24, 33, 33),
    ("action", "cold"): _work(3, 0, 0, 0, 5, 3, 3, 3),
    ("action", "unchanged"): _work(0, 3, 0, 0, 5, 0, 3, 0),
    ("action", "high_fanout_shared_edit"): _work(3, 0, 0, 0, 5, 0, 3, 0),
    ("action", "removed_emitted_artifact"): _work(0, 2, 0, 0, 5, 0, 3, 0),
    ("action", "tampered_generated_output"): _work(0, 2, 0, 0, 5, 0, 3, 0),
}


def _minimum(expectation: CountExpectation) -> int:
    return expectation if isinstance(expectation, int) else expectation[0]


def expected_work_metrics(target: str, scenario: str) -> WorkMetrics:
    """Return one fixture vector for the benchmark work contract."""
    expectation = PYINC_WORK_EXPECTATIONS[(target, scenario)]
    return WorkMetrics(
        query_executions=_minimum(expectation.query_executions),
        query_reuses=expectation.query_reuses_fixture,
        query_backdates=_minimum(expectation.query_backdates),
        resource_loads=_minimum(expectation.resource_loads),
        memo_nodes=_minimum(expectation.memo_nodes),
        memo_node_delta=_minimum(expectation.memo_node_delta),
        dep_graph_edges=_minimum(expectation.dep_graph_edges),
        dep_graph_edge_delta=_minimum(expectation.dep_graph_edge_delta),
    )


_WORK_FIELDS = (
    "query_executions",
    "query_reuses",
    "query_backdates",
    "resource_loads",
    "memo_nodes",
    "memo_node_delta",
    "dep_graph_edges",
    "dep_graph_edge_delta",
)
_AUTHORITATIVE_WORK_FIELDS = (
    "query_executions",
    "query_backdates",
    "resource_loads",
    "memo_nodes",
    "memo_node_delta",
    "dep_graph_edges",
    "dep_graph_edge_delta",
)
_SAMPLE_FIELDS = (
    "repetition",
    "target",
    "scenario",
    "engine",
    "wall_seconds",
    *_WORK_FIELDS,
    "matches_fresh",
)
_SUMMARY_FIELDS = (
    "target",
    "scenario",
    "engine",
    "median_wall_seconds",
    "min_wall_seconds",
    "max_wall_seconds",
    *_WORK_FIELDS,
    "matches_fresh",
)


@dataclass(frozen=True)
class BenchmarkSummary:
    target: str
    scenario: str
    engine: str
    median_seconds: float
    min_seconds: float
    max_seconds: float
    matches_fresh: bool
    query_executions: int | None
    query_reuses: int | None
    query_backdates: int | None
    resource_loads: int | None
    memo_nodes: int | None
    memo_node_delta: int | None
    dep_graph_edges: int | None
    dep_graph_edge_delta: int | None


def run_scenarios(
    targets: Iterable[str],
    *,
    out_dir: str | Path,
    comparators: Sequence[str] | None = None,
) -> list[ScenarioResult]:
    from . import scenarios

    comps = required_comparators()
    if comparators is not None and tuple(comparators) != comps:
        raise ValueError(f"benchmark comparator set is fixed at {comps!r}")
    results: list[ScenarioResult] = []
    for name in targets:
        target = scenarios.TARGETS.get(name)
        if target is None:
            raise KeyError(f"unknown bench target: {name!r}")
        results.extend(target(out_dir=Path(out_dir), comparators=comps))
    return results


def _key(result: ScenarioResult) -> tuple[str, str, str]:
    return (result.target, result.scenario, result.engine)


def _work_values(result: ScenarioResult) -> tuple[int | None, ...]:
    return (
        result.query_executions,
        result.query_reuses,
        result.query_backdates,
        result.resource_loads,
        result.memo_nodes,
        result.memo_node_delta,
        result.dep_graph_edges,
        result.dep_graph_edge_delta,
    )


def _authoritative_work_values(result: ScenarioResult) -> tuple[int | None, ...]:
    return (
        result.query_executions,
        result.query_backdates,
        result.resource_loads,
        result.memo_nodes,
        result.memo_node_delta,
        result.dep_graph_edges,
        result.dep_graph_edge_delta,
    )


def _expectation_values(expectation: WorkExpectation) -> tuple[CountExpectation, ...]:
    return (
        expectation.query_executions,
        expectation.query_backdates,
        expectation.resource_loads,
        expectation.memo_nodes,
        expectation.memo_node_delta,
        expectation.dep_graph_edges,
        expectation.dep_graph_edge_delta,
    )


def _matches_expectation(actual: int, expected: CountExpectation) -> bool:
    if isinstance(expected, int):
        return actual == expected
    return expected[0] <= actual <= expected[1]


def _validate_authoritative_work(result: ScenarioResult) -> None:
    expectation = PYINC_WORK_EXPECTATIONS[(result.target, result.scenario)]
    for field, actual, expected in zip(
        _AUTHORITATIVE_WORK_FIELDS,
        _authoritative_work_values(result),
        _expectation_values(expectation),
        strict=True,
    ):
        if actual is None or not _matches_expectation(actual, expected):
            raise AssertionError(
                f"authoritative work gate failed for {_key(result)!r}: "
                f"{field} expected {expected!r}, got {actual!r}"
            )


def validate_repetition(
    results: Sequence[ScenarioResult], targets: Sequence[str] = ALL_TARGETS
) -> None:
    """Enforce the correctness, coverage, work, and node gates for one run."""
    expected = _expected_rows(targets)
    actual = tuple(_key(result) for result in results)
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise AssertionError(
            f"benchmark row matrix changed: expected={len(expected)}, actual={len(actual)}, "
            f"missing={missing!r}, extra={extra!r}"
        )
    if tuple(targets) == ALL_TARGETS and len(results) != ROWS_PER_REPETITION:
        raise AssertionError(
            f"benchmark must emit {ROWS_PER_REPETITION} rows, emitted {len(results)}"
        )

    expected_stale = INTENTIONAL_STALE.intersection(expected)
    actual_stale = {_key(result) for result in results if not result.matches_fresh}
    if actual_stale != expected_stale:
        raise AssertionError(
            f"unexpected correctness results: expected stale={sorted(expected_stale)!r}, "
            f"actual stale={sorted(actual_stale)!r}"
        )

    for result in results:
        values = _work_values(result)
        if result.engine == "pyinc":
            if any(value is None for value in values):
                raise AssertionError(f"missing pyinc work metrics for {_key(result)!r}")
            _validate_authoritative_work(result)
            assert result.memo_nodes is not None
            ceiling = NODE_CEILINGS[result.target]
            if result.memo_nodes > ceiling:
                raise AssertionError(
                    f"{result.target} memo nodes {result.memo_nodes} exceed ceiling {ceiling}"
                )
        elif any(value is not None for value in values):
            raise AssertionError(f"comparator row has pyinc-only metrics: {_key(result)!r}")


def validate_repetitions(
    repetitions: Sequence[Sequence[ScenarioResult]],
    targets: Sequence[str] = ALL_TARGETS,
) -> None:
    if len(repetitions) != REPETITIONS:
        raise AssertionError(
            f"benchmark requires {REPETITIONS} isolated repetitions, got {len(repetitions)}"
        )
    for results in repetitions:
        validate_repetition(results, targets)

    for row_index, expected_key in enumerate(_expected_rows(targets)):
        first = repetitions[0][row_index]
        signature = (_key(first), first.matches_fresh, _work_values(first))
        for repetition, results in enumerate(repetitions[1:], start=2):
            current = results[row_index]
            current_signature = (_key(current), current.matches_fresh, _work_values(current))
            if current_signature != signature:
                raise AssertionError(
                    f"non-deterministic work counts for {expected_key!r} in repetition "
                    f"{repetition}: expected={signature!r}, actual={current_signature!r}"
                )


def aggregate_repetitions(
    repetitions: Sequence[Sequence[ScenarioResult]],
    targets: Sequence[str] = ALL_TARGETS,
) -> list[BenchmarkSummary]:
    validate_repetitions(repetitions, targets)
    summaries: list[BenchmarkSummary] = []
    for row_index, _expected_key in enumerate(_expected_rows(targets)):
        rows = [results[row_index] for results in repetitions]
        first = rows[0]
        seconds = [row.seconds for row in rows]
        summaries.append(
            BenchmarkSummary(
                target=first.target,
                scenario=first.scenario,
                engine=first.engine,
                median_seconds=statistics.median(seconds),
                min_seconds=min(seconds),
                max_seconds=max(seconds),
                matches_fresh=first.matches_fresh,
                query_executions=first.query_executions,
                query_reuses=first.query_reuses,
                query_backdates=first.query_backdates,
                resource_loads=first.resource_loads,
                memo_nodes=first.memo_nodes,
                memo_node_delta=first.memo_node_delta,
                dep_graph_edges=first.dep_graph_edges,
                dep_graph_edge_delta=first.dep_graph_edge_delta,
            )
        )
    return summaries


def _write_samples_csv(repetitions: Sequence[Sequence[ScenarioResult]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(_SAMPLE_FIELDS)
        for repetition, results in enumerate(repetitions, start=1):
            for result in results:
                writer.writerow(
                    (
                        repetition,
                        result.target,
                        result.scenario,
                        result.engine,
                        f"{result.seconds:.9f}",
                        *_work_values(result),
                        result.matches_fresh,
                    )
                )


def _write_summary_csv(summaries: Sequence[BenchmarkSummary], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(_SUMMARY_FIELDS)
        for result in summaries:
            writer.writerow(
                (
                    result.target,
                    result.scenario,
                    result.engine,
                    f"{result.median_seconds:.9f}",
                    f"{result.min_seconds:.9f}",
                    f"{result.max_seconds:.9f}",
                    result.query_executions,
                    result.query_reuses,
                    result.query_backdates,
                    result.resource_loads,
                    result.memo_nodes,
                    result.memo_node_delta,
                    result.dep_graph_edges,
                    result.dep_graph_edge_delta,
                    result.matches_fresh,
                )
            )


def _display_work(result: BenchmarkSummary) -> str:
    if result.engine != "pyinc":
        return "-"
    return (
        f"{result.query_executions}/{result.query_reuses}/"
        f"{result.query_backdates}/{result.resource_loads}"
    )


def _display_graph(result: BenchmarkSummary) -> str:
    if result.engine != "pyinc":
        return "-"
    return f"{result.memo_nodes}/{result.dep_graph_edges}"


def _write_markdown(summaries: Sequence[BenchmarkSummary], path: Path) -> None:
    stale = [result for result in summaries if not result.matches_fresh]
    lines = [
        "# pyinc benchmark",
        "",
        (
            "Correctness, authoritative deterministic work, and same-path repeatability are "
            "release gates. Timings are informational and report the median and range from "
            "five isolated `PYTHONHASHSEED=0` processes."
        ),
        "",
        (
            f"Validated {len(summaries)} rows per repetition. Every pyinc, full-recompute, and "
            "joblib row matched fresh recomputation; the two naive-cache rows below are "
            "intentional stale controls."
        ),
        "",
        "## Intentional stale controls",
        "",
    ]
    for result in stale:
        lines.append(
            f"- {labels.target_label(result.target)} / "
            f"{labels.scenario_title(result.scenario)} / {labels.engine_label(result.engine)}"
        )
    lines.extend(
        [
            "",
            "## Results",
            "",
            "Work is `executions/reuses/backdates/resource loads`; graph is `nodes/edges`.",
            "`reuses` is a call-level diagnostic that must repeat exactly across the five "
            "same-path runs but has no absolute cross-path envelope: absolute path arguments "
            "can change verification order and repeated already-checked calls without changing "
            "executions or graph work.",
            "",
            "| target | scenario | engine | median ms | min-max ms | work | graph | fresh |",
            "|---|---|---|---:|---:|---:|---:|:---:|",
        ]
    )
    for result in summaries:
        fresh = "yes" if result.matches_fresh else "STALE CONTROL"
        lines.append(
            f"| {labels.target_label(result.target)} | "
            f"{labels.scenario_title(result.scenario)} | "
            f"{labels.engine_label(result.engine)} | "
            f"{result.median_seconds * 1000:.3f} | "
            f"{result.min_seconds * 1000:.3f}-{result.max_seconds * 1000:.3f} | "
            f"{_display_work(result)} | {_display_graph(result)} | {fresh} |"
        )
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_reports(
    repetitions: Sequence[Sequence[ScenarioResult]],
    out_dir: str | Path,
    metadata: Mapping[str, object],
    targets: Sequence[str] = ALL_TARGETS,
) -> tuple[Path, Path, Path, Path]:
    """Validate five repetitions and write the four workflow artifacts."""
    summaries = aggregate_repetitions(repetitions, targets)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    samples_path = out / "samples.csv"
    summary_path = out / "benchmark.csv"
    markdown_path = out / "benchmark.md"
    metadata_path = out / "metadata.json"
    _write_samples_csv(repetitions, samples_path)
    _write_summary_csv(summaries, summary_path)
    _write_markdown(summaries, markdown_path)
    metadata_path.write_text(
        json.dumps(dict(metadata), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return samples_path, summary_path, markdown_path, metadata_path
