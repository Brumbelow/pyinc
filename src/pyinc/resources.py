from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from .runtime import Database

@dataclass(frozen=True)
class FileResource:
    encoding: str = "utf-8"

    def read(self, db: Database, path: str | os.PathLike[str]) -> str:
        return cast(str, db._read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"file[{path}]"

    def probe(self, path: str) -> tuple[str, str] | tuple[str]:
        file_path = Path(path)
        if not file_path.exists():
            return ("missing",)
        return ("present", hashlib.sha256(file_path.read_bytes()).hexdigest())

    def load(self, db: Database, path: str) -> str:
        with db._allow_raw_open():
            data = Path(path).read_bytes()
        return data.decode(self.encoding)

    def recompute_probe(self, path: str, loaded_value: str) -> tuple[str, str]:
        return ("present", hashlib.sha256(loaded_value.encode(self.encoding)).hexdigest())


@dataclass(frozen=True)
class FileStatSnapshot:
    exists: bool
    size: int | None
    mtime_ns: int | None


@dataclass(frozen=True)
class FileStatResource:
    def read(self, db: Database, path: str | os.PathLike[str]) -> FileStatSnapshot:
        return cast(FileStatSnapshot, db._read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"filestat[{path}]"

    def probe(self, path: str) -> tuple[bool, int | None, int | None]:
        snapshot = _stat_snapshot(path)
        return (snapshot.exists, snapshot.size, snapshot.mtime_ns)

    def load(self, db: Database, path: str) -> FileStatSnapshot:
        return _stat_snapshot(path)


@dataclass(frozen=True)
class EnvResource:
    def read(self, db: Database, name: str) -> str | None:
        return cast(str | None, db._read_resource(self, name))

    def label(self, name: str) -> str:
        return f"env[{name}]"

    def probe(self, name: str) -> tuple[str | None]:
        return (os.environ.get(name),)

    def load(self, db: Database, name: str) -> str | None:
        return os.environ.get(name)


@dataclass(frozen=True)
class DirectoryResource:
    def read(self, db: Database, path: str | os.PathLike[str]) -> tuple[str, ...]:
        return cast(tuple[str, ...], db._read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"dir[{path}]"

    def probe(self, path: str) -> tuple[str, tuple[str, ...]] | tuple[str]:
        dir_path = Path(path)
        if not dir_path.exists():
            return ("missing",)
        return ("present", tuple(sorted(child.name for child in dir_path.iterdir())))

    def load(self, db: Database, path: str) -> tuple[str, ...]:
        dir_path = Path(path)
        if not dir_path.exists():
            return tuple()
        return tuple(sorted(child.name for child in dir_path.iterdir()))


def _stat_snapshot(path: str) -> FileStatSnapshot:
    file_path = Path(path)
    try:
        metadata = file_path.stat()
    except FileNotFoundError:
        return FileStatSnapshot(exists=False, size=None, mtime_ns=None)
    return FileStatSnapshot(
        exists=True,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
    )
