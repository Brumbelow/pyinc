from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FileResource:
    encoding: str = "utf-8"

    def read(self, db: "Database", path: str | os.PathLike[str]) -> str:
        return db._read_resource(self, os.fspath(path))

    def label(self, path: str) -> str:
        return f"file[{path}]"

    def probe(self, path: str) -> tuple[str, str] | tuple[str]:
        file_path = Path(path)
        if not file_path.exists():
            return ("missing",)
        return ("present", hashlib.sha256(file_path.read_bytes()).hexdigest())

    def load(self, db: "Database", path: str) -> str:
        with db._allow_raw_open():
            return Path(path).read_text(encoding=self.encoding)


@dataclass(frozen=True)
class EnvResource:
    def read(self, db: "Database", name: str) -> str | None:
        return db._read_resource(self, name)

    def label(self, name: str) -> str:
        return f"env[{name}]"

    def probe(self, name: str) -> tuple[str | None]:
        return (os.environ.get(name),)

    def load(self, db: "Database", name: str) -> str | None:
        return os.environ.get(name)


@dataclass(frozen=True)
class DirectoryResource:
    def read(self, db: "Database", path: str | os.PathLike[str]) -> tuple[str, ...]:
        return db._read_resource(self, os.fspath(path))

    def label(self, path: str) -> str:
        return f"dir[{path}]"

    def probe(self, path: str) -> tuple[str, ...] | tuple[str]:
        dir_path = Path(path)
        if not dir_path.exists():
            return ("missing",)
        return tuple(sorted(child.name for child in dir_path.iterdir()))

    def load(self, db: "Database", path: str) -> tuple[str, ...]:
        dir_path = Path(path)
        if not dir_path.exists():
            return tuple()
        return tuple(sorted(child.name for child in dir_path.iterdir()))


from .runtime import Database  # noqa: E402
