#!/usr/bin/env python3
"""Run the bounded soundness mutation gate in isolated candidate copies."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MutationGateError(RuntimeError):
    """The mutation gate could not establish a trustworthy result."""


@dataclass(frozen=True)
class Mutation:
    """One exact source replacement and the regression that must kill it."""

    name: str
    seam: str
    source: Path
    before: str
    after: str
    tests: tuple[str, ...]


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        name="dependency-edge-removed",
        seam="remove a query dependency edge",
        source=Path("src/pyinc/runtime.py"),
        before=(
            "    def _record_dependency(self, key: NodeKey) -> None:\n"
            "        frame = self._current_frame()\n"
            "        if frame is None:\n"
            "            return\n"
            "        frame.dependencies.add(key)\n"
        ),
        after=(
            "    def _record_dependency(self, key: NodeKey) -> None:\n"
            "        frame = self._current_frame()\n"
            "        if frame is None:\n"
            "            return\n"
            "        return\n"
        ),
        tests=("tests/test_runtime.py::test_dynamic_dependencies_drop_stale_edges",),
    ),
    Mutation(
        name="typed-equality-coerced",
        seam="flip typed snapshot equality back to Python numeric equality",
        source=Path("src/pyinc/value.py"),
        before=(
            "    left_type = type(left)\n"
            "    if left_type is not type(right):\n"
            "        return False\n"
        ),
        after=(
            "    left_type = type(left)\n"
            "    if left_type is not type(right):\n"
            "        return bool(left == right)\n"
        ),
        tests=(
            "tests/test_typed_equality_soundness.py::"
            "test_semantic_equal_rejects_python_equal_numeric_representations",
        ),
    ),
    Mutation(
        name="resource-probe-verification-skipped",
        seam="reuse a resource record without comparing its live probe",
        source=Path("src/pyinc/runtime.py"),
        before=(
            "            and not record.is_failed\n"
            "            and not record.probe_unconfirmed\n"
            "            and snapshots_equal(record.snapshot, record.snapshot)\n"
            "            and snapshots_equal(record.probe, probe_snapshot)\n"
            "        ):\n"
            "            record.verified_at = self._revision\n"
        ),
        after=(
            "            and not record.is_failed\n"
            "            and not record.probe_unconfirmed\n"
            "            and snapshots_equal(record.snapshot, record.snapshot)\n"
            "            and True\n"
            "        ):\n"
            "            record.verified_at = self._revision\n"
        ),
        tests=(
            "tests/test_resource_probe_split.py::"
            "test_probe_mismatch_stores_the_atomically_observed_pair",
        ),
    ),
    Mutation(
        name="stale-checkpoint-probe-hint-accepted",
        seam="accept a checkpoint resource hint without comparing its live probe",
        source=Path("src/pyinc/runtime.py"),
        before=(
            "        if snapshots_equal(probe_snapshot, expected_probe_snapshot) and "
            "self._adapter_keys_trusted(\n"
        ),
        after=("        if True and self._adapter_keys_trusted(\n"),
        tests=(
            "tests/test_typed_equality_soundness.py::"
            "test_checkpoint_probe_hint_rejects_typed_numeric_collision",
        ),
    ),
    Mutation(
        name="action-manifest-path-validation-bypassed",
        seam="bypass validation of every claimed Action manifest path",
        source=Path("src/pyinc/action.py"),
        before='        _validate_path_set(raw_outputs, source="manifest")\n',
        after='        _validate_path_set({}, source="manifest")\n',
        tests=(
            "tests/test_action_adversarial_soundness.py::"
            "test_incarnation_mismatch_does_not_bypass_manifest_validation",
        ),
    ),
    Mutation(
        name="identity-safe-deletion-weakened",
        seam="drop the expected digest from Action quarantine deletion",
        source=Path("src/pyinc/action.py"),
        before=(
            "                    # The current directory entry is moved out of the live path\n"
            "                    # before verification.  Only that quarantined identity is\n"
            "                    # deleted, so a replacement cannot win a check/unlink race.\n"
            "                    if unlink_regular_file(target, expected_digest=previous[relative]):\n"
        ),
        after=(
            "                    # The current directory entry is moved out of the live path\n"
            "                    # before verification.  Only that quarantined identity is\n"
            "                    # deleted, so a replacement cannot win a check/unlink race.\n"
            "                    if unlink_regular_file(target):\n"
        ),
        tests=(
            "tests/test_action_adversarial_soundness.py::"
            "test_digest_race_restores_changed_leaf_and_reports_no_deletion",
        ),
    ),
)


def _validate_relative_path(path: Path, *, description: str) -> None:
    if path.is_absolute() or ".." in path.parts:
        raise MutationGateError(f"{description} must stay inside the candidate tree: {path}")


def validate_mutations(root: Path = PROJECT_ROOT) -> None:
    """Fail closed if a mutation anchor or targeted regression has drifted."""
    if len(MUTATIONS) != 6:
        raise MutationGateError(f"expected exactly six soundness mutations, found {len(MUTATIONS)}")
    names = [mutation.name for mutation in MUTATIONS]
    if len(set(names)) != len(names):
        raise MutationGateError("mutation names must be unique")

    for mutation in MUTATIONS:
        _validate_relative_path(mutation.source, description=f"{mutation.name} source")
        source = root / mutation.source
        try:
            text = source.read_text(encoding="utf-8")
        except OSError as error:
            raise MutationGateError(f"cannot read {mutation.source}: {error}") from error
        occurrences = text.count(mutation.before)
        if occurrences != 1:
            raise MutationGateError(
                f"{mutation.name}: expected its exact source anchor once in "
                f"{mutation.source}, found {occurrences}"
            )
        if mutation.before == mutation.after:
            raise MutationGateError(f"{mutation.name}: replacement does not change the source")
        if not mutation.tests:
            raise MutationGateError(f"{mutation.name}: no targeted regression command")
        for node in mutation.tests:
            relative_test, separator, test_name = node.partition("::")
            if not separator or not test_name:
                raise MutationGateError(f"{mutation.name}: invalid pytest node {node!r}")
            test_path = Path(relative_test)
            _validate_relative_path(test_path, description=f"{mutation.name} test")
            try:
                test_text = (root / test_path).read_text(encoding="utf-8")
            except OSError as error:
                raise MutationGateError(
                    f"cannot read targeted regression {test_path}: {error}"
                ) from error
            top_level_name = test_name.split("::", 1)[0].split("[", 1)[0]
            if f"def {top_level_name}(" not in test_text:
                raise MutationGateError(
                    f"{mutation.name}: targeted regression {top_level_name!r} "
                    f"is absent from {test_path}"
                )


def apply_mutation(root: Path, mutation: Mutation) -> None:
    """Apply one mutation only when its complete source anchor is unique."""
    target = root / mutation.source
    text = target.read_text(encoding="utf-8")
    occurrences = text.count(mutation.before)
    if occurrences != 1:
        raise MutationGateError(
            f"{mutation.name}: expected its exact source anchor once in "
            f"{mutation.source}, found {occurrences}"
        )
    target.write_text(text.replace(mutation.before, mutation.after, 1), encoding="utf-8")


def _copy_candidate(source_root: Path, destination: Path) -> None:
    """Copy only candidate package/test inputs, excluding generated state."""
    ignored = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", "*.pyo")
    for directory in ("src", "tests"):
        source = source_root / directory
        if not source.is_dir():
            raise MutationGateError(f"candidate is missing {directory}/")
        shutil.copytree(source, destination / directory, ignore=ignored)
    shutil.copy2(source_root / "pyproject.toml", destination / "pyproject.toml")


def _run_pytest(
    root: Path, nodes: Sequence[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONPATH"] = str(root / "src")
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short", *nodes],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )


def _all_test_nodes() -> tuple[str, ...]:
    return tuple(dict.fromkeys(node for mutation in MUTATIONS for node in mutation.tests))


def run_gate(root: Path = PROJECT_ROOT, *, timeout: float = 45.0) -> int:
    """Return zero only when the baseline passes and every mutant is killed."""
    validate_mutations(root)
    started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="pyinc-mutation-baseline-") as temporary:
        baseline_root = Path(temporary)
        _copy_candidate(root, baseline_root)
        try:
            baseline = _run_pytest(baseline_root, _all_test_nodes(), timeout)
        except subprocess.TimeoutExpired as error:
            raise MutationGateError(
                f"baseline targeted regressions exceeded {timeout:.1f}s"
            ) from error
        if baseline.returncode != 0:
            raise MutationGateError(
                "baseline targeted regressions must pass before mutation testing:\n"
                f"{baseline.stdout.rstrip()}"
            )
        print(f"baseline: passed {len(_all_test_nodes())} targeted regression nodes")

    survivors: list[str] = []
    for mutation in MUTATIONS:
        mutant_started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix=f"pyinc-mutant-{mutation.name}-") as temporary:
            mutant_root = Path(temporary)
            _copy_candidate(root, mutant_root)
            apply_mutation(mutant_root, mutation)
            try:
                result = _run_pytest(mutant_root, mutation.tests, timeout)
            except subprocess.TimeoutExpired as error:
                raise MutationGateError(
                    f"{mutation.name}: targeted regression exceeded {timeout:.1f}s; "
                    "a timeout is not accepted as a killed mutant"
                ) from error

        elapsed = time.monotonic() - mutant_started
        if result.returncode == 1:
            print(
                f"killed: {mutation.name} ({elapsed:.2f}s)\n"
                f"  seam: {mutation.seam}\n"
                f"  tests: {', '.join(mutation.tests)}"
            )
            continue
        if result.returncode == 0:
            survivors.append(mutation.name)
            print(
                f"SURVIVED: {mutation.name}\n"
                f"  seam: {mutation.seam}\n"
                f"  tests: {', '.join(mutation.tests)}\n"
                f"{result.stdout.rstrip()}",
                file=sys.stderr,
            )
            continue
        raise MutationGateError(
            f"{mutation.name}: pytest exited {result.returncode}, not the expected "
            f"test-failure code 1:\n{result.stdout.rstrip()}"
        )

    elapsed = time.monotonic() - started
    if survivors:
        print(f"mutation gate failed: {len(survivors)} survivor(s)", file=sys.stderr)
        return 1
    print(f"mutation gate passed: {len(MUTATIONS)} of {len(MUTATIONS)} killed in {elapsed:.2f}s")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-definitions",
        action="store_true",
        help="validate exact mutation anchors and pytest nodes without running tests",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=45.0,
        help="maximum seconds for each baseline or mutant pytest command (default: 45)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.timeout <= 0:
        print("mutation gate error: --timeout must be positive", file=sys.stderr)
        return 2
    try:
        if args.check_definitions:
            validate_mutations()
            print(f"mutation definitions valid: {len(MUTATIONS)} exact soundness seams")
            return 0
        return run_gate(timeout=args.timeout)
    except MutationGateError as error:
        print(f"mutation gate error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
