"""Filesystem shapes a caller can hand the library that no read should hang on."""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

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
from pyinc.errors import PyIncError
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
    # A pipe with no writer never delivers a byte, so the read answers from
    # the kind of the path instead of waiting for one.
    pipe = make_fifo(tmp_path / "pipe.py")
    assert within_budget(lambda: FileResource().probe(str(pipe))) == "returned"
    assert FileResource().probe(str(pipe)) == ("missing",)


#: Every public entry point built on the shared file read, as (name, call)
#: pairs. The three reading methods appear for both resource types because
#: each reads on its own rather than delegating. ``read`` is not among them:
#: it hands the key to the database, so exercising it means driving a real
#: request rather than calling the shared read.
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

#: The seams that report a path naming no readable file by raising rather than
#: by answering: a load and an atomic read have a value to hand back or nothing
#: at all, so they raise exactly the way a read of an absent path does.
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
    # three kinds of path a read can be handed that never returns or never can
    # succeed. Every seam runs here under a hard budget in a child of its own,
    # so a seam that goes back to waiting fails the run loudly instead of
    # hanging it.
    call = _seam(Database(), seam_name)
    expected = "raised: FileNotFoundError" if seam_name in _MISSING_RAISES else "returned"
    assert within_budget(lambda: call(hostile_source)) == expected


@posix_only
@pytest.mark.parametrize("seam_name", _SEAM_NAMES)
def test_a_hostile_source_kind_reads_as_missing(seam_name: str, hostile_source: str) -> None:
    # Bounded is not enough on its own: the answer has to be the one an absent
    # path gets, so a warm request and a fresh one agree about a path of this
    # kind and a run that meets one stays reproducible.
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
    # A read that guarded against links rather than against waiting would pass
    # every hostile-kind cell above and still refuse the ordinary case: a
    # repository whose sources sit behind a link, or an environment whose
    # installed packages do. A source reached through one link or two must
    # answer exactly what the file itself answers.
    call = _seam(Database(), seam_name)
    expected = _present_answers(_SOURCE_TEXT.encode("utf-8"), _SOURCE_TEXT)[seam_name]
    assert call(unchanged_source) == expected


@posix_only
@pytest.mark.parametrize("seam_name", _SEAM_NAMES)
def test_a_denied_regular_source_still_fails_the_read(tmp_path: Path, seam_name: str) -> None:
    # The other half of the policy: only a kind that can never be read answers
    # absent. A denial on an ordinary regular file is a genuine failure, and
    # every seam keeps propagating it into the failure record. The message is
    # the platform's, so only the type is asserted.
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
#: hands back a value or it hands back nothing at all, so it refuses the way a
#: read of an absent path does; naming that outcome gives a query something to
#: return, and a checkpoint something to carry.
_MISSING_READ = "missing"

#: The two file resources held as values rather than reached through their
#: classes. A query body's captures are fingerprinted, and a resource is
#: fingerprinted by the configuration that distinguishes it -- which an
#: instance has and a class does not.
_TEXT_FILE = FileResource()
_BYTE_FILE = BinaryFileResource()


def _tracked_reads(db: Database, path: str) -> tuple[str, str]:
    """Both tracked read entry points on one key, as a value a query returns.

    ``read`` is the entry point the seam table above leaves out, because it
    hands the key to the database rather than calling the shared read. It is
    driven here instead, through a real request, for the text resource and the
    byte one, so the two agree about a key and a caller can tell which of them
    stopped agreeing.
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
    # never returns is not one caller's problem -- it is every caller's.
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
    # under the forked budget first, where waiting is reported rather than
    # inherited, and nothing is started until they have answered.
    assert within_budget(lambda: _tracked_reads(Database(), pipe_path)) == "returned"

    db = Database()
    db.set(unrelated, "alpha")

    answers: dict[str, object] = {}
    finished = {"pipe": threading.Event(), "unrelated": threading.Event()}

    def drive(name: str, call: Callable[[], object]) -> None:
        # Reaching the end is what this cell measures, so a refusal is recorded
        # as an outcome rather than dropped: the flag says the thread got there
        # and the recorded answer says what it got there with.
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

    # Both halves. The unrelated query answered, which is what a lock held
    # across a waiting read takes away; and the pipe query answered too, the
    # way an absent path is answered, rather than by waiting for a byte.
    assert answers["unrelated"] == "ALPHA"
    assert answers["pipe"] == (_MISSING_READ, _MISSING_READ)


@posix_only
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_pipe_source_reads_as_missing_in_every_mode(mode: str, tmp_path: Path) -> None:
    # A bounded answer that differed between a warm request and a fresh one
    # would trade a hang for a worse thing: a run whose result depends on which
    # database asked. The choice has to be the same one in every mode, warm and
    # fresh alike.
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
    # The second request was served rather than re-derived, so the equality
    # above is the warm path agreeing and not the body running twice.
    assert warm.statistics().query_executions == 1


@posix_only
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_pipe_source_survives_a_checkpoint_round_trip(mode: str, tmp_path: Path) -> None:
    # A checkpoint may only carry a probe a later process can reproduce. The
    # answer a pipe gets is reached by asking the path what kind it is, and a
    # reload asks that question again in a database that never saw the first
    # answer -- so this answer is re-derived rather than replayed, and what the
    # round trip has to show is that re-deriving it lands where the warm run
    # landed.
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

    # The sibling reads an ordinary file, so its record is one a checkpoint
    # does carry, and the reloaded database answers it without running its
    # body. That is what says the round trip below was live: written, loaded
    # and used, rather than a reload that quietly carried nothing.
    assert reloaded.get(reads_an_ordinary_source) == warm_sibling
    assert warm_sibling == (_SOURCE_TEXT, _SOURCE_TEXT)
    assert reloaded.statistics().query_executions == 0

    # The pipe query is the one that re-derives, and the reason is specific: a
    # read of a path naming no file leaves a failure record, and a checkpoint
    # carries neither a failure record nor a reader that handled one. So the
    # counter moves here and did not move for the sibling.
    reloaded_answer = reloaded.get(reads_the_source)
    assert reloaded.statistics().query_executions == 1

    fresh = Database(mode)
    fresh.set(source, str(pipe))

    assert reloaded_answer == warm_answer == fresh.get(reads_the_source)
    assert reloaded_answer == (_MISSING_READ, _MISSING_READ)


@posix_only
def test_a_pipe_that_becomes_a_regular_file_is_re_read(tmp_path: Path) -> None:
    # The other half of reading as missing: the answer describes the path as it
    # is now, not a verdict recorded against the name for good. A pipe replaced
    # by an ordinary source is an ordinary source, to a database that watched it
    # happen as much as to one that never saw the pipe.
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


#: The probes whose value domain holds no member for a path that names nothing
#: readable at all, so a typed refusal is the only total answer they can give.
#: The resolved-path probe is deliberately not among them and its absence is not
#: an inconsistency: an unresolvable path is already a member of that probe's
#: value domain, so it answers where these four refuse, and that answer is
#: pinned beside the other resolved-path cells rather than here.
_REFUSING_PROBE_NAMES: tuple[str, ...] = ("file", "binary-file", "stat", "directory")

#: The two shapes every one of those probes must refuse.
_UNREADABLE_SHAPES: tuple[str, ...] = ("symlink-loop", "embedded-null")


def _refusing_probes() -> dict[str, Callable[[str], object]]:
    """Every probe that must refuse a path naming nothing readable, by name."""
    return {
        "file": FileResource().probe,
        "binary-file": BinaryFileResource().probe,
        "stat": FileStatResource().probe,
        "directory": DirectoryResource().probe,
    }


def _unreadable_path(shape: str, base: Path) -> str:
    """A path under ``base`` in one of the two shapes nothing can read."""
    if shape == "symlink-loop":
        return str(make_symlink_loop(base / "loop"))
    return nul_path(base)


def _expected_refusal(probe_name: str, shape: str) -> str:
    """The words the refusal composes, which are always this library's own.

    A file read refuses a path holding a NUL inside the read primitive, before
    the resource seam below it is reached, so that refusal arrives in the
    primitive's sentence; the three metadata seams compose theirs. Neither
    phrase belongs to the platform on purpose: what a symlink loop and a NUL
    path draw out of the operating system is spelled differently by interpreter
    version and by platform, so no cell here pins one.
    """

    if shape == "embedded-null" and probe_name in {"file", "binary-file"}:
        return "Cannot safely open regular file"
    return "names no readable"


@posix_only
@pytest.mark.parametrize("shape", _UNREADABLE_SHAPES)
@pytest.mark.parametrize("probe_name", _REFUSING_PROBE_NAMES)
def test_a_path_that_names_nothing_readable_is_refused_by_type(
    probe_name: str, shape: str, tmp_path: Path
) -> None:
    # A link pointing at itself and a path string holding a NUL name no file, no
    # listing and no metadata. Unlike a pipe or a device they have no reading at
    # all to report, so answering "missing" would certify an interval nothing
    # observed; and unlike an absent path they never become readable by being
    # asked again. Each probe refuses them by type, which is an outcome the
    # kernel already knows what to do with, rather than by whatever the platform
    # happened to raise.
    probe = _refusing_probes()[probe_name]
    path = _unreadable_path(shape, tmp_path)
    with pytest.raises(UnsafeFilesystemPathError, match=_expected_refusal(probe_name, shape)):
        probe(path)


@posix_only
@pytest.mark.parametrize("shape", _UNREADABLE_SHAPES)
@pytest.mark.parametrize("probe_name", _REFUSING_PROBE_NAMES)
def test_a_refusal_is_caught_by_the_library_base_and_as_an_operating_system_error(
    probe_name: str, shape: str, tmp_path: Path
) -> None:
    # The refusal wears two faces on purpose. A caller guarding a query with the
    # library's own base class reaches it, and so does every handler that has
    # always wrapped a filesystem call in `except OSError` -- so routing these
    # two shapes through a typed refusal takes nothing away from either.
    probe = _refusing_probes()[probe_name]
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
    # path is a genuine failure the kernel's failure records already carry
    # identically warm and fresh, so it keeps propagating as itself instead of
    # being restated as a refusal about the path's shape.
    skip_without_posix_permissions()
    holder = tmp_path / "holder"
    holder.mkdir()
    (holder / "sub").mkdir()
    (holder / "thing.txt").write_text(_SOURCE_TEXT, encoding="utf-8")
    denied = holder / ("sub" if probe_name == "directory" else "thing.txt")
    probe = _refusing_probes()[probe_name]

    holder.chmod(0o000)
    try:
        with pytest.raises(PermissionError) as refusal:
            probe(str(denied))
        assert not isinstance(refusal.value, UnsafeFilesystemPathError)
    finally:
        holder.chmod(0o755)

    # ... and the mode was the whole of it: the same paths read normally again.
    assert probe(str(denied)) is not None
