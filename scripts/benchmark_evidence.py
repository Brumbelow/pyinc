"""Build and verify a durable benchmark-evidence release bundle."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

_VERSION_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+){2}(?:(?:a|b|rc)[0-9]+)?")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_SOURCE_MEMBERS = {
    "samples.csv": "samples.csv",
    "benchmark.csv": "benchmark.csv",
    "benchmark.md": "benchmark.md",
    "metadata.json": "metadata.json",
}
_COMMAND = (
    "PYTHONHASHSEED=0 PYTHONPATH=src python -m bench.run --output bench/results --repetitions 5\n"
)
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class BenchmarkEvidenceError(ValueError):
    """Benchmark evidence is incomplete, inconsistent, or corrupt."""


def _reject(message: str) -> NoReturn:
    raise BenchmarkEvidenceError(message)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _checksums(payloads: Mapping[str, bytes]) -> bytes:
    return "".join(f"{_sha256(payloads[name])}  {name}\n" for name in sorted(payloads)).encode(
        "ascii"
    )


def bundle_name(version: str) -> str:
    """Return the canonical benchmark-evidence bundle name."""
    if _VERSION_PATTERN.fullmatch(version) is None:
        _reject(f"invalid release version: {version!r}")
    return f"pyinc-{version}-benchmark-evidence.zip"


def _metadata(payload: bytes, expected_commit: str, expected_version: str) -> None:
    try:
        document: object = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _reject(f"metadata.json is not valid UTF-8 JSON: {exc}")
    if not isinstance(document, dict):
        _reject("metadata.json must contain an object")
    if document.get("schema_version") != 1:
        _reject("release benchmark metadata must use schema_version 1")
    if document.get("commit_sha") != expected_commit:
        _reject("metadata.json commit_sha does not match the release commit")
    if document.get("working_tree_dirty") is not False:
        _reject("release benchmark metadata must record a clean working tree")
    if document.get("repetitions") != 5:
        _reject("release benchmark metadata must record five repetitions")
    generated_at = document.get("generated_at_utc")
    if not isinstance(generated_at, str):
        _reject("release benchmark metadata must record generated_at_utc")
    try:
        instant = datetime.fromisoformat(generated_at)
    except ValueError:
        _reject("release benchmark generated_at_utc must be an ISO timestamp")
    if instant.utcoffset() != UTC.utcoffset(instant):
        _reject("release benchmark generated_at_utc must be in UTC")
    if document.get("pyinc_version") != expected_version:
        _reject("release benchmark pyinc_version does not match the release version")
    if document.get("pythonhashseed") != "0":
        _reject("release benchmark metadata must record PYTHONHASHSEED=0")
    distributions = document.get("distributions")
    if not isinstance(distributions, list) or not distributions:
        _reject("release benchmark metadata must record the distribution snapshot")
    if not any(
        isinstance(item, dict)
        and item.get("name") == "pyinc"
        and item.get("version") == expected_version
        for item in distributions
    ):
        _reject("release benchmark distribution snapshot must contain the release pyinc")


def _zip_payload(payloads: Mapping[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(payloads):
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payloads[name])
    return stream.getvalue()


def prepare(source: Path, output: Path, version: str, expected_commit: str) -> tuple[Path, Path]:
    """Create the canonical evidence bundle and its external checksum."""
    if _COMMIT_PATTERN.fullmatch(expected_commit) is None:
        _reject(f"invalid release commit: {expected_commit!r}")
    if not source.is_dir():
        _reject(f"benchmark result directory does not exist: {source}")
    observed = frozenset(path.name for path in source.iterdir() if path.is_file())
    expected = frozenset(_SOURCE_MEMBERS.values())
    if observed != expected:
        _reject(
            "benchmark result directory must contain exactly "
            f"{', '.join(sorted(expected))}; found {', '.join(sorted(observed)) or 'nothing'}"
        )

    payloads = {
        archive_name: (source / source_name).read_bytes()
        for archive_name, source_name in _SOURCE_MEMBERS.items()
    }
    _metadata(payloads["metadata.json"], expected_commit, version)
    payloads["command.txt"] = _COMMAND.encode("utf-8")
    payloads["SHA256SUMS"] = _checksums(payloads)
    bundle = _zip_payload(payloads)

    output.mkdir(parents=True, exist_ok=True)
    bundle_path = output / bundle_name(version)
    checksum_path = output / "BENCHMARK-SHA256SUMS"
    bundle_path.write_bytes(bundle)
    checksum_path.write_text(
        f"{_sha256(bundle)}  {bundle_path.name}\n", encoding="ascii", newline="\n"
    )
    return bundle_path, checksum_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    bundle, checksum = prepare(
        arguments.source, arguments.output, arguments.version, arguments.commit.lower()
    )
    print(f"benchmark_bundle={bundle}")
    print(f"benchmark_checksums={checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
