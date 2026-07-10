from __future__ import annotations

from pathlib import Path

import pytest

import pyinc.integrations as integrations
import pyinc.integrations.deep_module_resolution as dmr
from pyinc import Database
from pyinc.integrations.deep_module_resolution import (
    DeepModuleResolutionAnalysis,
    ModulePathEntry,
    NamespacePackage,
    PthDirective,
    ResolvedModuleLocation,
    deep_module_resolution_analysis,
    resolve_module_location,
    resolve_module_path,
)


def _install_search_paths(monkeypatch: pytest.MonkeyPatch, paths: tuple[str, ...]) -> None:
    monkeypatch.setattr(
        "pyinc.integrations.deep_module_resolution._get_sys_path_entries",
        lambda: paths,
    )


# ---------------------------------------------------------------------------
# Contract lock
# ---------------------------------------------------------------------------


def test_deep_module_resolution_stable_api() -> None:
    assert set(dmr.__all__) == {
        "DeepModuleResolutionAnalysis",
        "ModulePathEntry",
        "NamespacePackage",
        "PthDirective",
        "ResolvedModuleLocation",
        "deep_module_resolution_analysis",
        "resolve_module_location",
        "resolve_module_path",
    }

    # Stable types/entrypoints are re-exported from pyinc.integrations.
    assert hasattr(integrations, "deep_module_resolution_analysis")
    assert hasattr(integrations, "resolve_module_path")
    assert hasattr(integrations, "DeepModuleResolutionAnalysis")
    assert hasattr(integrations, "ModulePathEntry")
    assert hasattr(integrations, "NamespacePackage")
    assert hasattr(integrations, "PthDirective")
    assert hasattr(integrations, "ResolvedModuleLocation")

    # Cross-integration @query must NOT be re-exported.
    assert not hasattr(integrations, "resolve_module_location")
    # Experimental helpers must NOT be re-exported.
    assert not hasattr(integrations, "_deep_analysis_payload")
    assert not hasattr(integrations, "_pth_directives_payload")
    assert not hasattr(integrations, "_effective_search_paths_payload")


# ---------------------------------------------------------------------------
# Search path discovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_sys_path_entries_appear_in_analysis(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    _install_search_paths(monkeypatch, (str(site),))

    db = Database(mode=mode)
    analysis = deep_module_resolution_analysis(db)

    assert isinstance(analysis, DeepModuleResolutionAnalysis)
    paths = {entry.path for entry in analysis.entries}
    assert str(site.resolve()) in paths
    assert all(isinstance(entry, ModulePathEntry) for entry in analysis.entries)


# ---------------------------------------------------------------------------
# .pth path directives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_pth_path_directives_expand_search_paths(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    extra_abs = tmp_path / "extra-abs"
    extra_abs.mkdir()
    extra_rel = tmp_path / "extra-rel"
    extra_rel.mkdir()
    pth = site / "extras.pth"
    pth.write_text(f"{extra_abs}\n../extra-rel\n", encoding="utf-8")

    _install_search_paths(monkeypatch, (str(site),))
    db = Database(mode=mode)
    analysis = deep_module_resolution_analysis(db)

    paths = {entry.path for entry in analysis.entries}
    assert str(extra_abs.resolve()) in paths
    assert str(extra_rel.resolve()) in paths

    path_directives = [d for d in analysis.pth_directives if d.kind == "path"]
    assert any(str(extra_abs) == d.value for d in path_directives)
    assert any(str(d.source_file).endswith("extras.pth") for d in path_directives)


# ---------------------------------------------------------------------------
# .pth exec-line diagnostics
# ---------------------------------------------------------------------------


def test_pth_exec_line_recorded_as_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "weird.pth").write_text("# a comment\nimport something_fancy\n", encoding="utf-8")

    _install_search_paths(monkeypatch, (str(site),))
    db = Database()
    analysis = deep_module_resolution_analysis(db)

    exec_directives = [d for d in analysis.pth_directives if d.kind == "exec"]
    assert len(exec_directives) == 1
    assert "import something_fancy" in exec_directives[0].value

    assert any(code == "pth-exec-lines" for code, _ in analysis.diagnostics)


# ---------------------------------------------------------------------------
# Namespace packages (PEP 420)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_namespace_package_collects_contributions_from_multiple_paths(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site1 = tmp_path / "s1"
    site1.mkdir()
    site2 = tmp_path / "s2"
    site2.mkdir()
    # ns is a namespace package spread across site1 and site2 (no __init__.py)
    (site1 / "ns").mkdir()
    (site1 / "ns" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (site2 / "ns").mkdir()
    (site2 / "ns" / "b.py").write_text("y = 2\n", encoding="utf-8")

    _install_search_paths(monkeypatch, (str(site1), str(site2)))
    db = Database(mode=mode)

    ns = resolve_module_path(db, "ns")
    assert isinstance(ns, ResolvedModuleLocation)
    assert ns.kind == "namespace-package"
    canonical_contribs = {str(Path(p).resolve()) for p in ns.contributing_paths}
    assert str((site1 / "ns").resolve()) in canonical_contribs
    assert str((site2 / "ns").resolve()) in canonical_contribs
    assert ns.file_path is None

    leaf_a = resolve_module_path(db, "ns.a")
    assert leaf_a.kind == "regular-module"
    assert leaf_a.file_path is not None
    assert Path(leaf_a.file_path).resolve() == (site1 / "ns" / "a.py").resolve()

    leaf_b = resolve_module_path(db, "ns.b")
    assert leaf_b.kind == "regular-module"
    assert Path(leaf_b.file_path or "").resolve() == (site2 / "ns" / "b.py").resolve()


# ---------------------------------------------------------------------------
# Regular package shadows namespace contribution
# ---------------------------------------------------------------------------


def test_regular_package_shadows_namespace_contribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site1 = tmp_path / "s1"
    site1.mkdir()
    site2 = tmp_path / "s2"
    site2.mkdir()
    # site1 has a REGULAR package "pkg" (with __init__.py)
    (site1 / "pkg").mkdir()
    (site1 / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    # site2 has a bare "pkg" directory (would be namespace contribution,
    # but site1 shadows it)
    (site2 / "pkg").mkdir()
    (site2 / "pkg" / "extra.py").write_text("", encoding="utf-8")

    _install_search_paths(monkeypatch, (str(site1), str(site2)))
    db = Database()
    result = resolve_module_path(db, "pkg")
    assert result.kind == "regular-package"
    assert result.file_path is not None
    assert Path(result.file_path).resolve() == (site1 / "pkg" / "__init__.py").resolve()


# ---------------------------------------------------------------------------
# sys.path order precedence for modules
# ---------------------------------------------------------------------------


def test_first_search_path_wins_for_module_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site1 = tmp_path / "s1"
    site1.mkdir()
    site2 = tmp_path / "s2"
    site2.mkdir()
    (site1 / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (site2 / "mod.py").write_text("x = 2\n", encoding="utf-8")

    _install_search_paths(monkeypatch, (str(site1), str(site2)))
    db = Database()
    result = resolve_module_path(db, "mod")
    assert result.kind == "regular-module"
    assert Path(result.file_path or "").resolve() == (site1 / "mod.py").resolve()


# ---------------------------------------------------------------------------
# Stdlib short-circuit
# ---------------------------------------------------------------------------


def test_stdlib_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    _install_search_paths(monkeypatch, (str(site),))
    db = Database()
    result = resolve_module_path(db, "json")
    assert result.kind == "stdlib"
    assert result.file_path is None
    assert result.distribution_name is None

    result2 = resolve_module_path(db, "json.encoder")
    assert result2.kind == "stdlib"


# ---------------------------------------------------------------------------
# Missing module
# ---------------------------------------------------------------------------


def test_missing_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site = tmp_path / "site"
    site.mkdir()
    _install_search_paths(monkeypatch, (str(site),))
    db = Database()
    result = resolve_module_path(db, "does_not_exist_xyz")
    assert result.kind == "missing"
    assert result.file_path is None


# ---------------------------------------------------------------------------
# Backdating on .pth whitespace/comment edit
# ---------------------------------------------------------------------------


def test_pth_comment_edit_backdates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site = tmp_path / "site"
    site.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    pth = site / "z.pth"
    pth.write_text(f"{extra}\n", encoding="utf-8")

    _install_search_paths(monkeypatch, (str(site),))
    db = Database()
    first = deep_module_resolution_analysis(db)

    # Rewrite with whitespace/comments but identical path directives.
    pth.write_text(f"\n# leading comment\n{extra}\n\n", encoding="utf-8")
    second = deep_module_resolution_analysis(db)

    assert first == second


# ---------------------------------------------------------------------------
# From-scratch consistency oracle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_deep_module_resolution_matches_fresh_recomputation(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    _install_search_paths(monkeypatch, (str(site),))

    incremental = Database(mode=mode)

    def fresh() -> Database:
        return Database(mode=mode)

    # Step 1: empty
    assert deep_module_resolution_analysis(incremental) == deep_module_resolution_analysis(fresh())

    # Step 2: add a .pth file pointing to an extra path
    extra = tmp_path / "extra"
    extra.mkdir()
    (site / "a.pth").write_text(f"{extra}\n", encoding="utf-8")
    assert deep_module_resolution_analysis(incremental) == deep_module_resolution_analysis(fresh())

    # Step 3: add a namespace contribution under extra
    (extra / "ns").mkdir()
    (extra / "ns" / "leaf.py").write_text("", encoding="utf-8")
    assert deep_module_resolution_analysis(incremental) == deep_module_resolution_analysis(fresh())

    # Step 4: convert the namespace into a regular package
    (extra / "ns" / "__init__.py").write_text("", encoding="utf-8")
    assert deep_module_resolution_analysis(incremental) == deep_module_resolution_analysis(fresh())

    # Step 5: remove the .pth file
    (site / "a.pth").unlink()
    assert deep_module_resolution_analysis(incremental) == deep_module_resolution_analysis(fresh())


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_resolve_module_location_matches_fresh_recomputation(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    _install_search_paths(monkeypatch, (str(site),))

    incremental = Database(mode=mode)

    # Step 1: missing
    fresh1 = Database(mode=mode)
    assert resolve_module_path(incremental, "pkg") == resolve_module_path(fresh1, "pkg")

    # Step 2: add module file
    (site / "pkg.py").write_text("", encoding="utf-8")
    fresh2 = Database(mode=mode)
    assert resolve_module_path(incremental, "pkg") == resolve_module_path(fresh2, "pkg")
    assert resolve_module_path(incremental, "pkg").kind == "regular-module"

    # Step 3: replace module with regular package
    (site / "pkg.py").unlink()
    (site / "pkg").mkdir()
    (site / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (site / "pkg" / "leaf.py").write_text("", encoding="utf-8")
    fresh3 = Database(mode=mode)
    assert resolve_module_path(incremental, "pkg") == resolve_module_path(fresh3, "pkg")
    assert resolve_module_path(incremental, "pkg.leaf") == resolve_module_path(fresh3, "pkg.leaf")


# ---------------------------------------------------------------------------
# Cross-integration query visibility
# ---------------------------------------------------------------------------


def test_resolve_module_location_exposed_in_submodule_all() -> None:
    # resolve_module_location is intentionally exported from the submodule so
    # python_source can compose with it, but NOT re-exported from
    # pyinc.integrations (tested above). Confirm the @query is callable.
    assert "resolve_module_location" in dmr.__all__
    assert callable(resolve_module_location)


# ---------------------------------------------------------------------------
# PthDirective/NamespacePackage dataclass decoding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_decoded_dataclasses_are_frozen(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "a.pth").write_text(str(tmp_path / "x"), encoding="utf-8")
    (tmp_path / "x").mkdir()
    _install_search_paths(monkeypatch, (str(site),))

    db = Database(mode=mode)
    analysis = deep_module_resolution_analysis(db)

    if analysis.pth_directives:
        directive = analysis.pth_directives[0]
        assert isinstance(directive, PthDirective)
        with pytest.raises((AttributeError, TypeError)):
            directive.kind = "other"  # type: ignore[misc]
    assert all(isinstance(n, NamespacePackage) for n in analysis.namespace_packages)
