from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.check_docs import (
        _ACTION_VERSION_PROSE,
        PROJECT_ROOT,
        check_action_manifest_version,
        check_checkpoint_manifest_version,
        check_docs,
        check_documented_dataclass_fields,
        check_local_links,
    )
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from check_docs import (  # noqa: E402
        _ACTION_VERSION_PROSE,
        PROJECT_ROOT,
        check_action_manifest_version,
        check_checkpoint_manifest_version,
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


def _write_action_version_tree(
    root: Path,
    *,
    constant: int,
    action_contract: int | None,
    architecture: int | None,
) -> None:
    """Write the smallest tree the action-ledger check reads: the module and two documents.

    The module is written rather than read from the repository so a fixture can
    say what the constant is; a check that only ever sees the shipped 3 could be
    comparing against a literal and nothing here would notice.
    """
    package = root / "src" / "pyinc"
    package.mkdir(parents=True, exist_ok=True)
    (package / "action.py").write_text(
        '"""The action ledger."""\n'
        "\n"
        f"_MANIFEST_VERSION = {constant}\n"
        '_LEDGER_NAME = "ledger.json"\n',
        encoding="utf-8",
    )
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    if action_contract is not None:
        (docs / "action-contract.md").write_text(
            "# Action contract\n\n"
            f"Schema v{action_contract} records exactly `root`, `tool`, and `outputs`.\n\n"
            "v1 and v2 manifests are intentionally not compatible with "
            f"v{action_contract}'s ledger semantics and may be discarded.\n",
            encoding="utf-8",
        )
    if architecture is not None:
        (docs / "architecture.md").write_text(
            "# Architecture\n\n"
            f"Files publish atomically, and the schema-v{architecture} ledger is "
            "published last.\n",
            encoding="utf-8",
        )


def _write_checkpoint_version_tree(
    root: Path,
    *,
    constant: int,
    kernel_contract: int,
    architecture: int | None,
) -> None:
    """Write the smallest tree the checkpoint check reads: the module and two documents."""
    package = root / "src" / "pyinc"
    package.mkdir(parents=True, exist_ok=True)
    (package / "runtime.py").write_text(
        '"""The durable cache."""\n'
        "\n"
        f"_CHECKPOINT_MANIFEST_VERSION = {constant}\n",
        encoding="utf-8",
    )
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "kernel-contract.md").write_text(
        "# Kernel contract\n\n"
        f"Each checkpoint carries a content-addressed manifest (schema v{kernel_contract}).\n\n"
        f"Manifest schema v{kernel_contract} rejects everything older with "
        "`CheckpointVersionError`.\n",
        encoding="utf-8",
    )
    if architecture is not None:
        (docs / "architecture.md").write_text(
            "# Architecture\n\n"
            f"The loader accepts manifest schema v{architecture} only and validates "
            "every entry.\n",
            encoding="utf-8",
        )


def test_action_ledger_check_reports_a_document_naming_the_wrong_schema_version(
    tmp_path: Path,
) -> None:
    _write_action_version_tree(tmp_path, constant=3, action_contract=3, architecture=2)

    errors = check_action_manifest_version(tmp_path)

    assert len(errors) == 1
    assert "the ledger-publication clause" in errors[0]
    assert "v2" in errors[0]
    assert "is 3" in errors[0]


def test_action_ledger_check_accepts_documents_that_agree_with_the_constant(
    tmp_path: Path,
) -> None:
    _write_action_version_tree(tmp_path, constant=3, action_contract=3, architecture=3)

    assert check_action_manifest_version(tmp_path) == ()


def test_action_ledger_check_reports_every_stale_sentence_separately(tmp_path: Path) -> None:
    """Three stale sentences over two documents have to arrive as three readable errors.

    Two of them are in the same file, so an error line that does not name the
    sentence it came from would report the same text twice and leave a reader
    with no way to tell which one to fix.
    """
    _write_action_version_tree(tmp_path, constant=4, action_contract=3, architecture=3)

    errors = check_action_manifest_version(tmp_path)

    assert len(errors) == 3
    assert len(set(errors)) == 3
    for label in (
        "the schema-records sentence",
        "the incompatibility sentence",
        "the ledger-publication clause",
    ):
        assert sum(1 for error in errors if label in error) == 1


def test_action_ledger_check_reports_a_missing_document_instead_of_raising(
    tmp_path: Path,
) -> None:
    _write_action_version_tree(tmp_path, constant=3, action_contract=3, architecture=None)

    errors = check_action_manifest_version(tmp_path)

    assert len(errors) == 1
    assert "docs/architecture.md" in errors[0]
    assert "missing document" in errors[0]


def test_checkpoint_check_reports_a_missing_document_instead_of_raising(tmp_path: Path) -> None:
    """A named document that has been removed must not take every other check down with it."""
    _write_checkpoint_version_tree(tmp_path, constant=8, kernel_contract=8, architecture=None)

    errors = check_checkpoint_manifest_version(tmp_path)

    assert len(errors) == 1
    assert "docs/architecture.md" in errors[0]
    assert "missing document" in errors[0]


def test_checkpoint_check_distinguishes_two_stale_sentences_in_one_document(
    tmp_path: Path,
) -> None:
    _write_checkpoint_version_tree(tmp_path, constant=9, kernel_contract=8, architecture=9)

    errors = check_checkpoint_manifest_version(tmp_path)

    assert len(errors) == 2
    assert errors[0] != errors[1]


def test_every_documentation_check_is_registered_with_the_composed_checker() -> None:
    """A check the script defines but the composition never calls is a check that never runs."""
    source = PROJECT_ROOT / "scripts" / "check_docs.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        and node.name.startswith("check_")
        # The composition is excluded because it cannot register itself: a
        # function's own name is absent from its own co_names unless it recurses.
        and node.name != check_docs.__name__
    }
    assert defined, "expected the checker to define checks to compose"

    unregistered = sorted(defined - set(check_docs.__code__.co_names))

    assert not unregistered, "checks the composition never calls: " + ", ".join(unregistered)


def test_the_action_ledger_check_reads_three_sentences_in_two_documents() -> None:
    """Dropping an entry would narrow the check silently: the sentence left behind stays green."""
    documents = [relative for relative, _label, _pattern in _ACTION_VERSION_PROSE]

    assert len(documents) == 3
    assert documents.count(Path("docs/action-contract.md")) == 2
    assert documents.count(Path("docs/architecture.md")) == 1
