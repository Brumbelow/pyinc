from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pyinc import Database, FileResource, Input, MutationError, query
from pyinc.integrations.python_source import workspace_analysis

Operation = tuple[str, int | str]
WorkspaceState = tuple[str, str, bool]


def operation_sequences() -> st.SearchStrategy[list[Operation]]:
    choose_side = st.tuples(st.just("chooser"), st.sampled_from(["left", "right"]))
    update_value = st.one_of(
        st.tuples(st.just("left"), st.integers(min_value=-20, max_value=20)),
        st.tuples(st.just("right"), st.integers(min_value=-20, max_value=20)),
        st.tuples(st.just("offset"), st.integers(min_value=-20, max_value=20)),
    )
    return st.lists(st.one_of(choose_side, update_value), min_size=1, max_size=30)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("max_query_nodes", [None, 2])
@settings(max_examples=50, deadline=None)
@given(steps=operation_sequences())
def test_incremental_results_match_fresh_recomputation(
    mode: str,
    max_query_nodes: int | None,
    steps: list[Operation],
) -> None:
    chooser = Input[str]("chooser")
    left = Input[int]("left")
    right = Input[int]("right")
    offset = Input[int]("offset")
    state: dict[str, int | str] = {"chooser": "left", "left": 0, "right": 0, "offset": 0}
    inputs = {
        "chooser": chooser,
        "left": left,
        "right": right,
        "offset": offset,
    }

    @query
    def selected(db: Database) -> int:
        if chooser.read(db) == "left":
            return left.read(db)
        return right.read(db)

    @query
    def parity(db: Database) -> str:
        return "even" if selected(db) % 2 == 0 else "odd"

    @query
    def describe(db: Database) -> tuple[str, int]:
        return parity(db), selected(db) + offset.read(db)

    incremental = Database(mode=mode, max_query_nodes=max_query_nodes)
    for name, value in state.items():
        incremental.set(inputs[name], value)

    for name, value in steps:
        state[name] = value
        incremental.set(inputs[name], value)

        fresh = Database(mode=mode, max_query_nodes=max_query_nodes)
        for current_name, current_value in state.items():
            fresh.set(inputs[current_name], current_value)

        assert incremental.get(describe) == fresh.get(describe)


def file_contents() -> st.SearchStrategy[list[str]]:
    return st.lists(
        st.sampled_from(
            [
                "",
                "import os\n",
                "# comment\nimport os\n",
                "import sys\n",
                "value = 1\n",
            ]
        ),
        min_size=1,
        max_size=20,
    )


def workspace_states() -> st.SearchStrategy[list[WorkspaceState]]:
    provider_variant = st.sampled_from(["internal_a", "internal_b", "export_a", "export_b"])
    consumer_variant = st.sampled_from(
        ["provider_only", "provider_and_helper", "provider_star", "external_only"]
    )
    helper_present = st.booleans()
    return st.lists(
        st.tuples(provider_variant, consumer_variant, helper_present),
        min_size=1,
        max_size=20,
    )


def _provider_source(variant: str) -> str:
    if variant == "internal_a":
        return "def exported() -> int:\n    return 1\n"
    if variant == "internal_b":
        return "def exported() -> int:\n    return 2\n"
    if variant == "export_a":
        return "def exported() -> int:\n    return 1\n"
    return "def exported() -> int:\n    return 1\n\ndef extra() -> int:\n    return 2\n"


def _consumer_source(variant: str) -> str:
    if variant == "provider_only":
        return "from provider import exported\n"
    if variant == "provider_and_helper":
        return "from provider import exported\nfrom pkg import helper\n"
    if variant == "provider_star":
        return "from provider import *\n"
    return "import os\n"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("max_query_nodes", [None, 2])
@settings(max_examples=30, deadline=None)
@given(contents=file_contents())
def test_resource_backed_queries_match_fresh_recomputation(
    mode: str,
    max_query_nodes: int | None,
    contents: list[str],
) -> None:
    files = FileResource()

    @query
    def source(db: Database, filename: str) -> str:
        return files.read(db, filename)

    @query
    def diagnostics(db: Database, filename: str) -> tuple[bool, int]:
        current = source(db, filename)
        return "import os" in current, len(current.splitlines())

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "sample.py"
        incremental = Database(mode=mode, max_query_nodes=max_query_nodes)

        for content in contents:
            path.write_text(content, encoding="utf-8")

            fresh = Database(mode=mode, max_query_nodes=max_query_nodes)
            assert incremental.get(diagnostics, str(path)) == fresh.get(diagnostics, str(path))


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("max_query_nodes", [None, 2])
@settings(max_examples=30, deadline=None)
@given(states=workspace_states())
def test_workspace_queries_match_fresh_recomputation(
    mode: str,
    max_query_nodes: int | None,
    states: list[WorkspaceState],
) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "workspace"
        root.mkdir()
        pkg = root / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")

        incremental = Database(mode=mode, max_query_nodes=max_query_nodes)
        provider = root / "provider.py"
        consumer = root / "consumer.py"
        helper = pkg / "helper.py"

        for provider_variant, consumer_variant, helper_present in states:
            provider.write_text(_provider_source(provider_variant), encoding="utf-8")
            consumer.write_text(_consumer_source(consumer_variant), encoding="utf-8")
            if helper_present:
                helper.write_text("def helper() -> int:\n    return 1\n", encoding="utf-8")
            elif helper.exists():
                helper.unlink()

            fresh = Database(mode=mode, max_query_nodes=max_query_nodes)
            assert workspace_analysis(incremental, root) == workspace_analysis(fresh, root)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=40, deadline=None)
@given(values=st.lists(st.integers(min_value=-20, max_value=20), min_size=1, max_size=20))
def test_aliasing_mutation_boundaries_behave_by_mode(mode: str, values: list[int]) -> None:
    payload = Input[tuple[dict[str, int], dict[str, int]]]("payload")

    @query
    def mutate_left(db: Database) -> int:
        left, right = payload.read(db)
        left["x"] = left["x"] + 1
        return right["x"]

    @query
    def read_right(db: Database) -> int:
        _, right = payload.read(db)
        return right["x"]

    db = Database(mode=mode)
    for value in values:
        # Two independent dicts at the boundary — identity is not preserved across
        # the membrane unless the caller deliberately shared the input. See the
        # companion test that exercises the shared-identity case.
        db.set(payload, ({"x": value}, {"x": value}))
        if mode == "fast":
            assert db.get(mutate_left) == value
            assert db.get(read_right) == value
        else:
            with pytest.raises((MutationError, TypeError, AttributeError)):
                db.get(mutate_left)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=20, deadline=None)
@given(values=st.lists(st.integers(min_value=-20, max_value=20), min_size=1, max_size=10))
def test_shared_identity_preserved_across_boundary_in_fast_mode(mode: str, values: list[int]) -> None:
    payload = Input[tuple[dict[str, int], dict[str, int]]]("payload")

    @query
    def mutate_left(db: Database) -> int:
        left, right = payload.read(db)
        left["x"] = left["x"] + 1
        return right["x"]

    @query
    def read_right(db: Database) -> int:
        _, right = payload.read(db)
        return right["x"]

    db = Database(mode=mode)
    for value in values:
        shared = {"x": value}
        db.set(payload, (shared, shared))
        if mode == "fast":
            # left is right after the boundary — mutation propagates within the call.
            assert db.get(mutate_left) == value + 1
            # A separate query thaws fresh; the in-query mutation is not persisted.
            assert db.get(read_right) == value
        else:
            with pytest.raises((MutationError, TypeError, AttributeError)):
                db.get(mutate_left)


def multi_level_rewiring_steps() -> st.SearchStrategy[list[tuple[str, str, int, int, int, int]]]:
    return st.lists(
        st.tuples(
            st.sampled_from(["a", "b"]),       # level0 chooser
            st.sampled_from(["x", "y"]),       # level1 chooser
            st.integers(min_value=-10, max_value=10),  # a
            st.integers(min_value=-10, max_value=10),  # b
            st.integers(min_value=-10, max_value=10),  # x
            st.integers(min_value=-10, max_value=10),  # y
        ),
        min_size=1,
        max_size=20,
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("max_query_nodes", [None, 3])
@settings(max_examples=40, deadline=None)
@given(steps=multi_level_rewiring_steps())
def test_multi_level_rewiring_matches_fresh_recomputation(
    mode: str,
    max_query_nodes: int | None,
    steps: list[tuple[str, str, int, int, int, int]],
) -> None:
    l0_chooser = Input[str]("l0_chooser")
    l1_chooser = Input[str]("l1_chooser")
    a = Input[int]("a")
    b = Input[int]("b")
    x = Input[int]("x")
    y = Input[int]("y")
    inputs = {"l0_chooser": l0_chooser, "l1_chooser": l1_chooser, "a": a, "b": b, "x": x, "y": y}

    @query
    def level0(db: Database) -> int:
        return a.read(db) if l0_chooser.read(db) == "a" else b.read(db)

    @query
    def level1(db: Database) -> int:
        return x.read(db) if l1_chooser.read(db) == "x" else y.read(db)

    @query
    def combined(db: Database) -> tuple[int, int, str]:
        v0, v1 = level0(db), level1(db)
        return (v0, v1, "even" if (v0 + v1) % 2 == 0 else "odd")

    incremental = Database(mode=mode, max_query_nodes=max_query_nodes)
    state: dict[str, int | str] = {"l0_chooser": "a", "l1_chooser": "x", "a": 0, "b": 0, "x": 0, "y": 0}
    for name, inp in inputs.items():
        incremental.set(inp, state[name])

    for l0c, l1c, av, bv, xv, yv in steps:
        state.update({"l0_chooser": l0c, "l1_chooser": l1c, "a": av, "b": bv, "x": xv, "y": yv})
        for name, inp in inputs.items():
            incremental.set(inp, state[name])

        fresh = Database(mode=mode, max_query_nodes=max_query_nodes)
        for name, inp in inputs.items():
            fresh.set(inp, state[name])

        assert incremental.get(combined) == fresh.get(combined)
