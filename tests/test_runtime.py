from __future__ import annotations

import os
from pathlib import Path

import pytest

from pyfoundinc import (
    Database,
    DirectoryResource,
    FileResource,
    FrozenDict,
    Input,
    MutationError,
    UntrackedReadError,
    query,
)


def test_equal_input_update_does_not_dirty_dependents() -> None:
    calls = {"double": 0}
    number = Input[int]("number")

    @query
    def double(db: Database) -> int:
        calls["double"] += 1
        return number.read(db) * 2

    db = Database()
    db.set(number, 4)
    assert db.get(double) == 8
    assert calls["double"] == 1

    db.set(number, 4)
    assert db.get(double) == 8
    assert calls["double"] == 1


def test_equal_recompute_backdates_and_skips_downstream() -> None:
    calls = {"parity": 0, "describe": 0}
    number = Input[int]("number")

    @query
    def parity(db: Database) -> str:
        calls["parity"] += 1
        return "even" if number.read(db) % 2 == 0 else "odd"

    @query
    def describe(db: Database) -> str:
        calls["describe"] += 1
        return f"value-is-{parity(db)}"

    db = Database()
    db.set(number, 2)
    assert db.get(describe) == "value-is-even"
    assert calls == {"parity": 1, "describe": 1}

    db.set(number, 4)
    assert db.get(describe) == "value-is-even"
    assert calls["parity"] == 2
    assert calls["describe"] == 1

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


def test_untracked_queries_rerun_without_backdating_the_impure_node() -> None:
    calls = {"impure_source": 0, "consumer": 0}

    @query
    def impure_source(db: Database) -> str:
        calls["impure_source"] += 1
        db.report_untracked_read("clock.now()")
        return "stable"

    @query
    def consumer(db: Database) -> str:
        calls["consumer"] += 1
        return impure_source(db)

    db = Database()
    assert db.get(consumer) == "stable"
    assert db.get(consumer) == "stable"
    assert calls["consumer"] == 2
    assert calls["impure_source"] >= 2

    explanation = db.explain(consumer)
    assert "impure_source(): backdated" not in explanation
    assert "untracked: clock.now()" in explanation


def test_comment_only_file_edit_backdates_parse(tmp_path: Path) -> None:
    files = FileResource()
    path = tmp_path / "module.py"
    path.write_text("import os\n", encoding="utf-8")
    counters = {"parse": 0, "imports": 0, "diagnostics": 0}

    def ast_semantic_eq(left: str, right: str) -> bool:
        import ast

        return ast.dump(ast.parse(left), include_attributes=False) == ast.dump(
            ast.parse(right),
            include_attributes=False,
        )

    @query(eq=ast_semantic_eq)
    def parse_source(db: Database, filename: str) -> str:
        counters["parse"] += 1
        return files.read(db, filename)

    @query
    def imports(db: Database, filename: str) -> tuple[str, ...]:
        counters["imports"] += 1
        source = parse_source(db, filename)
        if "import os" in source:
            return ("os",)
        return tuple()

    @query
    def diagnostics(db: Database, filename: str) -> tuple[str, ...]:
        counters["diagnostics"] += 1
        return tuple(f"import:{name}" for name in imports(db, filename))

    db = Database()
    assert db.get(diagnostics, str(path)) == ("import:os",)
    assert counters == {"parse": 1, "imports": 1, "diagnostics": 1}

    path.write_text("# comment\nimport os\n", encoding="utf-8")
    assert db.get(diagnostics, str(path)) == ("import:os",)
    assert counters["parse"] == 2
    assert counters["imports"] == 1
    assert counters["diagnostics"] == 1


def test_file_resource_detects_content_changes_even_when_stat_signature_is_stable(tmp_path: Path) -> None:
    files = FileResource()
    path = tmp_path / "sample.txt"
    path.write_text("alpha", encoding="utf-8")
    original_stat = path.stat()
    calls = {"read_file": 0}

    @query
    def read_file(db: Database, filename: str) -> str:
        calls["read_file"] += 1
        return files.read(db, filename)

    db = Database()
    assert db.get(read_file, str(path)) == "alpha"
    assert calls["read_file"] == 1

    path.write_text("bravo", encoding="utf-8")
    os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    assert db.get(read_file, str(path)) == "bravo"
    assert calls["read_file"] == 2


def test_directory_resource_tracks_listing_not_child_contents(tmp_path: Path) -> None:
    directories = DirectoryResource()
    path = tmp_path / "workspace"
    path.mkdir()
    child = path / "a.txt"
    child.write_text("alpha", encoding="utf-8")
    calls = {"entries": 0}

    @query
    def entries(db: Database, dirname: str) -> tuple[str, ...]:
        calls["entries"] += 1
        return directories.read(db, dirname)

    db = Database()
    assert db.get(entries, str(path)) == ("a.txt",)
    assert calls["entries"] == 1

    child.write_text("beta", encoding="utf-8")
    assert db.get(entries, str(path)) == ("a.txt",)
    assert calls["entries"] == 1
