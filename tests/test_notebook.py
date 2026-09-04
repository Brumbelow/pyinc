from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

import pyinc.integrations as integrations
from pyinc import Database, InMemoryArtifactStore
from pyinc.integrations import notebook
from pyinc.integrations.notebook import (
    NotebookAnalysis,
    NotebookCell,
    NotebookDiagnostic,
    notebook_analysis,
    notebook_analysis_payload,
    notebook_cells_payload,
    notebook_diagnostics_payload,
    workspace_notebook_analysis,
)


def _notebook(
    cells: list[dict[str, Any]],
    *,
    kernel: str | None = "python3",
    language: str | None = "python",
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if kernel is not None or language is not None:
        kernelspec: dict[str, Any] = {}
        if kernel is not None:
            kernelspec["name"] = kernel
        if language is not None:
            kernelspec["language"] = language
        metadata["kernelspec"] = kernelspec
    return {
        "cells": cells,
        "metadata": metadata,
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def _write_notebook(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# Contract lock
# ---------------------------------------------------------------------------


def test_package_namespace_exports_notebook_stable_api() -> None:
    assert "NotebookAnalysis" in integrations.__all__
    assert "NotebookCell" in integrations.__all__
    assert "NotebookDefinition" in integrations.__all__
    assert "NotebookDiagnostic" in integrations.__all__
    assert "NotebookImport" in integrations.__all__
    assert "notebook_analysis" in integrations.__all__
    assert "workspace_notebook_analysis" in integrations.__all__
    assert hasattr(integrations, "notebook_analysis")
    assert hasattr(integrations, "workspace_notebook_analysis")
    assert hasattr(integrations, "NotebookAnalysis")
    # Experimental helpers must not leak.
    assert not hasattr(integrations, "notebook_text")
    assert not hasattr(integrations, "notebook_cells_payload")
    assert not hasattr(integrations, "notebook_analysis_payload")
    assert not hasattr(integrations, "notebook_diagnostics_payload")
    assert not hasattr(integrations, "notebook_metadata_payload")


# ---------------------------------------------------------------------------
# Mode-parametrized correctness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_notebook_analysis_extracts_cells_and_metadata(mode: str, tmp_path: Path) -> None:
    nb = _notebook(
        [
            {"cell_type": "markdown", "source": ["# Title\n", "Some prose."]},
            {
                "cell_type": "code",
                "source": "import os\n\ndef f():\n    return 1\n\nclass C:\n    pass\n",
                "outputs": [],
                "execution_count": None,
            },
            {"cell_type": "raw", "source": "literal text"},
        ]
    )
    path = tmp_path / "notebook.ipynb"
    _write_notebook(path, nb)

    db = Database(mode=mode)
    result = notebook_analysis(db, str(path))

    assert isinstance(result, NotebookAnalysis)
    assert result.path == str(path)
    assert result.kernel_name == "python3"
    assert result.language == "python"
    assert len(result.cells) == 3

    md, code, raw = result.cells
    assert md.cell_type == "markdown"
    assert md.heading == "Title"
    assert md.imports == ()
    assert md.definitions == ()

    assert code.cell_type == "code"
    assert tuple((i.module, i.kind, i.range.start.line) for i in code.imports) == (
        ("os", "import", 0),
    )
    assert tuple((d.name, d.kind, d.range.start.line) for d in code.definitions) == (
        ("f", "function", 2),
        ("C", "class", 5),
    )

    assert raw.cell_type == "raw"
    assert raw.source == "literal text"
    assert result.diagnostics == ()


def test_notebook_handles_source_as_string_or_list(tmp_path: Path) -> None:
    nb = _notebook(
        [
            {"cell_type": "code", "source": "x = 1\n"},
            {"cell_type": "code", "source": ["y = ", "2\n"]},
        ]
    )
    path = tmp_path / "nb.ipynb"
    _write_notebook(path, nb)

    db = Database()
    result = notebook_analysis(db, str(path))
    assert result.cells[0].source == "x = 1\n"
    assert result.cells[1].source == "y = 2\n"


def test_notebook_definition_range_uses_decomposed_source_spelling(tmp_path: Path) -> None:
    path = tmp_path / "nb.ipynb"
    _write_notebook(
        path,
        _notebook([{"cell_type": "code", "source": "def e\u0301():\n    pass\n"}]),
    )

    definition = notebook_analysis(Database(), str(path)).cells[0].definitions[0]

    assert definition.name == "é"
    assert (definition.range.start.line, definition.range.start.character) == (0, 4)
    assert (definition.range.end.line, definition.range.end.character) == (0, 6)


def test_notebook_from_import_lineno_and_kind(tmp_path: Path) -> None:
    nb = _notebook(
        [
            {
                "cell_type": "code",
                "source": "from typing import Any\nfrom .util import helper\n",
            }
        ]
    )
    path = tmp_path / "nb.ipynb"
    _write_notebook(path, nb)

    db = Database()
    result = notebook_analysis(db, str(path))
    code = result.cells[0]
    assert tuple((i.module, i.kind, i.range.start.line) for i in code.imports) == (
        ("typing", "from", 0),
        (".util", "from", 1),
    )


def test_notebook_records_per_cell_syntax_error(tmp_path: Path) -> None:
    nb = _notebook(
        [
            {"cell_type": "code", "source": "x = 1\n"},
            {"cell_type": "code", "source": "def broken(:\n    pass\n"},
        ]
    )
    path = tmp_path / "nb.ipynb"
    _write_notebook(path, nb)

    db = Database()
    result = notebook_analysis(db, str(path))
    assert len(result.diagnostics) == 1
    diag = result.diagnostics[0]
    assert diag.code == "syntax-error"
    assert diag.cell_index == 1
    assert diag.range is not None
    assert diag.range.start.line == 0


def test_notebook_line_magic_keeps_the_rest_of_the_cell(tmp_path: Path) -> None:
    nb = _notebook(
        [
            {
                "cell_type": "code",
                "source": (
                    "%matplotlib inline\n"
                    "import os\n"
                    "from pathlib import Path\n"
                    "\n"
                    "def load():\n"
                    "    return Path(os.curdir)\n"
                    "\n"
                    "class Loader:\n"
                    "    pass\n"
                ),
            }
        ]
    )
    path = tmp_path / "nb.ipynb"
    _write_notebook(path, nb)

    result = notebook_analysis(Database(), str(path))
    cell = result.cells[0]

    assert result.diagnostics == ()
    assert tuple((i.module, i.kind, i.range.start.line) for i in cell.imports) == (
        ("os", "import", 1),
        ("pathlib", "from", 2),
    )
    assert tuple((d.name, d.kind, d.range.start.line) for d in cell.definitions) == (
        ("load", "function", 4),
        ("Loader", "class", 7),
    )


def test_notebook_definition_range_survives_neutralization(tmp_path: Path) -> None:
    source = (
        "%matplotlib inline\n"
        "import pandas as pd\n"
        "\n"
        "def load(path):\n"
        "    return pd.read_csv(path)\n"
    )
    path = tmp_path / "nb.ipynb"
    _write_notebook(path, _notebook([{"cell_type": "code", "source": source}]))

    cell = notebook_analysis(Database(), str(path)).cells[0]
    definition = cell.definitions[0]
    import_range = cell.imports[0].range

    # The placeholder is exactly as wide as the magic it replaced, so every
    # reported range still names its notebook line and column.
    assert (definition.range.start.line, definition.range.start.character) == (3, 4)
    assert (definition.range.end.line, definition.range.end.character) == (3, 8)
    assert cell.source.splitlines()[3][4:8] == "load"
    assert (import_range.start.line, import_range.start.character) == (1, 0)
    assert (import_range.end.line, import_range.end.character) == (1, 19)


def test_notebook_cell_magic_suppresses_the_rest_of_the_cell(tmp_path: Path) -> None:
    source = "%%bash\nimport os\ndef install():\n    pass\n"
    path = tmp_path / "nb.ipynb"
    _write_notebook(
        path,
        _notebook(
            [
                {"cell_type": "code", "source": source},
                {"cell_type": "code", "source": "import json\n"},
            ]
        ),
    )

    result = notebook_analysis(Database(), str(path))

    assert result.diagnostics == ()
    assert result.cells[0].source == source
    assert result.cells[0].imports == ()
    assert result.cells[0].definitions == ()
    # The suppression is scoped to its own cell.
    assert tuple(i.module for i in result.cells[1].imports) == ("json",)


def test_notebook_python_bodied_cell_magic_keeps_its_body(tmp_path: Path) -> None:
    nb = _notebook(
        [
            {
                "cell_type": "code",
                "source": "%%capture\nimport os\n\ndef timed():\n    return os.getpid()\n",
            }
        ]
    )
    path = tmp_path / "nb.ipynb"
    _write_notebook(path, nb)

    cell = notebook_analysis(Database(), str(path)).cells[0]
    assert tuple((i.module, i.range.start.line) for i in cell.imports) == (("os", 1),)
    assert tuple((d.name, d.range.start.line) for d in cell.definitions) == (("timed", 3),)


def test_notebook_shell_escape_and_help_lines_are_neutralized(tmp_path: Path) -> None:
    nb = _notebook(
        [
            {
                "cell_type": "code",
                "source": (
                    "!pip install pandas\n"
                    "import pandas\n"
                    "?pandas\n"
                    "pandas.read_csv?\n"
                    "pandas.read_csv??\n"
                    "listing = !ls -la\n"
                    "\n"
                    "def frame():\n"
                    "    return pandas.DataFrame()\n"
                ),
            }
        ]
    )
    path = tmp_path / "nb.ipynb"
    _write_notebook(path, nb)

    result = notebook_analysis(Database(), str(path))
    cell = result.cells[0]
    assert result.diagnostics == ()
    assert tuple((i.module, i.range.start.line) for i in cell.imports) == (("pandas", 1),)
    assert tuple((d.name, d.range.start.line) for d in cell.definitions) == (("frame", 7),)


def test_notebook_magic_shaped_lines_inside_strings_and_brackets_are_kept(tmp_path: Path) -> None:
    source = (
        "%matplotlib inline\n"
        'BANNER = """\n'
        '%matplotlib inline"""\n'
        "\n"
        "rate = (total\n"
        "%count)\n"
        "flag = (left\n"
        "!= right)\n"
        "\n"
        "def report():\n"
        "    return BANNER, rate, flag\n"
    )
    path = tmp_path / "nb.ipynb"
    _write_notebook(path, _notebook([{"cell_type": "code", "source": source}]))

    result = notebook_analysis(Database(), str(path))

    # Rewriting the string's closing line would leave it unterminated, and
    # rewriting either continuation line would leave a bracket open; both would
    # surface here as a diagnostic.
    assert result.diagnostics == ()
    assert tuple((d.name, d.range.start.line) for d in result.cells[0].definitions) == (
        ("report", 9),
    )


def test_notebook_comment_does_not_continue_onto_a_magic_line(tmp_path: Path) -> None:
    source = "# a backslash in a comment continues nothing \\\n%matplotlib inline\nimport os\n"
    path = tmp_path / "nb.ipynb"
    _write_notebook(path, _notebook([{"cell_type": "code", "source": source}]))

    result = notebook_analysis(Database(), str(path))
    assert result.diagnostics == ()
    assert tuple((i.module, i.range.start.line) for i in result.cells[0].imports) == (("os", 2),)


def test_notebook_reports_a_plain_syntax_error_with_its_position(tmp_path: Path) -> None:
    source = "import os\n\ndef broken(:\n    pass\n"
    path = tmp_path / "nb.ipynb"
    _write_notebook(path, _notebook([{"cell_type": "code", "source": source}]))

    result = notebook_analysis(Database(), str(path))
    assert len(result.diagnostics) == 1
    diag = result.diagnostics[0]
    assert diag.code == "syntax-error"
    assert diag.cell_index == 0
    assert diag.range is not None
    assert diag.range.start.line == 2
    broken_line = source.splitlines()[diag.range.start.line]
    assert broken_line == "def broken(:"
    assert broken_line[diag.range.start.character] in "(:"


def test_notebook_non_python_cell_is_distinguishable_from_a_syntax_error(tmp_path: Path) -> None:
    source = "%matplotlib inline\nimport os\n\ndef broken(:\n    pass\n"
    path = tmp_path / "nb.ipynb"
    _write_notebook(path, _notebook([{"cell_type": "code", "source": source}]))

    result = notebook_analysis(Database(), str(path))
    assert len(result.diagnostics) == 1
    diag = result.diagnostics[0]
    assert diag.code == "notebook-non-python-cell"
    assert "invalid syntax" in diag.message
    assert diag.cell_index == 0
    assert diag.range is not None
    assert diag.range.start.line == 3
    broken_line = source.splitlines()[diag.range.start.line]
    assert broken_line == "def broken(:"
    assert broken_line[diag.range.start.character] in "(:"
    assert result.cells[0].imports == ()


def test_notebook_decode_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.ipynb"
    path.write_text("{not json", encoding="utf-8")

    db = Database()
    result = notebook_analysis(db, str(path))
    assert result.cells == ()
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].code == "notebook-decode-error"
    assert result.diagnostics[0].cell_index is None
    assert result.diagnostics[0].range is not None


def test_notebook_shape_error_top_level(tmp_path: Path) -> None:
    path = tmp_path / "bad.ipynb"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    db = Database()
    result = notebook_analysis(db, str(path))
    assert result.cells == ()
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].code == "notebook-shape-error"


def test_notebook_shape_error_missing_cells(tmp_path: Path) -> None:
    path = tmp_path / "no-cells.ipynb"
    path.write_text(json.dumps({"metadata": {}}), encoding="utf-8")

    db = Database()
    result = notebook_analysis(db, str(path))
    assert result.diagnostics[0].code == "notebook-shape-error"


def test_notebook_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "missing.ipynb"
    db = Database()
    result = notebook_analysis(db, str(path))
    assert result.cells == ()
    assert result.diagnostics == ()
    assert result.kernel_name is None
    assert result.language is None


def test_notebook_unknown_cell_type_is_preserved(tmp_path: Path) -> None:
    nb = _notebook([{"cell_type": "exotic", "source": "weird"}])
    path = tmp_path / "nb.ipynb"
    _write_notebook(path, nb)

    db = Database()
    result = notebook_analysis(db, str(path))
    assert result.cells[0].cell_type == "unknown"
    assert result.cells[0].source == "weird"


def test_notebook_invalid_cell_skipped(tmp_path: Path) -> None:
    nb_payload: dict[str, Any] = {
        "cells": [
            {"cell_type": "code", "source": "x = 1\n"},
            "not-an-object",
            {"cell_type": "markdown", "source": "## sub"},
        ],
        "metadata": {},
    }
    path = tmp_path / "nb.ipynb"
    path.write_text(json.dumps(nb_payload), encoding="utf-8")

    db = Database()
    result = notebook_analysis(db, str(path))
    assert tuple(c.cell_type for c in result.cells) == ("code", "markdown")
    assert result.cells[1].heading == "sub"
    shape_diags = [d for d in result.diagnostics if d.code == "notebook-shape-error"]
    assert any(d.cell_index == 1 for d in shape_diags)


def test_notebook_falls_back_to_language_info(tmp_path: Path) -> None:
    nb_payload: dict[str, Any] = {
        "cells": [],
        "metadata": {"language_info": {"name": "python"}},
    }
    path = tmp_path / "nb.ipynb"
    path.write_text(json.dumps(nb_payload), encoding="utf-8")

    db = Database()
    result = notebook_analysis(db, str(path))
    assert result.kernel_name is None
    assert result.language == "python"


# ---------------------------------------------------------------------------
# Backdating
# ---------------------------------------------------------------------------


def test_output_only_edit_backdates_notebook(tmp_path: Path) -> None:
    nb = _notebook(
        [
            {
                "cell_type": "code",
                "source": "x = 1\n",
                "outputs": [],
                "execution_count": None,
            }
        ]
    )
    path = tmp_path / "nb.ipynb"
    _write_notebook(path, nb)

    db = Database()
    first = notebook_analysis(db, str(path))
    first_changed = db.inspect(notebook_analysis_payload, str(path)).changed_at

    nb["cells"][0]["outputs"] = [{"output_type": "stream", "name": "stdout", "text": "noise\n"}]
    nb["cells"][0]["execution_count"] = 7
    _write_notebook(path, nb)

    second = notebook_analysis(db, str(path))
    second_changed = db.inspect(notebook_analysis_payload, str(path)).changed_at

    assert first == second
    assert second_changed == first_changed


def test_whitespace_only_envelope_edit_backdates_notebook(tmp_path: Path) -> None:
    nb = _notebook([{"cell_type": "code", "source": "x = 1\n"}])
    path = tmp_path / "nb.ipynb"
    path.write_text(json.dumps(nb), encoding="utf-8")

    db = Database()
    first = notebook_analysis(db, str(path))
    first_changed = db.inspect(notebook_analysis_payload, str(path)).changed_at

    # Reformat with indent — semantically identical envelope.
    path.write_text(json.dumps(nb, indent=2), encoding="utf-8")
    second = notebook_analysis(db, str(path))
    second_changed = db.inspect(notebook_analysis_payload, str(path)).changed_at

    assert first == second
    assert second_changed == first_changed


def test_semantic_source_edit_invalidates_notebook(tmp_path: Path) -> None:
    nb = _notebook([{"cell_type": "code", "source": "x = 1\n"}])
    path = tmp_path / "nb.ipynb"
    _write_notebook(path, nb)

    db = Database()
    first = notebook_analysis(db, str(path))
    first_changed = db.inspect(notebook_analysis_payload, str(path)).changed_at

    nb["cells"][0]["source"] = "x = 2\n"
    _write_notebook(path, nb)

    second = notebook_analysis(db, str(path))
    second_changed = db.inspect(notebook_analysis_payload, str(path)).changed_at

    assert first != second
    assert second_changed > first_changed


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_workspace_notebook_analysis_discovers_ipynb(tmp_path: Path) -> None:
    a = _notebook([{"cell_type": "code", "source": "import a"}])
    b = _notebook([{"cell_type": "code", "source": "import b"}])
    _write_notebook(tmp_path / "alpha.ipynb", a)
    _write_notebook(tmp_path / "beta.ipynb", b)
    (tmp_path / "ignore.txt").write_text("not a notebook", encoding="utf-8")

    db = Database()
    results = workspace_notebook_analysis(db, str(tmp_path))
    assert len(results) == 2
    paths = {r.path for r in results}
    assert paths == {str(tmp_path / "alpha.ipynb"), str(tmp_path / "beta.ipynb")}


def test_workspace_notebook_analysis_empty_directory(tmp_path: Path) -> None:
    db = Database()
    assert workspace_notebook_analysis(db, str(tmp_path)) == ()


def test_workspace_notebook_analysis_nonexistent_directory(tmp_path: Path) -> None:
    db = Database()
    missing = tmp_path / "no-such-dir"
    # DirectoryResource raises NotADirectoryError on missing paths in some
    # underlying backends; the integration coerces this to an empty tuple.
    result = workspace_notebook_analysis(db, str(missing))
    assert result == ()


# ---------------------------------------------------------------------------
# From-scratch oracle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_notebook_analysis_matches_fresh_recomputation_over_changes(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "nb.ipynb"
    base = _notebook(
        [
            {"cell_type": "markdown", "source": "# Heading"},
            {"cell_type": "code", "source": "import os\n"},
        ]
    )

    def with_outputs() -> dict[str, Any]:
        nb: dict[str, Any] = json.loads(json.dumps(base))
        nb["cells"][1]["outputs"] = [{"output_type": "stream", "name": "stdout", "text": "hi"}]
        nb["cells"][1]["execution_count"] = 1
        return nb

    def with_added_cell() -> dict[str, Any]:
        nb: dict[str, Any] = json.loads(json.dumps(base))
        nb["cells"].append({"cell_type": "code", "source": "y = 1\n"})
        return nb

    def with_changed_source() -> dict[str, Any]:
        nb: dict[str, Any] = json.loads(json.dumps(base))
        nb["cells"][1]["source"] = "import os\nimport sys\n"
        return nb

    def with_kernel_change() -> dict[str, Any]:
        nb: dict[str, Any] = json.loads(json.dumps(base))
        nb["metadata"]["kernelspec"] = {"name": "python2", "language": "python"}
        return nb

    steps = (
        ("initial", base),
        ("outputs only", with_outputs()),
        ("add cell", with_added_cell()),
        ("change source", with_changed_source()),
        ("change kernel", with_kernel_change()),
    )

    incremental = Database(mode=mode)
    for _label, content in steps:
        path.write_text(json.dumps(content), encoding="utf-8")
        fresh = Database(mode=mode)
        assert notebook_analysis(incremental, str(path)) == notebook_analysis(fresh, str(path))


# The pool above is well-formed throughout, which is why it never caught the
# reshape below: these two documents are deliberately malformed, so the
# `_notebook` envelope is the wrong shape for them and they are written raw.
_ARITY_A = '{"cells": [1, 2]}'
_ARITY_B = '{"cells": [{"cell_type": "invalid-cell", "source": "invalid-cell"}]}'


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize(
    ("before", "after"),
    [(_ARITY_A, _ARITY_B), (_ARITY_B, _ARITY_A)],
    ids=["A->B", "B->A"],
)
def test_notebook_edit_matches_a_fresh_read(
    tmp_path: Path, mode: str, before: str, after: str
) -> None:
    # These two documents project to the same flat tuple of strings: a cell
    # that is not an object contributes one element and a cell that is
    # contributes two, so a one-cell notebook and a two-cell one can line up.
    # A read that compared by that projection answered the second document
    # with the first document's analysis.
    path = tmp_path / "sample.ipynb"
    path.write_text(before, encoding="utf-8")
    incremental = Database(mode=mode)
    notebook_analysis(incremental, str(path))

    path.write_text(after, encoding="utf-8")
    warm = notebook_analysis(incremental, str(path))
    scratch = Database(mode=mode)
    fresh = notebook_analysis(scratch, str(path))

    # One line, counts first. A parametrized node id this long fills the whole
    # summary line on its own, so under the default `--tb=no` no message
    # survives at all; read a failure here with `-o addopts="" --tb=long`. The
    # message is laid out for that read: the numbers that identify the failure
    # first, the documents last.
    assert warm == fresh, (
        f"cells {len(warm.cells)}!={len(fresh.cells)} | "
        f"diags {len(warm.diagnostics)}!={len(fresh.diagnostics)} | "
        f"{before} -> {after}"
    )

    # Secondary diagnostics: which parsed projection moved. Metadata is left
    # out on purpose -- it reads only the kernelspec and the language info,
    # which both documents share, so a metadata comparison passes here whether
    # or not the analysis diverged.
    warm_cells = notebook_cells_payload(incremental, str(path))
    scratch_cells = notebook_cells_payload(scratch, str(path))
    assert warm_cells == scratch_cells, (
        f"cells payload {len(warm_cells)}!={len(scratch_cells)} | {before} -> {after}"
    )

    warm_diags = notebook_diagnostics_payload(incremental, str(path))
    scratch_diags = notebook_diagnostics_payload(scratch, str(path))
    assert warm_diags == scratch_diags, (
        f"diagnostics payload {len(warm_diags)}!={len(scratch_diags)} | {before} -> {after}"
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_notebook_edit_survives_a_checkpoint(tmp_path: Path, mode: str) -> None:
    # The edit has to happen before the save. Saving first and editing after
    # does not reproduce: on reload the resource probe mismatches, the read
    # executes on the new bytes, and there is no earlier comparison left to
    # answer from -- the test would then be green whether or not the bug is
    # present.
    path = tmp_path / "sample.ipynb"
    path.write_text(_ARITY_A, encoding="utf-8")

    store = InMemoryArtifactStore()
    saver = Database(mode=mode, store=store)
    notebook_analysis(saver, str(path))

    path.write_text(_ARITY_B, encoding="utf-8")
    notebook_analysis(saver, str(path))
    key = saver.save_checkpoint()

    reloaded = Database(mode=mode, store=store)
    reloaded.load_checkpoint(key)

    # Compare values, never a recompute marker: a reloaded record reports
    # `executed` or `reused` either way, so the marker cannot tell a restored
    # analysis from a stale one. Ask the entrypoint rather than a payload
    # leaf, too -- checkpoint warming is parent-driven, and a leaf asked on
    # its own cold-executes even when its record is in the manifest.
    warm = notebook_analysis(reloaded, str(path))
    fresh = notebook_analysis(Database(mode=mode), str(path))

    assert warm == fresh, (
        f"cells {len(warm.cells)}!={len(fresh.cells)} | "
        f"diags {len(warm.diagnostics)}!={len(fresh.diagnostics)} | "
        f"reloaded from a checkpoint saved after {_ARITY_A} -> {_ARITY_B}"
    )


# ---------------------------------------------------------------------------
# Shapes a flat projection could not tell apart
# ---------------------------------------------------------------------------
#
# The diagnostics payload decides on `cells` three ways -- the field is absent,
# it is present but is not a list, or it is walked cell by cell -- and the
# documents below cover all three, together with the pair above whose cell
# counts differ while their flattened text lines up. Each row drives the public
# entrypoint over an explicit ordered pair, never a sampled one, and pins the
# analysis the second document has to produce, so a change to a reported
# diagnostic fails here as itself rather than somewhere downstream as a
# mismatch against a corpus.

_NB_NO_CELLS_FIELD = '{"metadata": {}, "nbformat": 4}'
_NB_EMPTY_CELLS = '{"cells": [], "metadata": {}, "nbformat": 4}'
_NB_CELLS_NOT_A_LIST = '{"cells": {}, "metadata": {}, "nbformat": 4}'

_NotebookShape = tuple[str, tuple[NotebookCell, ...], tuple[NotebookDiagnostic, ...]]

# One spelling for the comparand throughout: the entrypoint reports
# `NotebookDiagnostic`, which compares unequal to any tuple, so the expectation
# is written as the dataclass rather than as the payload's flat triple.
_NOTEBOOK_SHAPES: dict[str, _NotebookShape] = {
    "missing": (
        _NB_NO_CELLS_FIELD,
        (),
        (
            NotebookDiagnostic(
                code="notebook-shape-error",
                message="missing 'cells' field",
                cell_index=None,
                range=None,
            ),
        ),
    ),
    "empty": (_NB_EMPTY_CELLS, (), ()),
    "not-a-list": (
        _NB_CELLS_NOT_A_LIST,
        (),
        (
            NotebookDiagnostic(
                code="notebook-shape-error",
                message="'cells' is not a list",
                cell_index=None,
                range=None,
            ),
        ),
    ),
    "non-object-cells": (
        _ARITY_A,
        (),
        (
            NotebookDiagnostic(
                code="notebook-shape-error",
                message="cell is not an object",
                cell_index=0,
                range=None,
            ),
            NotebookDiagnostic(
                code="notebook-shape-error",
                message="cell is not an object",
                cell_index=1,
                range=None,
            ),
        ),
    ),
    "unknown-cell": (
        _ARITY_B,
        (
            NotebookCell(
                index=0,
                cell_type="unknown",
                source="invalid-cell",
                heading=None,
                imports=(),
                definitions=(),
            ),
        ),
        (),
    ),
}

# Both directions for every pair. A read that answers with an earlier analysis
# answers with the *first* document's, so which document that is decides what a
# wrong read reports and one direction alone leaves half the shape unpinned.
_NOTEBOOK_SHAPE_PAIRS = (
    ("missing", "empty"),
    ("empty", "missing"),
    ("non-object-cells", "unknown-cell"),
    ("unknown-cell", "non-object-cells"),
    ("not-a-list", "empty"),
    ("empty", "not-a-list"),
    ("not-a-list", "missing"),
    ("missing", "not-a-list"),
)

# Across a checkpoint, the missing/empty and object/non-object pairs: the two
# whose members a projection reading `cells` with a default cannot tell apart.
# A `cells` field that is not a list is distinguishable in the text itself, so
# it is covered live above rather than across a save as well.
_NOTEBOOK_SHAPE_CHECKPOINT_PAIRS = (
    ("missing", "empty"),
    ("empty", "missing"),
    ("non-object-cells", "unknown-cell"),
    ("unknown-cell", "non-object-cells"),
)


def _diagnostic_summary(diagnostics: tuple[NotebookDiagnostic, ...]) -> str:
    return ";".join(f"{d.message}@{d.cell_index}" for d in diagnostics) or "none"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize(("before", "after"), _NOTEBOOK_SHAPE_PAIRS)
def test_notebook_shape_pair_reads_the_same_warm_and_fresh(
    tmp_path: Path, mode: str, before: str, after: str
) -> None:
    before_text = _NOTEBOOK_SHAPES[before][0]
    after_text, expected_cells, expected_diagnostics = _NOTEBOOK_SHAPES[after]

    path = tmp_path / "sample.ipynb"
    path.write_text(before_text, encoding="utf-8")
    incremental = Database(mode=mode)
    notebook_analysis(incremental, str(path))

    path.write_text(after_text, encoding="utf-8")
    warm = notebook_analysis(incremental, str(path))
    fresh = notebook_analysis(Database(mode=mode), str(path))

    # Pin the diagnostics, not only warm == fresh: two reads that agree on the
    # wrong analysis agree just as loudly as two that are right. One line per
    # message, discriminator first -- a node id this long fills the summary
    # line on its own, so read a failure with `-o addopts="" --tb=long`.
    expected = _diagnostic_summary(expected_diagnostics)
    assert warm.diagnostics == expected_diagnostics, (
        f"warm diagnostics {_diagnostic_summary(warm.diagnostics)} != {expected}"
        f" | {before} -> {after}"
    )
    assert warm.cells == expected_cells, (
        f"warm cells {len(warm.cells)}!={len(expected_cells)} | {before} -> {after}"
    )
    assert fresh.diagnostics == expected_diagnostics, (
        f"fresh diagnostics {_diagnostic_summary(fresh.diagnostics)} != {expected}"
        f" | {before} -> {after}"
    )
    assert fresh.cells == expected_cells, (
        f"fresh cells {len(fresh.cells)}!={len(expected_cells)} | {before} -> {after}"
    )
    assert warm == fresh, (
        f"warm cells {len(warm.cells)}!={len(fresh.cells)} | "
        f"diags {len(warm.diagnostics)}!={len(fresh.diagnostics)} | {before} -> {after}"
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize(("before", "after"), _NOTEBOOK_SHAPE_CHECKPOINT_PAIRS)
def test_notebook_shape_pair_survives_a_checkpoint(
    tmp_path: Path, mode: str, before: str, after: str
) -> None:
    # The edit has to happen before the save, and the entrypoint has to be
    # driven again afterwards. Saving first and editing after does not
    # reproduce: on reload the resource probe mismatches, the read executes on
    # the new bytes, and there is no earlier comparison left to answer from.
    before_text, before_cells, before_diagnostics = _NOTEBOOK_SHAPES[before]
    after_text, expected_cells, expected_diagnostics = _NOTEBOOK_SHAPES[after]

    path = tmp_path / "sample.ipynb"
    path.write_text(before_text, encoding="utf-8")

    store = InMemoryArtifactStore()
    saver = Database(mode=mode, store=store)
    notebook_analysis(saver, str(path))

    path.write_text(after_text, encoding="utf-8")
    notebook_analysis(saver, str(path))
    key = saver.save_checkpoint()

    reloaded_db = Database(mode=mode, store=store)
    reloaded_db.load_checkpoint(key)

    # Values only. A reloaded record reports `executed` or `reused` either way,
    # so a recompute marker cannot tell a restored analysis from a stale one.
    reloaded = notebook_analysis(reloaded_db, str(path))
    fresh = notebook_analysis(Database(mode=mode), str(path))

    expected = _diagnostic_summary(expected_diagnostics)
    assert reloaded.diagnostics == expected_diagnostics, (
        f"reloaded diagnostics {_diagnostic_summary(reloaded.diagnostics)} != {expected}"
        f" | saved after {before} -> {after}"
    )
    assert reloaded.cells == expected_cells, (
        f"reloaded cells {len(reloaded.cells)}!={len(expected_cells)}"
        f" | saved after {before} -> {after}"
    )
    assert reloaded == fresh, (
        f"reloaded cells {len(reloaded.cells)}!={len(fresh.cells)} | "
        f"diags {len(reloaded.diagnostics)}!={len(fresh.diagnostics)} | "
        f"saved after {before} -> {after}"
    )
    # The two documents have to analyze differently at the entrypoint, or the
    # assertions above would hold whatever the reload carried across.
    assert (reloaded.cells, reloaded.diagnostics) != (before_cells, before_diagnostics), (
        f"reloaded matches the pre-edit analysis {_diagnostic_summary(before_diagnostics)}"
        f" | saved after {before} -> {after}"
    )


# ---------------------------------------------------------------------------
# Extra invariants
# ---------------------------------------------------------------------------


def test_notebook_cell_dataclass_is_frozen() -> None:
    cell = NotebookCell(
        index=0,
        cell_type="code",
        source="",
        heading=None,
        imports=(),
        definitions=(),
    )
    with pytest.raises(AttributeError):
        cell.index = 5  # type: ignore[misc]


def test_notebook_markdown_heading_strips_hashes(tmp_path: Path) -> None:
    nb = _notebook(
        [
            {"cell_type": "markdown", "source": "### Sub-heading text"},
            {"cell_type": "markdown", "source": "no heading here"},
            {"cell_type": "markdown", "source": "    # indented heading"},
        ]
    )
    path = tmp_path / "nb.ipynb"
    _write_notebook(path, nb)

    db = Database()
    result = notebook_analysis(db, str(path))
    assert result.cells[0].heading == "Sub-heading text"
    assert result.cells[1].heading is None
    assert result.cells[2].heading == "indented heading"


# ---------------------------------------------------------------------------
# Lone surrogates
# ---------------------------------------------------------------------------
#
# RFC 8259 permits `\uD800`-style escapes and `json.loads` decodes them, but a
# lone surrogate is not a Unicode scalar value and so cannot cross a cached
# boundary. Cell sources and the kernel metadata reach the parsed payloads
# verbatim, so whatever the integration reports for such a notebook, it has to
# report it identically on a first read, after an edit, and from a database that
# never saw the file.


_SURROGATE_NOTEBOOKS: tuple[tuple[str, str], ...] = (
    ("markdown source", '{"cells": [{"cell_type": "markdown", "source": ["\\ud800"]}]}'),
    ("code source", '{"cells": [{"cell_type": "code", "source": ["x = \\"\\udfff\\"\\n"]}]}'),
    ("raw source", '{"cells": [{"cell_type": "raw", "source": "\\ud800"}]}'),
    (
        "kernel name",
        '{"cells": [], "metadata": {"kernelspec": {"name": "\\ud800", "language": "python"}}}',
    ),
    (
        "language info",
        '{"cells": [], "metadata": {"language_info": {"name": "\\udfff"}}}',
    ),
)


@pytest.mark.parametrize(("label", "document"), _SURROGATE_NOTEBOOKS)
def test_lone_surrogate_notebooks_analyze_identically_warm_and_fresh(
    label: str, document: str, tmp_path: Path
) -> None:
    assert json.loads(document) is not None

    path = tmp_path / "nb.ipynb"
    path.write_text(document, encoding="utf-8")
    first = notebook_analysis(Database(), str(path))

    incremental = Database()
    _write_notebook(path, _notebook([]))
    notebook_analysis(incremental, str(path))
    # Warm the database on a clean notebook first, so the surrogate arrives as
    # an edit rather than as a first read.
    path.write_text(document, encoding="utf-8")

    assert notebook_analysis(incremental, str(path)) == first
    assert notebook_analysis(Database(), str(path)) == first


@pytest.mark.parametrize(("label", "document"), _SURROGATE_NOTEBOOKS)
def test_lone_surrogate_notebooks_are_reported_as_a_decode_error(
    label: str, document: str, tmp_path: Path
) -> None:
    path = tmp_path / "nb.ipynb"
    path.write_text(document, encoding="utf-8")

    analysis = notebook_analysis(Database(), str(path))
    assert analysis.cells == ()
    assert analysis.kernel_name is None
    assert analysis.language is None
    assert [diagnostic.code for diagnostic in analysis.diagnostics] == ["notebook-decode-error"]
    assert "surrogate" in analysis.diagnostics[0].message


def test_surrogate_outside_the_payload_leaves_a_notebook_analyzable(tmp_path: Path) -> None:
    # Outputs and execution metadata never reach the payload, so a surrogate
    # there is not the integration's problem and must not cost the notebook its
    # analysis.
    path = tmp_path / "nb.ipynb"
    path.write_text(
        '{"cells": [{"cell_type": "code", "source": ["import os\\n"],'
        ' "outputs": [{"text": "\\ud800"}], "execution_count": 1}],'
        ' "metadata": {"kernelspec": {"name": "python3", "language": "python"}}}',
        encoding="utf-8",
    )

    analysis = notebook_analysis(Database(), str(path))
    assert analysis.diagnostics == ()
    assert tuple(imported.module for imported in analysis.cells[0].imports) == ("os",)


def test_notebook_payload_queries_answer_malformed_documents(tmp_path: Path) -> None:
    db = Database()

    not_json = tmp_path / "not-json.ipynb"
    not_json.write_text("not json", encoding="utf-8")
    decode_diagnostics = notebook.notebook_diagnostics_payload(db, str(not_json))
    # The decoder's own wording belongs to the interpreter, so only the code
    # and the (absent) cell index are pinned here.
    assert [(entry[0], entry[2]) for entry in decode_diagnostics] == [
        ("notebook-decode-error", None)
    ]
    assert notebook.notebook_cells_payload(db, str(not_json)) == ()

    cells_not_list = tmp_path / "cells-not-list.ipynb"
    cells_not_list.write_text('{"cells": {}}', encoding="utf-8")
    assert notebook.notebook_diagnostics_payload(db, str(cells_not_list)) == (
        ("notebook-shape-error", "'cells' is not a list", None),
    )
    assert notebook.notebook_cells_payload(db, str(cells_not_list)) == ()

    null_cell = tmp_path / "null-cell.ipynb"
    null_cell.write_text(
        '{"cells":[null],"metadata":{"kernelspec":null,"language_info":{"name":"python"}}}',
        encoding="utf-8",
    )
    assert notebook.notebook_metadata_payload(db, str(null_cell)) == (None, "python")
    assert notebook.notebook_cells_payload(db, str(null_cell)) == ()
    assert notebook.notebook_diagnostics_payload(db, str(null_cell)) == (
        ("notebook-shape-error", "cell is not an object", 0),
    )


def test_notebook_metadata_payload_handles_malformed_shapes_and_metadata(
    tmp_path: Path,
) -> None:
    db = Database()

    def metadata_of(name: str, text: str) -> Any:
        path = tmp_path / f"{name}.ipynb"
        path.write_text(text, encoding="utf-8")
        return notebook.notebook_metadata_payload(db, os.fspath(path))

    # Undecodable text never becomes a document: `_try_parse_notebook` answers
    # `None` and the payload returns before it looks at any metadata.
    assert metadata_of("not_json", "not json") == (None, None)

    # A `cells` field that is not a list still decodes to a document, so this
    # one reaches the same answer by the other route: the metadata block runs
    # and the empty `metadata` object yields neither a kernel nor a language.
    assert metadata_of("cells_not_list", json.dumps({"cells": {}, "metadata": {}})) == (
        None,
        None,
    )

    # A `metadata` field that is not an object short-circuits before either
    # holder is consulted.
    invalid_metadata = json.dumps({"cells": [None], "metadata": "invalid"})
    assert metadata_of("invalid_metadata", invalid_metadata) == (None, None)

    # A non-string `kernelspec.name` is ignored; `kernelspec.language` is taken.
    kernelspec_language = json.dumps(
        {
            "cells": [],
            "metadata": {"kernelspec": {"name": 7, "language": "R"}},
        }
    )
    assert metadata_of("kernelspec_language", kernelspec_language) == (None, "R")

    # A non-string `kernelspec.language` falls through to `language_info`.
    language_info = json.dumps(
        {
            "cells": [],
            "metadata": {
                "kernelspec": {"language": 7},
                "language_info": {"name": "python"},
            },
        }
    )
    assert metadata_of("language_info", language_info) == (None, "python")

    # Neither holder yields a string: `kernelspec` is not an object at all and
    # `language_info.name` is a number.
    nonstring_language_info = json.dumps(
        {
            "cells": [],
            "metadata": {
                "kernelspec": [],
                "language_info": {"name": 7},
            },
        }
    )
    assert metadata_of("nonstring_language_info", nonstring_language_info) == (None, None)

    # A `language_info` that is present but not an object is passed over the
    # same way an absent one is.
    invalid_language_info = json.dumps({"cells": [], "metadata": {"language_info": "invalid"}})
    assert metadata_of("invalid_language_info", invalid_language_info) == (None, None)


def test_notebook_workspace_rejects_a_regular_file(tmp_path: Path) -> None:
    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("content", encoding="utf-8")

    assert notebook.workspace_notebook_analysis(Database(), regular_file) == ()
