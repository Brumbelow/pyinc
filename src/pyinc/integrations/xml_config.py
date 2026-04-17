from __future__ import annotations

import hashlib
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, cast

from pyinc.core import query
from pyinc.resources import DirectoryResource
from pyinc.runtime import Database
from pyinc.value import thaw

XmlAttributePayload: TypeAlias = tuple[str, str]
XmlElementPayload: TypeAlias = tuple[
    str,                              # tag
    str,                              # path
    str,                              # text
    tuple[XmlAttributePayload, ...],  # attributes
    tuple[str, ...],                  # children tags
]
DiagnosticPayload: TypeAlias = tuple[str, str]
XmlAnalysisPayload: TypeAlias = tuple[
    str,                              # path
    str,                              # root_tag
    tuple[XmlElementPayload, ...],    # elements
    tuple[DiagnosticPayload, ...],    # diagnostics
]


class XmlSecurityError(ValueError):
    """Raised when an XML document uses a feature that is disallowed for safety reasons."""


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
        return cast(str, db._read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"xmlfile[{path}]"

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
            data = file_path.read_bytes()
        return data.decode(self.encoding)

    def recompute_probe(self, path: str, loaded_value: str) -> tuple[str, str] | tuple[str]:
        if not loaded_value:
            file_path = Path(path)
            if not file_path.exists():
                return ("missing",)
        return ("present", hashlib.sha256(loaded_value.encode(self.encoding)).hexdigest())


_FILES = _XmlFileResource()
_DIRECTORIES = DirectoryResource()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NS_PAT = "}"


def _strip_namespace(tag: str) -> str:
    if tag.startswith("{"):
        idx = tag.find(_NS_PAT)
        if idx >= 0:
            return tag[idx + 1:]
    return tag


def _walk_elements(
    elem: ET.Element,
    prefix: str,
) -> list[XmlElementPayload]:
    elements: list[XmlElementPayload] = []

    local_tag = _strip_namespace(elem.tag)
    current_path = f"{prefix}.{local_tag}" if prefix else local_tag

    attrs: tuple[XmlAttributePayload, ...] = tuple(
        (_strip_namespace(k), v) for k, v in sorted(elem.attrib.items())
    )
    child_tags: tuple[str, ...] = tuple(
        _strip_namespace(child.tag) for child in elem
    )
    text = (elem.text or "").strip()

    elements.append((local_tag, current_path, text, attrs, child_tags))

    for child in elem:
        elements.extend(_walk_elements(child, current_path))

    return elements


def _reject_doctype(text: str) -> None:
    # DOCTYPE declarations must appear in the XML prolog; we additionally reject
    # any occurrence anywhere as a belt-and-braces defense against entity-expansion
    # ("billion laughs") attacks. Stdlib xml.etree disables external entity
    # retrieval but still expands internal entity definitions.
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise XmlSecurityError(
            "XML DOCTYPE/ENTITY declarations are not permitted; strip the prolog "
            "before loading or use a different parser."
        )


def _normalize_xml_text(text: str) -> str:
    # Produce a cutoff token that is cheap to compute (no XML parse) and stable
    # under whitespace-only reformatting. Drop XML comments, collapse ASCII
    # whitespace runs outside double-quoted attribute values to a single space,
    # then drop spaces adjacent to tag boundaries. This treats inter-tag and
    # leading/trailing text whitespace as insignificant — a semantic edit (tag
    # rename, attribute change, text change) still invalidates; a reformat does
    # not. False invalidations are still possible for whitespace-significant
    # XML; the kernel is correctness-first, so false-reuse is the failure mode
    # to avoid.
    out: list[str] = []
    i = 0
    n = len(text)
    in_quote = False
    while i < n:
        ch = text[i]
        if not in_quote and text.startswith("<!--", i):
            end = text.find("-->", i + 4)
            if end < 0:
                break
            i = end + 3
            continue
        if ch == '"':
            in_quote = not in_quote
            out.append(ch)
            i += 1
            continue
        if not in_quote and ch in " \t\r\n":
            if out and out[-1] != " ":
                out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    collapsed = "".join(out).strip()
    # Drop spaces immediately adjacent to '<' or '>'.
    result: list[str] = []
    j = 0
    m = len(collapsed)
    while j < m:
        c = collapsed[j]
        if c == " " and (
            (result and result[-1] == ">")
            or (j + 1 < m and collapsed[j + 1] == "<")
        ):
            j += 1
            continue
        result.append(c)
        j += 1
    return "".join(result)


def _xml_cutoff_token(text: str) -> tuple[str, str]:
    return ("text", _normalize_xml_text(text))


def _try_parse_xml(text: str) -> ET.Element | None:
    try:
        _reject_doctype(text)
        return ET.fromstring(text)  # noqa: S314
    except (ET.ParseError, XmlSecurityError):
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
        _reject_doctype(text)
    except XmlSecurityError as exc:
        return (("xml-security-error", str(exc)),)
    try:
        ET.fromstring(text)  # noqa: S314
        return ()
    except ET.ParseError as exc:
        return (("xml-parse-error", str(exc)),)


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
    "XmlSecurityError",
    "xml_analysis",
    "workspace_xml_analysis",
]
