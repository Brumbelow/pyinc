"""Capture installed-wheel examples as durable release evidence."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, cast

_VERSION_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+){2}(?:(?:a|b|rc)[0-9]+)?")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_EXECUTION_ENVIRONMENT = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONHASHSEED": "0",
    "PYTHONNOUSERSITE": "1",
}
_REMOVED_ENVIRONMENT = ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP")
_INTERPRETER_PROBE = r"""
import importlib.metadata
import json
import platform
import re
import sys

distributions = []
for distribution in importlib.metadata.distributions():
    name = distribution.metadata.get("Name")
    if not isinstance(name, str) or not name.strip():
        continue
    distributions.append(
        {
            "name": name,
            "normalized_name": re.sub(r"[-_.]+", "-", name).casefold(),
            "version": distribution.version,
        }
    )
distributions.sort(
    key=lambda item: (item["normalized_name"], item["name"].casefold(), item["version"])
)
document = {
    "python": {
        "build": " ".join(platform.python_build()),
        "cache_tag": sys.implementation.cache_tag or "",
        "compiler": platform.python_compiler(),
        "executable": sys.executable,
        "implementation": platform.python_implementation(),
        "implementation_name": sys.implementation.name,
        "version": platform.python_version(),
        "version_raw": sys.version,
    },
    "os": {
        "machine": platform.machine(),
        "platform": platform.platform(),
        "release": platform.release(),
        "system": platform.system(),
        "version": platform.version(),
    },
    "distributions": distributions,
}
print(json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
""".strip()


class DemoEvidenceError(RuntimeError):
    """Demo evidence could not be captured completely and consistently."""


@dataclass(frozen=True)
class DistributionSnapshot:
    """One installed distribution reported by the supplied interpreter."""

    name: str
    normalized_name: str
    version: str

    def document(self) -> dict[str, str]:
        return {
            "name": self.name,
            "normalized_name": self.normalized_name,
            "version": self.version,
        }


@dataclass(frozen=True)
class InterpreterSnapshot:
    """Python, operating-system, and distribution facts from one interpreter."""

    python: Mapping[str, str]
    os: Mapping[str, str]
    distributions: tuple[DistributionSnapshot, ...]


@dataclass(frozen=True)
class GitSnapshot:
    """Repository identity captured without including potentially sensitive paths."""

    commit_sha: str
    working_tree_dirty: bool
    status_sha256: str


@dataclass(frozen=True)
class ExampleRun:
    """Raw result of executing one shipped example."""

    index: int
    example: str
    source_sha256: str
    argv: tuple[str, ...]
    cwd: str
    exit_code: int
    stdout: bytes
    stderr: bytes

    @property
    def stdout_path(self) -> str:
        return f"runs/{self.index:03d}.stdout"

    @property
    def stderr_path(self) -> str:
        return f"runs/{self.index:03d}.stderr"

    def document(self) -> dict[str, object]:
        return {
            "argv": list(self.argv),
            "cwd": self.cwd,
            "environment": dict(sorted(_EXECUTION_ENVIRONMENT.items())),
            "environment_removed": list(_REMOVED_ENVIRONMENT),
            "example": self.example,
            "exit_code": self.exit_code,
            "source_sha256": self.source_sha256,
            "stderr": {
                "byte_length": len(self.stderr),
                "path": self.stderr_path,
                "sha256": _sha256(self.stderr),
            },
            "stdout": {
                "byte_length": len(self.stdout),
                "path": self.stdout_path,
                "sha256": _sha256(self.stdout),
            },
        }


class DemoExecutionError(DemoEvidenceError):
    """One or more examples returned a nonzero exit code."""

    runs: tuple[ExampleRun, ...]

    def __init__(self, runs: tuple[ExampleRun, ...]) -> None:
        self.runs = runs
        failures = ", ".join(
            f"{run.example}={run.exit_code}" for run in runs if run.exit_code != 0
        )
        super().__init__(f"demo execution failed: {failures}")


def _reject(message: str) -> NoReturn:
    raise DemoEvidenceError(message)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(document: object) -> bytes:
    return (
        json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")


def _checksums(payloads: Mapping[str, bytes]) -> bytes:
    return "".join(
        f"{_sha256(payloads[name])}  {name}\n" for name in sorted(payloads)
    ).encode("ascii")


def bundle_name(version: str) -> str:
    """Return the canonical versioned demo-evidence bundle name."""
    if _VERSION_PATTERN.fullmatch(version) is None:
        _reject(f"invalid release version: {version!r}")
    return f"pyinc-{version}-demo-evidence.zip"


def _absolute_without_resolving_symlinks(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _python_path(python: Path) -> Path:
    absolute = _absolute_without_resolving_symlinks(python)
    if not absolute.is_file():
        _reject(f"supplied Python interpreter is not a file: {absolute}")
    return absolute


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
        return _reject(f"could not execute {argv[0]!r}: {type(exc).__name__}: {exc}")


def _git_command(project_root: Path, arguments: Sequence[str]) -> bytes:
    result = _run(("git", "-C", os.fspath(project_root), *arguments), cwd=project_root)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip() or "no stderr"
        _reject(f"git {' '.join(arguments)} failed with {result.returncode}: {detail}")
    return result.stdout


def _git_snapshot(project_root: Path) -> GitSnapshot:
    raw_commit = _git_command(project_root, ("rev-parse", "--verify", "HEAD")).strip()
    try:
        commit = raw_commit.decode("ascii").lower()
    except UnicodeDecodeError:
        _reject("git rev-parse returned a non-ASCII commit")
    if _COMMIT_PATTERN.fullmatch(commit) is None:
        _reject(f"git rev-parse returned an invalid commit: {commit!r}")
    status = _git_command(
        project_root, ("status", "--porcelain=v1", "--untracked-files=all")
    )
    return GitSnapshot(
        commit_sha=commit,
        working_tree_dirty=bool(status),
        status_sha256=_sha256(status),
    )


def _string_mapping(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        _reject(f"interpreter probe {label} must be an object")
    mapping = cast("dict[object, object]", value)
    if not all(isinstance(key, str) and isinstance(item, str) for key, item in mapping.items()):
        _reject(f"interpreter probe {label} fields must be strings")
    return cast("dict[str, str]", mapping)


def _interpreter_snapshot(project_root: Path, python: Path) -> InterpreterSnapshot:
    result = _run((os.fspath(python), "-I", "-c", _INTERPRETER_PROBE), cwd=project_root)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip() or "no stderr"
        _reject(f"supplied Python interpreter probe failed with {result.returncode}: {detail}")
    try:
        document: object = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _reject(f"supplied Python interpreter returned invalid metadata JSON: {exc}")
    if not isinstance(document, dict):
        _reject("supplied Python interpreter metadata must be an object")
    raw_document = cast("dict[str, object]", document)
    python_document = _string_mapping(raw_document.get("python"), "python")
    os_document = _string_mapping(raw_document.get("os"), "os")
    raw_distributions = raw_document.get("distributions")
    if not isinstance(raw_distributions, list):
        _reject("interpreter probe distributions must be an array")
    distributions: list[DistributionSnapshot] = []
    for index, value in enumerate(raw_distributions):
        fields = _string_mapping(value, f"distributions[{index}]")
        required = {"name", "normalized_name", "version"}
        if fields.keys() != required:
            _reject(f"interpreter probe distributions[{index}] has unexpected fields")
        distributions.append(
            DistributionSnapshot(
                name=fields["name"],
                normalized_name=fields["normalized_name"],
                version=fields["version"],
            )
        )
    return InterpreterSnapshot(
        python=python_document,
        os=os_document,
        distributions=tuple(distributions),
    )


def _verify_release_distribution(snapshot: InterpreterSnapshot, version: str) -> None:
    installed_versions = {
        distribution.version
        for distribution in snapshot.distributions
        if distribution.normalized_name == "pyinc"
    }
    if installed_versions != {version}:
        observed = ", ".join(sorted(installed_versions)) or "not installed"
        _reject(
            f"supplied Python interpreter has pyinc {observed}; expected release {version}"
        )


def _examples(project_root: Path) -> tuple[Path, ...]:
    directory = project_root / "examples"
    if not directory.is_dir():
        _reject(f"examples directory does not exist: {directory}")
    examples = tuple(sorted(directory.glob("*.py"), key=lambda path: path.name))
    if not examples:
        _reject("examples directory contains no top-level Python files")
    for example in examples:
        if example.is_symlink() or not example.is_file():
            _reject(f"example must be a regular non-symlink file: {example}")
    return examples


def _example_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in _REMOVED_ENVIRONMENT:
        environment.pop(name, None)
    environment.update(_EXECUTION_ENVIRONMENT)
    return environment


def _run_examples(project_root: Path, python: Path) -> tuple[ExampleRun, ...]:
    environment = _example_environment()
    runs: list[ExampleRun] = []
    for index, example in enumerate(_examples(project_root)):
        relative = example.relative_to(project_root).as_posix()
        argv = (os.fspath(python), relative)
        source = example.read_bytes()
        result = _run(argv, cwd=project_root, environment=environment)
        if example.read_bytes() != source:
            _reject(f"example source changed during execution: {relative}")
        runs.append(
            ExampleRun(
                index=index,
                example=relative,
                source_sha256=_sha256(source),
                argv=argv,
                cwd=os.fspath(project_root),
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        )
    return tuple(runs)


def _timestamp(generated_at_utc: datetime | None) -> str:
    instant = datetime.now(UTC) if generated_at_utc is None else generated_at_utc
    if instant.tzinfo is None or instant.utcoffset() != UTC.utcoffset(instant):
        _reject("generated_at_utc must be timezone-aware UTC")
    return instant.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _payloads(
    *,
    version: str,
    generated_at_utc: str,
    git: GitSnapshot,
    interpreter: InterpreterSnapshot,
    runs: tuple[ExampleRun, ...],
) -> dict[str, bytes]:
    metadata = {
        "commit_sha": git.commit_sha,
        "distribution_snapshot": [item.document() for item in interpreter.distributions],
        "evidence_kind": "pyinc-demo",
        "example_count": len(runs),
        "example_glob": "examples/*.py",
        "generated_at_utc": generated_at_utc,
        "git_status_sha256": git.status_sha256,
        "os": dict(sorted(interpreter.os.items())),
        "python": dict(sorted(interpreter.python.items())),
        "release_version": version,
        "schema_version": 1,
        "working_tree_dirty": git.working_tree_dirty,
    }
    payloads = {
        "metadata.json": _json_bytes(metadata),
        "runs.json": _json_bytes([run.document() for run in runs]),
    }
    for run in runs:
        payloads[run.stdout_path] = run.stdout
        payloads[run.stderr_path] = run.stderr
    payloads["SHA256SUMS"] = _checksums(payloads)
    return payloads


def _zip_payload(payloads: Mapping[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(payloads):
            if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
                _reject(f"unsafe demo evidence member: {name!r}")
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, payloads[name])
    return stream.getvalue()


def _write_exact(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        _reject(f"refusing to write evidence through a symlink: {path}")
    if path.exists():
        if not path.is_file():
            _reject(f"evidence output is not a regular file: {path}")
        if path.read_bytes() != payload:
            _reject(f"refusing to overwrite different evidence: {path}")
        return
    path.write_bytes(payload)


def capture(
    project_root: Path,
    output: Path,
    python: Path,
    version: str,
    *,
    generated_at_utc: datetime | None = None,
) -> tuple[Path, Path]:
    """Run every top-level example and create the versioned evidence assets."""
    canonical_bundle_name = bundle_name(version)
    root = project_root.resolve(strict=True)
    if not root.is_dir():
        _reject(f"project root is not a directory: {root}")
    interpreter_path = _python_path(python)
    initial_git = _git_snapshot(root)
    interpreter = _interpreter_snapshot(root, interpreter_path)
    _verify_release_distribution(interpreter, version)
    runs = _run_examples(root, interpreter_path)
    final_git = _git_snapshot(root)
    if final_git != initial_git:
        _reject("repository commit or working-tree state changed during demo capture")
    if any(run.exit_code != 0 for run in runs):
        raise DemoExecutionError(runs)

    payloads = _payloads(
        version=version,
        generated_at_utc=_timestamp(generated_at_utc),
        git=initial_git,
        interpreter=interpreter,
        runs=runs,
    )
    bundle = _zip_payload(payloads)
    output.mkdir(parents=True, exist_ok=True)
    bundle_path = output / canonical_bundle_name
    checksum_path = output / "DEMO-SHA256SUMS"
    _write_exact(bundle_path, bundle)
    _write_exact(
        checksum_path,
        f"{_sha256(bundle)}  {canonical_bundle_name}\n".encode("ascii"),
    )
    return bundle_path, checksum_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--version", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    bundle, checksums = capture(
        arguments.project_root,
        arguments.output,
        arguments.python,
        arguments.version,
    )
    print(f"demo_bundle={bundle}")
    print(f"demo_checksums={checksums}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
