from __future__ import annotations

import importlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pyinc import ActionManifestError, ActionPathError, Database
from pyinc.action import Output, _manifest_path, action

action_module: Any = importlib.import_module("pyinc.action")
safe_fs_module: Any = importlib.import_module("pyinc._safe_fs")


def _tree_bytes(root: Path) -> dict[str, bytes | None]:
    if not root.exists():
        return {}
    result: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        result[relative] = None if path.is_dir() else path.read_bytes()
    return result


@pytest.mark.parametrize("mode", ("strict", "checked", "fast"))
def test_scandir_failure_aborts_before_tree_or_ledger_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    layout = "directory"

    @action(tool=f"fail-closed-scandir-{mode}")
    def emit(_db: Database) -> list[Output]:
        if layout == "directory":
            return [Output.text("pkg/model.py", "owned")]
        return [Output.text("pkg", "replacement")]

    root = tmp_path / "root"
    emit.reconcile(Database(mode=mode), root=root)
    manifest = _manifest_path(root, emit.tool)
    tree_before = _tree_bytes(root)
    manifest_before = manifest.read_bytes()
    layout = "file"

    original_scandir = action_module.os.scandir

    def deny_scandir(path: Any) -> Any:
        if Path(os.fspath(path)) == root / "pkg":
            raise PermissionError("preflight denied")
        return original_scandir(path)

    monkeypatch.setattr(action_module.os, "scandir", deny_scandir)
    with pytest.raises(ActionPathError, match="Cannot inspect deletion recovery state"):
        emit.reconcile(Database(mode=mode), root=root)
    monkeypatch.setattr(action_module.os, "scandir", original_scandir)

    assert _tree_bytes(root) == tree_before
    assert manifest.read_bytes() == manifest_before


@pytest.mark.parametrize("mode", ("strict", "checked", "fast"))
def test_empty_root_incarnation_adoption_rewrites_empty_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    wanted = True
    incarnation = [1, 1]

    @action(tool=f"empty-incarnation-adoption-{mode}")
    def emit(_db: Database) -> list[Output]:
        return [Output.text("owned.txt", "same bytes")] if wanted else []

    monkeypatch.setattr(action_module, "_root_incarnation", lambda _root: list(incarnation))
    root = tmp_path / "root"
    state = tmp_path / "state"
    emit.reconcile(Database(mode=mode), root=root, state_dir=state)

    wanted = False
    incarnation[:] = [2, 2]
    replacement = root / "owned.txt"
    replacement.write_text("same bytes", encoding="utf-8")
    result = emit.reconcile(Database(mode=mode), root=root, state_dir=state)

    manifest = json.loads(_manifest_path(state, emit.tool).read_text(encoding="utf-8"))
    assert result.deleted == ()
    assert replacement.read_text(encoding="utf-8") == "same bytes"
    assert manifest["root_incarnation"] == [2, 2]
    assert manifest["outputs"] == {}

    incarnation[:] = [1, 1]
    result = emit.reconcile(Database(mode=mode), root=root, state_dir=state)
    assert result.deleted == ()
    assert replacement.read_text(encoding="utf-8") == "same bytes"


@pytest.mark.parametrize("mode", ("strict", "checked", "fast"))
def test_incarnation_mismatch_does_not_bypass_manifest_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    wanted = True
    incarnation = [1, 1]

    @action(tool=f"validate-before-incarnation-{mode}")
    def emit(_db: Database) -> list[Output]:
        return [Output.text("owned.txt", "owned")] if wanted else []

    monkeypatch.setattr(action_module, "_root_incarnation", lambda _root: list(incarnation))
    root = tmp_path / "root"
    state = tmp_path / "state"
    emit.reconcile(Database(mode=mode), root=root, state_dir=state)
    manifest_path = _manifest_path(state, emit.tool)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["outputs"] = {"../escape": "0" * 64}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    tree_before = _tree_bytes(root)
    ledger_before = manifest_path.read_bytes()

    wanted = False
    incarnation[:] = [2, 2]
    with pytest.raises(ActionManifestError, match="invalid path"):
        emit.reconcile(Database(mode=mode), root=root, state_dir=state)

    assert _tree_bytes(root) == tree_before
    assert manifest_path.read_bytes() == ledger_before


@pytest.mark.skipif(os.name == "nt", reason="POSIX quarantine race injection")
@pytest.mark.parametrize("mode", ("strict", "checked", "fast"))
def test_digest_race_restores_changed_leaf_and_reports_no_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    wanted = True

    @action(tool=f"digest-race-{mode}")
    def emit(_db: Database) -> list[Output]:
        return [Output.text("owned.txt", "owned")] if wanted else []

    root = tmp_path / "root"
    emit.reconcile(Database(mode=mode), root=root)
    wanted = False
    original_rename = safe_fs_module.os.rename
    raced = False

    def change_before_quarantine(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal raced
        if source == "owned.txt" and destination == "payload" and not raced:
            raced = True
            (root / "owned.txt").write_text("replacement", encoding="utf-8")
        original_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(safe_fs_module.os, "rename", change_before_quarantine)
    result = emit.reconcile(Database(mode=mode), root=root)

    assert raced
    assert result.deleted == ()
    assert result.would_delete == ()
    assert (root / "owned.txt").read_text(encoding="utf-8") == "replacement"
    assert not list(root.glob(".pyinc-delete-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX quarantine race injection")
def test_replacement_created_after_quarantine_is_never_unlinked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "owned.txt"
    target.write_bytes(b"owned")
    digest = action_module._content_hash(b"owned")
    original_rename = safe_fs_module.os.rename

    def replace_after_quarantine(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        original_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        if source == "owned.txt" and destination == "payload":
            target.write_bytes(b"replacement")

    monkeypatch.setattr(safe_fs_module.os, "rename", replace_after_quarantine)

    assert safe_fs_module.unlink_regular_file(target, expected_digest=digest)
    assert target.read_bytes() == b"replacement"
    assert not list(tmp_path.glob(".pyinc-delete-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX quarantine fault injection")
@pytest.mark.parametrize("mode", ("strict", "checked", "fast"))
def test_open_failure_after_quarantine_restores_tree_and_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    wanted = True

    @action(tool=f"quarantine-open-failure-{mode}")
    def emit(_db: Database) -> list[Output]:
        return [Output.text("owned.txt", "owned")] if wanted else []

    root = tmp_path / "root"
    emit.reconcile(Database(mode=mode), root=root)
    manifest = _manifest_path(root, emit.tool)
    ledger_before = manifest.read_bytes()
    tree_before = _tree_bytes(root)
    wanted = False
    original_open: Callable[..., int] = safe_fs_module.os.open

    def fail_quarantined_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path == "payload":
            raise PermissionError("injected post-rename open failure")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(safe_fs_module.os, "open", fail_quarantined_open)
    with pytest.raises(ActionPathError, match="Cannot safely delete"):
        emit.reconcile(Database(mode=mode), root=root)

    assert _tree_bytes(root) == tree_before
    assert manifest.read_bytes() == ledger_before
    assert not list(root.glob(".pyinc-delete-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX quarantine recovery contract")
@pytest.mark.parametrize("mode", ("strict", "checked", "fast"))
def test_process_death_quarantine_blocks_adoption_without_mutation(
    tmp_path: Path, mode: str
) -> None:
    wanted = True

    @action(tool=f"quarantine-process-death-{mode}")
    def emit(_db: Database) -> list[Output]:
        return [Output.text("owned.txt", "owned")] if wanted else []

    root = tmp_path / "root"
    emit.reconcile(Database(mode=mode), root=root)
    manifest = _manifest_path(root, emit.tool)
    ledger_before = manifest.read_bytes()
    quarantine = root / ".pyinc-delete-interrupted"
    quarantine.mkdir()
    (root / "owned.txt").rename(quarantine / "payload")
    crashed_tree = _tree_bytes(root)
    wanted = False

    with pytest.raises(ActionPathError, match="Interrupted deletion quarantine"):
        emit.reconcile(Database(mode=mode), root=root)

    assert _tree_bytes(root) == crashed_tree
    assert manifest.read_bytes() == ledger_before
    assert (quarantine / "payload").read_bytes() == b"owned"


@pytest.mark.skipif(os.name == "nt", reason="POSIX quarantine recovery contract")
@pytest.mark.parametrize("mode", ("strict", "checked", "fast"))
def test_process_death_quarantine_with_replacement_fails_closed(
    tmp_path: Path, mode: str
) -> None:
    wanted = True

    @action(tool=f"quarantine-process-death-replacement-{mode}")
    def emit(_db: Database) -> list[Output]:
        return [Output.text("owned.txt", "owned")] if wanted else []

    root = tmp_path / "root"
    emit.reconcile(Database(mode=mode), root=root)
    manifest = _manifest_path(root, emit.tool)
    ledger_before = manifest.read_bytes()
    quarantine = root / ".pyinc-delete-interrupted"
    quarantine.mkdir()
    (root / "owned.txt").rename(quarantine / "payload")
    (root / "owned.txt").write_bytes(b"replacement")
    crashed_tree = _tree_bytes(root)
    wanted = False

    with pytest.raises(ActionPathError, match="Interrupted deletion quarantine"):
        emit.reconcile(Database(mode=mode), root=root)

    assert _tree_bytes(root) == crashed_tree
    assert manifest.read_bytes() == ledger_before
    assert (root / "owned.txt").read_bytes() == b"replacement"
    assert (quarantine / "payload").read_bytes() == b"owned"


@pytest.mark.skipif(os.name == "nt", reason="POSIX open-writer contract")
def test_noncooperating_open_writer_is_outside_posix_deletion_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "owned.txt"
    target.write_bytes(b"owned")
    expected = action_module._content_hash(b"owned")
    writer = os.open(target, os.O_RDWR)
    original_read: Callable[[int], bytes] = safe_fs_module._read_descriptor

    def mutate_after_hash_read(descriptor: int) -> bytes:
        payload = original_read(descriptor)
        os.lseek(writer, 0, os.SEEK_SET)
        os.write(writer, b"changed")
        os.ftruncate(writer, len(b"changed"))
        return payload

    monkeypatch.setattr(safe_fs_module, "_read_descriptor", mutate_after_hash_read)
    try:
        assert safe_fs_module.unlink_regular_file(target, expected_digest=expected)
    finally:
        os.close(writer)

    assert not target.exists()
    assert not list(tmp_path.glob(".pyinc-delete-*"))


@pytest.mark.parametrize("mode", ("strict", "checked", "fast"))
def test_dry_run_deletion_predictions_are_separate_from_actual_telemetry(
    tmp_path: Path, mode: str
) -> None:
    wanted = True

    @action(tool=f"dry-delete-telemetry-{mode}")
    def emit(_db: Database) -> list[Output]:
        return [Output.text("owned.txt", "owned")] if wanted else []

    root = tmp_path / "root"
    emit.reconcile(Database(mode=mode), root=root)
    wanted = False

    plan = emit.plan(Database(mode=mode), root=root)
    assert plan.deleted == ()
    assert plan.would_delete == ("owned.txt",)
    assert (root / "owned.txt").exists()

    result = emit.reconcile(Database(mode=mode), root=root)
    assert result.deleted == ("owned.txt",)
    assert result.would_delete == ()
    assert not (root / "owned.txt").exists()
