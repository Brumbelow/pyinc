"""Filesystem shapes a caller can hand the library that every read must answer promptly."""

from __future__ import annotations

import hashlib
import importlib
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from _hostile_paths import (
    BUDGET_SECONDS,
    character_device,
    make_fifo,
    make_socket,
    make_symlink_loop,
    nul_path,
    posix_only,
    skip_without_posix_permissions,
    within_budget,
)

from pyinc import Database, InMemoryArtifactStore, Input, query
from pyinc._safe_fs import UnsafeFilesystemPathError
from pyinc.errors import PyIncError, UnsupportedValueError
from pyinc.integrations._resources import file_bytes, file_probe, file_read_snapshot, file_text
from pyinc.resources import (
    BinaryFileResource,
    DirectoryResource,
    FileResource,
    FileStatResource,
)

#: What every unchanged-source cell writes and reads back.
_SOURCE_TEXT = "VALUE = 1\n"


@posix_only
def test_a_readable_source_answers_within_the_budget(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    assert within_budget(lambda: FileResource().probe(str(source))) == "returned"
    assert FileResource().probe(str(source))[0] == "present"


@posix_only
def test_a_named_pipe_source_is_answered_rather_than_waited_on(tmp_path: Path) -> None:
    # A pipe with no writer never delivers a byte, so the read answers at once
    # from the path's kind.
    pipe = make_fifo(tmp_path / "pipe.py")
    assert within_budget(lambda: FileResource().probe(str(pipe))) == "returned"
    assert FileResource().probe(str(pipe)) == ("missing",)


#: Every public entry point built on the shared file read, as (name, call)
#: pairs. The three reading methods appear for both resource types because
#: each reads for itself. ``read`` is left out. It hands the key to the
#: database, so exercising it takes a real request.
def _file_read_seams(db: Database) -> tuple[tuple[str, Callable[[str], object]], ...]:
    text = FileResource()
    raw = BinaryFileResource()
    return (
        ("FileResource.probe", text.probe),
        ("FileResource.load", lambda path: text.load(db, path)),
        ("FileResource.probe_and_load", lambda path: text.probe_and_load(db, path)),
        ("BinaryFileResource.probe", raw.probe),
        ("BinaryFileResource.load", lambda path: raw.load(db, path)),
        ("BinaryFileResource.probe_and_load", lambda path: raw.probe_and_load(db, path)),
        ("file_bytes", file_bytes),
        ("file_probe", file_probe),
        ("file_text", lambda path: file_text(path, "utf-8")),
        ("file_read_snapshot", lambda path: file_read_snapshot(path, "utf-8")),
    )


#: The seam names in table order; the parametrize id of every cell below.
_SEAM_NAMES: tuple[str, ...] = tuple(name for name, _call in _file_read_seams(Database()))

#: The seams that raise for a path naming no readable file. A load and an
#: atomic read either return a value or have nothing to return, so they raise
#: the way a read of an absent path does.
_MISSING_RAISES: frozenset[str] = frozenset(
    {
        "FileResource.load",
        "FileResource.probe_and_load",
        "BinaryFileResource.load",
        "BinaryFileResource.probe_and_load",
    }
)

#: What each answering seam hands back for a path that names no readable file.
_MISSING_ANSWERS: dict[str, object] = {
    "FileResource.probe": ("missing",),
    "BinaryFileResource.probe": ("missing",),
    "file_bytes": None,
    "file_probe": ("missing",),
    "file_text": None,
    "file_read_snapshot": (("missing",), None),
}


def _seam(db: Database, name: str) -> Callable[[str], object]:
    return dict(_file_read_seams(db))[name]


def _present_answers(raw: bytes, text: str) -> dict[str, object]:
    """What each seam hands back for a source holding ``raw``."""
    present = ("present", hashlib.sha256(raw).hexdigest())
    return {
        "FileResource.probe": present,
        "FileResource.load": text,
        "FileResource.probe_and_load": (present, text),
        "BinaryFileResource.probe": present,
        "BinaryFileResource.load": raw,
        "BinaryFileResource.probe_and_load": (present, raw),
        "file_bytes": raw,
        "file_probe": present,
        "file_text": text,
        "file_read_snapshot": (present, text),
    }


@pytest.fixture(params=("fifo", "socket", "device"))
def hostile_source(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    """A path whose kind means a read of it never returns or never can succeed."""
    kind: str = request.param
    if kind == "fifo":
        yield str(make_fifo(tmp_path / "pipe.py"))
    elif kind == "socket":
        path, server = make_socket(tmp_path / "socket.py")
        try:
            yield str(path)
        finally:
            server.close()
    else:
        yield character_device()


@pytest.fixture(params=("regular", "symlink", "symlink-to-symlink"))
def unchanged_source(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    """An ordinary source, reached directly or through one or two links."""
    shape: str = request.param
    source = tmp_path / "module.py"
    source.write_text(_SOURCE_TEXT, encoding="utf-8")
    if shape == "regular":
        return str(source)
    link = tmp_path / "link.py"
    outer = tmp_path / "link-to-link.py"
    try:
        os.symlink(source, link)
        os.symlink(link, outer)
    except (NotImplementedError, OSError):
        pytest.skip("symlink support is unavailable in this environment")
    return str(link) if shape == "symlink" else str(outer)


@posix_only
@pytest.mark.parametrize("seam_name", _SEAM_NAMES)
def test_a_hostile_source_kind_is_answered_at_every_file_read_seam(
    seam_name: str, hostile_source: str
) -> None:
    # A pipe with no writer, a bound socket and an unending device are the
    # three kinds of path whose read never returns or can never succeed. Each
    # seam runs here under a hard budget in its own child, so a seam that goes
    # back to waiting fails the run loudly before it can hang it.
    call = _seam(Database(), seam_name)
    expected = "raised: FileNotFoundError" if seam_name in _MISSING_RAISES else "returned"
    assert within_budget(lambda: call(hostile_source)) == expected


@posix_only
@pytest.mark.parametrize("seam_name", _SEAM_NAMES)
def test_a_hostile_source_kind_reads_as_missing(seam_name: str, hostile_source: str) -> None:
    # A bounded answer must also be the one an absent path gets. Then a warm
    # request and a fresh one agree about a path of this kind, and a run that
    # meets one stays reproducible.
    call = _seam(Database(), seam_name)
    if seam_name in _MISSING_RAISES:
        with pytest.raises(FileNotFoundError):
            call(hostile_source)
        return
    assert call(hostile_source) == _MISSING_ANSWERS[seam_name]


@posix_only
@pytest.mark.parametrize("seam_name", _SEAM_NAMES)
def test_ordinary_and_symlinked_sources_are_unchanged(
    seam_name: str, unchanged_source: str
) -> None:
    # A read that guarded against links, when it should guard against waiting,
    # would pass every hostile-kind cell above. It would still refuse the
    # ordinary case: a repository whose sources sit behind a link, or an
    # environment whose installed packages do. A source reached through one or two links
    # must answer what the file itself answers.
    call = _seam(Database(), seam_name)
    expected = _present_answers(_SOURCE_TEXT.encode("utf-8"), _SOURCE_TEXT)[seam_name]
    assert call(unchanged_source) == expected


@posix_only
@pytest.mark.parametrize("seam_name", _SEAM_NAMES)
def test_a_denied_regular_source_still_fails_the_read(tmp_path: Path, seam_name: str) -> None:
    # The other half of the policy: only a kind that can never be read answers
    # absent. A denial on an ordinary regular file is a real failure, and every
    # seam propagates it into the failure record. The message is the
    # platform's, so only the type is asserted.
    skip_without_posix_permissions()
    source = tmp_path / "denied.py"
    source.write_text(_SOURCE_TEXT, encoding="utf-8")
    source.chmod(0o000)
    call = _seam(Database(), seam_name)
    try:
        with pytest.raises(PermissionError):
            call(str(source))
    finally:
        source.chmod(0o644)


#: What a tracked read answers for a key that names no readable file. A read
#: either returns a value or has nothing to return, so it refuses the way a
#: read of an absent path does. Naming that outcome gives a query something to
#: return and a checkpoint something to carry.
_MISSING_READ = "missing"

#: The two file resources, held as instances. A query body's captures are
#: fingerprinted, and a resource is fingerprinted by the configuration that
#: distinguishes it, which only an instance has.
_TEXT_FILE = FileResource()
_BYTE_FILE = BinaryFileResource()


def _tracked_reads(db: Database, path: str) -> tuple[str, str]:
    """Both tracked read entry points on one key, as a value a query returns.

    ``read`` is the entry point the seam table above leaves out, because it
    hands the key to the database. It is driven here through a real request,
    for both the text and the byte resource, so the two agree about a key and a
    caller can tell which one stopped agreeing.
    """

    try:
        text = _TEXT_FILE.read(db, path)
    except FileNotFoundError:
        text = _MISSING_READ
    try:
        raw = _BYTE_FILE.read(db, path).decode("utf-8")
    except FileNotFoundError:
        raw = _MISSING_READ
    return (text, raw)


@posix_only
def test_an_unrelated_query_still_answers_while_a_pipe_is_being_read(tmp_path: Path) -> None:
    # The database holds one lock across a resource read, so a read that
    # never returns blocks every caller.
    pipe = make_fifo(tmp_path / "pipe.py")
    pipe_path = str(pipe)
    unrelated = Input[str]("hostile.paths.escalation.unrelated")

    @query
    def reads_the_pipe(db: Database) -> tuple[str, str]:
        return _tracked_reads(db, pipe_path)

    @query
    def reads_an_input(db: Database) -> str:
        return unrelated.read(db).upper()

    # The threads below are ordinary joinable ones, so a read that went back to
    # waiting would strand them for the rest of the run. Both tracked reads run
    # under the forked budget first, which reports a wait without passing it to
    # this process. The threads start only after those reads have answered.
    assert within_budget(lambda: _tracked_reads(Database(), pipe_path)) == "returned"

    db = Database()
    db.set(unrelated, "alpha")

    answers: dict[str, object] = {}
    finished = {"pipe": threading.Event(), "unrelated": threading.Event()}

    def drive(name: str, call: Callable[[], object]) -> None:
        # This cell measures reaching the end, so a refusal is recorded as an
        # outcome. The flag says the thread got there, and the recorded answer
        # says what it got there with.
        try:
            answers[name] = call()
        except BaseException as error:  # noqa: BLE001 - the outcome IS the result
            answers[name] = error
        finally:
            finished[name].set()

    pipe_thread = threading.Thread(target=drive, args=("pipe", lambda: db.get(reads_the_pipe)))
    other_thread = threading.Thread(
        target=drive, args=("unrelated", lambda: db.get(reads_an_input))
    )
    pipe_thread.start()
    try:
        # Long enough for the pipe request to have taken the lock it takes.
        time.sleep(0.2)
        other_thread.start()
        assert finished["unrelated"].wait(BUDGET_SECONDS)
        assert finished["pipe"].wait(BUDGET_SECONDS)
    finally:
        pipe_thread.join(BUDGET_SECONDS)
        other_thread.join(BUDGET_SECONDS)
    assert not pipe_thread.is_alive()
    assert not other_thread.is_alive()

    # Both halves. The unrelated query answered, which a lock held across a
    # waiting read would prevent. The pipe query answered too, the way an
    # absent path is answered.
    assert answers["unrelated"] == "ALPHA"
    assert answers["pipe"] == (_MISSING_READ, _MISSING_READ)


@posix_only
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_pipe_source_reads_as_missing_in_every_mode(mode: str, tmp_path: Path) -> None:
    # A bounded answer that differed between a warm request and a fresh one
    # would trade a hang for something worse: a run whose result depends on
    # which database asked. The answer must match in every mode, warm and fresh
    # alike.
    pipe = make_fifo(tmp_path / "pipe.py")
    pipe_path = str(pipe)

    @query
    def reads_the_pipe(db: Database) -> tuple[str, str]:
        return _tracked_reads(db, pipe_path)

    warm = Database(mode)
    cold_answer = warm.get(reads_the_pipe)
    warm_answer = warm.get(reads_the_pipe)
    fresh_answer = Database(mode).get(reads_the_pipe)

    assert cold_answer == warm_answer == fresh_answer == (_MISSING_READ, _MISSING_READ)
    # The second request reused the first answer, so the equality above shows
    # the warm path agreeing after one execution of the body.
    assert warm.statistics().query_executions == 1


@posix_only
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_pipe_source_survives_a_checkpoint_round_trip(mode: str, tmp_path: Path) -> None:
    # A checkpoint may carry only a probe a later process can reproduce. A
    # pipe's answer comes from asking the path what kind it is, and a reload
    # asks again in a database that never saw the first answer. So the answer
    # is re-derived after the reload, and the round trip has to show that
    # re-deriving it lands where the warm run landed.
    pipe = make_fifo(tmp_path / "pipe.py")
    ordinary = tmp_path / "module.py"
    ordinary.write_text(_SOURCE_TEXT, encoding="utf-8")
    ordinary_path = str(ordinary)
    store = InMemoryArtifactStore()
    source = Input[str]("hostile.paths.checkpoint.source")

    @query
    def reads_the_source(db: Database) -> tuple[str, str]:
        return _tracked_reads(db, source.read(db))

    @query
    def reads_an_ordinary_source(db: Database) -> tuple[str, str]:
        return _tracked_reads(db, ordinary_path)

    warm = Database(mode, store=store)
    warm.set(source, str(pipe))
    warm_answer = warm.get(reads_the_source)
    warm_sibling = warm.get(reads_an_ordinary_source)
    key = warm.save_checkpoint()

    reloaded = Database(mode, store=store)
    reloaded.set(source, str(pipe))
    reloaded.load_checkpoint(key)

    # The sibling reads an ordinary file, so a checkpoint carries its record,
    # and the reloaded database answers it without running its body. That
    # proves the round trip below was live: written, loaded and used. A reload
    # that carried nothing would fail here.
    assert reloaded.get(reads_an_ordinary_source) == warm_sibling
    assert warm_sibling == (_SOURCE_TEXT, _SOURCE_TEXT)
    assert reloaded.statistics().query_executions == 0

    # The pipe query re-derives for a specific reason. A read of a path naming
    # no file leaves a failure record, and a checkpoint carries neither failure
    # records nor readers that handled one. So the counter moves here, and it
    # stayed put for the sibling.
    reloaded_answer = reloaded.get(reads_the_source)
    assert reloaded.statistics().query_executions == 1

    fresh = Database(mode)
    fresh.set(source, str(pipe))

    assert reloaded_answer == warm_answer == fresh.get(reads_the_source)
    assert reloaded_answer == (_MISSING_READ, _MISSING_READ)


@posix_only
def test_a_pipe_that_becomes_a_regular_file_is_re_read(tmp_path: Path) -> None:
    # The other half of reading as missing: the answer describes the path as it
    # is now, and it changes when the path does. A pipe replaced by an ordinary
    # source is an ordinary source, both to a database that watched the change
    # and to one that never saw the pipe.
    source = tmp_path / "module.py"
    make_fifo(source)
    source_path = str(source)

    @query
    def reads_the_source(db: Database) -> tuple[str, str]:
        return _tracked_reads(db, source_path)

    warm = Database()
    assert warm.get(reads_the_source) == (_MISSING_READ, _MISSING_READ)

    source.unlink()
    source.write_text(_SOURCE_TEXT, encoding="utf-8")

    assert Database().get(reads_the_source) == (_SOURCE_TEXT, _SOURCE_TEXT)
    assert warm.get(reads_the_source) == (_SOURCE_TEXT, _SOURCE_TEXT)


#: The probes whose value domain has no member for a path that names nothing
#: readable, so a typed refusal is the only total answer they can give. The
#: resolved-path probe is left out on purpose. An unresolvable path is already
#: a member of that probe's value domain, so it answers a looping path and a
#: path string holding a NUL. The other resolved-path cells pin what those two
#: answers are and that every interpreter gives the same one.
_REFUSING_PROBE_NAMES: tuple[str, ...] = ("file", "binary-file", "stat", "directory")

#: The two shapes every one of those probes must refuse.
_UNREADABLE_SHAPES: tuple[str, ...] = ("symlink-loop", "embedded-null")

#: Every entry point of a resource that reaches the filesystem for itself. The
#: probe, the load and the atomic probe-and-load are three separate reads. A
#: refusal made by only one of them is one a caller can walk around by asking a
#: different way.
_ENTRY_POINTS: tuple[str, ...] = ("probe", "load", "probe_and_load")


def _refusing_seams(db: Database, probe_name: str) -> dict[str, Callable[[str], object]]:
    """One resource's three filesystem entry points, keyed by entry-point name.

    The load and the probe-and-load take the database the kernel hands them,
    so they are wrapped here as calls of one path. The probe takes the path
    alone.
    """

    files = FileResource()
    binaries = BinaryFileResource()
    stats = FileStatResource()
    listings = DirectoryResource()
    seams: dict[str, dict[str, Callable[[str], object]]] = {
        "file": {
            "probe": files.probe,
            "load": lambda path: files.load(db, path),
            "probe_and_load": lambda path: files.probe_and_load(db, path),
        },
        "binary-file": {
            "probe": binaries.probe,
            "load": lambda path: binaries.load(db, path),
            "probe_and_load": lambda path: binaries.probe_and_load(db, path),
        },
        "stat": {
            "probe": stats.probe,
            "load": lambda path: stats.load(db, path),
            "probe_and_load": lambda path: stats.probe_and_load(db, path),
        },
        "directory": {
            "probe": listings.probe,
            "load": lambda path: listings.load(db, path),
            "probe_and_load": lambda path: listings.probe_and_load(db, path),
        },
    }
    return seams[probe_name]


def _unreadable_path(shape: str, base: Path) -> str:
    """A path under ``base`` in one of the two unreadable shapes."""
    if shape == "symlink-loop":
        return str(make_symlink_loop(base / "loop"))
    return nul_path(base)


def _expected_refusal(probe_name: str, shape: str) -> str:
    """The words the refusal composes, which are always this library's own.

    A file read refuses a path holding a NUL inside the read primitive it
    calls, so that one refusal reaches a caller in the primitive's sentence.
    Every other cell here meets a sentence the file, listing or stat seam
    composed itself. Both phrases are the library's on purpose. The operating
    system's message for a symlink loop or a NUL path varies by interpreter
    version and platform, so the cells here pin only the library's words.
    """

    if shape == "embedded-null" and probe_name in {"file", "binary-file"}:
        return "Cannot safely open regular file"
    return "names no readable"


@posix_only
@pytest.mark.parametrize("entry_point", _ENTRY_POINTS)
@pytest.mark.parametrize("shape", _UNREADABLE_SHAPES)
@pytest.mark.parametrize("probe_name", _REFUSING_PROBE_NAMES)
def test_a_path_that_names_nothing_readable_is_refused_by_type(
    probe_name: str, shape: str, entry_point: str, tmp_path: Path
) -> None:
    # A link pointing at itself and a path string holding a NUL name no file, no
    # listing and no metadata. A pipe or a device has a reading to report, and
    # these have none, so answering "missing" would certify an interval nothing
    # observed. An absent path can become readable when asked again, and these
    # never do. Each resource refuses them by type, an outcome the kernel
    # already handles, in place of whatever the platform happened to raise.
    #
    # All three entry points are driven. The probe, the load and the atomic
    # probe-and-load each read for themselves, so a refusal made at only one of
    # them is one a caller reaches around without noticing.
    seam = _refusing_seams(Database(), probe_name)[entry_point]
    path = _unreadable_path(shape, tmp_path)
    with pytest.raises(UnsafeFilesystemPathError, match=_expected_refusal(probe_name, shape)):
        seam(path)


@posix_only
@pytest.mark.parametrize("shape", _UNREADABLE_SHAPES)
@pytest.mark.parametrize("probe_name", _REFUSING_PROBE_NAMES)
def test_a_refusal_is_caught_by_the_library_base_and_as_an_operating_system_error(
    probe_name: str, shape: str, tmp_path: Path
) -> None:
    # The refusal has two faces on purpose. A caller guarding a query with the
    # library's own base class catches it, and so does every handler that wraps
    # a filesystem call in `except OSError`. Routing these two shapes through a
    # typed refusal keeps both handlers working.
    probe = _refusing_seams(Database(), probe_name)["probe"]
    path = _unreadable_path(shape, tmp_path)

    reached: list[str] = []
    try:
        probe(path)
    except PyIncError as error:
        reached.append(f"PyIncError:{type(error).__name__}")
    try:
        probe(path)
    except OSError as error:
        reached.append(f"OSError:{type(error).__name__}")

    assert reached == [
        "PyIncError:UnsafeFilesystemPathError",
        "OSError:UnsafeFilesystemPathError",
    ]


@posix_only
@pytest.mark.parametrize("probe_name", _REFUSING_PROBE_NAMES)
def test_a_denied_path_still_fails_every_probe_as_a_denial(
    probe_name: str, tmp_path: Path
) -> None:
    # The other half of the policy, at all four probes: only a path that names
    # nothing readable is refused as such. A denial on an otherwise ordinary
    # path is a real failure, which the kernel's failure records already carry
    # identically warm and fresh. So it propagates as itself, a PermissionError,
    # and stays separate from the refusal about a path's shape.
    skip_without_posix_permissions()
    holder = tmp_path / "holder"
    holder.mkdir()
    (holder / "sub").mkdir()
    (holder / "thing.txt").write_text(_SOURCE_TEXT, encoding="utf-8")
    denied = holder / ("sub" if probe_name == "directory" else "thing.txt")
    probe = _refusing_seams(Database(), probe_name)["probe"]

    holder.chmod(0o000)
    try:
        with pytest.raises(PermissionError) as refusal:
            probe(str(denied))
        assert not isinstance(refusal.value, UnsafeFilesystemPathError)
    finally:
        holder.chmod(0o755)

    # The mode was the only cause: the same paths read normally again.
    assert probe(str(denied)) is not None


def _module_on_disk(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ModuleType, Path]:
    """Import a real module written under ``tmp_path``, with its file.

    A captured module is identified by the bytes at its ``__file__``, so the
    fixture has to be a module that came from a file. Only such a file can then
    be replaced by a pipe underneath the loaded module. The import is undone
    through ``monkeypatch``, so a cell that fails partway leaves no stale entry
    for the next cell to import.
    """

    source = tmp_path / f"{name}.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(name)
    del sys.modules[name]
    monkeypatch.setitem(sys.modules, name, module)
    return module, source


@posix_only
def test_hashing_a_captured_module_whose_file_is_a_pipe_does_not_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The library found this module's file itself, so it is a hostile input
    # only in a loose sense. Still, a module loaded from a file that was later
    # replaced with a pipe is a world the kernel must finish a request in. This
    # seam still refuses a file it cannot read, because a fingerprint that
    # skipped the module's own bytes would certify nothing. Only the waiting is
    # removed.
    module, source = _module_on_disk("pyinc_hostile_module_hash", tmp_path, monkeypatch)

    @query
    def reads_the_module(db: Database) -> int:
        return cast(int, module.VALUE)

    assert Database().get(reads_the_module) == 1

    source.unlink()
    make_fifo(source)

    def fingerprint_a_fresh_database() -> None:
        with pytest.raises(UnsupportedValueError, match="cannot be read safely"):
            Database()._query_fingerprint(reads_the_module)

    assert within_budget(fingerprint_a_fresh_database) == "returned"

    # Asserted again in the parent, now that the read is known to return,
    # because the type is half the claim. A read that reports "no readable
    # file" has to reach the refusal before the hash sees it. Hashing that
    # report raises a TypeError about the argument, which tells a caller
    # nothing about their module and breaks this seam's promise.
    with pytest.raises(UnsupportedValueError, match="cannot be read safely"):
        Database()._query_fingerprint(reads_the_module)


@posix_only
def test_a_module_stamp_reports_a_pipe_rather_than_waiting_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The second read of the same file, with a different answer on purpose.
    # This one is the token that gates reuse of a memoized fingerprint, so it
    # reports what it observed and lets the comparison decide. A module file
    # that has stopped being readable gives a token that fails to match. That
    # sends the request back to the identity read, which refuses it there,
    # once, as the seam that owns that refusal.
    module, _source = _module_on_disk("pyinc_hostile_module_stamp", tmp_path, monkeypatch)
    file_path = Path(cast(str, module.__file__))

    readable = Database()._module_observation_stamp(module)
    assert ("unreadable-file",) not in readable

    file_path.unlink()
    make_fifo(file_path)

    assert within_budget(lambda: Database()._module_observation_stamp(module)) == "returned"

    unreadable = Database()._module_observation_stamp(module)
    assert ("unreadable-file",) in unreadable
    assert unreadable != readable
