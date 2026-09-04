from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

import pyinc.integrations as integrations
from pyinc import Database, InMemoryArtifactStore
from pyinc.integrations.installed_packages import (
    InstalledPackagesAnalysis,
    environment_index,
    installed_distributions_index,
    installed_packages_analysis,
    resolve_import_name,
)

Operation = tuple[Literal["write", "delete"], str, str | None]


# ---------------------------------------------------------------------------
# Helpers: build fake dist-info directories
# ---------------------------------------------------------------------------


def _make_dist_info(
    site_dir: Path,
    name: str,
    version: str,
    *,
    summary: str = "A test package",
    top_level: str | None = None,
    requires_dist: tuple[str, ...] = (),
) -> Path:
    """Create a fake .dist-info directory with METADATA and optional top_level.txt."""
    dist_info = site_dir / f"{name}-{version}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)

    meta_lines = [
        "Metadata-Version: 2.1",
        f"Name: {name}",
        f"Version: {version}",
        f"Summary: {summary}",
    ]
    for dep in requires_dist:
        meta_lines.append(f"Requires-Dist: {dep}")
    (dist_info / "METADATA").write_text("\n".join(meta_lines) + "\n", encoding="utf-8")

    if top_level is not None:
        (dist_info / "top_level.txt").write_text(top_level + "\n", encoding="utf-8")

    return dist_info


# ---------------------------------------------------------------------------
# Contract lock
# ---------------------------------------------------------------------------


def test_package_namespace_exports_installed_packages_stable_api() -> None:
    assert "InstalledPackageRef" in integrations.__all__
    assert "ImportNameResolution" in integrations.__all__
    assert "InstalledPackagesAnalysis" in integrations.__all__
    assert "installed_packages_analysis" in integrations.__all__
    assert "resolve_import_name" in integrations.__all__
    assert hasattr(integrations, "installed_packages_analysis")
    assert hasattr(integrations, "resolve_import_name")
    assert hasattr(integrations, "InstalledPackageRef")
    # Experimental helpers must not leak.
    assert not hasattr(integrations, "_site_packages_dirs")
    assert not hasattr(integrations, "_dist_info_listing")
    assert not hasattr(integrations, "_metadata_text")
    assert not hasattr(integrations, "_top_level_text")
    assert not hasattr(integrations, "_package_metadata_payload")
    assert not hasattr(integrations, "_installed_packages_payload")


# ---------------------------------------------------------------------------
# Mode-parametrized correctness (uses fake site-packages)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_installed_packages_discovers_packages_in_fake_site(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "example-pkg", "1.0.0", top_level="example_pkg")
    _make_dist_info(
        site_dir,
        "another",
        "2.3.1",
        summary="Another package",
        top_level="another",
        requires_dist=("dep1>=1.0", "dep2"),
    )

    # Patch site-packages discovery to use our fake directory
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    db = Database(mode=mode)
    analysis = installed_packages_analysis(db)

    assert isinstance(analysis, InstalledPackagesAnalysis)
    assert len(analysis.packages) == 2

    names = {p.distribution_name for p in analysis.packages}
    assert "example-pkg" in names
    assert "another" in names

    another = next(p for p in analysis.packages if p.distribution_name == "another")
    assert another.version == "2.3.1"
    assert another.top_level_names == ("another",)
    assert "dep1>=1.0" in another.requires_dist
    assert "dep2" in another.requires_dist

    assert analysis.diagnostics == ()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_resolve_import_name_stdlib(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    db = Database(mode=mode)
    result = resolve_import_name(db, "os")
    assert result.origin == "stdlib"
    assert result.distribution_name is None

    result2 = resolve_import_name(db, "os.path")
    assert result2.origin == "stdlib"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_resolve_import_name_installed(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "requests", "2.31.0", top_level="requests")
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    db = Database(mode=mode)
    result = resolve_import_name(db, "requests")
    assert result.origin == "installed"
    assert result.distribution_name == "requests"
    assert result.distribution_version == "2.31.0"

    # Submodule resolution
    result2 = resolve_import_name(db, "requests.auth")
    assert result2.origin == "installed"
    assert result2.distribution_name == "requests"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_resolve_import_name_unknown(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    db = Database(mode=mode)
    result = resolve_import_name(db, "nonexistent_xyz")
    assert result.origin == "unknown"
    assert result.distribution_name is None


# ---------------------------------------------------------------------------
# Top-level name fallback
# ---------------------------------------------------------------------------


def test_top_level_fallback_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    # No top_level.txt → should fall back to normalized dist name
    _make_dist_info(site_dir, "My-Package", "1.0.0")
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    db = Database()
    analysis = installed_packages_analysis(db)
    assert len(analysis.packages) == 1
    # Fallback: My-Package → my_package
    assert analysis.packages[0].top_level_names == ("my_package",)


# ---------------------------------------------------------------------------
# Multiple top-level names
# ---------------------------------------------------------------------------


def test_multiple_top_level_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "boto3", "1.28.0", top_level="boto3\nbotocore")
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    db = Database()
    result = resolve_import_name(db, "botocore")
    assert result.origin == "installed"
    assert result.distribution_name == "boto3"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_malformed_metadata_produces_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    dist_info = site_dir / "broken-1.0.0.dist-info"
    dist_info.mkdir()
    # Write METADATA without Name or Version fields
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\n", encoding="utf-8")
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    db = Database()
    analysis = installed_packages_analysis(db)
    assert len(analysis.packages) == 0
    assert len(analysis.diagnostics) == 1
    assert analysis.diagnostics[0][0] == "metadata-parse-failed"


def test_metadata_headers_are_case_insensitive_and_unfolded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    dist_info = site_dir / "mixed-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "metadata-version: 2.1\n"
        "nAmE: Mixed-Pkg\n"
        "vErSiOn: 1.0\n"
        "sUmMaRy: first line\n"
        " second line\n"
        "rEqUiReS-DiSt: dependency>=1;\n"
        ' python_version >= "3.11"\n',
        encoding="utf-8",
    )
    (dist_info / "top_level.txt").write_text("mixed_pkg\n", encoding="utf-8")
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    analysis = installed_packages_analysis(Database(mode="strict"))

    assert analysis.diagnostics == ()
    package = analysis.packages[0]
    assert package.distribution_name == "Mixed-Pkg"
    assert package.version == "1.0"
    assert package.summary == "first line second line"
    assert package.requires_dist == ('dependency>=1; python_version >= "3.11"',)


def test_empty_site_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    db = Database()
    analysis = installed_packages_analysis(db)
    assert analysis.packages == ()
    assert analysis.diagnostics == ()
    assert len(analysis.stdlib_modules) > 0


def test_non_dist_info_entries_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    # Create non-dist-info directories/files that should be ignored
    (site_dir / "some_package").mkdir()
    (site_dir / "some_package" / "__init__.py").write_text("", encoding="utf-8")
    (site_dir / "README.txt").write_text("ignore me", encoding="utf-8")
    (site_dir / "some.egg-info").mkdir()  # egg-info not supported yet
    # One real dist-info
    _make_dist_info(site_dir, "real-pkg", "1.0.0", top_level="real_pkg")
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    db = Database()
    analysis = installed_packages_analysis(db)
    assert len(analysis.packages) == 1
    assert analysis.packages[0].distribution_name == "real-pkg"


# ---------------------------------------------------------------------------
# Backdating
# ---------------------------------------------------------------------------


def test_metadata_comment_edit_backdates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    dist_info = _make_dist_info(site_dir, "example", "1.0.0", top_level="example")
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    db = Database()
    first = installed_packages_analysis(db)

    # Rewrite METADATA with different whitespace but same fields
    meta = dist_info / "METADATA"
    meta.write_text(
        "Metadata-Version: 2.1\n\nName: example\nVersion: 1.0.0\n\nSummary: A test package\n",
        encoding="utf-8",
    )
    second = installed_packages_analysis(db)

    assert first == second


def test_version_change_invalidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    dist_info = _make_dist_info(site_dir, "example", "1.0.0", top_level="example")
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    db = Database()
    first = installed_packages_analysis(db)
    assert first.packages[0].version == "1.0.0"

    # Change the version — but keep same dist-info directory name
    meta = dist_info / "METADATA"
    meta.write_text(
        "Metadata-Version: 2.1\nName: example\nVersion: 2.0.0\nSummary: A test package\n",
        encoding="utf-8",
    )
    second = installed_packages_analysis(db)
    assert second.packages[0].version == "2.0.0"
    assert first != second


# ---------------------------------------------------------------------------
# Reads that must answer with the text they compared
# ---------------------------------------------------------------------------

# A METADATA header has three states, not two: absent, present-and-empty, and
# present-with-a-value. _parse_metadata_field answers None for the first and ''
# for the second, and the package layer branches on exactly that difference --
# an absent Name or Version is a distribution it cannot describe, an empty one
# is a distribution whose name or version is the empty string. Any comparison
# that fills a missing field in with '' cannot tell those two apart, so it
# reports no change across an edit that moves a package on and off the listing.


def _metadata_document(*, name: str | None, version: str | None, summary: str = "s") -> str:
    """Build a METADATA document, omitting a header entirely when its value is None."""
    lines = ["Metadata-Version: 2.1"]
    for field, value in (("Name", name), ("Version", version)):
        if value is not None:
            # An empty value writes "Name:", not "Name: " -- a header left
            # empty carries nothing after the colon.
            lines.append(f"{field}: {value}".rstrip())
    lines.append(f"Summary: {summary}")
    return "\n".join(lines) + "\n"


# Each step carries the document, the distribution listing it must produce, and
# the diagnostic codes that must come with it. Both sequences pass through the
# empty state twice, so both directions of the edit that a field-value
# comparison cannot see are walked: absent -> empty on the way in, and
# empty -> absent on the way out. The two middle steps move the field to a real
# value and back, which any comparison can see; they are here to separate the
# two blind transitions rather than to witness anything themselves.
_Step = tuple[str, str, tuple[tuple[str, str], ...], tuple[str, ...]]

_NAME_SEQUENCE: tuple[_Step, ...] = (
    (
        "no Name header",
        _metadata_document(name=None, version="1.0"),
        (),
        ("metadata-parse-failed",),
    ),
    ("an empty Name header", _metadata_document(name="", version="1.0"), (("", "1.0"),), ()),
    (
        "a Name header with a value",
        _metadata_document(name="example", version="1.0"),
        (("example", "1.0"),),
        (),
    ),
    ("an empty Name header again", _metadata_document(name="", version="1.0"), (("", "1.0"),), ()),
    (
        "the Name header removed",
        _metadata_document(name=None, version="1.0"),
        (),
        ("metadata-parse-failed",),
    ),
)

_VERSION_SEQUENCE: tuple[_Step, ...] = (
    (
        "no Version header",
        _metadata_document(name="example", version=None),
        (),
        ("metadata-parse-failed",),
    ),
    (
        "an empty Version header",
        _metadata_document(name="example", version=""),
        (("example", ""),),
        (),
    ),
    (
        "a Version header with a value",
        _metadata_document(name="example", version="1.0"),
        (("example", "1.0"),),
        (),
    ),
    (
        "an empty Version header again",
        _metadata_document(name="example", version=""),
        (("example", ""),),
        (),
    ),
    (
        "the Version header removed",
        _metadata_document(name="example", version=None),
        (),
        ("metadata-parse-failed",),
    ),
)


def _site_with_one_dist_info(tmp_path: Path) -> tuple[Path, Path]:
    """A site-packages holding one dist-info whose METADATA the caller rewrites."""
    site_dir = tmp_path / "site-packages"
    dist_info = site_dir / "example-1.0.dist-info"
    dist_info.mkdir(parents=True)
    # A top_level.txt keeps the import name fixed across the sequence, so the
    # resolution surface moves only when the distribution itself moves.
    (dist_info / "top_level.txt").write_text("example\n", encoding="utf-8")
    return site_dir, dist_info / "METADATA"


def _every_surface(db: Database) -> dict[str, object]:
    """The four public surfaces this metadata read reaches.

    The two indexes are the cross-integration half: they are what dependency
    checking, python-source enrichment and module resolution read the installed
    environment through, so a stale answer here travels well past this module.
    """
    analysis = installed_packages_analysis(db)
    stdlib_modules, package_entries = environment_index(db)
    return {
        "installed_packages_analysis packages": analysis.packages,
        "installed_packages_analysis diagnostics": analysis.diagnostics,
        "resolve_import_name": resolve_import_name(db, "example"),
        # environment_index is compared as both of its halves rather than as one
        # value, so that a failure prints the package entries instead of
        # truncating inside three hundred stdlib module names.
        "environment_index stdlib modules": stdlib_modules,
        "environment_index package entries": package_entries,
        "installed_distributions_index": installed_distributions_index(db),
    }


def _brief(value: object) -> str:
    """A bounded single-line rendering, for the one surface that can still be long."""
    text = repr(value)
    return text if len(text) <= 160 else text[:160] + "...<truncated>"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize(
    "field, sequence",
    [("Name", _NAME_SEQUENCE), ("Version", _VERSION_SEQUENCE)],
    ids=["Name", "Version"],
)
def test_every_installed_packages_surface_matches_a_fresh_read_across_the_field_sequence(
    field: str,
    sequence: tuple[_Step, ...],
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every surface answers each METADATA edit the way a fresh database does."""
    site_dir, meta = _site_with_one_dist_info(tmp_path)
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    incremental = Database(mode=mode)
    for label, document, expected_listing, expected_codes in sequence:
        meta.write_text(document, encoding="utf-8")
        warm = _every_surface(incremental)
        fresh = _every_surface(Database(mode=mode))

        for name in warm:
            assert warm[name] == fresh[name], (
                f"{name} disagrees with a fresh read | after: {label} | field={field} | "
                f"mode={mode} | warm={_brief(warm[name])} | fresh={_brief(fresh[name])}"
            )

        # The equality above is satisfied by two databases that are wrong the
        # same way, so each step also pins the answer itself. This is where the
        # empty-field states are held to what they report: a distribution whose
        # Name or Version is present and empty stays on the listing with that
        # empty value, and carries no diagnostic.
        analysis = installed_packages_analysis(incremental)
        listing = tuple((pkg.distribution_name, pkg.version) for pkg in analysis.packages)
        assert listing == expected_listing, (
            f"listing is not what this document describes | after: {label} | field={field} | "
            f"mode={mode} | listing={listing} | expected={expected_listing}"
        )
        codes = tuple(code for code, _ in analysis.diagnostics)
        assert codes == expected_codes, (
            f"diagnostics are not what this document produces | after: {label} | field={field} | "
            f"mode={mode} | codes={codes} | expected={expected_codes}"
        )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_reloaded_installed_packages_checkpoint_answers_like_a_fresh_database(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir, meta = _site_with_one_dist_info(tmp_path)
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    meta.write_text(_metadata_document(name=None, version="1.0"), encoding="utf-8")
    store = InMemoryArtifactStore()
    saver = Database(mode=mode, store=store)
    _every_surface(saver)

    # The edit lands BEFORE the save and the surfaces are re-driven, so what
    # gets written is the state the database reached by answering after the
    # edit. Saving first and editing afterwards would checkpoint a database that
    # had never answered across this edit at all, and the row would pass whether
    # or not anything is wrong.
    meta.write_text(_metadata_document(name="", version="1.0"), encoding="utf-8")
    _every_surface(saver)
    key = saver.save_checkpoint()

    reloaded = Database(mode=mode, store=store)
    reloaded.load_checkpoint(key)
    restored = _every_surface(reloaded)
    fresh = _every_surface(Database(mode=mode))

    for name in restored:
        assert restored[name] == fresh[name], (
            f"{name} from a reloaded checkpoint disagrees with a fresh read | mode={mode} | "
            f"restored={_brief(restored[name])} | fresh={_brief(fresh[name])}"
        )

    # A reload is the durable half of what this guards against: an answer
    # carried over from before the edit would outlive the process that produced
    # it, and every consumer of the two indexes would inherit it.
    analysis = installed_packages_analysis(reloaded)
    assert [(p.distribution_name, p.version) for p in analysis.packages] == [("", "1.0")]
    assert analysis.diagnostics == ()


# ---------------------------------------------------------------------------
# From-scratch oracle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_installed_packages_matches_fresh_recomputation(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    incremental = Database(mode=mode)

    # Step 1: empty
    fresh1 = Database(mode=mode)
    assert installed_packages_analysis(incremental) == installed_packages_analysis(fresh1)

    # Step 2: add a package
    _make_dist_info(site_dir, "pkg-a", "1.0.0", top_level="pkg_a")
    fresh2 = Database(mode=mode)
    assert installed_packages_analysis(incremental) == installed_packages_analysis(fresh2)

    # Step 3: add another package
    _make_dist_info(
        site_dir,
        "pkg-b",
        "2.0.0",
        top_level="pkg_b",
        requires_dist=("pkg-a>=1.0",),
    )
    fresh3 = Database(mode=mode)
    assert installed_packages_analysis(incremental) == installed_packages_analysis(fresh3)

    # Step 4: modify metadata (version bump)
    dist_info = site_dir / "pkg-a-1.0.0.dist-info"
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: pkg-a\nVersion: 1.1.0\nSummary: A test package\n",
        encoding="utf-8",
    )
    fresh4 = Database(mode=mode)
    assert installed_packages_analysis(incremental) == installed_packages_analysis(fresh4)

    # Step 5: remove a package
    import shutil

    shutil.rmtree(site_dir / "pkg-b-2.0.0.dist-info")
    fresh5 = Database(mode=mode)
    assert installed_packages_analysis(incremental) == installed_packages_analysis(fresh5)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_resolve_import_matches_fresh_recomputation(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    incremental = Database(mode=mode)

    # Step 1: resolve before any package exists
    fresh1 = Database(mode=mode)
    assert resolve_import_name(incremental, "pkg_a") == resolve_import_name(fresh1, "pkg_a")

    # Step 2: add the package
    _make_dist_info(site_dir, "pkg-a", "1.0.0", top_level="pkg_a")
    fresh2 = Database(mode=mode)
    assert resolve_import_name(incremental, "pkg_a") == resolve_import_name(fresh2, "pkg_a")
    assert resolve_import_name(incremental, "pkg_a").origin == "installed"

    # Step 3: check stdlib resolution stays correct
    assert resolve_import_name(incremental, "os") == resolve_import_name(fresh2, "os")
    assert resolve_import_name(incremental, "os").origin == "stdlib"


# ---------------------------------------------------------------------------
# environment_index query (cross-integration composition support)
# ---------------------------------------------------------------------------


def test_environment_index_returns_stdlib_and_package_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    _make_dist_info(site_dir, "requests", "2.31.0", top_level="requests")
    _make_dist_info(site_dir, "boto3", "1.28.0", top_level="boto3\nbotocore")
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    db = Database(mode="strict")
    stdlib_modules, package_entries = environment_index(db)

    assert "os" in stdlib_modules
    assert "sys" in stdlib_modules

    entry_map = {name: (dist, ver) for name, dist, ver in package_entries}
    assert entry_map["requests"] == ("requests", "2.31.0")
    assert entry_map["boto3"] == ("boto3", "1.28.0")
    assert entry_map["botocore"] == ("boto3", "1.28.0")


def test_environment_index_not_in_integrations_namespace() -> None:
    """environment_index is a composition query, not re-exported from integrations."""
    assert "environment_index" not in integrations.__all__


def test_empty_distribution_metadata_produces_a_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site-packages"
    (site_dir / "empty-1.0.dist-info").mkdir(parents=True)
    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site_dir),),
    )

    analysis = installed_packages_analysis(Database())

    assert analysis.packages == ()
    assert analysis.diagnostics[0][0] == "metadata-parse-failed"
