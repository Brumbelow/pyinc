from __future__ import annotations

import csv
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bench import harness  # noqa: E402
from bench.baselines import FIXED_COMPARATORS, required_comparators  # noqa: E402
from bench.measure import ScenarioResult  # noqa: E402


@pytest.fixture(scope="module")
def synthetic_results(tmp_path_factory: pytest.TempPathFactory) -> list[ScenarioResult]:
    out = tmp_path_factory.mktemp("benchmark-synthetic")
    results = harness.run_scenarios(["synthetic"], out_dir=out)
    harness.validate_repetition(results, targets=("synthetic",))
    return results


def test_harness_runs_one_synthetic_smoke(
    synthetic_results: list[ScenarioResult],
) -> None:
    assert required_comparators() == FIXED_COMPARATORS
    with pytest.raises(ValueError, match="comparator set is fixed"):
        harness.run_scenarios(
            ["synthetic"], out_dir=Path("unused"), comparators=("full", "naive")
        )
    assert len(synthetic_results) == 17
    stale = {
        (result.target, result.scenario, result.engine)
        for result in synthetic_results
        if not result.matches_fresh
    }
    assert stale == {("synthetic", "high_fanout_shared_edit", "naive")}

    cold = next(
        result
        for result in synthetic_results
        if result.engine == "pyinc" and result.scenario == "cold"
    )
    localized = next(
        result
        for result in synthetic_results
        if result.engine == "pyinc" and result.scenario == "localized_semantic_edit"
    )
    assert cold.memo_nodes == 14
    assert cold.dep_graph_edges == 18  # six branch->input pairs plus aggregate->branches
    assert localized.query_executions == 2


def _valid_matrix() -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    for target, scenario, engine in harness.EXPECTED_ROW_KEYS:
        matches = (target, scenario, engine) not in harness.INTENTIONAL_STALE
        if engine == "pyinc":
            results.append(
                ScenarioResult.pyinc(
                    target,
                    scenario,
                    0.001,
                    matches,
                    harness.expected_work_metrics(target, scenario),
                )
            )
        else:
            results.append(
                ScenarioResult.comparator(target, scenario, engine, 0.001, matches)
            )
    return results


def test_release_matrix_validation_is_exact() -> None:
    results = _valid_matrix()
    assert len(results) == harness.ROWS_PER_REPETITION == 67
    harness.validate_repetition(results)

    wrong = list(results)
    joblib_index = next(index for index, row in enumerate(wrong) if row.engine == "joblib")
    row = wrong[joblib_index]
    wrong[joblib_index] = ScenarioResult.comparator(
        row.target, row.scenario, row.engine, row.seconds, False
    )
    with pytest.raises(AssertionError, match="unexpected correctness"):
        harness.validate_repetition(wrong)


def test_report_artifacts_contain_raw_and_summarized_samples(
    tmp_path: Path,
    synthetic_results: list[ScenarioResult],
) -> None:
    repetitions = [list(synthetic_results) for _ in range(harness.REPETITIONS)]
    metadata: dict[str, object] = {
        "commit_sha": "0" * 40,
        "targets": ["synthetic"],
        "repetitions": harness.REPETITIONS,
    }
    samples_path, summary_path, markdown_path, metadata_path = harness.write_reports(
        repetitions,
        tmp_path,
        metadata,
        targets=("synthetic",),
    )

    with samples_path.open(encoding="utf-8") as handle:
        samples = list(csv.DictReader(handle))
    with summary_path.open(encoding="utf-8") as handle:
        summaries = list(csv.DictReader(handle))

    assert len(samples) == harness.REPETITIONS * 17
    assert len(summaries) == 17
    assert set(samples[0]) == {
        "repetition",
        "target",
        "scenario",
        "engine",
        "wall_seconds",
        "query_executions",
        "query_reuses",
        "query_backdates",
        "resource_loads",
        "memo_nodes",
        "memo_node_delta",
        "dep_graph_edges",
        "dep_graph_edge_delta",
        "matches_fresh",
    }
    assert {
        "median_wall_seconds",
        "min_wall_seconds",
        "max_wall_seconds",
    }.issubset(summaries[0])
    comparator = next(row for row in samples if row["engine"] == "full")
    assert comparator["query_executions"] == comparator["dep_graph_edges"] == ""

    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Correctness and deterministic work are release gates" in markdown
    assert "STALE CONTROL" in markdown
    assert json.loads(metadata_path.read_text(encoding="utf-8")) == metadata


def test_repetition_validation_rejects_work_count_drift() -> None:
    repetitions = [_valid_matrix() for _ in range(harness.REPETITIONS)]
    index = next(
        index
        for index, row in enumerate(repetitions[-1])
        if row.target == "codegen"
        and row.scenario == "localized_semantic_edit"
        and row.engine == "pyinc"
    )
    original = repetitions[-1][index]
    assert original.query_reuses is not None
    repetitions[-1][index] = replace(
        original, query_reuses=original.query_reuses + 1
    )
    with pytest.raises(AssertionError, match="non-deterministic work counts"):
        harness.validate_repetitions(repetitions)


@pytest.mark.parametrize(
    ("target", "scenario", "inflated_executions"),
    (
        ("synthetic", "localized_semantic_edit", 7),
        ("calc", "localized_semantic_edit", 15),
        ("codegen", "localized_semantic_edit", 28),
        ("calc", "high_fanout_shared_edit", 15),
        ("codegen", "removed_emitted_artifact", 28),
    ),
)
def test_authoritative_work_gate_rejects_full_graph_recomputation(
    target: str,
    scenario: str,
    inflated_executions: int,
) -> None:
    results = _valid_matrix()
    index = next(
        index
        for index, row in enumerate(results)
        if row.target == target and row.scenario == scenario and row.engine == "pyinc"
    )
    results[index] = replace(results[index], query_executions=inflated_executions)
    with pytest.raises(AssertionError, match="authoritative work gate failed"):
        harness.validate_repetition(results)


def test_authoritative_work_gate_checks_removal_backdates_and_resource_loads() -> None:
    results = _valid_matrix()
    index = next(
        index
        for index, row in enumerate(results)
        if row.target == "codegen"
        and row.scenario == "removed_emitted_artifact"
        and row.engine == "pyinc"
    )
    results[index] = replace(results[index], query_backdates=0, resource_loads=2)
    with pytest.raises(AssertionError, match="query_backdates expected 6"):
        harness.validate_repetition(results)
