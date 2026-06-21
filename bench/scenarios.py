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
from collections.abc import Sequence
from pathlib import Path

from pyinc import Database, Input, InMemoryArtifactStore, query
from pyinc_codegen import generate
from pyinc_codegen.codegen import generate_outputs

_EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))

from calc.engine import calc_emit, evaluate_name  # noqa: E402

from .baselines import make_joblib_memory  # noqa: E402
from .harness import ScenarioResult, measure  # noqa: E402


def _tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


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
    state = {"root": 10, "leaf": [1, 2, 3, 4, 5, 6]}

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

    joblib_compute = None
    if "joblib" in comparators:
        memory = make_joblib_memory(str(out_dir / "joblib_synth"))

        @memory.cache  # type: ignore[misc]
        def jbranch(i: int, root: int, leaf: int) -> int:
            return root + leaf * 2

        def joblib_compute() -> int:  # type: ignore[misc]
            return sum(jbranch(i, state["root"], state["leaf"][i]) for i in range(_WIDTH))

    db = Database(mode="strict")
    store = InMemoryArtifactStore()
    results: list[ScenarioResult] = []

    def emit(scenario: str) -> None:
        apply_to(db)
        value, secs, peak = measure(lambda: db.get(_aggregate))
        results.append(
            ScenarioResult(
                "synthetic", scenario, "pyinc", secs, peak,
                len(db.dependency_graph()), db.statistics().node_count, value == reference(),
            )
        )
        if "full" in comparators:
            value, secs, peak = measure(lambda: sum(state["root"] + state["leaf"][i] * 2 for i in range(_WIDTH)))
            results.append(ScenarioResult("synthetic", scenario, "full", secs, peak, 0, 0, value == reference()))
        if "naive" in comparators:
            value, secs, peak = measure(naive_compute)
            results.append(ScenarioResult("synthetic", scenario, "naive", secs, peak, 0, len(naive_cache), value == reference()))
        if joblib_compute is not None:
            value, secs, peak = measure(joblib_compute)
            results.append(ScenarioResult("synthetic", scenario, "joblib", secs, peak, 0, 0, value == reference()))

    emit("cold")
    emit("unchanged")

    state["leaf"][0] += 100  # localized: one leaf
    emit("localized_semantic_edit")

    state["root"] += 1000  # high fan-out: shared root invalidates every branch
    emit("high_fanout_shared_edit")

    # checkpoint restore: warm a fresh database from a saved checkpoint
    db.save_checkpoint(store=store)
    db2 = Database(mode="strict", store=store)
    apply_to(db2)

    def restore() -> int:
        db2.load_checkpoint(db.save_checkpoint(store=store), store=store)
        return db2.get(_aggregate)

    value, secs, peak = measure(restore)
    results.append(
        ScenarioResult(
            "synthetic", "checkpoint_restore", "pyinc", secs, peak,
            len(db2.dependency_graph()), db2.statistics().node_count, value == reference(),
        )
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

    def emit(scenario: str) -> None:
        value, secs, peak = measure(lambda: calc_emit.reconcile(db, str(root), root=out_inc))
        results.append(
            ScenarioResult(
                "calc", scenario, "pyinc", secs, peak,
                len(db.dependency_graph()), db.statistics().node_count,
                _tree(out_inc) == fresh_tree(),
            )
        )
        if "full" in comparators:
            full_dir = work / "full"
            if full_dir.exists():
                shutil.rmtree(full_dir)
            _value, secs, peak = measure(lambda: calc_emit.reconcile(Database(mode="strict"), str(root), root=full_dir))
            results.append(ScenarioResult("calc", scenario, "full", secs, peak, 0, 0, _tree(full_dir) == fresh_tree()))
        if "naive" in comparators:
            _value, secs, peak = measure(naive_reconcile)
            results.append(ScenarioResult("calc", scenario, "naive", secs, peak, 0, len(naive_sig), _tree(naive_out) == fresh_tree()))

    emit("cold")
    emit("unchanged")

    unrelated.write_text("let z = 2\n", encoding="utf-8")  # not included anywhere
    emit("unreferenced_file_edit")

    root.write_text("# note\n" + _CALC_ROOT, encoding="utf-8")  # comment-only
    emit("comment_only_referenced_edit")

    root.write_text(_CALC_ROOT.replace("let c = 5", "let c = 6"), encoding="utf-8")  # one emit
    emit("localized_semantic_edit")

    constants.write_text("let base = 20\n", encoding="utf-8")  # shared by a and b
    emit("high_fanout_shared_edit")

    root.write_text(
        'include "constants.calc"\nlet a = base + 1\nlet b = base + 2\nemit a\nemit b\n',
        encoding="utf-8",
    )  # emit c removed
    emit("removed_emitted_artifact")

    (out_inc / "a.out").write_text("TAMPERED\n", encoding="utf-8")  # corrupt a generated file
    if "naive" in comparators and naive_out.exists():
        # The naive cache tracks input mtimes only, so it cannot notice that an
        # *output* was corrupted — it stays stale where the real action repairs.
        (naive_out / "a.out").write_text("TAMPERED\n", encoding="utf-8")
    emit("tampered_generated_output")  # real reconcile detects + repairs via content hash

    store = InMemoryArtifactStore()
    db.save_checkpoint(store=store)
    db3 = Database(mode="strict", store=store)
    out_ck = work / "ck"

    def restore() -> object:
        db3.load_checkpoint(db.save_checkpoint(store=store), store=store)
        return calc_emit.reconcile(db3, str(root), root=out_ck)

    _value, secs, peak = measure(restore)
    results.append(
        ScenarioResult(
            "calc", "checkpoint_restore", "pyinc", secs, peak,
            len(db3.dependency_graph()), db3.statistics().node_count,
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

    schema = json.loads(json.dumps(_SCHEMA))
    write(schema)
    db = Database(mode="strict")
    results: list[ScenarioResult] = []

    def emit(scenario: str) -> None:
        value, secs, peak = measure(lambda: generate(db, schema_path, out_inc))
        results.append(
            ScenarioResult(
                "codegen", scenario, "pyinc", secs, peak,
                len(db.dependency_graph()), db.statistics().node_count,
                _tree(out_inc) == fresh_tree(schema),
            )
        )
        if "full" in comparators:
            full_dir = work / "full"
            if full_dir.exists():
                shutil.rmtree(full_dir)
            measured = measure(lambda: generate(Database(mode="strict"), schema_path, full_dir))
            results.append(ScenarioResult("codegen", scenario, "full", measured[1], measured[2], 0, 0, _tree(full_dir) == fresh_tree(schema)))

    emit("cold")
    emit("unchanged")

    write(schema, indent=4)  # whitespace/formatting only
    emit("comment_only_referenced_edit")

    schema["$defs"]["Widget"]["required"] = ["id", "color"]  # type: ignore[index]
    write(schema)
    emit("localized_semantic_edit")

    schema["$defs"]["Color"]["enum"] = ["red", "green", "blue"]  # type: ignore[index]  # shared by Widget+Gizmo
    write(schema)
    emit("high_fanout_shared_edit")

    del schema["$defs"]["Size"]  # type: ignore[attr-defined]
    write(schema)
    emit("removed_emitted_artifact")

    (out_inc / "widget.py").write_text("# TAMPERED\n", encoding="utf-8")
    emit("tampered_generated_output")  # real reconcile repairs via content hash

    store = InMemoryArtifactStore()
    db.save_checkpoint(store=store)
    db2 = Database(mode="strict", store=store)
    out_ck = work / "ck"

    def restore() -> object:
        db2.load_checkpoint(db.save_checkpoint(store=store), store=store)
        return generate(db2, schema_path, out_ck)

    _value, secs, peak = measure(restore)
    results.append(
        ScenarioResult(
            "codegen", "checkpoint_restore", "pyinc", secs, peak,
            len(db2.dependency_graph()), db2.statistics().node_count,
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

    def emit(scenario: str) -> None:
        apply_to(db)
        value, secs, peak = measure(lambda: emit_files.reconcile(db, root=out_inc))
        results.append(
            ScenarioResult(
                "action", scenario, "pyinc", secs, peak,
                len(db.dependency_graph()), db.statistics().node_count,
                _tree(out_inc) == fresh_tree(),
            )
        )
        if "full" in comparators:
            full_dir = work / "full"
            if full_dir.exists():
                shutil.rmtree(full_dir)
            full_db = Database(mode="strict")
            apply_to(full_db)
            measured = measure(lambda: emit_files.reconcile(full_db, root=full_dir))
            results.append(ScenarioResult("action", scenario, "full", measured[1], measured[2], 0, 0, _tree(full_dir) == fresh_tree()))

    emit("cold")
    emit("unchanged")

    state["seed"] = 2  # changes every output
    emit("high_fanout_shared_edit")

    state["names"] = ("alpha", "beta")  # gamma removed
    emit("removed_emitted_artifact")

    (out_inc / "alpha.txt").write_text("TAMPERED", encoding="utf-8")
    emit("tampered_generated_output")  # real reconcile repairs via content hash

    return results


TARGETS = {
    "synthetic": _synthetic,
    "calc": _calc,
    "codegen": _codegen,
    "action": _action_target,
}
