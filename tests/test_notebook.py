from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import pyinc.integrations as integrations
from pyinc import Database
from pyinc.integrations.notebook import (
    NotebookAnalysis,
    NotebookCell,
    notebook_analysis,
    notebook_analysis_payload,
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


def test_cell_reshape_edit_matches_fresh_notebook_analysis(tmp_path: Path) -> None:
    # The two documents differ in cell shape -- two cells that are not objects
    # on one side, a single object cell whose type and source both read
    # "invalid-cell" on the other -- but a projection that reads cell types and
    # sources as one flat sequence of strings cannot tell them apart. A read
    # that compared them that coarsely would serve the second document's bytes
    # under the first document's analysis; `notebook_text` compares the bytes.
    a = '{"cells": [1, 2]}'
    b = '{"cells": [{"cell_type": "invalid-cell", "source": "invalid-cell"}]}'
    path = tmp_path / "nb.ipynb"
    path.write_text(a, encoding="utf-8")

    incremental = Database(mode="strict")
    notebook_analysis(incremental, str(path))
    path.write_text(b, encoding="utf-8")

    warm = notebook_analysis(incremental, str(path))
    fresh = notebook_analysis(Database(mode="strict"), str(path))

    assert warm == fresh, (
        f"mode=strict | warm_cells={len(warm.cells)} fresh_cells={len(fresh.cells)}"
        f" | warm_diags={len(warm.diagnostics)} fresh_diags={len(fresh.diagnostics)}"
        " | the warm read answered with the analysis of the previous bytes"
    )


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
