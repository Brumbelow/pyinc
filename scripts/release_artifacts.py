"""Prepare, inspect, and compare release artifacts."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote
from urllib.request import Request, urlopen

_PROJECT_NAME = "pyinc"
_VERSION_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+){2}(?:(?:a|b|rc)[0-9]+)?")
_REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_CHECKSUM_LINE_PATTERN = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]*)")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class ReleaseArtifactError(ValueError):
    """Release artifacts do not satisfy the publication contract."""


@dataclass(frozen=True)
class PublishedArtifact:
    """One published distribution or GitHub Release asset."""

    name: str
    url: str
    sha256: str | None


def _reject(message: str) -> NoReturn:
    raise ReleaseArtifactError(message)


def _validated_version(version: str) -> str:
    if _VERSION_PATTERN.fullmatch(version) is None:
        _reject(f"invalid release version: {version!r}")
    return version


def distribution_names(version: str) -> tuple[str, str]:
    """Return the canonical sdist and universal-wheel names for a version."""

    version = _validated_version(version)
    return (f"pyinc-{version}.tar.gz", f"pyinc-{version}-py3-none-any.whl")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_distributions(directory: Path, version: str) -> tuple[Path, Path]:
    expected = frozenset(distribution_names(version))
    if not directory.is_dir():
        _reject(f"distribution directory does not exist: {directory}")
    observed = frozenset(path.name for path in directory.iterdir() if path.is_file())
    if observed != expected:
        _reject(
            "distribution directory must contain exactly "
            f"{', '.join(sorted(expected))}; found {', '.join(sorted(observed)) or 'nothing'}"
        )
    names = sorted(expected)
    return directory / names[0], directory / names[1]


def render_checksums(checksums: Mapping[str, str]) -> str:
    """Render canonical sha256sum-compatible contents."""

    for name, digest in checksums.items():
        if _CHECKSUM_LINE_PATTERN.fullmatch(f"{digest}  {name}") is None:
            _reject(f"invalid checksum entry for {name!r}")
    return "".join(f"{checksums[name]}  {name}\n" for name in sorted(checksums))


def parse_checksums(document: bytes) -> dict[str, str]:
    """Parse strict sha256sum-compatible contents without accepting duplicate names."""

    try:
        lines = document.decode("ascii").splitlines()
    except UnicodeDecodeError:
        _reject("SHA256SUMS must be ASCII")
    if not lines:
        _reject("SHA256SUMS must not be empty")
    checksums: dict[str, str] = {}
    for line in lines:
        match = _CHECKSUM_LINE_PATTERN.fullmatch(line)
        if match is None:
            _reject(f"malformed SHA256SUMS line: {line!r}")
        digest, name = match.groups()
        if name in checksums:
            _reject(f"duplicate SHA256SUMS entry: {name}")
        checksums[name] = digest
    return checksums


def write_checksums(directory: Path, version: str, output: Path) -> None:
    """Write checksums for the exact release sdist and wheel."""

    artifacts = _local_distributions(directory, version)
    contents = render_checksums({path.name: _sha256_file(path) for path in artifacts})
    output.write_text(contents, encoding="ascii", newline="\n")


def verify_checksums(directory: Path, version: str, checksum_path: Path) -> None:
    """Verify the exact release sdist and wheel against SHA256SUMS."""

    artifacts = _local_distributions(directory, version)
    expected_names = frozenset(path.name for path in artifacts)
    checksums = parse_checksums(checksum_path.read_bytes())
    if frozenset(checksums) != expected_names:
        _reject("SHA256SUMS must name exactly the release sdist and wheel")
    for path in artifacts:
        if _sha256_file(path) != checksums[path.name]:
            _reject(f"checksum mismatch for {path.name}")


def extract_release_notes(changelog: str, version: str) -> str:
    """Extract one dated release section from a Keep a Changelog document."""

    version = _validated_version(version)
    lines = changelog.splitlines()
    prefix = f"## [{version}]"
    starts = [index for index, line in enumerate(lines) if line.startswith(prefix)]
    if len(starts) != 1:
        _reject(f"CHANGELOG.md must contain exactly one {version} release section")
    start = starts[0]
    dated = re.fullmatch(
        rf"## \[{re.escape(version)}\] - (?P<date>\d{{4}}-\d{{2}}-\d{{2}})", lines[start]
    )
    if dated is None:
        _reject(f"the {version} release heading must include an ISO date")
    date = dated.group("date")
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        _reject(f"the {version} release heading names {date}, which is not a real date")
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("## [")),
        len(lines),
    )
    section = lines[start + 1 : end]
    while section and not section[0].strip():
        section.pop(0)
    while section and not section[-1].strip():
        section.pop()
    if not section:
        _reject(f"the {version} release notes must not be empty")
    return "\n".join(section) + "\n"


def write_release_notes(changelog: Path, version: str, output: Path) -> None:
    """Write the selected changelog section as GitHub Release notes."""

    output.write_text(
        extract_release_notes(changelog.read_text(encoding="utf-8"), version),
        encoding="utf-8",
        newline="\n",
    )


def _object(value: object, context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _reject(f"{context} must be a JSON object")
    return value


def _string(document: Mapping[str, object], key: str, context: str) -> str:
    value = document.get(key)
    if not isinstance(value, str):
        _reject(f"{context}.{key} must be a string")
    return value


def _request_json(url: str, token: str | None = None) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json, application/json",
        "User-Agent": "pyinc-release-validation",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=60) as response:  # noqa: S310 - fixed HTTPS API URLs
        payload = response.read()
    return _object(json.loads(payload), url)


def _download(url: str, destination: Path) -> str:
    request = Request(url, headers={"User-Agent": "pyinc-release-validation"})
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    digest = hashlib.sha256()
    try:
        with urlopen(request, timeout=120) as response, temporary.open("wb") as stream:  # noqa: S310
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                digest.update(chunk)
                stream.write(chunk)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return digest.hexdigest()


def _pypi_artifacts(document: Mapping[str, object], version: str) -> dict[str, PublishedArtifact]:
    expected_names = frozenset(distribution_names(version))
    info = _object(document.get("info"), "PyPI info")
    if _string(info, "name", "PyPI info").lower() != _PROJECT_NAME:
        _reject("PyPI project name is not pyinc")
    if _string(info, "version", "PyPI info") != version:
        _reject("PyPI project version does not match the requested version")
    entries = document.get("urls")
    if not isinstance(entries, list):
        _reject("PyPI urls must be a JSON array")
    artifacts: dict[str, PublishedArtifact] = {}
    expected_types = {
        f"pyinc-{version}.tar.gz": "sdist",
        f"pyinc-{version}-py3-none-any.whl": "bdist_wheel",
    }
    for raw_entry in entries:
        entry = _object(raw_entry, "PyPI file")
        name = _string(entry, "filename", "PyPI file")
        if name in artifacts:
            _reject(f"duplicate PyPI artifact: {name}")
        if name not in expected_names:
            _reject(f"unexpected PyPI artifact: {name}")
        if _string(entry, "packagetype", f"PyPI file {name}") != expected_types[name]:
            _reject(f"unexpected PyPI package type for {name}")
        digests = _object(entry.get("digests"), f"PyPI file {name} digests")
        digest = _string(digests, "sha256", f"PyPI file {name} digests")
        if _SHA256_PATTERN.fullmatch(digest) is None:
            _reject(f"invalid PyPI sha256 for {name}")
        artifacts[name] = PublishedArtifact(
            name=name,
            url=_string(entry, "url", f"PyPI file {name}"),
            sha256=digest,
        )
    if frozenset(artifacts) != expected_names:
        _reject("PyPI must publish exactly one sdist and one universal wheel")
    return artifacts


def _github_assets(
    document: Mapping[str, object], version: str
) -> tuple[dict[str, PublishedArtifact], PublishedArtifact]:
    tag = f"v{version}"
    if _string(document, "tag_name", "GitHub Release") != tag:
        _reject("GitHub Release tag does not match the requested version")
    if document.get("draft") is not False:
        _reject("GitHub Release must be published")
    prerelease = re.search(r"(?:a|b|rc)[0-9]+$", version) is not None
    if document.get("prerelease") is not prerelease:
        _reject("GitHub Release prerelease state does not match the version")
    raw_assets = document.get("assets")
    if not isinstance(raw_assets, list):
        _reject("GitHub Release assets must be a JSON array")
    expected_names = frozenset((*distribution_names(version), "SHA256SUMS"))
    assets: dict[str, PublishedArtifact] = {}
    for raw_asset in raw_assets:
        asset = _object(raw_asset, "GitHub Release asset")
        name = _string(asset, "name", "GitHub Release asset")
        if name in assets:
            _reject(f"duplicate GitHub Release asset: {name}")
        if name not in expected_names:
            _reject(f"unexpected GitHub Release asset: {name}")
        if asset.get("state") != "uploaded":
            _reject(f"GitHub Release asset is not uploaded: {name}")
        digest_value = asset.get("digest")
        digest: str | None = None
        if digest_value is not None:
            if not isinstance(digest_value, str) or not digest_value.startswith("sha256:"):
                _reject(f"invalid GitHub digest for {name}")
            digest = digest_value.removeprefix("sha256:")
            if _SHA256_PATTERN.fullmatch(digest) is None:
                _reject(f"invalid GitHub sha256 for {name}")
        assets[name] = PublishedArtifact(
            name=name,
            url=_string(asset, "browser_download_url", f"GitHub Release asset {name}"),
            sha256=digest,
        )
    if frozenset(assets) != expected_names:
        _reject("GitHub Release must contain exactly the sdist, wheel, and SHA256SUMS")
    checksum_asset = assets.pop("SHA256SUMS")
    return assets, checksum_asset


def verify_pypi_artifacts(version: str, directory: Path) -> None:
    """Verify local release distributions against PyPI's published hashes."""

    version = _validated_version(version)
    pypi_url = f"https://pypi.org/pypi/{_PROJECT_NAME}/{quote(version, safe='')}/json"
    published = _pypi_artifacts(_request_json(pypi_url), version)
    for path in _local_distributions(directory, version):
        if _sha256_file(path) != published[path.name].sha256:
            _reject(f"local artifact does not match PyPI for {path.name}")


def verify_published_artifacts(
    version: str,
    repository: str,
    output_directory: Path,
    *,
    github_token: str | None = None,
) -> None:
    """Download and compare PyPI distributions with their GitHub Release assets."""

    version = _validated_version(version)
    if _REPOSITORY_PATTERN.fullmatch(repository) is None:
        _reject(f"invalid GitHub repository: {repository!r}")
    pypi_url = f"https://pypi.org/pypi/{_PROJECT_NAME}/{quote(version, safe='')}/json"
    github_url = (
        f"https://api.github.com/repos/{repository}/releases/tags/"
        f"{quote(f'v{version}', safe='')}"
    )
    pypi = _pypi_artifacts(_request_json(pypi_url), version)
    github, checksum_asset = _github_assets(
        _request_json(github_url, token=github_token), version
    )

    pypi_hashes: dict[str, str] = {}
    for artifact in pypi.values():
        digest = _download(artifact.url, output_directory / "pypi" / artifact.name)
        if digest != artifact.sha256:
            _reject(f"downloaded PyPI sha256 does not match metadata for {artifact.name}")
        pypi_hashes[artifact.name] = digest

    checksum_path = output_directory / "github" / checksum_asset.name
    checksum_digest = _download(checksum_asset.url, checksum_path)
    if checksum_asset.sha256 is not None and checksum_digest != checksum_asset.sha256:
        _reject("downloaded SHA256SUMS does not match its GitHub digest")
    checksums = parse_checksums(checksum_path.read_bytes())
    if frozenset(checksums) != frozenset(pypi):
        _reject("SHA256SUMS must name exactly the PyPI sdist and wheel")

    for artifact in github.values():
        digest = _download(artifact.url, output_directory / "github" / artifact.name)
        if artifact.sha256 is not None and digest != artifact.sha256:
            _reject(f"downloaded GitHub sha256 does not match metadata for {artifact.name}")
        if digest != pypi_hashes[artifact.name]:
            _reject(f"PyPI and GitHub artifacts differ for {artifact.name}")
        if digest != checksums[artifact.name]:
            _reject(f"SHA256SUMS does not match {artifact.name}")


def verify_remote_assets(
    version: str,
    local_directory: Path,
    checksum_path: Path,
    remote_directory: Path,
) -> None:
    """Verify a downloaded GitHub Release asset set against the local release files."""

    local_artifacts = _local_distributions(local_directory, version)
    verify_checksums(local_directory, version, checksum_path)
    expected_names = frozenset((*distribution_names(version), "SHA256SUMS"))
    if not remote_directory.is_dir():
        _reject(f"remote asset directory does not exist: {remote_directory}")
    observed_names = frozenset(
        path.name for path in remote_directory.iterdir() if path.is_file()
    )
    if observed_names != expected_names:
        _reject("GitHub Release must contain exactly the sdist, wheel, and SHA256SUMS")

    remote_checksum_path = remote_directory / "SHA256SUMS"
    if remote_checksum_path.read_bytes() != checksum_path.read_bytes():
        _reject("GitHub Release SHA256SUMS differs from the verified local file")
    remote_checksums = parse_checksums(remote_checksum_path.read_bytes())
    if frozenset(remote_checksums) != frozenset(path.name for path in local_artifacts):
        _reject("GitHub Release SHA256SUMS must name exactly the sdist and wheel")

    for local_path in local_artifacts:
        remote_path = remote_directory / local_path.name
        local_digest = _sha256_file(local_path)
        if _sha256_file(remote_path) != local_digest:
            _reject(f"GitHub Release asset differs from the verified build: {local_path.name}")
        if remote_checksums[local_path.name] != local_digest:
            _reject(f"GitHub Release SHA256SUMS does not match {local_path.name}")


def verify_release_state(
    version: str,
    metadata_path: Path,
    notes_path: Path,
    release_list_path: Path,
) -> None:
    """Verify the user-visible state of an already-published GitHub Release."""

    version = _validated_version(version)
    metadata = _object(json.loads(metadata_path.read_bytes()), "GitHub Release metadata")
    prerelease = re.search(r"(?:a|b|rc)[0-9]+$", version) is not None
    expected_values: dict[str, object] = {
        "tagName": f"v{version}",
        "name": f"pyinc {version}",
        "isDraft": False,
        "isPrerelease": prerelease,
    }
    for key, expected in expected_values.items():
        if metadata.get(key) != expected:
            _reject(f"GitHub Release {key} does not match the expected published state")

    body = metadata.get("body")
    if not isinstance(body, str):
        _reject("GitHub Release body must be a string")
    expected_notes = notes_path.read_text(encoding="utf-8")
    normalized_body = body.replace("\r\n", "\n").rstrip("\n")
    normalized_notes = expected_notes.replace("\r\n", "\n").rstrip("\n")
    if normalized_body != normalized_notes:
        _reject("GitHub Release notes do not match the changelog section")

    release_list: object = json.loads(release_list_path.read_bytes())
    if not isinstance(release_list, list):
        _reject("GitHub Release list must be a JSON array")
    matching_releases = [
        _object(entry, "GitHub Release list entry")
        for entry in release_list
        if isinstance(entry, dict) and entry.get("tagName") == f"v{version}"
    ]
    if len(matching_releases) != 1:
        _reject("GitHub Release list must contain the release tag exactly once")
    if matching_releases[0].get("isLatest") != (not prerelease):
        _reject("GitHub Release latest state does not match the release version")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    write = subparsers.add_parser("write-checksums", help="write SHA256SUMS")
    write.add_argument("--version", required=True)
    write.add_argument("--directory", type=Path, required=True)
    write.add_argument("--output", type=Path, required=True)

    verify = subparsers.add_parser("verify-checksums", help="verify SHA256SUMS")
    verify.add_argument("--version", required=True)
    verify.add_argument("--directory", type=Path, required=True)
    verify.add_argument("--checksums", type=Path, required=True)

    notes = subparsers.add_parser("release-notes", help="extract changelog release notes")
    notes.add_argument("--version", required=True)
    notes.add_argument("--changelog", type=Path, required=True)
    notes.add_argument("--output", type=Path, required=True)

    published = subparsers.add_parser(
        "verify-published", help="compare PyPI and GitHub Release artifacts"
    )
    published.add_argument("--version", required=True)
    published.add_argument("--repository", required=True)
    published.add_argument("--output-directory", type=Path, required=True)

    pypi = subparsers.add_parser("verify-pypi", help="compare local artifacts with PyPI")
    pypi.add_argument("--version", required=True)
    pypi.add_argument("--directory", type=Path, required=True)

    remote = subparsers.add_parser(
        "verify-remote-assets", help="compare downloaded GitHub Release assets"
    )
    remote.add_argument("--version", required=True)
    remote.add_argument("--directory", type=Path, required=True)
    remote.add_argument("--checksums", type=Path, required=True)
    remote.add_argument("--remote-directory", type=Path, required=True)

    state = subparsers.add_parser(
        "verify-release-state", help="verify published GitHub Release metadata"
    )
    state.add_argument("--version", required=True)
    state.add_argument("--metadata", type=Path, required=True)
    state.add_argument("--notes", type=Path, required=True)
    state.add_argument("--release-list", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "write-checksums":
            write_checksums(arguments.directory, arguments.version, arguments.output)
        elif arguments.command == "verify-checksums":
            verify_checksums(arguments.directory, arguments.version, arguments.checksums)
        elif arguments.command == "release-notes":
            write_release_notes(arguments.changelog, arguments.version, arguments.output)
        elif arguments.command == "verify-published":
            verify_published_artifacts(
                arguments.version,
                arguments.repository,
                arguments.output_directory,
                github_token=os.environ.get("GH_TOKEN"),
            )
        elif arguments.command == "verify-pypi":
            verify_pypi_artifacts(arguments.version, arguments.directory)
        elif arguments.command == "verify-remote-assets":
            verify_remote_assets(
                arguments.version,
                arguments.directory,
                arguments.checksums,
                arguments.remote_directory,
            )
        elif arguments.command == "verify-release-state":
            verify_release_state(
                arguments.version,
                arguments.metadata,
                arguments.notes,
                arguments.release_list,
            )
        else:  # pragma: no cover - argparse constrains the command
            raise AssertionError(arguments.command)
    except (OSError, ReleaseArtifactError, json.JSONDecodeError) as error:
        print(f"release artifact verification failed: {error}", file=sys.stderr)
        return 1
    print(f"release artifact command completed: {arguments.command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
