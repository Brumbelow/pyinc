from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from scripts import benchmark_evidence

VERSION = "3.1.2"
COMMIT = "a" * 40


def _results(root: Path, **metadata_overrides: object) -> Path:
    root.mkdir()
    (root / "samples.csv").write_text("sample\n1\n", encoding="utf-8")
    (root / "benchmark.csv").write_text("summary\n1\n", encoding="utf-8")
    (root / "benchmark.md").write_text("# Benchmark\n", encoding="utf-8")
    metadata: dict[str, object] = {
        "schema_version": 1,
        "commit_sha": COMMIT,
        "working_tree_dirty": False,
        "repetitions": 5,
        "generated_at_utc": "2026-08-04T12:00:00+00:00",
        "pyinc_version": VERSION,
        "pythonhashseed": "0",
        "distributions": [{"name": "pyinc", "version": VERSION}],
    }
    metadata.update(metadata_overrides)
    (root / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return root


def _parse_checksums(document: bytes) -> dict[str, str]:
    return {
        line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in document.decode().splitlines()
    }


def test_prepares_deterministic_complete_benchmark_evidence(tmp_path: Path) -> None:
    source = _results(tmp_path / "results")
    first_bundle, first_checksum = benchmark_evidence.prepare(
        source, tmp_path / "first", VERSION, COMMIT
    )
    second_bundle, second_checksum = benchmark_evidence.prepare(
        source, tmp_path / "second", VERSION, COMMIT
    )

    assert first_bundle.name == f"pyinc-{VERSION}-benchmark-evidence.zip"
    assert first_bundle.read_bytes() == second_bundle.read_bytes()
    assert first_checksum.read_bytes() == second_checksum.read_bytes()
    external = _parse_checksums(first_checksum.read_bytes())
    assert external == {first_bundle.name: hashlib.sha256(first_bundle.read_bytes()).hexdigest()}

    with zipfile.ZipFile(first_bundle) as archive:
        assert set(archive.namelist()) == {
            "samples.csv",
            "benchmark.csv",
            "benchmark.md",
            "metadata.json",
            "command.txt",
            "SHA256SUMS",
        }
        internal = _parse_checksums(archive.read("SHA256SUMS"))
        assert set(internal) == set(archive.namelist()) - {"SHA256SUMS"}
        for name, digest in internal.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
        assert archive.read("command.txt") == (
            b"PYTHONHASHSEED=0 PYTHONPATH=src python -m bench.run "
            b"--output bench/results --repetitions 5\n"
        )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"commit_sha": "b" * 40}, "commit_sha"),
        ({"schema_version": 2}, "schema_version"),
        ({"working_tree_dirty": True}, "clean working tree"),
        ({"repetitions": 4}, "five repetitions"),
        ({"generated_at_utc": None}, "generated_at_utc"),
        ({"pyinc_version": None}, "pyinc_version"),
        ({"pythonhashseed": "1"}, "PYTHONHASHSEED"),
        ({"distributions": []}, "distribution snapshot"),
    ],
)
def test_rejects_incomplete_or_mismatched_metadata(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    source = _results(tmp_path / "results", **overrides)
    with pytest.raises(benchmark_evidence.BenchmarkEvidenceError, match=message):
        benchmark_evidence.prepare(source, tmp_path / "output", VERSION, COMMIT)


def test_rejects_incomplete_result_directory(tmp_path: Path) -> None:
    source = _results(tmp_path / "results")
    (source / "samples.csv").unlink()
    with pytest.raises(benchmark_evidence.BenchmarkEvidenceError, match="exactly"):
        benchmark_evidence.prepare(source, tmp_path / "output", VERSION, COMMIT)
