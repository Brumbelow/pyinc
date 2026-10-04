from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from ._safe_fs import (
    UnsafeFilesystemPathError,
    _read_error_means_missing,
    read_regular_file_following_links,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pyinc.runtime as _runtime

    from .value import FreezeFn, ThawFn, ValueAdapter


KeyT = TypeVar("KeyT")
ValueT = TypeVar("ValueT")
ProbeT = TypeVar("ProbeT")

# The ways a path stops naming what a file or listing resource reads. The path
# is absent, it is a directory, something in its parent chain is a file, or it
# names a pipe, a socket or a device. A probe has to be total (it answers for
# every key it is handed), and none of these is a transient failure that a
# later read could survive. They answer the way an absent path does. The last
# three kinds force one carve-out. Opening a bound socket reports an errno that
# CPython gives no subclass, so the file read names that errno explicitly as
# well as deciding by type.
#
# A permission denial is the real failure, and it keeps propagating. The
# kernel's failure records handle it identically warm and fresh. What is left
# names nothing readable, however often it is asked: a link that leads back to
# itself, or a path string holding a NUL. The file, listing and stat seams
# below refuse those by type. That is the third outcome a total probe is
# allowed. It keeps the platform's own spelling of these two cases, which
# differs by interpreter version and by platform, out of a caller's handlers.
#
# The platforms disagree on which error carries which case. POSIX raises
# IsADirectoryError for a directory opened as a file and NotADirectoryError for
# a path reached through a file. Windows raises PermissionError for the
# directory and FileNotFoundError for the path under a file. So a permission
# denial needs more than this tuple: see `_reads_as_missing`.
#
# The listing and stat probes match this tuple directly. A file read can also
# be handed a kind that no read of it can ever answer, so it asks
# `_reads_as_missing`.
_MISSING_FILE_ERRORS = (FileNotFoundError, IsADirectoryError, NotADirectoryError)


def _reads_as_missing(path: str, exc: OSError) -> bool:
    """Report whether a failed file read means the path names no readable file.

    A permission denial needs the path's kind to decide it. Windows raises
    PermissionError for a directory opened as a file, where POSIX raises
    IsADirectoryError. An ACL denial on an ordinary file raises it too, and
    that one must keep propagating into a failure record. Only the kind of
    the path separates them, so that is what is asked.

    A bound socket also needs more than a type. Opening one reports an errno
    that CPython gives no subclass, so it is named by errno beside the three
    types. A pipe and a device raise nothing to classify: the read answers
    those from the kind it observed, so only a failed open arrives here.

    The question races the read it explains. Either answer was true at some
    instant inside this call, and the probe the caller goes on to record
    observed one of them. So a race costs a re-read and never a wrong answer.

    The classification lives beside the read that raises these errors, so the
    two stay in step.
    """

    return _read_error_means_missing(path, exc)


class Resource(Generic[KeyT, ValueT, ProbeT]):
    """A tracked external value.

    Implementations provide a cheap probe and a load operation. Resources whose
    probe and value can race should override :meth:`probe_and_load` and observe
    both from one underlying read, as all built-in resources do.

    On a warm request the kernel may answer an unchanged-probe check from
    :meth:`probe` alone. It calls :meth:`probe_and_load` only when that probe
    misses or the record cannot answer. So :meth:`probe` and the probe
    component of :meth:`probe_and_load` must agree on an unchanged world.

    The kernel may spend one standalone :meth:`probe` per warm request, and a
    miss then pays the full :meth:`probe_and_load` on top. So :meth:`probe`
    should cost no more than :meth:`probe_and_load`, and it must answer
    "unchanged" only when the world is unchanged. A probe that advances on
    every call defeats the warm-path check and turns each warm request into
    two reads.
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
        """Return snapshot-safe configuration that distinguishes this resource.

        The configuration must stay fixed while the resource is in use. The
        default hands back the resource itself, so a resource that keeps
        observation state of its own redefines itself every time it is read.
        When that state is written into a list, dict or set the resource
        holds (such as a read log or a cache), the read that observes the
        change is refused. A change made anywhere else is accepted at a cost.
        A value rebound on the resource, or rebound inside another object it
        holds, makes the query re-fingerprint on every request, so it
        executes cold each time and reuses nothing. Either way, such a
        resource defines this method and returns the configuration that
        distinguishes it.
        """
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


class FileStatAdapter:
    """Rebuilds a :class:`FileStatSnapshot` at every cached value boundary.

    Without an adapter, the kernel freezes a file-stat reading field by field
    into a record and hands that record back. A caller reading one out of the
    cache then gets a mapping of the three fields, where a fresh read gave the
    dataclass. That mapping is a frozen record view in strict mode and a plain
    dict in the others. This adapter closes that gap. The stored payload is the
    positional triple ``(exists, size, mtime_ns)``, and every exposure
    rebuilds the dataclass from it.

    The payload is positional on purpose. Only a payload written inline comes
    back whole from the shared-structure encoding. The encoding holds a
    mapping, list, set or dataclass payload as a node of the shared structure,
    and the freeze refuses a value carrying one. Otherwise ``thaw`` would
    get an unresolved reference, or a container filled in an order nothing
    promises. This payload is inline all the way through because its three
    elements are scalars. Being a tuple is not enough: a tuple is inline only
    as far as its own elements, so a tuple holding a shared or cyclic
    container is refused on the same terms. This triple holds only scalars,
    which keeps this adapter correct in every mode and in every snapshot
    shape.

    It is stateless by construction: no instance attributes, no slot state, no
    captured objects. That lets the kernel treat it as fixed. The kernel
    derives its implementation digest once per process, and each checkpoint
    trust boundary reads that digest back. A caller adapter's implementation
    digest is re-derived at every checkpoint trust boundary. The kernel also
    leaves this adapter out of the request-scope configuration check, which
    exists for state this adapter does not have.
    """

    def freeze(self, value: FileStatSnapshot, freeze: FreezeFn) -> Any:
        return (value.exists, value.size, value.mtime_ns)

    def thaw(self, snapshot: Any, thaw: ThawFn) -> FileStatSnapshot:
        exists, size, mtime_ns = snapshot
        return FileStatSnapshot(exists=exists, size=size, mtime_ns=mtime_ns)


# The adapters every database carries for the kernel's own value types, as
# single fixed instances. A database's registry is these entries updated with
# the caller's. A caller who registers their own adapter for one of these types
# replaces the entry, with no collision. The replacement is a caller adapter in
# every respect, including the configuration check.
BUILTIN_ADAPTERS: Mapping[type[Any], ValueAdapter] = MappingProxyType(
    {FileStatSnapshot: FileStatAdapter()}
)


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


def _stopped_at_a_link(resolved: Path) -> bool:
    """Report a resolution that gave up: a resolved path holds no link.

    A full resolution has followed every link it met, so no component of its
    answer is one. A resolution that could not finish reports what it managed,
    and that still holds the link it stopped at. This asks about that link and
    leaves the failure that stopped the resolution unnamed.

    ``os.path.islink`` answers False for a path it cannot read, without
    raising, so asking it keeps the probe total. The one path it rejects is
    one holding a null character, and `_resolved_path` turns those away
    before anything here is asked.
    """
    return any(os.path.islink(candidate) for candidate in (resolved, *resolved.parents))


def _resolved_path(path: str) -> str | None:
    # A path that cannot resolve is answered as None, the way an unset
    # environment variable is. A NUL path and a looping path each name no
    # readable file, and a probe has to be total.
    #
    # Both are decided here by testing the path and the answer, because which
    # of them raises differs across platforms and is unsettled across
    # versions. A null character is caught before the ask, the way an action's
    # and a store's entry points test for one before they resolve. The answer,
    # however it was reached, is then checked for the links a finished
    # resolution cannot contain. One platform raises for a loop. Another joins
    # the unresolved remainder onto the link it gave up at and returns that,
    # without raising. So the value is pinned by testing the answer, which
    # every platform composes out of the same parts. Each platform spells the
    # failures differently, so enumerating them would leave it unpinned. The
    # handler below stays as the backstop for the platforms that do raise.
    if "\0" in path:
        return None
    try:
        resolved = Path(path).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    if _stopped_at_a_link(resolved):
        return None
    return str(resolved)


@dataclass(frozen=True)
class ResolvedPathResource(Resource[str | os.PathLike[str], str | None, tuple[str | None]]):
    """Symlink-aware canonicalization of one path, tracked as a dependency.

    The semantic value is the fully resolved path string, so retargeting any
    link along the chain invalidates readers. `Path.resolve` of a fully
    qualified path reaches the live filesystem untracked (kernel contract,
    limitation 1). Containment and visited-set decisions inside queries route
    through this resource instead.
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
    # A FIFO, a socket and a device file are paths a caller handed us that
    # name no readable file: reading one either never returns or fails in a
    # way re-reading cannot fix. They answer the way an absent path does,
    # identically warm and fresh and reproducible by a fresh run. That keeps
    # the probe built on this total. A symlink is followed, because a source
    # file reached through one is an ordinary source file. A link that leads
    # back to itself names no file, and neither does a path string holding a
    # NUL. Those two are refused by type, the third outcome a total probe is
    # allowed.
    try:
        return read_regular_file_following_links(Path(path))
    except UnsafeFilesystemPathError:
        # Already this library's own refusal, composed where the read decided
        # it. Re-raising it unchanged keeps the message that says why.
        raise
    except OSError as exc:
        if _reads_as_missing(path, exc):
            return None
        if isinstance(exc, PermissionError):
            # A denial on an otherwise ordinary path is a real failure, and
            # the kernel's failure records handle it identically warm and
            # fresh. It keeps propagating. This guard makes that explicit,
            # independent of the order of the arms. Which shapes read as
            # missing is a separate question with one answer, asked first, so
            # a denial counts as a denial only after that check.
            raise
        raise UnsafeFilesystemPathError(f"Path names no readable file: {path}") from exc


def _listing_snapshot(path: str) -> DirectoryProbe:
    dir_path = Path(path)
    try:
        names = tuple(sorted(child.name for child in dir_path.iterdir()))
    except FileNotFoundError:
        return False, ()
    except (IsADirectoryError, NotADirectoryError):
        # A path that is not a directory is reported as not a directory: a
        # directory walk tells a module from a package by that answer. The
        # probe built on this listing turns it into a third state of its own,
        # apart from absent.
        raise
    except PermissionError:
        # A denial on an otherwise ordinary path is a real failure, and the
        # kernel's failure records handle it identically warm and fresh. It
        # keeps propagating.
        raise
    except (OSError, ValueError) as error:
        # What is left names no directory, however often it is asked: a link
        # that leads back to itself, or a path string holding a NUL. The
        # platform raises for these out of the listing call in a spelling of
        # its own. Refused by type, so the answer is the library's.
        raise UnsafeFilesystemPathError(f"Path names no readable directory: {path}") from error
    return True, names


# A path that is not a directory needs a probe apart from an absent path's.
# Reading them differs: an absent path yields no entries, and a non-directory
# raises. One probe for both would certify an interval a change happened in.
# "" is never a directory entry name, so it names the third state without
# widening the probe every directory record already carries.
_NOT_A_DIRECTORY_PROBE: DirectoryProbe = (False, ("",))


def _listing_probe(path: str) -> DirectoryProbe:
    """Report a path whose kind holds no listing, without raising for it.

    The load keeps raising: a caller reading a listing is told a file is not a
    directory, which is how a directory walk tells a module from a package. The
    probe has to answer instead. A probe that raises retires the record it was
    checking, and a warm database would then answer a path whose kind changed
    differently from a fresh one reading the same world.

    The same reasoning covers the two shapes this probe still raises for. A
    link that leads back to itself and a path string holding a NUL name no
    listing in any world. So retiring the record that asked about one of them
    is the right outcome. They are refused by type, so the caller is told in a
    sentence this library composed, the same on every interpreter and
    platform.

    A path reached *through* a file is where the platforms part. POSIX raises
    NotADirectoryError and lands here. Windows reports the path absent and
    never gets this far. Both are sound, because on each the probe still
    matches what a read of that path does. Windows reads it the same way as an
    absent path, so the two may share a probe there.
    """

    try:
        return _listing_snapshot(path)
    except _MISSING_FILE_ERRORS:
        return _NOT_A_DIRECTORY_PROBE


def _stat_snapshot(path: str) -> FileStatSnapshot:
    # A stat answers for directories, so of _MISSING_FILE_ERRORS only the
    # absent-path members can fire here. A PermissionError means a parent ACL
    # denial and keeps propagating into a failure record. What is left names no
    # file whose metadata any read could ever reach: a link that leads back to
    # itself, or a path string holding a NUL. It is refused by type, so the
    # caller sees the library's error in place of the platform's own spelling.
    try:
        metadata = Path(path).stat()
    except _MISSING_FILE_ERRORS:
        return FileStatSnapshot(exists=False, size=None, mtime_ns=None)
    except PermissionError:
        # A denial is a real failure and keeps propagating. The refusal arm
        # below must never take it.
        raise
    except (OSError, ValueError) as error:
        raise UnsafeFilesystemPathError(f"Path names no readable file: {path}") from error
    return FileStatSnapshot(
        exists=True,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
    )


def _stat_probe(snapshot: FileStatSnapshot) -> FileStatProbe:
    return snapshot.exists, snapshot.size, snapshot.mtime_ns
