"""Verify every commit in a release range against the release signing key."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

_FULL_COMMIT_PATTERN = re.compile(r"\A[0-9a-f]{40}\Z")


class SignedHistoryError(ValueError):
    """The commit range does not satisfy the signed-history policy."""


@dataclass(frozen=True)
class CommitVerdict:
    """How one commit in the verified range satisfied the policy."""

    commit: str
    verified: bool
    allowlisted: bool


def _reject(message: str) -> NoReturn:
    raise SignedHistoryError(message)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        _reject(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout


def _signature_status(repository: Path, commit: str) -> str:
    completed = subprocess.run(
        ["git", "verify-commit", "--raw", commit],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout + completed.stderr


def _is_signed_by(status: str, expected_fingerprint: str) -> bool:
    for line in status.splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[0] != "[GNUPG:]" or fields[1] != "VALIDSIG":
            continue
        if expected_fingerprint in (fields[2], fields[-1]):
            return True
    return False


def _parent_commits(repository: Path, commit: str) -> tuple[str, ...]:
    fields = _git(repository, "rev-list", "--parents", "-n", "1", commit).split()
    return tuple(fields[1:])


def _tree_identifier(repository: Path, reference: str) -> str:
    return _git(repository, "rev-parse", f"{reference}^{{tree}}").strip()


def _require_structural_merge(
    repository: Path, commit: str, expected_fingerprint: str
) -> None:
    parents = _parent_commits(repository, commit)
    if len(parents) < 2:
        _reject(f"allowlisted commit {commit} is not a merge commit")
    for parent in parents:
        if not _is_signed_by(_signature_status(repository, parent), expected_fingerprint):
            _reject(
                f"allowlisted merge {commit} has parent {parent} that is not "
                f"signed by {expected_fingerprint}"
            )
    merge_tree = _tree_identifier(repository, commit)
    parent_trees = {_tree_identifier(repository, parent) for parent in parents}
    if merge_tree not in parent_trees:
        _reject(
            f"allowlisted merge {commit} has tree {merge_tree} that matches "
            f"no parent tree"
        )


def _require_full_commit(value: str, label: str) -> str:
    if not _FULL_COMMIT_PATTERN.match(value):
        _reject(f"{label} {value!r} is not a full 40-character commit id")
    return value


def verify_signed_history(
    repository: Path,
    baseline: str,
    head: str,
    expected_fingerprint: str,
    allowed_merge_commits: frozenset[str],
) -> tuple[CommitVerdict, ...]:
    """Verify baseline..head, returning a verdict per commit, oldest first."""

    _require_full_commit(baseline, "baseline")
    _require_full_commit(head, "head")
    for allowed in sorted(allowed_merge_commits):
        _require_full_commit(allowed, "allowlisted commit")
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", baseline, head],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    if ancestry.returncode != 0:
        _reject(f"baseline {baseline} is not an ancestor of head {head}")
    listing = _git(repository, "rev-list", "--reverse", f"{baseline}..{head}")
    verdicts: list[CommitVerdict] = []
    for commit in listing.split():
        status = _signature_status(repository, commit)
        if _is_signed_by(status, expected_fingerprint):
            verdicts.append(CommitVerdict(commit=commit, verified=True, allowlisted=False))
            continue
        if commit in allowed_merge_commits:
            _require_structural_merge(repository, commit, expected_fingerprint)
            verdicts.append(CommitVerdict(commit=commit, verified=False, allowlisted=True))
            continue
        summary = " / ".join(
            line for line in status.splitlines() if line.startswith("[GNUPG:]")
        )
        _reject(
            f"commit {commit} is not signed by {expected_fingerprint}: "
            f"{summary or 'no signature status reported'}"
        )
    return tuple(verdicts)


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path())
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--expected-fingerprint", required=True)
    parser.add_argument(
        "--allowed-merge-commit",
        action="append",
        default=[],
        dest="allowed_merge_commits",
        metavar="COMMIT",
    )
    return parser.parse_args(argv)


def _describe(verdicts: Iterable[CommitVerdict]) -> str:
    lines = []
    for verdict in verdicts:
        if verdict.allowlisted:
            lines.append(f"allowlisted structural merge {verdict.commit}")
        else:
            lines.append(f"verified {verdict.commit}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_arguments(argv)
    try:
        verdicts = verify_signed_history(
            repository=arguments.repository,
            baseline=arguments.baseline,
            head=arguments.head,
            expected_fingerprint=arguments.expected_fingerprint,
            allowed_merge_commits=frozenset(arguments.allowed_merge_commits),
        )
    except SignedHistoryError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    description = _describe(verdicts)
    if description:
        print(description)
    print(f"{len(verdicts)} commits satisfy the signed-history policy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
