from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

import pyinc.integrations as integrations
from pyinc import Database
from pyinc.integrations.symbol_resolution import (
    ClassMember,
    Parameter,
    Signature,
    class_model,
    class_models_for_file,
    find_references,
    module_symbol_table,
    module_symbol_table_payload,
    name_occurrences_for_file,
    resolve_symbol,
    resolve_symbol_payload,
    resolved_class_model_payload,
    workspace_name_occurrence_index,
    workspace_symbol_index,
)

Operation = tuple[Literal["write", "delete"], str, str | None]


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
        "ResolvedSymbol",
        "Signature",
        "Symbol",
        "WorkspaceSymbolEntry",
        "WorkspaceSymbolIndex",
        "class_model",
        "find_references",
        "module_symbol_table",
        "resolve_symbol",
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
        "ResolvedSymbol",
        "Signature",
        "Symbol",
        "WorkspaceSymbolEntry",
        "WorkspaceSymbolIndex",
        "class_model",
        "find_references",
        "module_symbol_table",
        "resolve_symbol",
        "workspace_symbol_index",
    ):
        assert hasattr(integrations, name)


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
def test_module_symbol_table_captures_top_level_symbols(
    mode: str, tmp_path: Path
) -> None:
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
    assert by_qname["alpha"].lineno == 4
    assert by_qname["beta"].kind == "function"
    assert by_qname["beta"].lineno == 7
    assert by_qname["beta"].signature == Signature(
        parameters=(Parameter(name="x", annotation="int"),),
        return_annotation="int",
    )

    assert by_qname["Gamma"].kind == "class"
    assert by_qname["Gamma"].lineno == 10
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
    assert resolved.defining_lineno == 1
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
    assert resolved.defining_lineno is None


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
    assert resolved.defining_lineno is None


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
    assert resolved.defining_lineno == 1
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
    (root / "m0.py").write_text(
        "def target() -> int:\n    return 1\n", encoding="utf-8"
    )
    for i in range(1, 10):
        (root / f"m{i}.py").write_text(
            f"from m{i - 1} import target\n", encoding="utf-8"
        )

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
    assert resolved.defining_lineno == 1
    assert resolved.trail == ("consumer:shown", "provider:shown")


def test_resolve_symbol_with_dynamic_all_is_ambiguous(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    provider = root / "provider.py"
    consumer = root / "consumer.py"
    provider.write_text(
        "def shown() -> int:\n"
        "    return 1\n"
        "__all__ = ['shown']\n"
        "__all__ += ['extra']\n",
        encoding="utf-8",
    )
    consumer.write_text("from provider import *\n", encoding="utf-8")

    db = Database(mode="strict")
    resolved = resolve_symbol(db, root, consumer, "missing_name")

    assert resolved.resolution == "ambiguous"
    inspection = db.inspect(
        resolve_symbol_payload, str(root), str(consumer), "missing_name"
    )
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
        "Metadata-Version: 2.1\n"
        "Name: fake_installed\n"
        "Version: 1.2.3\n"
        "Summary: Fake\n",
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

    path.write_text("def foo() -> int:\n    return 1\n# trailing\n", encoding="utf-8")
    second = module_symbol_table(db, root, path)

    assert first == second
    assert db.inspect(module_symbol_table_payload, str(path)).last_decision == "reused"


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
        "# spacer\n" "def foo(x: int) -> int:\n" "    return x\n",
        encoding="utf-8",
    )
    second = resolve_symbol(db, root, b, "foo")

    assert second.defining_lineno == 2
    assert db.inspect(module_symbol_table_payload, str(a)).last_recompute == "executed"
    assert (
        db.inspect(resolve_symbol_payload, str(root), str(b), "foo").last_recompute
        == "executed"
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
        "import typing\n" "if typing.TYPE_CHECKING:\n" "    from helper import Bar\n",
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
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from models import User\n",
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
        "def util() -> int:\n    return 1\n" "class Holder:\n    pass\n",
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        "from pkg.helper import util\n" "flag: bool = True\n",
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
        assert workspace_symbol_index(incremental, root) == workspace_symbol_index(
            fresh, root
        )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_resolve_symbol_matches_fresh_recomputation_over_edits(
    mode: str, tmp_path: Path
) -> None:
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
        assert resolve_symbol(incremental, root, b, "foo") == resolve_symbol(
            fresh, root, b, "foo"
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
        entry.qualified_name == "ResolvedSymbol"
        and entry.module == "integrations.symbol_resolution"
        for entry in idx.entries
    )


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

    assert result.target.resolution == "workspace"
    assert result.target.defining_lineno == 1
    assert len(result.references) == 2
    declaration = next(r for r in result.references if r.is_declaration)
    assert declaration.lineno == 1
    assert declaration.path == str(target)
    call = next(r for r in result.references if not r.is_declaration)
    assert call.lineno == 4
    assert call.col_offset == 0
    assert call.end_col_offset == 3


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
    assert result.references[0].lineno == 4


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

    assert result.target.resolution == "workspace"
    call_sites = sorted(
        (r for r in result.references if not r.is_declaration),
        key=lambda r: (r.path, r.lineno),
    )
    assert [(Path(r.path).name, r.lineno) for r in call_sites] == [
        ("b.py", 3),
        ("b.py", 4),
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
    assert ref.lineno == 3
    # ``a.foo()`` — the ``foo`` portion is at cols 2-5.
    assert (ref.col_offset, ref.end_col_offset) == (2, 5)


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
    (root / "b.py").write_text(
        "import a as alias\n\nalias.foo()\n", encoding="utf-8"
    )

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    assert len(non_decl) == 1
    ref = non_decl[0]
    assert ref.lineno == 3
    # ``alias.foo()`` — the ``foo`` portion is at cols 6-9.
    assert (ref.col_offset, ref.end_col_offset) == (6, 9)


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
    assert ref.lineno == 3
    assert (ref.col_offset, ref.end_col_offset) == (2, 5)


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
    (root / "b.py").write_text(
        "import other\n\nother.foo()\n", encoding="utf-8"
    )

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
    (root / "b.py").write_text(
        "x = 1\n\nx.foo\n", encoding="utf-8"
    )

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    assert non_decl == []


def test_find_references_attribute_chain_on_nested_module_lhs_skipped(
    tmp_path: Path,
) -> None:
    """``import pkg.subpkg`` plus ``pkg.subpkg.foo()`` is NOT counted —
    the LHS of ``foo`` is the Attribute ``pkg.subpkg``, not a Name, and the
    occurrence walker only emits a hint when ``Attribute.value`` is a Name.
    Use ``from pkg import subpkg; subpkg.foo()`` (or
    ``from pkg.subpkg import foo``) to opt in. Documented limitation."""
    root = tmp_path / "workspace"
    root.mkdir()
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "subpkg.py").write_text(
        "def foo() -> int:\n    return 1\n", encoding="utf-8"
    )
    (root / "b.py").write_text(
        "import pkg.subpkg\n\npkg.subpkg.foo()\n", encoding="utf-8"
    )

    db = Database(mode="strict")
    result = find_references(db, root, pkg / "subpkg.py", "foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    assert non_decl == []


def test_find_references_ignores_shadowing_local_known_limitation(
    tmp_path: Path,
) -> None:
    """Pins v1.2.0 behavior: a function-local binding that shadows a module-level
    name is still reported as a reference to the module-level target, because
    ``symbol_resolution`` does not track function-local scopes."""
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "mod.py"
    target.write_text(
        "def foo() -> int:\n    return 1\n\n"
        "def other() -> int:\n"
        "    foo = 42\n"
        "    return foo\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    result = find_references(db, root, target, "foo")

    # Both the local ``foo = 42`` (line 5) and the local ``return foo`` (line 6)
    # currently count as references because the resolver is module-scoped.
    linenos = sorted(r.lineno for r in result.references if not r.is_declaration)
    assert linenos == [5, 6]


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
        key=lambda r: (r.path, r.lineno, r.col_offset),
    )
    assert len(non_decl) == 2
    # `def g(a: 'Foo') -> 'Foo':` — opening quotes at col 9 and 19.
    param_ref, return_ref = non_decl
    assert param_ref.lineno == 3
    assert (param_ref.col_offset, param_ref.end_col_offset) == (10, 13)
    assert return_ref.lineno == 3
    assert (return_ref.col_offset, return_ref.end_col_offset) == (20, 23)


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
    assert ref.lineno == 4
    # `    x: 'Foo'` — opening quote at col 7, name at cols 8-11.
    assert (ref.col_offset, ref.end_col_offset) == (8, 11)


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
    assert non_decl[0].lineno == 3
    assert (non_decl[0].col_offset, non_decl[0].end_col_offset) == (4, 7)


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
        "from a import Foo\n\n"
        "def g(a: list['Foo']) -> dict[str, 'Foo']:\n"
        "    return a\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "Foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    assert len(non_decl) == 2
    assert all(r.lineno == 3 for r in non_decl)


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
    assert ref.lineno == 3
    # `def g(a: 'Foo | None') -> None:` — opening quote at col 9, name at 10-13.
    assert (ref.col_offset, ref.end_col_offset) == (10, 13)


def test_name_occurrences_for_file_extracts_attribute_inside_string_annotation(
    tmp_path: Path,
) -> None:
    """Inside `'pkg.Foo'`, the rightmost attribute name is emitted as an
    occurrence with the correct in-file offsets. The 5th payload field carries
    the LHS Name ``"pkg"`` so the references verifier can route the lookup
    through ``pkg``'s import binding."""
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "b.py"
    path.write_text(
        "import pkg\n\ndef g(a: 'pkg.Foo') -> None:\n    return None\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    occurrences = name_occurrences_for_file(db, str(path))

    matching = [occ for occ in occurrences if occ[0] == "Foo"]
    assert len(matching) == 1
    bare_name, lineno, col_offset, end_col_offset, value_name_hint = matching[0]
    assert bare_name == "Foo"
    assert lineno == 3
    # `def g(a: 'pkg.Foo') -> None:` — `Foo` is at cols 14-17.
    assert (col_offset, end_col_offset) == (14, 17)
    assert value_name_hint == "pkg"


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
    assert ref.lineno == 3
    # `def g(x: 'a.Foo') -> None:` — inside the string, `Foo` is at cols 12-15.
    assert (ref.col_offset, ref.end_col_offset) == (12, 15)


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
    assert ref.lineno == 3
    assert (ref.col_offset, ref.end_col_offset) == (10, 13)


def test_find_references_skips_malformed_string_annotation(tmp_path: Path) -> None:
    """A string annotation that doesn't parse as an expression is silently
    skipped — the call doesn't raise, and no spurious reference is emitted."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("class Foo:\n    pass\n", encoding="utf-8")
    b = root / "b.py"
    b.write_text(
        "from a import Foo\n\ndef g(a: 'this is not valid python'): ...\n"
        "Foo()\n",
        encoding="utf-8",
    )

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "Foo")

    non_decl = [r for r in result.references if not r.is_declaration]
    # Only the unquoted `Foo()` on line 4 is reported; the malformed
    # annotation contributes nothing (and doesn't crash).
    assert len(non_decl) == 1
    assert non_decl[0].lineno == 4


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


def test_find_references_skips_triple_quoted_string_annotation(
    tmp_path: Path,
) -> None:
    """Triple-quoted (single- or multi-line) string annotations are
    skipped. Vanishingly rare in real code."""
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
    assert non_decl == []


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
    assert ref.lineno == 6
    # `    x: 'Foo'` on line 6 — opening quote at col 7, name at 8-11.
    assert (ref.col_offset, ref.end_col_offset) == (8, 11)


def test_find_references_on_stdlib_target_returns_empty_with_target_carried(
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
    result = find_references(db, root, consumer, "JSONDecoder")

    assert result.target.resolution == "stdlib"
    assert result.references == ()


def test_find_references_on_ambiguous_target_returns_empty(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.py").write_text("from b import foo\n", encoding="utf-8")
    (root / "b.py").write_text("from a import foo\n", encoding="utf-8")

    db = Database(mode="strict")
    result = find_references(db, root, root / "a.py", "foo")

    assert result.target.resolution == "ambiguous"
    assert result.references == ()


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

    assert result.target.resolution == "workspace"
    assert sorted(
        (Path(r.path).name, r.lineno, r.is_declaration) for r in result.references
    ) == [
        ("a.py", 1, True),
        ("b.py", 3, False),
    ]


def test_comment_only_edit_backdates_name_occurrences_for_file(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "a.py"
    path.write_text("x = 1\nprint(x)\n", encoding="utf-8")

    db = Database(mode="strict")
    first = name_occurrences_for_file(db, str(path))

    path.write_text("x = 1\nprint(x)\n# trailing\n", encoding="utf-8")
    second = name_occurrences_for_file(db, str(path))

    assert first == second
    assert db.inspect(name_occurrences_for_file, str(path)).last_decision == "reused"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_find_references_matches_fresh_recomputation_over_edits(
    mode: str, tmp_path: Path
) -> None:
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


def test_workspace_name_occurrence_index_skips_missing_syntax(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "good.py").write_text("x = 1\nprint(x)\n", encoding="utf-8")
    (root / "broken.py").write_text("def (\n", encoding="utf-8")

    db = Database(mode="strict")
    index = workspace_name_occurrence_index(db, str(root))
    mapping = dict(index)

    assert mapping[str(root / "broken.py")] == ()
    good_names = {entry[0] for entry in mapping[str(root / "good.py")]}
    assert "x" in good_names
    assert "print" in good_names


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
        "try:\n"
        "    import tomllib\n"
        "except ModuleNotFoundError:\n"
        "    import tomli as tomllib\n",
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
        "try:\n" "    import ujson\n" "except:\n" "    pass\n",
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
def test_class_model_captures_class_body_and_init_members(
    mode: str, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "widget.py"
    path.write_text(_CLASS_MODEL_SAMPLE, encoding="utf-8")

    model = class_model(Database(mode=mode), root, path, "Widget")
    assert model.path == str(path)
    assert model.qualified_name == "Widget"

    by_name = {member.name: member for member in model.members}

    assert by_name["size"].kind == "class_variable"
    assert by_name["size"].lineno == 2
    assert by_name["size"].annotation == "int"
    assert by_name["size"].signature is None

    assert by_name["tag"].kind == "class_variable"
    assert by_name["tag"].lineno == 3
    assert by_name["tag"].annotation is None

    assert by_name["__init__"].kind == "method"
    assert by_name["__init__"].lineno == 5
    assert by_name["__init__"].signature == Signature(
        parameters=(
            Parameter(name="self", annotation=None),
            Parameter(name="name", annotation="str"),
        ),
        return_annotation="None",
    )

    assert by_name["render"].kind == "method"
    assert by_name["render"].lineno == 9

    assert by_name["name"].kind == "instance_variable"
    assert by_name["name"].lineno == 6
    assert by_name["name"].annotation is None
    assert by_name["count"].kind == "instance_variable"
    assert by_name["count"].lineno == 7
    assert by_name["count"].annotation == "int"
    assert by_name["cache"].kind == "instance_variable"
    assert by_name["cache"].lineno == 10

    # Every own member is attributed to this file and class.
    for member in model.members:
        assert member.defining_path == str(path)
        assert member.defining_class == "Widget"

    # A single base is recorded but not followed in Stage 1.
    assert model.unresolved_bases == ("Base",)


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
    assert by_name["x"].lineno == 3  # __init__ occurrence, not the reset() rebind
    assert by_name["y"].lineno == 6


def test_class_model_requires_literal_self_first_param(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "c.py"
    path.write_text(
        "class C:\n"
        "    def m(this) -> None:\n"
        "        this.z = 1\n",
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
    instance_names = {
        member.name
        for member in model.members
        if member.kind == "instance_variable"
    }
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
    instance_names = {
        member.name
        for member in model.members
        if member.kind == "instance_variable"
    }
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
    # path, qualified_name, members, unresolved_bases
    assert payload[2] == ()
    assert payload[3] == ()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_class_model_matches_fresh_recomputation_over_edits(
    mode: str, tmp_path: Path
) -> None:
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

    path.write_text(_CLASS_MODEL_SAMPLE + "# trailing comment\n", encoding="utf-8")
    second = class_model(db, root, path, "Widget")

    assert first == second
    assert db.inspect(class_models_for_file, str(path)).last_decision == "reused"


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
        "class Base:\n"
        "    def render(self) -> str:\n"
        "        return 'base'\n",
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
    assert renders[0].lineno == 5  # def render in derived.py
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
    base_src = (
        "class Base:\n"
        "    def shared(self) -> None:\n"
        "        pass\n"
    )
    _write_file(base, base_src)
    derived = root / "derived.py"
    _write_file(
        derived,
        "from base import Base\n\n\nclass Derived(Base):\n    pass\n",
    )

    db = Database(mode="strict")
    first = class_model(db, root, derived, "Derived")

    # A trailing-comment edit to the BASE file leaves the AST attributes
    # untouched, so class_models_for_file(base) backdates and the derived
    # flattened model is reused unchanged.
    base.write_text(base_src + "# trailing comment\n", encoding="utf-8")
    second = class_model(db, root, derived, "Derived")

    assert first == second
    assert {m.name for m in second.members} == {"shared"}
    assert db.inspect(class_models_for_file, str(base)).last_decision == "reused"


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
