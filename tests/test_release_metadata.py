from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from scripts.verify_release_metadata import (
        FINAL_TAG,
        RC_TAG,
        FinalPromotionMetadata,
        ReleaseMetadata,
        ReleaseMetadataError,
        verify_release_metadata,
    )
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from verify_release_metadata import (  # noqa: E402
        FINAL_TAG,
        RC_TAG,
        FinalPromotionMetadata,
        ReleaseMetadata,
        ReleaseMetadataError,
        verify_release_metadata,
    )


RC_COMMIT = "1" * 40
FINAL_COMMIT = "2" * 40
VALIDATION_RECORD = f"""### Release validation

- RC candidate: `v3.0.0rc1` at `{RC_COMMIT}`
- [x] Clean installations from the published RC artifacts passed.
- [x] The benchmark/correctness report was reviewed; every pyinc result matched a fresh run.
- [x] Final promotion approved."""
RC_CHANGELOG_REFERENCE = "[3.0.0rc1]: https://github.com/Brumbelow/pyinc/releases/tag/v3.0.0rc1"
FINAL_CHANGELOG_REFERENCE = "[3.0.0]: https://github.com/Brumbelow/pyinc/releases/tag/v3.0.0"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _project(
    version: str,
    *,
    description: str = "Release fixture.",
    trailing_document: str = "",
) -> bytes:
    return f"""[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "pyinc"
version = "{version}"
description = "{description}"
dependencies = []
{trailing_document}""".encode()


def _rc_changelog(rc_entry: str = "- Release candidate.") -> str:
    return f"""# Changelog

## [Unreleased]

## [3.0.0rc1] - 2026-07-09

{rc_entry}

{RC_CHANGELOG_REFERENCE}
"""


def _changelog(record: str = VALIDATION_RECORD) -> str:
    return f"""# Changelog

## [Unreleased]

## [3.0.0] - 2026-07-10

### Changed

- Promoted the validated release candidate.

{record}

### Security

- Release signatures were verified.

## [3.0.0rc1] - 2026-07-09

- Release candidate.

{RC_CHANGELOG_REFERENCE}
{FINAL_CHANGELOG_REFERENCE}
"""


def _promotion(
    *,
    rc_tag: str = RC_TAG,
    rc_version: str = "3.0.0rc1",
    rc_commit: str = RC_COMMIT,
    parent_commits: tuple[str, ...] = (RC_COMMIT,),
    changed_paths: tuple[str, ...] = ("pyproject.toml", "CHANGELOG.md"),
    rc_project_document: bytes | None = None,
    rc_changelog: bytes | None = None,
) -> FinalPromotionMetadata:
    return FinalPromotionMetadata(
        rc_tag=rc_tag,
        rc_version=rc_version,
        rc_commit=rc_commit,
        parent_commits=parent_commits,
        changed_paths=changed_paths,
        rc_project_document=(
            _project("3.0.0rc1") if rc_project_document is None else rc_project_document
        ),
        rc_changelog=(_rc_changelog().encode() if rc_changelog is None else rc_changelog),
    )


class _DefaultPromotion:
    pass


_DEFAULT_PROMOTION = _DefaultPromotion()


def _final_metadata(
    *,
    release_commit: str = FINAL_COMMIT,
    changelog: str | None = None,
    project_document: bytes | None = None,
    final_promotion: FinalPromotionMetadata | None | _DefaultPromotion = _DEFAULT_PROMOTION,
) -> ReleaseMetadata:
    promotion = _promotion() if isinstance(final_promotion, _DefaultPromotion) else final_promotion
    return ReleaseMetadata(
        tag=FINAL_TAG,
        project_version="3.0.0",
        release_commit=release_commit,
        project_document=(_project("3.0.0") if project_document is None else project_document),
        changelog=(_changelog() if changelog is None else changelog).encode(),
        final_promotion=promotion,
    )


def test_accepts_exact_release_candidate_metadata() -> None:
    verify_release_metadata(
        ReleaseMetadata(
            tag=RC_TAG,
            project_version="3.0.0rc1",
            release_commit=RC_COMMIT,
            project_document=_project("3.0.0rc1"),
            changelog=_rc_changelog().encode(),
        )
    )


@pytest.mark.parametrize(
    ("tag", "version"),
    [("v3.0.1", "3.0.1"), ("v3.1.0rc1", "3.1.0rc1")],
)
def test_accepts_matching_non_promotion_tags(tag: str, version: str) -> None:
    verify_release_metadata(
        ReleaseMetadata(
            tag=tag,
            project_version=version,
            release_commit=FINAL_COMMIT,
            project_document=_project(version),
            changelog=f"# Changelog\n\n## [{version}] - 2026-07-09\n\n- Release.\n".encode(),
        )
    )


def test_repository_release_metadata_is_self_consistent() -> None:
    project_document = (PROJECT_ROOT / "pyproject.toml").read_bytes()
    project_version = tomllib.loads(project_document.decode("utf-8"))["project"]["version"]
    assert isinstance(project_version, str)

    verify_release_metadata(
        ReleaseMetadata(
            tag=f"v{project_version}",
            project_version=project_version,
            release_commit=FINAL_COMMIT,
            project_document=project_document,
            changelog=(PROJECT_ROOT / "CHANGELOG.md").read_bytes(),
        )
    )


@pytest.mark.parametrize(
    "changelog",
    [
        b"# Changelog\n",
        b"# Changelog\n\n## [3.0.0rc2] - 2026-07-09\n\n- Wrong RC.\n",
        b"# Changelog\n\n## [3.0.0rc1]\n\n- Missing date.\n",
        b"# Changelog\n\n## [3.0.0rc1] - 2026-02-30\n\n- Impossible date.\n",
        b"# Changelog\n\n## [3.0.0rc1] - 2026-07-09\n\n",
        (
            b"# Changelog\n\n## [3.0.0rc1] - 2026-07-09\n\n- First.\n\n"
            b"## [3.0.0rc1] - 2026-07-10\n\n- Second.\n"
        ),
    ],
)
def test_rejects_missing_mismatched_malformed_empty_or_duplicate_rc_section(
    changelog: bytes,
) -> None:
    with pytest.raises(ReleaseMetadataError, match="CHANGELOG.md"):
        verify_release_metadata(
            ReleaseMetadata(
                tag=RC_TAG,
                project_version="3.0.0rc1",
                release_commit=RC_COMMIT,
                project_document=_project("3.0.0rc1"),
                changelog=changelog,
            )
        )


def test_rejects_non_utf8_release_changelog() -> None:
    with pytest.raises(ReleaseMetadataError, match="valid UTF-8"):
        verify_release_metadata(
            ReleaseMetadata(
                tag=RC_TAG,
                project_version="3.0.0rc1",
                release_commit=RC_COMMIT,
                project_document=_project("3.0.0rc1"),
                changelog=b"\xff",
            )
        )


def test_rejects_tag_that_does_not_match_project_version() -> None:
    with pytest.raises(ReleaseMetadataError, match="does not match project version"):
        verify_release_metadata(
            ReleaseMetadata(
                tag="v3.0.1",
                project_version="3.0.2",
                release_commit=FINAL_COMMIT,
                project_document=_project("3.0.2"),
                changelog=b"",
            )
        )


def test_rejects_retired_release_version() -> None:
    with pytest.raises(ReleaseMetadataError, match="3.1.1.*retired.*must not be reused"):
        verify_release_metadata(
            ReleaseMetadata(
                tag="v3.1.1",
                project_version="3.1.1",
                release_commit=FINAL_COMMIT,
                project_document=_project("3.1.1"),
                changelog=b"# Changelog\n\n## [3.1.1] - 2026-08-03\n\n- Cancelled.\n",
            )
        )


def test_rejects_unexpected_3_0_release_candidate() -> None:
    with pytest.raises(ReleaseMetadataError, match="must be exactly"):
        verify_release_metadata(
            ReleaseMetadata(
                tag="v3.0.0rc2",
                project_version="3.0.0rc2",
                release_commit=FINAL_COMMIT,
                project_document=_project("3.0.0rc2"),
                changelog=b"",
            )
        )


def test_accepts_exact_final_promotion_metadata() -> None:
    verify_release_metadata(_final_metadata())


@pytest.mark.parametrize(
    "changelog",
    [
        _changelog().replace(f"{FINAL_CHANGELOG_REFERENCE}\n", ""),
        _changelog().replace(
            FINAL_CHANGELOG_REFERENCE,
            "[3.0.0]: https://github.com/Brumbelow/pyinc/releases/tag/v3.0.1",
        ),
        _changelog() + f"{FINAL_CHANGELOG_REFERENCE}\n",
        _changelog()
        .replace(
            f"{FINAL_CHANGELOG_REFERENCE}\n",
            "",
        )
        .replace(
            "## [3.0.0rc1] - 2026-07-09",
            f"{FINAL_CHANGELOG_REFERENCE}\n\n## [3.0.0rc1] - 2026-07-09",
        ),
    ],
)
def test_rejects_missing_changed_duplicate_or_misplaced_final_reference_link(
    changelog: str,
) -> None:
    with pytest.raises(ReleaseMetadataError, match="canonical reference link"):
        verify_release_metadata(_final_metadata(changelog=changelog))


def test_accepts_scoped_project_version_change_when_another_table_matches() -> None:
    trailing_rc = '\n[tool.fixture]\nversion = "3.0.0rc1"\n'
    trailing_final = '\n[tool.fixture]\nversion = "3.0.0rc1"\n'
    promotion = _promotion(rc_project_document=_project("3.0.0rc1", trailing_document=trailing_rc))
    verify_release_metadata(
        _final_metadata(
            project_document=_project("3.0.0", trailing_document=trailing_final),
            final_promotion=promotion,
        )
    )


def test_rejects_final_without_promotion_metadata() -> None:
    with pytest.raises(ReleaseMetadataError, match="requires .* promotion metadata"):
        verify_release_metadata(_final_metadata(final_promotion=None))


def test_rejects_wrong_release_candidate_tag() -> None:
    with pytest.raises(ReleaseMetadataError, match="must use RC tag"):
        verify_release_metadata(_final_metadata(final_promotion=_promotion(rc_tag="v3.0.0rc2")))


def test_rejects_wrong_release_candidate_version() -> None:
    with pytest.raises(ReleaseMetadataError, match="must carry project version"):
        verify_release_metadata(_final_metadata(final_promotion=_promotion(rc_version="3.0.0rc2")))


def test_rejects_noncanonical_final_commit() -> None:
    with pytest.raises(ReleaseMetadataError, match="final release commit"):
        verify_release_metadata(_final_metadata(release_commit="ABC123"))


def test_rejects_noncanonical_release_candidate_commit() -> None:
    with pytest.raises(ReleaseMetadataError, match="RC commit"):
        verify_release_metadata(_final_metadata(final_promotion=_promotion(rc_commit="ABC123")))


@pytest.mark.parametrize(
    "parents",
    [(), ("3" * 40,), (RC_COMMIT, "3" * 40)],
)
def test_rejects_final_that_is_not_direct_non_merge_child(
    parents: tuple[str, ...],
) -> None:
    with pytest.raises(ReleaseMetadataError, match="direct non-merge child"):
        verify_release_metadata(_final_metadata(final_promotion=_promotion(parent_commits=parents)))


@pytest.mark.parametrize(
    "paths",
    [
        ("CHANGELOG.md",),
        ("pyproject.toml",),
        ("CHANGELOG.md", "pyproject.toml", "README.md"),
        ("CHANGELOG.md", "CHANGELOG.md"),
    ],
)
def test_rejects_any_non_exact_final_changed_path_set(paths: tuple[str, ...]) -> None:
    with pytest.raises(ReleaseMetadataError, match="may change exactly"):
        verify_release_metadata(_final_metadata(final_promotion=_promotion(changed_paths=paths)))


@pytest.mark.parametrize(
    "project_document",
    [
        _project("3.0.0", description="Changed during final promotion."),
        _project("3.0.0") + b"\n[tool.final]\nenabled = true\n",
        _project("3.0.0").replace(b"dependencies = []", b'dependencies = ["packaging"]'),
    ],
)
def test_rejects_arbitrary_final_pyproject_edits(project_document: bytes) -> None:
    with pytest.raises(ReleaseMetadataError, match="only the .* version changed"):
        verify_release_metadata(_final_metadata(project_document=project_document))


def test_rejects_global_replacement_of_ambiguous_version_text() -> None:
    trailing = '\n[tool.fixture]\nversion = "3.0.0rc1"\n'
    rc_project = _project("3.0.0rc1", trailing_document=trailing)
    globally_replaced = rc_project.replace(b"3.0.0rc1", b"3.0.0")
    with pytest.raises(ReleaseMetadataError, match="only the .* version changed"):
        verify_release_metadata(
            _final_metadata(
                project_document=globally_replaced,
                final_promotion=_promotion(rc_project_document=rc_project),
            )
        )


def test_rejects_project_version_record_that_disagrees_with_document() -> None:
    metadata = _final_metadata(project_document=_project("3.0.0rc1"))
    with pytest.raises(ReleaseMetadataError, match="recorded project version"):
        verify_release_metadata(metadata)


def test_rejects_rc_version_record_that_disagrees_with_tag_document() -> None:
    promotion = _promotion(rc_project_document=_project("3.0.0"))
    with pytest.raises(ReleaseMetadataError, match="recorded RC version"):
        verify_release_metadata(_final_metadata(final_promotion=promotion))


@pytest.mark.parametrize(
    "changelog",
    [
        _changelog().replace("## [3.0.0] - 2026-07-10", "## [3.0.1] - 2026-07-10"),
        _changelog() + "\n## [3.0.0] - 2026-07-11\n",
    ],
)
def test_rejects_missing_or_duplicate_final_release_section(changelog: str) -> None:
    with pytest.raises(ReleaseMetadataError, match="exactly one 3.0.0 release section"):
        verify_release_metadata(_final_metadata(changelog=changelog))


@pytest.mark.parametrize(
    "changelog",
    [
        _changelog().replace("- Release candidate.", "- Rewritten release candidate."),
        _changelog().replace("# Changelog", "# Project history"),
        _changelog() + "\n## [2.0.0] - 2020-01-01\n\n- Rewritten history.\n",
    ],
)
def test_rejects_changelog_history_rewrites(changelog: str) -> None:
    with pytest.raises(ReleaseMetadataError, match="RC document plus exactly one"):
        verify_release_metadata(_final_metadata(changelog=changelog))


@pytest.mark.parametrize(
    "record",
    [
        VALIDATION_RECORD.replace("### Release validation", "### Validation"),
        f"{VALIDATION_RECORD}\n\n### Release validation",
    ],
)
def test_rejects_missing_or_duplicate_validation_heading(record: str) -> None:
    with pytest.raises(ReleaseMetadataError, match="exactly one Release validation heading"):
        verify_release_metadata(_final_metadata(changelog=_changelog(record)))


@pytest.mark.parametrize(
    "record",
    [
        VALIDATION_RECORD.replace("v3.0.0rc1", "v3.0.0rc2"),
        VALIDATION_RECORD.replace(RC_COMMIT, "3" * 40),
        VALIDATION_RECORD.replace(
            "- [x] Final promotion approved.", "- [ ] Final promotion approved."
        ),
        VALIDATION_RECORD.replace(
            "every pyinc result matched a fresh run.",
            "one incremental result was not reviewed.",
        ),
        VALIDATION_RECORD.replace(
            "- [x] Final promotion approved.",
            "- [x] Final promotion approved.\n- [x] An undocumented extra check.",
        ),
        VALIDATION_RECORD.replace(
            "- [x] Clean installations from the published RC artifacts passed.\n", ""
        ),
    ],
)
def test_rejects_any_non_exact_validation_record(record: str) -> None:
    with pytest.raises(ReleaseMetadataError, match="not exact or complete"):
        verify_release_metadata(_final_metadata(changelog=_changelog(record)))
