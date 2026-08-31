from __future__ import annotations

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
