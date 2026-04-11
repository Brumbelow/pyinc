from __future__ import annotations

import ast
from pathlib import Path

from pyfoundinc import Database, EnvResource, FileResource, query

files = FileResource()
env = EnvResource()


def ast_semantic_eq(left: str, right: str) -> bool:
    return ast.dump(ast.parse(left), include_attributes=False) == ast.dump(ast.parse(right), include_attributes=False)


@query(eq=ast_semantic_eq)
def parse_source(db: Database, path: str) -> str:
    return files.read(db, path)


@query
def imports(db: Database, path: str) -> tuple[str, ...]:
    source = parse_source(db, path)
    tree = ast.parse(source)
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.append(module)
    return tuple(names)


@query
def diagnostics(db: Database, path: str) -> tuple[str, ...]:
    py_path = env.read(db, "PYTHONPATH") or ""
    detected = imports(db, path)
    if not detected:
        return ("no-imports", f"pythonpath={py_path}")
    return tuple(f"import:{name}" for name in detected)


if __name__ == "__main__":
    sample = Path(__file__).with_name("sample_module.py")
    if not sample.exists():
        sample.write_text("import os\n", encoding="utf-8")
    db = Database(mode="strict")
    print(db.get(diagnostics, str(sample)))
    print(db.explain(diagnostics, str(sample)))
