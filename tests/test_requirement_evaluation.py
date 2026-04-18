from __future__ import annotations

from pathlib import Path

import pytest

import pyinc.integrations as integrations
from pyinc import Database
from pyinc.integrations import requirement_evaluation
from pyinc.integrations.requirement_evaluation import (
    ApplicableRequirement,
    ApplicableRequirementsAnalysis,
    MarkerEvaluation,
    PythonEnvironmentSnapshot,
    VersionSpecifierEvaluation,
    applicable_requirements,
    evaluate_markers,
    evaluate_version_specifier,
    workspace_applicable_requirements,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dist_info(
    site_dir: Path,
    name: str,
    version: str,
    *,
    top_level: str | None = None,
) -> Path:
    dist_info = site_dir / f"{name}-{version}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nSummary: test\n",
        encoding="utf-8",
    )
    if top_level is not None:
        (dist_info / "top_level.txt").write_text(top_level + "\n", encoding="utf-8")
    return dist_info


def _patch_site(monkeypatch: pytest.MonkeyPatch, site_dir: Path) -> None:
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )


def _fixed_env(
    *,
    python_version: str = "3.12",
    python_full_version: str = "3.12.3",
    os_name: str = "posix",
    sys_platform: str = "linux",
    platform_system: str = "Linux",
    platform_release: str = "6.0",
    platform_machine: str = "x86_64",
    implementation_name: str = "cpython",
    implementation_version: str = "3.12.3",
    platform_python_implementation: str = "CPython",
    platform_version: str = "#1 SMP",
) -> tuple[str, ...]:
    return (
        python_version,
        python_full_version,
        implementation_name,
        implementation_version,
        os_name,
        sys_platform,
        platform_system,
        platform_release,
        platform_machine,
        platform_python_implementation,
        platform_version,
    )


def _patch_env(monkeypatch: pytest.MonkeyPatch, env: tuple[str, ...]) -> None:
    monkeypatch.setattr(
        "pyinc.integrations.requirement_evaluation._current_python_env",
        lambda: env,
    )


# ---------------------------------------------------------------------------
# Contract lock
# ---------------------------------------------------------------------------


def test_requirement_evaluation_stable_api() -> None:
    assert set(requirement_evaluation.__all__) == {
        "ApplicableRequirement",
        "ApplicableRequirementsAnalysis",
        "MarkerEvaluation",
        "PythonEnvironmentSnapshot",
        "VersionSpecifierEvaluation",
        "applicable_requirements",
        "evaluate_markers",
        "evaluate_version_specifier",
        "python_environment_snapshot",
        "workspace_applicable_requirements",
    }
    assert hasattr(integrations, "evaluate_markers")
    assert hasattr(integrations, "evaluate_version_specifier")
    assert hasattr(integrations, "applicable_requirements")
    assert hasattr(integrations, "workspace_applicable_requirements")
    assert hasattr(integrations, "ApplicableRequirement")
    assert hasattr(integrations, "ApplicableRequirementsAnalysis")
    assert hasattr(integrations, "MarkerEvaluation")
    assert hasattr(integrations, "PythonEnvironmentSnapshot")
    assert hasattr(integrations, "VersionSpecifierEvaluation")

    # Composition queries and private helpers must not leak.
    assert not hasattr(integrations, "python_environment_snapshot")
    assert not hasattr(integrations, "applicable_requirements_payload")
    assert not hasattr(integrations, "_parse_version")
    assert not hasattr(integrations, "_parse_marker")
    assert not hasattr(integrations, "_parse_specifier_set")
    assert not hasattr(integrations, "_satisfies")


# ---------------------------------------------------------------------------
# PEP 440 — version specifier satisfaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize(
    "specifier,version,expected",
    [
        ("==1.0", "1.0", True),
        ("==1.0", "1.0.0", True),
        ("==1.0", "1.1", False),
        ("!=1.0", "1.1", True),
        ("!=1.0", "1.0", False),
        (">=1.0", "1.0", True),
        (">=1.0", "0.9", False),
        (">1.0", "1.0", False),
        (">1.0", "1.0.1", True),
        ("<=2.0", "2.0", True),
        ("<2.0", "2.0", False),
        (">=1.0,<2.0", "1.5", True),
        (">=1.0,<2.0", "2.0", False),
        (">=1.0,<2.0", "0.9", False),
        ("~=1.4.2", "1.4.3", True),
        ("~=1.4.2", "1.5.0", False),
        ("~=1.4", "1.5.0", True),
        ("~=1.4", "2.0.0", False),
        ("==1.*", "1.2.3", True),
        ("==1.*", "2.0", False),
        ("!=1.*", "2.0", True),
        ("!=1.*", "1.0", False),
        ("==1.2.*", "1.2.5", True),
        ("==1.2.*", "1.3.0", False),
        ("==1!2.0", "1!2.0", True),
        ("==1!2.0", "2.0", False),
        ("==1.0+local", "1.0+local", True),
        ("==1.0", "1.0+local", True),
    ],
)
def test_version_specifier_table(
    mode: str, specifier: str, version: str, expected: bool
) -> None:
    db = Database(mode=mode)
    result = evaluate_version_specifier(db, specifier, version)
    assert isinstance(result, VersionSpecifierEvaluation)
    assert result.specifier == specifier
    assert result.version == version
    assert result.satisfied is expected


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_version_specifier_prerelease_excluded_by_default(mode: str) -> None:
    db = Database(mode=mode)
    assert evaluate_version_specifier(db, ">=1.0", "1.0a1").satisfied is False
    # Opt-in via pre-release specifier.
    assert evaluate_version_specifier(db, ">=1.0a0", "1.0a1").satisfied is True
    assert evaluate_version_specifier(db, ">=1.0", "1.0.dev1").satisfied is False


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_version_specifier_post_release_accepted(mode: str) -> None:
    db = Database(mode=mode)
    assert evaluate_version_specifier(db, ">=1.0", "1.0.post1").satisfied is True


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_version_specifier_unparsable(mode: str) -> None:
    db = Database(mode=mode)
    result = evaluate_version_specifier(db, "!!not-a-spec", "1.0")
    assert result.satisfied is False
    assert "cannot parse specifier" in result.detail

    result2 = evaluate_version_specifier(db, ">=1.0", "not-a-version")
    assert result2.satisfied is False
    assert "unparseable version" in result2.detail


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_version_specifier_triple_equals_deferred(mode: str) -> None:
    db = Database(mode=mode)
    # === is deferred; surfaces as a non-satisfied "cannot evaluate".
    result = evaluate_version_specifier(db, "===1.0", "1.0")
    assert result.satisfied is False
    assert "cannot evaluate" in result.detail


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_version_trailing_zeros_equal(mode: str) -> None:
    db = Database(mode=mode)
    # 1.0 == 1.0.0 per PEP 440.
    assert evaluate_version_specifier(db, "==1.0", "1.0.0").satisfied is True
    assert evaluate_version_specifier(db, "==1.0.0", "1.0").satisfied is True


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_version_prerelease_ordering(mode: str) -> None:
    db = Database(mode=mode)
    # 1.0a1 < 1.0b1 < 1.0rc1 < 1.0 < 1.0.post1
    assert evaluate_version_specifier(db, ">=1.0a1", "1.0b1").satisfied is True
    assert evaluate_version_specifier(db, ">=1.0b1", "1.0rc1").satisfied is True
    assert evaluate_version_specifier(db, ">=1.0rc1", "1.0").satisfied is True
    assert evaluate_version_specifier(db, "<1.0.post1", "1.0").satisfied is True


# ---------------------------------------------------------------------------
# PEP 508 — marker expression evaluation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_marker_basic(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_env(monkeypatch, _fixed_env())
    db = Database(mode=mode)

    assert evaluate_markers(db, 'python_version >= "3.10"').value is True
    assert evaluate_markers(db, 'python_version >= "3.14"').value is False
    assert evaluate_markers(db, 'sys_platform == "linux"').value is True
    assert evaluate_markers(db, 'sys_platform == "win32"').value is False
    assert evaluate_markers(db, 'os_name == "posix"').value is True


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_marker_empty_is_true(mode: str) -> None:
    db = Database(mode=mode)
    assert evaluate_markers(db, "").value is True
    assert evaluate_markers(db, "   ").value is True


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_marker_and_or_grouping(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_env(monkeypatch, _fixed_env())
    db = Database(mode=mode)

    assert (
        evaluate_markers(
            db, 'python_version >= "3.10" and sys_platform == "linux"'
        ).value
        is True
    )
    assert (
        evaluate_markers(
            db, 'python_version >= "3.14" or sys_platform == "linux"'
        ).value
        is True
    )
    assert (
        evaluate_markers(
            db, 'python_version >= "3.14" and sys_platform == "linux"'
        ).value
        is False
    )
    assert (
        evaluate_markers(
            db,
            '(python_version >= "3.10") and (sys_platform == "linux" or os_name == "nt")',
        ).value
        is True
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_marker_in_not_in(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_env(monkeypatch, _fixed_env())
    db = Database(mode=mode)

    assert evaluate_markers(db, 'sys_platform in "linux darwin"').value is True
    assert evaluate_markers(db, '"linux" in sys_platform').value is True
    assert evaluate_markers(db, 'sys_platform not in "win32 cygwin"').value is True
    assert evaluate_markers(db, 'sys_platform not in "linux darwin"').value is False


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_marker_extras_not_modeled(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_env(monkeypatch, _fixed_env())
    db = Database(mode=mode)

    result = evaluate_markers(db, 'extra == "dev"')
    assert result.value is False
    codes = [code for code, _ in result.diagnostics]
    assert "extras-not-modeled" in codes


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_marker_platform_version_flagged(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_env(monkeypatch, _fixed_env(platform_version="#42"))
    db = Database(mode=mode)

    result = evaluate_markers(db, 'platform_version == "#42"')
    assert result.value is True
    codes = [code for code, _ in result.diagnostics]
    assert "platform-version-unstable" in codes


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_marker_malformed_yields_diagnostic(mode: str) -> None:
    db = Database(mode=mode)
    result = evaluate_markers(db, "python_version banana 3")
    assert result.value is False
    codes = [code for code, _ in result.diagnostics]
    assert "marker-parse-error" in codes


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_marker_version_variable_semantics(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PEP 508: <, >, ==, != with a version variable use PEP 440 semantics."""
    _patch_env(monkeypatch, _fixed_env(python_full_version="3.12.3"))
    db = Database(mode=mode)

    # Numeric comparison, not lexicographic.
    assert evaluate_markers(db, 'python_full_version >= "3.9"').value is True
    assert evaluate_markers(db, 'python_full_version < "3.13"').value is True


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_marker_string_variable_strict(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-version marker variables compare as plain strings."""
    _patch_env(monkeypatch, _fixed_env(platform_machine="x86_64"))
    db = Database(mode=mode)

    assert evaluate_markers(db, 'platform_machine == "x86_64"').value is True
    assert evaluate_markers(db, 'platform_machine != "aarch64"').value is True


# ---------------------------------------------------------------------------
# Environment snapshot
# ---------------------------------------------------------------------------


def test_python_environment_monkeypatch_flows_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_env(monkeypatch, _fixed_env(python_version="3.99"))
    db = Database()
    assert evaluate_markers(db, 'python_version == "3.99"').value is True
    assert evaluate_markers(db, 'python_version == "3.12"').value is False


# ---------------------------------------------------------------------------
# Composition — applicable_requirements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_applicable_requirements_filters_by_markers(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_env(monkeypatch, _fixed_env(sys_platform="linux", os_name="posix"))
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "requests", "2.31.0", top_level="requests")
    _patch_site(monkeypatch, site_dir)

    req_file = tmp_path / "requirements.txt"
    req_file.write_text(
        'requests>=2.0 ; sys_platform == "linux"\n'
        'requests>=5.0 ; sys_platform == "win32"\n',
        encoding="utf-8",
    )

    db = Database(mode=mode)
    result = applicable_requirements(db, str(req_file))

    assert isinstance(result, ApplicableRequirementsAnalysis)
    assert len(result.requirements) == 2

    linux_req = next(r for r in result.requirements if "linux" in r.markers)
    win_req = next(r for r in result.requirements if "win32" in r.markers)

    assert linux_req.applicable is True
    assert linux_req.status == "satisfied"
    assert linux_req.installed_version == "2.31.0"

    assert win_req.applicable is False
    assert win_req.status == "not_applicable"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_applicable_requirements_status_matrix(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_env(monkeypatch, _fixed_env())
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "requests", "2.31.0", top_level="requests")
    _patch_site(monkeypatch, site_dir)

    req_file = tmp_path / "requirements.txt"
    req_file.write_text(
        "requests>=2.0\n"
        "flask>=1.0\n"
        "requests>=5.0\n"
        "requests===2.31.0\n",
        encoding="utf-8",
    )

    db = Database(mode=mode)
    result = applicable_requirements(db, str(req_file))
    by_spec = {(r.name, r.version_spec): r for r in result.requirements}

    assert by_spec[("requests", ">=2.0")].status == "satisfied"
    assert by_spec[("flask", ">=1.0")].status == "missing"
    assert by_spec[("requests", ">=5.0")].status == "version_mismatch"
    assert by_spec[("requests", "===2.31.0")].status == "ambiguous"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_applicable_requirements_comment_only_edit_backdates(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_env(monkeypatch, _fixed_env())
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "requests", "2.31.0", top_level="requests")
    _patch_site(monkeypatch, site_dir)

    req_file = tmp_path / "requirements.txt"
    req_file.write_text("requests>=2.0\n# old comment\n", encoding="utf-8")

    db = Database(mode=mode)
    first = applicable_requirements(db, str(req_file))

    req_file.write_text("requests>=2.0\n# new comment\n", encoding="utf-8")
    second = applicable_requirements(db, str(req_file))

    assert first == second


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_applicable_requirements_empty_file(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_env(monkeypatch, _fixed_env())
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _patch_site(monkeypatch, site_dir)

    req_file = tmp_path / "requirements.txt"
    req_file.write_text("", encoding="utf-8")

    db = Database(mode=mode)
    result = applicable_requirements(db, str(req_file))
    assert result.requirements == ()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_applicable_requirements_missing_file(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_env(monkeypatch, _fixed_env())
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _patch_site(monkeypatch, site_dir)

    db = Database(mode=mode)
    result = applicable_requirements(db, str(tmp_path / "nonexistent.txt"))
    assert result.requirements == ()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_applicable_requirements_environment_captured(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_env(
        monkeypatch,
        _fixed_env(python_version="3.99", sys_platform="linux"),
    )
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _patch_site(monkeypatch, site_dir)

    req_file = tmp_path / "requirements.txt"
    req_file.write_text("anything\n", encoding="utf-8")

    db = Database(mode=mode)
    result = applicable_requirements(db, str(req_file))
    env = result.environment
    assert isinstance(env, PythonEnvironmentSnapshot)
    assert env.python_version == "3.99"
    assert env.sys_platform == "linux"


# ---------------------------------------------------------------------------
# Workspace discovery
# ---------------------------------------------------------------------------


def test_workspace_applicable_requirements_finds_requirements_txt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_env(monkeypatch, _fixed_env())
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _patch_site(monkeypatch, site_dir)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "requirements.txt").write_text("flask\n", encoding="utf-8")

    db = Database()
    result = workspace_applicable_requirements(db, str(workspace))
    assert result is not None
    assert len(result.requirements) == 1
    assert result.requirements[0].name == "flask"


def test_workspace_applicable_requirements_returns_none_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_env(monkeypatch, _fixed_env())
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _patch_site(monkeypatch, site_dir)

    db = Database()
    result = workspace_applicable_requirements(db, str(tmp_path))
    assert result is None


# ---------------------------------------------------------------------------
# From-scratch consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_applicable_requirements_matches_fresh_recomputation(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_env(monkeypatch, _fixed_env())
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "requests", "2.31.0", top_level="requests")
    _patch_site(monkeypatch, site_dir)

    req_file = tmp_path / "requirements.txt"

    steps: tuple[tuple[str, str], ...] = (
        ("initial", "requests>=2.0\n"),
        ("add marker", 'requests>=2.0 ; sys_platform == "linux"\n'),
        ("tighten marker", 'requests>=2.0 ; sys_platform == "win32"\n'),
        ("add comment", 'requests>=2.0 ; sys_platform == "win32"\n# notes\n'),
        ("change constraint", "requests>=3.0\n"),
        ("add second requirement", "requests>=3.0\nflask>=1.0\n"),
        ("wildcard spec", "requests==2.*\n"),
    )

    incremental = Database(mode=mode)
    for _label, content in steps:
        req_file.write_text(content, encoding="utf-8")
        fresh = Database(mode=mode)
        assert applicable_requirements(
            incremental, str(req_file)
        ) == applicable_requirements(fresh, str(req_file))


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_marker_results_match_fresh_recomputation(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    markers = (
        "",
        'python_version >= "3.10"',
        'sys_platform == "linux"',
        'python_version >= "3.10" and sys_platform == "linux"',
        '(python_version >= "3.10") or (os_name == "nt")',
        'sys_platform in "linux darwin"',
    )
    _patch_env(monkeypatch, _fixed_env())

    incremental = Database(mode=mode)
    for marker in markers:
        fresh = Database(mode=mode)
        assert evaluate_markers(incremental, marker) == evaluate_markers(fresh, marker)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_version_specifier_results_match_fresh_recomputation(mode: str) -> None:
    cases = (
        ("==1.0", "1.0"),
        (">=1.0,<2.0", "1.5"),
        ("~=1.4", "1.5.0"),
        ("==1.*", "1.2.3"),
        (">=1.0", "1.0a1"),
        (">=1.0a0", "1.0a1"),
    )
    incremental = Database(mode=mode)
    for specifier, version in cases:
        fresh = Database(mode=mode)
        assert evaluate_version_specifier(
            incremental, specifier, version
        ) == evaluate_version_specifier(fresh, specifier, version)


# ---------------------------------------------------------------------------
# Shape tests (decode correctness)
# ---------------------------------------------------------------------------


def test_applicable_requirement_decode_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_env(monkeypatch, _fixed_env())
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _patch_site(monkeypatch, site_dir)

    req_file = tmp_path / "requirements.txt"
    req_file.write_text('foo>=1.0 ; python_version >= "3"\n', encoding="utf-8")

    db = Database()
    result = applicable_requirements(db, str(req_file))
    assert len(result.requirements) == 1
    r = result.requirements[0]
    assert isinstance(r, ApplicableRequirement)
    assert r.name == "foo"
    assert r.version_spec == ">=1.0"
    assert 'python_version >= "3"' in r.markers
    assert r.applicable is True
    assert r.status == "missing"


def test_marker_evaluation_decode_shape() -> None:
    db = Database()
    result = evaluate_markers(db, 'python_version >= "3.0"')
    assert isinstance(result, MarkerEvaluation)
    assert result.marker == 'python_version >= "3.0"'
    assert isinstance(result.value, bool)
    assert isinstance(result.diagnostics, tuple)


def test_version_specifier_evaluation_decode_shape() -> None:
    db = Database()
    result = evaluate_version_specifier(db, ">=1.0", "1.5")
    assert isinstance(result, VersionSpecifierEvaluation)
    assert result.specifier == ">=1.0"
    assert result.version == "1.5"
    assert result.satisfied is True
