from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from _hostile_paths import make_symlink_loop, nul_path, posix_only

import pyinc.integrations as integrations
from pyinc import Database, UnsupportedValueError
from pyinc.integrations import DocumentMap, SourcePosition, SourceRange, request_scope
from pyinc.integrations.python_source import source_text
from pyinc.integrations.scope_resolution import ScopeTree, scope_tree, symbol_at
from pyinc.integrations.symbol_resolution import (
    ClassMember,
    Parameter,
    ReferenceQueryResult,
    Signature,
    class_model,
    class_models_for_file,
    module_symbol_table,
    module_symbol_table_payload,
    resolved_class_model_payload,
    workspace_symbol_index,
)
from pyinc.integrations.symbol_resolution import (
    _resolve_symbol_payload as resolve_symbol_payload,
)
from pyinc.integrations.symbol_resolution import (
    find_references as find_references_by_id,
)
from pyinc.integrations.symbol_resolution import (
    resolve_qualified_name as resolve_symbol,
)

Operation = tuple[Literal["write", "delete"], str, str | None]


def find_references(
    db: Database,
    root: Path,
    path: Path,
    qualified_name: str,
    *,
    include_declaration: bool = True,
) -> ReferenceQueryResult:
    """Resolve the legacy fixture inputs through the v3 position API."""

    resolved = resolve_symbol(db, root, path, qualified_name)
    if resolved.defining_path is None or resolved.range is None:
        raise AssertionError(f"{qualified_name!r} does not resolve to a workspace symbol")
    symbol_id = symbol_at(
        db,
        root,
        resolved.defining_path,
        resolved.range.start,
    )
    assert symbol_id is not None
    return find_references_by_id(
        db,
        root,
        symbol_id,
        include_declaration=include_declaration,
    )


# ---------------------------------------------------------------------------
# Contract lock
# ---------------------------------------------------------------------------


def test_symbol_resolution_all_list_is_exact() -> None:
    from pyinc.integrations import symbol_resolution

    assert set(symbol_resolution.__all__) == {
        "ClassMember",
        "ClassModel",
        "ModuleSymbolTable",
        "Parameter",
        "Reference",
        "ReferenceQueryResult",
        "Signature",
        "Symbol",
        "WorkspaceSymbolEntry",
        "WorkspaceSymbolIndex",
        "class_model",
        "find_references",
        "module_symbol_table",
        "workspace_symbol_index",
    }


def test_symbol_resolution_stable_surface_on_integrations_namespace() -> None:
    for name in (
        "ClassMember",
        "ClassModel",
        "ModuleSymbolTable",
        "Parameter",
        "Reference",
        "ReferenceQueryResult",
        "Signature",
        "Symbol",
        "WorkspaceSymbolEntry",
        "WorkspaceSymbolIndex",
        "class_model",
        "find_references",
        "module_symbol_table",
        "workspace_symbol_index",
    ):
        assert hasattr(integrations, name)
    assert not hasattr(integrations, "resolve_symbol")
    assert not hasattr(integrations, "ResolvedSymbol")


@pytest.mark.parametrize("name", ["ResolvedSymbol", "resolve_symbol"])
def test_v2_resolver_names_cannot_be_imported_from_submodule(name: str) -> None:
    from pyinc.integrations import symbol_resolution

    assert not hasattr(symbol_resolution, name)
    with pytest.raises(ImportError):
        exec(f"from pyinc.integrations.symbol_resolution import {name}", {})


def test_symbol_resolution_payload_helpers_are_not_re_exported() -> None:
    assert not hasattr(integrations, "module_symbol_table_payload")
    assert not hasattr(integrations, "module_symbol_table_for_module")
    assert not hasattr(integrations, "resolve_symbol_payload")
    assert not hasattr(integrations, "workspace_symbol_index_payload")
    assert not hasattr(integrations, "name_occurrences_for_file")
    assert not hasattr(integrations, "workspace_name_occurrence_index")
    assert not hasattr(integrations, "find_references_payload")
    assert not hasattr(integrations, "class_models_for_file")
    assert not hasattr(integrations, "resolved_class_model_payload")


# ---------------------------------------------------------------------------
# Per-module table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_module_symbol_table_captures_top_level_symbols(mode: str, tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "sample.py"
    path.write_text(
        "import os\n"
        "from pkg.sub import thing\n"
        "\n"
        "def alpha():\n"
        "    return 1\n"
        "\n"
        "async def beta(x: int) -> int:\n"
        "    return x\n"
        "\n"
        "class Gamma:\n"
        "    value: int = 0\n"
        "    tag = 'static'\n"
        "\n"
        "    def method(self, y: str) -> None:\n"
        "        self.value = len(y)\n"
        "\n"
        "    class Inner:\n"
        "        def nested(self) -> int:\n"
        "            return 2\n"
        "\n"
        "label: str = 'top'\n"
        "count = 42\n",
        encoding="utf-8",
    )

    table = module_symbol_table(Database(mode=mode), root, path)
    by_qname = {sym.qualified_name: sym for sym in table.symbols}

    assert table.module == "sample"
    assert table.path == str(path)
    assert table.impurity_reasons == ()

    assert by_qname["os"].kind == "import_alias"
    assert by_qname["os"].import_source_module == "os"
    assert by_qname["thing"].kind == "from_import_alias"
    assert by_qname["thing"].import_source_module == "pkg.sub"
    assert by_qname["thing"].import_source_name == "thing"

    assert by_qname["alpha"].kind == "function"
    assert by_qname["alpha"].range.start.line == 3
    assert by_qname["beta"].kind == "function"
    assert by_qname["beta"].range.start.line == 6
    assert by_qname["beta"].signature == Signature(
        parameters=(Parameter(name="x", annotation="int"),),
        return_annotation="int",
    )

    assert by_qname["Gamma"].kind == "class"
    assert by_qname["Gamma"].range.start.line == 9
    assert by_qname["Gamma.value"].kind == "class_variable"
    assert by_qname["Gamma.value"].annotation == "int"
    assert by_qname["Gamma.tag"].kind == "class_variable"
    assert by_qname["Gamma.method"].kind == "method"
    assert by_qname["Gamma.method"].signature == Signature(
        parameters=(
            Parameter(name="self", annotation=None),
            Parameter(name="y", annotation="str"),
        ),
        return_annotation="None",
    )
    assert by_qname["Gamma.Inner"].kind == "class"
    assert by_qname["Gamma.Inner.nested"].kind == "method"

    assert by_qname["label"].kind == "variable"
    assert by_qname["label"].annotation == "str"
    assert by_qname["count"].kind == "variable"


def test_module_symbol_table_rejects_out_of_workspace_path(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")

    db = Database(mode="strict")
    table = module_symbol_table(db, root, outside)
    assert table.module == ""
    assert table.path == str(outside)
    assert table.symbols == ()
    assert table.impurity_reasons == ("not in workspace",)


def test_module_symbol_table_reports_syntax_error(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "broken.py"
    path.write_text("def broken(\n", encoding="utf-8")

    db = Database(mode="strict")
    table = module_symbol_table(db, root, path)
    assert table.symbols == ()
    assert "syntax error" in table.impurity_reasons


# ---------------------------------------------------------------------------
# Annotation text extraction
# ---------------------------------------------------------------------------


def test_annotation_text_preserves_complex_type_forms(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "ann.py"
    path.write_text(
        "from typing import Callable\n"
        "\n"
        "def alpha(xs: list[int]) -> int: ...\n"
        "def beta(x: 'Foo | None') -> None: ...\n"
        "def gamma(fn: Callable[[int], str]) -> str: ...\n"
        "def delta(*args: int, **kwargs: str) -> None: ...\n"
        "\u00e9label: str = 'unicode'\n",
        encoding="utf-8",
    )
    db = Database(mode="strict")
    table = module_symbol_table(db, root, path)
    by_qname = {sym.qualified_name: sym for sym in table.symbols}

    assert by_qname["alpha"].signature is not None
    assert by_qname["alpha"].signature.parameters[0].annotation == "list[int]"
    assert by_qname["alpha"].signature.return_annotation == "int"

    beta_sig = by_qname["beta"].signature
    assert beta_sig is not None
    assert beta_sig.parameters[0].annotation == "'Foo | None'"

    gamma_sig = by_qname["gamma"].signature
    assert gamma_sig is not None
    assert gamma_sig.parameters[0].annotation == "Callable[[int], str]"

    delta_sig = by_qname["delta"].signature
    assert delta_sig is not None
    assert delta_sig.parameters[0].name == "*args"
    assert delta_sig.parameters[0].annotation == "int"
    assert delta_sig.parameters[1].name == "**kwargs"
    assert delta_sig.parameters[1].annotation == "str"

    assert by_qname["\u00e9label"].annotation == "str"


# ---------------------------------------------------------------------------
# Cross-module resolution (happy path)
# ---------------------------------------------------------------------------


def test_resolve_symbol_follows_from_import_to_definition(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    a = root / "a.py"
    b = root / "b.py"
    a.write_text("def foo() -> int:\n    return 1\n", encoding="utf-8")
    b.write_text("from a import foo\n", encoding="utf-8")

    db = Database(mode="strict")
    resolved = resolve_symbol(db, root, b, "foo")

    assert resolved.resolution == "workspace"
    assert resolved.defining_module == "a"
    assert resolved.defining_path == str(a)
    assert resolved.range is not None
    assert resolved.range.start.line == 0
    assert resolved.trail == ("b:foo", "a:foo")
    assert resolved.follow_depth == 1


def test_resolve_symbol_terminates_on_plain_import(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    consumer = root / "main.py"
    consumer.write_text("import pkg\n", encoding="utf-8")

    db = Database(mode="strict")
    resolved = resolve_symbol(db, root, consumer, "pkg")

    assert resolved.resolution == "workspace"
    assert resolved.defining_module == "pkg"
    assert resolved.defining_path == str(pkg / "__init__.py")
    assert resolved.range is None


def test_resolve_symbol_follows_submodule_import_as_module(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    helper = pkg / "helper.py"
    helper.write_text("def util() -> int:\n    return 1\n", encoding="utf-8")
    consumer = root / "main.py"
    consumer.write_text("from pkg import helper\n", encoding="utf-8")

    db = Database(mode="strict")
    resolved = resolve_symbol(db, root, consumer, "helper")

    assert resolved.resolution == "workspace"
    assert resolved.defining_module == "pkg.helper"
    assert resolved.defining_path == str(helper)
    assert resolved.range is None


def test_resolve_symbol_returns_missing_when_name_absent(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("def foo() -> int:\n    return 1\n", encoding="utf-8")
    db = Database(mode="strict")
    resolved = resolve_symbol(db, root, root / "a.py", "does_not_exist")
    assert resolved.resolution == "missing"


def test_resolve_symbol_rejects_path_outside_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("def thing() -> int:\n    return 1\n", encoding="utf-8")

    db = Database(mode="strict")
    resolved = resolve_symbol(db, root, outside, "thing")
    assert resolved.resolution == "missing"
    assert resolved.follow_depth == 0


# ---------------------------------------------------------------------------
# Chain depth + cycle
# ---------------------------------------------------------------------------


def test_resolve_symbol_follows_three_module_chain(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("def target() -> int:\n    return 1\n", encoding="utf-8")
    (root / "b.py").write_text("from a import target\n", encoding="utf-8")
    (root / "c.py").write_text("from b import target\n", encoding="utf-8")

    db = Database(mode="strict")
    resolved = resolve_symbol(db, root, root / "c.py", "target")

    assert resolved.resolution == "workspace"
    assert resolved.defining_module == "a"
    assert resolved.range is not None
    assert resolved.range.start.line == 0
    assert resolved.follow_depth == 2
    assert resolved.trail == ("c:target", "b:target", "a:target")


def test_resolve_symbol_detects_cycle_between_two_modules(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("from b import target\n", encoding="utf-8")
    (root / "b.py").write_text("from a import target\n", encoding="utf-8")

    db = Database(mode="strict")
    resolved = resolve_symbol(db, root, root / "a.py", "target")

    assert resolved.resolution == "ambiguous"
    assert resolved.defining_module is None
    assert resolved.follow_depth >= 1


# ---------------------------------------------------------------------------
# Depth cap
# ---------------------------------------------------------------------------


def test_resolve_symbol_depth_cap_terminates_at_max_follow_depth(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "m0.py").write_text("def target() -> int:\n    return 1\n", encoding="utf-8")
    for i in range(1, 10):
        (root / f"m{i}.py").write_text(f"from m{i - 1} import target\n", encoding="utf-8")

    db = Database(mode="strict")
    resolved = resolve_symbol(db, root, root / "m9.py", "target")

    assert resolved.resolution == "ambiguous"
    assert resolved.follow_depth == 8


# ---------------------------------------------------------------------------
# Wildcard resolution
# ---------------------------------------------------------------------------


def test_resolve_symbol_follows_wildcard_export_with_static_all(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "provider.py"
    consumer = root / "consumer.py"
    provider.write_text(
        "def shown() -> int:\n"
        "    return 1\n"
        "def _hidden() -> int:\n"
        "    return 2\n"
        "__all__ = ['shown']\n",
        encoding="utf-8",
    )
    consumer.write_text("from provider import *\n", encoding="utf-8")

    db = Database(mode="strict")
    resolved = resolve_symbol(db, root, consumer, "shown")

    assert resolved.resolution == "workspace"
    assert resolved.defining_module == "provider"
    assert resolved.range is not None
    assert resolved.range.start.line == 0
    assert resolved.trail == ("consumer:shown", "provider:shown")


def test_resolve_symbol_with_dynamic_all_is_ambiguous(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "provider.py"
    consumer = root / "consumer.py"
    provider.write_text(
        "def shown() -> int:\n    return 1\n__all__ = ['shown']\n__all__ += ['extra']\n",
        encoding="utf-8",
    )
    consumer.write_text("from provider import *\n", encoding="utf-8")

    db = Database(mode="strict")
    resolved = resolve_symbol(db, root, consumer, "missing_name")

    assert resolved.resolution == "ambiguous"
    inspection = db.inspect(resolve_symbol_payload, str(root), str(consumer), "missing_name")
    assert inspection.is_untracked


# ---------------------------------------------------------------------------
# Installed boundary
# ---------------------------------------------------------------------------


def test_resolve_symbol_stops_at_stdlib_boundary(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    consumer = root / "consumer.py"
    consumer.write_text("from json import JSONDecoder\n", encoding="utf-8")

    db = Database(mode="strict")
    resolved = resolve_symbol(db, root, consumer, "JSONDecoder")

    assert resolved.resolution == "stdlib"
    assert resolved.distribution_name is None
    assert resolved.defining_path is None


def test_resolve_symbol_stops_at_installed_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site-packages"
    site.mkdir()
    pkg_dir = site / "fake_installed"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")

    dist_info = site / "fake_installed-1.2.3.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: fake_installed\nVersion: 1.2.3\nSummary: Fake\n",
        encoding="utf-8",
    )
    (dist_info / "top_level.txt").write_text("fake_installed\n", encoding="utf-8")

    monkeypatch.setattr(
        "pyinc.integrations.installed_packages._get_site_packages_dirs",
        lambda: (str(site),),
    )
    monkeypatch.setattr(
        "pyinc.integrations.deep_module_resolution._get_sys_path_entries",
        lambda: (str(site),),
    )

    root = tmp_path / "workspace"
    root.mkdir()
    consumer = root / "consumer.py"
    consumer.write_text("from fake_installed import VALUE\n", encoding="utf-8")

    db = Database(mode="strict")
    resolved = resolve_symbol(db, root, consumer, "VALUE")

    assert resolved.resolution == "installed"
    assert resolved.distribution_name == "fake_installed"
    assert resolved.distribution_version == "1.2.3"
    assert resolved.defining_path is None


# ---------------------------------------------------------------------------
# Backdating
# ---------------------------------------------------------------------------


def test_comment_only_edit_backdates_symbol_table(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "a.py"
    path.write_text("def foo() -> int:\n    return 1\n", encoding="utf-8")

    db = Database(mode="strict")
    first = module_symbol_table(db, root, path)
    first_changed = db.inspect(module_symbol_table_payload, str(path)).changed_at

    path.write_text("def foo() -> int:\n    return 1\n# trailing\n", encoding="utf-8")
    second = module_symbol_table(db, root, path)

    assert first == second
    # `last_recompute` is the discriminating half: `changed_at` is unmoved
    # whether or not the read below this node compares by a coarser token, so
    # the marker is what says the payload -- not the read -- absorbed the edit.
    record = db.inspect(module_symbol_table_payload, str(path))
    assert record.last_recompute == "backdated", (
        f"last_recompute={record.last_recompute} | an equal table across a "
        "comment-only edit has to be backdated"
    )
    assert record.changed_at == first_changed, (
        f"changed_at={record.changed_at} before={first_changed} | the table "
        "moved under an edit it does not carry"
    )


def test_signature_change_triggers_downstream_reresolution(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    a = root / "a.py"
    b = root / "b.py"
    a.write_text("def foo() -> int:\n    return 1\n", encoding="utf-8")
    b.write_text("from a import foo\n", encoding="utf-8")

    db = Database(mode="strict")
    first = resolve_symbol(db, root, b, "foo")
    assert first.resolution == "workspace"

    a.write_text(
        "# spacer\ndef foo(x: int) -> int:\n    return x\n",
        encoding="utf-8",
    )
    second = resolve_symbol(db, root, b, "foo")

    assert second.range is not None
    assert second.range.start.line == 1
    assert db.inspect(module_symbol_table_payload, str(a)).last_recompute == "executed"
    assert db.inspect(resolve_symbol_payload, str(root), str(b), "foo").last_recompute == "executed"


def _module_scope_end(tree: ScopeTree) -> SourcePosition:
    # The module scope is the one scope without a parent. `scopes` carries no
    # documented ordering, so indexing it would be an assumption, not a lookup.
    return next(scope for scope in tree.scopes if scope.parent_id is None).range.end


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_comment_edit_scope_tree_matches_fresh(mode: str, tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")

    incremental = Database(mode=mode)
    scope_tree(incremental, path)

    path.write_text("x = 1\n# trailing comment\n", encoding="utf-8")
    warm = scope_tree(incremental, path)
    fresh = scope_tree(Database(mode=mode), path)

    assert warm == fresh, (
        f"mode={mode} | warm_end={_module_scope_end(warm)} "
        f"fresh_end={_module_scope_end(fresh)} | "
        "module scope ends before a comment the same database already reads"
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_blank_line_edit_scope_tree_matches_fresh(mode: str, tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")

    incremental = Database(mode=mode)
    scope_tree(incremental, path)

    path.write_text("x = 1\n\n\n", encoding="utf-8")
    warm = scope_tree(incremental, path)
    fresh = scope_tree(Database(mode=mode), path)

    assert warm == fresh, (
        f"mode={mode} | warm_end={_module_scope_end(warm)} "
        f"fresh_end={_module_scope_end(fresh)} | "
        "module scope ends before blank lines the same database already reads"
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_trailing_space_edit_scope_tree_matches_fresh(mode: str, tmp_path: Path) -> None:
    # Neither document ends in a newline, and that is what makes the cell
    # discriminating. `DocumentMap` splits on `\r\n?|\n`, so a file that does end
    # in one carries a trailing empty element and the module scope ends at
    # `(line, 0)` whatever spaces precede it -- written as `x = 1\n` -> `x = 1   \n`
    # the two ends are equal before the fix and the cell proves nothing.
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "a.py"
    path.write_text("x = 1", encoding="utf-8")

    incremental = Database(mode=mode)
    scope_tree(incremental, path)

    path.write_text("x = 1   ", encoding="utf-8")
    warm = scope_tree(incremental, path)
    fresh = scope_tree(Database(mode=mode), path)

    assert warm == fresh, (
        f"mode={mode} | warm_end={_module_scope_end(warm)} "
        f"fresh_end={_module_scope_end(fresh)} | "
        "module scope ends before trailing space the same database already reads"
    )


# ---------------------------------------------------------------------------
# Conditional top-level
# ---------------------------------------------------------------------------


def test_conditional_top_level_binding_marked_impure(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "cond.py"
    path.write_text(
        "import sys\n"
        "if sys.version_info >= (3, 12):\n"
        "    x: int = 1\n"
        "def visible() -> int:\n"
        "    return 2\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    table = module_symbol_table(db, root, path)
    qnames = {sym.qualified_name for sym in table.symbols}
    assert "visible" in qnames
    assert "x" not in qnames
    assert "conditional top-level binding" in table.impurity_reasons


def test_type_checking_imports_visible_in_symbol_table(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "helper.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
    path = root / "consumer.py"
    path.write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from helper import Foo\n"
        "    import os\n"
        "def visible() -> int:\n"
        "    return 1\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    table = module_symbol_table(db, root, path)
    qnames = {sym.qualified_name for sym in table.symbols}
    assert "Foo" in qnames
    assert "os" in qnames
    assert "visible" in qnames
    assert "conditional top-level binding" not in table.impurity_reasons

    foo_sym = next(s for s in table.symbols if s.qualified_name == "Foo")
    assert foo_sym.kind == "from_import_alias"
    assert foo_sym.import_source_module == "helper"
    assert foo_sym.import_source_name == "Foo"

    os_sym = next(s for s in table.symbols if s.qualified_name == "os")
    assert os_sym.kind == "import_alias"
    assert os_sym.import_source_module == "os"


def test_type_checking_typing_dot_form(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "mod.py"
    path.write_text(
        "import typing\nif typing.TYPE_CHECKING:\n    from helper import Bar\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    table = module_symbol_table(db, root, path)
    qnames = {sym.qualified_name for sym in table.symbols}
    assert "Bar" in qnames
    assert "conditional top-level binding" not in table.impurity_reasons


def test_type_checking_block_with_other_conditional_records_impurity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "mixed.py"
    path.write_text(
        "import sys\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from helper import Foo\n"
        "if sys.version_info >= (3, 12):\n"
        "    x: int = 1\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    table = module_symbol_table(db, root, path)
    qnames = {sym.qualified_name for sym in table.symbols}
    assert "Foo" in qnames
    assert "x" not in qnames
    assert "conditional top-level binding" in table.impurity_reasons


def test_type_checking_block_non_import_stmts_not_collected(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "mod.py"
    path.write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    def hidden() -> int:\n"
        "        return 1\n"
        "def visible() -> int:\n"
        "    return 2\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    table = module_symbol_table(db, root, path)
    qnames = {sym.qualified_name for sym in table.symbols}
    assert "visible" in qnames
    assert "hidden" not in qnames
    assert "conditional top-level binding" not in table.impurity_reasons


def test_resolve_symbol_from_type_checking_import(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "models.py").write_text("class User:\n    pass\n", encoding="utf-8")
    path = root / "service.py"
    path.write_text(
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from models import User\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    resolved = resolve_symbol(db, root, path, "User")
    assert resolved.resolution == "workspace"
    assert resolved.defining_module == "models"
    assert resolved.qualified_name == "User"


# ---------------------------------------------------------------------------
# Workspace index
# ---------------------------------------------------------------------------


def test_workspace_symbol_index_flattens_and_sorts(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "helper.py").write_text(
        "def util() -> int:\n    return 1\nclass Holder:\n    pass\n",
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        "from pkg.helper import util\nflag: bool = True\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    idx = workspace_symbol_index(db, root)
    qnames = [(entry.module, entry.qualified_name) for entry in idx.entries]

    assert ("main", "flag") in qnames
    assert ("main", "util") in qnames
    assert ("pkg.helper", "Holder") in qnames
    assert ("pkg.helper", "util") in qnames
    # sorted ascending
    assert qnames == sorted(qnames)


# ---------------------------------------------------------------------------
# From-scratch consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_workspace_symbol_index_matches_fresh_recomputation_over_edits(
    mode: str, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    steps: tuple[Operation, ...] = (
        ("write", "a.py", "def foo() -> int:\n    return 1\n"),
        ("write", "b.py", "from a import foo\n"),
        ("write", "a.py", "def foo() -> int:\n    return 2\n"),
        ("write", "a.py", "def foo(x: int) -> int:\n    return x\n"),
        ("write", "a.py", "def foo(x: int) -> int:\n    return x\n__all__ = ['foo']\n"),
        ("delete", "b.py", None),
        ("write", "a.py", "def bar() -> int:\n    return 1\n"),
    )

    incremental = Database(mode=mode)
    for operation, name, content in steps:
        target = root / name
        if operation == "write":
            assert content is not None
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        else:
            target.unlink()

        fresh = Database(mode=mode)
        assert workspace_symbol_index(incremental, root) == workspace_symbol_index(fresh, root)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_resolve_symbol_matches_fresh_recomputation_over_edits(mode: str, tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    a = root / "a.py"
    b = root / "b.py"
    a.write_text("def foo() -> int:\n    return 1\n", encoding="utf-8")
    b.write_text("from a import foo\n", encoding="utf-8")

    incremental = Database(mode=mode)
    contents_a = (
        "def foo() -> int:\n    return 1\n",
        "def foo() -> int:\n    return 2\n",
        "def foo(x: int) -> int:\n    return x\n",
        "def bar() -> int:\n    return 1\n",
    )
    for content in contents_a:
        a.write_text(content, encoding="utf-8")
        fresh = Database(mode=mode)
        assert resolve_symbol(incremental, root, b, "foo") == resolve_symbol(fresh, root, b, "foo")


# The repo's own `addopts = "-q --tb=no"` allows one line per failure, and both
# node ids below are longer than that line on their own: measured, a red in
# either prints `FAILED <node id>` and none of the message beneath it. The
# messages are still written single-line and discriminator-first, and reading
# one off a failure means re-running that node with `-o addopts="" --tb=long`.


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_scope_tree_matches_fresh_recomputation_over_edits(mode: str, tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "a.py"

    incremental = Database(mode=mode)
    contents = (
        # Every neighbouring pair below parses to the same tree while the
        # document geometry moves, except at the two steps marked as controls.
        # The controls are what keep the row from exercising only the shapes a
        # parse cannot see.
        "def foo() -> int:\n    return 1\n",
        "def foo() -> int:\n    return 1\n# trailing comment\n",
        "def foo() -> int:\n    return 1\n",
        "def foo() -> int:\n    return 1\n\n\n",
        "def foo() -> int:\n    return 2\n",  # control: the body changes
        "def foo() -> int:\n    return 2",
        # Neither this document nor the one above it ends in a newline, and
        # that is what makes the step discriminating: `DocumentMap` splits on
        # `\r\n?|\n`, so a document that does end in one carries a trailing
        # empty element and the module scope ends at `(line, 0)` whatever
        # spaces precede it.
        "def foo() -> int:\n    return 2   ",
        "def foo(x: int) -> int:\n    return x   ",  # control: the signature changes
    )
    for content in contents:
        path.write_text(content, encoding="utf-8")
        warm = scope_tree(incremental, path)
        fresh = scope_tree(Database(mode=mode), path)
        assert warm == fresh, (
            f"mode={mode} | warm_end={_module_scope_end(warm)} "
            f"fresh_end={_module_scope_end(fresh)} | content={content!r} | "
            "the module scope of a warm tree ends elsewhere than a fresh one"
        )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_scope_tree_matches_the_text_it_is_built_from(mode: str, tmp_path: Path) -> None:
    # The pair guarded here is (lexical, source): the ranges memo is keyed on a
    # scope tree and a source text read within one request, which is what the
    # `request_scope` below reproduces. A value-level divergence between the
    # two is not reachable today -- an exhaustive position sweep over six
    # parse-invisible edit shapes in three modes, 18 sweeps, found 0
    # divergences, because the module scope's end is the only range derived
    # from document geometry and the range builder derives every range it
    # returns from the parse -- bindings, plus star-import aliases -- so the
    # range that moves never reaches an answer. What is reachable is the pair
    # being drawn from two revisions of the file, which is what comparing the
    # tree against the text this same request returned rules out. Comparing
    # that text against the bytes on disk would not: it matches either way,
    # because the fresh snapshot is stored before the comparison decides
    # anything.
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")

    db = Database(mode=mode)
    scope_tree(db, path)

    path.write_text("x = 1\n# trailing comment\n", encoding="utf-8")
    with request_scope(db):
        text = source_text(db, str(path))
        document = DocumentMap(text)
        implied_end = SourcePosition(len(document.lines) - 1, len(document.lines[-1]))
        tree_end = _module_scope_end(scope_tree(db, path))

    assert tree_end == implied_end, (
        f"mode={mode} | tree_end={tree_end} text_implies={implied_end} | "
        f"text={text!r} | the tree and the text read beside it come from two "
        "revisions of the file"
    )


# ---------------------------------------------------------------------------
# Self-smoke against this repo
# ---------------------------------------------------------------------------


def test_workspace_symbol_index_against_own_source_tree() -> None:
    repo_src = Path(__file__).resolve().parent.parent / "src" / "pyinc"
    if not repo_src.exists():
        pytest.skip("source tree not available in this run")

    db = Database(mode="strict")
    idx = workspace_symbol_index(db, repo_src)
    modules = {entry.module for entry in idx.entries}
    assert "integrations.symbol_resolution" in modules
    assert any(
        entry.qualified_name == "SymbolId" and entry.module == "integrations.scope_resolution"
        for entry in idx.entries
    )
    assert not any(entry.qualified_name == "ResolvedSymbol" for entry in idx.entries)


# ---------------------------------------------------------------------------
# find_references
# ---------------------------------------------------------------------------


def test_find_references_returns_declaration_and_call_site(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    target.write_text(
        "def foo() -> int:\n    return 1\n\nfoo()\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    result = find_references(db, root, target, "foo")

    assert result.target.path == str(target)
    assert result.target.name == "foo"
    assert result.target.declaration.start.line == 0
    assert len(result.references) == 2
    declaration = next(r for r in result.references if r.is_declaration)
    assert declaration.range.start.line == 0
    assert declaration.path == str(target)
    call = next(r for r in result.references if not r.is_declaration)
    assert call.range.start.line == 3
    assert call.range.start.character == 0
    assert call.range.end.character == 3


def test_find_references_excludes_declaration_when_flag_false(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    target.write_text(
        "def foo() -> int:\n    return 1\n\nfoo()\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    result = find_references(db, root, target, "foo", include_declaration=False)

    assert all(not r.is_declaration for r in result.references)
    assert len(result.references) == 1
    assert result.references[0].range.start.line == 3


def test_find_references_crosses_re_export(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text(
        "def foo() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    b = root / "b.py"
    b.write_text("from a import foo\n\nfoo()\nfoo()\n", encoding="utf-8")

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "foo")

    assert result.target.path == str(root / "a.py")
    assert result.target.name == "foo"
    call_sites = sorted(
        (r for r in result.references if not r.is_declaration),
        key=lambda r: (r.path, r.range.start),
    )
    assert [(Path(r.path).name, r.range.start.line) for r in call_sites] == [
        ("b.py", 2),
        ("b.py", 3),
    ]
    declarations = [r for r in result.references if r.is_declaration]
    assert len(declarations) == 1
    assert Path(declarations[0].path).name == "a.py"


def test_find_references_resolves_attribute_chain_on_imported_module(
    tmp_path: Path,
) -> None:
    """``import a; a.foo()`` is counted as a reference to ``foo``: the
    occurrence walker remembers that ``foo`` is the rightmost attribute of a
    ``Name``-LHS access, and the verifier resolves the LHS through the
    ``import_alias`` to the target's defining module before checking the
    attribute name. Only the ``foo`` portion is reported, with offsets that
    point at the attribute span (not the leading ``a.``)."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text(
        "def foo() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    (root / "b.py").write_text("import a\n\na.foo()\n", encoding="utf-8")

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    assert len(non_decl) == 1
    ref = non_decl[0]
    assert ref.range.start.line == 2
    # ``a.foo()`` — the ``foo`` portion is at cols 2-5.
    assert (ref.range.start.character, ref.range.end.character) == (2, 5)


def test_find_references_resolves_attribute_chain_on_aliased_import(
    tmp_path: Path,
) -> None:
    """``import a as alias`` followed by ``alias.foo()`` is counted: the LHS
    ``alias`` resolves through ``import_alias`` (with `import_source_module="a"`)
    to module ``a``."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text(
        "def foo() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    (root / "b.py").write_text("import a as alias\n\nalias.foo()\n", encoding="utf-8")

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    assert len(non_decl) == 1
    ref = non_decl[0]
    assert ref.range.start.line == 2
    # ``alias.foo()`` — the ``foo`` portion is at cols 6-9.
    assert (ref.range.start.character, ref.range.end.character) == (6, 9)


def test_find_references_attribute_chain_through_module_re_export(
    tmp_path: Path,
) -> None:
    """When the LHS module re-exports the target via ``from c import foo``,
    ``M.foo()`` still resolves: the verifier resolves ``foo`` *inside* the LHS
    module (so it follows ``from_import_alias`` to the original definition)."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "c.py").write_text(
        "def foo() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    (root / "m.py").write_text("from c import foo\n", encoding="utf-8")
    (root / "b.py").write_text("import m\n\nm.foo()\n", encoding="utf-8")

    db = Database(mode="strict")
    result = find_references(db, root, root / "c.py", "foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    # The `from c import foo` line in m.py is not represented as a Name in
    # the AST (it's an ast.alias under ast.ImportFrom), so it contributes no
    # occurrence. The reference comes solely from ``m.foo()`` in b.py — the
    # verifier resolves ``m`` to module m, then resolves ``foo`` inside m,
    # which follows the from-import re-export back to the canonical c.foo.
    assert len(non_decl) == 1
    ref = non_decl[0]
    assert ref.path.endswith("b.py")
    assert ref.range.start.line == 2
    assert (ref.range.start.character, ref.range.end.character) == (2, 5)


def test_find_references_does_not_match_attribute_on_unrelated_module(
    tmp_path: Path,
) -> None:
    """``import other; other.foo()`` is NOT a reference to ``a.foo`` even when
    ``other`` also defines a top-level ``foo`` — the verification resolves
    ``foo`` inside ``other``'s module and the resulting definition site doesn't
    match the search target."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text(
        "def foo() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    (root / "other.py").write_text(
        "def foo() -> int:\n    return 99\n",
        encoding="utf-8",
    )
    (root / "b.py").write_text("import other\n\nother.foo()\n", encoding="utf-8")

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    assert non_decl == []


def test_find_references_skips_attribute_on_non_module_local(
    tmp_path: Path,
) -> None:
    """``x = SomeClass(); x.foo()`` is not a reference to a module-level
    ``foo`` — the LHS ``x`` resolves to a definition site (not a module), so
    the verifier ignores the attribute access (no false positive on the
    rightmost attr name)."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text(
        "def foo() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    (root / "b.py").write_text("x = 1\n\nx.foo\n", encoding="utf-8")

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    assert non_decl == []


def test_find_references_resolves_proven_nested_module_chain(
    tmp_path: Path,
) -> None:
    """A directly imported nested module proves each receiver component."""
    root = tmp_path / "workspace"
    root.mkdir()
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "subpkg.py").write_text("def foo() -> int:\n    return 1\n", encoding="utf-8")
    (root / "b.py").write_text("import pkg.subpkg\n\npkg.subpkg.foo()\n", encoding="utf-8")

    db = Database(mode="strict")
    result = find_references(db, root, pkg / "subpkg.py", "foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    assert len(non_decl) == 1
    assert non_decl[0].range == SourceRange(SourcePosition(2, 11), SourcePosition(2, 14))


def test_find_references_excludes_shadowing_locals(
    tmp_path: Path,
) -> None:
    """A local binding is distinct from the module symbol with the same name."""
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    target.write_text(
        "def foo() -> int:\n    return 1\n\ndef other() -> int:\n    foo = 42\n    return foo\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    result = find_references(db, root, target, "foo")

    lines = sorted(r.range.start.line for r in result.references if not r.is_declaration)
    assert lines == []


def test_find_references_includes_forward_ref_strings_in_param_and_return_annotation(
    tmp_path: Path,
) -> None:
    """Forward-reference strings in parameter and return annotations are
    parsed and walked; the inner names are reported as references with
    offsets that point inside the quotes."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
    b = root / "b.py"
    b.write_text(
        "from a import Foo\n\ndef g(a: 'Foo') -> 'Foo':\n    return a\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "Foo")

    non_decl = sorted(
        (r for r in result.references if not r.is_declaration),
        key=lambda r: (r.path, r.range.start),
    )
    assert len(non_decl) == 2
    # `def g(a: 'Foo') -> 'Foo':` — opening quotes at col 9 and 19.
    param_ref, return_ref = non_decl
    assert param_ref.range.start.line == 2
    assert (param_ref.range.start.character, param_ref.range.end.character) == (10, 13)
    assert return_ref.range.start.line == 2
    assert (return_ref.range.start.character, return_ref.range.end.character) == (20, 23)


def test_forward_ref_ranges_after_unicode_prefix_use_codepoint_columns(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "a.py"
    target.write_text("class Foo:\n    pass\n", encoding="utf-8")
    consumer = root / "b.py"
    source = "from a import Foo\ndef résumé(élément: 'É | Foo') -> 'Foo':\n    return élément\n"
    consumer.write_text(source, encoding="utf-8")
    line = source.splitlines()[1]
    expected = (
        (line.index("Foo"), line.index("Foo") + 3),
        (line.rindex("Foo"), line.rindex("Foo") + 3),
    )

    references = find_references(Database(), root, target, "Foo")
    reference_ranges = tuple(
        (item.range.start.character, item.range.end.character)
        for item in references.references
        if not item.is_declaration and item.path == str(consumer)
    )
    assert reference_ranges == expected


def test_find_references_includes_forward_ref_strings_in_class_variable_annotation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
    b = root / "b.py"
    b.write_text(
        "from a import Foo\n\nclass C:\n    x: 'Foo'\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "Foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    assert len(non_decl) == 1
    ref = non_decl[0]
    assert ref.range.start.line == 3
    # `    x: 'Foo'` — opening quote at col 7, name at cols 8-11.
    assert (ref.range.start.character, ref.range.end.character) == (8, 11)


def test_find_references_includes_forward_ref_strings_in_module_ann_assign(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
    b = root / "b.py"
    b.write_text(
        "from a import Foo\n\nx: 'Foo'\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "Foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    assert len(non_decl) == 1
    assert non_decl[0].range.start.line == 2
    assert (
        non_decl[0].range.start.character,
        non_decl[0].range.end.character,
    ) == (4, 7)


def test_find_references_includes_forward_ref_strings_in_subscript(
    tmp_path: Path,
) -> None:
    """Strings nested in subscripts like `list['Foo']` and
    `dict[str, 'Foo']` are reached and walked."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
    b = root / "b.py"
    b.write_text(
        "from a import Foo\n\ndef g(a: list['Foo']) -> dict[str, 'Foo']:\n    return a\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "Foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    assert len(non_decl) == 2
    assert all(r.range.start.line == 2 for r in non_decl)


def test_find_references_includes_forward_ref_strings_in_union(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
    b = root / "b.py"
    b.write_text(
        "from a import Foo\n\ndef g(a: 'Foo | None') -> None:\n    return None\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "Foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    assert len(non_decl) == 1
    ref = non_decl[0]
    assert ref.range.start.line == 2
    # `def g(a: 'Foo | None') -> None:` — opening quote at col 9, name at 10-13.
    assert (ref.range.start.character, ref.range.end.character) == (10, 13)


def test_find_references_resolves_attribute_chain_in_string_annotation(
    tmp_path: Path,
) -> None:
    """``import a; def g(x: 'a.Foo'): ...`` counts as a reference to
    ``a.Foo``: the string-annotation walker emits a hint=``"a"`` occurrence,
    and the same import-aware verification used for unquoted attribute access
    accepts it."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
    (root / "b.py").write_text(
        "import a\n\ndef g(x: 'a.Foo') -> None:\n    return None\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "Foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    assert len(non_decl) == 1
    ref = non_decl[0]
    assert ref.range.start.line == 2
    # `def g(x: 'a.Foo') -> None:` — inside the string, `Foo` is at cols 12-15.
    assert (ref.range.start.character, ref.range.end.character) == (12, 15)


def test_find_references_includes_forward_ref_strings_double_quoted(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
    b = root / "b.py"
    b.write_text(
        'from a import Foo\n\ndef g(a: "Foo") -> None:\n    return None\n',
        encoding="utf-8",
    )

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "Foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    assert len(non_decl) == 1
    ref = non_decl[0]
    assert ref.range.start.line == 2
    assert (ref.range.start.character, ref.range.end.character) == (10, 13)


def test_find_references_skips_malformed_string_annotation(tmp_path: Path) -> None:
    """A string annotation that doesn't parse as an expression is silently
    skipped — the call doesn't raise, and no spurious reference is emitted."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
    b = root / "b.py"
    b.write_text(
        "from a import Foo\n\ndef g(a: 'this is not valid python'): ...\nFoo()\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "Foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    # Only the unquoted `Foo()` on line 4 is reported; the malformed
    # annotation contributes nothing (and doesn't crash).
    assert len(non_decl) == 1
    assert non_decl[0].range.start.line == 3


def test_find_references_skips_string_annotation_with_escape_sequence(
    tmp_path: Path,
) -> None:
    """A string annotation containing an escape sequence is intentionally
    skipped — the source span and decoded value lengths differ, so offset
    reconstruction would be ambiguous. Documented limitation."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
    b = root / "b.py"
    b.write_text(
        "from a import Foo\n\ndef g(a: 'F\\x6fo') -> None:\n    return None\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "Foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    assert non_decl == []


def test_find_references_supports_single_line_triple_quoted_annotation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
    b = root / "b.py"
    b.write_text(
        "from a import Foo\n\ndef g(a: '''Foo''') -> None:\n    return None\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "Foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    assert len(non_decl) == 1
    assert (
        non_decl[0].range.start.character,
        non_decl[0].range.end.character,
    ) == (12, 15)


def test_find_references_skips_implicit_string_concatenation_annotation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
    b = root / "b.py"
    b.write_text(
        "from a import Foo\n\ndef g(a: 'Fo' 'o') -> None:\n    return None\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "Foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    # The implicitly-concatenated literal collapses to `'Foo'` at the AST
    # level but the source span is longer than the decoded value; we bail.
    assert non_decl == []


def test_find_references_finds_type_checking_imported_name_in_string_annotation(
    tmp_path: Path,
) -> None:
    """TYPE_CHECKING-imported names referenced by string annotations work:
    the existing TYPE_CHECKING import collection plus the new string scan
    compose without special wiring."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
    b = root / "b.py"
    b.write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from a import Foo\n\n"
        "class C:\n"
        "    x: 'Foo'\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "Foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    assert len(non_decl) == 1
    ref = non_decl[0]
    assert ref.range.start.line == 5
    # `    x: 'Foo'` on line 6 — opening quote at col 7, name at 8-11.
    assert (ref.range.start.character, ref.range.end.character) == (8, 11)


def test_symbol_at_returns_none_for_stdlib_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    consumer = root / "consumer.py"
    consumer.write_text(
        "from json import JSONDecoder\n\nJSONDecoder()\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    assert symbol_at(db, root, consumer, SourcePosition(2, 1)) is None


def test_symbol_at_returns_none_for_ambiguous_target(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("from b import foo\n", encoding="utf-8")
    (root / "b.py").write_text("from a import foo\n", encoding="utf-8")

    db = Database(mode="strict")
    assert symbol_at(db, root, root / "a.py", SourcePosition(0, 15)) is None


def test_subscript_assignment_reads_container_and_index(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "sample.py"
    path.write_text(
        "items = [0]\nindex = 0\nitems[index] = 1\nprint(items[index])\n",
        encoding="utf-8",
    )
    db = Database(mode="strict")

    item_refs = find_references(db, root, path, "items").references
    assert [(item.range.start.line, item.range.start.character) for item in item_refs] == [
        (0, 0),
        (2, 0),
        (3, 6),
    ]
    index_refs = find_references(db, root, path, "index").references
    assert [(item.range.start.line, item.range.start.character) for item in index_refs] == [
        (1, 0),
        (2, 6),
        (3, 12),
    ]


def test_decomposed_identifier_ranges_preserve_source_spelling(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "sample.py"
    path.write_text("def e\u0301():\n    return e\u0301()\n", encoding="utf-8")
    db = Database(mode="strict")

    table = module_symbol_table(db, root, path)
    symbol = next(item for item in table.symbols if item.qualified_name == "é")
    assert symbol.range == SourceRange(SourcePosition(0, 4), SourcePosition(0, 6))

    lexical = scope_tree(db, path)
    binding = next(item for item in lexical.bindings if item.name == "é")
    assert binding.range == SourceRange(SourcePosition(0, 4), SourcePosition(0, 6))
    refs = find_references(db, root, path, "é").references
    assert [item.range for item in refs] == [
        SourceRange(SourcePosition(0, 4), SourcePosition(0, 6)),
        SourceRange(SourcePosition(1, 11), SourcePosition(1, 13)),
    ]


def test_deleted_module_receiver_is_not_resolved_speculatively(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "provider.py").write_text("def f() -> None:\n    pass\n", encoding="utf-8")
    consumer = root / "consumer.py"
    consumer.write_text(
        "import provider\ndel provider\nprovider.f()\n",
        encoding="utf-8",
    )

    assert (
        symbol_at(
            Database(mode="strict"),
            root,
            consumer,
            SourcePosition(2, 9),
        )
        is None
    )


def test_pep695_bindings_share_the_lexical_scope_graph(tmp_path: Path) -> None:
    import ast

    if not hasattr(ast, "TypeAlias"):
        pytest.skip("PEP 695 syntax requires Python 3.12+")

    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "generic.py"
    path.write_text(
        "type Alias[T] = list[T]\n"
        "class Box[T]:\n"
        "    value: T\n"
        "def identity[T](value: T) -> T:\n"
        "    return value\n",
        encoding="utf-8",
    )
    db = Database(mode="strict")

    table = module_symbol_table(db, root, path)
    alias = next(item for item in table.symbols if item.qualified_name == "Alias")
    assert alias.kind == "variable"
    lexical = scope_tree(db, path)
    assert any(item.kind == "type_alias" and item.name == "Alias" for item in lexical.bindings)
    type_parameters = [item for item in lexical.bindings if item.kind == "type_parameter"]
    scope_kinds = {item.id: item.kind for item in lexical.scopes}
    assert [(item.name, scope_kinds[item.scope_id]) for item in type_parameters] == [
        ("T", "type_alias"),
        ("T", "class"),
        ("T", "function"),
    ]
    for parameter in type_parameters:
        assert (
            sum(occurrence.symbol_id == parameter.symbol_id for occurrence in lexical.occurrences)
            >= 2
        )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_find_references_by_mode(mode: str, tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text(
        "def foo() -> int:\n    return 1\n",
        encoding="utf-8",
    )
    b = root / "b.py"
    b.write_text("from a import foo\n\nfoo()\n", encoding="utf-8")

    db = Database(mode=mode)
    result = find_references(db, root, root / "a.py", "foo")

    assert result.target.path == str(root / "a.py")
    assert result.target.name == "foo"
    assert sorted(
        (Path(r.path).name, r.range.start.line, r.is_declaration) for r in result.references
    ) == [
        ("a.py", 0, True),
        ("b.py", 2, False),
    ]


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_find_references_matches_fresh_recomputation_over_edits(mode: str, tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    a = root / "a.py"
    b = root / "b.py"
    a.write_text("def foo() -> int:\n    return 1\n", encoding="utf-8")
    b.write_text("from a import foo\n\nfoo()\n", encoding="utf-8")

    incremental = Database(mode=mode)
    edits = (
        "def foo() -> int:\n    return 1\n",
        "# edit\ndef foo() -> int:\n    return 2\n",
        "def foo(x: int) -> int:\n    return x\n",
        "def foo() -> int:\n    return 3\n\nfoo()\n",
    )
    for content in edits:
        a.write_text(content, encoding="utf-8")
        fresh = Database(mode=mode)
        inc = find_references(incremental, root, a, "foo")
        fresh_result = find_references(fresh, root, a, "foo")
        assert inc == fresh_result


# ---------------------------------------------------------------------------
# try/except ImportError support
# ---------------------------------------------------------------------------


def test_import_error_try_imports_visible_in_symbol_table(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "mod.py"
    path.write_text(
        "try:\n"
        "    import ujson\n"
        "    from msgpack import pack\n"
        "except ImportError:\n"
        "    pass\n"
        "def visible() -> int:\n"
        "    return 1\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    table = module_symbol_table(db, root, path)
    qnames = {sym.qualified_name for sym in table.symbols}
    assert "ujson" in qnames
    assert "pack" in qnames
    assert "visible" in qnames
    assert "conditional top-level binding" not in table.impurity_reasons

    ujson_sym = next(s for s in table.symbols if s.qualified_name == "ujson")
    assert ujson_sym.kind == "import_alias"
    assert ujson_sym.import_source_module == "ujson"

    pack_sym = next(s for s in table.symbols if s.qualified_name == "pack")
    assert pack_sym.kind == "from_import_alias"
    assert pack_sym.import_source_module == "msgpack"
    assert pack_sym.import_source_name == "pack"


def test_import_error_try_module_not_found_error(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "mod.py"
    path.write_text(
        "try:\n    import tomllib\nexcept ModuleNotFoundError:\n    import tomli as tomllib\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    table = module_symbol_table(db, root, path)
    qnames = {sym.qualified_name for sym in table.symbols}
    assert "tomllib" in qnames
    assert "conditional top-level binding" not in table.impurity_reasons


def test_import_error_try_tuple_handler(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "mod.py"
    path.write_text(
        "try:\n"
        "    import rapidjson as json_impl\n"
        "except (ImportError, ModuleNotFoundError):\n"
        "    import json as json_impl\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    table = module_symbol_table(db, root, path)
    qnames = {sym.qualified_name for sym in table.symbols}
    assert "json_impl" in qnames
    assert "conditional top-level binding" not in table.impurity_reasons


def test_import_error_try_with_other_conditional_still_records_impurity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "mod.py"
    path.write_text(
        "import sys\n"
        "try:\n"
        "    import ujson\n"
        "except ImportError:\n"
        "    pass\n"
        "if sys.version_info >= (3, 12):\n"
        "    x: int = 1\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    table = module_symbol_table(db, root, path)
    qnames = {sym.qualified_name for sym in table.symbols}
    assert "ujson" in qnames
    assert "x" not in qnames
    assert "conditional top-level binding" in table.impurity_reasons


def test_import_error_try_bare_except_records_impurity(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "mod.py"
    path.write_text(
        "try:\n    import ujson\nexcept:\n    pass\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    table = module_symbol_table(db, root, path)
    assert "conditional top-level binding" in table.impurity_reasons


def test_resolve_symbol_from_import_error_try(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "helper.py").write_text("class FastParser:\n    pass\n", encoding="utf-8")
    path = root / "consumer.py"
    path.write_text(
        "try:\n"
        "    from helper import FastParser\n"
        "except ImportError:\n"
        "    FastParser = None  # type: ignore\n"
        "def use_parser() -> None:\n"
        "    pass\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    table = module_symbol_table(db, root, path)
    qnames = {sym.qualified_name for sym in table.symbols}
    assert "FastParser" in qnames

    result = resolve_symbol(db, root, path, "FastParser")
    assert result.resolution == "workspace"
    assert result.defining_path == str(root / "helper.py")


# ---------------------------------------------------------------------------
# class_model (own members)
# ---------------------------------------------------------------------------


_CLASS_MODEL_SAMPLE = (
    "class Widget(Base):\n"
    "    size: int = 3\n"
    "    tag = 'x'\n"
    "\n"
    "    def __init__(self, name: str) -> None:\n"
    "        self.name = name\n"
    "        self.count: int = 0\n"
    "\n"
    "    def render(self) -> str:\n"
    "        self.cache = None\n"
    "        return self.name\n"
)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_class_model_captures_class_body_and_init_members(mode: str, tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "widget.py"
    path.write_text(_CLASS_MODEL_SAMPLE, encoding="utf-8")

    model = class_model(Database(mode=mode), root, path, "Widget")
    assert model.path == str(path)
    assert model.qualified_name == "Widget"

    by_name = {member.name: member for member in model.members}

    assert by_name["size"].kind == "class_variable"
    assert by_name["size"].range.start.line == 1
    assert by_name["size"].annotation == "int"
    assert by_name["size"].signature is None

    assert by_name["tag"].kind == "class_variable"
    assert by_name["tag"].range.start.line == 2
    assert by_name["tag"].annotation is None

    assert by_name["__init__"].kind == "method"
    assert by_name["__init__"].range.start.line == 4
    assert by_name["__init__"].signature == Signature(
        parameters=(
            Parameter(name="self", annotation=None),
            Parameter(name="name", annotation="str"),
        ),
        return_annotation="None",
    )

    assert by_name["render"].kind == "method"
    assert by_name["render"].range.start.line == 8

    assert by_name["name"].kind == "instance_variable"
    assert by_name["name"].range.start.line == 5
    assert by_name["name"].annotation is None
    assert by_name["count"].kind == "instance_variable"
    assert by_name["count"].range.start.line == 6
    assert by_name["count"].annotation == "int"
    assert by_name["cache"].kind == "instance_variable"
    assert by_name["cache"].range.start.line == 9

    # Every own member is attributed to this file and class.
    for member in model.members:
        assert member.defining_path == str(path)
        assert member.defining_class == "Widget"

    # A single base is recorded but not followed in Stage 1.
    assert model.unresolved_bases == ("Base",)
    # An unresolvable base name is not a depth-cap truncation.
    assert model.truncated_bases == ()


def test_class_model_deterministic_member_order(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "widget.py"
    path.write_text(_CLASS_MODEL_SAMPLE, encoding="utf-8")

    model = class_model(Database(mode="strict"), root, path, "Widget")
    # annotated class-body, assigned class-body, methods, then instance attrs
    # ordered by lowest lineno.
    assert [member.name for member in model.members] == [
        "size",
        "tag",
        "__init__",
        "render",
        "name",
        "count",
        "cache",
    ]


def test_class_model_self_attrs_dedup_lowest_lineno_wins(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "c.py"
    path.write_text(
        "class C:\n"
        "    def __init__(self) -> None:\n"
        "        self.x = 1\n"
        "    def reset(self) -> None:\n"
        "        self.x = 0\n"
        "        self.y = 2\n",
        encoding="utf-8",
    )

    model = class_model(Database(mode="strict"), root, path, "C")
    by_name = {member.name: member for member in model.members}
    assert by_name["x"].kind == "instance_variable"
    assert by_name["x"].range.start.line == 2  # __init__ occurrence, not reset()
    assert by_name["y"].range.start.line == 5


def test_class_model_requires_literal_self_first_param(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "c.py"
    path.write_text(
        "class C:\n    def m(this) -> None:\n        this.z = 1\n",
        encoding="utf-8",
    )

    model = class_model(Database(mode="strict"), root, path, "C")
    names = {member.name for member in model.members}
    assert names == {"m"}  # `this.z` is not an instance attribute


def test_class_model_skips_nested_def_and_class(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "c.py"
    path.write_text(
        "class C:\n"
        "    def m(self) -> None:\n"
        "        self.a = 1\n"
        "        def inner(self) -> None:\n"
        "            self.b = 2\n"
        "        class Inner:\n"
        "            def n(self) -> None:\n"
        "                self.c = 3\n"
        "        helper = lambda: self.d\n"
        "        self.e = 4\n",
        encoding="utf-8",
    )

    model = class_model(Database(mode="strict"), root, path, "C")
    instance_names = {member.name for member in model.members if member.kind == "instance_variable"}
    assert instance_names == {"a", "e"}  # b, c live in nested scopes


def test_class_model_tuple_unpacked_self_targets(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "c.py"
    path.write_text(
        "class C:\n"
        "    def __init__(self) -> None:\n"
        "        self.a, self.b = 1, 2\n"
        "        [self.c, self.d] = [3, 4]\n"
        "        self.e, *self.rest = range(5)\n",
        encoding="utf-8",
    )

    model = class_model(Database(mode="strict"), root, path, "C")
    instance_names = {member.name for member in model.members if member.kind == "instance_variable"}
    assert instance_names == {"a", "b", "c", "d", "e", "rest"}


def test_class_model_records_all_base_encodings(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "c.py"
    path.write_text(
        "class Multi(Base, pkg.Mixin, Generic[T], *extra):\n    pass\n",
        encoding="utf-8",
    )

    model = class_model(Database(mode="strict"), root, path, "Multi")
    assert model.unresolved_bases == ("Base", "pkg.Mixin", "Generic", "*extra")


def test_class_model_syntax_error_file_is_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "broken.py"
    path.write_text("class Broken(\n", encoding="utf-8")

    model = class_model(Database(mode="strict"), root, path, "Broken")
    assert model.members == ()
    assert model.unresolved_bases == ()


def test_class_model_unknown_qname_is_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "widget.py"
    path.write_text(_CLASS_MODEL_SAMPLE, encoding="utf-8")

    model = class_model(Database(mode="strict"), root, path, "Missing")
    assert model.members == ()
    assert model.unresolved_bases == ()


def test_class_model_nested_class_has_its_own_members(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "nested.py"
    path.write_text(
        "class Outer:\n"
        "    outer_attr: int = 1\n"
        "    class Inner:\n"
        "        def method(self) -> None:\n"
        "            self.inner_attr = 2\n",
        encoding="utf-8",
    )

    outer = class_model(Database(mode="strict"), root, path, "Outer")
    outer_names = {member.name for member in outer.members}
    assert outer_names == {"outer_attr"}

    inner = class_model(Database(mode="strict"), root, path, "Outer.Inner")
    inner_names = {member.name: member.kind for member in inner.members}
    assert inner_names == {"method": "method", "inner_attr": "instance_variable"}


def test_class_models_for_file_covers_every_class(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "widget.py"
    path.write_text(_CLASS_MODEL_SAMPLE, encoding="utf-8")

    db = Database(mode="strict")
    payload = class_models_for_file(db, str(path))
    qnames = {model[0] for model in payload}
    assert qnames == {"Widget"}


def test_resolved_class_model_out_of_workspace_is_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text(_CLASS_MODEL_SAMPLE, encoding="utf-8")

    db = Database(mode="strict")
    payload = resolved_class_model_payload(db, str(root), str(outside), "Widget")
    # path, qualified_name, members, unresolved_bases, truncated_bases
    assert payload[2] == ()
    assert payload[3] == ()
    assert payload[4] == ()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_class_model_matches_fresh_recomputation_over_edits(mode: str, tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "widget.py"

    incremental = Database(mode=mode)
    contents = (
        _CLASS_MODEL_SAMPLE,
        _CLASS_MODEL_SAMPLE + "\nclass Other:\n    pass\n",
        "class Widget(Base, Extra):\n"
        "    size: int = 5\n"
        "    def __init__(self) -> None:\n"
        "        self.name = ''\n",
        "class Widget:\n    def render(self) -> str:\n        return ''\n",
    )
    for content in contents:
        path.write_text(content, encoding="utf-8")
        fresh = Database(mode=mode)
        assert class_model(incremental, root, path, "Widget") == class_model(
            fresh, root, path, "Widget"
        )


def test_comment_only_edit_backdates_class_models(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "widget.py"
    path.write_text(_CLASS_MODEL_SAMPLE, encoding="utf-8")

    db = Database(mode="strict")
    first = class_model(db, root, path, "Widget")
    first_changed = db.inspect(class_models_for_file, str(path)).changed_at

    path.write_text(_CLASS_MODEL_SAMPLE + "# trailing comment\n", encoding="utf-8")
    second = class_model(db, root, path, "Widget")

    assert first == second
    # `last_recompute` is the discriminating half here too; `changed_at` alone
    # is unmoved either way.
    record = db.inspect(class_models_for_file, str(path))
    assert record.last_recompute == "backdated", (
        f"last_recompute={record.last_recompute} | an equal set of models "
        "across a comment-only edit has to be backdated"
    )
    assert record.changed_at == first_changed, (
        f"changed_at={record.changed_at} before={first_changed} | the models "
        "moved under an edit they do not carry"
    )


def test_class_model_member_carries_no_class_kind(tmp_path: Path) -> None:
    # Instance attributes must never leak into the module symbol table; the two
    # views stay disjoint. A nested class in the body is not a `class_model`
    # member kind.
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "widget.py"
    path.write_text(_CLASS_MODEL_SAMPLE, encoding="utf-8")

    db = Database(mode="strict")
    model = class_model(db, root, path, "Widget")
    kinds = {member.kind for member in model.members}
    assert kinds <= {"method", "class_variable", "instance_variable"}

    # The instance attribute `name` is absent from the module symbol table.
    table = module_symbol_table(db, root, path)
    assert not any(sym.qualified_name == "Widget.name" for sym in table.symbols)
    assert all(isinstance(member, ClassMember) for member in model.members)


# ---------------------------------------------------------------------------
# class_model (inheritance flattening — Stage 3)
# ---------------------------------------------------------------------------


def _write_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_class_model_inherits_members_from_workspace_base(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    base = root / "base.py"
    _write_file(
        base,
        "class Base:\n"
        "    kind: str = 'b'\n"
        "    def shared(self) -> None:\n"
        "        self.base_attr = 1\n",
    )
    derived = root / "derived.py"
    _write_file(
        derived,
        "from base import Base\n\n"
        "class Derived(Base):\n"
        "    size: int = 3\n"
        "    def own(self) -> None:\n"
        "        self.own_attr = 2\n",
    )

    model = class_model(Database(mode="strict"), root, derived, "Derived")
    by_name = {member.name: member for member in model.members}

    # Own members are attributed to the derived file/class.
    assert by_name["size"].defining_path == str(derived)
    assert by_name["size"].defining_class == "Derived"
    assert by_name["own"].defining_class == "Derived"

    # Inherited members carry the base's defining_path/defining_class.
    assert by_name["shared"].kind == "method"
    assert by_name["shared"].defining_path == str(base)
    assert by_name["shared"].defining_class == "Base"
    assert by_name["kind"].kind == "class_variable"
    assert by_name["kind"].defining_class == "Base"
    assert by_name["base_attr"].kind == "instance_variable"
    assert by_name["base_attr"].defining_class == "Base"

    # A followed workspace base does not linger in unresolved_bases.
    assert model.unresolved_bases == ()


def test_class_model_derived_shadows_base(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    base = root / "base.py"
    _write_file(
        base,
        "class Base:\n    def render(self) -> str:\n        return 'base'\n",
    )
    derived = root / "derived.py"
    _write_file(
        derived,
        "from base import Base\n\n\n"
        "class Derived(Base):\n"
        "    def render(self) -> str:\n"
        "        return 'derived'\n",
    )

    model = class_model(Database(mode="strict"), root, derived, "Derived")
    renders = [m for m in model.members if m.name == "render"]
    # First-definition-wins: exactly one `render`, the derived override.
    assert len(renders) == 1
    assert renders[0].range.start.line == 4  # def render in derived.py
    assert renders[0].defining_class == "Derived"
    assert renders[0].defining_path == str(derived)


def test_class_model_non_workspace_base_is_unresolved(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    path = root / "d.py"
    _write_file(
        path,
        "from collections import OrderedDict\n\n\n"
        "class D(OrderedDict):\n"
        "    def own(self) -> None:\n"
        "        self.x = 1\n",
    )

    model = class_model(Database(mode="strict"), root, path, "D")
    names = {member.name for member in model.members}
    # No stdlib dict members leak in; only D's own members remain.
    assert names == {"own", "x"}
    assert model.unresolved_bases == ("OrderedDict",)


def test_class_model_base_cycle_terminates(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    path = root / "c.py"
    _write_file(
        path,
        "class A(B):\n"
        "    def am(self) -> None:\n"
        "        pass\n"
        "class B(A):\n"
        "    def bm(self) -> None:\n"
        "        pass\n",
    )

    model = class_model(Database(mode="strict"), root, path, "A")
    names = {member.name for member in model.members}
    # Cycle guard terminates and both classes contribute their own members.
    assert names == {"am", "bm"}
    # Nothing is lost to a cycle, so nothing is reported as truncated.
    assert model.truncated_bases == ()


def test_class_model_base_depth_cap(tmp_path: Path) -> None:
    # A linear chain C0(C1(...(C9))). Traversal is bounded at MAX_BASE_DEPTH = 8:
    # C0..C7 (depths 0..7) contribute members; C8/C9 are past the cap.
    root = tmp_path / "workspace"
    path = root / "chain.py"
    lines = []
    for i in range(10):
        base = f"(C{i + 1})" if i < 9 else ""
        lines.append(f"class C{i}{base}:\n    def m{i}(self) -> None:\n        pass\n")
    _write_file(path, "".join(lines))

    model = class_model(Database(mode="strict"), root, path, "C0")
    names = {member.name for member in model.members}
    assert "m0" in names
    assert "m7" in names
    assert "m8" not in names  # depth 8 is at the cap boundary
    assert "m9" not in names
    assert model.truncated_bases == ("C8",)


def test_class_model_subscripted_base_unwraps(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    path = root / "g.py"
    _write_file(
        path,
        "class Base:\n"
        "    def bm(self) -> None:\n"
        "        pass\n"
        "class D(Base[int]):\n"
        "    def dm(self) -> None:\n"
        "        pass\n"
        "class E(*mixins):\n"
        "    def em(self) -> None:\n"
        "        pass\n",
    )
    db = Database(mode="strict")

    # `Base[int]` unwraps to `Base`, a workspace class, and is followed.
    d_model = class_model(db, root, path, "D")
    assert {m.name for m in d_model.members} == {"dm", "bm"}
    assert d_model.unresolved_bases == ()

    # `*mixins` is a starred base — encoded as text, never followed.
    e_model = class_model(db, root, path, "E")
    assert {m.name for m in e_model.members} == {"em"}
    assert e_model.unresolved_bases == ("*mixins",)


def test_class_model_same_module_nested_base(tmp_path: Path) -> None:
    # `class Sub(Outer.Inner)` where `Outer.Inner` is a nested class in the same
    # module resolves through the `("attr", ...)` same-module branch.
    root = tmp_path / "workspace"
    path = root / "n.py"
    _write_file(
        path,
        "class Outer:\n"
        "    class Inner:\n"
        "        def inner_m(self) -> None:\n"
        "            pass\n"
        "class Sub(Outer.Inner):\n"
        "    def sub_m(self) -> None:\n"
        "        pass\n",
    )

    model = class_model(Database(mode="strict"), root, path, "Sub")
    names = {member.name for member in model.members}
    assert names == {"sub_m", "inner_m"}
    assert model.unresolved_bases == ()


def test_class_model_diamond_first_definition_wins(tmp_path: Path) -> None:
    # A(B, C); B and C both define `hit`. Depth-first, left-to-right, first wins:
    # B's `hit` is kept (B is the first base visited).
    root = tmp_path / "workspace"
    path = root / "diamond.py"
    _write_file(
        path,
        "class B:\n"
        "    def hit(self) -> str:\n"
        "        return 'B'\n"
        "class C:\n"
        "    def hit(self) -> str:\n"
        "        return 'C'\n"
        "class A(B, C):\n"
        "    pass\n",
    )

    model = class_model(Database(mode="strict"), root, path, "A")
    hits = [m for m in model.members if m.name == "hit"]
    assert len(hits) == 1
    assert hits[0].defining_class == "B"


def test_class_model_base_file_comment_edit_reused(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    base = root / "base.py"
    base_src = "class Base:\n    def shared(self) -> None:\n        pass\n"
    _write_file(base, base_src)
    derived = root / "derived.py"
    _write_file(
        derived,
        "from base import Base\n\n\nclass Derived(Base):\n    pass\n",
    )

    db = Database(mode="strict")
    first = class_model(db, root, derived, "Derived")
    base_changed = db.inspect(class_models_for_file, str(base)).changed_at

    # A trailing-comment edit to the BASE file leaves the AST attributes
    # untouched, so class_models_for_file(base) backdates and the derived
    # flattened model is reused unchanged.
    base.write_text(base_src + "# trailing comment\n", encoding="utf-8")
    second = class_model(db, root, derived, "Derived")

    assert first == second
    assert {m.name for m in second.members} == {"shared"}
    base_record = db.inspect(class_models_for_file, str(base))
    assert base_record.last_recompute == "backdated", (
        f"last_recompute={base_record.last_recompute} | the base file's models "
        "are equal across the edit and have to be backdated"
    )
    assert base_record.changed_at == base_changed, (
        f"changed_at={base_record.changed_at} before={base_changed} | the base "
        "file's models moved under an edit they do not carry"
    )
    assert db.inspect(class_models_for_file, str(derived)).last_decision == "reused"


def test_class_model_unrelated_edit_leaves_model_green(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    base = root / "base.py"
    _write_file(
        base,
        "class Base:\n    def shared(self) -> None:\n        pass\n",
    )
    derived = root / "derived.py"
    _write_file(
        derived,
        "from base import Base\n\n\nclass Derived(Base):\n    pass\n",
    )
    other = root / "other.py"
    _write_file(other, "def unrelated() -> int:\n    return 0\n")

    db = Database(mode="strict")
    first = class_model(db, root, derived, "Derived")

    # Editing an unrelated file's body must not disturb the derived model.
    other.write_text("def unrelated() -> int:\n    return 999\n", encoding="utf-8")
    second = class_model(db, root, derived, "Derived")

    assert first == second
    assert {m.name for m in second.members} == {"shared"}


# ---------------------------------------------------------------------------
# class_model (depth-aware base walk and truncation reporting)
# ---------------------------------------------------------------------------


def _base_chain(prefix: str, length: int, tail: str) -> str:
    """``prefix1(prefix2)`` … ``prefixN(tail)``, one method per class."""
    parts = []
    for index in range(1, length + 1):
        base = f"{prefix}{index + 1}" if index < length else tail
        parts.append(
            f"class {prefix}{index}({base}):\n"
            f"    def from_{prefix.lower()}{index}(self) -> None:\n"
            "        pass\n"
        )
    return "".join(parts)


def test_class_model_subclass_never_reports_fewer_members_than_its_base(
    tmp_path: Path,
) -> None:
    # `Root(L1, Mid)`. The L spine reaches `X` at depth 6, so `X`'s own base
    # chain runs out of budget and `XBase2` is cut. `Mid` reaches the same `X`
    # at depth 2, where the whole chain fits — but recording `X` as merely
    # "visited" on the deep reach made the shallow one a no-op. `Root` then
    # reported strictly fewer members than `Mid`, one of its own direct bases,
    # and said nothing about the loss. Nothing here is past the cap (see
    # `truncated_bases` below), so `Root` must cover `Mid` exactly.
    root = tmp_path / "workspace"
    path = root / "spine.py"
    _write_file(
        path,
        "class XBase2:\n    def from_xbase2(self) -> None:\n        pass\n"
        "class XBase(XBase2):\n    def from_xbase(self) -> None:\n        pass\n"
        "class X(XBase):\n    def from_x(self) -> None:\n        pass\n"
        + _base_chain("L", 5, "X")
        + "class Mid(X):\n    def from_mid(self) -> None:\n        pass\n"
        "class Root(L1, Mid):\n    def from_root(self) -> None:\n        pass\n",
    )

    db = Database(mode="strict")
    model = class_model(db, root, path, "Root")
    root_names = {member.name for member in model.members}
    mid_names = {member.name for member in class_model(db, root, path, "Mid").members}

    assert mid_names <= root_names
    assert "from_xbase2" in root_names
    assert model.unresolved_bases == ()
    assert model.truncated_bases == ()


@pytest.mark.parametrize(
    ("right_length", "reaches_deep", "expected_truncated"),
    [(6, False, ("Deep",)), (5, True, ())],
)
def test_class_model_revisits_only_strictly_shallower_reaches(
    right_length: int,
    reaches_deep: bool,
    expected_truncated: tuple[str, ...],
    tmp_path: Path,
) -> None:
    # `Root(A1, B1)`. The A spine reaches `Near` at depth 7, leaving its base
    # `Deep` at the cap. A B spine of the same length reaches `Near` at depth 7
    # again — not strictly shallower, so nothing is re-walked and the loss
    # stands and is reported. One link shorter reaches `Near` at depth 6, where
    # `Deep` fits, so the walk is redone and the truncation report retracted.
    root = tmp_path / "workspace"
    path = root / "spines.py"
    _write_file(
        path,
        "class Deep:\n    def from_deep(self) -> None:\n        pass\n"
        "class Near(Deep):\n    def from_near(self) -> None:\n        pass\n"
        + _base_chain("A", 6, "Near")
        + _base_chain("B", right_length, "Near")
        + "class Root(A1, B1):\n    def from_root(self) -> None:\n        pass\n",
    )

    model = class_model(Database(mode="strict"), root, path, "Root")
    names = {member.name for member in model.members}
    assert "from_near" in names
    assert ("from_deep" in names) is reaches_deep
    assert model.truncated_bases == expected_truncated
    assert model.unresolved_bases == ()


def test_class_model_names_the_base_the_depth_cap_stopped(tmp_path: Path) -> None:
    # The same linear chain C0(C1(...(C9))) as the cap test, read from three
    # starting points. Whatever the cap drops is named in `truncated_bases`
    # instead of vanishing.
    root = tmp_path / "workspace"
    path = root / "chain.py"
    lines = []
    for i in range(10):
        base = f"(C{i + 1})" if i < 9 else ""
        lines.append(f"class C{i}{base}:\n    def m{i}(self) -> None:\n        pass\n")
    _write_file(path, "".join(lines))
    db = Database(mode="strict")

    from_c0 = class_model(db, root, path, "C0")
    assert "m8" not in {member.name for member in from_c0.members}
    assert from_c0.truncated_bases == ("C8",)
    assert from_c0.unresolved_bases == ()

    # One link down the chain, the cap lands on C9 instead.
    assert class_model(db, root, path, "C1").truncated_bases == ("C9",)

    # Two links down, the whole remaining chain fits within the cap.
    from_c2 = class_model(db, root, path, "C2")
    assert {f"m{i}" for i in range(2, 10)} == {member.name for member in from_c2.members}
    assert from_c2.truncated_bases == ()


def test_class_model_truncated_base_reports_the_alias_as_written(tmp_path: Path) -> None:
    # The same chain split one class per file, each importing the next as `Up`.
    # Like `unresolved_bases`, the report names the base expression at the edge
    # the cap stopped — the alias, not the class it resolves to.
    root = tmp_path / "workspace"
    for index in range(10):
        body = f"class C{index}:\n    def m{index}(self) -> None:\n        pass\n"
        if index < 9:
            body = (
                f"from c{index + 1} import C{index + 1} as Up\n\n\n"
                f"class C{index}(Up):\n    def m{index}(self) -> None:\n        pass\n"
            )
        _write_file(root / f"c{index}.py", body)

    model = class_model(Database(mode="strict"), root, root / "c0.py", "C0")
    assert {member.name for member in model.members} == {f"m{i}" for i in range(8)}
    assert model.truncated_bases == ("Up",)
    assert model.unresolved_bases == ()


def test_class_model_wide_diamond_keeps_every_reachable_member(tmp_path: Path) -> None:
    # `N{k}` inherits from the next six classes, so nearly every class is
    # reachable at several depths and the deepest reach comes first. Each one
    # sits within `MAX_BASE_DEPTH` of `N0` by its shortest path, so a
    # depth-aware walk recovers all twenty member sets. The recorded depth
    # strictly decreases per revisit, which is what keeps this shape from
    # re-walking exponentially.
    levels, width = 20, 6
    root = tmp_path / "workspace"
    path = root / "wide.py"
    blocks = []
    for index in range(levels):
        bases = ", ".join(f"N{j}" for j in range(index + 1, min(index + 1 + width, levels)))
        suffix = f"({bases})" if bases else ""
        blocks.append(f"class N{index}{suffix}:\n    def m{index}(self) -> None:\n        pass\n")
    _write_file(path, "".join(reversed(blocks)))

    model = class_model(Database(mode="strict"), root, path, "N0")
    assert {member.name for member in model.members} == {f"m{i}" for i in range(levels)}
    assert model.truncated_bases == ()
    assert model.unresolved_bases == ()


def test_class_model_multi_class_cycle_terminates(tmp_path: Path) -> None:
    # A three-class cycle entered from outside it, plus a self-inheriting class.
    # The depth-aware map still cuts both: a second lap around a cycle is never
    # strictly shallower than the first, so it is never re-walked.
    root = tmp_path / "workspace"
    path = root / "cycle.py"
    _write_file(
        path,
        "class A(B):\n    def am(self) -> None:\n        pass\n"
        "class B(C):\n    def bm(self) -> None:\n        pass\n"
        "class C(A):\n    def cm(self) -> None:\n        pass\n"
        "class Enter(A):\n    def em(self) -> None:\n        pass\n"
        "class Selfish(Selfish):\n    def sm(self) -> None:\n        pass\n",
    )
    db = Database(mode="strict")

    model = class_model(db, root, path, "Enter")
    assert {member.name for member in model.members} == {"em", "am", "bm", "cm"}
    assert model.truncated_bases == ()
    assert model.unresolved_bases == ()

    selfish = class_model(db, root, path, "Selfish")
    assert {member.name for member in selfish.members} == {"sm"}
    assert selfish.truncated_bases == ()


def test_class_model_shallower_override_wins_over_revisited_base(tmp_path: Path) -> None:
    # `Root(A1, Repo, Widget)`. The A spine reaches `X` at depth 7, so `X`'s
    # base `Z` sits at the cap and is cut. `Repo` re-reaches `X` at depth 2, so
    # `Z` is walked at depth 3 — ahead of `Widget` at depth 1, which overrides
    # `save`. Arrival order alone would hand `save` to the base `Z`; the
    # winning definition is the one at the shortest inheritance distance.
    root = tmp_path / "workspace"
    path = root / "deep.py"
    _write_file(
        path,
        "class Z:\n    def save(self) -> None:\n        pass\n"
        "class X(Z):\n    pass\n"
        "class Repo(X):\n    def from_repo(self) -> None:\n        pass\n"
        + _base_chain("A", 6, "X")
        + "class Widget(Z):\n    def save(self, force: bool) -> int:\n        return 0\n"
        "class Root(A1, Repo, Widget):\n    pass\n",
    )

    model = class_model(Database(mode="strict"), root, path, "Root")
    by_name = {member.name: member for member in model.members}
    assert by_name["save"].defining_class == "Widget"
    assert by_name["save"].defining_path == str(path)
    assert by_name["save"].range.start.line == 27
    assert by_name["save"].signature == Signature(
        parameters=(
            Parameter(name="self", annotation=None),
            Parameter(name="force", annotation="bool"),
        ),
        return_annotation="int",
    )
    # Nothing is actually lost, so neither report fires.
    assert model.truncated_bases == ()
    assert model.unresolved_bases == ()


def test_class_model_diamond_prefers_the_nearer_definition(tmp_path: Path) -> None:
    # A plain diamond well inside the cap: `Z.m` sits at depth 2 through `A`
    # while `W.m` sits at depth 1, so `W` wins even though depth-first arrival
    # reaches `Z` first.
    root = tmp_path / "workspace"
    path = root / "diamond.py"
    _write_file(
        path,
        "class Z:\n    def m(self) -> None:\n        pass\n"
        "class A(Z):\n    def am(self) -> None:\n        pass\n"
        "class W(Z):\n    def m(self, times: int) -> str:\n        return ''\n"
        "class Root(A, W):\n    pass\n",
    )

    model = class_model(Database(mode="strict"), root, path, "Root")
    by_name = {member.name: member for member in model.members}
    assert by_name["m"].defining_class == "W"
    assert by_name["m"].range.start.line == 7
    assert by_name["m"].signature == Signature(
        parameters=(
            Parameter(name="self", annotation=None),
            Parameter(name="times", annotation="int"),
        ),
        return_annotation="str",
    )
    assert {member.name for member in model.members} == {"am", "m"}


def test_class_model_equal_depth_tie_goes_left_to_right(tmp_path: Path) -> None:
    # Nearest-definition-wins only reorders across depths; at equal depth the
    # depth-first left-to-right arrival still decides, both for direct bases
    # and for definitions one level further out.
    root = tmp_path / "workspace"
    path = root / "tie.py"
    _write_file(
        path,
        "class L:\n    def m(self) -> int:\n        return 0\n"
        "class R:\n    def m(self) -> str:\n        return ''\n"
        "class GrandL(L):\n    pass\n"
        "class GrandR(R):\n    pass\n"
        "class Direct(L, R):\n    pass\n"
        "class Indirect(GrandL, GrandR):\n    pass\n",
    )
    db = Database(mode="strict")

    direct = {m.name: m for m in class_model(db, root, path, "Direct").members}
    assert direct["m"].defining_class == "L"
    assert direct["m"].signature == Signature(
        parameters=(Parameter(name="self", annotation=None),),
        return_annotation="int",
    )

    indirect = {m.name: m for m in class_model(db, root, path, "Indirect").members}
    assert indirect["m"].defining_class == "L"


def test_class_model_truncation_reports_every_stopped_edge(tmp_path: Path) -> None:
    # `Root(A1, B1)`. Both spines end one step short of the same class `Deep`,
    # each importing it under its own alias, so the cap stops two distinct
    # edges onto one site. Both edges are named, not just the first.
    root = tmp_path / "workspace"
    _write_file(root / "deep.py", "class Deep:\n    def dm(self) -> None:\n        pass\n")
    for prefix, alias in (("A", "Alpha"), ("B", "Beta")):
        _write_file(
            root / f"{prefix.lower()}7.py",
            f"from deep import Deep as {alias}\n\n\n"
            f"class {prefix}7({alias}):\n"
            f"    def from_{prefix.lower()}7(self) -> None:\n        pass\n",
        )
        for index in range(1, 7):
            _write_file(
                root / f"{prefix.lower()}{index}.py",
                f"from {prefix.lower()}{index + 1} import {prefix}{index + 1}\n\n\n"
                f"class {prefix}{index}({prefix}{index + 1}):\n"
                f"    def from_{prefix.lower()}{index}(self) -> None:\n        pass\n",
            )
    _write_file(
        root / "root.py",
        "from a1 import A1\nfrom b1 import B1\n\n\nclass Root(A1, B1):\n    pass\n",
    )

    model = class_model(Database(mode="strict"), root, root / "root.py", "Root")
    assert "dm" not in {member.name for member in model.members}
    assert model.truncated_bases == ("Alpha", "Beta")
    assert model.unresolved_bases == ()


def _ordinary_bindings(db: Database, tmp_path: Path) -> set[str]:
    """Names bound by an ordinary module beside the hostile one.

    A refusal is only worth pinning together with the evidence that it refused
    the one path and left the database able to answer the next one.
    """
    ordinary = tmp_path / "ordinary.py"
    ordinary.write_text("VALUE = 1\n")
    return {binding.name for binding in scope_tree(db, str(ordinary)).bindings}


@posix_only
def test_a_scope_tree_of_a_looping_path_is_refused_by_type(tmp_path: Path) -> None:
    # A link pointing at itself names no source file, and asking the platform
    # to canonicalize it answers differently by interpreter version: the older
    # ones raise a loop error, the newer ones hand back a path that is still a
    # link. Canonicalizing through the tracked path makes the answer the same
    # everywhere and the entry point refuses it in its own voice, rather than
    # letting whatever the interpreter happened to raise reach the caller.
    db = Database()
    looping = make_symlink_loop(tmp_path / "loop")

    with pytest.raises(UnsupportedValueError, match="Path cannot be resolved"):
        scope_tree(db, str(looping))

    assert _ordinary_bindings(db, tmp_path) == {"VALUE"}


@posix_only
def test_a_scope_tree_of_a_null_path_is_refused_by_type(tmp_path: Path) -> None:
    # A path string holding a NUL names no file either, and the sentence the
    # platform composes for it is spelled differently again by version. The
    # refusal is the same one the looping path gets, for the same reason.
    db = Database()

    with pytest.raises(UnsupportedValueError, match="Path cannot be resolved"):
        scope_tree(db, nul_path(tmp_path))

    assert _ordinary_bindings(db, tmp_path) == {"VALUE"}
