from __future__ import annotations

import re
import runpy
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def _run_example(name: str) -> None:
    runpy.run_path(str(EXAMPLES_DIR / name), run_name="__main__")


def _make_dist_info(site_dir: Path, name: str, version: str, *, top_level: str) -> Path:
    """The metadata an installer leaves in site-packages for one distribution.

    Written here rather than imported from the dependency-check tests: the two
    files would then share a collection and a future, for nine lines.
    """
    dist_info = site_dir / f"{name}-{version}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nSummary: A test package\n",
        encoding="utf-8",
    )
    (dist_info / "top_level.txt").write_text(top_level + "\n", encoding="utf-8")
    return dist_info


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


def test_checkpoint_demo_runs(capsys: pytest.CaptureFixture[str]) -> None:
    _run_example("checkpoint_demo.py")
    output = capsys.readouterr().out
    assert "run1_result=15" in output
    assert "run1_executions=3" in output
    assert "run2_result=15" in output
    assert "run2_last_recompute=reused" in output
    assert "run2_executions=0" in output
    assert "run3_result=50" in output
    assert "run3_last_recompute=executed" in output
    assert "run3_executions=1" in output

    # The two labels above name the field the demo reads, and the values they
    # print are equal to `last_decision` at both runs -- so the assertions on
    # the output alone hold just as well if the reads are switched. These pin
    # the reads themselves.
    source = (EXAMPLES_DIR / "checkpoint_demo.py").read_text(encoding="utf-8")
    assert "node2.last_recompute" in source
    assert "node3.last_recompute" in source
    assert "last_decision" not in source


def test_action_reconcile_demo_runs(capsys: pytest.CaptureFixture[str]) -> None:
    _run_example("action_reconcile_demo.py")
    output = capsys.readouterr().out
    assert "first_created=('alpha.txt', 'beta.txt')" in output
    assert "rerun_updated=()" in output
    assert "tamper_repaired=('beta.txt',)" in output
    assert "orphan_deleted=('beta.txt',)" in output
    assert "plan_only_no_files=True" in output


def test_calc_demo_runs(capsys: pytest.CaptureFixture[str]) -> None:
    _run_example("calc_demo.py")
    output = capsys.readouterr().out
    assert "alpha=42" in output
    assert "unrelated_edit_changes=()" in output
    assert "unrelated_edit_executions=0" in output
    reuses = re.search(r"unrelated_edit_reuses=(\d+)", output)
    assert reuses is not None
    # The count itself is a kernel counter and has moved before now. What the
    # example claims is that the reconcile did real work without running a
    # query body, and any reuse at all witnesses that.
    assert int(reuses.group(1)) > 0
    assert "comment_edit_backdated=True" in output
    assert "removed_emit_deleted=('base.out',)" in output


def test_codegen_demo_runs(capsys: pytest.CaptureFixture[str]) -> None:
    _run_example("codegen_demo.py")
    output = capsys.readouterr().out
    assert (
        "generated=('__init__.py', 'color.py', 'docs/color.md', 'docs/widget.md', 'widget.py')"
        in output
    )
    assert "whitespace_edit_changed=()" in output
    assert "description_edit_updated=('docs/widget.md',)" in output
    assert "removed_def_deleted=('color.py', 'docs/color.md')" in output


def test_undeclared_imports_reports_the_promised_finding(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The example exists to name an import the environment has and the project does not declare."""
    # Returning at all is half the witness: the example raises SystemExit when
    # it cannot produce the finding, so reaching the assertions below means it
    # produced one.
    _run_example("undeclared_imports.py")
    output = capsys.readouterr().out
    assert "- pyinc" in output
    assert "distribution: pyinc" in output


def test_undeclared_imports_fails_when_the_environment_cannot_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An example that cannot show its finding says so, rather than reporting that it found nothing and exiting 0."""
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "unrelated", "1.0", top_level="unrelated")
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    with pytest.raises(SystemExit, match="found no undeclared import"):
        _run_example("undeclared_imports.py")
