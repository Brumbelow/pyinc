from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from scripts import check_toolchain, reproducible_builds

VERSION = "3.1.2"
COMMIT = "a" * 40
EPOCH = 1_785_863_036
HOSTED_RUNNER = {
    "ImageOS": "ubuntu24",
    "ImageVersion": "20260801.1",
    "RUNNER_ARCH": "X64",
    "RUNNER_OS": "Linux",
    "provider": "github-actions-hosted",
    "runnerLabel": "ubuntu-24.04",
}


def _artifact_payloads(suffix: bytes = b"") -> dict[str, bytes]:
    return {
        f"pyinc-{VERSION}.tar.gz": b"sdist" + suffix,
        f"pyinc-{VERSION}-py3-none-any.whl": b"wheel" + suffix,
    }


def _write_artifacts(directory: Path, suffix: bytes = b"") -> None:
    directory.mkdir()
    for name, payload in _artifact_payloads(suffix).items():
        (directory / name).write_bytes(payload)


def _source_archive(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, payload in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    return stream.getvalue()


def test_equal_builds_emit_exact_artifact_hashes(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_artifacts(first)
    _write_artifacts(second)

    artifacts = reproducible_builds.compare_builds(first, second, VERSION)

    assert {artifact.name: artifact.sha256 for artifact in artifacts} == {
        name: hashlib.sha256(payload).hexdigest() for name, payload in _artifact_payloads().items()
    }


def test_rejects_different_build_bytes(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_artifacts(first)
    _write_artifacts(second, b"-changed")

    with pytest.raises(reproducible_builds.ReproducibleBuildError, match="isolated builds differ"):
        reproducible_builds.compare_builds(first, second, VERSION)


def test_rejects_unsafe_source_archive_member(tmp_path: Path) -> None:
    archive = _source_archive({"../escape": b"payload"})

    with pytest.raises(reproducible_builds.ReproducibleBuildError, match="unsafe"):
        reproducible_builds._extract_source(archive, tmp_path / "source")
    assert not (tmp_path / "escape").exists()


def test_metadata_is_deterministic_and_binds_artifacts_and_toolchain() -> None:
    artifacts = tuple(
        reproducible_builds.Artifact(name=name, payload=payload)
        for name, payload in sorted(_artifact_payloads().items())
    )
    state = reproducible_builds.GitState(COMMIT, EPOCH)
    tools = {"build": "1.5.0", "hatchling": "1.31.0"}

    first = reproducible_builds.metadata_payloads(
        VERSION,
        state,
        artifacts,
        tools,
        b"build==1.5.0\n",
        b"build==1.5.0 --hash=sha256:lock\n",
        runner_environment=HOSTED_RUNNER,
    )
    second = reproducible_builds.metadata_payloads(
        VERSION,
        state,
        artifacts,
        tools,
        b"build==1.5.0\n",
        b"build==1.5.0 --hash=sha256:lock\n",
        runner_environment=HOSTED_RUNNER,
    )

    assert first == second
    provenance = json.loads(first[f"pyinc-{VERSION}.provenance.json"])
    assert provenance["predicateType"] == "https://slsa.dev/provenance/v1"
    assert provenance["predicate"]["buildDefinition"]["externalParameters"]["buildRuns"] == 2
    assert provenance["predicate"]["buildDefinition"]["internalParameters"]["toolchain"] == tools
    assert (
        provenance["predicate"]["buildDefinition"]["internalParameters"]["runner"] == HOSTED_RUNNER
    )
    dependencies = provenance["predicate"]["buildDefinition"]["resolvedDependencies"]
    locked = next(
        dependency
        for dependency in dependencies
        if dependency["uri"].endswith("requirements/toolchain.lock")
    )
    assert locked["digest"] == {
        "sha256": hashlib.sha256(b"build==1.5.0 --hash=sha256:lock\n").hexdigest()
    }
    assert {subject["name"] for subject in provenance["subject"]} == set(_artifact_payloads())
    spdx = json.loads(first[f"pyinc-{VERSION}.spdx.json"])
    assert spdx["spdxVersion"] == "SPDX-2.3"
    assert spdx["packages"][0]["name"] == "pyinc"
    assert spdx["packages"][0]["versionInfo"] == VERSION

    checksum_lines = first["BUILD-METADATA-SHA256SUMS"].decode("ascii").splitlines()
    assert len(checksum_lines) == 4
    assert all(
        line.split("  ", 1)[0]
        == hashlib.sha256(
            _artifact_payloads().get(line.split("  ", 1)[1]) or first[line.split("  ", 1)[1]]
        ).hexdigest()
        for line in checksum_lines
    )


def test_metadata_verifier_checks_hashes_subjects_sbom_and_commit(tmp_path: Path) -> None:
    artifacts = tuple(
        reproducible_builds.Artifact(name=name, payload=payload)
        for name, payload in sorted(_artifact_payloads().items())
    )
    metadata = reproducible_builds.metadata_payloads(
        VERSION,
        reproducible_builds.GitState(COMMIT, EPOCH),
        artifacts,
        {"build": "1.5.0"},
        b"build==1.5.0\n",
        b"build==1.5.0 --hash=sha256:lock\n",
        runner_environment=HOSTED_RUNNER,
    )
    dist_directory = tmp_path / "dist"
    metadata_directory = tmp_path / "metadata"
    dist_directory.mkdir()
    metadata_directory.mkdir()
    for artifact in artifacts:
        (dist_directory / artifact.name).write_bytes(artifact.payload)
    for name, payload in metadata.items():
        (metadata_directory / name).write_bytes(payload)

    checksums = reproducible_builds.verify_metadata_outputs(
        dist_directory,
        metadata_directory,
        VERSION,
        expected_commit=COMMIT,
        expected_runner_label="ubuntu-24.04",
    )

    assert set(checksums) == set(_artifact_payloads()) | {
        f"pyinc-{VERSION}.provenance.json",
        f"pyinc-{VERSION}.spdx.json",
    }
    (dist_directory / f"pyinc-{VERSION}.tar.gz").write_bytes(b"corrupt")
    with pytest.raises(reproducible_builds.ReproducibleBuildError, match="checksum mismatch"):
        reproducible_builds.verify_metadata_outputs(
            dist_directory, metadata_directory, VERSION, expected_commit=COMMIT
        )


def test_build_release_uses_two_clean_source_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        f'[project]\nname = "pyinc"\nversion = "{VERSION}"\ndependencies = []\n',
        encoding="utf-8",
    )
    manifest = b"build==1.5.0\n"
    (project / "requirements").mkdir()
    (project / "requirements/toolchain.txt").write_bytes(manifest)
    (project / "requirements/toolchain.lock").write_bytes(b"locked\n")
    source = _source_archive(
        {
            "pyproject.toml": (project / "pyproject.toml").read_bytes(),
            "requirements/toolchain.txt": manifest,
            "requirements/toolchain.lock": b"locked\n",
        }
    )
    runs: list[Path] = []

    monkeypatch.setattr(
        reproducible_builds,
        "_git_state",
        lambda _root: reproducible_builds.GitState(COMMIT, EPOCH),
    )
    monkeypatch.setattr(reproducible_builds, "_source_archive", lambda _root: source)
    monkeypatch.setattr(
        reproducible_builds,
        "_runner_environment",
        lambda _label=None: HOSTED_RUNNER,
    )
    monkeypatch.setattr(
        check_toolchain,
        "validate",
        lambda _root, *, verify_installed: {"build": "1.5.0"},
    )

    def run_build(source_root: Path, output: Path, epoch: int) -> None:
        assert epoch == EPOCH
        assert (source_root / "pyproject.toml").is_file()
        runs.append(source_root)
        _write_artifacts(output)

    monkeypatch.setattr(reproducible_builds, "_run_build", run_build)

    result = reproducible_builds.build_release(
        project,
        tmp_path / "dist",
        tmp_path / "metadata",
        expected_version=VERSION,
        expected_commit=COMMIT,
    )

    assert len(runs) == 2
    assert runs[0] != runs[1]
    assert {path.name for path in result.dist_directory.iterdir()} == set(_artifact_payloads())
    assert {path.name for path in result.metadata_directory.iterdir()} == set(
        reproducible_builds.metadata_names(VERSION)
    )


def test_github_runner_environment_requires_and_records_exact_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    for name in reproducible_builds._GITHUB_RUNNER_OPTIONAL:
        monkeypatch.delenv(name, raising=False)
    for name in (*reproducible_builds._GITHUB_RUNNER_REQUIRED, "ImageRelease"):
        monkeypatch.setenv(name, f"exact-{name}")

    snapshot = reproducible_builds._runner_environment("ubuntu-24.04")

    assert snapshot == {
        "ImageOS": "exact-ImageOS",
        "ImageRelease": "exact-ImageRelease",
        "ImageVersion": "exact-ImageVersion",
        "RUNNER_ARCH": "exact-RUNNER_ARCH",
        "RUNNER_OS": "exact-RUNNER_OS",
        "provider": "github-actions-hosted",
        "runnerLabel": "ubuntu-24.04",
    }


def test_github_runner_environment_fails_closed_when_identity_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    for name in reproducible_builds._GITHUB_RUNNER_REQUIRED:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(
        reproducible_builds.ReproducibleBuildError,
        match="missing runner identity",
    ):
        reproducible_builds._runner_environment("ubuntu-24.04")


def test_script_can_be_invoked_by_path_outside_the_repository(tmp_path: Path) -> None:
    script = Path(reproducible_builds.__file__).resolve()

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--dist-output" in result.stdout
