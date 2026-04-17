from __future__ import annotations

from pathlib import Path

import pytest

import pyinc.integrations as integrations
from pyinc import Database
from pyinc.integrations.xml_config import (
    XmlAnalysis,
    XmlSecurityError,
    _normalize_xml_text,
    _reject_doctype,
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


# ---------------------------------------------------------------------------
# Audit remediation: DOCTYPE/ENTITY rejection + parse-free cutoff (Finding 6)
# ---------------------------------------------------------------------------


_BILLION_LAUGHS = """\
<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
]>
<lolz>&lol2;</lolz>
"""


def test_reject_doctype_blocks_billion_laughs() -> None:
    with pytest.raises(XmlSecurityError):
        _reject_doctype(_BILLION_LAUGHS)


def test_reject_doctype_blocks_plain_doctype() -> None:
    with pytest.raises(XmlSecurityError):
        _reject_doctype('<?xml version="1.0"?>\n<!DOCTYPE note SYSTEM "note.dtd">\n<note/>')


def test_reject_doctype_accepts_xml_declaration() -> None:
    # XML declaration is not a DOCTYPE.
    _reject_doctype('<?xml version="1.0" encoding="UTF-8"?>\n<root/>')


def test_xml_analysis_surfaces_security_error_as_diagnostic(tmp_path: Path) -> None:
    path = tmp_path / "lolz.xml"
    path.write_text(_BILLION_LAUGHS, encoding="utf-8")

    db = Database()
    result = xml_analysis(db, str(path))
    # No elements extracted (security-rejected inputs are treated like unparseable).
    assert result.elements == ()
    assert any(kind == "xml-security-error" for kind, _msg in result.diagnostics)


def test_xml_cutoff_token_does_not_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    import xml.etree.ElementTree as ET

    call_count = {"n": 0}
    original = ET.fromstring

    def spy(text: str, *args: object, **kwargs: object) -> object:
        call_count["n"] += 1
        return original(text, *args, **kwargs)

    monkeypatch.setattr(ET, "fromstring", spy)
    _xml_cutoff_token("<root><child>text</child></root>")
    assert call_count["n"] == 0


def test_xml_cutoff_token_stable_under_whitespace_reformat() -> None:
    a = _xml_cutoff_token("<root><child>text</child></root>")
    b = _xml_cutoff_token("<root>\n  <child>text</child>\n</root>\n")
    assert a == b


def test_xml_cutoff_token_differs_on_tag_rename() -> None:
    a = _xml_cutoff_token("<root><child>text</child></root>")
    b = _xml_cutoff_token("<root><renamed>text</renamed></root>")
    assert a != b


def test_xml_normalize_preserves_attribute_whitespace() -> None:
    # Attribute values (inside double quotes) must not be whitespace-collapsed.
    normalized = _normalize_xml_text('<root attr="a  b  c"/>')
    assert 'attr="a  b  c"' in normalized
