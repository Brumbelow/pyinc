"""Verify the release metadata a tag names after the cryptographic trust checks.

A release tag ``vX.Y.Z`` (or a pre-release ``vX.Y.ZrcN``) must name the
``[project]`` version in ``pyproject.toml``, a dated ``## [X.Y.Z] - YYYY-MM-DD``
section in ``CHANGELOG.md``, and that version's release link at the foot of
the changelog.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

_TAG_PATTERN = re.compile(r"v(?P<version>\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?)")
_RELEASE_LINK = "[{version}]: https://github.com/Brumbelow/pyinc/releases/tag/v{version}"


class ReleaseMetadataError(ValueError):
    """The release metadata does not satisfy the release policy."""


@dataclass(frozen=True)
class ReleaseMetadata:
    tag: str
    project_version: str
    project_document: bytes
    changelog: bytes


def _reject(message: str) -> NoReturn:
    raise ReleaseMetadataError(message)


def _decode_document(document: bytes, path: str) -> str:
    try:
        return document.decode("utf-8")
    except UnicodeDecodeError:
        _reject(f"{path} must be valid UTF-8")


def _tag_version(tag: str) -> str:
    match = _TAG_PATTERN.fullmatch(tag)
    if match is None:
        _reject(f"release tag {tag!r} is not of the form vX.Y.Z or vX.Y.ZrcN")
    return match.group("version")


def _project_version(document: bytes) -> str:
    project = tomllib.loads(_decode_document(document, "pyproject.toml"))["project"]
    version = project["version"]
    if not isinstance(version, str):
        raise TypeError("project.version must be a string")
    return version


def _release_section(changelog: str, version: str) -> None:
    lines = changelog.splitlines()
    prefix = f"## [{version}]"
    starts = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    if len(starts) != 1:
        _reject(f"CHANGELOG.md must contain exactly one {version} release section")
    heading = lines[starts[0]]
    dated = re.fullmatch(
        rf"## \[{re.escape(version)}\] - (?P<date>\d{{4}}-\d{{2}}-\d{{2}})", heading
    )
    if dated is None:
        _reject(f"the {version} CHANGELOG.md heading must use '## [{version}] - YYYY-MM-DD'")
    date = dated.group("date")
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        _reject(f"the {version} CHANGELOG.md heading is dated {date}, which is not a real date")
    start = starts[0] + 1
    end = next(
        (index for index in range(start, len(lines)) if lines[index].startswith("## [")),
        len(lines),
    )
    if not any(line.strip() for line in lines[start:end]):
        _reject(f"the {version} CHANGELOG.md release section must not be empty")


def _release_link(changelog: str, version: str) -> None:
    expected = _RELEASE_LINK.format(version=version)
    prefix = f"[{version}]:"
    links = [line for line in changelog.splitlines() if line.startswith(prefix)]
    if len(links) != 1:
        _reject(f"CHANGELOG.md must contain exactly one {version} release link")
    if links[0] != expected:
        _reject(f"the {version} CHANGELOG.md release link must be exactly {expected!r}")


def verify_release_metadata(metadata: ReleaseMetadata) -> None:
    """Validate the release policy using only the supplied immutable metadata."""

    version = _tag_version(metadata.tag)
    if _project_version(metadata.project_document) != metadata.project_version:
        _reject("project metadata does not match the recorded project version")
    if metadata.project_version != version:
        _reject(
            f"release tag {metadata.tag!r} does not match project version "
            f"{metadata.project_version!r}"
        )
    changelog = _decode_document(metadata.changelog, "CHANGELOG.md")
    _release_section(changelog, version)
    _release_link(changelog, version)


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
    )
    return result.stdout


def load_repository_metadata(repository: Path, tag: str, commit: str) -> ReleaseMetadata:
    """Read the repository facts consumed by :func:`verify_release_metadata`."""

    project_document = _git_bytes(repository, "show", f"{commit}:pyproject.toml")
    return ReleaseMetadata(
        tag=tag,
        project_version=_project_version(project_document),
        project_document=project_document,
        changelog=_git_bytes(repository, "show", f"{commit}:CHANGELOG.md"),
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
