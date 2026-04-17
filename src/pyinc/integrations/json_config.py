from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias, cast

from pyinc.core import query
from pyinc.resources import DirectoryResource, _file_read_snapshot
from pyinc.runtime import Database
from pyinc.value import freeze, thaw

JsonKeyPayload: TypeAlias = tuple[str, str, str, str]
JsonSectionPayload: TypeAlias = tuple[str, tuple[JsonKeyPayload, ...], tuple[str, ...]]
DiagnosticPayload: TypeAlias = tuple[str, str]
JsonAnalysisPayload: TypeAlias = tuple[
    str,
    tuple[JsonSectionPayload, ...],
    tuple[DiagnosticPayload, ...],
]


@dataclass(frozen=True)
class JsonKey:
    section: str
    key: str
    value_type: str
    string_value: str


@dataclass(frozen=True)
class JsonSection:
    name: str
    keys: tuple[JsonKey, ...]
    subsections: tuple[str, ...]


@dataclass(frozen=True)
class JsonAnalysis:
    path: str
    sections: tuple[JsonSection, ...]
    diagnostics: tuple[tuple[str, str], ...]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _JsonFileResource:
    encoding: str = "utf-8"

    def read(self, db: Database, path: str | os.PathLike[str]) -> str:
        return cast(str, db._read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"jsonfile[{path}]"

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

    def probe_and_load(
        self, db: Database, path: str
    ) -> tuple[tuple[str, str] | tuple[str], str]:
        probe, text = _file_read_snapshot(path, self.encoding)
        return probe, text if text is not None else ""


_FILES = _JsonFileResource()
_DIRECTORIES = DirectoryResource()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _json_value_to_string(value: Any) -> str:
    if isinstance(value, dict):
        return repr(sorted(value.items()))
    return repr(value)


def _walk_sections(
    data: dict[str, Any],
    prefix: str,
) -> list[JsonSectionPayload]:
    sections: list[JsonSectionPayload] = []
    keys: list[JsonKeyPayload] = []
    subsections: list[str] = []
    section_name = prefix or "<root>"

    for key, value in sorted(data.items()):
        if isinstance(value, dict):
            child_prefix = f"{prefix}.{key}" if prefix else key
            subsections.append(child_prefix)
            sections.extend(_walk_sections(value, child_prefix))
        else:
            keys.append((section_name, key, _json_value_type(value), _json_value_to_string(value)))

    sections.insert(0, (section_name, tuple(keys), tuple(subsections)))
    return sections


def _json_cutoff_token(text: str) -> tuple[str, str]:
    try:
        parsed = json.loads(text)
        snapshot = freeze(parsed)
        return ("parsed", repr(snapshot))
    except json.JSONDecodeError:
        return ("raw", text)


def _try_parse_json(text: str) -> dict[str, Any] | None:
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
        return None
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Layer 1 — Payload queries
# ---------------------------------------------------------------------------


@query(cutoff=_json_cutoff_token)
def json_file_text(db: Database, path: str) -> str:
    return _FILES.read(db, path)


@query
def json_sections_payload(db: Database, path: str) -> tuple[JsonSectionPayload, ...]:
    text = json_file_text(db, path)
    parsed = _try_parse_json(text)
    if parsed is None:
        return ()
    return tuple(_walk_sections(parsed, ""))


@query
def json_diagnostics_payload(db: Database, path: str) -> tuple[DiagnosticPayload, ...]:
    text = json_file_text(db, path)
    if not text:
        return ()
    try:
        json.loads(text)
        return ()
    except json.JSONDecodeError as exc:
        return (("json-decode-error", str(exc)),)


# ---------------------------------------------------------------------------
# Layer 2 — Composition
# ---------------------------------------------------------------------------


@query
def json_analysis_payload(db: Database, path: str) -> JsonAnalysisPayload:
    sections = json_sections_payload(db, path)
    diagnostics = json_diagnostics_payload(db, path)
    return (path, sections, diagnostics)


# ---------------------------------------------------------------------------
# Layer 3 — Entrypoints
# ---------------------------------------------------------------------------


def _decode_section(payload: JsonSectionPayload) -> JsonSection:
    name, keys, subsections = payload
    return JsonSection(
        name=name,
        keys=tuple(JsonKey(section=k[0], key=k[1], value_type=k[2], string_value=k[3]) for k in keys),
        subsections=subsections,
    )


def json_analysis(db: Database, path: str | os.PathLike[str]) -> JsonAnalysis:
    normalized = os.fspath(path)
    payload = cast(JsonAnalysisPayload, thaw(db.get(json_analysis_payload, normalized)))
    path_str, sections, diagnostics = payload
    return JsonAnalysis(
        path=path_str,
        sections=tuple(_decode_section(s) for s in sections),
        diagnostics=diagnostics,
    )


def workspace_json_analysis(
    db: Database,
    root: str | os.PathLike[str],
    filename: str = "package.json",
) -> JsonAnalysis | None:
    normalized_root = os.fspath(root)
    entries = _DIRECTORIES.read(db, normalized_root)
    json_path = None
    for name in entries:
        if name == filename:
            json_path = str(Path(normalized_root) / name)
            break
    if json_path is None:
        return None
    return json_analysis(db, json_path)


__all__ = [
    "JsonAnalysis",
    "JsonKey",
    "JsonSection",
    "json_analysis",
    "workspace_json_analysis",
]
