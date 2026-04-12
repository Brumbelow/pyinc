from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH_PATH = ROOT / "benchmarks" / "run_microbench.py"
SPEC = importlib.util.spec_from_file_location("benchmarks.run_microbench", BENCH_PATH)
assert SPEC is not None
assert SPEC.loader is not None
bench = importlib.util.module_from_spec(SPEC)
sys.modules.setdefault("benchmarks.run_microbench", bench)
SPEC.loader.exec_module(bench)


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


def test_selected_benchmark_invariants_pass() -> None:
    config = bench.BenchConfig(samples=1, warmup=0, rounds=1, payload_size=8)

    for scenario_key in ("backdating_chain", "rewiring_torture", "resource_granularity", "source_analysis"):
        result = bench.SCENARIO_INDEX[scenario_key].run(config, bench._resolved_mode(scenario_key, None))
        assert result.invariants
        assert all(invariant.passed for invariant in result.invariants)


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
