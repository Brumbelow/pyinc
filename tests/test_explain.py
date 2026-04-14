from __future__ import annotations

from pyinc.explain import InspectionNode, format_explanation


def _leaf(label: str = "leaf", reason: str = "set by user") -> InspectionNode:
    return InspectionNode(
        label=label,
        kind="input",
        changed_at=1,
        verified_at=1,
        last_decision="green",
        last_recompute="never",
        reason=reason,
    )


def test_format_explanation_single_node() -> None:
    node = _leaf()
    output = format_explanation(node)
    assert "leaf: green" in output
    assert "reason: set by user" in output
    assert output.count("\n") == 1


def test_format_explanation_with_dependencies() -> None:
    child = _leaf(label="child_input")
    parent = InspectionNode(
        label="parent_query",
        kind="query",
        changed_at=2,
        verified_at=2,
        last_decision="recomputed",
        last_recompute="r2",
        reason="",
        dependencies=(child,),
    )
    output = format_explanation(parent)
    lines = output.split("\n")
    # Parent at depth 0, child at depth 1 (indented).
    assert lines[0].startswith("- parent_query:")
    assert lines[1].startswith("  - child_input:")


def test_format_explanation_with_untracked_reasons() -> None:
    node = InspectionNode(
        label="impure_query",
        kind="query",
        changed_at=1,
        verified_at=1,
        last_decision="recomputed",
        last_recompute="r1",
        reason="",
        untracked_reasons=("dynamic __all__", "os.getenv call"),
    )
    output = format_explanation(node)
    assert "untracked: dynamic __all__" in output
    assert "untracked: os.getenv call" in output


def test_inspection_node_is_untracked_property() -> None:
    clean = _leaf()
    assert not clean.is_untracked

    impure = InspectionNode(
        label="q",
        kind="query",
        changed_at=1,
        verified_at=1,
        last_decision="green",
        last_recompute="r1",
        reason="",
        untracked_reasons=("raw read",),
    )
    assert impure.is_untracked
