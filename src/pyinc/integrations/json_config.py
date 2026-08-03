from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias, cast

from pyinc.core import query
from pyinc.errors import UnsupportedValueError
from pyinc.resources import DirectoryResource
from pyinc.runtime import Database
from pyinc.value import freeze, thaw

from ._resources import file_read_snapshot

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
        return cast(str, db.read_resource(self, os.fspath(path)))

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
        return file_path.read_text(encoding=self.encoding)

    def probe_and_load(self, db: Database, path: str) -> tuple[tuple[str, str] | tuple[str], str]:
        probe, text = file_read_snapshot(path, self.encoding)
        return probe, text if text is not None else ""


_FILES = _JsonFileResource()
_DIRECTORIES = DirectoryResource()


class _DuplicateJsonKeyError(ValueError):
    pass


class _JsonNestingLimitError(ValueError):
    pass


class _JsonSurrogateKeyError(ValueError):
    pass


# Object and array nesting is capped before parsing because every section re-emits
# the dot path of all its ancestors: `json_sections_payload` grows with the square
# of the nesting depth, so this cap is what bounds the *cache*, not just the parse.
# `xml_config` caps `_MAX_XML_DEPTH` for the same reason and against the same
# budget — a document at the cap must not cache more than ~1 MiB.
#
# Two constraints meet at 200. The budget: measured with 20-character keys, a
# document at this cap caches 832 KB of section payload text (422 KB of it section
# names); at 256 levels, the cap `xml_config` carries, the same document would
# cache 1.33 MiB, over budget. XML sits higher under one budget because an XML
# element emits its path once where a JSON object emits it twice, as its own
# section name and again in its parent's `subsections`. Independently, `freeze`
# refuses to snapshot a value nested deeper than 200 levels, and a JSON document's
# container depth is exactly its snapshot depth, so a document past 200 could never
# be cached at all — it raises `UnsupportedValueError` out of the cutoff instead.
# 200 is still an order of magnitude deeper than any real configuration document.
_MAX_JSON_DEPTH = 200

# Every byte that cannot change the nesting depth, so that only quotes and brackets
# are left to walk. Backslash escape pairs go first, separately: dropping just the
# escaped character would let a `\uXXXX` key leave a bare `\"` behind and swallow
# the quote that ends the string.
_NON_STRUCTURAL = bytes(byte for byte in range(256) if byte not in b'{}[]"')


def _text_nesting_depth(text: str) -> int:
    """Report the deepest object/array nesting in `text` without recursing.

    Well-formed JSON yields the exact depth the scanner would descend to. Input
    that is not well-formed yields an estimate, and is rejected either way; what
    matters is that neither answer depends on the caller's remaining stack.
    """
    encoded = text.encode("utf-8", "surrogatepass")
    unescaped = re.sub(rb"\\.", b"", encoded, flags=re.DOTALL)
    tokens = unescaped.translate(None, _NON_STRUCTURAL).decode("ascii")

    depth = 0
    deepest = 0
    in_string = False
    for token in tokens:
        if token == '"':
            in_string = not in_string
        elif in_string:
            continue
        elif token in "{[":
            depth += 1
            if depth > deepest:
                deepest = depth
        else:
            depth -= 1
    return deepest


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number {value!r} is not permitted")


def _parse_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number {value!r} is not permitted")
    return parsed


def _json_object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(f"duplicate JSON object key {key!r}")
        if not _is_unicode_text(key):
            # An object key reaches the cached payload verbatim, as its own
            # section name and inside every descendant's dot path, where a lone
            # surrogate is not a value `freeze` can snapshot. Values are safe
            # because they reach the payload through `repr`, which escapes one.
            raise _JsonSurrogateKeyError(f"JSON object key {key!r} contains an unpaired surrogate")
        result[key] = value
    return result


def _is_unicode_text(value: str) -> bool:
    """Report whether `value` is made only of Unicode scalar values.

    The ASCII test is the fast path an overwhelming majority of keys take; only
    a key that leaves it pays for an encode.
    """
    if value.isascii():
        return True
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _load_json(text: str) -> Any:
    depth = _text_nesting_depth(text)
    if depth > _MAX_JSON_DEPTH:
        raise _JsonNestingLimitError(
            f"JSON nesting exceeds the supported limit of {_MAX_JSON_DEPTH} levels"
        )
    return json.loads(
        text,
        object_pairs_hook=_json_object_from_pairs,
        parse_constant=_reject_json_constant,
        parse_float=_parse_json_float,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# CPython's JSON scanner checks the interpreter's recursion budget, so which frame
# runs out — and so which message it raises — depends on how much stack the caller
# had already spent, not on the file. The same document reports "...while decoding
# a JSON object..." or "...while decoding a JSON array..." from different call
# depths. This payload is cached, so a fixed string is emitted instead.
#
# `_MAX_JSON_DEPTH` is measured off the text rather than by descending, so runaway
# nesting is rejected as a decode error before the scanner ever runs, and a
# RecursionError here means only that the caller entered with its stack nearly
# spent. That closes the message axis, not the outcome axis: the scanner still
# descends once per container level, so whether a document within the cap parses
# at all remains a property of the call site as well as of the file. The residual
# is disclosed in `docs/integration-contract.md`. `xml_config` emits the same shape
# for the same reason and carries the same residual.
_STACK_EXHAUSTED_DIAGNOSTIC = "JSON parsing exhausted the interpreter stack"


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
    """Collect every object in document pre-order, deepest nesting included.

    The traversal keeps its own stack rather than recursing, so the payload a
    document produces depends only on the document — never on how much of the
    interpreter's recursion budget the caller has already spent.
    """
    sections: list[JsonSectionPayload] = []
    pending: list[tuple[dict[str, Any], str]] = [(data, prefix)]

    while pending:
        current, current_prefix = pending.pop()
        section_name = current_prefix or "<root>"
        keys: list[JsonKeyPayload] = []
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
                        _json_value_type(value),
                        _json_value_to_string(value),
                    )
                )

        sections.append((section_name, tuple(keys), tuple(subsections)))
        # Reversed so the first subsection is popped first, preserving document order.
        pending.extend(reversed(children))

    return sections


def _json_cutoff_token(text: str) -> tuple[str, str]:
    try:
        parsed = _load_json(text)
        snapshot = freeze(parsed)
        return ("parsed", repr(snapshot))
    except (ValueError, RecursionError, OverflowError, UnsupportedValueError):
        # `freeze` rejects a value outside the snapshot grammar with
        # `UnsupportedValueError`, which is a `PyIncError` and not a
        # `ValueError`: a document `json.loads` accepts but `freeze` refuses --
        # a lone surrogate escape in a string value, say -- must degrade to the
        # raw text here, not escape the cutoff and fail the recomputation a
        # fresh database completes.
        return ("raw", text)


def _try_parse_json(text: str) -> dict[str, Any] | None:
    try:
        result = _load_json(text)
        if isinstance(result, dict):
            return result
        return None
    except (ValueError, RecursionError, OverflowError, UnsupportedValueError):
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
        _load_json(text)
        return ()
    except (ValueError, OverflowError) as exc:
        return (("json-decode-error", str(exc)),)
    except RecursionError:
        return (("json-decode-error", _STACK_EXHAUSTED_DIAGNOSTIC),)


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
        keys=tuple(
            JsonKey(section=k[0], key=k[1], value_type=k[2], string_value=k[3]) for k in keys
        ),
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
