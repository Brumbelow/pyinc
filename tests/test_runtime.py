from __future__ import annotations

import os
from pathlib import Path

import pytest

from pyfoundinc import (
    CycleError,
    Database,
    DirectoryResource,
    EnvResource,
    FileResource,
    FrozenDict,
    Input,
    MutationError,
    UnsupportedValueError,
    UntrackedReadError,
    query,
)


_GLOBAL_BOX = {"x": 1}


@query
def read_global_box(db: Database) -> int:
    return _GLOBAL_BOX["x"]


def _query_record(db: Database, query_fn: object, *args: object, **kwargs: object) -> object:
    key, _ = db._query_key(query_fn, args, kwargs)
    return db._records[key]


def test_equal_input_update_does_not_dirty_dependents() -> None:
    number = Input[int]("number")

    @query
    def double(db: Database) -> int:
        return number.read(db) * 2

    db = Database()
    db.set(number, 4)
    assert db.get(double) == 8
    assert _query_record(db, double).last_decision == "executed"

    db.set(number, 4)
    assert db.revision == 1
    assert db.get(double) == 8
    record = _query_record(db, double)
    assert record.last_decision == "reused"
    assert record.changed_at == 1


def test_equal_recompute_backdates_and_skips_downstream() -> None:
    number = Input[int]("number")

    @query
    def parity(db: Database) -> str:
        return "even" if number.read(db) % 2 == 0 else "odd"

    @query
    def describe(db: Database) -> str:
        return f"value-is-{parity(db)}"

    db = Database()
    db.set(number, 2)
    assert db.get(describe) == "value-is-even"

    db.set(number, 4)
    assert db.get(describe) == "value-is-even"
    parity_record = _query_record(db, parity)
    describe_record = _query_record(db, describe)
    assert parity_record.last_decision == "backdated"
    assert parity_record.changed_at == 1
    assert describe_record.last_decision == "reused"
    assert describe_record.changed_at == 1

    explanation = db.explain(describe)
    assert "describe" in explanation
    assert "backdated" in explanation


def test_dynamic_dependencies_drop_stale_edges() -> None:
    chooser = Input[str]("chooser")
    left = Input[int]("left")
    right = Input[int]("right")

    @query
    def branch(db: Database) -> int:
        if chooser.read(db) == "left":
            return left.read(db)
        return right.read(db)

    db = Database()
    db.set(chooser, "left")
    db.set(left, 1)
    db.set(right, 10)

    assert db.get(branch) == 1

    db.set(chooser, "right")
    assert db.get(branch) == 10

    explanation = db.explain(branch)
    assert "input[right]" in explanation
    assert "input[left]" not in explanation


@pytest.mark.parametrize(
    ("mode", "expected_type"),
    [("strict", FrozenDict), ("checked", dict), ("fast", dict)],
)
def test_modes_expose_expected_boundary_shapes(mode: str, expected_type: type[object]) -> None:
    payload = Input[dict[str, int]]("payload")

    @query
    def echo(db: Database) -> object:
        return payload.read(db)

    db = Database(mode=mode)
    db.set(payload, {"x": 1})
    result = db.get(echo)
    assert isinstance(result, expected_type)


@pytest.mark.parametrize("mode", ["strict", "checked"])
def test_mutation_raises_for_boundary_inputs(mode: str) -> None:
    payload = Input[dict[str, int]]("payload")

    @query
    def mutate(db: Database) -> int:
        current = payload.read(db)
        current["x"] = 99
        return 1

    db = Database(mode=mode)
    db.set(payload, {"x": 1})
    with pytest.raises((MutationError, TypeError, AttributeError)):
        db.get(mutate)


def test_fast_mode_uses_owned_values_without_mutation_detection() -> None:
    payload = Input[tuple[dict[str, int], dict[str, int]]]("payload")

    @query
    def mutate_left(db: Database) -> int:
        left, right = payload.read(db)
        left["x"] = 99
        return right["x"]

    @query
    def read_right(db: Database) -> int:
        _, right = payload.read(db)
        return right["x"]

    shared = {"x": 1}
    db = Database(mode="fast")
    db.set(payload, (shared, shared))

    assert db.get(mutate_left) == 1
    assert db.get(read_right) == 1


def test_raw_open_is_rejected_inside_query(tmp_path: Path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("hello", encoding="utf-8")

    @query
    def read_directly(db: Database) -> str:
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    db = Database()
    with pytest.raises(UntrackedReadError):
        db.get(read_directly)


@pytest.mark.parametrize("method_name", ["read_text", "read_bytes"])
def test_path_read_helpers_are_rejected_inside_query(tmp_path: Path, method_name: str) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("hello", encoding="utf-8")

    @query
    def read_via_path(db: Database) -> object:
        method = getattr(path, method_name)
        if method_name == "read_text":
            return method(encoding="utf-8")
        return method()

    db = Database()
    with pytest.raises(UntrackedReadError):
        db.get(read_via_path)


def test_untracked_queries_rerun_without_backdating_the_impure_node() -> None:
    @query
    def impure_source(db: Database) -> str:
        db.report_untracked_read("clock.now()")
        return "stable"

    @query
    def consumer(db: Database) -> str:
        return impure_source(db)

    db = Database()
    assert db.get(consumer) == "stable"
    assert db.get(consumer) == "stable"
    impure_record = _query_record(db, impure_source)
    assert impure_record.is_untracked
    assert impure_record.last_decision == "executed"

    explanation = db.explain(consumer)
    assert "impure_source(): backdated" not in explanation
    assert "untracked: clock.now()" in explanation


def test_comment_only_file_edit_backdates_parse(tmp_path: Path) -> None:
    files = FileResource()
    path = tmp_path / "module.py"
    path.write_text("import os\n", encoding="utf-8")

    def ast_semantic_eq(left: str, right: str) -> bool:
        import ast

        return ast.dump(ast.parse(left), include_attributes=False) == ast.dump(
            ast.parse(right),
            include_attributes=False,
        )

    @query(eq=ast_semantic_eq)
    def parse_source(db: Database, filename: str) -> str:
        return files.read(db, filename)

    @query
    def imports(db: Database, filename: str) -> tuple[str, ...]:
        source = parse_source(db, filename)
        if "import os" in source:
            return ("os",)
        return tuple()

    @query
    def diagnostics(db: Database, filename: str) -> tuple[str, ...]:
        return tuple(f"import:{name}" for name in imports(db, filename))

    db = Database()
    assert db.get(diagnostics, str(path)) == ("import:os",)

    path.write_text("# comment\nimport os\n", encoding="utf-8")
    assert db.get(diagnostics, str(path)) == ("import:os",)
    assert _query_record(db, parse_source, str(path)).last_decision == "backdated"
    assert _query_record(db, imports, str(path)).last_decision == "reused"
    assert _query_record(db, diagnostics, str(path)).last_decision == "reused"


def test_file_resource_detects_content_changes_even_when_stat_signature_is_stable(tmp_path: Path) -> None:
    files = FileResource()
    path = tmp_path / "sample.txt"
    path.write_text("alpha", encoding="utf-8")
    original_stat = path.stat()

    @query
    def read_file(db: Database, filename: str) -> str:
        return files.read(db, filename)

    db = Database()
    assert db.get(read_file, str(path)) == "alpha"

    path.write_text("bravo", encoding="utf-8")
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    assert db.get(read_file, str(path)) == "bravo"
    record = _query_record(db, read_file, str(path))
    assert record.last_decision == "executed"
    assert record.changed_at == 1


def test_directory_resource_tracks_listing_not_child_contents(tmp_path: Path) -> None:
    directories = DirectoryResource()
    path = tmp_path / "workspace"
    path.mkdir()
    child = path / "a.txt"
    child.write_text("alpha", encoding="utf-8")

    @query
    def entries(db: Database, dirname: str) -> tuple[str, ...]:
        return directories.read(db, dirname)

    db = Database()
    assert db.get(entries, str(path)) == ("a.txt",)

    child.write_text("beta", encoding="utf-8")
    assert db.get(entries, str(path)) == ("a.txt",)
    assert _query_record(db, entries, str(path)).last_decision == "reused"


def test_direct_cycles_raise_cycle_error() -> None:
    @query
    def direct(db: Database) -> int:
        return direct(db)

    with pytest.raises(CycleError):
        Database().get(direct)


def test_indirect_cycles_raise_cycle_error() -> None:
    @query
    def left(db: Database) -> int:
        return right(db)

    @query
    def right(db: Database) -> int:
        return left(db)

    with pytest.raises(CycleError):
        Database().get(left)


def test_queries_reject_mutable_closure_captures() -> None:
    box = {"x": 1}

    @query
    def read_box(db: Database) -> int:
        return box["x"]

    with pytest.raises(UnsupportedValueError, match="box"):
        Database().get(read_box)


def test_queries_reject_mutable_global_captures() -> None:
    with pytest.raises(UnsupportedValueError, match="_GLOBAL_BOX"):
        Database().get(read_global_box)


def test_queries_allow_immutable_closure_values() -> None:
    suffix = ("!",)
    number = Input[int]("number")

    @query
    def decorate(db: Database) -> str:
        return f"{number.read(db)}{suffix[0]}"

    db = Database()
    db.set(number, 3)
    assert db.get(decorate) == "3!"


def test_file_resource_identity_includes_configuration(tmp_path: Path) -> None:
    path = tmp_path / "encoded.txt"
    path.write_bytes("café".encode("latin-1"))
    latin1 = FileResource(encoding="latin-1")
    utf8 = FileResource(encoding="utf-8")

    @query
    def read_latin1(db: Database, filename: str) -> str:
        return latin1.read(db, filename)

    @query
    def read_utf8(db: Database, filename: str) -> str:
        return utf8.read(db, filename)

    db = Database()
    assert db.get(read_latin1, str(path)) == "café"
    with pytest.raises(UnicodeDecodeError):
        db.get(read_utf8, str(path))
    assert db.get(read_latin1, str(path)) == "café"


def test_env_resource_instances_share_stable_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    env_a = EnvResource()
    env_b = EnvResource()
    monkeypatch.setenv("PYFOUNDINC_SAMPLE", "value")

    @query
    def read_a(db: Database) -> str | None:
        return env_a.read(db, "PYFOUNDINC_SAMPLE")

    @query
    def read_b(db: Database) -> str | None:
        return env_b.read(db, "PYFOUNDINC_SAMPLE")

    db = Database()
    assert db.get(read_a) == "value"
    assert db.get(read_b) == "value"
