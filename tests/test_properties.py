from __future__ import annotations

import json
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
from pyinc.integrations import SourcePosition
from pyinc.integrations.notebook import (
    NotebookAnalysis,
    notebook_analysis,
    notebook_analysis_payload,
    workspace_notebook_analysis,
)
from pyinc.integrations.python_source import (
    directory_analysis,
    file_analysis,
    file_analysis_payload,
    imports_for_file,
    module_analysis,
    workspace_analysis,
)
from pyinc.integrations.scope_resolution import scope_tree, symbol_at
from pyinc.integrations.symbol_resolution import class_model, find_references, module_symbol_table

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


# ---------------------------------------------------------------------------
# Integration entrypoints against a fresh read
# ---------------------------------------------------------------------------
#
# One property, four shapes of it: for every generated edit sequence, in every
# mode, and across a checkpoint round trip, a public entrypoint answers a warm
# database exactly as it answers a fresh one. The rows below carry the two
# source-file integrations; the same shape extends to the rest of them.
#
# The document pools are hand-written corpora sampled from, never generated
# text. Every projection in these integrations degenerates to a raw-text escape
# hatch on a parse failure, so a free-form generator would spend its budget on
# documents where the property is trivially true.

_PY_BASE = (
    "import os\n\n\nclass Shared:\n    def method(self) -> int:\n        return len(os.sep)\n"
)

_PY_STAR = (
    "from os.path import *\n"
    "\n"
    "\n"
    "class Shared:\n"
    "    def method(self) -> int:\n"
    "        return len(sep)\n"
)

_PY_OTHER = (
    "import sys\n"
    "\n"
    "\n"
    "class Shared:\n"
    "    def method(self) -> int:\n"
    "        return len(sys.path)\n"
    "\n"
    "\n"
    "def helper() -> int:\n"
    "    return Shared().method()\n"
)

# Every document carries a class of the same known name, because class_model is
# asked for one by qualified name and the row cannot drive it otherwise. The
# comment-removed shape is the reverse direction of the two comment-added ones:
# the sequence is a walk over this pool, so every ordered pair of members is a
# reachable edit.
_PYTHON_DOCUMENTS = (
    _PY_BASE,
    _PY_BASE + "# trailing comment\n",
    "# leading note\n" + _PY_BASE,
    _PY_BASE + "\n\n",
    _PY_BASE.rstrip("\n"),
    _PY_BASE.rstrip("\n") + "   ",
    _PY_STAR,
    _PY_OTHER,
    _PY_OTHER + "# note\n",
)

_PYTHON_CLASS = "Shared"

# A single position can sit in a region no edit in the pool moves, which makes
# that target vacuous, so sweep a few. In the base module these land in turn on
# the imported name, the class name, the method name, and the name the method
# body reads; the shifted revisions of it put other things under them.
_SYMBOL_POSITIONS = (
    SourcePosition(0, 7),
    SourcePosition(3, 7),
    SourcePosition(4, 9),
    SourcePosition(5, 19),
)


def _notebook_document(cells: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "cells": cells,
            "metadata": {"kernelspec": {"name": "python3", "language": "python"}},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    )


_NB_CELL: dict[str, Any] = {
    "cell_type": "code",
    "source": "import os\nx = 1\n",
    "outputs": [],
    "execution_count": None,
}

_NB_CELL_RUN: dict[str, Any] = {
    "cell_type": "code",
    "source": "import os\nx = 1\n",
    "outputs": [{"output_type": "stream", "name": "stdout", "text": "noise\n"}],
    "execution_count": 7,
}

_NB_CELL_OTHER: dict[str, Any] = {
    "cell_type": "code",
    "source": "import sys\n\n\ndef helper() -> int:\n    return 2\n",
    "outputs": [],
    "execution_count": None,
}

_NB_MARKDOWN: dict[str, Any] = {"cell_type": "markdown", "source": "# Title\n"}

# The last four are written raw rather than through the envelope above. The
# first pair are a two-cell and a one-cell notebook that a flat projection of
# the text cannot tell apart; the second pair differ only in whether `cells` is
# absent or empty, which a projection that reads it with a default also cannot.
_NOTEBOOK_DOCUMENTS = (
    _notebook_document([_NB_CELL]),
    _notebook_document([_NB_CELL_RUN]),
    _notebook_document([_NB_MARKDOWN, _NB_CELL]),
    _notebook_document([_NB_CELL_OTHER]),
    '{"cells": [1, 2]}',
    '{"cells": [{"cell_type": "invalid-cell", "source": "invalid-cell"}]}',
    '{"metadata": {}, "nbformat": 4}',
    '{"cells": [], "metadata": {}, "nbformat": 4}',
)


def python_source_documents() -> st.SearchStrategy[list[str]]:
    # The sequence length is what dominates this row's cost -- one fresh
    # Database per step, and each one pays to fingerprint the whole closure
    # before it answers anything. min_size=2 so every example writes the file
    # at least twice.
    return st.lists(st.sampled_from(_PYTHON_DOCUMENTS), min_size=2, max_size=4)


def notebook_documents() -> st.SearchStrategy[list[str]]:
    return st.lists(st.sampled_from(_NOTEBOOK_DOCUMENTS), min_size=2, max_size=4)


def _python_entrypoint_values(db: Database, root: str, path: str) -> dict[str, object]:
    # The reverse call graph of the raw-text read, taken tree-wide and with the
    # read itself left out: including it would degenerate the property into
    # "the file changed", since raw text always differs when the bytes do. The
    # five entrypoints from the other two modules are not optional -- measured,
    # python_source's own dependents discriminate on nothing in this pool at
    # all, and scope_tree's geometry is where a read answered from a coarser
    # comparison shows. Two further entrypoints in that graph are left out on
    # purpose: workspace_analysis has its own row in this file above, and
    # workspace_symbol_index drives the same workspace walk at a cost this
    # row's budget cannot absorb.
    values: dict[str, object] = {
        "file_analysis": file_analysis(db, path),
        "directory_analysis": directory_analysis(db, root),
        "module_analysis": module_analysis(db, root, path),
        "scope_tree": scope_tree(db, path),
        "module_symbol_table": module_symbol_table(db, root, path),
        "class_model": class_model(db, root, path, _PYTHON_CLASS),
    }
    for index, position in enumerate(_SYMBOL_POSITIONS):
        # find_references takes a resolved SymbolId rather than a path, so the
        # id has to be resolved separately in each database -- which means the
        # id itself is compared too. Resolving in each and comparing only the
        # references would compare two different symbols and agree while both
        # halves were wrong.
        symbol_id = symbol_at(db, root, path, position)
        values[f"symbol_at[{index}]"] = symbol_id
        values[f"find_references[{index}]"] = (
            find_references(db, root, symbol_id) if symbol_id is not None else None
        )
    return values


# mode is a pytest parametrize; max_query_nodes deliberately is not. The
# [None, 2] stack the rows above use exists for kernel-level toy graphs, and a
# two-node cap over this closure evicts essentially everything -- which turns
# the warm database into a cold one and makes warm == fresh true for the wrong
# reason.
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=10, deadline=None)
@given(documents=python_source_documents())
def test_python_source_entrypoints_match_fresh_recomputation(
    mode: str, documents: list[str]
) -> None:
    # Installed-distribution discovery has its own integration tests, and
    # several of the entrypoints driven below reach it; without this patch every
    # fresh Database re-reads every METADATA file in the development
    # environment.
    with (
        patch.object(site, "getsitepackages", return_value=[]),
        patch.object(site, "getusersitepackages", return_value=""),
        tempfile.TemporaryDirectory() as tmpdir,
    ):
        root = Path(tmpdir) / "workspace"
        root.mkdir()
        path = root / "sample.py"
        incremental = Database(mode=mode)

        for step, content in enumerate(documents):
            previous = documents[step - 1] if step else None
            path.write_text(content, encoding="utf-8")
            warm = _python_entrypoint_values(incremental, str(root), str(path))
            fresh = _python_entrypoint_values(Database(mode=mode), str(root), str(path))
            for name in warm:
                # One line, discriminator first: a parametrized node id this
                # long fills the summary line on its own, so under the repo's
                # own `--tb=no` no part of the message survives. Read a failure
                # here with `-o addopts="" --tb=long`.
                assert warm[name] == fresh[name], (
                    f"{name} warm!=fresh | mode={mode} | step={step} | {previous!r} -> {content!r}"
                )


# max_examples is above the floor the python_source rows hold to, on measured
# grounds: of the three pairs in this pool that project to the same flat tuple
# of strings, two analyse differently -- the third is the output-only shape,
# whose equal analysis is what the row below pins. So it is the number of
# examples rather than the length of any one sequence that decides how often a
# walk over the pool steps across a pair that can disagree. This row is cheap
# enough to afford the higher count; the python_source row, at ten times the
# cost per step, is not.
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=30, deadline=None)
@given(documents=notebook_documents())
def test_notebook_entrypoints_match_fresh_recomputation(mode: str, documents: list[str]) -> None:
    # notebook_analysis reads the raw text directly, to place its diagnostic
    # ranges, and it is compared here for exactly that reason: its dependence
    # on the bytes is the thing a coarser comparison gets wrong, not an
    # artefact of the measurement. The raw read itself stays out of the
    # comparison -- it always differs when the bytes do.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "workspace"
        root.mkdir()
        path = root / "sample.ipynb"
        incremental = Database(mode=mode)

        for step, content in enumerate(documents):
            previous = documents[step - 1] if step else None
            path.write_text(content, encoding="utf-8")
            fresh = Database(mode=mode)

            warm_analysis = notebook_analysis(incremental, str(path))
            fresh_analysis = notebook_analysis(fresh, str(path))
            assert warm_analysis == fresh_analysis, (
                f"notebook_analysis warm!=fresh | "
                f"cells {len(warm_analysis.cells)}!={len(fresh_analysis.cells)} | "
                f"diags {len(warm_analysis.diagnostics)}!={len(fresh_analysis.diagnostics)} | "
                f"mode={mode} | step={step} | {previous!r} -> {content!r}"
            )

            warm_workspace = workspace_notebook_analysis(incremental, str(root))
            fresh_workspace = workspace_notebook_analysis(fresh, str(root))
            assert warm_workspace == fresh_workspace, (
                f"workspace_notebook_analysis warm!=fresh | mode={mode} | step={step} | "
                f"{previous!r} -> {content!r}"
            )


# Output-only revisions of one code cell: the outputs and the execution count
# move, the cell source never does.
_OUTPUT_ONLY_REVISIONS: tuple[tuple[tuple[dict[str, Any], ...], int | None], ...] = (
    ((), None),
    (({"output_type": "stream", "name": "stdout", "text": "noise\n"},), 7),
    (
        (
            {
                "output_type": "execute_result",
                "data": {"text/plain": "1"},
                "metadata": {},
                "execution_count": 2,
            },
        ),
        2,
    ),
    (
        (
            {"output_type": "stream", "name": "stderr", "text": "warn\n"},
            {"output_type": "stream", "name": "stdout", "text": "more\n"},
        ),
        11,
    ),
)

_NOTEBOOK_CELL_SOURCES = (
    "x = 1\n",
    "import os\nx = 1\n",
    "def f() -> int:\n    return 1\n",
    "",
)


def _notebook_run_document(
    source: str, outputs: tuple[dict[str, Any], ...], execution_count: int | None
) -> str:
    return _notebook_document(
        [
            {
                "cell_type": "code",
                "source": source,
                "outputs": list(outputs),
                "execution_count": execution_count,
            }
        ]
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=15, deadline=None)
@given(
    source=st.sampled_from(_NOTEBOOK_CELL_SOURCES),
    revisions=st.lists(
        st.integers(min_value=0, max_value=len(_OUTPUT_ONLY_REVISIONS) - 1),
        min_size=2,
        max_size=4,
        unique=True,
    ),
)
def test_a_notebook_output_edit_leaves_the_analysis_where_it_was(
    mode: str, source: str, revisions: list[int]
) -> None:
    # tests/test_notebook.py pins this at a single document. What this adds is
    # that it holds over the whole pool, so a later regression that spares one
    # document shape is still visible from here.
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "sample.ipynb"
        db = Database(mode=mode)
        first: NotebookAnalysis | None = None
        first_changed = 0

        for index in revisions:
            outputs, execution_count = _OUTPUT_ONLY_REVISIONS[index]
            path.write_text(
                _notebook_run_document(source, outputs, execution_count), encoding="utf-8"
            )
            analysis = notebook_analysis(db, str(path))
            # Inspected immediately after a single entrypoint call, which is
            # the one shape where a record's own stamps can be read at face
            # value: any node touched again inside the same request is
            # restamped "reused", so a cell that drives several entrypoints
            # before inspecting has to count executions from the query profile
            # instead. changed_at is the instrument here in any case -- the
            # payload reports "executed" on this edit while leaving changed_at
            # exactly where it was, which is what "the dependents stayed valid"
            # means.
            changed_at = db.inspect(notebook_analysis_payload, str(path)).changed_at
            if first is None:
                first, first_changed = analysis, changed_at
                continue

            assert analysis == first, (
                f"analysis moved | cells {len(analysis.cells)}!={len(first.cells)} | "
                f"mode={mode} | source={source!r} | revision={index}"
            )
            assert changed_at == first_changed, (
                f"changed_at {changed_at}!={first_changed} | mode={mode} | "
                f"source={source!r} | revision={index}"
            )


_PYTHON_COMMENT_BASES = (_PY_BASE, _PY_STAR, _PY_OTHER)

_PYTHON_COMMENTS = (
    "# one\n",
    "# two\n",
    "#\n",
    "# a longer trailing note\n",
    "# x = 1\n",
)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=15, deadline=None)
@given(
    base=st.sampled_from(_PYTHON_COMMENT_BASES),
    comments=st.lists(st.sampled_from(_PYTHON_COMMENTS), min_size=2, max_size=4, unique=True),
)
def test_a_python_comment_edit_leaves_the_import_analysis_reused(
    mode: str, base: str, comments: list[str]
) -> None:
    # tests/test_python_source.py pins this at a single document too, and the
    # same reason applies: over the pool a later regression that only affects
    # one module shape still shows up here.
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "sample.py"
        db = Database(mode=mode)
        path.write_text(base, encoding="utf-8")
        first = file_analysis(db, str(path))

        for comment in comments:
            path.write_text(base + comment, encoding="utf-8")
            analysis = file_analysis(db, str(path))
            # One entrypoint call, then the inspections -- see the note on the
            # row above for why anything that drives more than one has to read
            # the query profile instead.
            imports_decision = db.inspect(imports_for_file, str(path)).last_decision
            payload_decision = db.inspect(file_analysis_payload, str(path)).last_decision

            assert analysis == first, (
                f"analysis moved | imports {len(analysis.imports)}!={len(first.imports)} | "
                f"mode={mode} | comment={comment!r}"
            )
            assert imports_decision == "reused", (
                f"imports_for_file {imports_decision}!=reused | mode={mode} | comment={comment!r}"
            )
            assert payload_decision == "reused", (
                f"file_analysis_payload {payload_decision}!=reused | mode={mode} | "
                f"comment={comment!r}"
            )


def _python_checkpoint_values(db: Database, path: str) -> dict[str, object]:
    # Two entrypoints rather than the live row's eight, because the round trip
    # costs three databases per pair: scope_tree, which is the one that moves
    # when a read is answered from a coarser comparison, and file_analysis,
    # python_source's own, as the control beside it. The reduction is measured
    # rather than assumed -- across every ordered pair of the pool that can
    # disagree at all, the entrypoint that disagrees is scope_tree and no other,
    # so this pair separates exactly what the eight do. Entrypoints, never
    # payload leaves -- checkpoint warming is parent-driven, and a leaf asked on
    # its own cold-executes even when its record is in the manifest.
    return {"file_analysis": file_analysis(db, path), "scope_tree": scope_tree(db, path)}


# The edit happens BEFORE the save, and the ordering is the whole test. Saving
# first and editing after reproduces nothing: on reload the resource probe
# mismatches, the read executes on the new bytes, there is no earlier answer
# left to serve from, and the row is green whether or not a stale read is
# possible -- a regression that can never fail. Values are compared and never a
# recompute marker, because a reloaded record reports "reused" or "executed"
# either way.
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=10, deadline=None)
@given(documents=python_source_documents())
def test_python_source_checkpoint_reload_matches_fresh(mode: str, documents: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "sample.py"

        for before, after in zip(documents, documents[1:], strict=False):
            path.write_text(before, encoding="utf-8")
            store = InMemoryArtifactStore()
            saver = Database(mode=mode, store=store)
            _python_checkpoint_values(saver, str(path))

            path.write_text(after, encoding="utf-8")
            _python_checkpoint_values(saver, str(path))
            key = saver.save_checkpoint()

            reloaded = Database(mode=mode, store=store)
            reloaded.load_checkpoint(key)
            warm = _python_checkpoint_values(reloaded, str(path))
            fresh = _python_checkpoint_values(Database(mode=mode), str(path))
            for name in warm:
                assert warm[name] == fresh[name], (
                    f"{name} reloaded!=fresh | mode={mode} | {before!r} -> {after!r}"
                )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=30, deadline=None)
@given(documents=notebook_documents())
def test_notebook_checkpoint_reload_matches_fresh(mode: str, documents: list[str]) -> None:
    # Same ordering, same reasons, same two rules: edit before save, compare
    # values, drive the entrypoints. max_examples is raised for the same reason
    # as on the notebook row above -- the pool's aliasing pairs are sparse.
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "workspace"
        root.mkdir()
        path = root / "sample.ipynb"

        for before, after in zip(documents, documents[1:], strict=False):
            path.write_text(before, encoding="utf-8")
            store = InMemoryArtifactStore()
            saver = Database(mode=mode, store=store)
            notebook_analysis(saver, str(path))
            workspace_notebook_analysis(saver, str(root))

            path.write_text(after, encoding="utf-8")
            notebook_analysis(saver, str(path))
            workspace_notebook_analysis(saver, str(root))
            key = saver.save_checkpoint()

            reloaded = Database(mode=mode, store=store)
            reloaded.load_checkpoint(key)
            scratch = Database(mode=mode)

            warm_analysis = notebook_analysis(reloaded, str(path))
            fresh_analysis = notebook_analysis(scratch, str(path))
            assert warm_analysis == fresh_analysis, (
                f"notebook_analysis reloaded!=fresh | "
                f"cells {len(warm_analysis.cells)}!={len(fresh_analysis.cells)} | "
                f"diags {len(warm_analysis.diagnostics)}!={len(fresh_analysis.diagnostics)} | "
                f"mode={mode} | {before!r} -> {after!r}"
            )

            warm_workspace = workspace_notebook_analysis(reloaded, str(root))
            fresh_workspace = workspace_notebook_analysis(scratch, str(root))
            assert warm_workspace == fresh_workspace, (
                f"workspace_notebook_analysis reloaded!=fresh | mode={mode} | "
                f"{before!r} -> {after!r}"
            )
