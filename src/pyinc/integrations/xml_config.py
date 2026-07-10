from __future__ import annotations

import hashlib
import os
import xml.etree.ElementTree as ET
import xml.parsers.expat
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, cast

from pyinc.core import query
from pyinc.resources import DirectoryResource
from pyinc.runtime import Database
from pyinc.value import freeze, thaw

from ._resources import file_read_snapshot

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
            return tag[idx + 1 :]
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
    child_tags: tuple[str, ...] = tuple(_strip_namespace(child.tag) for child in elem)
    text = (elem.text or "").strip()

    elements.append((local_tag, current_path, text, attrs, child_tags))

    for child in elem:
        elements.extend(_walk_elements(child, current_path))

    return elements


def _safe_parse(text: str) -> ET.Element:
    """Parse XML with DOCTYPE and entity declarations rejected.

    DTDs and entity declarations are blocked at parse time; this neutralises
    billion-laughs expansion and external-DTD exfiltration regardless of the
    underlying expat version's default handling. Namespace-qualified tags are
    normalised to Clark notation (`{uri}localname`) so the result is shaped
    identically to `ET.fromstring`. Malformed input and rejected constructs
    both surface as `ET.ParseError`.
    """
    builder = ET.TreeBuilder()
    parser = xml.parsers.expat.ParserCreate(encoding=None, namespace_separator="}")

    def _forbid(*_args: object, **_kwargs: object) -> None:
        raise ET.ParseError("DTD / entity declarations disabled for safety")

    def _start_element(tag: str, attrs: dict[str, str]) -> None:
        normalised_tag = "{" + tag if "}" in tag else tag
        normalised_attrs = {("{" + k if "}" in k else k): v for k, v in attrs.items()}
        builder.start(normalised_tag, normalised_attrs)

    def _end_element(tag: str) -> None:
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
    except ET.ParseError:
        return ("raw", text)


def _try_parse_xml(text: str) -> ET.Element | None:
    try:
        return _safe_parse(text)
    except ET.ParseError:
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
