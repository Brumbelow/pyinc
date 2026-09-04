from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from enum import Enum, StrEnum
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


@dataclass(frozen=True)
class _EchoResource(Resource[str, str, str]):
    prefix: str = "value"

    def probe(self, key: str) -> str:
        return f"probe:{key}"

    def load(self, db: Database, key: str) -> str:
        return f"{self.prefix}:{key}"

    def label(self, key: str) -> str:
        return f"echo[{key}]"


@pytest.mark.parametrize("key", ["", 0, None])
def test_input_rejects_invalid_keys(key: object) -> None:
    with pytest.raises(InputKeyError, match="non-empty string"):
        Input[object](cast(Any, key))


class _TaggedKey(str):
    """A plain `str` subclass, whose instances can carry arbitrary attributes.

    Storing one as node identity would make the kernel hold that state for the
    lifetime of the database.
    """


class _ModernKey(StrEnum):
    A = "a"


class _LegacyKey(str, Enum):  # noqa: UP042 - the pre-StrEnum mixin idiom is under test
    A = "a"


class _TrappedKey(str):
    """Every dunder a key guard could consult is a trap; reaching one is the defect.

    The refusal is decided from `type(key)` alone and reports the offending type
    by name, so it never touches the value: not its emptiness (`__bool__`,
    `__len__`), not its rendering (`__str__`, `__repr__`, `__format__`) and not
    its equality (`__eq__`). `__hash__` stays `str`'s so the instance is still
    usable right up to the guard.
    """

    __hash__ = str.__hash__

    def __len__(self) -> int:
        raise AssertionError("no user dunder runs before the exactness check")

    def __bool__(self) -> bool:
        raise AssertionError("no user dunder runs before the exactness check")

    def __str__(self) -> str:
        raise AssertionError("no user dunder runs before the exactness check")

    def __repr__(self) -> str:
        raise AssertionError("no user dunder runs before the exactness check")

    def __format__(self, spec: str) -> str:
        raise AssertionError("no user dunder runs before the exactness check")

    def __eq__(self, other: object) -> bool:
        raise AssertionError("no user dunder runs before the exactness check")


def test_input_rejects_str_subclasses_and_names_the_plain_spelling() -> None:
    """Input keys are exactly `str`, and the refusal says what to pass instead.

    A subclass reaches the record table as node identity, so its own equality,
    formatting and emptiness behaviour decide what the kernel stores. Enum
    members are the common accidental case, which is why the message names
    `key.value` rather than only describing the rule.
    """
    for key in (_TaggedKey("a"), _ModernKey.A, _LegacyKey.A):
        with pytest.raises(InputKeyError, match="exactly str") as raised:
            Input[int](cast(Any, key))
        message = str(raised.value)
        assert "key.value" in message
        assert type(key).__name__ in message

    # The spelling the message names constructs, and so does any plain string.
    assert Input[int](_ModernKey.A.value).key == "a"
    assert type(Input[int]("a").key) is str


def test_query_rejects_str_subclass_keys_and_names_the_plain_spelling() -> None:
    """`@query(key=...)` and `Query(key=...)` share Input's exactness rule.

    A subclass key drifts query identity through `__format__` and reaches the
    checkpoint manifest as a recorded query id, so it is refused where it is
    written rather than where it is later rendered.
    """

    def calculate(db: Database) -> int:
        return 1

    for key in (_TaggedKey("a"), _ModernKey.A, _LegacyKey.A):
        with pytest.raises(ValueError, match="exactly str") as raised:
            Query(calculate, key=cast(Any, key))
        assert "key.value" in str(raised.value)

        with pytest.raises(ValueError, match="exactly str") as decorated:
            query(key=cast(Any, key))(calculate)
        assert "key.value" in str(decorated.value)

    # Exactness is decided before emptiness here too, so no user dunder runs on
    # the way to the refusal.
    with pytest.raises(ValueError, match="exactly str"):
        Query(calculate, key=cast(Any, _TrappedKey("")))

    # The derived default key and an explicit plain-string key both construct.
    assert Query(calculate).key == f"{calculate.__module__}:{calculate.__qualname__}"
    assert Query(calculate, key=_ModernKey.A.value).key == "a"
    assert type(Query(calculate, key="a").key) is str


def test_input_empty_key_guard_cannot_be_bypassed_by_a_subclass() -> None:
    """Emptiness is decided only after the key is known to be exactly `str`.

    `not key` consults `__bool__` and `__len__`, so a subclass lying about its
    own emptiness used to pass the non-empty guard and register a node labelled
    `input[]`. Ordering the exactness check first means no user dunder runs on
    the way to a refusal.
    """

    with pytest.raises(InputKeyError, match="non-empty string"):
        Input[int]("")
    with pytest.raises(InputKeyError, match="exactly str"):
        Input[int](cast(Any, _TrappedKey("")))

    # Only well-formed keys reach the record table, so a node can no longer be
    # labelled for a key the guard was supposed to have refused.
    db = Database()
    db.set(Input[int]("present"), 1)
    assert [key.label for key in db._records] == ["input[present]"]


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
    # Read through the database, in the mode that hands values back as frozen
    # views: the reading is still the resource's own snapshot type, because the
    # kernel rebuilds it through a built-in adapter at every boundary.
    assert resource.read(db, present).exists is True

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


def test_environ_union_operators_return_plain_dicts_after_database_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Database()
    monkeypatch.setenv("PYINC_UNION_RIGHT", "right")
    monkeypatch.delenv("PYINC_UNION_ADDED", raising=False)

    merged = os.environ | {"PYINC_UNION_ADDED": "added"}
    assert type(merged) is dict
    assert merged["PYINC_UNION_ADDED"] == "added"
    assert merged["PYINC_UNION_RIGHT"] == "right"
    assert "PYINC_UNION_ADDED" not in os.environ

    reflected = {"PYINC_UNION_ADDED": "added", "PYINC_UNION_RIGHT": "shadowed"} | os.environ
    assert type(reflected) is dict
    assert reflected["PYINC_UNION_ADDED"] == "added"
    assert reflected["PYINC_UNION_RIGHT"] == "right"


def test_environ_in_place_union_updates_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Database()
    monkeypatch.setenv("PYINC_IOR_MAPPING", "before")
    monkeypatch.setenv("PYINC_IOR_PAIRS", "before")
    environ_before = os.environ

    os.environ |= {"PYINC_IOR_MAPPING": "after"}
    os.environ |= [("PYINC_IOR_PAIRS", "after")]

    assert os.environ is environ_before
    assert os.environ["PYINC_IOR_MAPPING"] == "after"
    assert os.getenv("PYINC_IOR_PAIRS") == "after"


def test_environ_codec_helpers_survive_database_construction() -> None:
    Database()
    assert os.environ.decodekey(os.environ.encodekey("PYINC_CODEC_KEY")) == "PYINC_CODEC_KEY"
    assert os.environ.decodevalue(os.environ.encodevalue("codec-value")) == "codec-value"


def test_environ_raw_data_mapping_stays_hidden_after_database_construction() -> None:
    # `os._Environ` keeps the live process environment in `_data`, reachable as
    # a plain attribute without ever touching the mapping protocol. The wrapper
    # therefore refuses everything beyond the four codec helpers.
    Database()
    with pytest.raises(AttributeError, match="_data"):
        _ = os.environ._data  # type: ignore[attr-defined]


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
