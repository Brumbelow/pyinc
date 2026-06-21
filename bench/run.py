"""Benchmark entrypoint.

    python -m bench.run --output-dir bench/results [--warmup N] [--repetitions N]

Runs every workload/scenario, asserts correctness against fresh recomputation,
and writes ``benchmark.csv``, ``benchmark.md`` (generated from the CSV), and
``metadata.json`` under the output directory.
"""

from __future__ import annotations

import argparse
import importlib
import os
import platform
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .report import render_markdown, write_csv, write_metadata
from .scenarios import run_all


def _pyinc_version() -> str:
    try:
        from importlib.metadata import version

        return version("pyinc")
    except Exception:
        return "unknown"


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def _bench_dependency_versions() -> str:
    try:
        joblib = importlib.import_module("joblib")
        return f"joblib=={getattr(joblib, '__version__', 'unknown')}"
    except ImportError:
        return "joblib=not-installed"


def _metadata(warmup: int, repetitions: int) -> dict[str, Any]:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "git_commit": _git_commit(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "pyinc_version": _pyinc_version(),
        "bench_dependencies": _bench_dependency_versions(),
        "warmup": warmup,
        "repetitions": repetitions,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the pyinc benchmark + correctness harness.")
    parser.add_argument("--output-dir", default="bench/results", type=Path)
    parser.add_argument("--warmup", default=1, type=int)
    parser.add_argument("--repetitions", default=5, type=int)
    args = parser.parse_args(argv)

    metadata = _metadata(args.warmup, args.repetitions)
    with tempfile.TemporaryDirectory(prefix="pyinc-bench-") as scratch:
        records = run_all(Path(scratch), warmup=args.warmup, repetitions=args.repetitions)

    output_dir: Path = args.output_dir
    csv_path = output_dir / "benchmark.csv"
    write_csv(records, csv_path)
    write_metadata(metadata, output_dir / "metadata.json")
    (output_dir / "benchmark.md").write_text(render_markdown(csv_path, metadata), encoding="utf-8")

    passed = sum(1 for r in records if r.correctness == "pass")
    print(f"records={len(records)} correctness_pass={passed}")
    print(f"wrote {csv_path}")
    print(f"wrote {output_dir / 'benchmark.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
