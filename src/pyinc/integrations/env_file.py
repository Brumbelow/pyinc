from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, cast

from pyinc.core import query
from pyinc.resources import DirectoryResource
from pyinc.runtime import Database
from pyinc.value import freeze, thaw

EnvEntryPayload: TypeAlias = tuple[str, str, bool, int]
DiagnosticPayload: TypeAlias = tuple[str, str]
EnvAnalysisPayload: TypeAlias = tuple[
    str,
    tuple[EnvEntryPayload, ...],
    tuple[DiagnosticPayload, ...],
]


@dataclass(frozen=True)
class EnvEntry:
    key: str
    value: str
    quoted: bool
    line_number: int


@dataclass(frozen=True)
class EnvFileAnalysis:
    path: str
    entries: tuple[EnvEntry, ...]
    diagnostics: tuple[tuple[str, str], ...]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _EnvFileResource:
    encoding: str = "utf-8"

    def read(self, db: Database, path: str | os.PathLike[str]) -> str:
        return cast(str, db._read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"envfile[{path}]"

    def probe(self, path: str) -> tuple[str, str] | tuple[str]:
        file_path = Path(path)
        if not file_path.exists():
            return ("missing",)
        return ("present", hashlib.sha256(file_path.read_bytes()).hexdigest())

    def load(self, db: Database, path: str) -> str:
        file_path = Path(path)
        if not file_path.exists():
            return ""
        with db._allow_raw_open():
            return file_path.read_text(encoding=self.encoding)


_FILES = _EnvFileResource()
_DIRECTORIES = DirectoryResource()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INTERPOLATION_PAT = r"\$\{[^}]+\}"
_EXPORT_PREFIX_PAT = r"^export\s+"
_LINE_PAT = r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)"


def _parse_env_lines(
    text: str,
) -> tuple[list[EnvEntryPayload], list[DiagnosticPayload]]:
    entries: list[EnvEntryPayload] = []
    diagnostics: list[DiagnosticPayload] = []

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        # Strip optional 'export ' prefix
        line = re.sub(_EXPORT_PREFIX_PAT, "", line)

        m = re.match(_LINE_PAT, line)
        if m is None:
            diagnostics.append(
                ("invalid-line", f"line {lineno}: could not parse as KEY=VALUE")
            )
            continue

        key = m.group(1)
        raw_value = m.group(2)

        quoted = False
        value: str

        if raw_value.startswith('"'):
            end = raw_value.find('"', 1)
            if end >= 0:
                quoted = True
                value = raw_value[1:end]
            else:
                value = raw_value
        elif raw_value.startswith("'"):
            end = raw_value.find("'", 1)
            if end >= 0:
                quoted = True
                value = raw_value[1:end]
            else:
                value = raw_value
        else:
            # Strip inline comment for unquoted values
            comment_idx = raw_value.find(" #")
            value = raw_value[:comment_idx].rstrip() if comment_idx >= 0 else raw_value

        # Flag interpolation references as diagnostics (conservative)
        if re.search(_INTERPOLATION_PAT, value):
            diagnostics.append(
                (
                    "interpolation-reference",
                    f"line {lineno}: value for {key} contains variable interpolation "
                    f"which is not expanded",
                )
            )

        entries.append((key, value, quoted, lineno))

    return entries, diagnostics


def _env_cutoff_token(text: str) -> tuple[str, str]:
    entries, _ = _parse_env_lines(text)
    snapshot = freeze(entries)
    return ("parsed", repr(snapshot))


# ---------------------------------------------------------------------------
# Layer 1 — Payload queries
# ---------------------------------------------------------------------------


@query(cutoff=_env_cutoff_token)
def env_file_text(db: Database, path: str) -> str:
    return _FILES.read(db, path)


@query
def env_entries_payload(db: Database, path: str) -> tuple[EnvEntryPayload, ...]:
    text = env_file_text(db, path)
    if not text:
        return ()
    entries, _ = _parse_env_lines(text)
    return tuple(entries)


@query
def env_diagnostics_payload(db: Database, path: str) -> tuple[DiagnosticPayload, ...]:
    text = env_file_text(db, path)
    if not text:
        return ()
    _, diagnostics = _parse_env_lines(text)
    return tuple(diagnostics)


# ---------------------------------------------------------------------------
# Layer 2 — Composition
# ---------------------------------------------------------------------------


@query
def env_analysis_payload(db: Database, path: str) -> EnvAnalysisPayload:
    entries = env_entries_payload(db, path)
    diagnostics = env_diagnostics_payload(db, path)
    return (path, entries, diagnostics)


# ---------------------------------------------------------------------------
# Layer 3 — Entrypoints
# ---------------------------------------------------------------------------


def env_analysis(db: Database, path: str | os.PathLike[str]) -> EnvFileAnalysis:
    normalized = os.fspath(path)
    payload = cast(EnvAnalysisPayload, thaw(db.get(env_analysis_payload, normalized)))
    path_str, entries, diagnostics = payload
    return EnvFileAnalysis(
        path=path_str,
        entries=tuple(
            EnvEntry(key=e[0], value=e[1], quoted=e[2], line_number=e[3])
            for e in entries
        ),
        diagnostics=diagnostics,
    )


def workspace_env_analysis(
    db: Database,
    root: str | os.PathLike[str],
    filename: str = ".env",
) -> EnvFileAnalysis | None:
    normalized_root = os.fspath(root)
    dir_entries = _DIRECTORIES.read(db, normalized_root)
    env_path = None
    for name in dir_entries:
        if name == filename:
            env_path = str(Path(normalized_root) / name)
            break
    if env_path is None:
        return None
    return env_analysis(db, env_path)


__all__ = [
    "EnvEntry",
    "EnvFileAnalysis",
    "env_analysis",
    "workspace_env_analysis",
]
