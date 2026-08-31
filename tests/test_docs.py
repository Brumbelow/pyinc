from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.check_docs import (
        PROJECT_ROOT,
        check_docs,
        check_documented_dataclass_fields,
        check_local_links,
    )
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from check_docs import (  # noqa: E402
        PROJECT_ROOT,
        check_docs,
        check_documented_dataclass_fields,
        check_local_links,
    )


def test_documentation_checker_accepts_repository() -> None:
    errors = check_docs(PROJECT_ROOT)
    assert not errors, "\n".join(errors)


def test_documentation_checker_reports_missing_anchor(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    target = tmp_path / "target.md"
    readme.write_text("# Root\n\n[bad](target.md#missing)\n", encoding="utf-8")
    target.write_text("# Present\n", encoding="utf-8")

    errors = check_local_links(tmp_path, (readme, target))

    assert len(errors) == 1
    assert "missing anchor #missing" in errors[0]


def test_documentation_checker_ignores_external_links(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# Root\n\n[external](https://example.invalid/missing)\n", encoding="utf-8")

    assert check_local_links(tmp_path, (readme,)) == ()


def _write_field_parity_tree(root: Path, *rows: str) -> None:
    """Write the smallest tree the field-parity check reads: a contract and two modules."""
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{row}\n" for row in rows)
    (docs / "kernel-contract.md").write_text(
        "# Kernel\n\n## Public Surface\n\n| Name | What it is |\n|---|---|\n" + body,
        encoding="utf-8",
    )
    package = root / "src" / "pyinc"
    package.mkdir(parents=True, exist_ok=True)
    (package / "explain.py").write_text(
        "from dataclasses import dataclass\n\n\n"
        "@dataclass(frozen=True)\n"
        "class InspectionNode:\n"
        "    label: str\n"
        "    kind: str\n",
        encoding="utf-8",
    )
    (package / "runtime.py").write_text(
        "from dataclasses import dataclass\n\n\n"
        "@dataclass(frozen=True)\n"
        "class QueryProfile:\n"
        "    query_label: str\n"
        "    total_ns: int\n",
        encoding="utf-8",
    )


def test_field_parity_reports_a_documented_name_the_dataclass_does_not_declare(
    tmp_path: Path,
) -> None:
    _write_field_parity_tree(
        tmp_path,
        "| `InspectionNode` | One node. Fields: `label`, `name`. |",
    )

    errors = check_documented_dataclass_fields(tmp_path)

    assert len(errors) == 1
    assert "InspectionNode" in errors[0]
    assert "`kind`" in errors[0]


def test_field_parity_accepts_rows_matching_their_dataclasses(tmp_path: Path) -> None:
    _write_field_parity_tree(
        tmp_path,
        "| `InspectionNode` | One node. Fields: `label`, `kind`. |",
        "| `QueryProfile` | One profile. Fields: `query_label`, `total_ns`. |",
    )

    assert check_documented_dataclass_fields(tmp_path) == ()


def test_field_parity_reports_a_row_with_no_field_sentence(tmp_path: Path) -> None:
    _write_field_parity_tree(
        tmp_path,
        "| `InspectionNode` | One node in a report. |",
    )

    errors = check_documented_dataclass_fields(tmp_path)

    assert len(errors) == 1
    assert "InspectionNode" in errors[0]


def test_field_parity_reports_field_names_listed_out_of_order(tmp_path: Path) -> None:
    _write_field_parity_tree(
        tmp_path,
        "| `InspectionNode` | One node. Fields: `kind`, `label`. |",
    )

    errors = check_documented_dataclass_fields(tmp_path)

    assert len(errors) == 1
    assert "InspectionNode" in errors[0]


def test_field_parity_check_is_registered_with_the_composed_checker() -> None:
    assert "check_documented_dataclass_fields" in check_docs.__code__.co_names


_TEST_TOKEN = re.compile(r"\btest_[A-Za-z0-9_]+")


def _cited_test_names(root: Path) -> set[str]:
    """Every `test_*` token the shipped documents name, minus the test modules themselves.

    A token that also names a file under `tests/` was matched inside a path such
    as `tests/test_python_source.py`; it names a module, not a function, and the
    census would report all six of them as missing without this exclusion.
    """
    tokens: set[str] = set()
    for document in sorted((root / "docs").glob("*.md")):
        tokens.update(_TEST_TOKEN.findall(document.read_text(encoding="utf-8")))
    return {token for token in tokens if not (root / "tests" / f"{token}.py").exists()}


def _defined_test_names(root: Path) -> set[str]:
    """Every `test_*` function name defined under `tests/`, read without importing."""
    names: set[str] = set()
    for module in sorted((root / "tests").rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name.startswith(
                "test_"
            ):
                names.add(node.name)
    return names


def test_every_test_the_documentation_cites_still_exists() -> None:
    """A renamed test has to take its citations with it.

    The documentation names individual tests as the evidence for what it claims,
    and the documentation checker reads none of those names -- a rename that
    leaves one behind passes every other gate silently. This is the gate that
    catches it.
    """
    cited = _cited_test_names(PROJECT_ROOT)
    assert len(cited) > 50, f"expected the documents to cite many tests, found {len(cited)}"

    orphaned = sorted(cited - _defined_test_names(PROJECT_ROOT))

    assert not orphaned, "documentation cites tests that do not exist: " + ", ".join(orphaned)
