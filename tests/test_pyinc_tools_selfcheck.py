"""Analyze pyinc's own source and expect no diagnostics.

All 13 diagnostics 5.0.0 reported on ``src`` were false, and their quick fixes
deleted live imports. The first test pins the count at zero. The second keeps
it honest: an unused import in a small workspace must still be reported, so a
clean ``src`` cannot come from a check that stopped firing.

The analysis of ``src`` is slow. The ``process`` mark keeps it out of the
platform matrix, and the quality job runs it once.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pyinc_tools import WorkspaceSession

pytestmark = pytest.mark.process

_SRC = Path(__file__).resolve().parent.parent / "src"


def test_pyinc_src_analyzes_clean() -> None:
    with WorkspaceSession(_SRC) as session:
        result = session.analyze_workspace()

    assert len(result.files) >= 45
    assert result.diagnostics == ()


def test_unused_from_import_is_still_reported(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "m.py").write_text("def name() -> int:\n    return 1\n", encoding="utf-8")
    (root / "consumer.py").write_text("from m import name\n\nx = 1\n", encoding="utf-8")

    with WorkspaceSession(root) as session:
        result = session.analyze_workspace()

    unused = [d for d in result.diagnostics if d.code == "unused-import"]
    assert len(unused) == 1
    assert Path(unused[0].path).name == "consumer.py"
    assert unused[0].range is not None
    assert unused[0].range.start.line == 0
    assert "name" in unused[0].message
