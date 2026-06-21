from __future__ import annotations

import runpy
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def _run_example(name: str) -> None:
    runpy.run_path(str(EXAMPLES_DIR / name), run_name="__main__")


def test_inspect_fresh_demo_runs(capsys: pytest.CaptureFixture[str]) -> None:
    _run_example("inspect_fresh_demo.py")
    output = capsys.readouterr().out
    assert "initial result: 6" in output
    assert "inspect:" in output
    assert "inspect_fresh:" in output


def test_capture_diagnostics_demo_runs(capsys: pytest.CaptureFixture[str]) -> None:
    _run_example("capture_diagnostics.py")
    output = capsys.readouterr().out
    assert "accepted=False" in output
    assert "runtime failure:" in output
    assert "explain_query_captures" in output


def test_untracked_escape_hatch_demo_runs(capsys: pytest.CaptureFixture[str]) -> None:
    _run_example("untracked_escape_hatch.py")
    output = capsys.readouterr().out
    assert "first=" in output
    assert "second=" in output
    assert "untracked_reasons=" in output


def test_observers_demo_runs(capsys: pytest.CaptureFixture[str]) -> None:
    _run_example("observers_demo.py")
    output = capsys.readouterr().out
    assert "event_count=3" in output
    assert "decision=executed" in output
    assert "final_decision=executed" in output


def test_artifact_store_demo_runs(capsys: pytest.CaptureFixture[str]) -> None:
    _run_example("artifact_store_demo.py")
    output = capsys.readouterr().out
    assert "result=('ALPHA', 'BETA', 'GAMMA')" in output
    assert "in_memory_object_count=" in output
    assert "on_disk_object_count=" in output
    assert "round_trip=('hello', 'world')" in output
    assert "round_trip_equal=True" in output


def test_frozen_graph_demo_runs(capsys: pytest.CaptureFixture[str]) -> None:
    _run_example("frozen_graph_demo.py")
    output = capsys.readouterr().out
    assert "tree_is_FrozenGraph=False" in output
    assert "shared_is_FrozenGraph=True" in output
    assert "shared_left_is_right=True" in output
    assert "shared_after_mutation_right=[10, 20, 30]" in output
    assert "cycle_is_FrozenGraph=True" in output
    assert "cycle_self_referential=True" in output


def test_notebook_demo_runs(capsys: pytest.CaptureFixture[str]) -> None:
    _run_example("notebook_demo.py")
    output = capsys.readouterr().out
    assert "kernel_name=python3" in output
    assert "language=python" in output
    assert "cell_count=2" in output
    assert "heading='Daily ETL'" in output
    assert "imports=('pandas',)" in output
    assert "definitions=('load',)" in output
    assert "output_only_edit_backdated=True" in output
    assert "analysis_unchanged=True" in output


def test_action_reconcile_demo_runs(capsys: pytest.CaptureFixture[str]) -> None:
    _run_example("action_reconcile_demo.py")
    output = capsys.readouterr().out
    assert "run1_writes=('alpha.py', 'beta.py')" in output
    assert "run2_writes=()" in output
    assert "run2_mtime_stable=True" in output
    assert "run3_writes=('alpha.py',)" in output
    assert "run4_writes=('beta.py',)" in output
    assert "run5_deletions=('beta.py',)" in output
    assert "run5_foreign_preserved=True" in output


def test_graphql_codegen_demo_runs(capsys: pytest.CaptureFixture[str]) -> None:
    _run_example("graphql_codegen_demo.py")
    output = capsys.readouterr().out
    assert "cold_write_count=11" in output
    assert "rerun_writes=()" in output
    assert "whitespace_writes=()" in output
    assert "description_edit_writes=('docs/types/User.md',)" in output


def test_detection_compile_demo_runs(capsys: pytest.CaptureFixture[str]) -> None:
    _run_example("detection_compile_demo.py")
    output = capsys.readouterr().out
    assert "cold_write_count=8" in output
    assert "rerun_writes=()" in output
    assert "unused_mapping_writes=()" in output
    assert "used_mapping_writes=['queries/splunk/ps_enc.spl']" in output
    assert "provenance_rule=ps_enc" in output
    assert "provenance_macros=('encoded',)" in output


def test_checkpoint_demo_runs(capsys: pytest.CaptureFixture[str]) -> None:
    _run_example("checkpoint_demo.py")
    output = capsys.readouterr().out
    assert "run1_result=15" in output
    assert "run1_executions=3" in output
    assert "run2_result=15" in output
    assert "run2_decision=reused" in output
    assert "run2_executions=0" in output
    assert "run3_result=50" in output
    assert "run3_decision=executed" in output
    assert "run3_executions=1" in output
