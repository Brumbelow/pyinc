from __future__ import annotations

from pathlib import Path

import pytest

import pyinc.integrations as integrations
from pyinc import Database, FileSystemArtifactStore
from pyinc.integrations.csv_data import (
    CsvAnalysis,
    csv_analysis,
    csv_file_text,
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

# The same table twice: unquoted, then with every field quoted and a trailing
# blank line added.
_UNFORMATTED_CSV = "name,age\nAlice,30\nBob,25\n"
_REFORMATTED_CSV = '"name","age"\n"Alice","30"\n"Bob","25"\n\n'

# Written as bytes so the carriage returns reach the parser untranslated: a
# single quoted column with CRLF line endings, and a file whose only line breaks
# are bare carriage returns. Sniffing either one guesses a line terminator for
# the delimiter.
_CRLF_CSV_BYTES = b'"a"\r\n"b"\r\n"c"\r\n'
_LONE_CR_CSV_BYTES = b"a\rb\rc\r"

_REFUSED_CR = ("csv-dialect-error", "sniffed delimiter '\\r' is a line terminator; read as ','")
_UNREADABLE_AS_COMMA = ("csv-dialect-error", "text is not readable as ','-delimited")


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
# Reformatting costs nothing above the payloads
# ---------------------------------------------------------------------------

# The three queries that re-read the text and re-derive a projection of it.
_PAYLOAD_QUERIES = ("csv_columns_payload", "csv_meta_payload", "csv_diagnostics_payload")


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_reformat_recomputes_the_payloads_and_backdates_the_composition(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "data.csv"
    path.write_text(_UNFORMATTED_CSV, encoding="utf-8")

    db = Database(mode=mode)
    first = csv_analysis(db, str(path))

    db.reset_statistics()
    path.write_text(_REFORMATTED_CSV, encoding="utf-8")
    second = csv_analysis(db, str(path))

    assert first == second, f"a reformat moved the analysis | first {first} | second {second}"

    # `query_profile()` records executions only, and `reset_statistics()` has
    # just cleared it, so a query that was reused has no row at all -- there is
    # no row carrying a zero to look for. Labels also carry an argument-hash
    # suffix, so a lookup by bare query name never matches; match by substring.
    executed = [profile.query_label for profile in db.query_profile()]
    for name in _PAYLOAD_QUERIES:
        assert any(name in label for label in executed), (
            f"{name} did not re-run | executed {executed}"
        )
    assert not [label for label in executed if "csv_analysis_payload" in label], (
        f"csv_analysis_payload re-ran instead of backdating | executed {executed}"
    )

    # Absolute counts for the second read, not deltas: the read executes on the
    # new bytes, the three payload queries re-derive equal projections and are
    # backdated, and everything above them is reused. The reuse figure is what
    # the reformat is supposed to cost nothing on.
    statistics = db.statistics()
    counts = (
        statistics.query_executions,
        statistics.query_backdates,
        statistics.query_reuses,
    )
    assert counts == (1, 3, 6), f"work counts moved | exec/backdate/reuse {counts}"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_reformat_leaves_workspace_discovery_identical(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text(_UNFORMATTED_CSV, encoding="utf-8")

    db = Database(mode=mode)
    first = workspace_csv_analysis(db, str(tmp_path))

    path.write_text(_REFORMATTED_CSV, encoding="utf-8")
    second = workspace_csv_analysis(db, str(tmp_path))

    assert first is not None, "discovery found no file to analyse"
    assert first == second, f"a reformat moved discovery | first {first} | second {second}"


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------

# The ordering other integrations use -- edit, drive the entrypoint so a stale
# answer forms, then save -- is unconstructible here: this read compares the text
# it hands back, so there is no answer that disagrees with the file to save. The
# substitute edits the file after the save, which the reload has to notice. The
# second arm is what keeps that honest: with no edit the saved answer and a fresh
# one agree, and the row would pass on any tree at all.


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_checkpoint_reload_answers_an_edit_made_after_the_save(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "data.csv"
    path.write_text(_UNFORMATTED_CSV, encoding="utf-8")

    store_dir = tmp_path / "store"
    saver = Database(mode=mode, store=FileSystemArtifactStore(store_dir))
    warm = csv_analysis(saver, str(path))
    key = saver.save_checkpoint()

    path.write_text("name,age\nAlice,31\nBob,25\nCarol,41\n", encoding="utf-8")

    reloaded = Database(mode=mode, store=FileSystemArtifactStore(store_dir))
    reloaded.load_checkpoint(key)

    restored = csv_analysis(reloaded, str(path))
    fresh = csv_analysis(Database(mode=mode), str(path))

    assert restored == fresh, f"reloaded != fresh | reloaded {restored} | fresh {fresh}"
    assert restored != warm, f"the edit never reached the reload | warm {warm}"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_checkpoint_reload_over_an_unchanged_file_runs_nothing(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "data.csv"
    path.write_text(_UNFORMATTED_CSV, encoding="utf-8")

    store_dir = tmp_path / "store"
    saver = Database(mode=mode, store=FileSystemArtifactStore(store_dir))
    warm = csv_analysis(saver, str(path))
    key = saver.save_checkpoint()

    reloaded = Database(mode=mode, store=FileSystemArtifactStore(store_dir))
    reloaded.load_checkpoint(key)
    restored = csv_analysis(reloaded, str(path))

    assert restored == warm, f"the saved answer did not survive | warm {warm}"

    statistics = reloaded.statistics()
    counts = (
        statistics.query_executions,
        statistics.query_reuses,
        statistics.resource_loads,
        statistics.resource_probe_hits,
    )
    assert counts == (0, 1, 0, 1), f"the reload did work | exec/reuse/loads/probes {counts}"


# ---------------------------------------------------------------------------
# Dialects no reader can use
# ---------------------------------------------------------------------------


def _shape_of(analysis: CsvAnalysis) -> tuple[object, ...]:
    return (
        tuple((column.name, column.index) for column in analysis.columns),
        analysis.row_count,
        analysis.delimiter,
        analysis.has_header,
        analysis.diagnostics,
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_crlf_file_is_read_as_comma_delimited(mode: str, tmp_path: Path) -> None:
    # The whole answer, not just the delimiter: refusing the sniffed value is
    # what makes this shape independent of which version read the file, so the
    # row pins all of it rather than only the field that used to move.
    path = tmp_path / "data.csv"
    path.write_bytes(_CRLF_CSV_BYTES)

    result = csv_analysis(Database(mode=mode), str(path))

    assert _shape_of(result) == ((("column_0", 0),), 3, ",", False, (_REFUSED_CR,)), (
        f"CRLF file | {_shape_of(result)}"
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_carriage_return_only_file_reports_what_it_could_not_read(
    mode: str, tmp_path: Path
) -> None:
    # Two diagnostics and an empty table, where the CRLF file gets one and three
    # rows. The first records the refused delimiter; the second records that the
    # comma fallback did not read this text either, which is where the reader
    # gives up rather than where anything is refused.
    path = tmp_path / "data.csv"
    path.write_bytes(_LONE_CR_CSV_BYTES)

    result = csv_analysis(Database(mode=mode), str(path))

    assert _shape_of(result) == ((), 0, ",", False, (_REFUSED_CR, _UNREADABLE_AS_COMMA)), (
        f"carriage-return-only file | {_shape_of(result)}"
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_the_read_returns_the_bytes_and_the_payloads_choose_the_dialect(
    mode: str, tmp_path: Path
) -> None:
    # Warm first, then edit. A cold read has no earlier answer to compare
    # against, so it cannot show which layer the dialect decision belongs to;
    # the second read is where a comparison happens and where the whole question
    # of what this query is allowed to know about the text arises.
    path = tmp_path / "data.csv"
    path.write_text(_UNFORMATTED_CSV, encoding="utf-8")

    db = Database(mode=mode)
    db.get(csv_file_text, str(path))

    path.write_bytes(_CRLF_CSV_BYTES)
    text = db.get(csv_file_text, str(path))

    assert text == '"a"\r\n"b"\r\n"c"\r\n', f"the read did not hand back the file | {text!r}"

    analysis = csv_analysis(db, str(path))
    assert (analysis.delimiter, analysis.diagnostics) == (",", (_REFUSED_CR,)), (
        f"the dialect decision did not land in the payloads | {_shape_of(analysis)}"
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_an_ordinary_comma_file_gains_no_dialect_diagnostic(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text(_MINIMAL_CSV, encoding="utf-8")

    result = csv_analysis(Database(mode=mode), str(path))

    assert _shape_of(result) == (
        (("name", 0), ("age", 1), ("city", 2)),
        3,
        ",",
        True,
        (),
    ), f"an ordinary file moved | {_shape_of(result)}"


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
