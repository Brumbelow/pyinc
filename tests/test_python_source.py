from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from pyfoundinc import Database
from pyfoundinc.integrations.python_source import (
    DefinitionRef,
    ImportRef,
    PythonFileAnalysis,
    directory_analysis,
    file_analysis,
    file_analysis_payload,
    imports_for_file,
    source_text,
)

Operation = tuple[Literal["write", "delete"], str, str | None]


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_file_analysis_reports_top_level_symbols_by_mode(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text(
        "import os\n"
        "from pkg.sub import thing\n"
        "def alpha():\n"
        "    return 1\n"
        "async def beta():\n"
        "    return 2\n"
        "class Gamma:\n"
        "    pass\n",
        encoding="utf-8",
    )

    analysis = file_analysis(Database(mode=mode), path)

    assert analysis == PythonFileAnalysis(
        path=str(path),
        imports=(
            ImportRef(module="os", kind="import", lineno=1),
            ImportRef(module="pkg.sub", kind="from", lineno=2),
        ),
        definitions=(
            DefinitionRef(name="alpha", kind="function", lineno=3),
            DefinitionRef(name="beta", kind="function", lineno=5),
            DefinitionRef(name="Gamma", kind="class", lineno=7),
        ),
        diagnostics=(),
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_file_analysis_reports_syntax_errors(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    path.write_text("def broken(\n", encoding="utf-8")

    analysis = file_analysis(Database(mode=mode), path)

    assert analysis.imports == ()
    assert analysis.definitions == ()
    assert len(analysis.diagnostics) == 1
    assert analysis.diagnostics[0].code == "syntax-error"
    assert analysis.diagnostics[0].message
    assert analysis.diagnostics[0].lineno == 1
    assert analysis.diagnostics[0].col_offset is not None


def test_comment_only_edit_backdates_source_and_reuses_downstream(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("import os\n", encoding="utf-8")
    db = Database(mode="strict")

    first = file_analysis(db, path)
    assert first.imports == (ImportRef(module="os", kind="import", lineno=1),)

    path.write_text("import os\n# trailing comment\n", encoding="utf-8")
    second = file_analysis(db, path)

    assert second.imports == (ImportRef(module="os", kind="import", lineno=1),)
    assert db.inspect(source_text, str(path)).last_recompute == "backdated"
    assert db.inspect(imports_for_file, str(path)).last_decision == "reused"
    assert db.inspect(file_analysis_payload, str(path)).last_decision == "reused"


def test_semantic_edit_invalidates_downstream_analysis(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    path.write_text("import os\n", encoding="utf-8")
    db = Database(mode="strict")

    assert file_analysis(db, path).imports == (ImportRef(module="os", kind="import", lineno=1),)

    path.write_text("import sys\n", encoding="utf-8")
    updated = file_analysis(db, path)

    assert updated.imports == (ImportRef(module="sys", kind="import", lineno=1),)
    assert db.inspect(source_text, str(path)).last_recompute == "executed"
    assert db.inspect(imports_for_file, str(path)).last_recompute == "executed"
    assert db.inspect(file_analysis_payload, str(path)).last_decision == "executed"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_directory_analysis_is_non_recursive_and_sorted(mode: str, tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    nested = root / "nested"
    nested.mkdir()

    (root / "b.py").write_text("import sys\n", encoding="utf-8")
    (root / "a.py").write_text("import os\n", encoding="utf-8")
    (root / "notes.txt").write_text("ignored\n", encoding="utf-8")
    (nested / "inner.py").write_text("import json\n", encoding="utf-8")

    analyses = directory_analysis(Database(mode=mode), root)

    assert tuple(Path(item.path).name for item in analyses) == ("a.py", "b.py")
    assert analyses[0].imports == (ImportRef(module="os", kind="import", lineno=1),)
    assert analyses[1].imports == (ImportRef(module="sys", kind="import", lineno=1),)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_file_analysis_matches_fresh_recomputation_over_edits(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    contents = (
        "import os\n",
        "import os\n# trailing comment\n",
        "import sys\n",
        "def broken(\n",
        "class Example:\n    pass\n",
    )

    incremental = Database(mode=mode)
    for content in contents:
        path.write_text(content, encoding="utf-8")
        fresh = Database(mode=mode)
        assert file_analysis(incremental, path) == file_analysis(fresh, path)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_directory_analysis_matches_fresh_recomputation_over_changes(mode: str, tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    steps: tuple[Operation, ...] = (
        ("write", "b.py", "import sys\n"),
        ("write", "a.py", "import os\n"),
        ("write", "a.py", "import os\n# trailing comment\n"),
        ("write", "notes.txt", "ignored\n"),
        ("delete", "b.py", None),
        ("write", "c.py", "def broken(\n"),
    )

    incremental = Database(mode=mode)
    for operation, name, content in steps:
        target = root / name
        if operation == "write":
            assert content is not None
            target.write_text(content, encoding="utf-8")
        else:
            target.unlink()

        fresh = Database(mode=mode)
        assert directory_analysis(incremental, root) == directory_analysis(fresh, root)
