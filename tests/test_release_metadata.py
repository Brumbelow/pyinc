from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from scripts.verify_release_metadata import (
        ReleaseMetadata,
        ReleaseMetadataError,
        main,
        verify_release_metadata,
    )
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from verify_release_metadata import (  # noqa: E402
        ReleaseMetadata,
        ReleaseMetadataError,
        main,
        verify_release_metadata,
    )


def _project(version: str) -> bytes:
    return f"""[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "pyinc"
version = "{version}"
dependencies = []

[tool.fixture]
version = "0.0.0"
""".encode()


def _link(version: str) -> str:
    return f"[{version}]: https://github.com/Brumbelow/pyinc/releases/tag/v{version}"


def _changelog(
    version: str,
    *,
    heading: str | None = None,
    body: str = "- Release.",
    links: tuple[str, ...] | None = None,
) -> str:
    heading = f"## [{version}] - 2026-09-04" if heading is None else heading
    links = (_link("0.9.0"), _link(version)) if links is None else links
    link_lines = "\n".join(links)
    return f"""# Changelog

## [Unreleased]

{heading}

{body}

## [0.9.0] - 2026-01-01

- Earlier release.

{link_lines}
"""


def _metadata(
    tag: str = "v4.0.0",
    *,
    project_version: str | None = None,
    project_document: bytes | None = None,
    changelog: str | bytes | None = None,
) -> ReleaseMetadata:
    version = tag[1:] if project_version is None else project_version
    document = _project(version) if project_document is None else project_document
    if changelog is None:
        changelog = _changelog(version)
    if isinstance(changelog, str):
        changelog = changelog.encode()
    return ReleaseMetadata(
        tag=tag,
        project_version=version,
        project_document=document,
        changelog=changelog,
    )


@pytest.mark.parametrize("tag", ["v4.0.0", "v4.1.7", "v10.20.30", "v4.0.0rc1", "v4.0.0a2"])
def test_accepts_a_tag_naming_the_version_section_and_link(tag: str) -> None:
    verify_release_metadata(_metadata(tag))


@pytest.mark.parametrize("tag", ["4.0.0", "v4.0", "v4.0.0-rc1", "v4.0.0.1", "release-4.0.0"])
def test_rejects_a_tag_that_is_not_a_release_tag(tag: str) -> None:
    with pytest.raises(ReleaseMetadataError, match="not of the form"):
        verify_release_metadata(_metadata(tag, project_version="4.0.0"))


@pytest.mark.parametrize(("tag", "version"), [("v4.0.0", "4.0.1"), ("v4.0.0rc1", "4.0.0")])
def test_rejects_a_tag_that_does_not_match_the_project_version(tag: str, version: str) -> None:
    with pytest.raises(ReleaseMetadataError, match="does not match project version"):
        verify_release_metadata(_metadata(tag, project_version=version))


def test_rejects_a_recorded_version_the_project_document_disagrees_with() -> None:
    with pytest.raises(ReleaseMetadataError, match="recorded project version"):
        verify_release_metadata(_metadata(project_document=_project("3.1.1")))


def test_rejects_a_non_utf8_changelog() -> None:
    with pytest.raises(ReleaseMetadataError, match="valid UTF-8"):
        verify_release_metadata(_metadata(changelog=b"\xff"))


@pytest.mark.parametrize(
    ("changelog", "message"),
    [
        (_changelog("4.0.0", heading="## [4.0.1] - 2026-09-04"), "exactly one 4.0.0 release"),
        (_changelog("4.0.0") + "\n## [4.0.0] - 2026-09-05\n\n- Again.\n", "exactly one 4.0.0"),
        (_changelog("4.0.0", heading="## [4.0.0]"), "must use"),
        (_changelog("4.0.0", heading="## [4.0.0] - 2026-09-04 (final)"), "must use"),
        (_changelog("4.0.0", heading="## [4.0.0] - 2026-02-30"), "not a real date"),
        (_changelog("4.0.0", body=""), "must not be empty"),
    ],
)
def test_rejects_a_missing_duplicate_malformed_misdated_or_empty_section(
    changelog: str, message: str
) -> None:
    with pytest.raises(ReleaseMetadataError, match=message):
        verify_release_metadata(_metadata(changelog=changelog))


def test_accepts_a_section_dated_a_leap_day_that_exists() -> None:
    verify_release_metadata(
        _metadata(changelog=_changelog("4.0.0", heading="## [4.0.0] - 2024-02-29"))
    )


@pytest.mark.parametrize(
    ("links", "message"),
    [
        ((_link("0.9.0"),), "exactly one 4.0.0 release link"),
        ((_link("0.9.0"), _link("4.0.0"), _link("4.0.0")), "exactly one 4.0.0 release link"),
        (
            (_link("0.9.0"), "[4.0.0]: https://github.com/Brumbelow/pyinc/releases/tag/v4.0.1"),
            "must be exactly",
        ),
        ((_link("0.9.0"), "[4.0.0]: https://example.invalid/v4.0.0"), "must be exactly"),
    ],
)
def test_rejects_a_missing_duplicate_or_wrong_release_link(
    links: tuple[str, ...], message: str
) -> None:
    with pytest.raises(ReleaseMetadataError, match=message):
        verify_release_metadata(_metadata(changelog=_changelog("4.0.0", links=links)))


def test_the_release_candidate_section_and_link_carry_the_candidate_version() -> None:
    with pytest.raises(ReleaseMetadataError, match="exactly one 4.0.0rc1 release section"):
        verify_release_metadata(_metadata("v4.0.0rc1", changelog=_changelog("4.0.0")))


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_release_files(repository: Path, version: str, changelog: str) -> str:
    """Commit pyproject.toml and CHANGELOG.md through plumbing, so no signing is involved."""

    _git(repository, "init", "--quiet")
    (repository / "pyproject.toml").write_bytes(_project(version))
    (repository / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    _git(repository, "add", "pyproject.toml", "CHANGELOG.md")
    tree = _git(repository, "write-tree")
    return _git(
        repository,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit-tree",
        tree,
        "-m",
        f"Release {version}",
    )


def test_main_reads_the_tagged_commit_and_reports_the_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commit = _commit_release_files(tmp_path, "4.0.0", _changelog("4.0.0"))
    arguments = ["--repository", str(tmp_path), "--commit", commit]

    assert main([*arguments, "--tag", "v4.0.0"]) == 0
    assert capsys.readouterr().out == "release metadata verified for v4.0.0\n"

    assert main([*arguments, "--tag", "v4.0.1"]) == 1
    assert "does not match project version" in capsys.readouterr().err


def test_main_reports_a_commit_that_lacks_the_release_files(tmp_path: Path) -> None:
    _git(tmp_path, "init", "--quiet")
    assert main(["--repository", str(tmp_path), "--tag", "v4.0.0", "--commit", "HEAD"]) == 1
