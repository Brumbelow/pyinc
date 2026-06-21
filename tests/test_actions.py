from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import pyinc
from pyinc import Database, query
from pyinc.actions import (
    ActionIdentity,
    ActionLockError,
    ActionManifest,
    ActionStateError,
    DesiredArtifact,
    DesiredArtifactSet,
    DuplicateArtifactError,
    FilesystemReconciler,
    InvalidArtifactPathError,
    SymlinkEscapeError,
    ToolIdentity,
    default_state_dir,
)

_TOOL = ToolIdentity(name="test-tool", version="1.0.0", schema_version=1)


def _make_set(
    output_root: Path,
    files: dict[str, bytes],
    *,
    action_id: str = "test-action",
) -> DesiredArtifactSet:
    artifacts = tuple(DesiredArtifact(path, content) for path, content in files.items())
    identity = ActionIdentity(action_id=action_id, output_root=str(output_root), tool=_TOOL)
    return DesiredArtifactSet(identity, artifacts)


def _reconciler(tmp_path: Path) -> FilesystemReconciler:
    return FilesystemReconciler(tmp_path / "out", state_dir=tmp_path / "state")


def _tree(root: Path) -> dict[str, bytes]:
    """Map of relative-path -> bytes for every regular file under ``root``."""
    return {
        str(p.relative_to(root)).replace(os.sep, "/"): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# ---------------------------------------------------------------------------
# Core reconciliation behavior
# ---------------------------------------------------------------------------


def test_initial_creation(tmp_path: Path) -> None:
    out = tmp_path / "out"
    rec = _reconciler(tmp_path)
    result = rec.apply(_make_set(out, {"a.txt": b"A", "sub/b.txt": b"B"}))
    assert result.writes == ("a.txt", "sub/b.txt")
    assert result.deletions == ()
    assert result.unchanged == 0
    assert _tree(out) == {"a.txt": b"A", "sub/b.txt": b"B"}


def test_identical_rerun_zero_writes_and_stable_mtime(tmp_path: Path) -> None:
    out = tmp_path / "out"
    rec = _reconciler(tmp_path)
    desired = _make_set(out, {"a.txt": b"A", "sub/b.txt": b"B"})
    rec.apply(desired)
    mtimes = {p: (out / p).stat().st_mtime_ns for p in ("a.txt", "sub/b.txt")}

    plan = rec.plan(desired)
    assert plan.is_noop
    assert plan.unchanged == ("a.txt", "sub/b.txt")

    result = rec.apply(desired)
    assert result.writes == ()
    assert result.unchanged == 2
    for p, mtime in mtimes.items():
        assert (out / p).stat().st_mtime_ns == mtime, f"{p} mtime changed"


def test_one_changed_input_rewrites_only_affected(tmp_path: Path) -> None:
    out = tmp_path / "out"
    rec = _reconciler(tmp_path)
    rec.apply(_make_set(out, {"a.txt": b"A", "b.txt": b"B"}))
    b_mtime = (out / "b.txt").stat().st_mtime_ns

    result = rec.apply(_make_set(out, {"a.txt": b"A2", "b.txt": b"B"}))
    assert result.writes == ("a.txt",)
    assert (out / "a.txt").read_bytes() == b"A2"
    assert (out / "b.txt").stat().st_mtime_ns == b_mtime


def test_externally_tampered_output_is_repaired(tmp_path: Path) -> None:
    out = tmp_path / "out"
    rec = _reconciler(tmp_path)
    desired = _make_set(out, {"a.txt": b"A", "b.txt": b"B"})
    rec.apply(desired)

    # Corrupt an owned output with the *same* declared inputs unchanged.
    (out / "a.txt").write_bytes(b"CORRUPTED")
    plan = rec.plan(desired)
    assert plan.updates == ("a.txt",)
    result = rec.apply(desired)
    assert result.writes == ("a.txt",)
    assert (out / "a.txt").read_bytes() == b"A"


def test_removed_declaration_deletes_only_owned_stale(tmp_path: Path) -> None:
    out = tmp_path / "out"
    rec = _reconciler(tmp_path)
    rec.apply(_make_set(out, {"a.txt": b"A", "stale.txt": b"S"}))

    result = rec.apply(_make_set(out, {"a.txt": b"A"}))
    assert result.deletions == ("stale.txt",)
    assert not (out / "stale.txt").exists()
    assert (out / "a.txt").read_bytes() == b"A"


def test_foreign_files_are_preserved(tmp_path: Path) -> None:
    out = tmp_path / "out"
    rec = _reconciler(tmp_path)
    rec.apply(_make_set(out, {"a.txt": b"A", "stale.txt": b"S"}))

    # A file the action never owned.
    (out / "foreign.txt").write_bytes(b"FOREIGN")
    result = rec.apply(_make_set(out, {"a.txt": b"A"}))
    assert result.deletions == ("stale.txt",)
    assert (out / "foreign.txt").read_bytes() == b"FOREIGN"


def test_dry_run_plan_changes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "out"
    rec = _reconciler(tmp_path)
    rec.apply(_make_set(out, {"a.txt": b"A", "stale.txt": b"S"}))
    before = _tree(out)
    state_before = sorted((tmp_path / "state").rglob("*"))

    plan = rec.plan(_make_set(out, {"a.txt": b"A2", "new.txt": b"N"}))
    assert plan.creates == ("new.txt",)
    assert plan.updates == ("a.txt",)
    assert plan.deletes == ("stale.txt",)
    assert plan.unchanged == ()
    # Nothing on disk changed.
    assert _tree(out) == before
    assert sorted((tmp_path / "state").rglob("*")) == state_before


def test_state_lives_outside_output_root(tmp_path: Path) -> None:
    out = tmp_path / "out"
    rec = _reconciler(tmp_path)
    rec.apply(_make_set(out, {"a.txt": b"A"}))
    # No manifest/lock bookkeeping leaked into the output tree.
    assert set(_tree(out)) == {"a.txt"}
    assert (tmp_path / "state" / "manifest.json").exists()


# ---------------------------------------------------------------------------
# Path safety / validation
# ---------------------------------------------------------------------------


def test_duplicate_output_claims_fail(tmp_path: Path) -> None:
    out = tmp_path / "out"
    identity = ActionIdentity(action_id="dup", output_root=str(out), tool=_TOOL)
    with pytest.raises(DuplicateArtifactError):
        DesiredArtifactSet(
            identity,
            (DesiredArtifact("a/b.txt", b"1"), DesiredArtifact("a/./b.txt", b"2")),
        )


@pytest.mark.parametrize("bad", ["/abs/path", "../escape", "", "   ", "a/../../b"])
def test_unsafe_paths_rejected(bad: str) -> None:
    with pytest.raises(InvalidArtifactPathError):
        DesiredArtifact(bad, b"x")


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # An attacker plants a directory symlink inside the output root.
    (out / "link").symlink_to(outside, target_is_directory=True)
    rec = _reconciler(tmp_path)
    with pytest.raises(SymlinkEscapeError):
        rec.apply(_make_set(out, {"link/evil.txt": b"PWNED"}))
    assert not (outside / "evil.txt").exists()


# ---------------------------------------------------------------------------
# Failure / atomicity semantics
# ---------------------------------------------------------------------------


def _no_temp_files(root: Path) -> bool:
    return not any(p.name.startswith(".pyinc-tmp-") for p in root.rglob("*"))


def test_injected_write_failure_cleans_temps_and_publishes_no_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    rec = _reconciler(tmp_path)

    real_replace = os.replace

    def failing_replace(src: object, dst: object) -> None:
        if str(dst).endswith("b.txt"):
            raise OSError("injected staging failure")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError, match="injected"):
        rec.apply(_make_set(out, {"a.txt": b"A", "b.txt": b"B"}))

    # No successful manifest published, no leftover temp files.
    assert not (tmp_path / "state" / "manifest.json").exists()
    assert _no_temp_files(out)

    # A subsequent clean run converges to the full desired set.
    monkeypatch.undo()
    result = rec.apply(_make_set(out, {"a.txt": b"A", "b.txt": b"B"}))
    assert (out / "b.txt").read_bytes() == b"B"
    assert "b.txt" in result.writes
    assert _tree(out) == {"a.txt": b"A", "b.txt": b"B"}


def test_failure_does_not_prematurely_delete_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    rec = _reconciler(tmp_path)
    rec.apply(_make_set(out, {"a.txt": b"A", "stale.txt": b"S"}))

    real_replace = os.replace

    def failing_replace(src: object, dst: object) -> None:
        if str(dst).endswith("new.txt"):
            raise OSError("injected")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", failing_replace)
    # Desired drops stale.txt (would be deleted) and adds new.txt (write fails).
    with pytest.raises(OSError, match="injected"):
        rec.apply(_make_set(out, {"a.txt": b"A", "new.txt": b"N"}))

    # The stale file must survive: deletion happens only after all writes succeed.
    assert (out / "stale.txt").read_bytes() == b"S"
    # Manifest still records the previous owned set.
    manifest = ActionManifest.from_json_bytes((tmp_path / "state" / "manifest.json").read_bytes())
    assert manifest.owned_paths == {"a.txt", "stale.txt"}


def test_deterministic_manifest_bytes(tmp_path: Path) -> None:
    out = tmp_path / "out"
    # Same logical content, different insertion order -> identical manifest bytes.
    rec_a = FilesystemReconciler(out, state_dir=tmp_path / "sa")
    rec_a.apply(_make_set(out, {"a.txt": b"A", "b.txt": b"B", "c.txt": b"C"}))
    rec_b = FilesystemReconciler(out, state_dir=tmp_path / "sb")
    rec_b.apply(_make_set(out, {"c.txt": b"C", "a.txt": b"A", "b.txt": b"B"}))
    assert (tmp_path / "sa" / "manifest.json").read_bytes() == (
        tmp_path / "sb" / "manifest.json"
    ).read_bytes()


# ---------------------------------------------------------------------------
# Side-effect isolation + concurrency
# ---------------------------------------------------------------------------


def test_apply_rejected_inside_active_query(tmp_path: Path) -> None:
    out = tmp_path / "out"
    state = tmp_path / "state"
    desired = _make_set(out, {"a.txt": b"A"})

    @query
    def offending(db: Database) -> str:
        assert pyinc.is_query_active()
        rec = FilesystemReconciler(out, state_dir=state)
        try:
            rec.apply(desired)
        except ActionStateError:
            return "rejected"
        return "applied"

    assert not pyinc.is_query_active()
    assert Database().get(offending) == "rejected"
    assert not out.exists()


def test_concurrent_writer_is_rejected(tmp_path: Path) -> None:
    out = tmp_path / "out"
    state = tmp_path / "state"
    rec = FilesystemReconciler(out, state_dir=state)
    # Simulate another live writer by planting the lock file.
    state.mkdir(parents=True)
    (state / "lock").write_text("pid=99999\n")
    with pytest.raises(ActionLockError):
        rec.apply(_make_set(out, {"a.txt": b"A"}))
    # Removing the stale lock lets a fresh run proceed.
    (state / "lock").unlink()
    rec.apply(_make_set(out, {"a.txt": b"A"}))
    assert (out / "a.txt").read_bytes() == b"A"
    assert not (state / "lock").exists()  # lock released after success


def test_default_state_dir_is_outside_output_root() -> None:
    state = default_state_dir("/repo/generated", "graphql:client")
    assert state == Path("/repo/.pyinc-actions/graphql_client")


# ---------------------------------------------------------------------------
# From-scratch consistency over an edit sequence (property-based)
# ---------------------------------------------------------------------------

_PATHS = ["x.txt", "y.txt", "pkg/z.txt"]
Operation = tuple[str, str, int]


def _operations() -> st.SearchStrategy[list[Operation]]:
    op = st.tuples(
        st.sampled_from(["set", "remove"]),
        st.sampled_from(_PATHS),
        st.integers(min_value=0, max_value=5),
    )
    return st.lists(op, min_size=1, max_size=25)


@settings(max_examples=60, deadline=None)
@given(steps=_operations())
def test_incremental_reconciliation_matches_from_scratch(
    tmp_path_factory: pytest.TempPathFactory, steps: list[Operation]
) -> None:
    base = tmp_path_factory.mktemp("seq")
    inc_out = base / "inc_out"
    inc = FilesystemReconciler(inc_out, state_dir=base / "inc_state")

    state: dict[str, bytes] = {}
    for i, (kind, path, value) in enumerate(steps):
        if kind == "set":
            state[path] = f"v{value}".encode()
        else:
            state.pop(path, None)

        identity = ActionIdentity(action_id="seq", output_root=str(inc_out), tool=_TOOL)
        desired = DesiredArtifactSet(
            identity, tuple(DesiredArtifact(p, c) for p, c in state.items())
        )
        inc.apply(desired)

        # From-scratch: a clean output + state reconciled once to the same set.
        fresh_out = base / f"fresh_{i}"
        fresh = FilesystemReconciler(fresh_out, state_dir=base / f"fresh_state_{i}")
        fresh_identity = ActionIdentity(
            action_id="seq", output_root=str(fresh_out), tool=_TOOL
        )
        fresh.apply(
            DesiredArtifactSet(
                fresh_identity, tuple(DesiredArtifact(p, c) for p, c in state.items())
            )
        )

        assert _tree(inc_out) == _tree(fresh_out) == state
        inc_manifest = ActionManifest.from_json_bytes(
            (base / "inc_state" / "manifest.json").read_bytes()
        )
        assert inc_manifest.owned_paths == set(state)


# ---------------------------------------------------------------------------
# Contract lock
# ---------------------------------------------------------------------------


def test_actions_all_is_exact() -> None:
    from pyinc import actions

    assert set(actions.__all__) == {
        "MANIFEST_VERSION",
        "ActionError",
        "ActionIdentity",
        "ActionLockError",
        "ActionManifest",
        "ActionPlan",
        "ActionResult",
        "ActionStateError",
        "DesiredArtifact",
        "DesiredArtifactSet",
        "DuplicateArtifactError",
        "FilesystemReconciler",
        "InvalidArtifactPathError",
        "SymlinkEscapeError",
        "ToolIdentity",
        "default_state_dir",
        "digest_bytes",
        "normalize_relative_path",
        "sanitize_component",
    }


def test_is_query_active_exported() -> None:
    assert "is_query_active" in pyinc.__all__
    assert hasattr(pyinc, "is_query_active")
