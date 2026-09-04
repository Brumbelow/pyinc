from __future__ import annotations

import json
import math
import os
import site
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import pyinc.integrations.deep_module_resolution as deep_module_resolution
import pyinc.integrations.installed_packages as installed_packages
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
from pyinc.integrations.csv_data import csv_analysis, workspace_csv_analysis
from pyinc.integrations.deep_module_resolution import (
    deep_module_resolution_analysis,
    resolve_module_path,
)
from pyinc.integrations.dependency_check import (
    dependency_check_analysis,
    workspace_dependency_check,
)
from pyinc.integrations.env_file import env_analysis, workspace_env_analysis
from pyinc.integrations.installed_packages import (
    installed_packages_analysis,
    resolve_import_name,
)
from pyinc.integrations.json_config import json_analysis, workspace_json_analysis
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
from pyinc.integrations.requirement_evaluation import (
    applicable_requirements,
    workspace_applicable_requirements,
)
from pyinc.integrations.requirements_txt import (
    deep_requirements_analysis,
    requirements_analysis,
    workspace_requirements_analysis,
)
from pyinc.integrations.scope_resolution import scope_tree, symbol_at
from pyinc.integrations.symbol_resolution import class_model, find_references, module_symbol_table
from pyinc.integrations.toml_config import config_analysis, workspace_config_analysis
from pyinc.integrations.xml_config import workspace_xml_analysis, xml_analysis
from pyinc_codegen import generate, schema_analysis

Operation = tuple[str, object]
WorkspaceState = tuple[str, str, bool]
CheckpointOp = tuple[str, object]


def _examples(default: int) -> int:
    # PYINC_PROPERTY_MAX_EXAMPLES caps every row's budget so one quick job can
    # run the file; unset, each row keeps the budget written beside it.
    cap = os.environ.get("PYINC_PROPERTY_MAX_EXAMPLES", "")
    return min(default, int(cap)) if cap else default


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
@settings(max_examples=_examples(50), deadline=None)
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
@settings(max_examples=_examples(30), deadline=None)
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
@settings(max_examples=_examples(20), deadline=None)
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
@settings(max_examples=_examples(10), deadline=None)
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
@settings(max_examples=_examples(40), deadline=None)
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
@settings(max_examples=_examples(20), deadline=None)
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
@settings(max_examples=_examples(40), deadline=None)
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
@settings(max_examples=_examples(30), deadline=None)
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
@settings(max_examples=_examples(40), deadline=None)
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
# database exactly as it answers a fresh one. The two source-file integrations
# come first; the section below this one carries the same pair of shapes for
# each of the remaining nine.
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
@settings(max_examples=_examples(10), deadline=None)
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
@settings(max_examples=_examples(30), deadline=None)
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
@settings(max_examples=_examples(15), deadline=None)
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
@settings(max_examples=_examples(15), deadline=None)
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
@settings(max_examples=_examples(10), deadline=None)
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
@settings(max_examples=_examples(30), deadline=None)
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


# ---------------------------------------------------------------------------
# Configuration, environment and packaging entrypoints against a fresh read
# ---------------------------------------------------------------------------
#
# The same property as the two source-file sections above, one live row and one
# checkpoint row per integration. Every row drives public entrypoints and never
# a payload leaf, and every row leaves the raw-text read out of the comparison:
# raw text always differs when the bytes do, so including it would degenerate
# the property into "the file changed".
#
# mode is a pytest parametrize on every row; max_query_nodes deliberately is
# not, for the reason recorded above the python_source row -- a two-node cap
# over a real integration closure evicts the graph and makes warm == fresh true
# for the wrong reason.
#
# The document pools are hand-written corpora sampled from, never generated
# text, and each pool carries the shapes a projection of the file could collapse:
# reordered inline tables and objects for TOML and JSON, the
# continuation-backslash pair and an editable install with an inline comment for
# requirements, an absent header against an empty one for distribution metadata,
# and the quoting, comment and whitespace classes for the rest. A pool with no
# such shape in it makes its row vacuous.

_EntrypointGroup = Callable[[Database, str, str], dict[str, object]]


def _sampled_documents(documents: tuple[str, ...]) -> st.SearchStrategy[list[str]]:
    # min_size=2 so every example writes the file at least twice; max_size=4
    # because the sequence length, not the pool size, is what each row's cost
    # scales with -- one extra fresh Database per step.
    return st.lists(st.sampled_from(documents), min_size=2, max_size=4)


def _assert_entrypoints_match_fresh(
    *,
    filename: str,
    entrypoints: _EntrypointGroup,
    mode: str,
    documents: list[str],
) -> None:
    """Walk the pool, comparing every entrypoint against a database with no history.

    One file at a workspace root, under the name workspace discovery looks for,
    so the path-taking and the root-taking entrypoints both have something to
    answer about.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "workspace"
        root.mkdir()
        path = root / filename
        incremental = Database(mode=mode)

        for step, content in enumerate(documents):
            previous = documents[step - 1] if step else None
            path.write_text(content, encoding="utf-8")
            warm = entrypoints(incremental, str(root), str(path))
            fresh = entrypoints(Database(mode=mode), str(root), str(path))
            for name in warm:
                # One line, discriminator first: these node ids are long enough
                # to fill the summary line on their own under the repo's own
                # `--tb=no`. Read a failure with `-o addopts="" --tb=long`.
                assert warm[name] == fresh[name], (
                    f"{name} warm!=fresh | mode={mode} | step={step} | "
                    f"{previous!r} -> {content!r}"
                )


# The checkpoint rows come in two orderings and the difference is the whole
# point of each. Both compare a reloaded database against a fresh one and assert
# nothing else.
#
# The `!= warm` arm that would show the comparison is not vacuous is left out of
# every checkpoint row in this section on purpose: each step's before/after pair
# is two separate draws from the pool, so a sequence can repeat one document
# or pair two members that analyse equal, and then the answer before the edit
# equals the answer after it -- a `!=` arm goes red on a correct tree. That arm
# lives in the hand-written rows that choose their own semantic edit --
# test_a_checkpoint_reload_answers_an_edit_made_after_the_save in
# tests/test_csv_data.py, tests/test_env_file.py, tests/test_xml_config.py,
# tests/test_deep_module_resolution.py and tests/test_codegen.py.


def _assert_reload_after_an_edit_before_the_save_matches_fresh(
    *,
    filename: str,
    entrypoints: _EntrypointGroup,
    mode: str,
    documents: list[str],
) -> None:
    """Save a database that has already answered across the edit, then reload it.

    The edit lands BEFORE the save and the entrypoints are re-driven after it,
    so what gets written is the state the database reached by answering on the
    new bytes. Saving first and editing afterwards reproduces nothing: on reload
    the resource probe mismatches, the read executes on the new bytes, and there
    is no earlier answer left to serve from -- a row that cannot fail.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "workspace"
        root.mkdir()
        path = root / filename

        for before, after in zip(documents, documents[1:], strict=False):
            path.write_text(before, encoding="utf-8")
            store = InMemoryArtifactStore()
            saver = Database(mode=mode, store=store)
            entrypoints(saver, str(root), str(path))

            path.write_text(after, encoding="utf-8")
            entrypoints(saver, str(root), str(path))
            key = saver.save_checkpoint()

            reloaded = Database(mode=mode, store=store)
            reloaded.load_checkpoint(key)
            warm = entrypoints(reloaded, str(root), str(path))
            fresh = entrypoints(Database(mode=mode), str(root), str(path))
            for name in warm:
                assert warm[name] == fresh[name], (
                    f"{name} reloaded!=fresh | mode={mode} | {before!r} -> {after!r}"
                )


def _assert_reload_after_an_edit_after_the_save_matches_fresh(
    *,
    filename: str,
    entrypoints: _EntrypointGroup,
    mode: str,
    documents: list[str],
) -> None:
    """Edit after the save, and the reload has to notice.

    This is the substitute ordering for the reads that hand back the text they
    compared: no answer they hold can disagree with the file, so a database
    saved mid-edit carries nothing stale and the ordering above cannot be built
    at all. It is the standard for those sites, recorded here so a later reader
    does not "fix" one of these rows into an ordering that measures nothing.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "workspace"
        root.mkdir()
        path = root / filename

        for before, after in zip(documents, documents[1:], strict=False):
            path.write_text(before, encoding="utf-8")
            store = InMemoryArtifactStore()
            saver = Database(mode=mode, store=store)
            entrypoints(saver, str(root), str(path))
            key = saver.save_checkpoint()

            path.write_text(after, encoding="utf-8")

            reloaded = Database(mode=mode, store=store)
            reloaded.load_checkpoint(key)
            warm = entrypoints(reloaded, str(root), str(path))
            fresh = entrypoints(Database(mode=mode), str(root), str(path))
            for name in warm:
                assert warm[name] == fresh[name], (
                    f"{name} reloaded!=fresh | mode={mode} | {before!r} -> {after!r}"
                )


# ---------------------------------------------------------------------------
# CSV

# Quoting classes carrying the same table, a separator and a quote character
# living inside a quoted field, a ragged row that produces a diagnostic, and a
# file whose delimiter is not a comma at all.
_CSV_DOCUMENTS = (
    "name,age\nAlice,30\nBob,25\n",
    '"name","age"\n"Alice","30"\n"Bob","25"\n',
    '"name","age"\n"Alice","30"\n"Bob","25"\n\n',
    'name,"age"\n"Alice",30\nBob,"25"\n',
    'name,age\n"Doe, Alice",30\nBob,25\n',
    'name,age\n"Alice ""A""",30\nBob,25\n',
    "name,age\nAlice,30\nBob\n",
    "name;age\nAlice;30\nBob;25\n",
)


def _csv_entrypoints(db: Database, root: str, path: str) -> dict[str, object]:
    return {
        "csv_analysis": csv_analysis(db, path),
        "workspace_csv_analysis": workspace_csv_analysis(db, root),
    }


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(30), deadline=None)
@given(documents=_sampled_documents(_CSV_DOCUMENTS))
def test_csv_entrypoints_match_fresh_recomputation(mode: str, documents: list[str]) -> None:
    _assert_entrypoints_match_fresh(
        filename="data.csv", entrypoints=_csv_entrypoints, mode=mode, documents=documents
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(30), deadline=None)
@given(documents=_sampled_documents(_CSV_DOCUMENTS))
def test_csv_checkpoint_reload_matches_fresh(mode: str, documents: list[str]) -> None:
    _assert_reload_after_an_edit_after_the_save_matches_fresh(
        filename="data.csv", entrypoints=_csv_entrypoints, mode=mode, documents=documents
    )


# ---------------------------------------------------------------------------
# Environment files

# Whole-line comments of two different lengths, both quote styles and none, an
# export prefix, an inline comment, blank lines that move every range below
# them, and a line the parser rejects.
_ENV_DOCUMENTS = (
    "# aaa\nKEY=value\nOTHER='x'\n",
    "# a much longer comment body\nKEY=value\nOTHER='x'\n",
    '# aaa\nKEY=value\nOTHER="x"\n',
    "# aaa\nKEY=value\nOTHER=x\n",
    "# aaa\nexport KEY=value\nOTHER='x'\n",
    "# aaa\nKEY=value  # the key\nOTHER='x'\n",
    "# aaa\n\nKEY=value\n\nOTHER='x'\n",
    "# aaa\nKEY=value\nnot an assignment\nOTHER='x'\n",
)


def _env_entrypoints(db: Database, root: str, path: str) -> dict[str, object]:
    return {
        "env_analysis": env_analysis(db, path),
        "workspace_env_analysis": workspace_env_analysis(db, root),
    }


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(30), deadline=None)
@given(documents=_sampled_documents(_ENV_DOCUMENTS))
def test_env_file_entrypoints_match_fresh_recomputation(mode: str, documents: list[str]) -> None:
    _assert_entrypoints_match_fresh(
        filename=".env", entrypoints=_env_entrypoints, mode=mode, documents=documents
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(30), deadline=None)
@given(documents=_sampled_documents(_ENV_DOCUMENTS))
def test_env_file_checkpoint_reload_matches_fresh(mode: str, documents: list[str]) -> None:
    _assert_reload_after_an_edit_after_the_save_matches_fresh(
        filename=".env", entrypoints=_env_entrypoints, mode=mode, documents=documents
    )


# ---------------------------------------------------------------------------
# JSON

# The first two are the discriminating pair: the same object with its keys in a
# different order, which a canonicalizing projection of the document maps onto
# one value while the reported section strings differ. The rest are that shape
# reformatted, the pair repeated one and two containers down, a scalar spread,
# and a document the parser rejects.
_JSON_DOCUMENTS = (
    '{"deps": [{"name": "a", "version": "1"}]}',
    '{"deps": [{"version": "1", "name": "a"}]}',
    '{\n  "deps": [\n    { "name": "a", "version": "1" }\n  ]\n}\n',
    '{"a": [{"x": 1, "y": 2}]}',
    '{"a": [{"y": 2, "x": 1}]}',
    '{"a": [[{"x": 1, "y": 2}]]}',
    '{"n": 1, "s": "t", "b": true, "z": null}',
    '{"a": [1, 2',
)


def _json_entrypoints(db: Database, root: str, path: str) -> dict[str, object]:
    return {
        "json_analysis": json_analysis(db, path),
        "workspace_json_analysis": workspace_json_analysis(db, root),
    }


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(30), deadline=None)
@given(documents=_sampled_documents(_JSON_DOCUMENTS))
def test_json_config_entrypoints_match_fresh_recomputation(mode: str, documents: list[str]) -> None:
    _assert_entrypoints_match_fresh(
        filename="package.json", entrypoints=_json_entrypoints, mode=mode, documents=documents
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(30), deadline=None)
@given(documents=_sampled_documents(_JSON_DOCUMENTS))
def test_json_config_checkpoint_reload_matches_fresh(mode: str, documents: list[str]) -> None:
    _assert_reload_after_an_edit_before_the_save_matches_fresh(
        filename="package.json", entrypoints=_json_entrypoints, mode=mode, documents=documents
    )


# ---------------------------------------------------------------------------
# TOML

# The first two are the inline-table reorder pair, the third and fourth the same
# pair written as an array of tables. Then a table against the array carrying
# the same names, one of the date-like scalars the public value type folds
# together against an array that spells it out, a project table with
# dependencies, and a document the parser rejects.
_TOML_DOCUMENTS = (
    "x = [{b = 1, a = 2}]\n",
    "x = [{a = 2, b = 1}]\n",
    "[[array.of.tables]]\nb = 1\na = 2\n",
    "[[array.of.tables]]\na = 2\nb = 1\n",
    '[x]\na = "b"\n',
    'x = [["a", "b"]]\n',
    "x = 1979-05-27T07:32:00\n",
    'x = ["datetime", "1979-05-27T07:32:00"]\n',
    '[project]\nname = "demo"\ndependencies = ["requests>=2.0"]\n',
    "[x\n",
)


def _toml_entrypoints(db: Database, root: str, path: str) -> dict[str, object]:
    return {
        "config_analysis": config_analysis(db, path),
        "workspace_config_analysis": workspace_config_analysis(db, root),
    }


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(30), deadline=None)
@given(documents=_sampled_documents(_TOML_DOCUMENTS))
def test_toml_config_entrypoints_match_fresh_recomputation(mode: str, documents: list[str]) -> None:
    _assert_entrypoints_match_fresh(
        filename="pyproject.toml", entrypoints=_toml_entrypoints, mode=mode, documents=documents
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(30), deadline=None)
@given(documents=_sampled_documents(_TOML_DOCUMENTS))
def test_toml_config_checkpoint_reload_matches_fresh(mode: str, documents: list[str]) -> None:
    _assert_reload_after_an_edit_before_the_save_matches_fresh(
        filename="pyproject.toml", entrypoints=_toml_entrypoints, mode=mode, documents=documents
    )


# ---------------------------------------------------------------------------
# XML

# The same element tree flat and indented, its attributes in both orders, a
# comment inserted between siblings, a declaration prologue, one more level of
# nesting, and a document that never closes its element.
_XML_DOCUMENTS = (
    '<root><child a="1">t</child></root>',
    '<root>\n  <child a="1">t</child>\n</root>\n',
    '<root><child a="1" b="2">t</child></root>',
    '<root><child b="2" a="1">t</child></root>',
    '<root><!-- note --><child a="1">t</child></root>',
    '<?xml version="1.0" encoding="UTF-8"?>\n<root><child a="1">t</child></root>',
    '<root><child a="1"><grand>t</grand></child></root>',
    "<root><unclosed>",
)


def _xml_entrypoints(db: Database, root: str, path: str) -> dict[str, object]:
    return {
        "xml_analysis": xml_analysis(db, path),
        "workspace_xml_analysis": workspace_xml_analysis(db, root),
    }


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(30), deadline=None)
@given(documents=_sampled_documents(_XML_DOCUMENTS))
def test_xml_config_entrypoints_match_fresh_recomputation(mode: str, documents: list[str]) -> None:
    _assert_entrypoints_match_fresh(
        filename="pom.xml", entrypoints=_xml_entrypoints, mode=mode, documents=documents
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(30), deadline=None)
@given(documents=_sampled_documents(_XML_DOCUMENTS))
def test_xml_config_checkpoint_reload_matches_fresh(mode: str, documents: list[str]) -> None:
    _assert_reload_after_an_edit_after_the_save_matches_fresh(
        filename="pom.xml", entrypoints=_xml_entrypoints, mode=mode, documents=documents
    )


# ---------------------------------------------------------------------------
# Requirements

# Every line kind whose public output is built from text a line-normalizing
# projection throws away, varied one at a time: inline comments on a
# requirement, on an index directive, on an editable install and on a line the
# parser rejects; an indent; trailing whitespace; and the pair that matters
# most -- a continuation backslash at the end of a line against the same
# backslash with one space after it, which stops it continuing the line at all.
_REQUIREMENTS_DOCUMENTS = (
    "--index-url https://example.com/simple  # primary\n"
    "requests>=2.0  # http client\n"
    "-e ./pkg  # local\n"
    "this is not a requirement  # junk\n"
    "flask \\\n==2.0\n",
    "--index-url https://example.com/simple  # primary\n"
    "requests>=2.0  # the http client we use\n"
    "-e ./pkg  # local\n"
    "this is not a requirement  # junk\n"
    "flask \\\n==2.0\n",
    "--index-url https://example.com/simple  # the primary index\n"
    "requests>=2.0  # http client\n"
    "-e ./pkg  # local\n"
    "this is not a requirement  # junk\n"
    "flask \\\n==2.0\n",
    "--index-url https://example.com/simple  # primary\n"
    "requests>=2.0  # http client\n"
    "-e ./pkg  # the local package\n"
    "this is not a requirement  # junk\n"
    "flask \\\n==2.0\n",
    "--index-url https://example.com/simple  # primary\n"
    "requests>=2.0  # http client\n"
    "-e ./pkg  # local\n"
    "this is not a requirement  # not a requirement at all\n"
    "flask \\\n==2.0\n",
    "--index-url https://example.com/simple  # primary\n"
    "    requests>=2.0  # http client\n"
    "-e ./pkg  # local\n"
    "this is not a requirement  # junk\n"
    "flask \\\n==2.0\n",
    "--index-url https://example.com/simple  # primary\n"
    "requests>=2.0  # http client\n"
    "-e ./pkg  # local   \n"
    "this is not a requirement  # junk\n"
    "flask \\\n==2.0\n",
    "--index-url https://example.com/simple  # primary\n"
    "requests>=2.0  # http client\n"
    "-e ./pkg  # local\n"
    "this is not a requirement  # junk\n"
    "flask \\ \n==2.0\n",
)


# All five documented entrypoints are driven, in two rows rather than one. One
# row over all five measured past this file's per-cell budget, and the budget is
# what the CI job that runs this file alone exists to hold; splitting by
# entrypoint group is the only reduction taken, and the pool, the mode
# parametrize and the example count are untouched by it.
#
# The boundary is the one the two groups already draw, not an arbitrary cut:
# only the three reporting surfaces move on the comment, index-directive and
# diagnostic classes at all. The two evaluation surfaces move on the
# editable-install name and on the continuation-backslash pair and nowhere else,
# so several of their cells agree for reasons that have nothing to do with the
# property -- uninformative by construction rather than by accident.


def _requirements_entrypoints(db: Database, root: str, path: str) -> dict[str, object]:
    return {
        "requirements_analysis": requirements_analysis(db, path),
        "deep_requirements_analysis": deep_requirements_analysis(db, path),
        "workspace_requirements_analysis": workspace_requirements_analysis(db, root),
    }


def _requirements_evaluation_entrypoints(
    db: Database, root: str, path: str
) -> dict[str, object]:
    return {
        "applicable_requirements": applicable_requirements(db, path),
        "workspace_applicable_requirements": workspace_applicable_requirements(db, root),
    }


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(10), deadline=None)
@given(documents=_sampled_documents(_REQUIREMENTS_DOCUMENTS))
def test_requirements_entrypoints_match_fresh_recomputation(
    mode: str, documents: list[str]
) -> None:
    _assert_entrypoints_match_fresh(
        filename="requirements.txt",
        entrypoints=_requirements_entrypoints,
        mode=mode,
        documents=documents,
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(10), deadline=None)
@given(documents=_sampled_documents(_REQUIREMENTS_DOCUMENTS))
def test_requirements_evaluation_entrypoints_match_fresh_recomputation(
    mode: str, documents: list[str]
) -> None:
    _assert_entrypoints_match_fresh(
        filename="requirements.txt",
        entrypoints=_requirements_evaluation_entrypoints,
        mode=mode,
        documents=documents,
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(10), deadline=None)
@given(documents=_sampled_documents(_REQUIREMENTS_DOCUMENTS))
def test_requirements_checkpoint_reload_matches_fresh(mode: str, documents: list[str]) -> None:
    _assert_reload_after_an_edit_before_the_save_matches_fresh(
        filename="requirements.txt",
        entrypoints=_requirements_entrypoints,
        mode=mode,
        documents=documents,
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(10), deadline=None)
@given(documents=_sampled_documents(_REQUIREMENTS_DOCUMENTS))
def test_requirements_evaluation_checkpoint_reload_matches_fresh(
    mode: str, documents: list[str]
) -> None:
    _assert_reload_after_an_edit_before_the_save_matches_fresh(
        filename="requirements.txt",
        entrypoints=_requirements_evaluation_entrypoints,
        mode=mode,
        documents=documents,
    )


# ---------------------------------------------------------------------------
# Path-configuration files

# A directory named, the same line commented out, the name with a comment beside
# it, the name surrounded by blank lines and by spaces, a directory that does
# not exist, both together, and an executable line.
_PTH_DOCUMENTS = (
    "extra\n",
    "# extra\n",
    "extra\n# a note\n",
    "\nextra\n\n",
    "   extra   \n",
    "missing\n",
    "extra\nmissing\n",
    "import os\nextra\n",
)

_PTH_MODULE = "import extra_pkg\n\n\ndef helper() -> int:\n    return 1\n"


def _build_pth_workspace(tmpdir: str) -> tuple[Path, Path, Path, Path]:
    """A search-path root holding one .pth file, the directory it can add, and a module."""
    base = Path(tmpdir)
    search_root = base / "site"
    search_root.mkdir()
    package = search_root / "extra" / "extra_pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    workspace = base / "workspace"
    workspace.mkdir()
    module = workspace / "sample.py"
    module.write_text(_PTH_MODULE, encoding="utf-8")
    return search_root, search_root / "paths.pth", workspace, module


# The two entrypoints this integration publishes, and module_analysis as the one
# cross-integration consumer: the import resolution it reports is enriched from
# the effective search path, so a .pth read answered from a coarser comparison
# surfaces there and nowhere cheaper.
#
# They are driven in two rows rather than one for the reason recorded above the
# requirements rows: one row over all of them measured past this file's per-cell
# budget, and the group boundary is where the cost is -- module_analysis drags
# the whole python-source closure into every step, and the other two do not.
# Nothing else about the rows is reduced.
#
# Left out on purpose, and none of them for lack of a connection: file_analysis
# and directory_analysis never resolve an import against the search path at all,
# and workspace_analysis, module_symbol_table, class_model, find_references and
# symbol_at reach it only through the same resolution module_analysis already
# drives, at several times the cost per step -- the python_source row above pays
# that closure once.


def _pth_entrypoints(db: Database, root: str, path: str) -> dict[str, object]:
    del root, path
    return {
        "deep_module_resolution_analysis": deep_module_resolution_analysis(db),
        "resolve_module_path[extra_pkg]": resolve_module_path(db, "extra_pkg"),
        "resolve_module_path[missing_pkg]": resolve_module_path(db, "missing_pkg"),
    }


def _pth_consumer_entrypoints(db: Database, root: str, path: str) -> dict[str, object]:
    return {"module_analysis": module_analysis(db, root, path)}


def _walk_the_pth_pool_against_fresh(
    mode: str, documents: list[str], entrypoints: _EntrypointGroup
) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        search_root, pth_path, workspace, module = _build_pth_workspace(tmpdir)
        # Discovery is patched with a context manager rather than the
        # monkeypatch fixture, which Hypothesis refuses under @given, and the
        # replacement is written in this module: the kernel pins the source of
        # the query that reads it and will not accept a global it cannot place.
        # Installed-distribution discovery is emptied for the reason the
        # python_source row gives -- without it every fresh Database re-reads
        # every METADATA file in the development environment.
        with (
            patch.object(
                deep_module_resolution, "_get_sys_path_entries", lambda: (str(search_root),)
            ),
            patch.object(site, "getsitepackages", return_value=[]),
            patch.object(site, "getusersitepackages", return_value=""),
        ):
            incremental = Database(mode=mode)

            for step, content in enumerate(documents):
                previous = documents[step - 1] if step else None
                pth_path.write_text(content, encoding="utf-8")
                warm = entrypoints(incremental, str(workspace), str(module))
                fresh = entrypoints(Database(mode=mode), str(workspace), str(module))
                for name in warm:
                    assert warm[name] == fresh[name], (
                        f"{name} warm!=fresh | mode={mode} | step={step} | "
                        f"{previous!r} -> {content!r}"
                    )


def _reload_the_pth_pool_against_fresh(
    mode: str, documents: list[str], entrypoints: _EntrypointGroup
) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        search_root, pth_path, workspace, module = _build_pth_workspace(tmpdir)
        with (
            patch.object(
                deep_module_resolution, "_get_sys_path_entries", lambda: (str(search_root),)
            ),
            patch.object(site, "getsitepackages", return_value=[]),
            patch.object(site, "getusersitepackages", return_value=""),
        ):
            # Edited after the save, for the reason set out above the two
            # orderings: this read hands back the text it compared, so a
            # database saved in the middle of an edit holds nothing stale.
            for before, after in zip(documents, documents[1:], strict=False):
                pth_path.write_text(before, encoding="utf-8")
                store = InMemoryArtifactStore()
                saver = Database(mode=mode, store=store)
                entrypoints(saver, str(workspace), str(module))
                key = saver.save_checkpoint()

                pth_path.write_text(after, encoding="utf-8")

                reloaded = Database(mode=mode, store=store)
                reloaded.load_checkpoint(key)
                warm = entrypoints(reloaded, str(workspace), str(module))
                fresh = entrypoints(Database(mode=mode), str(workspace), str(module))
                for name in warm:
                    assert warm[name] == fresh[name], (
                        f"{name} reloaded!=fresh | mode={mode} | {before!r} -> {after!r}"
                    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(10), deadline=None)
@given(documents=_sampled_documents(_PTH_DOCUMENTS))
def test_pth_entrypoints_match_fresh_recomputation(mode: str, documents: list[str]) -> None:
    _walk_the_pth_pool_against_fresh(mode, documents, _pth_entrypoints)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(10), deadline=None)
@given(documents=_sampled_documents(_PTH_DOCUMENTS))
def test_pth_import_resolution_matches_fresh_recomputation(
    mode: str, documents: list[str]
) -> None:
    _walk_the_pth_pool_against_fresh(mode, documents, _pth_consumer_entrypoints)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(10), deadline=None)
@given(documents=_sampled_documents(_PTH_DOCUMENTS))
def test_pth_checkpoint_reload_matches_fresh(mode: str, documents: list[str]) -> None:
    _reload_the_pth_pool_against_fresh(mode, documents, _pth_entrypoints)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(10), deadline=None)
@given(documents=_sampled_documents(_PTH_DOCUMENTS))
def test_pth_import_resolution_checkpoint_reload_matches_fresh(
    mode: str, documents: list[str]
) -> None:
    _reload_the_pth_pool_against_fresh(mode, documents, _pth_consumer_entrypoints)


# ---------------------------------------------------------------------------
# Installed distribution metadata


def _metadata_document(name: str | None, version: str | None) -> str:
    """A METADATA document, omitting a header entirely when its value is None."""
    lines = ["Metadata-Version: 2.1"]
    for field, value in (("Name", name), ("Version", version)):
        if value is not None:
            # An empty value writes "Name:", not "Name: " -- a header left empty
            # carries nothing after the colon.
            lines.append(f"{field}: {value}".rstrip())
    lines.append("Summary: s")
    return "\n".join(lines) + "\n"


# A METADATA header has three states, not two: absent, present-and-empty, and
# present-with-a-value. The package layer branches on the difference between the
# first two -- an absent Name is a distribution it cannot describe, an empty one
# is a distribution whose name is the empty string -- so the absent/empty pair is
# exactly what a comparison that fills a missing field in with '' cannot see. It
# is in this pool for both headers, and for both together.
_METADATA_DOCUMENTS = (
    _metadata_document(None, "1.0"),
    _metadata_document("", "1.0"),
    _metadata_document("example", "1.0"),
    _metadata_document("example", None),
    _metadata_document("example", ""),
    _metadata_document("example", "2.0"),
    _metadata_document(None, None),
    _metadata_document("", ""),
)

_METADATA_MODULE = "import example\n\n\ndef helper() -> int:\n    return 1\n"

_DECLARED_DEPENDENCIES = ("example", "absent-package")


def _build_metadata_workspace(tmpdir: str) -> tuple[Path, Path, Path, Path]:
    """A site-packages holding one dist-info, and a workspace importing its name."""
    base = Path(tmpdir)
    site_dir = base / "site-packages"
    dist_info = site_dir / "example-1.0.dist-info"
    dist_info.mkdir(parents=True)
    # A top_level.txt keeps the import name fixed across the pool, so the
    # resolution surface moves only when the distribution itself moves.
    (dist_info / "top_level.txt").write_text("example\n", encoding="utf-8")
    workspace = base / "workspace"
    workspace.mkdir()
    module = workspace / "sample.py"
    module.write_text(_METADATA_MODULE, encoding="utf-8")
    return site_dir, dist_info / "METADATA", workspace, module


# Four of the twelve entrypoints this metadata read reaches. The other eight are
# left out with reasons rather than by omission: module_analysis,
# workspace_analysis, class_model, find_references and symbol_at reach it through
# python-source import enrichment, which the python_source row above already pays
# for; resolve_module_path is driven by the path-configuration rows above; and
# applicable_requirements and workspace_applicable_requirements are driven by the
# requirements rows.
#
# The two dependency-checking surfaces are not optional. They are how the
# installed environment reaches a workspace's declared dependencies, so a
# metadata read answered from a coarser comparison travels past this module
# through them. What they cannot see, measured rather than assumed, is this
# pool's absent-versus-empty transition: a distribution with no Name is not
# listed and a distribution whose Name is empty is listed under the empty
# string, and neither state answers to a declared dependency by name -- not even
# to the empty name. They stay in the set because they carry every other way
# this read can go wrong into a workspace, not because they discriminate here.
#
# All four are driven by both shapes. The live row drives them together, which
# measured inside this file's per-cell budget; the checkpoint shape, which pays
# for three databases per pair rather than two, did not, so it drives them in
# three rows -- this integration's own surfaces, then the declared-dependency
# check, then the workspace check that walks a tree on top of it. Splitting is
# the only reduction taken: the pool, the mode parametrize and the example count
# are the same in every one of them, and no entrypoint is dropped.


def _metadata_own_entrypoints(db: Database, root: str, path: str) -> dict[str, object]:
    # Neither the root nor the module path is needed for these two, and neither
    # is compared: each is the same string in both arms by construction, which
    # is exactly the kind of cell that passes while measuring nothing.
    del root, path
    analysis = installed_packages_analysis(db)
    return {
        "installed_packages_analysis packages": analysis.packages,
        # Split from the packages tuple so a failure prints the diagnostics
        # instead of truncating inside the package listing.
        "installed_packages_analysis diagnostics": analysis.diagnostics,
        "resolve_import_name": resolve_import_name(db, "example"),
    }


def _dependency_check_entrypoints(db: Database, root: str, path: str) -> dict[str, object]:
    del root, path
    return {"dependency_check_analysis": dependency_check_analysis(db, _DECLARED_DEPENDENCIES)}


def _workspace_dependency_check_entrypoints(
    db: Database, root: str, path: str
) -> dict[str, object]:
    del path
    return {
        "workspace_dependency_check": workspace_dependency_check(db, root, _DECLARED_DEPENDENCIES)
    }


def _metadata_entrypoints(db: Database, root: str, path: str) -> dict[str, object]:
    return {
        **_metadata_own_entrypoints(db, root, path),
        **_dependency_check_entrypoints(db, root, path),
        **_workspace_dependency_check_entrypoints(db, root, path),
    }


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(10), deadline=None)
@given(documents=_sampled_documents(_METADATA_DOCUMENTS))
def test_installed_packages_entrypoints_match_fresh_recomputation(
    mode: str, documents: list[str]
) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        site_dir, metadata, workspace, module = _build_metadata_workspace(tmpdir)
        with (
            patch.object(installed_packages, "_get_site_packages_dirs", lambda: (str(site_dir),)),
            patch.object(deep_module_resolution, "_get_sys_path_entries", lambda: (str(site_dir),)),
        ):
            incremental = Database(mode=mode)

            for step, content in enumerate(documents):
                previous = documents[step - 1] if step else None
                metadata.write_text(content, encoding="utf-8")
                warm = _metadata_entrypoints(incremental, str(workspace), str(module))
                fresh = _metadata_entrypoints(Database(mode=mode), str(workspace), str(module))
                for name in warm:
                    assert warm[name] == fresh[name], (
                        f"{name} warm!=fresh | mode={mode} | step={step} | "
                        f"{previous!r} -> {content!r}"
                    )


def _reload_the_metadata_pool_against_fresh(
    mode: str, documents: list[str], entrypoints: _EntrypointGroup
) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        site_dir, metadata, workspace, module = _build_metadata_workspace(tmpdir)
        with (
            patch.object(installed_packages, "_get_site_packages_dirs", lambda: (str(site_dir),)),
            patch.object(deep_module_resolution, "_get_sys_path_entries", lambda: (str(site_dir),)),
        ):
            for before, after in zip(documents, documents[1:], strict=False):
                metadata.write_text(before, encoding="utf-8")
                store = InMemoryArtifactStore()
                saver = Database(mode=mode, store=store)
                entrypoints(saver, str(workspace), str(module))

                # Edited before the save and the surfaces re-driven after it:
                # this read can hold an answer that disagrees with the file, and
                # the ordering is what puts one into the checkpoint.
                metadata.write_text(after, encoding="utf-8")
                entrypoints(saver, str(workspace), str(module))
                key = saver.save_checkpoint()

                reloaded = Database(mode=mode, store=store)
                reloaded.load_checkpoint(key)
                warm = entrypoints(reloaded, str(workspace), str(module))
                fresh = entrypoints(Database(mode=mode), str(workspace), str(module))
                for name in warm:
                    assert warm[name] == fresh[name], (
                        f"{name} reloaded!=fresh | mode={mode} | {before!r} -> {after!r}"
                    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(10), deadline=None)
@given(documents=_sampled_documents(_METADATA_DOCUMENTS))
def test_installed_packages_checkpoint_reload_matches_fresh(
    mode: str, documents: list[str]
) -> None:
    _reload_the_metadata_pool_against_fresh(mode, documents, _metadata_own_entrypoints)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(10), deadline=None)
@given(documents=_sampled_documents(_METADATA_DOCUMENTS))
def test_dependency_check_checkpoint_reload_matches_fresh(
    mode: str, documents: list[str]
) -> None:
    _reload_the_metadata_pool_against_fresh(mode, documents, _dependency_check_entrypoints)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(10), deadline=None)
@given(documents=_sampled_documents(_METADATA_DOCUMENTS))
def test_workspace_dependency_check_checkpoint_reload_matches_fresh(
    mode: str, documents: list[str]
) -> None:
    _reload_the_metadata_pool_against_fresh(
        mode, documents, _workspace_dependency_check_entrypoints
    )


# ---------------------------------------------------------------------------
# Code-generation schemas


def _schema_text(definitions: dict[str, Any], **dumps_kwargs: Any) -> str:
    return json.dumps({"$defs": definitions}, **dumps_kwargs)


_SCHEMA_A: dict[str, Any] = {
    "type": "object",
    "properties": {"y": {"type": "string"}, "x": {"type": "integer"}},
}
_SCHEMA_B: dict[str, Any] = {
    "type": "object",
    "properties": {"q": {"type": "string"}, "p": {"type": "integer"}},
}
_SCHEMA_C: dict[str, Any] = {"type": "object", "properties": {"z": {"type": "string"}}}

# Every member is error-free, and the restriction is not stylistic: both
# generating entrypoints raise on a schema whose analysis carries errors, so a
# malformed member would make this row raise instead of compare. The
# discriminating shapes are the key orders -- definitions and properties in both
# directions -- which a canonicalizing projection of the document maps onto one
# value while the emitted models take their order from the text.
_SCHEMA_DOCUMENTS = (
    _schema_text({"B": _SCHEMA_B, "A": _SCHEMA_A}),
    _schema_text({"A": _SCHEMA_A, "B": _SCHEMA_B}),
    _schema_text({"A": _SCHEMA_A, "B": _SCHEMA_B}, indent=2, sort_keys=True),
    _schema_text({"A": _SCHEMA_A}),
    _schema_text({"A": _SCHEMA_A, "B": _SCHEMA_B, "C": _SCHEMA_C}),
    _schema_text({"C": _SCHEMA_C, "B": _SCHEMA_B, "A": _SCHEMA_A}, indent=4),
    _schema_text({"A": _SCHEMA_A, "Alias": {"$ref": "#/$defs/A"}}),
    _schema_text({"A": {"type": "object", "properties": {"x": {"type": "integer"}}}}),
)


def _generated_tree(root: Path) -> dict[str, bytes]:
    """The emitted files, keyed by path, with the action's own manifest left out."""
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.startswith(".pyinc-action.")
    }


# generate_outputs is not driven directly: it is the action behind generate and
# it takes no output root -- the root is a reconcile argument rather than a
# parameter of the function -- so calling it alone would compare a desired output
# set that never reaches a disk. What is compared instead is what generate
# leaves behind. The ReconcileResult it returns is deliberately not compared:
# its created/updated/repaired split is a fact about what the destination
# already held, so an arm writing into a directory it has filled before and an
# arm writing into an empty one differ there by construction. Each arm therefore
# gets its own output root.
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(10), deadline=None)
@given(documents=_sampled_documents(_SCHEMA_DOCUMENTS))
def test_schema_entrypoints_match_fresh_recomputation(mode: str, documents: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        path = base / "sample.schema.json"
        warm_out = base / "warm"
        incremental = Database(mode=mode)

        for step, content in enumerate(documents):
            previous = documents[step - 1] if step else None
            path.write_text(content, encoding="utf-8")
            fresh = Database(mode=mode)
            fresh_out = base / f"fresh-{step}"

            warm_analysis = schema_analysis(incremental, str(path))
            fresh_analysis = schema_analysis(fresh, str(path))
            assert warm_analysis == fresh_analysis, (
                f"schema_analysis warm!=fresh | mode={mode} | step={step} | "
                f"{previous!r} -> {content!r}"
            )

            generate(incremental, str(path), warm_out)
            generate(fresh, str(path), fresh_out)
            warm_tree = _generated_tree(warm_out)
            fresh_tree = _generated_tree(fresh_out)
            assert warm_tree == fresh_tree, (
                f"generated tree warm!=fresh | mode={mode} | step={step} | "
                f"warm {sorted(warm_tree)} | fresh {sorted(fresh_tree)}"
            )


# The ordering below is the one that can put a stale answer into a checkpoint,
# and it is written that way here even though this read cannot be caught with
# one: the state it forms is not observable at any public surface -- the
# analysis, the emitted models and the generated tree all agree with a fresh
# read across every edit in this pool. So this is a consistency row rather than
# a counterexample row, and what would falsify a regression here is the
# hand-written row in tests/test_codegen.py that chooses its own semantic edit.
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=_examples(10), deadline=None)
@given(documents=_sampled_documents(_SCHEMA_DOCUMENTS))
def test_schema_checkpoint_reload_matches_fresh(mode: str, documents: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        path = base / "sample.schema.json"

        for index, (before, after) in enumerate(zip(documents, documents[1:], strict=False)):
            path.write_text(before, encoding="utf-8")
            store = InMemoryArtifactStore()
            saver = Database(mode=mode, store=store)
            schema_analysis(saver, str(path))
            generate(saver, str(path), base / f"saver-{index}")

            path.write_text(after, encoding="utf-8")
            schema_analysis(saver, str(path))
            generate(saver, str(path), base / f"saver-{index}")
            key = saver.save_checkpoint()

            reloaded = Database(mode=mode, store=store)
            reloaded.load_checkpoint(key)
            reloaded_analysis = schema_analysis(reloaded, str(path))
            fresh_analysis = schema_analysis(Database(mode=mode), str(path))
            assert reloaded_analysis == fresh_analysis, (
                f"schema_analysis reloaded!=fresh | mode={mode} | {before!r} -> {after!r}"
            )

            reloaded_out = base / f"reloaded-{index}"
            fresh_out = base / f"fresh-{index}"
            generate(reloaded, str(path), reloaded_out)
            generate(Database(mode=mode), str(path), fresh_out)
            reloaded_tree = _generated_tree(reloaded_out)
            fresh_tree = _generated_tree(fresh_out)
            assert reloaded_tree == fresh_tree, (
                f"generated tree reloaded!=fresh | mode={mode} | "
                f"reloaded {sorted(reloaded_tree)} | fresh {sorted(fresh_tree)}"
            )
