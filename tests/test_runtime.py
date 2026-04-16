from __future__ import annotations

import mmap
import os
from pathlib import Path
from typing import Any, cast

import pytest

from pyinc import (
    CycleError,
    Database,
    DatabaseStatistics,
    DependencyGraphNode,
    DirectoryResource,
    EnvResource,
    FileResource,
    FileStatResource,
    FrozenDict,
    Input,
    InspectionNode,
    MutationError,
    QueryProfile,
    UnsupportedValueError,
    UntrackedReadError,
    query,
)

_GLOBAL_BOX = {"x": 1}


@query
def read_global_box(db: Database) -> int:
    return _GLOBAL_BOX["x"]


def _query_record(db: Database, query_fn: object, *args: object, **kwargs: object) -> Any:
    key, _ = db._query_key(query_fn, args, kwargs)
    return db._records[key]


def _inspect_node(db: Database, query_fn: Any, *args: object, **kwargs: object) -> InspectionNode:
    return db.inspect(query_fn, *args, **kwargs)


def _find_node(root: InspectionNode, needle: str) -> InspectionNode:
    if needle in root.label:
        return root
    for dependency in root.dependencies:
        try:
            return _find_node(dependency, needle)
        except LookupError:
            continue
    raise LookupError(needle)


def test_max_query_nodes_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Database(max_query_nodes=0)


def test_inputs_and_queries_reject_eq_and_cutoff_together() -> None:
    with pytest.raises(ValueError, match="either eq= or cutoff="):
        Input[int]("number", eq=lambda left, right: left == right, cutoff=abs)

    with pytest.raises(ValueError, match="either eq= or cutoff="):

        @query(eq=lambda left, right: left == right, cutoff=abs)
        def invalid(db: Database) -> int:
            return 1


def test_equal_input_update_does_not_dirty_dependents() -> None:
    number = Input[int]("number")

    @query
    def double(db: Database) -> int:
        return number.read(db) * 2

    db = Database()
    db.set(number, 4)
    assert db.get(double) == 8
    assert _inspect_node(db, double).last_decision == "executed"

    db.set(number, 4)
    assert db.revision == 1
    assert db.get(double) == 8
    record = _inspect_node(db, double)
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
    inspection = _inspect_node(db, describe)
    parity_record = _find_node(inspection, "parity")
    describe_record = inspection
    assert parity_record.last_decision == "backdated"
    assert parity_record.changed_at == 1
    assert describe_record.last_decision == "reused"
    assert describe_record.changed_at == 1

    explanation = db.explain(describe)
    assert "describe" in explanation
    assert "backdated" in explanation


def test_input_cutoff_suppresses_equal_updates() -> None:
    number = Input[int]("number", cutoff=abs)

    @query
    def describe(db: Database) -> int:
        return abs(number.read(db))

    db = Database()
    db.set(number, 4)
    assert db.get(describe) == 4

    db.set(number, -4)
    assert db.revision == 1
    assert db.get(describe) == 4
    inspection = _inspect_node(db, describe)
    assert inspection.last_decision == "reused"
    input_node = _find_node(inspection, "input[number]")
    assert input_node.last_decision == "reused"
    assert input_node.reason == "equal input update ignored"


def test_query_cutoff_backdates_and_skips_downstream(tmp_path: Path) -> None:
    files = FileResource()
    path = tmp_path / "module.py"
    path.write_text("import os\n", encoding="utf-8")

    def ast_cutoff(source: str) -> str:
        import ast

        return ast.dump(ast.parse(source), include_attributes=False)

    @query(cutoff=ast_cutoff)
    def parse_source(db: Database, filename: str) -> str:
        return files.read(db, filename)

    @query
    def imports(db: Database, filename: str) -> tuple[str, ...]:
        source = parse_source(db, filename)
        return ("os",) if "import os" in source else tuple()

    @query
    def diagnostics(db: Database, filename: str) -> tuple[str, ...]:
        return tuple(f"import:{name}" for name in imports(db, filename))

    db = Database()
    assert db.get(diagnostics, str(path)) == ("import:os",)

    path.write_text("# comment\nimport os\n", encoding="utf-8")
    assert db.get(diagnostics, str(path)) == ("import:os",)
    inspection = _inspect_node(db, diagnostics, str(path))
    assert _find_node(inspection, "parse_source").last_decision == "backdated"
    assert _find_node(inspection, "imports").last_decision == "reused"
    assert inspection.last_decision == "reused"


def test_cutoff_tokens_must_be_snapshot_safe() -> None:
    number = Input[int]("number", cutoff=lambda value: iter((value,)))

    db = Database()
    db.set(number, 1)
    with pytest.raises(UnsupportedValueError, match="Cutoff functions must return snapshot-safe values"):
        db.set(number, 1)


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

    inspection = _inspect_node(db, branch)
    assert any(dependency.label == "input[right]" for dependency in inspection.dependencies)
    assert all(dependency.label != "input[left]" for dependency in inspection.dependencies)


def test_inspect_returns_structured_dependency_tree() -> None:
    number = Input[int]("number")

    @query
    def double(db: Database) -> int:
        return number.read(db) * 2

    @query
    def describe(db: Database) -> str:
        return f"value={double(db)}"

    db = Database()
    db.set(number, 3)

    inspection = db.inspect(describe)

    assert inspection.kind == "query"
    assert inspection.label.endswith("describe()")
    assert inspection.last_decision == "executed"
    double_node = _find_node(inspection, "double")
    input_node = _find_node(inspection, "input[number]")
    assert double_node.kind == "query"
    assert input_node.kind == "input"
    assert input_node.dependencies == ()


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


def test_os_getenv_is_rejected_inside_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYINC_DIRECT_ENV", "value")

    @query
    def read_env(db: Database) -> str | None:
        return os.getenv("PYINC_DIRECT_ENV")

    with pytest.raises(UntrackedReadError):
        Database().get(read_env)


def test_os_environ_access_is_rejected_inside_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYINC_DIRECT_ENV", "value")

    @query
    def read_env_mapping(db: Database) -> str:
        return os.environ["PYINC_DIRECT_ENV"]

    with pytest.raises(UntrackedReadError):
        Database().get(read_env_mapping)


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


@pytest.mark.parametrize("method_name", ["listdir", "scandir", "iterdir"])
def test_directory_helpers_are_rejected_inside_query(tmp_path: Path, method_name: str) -> None:
    path = tmp_path / "workspace"
    path.mkdir()
    (path / "a.txt").write_text("alpha", encoding="utf-8")

    @query
    def read_directory(db: Database) -> tuple[str, ...]:
        if method_name == "listdir":
            return tuple(sorted(os.listdir(path)))
        if method_name == "scandir":
            return tuple(sorted(entry.name for entry in os.scandir(path)))
        return tuple(sorted(child.name for child in path.iterdir()))

    with pytest.raises(UntrackedReadError):
        Database().get(read_directory)


def test_resource_reads_are_allowed_inside_query(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = EnvResource()
    directories = DirectoryResource()
    monkeypatch.setenv("PYINC_TRACKED_ENV", "value")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("alpha", encoding="utf-8")

    @query
    def read_tracked(db: Database, dirname: str) -> tuple[str | None, tuple[str, ...]]:
        return env.read(db, "PYINC_TRACKED_ENV"), directories.read(db, dirname)

    db = Database()
    assert db.get(read_tracked, str(workspace)) == ("value", ("a.txt",))


def test_failed_resource_reads_do_not_leave_dangling_dependencies(tmp_path: Path) -> None:
    directories = DirectoryResource()
    path = tmp_path / "sample.py"
    path.write_text("value = 1\n", encoding="utf-8")

    @query
    def classify(db: Database, candidate: str) -> str:
        try:
            directories.read(db, candidate)
        except NotADirectoryError:
            return "file"
        return "directory"

    db = Database()
    assert db.get(classify, str(path)) == "file"
    inspection = _inspect_node(db, classify, str(path))
    assert inspection.last_decision == "executed"
    assert inspection.dependencies == ()


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
    inspection = _inspect_node(db, consumer)
    impure_record = _find_node(inspection, "impure_source")
    assert impure_record.is_untracked
    assert impure_record.last_recompute == "executed"

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
    inspection = _inspect_node(db, diagnostics, str(path))
    assert _find_node(inspection, "parse_source").last_decision == "backdated"
    assert _find_node(inspection, "imports").last_decision == "reused"
    assert inspection.last_decision == "reused"


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
    record = _inspect_node(db, read_file, str(path))
    assert record.last_decision == "executed"
    assert record.changed_at == 1


def test_file_stat_resource_tracks_metadata_changes(tmp_path: Path) -> None:
    stats = FileStatResource()
    path = tmp_path / "sample.txt"
    path.write_text("alpha", encoding="utf-8")

    @query
    def read_stat(db: Database, filename: str) -> object:
        return stats.read(db, filename)

    db = Database(mode="checked")
    first = cast(dict[str, object], db.get(read_stat, str(path)))
    assert first["exists"] is True
    assert first["size"] == 5

    path.write_text("bravo!", encoding="utf-8")
    second = cast(dict[str, object], db.get(read_stat, str(path)))
    assert second["exists"] is True
    assert second["size"] == 6
    assert _inspect_node(db, read_stat, str(path)).last_decision == "executed"


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
    assert _inspect_node(db, entries, str(path)).last_decision == "reused"


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
    path.write_bytes("caf\xe9".encode("latin-1"))
    latin1 = FileResource(encoding="latin-1")
    utf8 = FileResource(encoding="utf-8")

    @query
    def read_latin1(db: Database, filename: str) -> str:
        return latin1.read(db, filename)

    @query
    def read_utf8(db: Database, filename: str) -> str:
        return utf8.read(db, filename)

    db = Database()
    assert db.get(read_latin1, str(path)) == "caf\xe9"
    with pytest.raises(UnicodeDecodeError):
        db.get(read_utf8, str(path))
    assert db.get(read_latin1, str(path)) == "caf\xe9"


def test_env_resource_instances_share_stable_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    env_a = EnvResource()
    env_b = EnvResource()
    monkeypatch.setenv("PYINC_SAMPLE", "value")

    @query
    def read_a(db: Database) -> str | None:
        return env_a.read(db, "PYINC_SAMPLE")

    @query
    def read_b(db: Database) -> str | None:
        return env_b.read(db, "PYINC_SAMPLE")

    db = Database()
    assert db.get(read_a) == "value"
    assert db.get(read_b) == "value"


def test_query_lru_eviction_prunes_oldest_query_records() -> None:
    @query
    def echo_number(db: Database, value: int) -> int:
        return value

    db = Database(max_query_nodes=2)
    key_one, _ = db._query_key(echo_number, (1,), {})
    assert db.get(echo_number, 1) == 1
    key_two, _ = db._query_key(echo_number, (2,), {})
    assert db.get(echo_number, 2) == 2
    key_three, _ = db._query_key(echo_number, (3,), {})
    assert db.get(echo_number, 3) == 3

    assert key_one not in db._records
    assert key_one not in db._call_snapshots()
    assert key_two in db._records
    assert key_three in db._records
    assert len([key for key in db._records if key.kind == "query"]) == 2


def test_dependencies_revalidate_correctly_after_lru_eviction() -> None:
    number = Input[int]("number")

    @query
    def child(db: Database) -> int:
        return number.read(db) * 2

    @query
    def parent(db: Database) -> int:
        return child(db) + 1

    @query
    def unrelated(db: Database) -> str:
        return "x"

    db = Database(max_query_nodes=2)
    db.set(number, 1)
    assert db.get(parent) == 3
    assert db.get(unrelated) == "x"

    child_key, _ = db._query_key(child, (), {})
    assert child_key not in db._records

    db.set(number, 2)
    assert db.get(parent) == 5
    parent_record = _query_record(db, parent)
    assert parent_record.last_decision == "executed"


# ---------------------------------------------------------------------------
# Rewiring torture suite (Adapton sharing / switching / swapping patterns)
# ---------------------------------------------------------------------------


def test_diamond_dependency_with_rewiring() -> None:
    chooser = Input[str]("chooser")
    left = Input[int]("left")
    right = Input[int]("right")

    @query
    def intermediate(db: Database) -> int:
        if chooser.read(db) == "left":
            return left.read(db)
        return right.read(db)

    @query
    def consumer_a(db: Database) -> int:
        return intermediate(db) + 1

    @query
    def consumer_b(db: Database) -> int:
        return intermediate(db) * 2

    db = Database()
    db.set(chooser, "left")
    db.set(left, 5)
    db.set(right, 10)

    assert db.get(consumer_a) == 6
    assert db.get(consumer_b) == 10

    # Switch intermediate from left to right.
    db.set(chooser, "right")
    assert db.get(consumer_a) == 11
    assert db.get(consumer_b) == 20

    # Verify stale edge dropped and new edge present.
    intermediate_node = _find_node(_inspect_node(db, consumer_a), "intermediate")
    dep_labels = {dep.label for dep in intermediate_node.dependencies}
    assert "input[right]" in dep_labels
    assert "input[left]" not in dep_labels


def test_multi_level_switching() -> None:
    level0_chooser = Input[str]("level0_chooser")
    level1_chooser = Input[str]("level1_chooser")
    a = Input[int]("a")
    b = Input[int]("b")
    x = Input[int]("x")
    y = Input[int]("y")

    @query
    def level0(db: Database) -> int:
        return a.read(db) if level0_chooser.read(db) == "a" else b.read(db)

    @query
    def level1(db: Database) -> int:
        return x.read(db) if level1_chooser.read(db) == "x" else y.read(db)

    @query
    def combined(db: Database) -> int:
        return level0(db) + level1(db)

    db = Database()
    db.set(level0_chooser, "a")
    db.set(level1_chooser, "x")
    db.set(a, 1)
    db.set(b, 2)
    db.set(x, 10)
    db.set(y, 20)

    assert db.get(combined) == 11  # a(1) + x(10)

    # Switch both choosers simultaneously.
    db.set(level0_chooser, "b")
    db.set(level1_chooser, "y")
    assert db.get(combined) == 22  # b(2) + y(20)

    # Verify stale edges dropped at both levels.
    tree = _inspect_node(db, combined)
    level0_node = _find_node(tree, "level0")
    level1_node = _find_node(tree, "level1")
    l0_deps = {dep.label for dep in level0_node.dependencies}
    l1_deps = {dep.label for dep in level1_node.dependencies}
    assert "input[b]" in l0_deps and "input[a]" not in l0_deps
    assert "input[y]" in l1_deps and "input[x]" not in l1_deps


def test_sharing_pattern_backdates_when_rewired_result_is_equal() -> None:
    chooser = Input[str]("chooser")
    left = Input[int]("left")
    right = Input[int]("right")

    @query
    def shared(db: Database) -> int:
        return left.read(db) if chooser.read(db) == "left" else right.read(db)

    @query
    def consumer_a(db: Database) -> str:
        return f"a={shared(db)}"

    @query
    def consumer_b(db: Database) -> str:
        return f"b={shared(db)}"

    db = Database()
    db.set(chooser, "left")
    db.set(left, 5)
    db.set(right, 5)  # Same value as left.

    assert db.get(consumer_a) == "a=5"
    assert db.get(consumer_b) == "b=5"

    # Switch chooser; shared rewires but produces the same value.
    db.set(chooser, "right")
    assert db.get(consumer_a) == "a=5"

    # Inspect before consumer_b runs — shared was backdated during consumer_a's request.
    tree_a = _inspect_node(db, consumer_a)
    shared_node = _find_node(tree_a, "shared")
    assert shared_node.last_decision == "backdated"
    assert tree_a.last_decision == "reused"

    assert db.get(consumer_b) == "b=5"
    tree_b = _inspect_node(db, consumer_b)
    assert tree_b.last_decision == "reused"


def test_swapping_pattern_two_queries_exchange_deps() -> None:
    selector = Input[str]("selector")
    a_val = Input[int]("a_val")
    b_val = Input[int]("b_val")

    @query
    def read_a(db: Database) -> int:
        if selector.read(db) == "normal":
            return a_val.read(db)
        return b_val.read(db)

    @query
    def read_b(db: Database) -> int:
        if selector.read(db) == "normal":
            return b_val.read(db)
        return a_val.read(db)

    @query
    def combined(db: Database) -> tuple[int, int]:
        return (read_a(db), read_b(db))

    db = Database()
    db.set(selector, "normal")
    db.set(a_val, 1)
    db.set(b_val, 2)

    assert db.get(combined) == (1, 2)

    # Swap: read_a now reads b_val, read_b now reads a_val.
    db.set(selector, "swapped")
    assert db.get(combined) == (2, 1)

    # Verify edges swapped.
    tree = _inspect_node(db, combined)
    ra_node = _find_node(tree, "read_a")
    rb_node = _find_node(tree, "read_b")
    ra_deps = {dep.label for dep in ra_node.dependencies}
    rb_deps = {dep.label for dep in rb_node.dependencies}
    assert "input[b_val]" in ra_deps and "input[a_val]" not in ra_deps
    assert "input[a_val]" in rb_deps and "input[b_val]" not in rb_deps


def test_rewiring_with_lru_eviction() -> None:
    chooser = Input[str]("chooser")
    left = Input[int]("left")
    right = Input[int]("right")

    @query
    def branch(db: Database) -> int:
        return left.read(db) if chooser.read(db) == "left" else right.read(db)

    @query
    def consumer(db: Database) -> int:
        return branch(db) + 1

    @query
    def filler(db: Database) -> str:
        return "filler"

    db = Database(max_query_nodes=2)
    db.set(chooser, "left")
    db.set(left, 10)
    db.set(right, 20)

    assert db.get(consumer) == 11

    # Evict branch by requesting filler (max_query_nodes=2 keeps consumer + filler).
    assert db.get(filler) == "filler"

    # Now switch and request consumer again — branch must re-execute from scratch.
    db.set(chooser, "right")
    assert db.get(consumer) == 21

    # Verify correctness against a fresh database.
    fresh = Database()
    fresh.set(chooser, "right")
    fresh.set(left, 10)
    fresh.set(right, 20)
    assert fresh.get(consumer) == db.get(consumer)


# ---------------------------------------------------------------------------
# Mutation adversarial tests
# ---------------------------------------------------------------------------


def test_external_alias_mutation_after_boundary_crossing() -> None:
    payload = Input[dict[str, list[int]]]("payload")

    @query
    def echo(db: Database) -> object:
        return payload.read(db)

    data: dict[str, list[int]] = {"key": [1, 2, 3]}
    db = Database(mode="checked")
    db.set(payload, data)

    assert db.get(echo) == {"key": [1, 2, 3]}

    # Mutate through the external alias — kernel snapshot must be unaffected.
    data["key"].append(4)
    data["new_key"] = [99]

    assert db.get(echo) == {"key": [1, 2, 3]}


@pytest.mark.parametrize("mode", ["strict", "checked"])
def test_deeply_nested_mutation_detection(mode: str) -> None:
    payload = Input[dict[str, Any]]("nested")

    @query
    def mutate_deep(db: Database) -> int:
        value = payload.read(db)
        value["l1"]["l2"]["l3"].append(4)
        return 1

    db = Database(mode=mode)
    db.set(payload, {"l1": {"l2": {"l3": [1, 2, 3]}}})
    with pytest.raises((MutationError, TypeError, AttributeError)):
        db.get(mutate_deep)


@pytest.mark.parametrize("mode", ["checked", "fast"])
def test_mutation_of_query_return_value_does_not_corrupt_memo(mode: str) -> None:
    payload = Input[int]("trigger")

    @query
    def produce(db: Database) -> dict[str, list[int]]:
        payload.read(db)
        return {"items": [1, 2, 3]}

    db = Database(mode=mode)
    db.set(payload, 1)

    first = db.get(produce)
    assert first == {"items": [1, 2, 3]}

    # Caller mutates the returned copy.
    first["items"].append(4)
    assert first == {"items": [1, 2, 3, 4]}

    # Next get must return a fresh copy from the frozen memo.
    second = db.get(produce)
    assert second == {"items": [1, 2, 3]}


def test_custom_eq_with_side_effect_does_not_corrupt_graph() -> None:
    call_count = [0]

    def parity_eq(left: int, right: int) -> bool:
        call_count[0] += 1
        return left % 2 == right % 2

    number = Input[int]("number")

    @query(eq=parity_eq)
    def transform(db: Database) -> int:
        return number.read(db)

    @query
    def describe(db: Database) -> str:
        return f"v={transform(db)}"

    db = Database()
    db.set(number, 3)
    assert db.get(describe) == "v=3"

    # Change input: 3 → 5 (both odd), parity_eq(3, 5) → True → backdated.
    # eq callback fires with a side effect; kernel should still function correctly.
    db.set(number, 5)
    assert db.get(describe) == "v=3"  # Backdated — parity says equal.
    assert call_count[0] > 0
    assert _inspect_node(db, describe).last_decision == "reused"

    # Now change parity: odd → even, eq returns False → graph updates.
    db.set(number, 4)
    assert db.get(describe) == "v=4"


@pytest.mark.parametrize("mode", ["checked", "fast"])
def test_two_queries_reading_same_input_get_independent_copies(mode: str) -> None:
    payload = Input[dict[str, list[int]]]("shared")

    @query
    def query_a(db: Database) -> object:
        return payload.read(db)

    @query
    def query_b(db: Database) -> object:
        return payload.read(db)

    db = Database(mode=mode)
    db.set(payload, {"items": [1, 2, 3]})

    result_a = db.get(query_a)
    result_b = db.get(query_b)

    # Independent objects — no shared aliases.
    assert result_a is not result_b
    assert result_a == result_b
    assert cast(dict[str, list[int]], result_a)["items"] is not cast(dict[str, list[int]], result_b)["items"]

    # Mutating one must not affect the other.
    cast(dict[str, list[int]], result_a)["items"].append(4)
    result_b_again = db.get(query_b)
    assert cast(dict[str, list[int]], result_b_again)["items"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Outside-the-envelope behavior tests
# ---------------------------------------------------------------------------


def test_os_open_bypasses_untracked_read_guard(tmp_path: Path) -> None:
    """Documents that os.open() (the low-level syscall) is NOT intercepted.

    This is a known limitation of the enforcement boundary — only builtins.open,
    io.open, os.getenv, os.environ, os.listdir, os.scandir, and Path.iterdir
    are guarded.
    """
    path = tmp_path / "sample.txt"
    path.write_text("hello", encoding="utf-8")

    @query
    def read_via_os_open(db: Database) -> str:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            data = os.read(fd, 4096)
        finally:
            os.close(fd)
        return data.decode("utf-8")

    db = Database()
    # This does NOT raise — os.open is outside the guard.
    assert db.get(read_via_os_open) == "hello"


def test_report_untracked_read_forces_reexecution_on_every_request() -> None:
    @query
    def impure(db: Database) -> str:
        db.report_untracked_read("external_clock")
        return "stable"

    db = Database()

    # Three consecutive gets with no input changes at all.
    # Each must re-execute (not reuse) because the query is impure.
    for _ in range(3):
        assert db.get(impure) == "stable"
        record = _inspect_node(db, impure)
        assert record.last_recompute == "executed"
        assert record.is_untracked


def test_impure_child_prevents_parent_backdating_unless_result_unchanged() -> None:
    @query
    def impure_source(db: Database) -> int:
        db.report_untracked_read("sensor")
        return 42  # Always returns the same value.

    @query
    def consumer(db: Database) -> str:
        return f"v={impure_source(db)}"

    db = Database()
    assert db.get(consumer) == "v=42"

    # Second request: impure_source re-executes (impure), never backdates.
    # consumer re-executes because its dependency (impure_source) reports
    # changed_at == current_revision. But consumer's result is unchanged,
    # so consumer itself CAN backdate.
    assert db.get(consumer) == "v=42"

    source_node = _find_node(_inspect_node(db, consumer), "impure_source")
    assert source_node.is_untracked
    assert source_node.last_recompute == "executed"

    consumer_node = _inspect_node(db, consumer)
    assert consumer_node.last_decision == "backdated"


def test_cycle_error_does_not_corrupt_database_for_subsequent_queries() -> None:
    number = Input[int]("number")

    @query
    def cyclic(db: Database) -> int:
        return cyclic(db)

    @query
    def safe(db: Database) -> int:
        return number.read(db) * 2

    db = Database()
    db.set(number, 5)

    # Trigger cycle error.
    with pytest.raises(CycleError):
        db.get(cyclic)

    # Database must still work for non-cyclic queries.
    assert db.get(safe) == 10

    # Input updates must still propagate.
    db.set(number, 7)
    assert db.get(safe) == 14


# ---------------------------------------------------------------------------
# Soundness boundary tests — deeper coverage per kernel-contract.md
# ---------------------------------------------------------------------------


# Limitation 1 — Unintercepted ambient reads


def test_os_pipe_and_os_read_bypass_untracked_read_guard() -> None:
    """os.pipe/os.read/os.write (raw fd I/O) are NOT intercepted by the guard."""

    @query
    def communicate_via_pipe(db: Database) -> str:
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"hello from pipe")
            os.close(write_fd)
            data = os.read(read_fd, 4096)
        finally:
            os.close(read_fd)
        return data.decode("utf-8")

    db = Database()
    assert db.get(communicate_via_pipe) == "hello from pipe"


def test_mmap_bypasses_untracked_read_guard(tmp_path: Path) -> None:
    """mmap.mmap over an os.open fd is NOT intercepted by the untracked-read guard."""
    path = tmp_path / "sample.txt"
    path.write_text("hello from mmap", encoding="utf-8")

    @query
    def read_via_mmap(db: Database) -> str:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            with mmap.mmap(fd, 0, access=mmap.ACCESS_READ) as mm:
                return mm[:].decode("utf-8")
        finally:
            os.close(fd)

    db = Database()
    assert db.get(read_via_mmap) == "hello from mmap"


# Limitation 2 — Custom eq/cutoff with side effects


def test_cutoff_performing_ambient_read_does_not_crash(tmp_path: Path) -> None:
    """A cutoff= that performs ambient file I/O doesn't crash the graph.

    The backdating decision may be wrong, but the database stays functional.
    """
    control_file = tmp_path / "control.txt"
    control_file.write_text("1", encoding="utf-8")

    number = Input[int]("number")

    def side_effecting_cutoff(value: int) -> tuple[str, int]:
        content = control_file.read_text(encoding="utf-8")
        return (content, value % 2)

    @query(cutoff=side_effecting_cutoff)
    def transform(db: Database) -> int:
        return number.read(db)

    @query
    def downstream(db: Database) -> str:
        return f"v={transform(db)}"

    db = Database()
    db.set(number, 2)
    assert db.get(downstream) == "v=2"

    # Same parity — cutoff may backdate.
    db.set(number, 4)
    db.get(downstream)

    # Change control file — cutoff token changes, but the cutoff re-evaluates
    # BOTH old and new values at comparison time. Since the control file now
    # reads "2" for both, the tokens still match and the graph keeps backdating
    # to the original stale result. This is the documented limitation:
    # side-effecting cutoffs can cause incorrect but structurally safe backdating.
    control_file.write_text("2", encoding="utf-8")
    db.set(number, 6)
    result = db.get(downstream)
    assert isinstance(result, str)
    # The graph is functional — further queries still work.
    db.set(number, 8)
    assert isinstance(db.get(downstream), str)


def test_eq_raising_exception_mid_comparison_leaves_database_usable() -> None:
    """If eq= raises an exception, the error propagates but safe queries still work."""
    comparisons = {"count": 0}

    def raising_eq(left: int, right: int) -> bool:
        comparisons["count"] += 1
        if comparisons["count"] >= 2:
            raise RuntimeError("boom")
        return left == right

    number = Input[int]("number")

    @query(eq=raising_eq)
    def transform(db: Database) -> int:
        return number.read(db)

    @query
    def safe(db: Database) -> int:
        return number.read(db) * 2

    db = Database()
    db.set(number, 1)
    assert db.get(transform) == 1  # First execution — no comparison.

    db.set(number, 2)
    assert db.get(transform) == 2  # Re-executes, comparison #1 (doesn't raise).

    # Third change triggers comparison #2 which raises.
    db.set(number, 3)
    with pytest.raises(RuntimeError, match="boom"):
        db.get(transform)

    # Safe queries must still work.
    assert db.get(safe) == 6
    db.set(number, 4)
    assert db.get(safe) == 8


# Limitation 3 — Mutation in fast mode


def test_fast_mode_does_not_detect_return_value_mutation_unlike_checked() -> None:
    """Fast mode allows silent mutation; checked mode detects it."""
    trigger = Input[int]("trigger")

    @query
    def produce(db: Database) -> dict[str, list[int]]:
        trigger.read(db)
        return {"items": [1, 2, 3]}

    @query
    def mutate_and_read(db: Database) -> int:
        data = produce(db)
        data["items"].append(4)
        return len(data["items"])

    # Fast mode: no error, mutation silently allowed.
    fast_db = Database(mode="fast")
    fast_db.set(trigger, 1)
    assert fast_db.get(mutate_and_read) == 4

    # Checked mode: mutation detected.
    checked_db = Database(mode="checked")
    checked_db.set(trigger, 1)
    with pytest.raises(MutationError):
        checked_db.get(mutate_and_read)


def test_fast_mode_frozen_snapshot_safe_despite_mutation() -> None:
    """After mutation in fast mode, the frozen snapshot is still intact."""
    trigger = Input[int]("trigger")

    @query
    def produce(db: Database) -> dict[str, list[int]]:
        trigger.read(db)
        return {"items": [1, 2, 3]}

    db = Database(mode="fast")
    db.set(trigger, 1)

    first = db.get(produce)
    first["items"].append(4)

    # Fresh get returns uncontaminated copy from the frozen snapshot.
    second = db.get(produce)
    assert second["items"] == [1, 2, 3]


# Limitation 5 — Cycle-adjacent partial state


def test_indirect_cycle_three_node_chain_recovery() -> None:
    """A->B->C->A cycle raises CycleError; safe queries work after."""
    number = Input[int]("number")

    @query
    def query_a(db: Database) -> int:
        return query_b(db)

    @query
    def query_b(db: Database) -> int:
        return query_c(db)

    @query
    def query_c(db: Database) -> int:
        return query_a(db)

    @query
    def safe(db: Database) -> int:
        return number.read(db) * 2

    db = Database()
    db.set(number, 5)

    with pytest.raises(CycleError):
        db.get(query_a)

    assert db.get(safe) == 10
    db.set(number, 7)
    assert db.get(safe) == 14


def test_cycle_and_safe_query_sharing_dependency() -> None:
    """Cyclic and safe queries share an Input; cycle doesn't block the safe path."""
    shared = Input[int]("shared")

    @query
    def cyclic(db: Database) -> int:
        shared.read(db)
        return cyclic(db)

    @query
    def safe(db: Database) -> int:
        return shared.read(db) * 3

    db = Database()
    db.set(shared, 4)

    with pytest.raises(CycleError):
        db.get(cyclic)

    assert db.get(safe) == 12

    db.set(shared, 5)
    assert db.get(safe) == 15
    assert _inspect_node(db, safe).last_decision == "executed"


# Limitation 6 — LRU eviction under active dependencies


def test_very_low_max_query_nodes_causes_reexecution_cascade() -> None:
    """max_query_nodes=1 with a 3-node chain — correctness via full re-execution."""
    number = Input[int]("number")

    @query
    def step1(db: Database) -> int:
        return number.read(db)

    @query
    def step2(db: Database) -> int:
        return step1(db) + 10

    @query
    def step3(db: Database) -> int:
        return step2(db) + 100

    db = Database(max_query_nodes=1)
    db.set(number, 1)

    assert db.get(step3) == 111

    # Second request — eviction forces re-execution from scratch.
    assert db.get(step3) == 111

    # Input change still propagates correctly through the chain.
    db.set(number, 2)
    assert db.get(step3) == 112

    # Verify from-scratch consistency.
    fresh = Database()
    fresh.set(number, 2)
    assert fresh.get(step3) == db.get(step3)


def test_inputs_and_resources_survive_eviction(tmp_path: Path) -> None:
    """LRU eviction only affects query nodes; inputs and resources remain resident."""
    number = Input[int]("number")
    files = FileResource()

    sample = tmp_path / "data.txt"
    sample.write_text("content", encoding="utf-8")

    @query
    def read_input(db: Database) -> int:
        return number.read(db)

    @query
    def read_file(db: Database) -> str:
        return files.read(db, str(sample))

    @query
    def filler_a(db: Database) -> str:
        return "a"

    @query
    def filler_b(db: Database) -> str:
        return "b"

    db = Database(max_query_nodes=1)
    db.set(number, 42)

    assert db.get(read_input) == 42
    assert db.get(read_file) == "content"

    # Evict all query nodes by requesting fillers.
    assert db.get(filler_a) == "a"
    assert db.get(filler_b) == "b"

    # Input and resource nodes survive eviction.
    input_keys = [k for k in db._records if k.kind == "input"]
    resource_keys = [k for k in db._records if k.kind == "resource"]
    assert len(input_keys) >= 1
    assert len(resource_keys) >= 1

    # Queries still return correct results after re-execution.
    assert db.get(read_input) == 42
    assert db.get(read_file) == "content"


# ---------------------------------------------------------------------------
# Database statistics
# ---------------------------------------------------------------------------


def test_statistics_returns_frozen_snapshot() -> None:
    db = Database()
    stats = db.statistics()
    assert isinstance(stats, DatabaseStatistics)
    assert all(isinstance(getattr(stats, f.name), int) for f in stats.__dataclass_fields__.values())
    with pytest.raises(AttributeError):
        stats.node_count = 99  # type: ignore[misc]


def test_statistics_counts_input_operations() -> None:
    number = Input[int]("number")
    db = Database()

    db.set(number, 1)
    stats = db.statistics()
    assert stats.input_sets == 1
    assert stats.input_equal_ignores == 0

    db.set(number, 1)  # equal value
    stats = db.statistics()
    assert stats.input_sets == 1
    assert stats.input_equal_ignores == 1

    db.set(number, 2)  # changed value
    stats = db.statistics()
    assert stats.input_sets == 2
    assert stats.input_equal_ignores == 1


def test_statistics_counts_query_operations() -> None:
    number = Input[int]("number")

    @query
    def double(db: Database) -> int:
        return number.read(db) * 2

    db = Database()
    db.set(number, 5)

    db.get(double)
    stats = db.statistics()
    assert stats.query_executions == 1
    assert stats.query_reuses == 0

    db.get(double)  # same state -> reused
    stats = db.statistics()
    assert stats.query_reuses == 1
    assert stats.query_executions == 1

    db.set(number, 10)
    db.get(double)  # dependency changed -> re-executed
    stats = db.statistics()
    assert stats.query_executions == 2


def test_statistics_counts_backdating() -> None:
    number = Input[int]("number")

    @query
    def parity(db: Database) -> str:
        return "even" if number.read(db) % 2 == 0 else "odd"

    db = Database()
    db.set(number, 2)
    db.get(parity)  # initial execution: "even"

    db.set(number, 4)  # still even -> backdate
    db.get(parity)
    stats = db.statistics()
    assert stats.query_backdates == 1


def test_statistics_counts_evictions() -> None:
    @query
    def a(db: Database) -> str:
        return "a"

    @query
    def b(db: Database) -> str:
        return "b"

    @query
    def c(db: Database) -> str:
        return "c"

    db = Database(max_query_nodes=2)
    db.get(a)
    db.get(b)
    db.get(c)  # triggers eviction
    stats = db.statistics()
    assert stats.evictions >= 1


def test_statistics_counts_resource_operations(tmp_path: Path) -> None:
    file_path = tmp_path / "data.txt"
    file_path.write_text("hello")

    resource = FileResource()

    @query
    def read_file(db: Database) -> str:
        return resource.read(db, str(file_path))

    db = Database()
    db.get(read_file)
    stats = db.statistics()
    assert stats.resource_loads == 1
    assert stats.resource_probe_hits == 0

    db.get(read_file)  # probe unchanged -> hit
    stats = db.statistics()
    assert stats.resource_probe_hits == 1
    assert stats.resource_loads == 1

    file_path.write_text("world")
    db.get(read_file)  # probe changed -> reload
    stats = db.statistics()
    assert stats.resource_loads == 2


def test_reset_statistics_zeroes_counters() -> None:
    number = Input[int]("number")

    @query
    def double(db: Database) -> int:
        return number.read(db) * 2

    db = Database()
    db.set(number, 5)
    db.get(double)
    db.get(double)

    stats_before = db.statistics()
    assert stats_before.query_executions > 0
    assert stats_before.total_requests > 0

    db.reset_statistics()
    stats_after = db.statistics()
    assert stats_after.query_executions == 0
    assert stats_after.query_reuses == 0
    assert stats_after.input_sets == 0
    # Node counts and total_requests are structural, not reset
    assert stats_after.total_requests == stats_before.total_requests
    assert stats_after.node_count == stats_before.node_count


def test_statistics_node_counts_match_records(tmp_path: Path) -> None:
    number = Input[int]("number")
    file_path = tmp_path / "data.txt"
    file_path.write_text("content")
    resource = FileResource()

    @query
    def compute(db: Database) -> str:
        val = number.read(db)
        text = resource.read(db, str(file_path))
        return f"{val}:{text}"

    db = Database()
    db.set(number, 42)
    db.get(compute)

    stats = db.statistics()
    assert stats.node_count == stats.input_count + stats.query_count + stats.resource_count
    assert stats.input_count >= 1
    assert stats.query_count >= 1
    assert stats.resource_count >= 1


# ---------------------------------------------------------------------------
# Dependency graph export
# ---------------------------------------------------------------------------


def test_dependency_graph_empty_database() -> None:
    db = Database()
    graph = db.dependency_graph()
    assert graph == ()


def test_dependency_graph_inputs_and_queries() -> None:
    x = Input[int]("x")
    y = Input[int]("y")

    @query
    def add(db: Database) -> int:
        return x.read(db) + y.read(db)

    db = Database()
    db.set(x, 1)
    db.set(y, 2)
    db.get(add)

    graph = db.dependency_graph()

    add_nodes = [n for n in graph if n.kind == "query"]
    assert len(add_nodes) == 1
    add_node = add_nodes[0]
    assert add_node.last_decision == "executed"
    assert not add_node.is_untracked
    assert len(add_node.dependency_labels) == 2

    input_nodes = [n for n in graph if n.kind == "input"]
    assert len(input_nodes) == 2
    for node in input_nodes:
        assert node.dependency_labels == ()


def test_dependency_graph_with_resource(tmp_path: Path) -> None:
    file_path = tmp_path / "data.txt"
    file_path.write_text("hello")
    resource = FileResource()

    @query
    def read_file(db: Database) -> str:
        return resource.read(db, str(file_path))

    db = Database()
    db.get(read_file)

    graph = db.dependency_graph()
    kinds = {n.kind for n in graph}
    assert "query" in kinds
    assert "resource" in kinds


def test_dependency_graph_diamond_structure() -> None:
    x = Input[int]("x")

    @query
    def left(db: Database) -> int:
        return x.read(db) + 1

    @query
    def right(db: Database) -> int:
        return x.read(db) * 2

    @query
    def combine(db: Database) -> int:
        return db.get(left) + db.get(right)

    db = Database()
    db.set(x, 5)
    db.get(combine)

    graph = db.dependency_graph()

    combine_nodes = [n for n in graph if n.kind == "query" and len(n.dependency_labels) == 2
                     and all(d not in [n2.label for n2 in graph if n2.kind == "input"] for d in n.dependency_labels)]
    assert len(combine_nodes) == 1
    combine_node = combine_nodes[0]

    input_label = [n.label for n in graph if n.kind == "input"][0]
    mid_nodes = [n for n in graph if n.kind == "query" and input_label in n.dependency_labels]
    assert len(mid_nodes) == 2
    mid_labels = {n.label for n in mid_nodes}
    assert set(combine_node.dependency_labels) == mid_labels


def test_dependency_graph_untracked_node() -> None:
    @query
    def impure(db: Database) -> str:
        db.report_untracked_read("test reason")
        return "value"

    db = Database()
    db.get(impure)

    graph = db.dependency_graph()
    query_nodes = [n for n in graph if n.kind == "query"]
    assert len(query_nodes) == 1
    assert query_nodes[0].is_untracked


def test_dependency_graph_returns_frozen_nodes() -> None:
    x = Input[int]("x")
    db = Database()
    db.set(x, 1)

    graph = db.dependency_graph()
    assert isinstance(graph, tuple)
    assert all(isinstance(n, DependencyGraphNode) for n in graph)


# ---------------------------------------------------------------------------
# Batch invalidation
# ---------------------------------------------------------------------------


def test_set_many_empty_iterable_is_noop() -> None:
    db = Database()
    rev_before = db.revision
    db.set_many([])
    assert db.revision == rev_before


def test_set_many_single_changed_input() -> None:
    x = Input[int]("x")
    db = Database()
    db.set_many([(x, 42)])
    assert db.revision == 1

    @query
    def read_x(db: Database) -> int:
        return x.read(db)

    assert db.get(read_x) == 42


def test_set_many_multiple_changed_inputs_single_revision() -> None:
    x = Input[int]("x")
    y = Input[int]("y")
    db = Database()
    db.set_many([(x, 1), (y, 2)])
    assert db.revision == 1


def test_set_many_no_change_no_revision_bump() -> None:
    x = Input[int]("x")
    db = Database()
    db.set(x, 10)
    rev_before = db.revision
    db.set_many([(x, 10)])
    assert db.revision == rev_before


def test_set_many_mixed_equal_and_changed() -> None:
    x = Input[int]("x")
    y = Input[int]("y")
    db = Database()
    db.set(x, 1)
    db.set(y, 2)
    rev_before = db.revision
    db.set_many([(x, 1), (y, 99)])
    assert db.revision == rev_before + 1
    stats = db.statistics()
    assert stats.input_equal_ignores >= 1


def test_set_many_downstream_query_sees_all_updates() -> None:
    x = Input[int]("x")
    y = Input[int]("y")

    @query
    def add(db: Database) -> int:
        return x.read(db) + y.read(db)

    db = Database()
    db.set_many([(x, 10), (y, 20)])
    assert db.get(add) == 30

    db.set_many([(x, 100), (y, 200)])
    assert db.get(add) == 300


def test_set_many_stats_counters() -> None:
    x = Input[int]("x")
    y = Input[int]("y")
    db = Database()
    db.set(x, 1)
    db.set(y, 2)
    db.reset_statistics()

    db.set_many([(x, 1), (y, 99)])
    stats = db.statistics()
    assert stats.input_sets >= 1
    assert stats.input_equal_ignores >= 1


def test_set_many_rejects_non_input() -> None:
    db = Database()
    with pytest.raises(TypeError, match="set_many"):
        db.set_many([("not_an_input", 1)])


# ---------------------------------------------------------------------------
# Query profiling
# ---------------------------------------------------------------------------


def test_query_profile_empty_on_fresh_database() -> None:
    db = Database()
    assert db.query_profile() == ()


def test_query_profile_after_execution() -> None:
    x = Input[int]("x")

    @query
    def double(db: Database) -> int:
        return x.read(db) * 2

    db = Database()
    db.set(x, 5)
    db.get(double)

    profiles = db.query_profile()
    assert len(profiles) == 1
    p = profiles[0]
    assert isinstance(p, QueryProfile)
    assert p.execution_count == 1
    assert p.total_ns > 0
    assert p.mean_ns == p.total_ns


def test_query_profile_counts_executions_not_reuses() -> None:
    x = Input[int]("x")

    @query
    def read_x(db: Database) -> int:
        return x.read(db)

    db = Database()
    db.set(x, 1)
    db.get(read_x)
    db.get(read_x)  # reuse, not re-execution

    profiles = db.query_profile()
    assert len(profiles) == 1
    assert profiles[0].execution_count == 1

    db.set(x, 2)
    db.get(read_x)  # re-execution

    profiles = db.query_profile()
    assert profiles[0].execution_count == 2


def test_query_profile_multiple_queries() -> None:
    x = Input[int]("x")

    @query
    def alpha(db: Database) -> int:
        return x.read(db) + 1

    @query
    def beta(db: Database) -> int:
        return x.read(db) * 2

    db = Database()
    db.set(x, 1)
    db.get(alpha)
    db.get(beta)

    profiles = db.query_profile()
    assert len(profiles) == 2
    labels = {p.query_label for p in profiles}
    assert len(labels) == 2


def test_reset_statistics_clears_query_timings() -> None:
    x = Input[int]("x")

    @query
    def compute(db: Database) -> int:
        return x.read(db)

    db = Database()
    db.set(x, 1)
    db.get(compute)
    assert len(db.query_profile()) == 1

    db.reset_statistics()
    assert db.query_profile() == ()
