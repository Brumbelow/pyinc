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
    FileStatResource,
    FrozenDict,
    InspectionNode,
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


def _inspect_node(db: Database, query_fn: object, *args: object, **kwargs: object) -> InspectionNode:
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
    monkeypatch.setenv("PYFOUNDINC_DIRECT_ENV", "value")

    @query
    def read_env(db: Database) -> str | None:
        return os.getenv("PYFOUNDINC_DIRECT_ENV")

    with pytest.raises(UntrackedReadError):
        Database().get(read_env)


def test_os_environ_access_is_rejected_inside_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYFOUNDINC_DIRECT_ENV", "value")

    @query
    def read_env_mapping(db: Database) -> str:
        return os.environ["PYFOUNDINC_DIRECT_ENV"]

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
    monkeypatch.setenv("PYFOUNDINC_TRACKED_ENV", "value")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("alpha", encoding="utf-8")

    @query
    def read_tracked(db: Database, dirname: str) -> tuple[str | None, tuple[str, ...]]:
        return env.read(db, "PYFOUNDINC_TRACKED_ENV"), directories.read(db, dirname)

    db = Database()
    assert db.get(read_tracked, str(workspace)) == ("value", ("a.txt",))


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
    first = db.get(read_stat, str(path))
    assert first["exists"] is True
    assert first["size"] == 5

    path.write_text("bravo!", encoding="utf-8")
    second = db.get(read_stat, str(path))
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
