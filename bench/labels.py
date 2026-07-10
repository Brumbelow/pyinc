"""Human-readable display names for the benchmark report.

The ``target`` / ``scenario`` / ``engine`` strings on :class:`ScenarioResult`
are canonical identifiers — tests assert on them and they must stay stable. This
module maps those identifiers to readable titles, plain-English descriptions,
and column metadata used only when rendering the CSV and markdown reports. It is
presentation only; it never changes what is measured.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Targets — the four things the harness exercises.
# --------------------------------------------------------------------------- #

TARGET_LABELS: dict[str, str] = {
    "synthetic": "Synthetic query graph",
    "calc": "calc-with-includes fixture",
    "codegen": "JSON-Schema codegen",
    "action": "Action reconciliation",
}

# One-line context per target, shown under its heading. Notably, the synthetic
# target's "full recompute" is a trivial arithmetic sum, so pyinc's graph
# machinery is *slower* there — that target exists to stress graph mechanics and
# correctness, not to win on raw speed. The realistic targets (calc, codegen)
# are where incremental reuse pays off.
TARGET_NOTES: dict[str, str] = {
    "synthetic": (
        "A minimal query graph. Its full-recompute baseline is a trivial "
        "arithmetic sum measured in microseconds, so pyinc is *slower* in "
        "absolute terms here — this target checks graph mechanics and "
        "correctness, not raw speed."
    ),
    "calc": (
        "A small include-aware expression language reconciled to disk — a "
        "realistic workload where incremental reuse pays off."
    ),
    "codegen": (
        "The JSON-Schema → typed-Python compiler. Edits touch only the affected "
        "models, so incremental runs stay well under a full recompile."
    ),
    "action": (
        "Declared-output reconciliation: only changed files are written, and "
        "tampered outputs are repaired via content hash."
    ),
}

# --------------------------------------------------------------------------- #
# Engines — pyinc versus the comparators it is measured against.
# --------------------------------------------------------------------------- #

ENGINE_LABELS: dict[str, str] = {
    "pyinc": "pyinc (incremental)",
    "full": "full recompute",
    "naive": "naive per-key cache",
    "joblib": "joblib.Memory",
}

# The engine every scenario's speedup is measured against.
BASELINE_ENGINE = "full"

# --------------------------------------------------------------------------- #
# Scenarios — one canonical edit each, with a plain-English description of what
# the edit is and what a correct incremental engine should do with it.
# --------------------------------------------------------------------------- #

# token -> (short title, one-line description)
SCENARIO_LABELS: dict[str, tuple[str, str]] = {
    "cold": (
        "Cold build",
        "first run with an empty cache — everything computes from scratch",
    ),
    "unchanged": (
        "No-op rebuild",
        "re-run with nothing changed — everything should be reused",
    ),
    "unreferenced_file_edit": (
        "Edit an unused file",
        "change a file nothing depends on — no downstream work should run",
    ),
    "comment_only_referenced_edit": (
        "Comment-only edit",
        "edit only comments/whitespace of a referenced file — should backdate "
        "to zero downstream work",
    ),
    "localized_semantic_edit": (
        "Localized edit",
        "change one value used by one output — only that output recomputes",
    ),
    "high_fanout_shared_edit": (
        "Shared edit, high fan-out",
        "change one input many outputs depend on — every dependent recomputes",
    ),
    "removed_emitted_artifact": (
        "Remove an artifact",
        "stop declaring a previously emitted output — it is deleted from disk",
    ),
    "tampered_generated_output": (
        "Tampered output",
        "an out-of-band edit corrupts a generated file — content-hash repair restores it",
    ),
    "checkpoint_restore": (
        "Checkpoint restore",
        "warm a fresh database from a saved checkpoint instead of recomputing",
    ),
}

# --------------------------------------------------------------------------- #
# Metrics — CSV column name, readable header, and what each column means. Order
# matches the report layout. ``pyinc_only`` columns are blank for other engines
# because the harness does not compute a dependency graph or memo count for the
# comparators.
# --------------------------------------------------------------------------- #


class Metric:
    __slots__ = ("csv_field", "header", "description", "pyinc_only")

    def __init__(
        self, csv_field: str, header: str, description: str, *, pyinc_only: bool = False
    ) -> None:
        self.csv_field = csv_field
        self.header = header
        self.description = description
        self.pyinc_only = pyinc_only


WALL = Metric(
    "wall_seconds",
    "wall (ms)",
    "wall-clock time for the run (CSV in seconds, table in milliseconds)",
)
PEAK = Metric("peak_memory_kib", "peak (KiB)", "peak traced memory during the run, in KiB")
GRAPH = Metric(
    "dep_graph_edges",
    "graph edges",
    "edges in pyinc's dependency graph (pyinc only)",
    pyinc_only=True,
)
NODES = Metric(
    "memo_nodes",
    "memo nodes",
    "memoized nodes pyinc is holding — inputs, resources, and queries (pyinc only)",
    pyinc_only=True,
)
CORRECT = Metric(
    "matches_fresh",
    "correct?",
    "does the engine's output equal a fresh, cache-free run? pyinc is always yes",
)

METRICS: tuple[Metric, ...] = (WALL, PEAK, GRAPH, NODES, CORRECT)


def target_label(token: str) -> str:
    return TARGET_LABELS.get(token, token)


def engine_label(token: str) -> str:
    return ENGINE_LABELS.get(token, token)


def scenario_title(token: str) -> str:
    return SCENARIO_LABELS.get(token, (token, ""))[0]


def scenario_description(token: str) -> str:
    return SCENARIO_LABELS.get(token, (token, ""))[1]
