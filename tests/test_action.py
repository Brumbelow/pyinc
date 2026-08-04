from __future__ import annotations

import hashlib
import importlib
import json
import multiprocessing
import os
import queue
import shutil
from pathlib import Path
from typing import Any

import pytest

import pyinc
from pyinc import (
    ActionLockTimeoutError,
    ActionManifestError,
    ActionPathError,
    Database,
    FileResource,
    Input,
    query,
)
from pyinc import (
    _safe_fs as safe_fs_module,
)
from pyinc._locking import FileLock
from pyinc.action import (
    Output,
    ReconcileResult,
    _atomic_write,
    _content_hash,
    _lock_path,
    _manifest_path,
    _normalize_rel,
    action,
)

_PROCESS_ACTION_TOOL = "cross-process-action"


def _action_reconcile_worker(
    root: str,
    label: str,
    content: bytes,
    hold_lock: bool,
    start: Any,
    release: Any,
    entered: Any,
    results: Any,
) -> None:
    @action(tool=_PROCESS_ACTION_TOOL)
    def process_action(db: Database) -> list[Output]:
        entered.put(label)
        if hold_lock:
            release.wait()
        return [Output("result.txt", content)]

    start.wait()
    try:
        result = process_action.reconcile(Database(), root=root)
    except Exception as error:  # noqa: BLE001 - cross-process result transport
        results.put((label, type(error).__name__))
    else:
        results.put(
            (
                label,
                result.created,
                result.updated,
                result.repaired,
                result.unchanged,
            )
        )


# --------------------------------------------------------------------------- #
# Task 1.1 — value types + fs helpers
# --------------------------------------------------------------------------- #


def test_output_text_encodes_utf8() -> None:
    out = Output.text("a/b.txt", "héllo")
    assert out.path == "a/b.txt"
    assert out.content == "héllo".encode()


def test_output_is_frozen() -> None:
    out = Output(path="x", content=b"y")
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError
        out.path = "z"  # type: ignore[misc]


def test_reconcile_result_is_frozen_dataclass() -> None:
    res = ReconcileResult(
        created=("a",),
        updated=(),
        repaired=(),
        deleted=(),
        unchanged=(),
        dry_run=False,
    )
    assert res.created == ("a",)
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError
        res.dry_run = True  # type: ignore[misc]


def test_normalize_rel_rejects_absolute_and_escape() -> None:
    assert _normalize_rel("a/b/c.py") == "a/b/c.py"
    with pytest.raises(ValueError):
        _normalize_rel("/abs/path")
    with pytest.raises(ValueError):
        _normalize_rel("../escape")
    with pytest.raises(ValueError):
        _normalize_rel("a/../../b")


def test_normalize_rel_rejects_surrogate_code_points_with_typed_error() -> None:
    with pytest.raises(ActionPathError):
        _normalize_rel("bad-\ud800.py")


def test_action_rejects_surrogate_output_path_before_mutation(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    sentinel = root / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")

    @action(tool="surrogate-output-path")
    def invalid_output_action(db: Database) -> list[Output]:
        return [Output("bad-\ud800.py", b"content")]

    with pytest.raises(ActionPathError):
        invalid_output_action.reconcile(Database(), root=root)
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert sorted(path.name for path in root.iterdir()) == ["sentinel.txt"]


def test_action_wraps_non_directory_root_as_typed_path_error(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.write_bytes(b"sentinel")
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    @action(tool="non-directory-root")
    def invalid_root_action(db: Database) -> list[Output]:
        return [Output("result.txt", b"content")]

    with pytest.raises(ActionPathError, match="inspect owned output path"):
        invalid_root_action.reconcile(Database(), root=root, state_dir=state_dir)

    assert root.read_bytes() == b"sentinel"
    assert tuple(state_dir.iterdir()) == ()


def test_action_wraps_root_inspection_failure_as_typed_path_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()

    @action(tool="uninspectable-root")
    def invalid_root_action(db: Database) -> list[Output]:
        return [Output("result.txt", b"content")]

    original_lstat = Path.lstat

    def fail_root_lstat(path: Path) -> os.stat_result:
        if path == root:
            raise OSError("inspection denied")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_root_lstat)
    with pytest.raises(ActionPathError, match="Cannot safely inspect owned output path"):
        invalid_root_action.reconcile(Database(), root=root)

    assert tuple(root.iterdir()) == ()


@pytest.mark.parametrize(
    ("root", "state_dir"),
    (("bad\0root", None), ("valid-root", "bad\0state")),
)
def test_action_wraps_invalid_root_paths_as_typed_errors(
    tmp_path: Path, root: str, state_dir: str | None
) -> None:
    @action(tool="invalid-root-path")
    def invalid_root_action(db: Database) -> list[Output]:
        return [Output("result.txt", b"content")]

    root_path = root if "\0" in root else os.fspath(tmp_path / root)
    state_path = state_dir if state_dir is None else os.fspath(tmp_path / state_dir)
    with pytest.raises(ActionPathError, match="root or state directory"):
        invalid_root_action.reconcile(Database(), root=root_path, state_dir=state_path)
    assert tuple(tmp_path.iterdir()) == ()


def test_action_rejects_surrogate_tool_identity_at_decoration() -> None:
    with pytest.raises(ValueError, match="valid UTF-8"):

        @action(tool="bad-\ud800-tool")
        def invalid_tool_action(db: Database) -> list[Output]:
            return []


def test_action_rejects_stateful_string_subclass_identities_and_paths(
    tmp_path: Path,
) -> None:
    class StatefulString(str):
        def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
            return b"changing-identity"

    with pytest.raises(TypeError, match="tool identity"):

        @action(tool=StatefulString("stateful-tool"))
        def invalid_tool_action(db: Database) -> list[Output]:
            return []

    @action(tool="exact-tool")
    def invalid_path_action(db: Database) -> list[Output]:
        return [Output(StatefulString("result.txt"), b"content")]

    with pytest.raises(ActionPathError, match="must be strings"):
        invalid_path_action.reconcile(Database(), root=tmp_path)
    assert tuple(tmp_path.iterdir()) == ()


def test_content_hash_is_sha256_hex() -> None:
    assert _content_hash(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_atomic_write_creates_parents_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "file.bin"
    _atomic_write(target, b"v1")
    assert target.read_bytes() == b"v1"
    _atomic_write(target, b"v2")
    assert target.read_bytes() == b"v2"
    assert [p.name for p in target.parent.iterdir()] == ["file.bin"]


# --------------------------------------------------------------------------- #
# Task 1.2 — Action.reconcile (A1, A2, A4, A6 dry-run)
# --------------------------------------------------------------------------- #

_FILES = FileResource()
SUFFIX = Input[str]("action_suffix")


@query
def _doc_text(db: Database, src: str) -> str:
    return _FILES.read(db, src)


@action(tool="demo-tool/1")
def _emit(db: Database, src: str) -> list[Output]:
    body = _doc_text(db, src)
    return [
        Output.text("out/a.txt", body.upper() + SUFFIX.read(db)),
        Output.text("out/b.txt", body.lower()),
    ]


def _setup(tmp_path: Path) -> tuple[Database, Path, Path]:
    src = tmp_path / "src.txt"
    src.write_text("Hello", encoding="utf-8")
    out = tmp_path / "out_root"
    db = Database(mode="strict")
    db.set(SUFFIX, "!")
    return db, src, out


def test_action_requires_tool() -> None:
    with pytest.raises(ValueError):

        @action(tool="")
        def _bad(db: Database) -> list[Output]:
            return []


def test_first_reconcile_writes_all(tmp_path: Path) -> None:
    db, src, out = _setup(tmp_path)
    res = _emit.reconcile(db, str(src), root=out)
    assert res.created == ("out/a.txt", "out/b.txt")
    assert res.updated == res.repaired == res.deleted == res.unchanged == ()
    assert (out / "out/a.txt").read_text() == "HELLO!"
    assert (out / "out/b.txt").read_text() == "hello"


def test_rerun_no_change_zero_writes(tmp_path: Path) -> None:  # A1
    db, src, out = _setup(tmp_path)
    _emit.reconcile(db, str(src), root=out)
    manifest = _manifest_path(out, _emit.tool)
    before = manifest.read_bytes()
    res = _emit.reconcile(db, str(src), root=out)
    assert res.created == res.updated == res.repaired == res.deleted == ()
    assert res.unchanged == ("out/a.txt", "out/b.txt")
    assert manifest.read_bytes() == before  # manifest untouched


def test_input_change_rewrites_only_affected(tmp_path: Path) -> None:  # A2
    db, src, out = _setup(tmp_path)
    _emit.reconcile(db, str(src), root=out)
    db.set(SUFFIX, "?")  # only a.txt embeds SUFFIX
    res = _emit.reconcile(db, str(src), root=out)
    assert res.updated == ("out/a.txt",)
    assert res.created == res.repaired == ()
    assert res.unchanged == ("out/b.txt",)
    assert (out / "out/a.txt").read_text() == "HELLO?"


def test_out_of_band_edit_is_repaired(tmp_path: Path) -> None:  # A4
    db, src, out = _setup(tmp_path)
    _emit.reconcile(db, str(src), root=out)
    (out / "out/b.txt").write_text("TAMPERED", encoding="utf-8")
    res = _emit.reconcile(db, str(src), root=out)
    assert res.repaired == ("out/b.txt",)
    assert res.created == res.updated == ()
    assert res.unchanged == ("out/a.txt",)  # untouched
    assert (out / "out/b.txt").read_text() == "hello"


def test_dry_run_touches_nothing(tmp_path: Path) -> None:  # A6 dry-run
    db, src, out = _setup(tmp_path)
    plan = _emit.plan(db, str(src), root=out)
    assert plan.dry_run is True
    assert plan.created == ("out/a.txt", "out/b.txt")
    assert not out.exists()  # plan() created no files


# --------------------------------------------------------------------------- #
# Task 1.3 — orphan deletion (A3) + failure cleanup (A6)
# --------------------------------------------------------------------------- #

EMIT_SET = Input[tuple[str, ...]]("emit_names")


@action(tool="orphan-tool/1")
def _emit_named(db: Database) -> list[Output]:
    return [Output.text(f"{name}.txt", name) for name in EMIT_SET.read(db)]


def test_removing_declaration_deletes_only_that_output(tmp_path: Path) -> None:  # A3
    out = tmp_path / "o"
    db = Database(mode="strict")
    db.set(EMIT_SET, ("alpha", "beta"))
    _emit_named.reconcile(db, root=out)
    assert (out / "alpha.txt").exists() and (out / "beta.txt").exists()

    db.set(EMIT_SET, ("alpha",))  # beta removed
    res = _emit_named.reconcile(db, root=out)
    assert res.deleted == ("beta.txt",)
    assert res.created == res.updated == res.repaired == ()
    assert res.unchanged == ("alpha.txt",)
    assert not (out / "beta.txt").exists()
    assert (out / "alpha.txt").read_text() == "alpha"


@action(tool="boom-tool/1")
def _emit_boom(db: Database) -> list[Output]:
    raise RuntimeError("output computation failed")


def test_failure_in_outputs_writes_nothing(tmp_path: Path) -> None:  # A6 failure cleanup
    out = tmp_path / "o"
    db = Database(mode="strict")
    with pytest.raises(RuntimeError):
        _emit_boom.reconcile(db, root=out)
    assert not out.exists()


# --------------------------------------------------------------------------- #
# Task 1.4 — from-scratch consistency over an edit sequence (A5)
# --------------------------------------------------------------------------- #


def _tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.startswith(".pyinc-action.")
    }


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_action_incremental_matches_fresh_over_edits(mode: str, tmp_path: Path) -> None:  # A5
    src = tmp_path / "src.txt"
    inc_root = tmp_path / "inc"
    inc_db = Database(mode=mode)
    inc_db.set(SUFFIX, "!")

    steps: tuple[tuple[str, str], ...] = (
        ("Hello", "!"),
        ("Hello", "!"),  # no-op
        ("Hello world", "!"),  # input file change
        ("Hello world", "?"),  # only-suffix input change
        ("Goodbye", "?"),
    )
    for text, suffix in steps:
        src.write_text(text, encoding="utf-8")
        inc_db.set(SUFFIX, suffix)
        _emit.reconcile(inc_db, str(src), root=inc_root)

        fresh_root = tmp_path / "fresh"
        if fresh_root.exists():
            shutil.rmtree(fresh_root)
        fresh_db = Database(mode=mode)
        fresh_db.set(SUFFIX, suffix)
        _emit.reconcile(fresh_db, str(src), root=fresh_root)

        assert _tree(inc_root) == _tree(fresh_root)


# --------------------------------------------------------------------------- #
# Task 1.5 — public export lock
# --------------------------------------------------------------------------- #


def test_action_names_are_public() -> None:
    for name in ("Action", "Output", "ReconcileResult", "action"):
        assert name in pyinc.__all__
        assert hasattr(pyinc, name)


# --------------------------------------------------------------------------- #
# v3 trust boundary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    (
        "",
        ".",
        "./a",
        "a/",
        "a//b",
        "../a",
        "a/../b",
        "/a",
        "C:/a",
        r"C:\a",
        "CON",
        "CONIN$.txt",
        "CONOUT$",
        "aux.txt",
        "COM¹.log",
        "name.",
        "name ",
        "a?.txt",
        "a|b",
    ),
)
def test_output_paths_reject_nonportable_or_escaping_forms(path: str) -> None:
    with pytest.raises(ActionPathError):
        _normalize_rel(path)


def test_desired_set_rejects_case_and_unicode_collisions(tmp_path: Path) -> None:
    @action(tool="collision-test")
    def emit_collisions(db: Database) -> list[Output]:
        return [Output.text("Café.py", "a"), Output.text("CAFÉ.py", "b")]

    with pytest.raises(ActionPathError, match="collision"):
        emit_collisions.reconcile(Database(), root=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_desired_set_rejects_file_directory_conflict(tmp_path: Path) -> None:
    @action(tool="tree-collision-test")
    def emit_conflict(db: Database) -> list[Output]:
        return [Output.text("pkg", "a"), Output.text("pkg/model.py", "b")]

    with pytest.raises(ActionPathError, match="file and directory"):
        emit_conflict.reconcile(Database(), root=tmp_path)
    assert list(tmp_path.iterdir()) == []


LAYOUT_KIND = Input[str]("layout_kind")


@action(tool="layout-migration/1")
def _emit_layout(db: Database) -> list[Output]:
    kind = LAYOUT_KIND.read(db)
    if kind == "none":
        return []
    if kind == "file":
        return [Output.text("pkg", "file layout")]
    return [Output.text("pkg/model.py", "directory layout")]


class _StoppedRun(RuntimeError):
    """Marks the simulated stop between publication and the ledger write."""


def _stop_before_the_ledger(*_args: object, **_kwargs: object) -> None:
    raise _StoppedRun("stopped before the ledger was published")


def _stop_layout_migration(root: Path, db: Database, kind: str) -> None:
    """Publish the outputs of ``kind`` but stop before the ledger is written."""
    action_module = importlib.import_module("pyinc.action")
    db.set(LAYOUT_KIND, kind)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(action_module, "_write_manifest", _stop_before_the_ledger)
        with pytest.raises(_StoppedRun):
            _emit_layout.reconcile(db, root=root)


def _ledger_outputs(root: Path) -> dict[str, str]:
    manifest = _manifest_path(root, _emit_layout.tool)
    outputs = json.loads(manifest.read_text(encoding="utf-8"))["outputs"]
    assert isinstance(outputs, dict)
    return outputs


def test_file_to_directory_output_migration_converges_like_a_fresh_root(tmp_path: Path) -> None:
    inc_root = tmp_path / "inc"
    db = Database(mode="strict")
    db.set(LAYOUT_KIND, "file")
    _emit_layout.reconcile(db, root=inc_root)

    db.set(LAYOUT_KIND, "directory")
    result = _emit_layout.reconcile(db, root=inc_root)

    assert result.created == ("pkg/model.py",)
    assert result.deleted == ("pkg",)
    assert result.updated == result.repaired == result.unchanged == ()

    fresh_root = tmp_path / "fresh"
    fresh_db = Database(mode="strict")
    fresh_db.set(LAYOUT_KIND, "directory")
    _emit_layout.reconcile(fresh_db, root=fresh_root)
    assert _tree(inc_root) == _tree(fresh_root)
    assert (inc_root / "pkg").is_dir()


def test_directory_to_file_output_migration_converges_like_a_fresh_root(tmp_path: Path) -> None:
    inc_root = tmp_path / "inc"
    db = Database(mode="strict")
    db.set(LAYOUT_KIND, "directory")
    _emit_layout.reconcile(db, root=inc_root)

    db.set(LAYOUT_KIND, "file")
    result = _emit_layout.reconcile(db, root=inc_root)

    assert result.created == ("pkg",)
    assert result.deleted == ("pkg/model.py",)
    assert result.updated == result.repaired == result.unchanged == ()

    fresh_root = tmp_path / "fresh"
    fresh_db = Database(mode="strict")
    fresh_db.set(LAYOUT_KIND, "file")
    _emit_layout.reconcile(fresh_db, root=fresh_root)
    assert _tree(inc_root) == _tree(fresh_root)
    assert (inc_root / "pkg").is_file()


def test_plan_reports_layout_migration_deletion_without_mutating(tmp_path: Path) -> None:
    db = Database(mode="strict")
    db.set(LAYOUT_KIND, "file")
    _emit_layout.reconcile(db, root=tmp_path)
    manifest = _manifest_path(tmp_path, _emit_layout.tool)
    manifest_before = manifest.read_bytes()

    db.set(LAYOUT_KIND, "directory")
    plan = _emit_layout.plan(db, root=tmp_path)

    assert plan.dry_run is True
    assert plan.created == ("pkg/model.py",)
    assert plan.deleted == ("pkg",)
    assert (tmp_path / "pkg").read_text(encoding="utf-8") == "file layout"
    assert not (tmp_path / "pkg").is_dir()
    assert manifest.read_bytes() == manifest_before


def test_directory_to_file_migration_refuses_to_prune_unowned_entries(tmp_path: Path) -> None:
    db = Database(mode="strict")
    db.set(LAYOUT_KIND, "directory")
    _emit_layout.reconcile(db, root=tmp_path)
    unowned = tmp_path / "pkg" / "unowned.txt"
    unowned.write_text("keep", encoding="utf-8")

    db.set(LAYOUT_KIND, "file")
    with pytest.raises(ActionPathError, match="prune"):
        _emit_layout.reconcile(db, root=tmp_path)
    assert unowned.read_text(encoding="utf-8") == "keep"

    unowned.unlink()
    result = _emit_layout.reconcile(db, root=tmp_path)
    assert result.created == ("pkg",)
    assert (tmp_path / "pkg").read_text(encoding="utf-8") == "file layout"


def test_migration_orphan_replaced_by_a_directory_follows_tamper_policy(tmp_path: Path) -> None:
    db = Database(mode="strict")
    db.set(LAYOUT_KIND, "file")
    _emit_layout.reconcile(db, root=tmp_path)
    (tmp_path / "pkg").unlink()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "user.txt").write_text("user data", encoding="utf-8")

    db.set(LAYOUT_KIND, "directory")
    with pytest.raises(ActionPathError, match="not a regular file"):
        _emit_layout.reconcile(db, root=tmp_path)
    assert (tmp_path / "pkg" / "user.txt").read_text(encoding="utf-8") == "user data"
    assert not (tmp_path / "pkg" / "model.py").exists()


CASE_LAYOUT_KIND = Input[str]("case_layout_kind")


@action(tool="layout-migration-case/1")
def _emit_case_layout(db: Database) -> list[Output]:
    if CASE_LAYOUT_KIND.read(db) == "upper-file":
        return [Output.text("PKG", "upper file layout")]
    return [Output.text("pkg/model.py", "directory layout")]


def test_casefold_twin_of_an_orphan_does_not_bypass_desired_target_validation(
    tmp_path: Path,
) -> None:
    (tmp_path / "case_probe").write_text("probe", encoding="utf-8")
    if (tmp_path / "CASE_PROBE").exists():
        pytest.skip("requires a case-sensitive filesystem")
    (tmp_path / "case_probe").unlink()

    db = Database(mode="strict")
    db.set(CASE_LAYOUT_KIND, "upper-file")
    _emit_case_layout.reconcile(db, root=tmp_path)
    # Deleting the orphan "PKG" frees nothing at "pkg": the unowned directory
    # there merely casefold-matches it, so the usual target validation applies.
    (tmp_path / "pkg" / "model.py").mkdir(parents=True)
    (tmp_path / "pkg" / "model.py" / "user.txt").write_text("user data", encoding="utf-8")

    db.set(CASE_LAYOUT_KIND, "directory")
    with pytest.raises(ActionPathError, match="not a regular file"):
        _emit_case_layout.plan(db, root=tmp_path)
    with pytest.raises(ActionPathError, match="not a regular file"):
        _emit_case_layout.reconcile(db, root=tmp_path)
    assert (tmp_path / "PKG").read_text(encoding="utf-8") == "upper file layout"
    assert (tmp_path / "pkg" / "model.py" / "user.txt").read_text(encoding="utf-8") == "user data"


def test_file_to_directory_crash_before_the_ledger_write_converges(tmp_path: Path) -> None:
    inc_root = tmp_path / "inc"
    db = Database(mode="strict")
    db.set(LAYOUT_KIND, "file")
    _emit_layout.reconcile(db, root=inc_root)
    stale_ledger = {"pkg": _content_hash(b"file layout")}

    _stop_layout_migration(inc_root, db, "directory")
    assert (inc_root / "pkg" / "model.py").read_text(encoding="utf-8") == "directory layout"
    assert _ledger_outputs(inc_root) == stale_ledger

    plan = _emit_layout.plan(db, root=inc_root)
    assert plan.dry_run is True
    assert plan.unchanged == ("pkg/model.py",)
    assert plan.deleted == plan.created == plan.updated == plan.repaired == ()
    assert _ledger_outputs(inc_root) == stale_ledger

    result = _emit_layout.reconcile(db, root=inc_root)
    assert result.unchanged == ("pkg/model.py",)
    assert result.deleted == result.created == result.updated == result.repaired == ()
    assert _ledger_outputs(inc_root) == {"pkg/model.py": _content_hash(b"directory layout")}

    fresh_root = tmp_path / "fresh"
    fresh_db = Database(mode="strict")
    fresh_db.set(LAYOUT_KIND, "directory")
    _emit_layout.reconcile(fresh_db, root=fresh_root)
    assert _tree(inc_root) == _tree(fresh_root)


def test_directory_to_file_crash_before_the_ledger_write_converges(tmp_path: Path) -> None:
    inc_root = tmp_path / "inc"
    db = Database(mode="strict")
    db.set(LAYOUT_KIND, "directory")
    _emit_layout.reconcile(db, root=inc_root)
    stale_ledger = {"pkg/model.py": _content_hash(b"directory layout")}

    _stop_layout_migration(inc_root, db, "file")
    assert (inc_root / "pkg").read_text(encoding="utf-8") == "file layout"
    assert _ledger_outputs(inc_root) == stale_ledger

    plan = _emit_layout.plan(db, root=inc_root)
    assert plan.dry_run is True
    assert plan.unchanged == ("pkg",)
    assert plan.deleted == plan.created == plan.updated == plan.repaired == ()
    assert _ledger_outputs(inc_root) == stale_ledger

    result = _emit_layout.reconcile(db, root=inc_root)
    assert result.unchanged == ("pkg",)
    assert result.deleted == result.created == result.updated == result.repaired == ()
    assert _ledger_outputs(inc_root) == {"pkg": _content_hash(b"file layout")}

    fresh_root = tmp_path / "fresh"
    fresh_db = Database(mode="strict")
    fresh_db.set(LAYOUT_KIND, "file")
    _emit_layout.reconcile(fresh_db, root=fresh_root)
    assert _tree(inc_root) == _tree(fresh_root)


def test_partially_published_migration_converges(tmp_path: Path) -> None:
    inc_root = tmp_path / "inc"
    db = Database(mode="strict")
    db.set(LAYOUT_KIND, "file")
    _emit_layout.reconcile(db, root=inc_root)
    (inc_root / "pkg").unlink()
    (inc_root / "pkg").mkdir()

    db.set(LAYOUT_KIND, "directory")
    result = _emit_layout.reconcile(db, root=inc_root)

    assert result.created == ("pkg/model.py",)
    assert result.deleted == ()
    assert (inc_root / "pkg" / "model.py").read_text(encoding="utf-8") == "directory layout"


@pytest.mark.parametrize(
    ("stopped", "rolled_back"),
    (("directory", "file"), ("file", "directory")),
)
def test_rollback_after_a_crash_window_converges(
    tmp_path: Path, stopped: str, rolled_back: str
) -> None:
    inc_root = tmp_path / "inc"
    db = Database(mode="strict")
    db.set(LAYOUT_KIND, rolled_back)
    _emit_layout.reconcile(db, root=inc_root)
    _stop_layout_migration(inc_root, db, stopped)
    _emit_layout.reconcile(db, root=inc_root)

    db.set(LAYOUT_KIND, rolled_back)
    result = _emit_layout.reconcile(db, root=inc_root)

    assert result.deleted != ()
    fresh_root = tmp_path / "fresh"
    fresh_db = Database(mode="strict")
    fresh_db.set(LAYOUT_KIND, rolled_back)
    _emit_layout.reconcile(fresh_db, root=fresh_root)
    assert _tree(inc_root) == _tree(fresh_root)


def test_teardown_after_a_directory_to_file_crash_releases_the_recorded_layout(
    tmp_path: Path,
) -> None:
    inc_root = tmp_path / "inc"
    db = Database(mode="strict")
    db.set(LAYOUT_KIND, "directory")
    _emit_layout.reconcile(db, root=inc_root)
    _stop_layout_migration(inc_root, db, "file")

    db.set(LAYOUT_KIND, "none")
    result = _emit_layout.reconcile(db, root=inc_root)

    assert result.deleted == ()
    assert _ledger_outputs(inc_root) == {}
    # The stopped run published this file without recording it; it is unowned
    # now and must survive the teardown.
    assert (inc_root / "pkg").read_text(encoding="utf-8") == "file layout"


def test_teardown_after_a_file_to_directory_crash_follows_tamper_policy(tmp_path: Path) -> None:
    inc_root = tmp_path / "inc"
    db = Database(mode="strict")
    db.set(LAYOUT_KIND, "file")
    _emit_layout.reconcile(db, root=inc_root)
    _stop_layout_migration(inc_root, db, "directory")

    db.set(LAYOUT_KIND, "none")
    with pytest.raises(ActionPathError, match="not a regular file"):
        _emit_layout.reconcile(db, root=inc_root)
    assert (inc_root / "pkg" / "model.py").read_text(encoding="utf-8") == "directory layout"

    # Converging the layout the stopped run published records it, after which
    # the teardown owns the file it must delete.
    db.set(LAYOUT_KIND, "directory")
    _emit_layout.reconcile(db, root=inc_root)
    db.set(LAYOUT_KIND, "none")
    result = _emit_layout.reconcile(db, root=inc_root)

    assert result.deleted == ("pkg/model.py",)
    assert not (inc_root / "pkg" / "model.py").exists()


def test_rollback_before_the_ledger_is_repaired_keeps_unrecorded_files(tmp_path: Path) -> None:
    inc_root = tmp_path / "inc"
    db = Database(mode="strict")
    db.set(LAYOUT_KIND, "file")
    _emit_layout.reconcile(db, root=inc_root)
    _stop_layout_migration(inc_root, db, "directory")

    db.set(LAYOUT_KIND, "file")
    with pytest.raises(ActionPathError, match="not a regular file"):
        _emit_layout.reconcile(db, root=inc_root)
    assert (inc_root / "pkg" / "model.py").read_text(encoding="utf-8") == "directory layout"
    assert _ledger_outputs(inc_root) == {"pkg": _content_hash(b"file layout")}


def test_crash_recovery_does_not_release_a_directory_holding_unowned_files(
    tmp_path: Path,
) -> None:
    inc_root = tmp_path / "inc"
    db = Database(mode="strict")
    db.set(LAYOUT_KIND, "file")
    _emit_layout.reconcile(db, root=inc_root)
    _stop_layout_migration(inc_root, db, "directory")
    (inc_root / "pkg" / "user.txt").write_text("user data", encoding="utf-8")

    with pytest.raises(ActionPathError, match="not a regular file"):
        _emit_layout.plan(db, root=inc_root)
    with pytest.raises(ActionPathError, match="not a regular file"):
        _emit_layout.reconcile(db, root=inc_root)
    assert (inc_root / "pkg" / "user.txt").read_text(encoding="utf-8") == "user data"


def test_orphan_whose_directory_was_already_pruned_converges(tmp_path: Path) -> None:
    inc_root = tmp_path / "inc"
    db = Database(mode="strict")
    db.set(LAYOUT_KIND, "directory")
    _emit_layout.reconcile(db, root=inc_root)
    # A run that stopped after its deletions and prune but before publication.
    (inc_root / "pkg" / "model.py").unlink()
    (inc_root / "pkg").rmdir()

    db.set(LAYOUT_KIND, "file")
    result = _emit_layout.reconcile(db, root=inc_root)

    assert result.created == ("pkg",)
    assert result.deleted == ()
    assert (inc_root / "pkg").read_text(encoding="utf-8") == "file layout"


def test_orphan_under_a_symbolic_link_is_never_released(tmp_path: Path) -> None:
    inc_root = tmp_path / "inc"
    db = Database(mode="strict")
    db.set(LAYOUT_KIND, "directory")
    _emit_layout.reconcile(db, root=inc_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "model.py").write_text("outside data", encoding="utf-8")
    (inc_root / "pkg" / "model.py").unlink()
    (inc_root / "pkg").rmdir()
    try:
        (inc_root / "pkg").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink support is unavailable")

    db.set(LAYOUT_KIND, "file")
    with pytest.raises(ActionPathError, match="symbolic link"):
        _emit_layout.reconcile(db, root=inc_root)
    assert (outside / "model.py").read_text(encoding="utf-8") == "outside data"


NESTED_LAYOUT_KIND = Input[str]("nested_layout_kind")


@action(tool="layout-migration-nested/1")
def _emit_nested_layout(db: Database) -> list[Output]:
    if NESTED_LAYOUT_KIND.read(db) == "file":
        return [Output.text("pkg", "file layout")]
    return [Output.text("pkg/inner/model.py", "nested layout")]


def test_nested_layout_crash_before_the_ledger_write_converges(tmp_path: Path) -> None:
    inc_root = tmp_path / "inc"
    db = Database(mode="strict")
    db.set(NESTED_LAYOUT_KIND, "file")
    _emit_nested_layout.reconcile(db, root=inc_root)

    db.set(NESTED_LAYOUT_KIND, "nested")
    action_module = importlib.import_module("pyinc.action")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(action_module, "_write_manifest", _stop_before_the_ledger)
        with pytest.raises(_StoppedRun):
            _emit_nested_layout.reconcile(db, root=inc_root)

    result = _emit_nested_layout.reconcile(db, root=inc_root)

    assert result.unchanged == ("pkg/inner/model.py",)
    assert result.deleted == ()

    db.set(NESTED_LAYOUT_KIND, "file")
    rolled_back = _emit_nested_layout.reconcile(db, root=inc_root)
    assert rolled_back.created == ("pkg",)
    assert rolled_back.deleted == ("pkg/inner/model.py",)
    assert (inc_root / "pkg").read_text(encoding="utf-8") == "file layout"


def test_nested_prune_refusal_names_the_directory_that_still_holds_an_entry(
    tmp_path: Path,
) -> None:
    db = Database(mode="strict")
    db.set(NESTED_LAYOUT_KIND, "nested")
    _emit_nested_layout.reconcile(db, root=tmp_path)
    unowned = tmp_path / "pkg" / "inner" / "unowned.txt"
    unowned.write_text("keep", encoding="utf-8")

    db.set(NESTED_LAYOUT_KIND, "file")
    with pytest.raises(ActionPathError, match="'pkg/inner'"):
        _emit_nested_layout.plan(db, root=tmp_path)
    assert unowned.read_text(encoding="utf-8") == "keep"
    assert (tmp_path / "pkg" / "inner" / "model.py").exists()


def test_plan_surfaces_the_prune_refusal_reconcile_enforces(tmp_path: Path) -> None:
    db = Database(mode="strict")
    db.set(LAYOUT_KIND, "directory")
    _emit_layout.reconcile(db, root=tmp_path)
    unowned = tmp_path / "pkg" / "unowned.txt"
    unowned.write_text("keep", encoding="utf-8")

    db.set(LAYOUT_KIND, "file")
    with pytest.raises(ActionPathError, match="prune"):
        _emit_layout.plan(db, root=tmp_path)
    with pytest.raises(ActionPathError, match="prune"):
        _emit_layout.reconcile(db, root=tmp_path)

    assert unowned.read_text(encoding="utf-8") == "keep"
    # The refusal is preflight, so the orphan is still there to delete once the
    # unowned entry is gone.
    assert (tmp_path / "pkg" / "model.py").read_text(encoding="utf-8") == "directory layout"


def test_tracked_missing_output_is_repaired(tmp_path: Path) -> None:
    db, src, out = _setup(tmp_path)
    _emit.reconcile(db, str(src), root=out)
    (out / "out/a.txt").unlink()

    result = _emit.reconcile(db, str(src), root=out)

    assert result.repaired == ("out/a.txt",)
    assert result.unchanged == ("out/b.txt",)


def test_malformed_manifest_fails_before_mutating_outputs(tmp_path: Path) -> None:
    db, src, out = _setup(tmp_path)
    _emit.reconcile(db, str(src), root=out)
    before = (out / "out/a.txt").read_bytes()
    db.set(SUFFIX, "?")
    manifest = _manifest_path(out, _emit.tool)
    payload = json.loads(manifest.read_text())
    payload["outputs"] = {"../escape": "0" * 64}
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ActionManifestError):
        _emit.reconcile(db, str(src), root=out)

    assert (out / "out/a.txt").read_bytes() == before


@pytest.mark.parametrize(
    "case",
    (
        "non-object",
        "missing-field",
        "unknown-field",
        "duplicate-root-field",
        "duplicate-output-path",
        "boolean-version",
        "old-version",
        "foreign-tool",
        "foreign-root",
        "non-object-outputs",
        "non-string-digest",
        "malformed-digest",
        "case-collision",
        "file-directory-collision",
    ),
)
def test_manifest_schema_is_strict_and_failure_is_premutation(tmp_path: Path, case: str) -> None:
    db, src, out = _setup(tmp_path)
    _emit.reconcile(db, str(src), root=out)
    output = out / "out/a.txt"
    before = output.read_bytes()
    db.set(SUFFIX, "?")
    manifest = _manifest_path(out, _emit.tool)
    valid = json.loads(manifest.read_text())
    zero = "0" * 64
    if case == "non-object":
        manifest_bytes = b"[]"
    elif case == "duplicate-root-field":
        root_json = json.dumps(valid["root"])
        manifest_bytes = (
            f'{{"root":{root_json},"root":{root_json},"tool":{json.dumps(_emit.tool)},'
            '"version":2,"outputs":{}}'
        ).encode()
    elif case == "duplicate-output-path":
        manifest_bytes = (
            f'{{"root":{json.dumps(valid["root"])},"tool":{json.dumps(_emit.tool)},'
            f'"version":2,"outputs":{{"a":"{zero}","a":"{zero}"}}}}'
        ).encode()
    else:
        if case == "missing-field":
            del valid["root"]
        elif case == "unknown-field":
            valid["extra"] = True
        elif case == "boolean-version":
            valid["version"] = True
        elif case == "old-version":
            valid["version"] = 1
        elif case == "foreign-tool":
            valid["tool"] = "foreign"
        elif case == "foreign-root":
            valid["root"] = zero
        elif case == "non-object-outputs":
            valid["outputs"] = []
        elif case == "non-string-digest":
            valid["outputs"] = {"a": False}
        elif case == "malformed-digest":
            valid["outputs"] = {"a": "ABCDEF"}
        elif case == "case-collision":
            valid["outputs"] = {"A": zero, "a": zero}
        elif case == "file-directory-collision":
            valid["outputs"] = {"a": zero, "a/b": zero}
        manifest_bytes = json.dumps(valid).encode()
    manifest.write_bytes(manifest_bytes)

    with pytest.raises(ActionManifestError):
        _emit.reconcile(db, str(src), root=out)

    assert output.read_bytes() == before


@pytest.mark.parametrize(
    "manifest_bytes",
    (
        b"[" * 200_000 + b"0" + b"]" * 200_000,
        b"9" * 5_000,
    ),
    ids=("deep", "huge-integer"),
)
def test_deep_or_huge_numeric_manifest_raises_typed_error_before_mutation(
    tmp_path: Path, manifest_bytes: bytes
) -> None:
    db, src, out = _setup(tmp_path)
    _emit.reconcile(db, str(src), root=out)
    output = out / "out/a.txt"
    before = output.read_bytes()
    db.set(SUFFIX, "?")
    _manifest_path(out, _emit.tool).write_bytes(manifest_bytes)

    with pytest.raises(ActionManifestError, match="Cannot read action manifest"):
        _emit.reconcile(db, str(src), root=out)
    assert output.read_bytes() == before


def test_manifest_surrogate_path_raises_typed_error_before_mutation(
    tmp_path: Path,
) -> None:
    db, src, out = _setup(tmp_path)
    _emit.reconcile(db, str(src), root=out)
    output = out / "out/a.txt"
    before = output.read_bytes()
    db.set(SUFFIX, "?")
    manifest = _manifest_path(out, _emit.tool)
    payload = json.loads(manifest.read_text())
    payload["outputs"] = {"bad-\ud800.py": "0" * 64}
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ActionManifestError, match="invalid path"):
        _emit.reconcile(db, str(src), root=out)
    assert output.read_bytes() == before


@pytest.mark.parametrize("manifest_kind", ("symlink", "directory", "fifo"))
def test_nonregular_manifest_is_rejected_before_mutation(
    tmp_path: Path, manifest_kind: str
) -> None:
    db, src, out = _setup(tmp_path)
    _emit.reconcile(db, str(src), root=out)
    output = out / "out/a.txt"
    before = output.read_bytes()
    db.set(SUFFIX, "?")
    manifest = _manifest_path(out, _emit.tool)
    manifest.unlink()
    if manifest_kind == "directory":
        manifest.mkdir()
    elif manifest_kind == "fifo":
        make_fifo = getattr(os, "mkfifo", None)
        if make_fifo is None:
            pytest.skip("FIFO creation is unavailable")
        make_fifo(manifest)
    else:
        outside = tmp_path / "outside-manifest.json"
        outside.write_text("{}", encoding="utf-8")
        try:
            manifest.symlink_to(outside)
        except OSError:
            pytest.skip("symbolic links are not available")

    with pytest.raises(ActionManifestError, match="regular file"):
        _emit.reconcile(db, str(src), root=out)
    assert output.read_bytes() == before


def test_manifest_uses_full_tool_identity_hash(tmp_path: Path) -> None:
    @action(tool="same/slug")
    def first(db: Database) -> list[Output]:
        return [Output.text("a", "a")]

    @action(tool="same_slug")
    def second(db: Database) -> list[Output]:
        return [Output.text("b", "b")]

    first.reconcile(Database(), root=tmp_path)
    second.reconcile(Database(), root=tmp_path)

    first_manifest = _manifest_path(tmp_path, first.tool)
    second_manifest = _manifest_path(tmp_path, second.tool)
    assert first_manifest != second_manifest
    assert json.loads(first_manifest.read_text())["tool"] == "same/slug"
    assert json.loads(second_manifest.read_text())["tool"] == "same_slug"


@pytest.mark.parametrize("shape", ("exact", "ancestor", "descendant", "case"))
def test_output_cannot_conflict_with_manifest_path(tmp_path: Path, shape: str) -> None:
    tool = "manifest-path-collision"
    manifest_name = _manifest_path(tmp_path, tool).name
    paths = {
        "exact": manifest_name,
        "ancestor": ".state",
        "descendant": f"{manifest_name}/child.txt",
        "case": manifest_name.upper(),
    }
    state_dir = tmp_path / ".state" if shape == "ancestor" else tmp_path

    @action(tool=tool)
    def emit_manifest_collision(db: Database) -> list[Output]:
        return [Output.text(paths[shape], "collision")]

    with pytest.raises(ActionPathError, match="manifest"):
        emit_manifest_collision.reconcile(Database(), root=tmp_path, state_dir=state_dir)
    assert not (tmp_path / paths[shape]).exists()


def test_symlink_in_owned_path_is_rejected_without_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are not available")

    @action(tool="symlink-test")
    def emit_link(db: Database) -> list[Output]:
        return [Output.text("link/escaped.txt", "unsafe")]

    with pytest.raises(ActionPathError, match="symbolic link"):
        emit_link.reconcile(Database(), root=root)
    assert not (outside / "escaped.txt").exists()


def test_parent_swap_before_atomic_write_cannot_redirect_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    parent = root / "nested"
    outside = tmp_path / "outside"
    moved = tmp_path / "moved-parent"
    parent.mkdir(parents=True)
    outside.mkdir()

    @action(tool="parent-swap-test")
    def parent_swap_action(db: Database) -> list[Output]:
        return [Output.text("nested/escaped.txt", "blocked")]

    action_module = importlib.import_module("pyinc.action")
    original_atomic_write = action_module.atomic_write

    def swap_then_write(target: Path, data: bytes) -> None:
        if target.name == "escaped.txt":
            parent.rename(moved)
            try:
                parent.symlink_to(outside, target_is_directory=True)
            except OSError:
                pytest.skip("symbolic links are not available")
        original_atomic_write(target, data)

    monkeypatch.setattr(action_module, "atomic_write", swap_then_write)
    with pytest.raises(ActionPathError):
        parent_swap_action.reconcile(Database(), root=root)
    assert not (outside / "escaped.txt").exists()
    assert not (moved / "escaped.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory-descriptor behavior")
def test_opened_parent_rename_is_rejected_before_action_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    parent = root / "nested"
    outside = tmp_path / "outside"
    moved = outside / "moved-parent"
    parent.mkdir(parents=True)
    outside.mkdir()

    @action(tool="opened-parent-rename")
    def emit(db: Database) -> list[Output]:
        return [Output.text("nested/result.txt", "blocked")]

    original = safe_fs_module._require_regular_or_missing
    raced = False

    def rename_after_validation(descriptor: int, name: str, path: Path) -> None:
        nonlocal raced
        original(descriptor, name, path)
        if path.name == "result.txt" and not raced:
            raced = True
            parent.rename(moved)
            parent.mkdir()

    monkeypatch.setattr(safe_fs_module, "_require_regular_or_missing", rename_after_validation)
    with pytest.raises(ActionPathError, match="trusted path"):
        emit.reconcile(Database(), root=root)

    assert raced
    assert not (moved / "result.txt").exists()
    assert not (parent / "result.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory-descriptor behavior")
def test_opened_parent_rename_is_rejected_before_action_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    parent = root / "nested"
    outside = tmp_path / "outside"
    moved = outside / "moved-parent"
    outside.mkdir()

    emit_file = True

    @action(tool="opened-parent-unlink")
    def emit(db: Database) -> list[Output]:
        return [Output.text("nested/result.txt", "owned")] if emit_file else []

    emit.reconcile(Database(), root=root)
    emit_file = False
    original = safe_fs_module._require_directory_identity
    raced = False

    def rename_before_identity_check(descriptor: int, path: Path) -> None:
        nonlocal raced
        if path == parent and not raced:
            raced = True
            parent.rename(moved)
        original(descriptor, path)

    monkeypatch.setattr(safe_fs_module, "_require_directory_identity", rename_before_identity_check)
    with pytest.raises(ActionPathError, match="trusted path"):
        emit.reconcile(Database(), root=root)

    assert raced
    assert (moved / "result.txt").read_bytes() == b"owned"


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 handle sharing")
def test_windows_action_holds_output_parent_against_a_junction_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pyinc import _safe_fs as safe_fs_module

    root = tmp_path / "root"
    parent = root / "nested"
    moved = tmp_path / "moved-parent"
    outside = tmp_path / "outside"
    parent.mkdir(parents=True)
    outside.mkdir()

    @action(tool="windows-parent-swap-test")
    def parent_swap_action(db: Database) -> list[Output]:
        return [Output.text("nested/result.txt", "safe")]

    target = parent / "result.txt"
    api = safe_fs_module._windows_api()
    api_type = type(api)
    original_rename = api_type.rename_handle
    swap_blocked = False

    def race_parent(self: Any, handle: int, destination: str) -> None:
        nonlocal swap_blocked
        if Path(destination) == target:
            try:
                parent.rename(moved)
            except OSError:
                swap_blocked = True
            else:
                parent.symlink_to(outside, target_is_directory=True)
        original_rename(self, handle, destination)

    monkeypatch.setattr(api_type, "rename_handle", race_parent)
    parent_swap_action.reconcile(Database(), root=root)

    assert swap_blocked
    assert target.read_text(encoding="utf-8") == "safe"
    assert not (outside / "result.txt").exists()
    assert not moved.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 handle sharing")
def test_windows_action_deletes_through_a_swap_protected_file_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pyinc import _safe_fs as safe_fs_module

    emit = Input[bool]("windows-delete-race.emit")

    @action(tool="windows-delete-race-test")
    def delete_race_action(db: Database) -> list[Output]:
        return [Output.text("owned.txt", "owned")] if emit.read(db) else []

    db = Database()
    db.set(emit, True)
    delete_race_action.reconcile(db, root=tmp_path)
    target = tmp_path / "owned.txt"
    moved = tmp_path / "moved-owned.txt"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    api = safe_fs_module._windows_api()
    api_type = type(api)
    original_delete = api_type.delete_handle
    swap_blocked = False

    def race_target(self: Any, handle: int, path: str) -> None:
        nonlocal swap_blocked
        if Path(path) == target:
            try:
                target.rename(moved)
            except OSError:
                swap_blocked = True
            else:
                target.symlink_to(outside)
        original_delete(self, handle, path)

    monkeypatch.setattr(api_type, "delete_handle", race_target)
    db.set(emit, False)
    delete_race_action.reconcile(db, root=tmp_path)

    assert swap_blocked
    assert not target.exists()
    assert not moved.exists()
    assert outside.read_text(encoding="utf-8") == "outside"


def test_nonregular_orphan_is_never_deleted(tmp_path: Path) -> None:
    db = Database()
    db.set(EMIT_SET, ("alpha",))
    _emit_named.reconcile(db, root=tmp_path)
    (tmp_path / "alpha.txt").unlink()
    (tmp_path / "alpha.txt").mkdir()
    db.set(EMIT_SET, ())

    with pytest.raises(ActionPathError, match="not a regular file"):
        _emit_named.reconcile(db, root=tmp_path)
    assert (tmp_path / "alpha.txt").is_dir()


def test_orphan_deletion_precedes_manifest_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declared = Input[bool]("action.sync-orphan")

    @action(tool="sync-order-test")
    def sync_order_action(db: Database) -> list[Output]:
        return [Output.text("nested/owned.txt", "owned")] if declared.read(db) else []

    db = Database()
    db.set(declared, True)
    sync_order_action.reconcile(db, root=tmp_path)
    db.set(declared, False)
    events: list[str] = []
    action_module = importlib.import_module("pyinc.action")
    original_unlink = action_module.unlink_regular_file
    original_manifest = action_module._write_manifest

    def record_unlink(path: Path) -> bool:
        events.append("delete")
        return bool(original_unlink(path))

    def record_manifest(*args: object, **kwargs: object) -> None:
        events.append("manifest")
        original_manifest(*args, **kwargs)

    monkeypatch.setattr(action_module, "unlink_regular_file", record_unlink)
    monkeypatch.setattr(action_module, "_write_manifest", record_manifest)

    sync_order_action.reconcile(db, root=tmp_path)

    assert events == ["delete", "manifest"]


def test_action_lock_timeout_is_typed(tmp_path: Path) -> None:
    @action(tool="lock-timeout-test")
    def locked_action(db: Database) -> list[Output]:
        return []

    root = tmp_path.resolve()
    lock = FileLock(_lock_path(root, locked_action.tool), timeout=0)
    with lock, pytest.raises(ActionLockTimeoutError):
        locked_action.reconcile(Database(), root=root, lock_timeout=0)


def test_action_rejects_nonregular_lock_path_with_typed_error(tmp_path: Path) -> None:
    @action(tool="unsafe-lock-path")
    def locked_action(db: Database) -> list[Output]:
        return []

    root = tmp_path / "root"
    root.mkdir()
    lock_path = _lock_path(root.resolve(), locked_action.tool)
    try:
        lock_path.symlink_to(tmp_path / "outside-lock")
    except OSError:
        pytest.skip("symlink support is unavailable")

    with pytest.raises(ActionPathError, match="reconciliation lock"):
        locked_action.reconcile(Database(), root=root)


def test_action_lock_identity_is_portable_across_case_and_unicode(tmp_path: Path) -> None:
    composed = tmp_path / "CAFÉ"
    decomposed_lower = tmp_path / "café"
    assert _lock_path(composed, "tool") == _lock_path(decomposed_lower, "tool")


def test_shared_state_directory_rejects_a_different_output_root(tmp_path: Path) -> None:
    state = tmp_path / "state"
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"

    @action(tool="root-bound-manifest")
    def root_bound_action(db: Database) -> list[Output]:
        return [Output.text("owned.txt", "content")]

    root_bound_action.reconcile(Database(), root=first_root, state_dir=state)
    with pytest.raises(ActionManifestError, match="root identity"):
        root_bound_action.reconcile(Database(), root=second_root, state_dir=state)

    assert (first_root / "owned.txt").read_text() == "content"
    assert not second_root.exists()


def test_stale_external_ledger_refuses_a_recreated_output_root(tmp_path: Path) -> None:
    state = tmp_path / "state"
    root = tmp_path / "root"

    @action(tool="incarnation-bound-manifest")
    def incarnation_action(db: Database) -> list[Output]:
        return [Output.text("owned.txt", "content")]

    incarnation_action.reconcile(Database(), root=root, state_dir=state)
    assert (root / "owned.txt").read_text() == "content"

    # The root is deleted and recreated at the same path: the ledger's claims
    # name a directory that no longer exists, so they must not delete files
    # somebody else placed in the new one.
    shutil.rmtree(root)
    root.mkdir()
    somebody_else = root / "owned.txt"
    somebody_else.write_text("not the ledger's file", encoding="utf-8")

    with pytest.raises(ActionManifestError, match="incarnation"):
        incarnation_action.reconcile(Database(), root=root, state_dir=state)
    assert somebody_else.read_text() == "not the ledger's file"


@pytest.mark.parametrize("timeout", (float("nan"), float("inf"), -float("inf")))
def test_action_rejects_nonfinite_lock_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite"):

        @action(tool="invalid-timeout", lock_timeout=timeout)
        def invalid_timeout_action(db: Database) -> list[Output]:
            return []


def _process_context() -> Any:
    return multiprocessing.get_context("spawn" if os.name == "nt" else "fork")


def test_complete_action_cycle_is_serialized_across_processes(tmp_path: Path) -> None:
    context = _process_context()
    start = context.Event()
    release = context.Event()
    entered = context.Queue()
    results = context.Queue()
    first = context.Process(
        target=_action_reconcile_worker,
        args=(str(tmp_path), "first", b"first", True, start, release, entered, results),
    )
    second = context.Process(
        target=_action_reconcile_worker,
        args=(str(tmp_path), "second", b"second", False, start, release, entered, results),
    )

    first.start()
    start.set()
    assert entered.get(timeout=5) == "first"
    second.start()
    with pytest.raises(queue.Empty):
        entered.get(timeout=0.2)
    release.set()
    assert entered.get(timeout=5) == "second"
    for process in (first, second):
        process.join(timeout=10)
        assert process.exitcode == 0

    outcomes = [results.get(timeout=1), results.get(timeout=1)]
    assert all(len(outcome) == 5 for outcome in outcomes)
    assert (tmp_path / "result.txt").read_bytes() == b"second"
    manifest = json.loads(_manifest_path(tmp_path, _PROCESS_ACTION_TOOL).read_text())
    assert manifest["outputs"] == {"result.txt": hashlib.sha256(b"second").hexdigest()}


def test_equal_cross_process_actions_converge_to_create_then_noop(tmp_path: Path) -> None:
    context = _process_context()
    start = context.Event()
    release = context.Event()
    entered = context.Queue()
    results = context.Queue()
    processes = [
        context.Process(
            target=_action_reconcile_worker,
            args=(
                str(tmp_path),
                label,
                b"same",
                False,
                start,
                release,
                entered,
                results,
            ),
        )
        for label in ("one", "two")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    outcomes = [results.get(timeout=1), results.get(timeout=1)]
    classifications = sorted((outcome[1], outcome[4]) for outcome in outcomes)
    assert classifications == [((), ("result.txt",)), (("result.txt",), ())]
    assert (tmp_path / "result.txt").read_bytes() == b"same"
