from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import pyinc.integrations as integrations
from pyinc import Database
from pyinc.integrations import xml_config
from pyinc.integrations.xml_config import (
    _MAX_XML_DEPTH,
    XmlAnalysis,
    _safe_parse,
    _try_parse_xml,
    _xml_cutoff_token,
    workspace_xml_analysis,
    xml_analysis,
)

_MINIMAL_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>myapp</artifactId>
  <version>1.0.0</version>
  <dependencies>
    <dependency>
      <groupId>junit</groupId>
      <artifactId>junit</artifactId>
      <version>4.13</version>
    </dependency>
  </dependencies>
</project>
"""

_NAMESPACED_XML = """\
<root xmlns:ns="http://example.com/ns">
  <ns:child ns:attr="value">text</ns:child>
</root>
"""

_XML_WITH_ATTRS = """\
<config>
  <setting name="debug" value="true"/>
  <setting name="timeout" value="30"/>
</config>
"""


# ---------------------------------------------------------------------------
# Contract lock
# ---------------------------------------------------------------------------


def test_package_namespace_exports_xml_config_stable_api() -> None:
    assert "XmlAttribute" in integrations.__all__
    assert "XmlElement" in integrations.__all__
    assert "XmlAnalysis" in integrations.__all__
    assert "xml_analysis" in integrations.__all__
    assert "workspace_xml_analysis" in integrations.__all__

    assert hasattr(integrations, "xml_analysis")
    assert hasattr(integrations, "workspace_xml_analysis")
    assert hasattr(integrations, "XmlAnalysis")
    assert hasattr(integrations, "XmlAttribute")
    assert hasattr(integrations, "XmlElement")

    # Experimental helpers must not leak.
    assert not hasattr(integrations, "xml_file_text")
    assert not hasattr(integrations, "xml_elements_payload")
    assert not hasattr(integrations, "xml_analysis_payload")
    assert not hasattr(integrations, "xml_diagnostics_payload")


# ---------------------------------------------------------------------------
# Mode-parametrized correctness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_xml_analysis_extracts_elements(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "pom.xml"
    path.write_text(_MINIMAL_XML, encoding="utf-8")

    db = Database(mode=mode)
    result = xml_analysis(db, str(path))

    assert isinstance(result, XmlAnalysis)
    assert result.path == str(path)
    assert result.root_tag == "project"

    tags = {e.tag for e in result.elements}
    assert "project" in tags
    assert "modelVersion" in tags
    assert "groupId" in tags
    assert "dependency" in tags


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_xml_analysis_extracts_paths(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "pom.xml"
    path.write_text(_MINIMAL_XML, encoding="utf-8")

    db = Database(mode=mode)
    result = xml_analysis(db, str(path))

    paths = {e.path for e in result.elements}
    assert "project" in paths
    assert "project.dependencies" in paths
    assert "project.dependencies.dependency" in paths
    assert "project.dependencies.dependency.groupId" in paths


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_xml_analysis_extracts_text(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "pom.xml"
    path.write_text(_MINIMAL_XML, encoding="utf-8")

    db = Database(mode=mode)
    result = xml_analysis(db, str(path))

    by_path = {e.path: e for e in result.elements}
    assert by_path["project.version"].text == "1.0.0"
    assert by_path["project.groupId"].text == "com.example"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_xml_analysis_handles_namespaces(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "ns.xml"
    path.write_text(_NAMESPACED_XML, encoding="utf-8")

    db = Database(mode=mode)
    result = xml_analysis(db, str(path))

    tags = {e.tag for e in result.elements}
    assert "root" in tags
    assert "child" in tags

    by_tag = {e.tag: e for e in result.elements}
    assert by_tag["child"].text == "text"
    attr_names = {a.name for a in by_tag["child"].attributes}
    assert "attr" in attr_names


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_xml_analysis_extracts_attributes(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "config.xml"
    path.write_text(_XML_WITH_ATTRS, encoding="utf-8")

    db = Database(mode=mode)
    result = xml_analysis(db, str(path))

    settings = [e for e in result.elements if e.tag == "setting"]
    assert len(settings) == 2

    by_name = {}
    for s in settings:
        name_attr = next(a for a in s.attributes if a.name == "name")
        by_name[name_attr.value] = s

    debug_val = next(a for a in by_name["debug"].attributes if a.name == "value")
    assert debug_val.value == "true"

    timeout_val = next(a for a in by_name["timeout"].attributes if a.name == "value")
    assert timeout_val.value == "30"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_xml_analysis_children_list(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "pom.xml"
    path.write_text(_MINIMAL_XML, encoding="utf-8")

    db = Database(mode=mode)
    result = xml_analysis(db, str(path))

    by_path = {e.path: e for e in result.elements}
    project = by_path["project"]
    assert "modelVersion" in project.children
    assert "groupId" in project.children
    assert "dependencies" in project.children


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_xml_analysis_reports_parse_errors(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "broken.xml"
    path.write_text("<root><unclosed>", encoding="utf-8")

    db = Database(mode=mode)
    result = xml_analysis(db, str(path))

    assert len(result.diagnostics) == 1
    assert result.diagnostics[0][0] == "xml-parse-error"
    assert result.elements == ()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_xml_analysis_empty_file(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "empty.xml"
    path.write_text("", encoding="utf-8")

    db = Database(mode=mode)
    result = xml_analysis(db, str(path))

    assert result.elements == ()
    assert result.diagnostics == ()
    assert result.root_tag == ""


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_xml_analysis_missing_file(mode: str, tmp_path: Path) -> None:
    db = Database(mode=mode)
    result = xml_analysis(db, str(tmp_path / "nonexistent.xml"))

    assert result.elements == ()
    assert result.diagnostics == ()


# ---------------------------------------------------------------------------
# Workspace discovery
# ---------------------------------------------------------------------------


def test_workspace_xml_analysis_discovers_pom(tmp_path: Path) -> None:
    path = tmp_path / "pom.xml"
    path.write_text(_MINIMAL_XML, encoding="utf-8")

    db = Database()
    result = workspace_xml_analysis(db, str(tmp_path))

    assert result is not None
    assert result.root_tag == "project"


def test_workspace_xml_analysis_returns_none_when_missing(tmp_path: Path) -> None:
    db = Database()
    result = workspace_xml_analysis(db, str(tmp_path))
    assert result is None


def test_workspace_xml_analysis_custom_filename(tmp_path: Path) -> None:
    path = tmp_path / "config.xml"
    path.write_text(_XML_WITH_ATTRS, encoding="utf-8")

    db = Database()
    result = workspace_xml_analysis(db, str(tmp_path), filename="config.xml")

    assert result is not None
    assert result.root_tag == "config"


# ---------------------------------------------------------------------------
# Backdating
# ---------------------------------------------------------------------------


def test_whitespace_only_edit_backdates_xml(tmp_path: Path) -> None:
    path = tmp_path / "config.xml"
    path.write_text("<root><child>text</child></root>", encoding="utf-8")

    db = Database()
    first = xml_analysis(db, str(path))

    # Reformat with indentation — semantically identical
    path.write_text("<root>\n  <child>text</child>\n</root>\n", encoding="utf-8")
    second = xml_analysis(db, str(path))

    assert first == second


def test_semantic_edit_invalidates_xml(tmp_path: Path) -> None:
    path = tmp_path / "config.xml"
    path.write_text("<root><child>old</child></root>", encoding="utf-8")

    db = Database()
    first = xml_analysis(db, str(path))

    path.write_text("<root><child>new</child></root>", encoding="utf-8")
    second = xml_analysis(db, str(path))

    assert first != second


def test_attribute_edit_invalidates_xml(tmp_path: Path) -> None:
    path = tmp_path / "config.xml"
    path.write_text('<root attr="old"/>', encoding="utf-8")

    db = Database()
    first = xml_analysis(db, str(path))

    path.write_text('<root attr="new"/>', encoding="utf-8")
    second = xml_analysis(db, str(path))

    assert first != second


# ---------------------------------------------------------------------------
# From-scratch consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_xml_analysis_matches_fresh_recomputation(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "config.xml"

    steps: tuple[tuple[str, str], ...] = (
        ("initial", "<root><child>text</child></root>"),
        ("reformat", "<root>\n  <child>text</child>\n</root>"),
        ("change text", "<root><child>new</child></root>"),
        ("add element", "<root><child>new</child><other>x</other></root>"),
        ("add attr", '<root attr="v"><child>new</child><other>x</other></root>'),
        ("remove element", '<root attr="v"><child>new</child></root>'),
    )

    incremental = Database(mode=mode)
    for _label, content in steps:
        path.write_text(content, encoding="utf-8")
        fresh = Database(mode=mode)
        assert xml_analysis(incremental, str(path)) == xml_analysis(fresh, str(path))


_BILLION_LAUGHS = """\
<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<root>&lol4;</root>
"""

_EXTERNAL_DTD = """\
<?xml version="1.0"?>
<!DOCTYPE r SYSTEM "file:///etc/passwd">
<r/>
"""


def test_safe_parse_rejects_billion_laughs_at_doctype() -> None:
    with pytest.raises(ET.ParseError) as exc_info:
        _safe_parse(_BILLION_LAUGHS + "<unterminated")

    assert str(exc_info.value) == "DTD / entity declarations disabled for safety"


def test_xml_analysis_rejects_billion_laughs_payload(tmp_path: Path) -> None:
    path = tmp_path / "evil.xml"
    path.write_text(_BILLION_LAUGHS, encoding="utf-8")

    result = xml_analysis(Database(), str(path))

    assert result.elements == ()
    assert any(diag[0] == "xml-parse-error" for diag in result.diagnostics)


def test_xml_analysis_rejects_external_dtd(tmp_path: Path) -> None:
    path = tmp_path / "ext.xml"
    path.write_text(_EXTERNAL_DTD, encoding="utf-8")

    result = xml_analysis(Database(), str(path))
    assert result.elements == ()
    assert any(diag[0] == "xml-parse-error" for diag in result.diagnostics)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_xml_analysis_matches_fresh_recomputation_with_adversarial_payloads(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "shifty.xml"

    steps: tuple[tuple[str, str], ...] = (
        ("safe", "<root><child>hi</child></root>"),
        ("billion-laughs", _BILLION_LAUGHS),
        ("external-dtd", _EXTERNAL_DTD),
        ("safe again", "<root><child>hi</child></root>"),
        ("minimal doctype", "<?xml version='1.0'?><!DOCTYPE r><r/>"),
        ("safe different", "<root><other>bye</other></root>"),
    )

    incremental = Database(mode=mode)
    for _label, content in steps:
        path.write_text(content, encoding="utf-8")
        fresh = Database(mode=mode)
        assert xml_analysis(incremental, str(path)) == xml_analysis(fresh, str(path))


# ---------------------------------------------------------------------------
# Nesting depth
# ---------------------------------------------------------------------------


def _nested_xml(levels: int) -> str:
    """A document `levels + 1` elements deep — `<root>` plus `levels` nestings."""
    return "<root>" + "<level>" * levels + "leaf" + "</level>" * levels + "</root>"


def test_safe_parse_stops_at_the_nesting_limit() -> None:
    with pytest.raises(ET.ParseError) as exc_info:
        _safe_parse(_nested_xml(_MAX_XML_DEPTH))

    assert str(exc_info.value) == (
        f"XML nesting exceeds the supported limit of {_MAX_XML_DEPTH} levels"
    )


def test_safe_parse_accepts_a_document_exactly_at_the_nesting_limit() -> None:
    root = _safe_parse(_nested_xml(_MAX_XML_DEPTH - 1))
    assert root.tag == "root"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_xml_analysis_diagnoses_runaway_nesting(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "deep.xml"
    path.write_text(_nested_xml(_MAX_XML_DEPTH + 500), encoding="utf-8")

    db = Database(mode=mode)
    result = xml_analysis(db, str(path))

    assert result.elements == ()
    assert result.root_tag == ""
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0][0] == "xml-parse-error"


def test_xml_nesting_diagnostic_names_the_limit(tmp_path: Path) -> None:
    path = tmp_path / "deep.xml"
    path.write_text(_nested_xml(_MAX_XML_DEPTH), encoding="utf-8")

    result = xml_analysis(Database(), str(path))

    assert result.diagnostics == (
        (
            "xml-parse-error",
            f"XML nesting exceeds the supported limit of {_MAX_XML_DEPTH} levels",
        ),
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_xml_analysis_walks_deeper_than_the_interpreter_recursion_limit(
    mode: str, tmp_path: Path
) -> None:
    assert sys.getrecursionlimit() < _MAX_XML_DEPTH

    path = tmp_path / "deep.xml"
    path.write_text(_nested_xml(_MAX_XML_DEPTH - 1), encoding="utf-8")

    db = Database(mode=mode)
    result = xml_analysis(db, str(path))

    assert result.diagnostics == ()
    assert len(result.elements) == _MAX_XML_DEPTH
    assert result.elements[-1].path.count(".") == _MAX_XML_DEPTH - 1


def test_xml_analysis_matches_fresh_recomputation_across_the_nesting_limit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "shifty.xml"

    steps: tuple[tuple[str, str], ...] = (
        ("shallow", "<root><child>hi</child></root>"),
        ("at the limit", _nested_xml(_MAX_XML_DEPTH - 1)),
        ("past the limit", _nested_xml(_MAX_XML_DEPTH)),
        ("shallow again", "<root><child>hi</child></root>"),
    )

    incremental = Database()
    for _label, content in steps:
        path.write_text(content, encoding="utf-8")
        fresh = Database()
        assert xml_analysis(incremental, str(path)) == xml_analysis(fresh, str(path))


def test_a_recursion_error_never_escapes_the_parse_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _exhaust_the_stack(_text: str) -> ET.Element:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(xml_config, "_safe_parse", _exhaust_the_stack)

    assert _try_parse_xml("<root/>") is None
    assert _xml_cutoff_token("<root/>") == ("raw", "<root/>")


# ---------------------------------------------------------------------------
# Payload and cutoff stability
# ---------------------------------------------------------------------------


def test_elements_are_emitted_in_document_pre_order(tmp_path: Path) -> None:
    path = tmp_path / "tree.xml"
    path.write_text("<a><b><c/><d/></b><e><f/></e></a>", encoding="utf-8")

    result = xml_analysis(Database(), str(path))

    assert tuple(e.path for e in result.elements) == (
        "a",
        "a.b",
        "a.b.c",
        "a.b.d",
        "a.e",
        "a.e.f",
    )


def test_cutoff_token_is_byte_for_byte_stable() -> None:
    assert _xml_cutoff_token('<root attr="v"><child>text</child><other/></root>') == (
        "parsed",
        "FrozenList(items=("
        "('root', 'root', '', (('attr', 'v'),), ('child', 'other')), "
        "('child', 'root.child', 'text', (), ()), "
        "('other', 'root.other', '', (), ())))",
    )
    assert _xml_cutoff_token("<root><unclosed>") == ("raw", "<root><unclosed>")
