"""JSON-Schema -> typed Python models via ``pyinc_codegen``, incrementally.

Generates models from a small schema, then shows the incremental properties of
the compiler: a whitespace/key-reorder edit writes nothing; a description-only
edit rewrites only the documentation artifact; removing a definition deletes
only the files that definition owned.

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
        print(f"generated={first.written}")

        # Whitespace + key reordering: parsed schema is identical, nothing rewrites.
        schema_path.write_text(json.dumps(schema, indent=4, sort_keys=True), encoding="utf-8")
        whitespace = generate(db, schema_path, out)
        print(f"whitespace_edit_written={whitespace.written}")

        # Description-only change: only the doc artifact rewrites, not the model.
        widget = schema["$defs"]["Widget"]  # type: ignore[index]
        widget["description"] = "A widget."
        schema_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
        described = generate(db, schema_path, out)
        print(f"description_edit_written={described.written}")

        # Removing a definition deletes only the files it owned.
        del schema["$defs"]["Color"]  # type: ignore[attr-defined]
        schema_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
        removed = generate(db, schema_path, out)
        print(f"removed_def_deleted={removed.deleted}")


if __name__ == "__main__":
    main()
