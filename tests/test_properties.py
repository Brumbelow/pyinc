from __future__ import annotations

import math
import site
import tempfile
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pyinc import (
    Database,
    FileResource,
    InMemoryArtifactStore,
    Input,
    MutationError,
    freeze,
    query,
    semantic_equal,
)
from pyinc.integrations.python_source import workspace_analysis

Operation = tuple[str, object]
WorkspaceState = tuple[str, str, bool]
CheckpointOp = tuple[str, object]


def boundary_scalars() -> st.SearchStrategy[object]:
    # The numeric pool behind operation_sequences and checkpoint_op_sequences,
    # so it feeds test_incremental_results_match_fresh_recomputation and
    # test_checkpoint_reload_matches_fresh_recomputation. Integers alone cannot
    # observe a numeric-tower or NaN mistake in the reuse decision, because no
    # two of them are equal-but-differently-typed and none of them is unequal to
    # itself. This pool carries both bool/int collisions, both zeros, a float
    # equal to an int, a non-integral float, and NaN.
    return st.sampled_from([-3, 0, 1, 7, True, False, 0.0, -0.0, 1.0, 2.5, float("nan")])


def operation_sequences() -> st.SearchStrategy[list[Operation]]:
    choose_side = st.tuples(st.just("chooser"), st.sampled_from(["left", "right"]))
    update_value = st.one_of(
        st.tuples(st.just("left"), boundary_scalars()),
        st.tuples(st.just("right"), boundary_scalars()),
        st.tuples(st.just("offset"), boundary_scalars()),
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
    left = Input[object]("left")
    right = Input[object]("right")
    offset = Input[object]("offset")
    state: dict[str, object] = {
        "chooser": "left",
        "left": 0,
        "right": 0,
        "offset": 0,
    }
    inputs = {
        "chooser": chooser,
        "left": left,
        "right": right,
        "offset": offset,
    }

    @query
    def selected(db: Database) -> object:
        if chooser.read(db) == "left":
            return left.read(db)
        return right.read(db)

    @query
    def parity(db: Database) -> str:
        value = cast(Any, selected(db))
        if isinstance(value, float) and math.isnan(value):
            return "nan"
        return "even" if value % 2 == 0 else "odd"

    @query
    def describe(db: Database) -> tuple[str, object]:
        return parity(db), cast(Any, selected(db)) + offset.read(db)

    incremental = Database(mode=mode, max_query_nodes=max_query_nodes)
    for name, value in state.items():
        incremental.set(inputs[name], value)

    for name, value in steps:
        state[name] = value
        incremental.set(inputs[name], value)

        fresh = Database(mode=mode, max_query_nodes=max_query_nodes)
        for current_name, current_value in state.items():
            fresh.set(inputs[current_name], current_value)

        fresh_before = fresh.statistics().query_executions
        fresh_result = fresh.get(describe)
        fresh_executions = fresh.statistics().query_executions - fresh_before

        warm_before = incremental.statistics().query_executions
        assert semantic_equal(incremental.get(describe), fresh_result)
        warm_executions = incremental.statistics().query_executions - warm_before

        # Equality on its own is satisfiable by a warm database that quietly
        # recomputes everything, which would make this property a statement
        # about the query bodies rather than about reuse. Witness the reuse:
        # the cold side really evaluated, the warm side never outworked it, and
        # a repeat request over an unchanged graph executes nothing. Under the
        # eviction cap the graph deliberately drops nodes, so the repeat can
        # only be held to doing strictly less than a cold evaluation there.
        assert fresh_executions > 0
        # A bound, not the discriminating assertion: a warm database that
        # recomputed everything satisfies it with equality. The repeat request
        # below is what separates reuse from silent recomputation.
        assert warm_executions <= fresh_executions
        repeat_before = incremental.statistics().query_executions
        assert semantic_equal(incremental.get(describe), fresh_result)
        repeat_executions = incremental.statistics().query_executions - repeat_before
        if max_query_nodes is None:
            assert repeat_executions == 0
        else:
            assert repeat_executions < fresh_executions


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
        max_size=10,
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


def optional_file_contents() -> st.SearchStrategy[list[str | None]]:
    return st.lists(
        st.sampled_from([None, "", "alpha\n", "beta\n"]),
        min_size=1,
        max_size=15,
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("max_query_nodes", [None, 2])
@settings(max_examples=20, deadline=None)
@given(contents=optional_file_contents())
def test_optional_resource_queries_match_fresh_recomputation(
    mode: str,
    max_query_nodes: int | None,
    contents: list[str | None],
) -> None:
    files = FileResource()

    @query
    def source(db: Database, filename: str) -> str:
        try:
            return files.read(db, filename)
        except FileNotFoundError:
            return "<missing>"

    @query
    def diagnostics(db: Database, filename: str) -> tuple[bool, int]:
        current = source(db, filename)
        return current == "<missing>", len(current)

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "optional.txt"
        incremental = Database(mode=mode, max_query_nodes=max_query_nodes)

        for content in contents:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(content, encoding="utf-8")

            fresh = Database(mode=mode, max_query_nodes=max_query_nodes)
            assert incremental.get(diagnostics, str(path)) == fresh.get(diagnostics, str(path))


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("max_query_nodes", [None, 2])
@settings(max_examples=10, deadline=None)
@given(states=workspace_states())
def test_workspace_queries_match_fresh_recomputation(
    mode: str,
    max_query_nodes: int | None,
    states: list[WorkspaceState],
) -> None:
    # Installed-distribution discovery has its own integration tests. Keep this
    # property focused on workspace graph rewiring instead of re-reading every
    # development-environment METADATA file for each fresh Database.
    with (
        patch.object(site, "getsitepackages", return_value=[]),
        patch.object(site, "getusersitepackages", return_value=""),
        tempfile.TemporaryDirectory() as tmpdir,
    ):
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
@given(
    values=st.lists(st.integers(min_value=-20, max_value=20), min_size=1, max_size=20),
    prefrozen=st.booleans(),
)
def test_aliasing_mutation_boundaries_behave_by_mode(
    mode: str, values: list[int], prefrozen: bool
) -> None:
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
        raw = ({"x": value}, {"x": value})
        # The pre-frozen arm asks whether an already-frozen payload reaches the
        # queries with the same mode behaviour as a raw one. It says nothing
        # about wrapper ownership -- the caller never mutates what it handed
        # over -- so it stays green with the freeze detach reverted too.
        db.set(payload, freeze(raw) if prefrozen else raw)
        if mode == "fast":
            assert db.get(mutate_left) == value
            assert db.get(read_right) == value
        else:
            with pytest.raises((MutationError, TypeError, AttributeError)):
                db.get(mutate_left)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=20, deadline=None)
@given(values=st.lists(st.integers(min_value=-20, max_value=20), min_size=1, max_size=10))
def test_shared_identity_preserved_across_boundary_in_fast_mode(
    mode: str, values: list[int]
) -> None:
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
            st.sampled_from(["a", "b"]),  # level0 chooser
            st.sampled_from(["x", "y"]),  # level1 chooser
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
    inputs = {
        "l0_chooser": l0_chooser,
        "l1_chooser": l1_chooser,
        "a": a,
        "b": b,
        "x": x,
        "y": y,
    }

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
    state: dict[str, int | str] = {
        "l0_chooser": "a",
        "l1_chooser": "x",
        "a": 0,
        "b": 0,
        "x": 0,
        "y": 0,
    }
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


def checkpoint_op_sequences() -> st.SearchStrategy[list[CheckpointOp]]:
    set_scale = st.tuples(st.just("set_scale"), boundary_scalars())
    set_bias = st.tuples(st.just("set_bias"), boundary_scalars())
    write = st.tuples(
        st.just("write"),
        st.sampled_from(["", "a", "abc", "hello world", "x\ny\nz", "unicode-é"]),
    )
    save = st.tuples(st.just("save"), st.just(""))
    get = st.tuples(st.just("get"), st.just(""))
    return st.lists(
        st.one_of(set_scale, set_bias, write, save, get),
        min_size=1,
        max_size=12,
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("max_query_nodes", [None, 2])
@settings(max_examples=30, deadline=None)
@given(steps=checkpoint_op_sequences())
def test_checkpoint_reload_matches_fresh_recomputation(
    mode: str,
    max_query_nodes: int | None,
    steps: list[CheckpointOp],
) -> None:
    scale = Input[object]("ckp_scale")
    bias = Input[object]("ckp_bias")
    files = FileResource()

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cell.txt"

        @query
        def cell_size(db: Database) -> int:
            return len(files.read(db, str(path)))

        @query
        def combiner(db: Database) -> object:
            return cast(Any, cell_size(db)) * scale.read(db) + bias.read(db)

        scale_value: object = 1
        bias_value: object = 0
        content = ""
        path.write_text(content, encoding="utf-8")

        store = InMemoryArtifactStore()
        saver = Database(mode=mode, max_query_nodes=max_query_nodes, store=store)
        saver.set(scale, scale_value)
        saver.set(bias, bias_value)

        last_key: str | None = None
        for kind, payload in steps:
            if kind == "set_scale":
                scale_value = payload
                saver.set(scale, scale_value)
            elif kind == "set_bias":
                bias_value = payload
                saver.set(bias, bias_value)
            elif kind == "write":
                assert isinstance(payload, str)
                content = payload
                path.write_text(content, encoding="utf-8")
            elif kind == "save":
                # Save the graph as-is, whatever state it is in -- including a
                # "dirty" graph whose inputs moved since the root was last
                # evaluated (no get before this save). The save path omits any
                # record whose cached value no longer matches the live graph, so
                # reload never warms a stale value; the strongest proof that the
                # dirty-save soundness fix holds is exercising it here directly.
                last_key = saver.save_checkpoint()
            elif kind == "get":
                saver.get(combiner)

        # Guarantee at least one checkpoint exists to reload from.
        if last_key is None:
            saver.get(combiner)
            last_key = saver.save_checkpoint()

        # Reload the LAST saved checkpoint into a brand-new database over the same
        # store, declare the final state, and evaluate the root.
        reloaded = Database(mode=mode, max_query_nodes=max_query_nodes, store=store)
        reloaded.set(scale, scale_value)
        reloaded.set(bias, bias_value)
        reloaded.load_checkpoint(last_key)

        # A completely fresh database (no store) over the same declared state is
        # the ground truth: whatever the checkpoint restores or invalidates, the
        # reloaded result must match it exactly.
        fresh = Database(mode=mode, max_query_nodes=max_query_nodes)
        fresh.set(scale, scale_value)
        fresh.set(bias, bias_value)

        fresh_before = fresh.statistics().query_executions
        fresh_result = fresh.get(combiner)
        fresh_executions = fresh.statistics().query_executions - fresh_before
        assert fresh_executions > 0

        warm_before = reloaded.statistics().query_executions
        assert semantic_equal(reloaded.get(combiner), fresh_result)
        # A bound again, satisfied with equality by a reload that warmed nothing
        # -- the rewarmed database below is the assertion with teeth.
        assert reloaded.statistics().query_executions - warm_before <= fresh_executions

        # How much the reload can warm depends on how far the graph moved after
        # the last save, so the warm case is pinned directly instead: save the
        # graph this reload just evaluated and load that into a third database.
        # That checkpoint describes the declared state exactly, so the answer
        # has to come back with no query executed at all. Without this witness
        # every equality above would hold just as well against a load_checkpoint
        # that warmed nothing and let each read recompute in silence.
        warm_key = reloaded.save_checkpoint()
        rewarmed = Database(mode=mode, max_query_nodes=max_query_nodes, store=store)
        rewarmed.set(scale, scale_value)
        rewarmed.set(bias, bias_value)
        rewarmed.load_checkpoint(warm_key)
        rewarmed_before = rewarmed.statistics().query_executions
        assert semantic_equal(rewarmed.get(combiner), fresh_result)
        assert rewarmed.statistics().query_executions == rewarmed_before


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=40, deadline=None)
@given(values=st.lists(st.integers(min_value=-20, max_value=20), min_size=1, max_size=8))
def test_prefrozen_wrapper_inputs_and_arguments_stay_detached(
    mode: str, values: list[int]
) -> None:
    # Honest about its reach, arm by arm.
    #
    # The INPUT arm pins detachment, through total_after_mutation alone: that
    # query's first evaluation happens after the caller mutates the wrapper it
    # handed over, so no cached answer can stand in for a fresh read of the
    # stored snapshot. With freeze's wrapper detach reverted it reads the
    # caller's mutated items instead -- 9 against a fresh 6 for values
    # [1, 2, 3] -- in all three modes. The warm `total` assertions do not pin
    # anything -- total is already memoized before the mutation, so its cached
    # answer comes back without the stored snapshot being re-read at all, which
    # is what the execution witness below records.
    #
    # The ARGUMENT arm is coverage on both sides of the fix, because the call
    # envelope hides the mutation: _query_key freezes (args, kwargs) as one
    # graph, and the empty kwargs dict forces the freeze memo path, so
    # _finalize_snapshot inlines refs and rebuilds every wrapper leaf before the
    # body runs. The caller's argument object never reaches the query body, so
    # mutating it afterwards is unobservable in any mode. The pin that goes red
    # without the fix is
    # tests/test_runtime.py::test_query_result_boundary_owns_returned_wrappers,
    # for result ingest; test_query_argument_envelope_never_aliased_the_caller
    # in the same file pins the envelope as a non-bug and is green on both
    # sides. What the argument assertions add is warm-vs-fresh agreement across
    # the pre-frozen ingest path in all three modes.
    payload = Input[object]("prefrozen-owned")

    @query
    def total(db: Database) -> int:
        return sum(list(cast("list[int]", payload.read(db))))

    @query
    def total_after_mutation(db: Database) -> int:
        # The same body as total, which is fine: a query is keyed by
        # module:qualname and never by its body, so this is a separate node with
        # nothing memoized in it. Nothing requests it until the caller mutation
        # below has happened, so its first evaluation is forced to read the
        # stored snapshot rather than answer from a cache.
        return sum(list(cast("list[int]", payload.read(db))))

    @query
    def echo(db: Database, value: object) -> object:
        return value

    db = Database(mode=mode)
    held = freeze(list(values))
    held_argument = freeze(list(values))
    db.set(payload, held)
    expected = sum(values)
    assert db.get(total) == expected
    assert list(cast("list[int]", db.get(echo, held_argument))) == list(values)

    object.__setattr__(held, "items", tuple(v + 1 for v in values))
    object.__setattr__(held_argument, "items", tuple(v + 1 for v in values))

    fresh = Database(mode=mode)
    fresh.set(payload, freeze(list(values)))
    warm_before = db.statistics().query_executions
    assert db.get(total) == fresh.get(total) == expected
    # No execution on this side: the equality above is the memo answering, which
    # is precisely why it cannot discriminate.
    assert db.statistics().query_executions == warm_before

    cold_before = db.statistics().query_executions
    warm_after_mutation = db.get(total_after_mutation)
    # This one did execute, for the first time, with the caller's mutation
    # already applied -- so it reads the stored snapshot rather than a memo.
    assert db.statistics().query_executions > cold_before
    assert semantic_equal(warm_after_mutation, fresh.get(total_after_mutation))
    assert warm_after_mutation == expected

    # Pre-frozen wrappers arrive as input AND argument values: an equal-encoding
    # argument keys the node that held_argument keyed at ingest, and the warm
    # answer is the ingested list, untouched by the mutation.
    assert list(cast("list[int]", db.get(echo, freeze(list(values))))) == list(values)
    assert list(cast("list[int]", fresh.get(echo, freeze(list(values))))) == list(values)
