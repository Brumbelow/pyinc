from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from scripts import release_artifacts, reproducible_builds

VERSION = "3.0.0rc1"
SDIST = f"pyinc-{VERSION}.tar.gz"
WHEEL = f"pyinc-{VERSION}-py3-none-any.whl"
PYPI_API_URL = f"https://pypi.org/pypi/pyinc/{VERSION}/json"
GITHUB_API_URL = f"https://api.github.com/repos/Brumbelow/pyinc/releases/tags/v{VERSION}"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _benchmark_payloads() -> dict[str, bytes]:
    members = {
        "samples.csv": b"sample\n1\n",
        "benchmark.csv": b"summary\n1\n",
        "benchmark.md": b"# Benchmark\n",
        "metadata.json": b'{"commit_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}\n',
        "command.txt": b"python -m bench.run\n",
    }
    members["SHA256SUMS"] = release_artifacts.render_checksums(
        {name: _sha256(payload) for name, payload in members.items()}
    ).encode("ascii")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    bundle_name, checksum_name = release_artifacts.benchmark_evidence_names(VERSION)
    bundle = stream.getvalue()
    checksum = release_artifacts.render_checksums({bundle_name: _sha256(bundle)}).encode("ascii")
    return {bundle_name: bundle, checksum_name: checksum}


def _demo_payloads() -> dict[str, bytes]:
    stdout = b"demo output\n"
    stderr = b""
    metadata = json.dumps(
        {
            "schema_version": 1,
            "evidence_kind": "pyinc-demo",
            "release_version": VERSION,
            "commit_sha": "a" * 40,
            "working_tree_dirty": False,
            "generated_at_utc": "2026-08-04T12:00:00Z",
            "example_count": 1,
            "distribution_snapshot": [
                {"normalized_name": "pyinc", "name": "pyinc", "version": VERSION}
            ],
        },
        sort_keys=True,
    ).encode()
    runs = json.dumps(
        [
            {
                "example": "examples/demo.py",
                "exit_code": 0,
                "stdout": {"path": "runs/000.stdout"},
                "stderr": {"path": "runs/000.stderr"},
            }
        ],
        sort_keys=True,
    ).encode()
    members = {
        "metadata.json": metadata,
        "runs.json": runs,
        "runs/000.stdout": stdout,
        "runs/000.stderr": stderr,
    }
    members["SHA256SUMS"] = "".join(
        f"{_sha256(payload)}  {name}\n" for name, payload in sorted(members.items())
    ).encode("ascii")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    bundle_name, checksum_name = release_artifacts.demo_evidence_names(VERSION)
    bundle = stream.getvalue()
    checksum = release_artifacts.render_checksums({bundle_name: _sha256(bundle)}).encode("ascii")
    return {bundle_name: bundle, checksum_name: checksum}


def _build_metadata_payloads(distributions: dict[str, bytes]) -> dict[str, bytes]:
    artifacts = tuple(
        reproducible_builds.Artifact(name=name, payload=payload)
        for name, payload in sorted(distributions.items())
    )
    return reproducible_builds.metadata_payloads(
        VERSION,
        reproducible_builds.GitState("a" * 40, 1_752_278_400),
        artifacts,
        {"build": "1.5.0"},
        b"build==1.5.0\n",
        b"build==1.5.0 --hash=sha256:lock\n",
        runner_environment={
            "architecture": "test",
            "operatingSystem": "test",
            "provider": "local",
        },
    )


def test_writes_and_verifies_exact_distribution_checksums(tmp_path: Path) -> None:
    directory = tmp_path / "dist"
    directory.mkdir()
    (directory / SDIST).write_bytes(b"sdist")
    (directory / WHEEL).write_bytes(b"wheel")
    checksum_path = tmp_path / "SHA256SUMS"

    release_artifacts.write_checksums(directory, VERSION, checksum_path)

    assert checksum_path.read_text(encoding="ascii") == (
        f"{_sha256(b'wheel')}  {WHEEL}\n{_sha256(b'sdist')}  {SDIST}\n"
    )
    release_artifacts.verify_checksums(directory, VERSION, checksum_path)


def test_rejects_extra_local_distribution_file(tmp_path: Path) -> None:
    (tmp_path / SDIST).write_bytes(b"sdist")
    (tmp_path / WHEEL).write_bytes(b"wheel")
    (tmp_path / "unexpected.zip").write_bytes(b"unexpected")

    with pytest.raises(release_artifacts.ReleaseArtifactError, match="exactly"):
        release_artifacts.write_checksums(tmp_path, VERSION, tmp_path / "SHA256SUMS")


@pytest.mark.parametrize(
    "document",
    [
        b"not a checksum\n",
        f"{'0' * 64}  {SDIST}\n{'1' * 64}  {SDIST}\n".encode(),
        b"\xff\n",
    ],
)
def test_rejects_malformed_duplicate_or_non_ascii_checksums(document: bytes) -> None:
    with pytest.raises(release_artifacts.ReleaseArtifactError):
        release_artifacts.parse_checksums(document)


def test_extracts_only_the_requested_dated_release_section() -> None:
    changelog = f"""# Changelog

## [Unreleased]

- Future.

## [{VERSION}] - 2026-07-12

### Added

- Release candidate.

## [2.6.0] - 2026-06-01

- Previous.
"""

    assert release_artifacts.extract_release_notes(changelog, VERSION) == (
        "### Added\n\n- Release candidate.\n"
    )


@pytest.mark.parametrize(
    "changelog",
    [
        "# Changelog\n",
        f"## [{VERSION}]\n\n- Missing date.\n",
        f"## [{VERSION}] - 2026-07-12\n\n",
        (f"## [{VERSION}] - 2026-07-12\n\n- First.\n\n## [{VERSION}] - 2026-07-13\n\n- Second.\n"),
    ],
)
def test_rejects_missing_malformed_empty_or_duplicate_release_section(changelog: str) -> None:
    with pytest.raises(release_artifacts.ReleaseArtifactError):
        release_artifacts.extract_release_notes(changelog, VERSION)


def _published_documents(
    pypi_payloads: dict[str, bytes],
    github_payloads: dict[str, bytes],
) -> tuple[dict[str, object], dict[str, object], dict[str, bytes]]:
    urls: dict[str, bytes] = {}
    pypi_files: list[dict[str, object]] = []
    github_assets: list[dict[str, object]] = []
    for name, package_type in ((SDIST, "sdist"), (WHEEL, "bdist_wheel")):
        pypi_url = f"https://files.pythonhosted.org/{name}"
        github_url = f"https://github.com/Brumbelow/pyinc/releases/download/v{VERSION}/{name}"
        urls[pypi_url] = pypi_payloads[name]
        urls[github_url] = github_payloads[name]
        pypi_files.append(
            {
                "filename": name,
                "packagetype": package_type,
                "url": pypi_url,
                "digests": {"sha256": _sha256(pypi_payloads[name])},
            }
        )
        github_assets.append(
            {
                "name": name,
                "state": "uploaded",
                "digest": f"sha256:{_sha256(github_payloads[name])}",
                "browser_download_url": github_url,
            }
        )
    checksums = release_artifacts.render_checksums(
        {name: _sha256(payload) for name, payload in pypi_payloads.items()}
    ).encode()
    checksum_url = f"https://github.com/Brumbelow/pyinc/releases/download/v{VERSION}/SHA256SUMS"
    urls[checksum_url] = checksums
    github_assets.append(
        {
            "name": "SHA256SUMS",
            "state": "uploaded",
            "digest": f"sha256:{_sha256(checksums)}",
            "browser_download_url": checksum_url,
        }
    )
    evidence_payloads = {
        **_benchmark_payloads(),
        **_demo_payloads(),
        **_build_metadata_payloads(github_payloads),
    }
    for name, payload in evidence_payloads.items():
        url = f"https://github.com/Brumbelow/pyinc/releases/download/v{VERSION}/{name}"
        urls[url] = payload
        github_assets.append(
            {
                "name": name,
                "state": "uploaded",
                "digest": f"sha256:{_sha256(payload)}",
                "browser_download_url": url,
            }
        )
    pypi: dict[str, object] = {
        "info": {"name": "pyinc", "version": VERSION},
        "urls": pypi_files,
    }
    github: dict[str, object] = {
        "tag_name": f"v{VERSION}",
        "draft": False,
        "prerelease": True,
        "assets": github_assets,
    }
    return pypi, github, urls


def test_verifies_pypi_and_github_release_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads = {SDIST: b"sdist", WHEEL: b"wheel"}
    pypi, github, urls = _published_documents(payloads, payloads)
    documents = {PYPI_API_URL: pypi, GITHUB_API_URL: github}

    def request_json(url: str, token: str | None = None) -> dict[str, object]:
        del token
        return documents[url]

    def download(url: str, destination: Path) -> str:
        payload = urls[url]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return _sha256(payload)

    monkeypatch.setattr(release_artifacts, "_request_json", request_json)
    monkeypatch.setattr(release_artifacts, "_download", download)

    release_artifacts.verify_published_artifacts(
        VERSION,
        "Brumbelow/pyinc",
        tmp_path,
        github_token="token",
    )

    assert (tmp_path / "pypi" / SDIST).read_bytes() == b"sdist"
    assert (tmp_path / "github" / WHEEL).read_bytes() == b"wheel"


def test_verifies_local_artifacts_against_pypi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads = {SDIST: b"sdist", WHEEL: b"wheel"}
    pypi, _, _ = _published_documents(payloads, payloads)
    directory = tmp_path / "dist"
    directory.mkdir()
    for name, payload in payloads.items():
        (directory / name).write_bytes(payload)

    def request_json(url: str, token: str | None = None) -> dict[str, object]:
        del url, token
        return pypi

    monkeypatch.setattr(release_artifacts, "_request_json", request_json)

    release_artifacts.verify_pypi_artifacts(VERSION, directory)
    (directory / WHEEL).write_bytes(b"different wheel")
    with pytest.raises(release_artifacts.ReleaseArtifactError, match="does not match PyPI"):
        release_artifacts.verify_pypi_artifacts(VERSION, directory)


def test_rejects_pypi_and_github_artifact_hash_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pypi_payloads = {SDIST: b"sdist", WHEEL: b"wheel"}
    github_payloads = {SDIST: b"sdist", WHEEL: b"different wheel"}
    pypi, github, urls = _published_documents(pypi_payloads, github_payloads)
    documents = {PYPI_API_URL: pypi, GITHUB_API_URL: github}

    def request_json(url: str, token: str | None = None) -> dict[str, object]:
        del token
        return documents[url]

    def download(url: str, destination: Path) -> str:
        payload = urls[url]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return _sha256(payload)

    monkeypatch.setattr(release_artifacts, "_request_json", request_json)
    monkeypatch.setattr(release_artifacts, "_download", download)

    with pytest.raises(release_artifacts.ReleaseArtifactError, match="differ"):
        release_artifacts.verify_published_artifacts(VERSION, "Brumbelow/pyinc", tmp_path)


def _local_release(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    directory = tmp_path / "dist"
    directory.mkdir()
    (directory / SDIST).write_bytes(b"sdist")
    (directory / WHEEL).write_bytes(b"wheel")
    checksum_path = tmp_path / "SHA256SUMS"
    release_artifacts.write_checksums(directory, VERSION, checksum_path)
    benchmark_directory = tmp_path / "benchmark"
    benchmark_directory.mkdir()
    for name, payload in _benchmark_payloads().items():
        (benchmark_directory / name).write_bytes(payload)
    demo_directory = tmp_path / "demo"
    demo_directory.mkdir()
    for name, payload in _demo_payloads().items():
        (demo_directory / name).write_bytes(payload)
    metadata_directory = tmp_path / "build-metadata"
    metadata_directory.mkdir()
    for name, payload in _build_metadata_payloads({SDIST: b"sdist", WHEEL: b"wheel"}).items():
        (metadata_directory / name).write_bytes(payload)
    return directory, checksum_path, benchmark_directory, demo_directory, metadata_directory


def test_verifies_downloaded_remote_release_assets(tmp_path: Path) -> None:
    directory, checksum_path, benchmark_directory, demo_directory, metadata_directory = (
        _local_release(tmp_path)
    )
    remote = tmp_path / "remote"
    remote.mkdir()
    for name in (SDIST, WHEEL):
        (remote / name).write_bytes((directory / name).read_bytes())
    (remote / "SHA256SUMS").write_bytes(checksum_path.read_bytes())
    for path in benchmark_directory.iterdir():
        (remote / path.name).write_bytes(path.read_bytes())
    for path in demo_directory.iterdir():
        (remote / path.name).write_bytes(path.read_bytes())
    for path in metadata_directory.iterdir():
        (remote / path.name).write_bytes(path.read_bytes())

    release_artifacts.verify_remote_assets(
        VERSION,
        directory,
        checksum_path,
        benchmark_directory,
        demo_directory,
        metadata_directory,
        remote,
    )


@pytest.mark.parametrize(
    "corruption",
    [
        "missing",
        "extra",
        "distribution",
        "checksums",
        "benchmark-bundle",
        "benchmark-checksums",
        "demo-bundle",
        "demo-checksums",
        "build-metadata",
    ],
)
def test_rejects_incomplete_or_changed_remote_release_assets(
    tmp_path: Path, corruption: str
) -> None:
    directory, checksum_path, benchmark_directory, demo_directory, metadata_directory = (
        _local_release(tmp_path)
    )
    remote = tmp_path / "remote"
    remote.mkdir()
    for name in (SDIST, WHEEL):
        (remote / name).write_bytes((directory / name).read_bytes())
    (remote / "SHA256SUMS").write_bytes(checksum_path.read_bytes())
    for path in benchmark_directory.iterdir():
        (remote / path.name).write_bytes(path.read_bytes())
    for path in demo_directory.iterdir():
        (remote / path.name).write_bytes(path.read_bytes())
    for path in metadata_directory.iterdir():
        (remote / path.name).write_bytes(path.read_bytes())
    if corruption == "missing":
        (remote / WHEEL).unlink()
    elif corruption == "extra":
        (remote / "unexpected.txt").write_text("unexpected", encoding="utf-8")
    elif corruption == "distribution":
        (remote / WHEEL).write_bytes(b"different wheel")
    elif corruption == "checksums":
        (remote / "SHA256SUMS").write_bytes(b"different checksums")
    elif corruption == "benchmark-bundle":
        bundle_name, _ = release_artifacts.benchmark_evidence_names(VERSION)
        (remote / bundle_name).write_bytes(b"different bundle")
    elif corruption == "benchmark-checksums":
        _, benchmark_checksum = release_artifacts.benchmark_evidence_names(VERSION)
        (remote / benchmark_checksum).write_bytes(b"different checksums")
    elif corruption == "demo-bundle":
        demo_bundle, _ = release_artifacts.demo_evidence_names(VERSION)
        (remote / demo_bundle).write_bytes(b"different bundle")
    elif corruption == "demo-checksums":
        _, demo_checksum = release_artifacts.demo_evidence_names(VERSION)
        (remote / demo_checksum).write_bytes(b"different checksums")
    else:
        metadata_name = release_artifacts.build_metadata_names(VERSION)[0]
        (remote / metadata_name).write_bytes(b"different metadata")

    with pytest.raises(release_artifacts.ReleaseArtifactError):
        release_artifacts.verify_remote_assets(
            VERSION,
            directory,
            checksum_path,
            benchmark_directory,
            demo_directory,
            metadata_directory,
            remote,
        )


def _release_metadata(**overrides: object) -> dict[str, object]:
    metadata: dict[str, object] = {
        "tagName": f"v{VERSION}",
        "name": f"pyinc {VERSION}",
        "isDraft": False,
        "isPrerelease": True,
        "body": "### Added\n\n- Release candidate.\n",
    }
    metadata.update(overrides)
    return metadata


def test_verifies_published_release_state(tmp_path: Path) -> None:
    metadata_path = tmp_path / "release.json"
    metadata_path.write_text(json.dumps(_release_metadata()), encoding="utf-8")
    notes_path = tmp_path / "notes.md"
    notes_path.write_text("### Added\n\n- Release candidate.\n", encoding="utf-8")
    release_list_path = tmp_path / "release-list.json"
    release_list_path.write_text(
        json.dumps([{"tagName": f"v{VERSION}", "isLatest": False}]),
        encoding="utf-8",
    )

    release_artifacts.verify_release_state(VERSION, metadata_path, notes_path, release_list_path)

    final_version = "3.0.0"
    final_metadata = _release_metadata(
        tagName=f"v{final_version}",
        name=f"pyinc {final_version}",
        isPrerelease=False,
    )
    metadata_path.write_text(json.dumps(final_metadata), encoding="utf-8")
    release_list_path.write_text(
        json.dumps([{"tagName": f"v{final_version}", "isLatest": True}]),
        encoding="utf-8",
    )
    release_artifacts.verify_release_state(
        final_version, metadata_path, notes_path, release_list_path
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("tagName", "v3.0.0rc2"),
        ("name", "Wrong title"),
        ("isDraft", True),
        ("isPrerelease", False),
        ("body", "Wrong notes"),
    ],
)
def test_rejects_incorrect_published_release_state(tmp_path: Path, key: str, value: object) -> None:
    metadata_path = tmp_path / "release.json"
    metadata_path.write_text(
        json.dumps(_release_metadata(**{key: value})),
        encoding="utf-8",
    )
    notes_path = tmp_path / "notes.md"
    notes_path.write_text("### Added\n\n- Release candidate.\n", encoding="utf-8")
    release_list_path = tmp_path / "release-list.json"
    release_list_path.write_text(
        json.dumps([{"tagName": f"v{VERSION}", "isLatest": False}]),
        encoding="utf-8",
    )

    with pytest.raises(release_artifacts.ReleaseArtifactError):
        release_artifacts.verify_release_state(
            VERSION, metadata_path, notes_path, release_list_path
        )


@pytest.mark.parametrize(
    "release_list",
    [
        [],
        [{"tagName": f"v{VERSION}", "isLatest": True}],
        [
            {"tagName": f"v{VERSION}", "isLatest": False},
            {"tagName": f"v{VERSION}", "isLatest": False},
        ],
    ],
)
def test_rejects_missing_wrong_or_duplicate_latest_state(
    tmp_path: Path, release_list: list[dict[str, object]]
) -> None:
    metadata_path = tmp_path / "release.json"
    metadata_path.write_text(json.dumps(_release_metadata()), encoding="utf-8")
    notes_path = tmp_path / "notes.md"
    notes_path.write_text("### Added\n\n- Release candidate.\n", encoding="utf-8")
    release_list_path = tmp_path / "release-list.json"
    release_list_path.write_text(json.dumps(release_list), encoding="utf-8")

    with pytest.raises(release_artifacts.ReleaseArtifactError):
        release_artifacts.verify_release_state(
            VERSION, metadata_path, notes_path, release_list_path
        )
