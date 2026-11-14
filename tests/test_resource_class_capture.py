"""A query may name a resource class as it names any other class.

A body names one to build the resource it reads, to test a value against it,
or through a default argument. A method of a resource subclass names its own
class when it calls zero-argument super(), because the compiler keeps the
class in the method's `__class__` cell. The class has callable `label`,
`probe` and `load` attributes, but each is a plain function that runs on an
instance. Such a class folds as a class. Taken for a resource handle, it had
its `identity()` called unbound, and the read raised TypeError. A class whose
hooks are classmethods, staticmethods or methods of its metaclass runs them on
the class itself, so it stays a handle with the key it had.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import pyinc
from pyinc import (
    Database,
    FileResource,
    InMemoryArtifactStore,
    Resource,
    explain_query_captures,
    query,
)


@dataclass(frozen=True)
class _UpperFiles(FileResource):
    """Reads a file through `FileResource` and upper-cases the text."""

    def load(self, db: Database, path: Any) -> str:
        return super().load(db, path).upper()

    def probe_and_load(self, db: Database, path: Any) -> tuple[Any, str]:
        probe, text = super().probe_and_load(db, path)
        return probe, text.upper()


@dataclass(frozen=True)
class _NoteFiles(FileResource):
    """A `FileResource` subclass built through a classmethod."""

    @classmethod
    def utf8(cls) -> _NoteFiles:
        return cls(encoding="utf-8")


_FILES = FileResource()
_UPPER = _UpperFiles()
_OPEN_NOTES = _NoteFiles.utf8


@query(key="resource-class-built-in-place")
def _built_in_place(db: Database, path: str) -> str:
    return db.read_resource(FileResource(), path)


@query(key="resource-class-isinstance")
def _isinstance_check(db: Database, path: str) -> tuple[bool, str]:
    return isinstance(_UPPER, FileResource), _FILES.read(db, path)


@query(key="resource-class-super-in-method")
def _super_in_method(db: Database, path: str) -> str:
    return _UPPER.read(db, path)


@query(key="resource-class-through-module")
def _through_module(db: Database, path: str) -> str:
    return pyinc.FileResource(encoding="utf-8").read(db, path)


@query(key="resource-class-bound-classmethod")
def _bound_classmethod(db: Database, path: str) -> str:
    return _OPEN_NOTES().read(db, path)


@query(key="resource-class-default-argument")
def _default_argument(db: Database, path: str, kind: type[FileResource] = FileResource) -> str:
    return kind().read(db, path)


# Each query, the answer it gives for a file holding the text, and the
# capture that holds the class itself. In the other shapes the fold reaches the
# class through a resource method, a module attribute or a bound method.
_SHAPES: dict[str, tuple[Any, Callable[[str], Any], str | None]] = {
    "built-in-place": (_built_in_place, lambda text: text, "FileResource"),
    "isinstance": (_isinstance_check, lambda text: (True, text), "FileResource"),
    "super-in-method": (_super_in_method, str.upper, None),
    "through-module": (_through_module, lambda text: text, None),
    "bound-classmethod": (_bound_classmethod, lambda text: text, None),
    "default-argument": (_default_argument, lambda text: text, "default[0]"),
}


@pytest.fixture(name="notes")
def fixture_notes(tmp_path: Path) -> Path:
    target = tmp_path / "notes.txt"
    target.write_text("some body", encoding="utf-8")
    return target


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_a_query_naming_a_resource_class_follows_the_file(shape: str, notes: Path) -> None:
    target, expected, _capture = _SHAPES[shape]
    db = Database()
    for text in ("some body", "some body", "another body"):
        notes.write_text(text, encoding="utf-8")
        answer = db.get(target, str(notes))
        assert answer == expected(text)
        assert answer == Database().get(target, str(notes))


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_a_query_naming_a_resource_class_answers_after_a_checkpoint(
    shape: str, notes: Path
) -> None:
    """The first get after a load walks the captures for the queries and resources they pin.

    That walk reaches the class through a module attribute, a bound
    classmethod's `__self__` or a default, as the fold does.
    """

    target, expected, _capture = _SHAPES[shape]
    store = InMemoryArtifactStore()
    saver = Database(store=store)
    assert saver.get(target, str(notes)) == expected("some body")
    checkpoint = saver.save_checkpoint()

    loader = Database(store=store)
    loader.load_checkpoint(checkpoint)
    assert loader.get(target, str(notes)) == expected("some body")
    notes.write_text("another body", encoding="utf-8")
    assert loader.get(target, str(notes)) == expected("another body")


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_explain_reports_a_resource_class_as_a_type(shape: str) -> None:
    target, _expected, capture = _SHAPES[shape]
    captures = explain_query_captures(target)
    assert [info.name for info in captures if not info.accepted] == []
    if capture is not None:
        assert {info.name: info.kind for info in captures}[capture] == "type"


def test_the_kernel_takes_a_resource_class_for_a_class() -> None:
    db = Database()
    for resource_class in (Resource, FileResource, _UpperFiles, _NoteFiles):
        assert not db._is_resource_handle(resource_class)
    for resource in (_FILES, _UPPER, _OPEN_NOTES()):
        assert db._is_resource_handle(resource)


_SHOUT_SOURCE = """
from pathlib import Path

from pyinc import Resource, query


class Shout(Resource):
    def __init__(self, mark):
        self.mark = mark

    def identity(self):
        return ("shout", self.mark)

    def label(self, path):
        return "shout[" + path + "]"

    def probe(self, path):
        return (Path(path).read_text(encoding="utf-8"),)

    def load(self, db, path):
        return Path(path).read_text(encoding="utf-8").upper() + self.mark


@query(key="resource-class-shout")
def shout(db, path):
    return Shout("!").read(db, path)
"""


def _import_shout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, Path]:
    """Import the module above from its own file, as a caller's code is imported."""

    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    name = "pyinc_resource_class_shout"
    path = tmp_path / f"{name}.py"
    path.write_text(_SHOUT_SOURCE, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module, path


def test_a_resource_subclass_built_in_the_body_follows_its_file_and_its_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, notes: Path
) -> None:
    """The class defines its own identity(), and the body builds an instance on each read."""

    module, path = _import_shout(tmp_path, monkeypatch)
    db = Database()
    assert db.get(module.shout, str(notes)) == "SOME BODY!"
    assert db.get(module.shout, str(notes)) == Database().get(module.shout, str(notes))

    notes.write_text("another body", encoding="utf-8")
    assert db.get(module.shout, str(notes)) == "ANOTHER BODY!"
    assert db.get(module.shout, str(notes)) == Database().get(module.shout, str(notes))

    # Rewrite load and run the module again in place, as a reload does.
    path.write_text(_SHOUT_SOURCE.replace(".upper()", ".title()"), encoding="utf-8")
    assert module.__spec__ is not None and module.__spec__.loader is not None
    module.__spec__.loader.exec_module(module)
    assert db.get(module.shout, str(notes)) == "Another Body!"
    assert db.get(module.shout, str(notes)) == Database().get(module.shout, str(notes))

    store = InMemoryArtifactStore()
    saver = Database(store=store)
    assert saver.get(module.shout, str(notes)) == "Another Body!"
    loader = Database(store=store)
    loader.load_checkpoint(saver.save_checkpoint())
    assert loader.get(module.shout, str(notes)) == "Another Body!"

    assert [info.name for info in explain_query_captures(module.shout) if not info.accepted] == []


class _Table:
    """A resource handle in its own right: every hook is a classmethod."""

    rows = ("alpha", "beta")

    @classmethod
    def identity(cls) -> Any:
        return ("table", cls.rows)

    @classmethod
    def label(cls, key: int) -> str:
        return f"table[{key}]"

    @classmethod
    def probe(cls, key: int) -> tuple[str]:
        return (cls.rows[key],)

    @classmethod
    def load(cls, db: Database, key: int) -> str:
        return cls.rows[key]


class _Doubler:
    """A resource handle whose hooks are staticmethods."""

    @staticmethod
    def identity() -> Any:
        return ("doubler",)

    @staticmethod
    def label(key: int) -> str:
        return f"doubler[{key}]"

    @staticmethod
    def probe(key: int) -> tuple[int]:
        return (key,)

    @staticmethod
    def load(db: Database, key: int) -> int:
        return key * 2


class _HookMeta(type):
    """Gives each class made with it the hooks of a resource handle."""

    def identity(cls) -> Any:
        return ("meta", cls.__name__)

    def label(cls, key: int) -> str:
        return f"{cls.__name__}[{key}]"

    def probe(cls, key: int) -> tuple[int]:
        return (key,)

    def load(cls, db: Database, key: int) -> str:
        return f"{cls.__name__}:{key}"


class _MetaHandle(metaclass=_HookMeta):
    """A resource handle whose hooks are methods of its metaclass."""


@query(key="resource-class-handle-table")
def _read_table(db: Database, key: int) -> Any:
    return db.read_resource(_Table, key)


@query(key="resource-class-handle-doubler")
def _read_doubler(db: Database, key: int) -> Any:
    return db.read_resource(_Doubler, key)


@query(key="resource-class-handle-meta")
def _read_meta(db: Database, key: int) -> Any:
    return db.read_resource(_MetaHandle, key)


_HANDLES: dict[str, tuple[Any, Any, Any]] = {
    "classmethods": (_Table, _read_table, "beta"),
    "staticmethods": (_Doubler, _read_doubler, 2),
    "metaclass": (_MetaHandle, _read_meta, "_MetaHandle:1"),
}


@pytest.mark.parametrize("handle", sorted(_HANDLES))
def test_a_class_whose_hooks_run_on_the_class_is_still_a_handle(
    handle: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    resource_class, reader, expected = _HANDLES[handle]
    db = Database()
    assert db._is_resource_handle(resource_class)
    assert db.read_resource(resource_class, 1) == expected
    assert db.get(reader, 1) == expected
    assert Database().get(reader, 1) == expected
    kinds = {info.name: info.kind for info in explain_query_captures(reader)}
    assert kinds[resource_class.__name__] == "resource"

    # The key is the one a test of the three callable hooks alone gives.
    key = Database()._resource_key(resource_class, 1)
    query_digest = Database()._query_fingerprint(reader)
    monkeypatch.setattr(
        Database,
        "_is_resource_handle",
        lambda self, value: all(
            callable(getattr(value, name, None)) for name in ("label", "probe", "load")
        ),
    )
    assert Database()._resource_key(resource_class, 1) == key
    assert Database()._query_fingerprint(reader) == query_digest
