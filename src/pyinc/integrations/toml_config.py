from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, TypeAlias, cast

from pyinc.core import query
from pyinc.resources import DirectoryResource
from pyinc.runtime import Database
from pyinc.value import thaw

from ._decoding import _reject_in_query
from ._resources import file_probe, file_read_snapshot, file_text

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
        return cast(str, db.read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"configfile[{path}]"

    def probe(self, path: str) -> tuple[str, str] | tuple[str]:
        return file_probe(path)

    def load(self, db: Database, path: str) -> str:
        text = file_text(path, self.encoding)
        return text if text is not None else ""

    def probe_and_load(self, db: Database, path: str) -> tuple[tuple[str, str] | tuple[str], str]:
        probe, text = file_read_snapshot(path, self.encoding)
        return probe, text if text is not None else ""


_FILES = _ConfigFileResource()
_DIRECTORIES = DirectoryResource()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _TomlNestingLimitError(ValueError):
    pass


# Table and array nesting is capped because every section re-emits the dot path of
# all its ancestors: `config_sections_payload` grows with the square of the nesting
# depth, so this cap is what bounds the *cache*, not just the parse. `json_config`
# caps `_MAX_JSON_DEPTH` and `xml_config` caps `_MAX_XML_DEPTH` for the same reason
# and against the same budget — a document at the cap must not cache more than
# ~1 MiB.
#
# That budget is the only thing that binds here. Depth counts the document's
# implicit top-level table as level 1, the way `json_config` counts the outermost
# `{`, so `[a.b]` is three levels. What gets cached is a flat tuple of
# `(name, keys, subsections)` triples whose own nesting does not grow with the
# document's, so nesting costs cache size and nothing else. Measured with
# 20-character table names, a document at this cap caches 823 KiB of section
# payload text — inside the ceiling, and an order of magnitude deeper than any real
# configuration document.
_MAX_TOML_DEPTH = 200


def _structure_depth(value: object) -> int:
    """Report the deepest table/array nesting in a parsed document without recursing.

    The document's own top-level table is level 1, so a flat file is 1 and `[a.b]`
    is 3. The traversal keeps its own stack, so the answer depends only on the
    parsed document — never on how much of the interpreter's recursion budget the
    caller has already spent.
    """
    deepest = 0
    pending: list[tuple[object, int]] = [(value, 1)]

    while pending:
        current, depth = pending.pop()
        children: list[object]
        if isinstance(current, dict):
            children = list(current.values())
        elif isinstance(current, list):
            children = list(current)
        else:
            continue
        if depth > deepest:
            deepest = depth
        pending.extend((child, depth + 1) for child in children)

    return deepest


def _load_toml(text: str) -> dict[str, Any]:
    """Parse `text`, rejecting nesting past `_MAX_TOML_DEPTH` as a decode error.

    The depth is measured on the parsed document rather than on the file text.
    TOML spreads nesting across table headers, dotted keys, inline tables, and
    arrays, and brackets and braces also appear inside comments and in four kinds
    of string literal, so counting depth from the text would mean re-lexing the
    grammar — and any disagreement with `tomllib` would reject documents this
    integration accepts today. `tomllib` builds header-nested and dotted-key tables
    iteratively, so those reach the check however deep they go (measured to 1500
    levels under the default recursion limit); inline tables and arrays recurse, so
    a document nested hundreds of levels deep in *those* can exhaust the parser
    before the cap is reported (measured, again under the default limit and from a
    fresh thread: 330 inline-table levels and 495 array levels still parse). Both
    outcomes are fixed strings under `toml-decode-error`; the residual is disclosed
    in `docs/integration-contract.md`.
    """
    parsed = tomllib.loads(text)
    if _structure_depth(parsed) > _MAX_TOML_DEPTH:
        raise _TomlNestingLimitError(
            f"TOML nesting exceeds the supported limit of {_MAX_TOML_DEPTH} levels"
        )
    return parsed


# `tomllib` recurses once per inline-table and once per array level, so which frame
# runs out of the interpreter's recursion budget — and so which message CPython
# raises — depends on how much stack the caller had already spent, not on the file.
# CPython names whichever frame ran out, so the same document can report a different
# message from different call depths. These payloads are cached, so a fixed string
# is emitted instead and the message stops recording which frame ran out.
#
# That closes the message axis, not the outcome axis: `tomllib` still descends once
# per inline-table and array level, so whether a document within the cap parses at
# all remains a property of the call site as well as of the file, and a caller
# entering with its stack nearly spent turns a valid document into this diagnostic.
# `json_config` and `xml_config` emit the same shape for the same reason and carry
# the same residual.
_STACK_EXHAUSTED_DIAGNOSTIC = "TOML parsing exhausted the interpreter stack"


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
    return repr(value)


def _walk_sections(
    data: dict[str, Any],
    prefix: str,
) -> list[ConfigSectionPayload]:
    """Collect every table in sorted pre-order, deepest nesting included.

    Keys are visited in sorted order at every level rather than in the order the
    document wrote them, so a file that writes `[b]` before `[a]` still yields
    `<root>`, `a`, `b`.

    The traversal keeps its own stack rather than recursing, so the payload a
    document produces depends only on the document — never on how much of the
    interpreter's recursion budget the caller has already spent.
    """
    sections: list[ConfigSectionPayload] = []
    pending: list[tuple[dict[str, Any], str]] = [(data, prefix)]

    while pending:
        current, current_prefix = pending.pop()
        section_name = current_prefix or "<root>"
        keys: list[ConfigKeyPayload] = []
        subsections: list[str] = []
        children: list[tuple[dict[str, Any], str]] = []

        for key, value in sorted(current.items()):
            if isinstance(value, dict):
                child_prefix = f"{current_prefix}.{key}" if current_prefix else key
                subsections.append(child_prefix)
                children.append((value, child_prefix))
            else:
                keys.append(
                    (
                        section_name,
                        key,
                        _toml_value_type(value),
                        _toml_value_to_string(value),
                    )
                )

        sections.append((section_name, tuple(keys), tuple(subsections)))
        # Reversed so the first subsection is popped first, preserving sorted order.
        pending.extend(reversed(children))

    return sections


def _try_parse_toml(text: str) -> dict[str, Any] | None:
    try:
        return _load_toml(text)
    except (ValueError, RecursionError, OverflowError):
        return None


def _config_shape_diagnostics(parsed: dict[str, Any]) -> tuple[DiagnosticPayload, ...]:
    diagnostics: list[DiagnosticPayload] = []
    project = parsed.get("project")
    if project is not None and not isinstance(project, dict):
        diagnostics.append(("invalid-project", "project must be a TOML table"))
    elif isinstance(project, dict):
        dependencies = project.get("dependencies")
        if dependencies is not None and (
            not isinstance(dependencies, list)
            or any(not isinstance(item, str) for item in dependencies)
        ):
            diagnostics.append(
                (
                    "invalid-project-dependencies",
                    "project.dependencies must be an array of strings",
                )
            )
        optional = project.get("optional-dependencies")
        if optional is not None and not isinstance(optional, dict):
            diagnostics.append(
                (
                    "invalid-optional-dependencies",
                    "project.optional-dependencies must be a TOML table",
                )
            )
        elif isinstance(optional, dict):
            for group, items in sorted(optional.items()):
                if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
                    diagnostics.append(
                        (
                            "invalid-optional-dependency-group",
                            f"project.optional-dependencies.{group} must be an array of strings",
                        )
                    )
    tool = parsed.get("tool")
    if tool is not None and not isinstance(tool, dict):
        diagnostics.append(("invalid-tool", "tool must be a TOML table"))
    return tuple(diagnostics)


# ---------------------------------------------------------------------------
# Layer 1 — Payload queries
# ---------------------------------------------------------------------------


@query
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

    project_value = parsed.get("project", {})
    project = project_value if isinstance(project_value, dict) else {}
    dependencies_value = project.get("dependencies", [])
    deps = (
        tuple(dependencies_value)
        if isinstance(dependencies_value, list)
        and all(isinstance(item, str) for item in dependencies_value)
        else ()
    )

    optional_value = project.get("optional-dependencies", {})
    optional_deps = optional_value if isinstance(optional_value, dict) else {}
    optional_groups = tuple(
        (group, tuple(items))
        for group, items in sorted(optional_deps.items())
        if isinstance(items, list) and all(isinstance(item, str) for item in items)
    )
    return (deps, optional_groups)


@query
def config_tool_configs_payload(db: Database, path: str) -> tuple[str, ...]:
    text = config_file_text(db, path)
    parsed = _try_parse_toml(text)
    if parsed is None:
        return ()
    tool_value = parsed.get("tool", {})
    tool = tool_value if isinstance(tool_value, dict) else {}
    return tuple(sorted(tool.keys()))


@query
def config_diagnostics_payload(db: Database, path: str) -> tuple[DiagnosticPayload, ...]:
    text = config_file_text(db, path)
    if not text:
        return ()
    try:
        parsed = _load_toml(text)
    except (ValueError, OverflowError) as exc:
        return (("toml-decode-error", str(exc)),)
    except RecursionError:
        return (("toml-decode-error", _STACK_EXHAUSTED_DIAGNOSTIC),)
    return _config_shape_diagnostics(parsed)


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
        keys=tuple(
            ConfigKey(section=k[0], key=k[1], value_type=k[2], string_value=k[3]) for k in keys
        ),
        subsections=subsections,
    )


def config_analysis(db: Database, path: str | os.PathLike[str]) -> ConfigAnalysis:
    _reject_in_query(db, "config_analysis")
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
    _reject_in_query(db, "workspace_config_analysis")
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
