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
