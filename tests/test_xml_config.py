from __future__ import annotations

from pathlib import Path

import pytest

import pyinc.integrations as integrations
from pyinc import Database
from pyinc.integrations.xml_config import (
    XmlAnalysis,
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


def test_xml_analysis_rejects_billion_laughs_payload(tmp_path: Path) -> None:
    import time

    path = tmp_path / "evil.xml"
    path.write_text(_BILLION_LAUGHS, encoding="utf-8")

    start = time.monotonic()
    result = xml_analysis(Database(), str(path))
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"parse took {elapsed:.2f}s; billion-laughs not bounded"
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
