from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from pyinc import (
    BinaryFileResource,
    Database,
    DirectoryResource,
    EnvResource,
    FileResource,
    FileStatResource,
    Input,
    Query,
    query,
)
from pyinc.errors import InputKeyError
from pyinc.resources import Resource
from pyinc.runtime import _default_observer_error_hook, _GuardedEnviron


@dataclass(frozen=True)
class _EchoResource(Resource[str, str, str]):
    prefix: str = "value"

    def probe(self, key: str) -> str:
        return f"probe:{key}"

    def load(self, db: Database, key: str) -> str:
        return f"{self.prefix}:{key}"

    def label(self, key: str) -> str:
        return f"echo[{key}]"


@dataclass(frozen=True)
class _FailingResource(Resource[str, str, str]):
    def probe(self, key: str) -> str:
        return key

    def load(self, db: Database, key: str) -> str:
        raise RuntimeError(f"cannot load {key}")

    def label(self, key: str) -> str:
        return f"failing[{key}]"


@pytest.mark.parametrize("key", ["", 0, None])
def test_input_rejects_invalid_keys(key: object) -> None:
    with pytest.raises(InputKeyError, match="non-empty string"):
        Input[object](cast(Any, key))


def test_input_rejects_conflicting_and_noncallable_policies() -> None:
    with pytest.raises(ValueError, match="either eq= or cutoff="):
        Input[int]("value", eq=lambda left, right: left == right, cutoff=abs)
    with pytest.raises(TypeError, match="eq= must be callable"):
        Input[int]("value", eq=cast(Any, 1))
    with pytest.raises(TypeError, match="cutoff= must be callable"):
        Input[int]("value", cutoff=cast(Any, 1))


def test_input_read_delegates_to_database_and_reports_missing_values() -> None:
    value = Input[int]("edge-input")
    db = Database()
    with pytest.raises(KeyError, match="has not been set"):
        value.read(db)
    with pytest.raises(TypeError, match="expects an Input"):
        db.read_input(cast(Any, "edge-input"))

    db.set(value, 7)
    assert value.read(db) == 7


def test_query_rejects_nonfunctions_and_async_or_generator_functions() -> None:
    with pytest.raises(TypeError, match="Python functions only"):
        Query(cast(Any, object()))

    async def coroutine(db: Database) -> int:
        return 1

    def generator(db: Database) -> Any:
        yield 1

    async def async_generator(db: Database) -> Any:
        yield 1

    for invalid in (coroutine, generator, async_generator):
        with pytest.raises(TypeError, match="synchronous, non-generator"):
            Query(cast(Any, invalid))


def test_query_rejects_invalid_keys_and_policies() -> None:
    def calculate(db: Database) -> int:
        return 1

    with pytest.raises(ValueError, match="either eq= or cutoff="):
        Query(calculate, eq=lambda left, right: left == right, cutoff=abs)
    with pytest.raises(TypeError, match="eq= must be callable"):
        Query(calculate, eq=cast(Any, 1))
    with pytest.raises(TypeError, match="cutoff= must be callable"):
        Query(calculate, cutoff=cast(Any, 1))
    for key in ("", 0):
        with pytest.raises(ValueError, match="non-empty string"):
            Query(calculate, key=cast(Any, key))


def test_query_comparison_supports_default_custom_and_cutoff_policies() -> None:
    def calculate(db: Database) -> object:
        return None

    default = Query(calculate)
    assert default.compare([1, 2], [1, 2])
    assert not default.compare([1, 2], [2, 1])

    custom = Query(calculate, eq=lambda left, right: len(left) == len(right))
    assert custom.compare([1], [2])
    assert not custom.compare([1], [2, 3])

    cutoff = Query(calculate, cutoff=lambda value: value["stable"])
    assert cutoff.compare({"stable": 1, "noise": 2}, {"stable": 1, "noise": 3})
    assert not cutoff.compare({"stable": 1}, {"stable": 2})


def test_query_decorator_forms_and_query_call_delegate_to_database() -> None:
    source = Input[int]("decorator-source")

    @query
    def direct(db: Database) -> int:
        return source.read(db)

    @query(key="tests:configured-query")
    def configured(db: Database) -> int:
        return direct(db) + 1

    db = Database()
    db.set(source, 5)
    assert direct(db) == 5
    assert configured(db) == 6
    assert configured.key == "tests:configured-query"


def test_base_resource_contract_and_default_probe_and_load() -> None:
    resource = _EchoResource(prefix="answer")
    db = Database()
    assert resource.identity() is resource
    assert resource.probe_and_load(db, "x") == ("probe:x", "answer:x")
    assert resource.read(db, "x") == "answer:x"

    abstract: Resource[str, str, str] = Resource()
    with pytest.raises(NotImplementedError):
        abstract.probe("x")
    with pytest.raises(NotImplementedError):
        abstract.load(db, "x")
    with pytest.raises(NotImplementedError):
        abstract.label("x")


def test_failed_resource_read_does_not_leave_a_stale_runtime_binding() -> None:
    db = Database()
    resource = _FailingResource()
    with pytest.raises(RuntimeError, match="cannot load x"):
        resource.read(db, "x")
    assert not db._resource_objects()


def test_text_and_binary_file_resources_cover_present_and_missing_paths(
    tmp_path: Path,
) -> None:
    path = tmp_path / "payload.txt"
    missing = tmp_path / "missing.txt"
    path.write_bytes("café".encode())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    db = Database()

    text = FileResource()
    assert text.label(path) == f"file[{path}]"
    assert text.probe(path) == ("present", digest)
    assert text.load(db, path) == "café"
    assert text.probe_and_load(db, path) == (("present", digest), "café")
    assert text.read(db, path) == "café"
    assert text.probe(missing) == ("missing",)
    with pytest.raises(FileNotFoundError):
        text.load(db, missing)
    with pytest.raises(FileNotFoundError):
        text.probe_and_load(db, missing)

    binary = BinaryFileResource()
    assert binary.label(path) == f"binary-file[{path}]"
    assert binary.probe(path) == ("present", digest)
    assert binary.load(db, path) == path.read_bytes()
    assert binary.probe_and_load(db, path) == (("present", digest), path.read_bytes())
    assert binary.read(db, path) == path.read_bytes()
    assert binary.probe(missing) == ("missing",)
    with pytest.raises(FileNotFoundError):
        binary.load(db, missing)
    with pytest.raises(FileNotFoundError):
        binary.probe_and_load(db, missing)


def test_file_stat_resource_covers_present_and_missing_paths(tmp_path: Path) -> None:
    present = tmp_path / "present.bin"
    missing = tmp_path / "missing.bin"
    present.write_bytes(b"1234")
    resource = FileStatResource()
    db = Database()

    assert resource.label(present) == f"filestat[{present}]"
    snapshot = resource.load(db, present)
    assert snapshot.exists
    assert snapshot.size == 4
    assert snapshot.mtime_ns is not None
    assert resource.probe(present) == (True, 4, snapshot.mtime_ns)
    assert resource.probe_and_load(db, present) == (
        (True, 4, snapshot.mtime_ns),
        snapshot,
    )
    assert cast(Any, resource.read(db, present))["exists"] is True

    absent = resource.load(db, missing)
    assert (absent.exists, absent.size, absent.mtime_ns) == (False, None, None)
    assert resource.probe(missing) == (False, None, None)
    assert resource.probe_and_load(db, missing) == ((False, None, None), absent)


def test_env_resource_covers_present_missing_and_database_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "PYINC_EDGE_CASE_ENV"
    resource = EnvResource()
    db = Database()

    monkeypatch.delenv(name, raising=False)
    assert resource.label(name) == f"env[{name}]"
    assert resource.probe(name) == (None,)
    assert resource.load(db, name) is None
    assert resource.probe_and_load(db, name) == ((None,), None)

    monkeypatch.setenv(name, "configured")
    assert resource.probe(name) == ("configured",)
    assert resource.load(db, name) == "configured"
    assert resource.probe_and_load(db, name) == (("configured",), "configured")
    assert resource.read(db, name) == "configured"


def test_directory_resource_covers_present_empty_sorted_and_missing_paths(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "directory"
    missing = tmp_path / "missing"
    directory.mkdir()
    resource = DirectoryResource()
    db = Database()

    assert resource.label(directory) == f"dir[{directory}]"
    assert resource.probe(directory) == (True, ())
    (directory / "z.txt").write_text("z")
    (directory / "a.txt").write_text("a")
    assert resource.load(db, directory) == ("a.txt", "z.txt")
    assert resource.probe(directory) == (True, ("a.txt", "z.txt"))
    assert resource.probe_and_load(db, directory) == (
        (True, ("a.txt", "z.txt")),
        ("a.txt", "z.txt"),
    )
    assert resource.read(db, directory) == ("a.txt", "z.txt")
    assert resource.probe(missing) == (False, ())
    assert resource.load(db, missing) == ()
    assert resource.probe_and_load(db, missing) == ((False, ()), ())


def test_guarded_environ_checks_every_read_but_allows_mutation() -> None:
    wrapped = {"A": "1", "B": "2"}
    reads: list[None] = []
    environ = _GuardedEnviron(wrapped, lambda: reads.append(None))

    environ["C"] = "3"
    del environ["C"]
    assert reads == []

    assert environ["A"] == "1"
    assert list(environ) == ["A", "B"]
    assert len(environ) == 2
    assert environ.get("missing", "fallback") == "fallback"
    assert list(environ.keys()) == ["A", "B"]
    assert list(environ.items()) == [("A", "1"), ("B", "2")]
    assert list(environ.values()) == ["1", "2"]
    assert environ.copy() == wrapped
    assert "A" in environ
    assert len(reads) == 10


def test_default_observer_error_hook_writes_a_concise_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _default_observer_error_hook(ValueError("broken callback"))
    assert capsys.readouterr().err == (
        "pyinc: observer callback raised ValueError: broken callback\n"
    )


def test_unsubscribe_is_idempotent() -> None:
    source = Input[int]("unsubscribe-source")

    @query
    def observed(db: Database) -> int:
        return source.read(db)

    db = Database()
    db.set(source, 1)
    events: list[object] = []
    subscription = db.observe(events.append, observed)
    subscription.unsubscribe()
    subscription.unsubscribe()
    db.get(observed)
    assert events == []


def test_resource_pathlike_read_normalizes_with_fspath(tmp_path: Path) -> None:
    path = tmp_path / "normalized.txt"
    path.write_text("value")
    db = Database()
    assert FileResource().read(db, os.fspath(path)) == "value"
