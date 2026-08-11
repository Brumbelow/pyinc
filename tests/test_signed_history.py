from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from scripts.verify_signed_history import (
        SignedHistoryError,
        main,
        verify_signed_history,
        verify_signed_tag,
    )
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from verify_signed_history import (  # noqa: E402
        SignedHistoryError,
        main,
        verify_signed_history,
        verify_signed_tag,
    )

pytestmark = pytest.mark.skipif(
    shutil.which("gpg") is None, reason="gpg is not available"
)


@dataclass(frozen=True)
class SigningKeys:
    gnupghome: str
    release_fingerprint: str
    foreign_fingerprint: str


def _run(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        arguments,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _generate_key(gnupghome: str, user_id: str) -> str:
    base_env = {**os.environ, "GNUPGHOME": gnupghome}
    _run(
        [
            "gpg",
            "--batch",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--quick-generate-key",
            user_id,
            "ed25519",
            "sign",
            "never",
        ],
        env=base_env,
    )
    listing = _run(
        ["gpg", "--batch", "--with-colons", "--list-secret-keys", user_id],
        env=base_env,
    )
    for line in listing.splitlines():
        fields = line.split(":")
        if fields[0] == "fpr":
            return fields[9]
    raise AssertionError(f"no fingerprint listed for {user_id}")


@pytest.fixture(scope="session")
def signing_keys() -> Iterator[SigningKeys]:
    gnupghome = tempfile.mkdtemp(prefix="pyinc-gpg-")
    os.chmod(gnupghome, stat.S_IRWXU)
    try:
        try:
            release = _generate_key(gnupghome, "pyinc release <release@example.invalid>")
            foreign = _generate_key(gnupghome, "pyinc foreign <foreign@example.invalid>")
        except (subprocess.CalledProcessError, OSError) as error:
            pytest.skip(f"gpg cannot generate keys here: {error}")
        yield SigningKeys(
            gnupghome=gnupghome,
            release_fingerprint=release,
            foreign_fingerprint=foreign,
        )
    finally:
        subprocess.run(
            ["gpgconf", "--homedir", gnupghome, "--kill", "gpg-agent"],
            capture_output=True,
            check=False,
        )
        shutil.rmtree(gnupghome, ignore_errors=True)


def _repository_env(keys: SigningKeys) -> dict[str, str]:
    return {**os.environ, "GNUPGHOME": keys.gnupghome}


def _initialize_repository(path: Path, keys: SigningKeys, fingerprint: str) -> None:
    env = _repository_env(keys)
    _run(["git", "init", "--quiet", "-b", "main", str(path)], env=env)
    for name, value in (
        ("user.name", "pyinc fixture"),
        ("user.email", "fixture@example.invalid"),
        ("user.signingkey", fingerprint),
        ("commit.gpgsign", "true"),
    ):
        _run(["git", "config", name, value], cwd=path, env=env)


def _commit(
    path: Path,
    keys: SigningKeys,
    message: str,
    *,
    fingerprint: str | None = None,
    sign: bool = True,
) -> str:
    env = _repository_env(keys)
    if fingerprint is not None:
        _run(["git", "config", "user.signingkey", fingerprint], cwd=path, env=env)
    _run(["git", "config", "commit.gpgsign", "true" if sign else "false"], cwd=path, env=env)
    _run(["git", "commit", "--quiet", "--allow-empty", "-m", message], cwd=path, env=env)
    return _run(["git", "rev-parse", "HEAD"], cwd=path, env=env).strip()


def _tag(
    path: Path,
    keys: SigningKeys,
    name: str,
    *,
    fingerprint: str | None = None,
    sign: bool = True,
) -> str:
    """Record an annotated tag, signed by the chosen key unless sign is false."""

    env = _repository_env(keys)
    if fingerprint is not None:
        _run(["git", "config", "user.signingkey", fingerprint], cwd=path, env=env)
    _run(["git", "config", "tag.gpgsign", "true" if sign else "false"], cwd=path, env=env)
    _run(
        ["git", "tag", "-s" if sign else "-a", "-m", f"Release {name}", name],
        cwd=path,
        env=env,
    )
    return name


def _expire_key(keys: SigningKeys, fingerprint: str) -> None:
    """Expire a fixture key, waiting until gpg itself reports it as expired."""

    base_env = {**os.environ, "GNUPGHOME": keys.gnupghome}
    _run(
        [
            "gpg",
            "--batch",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--quick-set-expire",
            fingerprint,
            "seconds=1",
        ],
        env=base_env,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        listing = _run(
            ["gpg", "--batch", "--with-colons", "--list-keys", fingerprint],
            env=base_env,
        )
        validity = next(
            (line.split(":")[1] for line in listing.splitlines() if line.startswith("pub")),
            "",
        )
        if validity == "e":
            return
        time.sleep(0.2)
    raise AssertionError(f"gpg never reported {fingerprint} as expired")


def _merge_of_parents(
    repository: Path,
    keys: SigningKeys,
    *,
    first_parent: str,
    second_parent: str,
    sign_fingerprint: str,
) -> str:
    """Record a merge with chosen parents, keeping the first parent's tree."""

    env = _repository_env(keys)
    tree = _run(
        ["git", "rev-parse", f"{first_parent}^{{tree}}"], cwd=repository, env=env
    ).strip()
    merge = _run(
        [
            "git",
            "commit-tree",
            tree,
            "-p",
            first_parent,
            "-p",
            second_parent,
            "-m",
            "Merge recorded for the fixture",
            f"-S{sign_fingerprint}",
        ],
        cwd=repository,
        env=env,
    ).strip()
    _run(["git", "update-ref", "refs/heads/main", merge], cwd=repository, env=env)
    return merge


@pytest.fixture()
def repository(
    tmp_path: Path, signing_keys: SigningKeys, monkeypatch: pytest.MonkeyPatch
) -> Path:
    path = tmp_path / "history"
    path.mkdir()
    _initialize_repository(path, signing_keys, signing_keys.release_fingerprint)
    monkeypatch.setenv("GNUPGHOME", signing_keys.gnupghome)
    return path


def test_accepts_range_where_every_commit_uses_the_release_key(
    repository: Path, signing_keys: SigningKeys
) -> None:
    baseline = _commit(repository, signing_keys, "Baseline")
    _commit(repository, signing_keys, "First change")
    head = _commit(repository, signing_keys, "Second change")
    verdicts = verify_signed_history(
        repository=repository,
        baseline=baseline,
        head=head,
        expected_fingerprint=signing_keys.release_fingerprint,
        allowed_merge_commits=frozenset(),
    )
    assert [verdict.verified for verdict in verdicts] == [True, True]
    assert all(not verdict.allowlisted for verdict in verdicts)


def test_rejects_commit_signed_by_a_foreign_key(
    repository: Path, signing_keys: SigningKeys
) -> None:
    baseline = _commit(repository, signing_keys, "Baseline")
    foreign = _commit(
        repository,
        signing_keys,
        "Foreign change",
        fingerprint=signing_keys.foreign_fingerprint,
    )
    head = _commit(
        repository,
        signing_keys,
        "Signed change",
        fingerprint=signing_keys.release_fingerprint,
    )
    with pytest.raises(SignedHistoryError, match=foreign):
        verify_signed_history(
            repository=repository,
            baseline=baseline,
            head=head,
            expected_fingerprint=signing_keys.release_fingerprint,
            allowed_merge_commits=frozenset(),
        )


def _merge_from_branch(
    repository: Path,
    keys: SigningKeys,
    *,
    merge_fingerprint: str,
    branch_file: str = "feature.txt",
) -> tuple[str, str]:
    """Create base -> feature branch -> merge; return (baseline, merge commit)."""

    env = _repository_env(keys)
    baseline = _commit(repository, keys, "Baseline")
    _run(["git", "checkout", "--quiet", "-b", "feature"], cwd=repository, env=env)
    (repository / branch_file).write_text("feature content\n", encoding="utf-8")
    _run(["git", "add", branch_file], cwd=repository, env=env)
    feature = _commit(repository, keys, "Feature change")
    _run(["git", "checkout", "--quiet", "main"], cwd=repository, env=env)
    _run(["git", "config", "user.signingkey", merge_fingerprint], cwd=repository, env=env)
    _run(
        ["git", "merge", "--quiet", "--no-ff", "--no-edit", "feature"],
        cwd=repository,
        env=env,
    )
    _run(
        ["git", "config", "user.signingkey", keys.release_fingerprint],
        cwd=repository,
        env=env,
    )
    merge = _run(["git", "rev-parse", "HEAD"], cwd=repository, env=env).strip()
    assert feature
    return baseline, merge


def test_rejects_unsigned_commit(repository: Path, signing_keys: SigningKeys) -> None:
    baseline = _commit(repository, signing_keys, "Baseline")
    unsigned = _commit(repository, signing_keys, "Unsigned change", sign=False)
    head = _commit(repository, signing_keys, "Signed change")
    assert head
    with pytest.raises(SignedHistoryError, match=unsigned):
        verify_signed_history(
            repository=repository,
            baseline=baseline,
            head=head,
            expected_fingerprint=signing_keys.release_fingerprint,
            allowed_merge_commits=frozenset(),
        )


def test_allowlisted_structural_merge_is_accepted(
    repository: Path, signing_keys: SigningKeys
) -> None:
    baseline, merge = _merge_from_branch(
        repository,
        signing_keys,
        merge_fingerprint=signing_keys.foreign_fingerprint,
    )
    verdicts = verify_signed_history(
        repository=repository,
        baseline=baseline,
        head=merge,
        expected_fingerprint=signing_keys.release_fingerprint,
        allowed_merge_commits=frozenset({merge}),
    )
    assert verdicts[-1].allowlisted
    assert not verdicts[-1].verified


def test_non_allowlisted_merge_is_still_rejected(
    repository: Path, signing_keys: SigningKeys
) -> None:
    baseline, merge = _merge_from_branch(
        repository,
        signing_keys,
        merge_fingerprint=signing_keys.foreign_fingerprint,
    )
    with pytest.raises(SignedHistoryError, match=merge):
        verify_signed_history(
            repository=repository,
            baseline=baseline,
            head=merge,
            expected_fingerprint=signing_keys.release_fingerprint,
            allowed_merge_commits=frozenset(),
        )


def test_allowlisted_merge_requires_all_parents_signed(
    repository: Path, signing_keys: SigningKeys
) -> None:
    env = _repository_env(signing_keys)
    # The unsigned parent is the baseline itself, so it sits outside the verified
    # range: only the parent check can catch it.
    baseline = _commit(repository, signing_keys, "Unsigned baseline", sign=False)
    _run(["git", "checkout", "--quiet", "-b", "feature"], cwd=repository, env=env)
    (repository / "feature.txt").write_text("feature content\n", encoding="utf-8")
    _run(["git", "add", "feature.txt"], cwd=repository, env=env)
    _commit(repository, signing_keys, "Feature change")
    _run(["git", "checkout", "--quiet", "main"], cwd=repository, env=env)
    _run(
        ["git", "config", "user.signingkey", signing_keys.foreign_fingerprint],
        cwd=repository,
        env=env,
    )
    _run(
        ["git", "merge", "--quiet", "--no-ff", "--no-edit", "feature"],
        cwd=repository,
        env=env,
    )
    merge = _run(["git", "rev-parse", "HEAD"], cwd=repository, env=env).strip()
    with pytest.raises(SignedHistoryError, match=f"parent {baseline} that is not signed"):
        verify_signed_history(
            repository=repository,
            baseline=baseline,
            head=merge,
            expected_fingerprint=signing_keys.release_fingerprint,
            allowed_merge_commits=frozenset({merge}),
        )


def test_allowlisted_merge_requires_tree_equal_to_a_parent(
    repository: Path, signing_keys: SigningKeys
) -> None:
    env = _repository_env(signing_keys)
    baseline = _commit(repository, signing_keys, "Baseline")
    _run(["git", "checkout", "--quiet", "-b", "feature"], cwd=repository, env=env)
    (repository / "feature.txt").write_text("feature content\n", encoding="utf-8")
    _run(["git", "add", "feature.txt"], cwd=repository, env=env)
    _commit(repository, signing_keys, "Feature change")
    _run(["git", "checkout", "--quiet", "main"], cwd=repository, env=env)
    (repository / "mainline.txt").write_text("mainline content\n", encoding="utf-8")
    _run(["git", "add", "mainline.txt"], cwd=repository, env=env)
    _commit(repository, signing_keys, "Mainline change")
    # The merge carries a foreign signature, as a GitHub-created merge does, so it
    # reaches the allowlist path instead of verifying on its own signature.
    _run(
        ["git", "config", "user.signingkey", signing_keys.foreign_fingerprint],
        cwd=repository,
        env=env,
    )
    _run(
        ["git", "merge", "--quiet", "--no-ff", "--no-edit", "feature"],
        cwd=repository,
        env=env,
    )
    merge = _run(["git", "rev-parse", "HEAD"], cwd=repository, env=env).strip()
    with pytest.raises(SignedHistoryError, match="matches no parent tree"):
        verify_signed_history(
            repository=repository,
            baseline=baseline,
            head=merge,
            expected_fingerprint=signing_keys.release_fingerprint,
            allowed_merge_commits=frozenset({merge}),
        )


def test_rejects_commit_signed_by_an_expired_key(
    repository: Path, signing_keys: SigningKeys
) -> None:
    expiring = _generate_key(
        signing_keys.gnupghome, "pyinc expiring <expiring@example.invalid>"
    )
    baseline = _commit(repository, signing_keys, "Baseline")
    head = _commit(
        repository, signing_keys, "Signed before expiry", fingerprint=expiring
    )
    # gpg keeps reporting VALIDSIG for this signature once the key expires; only the
    # EXPKEYSIG status distinguishes it from a good one.
    _expire_key(signing_keys, expiring)
    status = subprocess.run(
        ["git", "verify-commit", "--raw", head],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    assert f"VALIDSIG {expiring}" in status.stdout + status.stderr
    with pytest.raises(
        SignedHistoryError,
        match=f"commit {head} is signed by {expiring} but the signature "
        "was made by an expired key",
    ):
        verify_signed_history(
            repository=repository,
            baseline=baseline,
            head=head,
            expected_fingerprint=expiring,
            allowed_merge_commits=frozenset(),
        )


def test_allowlisted_merge_rejects_a_parent_signed_by_an_expired_key(
    repository: Path, signing_keys: SigningKeys
) -> None:
    expiring = _generate_key(
        signing_keys.gnupghome, "pyinc expiring parent <parent@example.invalid>"
    )
    ancestor = _commit(repository, signing_keys, "Ancestor", fingerprint=expiring)
    baseline = _commit(repository, signing_keys, "Baseline", fingerprint=expiring)
    # Both parents sit outside baseline..merge, so only the parent check can refuse
    # them once their signing key expires.
    merge = _merge_of_parents(
        repository,
        signing_keys,
        first_parent=baseline,
        second_parent=ancestor,
        sign_fingerprint=signing_keys.foreign_fingerprint,
    )
    _expire_key(signing_keys, expiring)
    with pytest.raises(SignedHistoryError, match="whose signature was made by an expired key"):
        verify_signed_history(
            repository=repository,
            baseline=baseline,
            head=merge,
            expected_fingerprint=expiring,
            allowed_merge_commits=frozenset({merge}),
        )


def test_allowlisted_merge_checks_parents_beyond_the_first(
    repository: Path, signing_keys: SigningKeys
) -> None:
    unsigned = _commit(repository, signing_keys, "Unsigned ancestor", sign=False)
    baseline = _commit(repository, signing_keys, "Baseline")
    first_parent = _commit(repository, signing_keys, "Mainline change")
    # The unsigned commit predates the baseline, so the range walk never sees it; it
    # is reachable only as the merge's SECOND parent.
    merge = _merge_of_parents(
        repository,
        signing_keys,
        first_parent=first_parent,
        second_parent=unsigned,
        sign_fingerprint=signing_keys.foreign_fingerprint,
    )
    with pytest.raises(SignedHistoryError, match=f"parent {unsigned} that is not signed"):
        verify_signed_history(
            repository=repository,
            baseline=baseline,
            head=merge,
            expected_fingerprint=signing_keys.release_fingerprint,
            allowed_merge_commits=frozenset({merge}),
        )


def test_allowlisting_cannot_launder_an_ordinary_commit(
    repository: Path, signing_keys: SigningKeys
) -> None:
    baseline = _commit(repository, signing_keys, "Baseline")
    unsigned = _commit(repository, signing_keys, "Unsigned change", sign=False)
    with pytest.raises(SignedHistoryError, match="is not a merge commit"):
        verify_signed_history(
            repository=repository,
            baseline=baseline,
            head=unsigned,
            expected_fingerprint=signing_keys.release_fingerprint,
            allowed_merge_commits=frozenset({unsigned}),
        )


def test_rejects_baseline_that_is_not_an_ancestor(
    repository: Path, signing_keys: SigningKeys
) -> None:
    first = _commit(repository, signing_keys, "First")
    second = _commit(repository, signing_keys, "Second")
    with pytest.raises(SignedHistoryError, match="ancestor"):
        verify_signed_history(
            repository=repository,
            baseline=second,
            head=first,
            expected_fingerprint=signing_keys.release_fingerprint,
            allowed_merge_commits=frozenset(),
        )


def test_rejects_malformed_commit_identifiers(
    repository: Path, signing_keys: SigningKeys
) -> None:
    head = _commit(repository, signing_keys, "Only commit")
    with pytest.raises(SignedHistoryError, match="full 40-character"):
        verify_signed_history(
            repository=repository,
            baseline="main",
            head=head,
            expected_fingerprint=signing_keys.release_fingerprint,
            allowed_merge_commits=frozenset(),
        )
    with pytest.raises(SignedHistoryError, match="full 40-character"):
        verify_signed_history(
            repository=repository,
            baseline=head,
            head=head,
            expected_fingerprint=signing_keys.release_fingerprint,
            allowed_merge_commits=frozenset({"HEAD"}),
        )


def test_verifies_a_tag_signed_by_the_release_key(
    repository: Path, signing_keys: SigningKeys
) -> None:
    _commit(repository, signing_keys, "Baseline")
    tag = _tag(repository, signing_keys, "v9.0.0")
    verify_signed_tag(
        repository=repository,
        tag=tag,
        expected_fingerprint=signing_keys.release_fingerprint,
    )


def test_rejects_a_tag_signed_by_a_foreign_key(
    repository: Path, signing_keys: SigningKeys
) -> None:
    _commit(repository, signing_keys, "Baseline")
    tag = _tag(
        repository,
        signing_keys,
        "v9-0-1",
        fingerprint=signing_keys.foreign_fingerprint,
    )
    with pytest.raises(SignedHistoryError, match=f"tag {tag} is not signed by"):
        verify_signed_tag(
            repository=repository,
            tag=tag,
            expected_fingerprint=signing_keys.release_fingerprint,
        )


def test_rejects_an_unsigned_annotated_tag(
    repository: Path, signing_keys: SigningKeys
) -> None:
    _commit(repository, signing_keys, "Baseline")
    tag = _tag(repository, signing_keys, "v9-0-2", sign=False)
    assert _run(["git", "cat-file", "-t", tag], cwd=repository).strip() == "tag"
    with pytest.raises(SignedHistoryError, match=f"tag {tag} is not signed by"):
        verify_signed_tag(
            repository=repository,
            tag=tag,
            expected_fingerprint=signing_keys.release_fingerprint,
        )


def test_rejects_a_tag_whose_signing_key_expired(
    repository: Path, signing_keys: SigningKeys
) -> None:
    expiring = _generate_key(
        signing_keys.gnupghome, "pyinc expiring tag <expiring-tag@example.invalid>"
    )
    _commit(repository, signing_keys, "Baseline")
    tag = _tag(repository, signing_keys, "v9-0-3", fingerprint=expiring)
    # As with commits, gpg keeps reporting VALIDSIG once the key expires, so only
    # the EXPKEYSIG status separates this tag from a good one.
    _expire_key(signing_keys, expiring)
    status = subprocess.run(
        ["git", "verify-tag", "--raw", tag],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    assert f"VALIDSIG {expiring}" in status.stdout + status.stderr
    with pytest.raises(
        SignedHistoryError,
        match=f"tag {tag} is signed by {expiring} but the signature "
        "was made by an expired key",
    ):
        verify_signed_tag(
            repository=repository, tag=tag, expected_fingerprint=expiring
        )


def test_cli_reports_success(
    repository: Path,
    signing_keys: SigningKeys,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline = _commit(repository, signing_keys, "Baseline")
    head = _commit(repository, signing_keys, "Change")
    exit_code = main(
        [
            "--repository",
            str(repository),
            "--baseline",
            baseline,
            "--head",
            head,
            "--expected-fingerprint",
            signing_keys.release_fingerprint,
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"verified {head}" in captured.out
    assert "1 commits satisfy the signed-history policy" in captured.out


def test_cli_accepts_an_allowlisted_structural_merge(
    repository: Path,
    signing_keys: SigningKeys,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline, merge = _merge_from_branch(
        repository,
        signing_keys,
        merge_fingerprint=signing_keys.foreign_fingerprint,
    )
    exit_code = main(
        [
            "--repository",
            str(repository),
            "--baseline",
            baseline,
            "--head",
            merge,
            "--expected-fingerprint",
            signing_keys.release_fingerprint,
            "--allowed-merge-commit",
            merge,
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"allowlisted structural merge {merge}" in captured.out
    assert "2 commits satisfy the signed-history policy" in captured.out


def test_cli_verifies_a_named_tag(
    repository: Path,
    signing_keys: SigningKeys,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline = _commit(repository, signing_keys, "Baseline")
    head = _commit(repository, signing_keys, "Change")
    tag = _tag(repository, signing_keys, "v9-1-0")
    exit_code = main(
        [
            "--repository",
            str(repository),
            "--baseline",
            baseline,
            "--head",
            head,
            "--expected-fingerprint",
            signing_keys.release_fingerprint,
            "--tag",
            tag,
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"verified tag {tag}" in captured.out
    assert "1 commits satisfy the signed-history policy" in captured.out


def test_cli_fails_on_a_tag_signed_by_a_foreign_key(
    repository: Path,
    signing_keys: SigningKeys,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline = _commit(repository, signing_keys, "Baseline")
    head = _commit(
        repository,
        signing_keys,
        "Change",
        fingerprint=signing_keys.release_fingerprint,
    )
    tag = _tag(
        repository,
        signing_keys,
        "v9-1-1",
        fingerprint=signing_keys.foreign_fingerprint,
    )
    exit_code = main(
        [
            "--repository",
            str(repository),
            "--baseline",
            baseline,
            "--head",
            head,
            "--expected-fingerprint",
            signing_keys.release_fingerprint,
            "--tag",
            tag,
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert f"tag {tag} is not signed by" in captured.err
    # The tag is checked before the range walk, so no commit line is printed.
    assert "satisfy the signed-history policy" not in captured.out


def test_cli_reports_a_missing_repository_without_a_traceback(
    tmp_path: Path,
    signing_keys: SigningKeys,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--repository",
            str(tmp_path / "absent"),
            "--baseline",
            "0" * 40,
            "--head",
            "1" * 40,
            "--expected-fingerprint",
            signing_keys.release_fingerprint,
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err.startswith("error:")


def test_cli_reports_failure_on_stderr(
    repository: Path,
    signing_keys: SigningKeys,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline = _commit(repository, signing_keys, "Baseline")
    head = _commit(
        repository,
        signing_keys,
        "Foreign change",
        fingerprint=signing_keys.foreign_fingerprint,
    )
    exit_code = main(
        [
            "--repository",
            str(repository),
            "--baseline",
            baseline,
            "--head",
            head,
            "--expected-fingerprint",
            signing_keys.release_fingerprint,
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error:" in captured.err
    assert head in captured.err


_REPO_ROOT = Path(__file__).resolve().parent.parent


def _workflow_text(name: str) -> str:
    return (_REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def _workflow_value(text: str, key: str) -> str:
    matches: list[str] = re.findall(rf"(?m)^\s*{key}:\s*([0-9A-Fa-f]{{40}})\s*$", text)
    assert matches, f"{key} with a 40-hex value is missing"
    assert len(set(matches)) == 1, f"{key} carries conflicting values"
    return matches[0]


def test_release_workflow_pins_full_commit_identifiers() -> None:
    text = _workflow_text("release.yml")
    fingerprint = _workflow_value(text, "EXPECTED_FINGERPRINT")
    baseline = _workflow_value(text, "TRUSTED_BASELINE")
    allowed = _workflow_value(text, "ALLOWED_MERGE_COMMIT")
    assert fingerprint == fingerprint.upper()
    assert baseline == baseline.lower()
    assert allowed == allowed.lower()


def test_release_workflow_fingerprint_matches_the_shipped_key() -> None:
    text = _workflow_text("release.yml")
    fingerprint = _workflow_value(text, "EXPECTED_FINGERPRINT")
    listing = _run(
        [
            "gpg",
            "--batch",
            "--with-colons",
            "--show-keys",
            str(_REPO_ROOT / ".github" / "release-signing-key.asc"),
        ]
    )
    key_fingerprints = [
        line.split(":")[9]
        for line in listing.splitlines()
        if line.split(":")[0] == "fpr"
    ]
    assert fingerprint in key_fingerprints
    primary_count = sum(
        1 for line in listing.splitlines() if line.split(":")[0] == "pub"
    )
    assert primary_count == 1


def test_release_workflow_calls_the_extracted_verifier() -> None:
    text = _workflow_text("release.yml")
    assert "scripts/verify_signed_history.py" in text
    assert "--tag" in text
    assert "--allowed-merge-commit" in text
    # The release-candidate tag is verified by the script too, not by shell.
    assert '--tag "$rc_tag"' in text
    assert "while IFS= read -r commit" not in text
    assert "verify_expected_signature" not in text
