from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from ._runtime_types import register_file_stat_boundary

if TYPE_CHECKING:
    import pyinc.runtime as _runtime


KeyT = TypeVar("KeyT")
ValueT = TypeVar("ValueT")
ProbeT = TypeVar("ProbeT")

# The ways a path stops naming the thing a file or listing resource reads. A
# probe has to be total -- it answers for every key it is handed -- and none of
# these is a transient failure a later read could survive: the path is absent,
# it is a directory, something in its parent chain is a file, its string is
# invalid, or its links cannot be followed to a target. They answer the way an
# absent path does. Every other OSError is a genuine failure and keeps
# propagating, where the kernel's failure records handle it identically warm
# and fresh.
#
# Which error carries which of those is the platform's business, and the two
# disagree. POSIX raises IsADirectoryError for a directory opened as a file and
# NotADirectoryError for a path reached through one; Windows raises
# PermissionError for the directory and FileNotFoundError for the path under a
# file. So this tuple does not decide a permission denial on its own -- see
# `_reads_as_missing`.
_MISSING_FILE_ERRORS = (FileNotFoundError, IsADirectoryError, NotADirectoryError)
_WIN_CANNOT_RESOLVE_FILENAME = 1921


def _is_symlink_loop_error(error: OSError) -> bool:
    """Recognize the native POSIX and Windows unfollowable-link errors."""

    return error.errno == errno.ELOOP or getattr(error, "winerror", None) == (
        _WIN_CANNOT_RESOLVE_FILENAME
    )


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
    kernel may spend one standalone :meth:`probe` when a warm request needs to
    verify that resource node, and a miss then pays the full
    :meth:`probe_and_load` on top, so :meth:`probe` should
    cost no more than :meth:`probe_and_load` and must answer "unchanged" only
    when it genuinely is: a probe that advances on every call defeats the
    warm-path check and turns each warm request into two reads.

    Hooks observe external state directly. They must not call a ``Database``
    read or compose an ``Input``, query, or another ``Resource``; those managed
    reads belong in the query that calls :meth:`read`. The runtime raises
    ``ResourceDependencyError`` in every execution mode when a hook violates
    that boundary, even if the hook catches the first raise itself.

    A hook may run an external command through the supported subprocess or
    ``os.spawn*`` entry points when its probe and value fully describe that
    observation. It may not launch worker threads, executors, multiprocessing
    workers, or fork the live database process. Those launches raise a sticky
    ``QueryConcurrencyError`` before the worker starts.
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


def _file_stat_public_value(value: object) -> FileStatSnapshot:
    """Rebuild the documented FileStatSnapshot after the value membrane."""
    if isinstance(value, FileStatSnapshot):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("Stored FileStatResource value is not a record.")
    if set(value) != {"exists", "size", "mtime_ns"}:
        raise TypeError("Stored FileStatResource value has invalid fields.")
    exists = value["exists"]
    size = value["size"]
    mtime_ns = value["mtime_ns"]
    if type(exists) is not bool:
        raise TypeError("Stored FileStatResource exists field is not a bool.")
    if size is not None and type(size) is not int:
        raise TypeError("Stored FileStatResource size field is not an int or None.")
    if mtime_ns is not None and type(mtime_ns) is not int:
        raise TypeError("Stored FileStatResource mtime_ns field is not an int or None.")
    return FileStatSnapshot(exists=exists, size=size, mtime_ns=mtime_ns)


register_file_stat_boundary(FileStatResource, _file_stat_public_value)


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


def _resolved_path(path: str) -> str | None:
    # `strict=False` raises for a symlink loop on older interpreters and can
    # leave the unresolved loop in place on newer ones. A probe has to be total
    # on both. The stat is only a loop check: missing paths still retain the
    # canonical spelling produced by `resolve(strict=False)`.
    try:
        candidate = Path(path)
        resolved = candidate.resolve(strict=False)
        try:
            candidate.stat()
        except (FileNotFoundError, NotADirectoryError):
            # Missing paths keep the canonical spelling from resolve(strict=False).
            pass
        except OSError as error:
            if _is_symlink_loop_error(error):
                return None
        except (RuntimeError, ValueError):
            return None
        return str(resolved)
    except (OSError, RuntimeError, ValueError):
        return None


@dataclass(frozen=True)
class ResolvedPathResource(Resource[str | os.PathLike[str], str | None, tuple[str | None]]):
    """Symlink-aware canonicalization of one path, tracked as a dependency.

    The semantic value is the fully resolved path string, so retargeting any
    link along the chain invalidates readers. `Path.resolve` reaches the live
    filesystem untracked (kernel contract, limitation 1); containment and
    visited-set decisions inside queries route through this resource instead.
    """

    def read(self, db: _runtime.Database, key: str | os.PathLike[str]) -> str | None:
        return db.read_resource(self, os.fspath(key))

    def label(self, path: str | os.PathLike[str]) -> str:
        return f"resolvedpath[{os.fspath(path)}]"

    def probe(self, path: str | os.PathLike[str]) -> tuple[str | None]:
        return (_resolved_path(os.fspath(path)),)

    def load(self, db: _runtime.Database, path: str | os.PathLike[str]) -> str | None:
        return _resolved_path(os.fspath(path))

    def probe_and_load(
        self, db: _runtime.Database, path: str | os.PathLike[str]
    ) -> tuple[tuple[str | None], str | None]:
        value = _resolved_path(os.fspath(path))
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
    """Read one regular file descriptor without ever blocking on a special file.

    The open is deliberately nonblocking and the descriptor, rather than a
    pathname observation made before it, decides whether the target is a
    regular file. That closes the stat/open race where a discovered ``.py``
    path can become a FIFO before the read. Some kernels reject opening a Unix
    socket before a descriptor exists; the post-failure ``stat`` classifies
    that special-file case without putting a blocking operation before open.

    Directories and other non-regular targets retain the built-in file
    resources' missing-file semantics. Permission and I/O failures on regular
    files continue to propagate into ordinary resource failure records.
    """

    descriptor = _open_regular_file(path)
    if descriptor is None:
        return None
    try:
        handle = os.fdopen(descriptor, "rb")
        descriptor = -1
        with handle:
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_regular_file(path: str) -> int | None:
    """Open ``path`` nonblockingly and return only a regular-file descriptor.

    The caller owns a non-negative returned descriptor. ``None`` means the path
    is absent, a directory, or another special-file kind.
    """

    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except ValueError:
        # Invalid path strings such as an embedded NUL have the same
        # conservative state as a path that names no readable regular file.
        return None
    except OSError as exc:
        if (
            _is_symlink_loop_error(exc)
            or _reads_as_missing(path, exc)
            or _failed_open_names_nonregular(path)
        ):
            return None
        raise
    try:
        is_regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
    except OSError:
        os.close(descriptor)
        raise
    if not is_regular:
        os.close(descriptor)
        return None
    return descriptor


def _failed_open_names_nonregular(path: str) -> bool:
    """Classify a special target when the kernel refused to return a handle."""

    try:
        metadata = os.stat(path)
    except (OSError, ValueError):
        return False
    return not stat.S_ISREG(metadata.st_mode)


def _listing_snapshot(path: str) -> DirectoryProbe:
    dir_path = Path(path)
    try:
        names = tuple(sorted(child.name for child in dir_path.iterdir()))
    except (FileNotFoundError, ValueError):
        return False, ()
    except OSError as error:
        if _is_symlink_loop_error(error):
            return False, ()
        raise
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
    # A stat answers for directories, so of _MISSING_FILE_ERRORS only the
    # absent-path members can fire here; a PermissionError means a parent ACL
    # denial and keeps propagating into a failure record like any other OSError.
    try:
        metadata = Path(path).stat()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError, ValueError):
        return FileStatSnapshot(exists=False, size=None, mtime_ns=None)
    except OSError as error:
        if not _is_symlink_loop_error(error):
            raise
        return FileStatSnapshot(exists=False, size=None, mtime_ns=None)
    return FileStatSnapshot(
        exists=True,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
    )


def _stat_probe(snapshot: FileStatSnapshot) -> FileStatProbe:
    return snapshot.exists, snapshot.size, snapshot.mtime_ns
