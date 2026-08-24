"""Filesystem shapes a caller can hand the library that no read should hang on."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from _hostile_paths import (
    character_device,
    make_fifo,
    make_socket,
    posix_only,
    skip_without_posix_permissions,
    within_budget,
)

from pyinc import Database
from pyinc.integrations._resources import file_bytes, file_probe, file_read_snapshot, file_text
from pyinc.resources import BinaryFileResource, FileResource

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
