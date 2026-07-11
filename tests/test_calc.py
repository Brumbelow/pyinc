from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

_EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))

from calc.engine import (  # noqa: E402
    _parse,
    _semantic_token,
    binding_expr,
    calc_emit,
    calc_source,
    evaluate_name,
    parse_calc,
)

from pyinc import Database  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _tree(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.startswith(".pyinc-action.")
    }


# --------------------------------------------------------------------------- #
# Task 2A.1 — parser + semantic cutoff token
# --------------------------------------------------------------------------- #


def test_parse_extracts_includes_bindings_emits() -> None:
    src = '# c\ninclude "constants.calc"\nlet alpha = beta + 2\nlet beta = 40\nemit alpha\n'
    parsed = _parse(src)
    assert parsed.includes == ("constants.calc",)
    assert parsed.emits == ("alpha",)
    assert tuple(name for name, _ in parsed.bindings) == ("alpha", "beta")
    assert parsed.diagnostics == ()


def test_semantic_token_ignores_comments_and_whitespace() -> None:
    a = "let x = 1 + 2\nemit x\n"
    b = "# header\nlet x = 1 + 2   \n\nemit x\n"
    assert _semantic_token(a) == _semantic_token(b)


def test_parse_reports_unparseable_line() -> None:
    parsed = _parse("let = =\n")
    assert parsed.diagnostics and parsed.diagnostics[0][0] == "calc-parse-error"


# --------------------------------------------------------------------------- #
# Task 2A.2 — evaluation + diagnostics
# --------------------------------------------------------------------------- #


def test_forward_reference_evaluates(tmp_path: Path) -> None:
    root = tmp_path / "m.calc"
    _write(root, "let alpha = beta + 2\nlet beta = 40\nemit alpha\n")
    db = Database(mode="strict")
    assert evaluate_name(db, str(root), "alpha") == ("value", 42, "", "")


def test_subtraction_evaluates(tmp_path: Path) -> None:
    root = tmp_path / "m.calc"
    _write(root, "let a = 50 - 8 - b\nlet b = 2\nemit a\n")
    db = Database(mode="strict")
    assert evaluate_name(db, str(root), "a") == ("value", 40, "", "")


def test_include_brings_constants(tmp_path: Path) -> None:
    _write(tmp_path / "constants.calc", "let base = 100\n")
    root = tmp_path / "m.calc"
    _write(root, 'include "constants.calc"\nlet total = base - 1\nemit total\n')
    db = Database(mode="strict")
    assert evaluate_name(db, str(root), "total") == ("value", 99, "", "")


def test_cycle_is_deterministic_diagnostic(tmp_path: Path) -> None:
    root = tmp_path / "m.calc"
    _write(root, "let a = b\nlet b = a\nemit a\n")
    db = Database(mode="strict")
    result = evaluate_name(db, str(root), "a")
    assert result[0] == "error" and result[2] == "calc-cycle"


def test_missing_name_is_diagnostic(tmp_path: Path) -> None:
    root = tmp_path / "m.calc"
    _write(root, "let a = missing + 1\nemit a\n")
    db = Database(mode="strict")
    result = evaluate_name(db, str(root), "a")
    assert result[0] == "error" and result[2] == "calc-unbound"


def test_include_cycle_resolves_without_hanging(tmp_path: Path) -> None:
    # Mutual includes (a -> b -> a) must not loop: binding_table's visited set
    # dedups the include graph, so bindings still resolve across the cycle.
    a = tmp_path / "a.calc"
    b = tmp_path / "b.calc"
    _write(a, 'include "b.calc"\nlet x = y + 1\nemit x\n')
    _write(b, 'include "a.calc"\nlet y = 41\n')
    db = Database(mode="strict")
    assert evaluate_name(db, str(a), "x") == ("value", 42, "", "")


# --------------------------------------------------------------------------- #
# Task 2A.3 — incremental dataflow + provenance (B1, B2, B3, B5)
# --------------------------------------------------------------------------- #


def test_unrelated_file_edit_no_execution(tmp_path: Path) -> None:  # B1
    root = tmp_path / "m.calc"
    other = tmp_path / "other.calc"
    _write(root, "let a = 1\nemit a\n")
    _write(other, "let z = 9\n")
    db = Database(mode="strict")
    evaluate_name(db, str(root), "a")
    db.reset_statistics()
    _write(other, "let z = 10\n")  # unrelated, not included anywhere
    assert evaluate_name(db, str(root), "a") == ("value", 1, "", "")
    # The specific downstream node is reused (not just "total count unchanged").
    assert db.inspect(evaluate_name, str(root), "a").last_decision == "reused"
    assert db.statistics().query_executions == 0


def test_comment_only_edit_backdates(tmp_path: Path) -> None:  # B3
    root = tmp_path / "m.calc"
    _write(root, "let a = 1 + 1\nemit a\n")
    db = Database(mode="strict")
    evaluate_name(db, str(root), "a")
    _write(root, "# note\nlet a = 1 + 1\nemit a\n")
    assert evaluate_name(db, str(root), "a") == ("value", 2, "", "")
    assert db.inspect(calc_source, str(root)).last_recompute == "backdated"
    assert db.inspect(parse_calc, str(root)).last_decision == "reused"
    assert db.inspect(evaluate_name, str(root), "a").last_decision == "reused"


def test_referenced_edit_invalidates_only_dependent(tmp_path: Path) -> None:  # B2
    root = tmp_path / "m.calc"
    _write(root, "let a = 10\nlet b = 20\nemit a\nemit b\n")
    db = Database(mode="strict")
    evaluate_name(db, str(root), "a")
    evaluate_name(db, str(root), "b")
    _write(root, "let a = 11\nlet b = 20\nemit a\nemit b\n")  # only a changes
    assert evaluate_name(db, str(root), "a") == ("value", 11, "", "")
    assert evaluate_name(db, str(root), "b") == ("value", 20, "", "")
    # `a` re-executes; `b`'s expression is re-validated but unchanged, so it
    # backdates and `b`'s evaluation is reused — only the dependent changed.
    assert db.inspect(evaluate_name, str(root), "a").last_recompute == "executed"
    assert db.inspect(binding_expr, str(root), "b").last_recompute == "backdated"
    assert db.inspect(evaluate_name, str(root), "b").last_decision == "reused"


def test_explain_shows_root_include_and_chain(tmp_path: Path) -> None:  # B5
    _write(tmp_path / "constants.calc", "let beta = 40\n")
    root = tmp_path / "m.calc"
    _write(root, 'include "constants.calc"\nlet alpha = beta + 2\nemit alpha\n')
    db = Database(mode="strict")
    evaluate_name(db, str(root), "alpha")
    text = db.explain(evaluate_name, str(root), "alpha")
    assert "m.calc" in text
    assert "constants.calc" in text
    assert "binding_expr[" in text
    assert "beta" not in text  # argument values stay behind digest-only labels


# --------------------------------------------------------------------------- #
# Task 2A.4 — action emitter, removal (B4), from-scratch (B6)
# --------------------------------------------------------------------------- #


def test_emit_writes_one_output_per_emit(tmp_path: Path) -> None:
    root = tmp_path / "m.calc"
    out = tmp_path / "out"
    _write(root, "let a = 6 + 1\nemit a\n")
    db = Database(mode="strict")
    res = calc_emit.reconcile(db, str(root), root=out)
    assert res.created == ("a.out",)
    assert (out / "a.out").read_text() == "7\n"


def test_removing_emit_deletes_owned_output(tmp_path: Path) -> None:  # B4
    root = tmp_path / "m.calc"
    out = tmp_path / "out"
    _write(root, "let a = 1\nlet b = 2\nemit a\nemit b\n")
    db = Database(mode="strict")
    calc_emit.reconcile(db, str(root), root=out)
    assert (out / "a.out").exists() and (out / "b.out").exists()
    _write(root, "let a = 1\nlet b = 2\nemit a\n")  # emit b removed
    res = calc_emit.reconcile(db, str(root), root=out)
    assert res.deleted == ("b.out",)
    assert res.created == res.updated == res.repaired == ()
    assert not (out / "b.out").exists()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_calc_incremental_matches_fresh(mode: str, tmp_path: Path) -> None:  # B6
    inc = tmp_path / "constants.calc"
    root = tmp_path / "m.calc"
    out_inc = tmp_path / "inc"
    inc_db = Database(mode=mode)
    edits: tuple[tuple[str, str], ...] = (
        ('include "constants.calc"\nlet a = base + 1\nemit a\n', "let base = 10\n"),
        ('include "constants.calc"\nlet a = base + 1\nemit a\n', "let base = 20\n"),  # ref edit
        ('# c\ninclude "constants.calc"\nlet a = base + 1\nemit a\n', "let base = 20\n"),  # comment
        (
            'include "constants.calc"\nlet a = base + 1\nlet z = 5\nemit a\nemit z\n',
            "let base = 20\n",
        ),  # add emit
        ('include "constants.calc"\nlet a = base + 1\nemit a\n', "let base = 20\n"),  # remove emit
    )
    for root_text, inc_text in edits:
        _write(inc, inc_text)
        _write(root, root_text)
        calc_emit.reconcile(inc_db, str(root), root=out_inc)

        out_fresh = tmp_path / "fresh"
        if out_fresh.exists():
            shutil.rmtree(out_fresh)
        fresh_db = Database(mode=mode)
        calc_emit.reconcile(fresh_db, str(root), root=out_fresh)

        assert _tree(out_inc) == _tree(out_fresh)
