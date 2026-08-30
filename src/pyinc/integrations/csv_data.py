from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, cast

from pyinc.core import query
from pyinc.resources import DirectoryResource
from pyinc.runtime import Database
from pyinc.value import thaw

from ._decoding import _reject_in_query
from ._resources import file_probe, file_read_snapshot, file_text

CsvColumnPayload: TypeAlias = tuple[str, int]
DiagnosticPayload: TypeAlias = tuple[str, str]
CsvMetaPayload: TypeAlias = tuple[int, str, bool]
CsvAnalysisPayload: TypeAlias = tuple[
    str,  # path
    tuple[CsvColumnPayload, ...],  # columns
    int,  # row_count
    str,  # delimiter
    bool,  # has_header
    tuple[DiagnosticPayload, ...],  # diagnostics
]


@dataclass(frozen=True)
class CsvColumn:
    name: str
    index: int


@dataclass(frozen=True)
class CsvAnalysis:
    path: str
    columns: tuple[CsvColumn, ...]
    row_count: int
    delimiter: str
    has_header: bool
    diagnostics: tuple[tuple[str, str], ...]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CsvFileResource:
    encoding: str = "utf-8"

    def read(self, db: Database, path: str | os.PathLike[str]) -> str:
        return cast(str, db.read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"csvfile[{path}]"

    def probe(self, path: str) -> tuple[str, str] | tuple[str]:
        return file_probe(path)

    def load(self, db: Database, path: str) -> str:
        text = file_text(path, self.encoding)
        return text if text is not None else ""

    def probe_and_load(self, db: Database, path: str) -> tuple[tuple[str, str] | tuple[str], str]:
        probe, text = file_read_snapshot(path, self.encoding)
        return probe, text if text is not None else ""


_FILES = _CsvFileResource()
_DIRECTORIES = DirectoryResource()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detect_delimiter(text: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(text[:8192])
        return dialect.delimiter
    except (csv.Error, ValueError):
        return ","


def _detect_has_header(text: str) -> bool:
    try:
        return csv.Sniffer().has_header(text[:8192])
    except (csv.Error, ValueError):
        return False


def _parse_csv(
    text: str,
) -> tuple[list[CsvColumnPayload], int, str, bool, list[DiagnosticPayload]]:
    stripped = text.strip()
    if not stripped:
        return [], 0, ",", False, []

    # The text arrives with whatever line endings the file holds, so translate
    # them before anything looks at it. Spelled as two replacements rather than
    # splitlines(), which also breaks on the vertical tab, form feed and the
    # other separators a field is allowed to contain.
    stripped = stripped.replace("\r\n", "\n").replace("\r", "\n")

    delimiter = _detect_delimiter(stripped)
    has_header = _detect_has_header(stripped)
    diagnostics: list[DiagnosticPayload] = []

    # Two guesses no reader can be relied on to use the same way: a line
    # terminator, and the quote character itself. Readers disagree about both --
    # some refuse them outright, some accept them and split fields somewhere the
    # file never meant. The translation above settles what the sniffer sees;
    # refusing these two settles what is done with the answer, so neither the
    # file's line endings nor a guess of the quote character can decide the
    # dialect. The quote character is read from the dialect the reader below
    # falls back on, which is the module's fixed default for a call that names
    # only a delimiter. The terminator arm is belt and braces: no sniffer path
    # measured here returns one once the text above has been translated, and it
    # is kept for the reader that does rather than removed for the ones that
    # do not.
    if delimiter in ("\r", "\n"):
        diagnostics.append(
            (
                "csv-dialect-error",
                f"sniffed delimiter {delimiter!r} is a line terminator; read as ','",
            )
        )
        delimiter = ","
    elif delimiter == csv.excel.quotechar:
        diagnostics.append(
            (
                "csv-dialect-error",
                f"sniffed delimiter {delimiter!r} is the quote character; read as ','",
            )
        )
        delimiter = ","

    try:
        rows = list(csv.reader(io.StringIO(stripped), delimiter=delimiter))
    except (csv.Error, ValueError):
        diagnostics.append(
            ("csv-dialect-error", f"text is not readable as {delimiter!r}-delimited")
        )
        return [], 0, delimiter, has_header, diagnostics

    if not rows:
        return [], 0, delimiter, has_header, diagnostics

    if has_header:
        header_row = rows[0]
        data_rows = rows[1:]
        columns: list[CsvColumnPayload] = [(name, idx) for idx, name in enumerate(header_row)]
    else:
        data_rows = rows
        col_count = len(rows[0]) if rows else 0
        columns = [(f"column_{idx}", idx) for idx in range(col_count)]

    expected_cols = len(columns)
    for row_idx, row in enumerate(data_rows):
        if len(row) != expected_cols:
            diagnostics.append(
                (
                    "inconsistent-columns",
                    f"row {row_idx + 1 + (1 if has_header else 0)}: "
                    f"expected {expected_cols} columns, got {len(row)}",
                )
            )

    row_count = len(data_rows)
    return columns, row_count, delimiter, has_header, diagnostics


# ---------------------------------------------------------------------------
# Layer 1 — Payload queries
# ---------------------------------------------------------------------------


@query
def csv_file_text(db: Database, path: str) -> str:
    return _FILES.read(db, path)


@query
def csv_columns_payload(db: Database, path: str) -> tuple[CsvColumnPayload, ...]:
    text = csv_file_text(db, path)
    if not text.strip():
        return ()
    columns, _, _, _, _ = _parse_csv(text)
    return tuple(columns)


@query
def csv_meta_payload(db: Database, path: str) -> CsvMetaPayload:
    text = csv_file_text(db, path)
    if not text.strip():
        return (0, ",", False)
    _, row_count, delimiter, has_header, _ = _parse_csv(text)
    return (row_count, delimiter, has_header)


@query
def csv_diagnostics_payload(db: Database, path: str) -> tuple[DiagnosticPayload, ...]:
    text = csv_file_text(db, path)
    if not text.strip():
        return ()
    _, _, _, _, diagnostics = _parse_csv(text)
    return tuple(diagnostics)


# ---------------------------------------------------------------------------
# Layer 2 — Composition
# ---------------------------------------------------------------------------


@query
def csv_analysis_payload(db: Database, path: str) -> CsvAnalysisPayload:
    columns = csv_columns_payload(db, path)
    meta = csv_meta_payload(db, path)
    diagnostics = csv_diagnostics_payload(db, path)
    row_count, delimiter, has_header = meta
    return (path, columns, row_count, delimiter, has_header, diagnostics)


# ---------------------------------------------------------------------------
# Layer 3 — Entrypoints
# ---------------------------------------------------------------------------


def csv_analysis(db: Database, path: str | os.PathLike[str]) -> CsvAnalysis:
    _reject_in_query(db, "csv_analysis")
    normalized = os.fspath(path)
    payload = cast(CsvAnalysisPayload, thaw(db.get(csv_analysis_payload, normalized)))
    path_str, columns, row_count, delimiter, has_header, diagnostics = payload
    return CsvAnalysis(
        path=path_str,
        columns=tuple(CsvColumn(name=c[0], index=c[1]) for c in columns),
        row_count=row_count,
        delimiter=delimiter,
        has_header=has_header,
        diagnostics=diagnostics,
    )


def workspace_csv_analysis(
    db: Database,
    root: str | os.PathLike[str],
    filename: str = "data.csv",
) -> CsvAnalysis | None:
    _reject_in_query(db, "workspace_csv_analysis")
    normalized_root = os.fspath(root)
    dir_entries = _DIRECTORIES.read(db, normalized_root)
    csv_path = None
    for name in dir_entries:
        if name == filename:
            csv_path = str(Path(normalized_root) / name)
            break
    if csv_path is None:
        return None
    return csv_analysis(db, csv_path)


__all__ = [
    "CsvColumn",
    "CsvAnalysis",
    "csv_analysis",
    "workspace_csv_analysis",
]
