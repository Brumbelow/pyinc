"""Demonstrate the GraphQL introspection-driven incremental generator.

Writes a tiny introspection document, generates typed models / operation stubs /
docs, and shows the incremental behavior: an identical rerun writes nothing, a
whitespace edit writes nothing, and a description-only edit regenerates only the
affected documentation file (the typed model is untouched).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from pyinc import Database
from pyinc.integrations.graphql_schema import generate_graphql


def _named(name: str, kind: str) -> dict[str, object]:
    return {"kind": kind, "name": name, "ofType": None}


def _nn(ref: dict[str, object]) -> dict[str, object]:
    return {"kind": "NON_NULL", "name": None, "ofType": ref}


def _schema_document(user_description: str) -> dict[str, object]:
    ID = _named("ID", "SCALAR")
    STR = _named("String", "SCALAR")
    ROLE = _named("Role", "ENUM")
    USER = _named("User", "OBJECT")
    return {
        "data": {
            "__schema": {
                "queryType": {"name": "Query"},
                "mutationType": None,
                "types": [
                    {"kind": "SCALAR", "name": "ID", "description": None},
                    {"kind": "SCALAR", "name": "String", "description": None},
                    {
                        "kind": "ENUM",
                        "name": "Role",
                        "description": "Access level.",
                        "enumValues": [
                            {"name": "ADMIN", "description": None},
                            {"name": "MEMBER", "description": None},
                        ],
                    },
                    {
                        "kind": "OBJECT",
                        "name": "User",
                        "description": user_description,
                        "interfaces": [],
                        "fields": [
                            {"name": "id", "description": None, "args": [], "type": _nn(ID)},
                            {"name": "name", "description": None, "args": [], "type": _nn(STR)},
                            {"name": "role", "description": None, "args": [], "type": _nn(ROLE)},
                        ],
                    },
                    {
                        "kind": "OBJECT",
                        "name": "Query",
                        "description": "Root.",
                        "interfaces": [],
                        "fields": [
                            {
                                "name": "user",
                                "description": None,
                                "args": [{"name": "id", "description": None, "type": _nn(ID)}],
                                "type": USER,
                            }
                        ],
                    },
                ],
            }
        }
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as root:
        base = Path(root)
        schema = base / "schema.json"
        out = base / "generated"
        state = base / "state"
        db = Database()

        schema.write_text(json.dumps(_schema_document("A user account."), indent=2))
        r1 = generate_graphql(db, schema, out, state_dir=state)
        files = sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file())
        print(f"cold_write_count={len(r1.writes)}")
        print(f"generated_files={files}")

        # Identical rerun: zero writes.
        r2 = generate_graphql(db, schema, out, state_dir=state)
        print(f"rerun_writes={r2.writes}")

        # Whitespace-only edit: zero writes.
        schema.write_text(json.dumps(_schema_document("A user account."), indent=6))
        r3 = generate_graphql(db, schema, out, state_dir=state)
        print(f"whitespace_writes={r3.writes}")

        # Description-only edit: only the User doc regenerates.
        schema.write_text(json.dumps(_schema_document("A registered account holder.")))
        r4 = generate_graphql(db, schema, out, state_dir=state)
        print(f"description_edit_writes={r4.writes}")


if __name__ == "__main__":
    main()
