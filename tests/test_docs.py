from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.check_docs import (
        _ACTION_VERSION_PROSE,
        _INLINE_CODE,
        _PUBLIC_ROW_NAMES,
        PROJECT_ROOT,
        TableRow,
        check_action_manifest_version,
        check_checkpoint_manifest_version,
        check_docs,
        check_documented_dataclass_fields,
        check_documented_lsp_methods,
        check_local_links,
        markdown_files,
        table_rows,
    )
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from check_docs import (  # noqa: E402
        _ACTION_VERSION_PROSE,
        _INLINE_CODE,
        _PUBLIC_ROW_NAMES,
        PROJECT_ROOT,
        TableRow,
        check_action_manifest_version,
        check_checkpoint_manifest_version,
        check_docs,
        check_documented_dataclass_fields,
        check_documented_lsp_methods,
        check_local_links,
        markdown_files,
        table_rows,
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


def test_the_checked_files_end_with_the_changelog_and_the_issue_templates(tmp_path: Path) -> None:
    """The changelog and every issue template are read, in a fixed order.

    Asserting the templates as well as the changelog matters because they arrive
    through a glob: drop the glob and the changelog alone would still be there,
    with three files silently unread. The `config.yml` beside them is written to
    say that the glob takes Markdown and leaves the rest of the directory alone.
    """
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    templates = tmp_path / ".github" / "ISSUE_TEMPLATE"
    templates.mkdir(parents=True)
    for name in ("soundness.md", "bug.md", "feature.md"):
        (templates / name).write_text(f"# {name}\n", encoding="utf-8")
    (templates / "config.yml").write_text("blank_issues_enabled: false\n", encoding="utf-8")

    files = markdown_files(tmp_path)

    assert files[-4:] == (
        tmp_path / "CHANGELOG.md",
        templates / "bug.md",
        templates / "feature.md",
        templates / "soundness.md",
    )


def test_documentation_checker_reports_a_broken_link_in_the_changelog(tmp_path: Path) -> None:
    """A dead local link in the changelog is reported now that the file is read.

    Written against a changelog of its own rather than the shipped one. A code
    span that opens on one line and closes on the next swallows every line
    between it, links included, so a link injected into a long file can land
    somewhere nothing would ever read it and the cell would pass without
    checking anything. This fixture carries no backtick at all, which the
    assertion below states rather than leaves to inspection.
    """
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## 3.0.0\n\n- See [gone](docs/nowhere.md) before upgrading.\n",
        encoding="utf-8",
    )
    assert "`" not in changelog.read_text(encoding="utf-8")

    errors = check_local_links(tmp_path, (changelog,))

    assert len(errors) == 1
    assert "missing local link target: docs/nowhere.md" in errors[0]


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
    """Every `test_*` token the checked documents name, minus the test modules themselves.

    The scan follows the checker's own file set rather than `docs/` alone. A
    test named in `CONTRIBUTING.md` is then guarded here in its own right,
    instead of only for as long as some other document happens to name it too.

    A token that also names a file under `tests/` was matched inside a path such
    as `tests/test_python_source.py`; it names a module, not a function, and the
    census would report every one of them as missing without this exclusion.
    """
    tokens: set[str] = set()
    for document in markdown_files(root):
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


def test_a_public_surface_row_is_read_even_when_it_is_indented(tmp_path: Path) -> None:
    """An indented row still renders as a table row, so a stale one still has to be reported."""
    _write_field_parity_tree(
        tmp_path,
        "  | `InspectionNode` | One node. Fields: `label`, `name`. |",
    )

    errors = check_documented_dataclass_fields(tmp_path)

    assert len(errors) == 1
    assert "InspectionNode" in errors[0]
    assert "`kind`" in errors[0]


def test_a_row_indented_four_spaces_is_not_read_as_a_table_row(tmp_path: Path) -> None:
    """Four spaces of indent is an indented code block, and nobody renders it as a table.

    The row is the one the cell above reports, moved two spaces to the right:
    what changes is not the sentence but whether the document says it at all.
    """
    _write_field_parity_tree(
        tmp_path,
        "    | `InspectionNode` | One node. Fields: `label`, `name`. |",
    )

    assert check_documented_dataclass_fields(tmp_path) == ()


def test_a_row_listing_fields_for_an_unchecked_type_is_reported(tmp_path: Path) -> None:
    """A `Fields:` sentence nothing compares reads exactly like one that is compared.

    Without this, removing a type from the checked set leaves its sentence in
    place, unread and green, which is the failure the check exists to catch
    happening to the check itself.
    """
    _write_field_parity_tree(
        tmp_path,
        "| `CaptureInfo` | One capture. Fields: `path`, `digest`. |",
    )

    errors = check_documented_dataclass_fields(tmp_path)

    assert len(errors) == 1
    assert "CaptureInfo" in errors[0]


def _documented_integration_names(root: Path) -> set[str]:
    """Every name the integration contract's stable-surface rows carry, as the check reads it."""
    document = (root / "docs" / "integration-contract.md").read_text(encoding="utf-8")
    names: set[str] = set()
    for row in table_rows(document):
        if len(row.cells) == 2 and row.cells[0] in _PUBLIC_ROW_NAMES:
            names.update(_INLINE_CODE.findall(row.cells[1]))
    return names


def _kernel_public_surface_rows(root: Path) -> list[TableRow]:
    """Every data row of the kernel contract's public-surface tables, as the check reads it."""
    document = (root / "docs" / "kernel-contract.md").read_text(encoding="utf-8")
    return [
        row
        for row in table_rows(document)
        if row.section == "Public Surface"
        and len(row.cells) == 2
        and row.cells[0] not in {"Name", ""}
        and not set(row.cells[0]) <= {"-"}
    ]


def test_the_integration_contract_rows_still_carry_the_whole_documented_surface() -> None:
    """Say where a collapsed harvest came from: the row reader, not the document.

    The checks that compare these names with `__all__` do report a narrowed row
    reader -- they compare both directions, so fewer documented names arrive as
    undocumented exports. What they report is every name in `__all__`, which
    reads as a document that stopped listing its surface and invites the fix to
    be made in `docs/`. This says the harvest itself collapsed. It is also the
    only cover a later consumer would have if it compared these rows against
    something other than an exact set, because such a check reports less when it
    is given less. A floor rather than an equality: ordinary documentation edits
    move the number, and a collapse does not.
    """
    names = _documented_integration_names(PROJECT_ROOT)

    assert len(names) >= 90, f"expected the contract to name the whole surface, found {len(names)}"


def test_the_kernel_contract_public_surface_rows_are_still_found() -> None:
    """The same floor for the other table shape, where the names sit in the first cell.

    Counted as rows rather than names, so what it pins is the parser's own
    harvest rather than whatever a consumer goes on to read out of a cell.
    """
    rows = _kernel_public_surface_rows(PROJECT_ROOT)

    assert len(rows) >= 60, f"expected the public-surface tables to hold many rows, found {len(rows)}"


def _write_lsp_tree(
    root: Path,
    *,
    documented: tuple[str, ...],
    handled: tuple[str, ...],
    published: tuple[str, ...] = (),
    dispatch_name: str = "_dispatch_request",
) -> None:
    """Write the smallest tree the LSP method check reads: the server module and the reference.

    Every stub defines all three dispatch functions, whatever the cell is
    about. A function the check cannot find is an error in its own right, so a
    stub that left one out would be answering for that rather than for the
    disagreement between the matrix and the server.
    """
    package = root / "src" / "pyinc_tools"
    package.mkdir(parents=True, exist_ok=True)
    dispatch = [f"    def {dispatch_name}(self, method, params):"]
    for name in handled:
        dispatch.append(f'        if method == "{name}":')
        dispatch.append("            return method")
    dispatch.append("        return None")
    notifications = [f'        self._send_notification("{name}", {{}})' for name in published]
    (package / "lsp.py").write_text(
        '"""A stub language server."""\n'
        "\n"
        "\n"
        "class _Server:\n"
        "    def _handle_request(self, method, params):\n"
        "        return None\n"
        "\n" + "\n".join(dispatch) + "\n"
        "\n"
        "    def _handle_notification(self, method, params):\n"
        + "".join(f"{line}\n" for line in notifications)
        + "        return False\n"
        "\n"
        "    def _send_notification(self, method, params):\n"
        "        return None\n",
        encoding="utf-8",
    )
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "lsp-reference.md").write_text(
        "# LSP Reference\n"
        "\n"
        "## Method matrix\n"
        "\n"
        "| Method | Result | User-visible limits |\n"
        "|---|---|---|\n"
        + "".join(f"| `{name}` | A result. | A limit. |\n" for name in documented),
        encoding="utf-8",
    )


def test_lsp_method_check_reports_a_handled_method_the_matrix_omits(tmp_path: Path) -> None:
    _write_lsp_tree(
        tmp_path,
        documented=("initialize",),
        handled=("initialize", "textDocument/hover"),
    )

    errors = check_documented_lsp_methods(tmp_path, minimum=1)

    assert len(errors) == 1
    assert "undocumented methods: textDocument/hover" in errors[0]


def test_lsp_method_check_reports_a_documented_method_the_server_never_sees(
    tmp_path: Path,
) -> None:
    _write_lsp_tree(
        tmp_path,
        documented=("initialize", "textDocument/hover"),
        handled=("initialize",),
    )

    errors = check_documented_lsp_methods(tmp_path, minimum=1)

    assert len(errors) == 1
    assert "methods the server does not handle: textDocument/hover" in errors[0]


def test_lsp_method_check_accepts_a_matrix_naming_what_the_server_handles(
    tmp_path: Path,
) -> None:
    """The published notification counts as implemented: the server sends it unasked."""
    _write_lsp_tree(
        tmp_path,
        documented=("initialize", "textDocument/hover", "textDocument/publishDiagnostics"),
        handled=("initialize", "textDocument/hover"),
        published=("textDocument/publishDiagnostics",),
    )

    assert check_documented_lsp_methods(tmp_path, minimum=1) == ()


def test_lsp_method_check_reports_a_dispatch_function_it_cannot_find(tmp_path: Path) -> None:
    """A renamed dispatch chain must be loud, not read as a server handling less."""
    _write_lsp_tree(
        tmp_path,
        documented=("initialize", "textDocument/hover"),
        handled=("initialize", "textDocument/hover"),
        dispatch_name="_dispatch_request_under_another_name",
    )

    errors = check_documented_lsp_methods(tmp_path, minimum=1)

    assert len(errors) == 1
    assert "no _dispatch_request to read LSP methods from" in errors[0]


def test_lsp_method_check_reports_a_harvest_too_small_for_the_shipped_floor(
    tmp_path: Path,
) -> None:
    """The default floor is the guard against a walk that quietly stops matching.

    The matrix and the stub agree here, so the comparison alone would pass: what
    reports the two-method harvest is the floor and nothing else.
    """
    _write_lsp_tree(
        tmp_path,
        documented=("initialize", "textDocument/hover"),
        handled=("initialize", "textDocument/hover"),
    )

    errors = check_documented_lsp_methods(tmp_path)

    assert len(errors) == 1
    assert "found 2 LSP method strings, too few to compare" in errors[0]


def test_lsp_method_check_reports_a_missing_reference_instead_of_raising(
    tmp_path: Path,
) -> None:
    """A removed reference must not take every other check down with it."""
    _write_lsp_tree(
        tmp_path,
        documented=("initialize", "textDocument/hover"),
        handled=("initialize", "textDocument/hover"),
    )
    (tmp_path / "docs" / "lsp-reference.md").unlink()

    errors = check_documented_lsp_methods(tmp_path, minimum=1)

    assert len(errors) == 1
    assert "docs/lsp-reference.md" in errors[0]
    assert "missing document" in errors[0]
