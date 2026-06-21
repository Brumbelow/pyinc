from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from pyinc import Database
from pyinc.actions import ActionManifest
from pyinc.integrations import detection_rules as det

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "detection"

EXPECTED_FILES = {
    "bundle.json",
    "coverage_matrix.json",
    "coverage/broken_rule.json",
    "coverage/powershell_enc.json",
    "coverage/suspicious_child.json",
    "docs/broken_rule.md",
    "docs/powershell_enc.md",
    "docs/suspicious_child.md",
    "queries/elastic/powershell_enc.kql",
    "queries/elastic/suspicious_child.kql",
    "queries/sentinel/powershell_enc.kql",
    "queries/sentinel/suspicious_child.kql",
    "queries/splunk/powershell_enc.spl",
    "queries/splunk/suspicious_child.spl",
    "tests/powershell_enc.json",
}


def _copy(tmp_path: Path) -> Path:
    root = tmp_path / "rules"
    shutil.copytree(FIXTURE, root)
    return root


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)).replace("\\", "/"): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


def _generate(db: Database, root: Path, out: Path, state: Path) -> Any:
    return det.generate_detections(db, root, out, state_dir=state)


# ---------------------------------------------------------------------------
# Analysis model + diagnostics
# ---------------------------------------------------------------------------


def test_detection_analysis_reports_rules_and_diagnostics() -> None:
    analysis = det.detection_analysis(Database(), FIXTURE)
    by_id = {r.rule_id: r for r in analysis.rules}
    assert by_id["powershell_enc"].status == "ok"
    assert by_id["powershell_enc"].techniques == ("T1059.001",)
    assert by_id["broken_rule"].status == "error"
    assert any(d.code == "unsupported-operator" for d in by_id["broken_rule"].diagnostics)


def test_malformed_rule_returns_deterministic_diagnostic(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    (root / "rules" / "bad.json").write_text("{not json")
    analysis = det.detection_analysis(Database(), root)
    bad = next(r for r in analysis.rules if r.rule_id == "bad")
    assert [d.code for d in bad.diagnostics] == ["json-decode-error"]


# ---------------------------------------------------------------------------
# Generation + incremental behavior
# ---------------------------------------------------------------------------


def test_cold_generation_produces_expected_files(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    out, state = tmp_path / "out", tmp_path / "state"
    result = _generate(Database(), root, out, state)
    assert set(_tree(out)) == EXPECTED_FILES
    assert result.deletions == ()
    # invalid rules render no backend query.
    assert not (out / "queries/splunk/broken_rule.spl").exists()


def test_rendered_queries_apply_field_mappings_and_macros(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    out, state = tmp_path / "out", tmp_path / "state"
    _generate(Database(), root, out, state)
    spl = (out / "queries/splunk/powershell_enc.spl").read_text()
    assert 'Image="powershell.exe"' in spl  # logical process.name -> splunk Image
    assert 'CommandLine="*-enc*"' in spl  # macro expanded + contains rendering
    kql = (out / "queries/sentinel/powershell_enc.kql").read_text()
    assert 'ProcessName == "powershell.exe"' in kql


def test_rule_test_results_are_generated(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    out, state = tmp_path / "out", tmp_path / "state"
    _generate(Database(), root, out, state)
    results = json.loads((out / "tests/powershell_enc.json").read_text())["results"]
    assert results[0] == {"event": 0, "matched": True, "expected": True, "pass": True}
    assert results[1]["matched"] is False and results[1]["pass"] is True


def test_identical_rerun_zero_writes(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    out, state = tmp_path / "out", tmp_path / "state"
    db = Database()
    _generate(db, root, out, state)
    result = _generate(db, root, out, state)
    assert result.writes == ()
    assert result.unchanged == len(EXPECTED_FILES)


def test_unused_mapping_edit_zero_writes(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    out, state = tmp_path / "out", tmp_path / "state"
    db = Database()
    _generate(db, root, out, state)
    mappings = json.loads((root / "mappings.json").read_text())
    mappings["unused.field"]["splunk"] = "Renamed"
    (root / "mappings.json").write_text(json.dumps(mappings, indent=2))
    result = _generate(db, root, out, state)
    assert result.writes == ()


def test_used_mapping_edit_regenerates_only_affected_backend(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    out, state = tmp_path / "out", tmp_path / "state"
    db = Database()
    _generate(db, root, out, state)
    mappings = json.loads((root / "mappings.json").read_text())
    mappings["process.name"]["splunk"] = "ImageRenamed"
    (root / "mappings.json").write_text(json.dumps(mappings, indent=2))
    result = _generate(db, root, out, state)
    # Both rules use process.name; only the splunk backend changed.
    assert set(result.writes) == {
        "queries/splunk/powershell_enc.spl",
        "queries/splunk/suspicious_child.spl",
    }


def test_rule_removal_deletes_only_owned_outputs(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    out, state = tmp_path / "out", tmp_path / "state"
    db = Database()
    _generate(db, root, out, state)
    (root / "rules" / "suspicious_child.json").unlink()
    result = _generate(db, root, out, state)
    assert set(result.deletions) == {
        "coverage/suspicious_child.json",
        "docs/suspicious_child.md",
        "queries/elastic/suspicious_child.kql",
        "queries/sentinel/suspicious_child.kql",
        "queries/splunk/suspicious_child.spl",
    }
    # Aggregates legitimately rewrite (rule removed from bundle + matrix).
    assert "bundle.json" in result.writes
    assert "coverage_matrix.json" in result.writes


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_provenance_names_rule_mapping_macro_and_backend() -> None:
    db = Database()
    prov = det.rule_provenance(db, FIXTURE, "powershell_enc", "splunk")
    assert prov.rule_id == "powershell_enc"
    assert prov.backend == "splunk"
    assert prov.rule_source.endswith("rules/powershell_enc.json")
    assert "process.name" in prov.mappings
    assert "encoded_command" in prov.macros
    assert "rendered_rule" in prov.transforms
    assert "field_mapping" in prov.transforms
    assert "macro_expr" in prov.transforms


# ---------------------------------------------------------------------------
# From-scratch consistency over an edit sequence
# ---------------------------------------------------------------------------


def test_incremental_matches_from_scratch_over_edit_sequence(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    out, state = tmp_path / "out", tmp_path / "state"
    db = Database()
    _generate(db, root, out, state)

    def edit_mapping(_: int) -> None:
        m = json.loads((root / "mappings.json").read_text())
        m["process.name"]["elastic"] = "renamed.process.name"
        (root / "mappings.json").write_text(json.dumps(m))

    def edit_macro(_: int) -> None:
        macros = json.loads((root / "macros.json").read_text())
        macros["encoded_command"]["any"].append(
            {"field": "command_line", "op": "contains", "value": "-e "}
        )
        (root / "macros.json").write_text(json.dumps(macros))

    def edit_rule(_: int) -> None:
        rule = json.loads((root / "rules" / "suspicious_child.json").read_text())
        rule["severity"] = "high"
        (root / "rules" / "suspicious_child.json").write_text(json.dumps(rule))

    def remove_rule(_: int) -> None:
        (root / "rules" / "powershell_enc.json").unlink()

    for i, edit in enumerate([edit_mapping, edit_macro, edit_rule, remove_rule]):
        edit(i)
        _generate(db, root, out, state)

        fresh_out = tmp_path / f"fresh_{i}"
        fresh_state = tmp_path / f"fresh_state_{i}"
        _generate(Database(), root, fresh_out, fresh_state)

        assert _tree(out) == _tree(fresh_out)
        inc = ActionManifest.from_json_bytes((state / "manifest.json").read_bytes())
        fresh = ActionManifest.from_json_bytes((fresh_state / "manifest.json").read_bytes())
        assert inc.owned_paths == fresh.owned_paths


# ---------------------------------------------------------------------------
# Contract lock
# ---------------------------------------------------------------------------


def test_detection_rules_all_is_exact() -> None:
    assert set(det.__all__) == {
        "DetectionAnalysis",
        "DetectionDiagnostic",
        "DetectionProvenance",
        "DetectionRule",
        "detection_analysis",
        "detection_artifacts",
        "generate_detections",
        "rule_provenance",
    }
