"""Verify deterministic release metadata after cryptographic trust checks."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import NoReturn

RC_TAG = "v3.0.0rc1"
RC_VERSION = "3.0.0rc1"
FINAL_TAG = "v3.0.0"
FINAL_VERSION = "3.0.0"
FINAL_CHANGELOG_REFERENCE = b"[3.0.0]: https://github.com/Brumbelow/pyinc/releases/tag/v3.0.0"
FINAL_CHANGED_PATHS = frozenset({"CHANGELOG.md", "pyproject.toml"})
RETIRED_RELEASE_VERSIONS = frozenset({"3.1.1"})
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_PROJECT_TABLE_PATTERN = re.compile(rb"(?m)^\[project\][ \t]*(?:#[^\r\n]*)?\r?$")
_TABLE_PATTERN = re.compile(rb"(?m)^\[\[?[^\r\n]+\]\]?[ \t]*(?:#[^\r\n]*)?\r?$")
_PROJECT_VERSION_PATTERN = re.compile(
    rb"(?m)^[ \t]*version[ \t]*=[ \t]*(?P<quote>['\"])(?P<version>3\.0\.0rc1)"
    rb"(?P=quote)[ \t]*(?:#[^\r\n]*)?\r?$"
)


class ReleaseMetadataError(ValueError):
    """The release metadata does not satisfy the promotion policy."""


@dataclass(frozen=True)
class FinalPromotionMetadata:
    rc_tag: str
    rc_version: str
    rc_commit: str
    parent_commits: tuple[str, ...]
    changed_paths: tuple[str, ...]
    rc_project_document: bytes
    rc_changelog: bytes


@dataclass(frozen=True)
class ReleaseMetadata:
    tag: str
    project_version: str
    release_commit: str
    project_document: bytes
    changelog: bytes
    final_promotion: FinalPromotionMetadata | None = None


def _reject(message: str) -> NoReturn:
    raise ReleaseMetadataError(message)


def _validation_record(rc_commit: str) -> tuple[str, ...]:
    return (
        "### Release validation",
        "",
        f"- RC candidate: `{RC_TAG}` at `{rc_commit}`",
        "- [x] Clean installations from the published RC artifacts passed.",
        "- [x] The benchmark/correctness report was reviewed; every pyinc result "
        "matched a fresh run.",
        "- [x] Final promotion approved.",
    )


def _decode_document(document: bytes, path: str) -> str:
    try:
        return document.decode("utf-8")
    except UnicodeDecodeError:
        _reject(f"{path} must be valid UTF-8")


def _release_section(changelog: str, version: str) -> tuple[str, ...]:
    lines = changelog.splitlines()
    prefix = f"## [{version}]"
    starts = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    if len(starts) != 1:
        _reject(f"CHANGELOG.md must contain exactly one {version} release section")
    heading = lines[starts[0]]
    match = re.fullmatch(
        rf"## \[{re.escape(version)}\] - (?P<date>\d{{4}}-\d{{2}}-\d{{2}})",
        heading,
    )
    if match is None:
        _reject(f"the {version} CHANGELOG.md heading must use '## [{version}] - YYYY-MM-DD'")
    try:
        date.fromisoformat(match.group("date"))
    except ValueError:
        _reject(f"the {version} CHANGELOG.md heading must contain a valid calendar date")
    start = starts[0] + 1
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## [")),
        len(lines),
    )
    section = tuple(lines[start:end])
    if not any(line.strip() for line in section):
        _reject(f"the {version} CHANGELOG.md release section must not be empty")
    return section


def _verify_validation_record(changelog: str, rc_commit: str) -> None:
    section = _release_section(changelog, FINAL_VERSION)
    headings = [index for index, line in enumerate(section) if line == "### Release validation"]
    if len(headings) != 1:
        _reject("the 3.0.0 section must contain exactly one Release validation heading")
    start = headings[0]
    end = next(
        (index for index in range(start + 1, len(section)) if section[index].startswith("### ")),
        len(section),
    )
    actual = list(section[start:end])
    while actual and actual[-1] == "":
        actual.pop()
    if tuple(actual) != _validation_record(rc_commit):
        _reject("the 3.0.0 Release validation record is not exact or complete")


def _expected_final_project_document(rc_document: bytes) -> bytes:
    project_tables = tuple(_PROJECT_TABLE_PATTERN.finditer(rc_document))
    if len(project_tables) != 1:
        _reject("RC pyproject.toml must contain exactly one [project] table")
    project_table = project_tables[0]
    next_table = _TABLE_PATTERN.search(rc_document, project_table.end())
    section_end = len(rc_document) if next_table is None else next_table.start()
    version_assignments = tuple(
        _PROJECT_VERSION_PATTERN.finditer(rc_document, project_table.end(), section_end)
    )
    if len(version_assignments) != 1:
        _reject(
            "RC pyproject.toml must contain exactly one canonical 3.0.0rc1 "
            "[project] version assignment"
        )
    version = version_assignments[0].span("version")
    return rc_document[: version[0]] + FINAL_VERSION.encode() + rc_document[version[1] :]


def _verify_project_promotion(project_document: bytes, promotion: FinalPromotionMetadata) -> None:
    rc_document_version = _project_version(promotion.rc_project_document)
    if rc_document_version != promotion.rc_version:
        _reject("RC project metadata does not match the recorded RC version")
    expected = _expected_final_project_document(promotion.rc_project_document)
    if project_document != expected:
        _reject(
            "final pyproject.toml must equal the RC document with only the "
            "[project] version changed to 3.0.0"
        )


def _verify_changelog_promotion(changelog: bytes, rc_changelog: bytes) -> str:
    final_prefix = f"## [{FINAL_VERSION}] - ".encode()
    lines = changelog.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.startswith(final_prefix)]
    if len(starts) != 1:
        _reject(f"CHANGELOG.md must contain exactly one {FINAL_VERSION} release section")
    start = starts[0]
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith(b"## [")),
        len(lines),
    )
    without_final = b"".join((*lines[:start], *lines[end:]))
    if rc_changelog.endswith(b"\r\n"):
        newline = b"\r\n"
    elif rc_changelog.endswith(b"\n"):
        newline = b"\n"
    else:
        _reject("RC CHANGELOG.md must end with a newline")
    expected = rc_changelog + FINAL_CHANGELOG_REFERENCE + newline
    if without_final != expected:
        _reject(
            "final CHANGELOG.md must equal the RC document plus exactly one new "
            "3.0.0 section and its canonical reference link"
        )
    return _decode_document(changelog, "CHANGELOG.md")


def verify_release_metadata(metadata: ReleaseMetadata) -> None:
    """Validate release policy using only the supplied immutable metadata."""

    document_version = _project_version(metadata.project_document)
    if document_version != metadata.project_version:
        _reject("project metadata does not match the recorded project version")
    if metadata.project_version in RETIRED_RELEASE_VERSIONS:
        _reject(
            f"release version {metadata.project_version!r} is retired and must not be reused"
        )
    expected_tag = f"v{metadata.project_version}"
    if metadata.tag != expected_tag:
        _reject(
            f"release tag {metadata.tag!r} does not match project version "
            f"{metadata.project_version!r}"
        )

    is_3_0_rc = metadata.tag.startswith("v3.0.0rc") or metadata.project_version.startswith(
        "3.0.0rc"
    )
    if is_3_0_rc and (metadata.tag, metadata.project_version) != (RC_TAG, RC_VERSION):
        _reject(f"the 3.0 release candidate must be exactly {RC_TAG} / {RC_VERSION}")

    changelog = _decode_document(metadata.changelog, "CHANGELOG.md")
    _release_section(changelog, metadata.project_version)

    if metadata.tag != FINAL_TAG:
        return

    promotion = metadata.final_promotion
    if promotion is None:
        _reject(f"{FINAL_TAG} requires {RC_TAG} promotion metadata")
    if promotion.rc_tag != RC_TAG:
        _reject(f"final promotion must use RC tag {RC_TAG}")
    if promotion.rc_version != RC_VERSION:
        _reject(f"{RC_TAG} must carry project version {RC_VERSION}")
    if _COMMIT_PATTERN.fullmatch(metadata.release_commit) is None:
        _reject("the final release commit must be a full lowercase 40-character SHA")
    if _COMMIT_PATTERN.fullmatch(promotion.rc_commit) is None:
        _reject("the RC commit must be a full lowercase 40-character SHA")
    if promotion.parent_commits != (promotion.rc_commit,):
        _reject("the final release commit must be the RC commit's direct non-merge child")
    if (
        len(promotion.changed_paths) != len(FINAL_CHANGED_PATHS)
        or frozenset(promotion.changed_paths) != FINAL_CHANGED_PATHS
    ):
        _reject("final promotion may change exactly CHANGELOG.md and pyproject.toml")
    _verify_project_promotion(metadata.project_document, promotion)
    promoted_changelog = _verify_changelog_promotion(metadata.changelog, promotion.rc_changelog)
    _verify_validation_record(promoted_changelog, promotion.rc_commit)


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
    )
    return result.stdout


def _project_version(document: bytes) -> str:
    project = tomllib.loads(_decode_document(document, "pyproject.toml"))["project"]
    version = project["version"]
    if not isinstance(version, str):
        raise TypeError("project.version must be a string")
    return version


def load_repository_metadata(repository: Path, tag: str, commit: str) -> ReleaseMetadata:
    """Read the repository facts consumed by :func:`verify_release_metadata`."""

    project_document = _git_bytes(repository, "show", f"{commit}:pyproject.toml")
    project_version = _project_version(project_document)
    changelog = _git_bytes(repository, "show", f"{commit}:CHANGELOG.md")
    promotion: FinalPromotionMetadata | None = None
    if tag == FINAL_TAG:
        rc_commit = _git(repository, "rev-parse", f"{RC_TAG}^{{commit}}")
        rc_project_document = _git_bytes(repository, "show", f"{RC_TAG}:pyproject.toml")
        rc_version = _project_version(rc_project_document)
        rc_changelog = _git_bytes(repository, "show", f"{RC_TAG}:CHANGELOG.md")
        parents = tuple(_git(repository, "show", "-s", "--format=%P", commit).split())
        changed_paths = tuple(
            line
            for line in _git(repository, "diff", "--name-only", rc_commit, commit).splitlines()
            if line
        )
        promotion = FinalPromotionMetadata(
            rc_tag=RC_TAG,
            rc_version=rc_version,
            rc_commit=rc_commit,
            parent_commits=parents,
            changed_paths=changed_paths,
            rc_project_document=rc_project_document,
            rc_changelog=rc_changelog,
        )
    return ReleaseMetadata(
        tag=tag,
        project_version=project_version,
        release_commit=commit,
        project_document=project_document,
        changelog=changelog,
        final_promotion=promotion,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        metadata = load_repository_metadata(
            arguments.repository.resolve(), arguments.tag, arguments.commit
        )
        verify_release_metadata(metadata)
    except (
        OSError,
        ReleaseMetadataError,
        subprocess.CalledProcessError,
        tomllib.TOMLDecodeError,
        TypeError,
    ) as error:
        print(f"release metadata verification failed: {error}", file=sys.stderr)
        return 1
    print(f"release metadata verified for {arguments.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
