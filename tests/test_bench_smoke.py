from __future__ import annotations

import sys
from pathlib import Path

_BENCH = Path(__file__).resolve().parent.parent
if str(_BENCH) not in sys.path:
    sys.path.insert(0, str(_BENCH))

from bench import harness  # noqa: E402

_EXPECTED_SCENARIOS = (
    "cold",
    "unchanged",
    "unreferenced_file_edit",
    "comment_only_referenced_edit",
    "localized_semantic_edit",
    "high_fanout_shared_edit",
    "removed_emitted_artifact",
    "tampered_generated_output",
    "checkpoint_restore",
)


def test_harness_runs_synthetic_and_asserts_correctness(tmp_path: Path) -> None:
    results = harness.run_scenarios(["synthetic"], out_dir=tmp_path, comparators=["full", "naive"])
    assert results, "expected scenario results"
    # E3: pyinc's incremental output equals a fresh, cache-free run in every scenario.
    assert all(r.correct for r in results if r.engine == "pyinc")
    csv_path, md_path = harness.write_reports(results, tmp_path)
    assert csv_path.exists() and md_path.exists()


def test_all_targets_cover_edit_sequence(tmp_path: Path) -> None:
    results = harness.run_scenarios(
        ["synthetic", "calc", "codegen", "action"],
        out_dir=tmp_path,
        comparators=["full", "naive"],
    )
    scenarios = {r.scenario for r in results}
    for expected in _EXPECTED_SCENARIOS:
        assert expected in scenarios, f"missing scenario: {expected}"
    assert all(r.correct for r in results if r.engine == "pyinc")


def test_naive_cache_is_observably_wrong_somewhere(tmp_path: Path) -> None:
    # The benchmark's value proposition: a naive cache is fast but can be stale.
    results = harness.run_scenarios(
        ["synthetic", "calc"], out_dir=tmp_path, comparators=["full", "naive"]
    )
    naive_correct = [r.correct for r in results if r.engine == "naive"]
    assert naive_correct and not all(naive_correct)
