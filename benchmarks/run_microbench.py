from __future__ import annotations

import argparse
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
import gc
import json
import math
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from pyfoundinc import Database, DirectoryResource, FileResource, Input, query


DEFAULT_MODES = {
    "diamond": "strict",
    "rewiring": "strict",
    "files": "strict",
    "large": "checked",
    "backdating": "strict",
}

SCENARIO_KEYS = {
    "diamond": "diamond_reuse",
    "rewiring": "dynamic_rewiring",
    "files": "resource_reads",
    "large": "large_boundary",
    "backdating": "query_backdating",
}


@dataclass(frozen=True)
class QueryCall:
    db: Database
    query: Any
    args: tuple[Any, ...] = ()


@dataclass(frozen=True)
class BenchConfig:
    samples: int
    warmup: int
    rounds: int


CleanupFn = Callable[[], None]
PrepareFn = Callable[[int], None]
SetupCall = Callable[[], tuple[QueryCall, CleanupFn]]
SetupSequence = Callable[[], tuple[QueryCall, PrepareFn, CleanupFn]]


def _noop() -> None:
    return None


@contextmanager
def _gc_suppressed() -> Any:
    was_enabled = gc.isenabled()
    gc.collect()
    if was_enabled:
        gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


def _timed_query(call: QueryCall) -> float:
    started = time.perf_counter()
    call.db.get(call.query, *call.args)
    return time.perf_counter() - started


def _phase_stats(times: list[float]) -> dict[str, int | float]:
    if not times:
        raise ValueError("benchmark phase produced no samples")
    ordered = sorted(times)
    return {
        "count": len(times),
        "min_s": ordered[0],
        "median_s": statistics.median(ordered),
        "mean_s": statistics.mean(times),
        "stdev_s": statistics.stdev(times) if len(times) > 1 else 0.0,
        "p95_s": ordered[_percentile_index(len(ordered), 0.95)],
        "max_s": ordered[-1],
    }


def _percentile_index(size: int, percentile: float) -> int:
    return max(0, min(size - 1, math.ceil(size * percentile) - 1))


def _measure_cold(setup: SetupCall, *, rounds: int) -> dict[str, int | float]:
    times: list[float] = []
    for _ in range(rounds):
        call, cleanup = setup()
        try:
            with _gc_suppressed():
                times.append(_timed_query(call))
        finally:
            cleanup()
    return _phase_stats(times)


def _measure_cached(setup: SetupCall, config: BenchConfig) -> dict[str, int | float]:
    times: list[float] = []
    for _ in range(config.rounds):
        call, cleanup = setup()
        try:
            call.db.get(call.query, *call.args)
            for _ in range(config.warmup):
                call.db.get(call.query, *call.args)
            with _gc_suppressed():
                for _ in range(config.samples):
                    times.append(_timed_query(call))
        finally:
            cleanup()
    return _phase_stats(times)


def _measure_sequence(setup: SetupSequence, config: BenchConfig) -> dict[str, int | float]:
    times: list[float] = []
    for _ in range(config.rounds):
        call, prepare, cleanup = setup()
        try:
            call.db.get(call.query, *call.args)
            for step in range(config.warmup):
                prepare(step)
                call.db.get(call.query, *call.args)
            with _gc_suppressed():
                for step in range(config.warmup, config.warmup + config.samples):
                    prepare(step)
                    times.append(_timed_query(call))
        finally:
            cleanup()
    return _phase_stats(times)


def _query_record(db: Database, query_obj: Any, *args: Any) -> Any:
    key, _ = db._query_key(query_obj, args, {})
    return db._records[key]


def _resolved_mode(benchmark: str, override: str | None) -> str:
    return DEFAULT_MODES[benchmark] if override is None else override


def _build_diamond(mode: str) -> tuple[QueryCall, Input[int]]:
    number = Input[int]("number")

    @query
    def left(db: Database) -> int:
        return number.read(db) + 1

    @query
    def right(db: Database) -> int:
        return number.read(db) + 2

    @query
    def root(db: Database) -> int:
        return left(db) * right(db)

    db = Database(mode=mode)
    db.set(number, 1)
    return QueryCall(db, root), number


def _preflight_diamond(mode: str) -> None:
    call, number = _build_diamond(mode)
    baseline = call.db.get(call.query)
    call.db.set(number, 2)
    updated = call.db.get(call.query)
    if updated == baseline:
        raise RuntimeError("diamond preflight failed: input update did not change the root value")


def benchmark_diamond(config: BenchConfig, mode: str) -> dict[str, Any]:
    _preflight_diamond(mode)

    def setup_call() -> tuple[QueryCall, CleanupFn]:
        call, _ = _build_diamond(mode)
        return call, _noop

    def setup_delta() -> tuple[QueryCall, PrepareFn, CleanupFn]:
        call, number = _build_diamond(mode)
        next_value = 2

        def prepare(_: int) -> None:
            nonlocal next_value
            call.db.set(number, next_value)
            next_value += 1

        return call, prepare, _noop

    return {
        "mode": mode,
        "cold": _measure_cold(setup_call, rounds=config.rounds),
        "warm": _measure_cached(setup_call, config),
        "delta": _measure_sequence(setup_delta, config),
    }


def _build_rewiring(mode: str) -> tuple[QueryCall, Input[str], Input[int], Input[int]]:
    chooser = Input[str]("chooser")
    left_input = Input[int]("left")
    right_input = Input[int]("right")

    @query
    def selected(db: Database) -> int:
        if chooser.read(db) == "left":
            return left_input.read(db)
        return right_input.read(db)

    @query
    def root(db: Database) -> int:
        return selected(db) + 1

    db = Database(mode=mode)
    db.set(chooser, "left")
    db.set(left_input, 1)
    db.set(right_input, 10)
    return QueryCall(db, root), chooser, left_input, right_input


def _preflight_rewiring(mode: str) -> None:
    call, chooser, left_input, right_input = _build_rewiring(mode)
    initial = call.db.get(call.query)
    if initial != 2:
        raise RuntimeError(f"rewiring preflight failed: expected left branch result 2, got {initial!r}")
    call.db.set(chooser, "right")
    call.db.set(right_input, 11)
    switched = call.db.get(call.query)
    if switched != 12:
        raise RuntimeError(f"rewiring preflight failed: expected right branch result 12, got {switched!r}")
    call.db.set(left_input, 999)
    reused = call.db.get(call.query)
    if reused != switched:
        raise RuntimeError("rewiring preflight failed: stale left dependency still affected the result")


def benchmark_rewiring(config: BenchConfig, mode: str) -> dict[str, Any]:
    _preflight_rewiring(mode)

    def setup_call() -> tuple[QueryCall, CleanupFn]:
        call, _, _, _ = _build_rewiring(mode)
        return call, _noop

    def setup_delta() -> tuple[QueryCall, PrepareFn, CleanupFn]:
        call, chooser, left_input, right_input = _build_rewiring(mode)
        current = "left"
        next_left = 2
        next_right = 20

        def prepare(_: int) -> None:
            nonlocal current, next_left, next_right
            current = "right" if current == "left" else "left"
            call.db.set(chooser, current)
            if current == "left":
                call.db.set(left_input, next_left)
                next_left += 1
            else:
                call.db.set(right_input, next_right)
                next_right += 1

        return call, prepare, _noop

    return {
        "mode": mode,
        "cold": _measure_cold(setup_call, rounds=config.rounds),
        "warm": _measure_cached(setup_call, config),
        "delta": _measure_sequence(setup_delta, config),
    }


def _build_file_resources(mode: str) -> tuple[QueryCall, Path, CleanupFn]:
    files = FileResource()
    directories = DirectoryResource()

    @query
    def digest(db: Database, filename: str) -> tuple[str, int]:
        parent = str(Path(filename).parent)
        entries = directories.read(db, parent)
        return files.read(db, filename), len(entries)

    tmpdir = tempfile.TemporaryDirectory()
    path = Path(tmpdir.name) / "sample.txt"
    path.write_text("alpha", encoding="utf-8")
    db = Database(mode=mode)
    return QueryCall(db, digest, (str(path),)), path, tmpdir.cleanup


def _preflight_file_resources(mode: str) -> None:
    call, path, cleanup = _build_file_resources(mode)
    try:
        baseline = call.db.get(call.query, *call.args)
        path.write_text("beta", encoding="utf-8")
        updated = call.db.get(call.query, *call.args)
        if updated == baseline:
            raise RuntimeError("file resource preflight failed: file change did not affect the result")
    finally:
        cleanup()


def benchmark_file_resources(config: BenchConfig, mode: str) -> dict[str, Any]:
    _preflight_file_resources(mode)

    def setup_call() -> tuple[QueryCall, CleanupFn]:
        call, _, cleanup = _build_file_resources(mode)
        return call, cleanup

    def setup_delta() -> tuple[QueryCall, PrepareFn, CleanupFn]:
        call, path, cleanup = _build_file_resources(mode)

        def prepare(step: int) -> None:
            path.write_text(f"value-{step}", encoding="utf-8")

        return call, prepare, cleanup

    return {
        "mode": mode,
        "cold": _measure_cold(setup_call, rounds=config.rounds),
        "warm": _measure_cached(setup_call, config),
        "delta": _measure_sequence(setup_delta, config),
    }


def _build_large_boundary(mode: str, payload_size: int) -> tuple[QueryCall, Input[list[int]], list[int]]:
    payload = Input[list[int]]("payload")

    @query
    def mirror(db: Database) -> object:
        return payload.read(db)

    db = Database(mode=mode)
    values = list(range(payload_size))
    db.set(payload, values)
    return QueryCall(db, mirror), payload, values


def _preflight_large_boundary(mode: str, payload_size: int) -> None:
    call, payload, values = _build_large_boundary(mode, payload_size)
    baseline = call.db.get(call.query)
    baseline_revision = call.db.revision

    call.db.set(payload, values)
    if call.db.revision != baseline_revision:
        raise RuntimeError("large boundary preflight failed: identical update advanced the revision")
    identical = call.db.get(call.query)
    if identical != baseline:
        raise RuntimeError("large boundary preflight failed: identical update changed the result")

    call.db.set(payload, list(values))
    if call.db.revision != baseline_revision:
        raise RuntimeError("large boundary preflight failed: equal-content update advanced the revision")
    equal_update = call.db.get(call.query)
    if equal_update != baseline:
        raise RuntimeError("large boundary preflight failed: equal-content update changed the result")

    values[0] += payload_size
    call.db.set(payload, values)
    if call.db.revision == baseline_revision:
        raise RuntimeError("large boundary preflight failed: delta update did not advance the revision")
    delta = call.db.get(call.query)
    if delta == baseline:
        raise RuntimeError("large boundary preflight failed: delta update did not change the result")


def benchmark_large_boundary(config: BenchConfig, mode: str, payload_size: int) -> dict[str, Any]:
    _preflight_large_boundary(mode, payload_size)

    def setup_call() -> tuple[QueryCall, CleanupFn]:
        call, _, _ = _build_large_boundary(mode, payload_size)
        return call, _noop

    def setup_identical() -> tuple[QueryCall, PrepareFn, CleanupFn]:
        call, payload, values = _build_large_boundary(mode, payload_size)

        def prepare(_: int) -> None:
            call.db.set(payload, values)

        return call, prepare, _noop

    def setup_equal() -> tuple[QueryCall, PrepareFn, CleanupFn]:
        call, payload, values = _build_large_boundary(mode, payload_size)

        def prepare(_: int) -> None:
            call.db.set(payload, list(values))

        return call, prepare, _noop

    def setup_delta() -> tuple[QueryCall, PrepareFn, CleanupFn]:
        call, payload, values = _build_large_boundary(mode, payload_size)
        next_index = 0
        next_value = payload_size

        def prepare(_: int) -> None:
            nonlocal next_index, next_value
            values[next_index % payload_size] = next_value
            next_index += 1
            next_value += 1
            call.db.set(payload, values)

        return call, prepare, _noop

    return {
        "mode": mode,
        "cold": _measure_cold(setup_call, rounds=config.rounds),
        "warm": _measure_cached(setup_call, config),
        "identical_update": _measure_sequence(setup_identical, config),
        "equal_update": _measure_sequence(setup_equal, config),
        "delta": _measure_sequence(setup_delta, config),
    }


def _build_query_backdating(mode: str) -> tuple[QueryCall, Input[int], Any]:
    source = Input[int]("source")

    @query
    def middle(db: Database) -> int:
        return abs(source.read(db))

    @query
    def root(db: Database) -> int:
        return middle(db) * 10 + 5

    db = Database(mode=mode)
    db.set(source, 42)
    return QueryCall(db, root), source, middle


def _preflight_query_backdating(mode: str) -> None:
    call, source, middle = _build_query_backdating(mode)
    baseline = call.db.get(call.query)
    root_record = _query_record(call.db, call.query)
    changed_at = root_record.changed_at

    call.db.set(source, -42)
    updated = call.db.get(call.query)
    middle_record = _query_record(call.db, middle)
    root_record = _query_record(call.db, call.query)

    if updated != baseline:
        raise RuntimeError("query backdating preflight failed: equal recompute changed the root value")
    if middle_record.last_recompute != "backdated":
        raise RuntimeError("query backdating preflight failed: middle query did not backdate")
    if root_record.last_decision != "reused":
        raise RuntimeError("query backdating preflight failed: downstream root was recomputed")
    if root_record.changed_at != changed_at:
        raise RuntimeError("query backdating preflight failed: downstream root changed_at was not preserved")


def benchmark_query_backdating(config: BenchConfig, mode: str) -> dict[str, Any]:
    _preflight_query_backdating(mode)

    def setup_call() -> tuple[QueryCall, CleanupFn]:
        call, _, _ = _build_query_backdating(mode)
        return call, _noop

    def setup_backdate() -> tuple[QueryCall, PrepareFn, CleanupFn]:
        call, source, _ = _build_query_backdating(mode)
        current = 42

        def prepare(_: int) -> None:
            nonlocal current
            current = -42 if current == 42 else 42
            call.db.set(source, current)

        return call, prepare, _noop

    def setup_real_change() -> tuple[QueryCall, PrepareFn, CleanupFn]:
        call, source, _ = _build_query_backdating(mode)
        next_value = 100

        def prepare(_: int) -> None:
            nonlocal next_value
            call.db.set(source, next_value)
            next_value += 1

        return call, prepare, _noop

    return {
        "mode": mode,
        "cold": _measure_cold(setup_call, rounds=config.rounds),
        "warm": _measure_cached(setup_call, config),
        "backdate": _measure_sequence(setup_backdate, config),
        "real_change": _measure_sequence(setup_real_change, config),
    }


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.samples <= 0:
        parser.error("--samples must be a positive integer")
    if args.warmup < 0:
        parser.error("--warmup must be zero or a positive integer")
    if args.rounds <= 0:
        parser.error("--rounds must be a positive integer")
    if args.payload_size <= 0:
        parser.error("--payload-size must be a positive integer")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run pyfoundinc kernel microbench scenarios.")
    parser.add_argument("--samples", type=int, default=200, help="Measured iterations per phase and round.")
    parser.add_argument("--warmup", type=int, default=50, help="Untimed warmup iterations before each measured phase.")
    parser.add_argument("--rounds", type=int, default=5, help="Independent benchmark rounds per phase.")
    parser.add_argument(
        "--payload-size",
        type=int,
        default=5000,
        help="Input payload size for the large-boundary scenario.",
    )
    parser.add_argument(
        "--bench",
        choices=["all", "diamond", "rewiring", "files", "large", "backdating"],
        default="all",
        help="Run a single benchmark scenario or the full suite.",
    )
    parser.add_argument(
        "--mode",
        choices=["strict", "checked", "fast"],
        default=None,
        help="Override the default mode for all selected scenarios.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path for writing the same JSON payload produced on stdout.",
    )
    args = parser.parse_args()
    _validate_args(parser, args)

    config = BenchConfig(samples=args.samples, warmup=args.warmup, rounds=args.rounds)
    selected = [args.bench] if args.bench != "all" else ["diamond", "rewiring", "files", "large", "backdating"]

    results: dict[str, dict[str, Any]] = {}
    for benchmark in selected:
        mode = _resolved_mode(benchmark, args.mode)
        if benchmark == "diamond":
            results[SCENARIO_KEYS[benchmark]] = benchmark_diamond(config, mode)
        elif benchmark == "rewiring":
            results[SCENARIO_KEYS[benchmark]] = benchmark_rewiring(config, mode)
        elif benchmark == "files":
            results[SCENARIO_KEYS[benchmark]] = benchmark_file_resources(config, mode)
        elif benchmark == "large":
            results[SCENARIO_KEYS[benchmark]] = benchmark_large_boundary(config, mode, args.payload_size)
        elif benchmark == "backdating":
            results[SCENARIO_KEYS[benchmark]] = benchmark_query_backdating(config, mode)

    payload = {
        "config": {
            "samples": args.samples,
            "warmup": args.warmup,
            "rounds": args.rounds,
            "payload_size": args.payload_size,
            "bench": args.bench,
            "mode_override": args.mode,
        },
        "results": results,
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True)
    print(encoded)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
