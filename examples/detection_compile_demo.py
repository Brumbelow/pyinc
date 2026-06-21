"""Demonstrate the security detection-content compiler.

Writes a tiny normalized rule set (rules + field mappings + a macro + a rule-test
fixture), compiles it to per-backend queries / bundle / coverage / docs / test
results, and shows incremental behavior plus provenance:

- an identical rerun writes nothing,
- editing an *unused* field mapping writes nothing,
- editing a *used* field mapping rewrites only the affected backend query,
- provenance names the source rule, the referenced mapping + macro, and backend.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from pyinc import Database
from pyinc.integrations.detection_rules import generate_detections, rule_provenance


def _write_inputs(root: Path) -> None:
    (root / "rules").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "mappings.json").write_text(
        json.dumps(
            {
                "process.name": {"splunk": "Image", "sentinel": "ProcessName"},
                "command_line": {"splunk": "CommandLine", "sentinel": "CommandLine"},
                "unused.field": {"splunk": "Unused", "sentinel": "Unused"},
            }
        )
    )
    (root / "macros.json").write_text(
        json.dumps(
            {
                "encoded": {
                    "any": [
                        {"field": "command_line", "op": "contains", "value": "-enc"},
                        {"field": "command_line", "op": "contains", "value": "-EncodedCommand"},
                    ]
                }
            }
        )
    )
    (root / "rules" / "ps_enc.json").write_text(
        json.dumps(
            {
                "id": "ps_enc",
                "title": "PowerShell Encoded Command",
                "severity": "high",
                "attack": ["T1059.001"],
                "detection": {
                    "all": [
                        {"field": "process.name", "op": "equals", "value": "powershell.exe"},
                        {"macro": "encoded"},
                    ]
                },
            }
        )
    )
    (root / "tests" / "ps_enc.json").write_text(
        json.dumps(
            {
                "events": [
                    {"process.name": "powershell.exe", "command_line": "powershell -enc AAA"},
                    {"process.name": "notepad.exe", "command_line": "notepad"},
                ],
                "expect": [True, False],
            }
        )
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / "rules_root"
        out = Path(base) / "generated"
        state = Path(base) / "state"
        _write_inputs(root)
        db = Database()

        r1 = generate_detections(db, root, out, state_dir=state)
        files = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
        print(f"cold_write_count={len(r1.writes)}")
        print(f"generated_files={files}")
        print("splunk_query=" + (out / "queries/splunk/ps_enc.spl").read_text().strip())

        r2 = generate_detections(db, root, out, state_dir=state)
        print(f"rerun_writes={r2.writes}")

        # Editing an unused mapping writes nothing.
        mappings = json.loads((root / "mappings.json").read_text())
        mappings["unused.field"]["splunk"] = "Renamed"
        (root / "mappings.json").write_text(json.dumps(mappings))
        print(f"unused_mapping_writes={generate_detections(db, root, out, state_dir=state).writes}")

        # Editing a used mapping rewrites only that backend's query.
        mappings["process.name"]["splunk"] = "ImageRenamed"
        (root / "mappings.json").write_text(json.dumps(mappings))
        print(f"used_mapping_writes={sorted(generate_detections(db, root, out, state_dir=state).writes)}")

        prov = rule_provenance(db, root, "ps_enc", "splunk")
        print(f"provenance_rule={prov.rule_id}")
        print(f"provenance_mappings={prov.mappings}")
        print(f"provenance_macros={prov.macros}")
        print(f"provenance_backend={prov.backend}")


if __name__ == "__main__":
    main()
