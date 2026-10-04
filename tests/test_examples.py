from __future__ import annotations

import re
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def _run_example(name: str) -> None:
    runpy.run_path(str(EXAMPLES_DIR / name), run_name="__main__")


def _load_example(name: str) -> dict[str, Any]:
    """The example's module namespace, without running the body under its __main__ guard.

    The run name is the file's own stem, so the `__main__` guard at the foot of
    the file skips the demo. The module body still runs and defines `main` and
    the queries it uses.
    """
    return runpy.run_path(str(EXAMPLES_DIR / name), run_name=Path(name).stem)


def _make_dist_info(site_dir: Path, name: str, version: str, *, top_level: str) -> Path:
    """The metadata an installer leaves in site-packages for one distribution.

    Written here instead of imported from the dependency-check tests. An
    import would tie the two files' collection and maintenance together, for
    nine lines.
    """
    dist_info = site_dir / f"{name}-{version}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nSummary: A test package\n",
        encoding="utf-8",
    )
    (dist_info / "top_level.txt").write_text(top_level + "\n", encoding="utf-8")
    return dist_info


def _examples_tree() -> dict[str, tuple[int, int]]:
    """Every file under examples/, by size and modification time.

    Bytecode caches are left out. Another example puts this directory on the
    import path and imports the package beside it, which writes a cache here on
    every run. Counting those caches would blame this example for that write.
    """
    return {
        str(path.relative_to(EXAMPLES_DIR)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(EXAMPLES_DIR.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def _emitted(root: Path) -> dict[str, bytes]:
    """The files an action reconciled into a root, without its ledger.

    The ledger's name is a digest of the tool, so a warm root and a fresh root
    each hold one. Its contents carry a digest of the output root's path and
    the directory's own inode, so the ledger bytes of two roots always differ.
    Comparing them would report a false difference.
    """
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.startswith(".pyinc-action.")
    }


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
    """Reporting an untracked read stops the reuse, and the reason travels with the node."""
    _run_example("untracked_escape_hatch.py")
    output = capsys.readouterr().out
    assert "first=" in output
    assert "second=" in output
    # The two clock values stay uncompared. On some platforms the example's
    # clock is coarser than the gap between the two calls, so the values may
    # be equal. The example proves that the second call ran instead
    # of reusing the first, and that the node carries the reason it reported.
    assert "last_decision=executed" in output
    assert "untracked_reasons=('time.monotonic_ns()',)" in output


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

    # The two labels above name the field the demo reads. The values they print
    # equal `last_decision` at both runs, so the output assertions would still
    # pass if the reads were switched. These lines pin the reads themselves.
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
    # The count is a kernel counter and has changed before. The example claims
    # that the reconcile did real work without running a query body, and any
    # reuse at all shows that.
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


@pytest.mark.real_site_packages
def test_undeclared_imports_reports_the_promised_finding(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The example names an import the environment has and the project leaves undeclared."""
    # Returning at all is half the witness. The example raises SystemExit when
    # it cannot produce the finding, so reaching the assertions below means it
    # produced one.
    _run_example("undeclared_imports.py")
    output = capsys.readouterr().out
    assert "- pyinc" in output
    assert "distribution: pyinc" in output


def test_undeclared_imports_fails_when_the_environment_cannot_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An example that cannot show its finding says so and exits nonzero."""
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "unrelated", "1.0", top_level="unrelated")
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    with pytest.raises(SystemExit, match="found no undeclared import"):
        _run_example("undeclared_imports.py")


def test_mini_analyzer_prints_named_fields_and_leaves_the_tree_alone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The example analyzes a workspace it builds and leaves its own directory unchanged."""
    before = _examples_tree()

    _run_example("mini_analyzer.py")
    output = capsys.readouterr().out

    assert _examples_tree() == before
    assert len(output) < 1000, f"unbounded example output: {len(output)} bytes"
    assert "file:          app.py" in output
    assert "workspace:     2 modules" in output
    assert "resolutions=('stdlib', 'workspace')" in output


def test_correctness_demo_runs(capsys: pytest.CaptureFixture[str]) -> None:
    """Phase 2 shows the key behaviour: equal counts backdate, so the query above them is reused."""
    _run_example("correctness_demo.py")
    output = capsys.readouterr().out

    # The comment-only edit changed the bytes, so read_source ran again. Both
    # counts recomputed the same numbers and were backdated, so the query above
    # them was reused. All four nodes are read in both fields (the decision and
    # the last recompute) because the two can differ. These substrings leave
    # out the temporary path and the per-arguments identity tag, which change
    # on every run.
    assert "summary decision: reused" in output
    assert "summary(): reused [last_recompute=executed]" in output
    assert "count_functions(): backdated [last_recompute=backdated]" in output
    assert "count_imports(): backdated [last_recompute=backdated]" in output
    assert "read_source(): reused [last_recompute=executed]" in output
    # Phase 3: a structural edit makes the counts run again.
    assert "Result: 2 functions, 3 imports" in output
    # Phases 4 and 5 end in refusals, and each prints the error it caught.
    assert "Caught UntrackedReadError:" in output
    assert "Caught TypeError:" in output


def test_symbol_lookup_follows_the_whole_re_export_chain(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both hops of the re-export chain are followed: facade to middle to origin."""
    _run_example("symbol_lookup.py")
    output = capsys.readouterr().out

    # Only the file name is read, because the example writes its modules into a
    # temporary directory with a new path on every run. The capture runs to the
    # end of the line because a temporary path may contain a space, and
    # stopping at the first whitespace would name a different file.
    match = re.search(r"Defining path:\s+(.+)", output)
    assert match is not None
    assert Path(match.group(1).strip()).name == "origin.py"
    # Following no hop at all would name facade.py and following one would name
    # middle.py, so the file name alone says both hops were taken. Zero-based
    # line 3 is `def process(items):` and character 4 is where `process` starts,
    # so the position landed on the identifier and not on the start of the line.
    assert "Defining position: SourcePosition(line=3, character=4)" in output


def test_applicable_requirements_evaluates_the_markers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The markers are evaluated against the running interpreter."""
    _run_example("applicable_requirements.py")
    output = capsys.readouterr().out

    # Derived from the running interpreter, so these two hold on every version
    # the project supports.
    assert f"python_version = {sys.version_info.major}.{sys.version_info.minor}" in output
    assert f"Active interpreter: {sys.version.split()[0]}" in output
    # The example exists to show the marker arithmetic. Both of these markers
    # are false on every interpreter the project supports (requires-python is
    # >=3.11), and the two requirements whose markers hold are reported
    # applicable.
    assert re.search(r"backports-zoneinfo\s+False\s+not_applicable", output) is not None
    assert re.search(r"tomli\s+False\s+not_applicable", output) is not None
    assert re.search(r"requests\s+True\s+", output) is not None
    assert re.search(r"packaging\s+True\s+", output) is not None
    # The Status and Detail of an applicable requirement stay unchecked. They
    # report what the environment has installed: a version that varies between
    # environments, and an absence that depends on which extras were installed.


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_calc_demo_warm_matches_fresh(
    mode: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A database that has been carrying results answers what a from-scratch one answers."""
    namespace = _load_example("calc_demo.py")
    database, calc_emit = namespace["Database"], namespace["calc_emit"]

    base = tmp_path / "workspace"
    base.mkdir()
    constants = base / "constants.calc"
    constants.write_text("let base = 40\n", encoding="utf-8")
    root = base / "m.calc"
    root.write_text(
        'include "constants.calc"\nlet alpha = base + 2\nemit alpha\nemit base\n',
        encoding="utf-8",
    )

    # The warm database reconciles once, then the included file changes a value
    # every emitted output depends on. A comment-only edit would backdate, so
    # both roots would hold the same bytes even if the warm database missed it.
    warm_out = tmp_path / "warm"
    warm = database(mode=mode)
    calc_emit.reconcile(warm, str(root), root=warm_out)
    constants.write_text("let base = 41\n", encoding="utf-8")
    calc_emit.reconcile(warm, str(root), root=warm_out)

    fresh_out = tmp_path / "fresh"
    fresh = database(mode=mode)
    calc_emit.reconcile(fresh, str(root), root=fresh_out)

    # Two empty roots would compare equal, so check that the fresh root has files.
    assert _emitted(fresh_out)
    assert _emitted(warm_out) == _emitted(fresh_out)

    namespace["main"](mode=mode)
    printed = capsys.readouterr().out
    assert "unrelated_edit_executions=0" in printed
    assert "comment_edit_backdated=True" in printed


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_action_reconcile_demo_warm_matches_fresh(
    mode: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A root reconciled twice holds what a root reconciled once from scratch holds."""
    namespace = _load_example("action_reconcile_demo.py")
    database, emit, names = namespace["Database"], namespace["emit"], namespace["NAMES"]

    src = tmp_path / "src.txt"
    src.write_text("hi", encoding="utf-8")

    # The warm root reaches the final state through an intermediate one. The
    # source changed and a declared name was replaced, so the warm root must
    # both rewrite alpha.txt and delete the beta.txt it once owned.
    warm_out = tmp_path / "warm"
    warm = database(mode=mode)
    warm.set(names, ("alpha", "beta"))
    emit.reconcile(warm, str(src), root=warm_out)
    src.write_text("bye", encoding="utf-8")
    warm.set(names, ("alpha", "gamma"))
    emit.reconcile(warm, str(src), root=warm_out)

    fresh_out = tmp_path / "fresh"
    fresh = database(mode=mode)
    fresh.set(names, ("alpha", "gamma"))
    emit.reconcile(fresh, str(src), root=fresh_out)

    # Two empty roots would compare equal, so check that the fresh root has files.
    assert _emitted(fresh_out)
    assert _emitted(warm_out) == _emitted(fresh_out)

    namespace["main"](mode=mode)
    printed = capsys.readouterr().out
    assert "orphan_deleted=('beta.txt',)" in printed


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_checkpoint_demo_reload_matches_fresh(
    mode: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A reloaded database answers what a from-scratch one answers, without running the queries."""
    # runpy reuses this interpreter, so the save and the reload below happen in
    # one process. This test checks the reload: the same values, with no query
    # running again. tests/test_checkpoint_cross_process.py covers carrying a
    # checkpoint across a real process boundary.
    namespace = _load_example("checkpoint_demo.py")
    database = namespace["Database"]
    store = namespace["FileSystemArtifactStore"](str(tmp_path / "store"))
    scaled, multiplier = namespace["scaled_word_count"], namespace["MULTIPLIER"]
    data = tmp_path / "data.txt"
    data.write_text("alpha beta gamma delta epsilon", encoding="utf-8")

    saver = database(mode, store=store)
    saver.set(multiplier, 3)
    saver.get(scaled, str(data))
    key = saver.save_checkpoint()

    # Saved and loaded in one mode. A checkpoint warms only a database running
    # the mode that wrote it, and a load across modes is refused.
    reloaded = database(mode, store=store)
    reloaded.set(multiplier, 3)
    reloaded.load_checkpoint(key)
    reloaded_value = reloaded.get(scaled, str(data))

    fresh = database(mode)
    fresh.set(multiplier, 3)
    fresh_value = fresh.get(scaled, str(data))

    assert reloaded_value == fresh_value
    assert reloaded.statistics().query_executions == 0
    assert fresh.statistics().query_executions == 3  # the three queries in the chain

    namespace["main"](mode=mode)
    printed = capsys.readouterr().out
    assert "run2_result=15" in printed
    assert "run3_result=50" in printed
    # checkpoint_key is content-addressed and differs on every run, so it stays unpinned.
