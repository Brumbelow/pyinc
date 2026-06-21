from __future__ import annotations

from pathlib import Path

import pytest

from bench.adapters import DetectionWorkload, GraphqlWorkload, KernelWorkload
from bench.report import read_csv, render_markdown, write_csv
from bench.scenarios import BenchRecord, CorrectnessError, run_scenario


def _record(impl: str, correctness: str = "pass") -> BenchRecord:
    return BenchRecord(
        workload="demo",
        scenario="semantic_edit",
        implementation=impl,
        correctness=correctness,
        repetitions=1,
        median_ns=1234,
        p95_ns=1234,
        query_executions=3,
        query_reuses=5,
        query_backdates=1,
        output_writes=2,
        output_deletes=0,
        graph_nodes=7,
        graph_edges=9,
        checkpoint_bytes=100,
        output_digest="deadbeef",
    )


def test_correctness_error_is_assertion_error() -> None:
    assert issubclass(CorrectnessError, AssertionError)


@pytest.mark.parametrize(
    ("workload", "scenario"),
    [
        (KernelWorkload, "semantic_edit"),
        (KernelWorkload, "checkpoint_restore"),
        (GraphqlWorkload, "semantic_edit"),
        (GraphqlWorkload, "output_tamper"),
        (DetectionWorkload, "high_fanout_edit"),
    ],
)
def test_scenario_passes_correctness(
    workload: type, scenario: str, tmp_path: Path
) -> None:
    # run_scenario asserts incremental == fresh internally; reaching here means it
    # passed. No timing assertions (timings are machine-dependent).
    records = run_scenario(workload, scenario, tmp_path, warmup=0, repetitions=1)
    by_impl = {r.implementation: r for r in records}
    assert by_impl["pyinc_incremental"].correctness == "pass"
    assert by_impl["fresh_full"].correctness == "pass"
    assert by_impl["naive_cache"].correctness == "pass"
    # pyinc rows carry real metrics; others are N/A (-1).
    assert by_impl["pyinc_incremental"].graph_nodes >= 1
    assert by_impl["fresh_full"].graph_nodes == -1


def test_csv_and_markdown_round_trip(tmp_path: Path) -> None:
    records = [_record("pyinc_incremental"), _record("joblib_memory", "n/a")]
    csv_path = tmp_path / "benchmark.csv"
    write_csv(records, csv_path)

    rows = read_csv(csv_path)
    assert len(rows) == 2
    assert rows[0]["implementation"] == "pyinc_incremental"

    markdown = render_markdown(csv_path, {"git_commit": "abc1234", "python_version": "3.13"})
    assert "# pyinc Benchmark Report" in markdown
    assert "not** universal speed claims" in markdown
    assert "pyinc_incremental" in markdown
    assert "## Capability differences" in markdown
    # N/A timing renders as N/A, not a negative number.
    assert "-1" not in markdown.split("## Results")[1]


def test_kernel_warm_scenario_reuses_queries(tmp_path: Path) -> None:
    records = run_scenario(KernelWorkload, "warm", tmp_path, warmup=0, repetitions=1)
    pyinc_row = next(r for r in records if r.implementation == "pyinc_incremental")
    assert pyinc_row.output_writes == 0
    assert pyinc_row.query_executions == 0  # warm rerun executes nothing
