from __future__ import annotations

from pathlib import Path

import pytest

import pyinc.integrations as integrations
from pyinc import Database
from pyinc.integrations.dependency_check import (
    dependency_check_analysis,
    workspace_dependency_check,
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
    meta_lines = [
        "Metadata-Version: 2.1",
        f"Name: {name}",
        f"Version: {version}",
        "Summary: A test package",
    ]
    (dist_info / "METADATA").write_text(
        "\n".join(meta_lines) + "\n", encoding="utf-8"
    )
    if top_level is not None:
        (dist_info / "top_level.txt").write_text(
            top_level + "\n", encoding="utf-8"
        )
    return dist_info


def _patch_site(
    monkeypatch: pytest.MonkeyPatch, site_dir: Path
) -> None:
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )


# ---------------------------------------------------------------------------
# Contract lock
# ---------------------------------------------------------------------------


def test_dependency_check_stable_api() -> None:
    from pyinc.integrations import dependency_check

    assert set(dependency_check.__all__) == {
        "DependencyCheckAnalysis",
        "DependencyStatus",
        "UndeclaredImport",
        "dependency_check_analysis",
        "workspace_dependency_check",
    }
    # Stable types re-exported from integrations namespace
    assert hasattr(integrations, "DependencyCheckAnalysis")
    assert hasattr(integrations, "DependencyStatus")
    assert hasattr(integrations, "UndeclaredImport")
    assert hasattr(integrations, "dependency_check_analysis")
    assert hasattr(integrations, "workspace_dependency_check")
    # Experimental helpers must not leak
    assert not hasattr(integrations, "dependency_check_payload")
    assert not hasattr(integrations, "_declared_deps_payload")


# ---------------------------------------------------------------------------
# Mode-parametrized correctness tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_missing_dependency_detected(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _patch_site(monkeypatch, site_dir)

    db = Database(mode=mode)
    result = dependency_check_analysis(db, ("requests>=2.0",))
    assert len(result.statuses) == 1
    assert result.statuses[0].status == "missing"
    assert result.statuses[0].name == "requests"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_satisfied_dependency_detected(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "requests", "2.31.0", top_level="requests")
    _patch_site(monkeypatch, site_dir)

    db = Database(mode=mode)
    result = dependency_check_analysis(db, ("requests>=2.0",))
    assert len(result.statuses) == 1
    assert result.statuses[0].status == "satisfied"
    assert result.statuses[0].installed_version == "2.31.0"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_version_mismatch_detected(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "requests", "2.31.0", top_level="requests")
    _patch_site(monkeypatch, site_dir)

    db = Database(mode=mode)
    result = dependency_check_analysis(db, ("requests>=3.0",))
    assert len(result.statuses) == 1
    assert result.statuses[0].status == "version_mismatch"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_no_version_constraint_satisfied(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "requests", "2.31.0", top_level="requests")
    _patch_site(monkeypatch, site_dir)

    db = Database(mode=mode)
    result = dependency_check_analysis(db, ("requests",))
    assert len(result.statuses) == 1
    assert result.statuses[0].status == "satisfied"
    assert result.statuses[0].detail == "installed, no constraint"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_ambiguous_for_complex_specifier(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "requests", "2.31.0", top_level="requests")
    _patch_site(monkeypatch, site_dir)

    db = Database(mode=mode)
    # Use a specifier the parser cannot handle
    result = dependency_check_analysis(db, ("requests===2.31.0",))
    assert len(result.statuses) == 1
    assert result.statuses[0].status == "ambiguous"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_multiple_constraints(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "requests", "2.31.0", top_level="requests")
    _patch_site(monkeypatch, site_dir)

    db = Database(mode=mode)
    result = dependency_check_analysis(db, ("requests>=2.0,<3.0",))
    assert result.statuses[0].status == "satisfied"

    result2 = dependency_check_analysis(db, ("requests>=2.0,<2.31.0",))
    assert result2.statuses[0].status == "version_mismatch"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_compatible_release_operator(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "requests", "2.31.0", top_level="requests")
    _patch_site(monkeypatch, site_dir)

    db = Database(mode=mode)
    # ~=2.0 means >=2.0, <3.0
    result = dependency_check_analysis(db, ("requests~=2.0",))
    assert result.statuses[0].status == "satisfied"

    # ~=3.0 means >=3.0, <4.0
    result2 = dependency_check_analysis(db, ("requests~=3.0",))
    assert result2.statuses[0].status == "version_mismatch"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_extras_stripped_from_specifier(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "requests", "2.31.0", top_level="requests")
    _patch_site(monkeypatch, site_dir)

    db = Database(mode=mode)
    result = dependency_check_analysis(db, ("requests[security]>=2.0",))
    assert result.statuses[0].status == "satisfied"
    assert result.statuses[0].name == "requests"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_markers_stripped_from_specifier(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "requests", "2.31.0", top_level="requests")
    _patch_site(monkeypatch, site_dir)

    db = Database(mode=mode)
    result = dependency_check_analysis(
        db, ('requests>=2.0; python_version>="3.8"',)
    )
    assert result.statuses[0].status == "satisfied"
    assert result.statuses[0].name == "requests"


# ---------------------------------------------------------------------------
# Undeclared import detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_undeclared_import_detected(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Set up fake installed packages
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "requests", "2.31.0", top_level="requests")
    _patch_site(monkeypatch, site_dir)

    # Set up a workspace with a file that imports requests
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("import requests\n", encoding="utf-8")

    db = Database(mode=mode)
    result = workspace_dependency_check(db, str(workspace), ())
    assert len(result.undeclared_imports) == 1
    assert result.undeclared_imports[0].distribution_name == "requests"
    assert result.undeclared_imports[0].import_name == "requests"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_declared_import_not_undeclared(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "requests", "2.31.0", top_level="requests")
    _patch_site(monkeypatch, site_dir)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("import requests\n", encoding="utf-8")

    db = Database(mode=mode)
    result = workspace_dependency_check(db, str(workspace), ("requests>=2.0",))
    assert len(result.undeclared_imports) == 0
    assert len(result.statuses) == 1
    assert result.statuses[0].status == "satisfied"


# ---------------------------------------------------------------------------
# From-scratch consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_dependency_check_matches_fresh_recomputation(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _patch_site(monkeypatch, site_dir)

    declared = ("requests>=2.0", "flask>=1.0")
    incremental = Database(mode=mode)

    # Step 1: both missing
    fresh1 = Database(mode=mode)
    assert dependency_check_analysis(incremental, declared) == dependency_check_analysis(
        fresh1, declared
    )

    # Step 2: install requests
    _make_dist_info(site_dir, "requests", "2.31.0", top_level="requests")
    fresh2 = Database(mode=mode)
    assert dependency_check_analysis(incremental, declared) == dependency_check_analysis(
        fresh2, declared
    )

    # Step 3: install flask
    _make_dist_info(site_dir, "Flask", "2.3.0", top_level="flask")
    fresh3 = Database(mode=mode)
    assert dependency_check_analysis(incremental, declared) == dependency_check_analysis(
        fresh3, declared
    )

    # Step 4: upgrade requests
    dist_info = site_dir / "requests-2.31.0.dist-info"
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: requests\nVersion: 3.0.0\nSummary: A test package\n",
        encoding="utf-8",
    )
    fresh4 = Database(mode=mode)
    result = dependency_check_analysis(incremental, declared)
    assert result == dependency_check_analysis(fresh4, declared)
    # requests>=2.0 should still be satisfied at 3.0.0
    requests_status = next(s for s in result.statuses if s.name == "requests")
    assert requests_status.status == "satisfied"
