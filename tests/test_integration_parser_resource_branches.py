from __future__ import annotations

import hashlib
import importlib
import json
import os
from collections.abc import Callable
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from pyinc import Database
from pyinc.value import freeze

csv_data: Any = importlib.import_module("pyinc.integrations.csv_data")
deep_resolution: Any = importlib.import_module("pyinc.integrations.deep_module_resolution")
dependency_check: Any = importlib.import_module("pyinc.integrations.dependency_check")
env_file: Any = importlib.import_module("pyinc.integrations.env_file")
installed_packages: Any = importlib.import_module("pyinc.integrations.installed_packages")
json_config: Any = importlib.import_module("pyinc.integrations.json_config")
notebook: Any = importlib.import_module("pyinc.integrations.notebook")
python_source: Any = importlib.import_module("pyinc.integrations.python_source")
requirements_txt: Any = importlib.import_module("pyinc.integrations.requirements_txt")
toml_config: Any = importlib.import_module("pyinc.integrations.toml_config")
xml_config: Any = importlib.import_module("pyinc.integrations.xml_config")


@pytest.mark.parametrize(
    ("resource", "contents"),
    (
        (csv_data._CsvFileResource(), "name,value\na,1\n"),
        (deep_resolution._PthFileResource(), "../extra\n"),
        (env_file._EnvFileResource(), "NAME=value\n"),
        (installed_packages._DistInfoMetadataResource(), "Name: example\nVersion: 1\n"),
        (json_config._JsonFileResource(), '{"name": "example"}\n'),
        (notebook._NotebookFileResource(), '{"cells": [], "metadata": {}}\n'),
        (requirements_txt._RequirementsFileResource(), "example>=1\n"),
        (toml_config._ConfigFileResource(), '[project]\nname = "example"\n'),
        (xml_config._XmlFileResource(), "<project />\n"),
    ),
    ids=("csv", "pth", "env", "metadata", "json", "notebook", "requirements", "toml", "xml"),
)
def test_file_resources_support_independent_probe_and_load(
    tmp_path: Path,
    resource: Any,
    contents: str,
) -> None:
    path = tmp_path / "resource.txt"
    missing = os.fspath(path)

    assert resource.probe(missing) == ("missing",)
    assert resource.load(Database(), missing) == ""

    path.write_bytes(contents.encode("utf-8"))
    assert resource.probe(missing) == (
        "present",
        hashlib.sha256(contents.encode()).hexdigest(),
    )
    assert resource.load(Database(), missing) == contents


@pytest.mark.parametrize(
    ("filename", "contents", "analyze"),
    (
        ("data.csv", "name,value\na,1\n", csv_data.workspace_csv_analysis),
        (".env", "NAME=value\n", env_file.workspace_env_analysis),
        ("package.json", "{}\n", json_config.workspace_json_analysis),
        ("requirements.txt", "example>=1\n", requirements_txt.workspace_requirements_analysis),
        ("pyproject.toml", "[project]\n", toml_config.workspace_config_analysis),
        ("pom.xml", "<project />\n", xml_config.workspace_xml_analysis),
    ),
    ids=("csv", "env", "json", "requirements", "toml", "xml"),
)
def test_workspace_scans_past_unrelated_entries(
    tmp_path: Path,
    filename: str,
    contents: str,
    analyze: Any,
) -> None:
    (tmp_path / "!unrelated").write_text("noise", encoding="utf-8")
    (tmp_path / filename).write_text(contents, encoding="utf-8")

    assert analyze(Database(), tmp_path) is not None


def test_csv_empty_input_has_an_empty_schema() -> None:
    assert csv_data._parse_csv(" \n\t") == ([], 0, ",", False, [])


def test_env_parser_preserves_unterminated_quoted_values() -> None:
    entries, diagnostics = env_file._parse_env_lines("DOUBLE=\"unterminated\nSINGLE='unterminated")

    assert diagnostics == []
    assert [(entry[0], entry[1], entry[2]) for entry in entries] == [
        ("DOUBLE", '"unterminated', False),
        ("SINGLE", "'unterminated", False),
    ]


def test_json_helpers_cover_raw_cutoff_and_nonstandard_values() -> None:
    assert json_config._json_cutoff_token("{") == ("raw", "{")
    assert json_config._json_value_type({"nested": True}) == "object"
    assert json_config._json_value_type((1, 2)) == "unknown"
    assert json_config._json_value_to_string({"b": 2, "a": 1}) == "[('a', 1), ('b', 2)]"


def test_toml_helpers_cover_value_kinds_and_shape_edges() -> None:
    timestamp = datetime(2025, 1, 2, 3, 4, 5)
    calendar_date = date(2025, 1, 2)
    clock_time = time(3, 4, 5)

    assert toml_config._toml_value_type(1.5) == "float"
    assert toml_config._toml_value_type({"key": "value"}) == "table"
    assert toml_config._toml_value_type(object()) == "unknown"
    assert toml_config._toml_value_to_string({"b": 2, "a": 1}) == "[('a', 1), ('b', 2)]"
    assert toml_config._config_cutoff_token("invalid = [") == ("raw", "invalid = [")
    assert toml_config._toml_cutoff_value(timestamp) == ("datetime", timestamp.isoformat())
    assert toml_config._toml_cutoff_value(calendar_date) == ("date", calendar_date.isoformat())
    assert toml_config._toml_cutoff_value(clock_time) == ("time", clock_time.isoformat())

    diagnostics = toml_config._config_shape_diagnostics(
        {"project": {"optional-dependencies": "invalid"}}
    )
    assert diagnostics == (
        ("invalid-optional-dependencies", "project.optional-dependencies must be a TOML table"),
    )
    assert toml_config._config_shape_diagnostics({"project": {"optional-dependencies": {}}}) == ()


def test_xml_namespace_strip_tolerates_an_unclosed_clark_prefix() -> None:
    assert xml_config._strip_namespace("{unclosed") == "{unclosed"


def test_dependency_helpers_cover_ambiguous_versions_and_unusual_specs() -> None:
    status, detail = dependency_check._check_version_constraints("not a spec", "1.0")
    assert status == "ambiguous"
    assert "cannot parse" in detail

    status, detail = dependency_check._check_version_constraints(">=1", "not-a-version")
    assert status == "ambiguous"
    assert "unparseable" in detail

    assert dependency_check._extract_dep_name_and_spec(
        "Example_Pkg @ https://example.invalid/archive.whl"
    ) == ("example-pkg", "")
    assert dependency_check._extract_dep_name_and_spec("!!!") == ("!!!", "")
    assert dependency_check._extract_dep_name_and_spec("!!! @ https://example.invalid") == (
        "!!! @ https://example-invalid",
        "",
    )
    assert dependency_check._extract_dep_name_and_spec("example[broken>=1") == (
        "example",
        "[broken>=1",
    )


def test_workspace_dependency_check_skips_noninstalled_resolutions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = dependency_check.DependencyCheckAnalysis(
        statuses=(),
        undeclared_imports=(),
        diagnostics=(),
    )
    imports = (
        SimpleNamespace(module="local", resolution="local", distribution_name=None),
        SimpleNamespace(module="unknown", resolution="installed", distribution_name=None),
        SimpleNamespace(module="external", resolution="installed", distribution_name="External"),
    )
    workspace = SimpleNamespace(modules=(SimpleNamespace(resolved_imports=imports),))
    python_source: Any = importlib.import_module("pyinc.integrations.python_source")
    monkeypatch.setattr(dependency_check, "dependency_check_analysis", lambda *_args: base)
    monkeypatch.setattr(dependency_check, "environment_index", lambda _db: freeze(((), ())))
    monkeypatch.setattr(python_source, "workspace_analysis", lambda *_args: workspace)

    result = dependency_check.workspace_dependency_check(
        cast(Database, object()),
        "workspace",
        (),
    )

    assert tuple(item.distribution_name for item in result.undeclared_imports) == ("External",)


def test_site_package_resource(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    existing = tmp_path / "site-packages"
    existing.mkdir()
    monkeypatch.setattr(
        installed_packages,
        "_get_site_packages_dirs",
        lambda: (os.fspath(existing),),
    )
    resource = installed_packages._SitePackagesResource()
    assert resource.probe("python") == (os.fspath(existing),)
    assert resource.load(Database(), "python") == (os.fspath(existing),)


def test_site_package_discovery_ignores_duplicates_missing_paths_and_nonstring_user_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = tmp_path / "site-packages"
    existing.mkdir()
    missing = tmp_path / "missing"

    monkeypatch.setattr(
        installed_packages.site,
        "getsitepackages",
        lambda: [os.fspath(existing), os.fspath(existing), os.fspath(missing)],
    )
    monkeypatch.setattr(
        installed_packages.site,
        "getusersitepackages",
        lambda: (os.fspath(existing),),
    )
    assert installed_packages._get_site_packages_dirs() == (os.fspath(existing),)


def test_empty_distribution_metadata_produces_a_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    dist_info = site_dir / "empty-1.0.dist-info"
    dist_info.mkdir(parents=True)
    monkeypatch.setattr(
        installed_packages,
        "_get_site_packages_dirs",
        lambda: (os.fspath(site_dir),),
    )

    analysis = installed_packages.installed_packages_analysis(Database())

    assert analysis.packages == ()
    assert analysis.diagnostics[0][0] == "metadata-parse-failed"


def test_import_resolution_checks_later_installed_packages() -> None:
    payload = (
        (
            ("first", "1", ("first",), (), ""),
            ("second", "2", ("second",), (), ""),
        ),
        (),
        (),
    )

    class FakeDatabase:
        def get(self, *_args: object, **_kwargs: object) -> object:
            return freeze(payload)

    result = installed_packages.resolve_import_name(cast(Database, FakeDatabase()), "second.child")

    assert result.origin == "installed"
    assert result.distribution_name == "second"


def test_notebook_metadata_payload_handles_malformed_shapes_and_metadata(
    tmp_path: Path,
) -> None:
    db = Database()

    def metadata_of(name: str, text: str) -> Any:
        path = tmp_path / f"{name}.ipynb"
        path.write_text(text, encoding="utf-8")
        return notebook.notebook_metadata_payload(db, os.fspath(path))

    # Undecodable text never becomes a document: `_try_parse_notebook` answers
    # `None` and the payload returns before it looks at any metadata.
    assert metadata_of("not_json", "not json") == (None, None)

    # A `cells` field that is not a list still decodes to a document, so this
    # one reaches the same answer by the other route: the metadata block runs
    # and the empty `metadata` object yields neither a kernel nor a language.
    assert metadata_of("cells_not_list", json.dumps({"cells": {}, "metadata": {}})) == (
        None,
        None,
    )

    # A `metadata` field that is not an object short-circuits before either
    # holder is consulted.
    invalid_metadata = json.dumps({"cells": [None], "metadata": "invalid"})
    assert metadata_of("invalid_metadata", invalid_metadata) == (None, None)

    # A non-string `kernelspec.name` is ignored; `kernelspec.language` is taken.
    kernelspec_language = json.dumps(
        {
            "cells": [],
            "metadata": {"kernelspec": {"name": 7, "language": "R"}},
        }
    )
    assert metadata_of("kernelspec_language", kernelspec_language) == (None, "R")

    # A non-string `kernelspec.language` falls through to `language_info`.
    language_info = json.dumps(
        {
            "cells": [],
            "metadata": {
                "kernelspec": {"language": 7},
                "language_info": {"name": "python"},
            },
        }
    )
    assert metadata_of("language_info", language_info) == (None, "python")

    # Neither holder yields a string: `kernelspec` is not an object at all and
    # `language_info.name` is a number.
    nonstring_language_info = json.dumps(
        {
            "cells": [],
            "metadata": {
                "kernelspec": [],
                "language_info": {"name": 7},
            },
        }
    )
    assert metadata_of("nonstring_language_info", nonstring_language_info) == (None, None)

    # A `language_info` that is present but not an object is passed over the
    # same way an absent one is.
    invalid_language_info = json.dumps({"cells": [], "metadata": {"language_info": "invalid"}})
    assert metadata_of("invalid_language_info", invalid_language_info) == (None, None)


def test_notebook_payload_queries_reject_invalid_container_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cells_not_list = json.dumps({"cells": {}, "metadata": {}})
    monkeypatch.setattr(notebook, "notebook_text", lambda *_args: cells_not_list)
    assert notebook.notebook_cells_payload.fn(cast(Database, object()), "notebook") == ()
    diagnostics = notebook.notebook_diagnostics_payload.fn(
        cast(Database, object()),
        "notebook",
    )
    assert diagnostics[0][0] == "notebook-shape-error"

    metadata_not_object = json.dumps({"cells": [], "metadata": []})
    monkeypatch.setattr(notebook, "notebook_text", lambda *_args: metadata_not_object)
    assert notebook.notebook_metadata_payload.fn(cast(Database, object()), "notebook") == (
        None,
        None,
    )


def test_notebook_metadata_payload_uses_language_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = json.dumps(
        {
            "cells": [],
            "metadata": {
                "kernelspec": {"name": "kernel", "language": 7},
                "language_info": {"name": "python"},
            },
        }
    )
    monkeypatch.setattr(notebook, "notebook_text", lambda *_args: value)

    assert notebook.notebook_metadata_payload.fn(cast(Database, object()), "notebook") == (
        "kernel",
        "python",
    )

    nonstring_names = json.dumps(
        {
            "cells": [],
            "metadata": {
                "kernelspec": {"name": 7, "language": 7},
                "language_info": {"name": 7},
            },
        }
    )
    monkeypatch.setattr(notebook, "notebook_text", lambda *_args: nonstring_names)
    assert notebook.notebook_metadata_payload.fn(cast(Database, object()), "notebook") == (
        None,
        None,
    )


def test_notebook_source_and_diagnostic_range_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert notebook._coerce_source(42) == ""
    assert notebook._syntax_error_range("valid = True\n") is None

    def raise_without_position(_source: str) -> object:
        raise SyntaxError("missing location")

    monkeypatch.setattr(notebook.ast, "parse", raise_without_position)
    assert notebook._syntax_error_range("invalid") is None

    positioned = SyntaxError("bad", ("cell", 1, 2, "x", 1, 2))

    def raise_zero_width(_source: str) -> object:
        raise positioned

    monkeypatch.setattr(notebook.ast, "parse", raise_zero_width)
    source_range = notebook._syntax_error_range("invalid")
    assert source_range is not None
    assert source_range.end.character == source_range.start.character + 1

    syntax_diagnostic = notebook._decode_diagnostic(
        ("syntax-error", "bad", 99),
        (),
        "",
    )
    decode_diagnostic = notebook._decode_diagnostic(
        ("notebook-decode-error", "bad", None),
        (),
        "{}",
    )
    assert syntax_diagnostic.range is None
    assert decode_diagnostic.range is None


def test_notebook_workspace_rejects_a_regular_file(tmp_path: Path) -> None:
    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("content", encoding="utf-8")

    assert notebook.workspace_notebook_analysis(Database(), regular_file) == ()


def test_deep_resolution_filters_invalid_sys_path_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    monkeypatch.setattr(
        deep_resolution.sys,
        "path",
        [None, "", os.fspath(site), os.fspath(site), os.fspath(tmp_path / "missing")],
    )

    assert deep_resolution._get_sys_path_entries() == (os.fspath(site),)


def test_deep_resolution_treats_files_as_missing_directories(tmp_path: Path) -> None:
    regular_file = tmp_path / "plain-file"
    regular_file.write_text("content", encoding="utf-8")
    db = Database()

    assert not deep_resolution._directory_exists(db, os.fspath(regular_file))
    assert deep_resolution._pth_listing.fn(db, os.fspath(regular_file)) == ()


def test_effective_search_paths_deduplicate_and_ignore_missing_pth_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    query = deep_resolution._effective_search_paths_payload
    monkeypatch.setattr(
        deep_resolution,
        "_raw_sys_path_entries",
        lambda _db: (os.fspath(site), os.fspath(site)),
    )
    monkeypatch.setattr(deep_resolution, "_pth_listing", lambda *_args: ("extra.pth",))
    monkeypatch.setattr(
        deep_resolution,
        "_pth_directives_payload",
        lambda *_args: ((os.fspath(tmp_path / "missing"),), ()),
    )
    monkeypatch.setattr(deep_resolution, "_directory_exists", lambda *_args: False)

    assert query.fn(Database()) == ((os.fspath(site), "sys.path"),)


def test_deep_resolution_skips_an_already_visited_candidate(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    module_path = site / "module.py"
    module_path.write_text("", encoding="utf-8")
    db = Database()
    visited = {deep_resolution._canonical_path(db, os.fspath(module_path))}

    assert deep_resolution._descend(
        db,
        (os.fspath(site),),
        "module",
        visited=visited,
    ) == ([], None, None)
    assert deep_resolution.resolve_module_location.fn(Database(), "") == (
        "",
        "missing",
        None,
        None,
        (),
        None,
        None,
    )


def test_namespace_discovery_ignores_unsafe_candidates_and_regular_shadows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / ".hidden").mkdir()
    (first / "plain").write_text("not a directory", encoding="utf-8")
    (first / "shared").mkdir()
    (second / "shared").mkdir()
    (second / "shared" / "__init__.py").write_text("", encoding="utf-8")
    query = deep_resolution._top_level_namespace_packages_payload
    monkeypatch.setattr(
        deep_resolution,
        "_effective_search_paths_payload",
        lambda _db: ((os.fspath(first), "sys.path"), (os.fspath(second), "sys.path")),
    )

    assert query.fn(Database()) == ()

    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        deep_resolution,
        "_effective_search_paths_payload",
        lambda _db: ((os.fspath(regular_file), "sys.path"),),
    )
    assert query.fn(Database()) == ()


def test_requirements_parser_defensive_branches() -> None:
    assert requirements_txt._parse_requirement_line("   ", 1) is None
    assert requirements_txt._parse_file_references("-c bad\0path") == ()
    assert requirements_txt._requirements_cutoff_token(" \n# comment\n") == ("", "#")

    source_range = requirements_txt._range_for_line({}, 0)
    assert source_range.start.line == 0
    assert source_range.start == source_range.end


def test_deep_requirements_reports_cycles_duplicates_and_escape(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    root = project / "requirements.txt"
    first = project / "first.txt"
    second = project / "second.txt"
    shared = project / "shared.txt"
    absolute = project / "absolute.txt"
    outside = tmp_path / "outside.txt"

    root.write_text(
        f"-r first.txt\n-r second.txt\n-r {absolute}\n-r ../outside.txt\nroot-pkg\n",
        encoding="utf-8",
    )
    first.write_text(f"-r {root}\n-r shared.txt\nfirst-pkg\n", encoding="utf-8")
    second.write_text("-r shared.txt\nsecond-pkg\n", encoding="utf-8")
    shared.write_text("shared-pkg\n", encoding="utf-8")
    absolute.write_text("absolute-pkg\n", encoding="utf-8")
    outside.write_text("outside-pkg\n", encoding="utf-8")

    analysis = requirements_txt.deep_requirements_analysis(Database(), root)

    assert {requirement.name for requirement in analysis.requirements} == {
        "absolute_pkg",
        "first_pkg",
        "root_pkg",
        "second_pkg",
        "shared_pkg",
    }
    assert any(code == "cycle" for code, _message in analysis.diagnostics)
    assert any("outside project" in message for _code, message in analysis.diagnostics)


def _denied(self: Path, *args: Any, **kwargs: Any) -> Any:
    raise PermissionError(13, "Permission denied", str(self))


def _denying_open(*targets: str) -> Callable[..., int]:
    """Refuse to open exactly ``targets``, the way an ACL denial does.

    A tracked read opens a descriptor and asks it what kind of thing it got, so
    a denial has to arrive at the open to be the denial the read meets. Every
    other path opens normally, including the ones pytest itself needs.
    """

    real_open = os.open

    def opener(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if str(path) in targets:
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    return opener


def test_shared_file_helpers_read_a_denied_directory_as_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Windows refuses to open a directory as a file with EACCES where POSIX
    # raises IsADirectoryError, and an ACL denial on a regular file raises the
    # same thing, so only the kind of the path separates them. This drives that
    # shape on any platform.
    resources: Any = importlib.import_module("pyinc.integrations._resources")
    directory = tmp_path / "holder"
    directory.mkdir()
    regular = tmp_path / "thing.txt"
    regular.write_text("hello", encoding="utf-8")

    monkeypatch.setattr(Path, "read_bytes", _denied)
    monkeypatch.setattr(Path, "read_text", _denied)
    monkeypatch.setattr(os, "open", _denying_open(str(directory), str(regular)))

    assert resources.file_bytes(str(directory)) is None
    assert resources.file_probe(str(directory)) == ("missing",)
    assert resources.file_text(str(directory), "utf-8") is None
    assert resources.file_read_snapshot(str(directory), "utf-8") == (("missing",), None)

    with pytest.raises(PermissionError):
        resources.file_bytes(str(regular))
    with pytest.raises(PermissionError):
        resources.file_probe(str(regular))
    with pytest.raises(PermissionError):
        resources.file_text(str(regular), "utf-8")
    with pytest.raises(PermissionError):
        resources.file_read_snapshot(str(regular), "utf-8")


@pytest.mark.parametrize(
    "resource",
    (
        csv_data._CsvFileResource(),
        deep_resolution._PthFileResource(),
        env_file._EnvFileResource(),
        installed_packages._DistInfoMetadataResource(),
        json_config._JsonFileResource(),
        notebook._NotebookFileResource(),
        requirements_txt._RequirementsFileResource(),
        toml_config._ConfigFileResource(),
        xml_config._XmlFileResource(),
        python_source._SourceTextResource(),
    ),
    ids=(
        "csv",
        "pth",
        "env",
        "metadata",
        "json",
        "notebook",
        "requirements",
        "toml",
        "xml",
        "source",
    ),
)
def test_shipped_file_resources_read_a_denied_directory_as_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    resource: Any,
) -> None:
    directory = tmp_path / "holder"
    directory.mkdir()
    regular = tmp_path / "thing.txt"
    regular.write_text("hello", encoding="utf-8")

    # Built before the denial: constructing a database fingerprints the kernel's
    # own adapters, which reads the kernel's own source, and these hooks deny
    # every read there is. The database is only the argument the hooks take --
    # none of them reaches it -- so building it first changes nothing under test.
    db = Database()
    monkeypatch.setattr(Path, "read_bytes", _denied)
    monkeypatch.setattr(Path, "read_text", _denied)
    monkeypatch.setattr(os, "open", _denying_open(str(directory), str(regular)))

    assert resource.probe(str(directory)) == ("missing",)
    assert resource.probe_and_load(db, str(directory))[0] == ("missing",)

    with pytest.raises(PermissionError):
        resource.probe(str(regular))
    with pytest.raises(PermissionError):
        resource.load(db, str(regular))
