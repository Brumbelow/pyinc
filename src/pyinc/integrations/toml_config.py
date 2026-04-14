from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, TypeAlias, cast

from pyinc.core import query
from pyinc.resources import DirectoryResource
from pyinc.runtime import Database
from pyinc.value import freeze, thaw

ConfigKeyPayload: TypeAlias = tuple[str, str, str, str]
ConfigSectionPayload: TypeAlias = tuple[str, tuple[ConfigKeyPayload, ...], tuple[str, ...]]
DiagnosticPayload: TypeAlias = tuple[str, str]
ConfigAnalysisPayload: TypeAlias = tuple[
    str,
    tuple[ConfigSectionPayload, ...],
    tuple[str, ...],
    tuple[tuple[str, tuple[str, ...]], ...],
    tuple[str, ...],
    tuple[DiagnosticPayload, ...],
]


@dataclass(frozen=True)
class ConfigKey:
    section: str
    key: str
    value_type: str
    string_value: str


@dataclass(frozen=True)
class ConfigSection:
    name: str
    keys: tuple[ConfigKey, ...]
    subsections: tuple[str, ...]


@dataclass(frozen=True)
class ConfigAnalysis:
    path: str
    sections: tuple[ConfigSection, ...]
    dependencies: tuple[str, ...]
    optional_dependency_groups: tuple[tuple[str, tuple[str, ...]], ...]
    tool_configs: tuple[str, ...]
    diagnostics: tuple[tuple[str, str], ...]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ConfigFileResource:
    encoding: str = "utf-8"

    def read(self, db: Database, path: str | os.PathLike[str]) -> str:
        return cast(str, db._read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"configfile[{path}]"

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


_FILES = _ConfigFileResource()
_DIRECTORIES = DirectoryResource()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _toml_value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "table"
    if isinstance(value, datetime | date | time):
        return "datetime"
    return "unknown"


def _toml_value_to_string(value: Any) -> str:
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, dict):
        return repr(sorted(value.items()))
    return repr(value)


def _walk_sections(
    data: dict[str, Any],
    prefix: str,
) -> list[ConfigSectionPayload]:
    sections: list[ConfigSectionPayload] = []
    keys: list[ConfigKeyPayload] = []
    subsections: list[str] = []
    section_name = prefix or "<root>"

    for key, value in sorted(data.items()):
        if isinstance(value, dict):
            child_prefix = f"{prefix}.{key}" if prefix else key
            subsections.append(child_prefix)
            sections.extend(_walk_sections(value, child_prefix))
        else:
            keys.append((section_name, key, _toml_value_type(value), _toml_value_to_string(value)))

    sections.insert(0, (section_name, tuple(keys), tuple(subsections)))
    return sections


def _config_cutoff_token(text: str) -> tuple[str, str]:
    try:
        parsed = tomllib.loads(text)
        snapshot = freeze(parsed)
        return ("parsed", repr(snapshot))
    except tomllib.TOMLDecodeError:
        return ("raw", text)


def _try_parse_toml(text: str) -> dict[str, Any] | None:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


# ---------------------------------------------------------------------------
# Layer 1 — Payload queries
# ---------------------------------------------------------------------------


@query(cutoff=_config_cutoff_token)
def config_file_text(db: Database, path: str) -> str:
    return _FILES.read(db, path)


@query
def config_sections_payload(db: Database, path: str) -> tuple[ConfigSectionPayload, ...]:
    text = config_file_text(db, path)
    parsed = _try_parse_toml(text)
    if parsed is None:
        return ()
    return tuple(_walk_sections(parsed, ""))


@query
def config_dependencies_payload(
    db: Database, path: str
) -> tuple[tuple[str, ...], tuple[tuple[str, tuple[str, ...]], ...]]:
    text = config_file_text(db, path)
    parsed = _try_parse_toml(text)
    if parsed is None:
        return ((), ())

    project = parsed.get("project", {})
    deps = tuple(str(d) for d in project.get("dependencies", []))

    optional_deps = project.get("optional-dependencies", {})
    optional_groups = tuple(
        (group, tuple(str(d) for d in items))
        for group, items in sorted(optional_deps.items())
    )
    return (deps, optional_groups)


@query
def config_tool_configs_payload(db: Database, path: str) -> tuple[str, ...]:
    text = config_file_text(db, path)
    parsed = _try_parse_toml(text)
    if parsed is None:
        return ()
    tool = parsed.get("tool", {})
    return tuple(sorted(tool.keys()))


@query
def config_diagnostics_payload(db: Database, path: str) -> tuple[DiagnosticPayload, ...]:
    text = config_file_text(db, path)
    if not text:
        return ()
    try:
        tomllib.loads(text)
        return ()
    except tomllib.TOMLDecodeError as exc:
        return (("toml-decode-error", str(exc)),)


# ---------------------------------------------------------------------------
# Layer 2 — Composition
# ---------------------------------------------------------------------------


@query
def config_analysis_payload(db: Database, path: str) -> ConfigAnalysisPayload:
    sections = config_sections_payload(db, path)
    deps, optional_deps = config_dependencies_payload(db, path)
    tools = config_tool_configs_payload(db, path)
    diagnostics = config_diagnostics_payload(db, path)
    return (path, sections, deps, optional_deps, tools, diagnostics)


# ---------------------------------------------------------------------------
# Layer 3 — Entrypoints
# ---------------------------------------------------------------------------


def _decode_section(payload: ConfigSectionPayload) -> ConfigSection:
    name, keys, subsections = payload
    return ConfigSection(
        name=name,
        keys=tuple(ConfigKey(section=k[0], key=k[1], value_type=k[2], string_value=k[3]) for k in keys),
        subsections=subsections,
    )


def config_analysis(db: Database, path: str | os.PathLike[str]) -> ConfigAnalysis:
    normalized = os.fspath(path)
    payload = cast(ConfigAnalysisPayload, thaw(db.get(config_analysis_payload, normalized)))
    path_str, sections, deps, optional_deps, tools, diagnostics = payload
    return ConfigAnalysis(
        path=path_str,
        sections=tuple(_decode_section(s) for s in sections),
        dependencies=deps,
        optional_dependency_groups=optional_deps,
        tool_configs=tools,
        diagnostics=diagnostics,
    )


def workspace_config_analysis(db: Database, root: str | os.PathLike[str]) -> ConfigAnalysis | None:
    normalized_root = os.fspath(root)
    entries = _DIRECTORIES.read(db, normalized_root)
    config_path = None
    for name in entries:
        if name == "pyproject.toml":
            config_path = str(Path(normalized_root) / name)
            break
    if config_path is None:
        return None
    return config_analysis(db, config_path)


__all__ = [
    "ConfigAnalysis",
    "ConfigKey",
    "ConfigSection",
    "config_analysis",
    "workspace_config_analysis",
]
