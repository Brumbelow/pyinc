"""Build the release twice and emit local SBOM and provenance metadata."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NoReturn, cast

if TYPE_CHECKING:
    from scripts import check_toolchain
else:
    try:
        from scripts import check_toolchain
    except ModuleNotFoundError:
        import check_toolchain

_VERSION_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+){2}(?:(?:a|b|rc)[0-9]+)?")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_CHECKSUM_LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]*)")
_PROJECT = "pyinc"
_REPOSITORY = "https://github.com/Brumbelow/pyinc"
_MAX_ARCHIVE_MEMBER_BYTES = 256 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
_BUILD_ENVIRONMENT = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONHASHSEED": "0",
    "PYTHONNOUSERSITE": "1",
}
_PRESERVED_ENVIRONMENT = (
    "COMSPEC",
    "HOME",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SystemRoot",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
)
_GITHUB_RUNNER_REQUIRED = (
    "ImageOS",
    "ImageVersion",
    "RUNNER_ARCH",
    "RUNNER_OS",
)
_GITHUB_RUNNER_OPTIONAL = (
    "ImageRelease",
    "RUNNER_NAME",
)


class ReproducibleBuildError(RuntimeError):
    """A release source or one of its two builds was not reproducible."""


@dataclass(frozen=True)
class GitState:
    """Exact source identity and deterministic timestamp for a clean checkout."""

    commit_sha: str
    source_date_epoch: int


@dataclass(frozen=True)
class Artifact:
    """One byte-identical distribution produced by both build runs."""

    name: str
    payload: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


@dataclass(frozen=True)
class ReleaseBuild:
    """Paths and identities emitted after both build runs agree."""

    version: str
    commit_sha: str
    source_date_epoch: int
    artifacts: tuple[Artifact, ...]
    dist_directory: Path
    metadata_directory: Path


def _reject(message: str) -> NoReturn:
    raise ReproducibleBuildError(message)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(document: object) -> bytes:
    return (
        json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _run(
    argv: Sequence[str], *, cwd: Path, environment: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=None if environment is None else dict(environment),
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        _reject(f"could not execute {argv[0]!r}: {type(exc).__name__}: {exc}")


def _git(project_root: Path, arguments: Sequence[str]) -> bytes:
    result = _run(("git", "-C", os.fspath(project_root), *arguments), cwd=project_root)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip() or "no stderr"
        _reject(f"git {' '.join(arguments)} failed with {result.returncode}: {detail}")
    return result.stdout


def _git_state(project_root: Path) -> GitState:
    status = _git(project_root, ("status", "--porcelain=v1", "--untracked-files=all"))
    if status:
        _reject("reproducible release builds require a clean working tree")
    raw_commit = _git(project_root, ("rev-parse", "--verify", "HEAD")).strip()
    raw_epoch = _git(project_root, ("show", "-s", "--format=%ct", "HEAD")).strip()
    try:
        commit = raw_commit.decode("ascii").lower()
        epoch = int(raw_epoch.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        _reject("git returned an invalid commit identity or timestamp")
    if _COMMIT_PATTERN.fullmatch(commit) is None or epoch < 0:
        _reject("git returned an invalid commit identity or timestamp")
    return GitState(commit_sha=commit, source_date_epoch=epoch)


def _project_version(project_root: Path) -> str:
    document = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    project = document.get("project")
    if not isinstance(project, dict) or project.get("name") != _PROJECT:
        _reject("pyproject.toml must describe the pyinc project")
    version = project.get("version")
    if not isinstance(version, str) or _VERSION_PATTERN.fullmatch(version) is None:
        _reject("pyproject.toml contains an invalid release version")
    if project.get("dependencies") != []:
        _reject("release builds require the documented zero runtime dependencies")
    return version


def _source_archive(project_root: Path) -> bytes:
    return _git(project_root, ("archive", "--format=tar", "HEAD"))


def _extract_source(payload: bytes, destination: Path) -> None:
    destination.mkdir(parents=True)
    seen: set[PurePosixPath] = set()
    total = 0
    try:
        archive = tarfile.open(fileobj=io.BytesIO(payload), mode="r:")  # noqa: SIM115
    except tarfile.TarError as exc:
        _reject(f"git archive output is invalid: {exc}")
    with archive:
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            if (
                not member.name
                or relative.is_absolute()
                or ".." in relative.parts
                or relative in seen
            ):
                _reject(f"git archive contains an unsafe or duplicate path: {member.name!r}")
            seen.add(relative)
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                _reject(f"git archive contains a non-file entry: {member.name!r}")
            if member.size < 0 or member.size > _MAX_ARCHIVE_MEMBER_BYTES:
                _reject(f"git archive member exceeds its size limit: {member.name!r}")
            total += member.size
            if total > _MAX_ARCHIVE_BYTES:
                _reject("git archive exceeds its total extraction limit")
            source = archive.extractfile(member)
            if source is None:
                _reject(f"git archive member could not be read: {member.name!r}")
            data = source.read(_MAX_ARCHIVE_MEMBER_BYTES + 1)
            if len(data) != member.size:
                _reject(f"git archive member length is inconsistent: {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)


def _build_environment(source_date_epoch: int) -> dict[str, str]:
    environment = {name: os.environ[name] for name in _PRESERVED_ENVIRONMENT if name in os.environ}
    environment.update(_BUILD_ENVIRONMENT)
    environment["SOURCE_DATE_EPOCH"] = str(source_date_epoch)
    return environment


def _run_build(source: Path, output: Path, source_date_epoch: int) -> None:
    output.mkdir()
    result = _run(
        (
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--sdist",
            "--wheel",
            "--outdir",
            os.fspath(output),
        ),
        cwd=source,
        environment=_build_environment(source_date_epoch),
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        if not detail:
            detail = result.stdout.decode("utf-8", errors="replace").strip() or "no output"
        _reject(f"isolated release build failed with {result.returncode}: {detail}")


def artifact_names(version: str) -> tuple[str, str]:
    """Return the exact sdist and universal-wheel names for a release."""
    if _VERSION_PATTERN.fullmatch(version) is None:
        _reject(f"invalid release version: {version!r}")
    return (f"pyinc-{version}.tar.gz", f"pyinc-{version}-py3-none-any.whl")


def metadata_names(version: str) -> tuple[str, str, str]:
    """Return the two metadata documents and their checksum manifest."""
    if _VERSION_PATTERN.fullmatch(version) is None:
        _reject(f"invalid release version: {version!r}")
    return (
        f"pyinc-{version}.provenance.json",
        f"pyinc-{version}.spdx.json",
        "BUILD-METADATA-SHA256SUMS",
    )


def compare_builds(first: Path, second: Path, version: str) -> tuple[Artifact, ...]:
    """Require both build directories to contain exactly equal release bytes."""
    expected = frozenset(artifact_names(version))
    artifacts: list[Artifact] = []
    for directory in (first, second):
        observed = frozenset(path.name for path in directory.iterdir() if path.is_file())
        if observed != expected:
            _reject(
                f"build directory must contain exactly {', '.join(sorted(expected))}; "
                f"found {', '.join(sorted(observed)) or 'nothing'}"
            )
    for name in sorted(expected):
        first_payload = (first / name).read_bytes()
        second_payload = (second / name).read_bytes()
        if first_payload != second_payload:
            _reject(
                f"isolated builds differ for {name}: "
                f"{_sha256(first_payload)} != {_sha256(second_payload)}"
            )
        artifacts.append(Artifact(name=name, payload=first_payload))
    return tuple(artifacts)


def _created_at(source_date_epoch: int) -> str:
    return datetime.fromtimestamp(source_date_epoch, UTC).isoformat().replace("+00:00", "Z")


def _spdx_document(version: str, state: GitState) -> dict[str, object]:
    package_id = "SPDXRef-Package-pyinc"
    return {
        "SPDXID": "SPDXRef-DOCUMENT",
        "creationInfo": {
            "created": _created_at(state.source_date_epoch),
            "creators": [
                "Person: Andrew Brumbelow",
                "Tool: pyinc scripts/reproducible_builds.py",
            ],
        },
        "dataLicense": "CC0-1.0",
        "documentNamespace": (
            f"{_REPOSITORY}/releases/download/v{version}/"
            f"pyinc-{version}.spdx.json#{state.commit_sha}"
        ),
        "name": f"pyinc-{version}",
        "packages": [
            {
                "SPDXID": package_id,
                "comment": (
                    "Runtime dependencies: none. Build, test, benchmark, and release tools "
                    "are outside this runtime package. Artifact hashes are bound by the "
                    "paired provenance statement and BUILD-METADATA-SHA256SUMS."
                ),
                "copyrightText": "Copyright Andrew Brumbelow",
                "downloadLocation": f"https://pypi.org/project/pyinc/{version}/",
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceLocator": f"pkg:pypi/pyinc@{version}",
                        "referenceType": "purl",
                    }
                ],
                "filesAnalyzed": False,
                "licenseConcluded": "Apache-2.0",
                "licenseDeclared": "Apache-2.0",
                "name": _PROJECT,
                "primaryPackagePurpose": "LIBRARY",
                "supplier": "Person: Andrew Brumbelow",
                "versionInfo": version,
            }
        ],
        "relationships": [
            {
                "relatedSpdxElement": package_id,
                "relationshipType": "DESCRIBES",
                "spdxElementId": "SPDXRef-DOCUMENT",
            }
        ],
        "spdxVersion": "SPDX-2.3",
    }


def _distribution_snapshot() -> list[dict[str, str]]:
    distributions: list[dict[str, str]] = []
    for distribution in importlib.metadata.distributions():
        try:
            name = distribution.metadata["Name"]
        except KeyError:
            continue
        if not name.strip():
            continue
        distributions.append({"name": name, "version": distribution.version})
    distributions.sort(key=lambda item: (item["name"].casefold(), item["name"], item["version"]))
    return distributions


def _runner_environment(runner_label: str | None = None) -> dict[str, str]:
    """Capture the selected runner generation and the concrete image identity."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        selected_label = (runner_label or "").strip()
        missing = [name for name in _GITHUB_RUNNER_REQUIRED if not os.environ.get(name)]
        if not selected_label:
            missing.append("runner label")
        if missing:
            _reject(
                "GitHub Actions release provenance is missing runner identity: "
                + ", ".join(missing)
            )
        snapshot = {
            "provider": "github-actions-hosted",
            "runnerLabel": selected_label,
        }
        snapshot.update({name: os.environ[name] for name in _GITHUB_RUNNER_REQUIRED})
        snapshot.update(
            {name: os.environ[name] for name in _GITHUB_RUNNER_OPTIONAL if os.environ.get(name)}
        )
        return snapshot
    return {
        "architecture": platform.machine(),
        "operatingSystem": platform.system(),
        "provider": "local",
    }


def _provenance_document(
    version: str,
    state: GitState,
    artifacts: Sequence[Artifact],
    toolchain: Mapping[str, str],
    manifest_sha256: str,
    lock_sha256: str,
    runner_environment: Mapping[str, str],
) -> dict[str, object]:
    deterministic_environment = dict(_BUILD_ENVIRONMENT)
    deterministic_environment["SOURCE_DATE_EPOCH"] = str(state.source_date_epoch)
    resolved_dependencies: list[dict[str, object]] = [
        {
            "digest": {"gitCommit": state.commit_sha},
            "uri": f"git+{_REPOSITORY}.git@{state.commit_sha}",
        },
        {
            "digest": {"sha256": manifest_sha256},
            "uri": f"{_REPOSITORY}/blob/v{version}/requirements/toolchain.txt",
        },
        {
            "digest": {"sha256": lock_sha256},
            "uri": f"{_REPOSITORY}/blob/v{version}/requirements/toolchain.lock",
        },
    ]
    resolved_dependencies.extend(
        {"uri": f"pkg:pypi/{name}@{toolchain[name]}"} for name in sorted(toolchain)
    )
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": (
                    f"{_REPOSITORY}/blob/v{version}/docs/releases.md#reproducible-builds"
                ),
                "externalParameters": {
                    "buildRuns": 2,
                    "sourceDateEpoch": state.source_date_epoch,
                    "version": version,
                },
                "internalParameters": {
                    "buildCommand": [
                        "python",
                        "-m",
                        "build",
                        "--no-isolation",
                        "--sdist",
                        "--wheel",
                        "--outdir",
                        "<isolated-output>",
                    ],
                    "environment": deterministic_environment,
                    "implementation": platform.python_implementation(),
                    "installedDistributions": _distribution_snapshot(),
                    "platform": platform.platform(),
                    "python": platform.python_version(),
                    "runner": dict(sorted(runner_environment.items())),
                    "toolchain": dict(sorted(toolchain.items())),
                },
                "resolvedDependencies": resolved_dependencies,
            },
            "runDetails": {
                "builder": {
                    "id": f"{_REPOSITORY}/actions/workflows/release.yml@refs/tags/v{version}"
                },
                "metadata": {"invocationId": f"pyinc-{version}-{state.commit_sha}"},
            },
        },
        "predicateType": "https://slsa.dev/provenance/v1",
        "subject": [
            {"digest": {"sha256": artifact.sha256}, "name": artifact.name} for artifact in artifacts
        ],
    }


def metadata_payloads(
    version: str,
    state: GitState,
    artifacts: Sequence[Artifact],
    toolchain: Mapping[str, str],
    manifest_payload: bytes,
    lock_payload: bytes,
    *,
    runner_environment: Mapping[str, str] | None = None,
) -> dict[str, bytes]:
    """Render deterministic unsigned SPDX and SLSA-format metadata documents."""
    provenance_name, spdx_name, checksum_name = metadata_names(version)
    payloads = {
        provenance_name: _json_bytes(
            _provenance_document(
                version,
                state,
                artifacts,
                toolchain,
                _sha256(manifest_payload),
                _sha256(lock_payload),
                _runner_environment() if runner_environment is None else runner_environment,
            )
        ),
        spdx_name: _json_bytes(_spdx_document(version, state)),
    }
    checksummed = {artifact.name: artifact.payload for artifact in artifacts} | payloads
    payloads[checksum_name] = "".join(
        f"{_sha256(checksummed[name])}  {name}\n" for name in sorted(checksummed)
    ).encode("ascii")
    return payloads


def _json_object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _reject(f"{context} must be a JSON object")
    return cast("dict[str, object]", value)


def _parse_metadata_checksums(payload: bytes) -> dict[str, str]:
    try:
        lines = payload.decode("ascii").splitlines()
    except UnicodeDecodeError:
        _reject("BUILD-METADATA-SHA256SUMS must be ASCII")
    checksums: dict[str, str] = {}
    for line in lines:
        match = _CHECKSUM_LINE.fullmatch(line)
        if match is None:
            _reject(f"malformed build-metadata checksum line: {line!r}")
        digest, name = match.groups()
        if name in checksums:
            _reject(f"duplicate build-metadata checksum entry: {name}")
        checksums[name] = digest
    if not checksums:
        _reject("BUILD-METADATA-SHA256SUMS must not be empty")
    return checksums


def verify_metadata_outputs(
    dist_directory: Path,
    metadata_directory: Path,
    version: str,
    *,
    expected_commit: str | None = None,
    expected_runner_label: str | None = None,
    exact_directories: bool = True,
) -> dict[str, str]:
    """Verify exact build metadata, checksums, subjects, and source identity."""
    expected_artifacts = frozenset(artifact_names(version))
    expected_metadata = frozenset(metadata_names(version))
    observed_artifacts = frozenset(path.name for path in dist_directory.iterdir() if path.is_file())
    observed_metadata = frozenset(
        path.name for path in metadata_directory.iterdir() if path.is_file()
    )
    if (
        exact_directories and observed_artifacts != expected_artifacts
    ) or not expected_artifacts <= (observed_artifacts):
        _reject("distribution output does not contain the exact release artifacts")
    if (exact_directories and observed_metadata != expected_metadata) or not expected_metadata <= (
        observed_metadata
    ):
        _reject("metadata output does not contain the exact release metadata")

    checksum_name = "BUILD-METADATA-SHA256SUMS"
    checksums = _parse_metadata_checksums((metadata_directory / checksum_name).read_bytes())
    expected_checksummed = expected_artifacts | (expected_metadata - {checksum_name})
    if frozenset(checksums) != expected_checksummed:
        _reject("BUILD-METADATA-SHA256SUMS must name both distributions and both JSON files")
    for name, digest in checksums.items():
        directory = dist_directory if name in expected_artifacts else metadata_directory
        if _sha256((directory / name).read_bytes()) != digest:
            _reject(f"build-metadata checksum mismatch for {name}")

    provenance_name = f"pyinc-{version}.provenance.json"
    try:
        raw_provenance: object = json.loads((metadata_directory / provenance_name).read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _reject(f"release provenance is not valid UTF-8 JSON: {exc}")
    provenance = _json_object(raw_provenance, "release provenance")
    if provenance.get("_type") != "https://in-toto.io/Statement/v1":
        _reject("release provenance must be an in-toto Statement v1")
    if provenance.get("predicateType") != "https://slsa.dev/provenance/v1":
        _reject("release provenance must use the SLSA provenance v1 predicate")
    raw_subjects = provenance.get("subject")
    if not isinstance(raw_subjects, list):
        _reject("release provenance subject must be an array")
    subjects: dict[str, str] = {}
    for raw_subject in raw_subjects:
        subject = _json_object(raw_subject, "release provenance subject")
        subject_name = subject.get("name")
        if not isinstance(subject_name, str) or subject_name in subjects:
            _reject("release provenance subjects must have unique string names")
        subject_digest = _json_object(subject.get("digest"), "release provenance subject digest")
        sha256 = subject_digest.get("sha256")
        if not isinstance(sha256, str):
            _reject("release provenance subject must contain a SHA-256 digest")
        subjects[subject_name] = sha256
    expected_subjects = {name: checksums[name] for name in sorted(expected_artifacts)}
    if subjects != expected_subjects:
        _reject("release provenance subjects do not match the distributions")

    predicate = _json_object(provenance.get("predicate"), "release provenance predicate")
    definition = _json_object(predicate.get("buildDefinition"), "release build definition")
    parameters = _json_object(
        definition.get("externalParameters"), "release build external parameters"
    )
    if parameters.get("version") != version or parameters.get("buildRuns") != 2:
        _reject("release provenance must record the version and two build runs")
    internal = _json_object(
        definition.get("internalParameters"), "release build internal parameters"
    )
    runner = _json_object(internal.get("runner"), "release build runner")
    provider = runner.get("provider")
    required_runner_fields: tuple[str, ...]
    if provider == "github-actions-hosted":
        required_runner_fields = (
            "runnerLabel",
            *_GITHUB_RUNNER_REQUIRED,
        )
    elif provider == "local":
        required_runner_fields = ("architecture", "operatingSystem")
    else:
        _reject("release provenance contains an unknown runner provider")
    if any(
        not isinstance(runner.get(field), str) or not runner[field]
        for field in required_runner_fields
    ):
        _reject("release provenance contains an incomplete runner identity")
    if expected_runner_label is not None and runner.get("runnerLabel") != expected_runner_label:
        _reject("release provenance does not bind the expected runner label")
    if expected_commit is not None:
        commit = expected_commit.lower()
        if _COMMIT_PATTERN.fullmatch(commit) is None:
            _reject(f"invalid expected commit: {expected_commit!r}")
        dependencies = definition.get("resolvedDependencies")
        if not isinstance(dependencies, list):
            _reject("release provenance resolvedDependencies must be an array")
        matched_commit = False
        for raw_dependency in dependencies:
            dependency = _json_object(raw_dependency, "release provenance dependency")
            dependency_digest = dependency.get("digest")
            if not isinstance(dependency_digest, dict):
                continue
            matched_commit |= dependency_digest.get("gitCommit") == commit
        if not matched_commit:
            _reject("release provenance does not bind the expected source commit")

    spdx_name = f"pyinc-{version}.spdx.json"
    try:
        raw_spdx: object = json.loads((metadata_directory / spdx_name).read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _reject(f"release SBOM is not valid UTF-8 JSON: {exc}")
    spdx = _json_object(raw_spdx, "release SBOM")
    packages = spdx.get("packages")
    if spdx.get("spdxVersion") != "SPDX-2.3" or not isinstance(packages, list):
        _reject("release SBOM must be an SPDX 2.3 document with packages")
    if len(packages) != 1:
        _reject("release SBOM must describe exactly the pyinc runtime package")
    package = _json_object(packages[0], "release SBOM package")
    if (
        package.get("name") != _PROJECT
        or package.get("versionInfo") != version
        or package.get("filesAnalyzed") is not False
    ):
        _reject("release SBOM package identity or analysis boundary is invalid")
    return checksums


def _publish_directory(destination: Path, payloads: Mapping[str, bytes]) -> None:
    if destination.exists():
        _reject(f"output directory already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=os.fspath(destination.parent))
    )
    try:
        for name, payload in payloads.items():
            (temporary / name).write_bytes(payload)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def build_release(
    project_root: Path,
    dist_output: Path,
    metadata_output: Path,
    *,
    expected_version: str | None = None,
    expected_commit: str | None = None,
    runner_label: str | None = None,
) -> ReleaseBuild:
    """Build two source snapshots, require equal bytes, and publish one copy."""
    project_root = project_root.resolve()
    dist_output = dist_output.resolve()
    metadata_output = metadata_output.resolve()
    if dist_output == metadata_output:
        _reject("distribution and metadata outputs must be different directories")
    version = _project_version(project_root)
    if expected_version is not None and version != expected_version:
        _reject(f"project version {version} does not match expected {expected_version}")
    state = _git_state(project_root)
    if expected_commit is not None:
        normalized_commit = expected_commit.lower()
        if _COMMIT_PATTERN.fullmatch(normalized_commit) is None:
            _reject(f"invalid expected commit: {expected_commit!r}")
        if state.commit_sha != normalized_commit:
            _reject(f"source commit {state.commit_sha} does not match expected {normalized_commit}")

    toolchain = check_toolchain.validate(project_root, verify_installed=True)
    manifest_payload = (project_root / "requirements/toolchain.txt").read_bytes()
    lock_payload = (project_root / "requirements/toolchain.lock").read_bytes()
    archive_payload = _source_archive(project_root)
    with tempfile.TemporaryDirectory(prefix="pyinc-reproducible-build-") as temporary_name:
        temporary = Path(temporary_name)
        build_directories: list[Path] = []
        for run in ("first", "second"):
            source = temporary / run / "source"
            output = temporary / run / "dist"
            _extract_source(archive_payload, source)
            _run_build(source, output, state.source_date_epoch)
            build_directories.append(output)
        artifacts = compare_builds(build_directories[0], build_directories[1], version)

    dist_payloads = {artifact.name: artifact.payload for artifact in artifacts}
    metadata = metadata_payloads(
        version,
        state,
        artifacts,
        toolchain,
        manifest_payload,
        lock_payload,
        runner_environment=_runner_environment(runner_label),
    )
    _publish_directory(dist_output, dist_payloads)
    _publish_directory(metadata_output, metadata)
    return ReleaseBuild(
        version=version,
        commit_sha=state.commit_sha,
        source_date_epoch=state.source_date_epoch,
        artifacts=artifacts,
        dist_directory=dist_output,
        metadata_directory=metadata_output,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--dist-output", type=Path, required=True)
    parser.add_argument("--metadata-output", type=Path, required=True)
    parser.add_argument("--version")
    parser.add_argument("--commit")
    parser.add_argument("--runner-label")
    parser.add_argument("--verify-existing", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.verify_existing:
        if arguments.version is None:
            _reject("--verify-existing requires --version")
        checksums = verify_metadata_outputs(
            arguments.dist_output,
            arguments.metadata_output,
            arguments.version,
            expected_commit=arguments.commit,
            expected_runner_label=arguments.runner_label,
        )
        print(f"verified_build_metadata={len(checksums)}")
        return 0
    result = build_release(
        arguments.project_root,
        arguments.dist_output,
        arguments.metadata_output,
        expected_version=arguments.version,
        expected_commit=arguments.commit,
        runner_label=arguments.runner_label,
    )
    print(f"version={result.version}")
    print(f"commit={result.commit_sha}")
    for artifact in result.artifacts:
        print(f"sha256={artifact.sha256}  {artifact.name}")
    print(f"metadata={result.metadata_directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
