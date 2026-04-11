from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
import time

from pyfoundinc import Database, DirectoryResource, FileResource, Input, query


def _timed(call: object) -> tuple[float, object]:
    assert callable(call)
    started = time.perf_counter()
    result = call()
    return time.perf_counter() - started, result


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def benchmark_diamond(samples: int) -> dict[str, float]:
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

    db = Database(mode="strict")
    db.set(number, 1)
    cold_s, _ = _timed(lambda: db.get(root))
    warm_s = [_timed(lambda: db.get(root))[0] for _ in range(samples)]
    delta_s: list[float] = []
    for value in range(2, samples + 2):
        db.set(number, value)
        delta_s.append(_timed(lambda: db.get(root))[0])
    return {
        "cold_s": cold_s,
        "warm_mean_s": _mean(warm_s),
        "delta_mean_s": _mean(delta_s),
    }


def benchmark_rewiring(samples: int) -> dict[str, float]:
    chooser = Input[str]("chooser")
    left = Input[int]("left")
    right = Input[int]("right")

    @query
    def selected(db: Database) -> int:
        if chooser.read(db) == "left":
            return left.read(db)
        return right.read(db)

    @query
    def root(db: Database) -> int:
        return selected(db) + 1

    db = Database(mode="strict")
    db.set(chooser, "left")
    db.set(left, 1)
    db.set(right, 10)
    cold_s, _ = _timed(lambda: db.get(root))
    warm_s = [_timed(lambda: db.get(root))[0] for _ in range(samples)]
    delta_s: list[float] = []
    current = "left"
    for value in range(samples):
        current = "right" if current == "left" else "left"
        db.set(chooser, current)
        if current == "left":
            db.set(left, value)
        else:
            db.set(right, value)
        delta_s.append(_timed(lambda: db.get(root))[0])
    return {
        "cold_s": cold_s,
        "warm_mean_s": _mean(warm_s),
        "delta_mean_s": _mean(delta_s),
    }


def benchmark_file_resources(samples: int) -> dict[str, float]:
    files = FileResource()
    directories = DirectoryResource()

    @query
    def digest(db: Database, filename: str) -> tuple[str, int]:
        parent = str(Path(filename).parent)
        entries = directories.read(db, parent)
        return files.read(db, filename), len(entries)

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "sample.txt"
        path.write_text("alpha", encoding="utf-8")
        db = Database(mode="strict")
        cold_s, _ = _timed(lambda: db.get(digest, str(path)))
        warm_s = [_timed(lambda: db.get(digest, str(path)))[0] for _ in range(samples)]
        delta_s: list[float] = []
        for value in range(samples):
            path.write_text(f"value-{value}", encoding="utf-8")
            delta_s.append(_timed(lambda: db.get(digest, str(path)))[0])
    return {
        "cold_s": cold_s,
        "warm_mean_s": _mean(warm_s),
        "delta_mean_s": _mean(delta_s),
    }


def benchmark_large_boundary(samples: int, payload_size: int) -> dict[str, float]:
    payload = Input[list[int]]("payload")

    @query
    def mirror(db: Database) -> object:
        return payload.read(db)

    db = Database(mode="checked")
    values = list(range(payload_size))
    db.set(payload, values)
    cold_s, _ = _timed(lambda: db.get(mirror))
    warm_s = [_timed(lambda: db.get(mirror))[0] for _ in range(samples)]

    equal_update_s: list[float] = []
    delta_s: list[float] = []
    for value in range(samples):
        db.set(payload, list(values))
        equal_update_s.append(_timed(lambda: db.get(mirror))[0])
        values[value % payload_size] = value
        db.set(payload, list(values))
        delta_s.append(_timed(lambda: db.get(mirror))[0])
    return {
        "cold_s": cold_s,
        "warm_mean_s": _mean(warm_s),
        "equal_update_mean_s": _mean(equal_update_s),
        "delta_mean_s": _mean(delta_s),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run pyfoundinc kernel microbench scenarios.")
    parser.add_argument("--samples", type=int, default=200, help="Number of warm/delta iterations per scenario.")
    parser.add_argument(
        "--payload-size",
        type=int,
        default=5000,
        help="Input payload size for the large-boundary scenario.",
    )
    args = parser.parse_args()

    results = {
        "samples": args.samples,
        "payload_size": args.payload_size,
        "diamond_reuse": benchmark_diamond(args.samples),
        "dynamic_rewiring": benchmark_rewiring(args.samples),
        "resource_reads": benchmark_file_resources(args.samples),
        "large_boundary": benchmark_large_boundary(args.samples, args.payload_size),
    }
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
