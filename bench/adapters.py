"""Benchmark workloads.

Each workload exposes a uniform surface the scenario runner drives:

- ``seed()`` writes the initial inputs,
- ``mutate(scenario)`` applies a scenario edit (to inputs, or — for the tamper
  scenario — to the output tree), returning ``True`` if it applies,
- ``run_pyinc(db)`` performs the incremental work and returns :class:`RunMetrics`
  (including a content digest used for correctness),
- ``run_fresh()`` runs the *final* state with a brand-new, cache-free database and
  returns the digest only,
- ``run_naive()`` runs a deliberately simple whole-input cache baseline and
  returns the digest (or ``None`` where the scenario cannot be represented).

Workloads are cheap to construct against a fresh temp directory, so the runner
builds a new instance per repetition.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from pyinc import Database, InMemoryArtifactStore, Input, query
from pyinc.integrations.detection_rules import generate_detections
from pyinc.integrations.graphql_schema import generate_graphql

# Scenarios shared across workloads. Not every workload supports every scenario;
# unsupported combinations are reported as N/A by the runner.
ALL_SCENARIOS: tuple[str, ...] = (
    "cold",
    "warm",
    "presentation_edit",
    "semantic_edit",
    "high_fanout_edit",
    "output_tamper",
    "checkpoint_restore",
    "full_recompute",
)


@dataclass
class RunMetrics:
    digest: str
    writes: int = 0
    deletes: int = 0
    query_executions: int = 0
    query_reuses: int = 0
    query_backdates: int = 0
    graph_nodes: int = 0
    graph_edges: int = 0
    checkpoint_bytes: int = 0


def _digest_tree(root: Path) -> str:
    hasher = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(root)).replace("\\", "/")
            hasher.update(rel.encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(path.read_bytes())
            hasher.update(b"\0")
    return hasher.hexdigest()


def _digest_value(value: object) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def _kernel_metrics(db: Database, digest: str) -> RunMetrics:
    stats = db.statistics()
    graph = db.dependency_graph()
    edges = sum(len(node.dependency_labels) for node in graph)
    store = InMemoryArtifactStore()
    try:
        db.save_checkpoint(store)
        checkpoint_bytes = sum(len(v) for v in store.keys().values())
    except Exception:
        checkpoint_bytes = 0
    return RunMetrics(
        digest=digest,
        query_executions=stats.query_executions,
        query_reuses=stats.query_reuses,
        query_backdates=stats.query_backdates,
        graph_nodes=len(graph),
        graph_edges=edges,
        checkpoint_bytes=checkpoint_bytes,
    )


# ---------------------------------------------------------------------------
# Kernel workload (synthetic high fan-out graph; no filesystem)
# ---------------------------------------------------------------------------

_FANOUT = 16
_SHARED = Input[int]("bench_shared")
_LEAVES: tuple[Input[int], ...] = tuple(Input[int](f"bench_leaf_{i}") for i in range(_FANOUT))


@query
def _kernel_node(db: Database, index: int) -> int:
    return _LEAVES[index].read(db) * _SHARED.read(db)


@query
def _kernel_total(db: Database) -> int:
    return sum(_kernel_node(db, i) for i in range(_FANOUT))


class KernelWorkload:
    name = "kernel"
    scenarios = ("cold", "warm", "semantic_edit", "high_fanout_edit", "checkpoint_restore", "full_recompute")
    uses_checkpoint = True

    def __init__(self, base: Path) -> None:
        self._base = base
        self._shared = 1
        self._leaves = [i + 1 for i in range(_FANOUT)]

    def seed(self) -> None:
        self._shared = 1
        self._leaves = [i + 1 for i in range(_FANOUT)]

    def state_tuple(self) -> tuple[int, tuple[int, ...]]:
        """Public snapshot of the synthetic inputs, for the joblib baseline."""
        return self._shared, tuple(self._leaves)

    def mutate(self, scenario: str) -> bool:
        if scenario == "semantic_edit":
            self._leaves[0] += 100
            return True
        if scenario == "high_fanout_edit":
            self._shared += 1
            return True
        return scenario in ("warm", "cold", "checkpoint_restore", "full_recompute")

    def _apply(self, db: Database) -> int:
        db.set(_SHARED, self._shared)
        db.set_many([(_LEAVES[i], self._leaves[i]) for i in range(_FANOUT)])
        return db.get(_kernel_total)

    def run_pyinc(self, db: Database) -> RunMetrics:
        total = self._apply(db)
        return _kernel_metrics(db, _digest_value(total))

    def run_fresh(self) -> str:
        return self.run_pyinc(Database()).digest

    def run_naive(self) -> str | None:
        total = sum(self._leaves[i] * self._shared for i in range(_FANOUT))
        return _digest_value(total)

    def tamper(self) -> None:  # no filesystem outputs
        return None


# ---------------------------------------------------------------------------
# Generator workloads (filesystem outputs via the action layer)
# ---------------------------------------------------------------------------


class _GeneratorWorkload:
    name = "generator"
    scenarios = (
        "cold",
        "warm",
        "presentation_edit",
        "semantic_edit",
        "high_fanout_edit",
        "output_tamper",
        "checkpoint_restore",
        "full_recompute",
    )
    uses_checkpoint = True

    def __init__(self, base: Path) -> None:
        self._base = base
        self._inputs = base / "inputs"
        self._out = base / "out"
        self._state = base / "state"

    # -- subclasses implement these --
    def _write_inputs(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _mutate_inputs(self, scenario: str) -> bool:  # pragma: no cover
        raise NotImplementedError

    def _generate(self, db: Database, out: Path, state: Path) -> object:  # pragma: no cover
        raise NotImplementedError

    def _tamper_target(self) -> Path:  # pragma: no cover
        raise NotImplementedError

    # -- uniform surface --
    def seed(self) -> None:
        if self._inputs.exists():
            shutil.rmtree(self._inputs)
        self._inputs.mkdir(parents=True)
        self._write_inputs()

    def mutate(self, scenario: str) -> bool:
        if scenario == "output_tamper":
            return True  # applied against the output tree after cold (see runner)
        if scenario in ("cold", "warm", "checkpoint_restore", "full_recompute"):
            return True
        return self._mutate_inputs(scenario)

    def run_pyinc(self, db: Database) -> RunMetrics:
        result = self._generate(db, self._out, self._state)
        writes = len(getattr(result, "writes", ()))
        deletes = len(getattr(result, "deletions", ()))
        stats = db.statistics()
        graph = db.dependency_graph()
        edges = sum(len(node.dependency_labels) for node in graph)
        store = InMemoryArtifactStore()
        try:
            db.save_checkpoint(store)
            checkpoint_bytes = sum(len(v) for v in store.keys().values())
        except Exception:
            checkpoint_bytes = 0
        return RunMetrics(
            digest=_digest_tree(self._out),
            writes=writes,
            deletes=deletes,
            query_executions=stats.query_executions,
            query_reuses=stats.query_reuses,
            query_backdates=stats.query_backdates,
            graph_nodes=len(graph),
            graph_edges=edges,
            checkpoint_bytes=checkpoint_bytes,
        )

    def run_fresh(self) -> str:
        fresh_out = self._base / "fresh_out"
        fresh_state = self._base / "fresh_state"
        if fresh_out.exists():
            shutil.rmtree(fresh_out)
        if fresh_state.exists():
            shutil.rmtree(fresh_state)
        self._generate(Database(), fresh_out, fresh_state)
        return _digest_tree(fresh_out)

    def run_naive(self) -> str | None:
        # Deliberately simple whole-input cache: a fresh cache-free Database that
        # regenerates from scratch into a separate tree. No dependency-aware early
        # cutoff, ownership, or stale deletion — capability differences are noted
        # in the report.
        naive_out = self._base / "naive_out"
        naive_state = self._base / "naive_state"
        if naive_out.exists():
            shutil.rmtree(naive_out)
        if naive_state.exists():
            shutil.rmtree(naive_state)
        self._generate(Database(), naive_out, naive_state)
        return _digest_tree(naive_out)

    def tamper(self) -> None:
        target = self._tamper_target()
        if target.exists():
            target.write_bytes(b"TAMPERED-CONTENT\n")


class GraphqlWorkload(_GeneratorWorkload):
    name = "graphql"

    def _schema(self, *, user_desc: str, extra_field: bool, name_nonnull: bool) -> dict[str, object]:
        def named(n: str, k: str) -> dict[str, object]:
            return {"kind": k, "name": n, "ofType": None}

        def nn(ref: dict[str, object]) -> dict[str, object]:
            return {"kind": "NON_NULL", "name": None, "ofType": ref}

        idt, strt, role = named("ID", "SCALAR"), named("String", "SCALAR"), named("Role", "ENUM")
        user = named("User", "OBJECT")
        name_type = nn(strt) if name_nonnull else strt
        user_fields: list[dict[str, object]] = [
            {"name": "id", "description": None, "args": [], "type": nn(idt)},
            {"name": "name", "description": None, "args": [], "type": name_type},
            {"name": "role", "description": None, "args": [], "type": nn(role)},
        ]
        if extra_field:
            user_fields.append({"name": "email", "description": None, "args": [], "type": strt})
        query_fields = [
            {"name": "user", "description": None, "args": [{"name": "id", "description": None, "type": nn(idt)}], "type": user},
        ]
        return {
            "data": {
                "__schema": {
                    "queryType": {"name": "Query"},
                    "mutationType": None,
                    "types": [
                        {"kind": "SCALAR", "name": "ID", "description": None},
                        {"kind": "SCALAR", "name": "String", "description": None},
                        {"kind": "ENUM", "name": "Role", "description": "Access level.",
                         "enumValues": [{"name": "ADMIN", "description": None}, {"name": "MEMBER", "description": None}]},
                        {"kind": "OBJECT", "name": "User", "description": user_desc, "interfaces": [], "fields": user_fields},
                        {"kind": "OBJECT", "name": "Query", "description": "Root.", "interfaces": [], "fields": query_fields},
                    ],
                }
            }
        }

    def _write(self, *, user_desc: str, extra_field: bool, name_nonnull: bool, indent: int = 2) -> None:
        doc = self._schema(user_desc=user_desc, extra_field=extra_field, name_nonnull=name_nonnull)
        (self._inputs / "schema.json").write_text(json.dumps(doc, indent=indent))

    def _write_inputs(self) -> None:
        self._write(user_desc="A user.", extra_field=False, name_nonnull=True)

    def _mutate_inputs(self, scenario: str) -> bool:
        if scenario == "presentation_edit":
            self._write(user_desc="A user.", extra_field=False, name_nonnull=True, indent=6)
            return True
        if scenario == "semantic_edit":
            self._write(user_desc="A user.", extra_field=False, name_nonnull=False)
            return True
        if scenario == "high_fanout_edit":
            self._write(user_desc="A user.", extra_field=True, name_nonnull=True)
            return True
        return False

    def _generate(self, db: Database, out: Path, state: Path) -> object:
        return generate_graphql(db, self._inputs / "schema.json", out, state_dir=state)

    def _tamper_target(self) -> Path:
        return self._out / "models" / "User.py"


class DetectionWorkload(_GeneratorWorkload):
    name = "detection"

    def _rules_dir(self) -> Path:
        return self._inputs / "rules"

    def _write_mappings(self, *, process_splunk: str) -> None:
        (self._inputs / "mappings.json").write_text(json.dumps({
            "process.name": {"splunk": process_splunk, "sentinel": "ProcessName"},
            "command_line": {"splunk": "CommandLine", "sentinel": "CommandLine"},
            "unused.field": {"splunk": "Unused", "sentinel": "Unused"},
        }))

    def _write_rule(self, rid: str, *, severity: str, desc: str) -> None:
        (self._rules_dir() / f"{rid}.json").write_text(json.dumps({
            "id": rid, "title": rid, "severity": severity, "description": desc,
            "attack": ["T1059.001"],
            "detection": {"all": [
                {"field": "process.name", "op": "equals", "value": "powershell.exe"},
                {"field": "command_line", "op": "contains", "value": "-enc"},
            ]},
        }))

    def _write_inputs(self) -> None:
        self._rules_dir().mkdir(parents=True)
        self._write_mappings(process_splunk="Image")
        for i in range(4):
            self._write_rule(f"rule_{i}", severity="high", desc="Detects encoded PowerShell.")

    def _mutate_inputs(self, scenario: str) -> bool:
        if scenario == "presentation_edit":  # rule description-only edit
            self._write_rule("rule_0", severity="high", desc="An updated description only.")
            return True
        if scenario == "semantic_edit":  # change a rule's severity (metadata/coverage)
            self._write_rule("rule_0", severity="low", desc="Detects encoded PowerShell.")
            return True
        if scenario == "high_fanout_edit":  # shared mapping used by all rules
            self._write_mappings(process_splunk="ImageRenamed")
            return True
        return False

    def _generate(self, db: Database, out: Path, state: Path) -> object:
        return generate_detections(db, self._inputs, out, state_dir=state)

    def _tamper_target(self) -> Path:
        return self._out / "queries" / "splunk" / "rule_0.spl"


WorkloadFactory = type[KernelWorkload] | type[GraphqlWorkload] | type[DetectionWorkload]

WORKLOADS: tuple[WorkloadFactory, ...] = (KernelWorkload, GraphqlWorkload, DetectionWorkload)
