from __future__ import annotations

import os
import xml.etree.ElementTree as ET
import xml.parsers.expat
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, cast

from pyinc.core import query
from pyinc.errors import UnsupportedValueError
from pyinc.resources import DirectoryResource
from pyinc.runtime import Database
from pyinc.value import freeze, thaw

from ._resources import file_probe, file_read_snapshot, file_text

XmlAttributePayload: TypeAlias = tuple[str, str]
XmlElementPayload: TypeAlias = tuple[
    str,  # tag
    str,  # path
    str,  # text
    tuple[XmlAttributePayload, ...],  # attributes
    tuple[str, ...],  # children tags
]
DiagnosticPayload: TypeAlias = tuple[str, str]
XmlAnalysisPayload: TypeAlias = tuple[
    str,  # path
    str,  # root_tag
    tuple[XmlElementPayload, ...],  # elements
    tuple[DiagnosticPayload, ...],  # diagnostics
]


@dataclass(frozen=True)
class XmlAttribute:
    name: str
    value: str


@dataclass(frozen=True)
class XmlElement:
    tag: str
    path: str
    text: str
    attributes: tuple[XmlAttribute, ...]
    children: tuple[str, ...]


@dataclass(frozen=True)
class XmlAnalysis:
    path: str
    root_tag: str
    elements: tuple[XmlElement, ...]
    diagnostics: tuple[tuple[str, str], ...]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _XmlFileResource:
    encoding: str = "utf-8"

    def read(self, db: Database, path: str | os.PathLike[str]) -> str:
        return cast(str, db.read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"xmlfile[{path}]"

    def probe(self, path: str) -> tuple[str, str] | tuple[str]:
        return file_probe(path)

    def load(self, db: Database, path: str) -> str:
        text = file_text(path, self.encoding)
        return text if text is not None else ""

    def probe_and_load(self, db: Database, path: str) -> tuple[tuple[str, str] | tuple[str], str]:
        probe, text = file_read_snapshot(path, self.encoding)
        return probe, text if text is not None else ""


_FILES = _XmlFileResource()
_DIRECTORIES = DirectoryResource()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NS_PAT = "}"

# Element nesting is capped during parsing because every element re-emits the dot
# path of all its ancestors: the cached cutoff token grows with the square of the
# nesting depth, so this cap is what bounds the *cache*, not just the parse.
#
# It is therefore set from an explicit amplification budget, not from the
# interpreter's recursion limit — the walk keeps its own stack and needs under 20
# frames at any depth, so the interpreter's ceiling is not the constraint here.
# Budget: a document at the cap must not cache more than ~1 MiB of cutoff token.
# At 256 levels that holds for element names up to 20 characters (measured: 708 KB
# for a 20-character name, 207 KB for a 5-character one, 74 KB for a 1-character
# one). The token scales linearly in name length on top of the quadratic depth
# term, so the budget is stated for that name length rather than unconditionally.
# 256 is still an order of magnitude deeper than any real configuration document.
_MAX_XML_DEPTH = 256

# A RecursionError raised anywhere under `_safe_parse` cannot be a property of the
# document: `_MAX_XML_DEPTH` rejects runaway nesting as a ParseError before the
# tree is built, and the element walk is iterative. It means only that the caller
# entered with the interpreter's stack all but spent, because expat invokes
# `_start_element` as a Python frame. CPython names whichever frame ran out
# ("...while calling a Python object", "...while getting the str of an object"),
# so the message describes the call site rather than the file. These payloads are
# cached, so a fixed string is emitted instead and the payload stays a function of
# the tracked inputs. `json_config` emits the same shape for the same reason.
_STACK_EXHAUSTED_DIAGNOSTIC = "XML parsing exhausted the interpreter stack"


def _strip_namespace(tag: str) -> str:
    if tag.startswith("{"):
        idx = tag.find(_NS_PAT)
        if idx >= 0:
            return tag[idx + 1 :]
    return tag


def _walk_elements(
    elem: ET.Element,
    prefix: str,
) -> list[XmlElementPayload]:
    """Collect every element in document pre-order, deepest nesting included.

    The traversal keeps its own stack rather than recursing, so the payload a
    document produces depends only on the document — never on how much of the
    interpreter's recursion budget the caller has already spent.
    """
    elements: list[XmlElementPayload] = []
    pending: list[tuple[ET.Element, str]] = [(elem, prefix)]

    while pending:
        current, current_prefix = pending.pop()

        local_tag = _strip_namespace(current.tag)
        current_path = f"{current_prefix}.{local_tag}" if current_prefix else local_tag

        attrs: tuple[XmlAttributePayload, ...] = tuple(
            (_strip_namespace(k), v) for k, v in sorted(current.attrib.items())
        )
        child_tags: tuple[str, ...] = tuple(_strip_namespace(child.tag) for child in current)
        text = (current.text or "").strip()

        elements.append((local_tag, current_path, text, attrs, child_tags))

        # Reversed so the first child is popped first, preserving document order.
        pending.extend((child, current_path) for child in reversed(current))

    return elements


def _safe_parse(text: str) -> ET.Element:
    """Parse XML with DOCTYPE, entity declarations, and runaway nesting rejected.

    DTDs and entity declarations are blocked at parse time; this neutralises
    billion-laughs expansion and external-DTD exfiltration regardless of the
    underlying expat version's default handling. Nesting past `_MAX_XML_DEPTH`
    is rejected at the element that crosses the cap, so the tree stops growing
    there rather than being built in full and rejected afterwards.
    Namespace-qualified tags are normalised to Clark notation
    (`{uri}localname`) so the result is shaped identically to `ET.fromstring`.
    Malformed input and rejected constructs both surface as `ET.ParseError`.
    """
    builder = ET.TreeBuilder()
    parser = xml.parsers.expat.ParserCreate(encoding=None, namespace_separator="}")
    depth = 0

    def _forbid(*_args: object, **_kwargs: object) -> None:
        raise ET.ParseError("DTD / entity declarations disabled for safety")

    def _start_element(tag: str, attrs: dict[str, str]) -> None:
        nonlocal depth
        depth += 1
        if depth > _MAX_XML_DEPTH:
            raise ET.ParseError(
                f"XML nesting exceeds the supported limit of {_MAX_XML_DEPTH} levels"
            )
        normalised_tag = "{" + tag if "}" in tag else tag
        normalised_attrs = {("{" + k if "}" in k else k): v for k, v in attrs.items()}
        builder.start(normalised_tag, normalised_attrs)

    def _end_element(tag: str) -> None:
        nonlocal depth
        depth -= 1
        builder.end("{" + tag if "}" in tag else tag)

    parser.StartDoctypeDeclHandler = _forbid
    parser.EntityDeclHandler = _forbid
    parser.StartElementHandler = _start_element
    parser.EndElementHandler = _end_element
    parser.CharacterDataHandler = builder.data
    try:
        parser.Parse(text.encode("utf-8"), True)
    except xml.parsers.expat.ExpatError as exc:
        raise ET.ParseError(str(exc)) from exc
    return builder.close()


def _xml_cutoff_token(text: str) -> tuple[str, str]:
    try:
        root = _safe_parse(text)
        elements = _walk_elements(root, "")
        snapshot = freeze(elements)
        return ("parsed", repr(snapshot))
    except (ET.ParseError, RecursionError, UnsupportedValueError):
        # `freeze` refuses a value outside the snapshot grammar with
        # `UnsupportedValueError`, which is a `PyIncError` and not a
        # `ValueError`: a document the parser accepts but `freeze` will not
        # snapshot must degrade to the raw text, not escape the cutoff and fail
        # a recomputation a fresh database completes.
        return ("raw", text)


def _try_parse_xml(text: str) -> ET.Element | None:
    try:
        return _safe_parse(text)
    except (ET.ParseError, RecursionError):
        return None


# ---------------------------------------------------------------------------
# Layer 1 — Payload queries
# ---------------------------------------------------------------------------


@query(cutoff=_xml_cutoff_token)
def xml_file_text(db: Database, path: str) -> str:
    return _FILES.read(db, path)


@query
def xml_elements_payload(db: Database, path: str) -> tuple[XmlElementPayload, ...]:
    text = xml_file_text(db, path)
    root = _try_parse_xml(text)
    if root is None:
        return ()
    return tuple(_walk_elements(root, ""))


@query
def xml_diagnostics_payload(db: Database, path: str) -> tuple[DiagnosticPayload, ...]:
    text = xml_file_text(db, path)
    if not text:
        return ()
    try:
        _safe_parse(text)
        return ()
    except ET.ParseError as exc:
        return (("xml-parse-error", str(exc)),)
    except RecursionError:
        return (("xml-parse-error", _STACK_EXHAUSTED_DIAGNOSTIC),)


# ---------------------------------------------------------------------------
# Layer 2 — Composition
# ---------------------------------------------------------------------------


@query
def xml_analysis_payload(db: Database, path: str) -> XmlAnalysisPayload:
    elements = xml_elements_payload(db, path)
    diagnostics = xml_diagnostics_payload(db, path)
    root_tag = elements[0][0] if elements else ""
    return (path, root_tag, elements, diagnostics)


# ---------------------------------------------------------------------------
# Layer 3 — Entrypoints
# ---------------------------------------------------------------------------


def _decode_element(payload: XmlElementPayload) -> XmlElement:
    tag, element_path, text, attrs, children = payload
    return XmlElement(
        tag=tag,
        path=element_path,
        text=text,
        attributes=tuple(XmlAttribute(name=a[0], value=a[1]) for a in attrs),
        children=children,
    )


def xml_analysis(db: Database, path: str | os.PathLike[str]) -> XmlAnalysis:
    normalized = os.fspath(path)
    payload = cast(XmlAnalysisPayload, thaw(db.get(xml_analysis_payload, normalized)))
    path_str, root_tag, elements, diagnostics = payload
    return XmlAnalysis(
        path=path_str,
        root_tag=root_tag,
        elements=tuple(_decode_element(e) for e in elements),
        diagnostics=diagnostics,
    )


def workspace_xml_analysis(
    db: Database,
    root: str | os.PathLike[str],
    filename: str = "pom.xml",
) -> XmlAnalysis | None:
    normalized_root = os.fspath(root)
    dir_entries = _DIRECTORIES.read(db, normalized_root)
    xml_path = None
    for name in dir_entries:
        if name == filename:
            xml_path = str(Path(normalized_root) / name)
            break
    if xml_path is None:
        return None
    return xml_analysis(db, xml_path)


__all__ = [
    "XmlAttribute",
    "XmlElement",
    "XmlAnalysis",
    "xml_analysis",
    "workspace_xml_analysis",
]
