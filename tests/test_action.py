from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

import pyinc
from pyinc import Database, FileResource, Input, query
from pyinc.action import (
    Output,
    ReconcileResult,
    _atomic_write,
    _content_hash,
    _normalize_rel,
    action,
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
    res = ReconcileResult(written=("a",), deleted=(), unchanged=(), dry_run=False)
    assert res.written == ("a",)
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
    assert res.written == ("out/a.txt", "out/b.txt")
    assert res.deleted == () and res.unchanged == ()
    assert (out / "out/a.txt").read_text() == "HELLO!"
    assert (out / "out/b.txt").read_text() == "hello"


def test_rerun_no_change_zero_writes(tmp_path: Path) -> None:  # A1
    db, src, out = _setup(tmp_path)
    _emit.reconcile(db, str(src), root=out)
    manifest = out / ".pyinc-action.demo-tool_1.json"
    before = manifest.read_bytes()
    res = _emit.reconcile(db, str(src), root=out)
    assert res.written == () and res.deleted == ()
    assert res.unchanged == ("out/a.txt", "out/b.txt")
    assert manifest.read_bytes() == before  # manifest untouched


def test_input_change_rewrites_only_affected(tmp_path: Path) -> None:  # A2
    db, src, out = _setup(tmp_path)
    _emit.reconcile(db, str(src), root=out)
    db.set(SUFFIX, "?")  # only a.txt embeds SUFFIX
    res = _emit.reconcile(db, str(src), root=out)
    assert res.written == ("out/a.txt",)
    assert res.unchanged == ("out/b.txt",)
    assert (out / "out/a.txt").read_text() == "HELLO?"


def test_out_of_band_edit_is_repaired(tmp_path: Path) -> None:  # A4
    db, src, out = _setup(tmp_path)
    _emit.reconcile(db, str(src), root=out)
    (out / "out/b.txt").write_text("TAMPERED", encoding="utf-8")
    res = _emit.reconcile(db, str(src), root=out)
    assert res.written == ("out/b.txt",)  # repaired
    assert res.unchanged == ("out/a.txt",)  # untouched
    assert (out / "out/b.txt").read_text() == "hello"


def test_dry_run_touches_nothing(tmp_path: Path) -> None:  # A6 dry-run
    db, src, out = _setup(tmp_path)
    plan = _emit.plan(db, str(src), root=out)
    assert plan.dry_run is True
    assert plan.written == ("out/a.txt", "out/b.txt")
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
    assert res.written == () and res.unchanged == ("alpha.txt",)
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
        if p.is_file()
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
