from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.check_docs import (
        _INLINE_CODE,
        _PUBLIC_ROW_NAMES,
        PROJECT_ROOT,
        check_docs,
        check_documented_consumer_api,
        check_documented_lsp_methods,
        check_local_links,
        check_schema_versions,
        external_urls,
        markdown_files,
        table_rows,
    )
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from check_docs import (  # noqa: E402
        _INLINE_CODE,
        _PUBLIC_ROW_NAMES,
        PROJECT_ROOT,
        check_docs,
        check_documented_consumer_api,
        check_documented_lsp_methods,
        check_local_links,
        check_schema_versions,
        external_urls,
        markdown_files,
        table_rows,
    )


def test_documentation_checker_accepts_repository() -> None:
    errors = check_docs(PROJECT_ROOT)
    assert not errors, "\n".join(errors)


def test_every_documentation_check_is_registered_with_the_composed_checker() -> None:
    """A check the script defines but the composition never calls is a check that never runs."""
    source = PROJECT_ROOT / "scripts" / "check_docs.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        and node.name.startswith("check_")
        and node.name != check_docs.__name__
    }
    assert defined, "expected the checker to define checks to compose"

    unregistered = sorted(defined - set(check_docs.__code__.co_names))

    assert not unregistered, "checks the composition never calls: " + ", ".join(unregistered)


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


def test_a_link_pinned_to_the_project_version_is_checked_like_a_relative_one(tmp_path: Path) -> None:
    """A GitHub URL naming `main` or the project's own tag is a local link; any other ref is not."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "pyinc"\nversion = "1.2.3"\n', encoding="utf-8"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("# Present\n", encoding="utf-8")
    blob = "https://github.com/Brumbelow/pyinc/blob"
    readme = tmp_path / "README.md"
    readme.write_text(
        f"# Root\n\n[a]({blob}/v1.2.3/docs/guide.md#missing)\n[b]({blob}/main/docs/gone.md)\n"
        f"[c]({blob}/v0.9.0/docs/gone.md)\n",
        encoding="utf-8",
    )

    errors = check_local_links(tmp_path, (readme,))

    assert len(errors) == 2
    assert "missing anchor #missing" in errors[0]
    assert "missing local link target" in errors[1]


def test_the_checked_files_end_with_the_changelog_and_the_issue_templates(tmp_path: Path) -> None:
    """The changelog and every Markdown issue template are read, in a fixed order."""
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


def test_the_public_surface_tables_are_still_read() -> None:
    """A narrowed row reader would report the whole surface as undocumented; floors catch it."""
    kernel = (PROJECT_ROOT / "docs" / "kernel-contract.md").read_text(encoding="utf-8")
    kernel_rows = [
        row
        for row in table_rows(kernel)
        if row.section == "Public Surface"
        and len(row.cells) == 2
        and row.cells[0] not in {"Name", ""}
        and not set(row.cells[0]) <= {"-"}
    ]
    integration = (PROJECT_ROOT / "docs" / "integration-contract.md").read_text(encoding="utf-8")
    integration_names: set[str] = set()
    for row in table_rows(integration):
        if len(row.cells) == 2 and row.cells[0] in _PUBLIC_ROW_NAMES:
            integration_names.update(_INLINE_CODE.findall(row.cells[1]))

    assert len(kernel_rows) >= 60, f"found {len(kernel_rows)} kernel rows"
    assert len(integration_names) >= 90, f"found {len(integration_names)} integration names"


def test_every_advertised_name_resolves_on_the_package_that_advertises_it() -> None:
    """A name in `__all__` that the package does not define breaks `import *` at import time."""
    for module in ("pyinc", "pyinc.integrations", "pyinc_codegen", "pyinc_tools"):
        imported = importlib.import_module(module)
        unresolved = [name for name in imported.__all__ if not hasattr(imported, name)]
        assert not unresolved, f"{module}: {unresolved}"


def _write_schema_version_tree(root: Path, *, constant: int, documented: tuple[int, ...]) -> None:
    package = root / "src" / "pyinc"
    package.mkdir(parents=True, exist_ok=True)
    (package / "runtime.py").write_text(
        f"_CHECKPOINT_MANIFEST_VERSION = {constant}\n", encoding="utf-8"
    )
    (package / "action.py").write_text("_MANIFEST_VERSION = 3\n", encoding="utf-8")
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "kernel-contract.md").write_text(
        "# Kernel\n\n" + "".join(f"A manifest (schema v{v}).\n\n" for v in documented),
        encoding="utf-8",
    )
    (docs / "action-contract.md").write_text("# Action\n\nSchema v3 records.\n", encoding="utf-8")


def test_schema_version_check_reports_a_number_the_constant_does_not_decide(tmp_path: Path) -> None:
    _write_schema_version_tree(tmp_path, constant=9, documented=(8, 9))

    errors = check_schema_versions(tmp_path)

    assert len(errors) == 1
    assert "documents schema v8, but _CHECKPOINT_MANIFEST_VERSION is 9" in errors[0]


def test_schema_version_check_accepts_documents_that_agree_with_the_constant(tmp_path: Path) -> None:
    _write_schema_version_tree(tmp_path, constant=8, documented=(8, 8))

    assert check_schema_versions(tmp_path) == ()


def _write_lsp_tree(
    root: Path,
    *,
    documented: tuple[str, ...],
    handled: tuple[str, ...],
    published: tuple[str, ...] = (),
) -> None:
    """The smallest tree the LSP method check reads: the server module and the reference."""
    package = root / "src" / "pyinc_tools"
    package.mkdir(parents=True, exist_ok=True)
    dispatch = ["    def _dispatch_request(self, method, params):"]
    for name in handled:
        dispatch.append(f'        if method == "{name}":')
        dispatch.append("            return method")
    dispatch.append("        return None")
    notifications = [f'        self._send_notification("{name}", {{}})' for name in published]
    (package / "lsp.py").write_text(
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
        "# LSP Reference\n\n## Method matrix\n\n| Method | Result | User-visible limits |\n|---|---|---|\n"
        + "".join(f"| `{name}` | A result. | A limit. |\n" for name in documented),
        encoding="utf-8",
    )


def test_lsp_method_check_reports_disagreement_in_both_directions(tmp_path: Path) -> None:
    _write_lsp_tree(
        tmp_path,
        documented=("initialize", "textDocument/hover"),
        handled=("initialize", "textDocument/definition"),
    )

    errors = check_documented_lsp_methods(tmp_path, minimum=1)

    assert len(errors) == 2
    assert "undocumented methods: textDocument/definition" in errors[0]
    assert "methods the server does not handle: textDocument/hover" in errors[1]


def test_lsp_method_check_accepts_a_matrix_naming_what_the_server_handles_or_publishes(
    tmp_path: Path,
) -> None:
    _write_lsp_tree(
        tmp_path,
        documented=("initialize", "textDocument/hover", "textDocument/publishDiagnostics"),
        handled=("initialize", "textDocument/hover"),
        published=("textDocument/publishDiagnostics",),
    )

    assert check_documented_lsp_methods(tmp_path, minimum=1) == ()


def _write_consumer_surface_tree(
    root: Path,
    *,
    tools_rows: tuple[tuple[str, tuple[str, ...]], ...] = (("Entrypoints", ("WorkspaceSession",)),),
    tools_exports: tuple[str, ...] = ("WorkspaceSession",),
    codegen_rows: tuple[tuple[str, tuple[str, ...]], ...] = (("Entrypoints", ("generate",)),),
    codegen_exports: tuple[str, ...] = ("generate",),
) -> None:
    """The smallest tree the consumer surface check reads: both packages, both guides."""
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    for module, document, rows, exports in (
        ("pyinc_tools", "pyinc-tools-guide.md", tools_rows, tools_exports),
        ("pyinc_codegen", "codegen-guide.md", codegen_rows, codegen_exports),
    ):
        package = root / "src" / module
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text(
            "__all__ = [\n" + "".join(f'    "{name}",\n' for name in exports) + "]\n",
            encoding="utf-8",
        )
        (docs / document).write_text(
            f"# {module}\n\n## Public surface\n\n| Group | Names |\n|---|---|\n"
            + "".join(
                f"| {label} | " + ", ".join(f"`{name}`" for name in names) + " |\n"
                for label, names in rows
            ),
            encoding="utf-8",
        )


def test_consumer_surface_check_reports_disagreement_in_both_directions(tmp_path: Path) -> None:
    _write_consumer_surface_tree(
        tmp_path,
        tools_rows=(("Entrypoints", ("WorkspaceSession", "WorkspaceWatcher")),),
        tools_exports=("WorkspaceSession", "PollingWorkspaceWatcher"),
    )

    errors = check_documented_consumer_api(tmp_path)

    assert len(errors) == 2
    assert "docs/pyinc-tools-guide.md: undocumented exports: PollingWorkspaceWatcher" in errors[0]
    assert "docs/pyinc-tools-guide.md: names absent from __all__: WorkspaceWatcher" in errors[1]


def test_the_documents_name_many_external_links() -> None:
    """The scheduled link check asks about every address this collects; a narrowed harvest passes silently."""
    urls = external_urls(markdown_files(PROJECT_ROOT))

    distinct = set(urls)
    assert len(distinct) > 30, f"expected the documents to name many external URLs, found {len(distinct)}"
    assert all(url.startswith("http") for url in urls)
