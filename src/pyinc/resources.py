from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, TypeVar

if TYPE_CHECKING:
    import pyinc.runtime as _runtime


KeyT = TypeVar("KeyT")
ValueT = TypeVar("ValueT")
ProbeT = TypeVar("ProbeT")

# The ways a path stops naming the thing a file or listing resource reads. A
# probe has to be total -- it answers for every key it is handed -- and none of
# these is a transient failure a later read could survive: the path is absent,
# it is a directory, or something in its parent chain is a file. They answer the
# way an absent path does. Every other OSError is a genuine failure and keeps
# propagating, where the kernel's failure records handle it identically warm and
# fresh.
#
# Which error carries which of those is the platform's business, and the two
# disagree. POSIX raises IsADirectoryError for a directory opened as a file and
# NotADirectoryError for a path reached through one; Windows raises
# PermissionError for the directory and FileNotFoundError for the path under a
# file. So this tuple does not decide a permission denial on its own -- see
# `_reads_as_missing`.
_MISSING_FILE_ERRORS = (FileNotFoundError, IsADirectoryError, NotADirectoryError)


def _reads_as_missing(path: str, exc: OSError) -> bool:
    """Report whether a failed file read means the path names no readable file.

    A permission denial cannot be decided by its type. Windows raises it for a
    directory opened as a file, where POSIX raises IsADirectoryError, and it is
    also what an ACL denial on a perfectly ordinary file raises -- which must
    keep propagating into a failure record. Only the kind of the path separates
    them, so that is what is asked.

    The question races the read it is explaining. Either answer was true at some
    instant inside this call, and the probe the caller goes on to record
    observed one of them, so a race costs a re-read and never a wrong answer.
    """

    if isinstance(exc, _MISSING_FILE_ERRORS):
        return True
    return isinstance(exc, PermissionError) and os.path.isdir(path)


class Resource(Generic[KeyT, ValueT, ProbeT]):
    """A tracked external value.

    Implementations provide a cheap probe and a load operation. Resources whose
    probe and value can race should override :meth:`probe_and_load` and observe
    both from one underlying read, as all built-in resources do.

    On a warm request the kernel may answer an unchanged-probe check from
    :meth:`probe` alone and calls :meth:`probe_and_load` only when that probe
    misses or the record cannot answer, so :meth:`probe` and the probe
    component of :meth:`probe_and_load` must agree on an unchanged world. The
    kernel may spend one standalone :meth:`probe` per warm request, and a miss
    then pays the full :meth:`probe_and_load` on top, so :meth:`probe` should
    cost no more than :meth:`probe_and_load` and must answer "unchanged" only
    when it genuinely is: a probe that advances on every call defeats the
    warm-path check and turns each warm request into two reads.
    """

    def read(self, db: _runtime.Database, key: KeyT) -> ValueT:
        return db.read_resource(self, key)

    def probe(self, key: KeyT) -> ProbeT:
        raise NotImplementedError

    def load(self, db: _runtime.Database, key: KeyT) -> ValueT:
        raise NotImplementedError

    def probe_and_load(self, db: _runtime.Database, key: KeyT) -> tuple[ProbeT, ValueT]:
        return self.probe(key), self.load(db, key)

    def identity(self) -> Any:
        """Return snapshot-safe configuration that distinguishes this resource."""
        return self

    def label(self, key: KeyT) -> str:
        raise NotImplementedError


FileProbe = tuple[str, str] | tuple[str]


@dataclass(frozen=True)
class FileResource(Resource[str | os.PathLike[str], str, FileProbe]):
    encoding: str = "utf-8"

    def read(self, db: _runtime.Database, key: str | os.PathLike[str]) -> str:
        return db.read_resource(self, os.fspath(key))

    def label(self, path: str | os.PathLike[str]) -> str:
        return f"file[{os.fspath(path)}]"

    def probe(self, path: str | os.PathLike[str]) -> FileProbe:
        raw = _read_file(os.fspath(path))
        if raw is None:
            return ("missing",)
        return ("present", hashlib.sha256(raw).hexdigest())

    def load(self, db: _runtime.Database, path: str | os.PathLike[str]) -> str:
        raw = _read_file(os.fspath(path))
        if raw is None:
            raise FileNotFoundError(os.fspath(path))
        return raw.decode(self.encoding)

    def probe_and_load(
        self, db: _runtime.Database, path: str | os.PathLike[str]
    ) -> tuple[FileProbe, str]:
        raw = _read_file(os.fspath(path))
        if raw is None:
            raise FileNotFoundError(os.fspath(path))
        return ("present", hashlib.sha256(raw).hexdigest()), raw.decode(self.encoding)


@dataclass(frozen=True)
class BinaryFileResource(Resource[str | os.PathLike[str], bytes, FileProbe]):
    def read(self, db: _runtime.Database, key: str | os.PathLike[str]) -> bytes:
        return db.read_resource(self, os.fspath(key))

    def label(self, path: str | os.PathLike[str]) -> str:
        return f"binary-file[{os.fspath(path)}]"

    def probe(self, path: str | os.PathLike[str]) -> FileProbe:
        raw = _read_file(os.fspath(path))
        if raw is None:
            return ("missing",)
        return ("present", hashlib.sha256(raw).hexdigest())

    def load(self, db: _runtime.Database, path: str | os.PathLike[str]) -> bytes:
        raw = _read_file(os.fspath(path))
        if raw is None:
            raise FileNotFoundError(os.fspath(path))
        return raw

    def probe_and_load(
        self, db: _runtime.Database, path: str | os.PathLike[str]
    ) -> tuple[FileProbe, bytes]:
        raw = _read_file(os.fspath(path))
        if raw is None:
            raise FileNotFoundError(os.fspath(path))
        return ("present", hashlib.sha256(raw).hexdigest()), raw


@dataclass(frozen=True)
class FileStatSnapshot:
    exists: bool
    size: int | None
    mtime_ns: int | None


FileStatProbe = tuple[bool, int | None, int | None]


@dataclass(frozen=True)
class FileStatResource(Resource[str | os.PathLike[str], FileStatSnapshot, FileStatProbe]):
    def read(self, db: _runtime.Database, key: str | os.PathLike[str]) -> FileStatSnapshot:
        return db.read_resource(self, os.fspath(key))

    def label(self, path: str | os.PathLike[str]) -> str:
        return f"filestat[{os.fspath(path)}]"

    def probe(self, path: str | os.PathLike[str]) -> FileStatProbe:
        return _stat_probe(_stat_snapshot(os.fspath(path)))

    def load(self, db: _runtime.Database, path: str | os.PathLike[str]) -> FileStatSnapshot:
        return _stat_snapshot(os.fspath(path))

    def probe_and_load(
        self, db: _runtime.Database, path: str | os.PathLike[str]
    ) -> tuple[FileStatProbe, FileStatSnapshot]:
        snapshot = _stat_snapshot(os.fspath(path))
        return _stat_probe(snapshot), snapshot


@dataclass(frozen=True)
class EnvResource(Resource[str, str | None, tuple[str | None]]):
    def label(self, name: str) -> str:
        return f"env[{name}]"

    def probe(self, name: str) -> tuple[str | None]:
        return (os.environ.get(name),)

    def load(self, db: _runtime.Database, name: str) -> str | None:
        return os.environ.get(name)

    def probe_and_load(
        self, db: _runtime.Database, name: str
    ) -> tuple[tuple[str | None], str | None]:
        value = os.environ.get(name)
        return (value,), value


DirectoryProbe = tuple[bool, tuple[str, ...]]


@dataclass(frozen=True)
class DirectoryResource(Resource[str | os.PathLike[str], tuple[str, ...], DirectoryProbe]):
    def read(self, db: _runtime.Database, key: str | os.PathLike[str]) -> tuple[str, ...]:
        return db.read_resource(self, os.fspath(key))

    def label(self, path: str | os.PathLike[str]) -> str:
        return f"dir[{os.fspath(path)}]"

    def probe(self, path: str | os.PathLike[str]) -> DirectoryProbe:
        return _listing_probe(os.fspath(path))

    def load(self, db: _runtime.Database, path: str | os.PathLike[str]) -> tuple[str, ...]:
        return _listing_snapshot(os.fspath(path))[1]

    def probe_and_load(
        self, db: _runtime.Database, path: str | os.PathLike[str]
    ) -> tuple[DirectoryProbe, tuple[str, ...]]:
        snapshot = _listing_snapshot(os.fspath(path))
        return snapshot, snapshot[1]


def _read_file(path: str) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        if _reads_as_missing(path, exc):
            return None
        raise


def _listing_snapshot(path: str) -> DirectoryProbe:
    dir_path = Path(path)
    try:
        names = tuple(sorted(child.name for child in dir_path.iterdir()))
    except FileNotFoundError:
        return False, ()
    return True, names


# A path that is not a directory may not share the probe an absent one gets:
# reading them differs -- an absent path yields no entries, a non-directory
# raises -- so one probe for both would certify an interval a change happened
# in. "" is never a directory entry name, so it names the third state without
# widening the probe every directory record already carries.
_NOT_A_DIRECTORY_PROBE: DirectoryProbe = (False, ("",))


def _listing_probe(path: str) -> DirectoryProbe:
    """Report a path that holds no listing rather than raising for it.

    The load keeps raising: a caller reading a listing is told a file is not a
    directory, which is how a directory walk tells a module from a package. The
    probe cannot, because a probe that raises retires the record it was
    checking, and a warm database would then answer a path whose kind changed
    differently from a fresh one reading the same world.

    A path reached *through* a file is where the platforms part: POSIX raises
    NotADirectoryError and lands here, Windows reports the path absent and never
    gets this far. Both are sound, because on each the probe still matches what
    a read of that path does -- Windows reads it exactly as it reads an absent
    path, so the two may share a probe there.
    """

    try:
        return _listing_snapshot(path)
    except _MISSING_FILE_ERRORS:
        return _NOT_A_DIRECTORY_PROBE


def _stat_snapshot(path: str) -> FileStatSnapshot:
    try:
        metadata = Path(path).stat()
    except FileNotFoundError:
        return FileStatSnapshot(exists=False, size=None, mtime_ns=None)
    return FileStatSnapshot(
        exists=True,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
    )


def _stat_probe(snapshot: FileStatSnapshot) -> FileStatProbe:
    return snapshot.exists, snapshot.size, snapshot.mtime_ns
