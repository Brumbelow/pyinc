from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from pyfoundinc import Database
from pyfoundinc.integrations.python_source import (
    directory_analysis,
    file_analysis,
    workspace_analysis,
)

ROOT = Path(__file__).resolve().parents[1]
BENCH_PATH = ROOT / "benchmarks" / "run_microbench.py"
PLAIN_PATH = ROOT / "benchmarks" / "plain_python_source.py"
SPEC = importlib.util.spec_from_file_location("benchmarks.run_microbench", BENCH_PATH)
assert SPEC is not None
assert SPEC.loader is not None
bench = importlib.util.module_from_spec(SPEC)
sys.modules.setdefault("benchmarks.run_microbench", bench)
SPEC.loader.exec_module(bench)

PLAIN_SPEC = importlib.util.spec_from_file_location("benchmarks.plain_python_source", PLAIN_PATH)
assert PLAIN_SPEC is not None
assert PLAIN_SPEC.loader is not None
plain_source = importlib.util.module_from_spec(PLAIN_SPEC)
sys.modules.setdefault("benchmarks.plain_python_source", plain_source)
PLAIN_SPEC.loader.exec_module(plain_source)


def test_plain_file_analysis_matches_incremental_for_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("import os\nclass Example:\n    pass\n", encoding="utf-8")

    assert plain_source.file_analysis(path) == file_analysis(Database(), path)


def test_plain_file_analysis_matches_incremental_for_syntax_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    path.write_text("def broken(\n", encoding="utf-8")

    assert plain_source.file_analysis(path) == file_analysis(Database(), path)


def test_plain_directory_analysis_matches_incremental_directory(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "b.py").write_text("import sys\n", encoding="utf-8")
    (root / "a.py").write_text("import os\n", encoding="utf-8")
    (root / "notes.txt").write_text("ignored\n", encoding="utf-8")

    assert plain_source.directory_analysis(root) == directory_analysis(Database(), root)


def test_plain_workspace_analysis_matches_incremental_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    pkg = root / "pkg"
    app = root / "app"
    pkg.mkdir(parents=True)
    app.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "provider.py").write_text("def exported() -> int:\n    return 1\n", encoding="utf-8")
    (app / "consumer.py").write_text("from pkg.provider import exported\n", encoding="utf-8")

    assert plain_source.workspace_analysis(root) == workspace_analysis(Database(), root)


def test_table_output_contains_scenario_rows_and_vs_fresh(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = bench.main(
        [
            "--bench",
            "diamond",
            "--samples",
            "1",
            "--warmup",
            "0",
            "--rounds",
            "1",
            "--payload-size",
            "8",
            "--format",
            "table",
        ],
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "scenario" in captured.out
    assert "vs_fresh" in captured.out
    assert "diamond_reuse" in captured.out
    assert "fresh_recompute" in captured.out


def test_markdown_output_contains_summary_and_scenario_sections(capsys: pytest.CaptureFixture[str]) -> None:
    bench.main(
        [
            "--bench",
            "source_analysis",
            "--samples",
            "1",
            "--warmup",
            "0",
            "--rounds",
            "1",
            "--payload-size",
            "8",
            "--format",
            "markdown",
        ],
    )

    captured = capsys.readouterr()
    assert "## Summary" in captured.out
    assert "## Workload" in captured.out
    assert "### Python Source Analysis" in captured.out
    assert "Interpretation:" in captured.out


def test_compare_table_output_contains_incremental_and_plain_columns(capsys: pytest.CaptureFixture[str]) -> None:
    bench.main(
        [
            "--suite",
            "workload",
            "--bench",
            "source_analysis",
            "--samples",
            "1",
            "--warmup",
            "0",
            "--rounds",
            "1",
            "--payload-size",
            "8",
            "--implementation",
            "compare",
            "--format",
            "table",
        ],
    )

    captured = capsys.readouterr()
    assert "inc_mean_ms" in captured.out
    assert "plain_mean_ms" in captured.out
    assert "speedup_pct" in captured.out
    assert "comment_only_edit" in captured.out


def test_workspace_compare_table_output_contains_new_workload_rows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    bench.main(
        [
            "--suite",
            "workload",
            "--bench",
            "workspace_import_graph",
            "--samples",
            "1",
            "--warmup",
            "0",
            "--rounds",
            "1",
            "--payload-size",
            "8",
            "--implementation",
            "compare",
            "--format",
            "table",
        ],
    )

    captured = capsys.readouterr()
    assert "workspace_import_graph" in captured.out
    assert "provider_internal_edit" in captured.out
    assert "plain_mean_ms" in captured.out


def test_json_output_includes_environment_and_comparisons(capsys: pytest.CaptureFixture[str]) -> None:
    bench.main(
        [
            "--bench",
            "diamond",
            "--samples",
            "1",
            "--warmup",
            "0",
            "--rounds",
            "1",
            "--payload-size",
            "8",
            "--format",
            "json",
        ],
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["environment"]["python_version"]
    assert payload["environment"]["python_implementation"]
    assert payload["results"][0]["comparisons"]
    assert payload["results"][0]["phases"][0]["metrics"]["mean_s"] >= 0.0


def test_compare_json_output_includes_implementations_and_ratios(capsys: pytest.CaptureFixture[str]) -> None:
    bench.main(
        [
            "--suite",
            "workload",
            "--bench",
            "source_analysis",
            "--samples",
            "1",
            "--warmup",
            "0",
            "--rounds",
            "1",
            "--payload-size",
            "8",
            "--implementation",
            "compare",
            "--format",
            "json",
        ],
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["implementations"] == ["incremental", "plain"]
    assert payload["results"][0]["operations"][0]["measurements"]
    assert payload["results"][0]["operations"][0]["comparison"]["speedup_ratio"] is not None


def test_compare_markdown_output_contains_summary_and_workload_sections(
    capsys: pytest.CaptureFixture[str],
) -> None:
    bench.main(
        [
            "--suite",
            "workload",
            "--bench",
            "source_analysis",
            "--samples",
            "1",
            "--warmup",
            "0",
            "--rounds",
            "1",
            "--payload-size",
            "8",
            "--implementation",
            "compare",
            "--format",
            "markdown",
        ],
    )

    captured = capsys.readouterr()
    assert "## Summary" in captured.out
    assert "speedup_pct" in captured.out
    assert "## Workload" in captured.out
    assert "### Python Source Analysis" in captured.out


def test_selected_benchmark_invariants_pass() -> None:
    config = bench.BenchConfig(samples=1, warmup=0, rounds=1, payload_size=8)

    for scenario_key in (
        "backdating_chain",
        "rewiring_torture",
        "resource_granularity",
        "source_analysis",
        "workspace_import_graph",
    ):
        result = bench.SCENARIO_INDEX[scenario_key].run(config, bench._resolved_mode(scenario_key, None))
        assert result.invariants
        assert all(invariant.passed for invariant in result.invariants)

    compare_result = bench.benchmark_source_analysis_compare(config, "strict")
    assert compare_result.invariants
    assert all(invariant.passed for invariant in compare_result.invariants)
    workspace_compare = bench.benchmark_workspace_import_graph_compare(config, "strict")
    assert workspace_compare.invariants
    assert all(invariant.passed for invariant in workspace_compare.invariants)


def test_compare_mode_rejects_micro_suite() -> None:
    with pytest.raises(SystemExit):
        bench.main(
            [
                "--suite",
                "micro",
                "--implementation",
                "compare",
            ],
        )


def test_benchmark_can_write_json_and_markdown_artifacts(tmp_path: Path) -> None:
    json_path = tmp_path / "bench.json"
    markdown_path = tmp_path / "bench.md"

    exit_code = bench.main(
        [
            "--bench",
            "diamond",
            "--samples",
            "1",
            "--warmup",
            "0",
            "--rounds",
            "1",
            "--payload-size",
            "8",
            "--format",
            "table",
            "--output-json",
            str(json_path),
            "--output-markdown",
            str(markdown_path),
        ],
    )

    assert exit_code == 0
    assert json.loads(json_path.read_text(encoding="utf-8"))["results"][0]["key"] == "diamond_reuse"
    assert markdown_path.read_text(encoding="utf-8").startswith("# pyfoundinc benchmark report")


def test_compare_mode_can_write_json_and_markdown_artifacts(tmp_path: Path) -> None:
    json_path = tmp_path / "compare.json"
    markdown_path = tmp_path / "compare.md"

    exit_code = bench.main(
        [
            "--suite",
            "workload",
            "--bench",
            "source_analysis",
            "--samples",
            "1",
            "--warmup",
            "0",
            "--rounds",
            "1",
            "--payload-size",
            "8",
            "--implementation",
            "compare",
            "--format",
            "table",
            "--output-json",
            str(json_path),
            "--output-markdown",
            str(markdown_path),
        ],
    )

    assert exit_code == 0
    assert json.loads(json_path.read_text(encoding="utf-8"))["implementations"] == ["incremental", "plain"]
    assert markdown_path.read_text(encoding="utf-8").startswith("# pyfoundinc workload comparison report")
