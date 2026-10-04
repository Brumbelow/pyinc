"""JSON-Schema -> typed Python models via ``pyinc_codegen``, incrementally.

Generates models from a small schema, then shows three incremental properties
of the compiler:

- A whitespace or key-order edit writes nothing.
- A description-only edit rewrites only the documentation artifact.
- Removing a definition deletes only the files that definition owned.

Run: ``python examples/codegen_demo.py``
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from pyinc import Database
from pyinc_codegen import generate


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        schema_path = base / "schema.json"
        out = base / "gen"

        schema: dict[str, object] = {
            "$defs": {
                "Color": {"type": "string", "enum": ["red", "green"]},
                "Widget": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "color": {"$ref": "#/$defs/Color"},
                    },
                    "required": ["id"],
                },
            }
        }
        schema_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")

        db = Database(mode="strict")
        first = generate(db, schema_path, out)
        print(f"generated={first.created}")

        # Reformat and reorder keys. The parsed schema is unchanged, so every file stays as is.
        schema_path.write_text(json.dumps(schema, indent=4, sort_keys=True), encoding="utf-8")
        whitespace = generate(db, schema_path, out)
        whitespace_changes = whitespace.created + whitespace.updated + whitespace.repaired
        print(f"whitespace_edit_changed={whitespace_changes}")

        # Change only a description. Only the doc artifact is rewritten.
        widget = schema["$defs"]["Widget"]  # type: ignore[index]
        widget["description"] = "A widget."
        schema_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
        described = generate(db, schema_path, out)
        print(f"description_edit_updated={described.updated}")

        # Removing a definition deletes only the files it owned.
        del widget["properties"]["color"]
        del schema["$defs"]["Color"]  # type: ignore[attr-defined]
        schema_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
        removed = generate(db, schema_path, out)
        print(f"removed_def_deleted={removed.deleted}")


if __name__ == "__main__":
    main()
