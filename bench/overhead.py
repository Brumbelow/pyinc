"""Same-run overhead ratios for the kernel and the workspace tools.

Run from the repository root:

    python -m bench.overhead [--root PATH] [--repeat N]

The script prints five lines and nothing else. Each line names a comparison,
the ratio of its first timing to its second, and both timings. Both sides of a
ratio run in this process, one after the other:

1. a warm ``db.get`` of a trivial query against a ``functools.lru_cache`` hit;
2. a warm get of a query that reads a file through ``FileResource`` against
   ``open().read()`` of the same file;
3. a warm get of a query annotated ``db: Database`` in a module without
   ``from __future__ import annotations`` against the same query in this
   module, which has the import;
4. a cold ``WorkspaceSession.analyze_workspace`` on ``--root`` against a
   second, no-change analysis in the same session, with the number of queries
   the second analysis executed;
5. the read of line 2 with the resource identity memo off against the same
   read with the memo on.

A timing of lines 1, 2, 3 and 5 is the fastest of ``--repeat`` loops, divided
by the loop's call count. The count doubles until one loop takes 20 ms, so a
fast call stays above the clock's resolution. Line 4 times one cold and one
no-change analysis, since each can take seconds.

The ratios are machine-specific. The cost of opening a file, the interpreter
build and the load on the host all move them. The absolute timings are
diagnostics.
"""

from __future__ import annotations

import argparse
import functools
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest import mock

from pyinc import Database, FileResource, Input, query
from pyinc import runtime as _runtime
from pyinc_tools import WorkspaceSession

from . import _overhead_annotated

_DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "src" / "pyinc_tools"
_DEFAULT_REPEAT = 5
_LOOP_TARGET_NS = 20_000_000
# 64 lines of 64 bytes, a 4 KiB text file.
_FILE_TEXT = ("x" * 63 + "\n") * 64

_NUMBER = Input[int]("bench-overhead-number")
_FILES = FileResource()


@query
def _double(db: Database) -> int:
    return _NUMBER.read(db) * 2


@query
def _file_text(db: Database, path: str) -> str:
    return _FILES.read(db, path)


@functools.lru_cache
def _cached_double() -> int:
    return 3 * 2


def _loop_ns(call: Callable[[], object], number: int) -> int:
    start = time.perf_counter_ns()
    for _ in range(number):
        call()
    return time.perf_counter_ns() - start


def best_ns(call: Callable[[], object], repeat: int) -> float:
    """Return the fastest per-call time of ``call`` in nanoseconds.

    The fastest loop is the one the host disturbed least.
    """
    number = 1
    while _loop_ns(call, number) < _LOOP_TARGET_NS:
        number *= 2
    return min(_loop_ns(call, number) for _ in range(repeat)) / number


def _duration(ns: float) -> str:
    for unit, scale in (("s", 1e9), ("ms", 1e6), ("us", 1e3)):
        if ns >= scale:
            return f"{ns / scale:.2f} {unit}"
    return f"{ns:.2f} ns"


def _line(label: str, first_ns: float, second_ns: float) -> str:
    ratio = first_ns / second_ns
    return f"{label}: {ratio:.2f}x ({_duration(first_ns)} / {_duration(second_ns)})"


def _warm_get_line(repeat: int) -> str:
    db = Database()
    db.set(_NUMBER, 3)
    db.get(_double)
    _cached_double()
    warm = best_ns(lambda: db.get(_double), repeat)
    hit = best_ns(_cached_double, repeat)
    return _line("warm-get/lru-cache-hit", warm, hit)


def _open_read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _tracked_read_line(repeat: int, path: str) -> str:
    db = Database()
    db.get(_file_text, path)
    tracked = best_ns(lambda: db.get(_file_text, path), repeat)
    plain = best_ns(lambda: _open_read(path), repeat)
    return _line("tracked-read/open-read", tracked, plain)


def _annotation_line(repeat: int) -> str:
    db = Database()
    db.set(_NUMBER, 3)
    db.set(_overhead_annotated.NUMBER, 3)
    db.get(_overhead_annotated.annotated_double)
    db.get(_double)
    evaluated = best_ns(lambda: db.get(_overhead_annotated.annotated_double), repeat)
    future = best_ns(lambda: db.get(_double), repeat)
    return _line("evaluated-annotation/future-annotation", evaluated, future)


def _analysis_line(root: Path) -> str:
    with WorkspaceSession(root) as session:
        start = time.perf_counter_ns()
        session.analyze_workspace()
        cold = time.perf_counter_ns() - start
        executions_before = session.db.statistics().query_executions
        start = time.perf_counter_ns()
        session.analyze_workspace()
        no_change = time.perf_counter_ns() - start
        executions = session.db.statistics().query_executions - executions_before
    return (
        _line("cold-analysis/no-change-analysis", cold, no_change)
        + f", query executions in the no-change run: {executions}"
    )


def _memo_line(repeat: int, path: str) -> str:
    # With the limit at 0 the memo stores nothing, so every warm read of a
    # database built here derives the resource's identity again.
    with mock.patch.object(_runtime, "_RESOURCE_IDENTITY_MEMO_LIMIT", 0):
        without_memo = Database()
        without_memo.get(_file_text, path)
        off = best_ns(lambda: without_memo.get(_file_text, path), repeat)
    with_memo = Database()
    with_memo.get(_file_text, path)
    on = best_ns(lambda: with_memo.get(_file_text, path), repeat)
    return _line("memo-off-read/memo-on-read", off, on)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=_DEFAULT_ROOT,
        help="workspace that line 4 analyzes (default: src/pyinc_tools)",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=_DEFAULT_REPEAT,
        help=f"timed loops per measurement, the fastest counts (default: {_DEFAULT_REPEAT})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    root: Path = args.root
    repeat: int = args.repeat
    if repeat < 1:
        parser.error("--repeat must be at least 1")
    with tempfile.TemporaryDirectory(prefix="pyinc-overhead-") as scratch:
        path = str(Path(scratch, "read.txt"))
        Path(path).write_bytes(_FILE_TEXT.encode("utf-8"))
        print(_warm_get_line(repeat), flush=True)
        print(_tracked_read_line(repeat, path), flush=True)
        print(_annotation_line(repeat), flush=True)
        print(_analysis_line(root), flush=True)
        print(_memo_line(repeat, path), flush=True)


if __name__ == "__main__":
    main()
