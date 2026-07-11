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


class Resource(Generic[KeyT, ValueT, ProbeT]):
    """A tracked external value.

    Implementations provide a cheap probe and a load operation. Resources whose
    probe and value can race should override :meth:`probe_and_load` and observe
    both from one underlying read, as all built-in resources do.
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
        return _listing_snapshot(os.fspath(path))

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
    except FileNotFoundError:
        return None


def _listing_snapshot(path: str) -> DirectoryProbe:
    dir_path = Path(path)
    try:
        names = tuple(sorted(child.name for child in dir_path.iterdir()))
    except FileNotFoundError:
        return False, ()
    return True, names


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
