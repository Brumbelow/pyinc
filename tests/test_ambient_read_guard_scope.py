"""Pins the exact boundary of the condition 2 ambient-read guard.

`docs/kernel-contract.md` condition 2 enumerates what the runtime intercepts and
limitation 1 enumerates the near neighbours it does not. Both lists are only
useful if they are true, so this module exercises each named entry point inside a
real query and asserts which side of the boundary it falls on. Widening the guard
therefore fails here first, forcing the contract to be updated with it.

The boundary is an observation about the interpreter's own implementation —
`pathlib` and `os.path` reroute their helpers across minor versions — so the
cases run on every interpreter in the support matrix rather than being assumed
from one.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

import pytest

from pyinc import Database, UntrackedReadError, query

_UNGUARDED_METADATA_READS = (
    "os.stat",
    "os.lstat",
    "os.access",
    "os.path.exists",
    "os.path.isfile",
    "os.path.getsize",
    "os.path.getmtime",
    "Path.stat",
    "Path.exists",
    "Path.is_file",
    "Path.is_dir",
    "Path.resolve",
)


def _metadata_read(reader: str, path: Path) -> bool:
    """Observe `path`'s metadata; True iff the read saw the live 5-byte file."""
    if reader == "os.stat":
        return os.stat(path).st_size == 5
    if reader == "os.lstat":
        return os.lstat(path).st_size == 5
    if reader == "os.access":
        return os.access(path, os.R_OK)
    if reader == "os.path.exists":
        return os.path.exists(path)
    if reader == "os.path.isfile":
        return os.path.isfile(path)
    if reader == "os.path.getsize":
        return os.path.getsize(path) == 5
    if reader == "os.path.getmtime":
        return isinstance(os.path.getmtime(path), float)
    if reader == "Path.stat":
        return path.stat().st_size == 5
    if reader == "Path.exists":
        return path.exists()
    if reader == "Path.is_file":
        return path.is_file()
    if reader == "Path.is_dir":
        return path.parent.is_dir()
    return path.resolve().name == "sample.txt"


@pytest.mark.parametrize("reader", _UNGUARDED_METADATA_READS)
def test_file_metadata_reads_bypass_untracked_read_guard(tmp_path: Path, reader: str) -> None:
    """Documents that `stat`-family reads are NOT intercepted (limitation 1).

    The guard sees file *contents* and directory *listings*. Asking whether a
    file exists, or how large or how recently modified it is, reaches the real
    filesystem from inside a query and records no dependency edge.
    """
    path = tmp_path / "sample.txt"
    path.write_text("hello", encoding="utf-8")

    @query(key=f"metadata-read:{reader}")
    def observe(db: Database) -> bool:
        return _metadata_read(reader, path)

    # None of these raise — they are outside the guard.
    assert Database().get(observe) is True


@pytest.mark.skipif(not os.supports_bytes_environ, reason="requires os.environb")
@pytest.mark.parametrize("reader", ["os.environb", "os.getenvb"])
def test_byte_environment_views_bypass_untracked_read_guard(
    monkeypatch: pytest.MonkeyPatch, reader: str
) -> None:
    """Documents that the byte-oriented environment is NOT intercepted.

    `os.environ` is replaced by a guarded mapping and `os.getenv` by a guarded
    function; `os.environb` and `os.getenvb` are a second, unwrapped view of the
    same process environment.
    """
    monkeypatch.setenv("PYINC_UNGUARDED_ENV", "value")

    @query(key=f"byte-env-read:{reader}")
    def read_env(db: Database) -> bytes | None:
        if reader == "os.environb":
            return os.environb[b"PYINC_UNGUARDED_ENV"]
        return os.getenvb(b"PYINC_UNGUARDED_ENV")

    assert Database().get(read_env) == b"value"


@pytest.mark.parametrize("reader", ["os.getcwd", "Path.cwd"])
def test_working_directory_reads_bypass_untracked_read_guard(reader: str) -> None:
    """Documents that the process working directory is NOT intercepted."""

    @query(key=f"cwd-read:{reader}")
    def read_cwd(db: Database) -> str:
        return os.getcwd() if reader == "os.getcwd" else str(Path.cwd())

    assert Database().get(read_cwd) == os.getcwd()


def test_stat_only_query_is_never_invalidated_by_the_file_it_stats(tmp_path: Path) -> None:
    """The user-visible consequence of the metadata gap.

    A query that stats a file rather than reading it has no recorded dependency,
    so it is reused forever while a fresh `Database` sees the new state.
    """
    path = tmp_path / "sample.txt"
    path.write_text("hello", encoding="utf-8")

    @query
    def observed_size(db: Database) -> int:
        return path.stat().st_size

    db = Database()
    assert db.get(observed_size) == 5

    path.write_text("hello, world", encoding="utf-8")

    # The stale value is served indefinitely: nothing was recorded to invalidate.
    assert db.get(observed_size) == 5
    node = db.inspect(observed_size)
    assert node.last_decision == "reused"
    assert node.dependencies == ()
    # A fresh database disagrees — from-scratch consistency does not hold here.
    assert Database().get(observed_size) == 12


def test_report_untracked_read_prevents_stat_query_memo_reuse(
    tmp_path: Path,
) -> None:
    """The declared escape hatch for the metadata gap."""
    path = tmp_path / "sample.txt"
    path.write_text("hello", encoding="utf-8")

    @query
    def declared_size(db: Database) -> int:
        db.report_untracked_read("file size observed via Path.stat")
        return path.stat().st_size

    db = Database()
    assert db.get(declared_size) == 5

    path.write_text("hello, world", encoding="utf-8")

    assert db.get(declared_size) == 12
    executions = db.statistics().query_executions
    assert db.get(declared_size) == 12
    assert db.statistics().query_executions == executions + 1
    assert db.inspect(declared_size).is_untracked


@pytest.mark.parametrize(
    "reader",
    ["builtins.open", "io.open", "os.getenv", "os.environ", "os.listdir", "os.scandir", "iterdir"],
)
def test_condition_two_entry_points_stay_guarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reader: str
) -> None:
    """The other half of the contract: everything condition 2 lists still raises."""
    monkeypatch.setenv("PYINC_GUARDED_ENV", "value")
    path = tmp_path / "sample.txt"
    path.write_text("hello", encoding="utf-8")

    @query(key=f"guarded-read:{reader}")
    def observe(db: Database) -> object:
        if reader == "builtins.open":
            with open(path, encoding="utf-8") as handle:
                return handle.read()
        if reader == "io.open":
            with io.open(path, encoding="utf-8") as handle:  # noqa: UP020
                return handle.read()
        if reader == "os.getenv":
            return os.getenv("PYINC_GUARDED_ENV")
        if reader == "os.environ":
            return os.environ["PYINC_GUARDED_ENV"]
        if reader == "os.listdir":
            return tuple(sorted(os.listdir(tmp_path)))
        if reader == "os.scandir":
            return tuple(sorted(entry.name for entry in os.scandir(tmp_path)))
        return tuple(sorted(child.name for child in tmp_path.iterdir()))

    with pytest.raises(UntrackedReadError, match="untracked"):
        Database().get(observe)


@pytest.mark.parametrize("direction", ["environ | dict", "dict | environ"])
def test_environ_union_operators_stay_guarded_inside_queries(direction: str) -> None:
    """PEP 584 unions iterate the whole environment, so they are condition 2 reads."""

    @query(key=f"environ-union-read:{direction}")
    def merge(db: Database) -> tuple[tuple[str, str], ...]:
        if direction == "environ | dict":
            merged = os.environ | {"PYINC_UNION_PROBE": "probe"}
        else:
            merged = {"PYINC_UNION_PROBE": "probe"} | os.environ
        return tuple(sorted(merged.items()))

    with pytest.raises(UntrackedReadError, match="untracked"):
        Database().get(merge)


def test_environ_raw_data_mapping_stays_hidden_inside_queries() -> None:
    """`os._Environ._data` bypasses the mapping protocol entirely, so the guard
    refuses the attribute outright instead of leaking the live environment."""

    @query(key="environ-raw-data-read")
    def peek(db: Database) -> tuple[str, ...]:
        raw = os.environ._data  # type: ignore[attr-defined]
        return tuple(sorted(str(key) for key in raw))

    with pytest.raises(AttributeError, match="_data"):
        Database().get(peek)


class _AdaptedPayload:
    def __init__(self, text: str) -> None:
        self.text = text


class _FreezeReadsFileAdapter:
    def __init__(self, side_file: str) -> None:
        self.side_file = side_file

    def freeze(self, value: _AdaptedPayload, freeze_value: Any) -> object:
        return freeze_value(Path(self.side_file).read_text(encoding="utf-8"))

    def thaw(self, snapshot: object, thaw_value: Any) -> _AdaptedPayload:
        return _AdaptedPayload(str(thaw_value(snapshot)))


class _ThawReadsFileAdapter:
    def __init__(self, side_file: str) -> None:
        self.side_file = side_file

    def freeze(self, value: _AdaptedPayload, freeze_value: Any) -> object:
        return freeze_value(value.text)

    def thaw(self, snapshot: object, thaw_value: Any) -> _AdaptedPayload:
        return _AdaptedPayload(Path(self.side_file).read_text(encoding="utf-8"))


def test_adapter_freeze_of_a_query_result_runs_under_the_guard(tmp_path: Path) -> None:
    """Freezing a result is part of the query boundary: an adapter that reads
    ambient state there smuggles it into the stored snapshot, so the condition 2
    guard has to see the read."""

    side = tmp_path / "side.txt"
    side.write_text("one", encoding="utf-8")

    @query
    def boxed(db: Database) -> _AdaptedPayload:
        return _AdaptedPayload("payload")

    db = Database(
        mode="checked",
        adapters={_AdaptedPayload: _FreezeReadsFileAdapter(str(side))},
    )
    with pytest.raises(UntrackedReadError, match="untracked"):
        db.get(boxed)


def test_adapter_thaw_of_query_arguments_runs_under_the_guard(tmp_path: Path) -> None:
    """Materializing call arguments is the thaw half of the same boundary."""

    side = tmp_path / "side.txt"
    side.write_text("one", encoding="utf-8")

    @query
    def consume(db: Database, payload: _AdaptedPayload) -> str:
        return payload.text

    db = Database(
        mode="checked",
        adapters={_AdaptedPayload: _ThawReadsFileAdapter(str(side))},
    )
    with pytest.raises(UntrackedReadError, match="untracked"):
        db.get(consume, _AdaptedPayload("payload"))
