"""Run five isolated benchmark repetitions and write workflow artifacts."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from .baselines import required_comparators
from .harness import (
    ALL_TARGETS,
    REPETITIONS,
    ROWS_PER_REPETITION,
    validate_repetition,
    write_reports,
)
from .measure import ScenarioResult

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUTPUT = Path(__file__).resolve().parent / "results"
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"git {' '.join(arguments)} failed: {message}")
    return completed.stdout.strip()


def _cpu_model() -> str:
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.lower().startswith("model name") and ":" in line:
                return line.split(":", 1)[1].strip()
    return platform.processor() or platform.machine() or "unknown"


def build_metadata(repetitions: int) -> dict[str, object]:
    commit_sha = _git("rev-parse", "HEAD").lower()
    if _SHA_PATTERN.fullmatch(commit_sha) is None:
        raise RuntimeError(f"git returned an invalid commit SHA: {commit_sha!r}")
    python_build = platform.python_build()
    return {
        "schema_version": 1,
        "commit_sha": commit_sha,
        "working_tree_dirty": bool(_git("status", "--porcelain")),
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "build_number": python_build[0],
            "build_date": python_build[1],
            "compiler": platform.python_compiler(),
            "executable": sys.executable,
        },
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "platform": platform.platform(),
        },
        "runner": {
            "environment": os.environ.get("RUNNER_ENVIRONMENT", "local"),
            "name": os.environ.get("RUNNER_NAME", "local"),
            "os": os.environ.get("RUNNER_OS", platform.system()),
            "arch": os.environ.get("RUNNER_ARCH", platform.machine()),
        },
        "cpu": {
            "model": _cpu_model(),
            "logical_count": os.cpu_count(),
        },
        "comparators": {
            "full": "in-process fresh recomputation",
            "naive": "intentional per-key stale-cache control",
            "joblib": importlib.metadata.version("joblib"),
        },
        "targets": list(ALL_TARGETS),
        "rows_per_repetition": ROWS_PER_REPETITION,
        "repetitions": repetitions,
        "pythonhashseed": "0",
    }


def _write_worker_result(path: Path, scratch: Path) -> None:
    if os.environ.get("PYTHONHASHSEED") != "0":
        raise RuntimeError("benchmark workers require PYTHONHASHSEED=0")
    from .harness import run_scenarios

    comparators = required_comparators()
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    # The scenarios key their files by the path they are given, and the
    # digest-sorted verification order follows those bytes, so run them from
    # inside the scratch: a relative key reads the same on every machine and
    # under any --output, where an absolute one moved the reuse counts.
    os.chdir(scratch)
    try:
        results = run_scenarios(ALL_TARGETS, out_dir=Path(), comparators=comparators)
    finally:
        os.chdir(_ROOT)
        shutil.rmtree(scratch)
    validate_repetition(results)
    path.write_text(
        json.dumps([result.as_json() for result in results], separators=(",", ":")),
        encoding="utf-8",
    )


def _read_worker_result(path: Path) -> list[ScenarioResult]:
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("benchmark worker result must be a JSON array")
    results: list[ScenarioResult] = []
    for item in payload:
        if not isinstance(item, Mapping):
            raise ValueError("benchmark worker rows must be JSON objects")
        results.append(ScenarioResult.from_json(item))
    return results


def _run_isolated_repetition(output: Path, scratch: Path) -> list[ScenarioResult]:
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "0"
    source = str(_ROOT / "src")
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        source if not existing_pythonpath else source + os.pathsep + existing_pythonpath
    )
    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "bench.run",
            "--_worker-output",
            str(output),
            "--_worker-scratch",
            str(scratch),
        ),
        cwd=_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = "\n".join(
            part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
        )
        raise RuntimeError(f"isolated benchmark worker failed:\n{detail}")
    return _read_worker_result(output)


def run(output: Path, repetitions: int = REPETITIONS) -> tuple[Path, Path, Path, Path]:
    if repetitions != REPETITIONS:
        raise ValueError(f"release benchmark requires exactly {REPETITIONS} repetitions")
    all_results: list[list[ScenarioResult]] = []
    worker_scratch = output.resolve() / ".work"
    with tempfile.TemporaryDirectory(prefix="pyinc-benchmark-results-") as scratch:
        scratch_root = Path(scratch)
        for repetition in range(1, repetitions + 1):
            print(f"benchmark repetition {repetition}/{repetitions}", flush=True)
            all_results.append(
                _run_isolated_repetition(scratch_root / f"{repetition}.json", worker_scratch)
            )
    metadata = build_metadata(repetitions)
    return write_reports(all_results, output, metadata)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help="artifact directory (default: bench/results)",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        choices=(REPETITIONS,),
        default=REPETITIONS,
        help=f"isolated subprocess repetitions (fixed at {REPETITIONS})",
    )
    parser.add_argument("--_worker-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_worker-scratch", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    worker_output: Path | None = args._worker_output
    if worker_output is not None:
        worker_scratch: Path | None = args._worker_scratch
        if worker_scratch is None:
            worker_scratch = worker_output.parent / "pyinc-benchmark-work"
        _write_worker_result(worker_output, worker_scratch)
        return
    output: Path = args.output
    repetitions: int = args.repetitions
    paths = run(output, repetitions)
    print(f"validated_rows_per_repetition={ROWS_PER_REPETITION}")
    for path in paths:
        print(f"artifact={path}")


if __name__ == "__main__":
    main()
