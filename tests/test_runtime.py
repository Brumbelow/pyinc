from __future__ import annotations

import hashlib
import math
import mmap
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest

from pyinc import (
    BinaryFileResource,
    CycleError,
    Database,
    DatabaseStatistics,
    DependencyGraphNode,
    DirectoryResource,
    EnvResource,
    FileResource,
    FileStatResource,
    FrozenAdapterValue,
    FrozenDict,
    FrozenGraph,
    FrozenList,
    InMemoryArtifactStore,
    Input,
    InspectionNode,
    MutationError,
    QueryChangeEvent,
    QueryProfile,
    ResolvedPathResource,
    Resource,
    Subscription,
    UnsupportedValueError,
    UntrackedReadError,
    ValueAdapter,
    freeze,
    query,
    serialize_snapshot,
)
from pyinc.runtime import _MISSING_SNAPSHOT
from pyinc.value import fingerprint_snapshot

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


def _outcome(call: Callable[[], Any]) -> tuple[Any, ...]:
    try:
        return ("value", call())
    except Exception as exc:
        return (type(exc).__name__, str(exc))


@dataclass(frozen=True)
class _DeniableFileResource(Resource[str, str, tuple[str, ...]]):
    """A file resource whose probe and load both raise while a marker exists.

    A file swapped for a directory used to be the vehicle for a failure neither
    the probe nor the load survives. A directory reads as a missing file now --
    a state, and so recordable -- so a scenario about an *unrecordable* failure
    needs a denial the boundary genuinely cannot absorb, which is what a
    permission error is. The marker lives beside the key on disk because a
    query's capture set may not hold mutable state.
    """

    def _denied(self, path: str) -> bool:
        return Path(path + ".denied").exists()

    def label(self, path: str) -> str:
        return f"deniable[{path}]"

    def probe(self, path: str) -> tuple[str, ...]:
        if self._denied(path):
            raise PermissionError(path)
        try:
            return ("present", hashlib.sha256(Path(path).read_bytes()).hexdigest())
        except FileNotFoundError:
            return ("missing",)

    def load(self, db: Database, path: str) -> str:
        if self._denied(path):
            raise PermissionError(path)
        return Path(path).read_text(encoding="utf-8")


def _deny(path: Path) -> None:
    Path(str(path) + ".denied").write_text("", encoding="utf-8")


def _allow(path: Path) -> None:
    Path(str(path) + ".denied").unlink()


@pytest.mark.parametrize("limit", [0, -1, 1.5, float("nan"), True, "1"])
def test_max_query_nodes_must_be_a_positive_integer(limit: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        Database(max_query_nodes=cast(Any, limit))


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
    with pytest.raises(
        UnsupportedValueError, match="Cutoff functions must return snapshot-safe values"
    ):
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


def test_inspect_fresh_triggers_reverification_after_input_change() -> None:
    number = Input[int]("number")

    @query
    def double(db: Database) -> int:
        return number.read(db) * 2

    db = Database()
    db.set(number, 3)
    assert db.get(double) == 6

    db.set(number, 5)

    stale = db.inspect(double)
    assert stale.last_decision == "executed"
    assert stale.verified_at == 1

    fresh = db.inspect_fresh(double)
    assert fresh.last_decision == "executed"
    assert fresh.verified_at > stale.verified_at
    assert db.get(double) == 10


def test_inspect_fresh_on_cold_cache_returns_same_shape_as_inspect() -> None:
    number = Input[int]("number")

    @query
    def double(db: Database) -> int:
        return number.read(db) * 2

    db_a = Database()
    db_a.set(number, 7)
    tree_a = db_a.inspect(double)

    db_b = Database()
    db_b.set(number, 7)
    tree_b = db_b.inspect_fresh(double)

    assert tree_a.label == tree_b.label
    assert tree_a.kind == tree_b.kind
    assert tree_a.last_decision == tree_b.last_decision == "executed"
    assert {dep.label for dep in tree_a.dependencies} == {dep.label for dep in tree_b.dependencies}


def test_inspect_fresh_rejects_non_query() -> None:
    db = Database()
    with pytest.raises(TypeError):
        db.inspect_fresh(cast(Any, lambda db_: None))


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


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_query_call_materializes_direct_mutable_argument(mode: str) -> None:
    @query
    def first(db: Database, values: list[int]) -> int:
        return values[0]

    assert Database(mode=mode).get(first, [7, 8]) == 7


def test_tree_query_call_snapshot_retains_flat_digest_shape() -> None:
    @query
    def combine(db: Database, value: int, *, enabled: bool) -> int:
        return value if enabled else 0

    db = Database()
    key, snapshot = db._query_key(combine, (7,), {"enabled": True})
    previous_shape = (freeze((7,)), freeze({"enabled": True}))

    assert snapshot == previous_shape
    assert key.args_digest == fingerprint_snapshot(previous_shape)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_query_call_preserves_shared_positional_identity(mode: str) -> None:
    @query
    def same_object(db: Database, left: list[int], right: list[int]) -> bool:
        return left is right

    shared = [1]
    db = Database(mode=mode)
    key, snapshot = db._query_key(same_object, (shared, shared), {})

    assert isinstance(snapshot, FrozenGraph)
    assert key.args_digest == fingerprint_snapshot(snapshot)
    assert db.get(same_object, shared, shared) is True


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_query_call_preserves_self_cycle(mode: str) -> None:
    @query
    def sees_cycle(db: Database, value: list[Any]) -> bool:
        return value[0] is value

    cyclic: list[Any] = []
    cyclic.append(cyclic)

    assert Database(mode=mode).get(sees_cycle, cyclic) is True


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_query_call_preserves_positional_keyword_alias(mode: str) -> None:
    @query
    def same_object(db: Database, positional: list[int], *, keyword: list[int]) -> bool:
        return positional is keyword

    shared = [1]

    assert Database(mode=mode).get(same_object, shared, keyword=shared) is True


def test_strict_query_call_exposes_safe_cyclic_view() -> None:
    @query
    def inspect_cycle(db: Database, value: list[Any]) -> tuple[bool, bool, bool]:
        return (
            isinstance(value, FrozenList),
            value[0] is value,
            hasattr(value, "append"),
        )

    cyclic: list[Any] = []
    cyclic.append(cyclic)

    assert Database(mode="strict").get(inspect_cycle, cyclic) == (
        True,
        True,
        False,
    )


def test_strict_get_exposes_cyclic_result_as_safe_view() -> None:
    @query
    def make_cycle(db: Database) -> list[Any]:
        value: list[Any] = []
        value.append(value)
        return value

    result = Database(mode="strict").get(make_cycle)

    assert isinstance(result, FrozenList)
    assert len(result) == 1
    (element,) = result
    assert element is result
    assert not hasattr(result, "append")


def test_strict_get_exposes_shared_acyclic_result_as_usable_containers() -> None:
    @query
    def make_diamond(db: Database) -> list[list[int]]:
        inner = [1, 2]
        return [inner, inner]

    result = Database(mode="strict").get(make_diamond)

    assert isinstance(result, FrozenList)
    assert len(result) == 2
    left, right = result
    assert isinstance(left, FrozenList)
    assert left is right
    assert list(left) == [1, 2]


def test_strict_dependent_query_computes_over_cyclic_result_from_db_get() -> None:
    @query
    def make_cycle(db: Database) -> list[Any]:
        value: list[Any] = []
        value.append(value)
        return value

    @query
    def measure_cycle(db: Database) -> tuple[int, bool]:
        value = db.get(make_cycle)
        return (len(value), value[0] is value)

    assert Database(mode="strict").get(measure_cycle) == (1, True)


def test_strict_read_input_exposes_cyclic_value_as_safe_view() -> None:
    payload = Input[Any]("payload")
    cyclic: list[Any] = []
    cyclic.append(cyclic)

    db = Database(mode="strict")
    db.set(payload, cyclic)
    value = payload.read(db)

    assert isinstance(value, FrozenList)
    assert len(value) == 1
    assert value[0] is value


def test_strict_set_round_trips_cyclic_view_from_db_get() -> None:
    payload = Input[Any]("payload")

    @query
    def make_cycle(db: Database) -> list[Any]:
        value: list[Any] = []
        value.append(value)
        return value

    @query
    def measure_payload(db: Database) -> tuple[int, bool]:
        value = payload.read(db)
        return (len(value), value[0] is value)

    db = Database(mode="strict")
    view = db.get(make_cycle)

    db.set(payload, view)

    assert db.get(measure_payload) == (1, True)
    # From-scratch consistency: re-freezing the view lands the exact snapshot
    # the raw cyclic structure produces.
    expected: list[Any] = []
    expected.append(expected)
    assert fingerprint_snapshot(freeze(view)) == fingerprint_snapshot(freeze(expected))


def test_strict_query_argument_accepts_cyclic_view_from_db_get() -> None:
    @query
    def make_cycle(db: Database) -> list[Any]:
        value: list[Any] = []
        value.append(value)
        return value

    @query
    def inspect_value(db: Database, value: list[Any]) -> tuple[bool, int, bool]:
        return (isinstance(value, FrozenList), len(value), value[0] is value)

    db = Database(mode="strict")
    view = db.get(make_cycle)

    assert db.get(inspect_value, view) == (True, 1, True)

    # The view addresses the same cache node as the raw cyclic structure.
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    key_from_view, _ = db._query_key(inspect_value, (view,), {})
    key_from_raw, _ = db._query_key(inspect_value, (cyclic,), {})
    assert key_from_view.args_digest == key_from_raw.args_digest


def test_strict_set_round_trips_shared_diamond_view_preserving_sharing() -> None:
    payload = Input[Any]("payload")

    @query
    def make_diamond(db: Database) -> list[list[int]]:
        inner = [1, 2]
        return [inner, inner]

    db = Database(mode="strict")
    view = db.get(make_diamond)

    db.set(payload, view)
    value = payload.read(db)

    assert isinstance(value, FrozenList)
    left, right = value
    assert left is right
    assert list(left) == [1, 2]
    # From-scratch consistency: re-freezing the view lands the exact snapshot
    # the raw shared structure produces.
    inner = [1, 2]
    assert fingerprint_snapshot(freeze(view)) == fingerprint_snapshot(freeze([inner, inner]))


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

    # Two independent dicts at the boundary — each thaws to its own owned copy
    # in fast mode, so a mutation through `left` cannot reach `right`.
    db = Database(mode="fast")
    db.set(payload, ({"x": 1}, {"x": 1}))

    assert db.get(mutate_left) == 1
    assert db.get(read_right) == 1


def test_fast_mode_preserves_shared_identity_across_boundary() -> None:
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

    # In v2.0.0 the value membrane preserves shared identity across cached
    # boundaries. Two slots pointing at the same dict thaw to the same dict, so
    # an in-query mutation through one alias is observable through the other —
    # the kernel's stored snapshot remains uncorrupted.
    shared = {"x": 1}
    db = Database(mode="fast")
    db.set(payload, (shared, shared))

    assert db.get(mutate_left) == 99
    # A separate query thaws fresh — the prior in-query mutation is gone.
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


def test_os_environ_access_is_rejected_inside_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_resource_reads_are_allowed_inside_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_query_nested_inside_resource_hook_cannot_read_raw_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variable = "PYINC_NESTED_RESOURCE_ENV"
    monkeypatch.setenv(variable, "value")

    @query(key="raw-resource-leaf")
    def raw_leaf(db: Database) -> str | None:
        return os.getenv(variable)

    class NestedQueryResource:
        def identity(self) -> tuple[str]:
            return ("nested-query-resource",)

        def read(self, db: Database, name: str) -> str | None:
            return cast(str | None, db.read_resource(self, name))

        def label(self, name: str) -> str:
            return f"nested-query[{name}]"

        def probe(self, name: str) -> str | None:
            return os.getenv(name)

        def load(self, db: Database, name: str) -> str | None:
            return db.get(raw_leaf)

    resource = NestedQueryResource()

    @query(key="nested-resource-root")
    def root(db: Database) -> str | None:
        return resource.read(db, variable)

    with pytest.raises(UntrackedReadError):
        Database().get(root)


def test_failed_resource_reads_do_not_leave_dangling_dependencies(
    tmp_path: Path,
) -> None:
    deniable = _DeniableFileResource()
    path = tmp_path / "sample.py"
    path.write_text("value = 1\n", encoding="utf-8")
    _deny(path)

    @query
    def classify(db: Database, candidate: str) -> str:
        try:
            deniable.read(db, candidate)
        except PermissionError:
            return "denied"
        return "readable"

    db = Database()
    assert db.get(classify, str(path)) == "denied"
    inspection = _inspect_node(db, classify, str(path))
    assert inspection.last_decision == "executed"
    # The probe raises rather than reporting a state, so the failure cannot be
    # recorded and no edge is published.
    assert inspection.dependencies == ()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_appearing_resource_invalidates_the_query_that_handled_its_absence(
    mode: str,
    tmp_path: Path,
) -> None:
    files = FileResource()
    path = tmp_path / "optional.txt"

    @query
    def read_optional(db: Database, filename: str) -> str:
        try:
            return files.read(db, filename)
        except FileNotFoundError:
            return "<default>"

    db = Database(mode=mode)
    assert db.get(read_optional, str(path)) == "<default>"
    inspection = _inspect_node(db, read_optional, str(path))
    failed = _find_node(inspection, "file[")
    assert failed.last_decision == "failed"
    assert "FileNotFoundError" in failed.reason
    assert db.statistics().resource_count == 1

    path.write_text("hello", encoding="utf-8")
    assert db.get(read_optional, str(path)) == Database(mode=mode).get(read_optional, str(path))
    assert db.get(read_optional, str(path)) == "hello"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_disappearing_resource_raises_inside_the_query_body(mode: str, tmp_path: Path) -> None:
    files = FileResource()
    path = tmp_path / "optional.txt"
    path.write_text("hello", encoding="utf-8")

    @query
    def read_optional(db: Database, filename: str) -> str:
        try:
            return files.read(db, filename)
        except FileNotFoundError:
            return "<default>"

    db = Database(mode=mode)
    assert db.get(read_optional, str(path)) == "hello"

    path.unlink()
    assert db.get(read_optional, str(path)) == Database(mode=mode).get(read_optional, str(path))
    assert db.get(read_optional, str(path)) == "<default>"


def test_unchanged_failing_resource_probe_keeps_dependents_green(tmp_path: Path) -> None:
    files = FileResource()
    path = tmp_path / "missing.txt"

    @query
    def read_optional(db: Database, filename: str) -> str:
        try:
            return files.read(db, filename)
        except FileNotFoundError:
            return "<default>"

    @query
    def shout(db: Database, filename: str) -> str:
        return read_optional(db, filename).upper()

    db = Database()
    assert db.get(shout, str(path)) == "<DEFAULT>"
    revision = db.revision
    executions = db.statistics().query_executions

    for _ in range(3):
        assert db.get(shout, str(path)) == "<DEFAULT>"

    assert db.revision == revision
    assert db.statistics().query_executions == executions
    inspection = _inspect_node(db, shout, str(path))
    assert inspection.last_decision == "reused"
    assert _find_node(inspection, "read_optional").last_decision == "reused"
    failed = _find_node(inspection, "file[")
    assert failed.last_decision == "failed"
    assert failed.changed_at == revision


def test_resource_create_delete_recreate_cycles_track_a_fresh_database(tmp_path: Path) -> None:
    files = FileResource()
    path = tmp_path / "toggle.txt"

    @query
    def read_optional(db: Database, filename: str) -> str:
        try:
            return files.read(db, filename)
        except FileNotFoundError:
            return "<default>"

    db = Database()
    for content in (None, "alpha", None, "beta", "beta", None, "alpha"):
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(content, encoding="utf-8")
        expected = "<default>" if content is None else content
        assert Database().get(read_optional, str(path)) == expected
        assert db.get(read_optional, str(path)) == expected


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_file_replaced_by_a_directory_matches_a_fresh_database(mode: str, tmp_path: Path) -> None:
    files = FileResource()
    path = tmp_path / "o.txt"
    path.write_text("hello", encoding="utf-8")

    @query
    def read_optional(db: Database, filename: str) -> str:
        try:
            return files.read(db, filename)
        except FileNotFoundError:
            return "<default>"

    db = Database(mode=mode)
    assert db.get(read_optional, str(path)) == "hello"

    path.unlink()
    path.mkdir()
    # A directory is not a readable regular file and never becomes one by being
    # read again, so the probe reports it as absent rather than raising: a probe
    # that raises retires the record and hands the caller an error a fresh
    # database, which sees the same directory, would have to raise too.
    fresh = _outcome(lambda: Database(mode=mode).get(read_optional, str(path)))
    assert fresh == ("value", "<default>")
    assert _outcome(lambda: db.get(read_optional, str(path))) == fresh


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_directory_replaced_by_a_file_matches_a_fresh_database(mode: str, tmp_path: Path) -> None:
    directories = DirectoryResource()
    path = tmp_path / "listing"
    path.mkdir()
    (path / "a.txt").write_text("a", encoding="utf-8")

    @query
    def names(db: Database, dirname: str) -> tuple[str, ...]:
        return directories.read(db, dirname)

    db = Database(mode=mode)
    assert db.get(names, str(path)) == ("a.txt",)

    (path / "a.txt").unlink()
    path.rmdir()
    path.write_text("not a directory", encoding="utf-8")
    # The read still tells the caller a file is not a directory; the probe
    # behind it reports the absent listing instead of raising, so the failure
    # is recorded rather than retiring the node it was checking.
    fresh = _outcome(lambda: Database(mode=mode).get(names, str(path)))
    assert fresh[0] == "NotADirectoryError"
    assert _outcome(lambda: db.get(names, str(path))) == fresh


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_directory_restored_after_a_kind_swap_matches_a_fresh_database(
    mode: str,
    tmp_path: Path,
) -> None:
    directories = DirectoryResource()
    path = tmp_path / "listing"
    path.mkdir()
    (path / "a.txt").write_text("a", encoding="utf-8")

    @query
    def names(db: Database, dirname: str) -> tuple[str, ...]:
        try:
            return directories.read(db, dirname)
        except NotADirectoryError:
            return ("<caught>",)

    db = Database(mode=mode)
    assert db.get(names, str(path)) == ("a.txt",)

    (path / "a.txt").unlink()
    path.rmdir()
    path.write_text("not a directory", encoding="utf-8")
    assert db.get(names, str(path)) == ("<caught>",)

    # The world returns to exactly the state the resource record described
    # before the swap (a branch switch, an undo), so the round trip has to land
    # the original answer and not the one the intervening kind held.
    path.unlink()
    path.mkdir()
    (path / "a.txt").write_text("a", encoding="utf-8")
    assert db.get(names, str(path)) == Database(mode=mode).get(names, str(path)) == ("a.txt",)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_missing_file_replaced_by_a_directory_matches_a_fresh_database(
    mode: str,
    tmp_path: Path,
) -> None:
    files = FileResource()
    path = tmp_path / "optional.txt"

    @query
    def read_optional(db: Database, filename: str) -> str:
        try:
            return files.read(db, filename)
        except FileNotFoundError:
            return "<default>"

    db = Database(mode=mode)
    assert db.get(read_optional, str(path)) == "<default>"

    # The failure record left by the absent file still describes the world: a
    # directory reads as a missing file, so nothing about the answer moves.
    path.mkdir()
    fresh = _outcome(lambda: Database(mode=mode).get(read_optional, str(path)))
    assert fresh == ("value", "<default>")
    assert _outcome(lambda: db.get(read_optional, str(path))) == fresh


# A probe has to be total: it answers for every path it is handed, so that a
# warm database re-probing a path it already knows lands where a fresh one
# reading the same world lands. The three ways a path stops naming a readable
# regular file -- absent, a directory, a file somewhere in its parent chain --
# are one answer. A permission denial is not among them; that is a genuine
# failure, and it keeps raising.


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("kind", ["directory", "parent-is-a-file"])
def test_file_resource_reads_an_unreadable_kind_as_a_missing_file(
    mode: str, kind: str, tmp_path: Path
) -> None:
    files = FileResource()
    binaries = BinaryFileResource()
    holder = tmp_path / "holder"
    holder.mkdir()
    path = holder / "thing.txt"
    path.write_text("hello", encoding="utf-8")

    @query
    def read_optional(db: Database, filename: str) -> str:
        try:
            return files.read(db, filename)
        except FileNotFoundError:
            return "<default>"

    @query
    def read_optional_bytes(db: Database, filename: str) -> str:
        try:
            return binaries.read(db, filename).decode("utf-8")
        except FileNotFoundError:
            return "<default>"

    db = Database(mode=mode)
    assert db.get(read_optional, str(path)) == "hello"
    assert db.get(read_optional_bytes, str(path)) == "hello"

    path.unlink()
    if kind == "directory":
        path.mkdir()
    else:
        holder.rmdir()
        holder.write_text("now a file", encoding="utf-8")

    def read_with(target: Database, node: Any) -> tuple[Any, ...]:
        return _outcome(lambda: target.get(node, str(path)))

    for reader in (read_optional, read_optional_bytes):
        fresh = read_with(Database(mode=mode), reader)
        assert fresh == ("value", "<default>")
        assert read_with(db, reader) == fresh

    assert files.probe(str(path)) == ("missing",)
    assert binaries.probe(str(path)) == ("missing",)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_file_resource_still_raises_a_permission_denial(mode: str, tmp_path: Path) -> None:
    # Only the kinds that mean "no readable regular file here" are absorbed; a
    # denial is a genuine failure and stays one, warm and fresh alike.
    files = FileResource()
    path = tmp_path / "thing.txt"
    path.write_text("hello", encoding="utf-8")

    @query
    def read_optional(db: Database, filename: str) -> str:
        try:
            return files.read(db, filename)
        except FileNotFoundError:
            return "<default>"

    db = Database(mode=mode)
    assert db.get(read_optional, str(path)) == "hello"

    path.chmod(0o000)
    try:
        fresh = _outcome(lambda: Database(mode=mode).get(read_optional, str(path)))
        if fresh[0] == "value":  # running as a user that ignores the mode bits
            pytest.skip("the filesystem does not enforce the permission bits here")
        assert fresh[0] == "PermissionError"
        assert _outcome(lambda: db.get(read_optional, str(path))) == fresh
    finally:
        path.chmod(0o644)


def test_listing_probe_follows_the_read_where_a_path_under_a_file_reads_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Windows reports a path reached through a regular file as absent
    # (ERROR_PATH_NOT_FOUND) where POSIX raises NotADirectoryError from the
    # listing. Drive that shape here: whichever the platform picks, the probe
    # must agree with what a read of the path does, and must not raise.
    directories = DirectoryResource()
    holder = tmp_path / "holder.txt"
    holder.write_text("not a directory", encoding="utf-8")
    nested = str(holder / "child")
    real_iterdir = Path.iterdir

    def iterdir(self: Path) -> Any:
        if str(self) == nested:
            raise FileNotFoundError(2, "The system cannot find the path specified", nested)
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)

    assert directories.probe(nested) == (False, ())
    assert directories.load(Database(), nested) == ()
    # ... which is exactly how an absent path reads, so they share a probe.
    absent = str(tmp_path / "never-existed")
    assert directories.probe(absent) == (False, ())
    assert directories.load(Database(), absent) == ()


def _denied(self: Path, *args: Any, **kwargs: Any) -> Any:
    raise PermissionError(13, "Permission denied", str(self))


def test_file_resources_read_a_denied_directory_as_a_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Windows refuses to open a directory as a file with EACCES where POSIX
    # raises IsADirectoryError, and that is the very error an ACL denial on a
    # regular file gives, so the type alone cannot classify it. This drives the
    # Windows shape on any platform: same raise, two kinds of path, and only
    # the kind decides.
    directory = tmp_path / "holder"
    directory.mkdir()
    regular = tmp_path / "thing.txt"
    regular.write_text("hello", encoding="utf-8")

    monkeypatch.setattr(Path, "read_bytes", _denied)
    monkeypatch.setattr(Path, "read_text", _denied)

    files = FileResource()
    binaries = BinaryFileResource()

    assert files.probe(str(directory)) == ("missing",)
    assert binaries.probe(str(directory)) == ("missing",)
    with pytest.raises(FileNotFoundError):
        files.load(Database(), str(directory))
    with pytest.raises(FileNotFoundError):
        binaries.load(Database(), str(directory))

    # A denial on a regular file is a genuine failure and keeps propagating.
    for call in (
        lambda: files.probe(str(regular)),
        lambda: binaries.probe(str(regular)),
        lambda: files.load(Database(), str(regular)),
        lambda: binaries.load(Database(), str(regular)),
        lambda: files.probe_and_load(Database(), str(regular)),
    ):
        with pytest.raises(PermissionError):
            call()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_directory_resource_probes_a_file_path_as_an_absent_listing(
    mode: str, tmp_path: Path
) -> None:
    # The probe has to answer for a path whose kind changed; the read behind it
    # still reports that a file is not a directory, which is how a workspace
    # walk tells a module from a package.
    directories = DirectoryResource()
    listing = tmp_path / "listing"
    listing.mkdir()
    (listing / "a.txt").write_text("a", encoding="utf-8")

    @query
    def names(db: Database, dirname: str) -> tuple[str, ...]:
        try:
            return directories.read(db, dirname)
        except NotADirectoryError:
            return ("<caught>",)

    db = Database(mode=mode)
    assert db.get(names, str(listing)) == ("a.txt",)

    (listing / "a.txt").unlink()
    listing.rmdir()
    listing.write_text("not a directory", encoding="utf-8")

    # The invariant a probe keeps: two paths may share one only when reading
    # them agrees. Which non-directory answer a path *under* a file lands on is
    # the platform's to decide -- POSIX raises NotADirectoryError from the
    # listing where Windows reports the path absent -- so the probe is pinned
    # against the read rather than against either platform's choice.
    kinds = ("directory", "file", "under a file", "absent")
    paths = (
        str(tmp_path),
        str(listing),
        str(listing / "child"),
        str(tmp_path / "never-existed"),
    )

    def read_kind(target: str) -> tuple[str, object]:
        try:
            return ("names", directories.load(Database(), target))
        except OSError as exc:
            return ("raised", type(exc).__name__)

    probes = [directories.probe(target) for target in paths]
    reads = [read_kind(target) for target in paths]
    for left in range(len(kinds)):
        for right in range(left + 1, len(kinds)):
            if probes[left] == probes[right]:
                assert reads[left] == reads[right], (kinds[left], kinds[right])

    # A file and an absent path read differently on every platform, so they may
    # never share a probe -- which is the state the listing probe had to grow.
    assert reads[1] != reads[3]
    assert probes[1] != probes[3]
    assert probes[3] == (False, ())

    fresh = Database(mode=mode)
    assert db.get(names, str(listing)) == fresh.get(names, str(listing)) == ("<caught>",)
    # The failure is now recorded against the probe that observed it, so the
    # reader keeps its edge instead of losing the node it depends on.
    assert _find_node(_inspect_node(db, names, str(listing)), "dir[").last_decision == "failed"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_handled_unrecordable_failure_invalidates_a_transitive_reader(
    mode: str,
    tmp_path: Path,
) -> None:
    deniable = _DeniableFileResource()
    path = tmp_path / "thing.txt"
    path.write_text("text", encoding="utf-8")

    @query
    def reader(db: Database, filename: str) -> str:
        try:
            return deniable.read(db, filename)
        except PermissionError:
            return "<denied>"

    @query
    def parent(db: Database, filename: str) -> str:
        return "P:" + reader(db, filename)

    db = Database(mode=mode)
    assert db.get(parent, str(path)) == "P:text"

    # Neither the load nor the probe survives a denial, so nothing about the
    # failure can be recorded. Reporting the resource changed is enough for
    # `reader`, which re-executes and catches; it is *not* enough for `parent`
    # unless the transition also moves the revision, because otherwise `reader`
    # re-executes at the revision `parent` already verified.
    _deny(path)
    assert db.get(parent, str(path)) == Database(mode=mode).get(parent, str(path)) == "P:<denied>"
    # Still right when the transitive reader is asked repeatedly, not just once.
    assert db.get(parent, str(path)) == "P:<denied>"

    # ... and it settles: once the world heals, the parent follows the value back.
    _allow(path)
    assert db.get(parent, str(path)) == Database(mode=mode).get(parent, str(path)) == "P:text"

    # A second break is a second transition and must invalidate again.
    _deny(path)
    assert db.get(parent, str(path)) == Database(mode=mode).get(parent, str(path)) == "P:<denied>"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_handled_unrecordable_failure_propagates_more_than_one_hop(
    mode: str,
    tmp_path: Path,
) -> None:
    deniable = _DeniableFileResource()
    path = tmp_path / "thing.txt"
    path.write_text("text", encoding="utf-8")

    @query
    def leaf(db: Database, filename: str) -> str:
        try:
            return deniable.read(db, filename)
        except PermissionError:
            return "<denied>"

    @query
    def middle(db: Database, filename: str) -> str:
        return "M:" + leaf(db, filename)

    @query
    def top(db: Database, filename: str) -> str:
        return "T:" + middle(db, filename)

    db = Database(mode=mode)
    assert db.get(top, str(path)) == "T:M:text"
    executions = db.statistics().query_executions
    verified = db.revision

    _deny(path)
    assert db.get(top, str(path)) == Database(mode=mode).get(top, str(path)) == "T:M:<denied>"
    # Every hop of the chain agrees, not only the root that was asked for.
    assert db.get(middle, str(path)) == "M:<denied>"
    assert db.get(leaf, str(path)) == "<denied>"

    # All three hops re-executed in that one request: the invalidation reached
    # past `middle`, which is the hop a direct-reader-only fix leaves behind.
    assert db.statistics().query_executions == executions + 3
    for node in (leaf, middle, top):
        assert _query_record(db, node, str(path)).changed_at > verified


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_permanently_unrecordable_failure_settles_the_revision(
    mode: str,
    tmp_path: Path,
) -> None:
    deniable = _DeniableFileResource()
    path = tmp_path / "thing.txt"
    path.write_text("text", encoding="utf-8")

    @query
    def reader(db: Database, filename: str) -> str:
        try:
            return deniable.read(db, filename)
        except PermissionError:
            return "<denied>"

    @query
    def parent(db: Database, filename: str) -> str:
        return "P:" + reader(db, filename)

    db = Database(mode=mode)
    assert db.get(parent, str(path)) == "P:text"
    before = db.revision

    _deny(path)
    assert db.get(parent, str(path)) == "P:<denied>"
    # The transition into "unconfirmed" moves the revision (once per node that
    # actually changed) -- not once per observation.
    transitioned = db.revision
    assert transitioned > before

    for _ in range(5):
        assert db.get(parent, str(path)) == "P:<denied>"
        assert db.revision == transitioned

    # A read that ultimately succeeds is not a transition either: the healing
    # load moves the revision once, and the requests after it leave it alone.
    _allow(path)
    assert db.get(parent, str(path)) == "P:text"
    healed = db.revision
    assert healed > transitioned
    for _ in range(5):
        assert db.get(parent, str(path)) == "P:text"
        assert db.revision == healed


def test_module_replaced_by_a_package_matches_a_fresh_database(tmp_path: Path) -> None:
    from pyinc.integrations import python_source

    path = tmp_path / "mod.py"
    path.write_text("import os\n", encoding="utf-8")

    db = Database()
    imports = python_source.file_analysis(db, str(path)).imports
    assert tuple(ref.module for ref in imports) == ("os",)

    # A module -> package refactor is the shipped-integration form of the same
    # transition, and lands on the same answer: the path no longer names a
    # source file, so it analyzes as an empty one in both databases.
    path.unlink()
    path.mkdir()
    (path / "__init__.py").write_text("import os\n", encoding="utf-8")
    fresh = _outcome(lambda: python_source.file_analysis(Database(), str(path)))
    assert fresh[0] == "value"
    assert fresh[1].imports == ()
    assert _outcome(lambda: python_source.file_analysis(db, str(path))) == fresh


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


def test_file_resource_detects_content_changes_even_when_stat_signature_is_stable(
    tmp_path: Path,
) -> None:
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
    assert record.changed_at == db.revision


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


def test_file_stat_resource_is_total_when_a_parent_path_component_is_a_file(
    tmp_path: Path,
) -> None:
    stats = FileStatResource()
    parent = tmp_path / "parent"
    parent.write_text("plain file", encoding="utf-8")
    child = parent / "child"

    @query
    def child_exists(db: Database, filename: str) -> bool:
        snapshot = cast(dict[str, object], stats.read(db, filename))
        return snapshot["exists"] is True

    db = Database(mode="checked")
    assert db.get(child_exists, str(child)) is False

    parent.unlink()
    parent.mkdir()
    child.write_text("now a real child", encoding="utf-8")
    assert db.get(child_exists, str(child)) is True
    fresh = Database(mode="checked")
    assert fresh.get(child_exists, str(child)) is True


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


def test_resolved_path_resource_tracks_symlink_retargeting(tmp_path: Path) -> None:
    resolver = ResolvedPathResource()
    first_target = tmp_path / "first"
    second_target = tmp_path / "second"
    first_target.mkdir()
    second_target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(first_target, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symlink support is unavailable in this environment")

    @query
    def resolved(db: Database, path: str) -> str | None:
        return resolver.read(db, path)

    db = Database(mode="checked")
    assert db.get(resolved, str(link)) == str(first_target)

    link.unlink()
    link.symlink_to(second_target, target_is_directory=True)
    assert db.get(resolved, str(link)) == str(second_target)
    assert _inspect_node(db, resolved, str(link)).last_decision == "executed"


def test_resolved_path_resource_probe_is_total_for_a_symlink_loop(tmp_path: Path) -> None:
    resolver = ResolvedPathResource()
    try:
        (tmp_path / "a").symlink_to(tmp_path / "b")
        (tmp_path / "b").symlink_to(tmp_path / "a")
    except (NotImplementedError, OSError):
        pytest.skip("symlink support is unavailable in this environment")

    looped = str(tmp_path / "a" / "child")
    first_probe = resolver.probe(looped)
    assert first_probe == resolver.probe(looped)
    value = resolver.load(Database(), looped)
    assert value is None or isinstance(value, str)
    assert first_probe == (value,)


def test_file_resource_atomic_probe_and_load_keeps_digest_and_text_coherent(
    tmp_path: Path,
) -> None:
    import hashlib

    files = FileResource()
    path = tmp_path / "config.txt"
    original = "alpha beta gamma"
    path.write_text(original, encoding="utf-8")

    @query
    def read(db: Database, target: str) -> str:
        return files.read(db, target)

    db = Database()
    text = db.get(read, str(path))
    assert text == original

    resource_record = db._records[db._resource_key(files, str(path))]
    probe = resource_record.probe
    assert probe[0] == "present"
    stored_digest = cast(str, probe[1])
    assert stored_digest == hashlib.sha256(original.encode("utf-8")).hexdigest()

    updated = "delta epsilon zeta"
    path.write_text(updated, encoding="utf-8")

    text_after = db.get(read, str(path))
    assert text_after == updated
    resource_record_after = db._records[db._resource_key(files, str(path))]
    probe_after = resource_record_after.probe
    assert probe_after[0] == "present"
    stored_digest_after = cast(str, probe_after[1])
    assert stored_digest_after == hashlib.sha256(updated.encode("utf-8")).hexdigest()


def test_file_resource_coherent_under_read_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib

    files = FileResource()
    path = tmp_path / "race.txt"
    path.write_text("first", encoding="utf-8")

    @query
    def read(db: Database, target: str) -> str:
        return files.read(db, target)

    # Simulate a concurrent writer: the first read_bytes returns "first",
    # a second unexpected read_bytes would return "second". Under atomic
    # probe_and_load, only one read happens, so probe and text must agree.
    sequence = iter([b"first", b"second"])

    real_read_bytes = Path.read_bytes

    def fake_read_bytes(self: Path) -> bytes:
        if str(self) == str(path):
            return next(sequence)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

    db = Database()
    text = db.get(read, str(path))
    assert text == "first"

    resource_record = db._records[db._resource_key(files, str(path))]
    probe = resource_record.probe
    assert probe[0] == "present"
    assert probe[1] == hashlib.sha256(b"first").hexdigest()


def test_directory_resource_distinguishes_missing_dir_from_entry_named_missing(
    tmp_path: Path,
) -> None:
    directories = DirectoryResource()
    workspace = tmp_path / "workspace"

    @query
    def listing(db: Database, dirname: str) -> tuple[str, ...]:
        return directories.read(db, dirname)

    workspace.mkdir()
    (workspace / "missing").write_text("x", encoding="utf-8")

    db = Database()
    present = db.get(listing, str(workspace))
    assert present == ("missing",)

    (workspace / "missing").unlink()
    workspace.rmdir()

    absent = db.get(listing, str(workspace))
    assert absent == ()
    assert _inspect_node(db, listing, str(workspace)).last_decision == "executed"


def test_directory_resource_matches_fresh_recomputation_across_missing_toggles(
    tmp_path: Path,
) -> None:
    directories = DirectoryResource()
    workspace = tmp_path / "workspace"

    @query
    def listing(db: Database, dirname: str) -> tuple[str, ...]:
        return directories.read(db, dirname)

    incremental = Database()

    transitions: list[tuple[str, tuple[str, ...]]] = []

    def current_listing() -> tuple[str, ...]:
        if not workspace.exists():
            return ()
        return tuple(sorted(child.name for child in workspace.iterdir()))

    def record(tag: str) -> None:
        incremental_result = incremental.get(listing, str(workspace))
        fresh_result = Database().get(listing, str(workspace))
        assert incremental_result == fresh_result == current_listing(), tag
        transitions.append((tag, incremental_result))

    record("initial-missing")

    workspace.mkdir()
    record("empty-present")

    (workspace / "missing").write_text("x", encoding="utf-8")
    record("single-child-named-missing")

    (workspace / "missing").unlink()
    workspace.rmdir()
    record("missing-again")

    workspace.mkdir()
    (workspace / "missing").write_text("y", encoding="utf-8")
    record("single-child-named-missing-again")

    assert len({payload for _, payload in transitions}) >= 2


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


def test_env_resource_instances_share_stable_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_strict_boundary_views_are_detached_from_the_stored_snapshot() -> None:
    numbers = Input[tuple[int, ...]]("strict-detach.numbers")

    @query
    def listed(db: Database) -> list[int]:
        return list(numbers.read(db))

    db = Database(mode="strict")
    db.set(numbers, (1, 2))
    view = db.get(listed)

    # Frozen dataclass setters refuse plain writes, but object.__setattr__
    # bypasses them; a view aliasing the stored snapshot would then corrupt it.
    object.__setattr__(view, "items", (99,))

    warm_view = db.get(listed)
    assert tuple(warm_view) == (1, 2)
    fresh = Database(mode="strict")
    fresh.set(numbers, (1, 2))
    fresh_view = fresh.get(listed)
    assert tuple(warm_view) == tuple(fresh_view)


def test_strict_boundary_views_detach_nested_shells_too() -> None:
    payload = Input[list[Any]]("strict-detach.nested")

    @query
    def echoed(db: Database) -> list[Any]:
        return list(payload.read(db))

    db = Database(mode="strict")
    db.set(payload, [{"key": 1}])
    view = db.get(echoed)

    inner = view[0]
    object.__setattr__(inner, "entries", (("key", 99),))

    assert dict(db.get(echoed)[0]) == {"key": 1}


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


def test_custom_eq_with_side_effect_does_not_corrupt_graph(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def parity_eq(left: Any, right: Any) -> bool:
        print("custom comparator ran")
        return bool(left[0] % 2 == right[0] % 2)

    number = Input[int]("number")

    @query(eq=parity_eq)
    def transform(db: Database) -> list[int]:
        return [number.read(db)]

    @query
    def describe(db: Database) -> str:
        return f"v={transform(db)[0]}"

    db = Database()
    db.set(number, 3)
    assert db.get(describe) == "v=3"

    # Change input: 3 → 5 (both odd), parity_eq([3], [5]) → True → backdated.
    # The comparator sees a container -- a FrozenList view here, since this
    # database is strict -- so a corrupting comparator would have a shell to
    # rebind; eq fires with a side effect and the kernel still functions.
    db.set(number, 5)
    assert db.get(describe) == "v=3"  # Backdated — parity says equal.
    assert "custom comparator ran" in capsys.readouterr().out
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
    assert (
        cast(dict[str, list[int]], result_a)["items"]
        is not cast(dict[str, list[int]], result_b)["items"]
    )

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


def test_untracked_leaf_value_change_two_levels_up_matches_a_fresh_database(
    tmp_path: Path,
) -> None:
    counter = tmp_path / "counter.txt"
    counter.write_text("1", encoding="utf-8")

    @query
    def unstable(db: Database) -> int:
        db.report_untracked_read("counter file read via os.open")
        fd = os.open(str(counter), os.O_RDONLY)
        try:
            data = os.read(fd, 64)
        finally:
            os.close(fd)
        return int(data.decode("utf-8"))

    @query
    def middle(db: Database) -> int:
        return unstable(db) * 10

    @query
    def top(db: Database) -> int:
        return middle(db) + 1

    db = Database()
    assert db.get(top) == 11

    # The external state moves between requests. Being untracked keeps the
    # leaf's *direct* parent honest; the new value must also reach the
    # grandparent, which only re-verifies through the middle hop's changed_at.
    counter.write_text("2", encoding="utf-8")
    assert db.get(middle) == Database().get(middle) == 20
    assert db.get(top) == Database().get(top) == 21


def test_stable_untracked_leaf_keeps_the_revision_settled_across_warm_requests(
    tmp_path: Path,
) -> None:
    counter = tmp_path / "counter.txt"
    counter.write_text("1", encoding="utf-8")

    @query
    def stable(db: Database) -> int:
        db.report_untracked_read("counter file read via os.open")
        fd = os.open(str(counter), os.O_RDONLY)
        try:
            data = os.read(fd, 64)
        finally:
            os.close(fd)
        return int(data.decode("utf-8"))

    @query
    def parent(db: Database) -> int:
        return stable(db) * 10

    db = Database()
    assert db.get(parent) == 10
    settled = db.revision

    # The untracked leaf re-executes on every request -- its result is never
    # trusted -- but it keeps landing the identical value, so nothing in the
    # graph has changed and the revision must not move.
    for _ in range(5):
        assert db.get(parent) == 10
    assert db.revision == settled

    # A real change in the external state is still a change in the graph and
    # still moves the counter.
    counter.write_text("2", encoding="utf-8")
    assert db.get(parent) == 20
    assert db.revision > settled


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

    def raising_eq(left: int, right: int) -> bool:
        if right == 3:
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
    assert db.get(transform) == 2  # Re-executes; this comparison does not raise.

    # The third value selects the policy's explicit failure branch.
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


# Limitation 4 (durable cross-run cache) is exercised in
# tests/test_checkpoint_trust.py and tests/test_checkpoint_cross_process.py.
# Limitation 5 (in-process module/class monkey-patching is not detected) has no
# dedicated test: the documented behaviour is that the kernel cannot observe it.


# Cycle detection and recovery — a guarantee, not a contract limitation


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

    combine_nodes = [
        n
        for n in graph
        if n.kind == "query"
        and len(n.dependency_labels) == 2
        and all(
            d not in [n2.label for n2 in graph if n2.kind == "input"] for d in n.dependency_labels
        )
    ]
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


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_queries_across_databases_are_thread_safe() -> None:
    x = Input[int]("x")

    @query
    def double(db: Database) -> int:
        return x.read(db) * 2

    errors: list[Exception] = []
    results: list[int] = []

    def worker(value: int) -> None:
        try:
            db = Database()
            db.set(x, value)
            for _ in range(50):
                results.append(db.get(double))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"thread errors: {errors}"
    # Each of 8 threads pushed 50 results — 400 total. Values are 2*i for i in [0..7].
    assert len(results) == 8 * 50
    assert set(results) == {2 * i for i in range(8)}


def test_shared_database_serializes_concurrent_set_and_get() -> None:
    x = Input[int]("x")

    @query
    def read_x(db: Database) -> int:
        return x.read(db)

    db = Database()
    db.set(x, 0)

    errors: list[Exception] = []
    observed: list[int] = []

    def setter() -> None:
        try:
            for value in range(100):
                db.set(x, value)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def getter() -> None:
        try:
            for _ in range(200):
                observed.append(db.get(read_x))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=setter),
        threading.Thread(target=setter),
        threading.Thread(target=getter),
        threading.Thread(target=getter),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"thread errors: {errors}"
    # All observed values must be valid iterations assigned by some setter.
    assert all(0 <= v < 100 for v in observed)


def test_untracked_read_still_enforced_per_thread(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    path.write_text("ok", encoding="utf-8")

    @query
    def raw_open(db: Database) -> int:
        with open(str(path), encoding="utf-8"):
            pass
        return 1

    db_a = Database()
    errors_a: list[Exception] = []

    started = threading.Event()
    may_finish = threading.Event()
    outside_reads_ok: list[bool] = []

    def worker_a() -> None:
        try:
            db_a.get(raw_open)
        except UntrackedReadError:
            errors_a.append(UntrackedReadError("raised as expected"))

    def worker_b() -> None:
        started.wait()
        try:
            with open(str(path), encoding="utf-8") as fh:
                outside_reads_ok.append(fh.read() == "ok")
        finally:
            may_finish.set()

    # Prime: trigger query execution on a separate thread; while it's active,
    # a parallel thread should still be able to read raw files because it's
    # outside any query frame.
    t_a = threading.Thread(target=worker_a)
    t_b = threading.Thread(target=worker_b)
    t_a.start()
    started.set()
    t_b.start()
    t_a.join()
    t_b.join()

    assert errors_a, "query that called raw open must raise UntrackedReadError"
    assert outside_reads_ok == [True]


def test_module_capture_invalidates_on_source_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib
    import sys as _sys

    module_dir = tmp_path
    module_file = module_dir / "pyinc_test_f5_source_change.py"
    module_file.write_text("CONSTANT = 'alpha'\n", encoding="utf-8")

    monkeypatch.syspath_prepend(str(module_dir))
    mod = importlib.import_module("pyinc_test_f5_source_change")
    try:

        @query
        def read_constant(db: Database) -> str:
            return cast(str, mod.CONSTANT)

        db1 = Database()
        first_fp = db1._query_fingerprint(read_constant)

        # Update the source and reload. A later query registration must see a
        # different code fingerprint because the captured module's source
        # changed.
        module_file.write_text("CONSTANT = 'omega'\n", encoding="utf-8")
        os.utime(str(module_file), ns=(0, 2_000_000_000))
        importlib.reload(mod)

        second_fp = db1._query_fingerprint(read_constant)

        assert first_fp != second_fp
    finally:
        _sys.modules.pop("pyinc_test_f5_source_change", None)


def test_module_capture_invalidates_on_version_attr_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib
    import sys as _sys

    module_dir = tmp_path
    module_file = module_dir / "pyinc_test_f5_version.py"
    module_file.write_text("__version__ = '1.0.0'\n", encoding="utf-8")

    monkeypatch.syspath_prepend(str(module_dir))
    mod = importlib.import_module("pyinc_test_f5_version")
    try:

        @query
        def read_version(db: Database) -> str:
            return cast(str, mod.__version__)

        db = Database()
        first_fp = db._query_fingerprint(read_version)

        # Simulate a version bump in-process; the captured module's payload
        # must reflect the new value.
        mod.__version__ = "2.0.0"  # type: ignore[attr-defined]

        second_fp = db._query_fingerprint(read_version)
        assert first_fp != second_fp
    finally:
        _sys.modules.pop("pyinc_test_f5_version", None)


def test_module_capture_stable_for_stdlib_within_same_interpreter() -> None:
    import os as _os

    @query
    def uses_os(db: Database) -> str:
        return _os.sep

    db_a = Database()
    db_b = Database()
    fp_a = db_a._code_fingerprint(cast(Any, uses_os.fn))
    fp_b = db_b._code_fingerprint(cast(Any, uses_os.fn))
    assert fp_a == fp_b


def test_module_capture_accepts_authenticated_stdlib_spec_identity() -> None:
    import collections.abc as collections_abc
    import sys

    specification = collections_abc.__spec__
    assert specification is not None
    # CPython 3.11/3.12 uses the canonical source-backed name, while newer
    # builds may expose the same live module through the frozen stdlib alias.
    assert specification.name in {"collections.abc", "_collections_abc"}
    assert sys.modules[collections_abc.__name__] is collections_abc
    assert sys.modules[specification.name] is collections_abc

    @query
    def uses_collections_abc(db: Database) -> str:
        return collections_abc.Mapping.__name__

    assert Database().get(uses_collections_abc) == "Mapping"


def test_guard_stack_reentrant_within_same_thread() -> None:
    @query
    def inner(db: Database) -> int:
        return 1

    @query
    def outer(db: Database) -> int:
        return inner(db) + 1

    db = Database()
    assert db.get(outer) == 2
    # Stack popped cleanly: running inner by itself after outer still works.
    assert db.get(inner) == 1


# --- Push observers (v2 development cycle) -----------------------------------


def test_observe_fires_on_cold_execution() -> None:
    db = Database()
    inp = Input[int]("x")
    db.set(inp, 10)

    @query
    def doubled(db: Database) -> int:
        return inp.read(db) * 2

    events: list[QueryChangeEvent] = []
    sub = db.observe(events.append, doubled)
    assert isinstance(sub, Subscription)
    assert db.get(doubled) == 20
    assert len(events) == 1
    assert events[0].query_id == doubled.key
    assert events[0].decision == "executed"
    assert events[0].changed_at == events[0].verified_at


def test_observe_fires_on_true_recompute() -> None:
    db = Database()
    inp = Input[int]("x")
    db.set(inp, 1)

    @query
    def doubled(db: Database) -> int:
        return inp.read(db) * 2

    events: list[QueryChangeEvent] = []
    db.observe(events.append, doubled)
    assert db.get(doubled) == 2
    db.set(inp, 5)
    assert db.get(doubled) == 10
    assert len(events) == 2
    assert events[1].changed_at > events[0].changed_at


def test_observe_does_not_fire_on_equal_input_update() -> None:
    db = Database()
    inp = Input[int]("x")
    db.set(inp, 1)

    @query
    def doubled(db: Database) -> int:
        return inp.read(db) * 2

    events: list[QueryChangeEvent] = []
    db.observe(events.append, doubled)
    assert db.get(doubled) == 2
    db.set(inp, 1)  # equal: ignored by the kernel, no re-execution
    assert db.get(doubled) == 2
    assert len(events) == 1


def test_observe_does_not_fire_on_backdate(tmp_path: Path) -> None:
    path = tmp_path / "src.py"
    path.write_bytes(b"x = 1\n")
    file_resource = FileResource()

    @query(cutoff=lambda value: value.strip())
    def trimmed(db: Database, target: Path) -> str:
        return file_resource.read(db, target)

    db = Database()
    events: list[QueryChangeEvent] = []
    db.observe(events.append, trimmed, path)
    assert db.get(trimmed, path) == "x = 1\n"
    assert len(events) == 1
    # Whitespace-only edit → same cutoff token → backdate
    path.write_bytes(b"x = 1\n\n")
    assert db.get(trimmed, path) == "x = 1\n\n"
    assert len(events) == 1, "backdate must not fire observer"


def test_observe_does_not_fire_on_reuse() -> None:
    db = Database()
    inp = Input[int]("x")
    db.set(inp, 1)

    @query
    def doubled(db: Database) -> int:
        return inp.read(db) * 2

    events: list[QueryChangeEvent] = []
    db.observe(events.append, doubled)
    assert db.get(doubled) == 2
    # No state change at all: verified_at advances silently, no re-exec.
    assert db.get(doubled) == 2
    assert db.get(doubled) == 2
    assert len(events) == 1


def test_unsubscribe_stops_future_events() -> None:
    db = Database()
    inp = Input[int]("x")
    db.set(inp, 1)

    @query
    def doubled(db: Database) -> int:
        return inp.read(db) * 2

    events: list[QueryChangeEvent] = []
    sub = db.observe(events.append, doubled)
    db.get(doubled)
    assert len(events) == 1
    sub.unsubscribe()
    db.set(inp, 99)
    db.get(doubled)
    assert len(events) == 1
    # Idempotent
    sub.unsubscribe()
    sub.unsubscribe()


def test_observe_dispatch_runs_after_lock_released() -> None:
    db = Database()
    inp = Input[int]("x")
    db.set(inp, 1)

    @query
    def src(db: Database) -> int:
        return inp.read(db) * 2

    @query
    def sink(db: Database) -> int:
        return inp.read(db) + 100

    seen_during_callback: list[int] = []

    def on_src_change(event: QueryChangeEvent) -> None:
        # Callback reaches back into the database: must not deadlock and
        # must observe committed state.
        seen_during_callback.append(db.get(sink))

    db.observe(on_src_change, src)
    db.get(src)
    assert seen_during_callback == [101]
    db.set(inp, 2)
    db.get(src)
    assert seen_during_callback == [101, 102]


def test_observe_exception_isolated_and_routed_to_error_hook() -> None:
    caught: list[Exception] = []
    db = Database(observer_error_hook=caught.append)
    inp = Input[int]("x")
    db.set(inp, 1)

    @query
    def doubled(db: Database) -> int:
        return inp.read(db) * 2

    good_events: list[QueryChangeEvent] = []

    def raiser(_: QueryChangeEvent) -> None:
        raise RuntimeError("boom")

    db.observe(raiser, doubled)
    db.observe(good_events.append, doubled)

    db.get(doubled)
    assert len(good_events) == 1
    assert len(caught) == 1
    assert isinstance(caught[0], RuntimeError)
    assert str(caught[0]) == "boom"


def test_observe_unsubscribe_during_dispatch_is_safe() -> None:
    db = Database()
    inp = Input[int]("x")
    db.set(inp, 1)

    @query
    def doubled(db: Database) -> int:
        return inp.read(db) * 2

    sibling_events: list[QueryChangeEvent] = []
    sub_holder: list[Subscription] = []

    def unsub_self(_: QueryChangeEvent) -> None:
        sub_holder[0].unsubscribe()

    sub_holder.append(db.observe(unsub_self, doubled))
    db.observe(sibling_events.append, doubled)

    db.get(doubled)  # both fire; unsub_self removes itself after firing
    assert len(sibling_events) == 1
    db.set(inp, 7)
    db.get(doubled)  # only sibling fires now
    assert len(sibling_events) == 2


def test_observe_set_many_fires_once_per_downstream_get() -> None:
    db = Database()
    a = Input[int]("a")
    b = Input[int]("b")
    db.set(a, 1)
    db.set(b, 2)

    @query
    def total(db: Database) -> int:
        return a.read(db) + b.read(db)

    events: list[QueryChangeEvent] = []
    db.observe(events.append, total)
    db.get(total)
    assert len(events) == 1
    db.set_many([(a, 10), (b, 20)])
    db.get(total)
    # One re-execution triggered by the single revision bump => one event
    assert len(events) == 2


def test_observe_rejects_non_query_and_non_callable() -> None:
    db = Database()

    @query
    def q(db: Database) -> int:
        return 1

    with pytest.raises(TypeError):
        db.observe(lambda e: None, cast(Any, object()))
    with pytest.raises(TypeError):
        db.observe(cast(Any, 42), q)


def test_observe_args_variant_keys_independently() -> None:
    db = Database()

    @query
    def square(db: Database, n: int) -> int:
        return n * n

    events_2: list[QueryChangeEvent] = []
    events_3: list[QueryChangeEvent] = []
    db.observe(events_2.append, square, 2)
    db.observe(events_3.append, square, 3)
    db.get(square, 2)
    assert len(events_2) == 1
    assert len(events_3) == 0
    db.get(square, 3)
    assert len(events_2) == 1
    assert len(events_3) == 1


def test_observers_thread_safe_under_contention() -> None:
    db = Database()
    inp = Input[int]("x")
    db.set(inp, 0)

    @query
    def doubled(db: Database) -> int:
        return inp.read(db) * 2

    events_lock = threading.Lock()
    events: list[QueryChangeEvent] = []

    def record(event: QueryChangeEvent) -> None:
        with events_lock:
            events.append(event)

    subs: list[Subscription] = []
    for _ in range(20):
        subs.append(db.observe(record, doubled))

    stop = threading.Event()

    def writer() -> None:
        value = 0
        while not stop.is_set():
            value += 1
            db.set(inp, value)
            db.get(doubled)

    def churner() -> None:
        while not stop.is_set():
            s = db.observe(record, doubled)
            s.unsubscribe()

    threads = [threading.Thread(target=writer) for _ in range(2)] + [
        threading.Thread(target=churner) for _ in range(2)
    ]
    for t in threads:
        t.start()
    threading.Event().wait(0.25)
    stop.set()
    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive()
    # Just verify no deadlock/crash and that events did get delivered.
    assert len(events) > 0
    for s in subs:
        s.unsubscribe()


def test_observe_evicted_node_does_not_raise_and_refires_after_reload() -> None:
    db = Database(max_query_nodes=1)

    @query
    def a(db: Database) -> int:
        return 1

    @query
    def b(db: Database) -> int:
        return 2

    events: list[QueryChangeEvent] = []
    db.observe(events.append, a)
    db.get(a)  # cold: event 1
    db.get(b)  # forces eviction of a under max_query_nodes=1
    assert len(events) == 1
    # a is no longer a record, but observer is still registered
    db.get(a)  # re-executes a from scratch → event 2 fires
    assert len(events) == 2


# ---------------------------------------------------------------------------
# Backdate decision on recomputation
# ---------------------------------------------------------------------------
#
# The default (no eq=, no cutoff=) backdate decision is computed on the stored
# canonical snapshots and must be identical in every mode. The tests below pin
# the decision for the value shapes where snapshot equality, digest equality,
# and live-value equality could plausibly disagree.


@dataclass(frozen=True)
class GridPoint:
    x: int
    y: int


@dataclass(frozen=True)
class MapPoint:
    x: int
    y: int


class Boxed:
    def __init__(self, payload: int) -> None:
        self.payload = payload


class BoxedAdapter(ValueAdapter):
    freeze_calls: ClassVar[int] = 0
    thaw_calls: ClassVar[int] = 0

    def freeze(self, value: Boxed, freeze: Any) -> object:
        assert callable(freeze)
        type(self).freeze_calls += 1
        return {"payload": freeze(value.payload)}

    def thaw(self, snapshot: Any, thaw: Any) -> Boxed:
        type(self).thaw_calls += 1
        data = cast(dict[str, Any], snapshot)
        return Boxed(thaw(data["payload"]))


# A module-level pre-frozen wrapper. freeze detaches it into a Database-owned
# clone at every boundary; the backdate below is justified by the equality
# relation over the stored snapshots, never by shared object identity.
_NAN_ITEMS = cast(FrozenList, freeze([float("nan")]))


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_dataclass_type_change_with_equal_fields_executes(mode: str) -> None:
    stage = Input[int]("stage")

    @query
    def locate(db: Database) -> object:
        if stage.read(db) == 0:
            return GridPoint(1, 2)
        return MapPoint(1, 2)

    db = Database(mode=mode)
    db.set(stage, 0)
    db.get(locate)

    db.set(stage, 1)
    revision_after_set = db.revision
    db.get(locate)
    record = _inspect_node(db, locate)
    assert record.last_decision == "executed"
    assert db.revision == revision_after_set + 1
    assert record.changed_at == db.revision


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_dataclass_to_dict_with_equal_shape_executes(mode: str) -> None:
    stage = Input[int]("stage")

    @query
    def shape(db: Database) -> object:
        if stage.read(db) == 0:
            return GridPoint(1, 2)
        return {"x": 1, "y": 2}

    db = Database(mode=mode)
    db.set(stage, 0)
    db.get(shape)

    db.set(stage, 1)
    revision_after_set = db.revision
    db.get(shape)
    record = _inspect_node(db, shape)
    assert record.last_decision == "executed"
    assert db.revision == revision_after_set + 1


# The default (no eq=, no cutoff=) input comparison is the same decision as the
# recomputation one, on the same operands: the stored canonical snapshots. The
# tests below drive the shapes where thawing would erase the distinction the
# comparison exists to make, through both input entry points and in every mode.


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("replacement", [MapPoint(1, 2), {"x": 1, "y": 2}])
def test_input_set_type_change_with_equal_fields_matches_fresh(
    mode: str, replacement: object
) -> None:
    point = Input[object]("point")

    @query
    def describe(db: Database) -> str:
        value = point.read(db)
        return f"{type(value).__name__}:{value!r}"

    db = Database(mode=mode)
    db.set(point, GridPoint(1, 2))
    db.get(describe)
    db.set(point, replacement)

    fresh = Database(mode=mode)
    fresh.set(point, replacement)
    assert db.get(describe) == fresh.get(describe)
    assert db.statistics().input_equal_ignores == 0


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("replacement", [MapPoint(1, 2), {"x": 1, "y": 2}])
def test_input_set_many_type_change_with_equal_fields_matches_fresh(
    mode: str, replacement: object
) -> None:
    point = Input[object]("point")

    @query
    def describe(db: Database) -> str:
        value = point.read(db)
        return f"{type(value).__name__}:{value!r}"

    db = Database(mode=mode)
    db.set_many([(point, GridPoint(1, 2))])
    db.get(describe)
    db.set_many([(point, replacement)])

    fresh = Database(mode=mode)
    fresh.set_many([(point, replacement)])
    assert db.get(describe) == fresh.get(describe)
    assert db.statistics().input_equal_ignores == 0


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_equal_input_set_does_not_run_adapter_hooks(mode: str) -> None:
    boxed = Input[Boxed]("boxed")

    db = Database(mode=mode, adapters={Boxed: BoxedAdapter()})
    db.set(boxed, Boxed(7))

    BoxedAdapter.freeze_calls = 0
    BoxedAdapter.thaw_calls = 0
    db.set(boxed, Boxed(7))

    # The one freeze is the incoming value's own snapshot; the comparison that
    # follows reads the stored snapshots and runs no hook of its own.
    assert BoxedAdapter.freeze_calls == 1
    assert BoxedAdapter.thaw_calls == 0
    assert db.statistics().input_equal_ignores == 1


@pytest.mark.parametrize("mode", ["checked", "fast"])
def test_backdate_compare_does_not_thaw_adapted_values(mode: str) -> None:
    stage = Input[int]("stage")

    @query
    def boxed(db: Database) -> Boxed:
        stage.read(db)
        return Boxed(7)

    db = Database(mode=mode, adapters={Boxed: BoxedAdapter()})
    db.set(stage, 0)
    first = db.get(boxed)
    assert isinstance(first, Boxed)
    assert first.payload == 7

    BoxedAdapter.thaw_calls = 0
    db.set(stage, 1)
    second = db.get(boxed)
    assert isinstance(second, Boxed)
    assert second.payload == 7
    record = _inspect_node(db, boxed)
    assert record.last_decision == "backdated"
    # The only thaw on the warm request is the caller-boundary exposure; the
    # equality decision runs on the stored snapshots directly.
    assert BoxedAdapter.thaw_calls == 1


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_prefrozen_nan_wrapper_result_backdates(mode: str) -> None:
    stage = Input[int]("stage")

    @query
    def constant_items(db: Database) -> object:
        stage.read(db)
        return _NAN_ITEMS

    db = Database(mode=mode)
    db.set(stage, 0)
    db.get(constant_items)

    # Each execution stores its own detached clone, so the backdate can no
    # longer rest on the two records holding one object. It still does not pin
    # NaN reflexivity across distinct floats: detach clones wrapper shells and
    # shares leaf scalars, so both snapshots hold the very same NaN float and
    # no comparison of them ever has to decide whether two distinct NaNs are
    # equal. That case is pinned by
    # test_freshly_built_nan_result_backdates_and_holds_dependents, which
    # builds a distinct NaN per execution.
    db.set(stage, 1)
    revision_after_set = db.revision
    second = db.get(constant_items)
    record = _inspect_node(db, constant_items)
    assert record.last_decision == "backdated"
    assert db.revision == revision_after_set

    fresh = Database(mode=mode)
    fresh.set(stage, 1)
    fresh_value = fresh.get(constant_items)
    assert math.isnan(list(cast(Any, second))[0])
    assert math.isnan(list(cast(Any, fresh_value))[0])


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_freshly_built_nan_result_backdates_and_holds_dependents(mode: str) -> None:
    stage = Input[int]("stage")

    # Freshly built operands each run: before the canonical relation this
    # backdate was carried ONLY by the digest fallback the runtime no longer
    # has. It now rests on snapshots_equal itself; if this test executes
    # instead of backdating, the canonical relation lost NaN reflexivity.
    @query
    def measurement(db: Database) -> object:
        stage.read(db)
        return (1.0, float("nan"))

    @query
    def arity(db: Database) -> int:
        return len(cast(Any, measurement(db)))

    db = Database(mode=mode)
    db.set(stage, 0)
    db.get(arity)
    changed_at = _inspect_node(db, measurement).changed_at
    backdates = db.statistics().query_backdates

    db.set(stage, 1)
    revision_after_set = db.revision
    assert db.get(arity) == 2
    record = _inspect_node(db, measurement)
    assert record.last_decision == "backdated"
    assert record.changed_at == changed_at
    assert db.revision == revision_after_set
    assert db.statistics().query_backdates == backdates + 1
    assert _inspect_node(db, arity).last_decision == "reused"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_int_to_float_recompute_executes(mode: str) -> None:
    stage = Input[int]("stage")

    @query
    def measure(db: Database) -> object:
        return 1 if stage.read(db) == 0 else 1.0

    db = Database(mode=mode)
    db.set(stage, 0)
    assert db.get(measure) == 1

    db.set(stage, 1)
    revision_after_set = db.revision
    value = db.get(measure)
    assert type(cast(Any, value)) is float
    record = _inspect_node(db, measure)
    assert record.last_decision == "executed"
    assert db.revision == revision_after_set + 1
    assert record.changed_at == db.revision


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_bool_to_int_recompute_executes(mode: str) -> None:
    stage = Input[int]("stage")

    @query
    def flag(db: Database) -> object:
        return True if stage.read(db) == 0 else 1

    db = Database(mode=mode)
    db.set(stage, 0)
    db.get(flag)

    db.set(stage, 1)
    value = db.get(flag)
    assert type(cast(Any, value)) is int
    assert _inspect_node(db, flag).last_decision == "executed"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_negative_zero_recompute_executes(mode: str) -> None:
    stage = Input[int]("stage")

    @query
    def bare(db: Database) -> object:
        return 0.0 if stage.read(db) == 0 else -0.0

    @query
    def nested(db: Database) -> object:
        if stage.read(db) == 0:
            return (0.0, {"k": 0.0})
        return (-0.0, {"k": -0.0})

    db = Database(mode=mode)
    db.set(stage, 0)
    db.get(bare)
    db.get(nested)

    db.set(stage, 1)
    assert math.copysign(1.0, cast(float, db.get(bare))) == -1.0
    db.get(nested)
    assert _inspect_node(db, bare).last_decision == "executed"
    assert _inspect_node(db, nested).last_decision == "executed"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_numeric_dict_key_recompute_decisions(mode: str) -> None:
    stage = Input[int]("stage")

    # An int key and a float key are different canonical encodings, so both
    # shapes execute -- the single-entry case used to backdate because raw
    # == unified 1 and 1.0.
    @query
    def single_entry(db: Database) -> object:
        return {1: "a"} if stage.read(db) == 0 else {1.0: "a"}

    @query
    def double_entry(db: Database) -> object:
        return {1: "a", 2: "b"} if stage.read(db) == 0 else {1.0: "a", 2: "b"}

    db = Database(mode=mode)
    db.set(stage, 0)
    db.get(single_entry)
    db.get(double_entry)

    db.set(stage, 1)
    db.get(single_entry)
    db.get(double_entry)
    assert _inspect_node(db, single_entry).last_decision == "executed"
    assert _inspect_node(db, double_entry).last_decision == "executed"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("shape", ["bare", "tuple", "dict_value", "set_member", "dict_key"])
def test_nan_result_backdates_on_every_recompute(mode: str, shape: str) -> None:
    # A NaN never equals itself under ==, so a relation resting on that would
    # call every one of these an unequal result forever. The canonical
    # encoding normalizes NaN to one byte sequence, so snapshots_equal calls
    # it equal in every position a NaN can hold and in every mode.
    stage = Input[int]("stage")

    @query
    def produce(db: Database) -> object:
        stage.read(db)
        nan = float("nan")
        if shape == "bare":
            return nan
        if shape == "tuple":
            return (nan, 1)
        if shape == "dict_value":
            return {"k": nan}
        if shape == "set_member":
            return {nan}
        return {nan: 1}

    db = Database(mode=mode)
    db.set(stage, 0)
    db.get(produce)
    changed_at = _inspect_node(db, produce).changed_at

    for step in (1, 2):
        db.set(stage, step)
        revision_after_set = db.revision
        db.get(produce)
        record = _inspect_node(db, produce)
        assert record.last_decision == "backdated"
        assert db.revision == revision_after_set
        assert record.changed_at == changed_at


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_str_to_bytes_recompute_executes(mode: str) -> None:
    stage = Input[int]("stage")

    @query
    def text(db: Database) -> object:
        return "x" if stage.read(db) == 0 else b"x"

    db = Database(mode=mode)
    db.set(stage, 0)
    db.get(text)

    db.set(stage, 1)
    db.get(text)
    assert _inspect_node(db, text).last_decision == "executed"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_set_to_frozenset_recompute_executes(mode: str) -> None:
    stage = Input[int]("stage")

    @query
    def members(db: Database) -> object:
        return {1, 2} if stage.read(db) == 0 else frozenset({1, 2})

    db = Database(mode=mode)
    db.set(stage, 0)
    db.get(members)

    db.set(stage, 1)
    db.get(members)
    assert _inspect_node(db, members).last_decision == "executed"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_shared_graph_construction_order_recompute_backdates(mode: str) -> None:
    stage = Input[int]("stage")

    @query
    def linked(db: Database) -> object:
        if stage.read(db) == 0:
            first = {"k": 1}
            second = {"k": 1}
            shared = [first, second]
            return {"left": shared, "right": shared, "z": 9}
        out: dict[str, object] = {"z": 9}
        shared_again: list[object] = [{"k": 1}, {"k": 1}]
        out["right"] = shared_again
        out["left"] = shared_again
        return out

    @query
    def looped(db: Database) -> object:
        if stage.read(db) == 0:
            items: list[object] = [1, 2]
            items.append(items)
            return {"c": items}
        rebuilt: list[object] = [1]
        rebuilt.extend([2])
        rebuilt.append(rebuilt)
        return {"c": rebuilt}

    db = Database(mode=mode)
    db.set(stage, 0)
    db.get(linked)
    db.get(looped)

    db.set(stage, 1)
    db.get(linked)
    db.get(looped)
    assert _inspect_node(db, linked).last_decision == "backdated"
    assert _inspect_node(db, looped).last_decision == "backdated"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_checkpoint_loaded_record_backdates_on_equal_recompute(mode: str) -> None:
    number = Input[int]("number")

    @query
    def parity_word(db: Database) -> str:
        return "even" if number.read(db) % 2 == 0 else "odd"

    store = InMemoryArtifactStore()
    first_db = Database(mode=mode, store=store)
    first_db.set(number, 2)
    assert first_db.get(parity_word) == "even"
    ck_key = first_db.save_checkpoint()

    second_db = Database(mode=mode, store=store)
    second_db.set(number, 2)
    second_db.load_checkpoint(ck_key)
    assert second_db.get(parity_word) == "even"
    loaded = _inspect_node(second_db, parity_word)
    assert loaded.last_decision == "reused"

    second_db.set(number, 4)
    assert second_db.get(parity_word) == "even"
    record = _inspect_node(second_db, parity_word)
    assert record.last_decision == "backdated"


@pytest.mark.parametrize(
    ("mode", "expected_operand_type"),
    [("strict", "FrozenDict"), ("checked", "dict"), ("fast", "dict")],
)
def test_custom_eq_receives_mode_exposed_operands(
    mode: str, expected_operand_type: str, capsys: pytest.CaptureFixture[str]
) -> None:
    stage = Input[int]("stage")

    def typed_eq(left: object, right: object) -> bool:
        print(f"eq-operands:{type(left).__name__}:{type(right).__name__}")
        return left == right

    @query(eq=typed_eq)
    def mapping(db: Database) -> dict[str, int]:
        stage.read(db)
        return {"x": 1}

    db = Database(mode=mode)
    db.set(stage, 0)
    db.get(mapping)
    assert capsys.readouterr().out == ""

    db.set(stage, 1)
    db.get(mapping)
    expected = f"eq-operands:{expected_operand_type}:{expected_operand_type}\n"
    assert capsys.readouterr().out == expected
    assert _inspect_node(db, mapping).last_decision == "backdated"


@pytest.mark.parametrize("mode", ["checked", "fast"])
def test_impure_custom_eq_skips_policy_but_still_exposes(mode: str) -> None:
    stage = Input[int]("stage")

    def never_called(left: object, right: object) -> bool:
        raise AssertionError("eq policy must not run for an impure recompute")

    @query(eq=never_called)
    def impure_boxed(db: Database) -> Boxed:
        stage.read(db)
        db.report_untracked_read("external state")
        return Boxed(3)

    db = Database(mode=mode, adapters={Boxed: BoxedAdapter()})
    db.set(stage, 0)
    db.get(impure_boxed)

    BoxedAdapter.thaw_calls = 0
    db.set(stage, 1)
    value = db.get(impure_boxed)
    assert isinstance(value, Boxed)
    assert value.payload == 3
    record = _inspect_node(db, impure_boxed)
    assert record.last_decision == "executed"
    # The custom-policy branch exposes both sides before the impure
    # short-circuit forces the decision: two compare exposures plus the
    # caller-boundary thaw.
    assert BoxedAdapter.thaw_calls == 3


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_hostile_eq_cannot_corrupt_the_stored_record(mode: str) -> None:
    stage = Input[int]("hostile-eq-stage")

    def hostile_eq(left: object, right: object) -> bool:
        for operand in (left, right):
            if isinstance(operand, FrozenList):
                object.__setattr__(operand, "items", ("CORRUPTED",))
            elif isinstance(operand, list):
                operand.clear()
                operand.append("CORRUPTED")
        return False

    @query(eq=hostile_eq)
    def produce(db: Database) -> list[int]:
        stage.read(db)
        return [1]

    db = Database(mode=mode)
    db.set(stage, 0)
    db.get(produce)
    db.set(stage, 1)
    db.get(produce)

    record = _query_record(db, produce)
    assert fingerprint_snapshot(record.snapshot) == record.digest

    fresh = Database(mode=mode)
    fresh.set(stage, 1)
    assert list(cast(Any, db.get(produce))) == list(cast(Any, fresh.get(produce))) == [1]


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_eq_operands_are_not_the_stored_snapshots(
    mode: str, capsys: pytest.CaptureFixture[str]
) -> None:
    stage = Input[int]("eq-identity-stage")

    def spy_eq(left: object, right: object) -> bool:
        # A policy may not capture mutable state, so the operand identities
        # leave through the print stream. The stored snapshot and its items
        # tuple are alive while these ids are taken, so no distinct object can
        # carry their id: an id match would mean the comparator holds the
        # record itself.
        for operand in (left, right):
            items = id(operand.items) if isinstance(operand, FrozenList) else 0
            print(f"eq-operand:{id(operand)}:{items}")
        return False

    @query(eq=spy_eq)
    def produce(db: Database) -> list[int]:
        stage.read(db)
        return [1]

    db = Database(mode=mode)
    db.set(stage, 0)
    db.get(produce)
    db.set(stage, 1)
    db.get(produce)

    record = _query_record(db, produce)
    stored = cast(FrozenList, record.snapshot)
    reported = capsys.readouterr().out.splitlines()
    assert len(reported) == 2
    for line in reported:
        _, shell, items = line.split(":")
        assert int(shell) != id(stored)
        # 0 stands for a thawed operand, which is a list and has no items
        # tuple to share -- thaw allocates the whole container fresh.
        assert int(items) != id(stored.items)


# ---------------------------------------------------------------------------
# Request spans
# ---------------------------------------------------------------------------
# The tallies live in side files next to each resource key because a query's
# capture set may not contain mutable state -- a counter attribute or module
# global is rejected before the first get().


def _span_tally(key: str, event: str) -> None:
    with open(f"{key}.calls", "a", encoding="utf-8") as handle:
        handle.write(event)


def _span_tallied(key: str) -> str:
    calls = Path(f"{key}.calls")
    return calls.read_text(encoding="utf-8") if calls.exists() else ""


@dataclass(frozen=True)
class _SpanTalliedResource(Resource[str, str, tuple[str, str]]):
    """Reads ``<key>``, tallying every probe ('p') and load ('l') call."""

    def probe(self, key: str) -> tuple[str, str]:
        _span_tally(key, "p")
        return ("present", hashlib.sha256(Path(key).read_bytes()).hexdigest())

    def load(self, db: Database, key: str) -> str:
        _span_tally(key, "l")
        return Path(key).read_text(encoding="utf-8")

    def label(self, key: str) -> str:
        return f"span-tallied[{key}]"


@dataclass(frozen=True)
class _SpanFailingResource(Resource[str, str, tuple[str, ...]]):
    """Never loads; the probe models the file as missing rather than raising."""

    def probe(self, key: str) -> tuple[str, ...]:
        _span_tally(key, "p")
        return ("missing",)

    def load(self, db: Database, key: str) -> str:
        _span_tally(key, "l")
        raise FileNotFoundError(key)

    def label(self, key: str) -> str:
        return f"span-failing[{key}]"


def test_request_span_validates_a_shared_resource_once(tmp_path: Path) -> None:
    resource = _SpanTalliedResource()
    target = str(tmp_path / "data.txt")
    Path(target).write_text("hello", encoding="utf-8")

    @query
    def raw_text(db: Database) -> str:
        return resource.read(db, target)

    @query
    def upper_text(db: Database) -> str:
        return resource.read(db, target).upper()

    @query
    def text_size(db: Database) -> int:
        return len(resource.read(db, target))

    db = Database()
    assert db.get(raw_text) == "hello"
    assert _span_tallied(target) == "pl"

    # Warm gets outside a span each open their own request: one validation
    # probe per get.
    assert db.get(raw_text) == "hello"
    assert db.get(upper_text) == "HELLO"
    assert db.get(text_size) == 5
    assert _span_tallied(target) == "pl" + "ppp"

    # The same three gets inside one span share one request: the resource is
    # validated once for the whole batch.
    requests_before = db.statistics().total_requests
    with db.request_span():
        assert db.get(raw_text) == "hello"
        assert db.get(upper_text) == "HELLO"
        assert db.get(text_size) == 5
    assert _span_tallied(target) == "pl" + "ppp" + "p"
    assert db.statistics().total_requests == requests_before + 1


def test_request_inputs_changed_reopens_the_span_to_the_world(tmp_path: Path) -> None:
    resource = _SpanTalliedResource()
    target = str(tmp_path / "data.txt")
    Path(target).write_text("old", encoding="utf-8")

    @query
    def read_text(db: Database) -> str:
        return resource.read(db, target)

    @query
    def echo_text(db: Database) -> str:
        return resource.read(db, target)

    db = Database()
    assert db.get(read_text) == "old"

    with db.request_span():
        assert db.get(read_text) == "old"
        Path(target).write_text("new!", encoding="utf-8")
        # The span declares the world stable, so the write stays invisible --
        # even to a query executing for the first time.
        assert db.get(read_text) == "old"
        assert db.get(echo_text) == "old"
        db.request_inputs_changed()
        # The declaration rolls the span onto a fresh request: the next reads
        # re-validate and see the write.
        assert db.get(read_text) == "new!"
        assert db.get(echo_text) == "new!"

    # Outside a span every call opens its own request; declaring a change is
    # a no-op rather than the start of anything.
    requests_before = db.statistics().total_requests
    db.request_inputs_changed()
    assert db.statistics().total_requests == requests_before
    assert db.get(read_text) == "new!"


def test_request_span_delivers_observer_events_at_close() -> None:
    number = Input[int]("number")

    @query
    def doubled(db: Database) -> int:
        return number.read(db) * 2

    db = Database()
    db.set(number, 1)
    events: list[QueryChangeEvent] = []
    db.observe(events.append, doubled)

    with db.request_span():
        assert db.get(doubled) == 2  # cold execution
        assert events == []
        # set() declares its own change: the next get inside the span must
        # re-execute rather than reuse the request's earlier answer.
        db.set(number, 5)
        assert db.get(doubled) == 10
        assert events == []
    assert [event.decision for event in events] == ["executed", "executed"]
    assert events[1].changed_at > events[0].changed_at


def test_request_span_delivers_committed_events_when_the_span_body_raises() -> None:
    number = Input[int]("number")

    @query
    def doubled(db: Database) -> int:
        return number.read(db) * 2

    @query
    def exploding(db: Database) -> int:
        raise RuntimeError("mid-span failure")

    db = Database()
    db.set(number, 3)
    events: list[QueryChangeEvent] = []
    db.observe(events.append, doubled)

    with pytest.raises(RuntimeError, match="mid-span failure"), db.request_span():
        assert db.get(doubled) == 6  # committed before the failure
        assert events == []
        db.get(exploding)

    # A failing remainder of the request does not undo the committed work: a
    # plain top-level get delivers events for what it committed even when a
    # later part of the request fails, and the span must match.
    assert [event.decision for event in events] == ["executed"]
    assert events[0].query_id == doubled.key


def test_outer_span_continues_and_delivers_after_an_inner_span_raises() -> None:
    number = Input[int]("number")

    @query
    def doubled(db: Database) -> int:
        return number.read(db) * 2

    @query
    def exploding(db: Database) -> int:
        raise RuntimeError("inner failure")

    db = Database()
    db.set(number, 2)
    events: list[QueryChangeEvent] = []
    db.observe(events.append, doubled)

    with db.request_span():
        assert db.get(doubled) == 4
        with pytest.raises(RuntimeError, match="inner failure"), db.request_span():
            db.get(exploding)
        # The inner span joined the outer request: its failing close neither
        # delivered events early nor ended the request.
        assert events == []
        db.set(number, 5)
        assert db.get(doubled) == 10
    assert [event.decision for event in events] == ["executed", "executed"]
    assert events[1].changed_at > events[0].changed_at


def test_request_span_extends_failure_exception_lifetime(tmp_path: Path) -> None:
    resource = _SpanFailingResource()
    target = str(tmp_path / "absent.txt")
    db = Database()

    with db.request_span():
        with pytest.raises(FileNotFoundError) as first:
            resource.read(db, target)
        with pytest.raises(FileNotFoundError) as second:
            resource.read(db, target)
        # One load serves the whole span; later reads re-raise its exception.
        assert second.value is first.value
        assert _span_tallied(target).count("l") == 1
        record = db._records[db._resource_key(resource, target)]
        assert record.failure_exc is first.value

    # The span end is the request end: the retained exception and traceback
    # are dropped, and the next read re-runs the load for a live one.
    assert record.failure_exc is None
    assert record.failure_traceback is None
    with pytest.raises(FileNotFoundError) as later:
        resource.read(db, target)
    assert later.value is not first.value
    assert _span_tallied(target).count("l") == 2


def test_nested_request_spans_join_the_outermost(tmp_path: Path) -> None:
    resource = _SpanTalliedResource()
    target = str(tmp_path / "data.txt")
    Path(target).write_text("hello", encoding="utf-8")

    @query
    def read_text(db: Database) -> str:
        return resource.read(db, target)

    number = Input[int]("number")

    @query
    def doubled(db: Database) -> int:
        return number.read(db) * 2

    db = Database()
    db.set(number, 1)
    events: list[QueryChangeEvent] = []
    db.observe(events.append, doubled)
    assert db.get(read_text) == "hello"
    requests_before = db.statistics().total_requests

    with db.request_span():
        assert db.get(read_text) == "hello"
        with db.request_span():
            assert db.get(read_text) == "hello"
            assert db.get(doubled) == 2
        # The inner span joined the outer request: closing it neither
        # delivered events nor ended the request.
        assert events == []
        assert db.get(read_text) == "hello"
    assert [event.decision for event in events] == ["executed"]
    assert _span_tallied(target) == "pl" + "p"
    assert db.statistics().total_requests == requests_before + 1


def test_request_span_inside_a_get_joins_that_request(tmp_path: Path) -> None:
    resource = _SpanTalliedResource()
    target = str(tmp_path / "data.txt")
    Path(target).write_text("hi", encoding="utf-8")

    @query
    def child(db: Database) -> str:
        return resource.read(db, target)

    @query
    def parent(db: Database) -> str:
        with db.request_span():
            return db.get(child) + "!"

    db = Database()
    events: list[QueryChangeEvent] = []
    db.observe(events.append, child)
    requests_before = db.statistics().total_requests
    assert db.get(parent) == "hi!"
    # The get already holds the request; the span joined it instead of
    # opening (or closing) one of its own, and the child's event was
    # delivered by the get exactly as without the span.
    assert db.statistics().total_requests == requests_before + 1
    assert [event.query_id for event in events] == [child.key]

    assert db.get(parent) == "hi!"
    assert _span_tallied(target) == "pl" + "p"


def test_cross_thread_set_rolls_an_open_span_onto_the_new_input() -> None:
    number = Input[int]("number")

    @query
    def doubled(db: Database) -> int:
        return number.read(db) * 2

    db = Database()
    db.set(number, 1)
    assert db.get(doubled) == 2

    with db.request_span():
        assert db.get(doubled) == 2
        # A committed set from another thread is the same declared change a
        # same-thread set is: the span must stop answering from the request
        # it settled before the commit.
        writer = threading.Thread(target=db.set, args=(number, 5))
        writer.start()
        writer.join()
        assert db.get(doubled) == 10


def test_cross_thread_set_many_rolls_an_open_span_onto_the_new_inputs() -> None:
    first = Input[int]("first")
    second = Input[int]("second")

    @query
    def total(db: Database) -> int:
        return first.read(db) + second.read(db)

    db = Database()
    db.set_many([(first, 1), (second, 2)])
    assert db.get(total) == 3

    with db.request_span():
        assert db.get(total) == 3
        writer = threading.Thread(target=db.set_many, args=([(first, 10), (second, 20)],))
        writer.start()
        writer.join()
        assert db.get(total) == 30


def test_equal_ignored_cross_thread_set_keeps_the_span_settled(tmp_path: Path) -> None:
    resource = _SpanTalliedResource()
    target = str(tmp_path / "data.txt")
    Path(target).write_text("hello", encoding="utf-8")
    number = Input[int]("number")

    @query
    def read_text(db: Database) -> str:
        return resource.read(db, target)

    db = Database()
    db.set(number, 1)
    assert db.get(read_text) == "hello"

    with db.request_span():
        assert db.get(read_text) == "hello"
        writer = threading.Thread(target=db.set, args=(number, 1))
        writer.start()
        writer.join()
        # The equal update was ignored -- nothing changed, so the span stays
        # on its request and the next get re-validates nothing.
        assert db.statistics().input_equal_ignores == 1
        assert db.get(read_text) == "hello"
    assert _span_tallied(target) == "pl" + "p"


def test_cross_thread_request_inputs_changed_reopens_the_span(tmp_path: Path) -> None:
    resource = _SpanTalliedResource()
    target = str(tmp_path / "data.txt")
    Path(target).write_text("old", encoding="utf-8")

    @query
    def read_text(db: Database) -> str:
        return resource.read(db, target)

    db = Database()
    assert db.get(read_text) == "old"

    with db.request_span():
        assert db.get(read_text) == "old"
        Path(target).write_text("new!", encoding="utf-8")
        # The span declares the world stable, so the write stays invisible
        # until some thread declares the change.
        assert db.get(read_text) == "old"
        declarer = threading.Thread(target=db.request_inputs_changed)
        declarer.start()
        declarer.join()
        assert db.get(read_text) == "new!"


def _held_wrapper() -> FrozenList:
    return cast(FrozenList, freeze([1, 2, 3]))


def _corrupt(wrapper: FrozenList) -> None:
    object.__setattr__(wrapper, "items", (9, 9, 9))


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("entry_point", ["set", "set_many"])
def test_input_boundary_owns_prefrozen_wrappers(mode: str, entry_point: str) -> None:
    held = _held_wrapper()
    payload = Input[object]("owned-input")

    @query
    def echoed(db: Database) -> object:
        return payload.read(db)

    db = Database(mode=mode)
    if entry_point == "set":
        db.set(payload, held)
    else:
        db.set_many([(payload, held)])
    first = db.get(echoed)
    assert list(cast(Any, first)) == [1, 2, 3]

    record = db._records[db._input_key(payload)]
    assert record.snapshot is not held
    _corrupt(held)
    assert fingerprint_snapshot(record.snapshot) == record.digest

    fresh = Database(mode=mode)
    fresh.set(payload, freeze([1, 2, 3]))
    assert list(cast(Any, db.get(echoed))) == list(cast(Any, fresh.get(echoed)))


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_query_argument_round_trip_serves_the_ingested_value(mode: str) -> None:
    # The wrapper crosses as an ARGUMENT and comes back as the RESULT rather
    # than being captured in the query closure: captured-value mutation will
    # later re-key a query, which would quietly turn a closure-based version of
    # this pin into a fresh execution.
    #
    # This test stays GREEN with freeze's wrapper detach reverted -- the
    # argument envelope rebuilds the wrapper before the body ever sees it, so
    # nothing here can observe the result-ingest boundary. That boundary is
    # pinned by test_query_result_boundary_owns_returned_wrappers; do not
    # delete it as a duplicate of this one.
    held = _held_wrapper()

    @query
    def echo(db: Database, value: object) -> object:
        return value

    db = Database(mode=mode)
    first = db.get(echo, held)
    assert list(cast(Any, first)) == [1, 2, 3]

    record = _query_record(db, echo, held)
    assert record.snapshot is not held

    _corrupt(held)
    assert fingerprint_snapshot(record.snapshot) == record.digest
    # An equal-encoding argument keys the same node: the warm hit must serve
    # the ingested value, untouched by the caller's reflective mutation.
    assert list(cast(Any, db.get(echo, freeze([1, 2, 3])))) == [1, 2, 3]


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_query_result_boundary_owns_returned_wrappers(mode: str) -> None:
    # The sibling above routes the wrapper through the argument envelope, which
    # rebuilds it before the body ever runs -- so that test cannot observe the
    # result boundary on its own. Returning a captured wrapper is the only way
    # to hand the result freeze the caller's object. This pin therefore takes a
    # single get and never re-reads after the corruption, so it stays honest
    # once a captured value's mutation re-keys its query.
    held = _held_wrapper()

    @query
    def emit(db: Database) -> object:
        return held

    db = Database(mode=mode)
    assert list(cast(Any, db.get(emit))) == [1, 2, 3]

    record = _query_record(db, emit)
    assert record.snapshot is not held
    _corrupt(held)
    assert fingerprint_snapshot(record.snapshot) == record.digest


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_query_argument_envelope_never_aliased_the_caller(mode: str) -> None:
    # Pin of a NON-bug: the (args, kwargs) call envelope always takes the
    # freeze memo path (the kwargs dict forces a rebuild), so the retained
    # call snapshot never aliased the caller even before the ownership fix.
    held = _held_wrapper()

    @query
    def width(db: Database, payload: object) -> int:
        return len(cast(Any, payload))

    db = Database(mode=mode)
    assert db.get(width, held) == 3
    key, call_snapshot = db._query_key(width, (held,), {})
    assert call_snapshot[0][0] is not held
    assert key in db._records


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_resource_load_boundary_owns_prefrozen_wrappers(mode: str) -> None:
    held = _held_wrapper()

    # A query's (and a resource method's) capture set may not contain mutable
    # state, so the held wrapper is captured directly rather than through the
    # holder dict a probe/load pair would normally share.
    @dataclass(frozen=True)
    class _HeldValueResource(Resource[str, Any, Any]):
        def probe(self, key: str) -> Any:
            return "stable"

        def load(self, db: Database, key: str) -> Any:
            return held

        def label(self, key: str) -> str:
            return f"held-value[{key}]"

    resource = _HeldValueResource()

    @query
    def loaded(db: Database) -> object:
        return resource.read(db, "cell")

    db = Database(mode=mode)
    first = db.get(loaded)
    assert list(cast(Any, first)) == [1, 2, 3]

    record = db._records[db._resource_key(resource, "cell")]
    assert record.snapshot is not held
    _corrupt(held)
    assert fingerprint_snapshot(record.snapshot) == record.digest
    assert list(cast(Any, db.get(loaded))) == [1, 2, 3]


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_adapter_payload_boundary_owns_prefrozen_wrappers(mode: str) -> None:
    held = _held_wrapper()

    class _HeldPayloadAdapter(ValueAdapter):
        def freeze(self, value: Any, freeze_fn: Any) -> Any:
            return held

        def thaw(self, snapshot: Any, thaw_fn: Any) -> Any:
            # The cast keeps mypy strict green: Boxed.__init__ declares an int
            # payload, and this test never invokes thaw.
            return Boxed(cast(Any, list(snapshot)))

    boxed = Input[Boxed]("owned-adapter")
    db = Database(mode=mode, adapters={Boxed: _HeldPayloadAdapter()})
    db.set(boxed, Boxed(0))

    record = db._records[db._input_key(boxed)]
    payload = cast(FrozenAdapterValue, record.snapshot).payload
    assert payload is not held
    _corrupt(held)
    assert fingerprint_snapshot(record.snapshot) == record.digest


@dataclass
class _TokenProbeCell:
    """The world a held-token resource models: a probe token and a payload.

    It rides on the resource instance and stays out of ``identity()``. A
    resource's method capture set may not hold ambient mutable state, and the
    world a resource observes is not part of what distinguishes the resource,
    so the node key is unaffected when either half moves.
    """

    token: Any
    payload: str = ""


@dataclass(frozen=True)
class _HeldTokenResource(Resource[str, str, Any]):
    """Probe returns a held pre-frozen wrapper token; load returns a string."""

    cell: _TokenProbeCell

    def identity(self) -> Any:
        return "held-token"

    def probe(self, key: str) -> Any:
        return self.cell.token

    def load(self, db: Database, key: str) -> str:
        return self.cell.payload

    def label(self, key: str) -> str:
        return f"held-token[{key}]"


@dataclass(frozen=True)
class _FailingHeldTokenResource(Resource[str, str, Any]):
    """Probe returns a held pre-frozen wrapper token; the load always raises."""

    cell: _TokenProbeCell

    def identity(self) -> Any:
        return "failing-held-token"

    def probe(self, key: str) -> Any:
        return self.cell.token

    def load(self, db: Database, key: str) -> str:
        raise FileNotFoundError(key)

    def label(self, key: str) -> str:
        return f"failing-held-token[{key}]"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_held_probe_token_is_detached_and_cannot_pin_the_resource(mode: str) -> None:
    token = cast(FrozenList, freeze(["v1"]))
    cell = _TokenProbeCell(token=token, payload="alpha")
    resource = _HeldTokenResource(cell)

    @query
    def loaded(db: Database) -> str:
        return resource.read(db, "pin-cell")

    db = Database(mode=mode)
    assert db.get(loaded) == "alpha"
    record = db._records[db._resource_key(resource, "pin-cell")]
    # The stored probe must be a clone: were it the caller's token, the
    # comparison at the probe-hit gate would be the token compared with
    # itself and could never miss.
    assert record.probe is not token

    # The external world moves: the held token's content changes in place.
    object.__setattr__(token, "items", ("v2",))
    cell.payload = "beta"

    hits_before = db.statistics().resource_probe_hits
    assert db.get(loaded) == "beta"
    assert db.statistics().resource_probe_hits == hits_before

    fresh = Database(mode=mode)
    assert fresh.get(loaded) == "beta"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_failure_record_probe_token_is_detached(mode: str) -> None:
    token = cast(FrozenList, freeze(["broken-v1"]))
    resource = _FailingHeldTokenResource(_TokenProbeCell(token=token))

    @query
    def loaded(db: Database) -> str:
        return resource.read(db, "fail-cell")

    db = Database(mode=mode)
    with pytest.raises(FileNotFoundError):
        db.get(loaded)
    key = db._resource_key(resource, "fail-cell")
    record = db._records[key]
    assert record.probe is not token
    assert fingerprint_snapshot(record.probe) == fingerprint_snapshot(freeze(["broken-v1"]))

    # A failure record that held the caller's token would compare it with
    # itself too, so a failing world that moves would keep changed_at frozen
    # and leave every dependent of the failure green across the change.
    changed_at_before = record.changed_at
    object.__setattr__(token, "items", ("broken-v2",))
    with pytest.raises(FileNotFoundError):
        db.get(loaded)
    assert db._records[key].changed_at > changed_at_before


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_checkpoint_preserves_owned_input_snapshots_after_caller_mutation(
    mode: str,
) -> None:
    # A checkpoint is where a corrupted snapshot would become durable, so the
    # reload has to answer from the persisted bytes and match a from-scratch
    # database. This stays GREEN with freeze's wrapper detach reverted: reading
    # an input thaws it before the query result is frozen, so the caller's shell
    # never reaches the persisted query snapshot. The input boundary itself is
    # pinned by test_input_boundary_owns_prefrozen_wrappers; this one pins that
    # the ownership survives the round trip.
    held = _held_wrapper()
    payload = Input[object]("ckp-owned-input")

    @query
    def echoed(db: Database) -> object:
        return payload.read(db)

    store = InMemoryArtifactStore()
    saver = Database(mode=mode, store=store)
    saver.set(payload, held)
    saver.get(echoed)
    _corrupt(held)
    key = saver.save_checkpoint()

    restored = Database(mode=mode, store=store)
    restored.set(payload, freeze([1, 2, 3]))
    restored.load_checkpoint(key)

    fresh = Database(mode=mode)
    fresh.set(payload, freeze([1, 2, 3]))
    executions_before = restored.statistics().query_executions
    assert list(cast(Any, restored.get(echoed))) == list(cast(Any, fresh.get(echoed)))
    # The warm answer came out of the persisted bytes; a re-derivation would
    # make the equality above prove nothing about what was stored.
    assert restored.statistics().query_executions == executions_before


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_checkpoint_preserves_owned_result_snapshots_after_caller_mutation(
    mode: str,
) -> None:
    # The wrapper crosses as an ARGUMENT and comes back as the RESULT rather
    # than being captured in the query closure, for the same reason the
    # non-checkpoint sibling does: captured-value mutation will later re-key a
    # query and would quietly turn a closure-based version of this into a fresh
    # execution. The envelope rebuilds the wrapper before the body runs, so this
    # too stays green with the detach reverted -- what it pins is that the
    # reload serves the ingested bytes rather than re-deriving them.
    held = _held_wrapper()

    @query
    def echo(db: Database, value: object) -> object:
        return value

    store = InMemoryArtifactStore()
    saver = Database(mode=mode, store=store)
    assert list(cast(Any, saver.get(echo, held))) == [1, 2, 3]
    _corrupt(held)
    key = saver.save_checkpoint()

    restored = Database(mode=mode, store=store)
    restored.load_checkpoint(key)
    # The same args encoding keys the checkpoint-warmed node; the restored
    # answer is the ingested bytes, not a view through the caller's object.
    executions_before = restored.statistics().query_executions
    assert list(cast(Any, restored.get(echo, freeze([1, 2, 3])))) == [1, 2, 3]
    assert restored.statistics().query_executions == executions_before

    fresh = Database(mode=mode)
    assert list(cast(Any, fresh.get(echo, freeze([1, 2, 3])))) == [1, 2, 3]


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_checkpoint_hint_restore_owns_the_probe_it_stores(mode: str) -> None:
    # Restoring a recordless resource from its checkpoint probe hint is the one
    # place a record is born without a load, and the probe it keeps is the live
    # one the caller's object produced. A retained probe would compare with
    # itself at the next request and pin the restored value forever.
    token = cast(FrozenList, freeze(["v1"]))
    cell = _TokenProbeCell(token=token, payload="alpha")
    resource = _HeldTokenResource(cell)

    @query
    def loaded(db: Database) -> str:
        return resource.read(db, "hint-cell")

    store = InMemoryArtifactStore()
    saver = Database(mode=mode, store=store)
    assert saver.get(loaded) == "alpha"
    key = saver.save_checkpoint()

    restored = Database(mode=mode, store=store)
    restored.load_checkpoint(key)
    loads_before = restored.statistics().resource_loads
    assert restored.get(loaded) == "alpha"
    # The hint answered: no load ran, so the record was born on the restore path.
    assert restored.statistics().resource_loads == loads_before
    record = restored._records[restored._resource_key(resource, "hint-cell")]
    assert record.probe is not token

    object.__setattr__(token, "items", ("v2",))
    cell.payload = "beta"
    assert restored.get(loaded) == "beta"

    fresh = Database(mode=mode)
    assert fresh.get(loaded) == "beta"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_nan_verdict_is_identical_on_input_and_query_paths(mode: str) -> None:
    # One relation: db.set(x, nan) twice is an equal update (was: a change),
    # and a query returning nan twice still backdates. The two paths can no
    # longer drift because both call snapshots_equal on stored snapshots.
    marker = Input[float]("nan-input")
    db = Database(mode=mode)
    db.set(marker, float("nan"))
    sets_before = db.statistics().input_sets
    ignores_before = db.statistics().input_equal_ignores
    db.set(marker, float("nan"))
    assert db.statistics().input_sets == sets_before
    assert db.statistics().input_equal_ignores == ignores_before + 1

    stage = Input[int]("nan-query-stage")

    @query
    def produce(db_: Database) -> float:
        stage.read(db_)
        return float("nan")

    db.set(stage, 0)
    db.get(produce)
    backdates_before = db.statistics().query_backdates
    db.set(stage, 1)
    db.get(produce)
    assert db.statistics().query_backdates == backdates_before + 1


# Pairs Python's == calls equal and the canonical encoding separates. Each is a
# flip a warm database has to execute through, so that its answer is the answer
# a database built from scratch would give.
_TOWER_FLIPS: tuple[tuple[object, object], ...] = ((1, 1.0), (1, True), (0.0, -0.0))


def _tower_repr(value: object) -> str:
    return f"{type(value).__name__}:{value!r}"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize(("first", "second"), _TOWER_FLIPS)
def test_result_type_flip_matches_fresh(
    mode: str, first: object, second: object
) -> None:
    stage = Input[int]("stage")

    @query
    def measure(db: Database) -> object:
        return first if stage.read(db) == 0 else second

    @query
    def described(db: Database) -> str:
        return _tower_repr(measure(db))

    db = Database(mode=mode)
    db.set(stage, 0)
    db.get(described)
    changed_at = _inspect_node(db, measure).changed_at

    db.set(stage, 1)
    warm = db.get(described)
    # The flip published a new value: a backdate would have held measure's
    # changed_at at the pre-flip revision and left the reader on the old type.
    # measure's own last_decision is not the witness here -- the reader reaches
    # it a second time while re-executing, which restamps it as reused.
    assert _inspect_node(db, measure).changed_at > changed_at
    assert _inspect_node(db, described).last_decision == "executed"

    fresh = Database(mode=mode)
    fresh.set(stage, 1)
    assert warm == fresh.get(described) == _tower_repr(second)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize(("first", "second"), _TOWER_FLIPS)
@pytest.mark.parametrize("entry_point", ["set", "set_many"])
def test_input_type_flip_matches_fresh(
    mode: str, first: object, second: object, entry_point: str
) -> None:
    point = Input[object]("tower-point")

    @query
    def describe(db: Database) -> str:
        return _tower_repr(point.read(db))

    db = Database(mode=mode)
    if entry_point == "set":
        db.set(point, first)
        db.get(describe)
        db.set(point, second)
    else:
        db.set_many([(point, first)])
        db.get(describe)
        db.set_many([(point, second)])
    assert db.statistics().input_equal_ignores == 0

    fresh = Database(mode=mode)
    fresh.set(point, second)
    assert db.get(describe) == fresh.get(describe)
    assert _inspect_node(db, describe).last_decision == "executed"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize(("first", "second"), _TOWER_FLIPS)
def test_probe_token_type_flip_reloads(
    mode: str, first: object, second: object
) -> None:
    # Settled at the probe-hit gate, which compares the stored probe with the
    # live one and has nothing standing in front of it: no digest filter, no
    # second opinion. A flip that gate calls equal serves the stale payload.
    cell = _TokenProbeCell(token=first, payload="alpha")
    resource = _HeldTokenResource(cell)

    @query
    def loaded(db: Database) -> str:
        return resource.read(db, "flip-cell")

    db = Database(mode=mode)
    assert db.get(loaded) == "alpha"

    cell.token = second
    cell.payload = "beta"
    hits_before = db.statistics().resource_probe_hits
    assert db.get(loaded) == "beta"
    assert db.statistics().resource_probe_hits == hits_before

    fresh = Database(mode=mode)
    assert fresh.get(loaded) == "beta"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_failing_probe_type_flip_counts_as_changed(mode: str) -> None:
    # Settled by the failure record's own probe comparison, which likewise
    # stands alone: a failure record carries no digest to filter on.
    cell = _TokenProbeCell(token=1)
    resource = _FailingHeldTokenResource(cell)

    @query
    def loaded(db: Database) -> str:
        return resource.read(db, "fail-flip-cell")

    db = Database(mode=mode)
    with pytest.raises(FileNotFoundError):
        db.get(loaded)
    key = db._resource_key(resource, "fail-flip-cell")
    changed_at_before = db._records[key].changed_at

    cell.token = 1.0
    with pytest.raises(FileNotFoundError):
        db.get(loaded)
    # An int probe and a float probe are different observations: the failure
    # record must register a change, not an unchanged-failure backdate.
    assert db._records[key].changed_at > changed_at_before


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize(("first", "second"), _TOWER_FLIPS)
def test_checkpoint_hint_probe_type_flip_reloads(
    mode: str, first: object, second: object
) -> None:
    # Settled by the hint-restore gate, the third probe comparison. It has
    # neither a digest filter nor a record to fall back on, so a flip it calls
    # equal hands back checkpoint bytes for a world that has already moved.
    cell = _TokenProbeCell(token=first, payload="alpha")
    resource = _HeldTokenResource(cell)

    @query
    def loaded(db: Database) -> str:
        return resource.read(db, "hint-flip-cell")

    store = InMemoryArtifactStore()
    saver = Database(mode=mode, store=store)
    assert saver.get(loaded) == "alpha"
    key = saver.save_checkpoint()

    cell.token = second
    cell.payload = "beta"
    restored = Database(mode=mode, store=store)
    restored.load_checkpoint(key)
    loads_before = restored.statistics().resource_loads
    assert restored.get(loaded) == "beta"
    # The hint declined: this value came from a load and not from the
    # checkpoint bytes -- exactly one load, the miss falling straight through.
    assert restored.statistics().resource_loads == loads_before + 1

    fresh = Database(mode=mode)
    assert fresh.get(loaded) == "beta"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_checkpoint_hint_probe_without_flip_restores_from_store(mode: str) -> None:
    # The control for the flip pin above. With the probe standing still the
    # hint is meant to fire, so the miss up there is the flip's doing rather
    # than a hint that never restores anything in the first place.
    cell = _TokenProbeCell(token=1, payload="alpha")
    resource = _HeldTokenResource(cell)

    @query
    def loaded(db: Database) -> str:
        return resource.read(db, "hint-steady-cell")

    store = InMemoryArtifactStore()
    saver = Database(mode=mode, store=store)
    assert saver.get(loaded) == "alpha"
    key = saver.save_checkpoint()

    # The probe holds, but what a load would return moves: only a restore from
    # the checkpoint bytes can still answer "alpha" here.
    cell.payload = "beta"
    restored = Database(mode=mode, store=store)
    restored.load_checkpoint(key)
    hits_before = restored.statistics().resource_probe_hits
    loads_before = restored.statistics().resource_loads
    assert restored.get(loaded) == "alpha"
    assert restored.statistics().resource_probe_hits == hits_before + 1
    assert restored.statistics().resource_loads == loads_before


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize(("first", "second"), _TOWER_FLIPS)
def test_cutoff_token_type_flip_executes_downstream(
    mode: str, first: object, second: object
) -> None:
    # Settled by the cutoff-token comparison. A declared cutoff sends the
    # recompute decision down the policy arm, which has no digest pre-filter
    # in front of it, so the token relation is the entire verdict here.
    stage = Input[int]("cutoff-flip-stage")

    @query(cutoff=lambda value: value["token"])
    def gated(db: Database) -> dict[str, Any]:
        step = stage.read(db)
        return {"token": first if step == 0 else second, "step": step}

    @query
    def downstream(db: Database) -> str:
        return _tower_repr(gated(db)["token"])

    db = Database(mode=mode)
    db.set(stage, 0)
    db.get(downstream)
    db.set(stage, 1)
    warm = db.get(downstream)

    fresh = Database(mode=mode)
    fresh.set(stage, 1)
    assert warm == fresh.get(downstream) == _tower_repr(second)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_nan_cutoff_token_backdates(mode: str) -> None:
    stage = Input[int]("cutoff-nan-stage")

    @query(cutoff=lambda value: value["token"])
    def gated(db: Database) -> dict[str, Any]:
        return {"token": float("nan"), "step": stage.read(db)}

    db = Database(mode=mode)
    db.set(stage, 0)
    db.get(gated)
    db.set(stage, 1)
    db.get(gated)
    # Canonical NaN tokens are equal under the one relation, so an unchanged
    # NaN token is a backdate, exactly as an unchanged NaN result is. Same
    # deciding site as the flip pin above, reached with the same policy arm:
    # nothing but the token comparison can produce this decision.
    assert _inspect_node(db, gated).last_decision == "backdated"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize(("first", "second"), _TOWER_FLIPS)
def test_checkpoint_reload_after_type_flip_matches_fresh(
    mode: str, first: object, second: object
) -> None:
    # The saving database reached its answer by executing through a flip. A
    # checkpoint has to carry that answer across the process boundary: warm
    # from durable state is only sound while it is what a fresh run gives.
    stage = Input[int]("ckp-flip-stage")

    @query
    def measure(db: Database) -> object:
        return first if stage.read(db) == 0 else second

    @query
    def described(db: Database) -> str:
        return _tower_repr(measure(db))

    store = InMemoryArtifactStore()
    saver = Database(mode=mode, store=store)
    saver.set(stage, 0)
    saver.get(described)
    saver.set(stage, 1)
    assert saver.get(described) == _tower_repr(second)
    key = saver.save_checkpoint()

    restored = Database(mode=mode, store=store)
    restored.set(stage, 1)
    restored.load_checkpoint(key)
    executions_before = restored.statistics().query_executions

    fresh = Database(mode=mode)
    fresh.set(stage, 1)
    assert restored.get(described) == fresh.get(described) == _tower_repr(second)
    # Warm, not recomputed: the reload answered the request from the
    # checkpoint, so this is the stored answer being right and not a
    # re-execution papering over a stored one that was wrong.
    assert restored.statistics().query_executions == executions_before


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_checkpoint_reload_after_nan_backdate_matches_fresh(mode: str) -> None:
    # The NaN half of the same question. Here the saving database backdated --
    # canonical NaN equals canonical NaN -- so the checkpoint persists a record
    # whose value outlived a change to its dependency. It still has to reload
    # into the answer a fresh database gives.
    stage = Input[int]("ckp-nan-stage")

    @query
    def produce(db: Database) -> float:
        stage.read(db)
        return float("nan")

    @query
    def described(db: Database) -> str:
        return _tower_repr(produce(db))

    store = InMemoryArtifactStore()
    saver = Database(mode=mode, store=store)
    saver.set(stage, 0)
    saver.get(described)
    backdates_before = saver.statistics().query_backdates
    saver.set(stage, 1)
    assert saver.get(described) == "float:nan"
    assert saver.statistics().query_backdates == backdates_before + 1
    key = saver.save_checkpoint()

    restored = Database(mode=mode, store=store)
    restored.set(stage, 1)
    restored.load_checkpoint(key)
    executions_before = restored.statistics().query_executions

    fresh = Database(mode=mode)
    fresh.set(stage, 1)
    assert restored.get(described) == fresh.get(described) == "float:nan"
    assert restored.statistics().query_executions == executions_before


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_thaw_colliding_wrappers_are_refused_at_database_boundaries(
    mode: str,
) -> None:
    colliding_entries: tuple[tuple[Any, Any], ...] = tuple(
        sorted(
            [(1, "a"), (1.0, "b")],
            key=lambda entry: fingerprint_snapshot(entry[0]),
        )
    )
    colliding = FrozenDict(colliding_entries)

    @query
    def size(db: Database, payload: object) -> int:
        return len(cast(Any, payload))

    db = Database(mode=mode)
    with pytest.raises(UnsupportedValueError, match="collapse"):
        db.get(size, colliding)

    holder = Input[object]("collider")
    with pytest.raises(UnsupportedValueError, match="collapse"):
        db.set(holder, colliding)
    with pytest.raises(UnsupportedValueError, match="collapse"):
        db.set_many([(holder, colliding)])


def test_store_bytes_carrying_a_thaw_collision_are_not_warmed_into_a_database() -> None:
    # The store-warm entry point. Every warmed byte path decodes through
    # Database._read_validated_snapshot, which validates the payload and
    # answers a refused one as a missing artifact rather than handing back a
    # snapshot whose thaw would drop a key. Bytes stored under their own true
    # digest -- the digest check below passes -- therefore warm nothing.
    payload = b"K2;D2:f20:0x1.0000000000000p+0;s1:b;i1:1;s1:a;;"
    digest = hashlib.sha256(payload).hexdigest()
    store = InMemoryArtifactStore()
    store.put(digest, payload)
    db = Database(store=store)
    assert db._read_validated_snapshot(store, digest) is _MISSING_SNAPSHOT

    # Control: the sentinel above is the collapse refusal, not one of the other
    # ways this method answers missing -- an absent or non-bytes payload, a
    # digest mismatch, a decode error, a fingerprint mismatch. The same
    # construction over a single-key mapping warms and hands back its snapshot.
    control = serialize_snapshot(FrozenDict(((1, "a"),)))
    control_digest = hashlib.sha256(control).hexdigest()
    store.put(control_digest, control)
    assert db._read_validated_snapshot(store, control_digest) == FrozenDict(((1, "a"),))
