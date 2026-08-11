from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
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
    )
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from verify_signed_history import (  # noqa: E402
        SignedHistoryError,
        main,
        verify_signed_history,
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
