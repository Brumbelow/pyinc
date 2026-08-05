from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from scripts.run_mutation_gate import (
        MUTATIONS,
        Mutation,
        MutationGateError,
        apply_mutation,
        validate_mutations,
    )
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from run_mutation_gate import (  # noqa: E402
        MUTATIONS,
        Mutation,
        MutationGateError,
        apply_mutation,
        validate_mutations,
    )

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_six_soundness_mutations_have_distinct_targeted_regressions() -> None:
    assert {mutation.name for mutation in MUTATIONS} == {
        "dependency-edge-removed",
        "typed-equality-coerced",
        "resource-probe-verification-skipped",
        "stale-checkpoint-probe-hint-accepted",
        "action-manifest-path-validation-bypassed",
        "identity-safe-deletion-weakened",
    }
    assert len(MUTATIONS) == 6
    assert all(mutation.tests for mutation in MUTATIONS)
    assert len({node for mutation in MUTATIONS for node in mutation.tests}) == 6


def test_current_mutation_anchors_and_pytest_nodes_are_exact() -> None:
    validate_mutations(PROJECT_ROOT)


def test_apply_mutation_requires_one_complete_source_anchor(tmp_path: Path) -> None:
    mutation = Mutation(
        name="example",
        seam="test seam",
        source=Path("src/module.py"),
        before="guard = True\n",
        after="guard = False\n",
        tests=("tests/test_module.py::test_guard",),
    )
    source = tmp_path / mutation.source
    source.parent.mkdir(parents=True)
    source.write_text("guard = True\n", encoding="utf-8")

    apply_mutation(tmp_path, mutation)

    assert source.read_text(encoding="utf-8") == "guard = False\n"

    source.write_text("guard = True\nguard = True\n", encoding="utf-8")
    with pytest.raises(MutationGateError, match="found 2"):
        apply_mutation(tmp_path, mutation)

    source.write_text("guard = False\n", encoding="utf-8")
    with pytest.raises(MutationGateError, match="found 0"):
        apply_mutation(tmp_path, mutation)
