"""Stable benchmark identifiers and human-readable report labels."""

from __future__ import annotations

TARGET_LABELS: dict[str, str] = {
    "synthetic": "Synthetic query graph",
    "calc": "calc-with-includes fixture",
    "codegen": "JSON-Schema codegen",
    "action": "Action reconciliation",
}

TARGET_NOTES: dict[str, str] = {
    "synthetic": (
        "A minimal graph whose full baseline is arithmetic; it checks dependency "
        "tracking and bounded work rather than speed."
    ),
    "calc": "An include-aware expression language with declared output reconciliation.",
    "codegen": "The JSON-Schema to typed-Python compiler and its output action.",
    "action": "Declared-output creation, reuse, deletion, and tamper repair.",
}

ENGINE_LABELS: dict[str, str] = {
    "pyinc": "pyinc",
    "full": "full recompute",
    "naive": "naive cache",
    "joblib": "joblib.Memory",
}

SCENARIO_LABELS: dict[str, tuple[str, str]] = {
    "cold": ("Cold build", "compute from an empty cache"),
    "unchanged": ("No-op rebuild", "reuse the unchanged graph"),
    "unreferenced_file_edit": (
        "Edit an unused file",
        "change a file outside the dependency graph",
    ),
    "comment_only_referenced_edit": (
        "Formatting-only edit",
        "backdate an equal semantic value and reuse downstream work",
    ),
    "localized_semantic_edit": (
        "Localized edit",
        "recompute only the affected path and output",
    ),
    "high_fanout_shared_edit": (
        "Shared edit",
        "recompute every dependent of one shared input",
    ),
    "removed_emitted_artifact": (
        "Remove an artifact",
        "delete an output no longer declared by the action",
    ),
    "tampered_generated_output": (
        "Tampered output",
        "repair an out-of-band output change without query work",
    ),
    "checkpoint_restore": (
        "Checkpoint restore",
        "load a pre-saved checkpoint and request the warmed result",
    ),
}


def target_label(token: str) -> str:
    return TARGET_LABELS.get(token, token)


def engine_label(token: str) -> str:
    return ENGINE_LABELS.get(token, token)


def scenario_title(token: str) -> str:
    return SCENARIO_LABELS.get(token, (token, ""))[0]


def scenario_description(token: str) -> str:
    return SCENARIO_LABELS.get(token, (token, ""))[1]
