"""Pins the exact boundary of the condition 2 ambient-read guard.

`docs/kernel-contract.md` condition 2 lists what the runtime intercepts, and
limitation 1 lists the near neighbours outside it. Both lists are useful only
while they are true. This module exercises each named entry point inside a real
query and asserts which side of the boundary it falls on. Widening the guard
therefore fails here first, and the contract must be updated with it.

The boundary depends on the interpreter's own implementation, because
`pathlib` and `os.path` reroute their helpers across minor versions. So the
cases run on every interpreter in the support matrix.
"""

from __future__ import annotations

import importlib
import inspect
import io
import json
import ntpath
import os
import pickle
import posixpath
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

from pyinc import Database, UntrackedReadError, query
from pyinc import runtime as pyinc_runtime
from pyinc._path_identity import is_fully_qualified
from pyinc.runtime import _CWD_READ_UNUSED, _cwd_anchoring_abspath, _cwd_anchoring_realpath

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
    """Observe `path`'s metadata, returning True iff the read saw the live 5-byte file."""
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
    """Documents that `stat`-family reads bypass the guard (limitation 1).

    The guard sees file *contents* and directory *listings*. Asking whether a
    file exists, or how large or how recently modified it is, reaches the real
    filesystem from inside a query and records no dependency edge.
    """
    path = tmp_path / "sample.txt"
    path.write_text("hello", encoding="utf-8")

    @query(key=f"metadata-read:{reader}")
    def observe(db: Database) -> bool:
        return _metadata_read(reader, path)

    # These reads are outside the guard, so none of them raises.
    assert Database().get(observe) is True


def test_stat_only_query_is_never_invalidated_by_the_file_it_stats(tmp_path: Path) -> None:
    """The user-visible consequence of the metadata gap.

    A query that stats a file without reading it has no recorded dependency. It
    is reused forever, while a fresh `Database` sees the new state.
    """
    path = tmp_path / "sample.txt"
    path.write_text("hello", encoding="utf-8")

    @query
    def observed_size(db: Database) -> int:
        return path.stat().st_size

    db = Database()
    assert db.get(observed_size) == 5

    path.write_text("hello, world", encoding="utf-8")

    # The stale value is served indefinitely, since nothing was recorded to invalidate.
    assert db.get(observed_size) == 5
    node = db.inspect(observed_size)
    assert node.last_decision == "reused"
    assert node.dependencies == ()
    # A fresh database disagrees. From-scratch consistency breaks here.
    assert Database().get(observed_size) == 12


def test_report_untracked_read_stops_memo_reuse_for_a_stat_only_query(
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
    # Nothing rewrites the file between these two gets, so both stat the same
    # size. The equality comes from the reading holding still. The declaration
    # does not make a warm database agree with a fresh one. Rewrite the file in
    # between and the two sides disagree. The companion below pins that limit
    # with a reading that moves on its own.
    assert db.get(declared_size) == Database().get(declared_size)
    assert db.inspect(declared_size).is_untracked


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_report_untracked_read_leaves_a_clock_reading_unreproducible(mode: str) -> None:
    """The limit of the hatch, beside the cell that pins what the hatch buys.

    Declaring the read stops the node being reused, and the reading stays
    unreproducible. In every mode, two requests to one database disagree, and a
    warm database and a fresh one disagree.
    """

    @query(key=f"declared-clock:{mode}")
    def declared_clock(db: Database) -> int:
        db.report_untracked_read("elapsed time observed via a monotonic clock")
        return time.perf_counter_ns()

    db = Database(mode=mode)
    first = db.get(declared_clock)
    second = db.get(declared_clock)

    # Re-execution on every request is all the declaration buys, and here it is
    # what makes the two answers differ.
    assert first != second
    # The declaration leaves from-scratch consistency broken. A fresh database
    # computes an answer of its own.
    assert db.get(declared_clock) != Database(mode=mode).get(declared_clock)

    node = db.inspect(declared_clock)
    assert node.is_untracked
    assert node.last_decision == "executed"


_requires_environb = pytest.mark.skipif(
    not os.supports_bytes_environ, reason="requires os.environb"
)

_GUARDED_ENTRY_POINTS = (
    "builtins.open",
    "io.open",
    "os.getenv",
    "os.environ",
    pytest.param("os.getenvb", marks=_requires_environb),
    pytest.param("os.environb", marks=_requires_environb),
    "os.listdir",
    "os.scandir",
    "iterdir",
    "os.getcwd",
    "os.getcwdb",
    "Path.cwd",
)


def _guarded_read(reader: str, path: Path, directory: Path) -> object:
    """Perform the named condition 2 read, and let the guard decide whether it raises.

    Shared by the in-query and in-child-thread cases, so the two provably
    exercise the same entry-point list.
    """
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
    # Windows has no byte environment, and its cells are skipped there.
    if sys.platform != "win32" and reader == "os.getenvb":
        return os.getenvb(b"PYINC_GUARDED_ENV")
    if sys.platform != "win32" and reader == "os.environb":
        return os.environb[b"PYINC_GUARDED_ENV"]
    if reader == "os.getcwd":
        return os.getcwd()
    if reader == "os.getcwdb":
        return os.getcwdb()
    if reader == "Path.cwd":
        return str(Path.cwd())
    if reader == "os.listdir":
        return tuple(sorted(os.listdir(directory)))
    if reader == "os.scandir":
        return tuple(sorted(entry.name for entry in os.scandir(directory)))
    return tuple(sorted(child.name for child in directory.iterdir()))


@pytest.mark.parametrize("reader", _GUARDED_ENTRY_POINTS)
def test_condition_two_entry_points_stay_guarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reader: str
) -> None:
    """The other half of the contract: everything condition 2 lists still raises."""
    monkeypatch.setenv("PYINC_GUARDED_ENV", "value")
    path = tmp_path / "sample.txt"
    path.write_text("hello", encoding="utf-8")

    @query(key=f"guarded-read:{reader}")
    def observe(db: Database) -> object:
        return _guarded_read(reader, path, tmp_path)

    with pytest.raises(UntrackedReadError, match="untracked"):
        Database().get(observe)


@pytest.mark.parametrize("reader", ["os.getcwd", "os.getcwdb", "Path.cwd"])
def test_a_working_directory_refusal_says_how_to_pass_the_path(reader: str) -> None:
    @query(key=f"cwd-advice:{reader}")
    def read_cwd(db: Database) -> object:
        return _guarded_read(reader, Path(), Path())

    with pytest.raises(UntrackedReadError, match="Pass an absolute path as a query argument"):
        Database().get(read_cwd)


_PATH_RESOLVERS = (
    "os.path.abspath",
    "os.path.realpath",
    "os.path.realpath(bytes)",
    "Path.resolve",
    "Path.absolute",
)


def _resolve(reader: str, path: str) -> str:
    if reader == "os.path.abspath":
        return os.path.abspath(path)
    if reader == "os.path.realpath":
        return os.path.realpath(path)
    if reader == "os.path.realpath(bytes)":
        return os.fsdecode(os.path.realpath(os.fsencode(path)))
    if reader == "Path.resolve":
        return str(Path(path).resolve())
    return str(Path(path).absolute())


@pytest.mark.parametrize("reader", _PATH_RESOLVERS)
def test_resolving_a_relative_path_reads_the_working_directory(reader: str) -> None:
    """The working-directory guard reaches the helpers that anchor a relative path.

    `os.path.realpath` and `os.path.abspath` are wrapped and refuse such a path
    themselves. On every platform, `pathlib` reaches one of them or
    `os.getcwd`. Windows' `ntpath.abspath` reads the directory in C, through
    `nt._getfullpathname`, so before it had a wrapper it answered here.
    """

    @query(key=f"relative-path:{reader}")
    def resolve_relative(db: Database) -> str:
        return _resolve(reader, "relative")

    with pytest.raises(UntrackedReadError, match="untracked"):
        Database().get(resolve_relative)


@pytest.mark.parametrize("reader", _PATH_RESOLVERS)
def test_resolving_an_absolute_path_never_reads_the_working_directory(
    tmp_path: Path, reader: str
) -> None:
    """An absolute path resolves independently of the working directory, on every platform.

    Windows' `realpath` reads the working directory for an absolute path too,
    without using it, and the guard lets that read through. The cells below pin
    the rule the guard uses to let it through.
    """

    @query(key=f"absolute-path:{reader}")
    def resolve_absolute(db: Database, path: str) -> str:
        return _resolve(reader, path)

    resolved = Database().get(resolve_absolute, str(tmp_path / "sample.txt"))
    assert Path(resolved).name == "sample.txt"


@pytest.mark.parametrize(
    ("path", "read_unused"),
    [
        ("C:\\data\\sample.txt", True),
        ("C:\\data\\..\\data\\sample.txt", True),
        ("\\\\server\\share\\sample.txt", True),
        ("\\\\?\\C:\\data\\sample.txt", True),
        ("NUL", True),
        (b"C:\\data\\sample.txt", True),
        (b"nul", True),
        ("relative\\sample.txt", False),
        ("C:relative", False),
        ("\\data\\sample.txt", False),
        ("/data/sample.txt", False),
        ("/:data", False),
        ("\\:data", False),
        ("/:\\data", False),
        ("/:/data", False),
        (b"relative", False),
    ],
)
def test_the_windows_realpath_read_is_let_through_only_where_realpath_ignores_it(
    path: str | bytes, read_unused: bool
) -> None:
    """Pins, on every platform, the rule the guard's Windows `realpath` wrapper uses.

    Through Python 3.13.15 and 3.14.7, `ntpath.realpath` reads the working
    directory before it looks at its argument. The answer matters only for a
    path that is not fully qualified. A fully qualified path is drive-and-root,
    UNC, `\\\\?\\`-prefixed, or the null device. The stand-in reads the
    directory the same way. A drive-relative `C:relative` is anchored to that
    drive's working directory. A rooted `\\data` is anchored to the working
    directory's drive (`ntpath.isabs` called it absolute before 3.13). So both
    reads are used.
    """
    seen: list[tuple[bool, bool]] = []

    def reads_the_working_directory_first(target: Any, *, strict: bool = False) -> Any:
        seen.append((_CWD_READ_UNUSED.get(), strict))
        return target

    wrapped = _cwd_anchoring_realpath(reads_the_working_directory_first, ntpath)

    assert wrapped(path, strict=True) == path
    assert seen == [(read_unused, True)]
    assert _CWD_READ_UNUSED.get() is False
    assert is_fully_qualified(path, ntpath) is read_unused


@pytest.mark.parametrize("path", ["/:/data", b"/:/data"])
def test_a_slash_left_before_a_colon_still_counts_as_rooted_on_windows(
    monkeypatch: pytest.MonkeyPatch, path: str | bytes
) -> None:
    """Windows 3.11 and 3.12 normalise in C and keep a leading slash before a colon.

    There `normpath("/:/data")` is `"/:\\data"`, a path rooted on the working
    directory's drive. The stand-in returns that shape on any platform.
    """
    real_normpath = ntpath.normpath

    def c_normpath(value: str | bytes) -> str | bytes:
        result = real_normpath(value)
        if isinstance(value, bytes):
            assert isinstance(result, bytes)
            return b"/" + result[1:] if value.startswith(b"/:") else result
        assert isinstance(result, str)
        return "/" + result[1:] if value.startswith("/:") else result

    monkeypatch.setattr(ntpath, "normpath", c_normpath)
    assert ntpath.normpath(path)[:1] in ("/", b"/")
    assert is_fully_qualified(path, ntpath) is False


@pytest.mark.parametrize(
    ("path", "qualified"),
    [("/data/sample.txt", True), ("/", True), (b"/data", True), ("relative", False), ("", False)],
)
def test_a_posix_path_is_fully_qualified_when_it_is_absolute(
    path: str | bytes, qualified: bool
) -> None:
    assert is_fully_qualified(path, posixpath) is qualified


def test_the_read_is_let_through_on_windows_only_and_never_around_caller_code() -> None:
    """Only realpath's own read of the working directory may run unguarded.

    POSIX's realpath never reads the directory for a fully qualified path, so
    it has nothing to let through. On Windows, a `strict` with a `__bool__` of
    its own is settled before the read is let through.
    """
    windows_reads: list[bool] = []
    posix_reads: list[bool] = []
    settled_at: list[bool] = []

    class Strict:
        def __bool__(self) -> bool:
            settled_at.append(_CWD_READ_UNUSED.get())
            return True

    def windows_realpath(target: Any, *, strict: bool = False) -> Any:
        windows_reads.append(_CWD_READ_UNUSED.get())
        assert strict is True
        return target

    def posix_realpath(filename: Any, *, strict: bool = False) -> Any:
        posix_reads.append(_CWD_READ_UNUSED.get())
        return filename

    _cwd_anchoring_realpath(windows_realpath, ntpath)("C:\\data", strict=Strict())
    _cwd_anchoring_realpath(posix_realpath, posixpath)("/data", strict=True)

    assert settled_at == [False]
    assert windows_reads == [True]
    assert posix_reads == [False]


def test_the_realpath_wrapper_refuses_a_relative_path_it_cannot_see_anchored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The wrapper refuses an anchored path itself, however realpath reaches the directory.

    From Python 3.14.8, `ntpath.realpath` anchors a relative path through
    `abspath`, which reads the working directory in C. That leaves no
    `os.getcwd` call for the guard to refuse. The stand-in never reads the
    directory at all, so only the wrapper can refuse here. An absolute path
    still answers.
    """
    # Installs the guard around the real realpath before the stand-in replaces it.
    Database()
    monkeypatch.setattr(
        os.path, "realpath", _cwd_anchoring_realpath(lambda target, **_: target, os.path)
    )

    @query(key="realpath-anchored-out-of-sight")
    def resolve(db: Database, path: str) -> str:
        return os.fspath(os.path.realpath(path))

    with pytest.raises(UntrackedReadError, match="relative path inside a query is untracked"):
        Database().get(resolve, "relative")
    absolute = str(tmp_path / "sample.txt")
    assert Database().get(resolve, absolute) == absolute


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need a privilege on Windows")
@pytest.mark.parametrize("reader", ["os.path.realpath", "Path.resolve"])
def test_a_relative_path_through_an_absolute_link_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reader: str
) -> None:
    """Which directory `link` is found in is the working directory's call.

    Before 3.13, `posixpath.realpath` resolves `link/sample.txt` without calling
    `os.getcwd` when `link` points somewhere absolute, so only the wrapper sees
    that the answer depends on the working directory.
    """
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "link").symlink_to(target)
    monkeypatch.chdir(tmp_path)

    @query(key=f"relative-through-link:{reader}")
    def resolve_through_link(db: Database) -> str:
        if reader == "os.path.realpath":
            return os.path.realpath("link/sample.txt")
        return str(Path("link/sample.txt").resolve())

    with pytest.raises(UntrackedReadError, match="untracked"):
        Database().get(resolve_through_link)


def test_the_wrapped_realpath_still_pickles_and_keeps_its_signature() -> None:
    """`os.path.realpath` is replaced process-wide, so it must still pickle and keep its signature.

    A process pool pickles a function it is handed by reference, and feature
    detection reads its signature.
    """
    Database()  # installs the guard

    assert pickle.loads(pickle.dumps(os.path.realpath)) is os.path.realpath
    parameters = inspect.signature(os.path.realpath).parameters
    assert "strict" in parameters
    # Its first parameter is still accepted by the name it advertises
    # (`filename` on POSIX, `path` on Windows).
    first = next(iter(parameters))
    assert os.path.realpath(**{first: os.path.abspath(os.sep)}) == os.path.realpath(os.sep)


_PICKLE_EVERY_GUARDED_NAME = """
import builtins, io, json, os, pickle, sys, threading
from pathlib import Path

NAMES = {
    "builtins.open": lambda: builtins.open,
    "io.open": lambda: io.open,
    "os.getenv": lambda: os.getenv,
    "os.listdir": lambda: os.listdir,
    "os.scandir": lambda: os.scandir,
    "os.getcwd": lambda: os.getcwd,
    "os.getcwdb": lambda: os.getcwdb,
    "Path.iterdir": lambda: Path.iterdir,
    "Path.cwd": lambda: Path.cwd,
    "os.path.realpath": lambda: os.path.realpath,
    "os.path.abspath": lambda: os.path.abspath,
    "threading.Thread.start": lambda: threading.Thread.start,
}
if sys.platform != "win32":
    NAMES["os.getenvb"] = lambda: os.getenvb


def round_trips():
    result = {}
    for name, read in NAMES.items():
        value = read()
        try:
            back = pickle.loads(pickle.dumps(value))
        except Exception as exc:
            result[name] = type(exc).__name__
        else:
            result[name] = back is value or back == value
    return result


before = round_trips()
from pyinc import Database

Database()
after = round_trips()
print("JSON " + json.dumps({"before": before, "after": after}))
"""


def test_every_guarded_name_still_pickles_by_reference(tmp_path: Path) -> None:
    """Each wrapper the guard installs pickles as the callable it replaced did.

    A process pool pickles a function by reference, so `submit(os.getcwd)` and
    friends must keep working once a `Database` exists. A fresh process is the
    only place where the guard is not installed yet.
    """
    script = tmp_path / "pickle_every_guarded_name.py"
    script.write_text(_PICKLE_EVERY_GUARDED_NAME, encoding="utf-8")
    src = str(Path(pyinc_runtime.__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": src, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, env=env, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("JSON ")][-1]
    out = json.loads(line[len("JSON ") :])
    assert all(value is True for value in out["before"].values()), out["before"]
    assert out["after"] == out["before"]


def _anchor(reader: str, path: str, start: str) -> str:
    """Call the named helper that anchors through `os.path.abspath`."""
    if reader == "os.path.abspath":
        return os.path.abspath(path)
    if reader == "os.path.abspath(path=)":
        return os.path.abspath(path=path)
    if reader == "os.path.abspath(bytes)":
        return os.fsdecode(os.path.abspath(os.fsencode(path)))
    if reader == "os.path.relpath(path)":
        return os.path.relpath(path, start)
    if reader == "os.path.relpath(start)":
        return os.path.relpath(start, path)
    return os.path.relpath(path)


@pytest.mark.parametrize(
    "reader",
    [
        "os.path.abspath",
        "os.path.abspath(path=)",
        "os.path.abspath(bytes)",
        "os.path.relpath(path)",
        "os.path.relpath(start)",
        "os.path.relpath(default start)",
    ],
)
def test_a_relative_abspath_is_refused_by_name_on_every_platform(
    tmp_path: Path, reader: str
) -> None:
    """`os.path.abspath` refuses a path the working directory anchors, wherever it runs.

    POSIX's `abspath` anchored such a path with `os.getcwd` and was refused in
    that function's name. Windows' `abspath` read the directory through
    `nt._getfullpathname` and answered. So did `relpath`, which anchors both
    of its arguments with `abspath` (its default start is the working directory
    itself). The wrapper refuses in `abspath`'s own name, for a keyword
    argument and bytes alike.
    """

    @query(key=f"relative-abspath:{reader}")
    def anchor(db: Database, start: str) -> str:
        return _anchor(reader, "relative", start)

    with pytest.raises(
        UntrackedReadError, match=r"os\.path\.abspath\(\) of a relative path .* Pass an absolute"
    ):
        Database().get(anchor, str(tmp_path))


@pytest.mark.parametrize(
    "reader",
    [
        "os.path.abspath",
        "os.path.abspath(path=)",
        "os.path.abspath(bytes)",
        "os.path.relpath(path)",
        "os.path.relpath(start)",
    ],
)
def test_a_fully_qualified_abspath_still_answers(tmp_path: Path, reader: str) -> None:
    """A fully qualified path never consults the working directory, so it resolves."""
    path = str(tmp_path / "sample.txt")

    @query(key=f"qualified-abspath:{reader}")
    def anchor(db: Database, path: str, start: str) -> str:
        return _anchor(reader, path, start)

    assert Database().get(anchor, path, str(tmp_path)) == _anchor(reader, path, str(tmp_path))


@pytest.mark.parametrize(
    ("path", "qualified"),
    [
        ("C:\\data\\sample.txt", True),
        ("C:/data/../data/sample.txt", True),
        ("\\\\server\\share\\sample.txt", True),
        ("\\\\?\\C:\\data\\sample.txt", True),
        ("NUL", True),
        (b"C:\\data\\sample.txt", True),
        ("relative\\sample.txt", False),
        ("", False),
        ("C:relative", False),
        # What `Path("C:data").absolute()` asks `abspath` for from 3.12.
        ("C:", False),
        (b"C:", False),
        ("\\data\\sample.txt", False),
        ("/data/sample.txt", False),
        ("\\:data", False),
        (b"relative", False),
    ],
)
def test_the_abspath_wrapper_refuses_by_windows_rules_on_windows(
    monkeypatch: pytest.MonkeyPatch, path: str | bytes, qualified: bool
) -> None:
    """Pins, on every platform, the rule the `abspath` wrapper applies under `ntpath`.

    Windows' `abspath` anchors a drive-relative `C:relative` to that drive's
    working directory, and a rooted `\\data` to the working directory's drive,
    so the wrapper refuses both. It lets through a drive with a root, a UNC or
    device path, and the null device. Whatever it decides, the wrapper hands
    `abspath` the one string it decided on.
    """
    refusals: list[str] = []
    seen: list[str | bytes] = []
    monkeypatch.setattr(pyinc_runtime, "_raise_if_guarded", refusals.append)

    def windows_abspath(path: Any) -> Any:
        seen.append(path)
        return path

    wrapped = _cwd_anchoring_abspath(windows_abspath, ntpath)

    assert wrapped(path) == path
    assert wrapped(path=path) == path
    assert seen == [path, path]
    assert len(refusals) == (0 if qualified else 2)
    assert all("os.path.abspath()" in message for message in refusals)


def test_the_abspath_wrapper_reads_its_argument_once_and_keeps_its_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One `__fspath__` call decides and answers, and a call `abspath` refuses still raises.

    A path `normpath` cannot read (bytes Windows cannot decode) is one
    `abspath` would go on to anchor. So it is refused inside a query, and left
    to `abspath`'s own error outside one.
    """
    refusals: list[str] = []
    monkeypatch.setattr(pyinc_runtime, "_raise_if_guarded", refusals.append)
    handed: list[Any] = []

    def recording_abspath(path: Any) -> Any:
        handed.append(path)
        return path

    class OncePath:
        calls = 0

        def __fspath__(self) -> str:
            OncePath.calls += 1
            return "C:\\data" if OncePath.calls == 1 else "relative"

    wrapped = _cwd_anchoring_abspath(recording_abspath, ntpath)
    assert wrapped(OncePath()) == "C:\\data"
    assert (OncePath.calls, handed, refusals) == (1, ["C:\\data"], [])

    with pytest.raises(TypeError, match="not NoneType"):
        wrapped(None)
    # Call shapes `abspath` refuses are handed to it whole, so its own
    # message comes back.
    with pytest.raises(TypeError, match=r"recording_abspath\(\) got an unexpected keyword"):
        wrapped(p="x")
    with pytest.raises(TypeError, match=r"recording_abspath\(\) missing 1 required"):
        wrapped()
    assert handed == ["C:\\data"]

    unreadable = types.ModuleType("unreadable_paths")

    def normpath(path: Any) -> Any:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    unreadable.normpath = normpath  # type: ignore[attr-defined]
    assert _cwd_anchoring_abspath(recording_abspath, unreadable)(b"\xff") == b"\xff"
    assert len(refusals) == 1


def test_the_wrapped_abspath_still_pickles_and_keeps_its_signature(tmp_path: Path) -> None:
    """`os.path.abspath` is replaced for the whole process, so outside a query it is unchanged.

    It still pickles by reference, advertises and accepts its own parameter
    name, and anchors a relative path where no query runs. It raises a
    TypeError wherever the original does. For a call it refuses by shape, that
    error is the original's own, word for word. A bad argument is refused by
    `os.fspath`, whose message Windows' 3.14 `abspath` words differently, from
    its C `normpath`.
    """
    Database()  # installs the guard
    wrapped: Any = os.path.abspath
    original: Any = getattr(wrapped, "__wrapped__", wrapped)

    assert pickle.loads(pickle.dumps(os.path.abspath)) is os.path.abspath
    assert list(inspect.signature(os.path.abspath).parameters) == ["path"]
    assert os.path.abspath(path=str(tmp_path)) == str(tmp_path)
    assert os.path.abspath("relative") == original("relative")
    assert os.path.abspath(b"relative") == original(b"relative")
    with pytest.raises(TypeError):
        wrapped(None)
    with pytest.raises(TypeError):
        original(None)
    calls: tuple[tuple[tuple[Any, ...], dict[str, Any]], ...] = (
        ((), {}),
        (("a", "b"), {}),
        ((), {"p": "a"}),
        (("a",), {"path": "a"}),
    )
    for args, kwargs in calls:
        with pytest.raises(TypeError) as wrapped_error:
            wrapped(*args, **kwargs)
        with pytest.raises(TypeError) as original_error:
            original(*args, **kwargs)
        assert str(wrapped_error.value) == str(original_error.value)


def test_the_realpath_and_abspath_wrappers_compose(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A realpath that anchors through `abspath`, as 3.14.8's Windows one does, still works.

    The `realpath` wrapper decides first, so a relative path is refused in
    `realpath`'s name. A fully qualified one reaches `abspath`, which lets it
    through. Outside a query, both wrappers stay out of the way.
    """
    Database()  # installs the guard

    def anchors_through_abspath(path: Any, *, strict: bool = False) -> Any:
        return os.path.abspath(path)

    monkeypatch.setattr(
        os.path, "realpath", _cwd_anchoring_realpath(anchors_through_abspath, os.path)
    )

    @query(key="realpath-through-abspath")
    def resolve(db: Database, path: str) -> str:
        return os.fspath(os.path.realpath(path))

    absolute = str(tmp_path / "sample.txt")
    assert Database().get(resolve, absolute) == absolute
    with pytest.raises(UntrackedReadError, match=r"os\.path\.realpath\(\) of a relative path"):
        Database().get(resolve, "relative")
    assert os.path.realpath("relative") == os.path.join(os.getcwd(), "relative")


def test_standard_library_callers_of_abspath_still_answer_for_qualified_paths(
    tmp_path: Path,
) -> None:
    """Library code that hands `abspath` a fully qualified path still answers.

    Three callers reach a wrapper here and answer: `ismount`,
    `tempfile.mkdtemp`, and `inspect` on code whose file name is absolute.
    `ismount` goes through `realpath` on POSIX and `abspath` on Windows. From
    3.12, `tempfile.mkdtemp` returns `abspath` of what it made.
    """

    @query(key="qualified-abspath-callers")
    def callers(db: Database, directory: str) -> tuple[bool, bool, str, str]:
        made = tempfile.mkdtemp(dir=directory)
        return (
            os.path.ismount(directory),
            os.path.isabs(made),
            inspect.getabsfile(_anchor),
            os.path.basename(inspect.getsourcefile(_anchor) or ""),
        )

    assert Database().get(callers, str(tmp_path)) == (
        False,
        True,
        os.path.normcase(os.path.abspath(_anchor.__code__.co_filename)),
        os.path.basename(_anchor.__code__.co_filename),
    )


def test_inspect_on_a_generated_file_name_is_refused_on_every_platform() -> None:
    """`inspect` anchors a file name that is not fully qualified, and that reads the directory.

    Code compiled under a name such as `<generated>` names a file whose place
    the working directory decides. So `inspect.getabsfile` of it anchors the
    name with `abspath`. On POSIX that is refused through `os.getcwd`. On
    Windows, where it answered, the wrapper refuses it.
    """

    @query(key="inspect-generated-file-name")
    def locate(db: Database) -> str:
        return inspect.getabsfile(compile("pass\n", "<generated>", "exec"))

    with pytest.raises(UntrackedReadError, match="untracked"):
        Database().get(locate)


def test_the_kernel_resolves_a_captured_module_file_outside_the_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Checking a captured module's file against its import spec is the kernel's work.

    A module whose `__file__` is relative resolves it against the working
    directory. The check used to run under the calling query's guard. So a
    query that captures such a module answered from top level, and was refused
    when first asked for from inside another query.
    """
    (tmp_path / "pyinc_guard_relative_file.py").write_text("VALUE = 7\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    module = importlib.import_module("pyinc_guard_relative_file")
    monkeypatch.setattr(module, "__file__", "pyinc_guard_relative_file.py")
    try:

        @query(key="captures-relative-file-module")
        def captured(db: Database) -> int:
            return int(module.VALUE)

        @query(key="asks-from-inside")
        def outer(db: Database) -> int:
            return captured(db) + 1

        assert Database().get(outer) == 8
    finally:
        sys.modules.pop("pyinc_guard_relative_file", None)


@_requires_environb
def test_byte_environment_writes_stay_allowed_inside_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only reads are guarded, in the byte view as in `os.environ`."""
    # Set first so the monkeypatch records the variable and removes it again.
    monkeypatch.setenv("PYINC_BYTE_WRITE", "before")

    @query(key="byte-env-write")
    def write_env(db: Database) -> bool:
        if sys.platform != "win32":  # the cell is skipped there
            os.environb[b"PYINC_BYTE_WRITE"] = b"value"
        return True

    assert Database().get(write_env) is True
    assert os.environ["PYINC_BYTE_WRITE"] == "value"


def _environment_views() -> list[str]:
    return ["os.environ", "os.environb"] if sys.platform != "win32" else ["os.environ"]


@pytest.mark.parametrize("view", _environment_views())
def test_clearing_the_environment_is_a_write_inside_queries(view: str) -> None:
    """`clear()` empties the environment without reading it, so a query may call it."""
    saved = dict(os.environ)

    @query(key=f"clear-environment:{view}")
    def clear_environment(db: Database) -> bool:
        if view == "os.environ":
            os.environ.clear()
        elif sys.platform != "win32":
            os.environb.clear()
        return True

    try:
        assert Database().get(clear_environment) is True
        assert dict(os.environ) == {}
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.mark.parametrize("view", _environment_views())
@pytest.mark.parametrize("call", ["pop", "popitem", "setdefault"])
def test_environment_calls_that_return_a_value_are_refused_inside_queries(
    monkeypatch: pytest.MonkeyPatch, view: str, call: str
) -> None:
    """`pop`, `popitem` and `setdefault` return what they read, so they are reads."""
    monkeypatch.setenv("PYINC_ENV_RETURNS", "value")

    @query(key=f"environment-returns:{view}:{call}")
    def read_back(db: Database) -> object:
        mapping: Any
        key: Any
        if view == "os.environ":
            mapping, key = os.environ, "PYINC_ENV_RETURNS"
        elif sys.platform != "win32":  # the byte view is not collected on Windows
            mapping, key = os.environb, b"PYINC_ENV_RETURNS"
        else:
            return None
        if call == "pop":
            return mapping.pop(key)
        if call == "popitem":
            return mapping.popitem()
        return mapping.setdefault(key, key)

    with pytest.raises(UntrackedReadError, match="untracked"):
        Database().get(read_back)
    assert os.environ["PYINC_ENV_RETURNS"] == "value"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("reader", _GUARDED_ENTRY_POINTS)
def test_query_spawned_thread_raw_reads_stay_guarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reader: str, mode: str
) -> None:
    """A thread started inside a query body is inside the query boundary too.

    Whatever such a thread reads flows back into the query's result. A child
    that read ambient state freely would record no dependency for it, and the
    warm answer would drift from a fresh one. The whole condition 2 list must
    hold on the child, in every mode.
    """
    monkeypatch.setenv("PYINC_GUARDED_ENV", "value")
    path = tmp_path / "sample.txt"
    path.write_text("hello", encoding="utf-8")

    @query(key=f"child-thread-read:{mode}:{reader}")
    def observe_in_child(db: Database) -> str:
        # The child's outcome comes back as the query's own result. A query may
        # not capture mutable ambient state, so the test has no shared list to
        # append to from out here.
        outcome: list[str] = []

        def child() -> None:
            try:
                _guarded_read(reader, path, tmp_path)
            except Exception as exc:  # noqa: BLE001
                outcome.append(f"{type(exc).__name__}: {exc}")
            else:
                outcome.append("read allowed")

        thread = threading.Thread(target=child)
        thread.start()
        thread.join(timeout=10)
        if thread.is_alive():
            # Report a stuck child to the main thread, without blocking on it.
            return "child still running"
        return outcome[0] if outcome else "child recorded nothing"

    reported = Database(mode=mode).get(observe_in_child)
    assert reported.startswith("UntrackedReadError:"), reported
    assert "untracked" in reported


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_query_spawning_a_file_reading_thread_raises_instead_of_caching(
    tmp_path: Path, mode: str
) -> None:
    """The read a spawned thread used to make freely is refused and kept out of the store.

    A query that farmed its file read out to a thread recorded no dependency
    on that file. The answer it stored outlived every later edit, while a
    fresh database read the new bytes. Now the child is refused, and a query
    that lets the refusal out fails instead of caching a wrong answer. A query
    that handles the refusal and answers something of its own is deterministic
    again. It gives the same value warm and fresh, with no dependency on a file
    it never managed to read.
    """
    path = tmp_path / "data.txt"
    path.write_text("one", encoding="utf-8")

    @query(key=f"child-read-propagated:{mode}")
    def read_through_child(db: Database) -> str:
        box: list[object] = []

        def child() -> None:
            try:
                with open(path, encoding="utf-8") as handle:
                    box.append(handle.read())
            except Exception as exc:  # noqa: BLE001
                box.append(exc)

        thread = threading.Thread(target=child)
        thread.start()
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("child thread did not finish")
        outcome = box[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return str(outcome)

    with pytest.raises(UntrackedReadError, match="untracked"):
        Database(mode=mode).get(read_through_child)

    @query(key=f"child-read-handled:{mode}")
    def constant_despite_child(db: Database) -> str:
        box: list[str] = []

        def child() -> None:
            try:
                with open(path, encoding="utf-8") as handle:
                    box.append(f"read allowed: {handle.read()}")
            except UntrackedReadError:
                box.append("refused")

        thread = threading.Thread(target=child)
        thread.start()
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError("child thread did not finish")
        return box[0]

    warm = Database(mode=mode)
    assert warm.get(constant_despite_child) == "refused"
    assert warm.statistics().query_executions == 1

    path.write_text("two", encoding="utf-8")

    assert warm.get(constant_despite_child) == "refused"
    # The count shows the second answer is the stored one. A re-execution that
    # happened to agree would count as a second execution.
    assert warm.statistics().query_executions == 1

    fresh = Database(mode=mode)
    assert fresh.get(constant_despite_child) == warm.get(constant_despite_child)
    assert fresh.statistics().query_executions == 1


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
    """The guard refuses `os._Environ._data` outright inside a query.

    The attribute bypasses the mapping protocol entirely, so reading it would
    leak the live environment.
    """

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
    """Freezing a result is part of the query boundary.

    An adapter that reads ambient state there smuggles it into the stored
    snapshot, so the condition 2 guard must see the read.
    """

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
