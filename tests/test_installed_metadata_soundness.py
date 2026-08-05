from __future__ import annotations

from pathlib import Path
from typing import TypeAlias

import pytest

from pyinc import Database, InMemoryArtifactStore
from pyinc.integrations import installed_packages
from pyinc.integrations.installed_packages import (
    EnvironmentIndexPayload,
    ImportNameResolution,
    InstalledDistributionsIndexPayload,
    InstalledPackagePayload,
    InstalledPackagesAnalysis,
    InstalledPackagesAnalysisPayload,
    _installed_packages_payload,
    _metadata_text,
    _package_metadata_payload,
    environment_index,
    installed_distributions_index,
    installed_packages_analysis,
    resolve_import_name,
)

Observation: TypeAlias = tuple[
    str,
    InstalledPackagePayload | None,
    InstalledPackagesAnalysisPayload,
    EnvironmentIndexPayload,
    InstalledDistributionsIndexPayload,
    InstalledPackagesAnalysis,
    ImportNameResolution,
]


def _metadata(
    *,
    name: str | None = "Demo-Pkg",
    version: str | None = "1.0",
    summary: str | None = "Demo package",
    requires: tuple[str, ...] = ("alpha>=1", "beta"),
) -> str:
    lines = ["Metadata-Version: 2.1"]
    if name is not None:
        lines.append(f"Name: {name}")
    if version is not None:
        lines.append(f"Version: {version}")
    if summary is not None:
        lines.append(f"Summary: {summary}")
    lines.extend(f"Requires-Dist: {requirement}" for requirement in requires)
    return "\n".join(lines) + "\n"


def _layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, str]:
    site_dir = tmp_path / "site-packages"
    dist_info_name = "demo-1.0.dist-info"
    dist_info = site_dir / dist_info_name
    dist_info.mkdir(parents=True)
    metadata_path = dist_info / "METADATA"
    (dist_info / "top_level.txt").write_text("demo_import\n", encoding="utf-8")
    monkeypatch.setattr(
        installed_packages,
        "_get_site_packages_dirs",
        lambda: (str(site_dir),),
    )
    return site_dir, metadata_path, dist_info_name


def _observe(
    db: Database,
    site_dir: Path,
    metadata_path: Path,
    dist_info_name: str,
) -> Observation:
    return (
        db.get(_metadata_text, str(metadata_path)),
        db.get(_package_metadata_payload, str(site_dir), dist_info_name),
        db.get(_installed_packages_payload),
        environment_index(db),
        installed_distributions_index(db),
        installed_packages_analysis(db),
        resolve_import_name(db, "demo_import.child"),
    )


def _assert_public_state(observation: Observation, *, valid: bool, version: str = "1.0") -> None:
    analysis = observation[5]
    resolution = observation[6]
    if valid:
        assert len(analysis.packages) == 1
        assert analysis.diagnostics == ()
        package = analysis.packages[0]
        assert package.distribution_name == "Demo-Pkg"
        assert package.version == version
        assert resolution.origin == "installed"
        assert resolution.distribution_name == "Demo-Pkg"
        assert observation[4] == (("demo-pkg", version),)
        assert ("demo_import", "Demo-Pkg", version) in observation[3][1]
    else:
        assert analysis.packages == ()
        assert len(analysis.diagnostics) == 1
        assert analysis.diagnostics[0][0] == "metadata-parse-failed"
        assert resolution.origin == "unknown"
        assert observation[4] == ()
        assert all(entry[0] != "demo_import" for entry in observation[3][1])


def test_required_metadata_projection_collapses_only_rejected_shapes() -> None:
    invalid = (
        _metadata(name=None),
        _metadata(name=""),
        _metadata(name=" \t "),
        _metadata(version=None),
        _metadata(version=""),
        _metadata(version=" \t "),
    )
    invalid_tokens = {installed_packages._metadata_cutoff_token(text) for text in invalid}

    assert invalid_tokens == {("invalid-required-field",)}
    valid_token = installed_packages._metadata_cutoff_token(_metadata())
    assert valid_token[0] == "package"
    assert valid_token not in invalid_tokens


def test_summary_presence_matches_its_public_empty_string_collapse() -> None:
    missing = _metadata(summary=None)
    empty = _metadata(summary="")
    whitespace = _metadata(summary=" \t ")

    assert installed_packages._metadata_cutoff_token(
        missing
    ) == installed_packages._metadata_cutoff_token(empty)
    assert installed_packages._metadata_cutoff_token(
        empty
    ) == installed_packages._metadata_cutoff_token(whitespace)


def test_repeated_requires_dist_order_is_part_of_the_projection() -> None:
    first = _metadata(requires=("alpha>=1", "beta", "alpha>=1"))
    reordered = _metadata(requires=("alpha>=1", "alpha>=1", "beta"))

    assert installed_packages._parse_metadata_fields(first, "Requires-Dist") == (
        "alpha>=1",
        "beta",
        "alpha>=1",
    )
    assert installed_packages._metadata_cutoff_token(
        first
    ) != installed_packages._metadata_cutoff_token(reordered)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_required_field_transitions_match_fresh_for_every_consumer(
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site_dir, metadata_path, dist_info_name = _layout(tmp_path, monkeypatch)
    warm = Database(mode=mode)
    states = (
        (_metadata(name=None), False, "1.0"),
        (_metadata(name=""), False, "1.0"),
        (_metadata(name=" \t "), False, "1.0"),
        (_metadata(), True, "1.0"),
        (_metadata(version=None), False, "1.0"),
        (_metadata(version=""), False, "1.0"),
        (_metadata(version=" \t "), False, "1.0"),
        (_metadata(version="2.0"), True, "2.0"),
        (_metadata(name=None), False, "1.0"),
    )

    for text, valid, version in states:
        metadata_path.write_text(text, encoding="utf-8")
        warm_observation = _observe(warm, site_dir, metadata_path, dist_info_name)
        fresh_observation = _observe(
            Database(mode=mode),
            site_dir,
            metadata_path,
            dist_info_name,
        )

        assert warm_observation == fresh_observation
        assert warm_observation[0] == text
        _assert_public_state(warm_observation, valid=valid, version=version)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_optional_metadata_transitions_match_fresh_and_preserve_requires_order(
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site_dir, metadata_path, dist_info_name = _layout(tmp_path, monkeypatch)
    warm = Database(mode=mode)
    states = (
        _metadata(summary=None),
        _metadata(summary=""),
        _metadata(summary=" \t "),
        _metadata(summary="Visible summary"),
        _metadata(summary="Visible summary", requires=("beta", "alpha>=1")),
        _metadata(summary=None, requires=("alpha>=1", "beta", "alpha>=1")),
    )
    previous_empty_analysis: InstalledPackagesAnalysis | None = None

    for index, text in enumerate(states):
        metadata_path.write_text(text, encoding="utf-8")
        warm_observation = _observe(warm, site_dir, metadata_path, dist_info_name)
        fresh_observation = _observe(
            Database(mode=mode),
            site_dir,
            metadata_path,
            dist_info_name,
        )

        assert warm_observation == fresh_observation
        assert warm_observation[0] == text
        _assert_public_state(warm_observation, valid=True)
        package = warm_observation[5].packages[0]
        if index < 3:
            assert package.summary == ""
            if previous_empty_analysis is not None:
                assert warm_observation[5] == previous_empty_analysis
            previous_empty_analysis = warm_observation[5]

    assert warm_observation[5].packages[0].requires_dist == (
        "alpha>=1",
        "beta",
        "alpha>=1",
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize(
    ("before", "after"),
    [
        (_metadata(name=None), _metadata()),
        (_metadata(), _metadata(version=" \t ")),
        (_metadata(summary=None), _metadata(summary="")),
        (
            _metadata(requires=("alpha>=1", "beta")),
            _metadata(requires=("beta", "alpha>=1")),
        ),
    ],
    ids=["missing-to-valid", "valid-to-blank", "summary-collapse", "requires-order"],
)
def test_metadata_same_mode_checkpoint_reload_matches_fresh(
    mode: str,
    before: str,
    after: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site_dir, metadata_path, dist_info_name = _layout(tmp_path, monkeypatch)
    store = InMemoryArtifactStore()
    metadata_path.write_text(before, encoding="utf-8")
    writer = Database(mode=mode, store=store)
    _observe(writer, site_dir, metadata_path, dist_info_name)
    checkpoint = writer.save_checkpoint()

    metadata_path.write_text(after, encoding="utf-8")
    loaded = Database(mode=mode, store=store)
    loaded.load_checkpoint(checkpoint)

    loaded_observation = _observe(loaded, site_dir, metadata_path, dist_info_name)
    fresh_observation = _observe(
        Database(mode=mode),
        site_dir,
        metadata_path,
        dist_info_name,
    )
    assert loaded_observation == fresh_observation
    assert loaded_observation[0] == after
