from __future__ import annotations

from pathlib import Path

import pytest

import pyinc.integrations as integrations
from pyinc import Database
from pyinc.integrations.csv_data import (
    CsvAnalysis,
    csv_analysis,
    workspace_csv_analysis,
)

_MINIMAL_CSV = """\
name,age,city
Alice,30,Portland
Bob,25,Seattle
Charlie,35,Denver
"""

_TSV_DATA = """\
name\tage\tcity
Alice\t30\tPortland
Bob\t25\tSeattle
"""

_NO_HEADER_CSV = """\
Alice,30,Portland
Bob,25,Seattle
"""

_INCONSISTENT_CSV = """\
name,age,city
Alice,30,Portland
Bob,25
Charlie,35,Denver,extra
"""


# ---------------------------------------------------------------------------
# Contract lock
# ---------------------------------------------------------------------------


def test_package_namespace_exports_csv_data_stable_api() -> None:
    assert "CsvColumn" in integrations.__all__
    assert "CsvAnalysis" in integrations.__all__
    assert "csv_analysis" in integrations.__all__
    assert "workspace_csv_analysis" in integrations.__all__

    assert hasattr(integrations, "csv_analysis")
    assert hasattr(integrations, "workspace_csv_analysis")
    assert hasattr(integrations, "CsvAnalysis")
    assert hasattr(integrations, "CsvColumn")

    # Experimental helpers must not leak.
    assert not hasattr(integrations, "csv_file_text")
    assert not hasattr(integrations, "csv_columns_payload")
    assert not hasattr(integrations, "csv_meta_payload")
    assert not hasattr(integrations, "csv_analysis_payload")
    assert not hasattr(integrations, "csv_diagnostics_payload")


# ---------------------------------------------------------------------------
# Mode-parametrized correctness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_csv_analysis_extracts_columns(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text(_MINIMAL_CSV, encoding="utf-8")

    db = Database(mode=mode)
    result = csv_analysis(db, str(path))

    assert isinstance(result, CsvAnalysis)
    assert result.path == str(path)
    assert result.has_header is True
    assert result.delimiter == ","

    col_names = [c.name for c in result.columns]
    assert col_names == ["name", "age", "city"]
    assert result.columns[0].index == 0
    assert result.columns[2].index == 2


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_csv_analysis_counts_rows(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text(_MINIMAL_CSV, encoding="utf-8")

    db = Database(mode=mode)
    result = csv_analysis(db, str(path))

    assert result.row_count == 3


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_csv_analysis_detects_tsv(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "data.tsv"
    path.write_text(_TSV_DATA, encoding="utf-8")

    db = Database(mode=mode)
    result = csv_analysis(db, str(path))

    assert result.delimiter == "\t"
    assert result.has_header is True
    col_names = [c.name for c in result.columns]
    assert col_names == ["name", "age", "city"]
    assert result.row_count == 2


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_csv_analysis_no_header(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text(_NO_HEADER_CSV, encoding="utf-8")

    db = Database(mode=mode)
    result = csv_analysis(db, str(path))

    # Sniffer may or may not detect header; check columns exist
    assert len(result.columns) > 0
    assert result.row_count >= 1


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_csv_analysis_inconsistent_columns(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text(_INCONSISTENT_CSV, encoding="utf-8")

    db = Database(mode=mode)
    result = csv_analysis(db, str(path))

    assert len(result.diagnostics) >= 1
    diag_types = {d[0] for d in result.diagnostics}
    assert "inconsistent-columns" in diag_types


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_csv_analysis_empty_file(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("", encoding="utf-8")

    db = Database(mode=mode)
    result = csv_analysis(db, str(path))

    assert result.columns == ()
    assert result.row_count == 0
    assert result.diagnostics == ()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_csv_analysis_missing_file(mode: str, tmp_path: Path) -> None:
    db = Database(mode=mode)
    result = csv_analysis(db, str(tmp_path / "nonexistent.csv"))

    assert result.columns == ()
    assert result.row_count == 0
    assert result.diagnostics == ()


# ---------------------------------------------------------------------------
# Workspace discovery
# ---------------------------------------------------------------------------


def test_workspace_csv_analysis_discovers_csv(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text(_MINIMAL_CSV, encoding="utf-8")

    db = Database()
    result = workspace_csv_analysis(db, str(tmp_path))

    assert result is not None
    assert result.row_count == 3


def test_workspace_csv_analysis_returns_none_when_missing(tmp_path: Path) -> None:
    db = Database()
    result = workspace_csv_analysis(db, str(tmp_path))
    assert result is None


def test_workspace_csv_analysis_custom_filename(tmp_path: Path) -> None:
    path = tmp_path / "export.csv"
    path.write_text(_MINIMAL_CSV, encoding="utf-8")

    db = Database()
    result = workspace_csv_analysis(db, str(tmp_path), filename="export.csv")

    assert result is not None
    assert result.row_count == 3


# ---------------------------------------------------------------------------
# Backdating
# ---------------------------------------------------------------------------


def test_trailing_whitespace_edit_backdates_csv(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("name,age\nAlice,30\n", encoding="utf-8")

    db = Database()
    first = csv_analysis(db, str(path))

    # Add trailing newlines — semantically identical
    path.write_text("name,age\nAlice,30\n\n\n", encoding="utf-8")
    second = csv_analysis(db, str(path))

    assert first == second


def test_column_rename_invalidates_csv(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("name,age\nAlice,30\n", encoding="utf-8")

    db = Database()
    first = csv_analysis(db, str(path))

    path.write_text("full_name,age\nAlice,30\n", encoding="utf-8")
    second = csv_analysis(db, str(path))

    assert first != second
    assert first.columns[0].name != second.columns[0].name


def test_add_row_invalidates_csv(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("name,age\nAlice,30\n", encoding="utf-8")

    db = Database()
    first = csv_analysis(db, str(path))

    path.write_text("name,age\nAlice,30\nBob,25\n", encoding="utf-8")
    second = csv_analysis(db, str(path))

    assert first.row_count != second.row_count


def test_diagnostic_only_edit_invalidates_csv(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("first,second\n1\n2,3\n", encoding="utf-8")
    db = Database(mode="strict")
    assert "row 2:" in csv_analysis(db, path).diagnostics[0][1]

    path.write_text("first,second\n1,2\n3\n", encoding="utf-8")
    incremental = csv_analysis(db, path)
    fresh = csv_analysis(Database(mode="strict"), path)

    assert incremental == fresh
    assert "row 3:" in incremental.diagnostics[0][1]


# ---------------------------------------------------------------------------
# From-scratch consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_csv_analysis_matches_fresh_recomputation(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "data.csv"

    steps: tuple[tuple[str, str], ...] = (
        ("initial", "name,age\nAlice,30\n"),
        ("trailing newline", "name,age\nAlice,30\n\n"),
        ("change value", "name,age\nAlice,31\n"),
        ("add row", "name,age\nAlice,31\nBob,25\n"),
        ("add column", "name,age,city\nAlice,31,Portland\nBob,25,Seattle\n"),
        ("remove row", "name,age,city\nAlice,31,Portland\n"),
    )

    incremental = Database(mode=mode)
    for _label, content in steps:
        path.write_text(content, encoding="utf-8")
        fresh = Database(mode=mode)
        assert csv_analysis(incremental, str(path)) == csv_analysis(fresh, str(path))
