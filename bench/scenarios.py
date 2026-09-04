"""The four benchmark targets and the canonical edit sequence.

Each target is a callable ``(out_dir, comparators) -> list[ScenarioResult]``.
Every pyinc row's ``correct`` flag compares its output to a fresh, cache-free
recomputation; the tampered-output scenarios drive the *real* action
reconcile path (not a re-implemented comparison).
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypedDict, cast

from pyinc import Database, InMemoryArtifactStore, Input, ReconcileResult, query
from pyinc_codegen import generate

_EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))

from calc.engine import calc_emit  # noqa: E402

from .baselines import make_joblib_memory  # noqa: E402
from .measure import ScenarioResult, WorkMetrics, measure, measure_database  # noqa: E402


class _SyntheticState(TypedDict):
    root: int
    leaf: list[int]


def _tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.startswith(".pyinc-action.")
    }


def _pyinc_row(
    target: str,
    scenario: str,
    seconds: float,
    work: WorkMetrics,
    matches_fresh: bool,
) -> ScenarioResult:
    return ScenarioResult.pyinc(target, scenario, seconds, matches_fresh, work)


def _comparator_row(
    target: str,
    scenario: str,
    engine: str,
    seconds: float,
    matches_fresh: bool,
) -> ScenarioResult:
    return ScenarioResult.comparator(target, scenario, engine, seconds, matches_fresh)


def _expect_reconcile(
    target: str,
    scenario: str,
    result: ReconcileResult,
    *,
    created: Sequence[str] = (),
    updated: Sequence[str] = (),
    repaired: Sequence[str] = (),
    deleted: Sequence[str] = (),
    unchanged: Sequence[str] = (),
) -> None:
    expected = {
        "created": frozenset(created),
        "updated": frozenset(updated),
        "repaired": frozenset(repaired),
        "deleted": frozenset(deleted),
        "unchanged": frozenset(unchanged),
    }
    actual = {
        "created": frozenset(result.created),
        "updated": frozenset(result.updated),
        "repaired": frozenset(result.repaired),
        "deleted": frozenset(result.deleted),
        "unchanged": frozenset(result.unchanged),
    }
    if actual != expected or result.dry_run:
        raise AssertionError(
            f"{target}/{scenario} reconciliation semantics changed: "
            f"expected={expected!r}, actual={actual!r}, dry_run={result.dry_run}"
        )


def _expect_incremental_work(target: str, scenario: str, work: WorkMetrics) -> None:
    if scenario in {"unchanged", "unreferenced_file_edit"} and work.query_executions != 0:
        raise AssertionError(
            f"{target}/{scenario} performed {work.query_executions} query executions"
        )
    if scenario == "comment_only_referenced_edit" and (
        work.query_executions > work.resource_loads or work.query_backdates < 1
    ):
        # The source read answers with the text it compared, so it executes on
        # every edit; the only executions allowed here are those reloads. The
        # parse above each one must re-run equal and backdate.
        raise AssertionError(
            f"{target}/{scenario} did not backdate cleanly: "
            f"executions={work.query_executions}, resource_loads={work.resource_loads}, "
            f"backdates={work.query_backdates}"
        )
    if scenario == "localized_semantic_edit" and work.query_executions == 0:
        raise AssertionError(f"{target}/{scenario} did not execute the affected query path")
    if scenario == "tampered_generated_output" and work.query_executions != 0:
        raise AssertionError(
            f"{target}/{scenario} recomputed queries while repairing an output"
        )


# --------------------------------------------------------------------------- #
# Target 1: synthetic kernel query graph
# --------------------------------------------------------------------------- #

_ROOT = Input[int]("bench_synth_root")
_L0 = Input[int]("bench_synth_l0")
_L1 = Input[int]("bench_synth_l1")
_L2 = Input[int]("bench_synth_l2")
_L3 = Input[int]("bench_synth_l3")
_L4 = Input[int]("bench_synth_l4")
_L5 = Input[int]("bench_synth_l5")
_WIDTH = 6


@query
def _branch(db: Database, i: int) -> int:
    leaves = (_L0, _L1, _L2, _L3, _L4, _L5)  # local tuple of individually-captured Inputs
    return _ROOT.read(db) + leaves[i].read(db) * 2


@query
def _aggregate(db: Database) -> int:
    return sum(_branch(db, i) for i in range(_WIDTH))


def _synthetic(*, out_dir: Path, comparators: Sequence[str]) -> list[ScenarioResult]:
    leaves = (_L0, _L1, _L2, _L3, _L4, _L5)
    state: _SyntheticState = {"root": 10, "leaf": [1, 2, 3, 4, 5, 6]}

    def reference() -> int:
        return sum(state["root"] + state["leaf"][i] * 2 for i in range(_WIDTH))

    def apply_to(db: Database) -> None:
        db.set(_ROOT, state["root"])
        db.set_many([(leaves[i], state["leaf"][i]) for i in range(_WIDTH)])

    # naive cache: keyed per branch on its leaf value, FORGETTING the shared root.
    naive_cache: dict[int, tuple[int, int]] = {}

    def naive_compute() -> int:
        total = 0
        for i in range(_WIDTH):
            leaf = state["leaf"][i]
            cached = naive_cache.get(i)
            if cached is None or cached[0] != leaf:
                value = state["root"] + leaf * 2
                naive_cache[i] = (leaf, value)
            else:
                value = cached[1]  # stale when only the shared root changed
            total += value
        return total

    joblib_compute: Callable[[], int] | None = None
    if "joblib" in comparators:
        memory = make_joblib_memory(str(out_dir / "joblib_synth"))

        def jbranch(i: int, root: int, leaf: int) -> int:
            return root + leaf * 2

        cached_jbranch = memory.cache(jbranch)

        def compute_with_joblib() -> int:
            return sum(
                cached_jbranch(i, state["root"], state["leaf"][i]) for i in range(_WIDTH)
            )

        joblib_compute = compute_with_joblib

    db = Database(mode="strict")
    store = InMemoryArtifactStore()
    results: list[ScenarioResult] = []

    def emit(scenario: str) -> None:
        apply_to(db)
        value, secs, work = measure_database(db, lambda: db.get(_aggregate))
        _expect_incremental_work("synthetic", scenario, work)
        if scenario == "localized_semantic_edit" and work.query_executions != 2:
            raise AssertionError(
                "synthetic/localized_semantic_edit must execute one branch and the aggregate"
            )
        results.append(_pyinc_row("synthetic", scenario, secs, work, value == reference()))
        if "full" in comparators:
            value, secs = measure(
                lambda: sum(state["root"] + state["leaf"][i] * 2 for i in range(_WIDTH))
            )
            results.append(_comparator_row("synthetic", scenario, "full", secs, value == reference()))
        if "naive" in comparators:
            value, secs = measure(naive_compute)
            results.append(_comparator_row("synthetic", scenario, "naive", secs, value == reference()))
        if joblib_compute is not None:
            value, secs = measure(joblib_compute)
            results.append(
                _comparator_row("synthetic", scenario, "joblib", secs, value == reference())
            )

    emit("cold")
    emit("unchanged")

    state["leaf"][0] += 100  # localized: one leaf
    emit("localized_semantic_edit")

    state["root"] += 1000  # high fan-out: shared root invalidates every branch
    emit("high_fanout_shared_edit")

    # checkpoint restore: warm a fresh database from a saved checkpoint
    checkpoint = db.save_checkpoint(store=store)
    db2 = Database(mode="strict", store=store)
    apply_to(db2)

    def restore() -> int:
        db2.load_checkpoint(checkpoint, store=store)
        return db2.get(_aggregate)

    value, secs, work = measure_database(db2, restore)
    results.append(
        _pyinc_row("synthetic", "checkpoint_restore", secs, work, value == reference())
    )
    return results


# --------------------------------------------------------------------------- #
# Target 2: calc-with-includes (covers the full edit sequence)
# --------------------------------------------------------------------------- #

_CALC_ROOT = (
    'include "constants.calc"\n'
    "let a = base + 1\n"
    "let b = base + 2\n"
    "let c = 5\n"
    "emit a\nemit b\nemit c\n"
)


def _calc(*, out_dir: Path, comparators: Sequence[str]) -> list[ScenarioResult]:
    work = out_dir / "calc_work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    constants = work / "constants.calc"
    root = work / "m.calc"
    unrelated = work / "other.calc"
    out_inc = work / "inc"

    constants.write_text("let base = 10\n", encoding="utf-8")
    root.write_text(_CALC_ROOT, encoding="utf-8")
    unrelated.write_text("let z = 1\n", encoding="utf-8")

    db = Database(mode="strict")
    results: list[ScenarioResult] = []
    naive_sig: dict[str, float] = {}
    naive_out = work / "naive"

    def fresh_tree() -> dict[str, bytes]:
        fresh_dir = work / "fresh"
        if fresh_dir.exists():
            shutil.rmtree(fresh_dir)
        calc_emit.reconcile(Database(mode="strict"), str(root), root=fresh_dir)
        return _tree(fresh_dir)

    def naive_reconcile() -> None:
        # Regenerate only when an INPUT file's mtime changed; blind to output tampering.
        sig = {f.name: f.stat().st_mtime_ns for f in (constants, root)}
        if sig != naive_sig:
            naive_sig.clear()
            naive_sig.update(sig)
            if naive_out.exists():
                shutil.rmtree(naive_out)
            calc_emit.reconcile(Database(mode="strict"), str(root), root=naive_out)

    def emit(
        scenario: str,
        *,
        created: Sequence[str] = (),
        updated: Sequence[str] = (),
        repaired: Sequence[str] = (),
        deleted: Sequence[str] = (),
        unchanged: Sequence[str] = (),
    ) -> None:
        reference_tree = fresh_tree()
        value, secs, work_metrics = measure_database(
            db, lambda: calc_emit.reconcile(db, str(root), root=out_inc)
        )
        _expect_reconcile(
            "calc",
            scenario,
            value,
            created=created,
            updated=updated,
            repaired=repaired,
            deleted=deleted,
            unchanged=unchanged,
        )
        _expect_incremental_work("calc", scenario, work_metrics)
        results.append(
            _pyinc_row("calc", scenario, secs, work_metrics, _tree(out_inc) == reference_tree)
        )
        if "full" in comparators:
            full_dir = work / "full"
            if full_dir.exists():
                shutil.rmtree(full_dir)
            _full_value, secs = measure(
                lambda: calc_emit.reconcile(Database(mode="strict"), str(root), root=full_dir)
            )
            results.append(
                _comparator_row("calc", scenario, "full", secs, _tree(full_dir) == reference_tree)
            )
        if "naive" in comparators:
            _naive_value, secs = measure(naive_reconcile)
            results.append(
                _comparator_row("calc", scenario, "naive", secs, _tree(naive_out) == reference_tree)
            )

    emit("cold", created=("a.out", "b.out", "c.out"))
    emit("unchanged", unchanged=("a.out", "b.out", "c.out"))

    unrelated.write_text("let z = 2\n", encoding="utf-8")  # not included anywhere
    emit("unreferenced_file_edit", unchanged=("a.out", "b.out", "c.out"))

    root.write_text("# note\n" + _CALC_ROOT, encoding="utf-8")  # comment-only
    emit("comment_only_referenced_edit", unchanged=("a.out", "b.out", "c.out"))

    root.write_text(_CALC_ROOT.replace("let c = 5", "let c = 6"), encoding="utf-8")  # one emit
    emit("localized_semantic_edit", updated=("c.out",), unchanged=("a.out", "b.out"))

    constants.write_text("let base = 20\n", encoding="utf-8")  # shared by a and b
    emit("high_fanout_shared_edit", updated=("a.out", "b.out"), unchanged=("c.out",))

    root.write_text(
        'include "constants.calc"\nlet a = base + 1\nlet b = base + 2\nemit a\nemit b\n',
        encoding="utf-8",
    )  # emit c removed
    emit("removed_emitted_artifact", deleted=("c.out",), unchanged=("a.out", "b.out"))

    (out_inc / "a.out").write_text("TAMPERED\n", encoding="utf-8")  # corrupt a generated file
    if "naive" in comparators and naive_out.exists():
        # The naive cache tracks input mtimes only, so it cannot notice that an
        # *output* was corrupted — it stays stale where the real action repairs.
        (naive_out / "a.out").write_text("TAMPERED\n", encoding="utf-8")
    emit(
        "tampered_generated_output",
        repaired=("a.out",),
        unchanged=("b.out",),
    )  # real reconcile detects + repairs via content hash

    store = InMemoryArtifactStore()
    checkpoint = db.save_checkpoint(store=store)
    db3 = Database(mode="strict", store=store)
    out_ck = work / "ck"

    def restore() -> object:
        db3.load_checkpoint(checkpoint, store=store)
        return calc_emit.reconcile(db3, str(root), root=out_ck)

    _value, secs, checkpoint_work = measure_database(db3, restore)
    results.append(
        _pyinc_row(
            "calc",
            "checkpoint_restore",
            secs,
            checkpoint_work,
            _tree(out_ck) == fresh_tree(),
        )
    )
    return results


# --------------------------------------------------------------------------- #
# Target 3: JSON-Schema code generation
# --------------------------------------------------------------------------- #

_SCHEMA = {
    "$defs": {
        "Color": {"type": "string", "enum": ["red", "green"]},
        "Size": {"type": "string", "enum": ["s", "m", "l"]},
        "Widget": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "color": {"$ref": "#/$defs/Color"},
                "size": {"$ref": "#/$defs/Size"},
            },
            "required": ["id"],
        },
        "Gizmo": {
            "type": "object",
            "properties": {"color": {"$ref": "#/$defs/Color"}},
        },
    }
}


def _codegen(*, out_dir: Path, comparators: Sequence[str]) -> list[ScenarioResult]:
    work = out_dir / "codegen_work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    schema_path = work / "schema.json"
    out_inc = work / "inc"

    def write(schema: dict[str, object], *, indent: int = 2) -> None:
        schema_path.write_text(json.dumps(schema, indent=indent), encoding="utf-8")

    def fresh_tree(schema: dict[str, object]) -> dict[str, bytes]:
        fresh_dir = work / "fresh"
        if fresh_dir.exists():
            shutil.rmtree(fresh_dir)
        generate(Database(mode="strict"), schema_path, fresh_dir)
        return _tree(fresh_dir)

    loaded_schema: object = json.loads(json.dumps(_SCHEMA))
    schema = cast(dict[str, object], loaded_schema)
    definitions = cast(dict[str, dict[str, object]], schema["$defs"])
    write(schema)
    db = Database(mode="strict")
    results: list[ScenarioResult] = []

    def emit(
        scenario: str,
        *,
        created: Sequence[str] = (),
        updated: Sequence[str] = (),
        repaired: Sequence[str] = (),
        deleted: Sequence[str] = (),
        unchanged: Sequence[str] = (),
    ) -> None:
        reference_tree = fresh_tree(schema)
        value, secs, work_metrics = measure_database(db, lambda: generate(db, schema_path, out_inc))
        _expect_reconcile(
            "codegen",
            scenario,
            value,
            created=created,
            updated=updated,
            repaired=repaired,
            deleted=deleted,
            unchanged=unchanged,
        )
        _expect_incremental_work("codegen", scenario, work_metrics)
        results.append(
            _pyinc_row(
                "codegen", scenario, secs, work_metrics, _tree(out_inc) == reference_tree
            )
        )
        if "full" in comparators:
            full_dir = work / "full"
            if full_dir.exists():
                shutil.rmtree(full_dir)
            _value, full_secs = measure(
                lambda: generate(Database(mode="strict"), schema_path, full_dir)
            )
            results.append(
                _comparator_row(
                    "codegen", scenario, "full", full_secs, _tree(full_dir) == reference_tree
                )
            )

    all_outputs = (
        "__init__.py",
        "color.py",
        "docs/color.md",
        "docs/gizmo.md",
        "docs/size.md",
        "docs/widget.md",
        "gizmo.py",
        "size.py",
        "widget.py",
    )

    emit("cold", created=all_outputs)
    emit("unchanged", unchanged=all_outputs)

    write(schema, indent=4)  # whitespace/formatting only
    emit("comment_only_referenced_edit", unchanged=all_outputs)

    definitions["Widget"]["required"] = ["id", "color"]
    write(schema)
    emit(
        "localized_semantic_edit",
        updated=("docs/widget.md", "widget.py"),
        unchanged=tuple(
            path for path in all_outputs if path not in {"docs/widget.md", "widget.py"}
        ),
    )

    definitions["Color"]["enum"] = ["red", "green", "blue"]  # shared by Widget+Gizmo
    write(schema)
    emit(
        "high_fanout_shared_edit",
        updated=("color.py", "docs/color.md"),
        unchanged=tuple(path for path in all_outputs if path not in {"color.py", "docs/color.md"}),
    )

    widget_properties = cast(dict[str, object], definitions["Widget"]["properties"])
    del widget_properties["size"]
    del definitions["Size"]
    write(schema)
    remaining_outputs = tuple(
        path for path in all_outputs if path not in {"docs/size.md", "size.py"}
    )
    emit(
        "removed_emitted_artifact",
        updated=("__init__.py", "docs/widget.md", "widget.py"),
        deleted=("docs/size.md", "size.py"),
        unchanged=("color.py", "docs/color.md", "docs/gizmo.md", "gizmo.py"),
    )

    (out_inc / "widget.py").write_text("# TAMPERED\n", encoding="utf-8")
    emit(
        "tampered_generated_output",
        repaired=("widget.py",),
        unchanged=tuple(path for path in remaining_outputs if path != "widget.py"),
    )  # real reconcile repairs via content hash

    store = InMemoryArtifactStore()
    checkpoint = db.save_checkpoint(store=store)
    db2 = Database(mode="strict", store=store)
    out_ck = work / "ck"

    def restore() -> object:
        db2.load_checkpoint(checkpoint, store=store)
        return generate(db2, schema_path, out_ck)

    _value, secs, checkpoint_work = measure_database(db2, restore)
    results.append(
        _pyinc_row(
            "codegen",
            "checkpoint_restore",
            secs,
            checkpoint_work,
            _tree(out_ck) == fresh_tree(schema),
        )
    )
    return results


# --------------------------------------------------------------------------- #
# Target 4: action planning + reconciliation in isolation
# --------------------------------------------------------------------------- #

_ACT_NAMES = Input[tuple[str, ...]]("bench_act_names")
_ACT_SEED = Input[int]("bench_act_seed")


@query
def _act_value(db: Database, name: str) -> str:
    return f"{name}:{_ACT_SEED.read(db)}"


def _action_target(*, out_dir: Path, comparators: Sequence[str]) -> list[ScenarioResult]:
    from pyinc import Output, action

    @action(tool="bench-action")
    def emit_files(db: Database) -> list[Output]:
        return [Output.text(f"{name}.txt", _act_value(db, name)) for name in _ACT_NAMES.read(db)]

    work = out_dir / "action_work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    out_inc = work / "inc"

    state = {"names": ("alpha", "beta", "gamma"), "seed": 1}
    db = Database(mode="strict")

    def apply_to(target: Database) -> None:
        target.set(_ACT_NAMES, state["names"])
        target.set(_ACT_SEED, state["seed"])

    def fresh_tree() -> dict[str, bytes]:
        fresh_dir = work / "fresh"
        if fresh_dir.exists():
            shutil.rmtree(fresh_dir)
        fresh_db = Database(mode="strict")
        apply_to(fresh_db)
        emit_files.reconcile(fresh_db, root=fresh_dir)
        return _tree(fresh_dir)

    results: list[ScenarioResult] = []

    def emit(
        scenario: str,
        *,
        created: Sequence[str] = (),
        updated: Sequence[str] = (),
        repaired: Sequence[str] = (),
        deleted: Sequence[str] = (),
        unchanged: Sequence[str] = (),
    ) -> None:
        apply_to(db)
        reference_tree = fresh_tree()
        value, secs, work_metrics = measure_database(
            db, lambda: emit_files.reconcile(db, root=out_inc)
        )
        _expect_reconcile(
            "action",
            scenario,
            value,
            created=created,
            updated=updated,
            repaired=repaired,
            deleted=deleted,
            unchanged=unchanged,
        )
        _expect_incremental_work("action", scenario, work_metrics)
        results.append(
            _pyinc_row(
                "action", scenario, secs, work_metrics, _tree(out_inc) == reference_tree
            )
        )
        if "full" in comparators:
            full_dir = work / "full"
            if full_dir.exists():
                shutil.rmtree(full_dir)
            full_db = Database(mode="strict")
            apply_to(full_db)
            _value, full_secs = measure(lambda: emit_files.reconcile(full_db, root=full_dir))
            results.append(
                _comparator_row(
                    "action", scenario, "full", full_secs, _tree(full_dir) == reference_tree
                )
            )

    emit("cold", created=("alpha.txt", "beta.txt", "gamma.txt"))
    emit("unchanged", unchanged=("alpha.txt", "beta.txt", "gamma.txt"))

    state["seed"] = 2  # changes every output
    emit("high_fanout_shared_edit", updated=("alpha.txt", "beta.txt", "gamma.txt"))

    state["names"] = ("alpha", "beta")  # gamma removed
    emit("removed_emitted_artifact", deleted=("gamma.txt",), unchanged=("alpha.txt", "beta.txt"))

    (out_inc / "alpha.txt").write_text("TAMPERED", encoding="utf-8")
    emit(
        "tampered_generated_output",
        repaired=("alpha.txt",),
        unchanged=("beta.txt",),
    )  # real reconcile repairs via content hash

    return results


TARGETS = {
    "synthetic": _synthetic,
    "calc": _calc,
    "codegen": _codegen,
    "action": _action_target,
}
