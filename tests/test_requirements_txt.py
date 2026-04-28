from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

import pyinc.integrations as integrations
from pyinc import Database
from pyinc.integrations.requirements_txt import (
    RequirementsAnalysis,
    deep_requirements_analysis,
    requirements_analysis,
    workspace_requirements_analysis,
)

Operation = tuple[Literal["write", "delete"], str, str | None]

_MINIMAL_REQUIREMENTS = """\
# Core dependencies
requests>=2.28,<3.0
click==8.1.7
flask[async]>=2.3
numpy  # numerical computing

# Platform-specific
pywin32; sys_platform == "win32"

# Editable
-e .

# References
-r dev-requirements.txt
-c constraints.txt

# Index
--index-url https://pypi.org/simple/
--extra-index-url https://internal.example.com/simple/
--find-links /local/wheels
"""


# ---------------------------------------------------------------------------
# Contract lock
# ---------------------------------------------------------------------------


def test_package_namespace_exports_requirements_txt_stable_api() -> None:
    assert "RequirementRef" in integrations.__all__
    assert "RequirementsAnalysis" in integrations.__all__
    assert "FileReference" in integrations.__all__
    assert "IndexDirective" in integrations.__all__
    assert "requirements_analysis" in integrations.__all__
    assert "workspace_requirements_analysis" in integrations.__all__
    assert hasattr(integrations, "requirements_analysis")
    assert hasattr(integrations, "workspace_requirements_analysis")
    assert hasattr(integrations, "RequirementsAnalysis")
    # Experimental helpers must not leak.
    assert not hasattr(integrations, "requirements_file_text")
    assert not hasattr(integrations, "requirements_payload")
    assert not hasattr(integrations, "requirements_analysis_payload")
    assert not hasattr(integrations, "file_references_payload")
    assert not hasattr(integrations, "index_directives_payload")
    assert not hasattr(integrations, "requirements_diagnostics_payload")


# ---------------------------------------------------------------------------
# Mode-parametrized correctness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_requirements_analysis_extracts_packages(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(_MINIMAL_REQUIREMENTS, encoding="utf-8")

    db = Database(mode=mode)
    result = requirements_analysis(db, str(path))

    assert isinstance(result, RequirementsAnalysis)
    assert result.path == str(path)

    names = {r.name for r in result.requirements}
    assert "requests" in names
    assert "click" in names
    assert "flask" in names
    assert "numpy" in names
    assert "pywin32" in names

    assert len(result.file_references) == 2
    assert len(result.index_directives) == 3
    assert result.diagnostics == ()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_requirements_analysis_reports_diagnostics_for_unparseable_lines(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("requests>=2.0\n!!! bad line !!!\nclick\n", encoding="utf-8")

    db = Database(mode=mode)
    result = requirements_analysis(db, str(path))

    assert len(result.requirements) == 2
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0][0] == "unparseable-line"


# ---------------------------------------------------------------------------
# Specific correctness
# ---------------------------------------------------------------------------


def test_requirements_analysis_parses_version_specifiers(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(
        "alpha>=2.0,<3.0\nbeta==1.0.0\ngamma~=1.4.0\ndelta!=2.0\n",
        encoding="utf-8",
    )

    db = Database()
    result = requirements_analysis(db, str(path))

    by_name = {r.name: r for r in result.requirements}
    assert by_name["alpha"].version_spec == ">=2.0,<3.0"
    assert by_name["beta"].version_spec == "==1.0.0"
    assert by_name["gamma"].version_spec == "~=1.4.0"
    assert by_name["delta"].version_spec == "!=2.0"


def test_requirements_analysis_parses_extras(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("requests[security,socks]>=2.0\n", encoding="utf-8")

    db = Database()
    result = requirements_analysis(db, str(path))

    assert len(result.requirements) == 1
    req = result.requirements[0]
    assert req.name == "requests"
    assert req.extras == ("security", "socks")
    assert req.version_spec == ">=2.0"


def test_requirements_analysis_parses_environment_markers(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text('pywin32; sys_platform == "win32"\n', encoding="utf-8")

    db = Database()
    result = requirements_analysis(db, str(path))

    assert len(result.requirements) == 1
    req = result.requirements[0]
    assert req.name == "pywin32"
    assert req.markers == 'sys_platform == "win32"'


def test_requirements_analysis_parses_editable_installs(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(
        "-e .\n-e git+https://github.com/example/pkg.git\n", encoding="utf-8"
    )

    db = Database()
    result = requirements_analysis(db, str(path))

    assert len(result.requirements) == 2
    assert all(r.is_editable for r in result.requirements)


def test_requirements_analysis_parses_file_references(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(
        "-r other.txt\n--requirement base.txt\n-c constraints.txt\n--constraint pins.txt\n",
        encoding="utf-8",
    )

    db = Database()
    result = requirements_analysis(db, str(path))

    assert len(result.file_references) == 4
    kinds = [r.kind for r in result.file_references]
    assert kinds.count("requirement") == 2
    assert kinds.count("constraint") == 2
    paths = [r.path for r in result.file_references]
    assert "other.txt" in paths
    assert "base.txt" in paths
    assert "constraints.txt" in paths
    assert "pins.txt" in paths


def test_requirements_analysis_parses_index_directives(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(
        "--index-url https://pypi.org/simple/\n"
        "--extra-index-url https://internal.example.com/simple/\n"
        "-f /local/wheels\n"
        "--find-links https://download.example.com/\n",
        encoding="utf-8",
    )

    db = Database()
    result = requirements_analysis(db, str(path))

    assert len(result.index_directives) == 4
    kinds = [d.kind for d in result.index_directives]
    assert "index-url" in kinds
    assert "extra-index-url" in kinds
    assert kinds.count("find-links") == 2


def test_requirements_analysis_handles_line_continuations(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(
        "requests\\\n>=2.0\nclick\n",
        encoding="utf-8",
    )

    db = Database()
    result = requirements_analysis(db, str(path))

    by_name = {r.name: r for r in result.requirements}
    assert "requests" in by_name
    assert by_name["requests"].version_spec == ">=2.0"
    assert "click" in by_name


def test_requirements_analysis_handles_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("", encoding="utf-8")

    db = Database()
    result = requirements_analysis(db, str(path))

    assert result.requirements == ()
    assert result.file_references == ()
    assert result.index_directives == ()
    assert result.diagnostics == ()


def test_requirements_analysis_handles_comments_only(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("# Just a comment\n# Another comment\n\n", encoding="utf-8")

    db = Database()
    result = requirements_analysis(db, str(path))

    assert result.requirements == ()
    assert result.diagnostics == ()


def test_requirements_analysis_on_nonexistent_file(tmp_path: Path) -> None:
    path = tmp_path / "nonexistent.txt"

    db = Database()
    result = requirements_analysis(db, str(path))

    assert result.requirements == ()
    assert result.file_references == ()
    assert result.index_directives == ()
    assert result.diagnostics == ()


def test_requirements_analysis_normalizes_package_names(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(
        "Requests>=2.0\nmy-package==1.0\nAnother.Pkg>=3.0\n", encoding="utf-8"
    )

    db = Database()
    result = requirements_analysis(db, str(path))

    names = {r.name for r in result.requirements}
    assert "requests" in names
    assert "my_package" in names
    assert "another_pkg" in names


def test_requirements_analysis_parses_url_requirements(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text(
        "mypackage @ https://example.com/mypackage-1.0.tar.gz\n",
        encoding="utf-8",
    )

    db = Database()
    result = requirements_analysis(db, str(path))

    assert len(result.requirements) == 1
    req = result.requirements[0]
    assert req.name == "mypackage"
    assert "https://example.com/mypackage-1.0.tar.gz" in req.version_spec


# ---------------------------------------------------------------------------
# Cutoff / backdating
# ---------------------------------------------------------------------------


def test_comment_text_edit_backdates_requirements(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("# Original comment\nrequests>=2.0\nclick\n", encoding="utf-8")

    db = Database()
    first = requirements_analysis(db, str(path))

    # Change comment wording — same line count, same requirement positions.
    path.write_text("# Different comment\nrequests>=2.0\nclick\n", encoding="utf-8")
    second = requirements_analysis(db, str(path))

    assert first == second


def test_semantic_edit_invalidates_downstream_requirements(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("requests>=2.0\nclick\n", encoding="utf-8")

    db = Database()
    first = requirements_analysis(db, str(path))
    assert len(first.requirements) == 2

    # Change a dependency — semantic edit.
    path.write_text("httpx>=0.24\nclick\n", encoding="utf-8")
    second = requirements_analysis(db, str(path))

    names = {r.name for r in second.requirements}
    assert "httpx" in names
    assert "requests" not in names
    assert first != second


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_workspace_requirements_analysis_discovers_requirements_txt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    reqs = root / "requirements.txt"
    reqs.write_text("requests>=2.0\n", encoding="utf-8")

    db = Database()
    result = workspace_requirements_analysis(db, str(root))
    assert result is not None
    assert isinstance(result, RequirementsAnalysis)
    assert len(result.requirements) == 1


def test_workspace_requirements_analysis_returns_none_when_missing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "empty"
    root.mkdir()

    db = Database()
    result = workspace_requirements_analysis(db, str(root))
    assert result is None


# ---------------------------------------------------------------------------
# From-scratch oracle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_requirements_analysis_matches_fresh_recomputation_over_changes(
    mode: str, tmp_path: Path
) -> None:
    path = tmp_path / "requirements.txt"
    steps: tuple[tuple[str, str], ...] = (
        ("initial", "# deps\nrequests>=2.0\nclick\n"),
        ("change comment", "# updated deps\nrequests>=2.0\nclick\n"),
        ("change dep", "# updated deps\nhttpx>=0.24\nclick\n"),
        ("add extras", "httpx[http2]>=0.24\nclick\nflask[async]\n"),
        ("remove line", "click\nflask[async]\n"),
    )

    incremental = Database(mode=mode)
    for _label, content in steps:
        path.write_text(content, encoding="utf-8")
        fresh = Database(mode=mode)
        assert requirements_analysis(incremental, str(path)) == requirements_analysis(
            fresh, str(path)
        )


# ---------------------------------------------------------------------------
# deep_requirements_analysis
# ---------------------------------------------------------------------------


def test_deep_single_file_matches_shallow(tmp_path: Path) -> None:
    req_file = tmp_path / "requirements.txt"
    req_file.write_text("requests>=2.0\nflask\n")
    db = Database()
    shallow = requirements_analysis(db, str(req_file))
    deep = deep_requirements_analysis(db, str(req_file))
    assert len(deep.requirements) == len(shallow.requirements)
    names = {r.name for r in deep.requirements}
    assert names == {"requests", "flask"}


def test_deep_two_level_chain(tmp_path: Path) -> None:
    base = tmp_path / "base.txt"
    base.write_text("numpy>=1.20\n")
    main = tmp_path / "requirements.txt"
    main.write_text("-r base.txt\npandas>=1.0\n")
    db = Database()
    result = deep_requirements_analysis(db, str(main))
    names = {r.name for r in result.requirements}
    assert names == {"numpy", "pandas"}


def test_deep_three_level_chain(tmp_path: Path) -> None:
    core = tmp_path / "core.txt"
    core.write_text("click\n")
    base = tmp_path / "base.txt"
    base.write_text("-r core.txt\nflask\n")
    main = tmp_path / "requirements.txt"
    main.write_text("-r base.txt\ngunicorn\n")
    db = Database()
    result = deep_requirements_analysis(db, str(main))
    names = {r.name for r in result.requirements}
    assert names == {"click", "flask", "gunicorn"}


def test_deep_circular_reference_produces_diagnostic(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("-r b.txt\nrequests\n")
    b.write_text("-r a.txt\nflask\n")
    db = Database()
    result = deep_requirements_analysis(db, str(a))
    cycle_diagnostics = [d for d in result.diagnostics if d[0] == "cycle"]
    assert len(cycle_diagnostics) >= 1
    names = {r.name for r in result.requirements}
    assert "requests" in names
    assert "flask" in names


def test_deep_duplicate_dedup_last_wins(tmp_path: Path) -> None:
    base = tmp_path / "base.txt"
    base.write_text("requests>=1.0\n")
    main = tmp_path / "requirements.txt"
    main.write_text("-r base.txt\nrequests>=2.0\n")
    db = Database()
    result = deep_requirements_analysis(db, str(main))
    req_names = [r.name for r in result.requirements]
    assert req_names.count("requests") == 1
    req = [r for r in result.requirements if r.name == "requests"][0]
    assert "2.0" in req.version_spec


def test_deep_relative_path_resolution(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    leaf = sub / "leaf.txt"
    leaf.write_text("boto3\n")
    main = tmp_path / "requirements.txt"
    main.write_text("-r sub/leaf.txt\ndjango\n")
    db = Database()
    result = deep_requirements_analysis(db, str(main))
    names = {r.name for r in result.requirements}
    assert names == {"boto3", "django"}


def test_deep_constraint_not_followed(tmp_path: Path) -> None:
    constraints = tmp_path / "constraints.txt"
    constraints.write_text("requests<3.0\n")
    main = tmp_path / "requirements.txt"
    main.write_text("-c constraints.txt\nrequests\n")
    db = Database()
    result = deep_requirements_analysis(db, str(main))
    names = {r.name for r in result.requirements}
    assert names == {"requests"}
    assert len(result.file_references) == 1
    assert result.file_references[0].kind == "constraint"


def test_deep_missing_referenced_file(tmp_path: Path) -> None:
    main = tmp_path / "requirements.txt"
    main.write_text("-r nonexistent.txt\nflask\n")
    db = Database()
    result = deep_requirements_analysis(db, str(main))
    assert "flask" in {r.name for r in result.requirements}


def test_deep_incremental_revalidation(tmp_path: Path) -> None:
    base = tmp_path / "base.txt"
    base.write_text("numpy>=1.0\n")
    main = tmp_path / "requirements.txt"
    main.write_text("-r base.txt\npandas\n")
    db = Database()

    result1 = deep_requirements_analysis(db, str(main))
    assert {r.name for r in result1.requirements} == {"numpy", "pandas"}

    base.write_text("numpy>=2.0\nscipy\n")
    result2 = deep_requirements_analysis(db, str(main))
    names = {r.name for r in result2.requirements}
    assert names == {"numpy", "scipy", "pandas"}
