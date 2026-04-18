from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

import pyinc.integrations as integrations
from pyinc import Database
from pyinc.integrations.symbol_resolution import (
    Parameter,
    Signature,
    module_symbol_table,
    module_symbol_table_payload,
    resolve_symbol,
    resolve_symbol_payload,
    workspace_symbol_index,
)

Operation = tuple[Literal["write", "delete"], str, str | None]


# ---------------------------------------------------------------------------
# Contract lock
# ---------------------------------------------------------------------------


def test_symbol_resolution_all_list_is_exact() -> None:
    from pyinc.integrations import symbol_resolution

    assert set(symbol_resolution.__all__) == {
        "ModuleSymbolTable",
        "Parameter",
        "ResolvedSymbol",
        "Signature",
        "Symbol",
        "WorkspaceSymbolEntry",
        "WorkspaceSymbolIndex",
        "module_symbol_table",
        "resolve_symbol",
        "workspace_symbol_index",
    }


def test_symbol_resolution_stable_surface_on_integrations_namespace() -> None:
    for name in (
        "ModuleSymbolTable",
        "Parameter",
        "ResolvedSymbol",
        "Signature",
        "Symbol",
        "WorkspaceSymbolEntry",
        "WorkspaceSymbolIndex",
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


def test_resolve_symbol_depth_cap_terminates_at_max_follow_depth(tmp_path: Path) -> None:
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
        "# spacer\n"
        "def foo(x: int) -> int:\n"
        "    return x\n",
        encoding="utf-8",
    )
    second = resolve_symbol(db, root, b, "foo")

    assert second.defining_lineno == 2
    assert db.inspect(module_symbol_table_payload, str(a)).last_recompute == "executed"
    assert db.inspect(resolve_symbol_payload, str(root), str(b), "foo").last_recompute == "executed"


# ---------------------------------------------------------------------------
# Conditional top-level
# ---------------------------------------------------------------------------


def test_conditional_top_level_binding_marked_impure(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "cond.py"
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
    assert "conditional top-level binding" in table.impurity_reasons

    resolved = resolve_symbol(db, root, path, "hidden")
    assert resolved.resolution == "missing"


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
        "def util() -> int:\n    return 1\n"
        "class Holder:\n    pass\n",
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        "from pkg.helper import util\n"
        "flag: bool = True\n",
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
        assert resolve_symbol(incremental, root, b, "foo") == resolve_symbol(fresh, root, b, "foo")


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
