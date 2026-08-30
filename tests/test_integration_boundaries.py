"""What the integration surface is, and where it may be called from.

The first cells lock the shape of the package: cross-module imports go through
declared contracts, and a payload query stays out of the package-level surface.

The rest lock the calling context. A high-level entrypoint is called from
outside a query; reaching one from inside a query body is refused. Every
documented entrypoint is driven both ways here -- from inside a real query
body, where none of them runs, and from outside one, where all of them answer.
The property harness cannot stand in for this: it reaches the entrypoints from
plain test bodies and never from a query, so it holds no opinion about the
calling context at all. That is why the composition family lives beside the
surface lock rather than in the harness.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path

import pytest

import pyinc.integrations as integrations
from pyinc import (
    CompositionError,
    Database,
    PyIncError,
    Query,
    UnsupportedValueError,
    query,
)
from pyinc.integrations import (
    SourcePosition,
    SymbolId,
    applicable_requirements,
    class_model,
    config_analysis,
    csv_analysis,
    deep_module_resolution_analysis,
    deep_requirements_analysis,
    dependency_check_analysis,
    directory_analysis,
    env_analysis,
    evaluate_markers,
    evaluate_version_specifier,
    file_analysis,
    find_references,
    installed_packages_analysis,
    json_analysis,
    module_analysis,
    module_symbol_table,
    notebook_analysis,
    requirements_analysis,
    resolve_import_name,
    resolve_module_path,
    scope_tree,
    symbol_at,
    workspace_analysis,
    workspace_applicable_requirements,
    workspace_config_analysis,
    workspace_csv_analysis,
    workspace_dependency_check,
    workspace_env_analysis,
    workspace_json_analysis,
    workspace_notebook_analysis,
    workspace_requirements_analysis,
    workspace_symbol_index,
    workspace_xml_analysis,
    xml_analysis,
)

_INTEGRATIONS = Path(__file__).parents[1] / "src" / "pyinc" / "integrations"
_CONTRACT = Path(__file__).parents[1] / "docs" / "integration-contract.md"
_INTERNAL_MODULE_GROUPS = (frozenset({"scope_resolution", "symbol_resolution"}),)


def _declared_exports(module: str) -> frozenset[str]:
    tree = ast.parse((_INTEGRATIONS / f"{module}.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            continue
        value = ast.literal_eval(node.value)
        return frozenset(value)
    return frozenset()


def _integration_import(node: ast.ImportFrom) -> str | None:
    if node.module is None:
        return None
    if node.level == 1:
        return node.module.split(".", 1)[0]
    prefix = "pyinc.integrations."
    if node.level == 0 and node.module.startswith(prefix):
        return node.module.removeprefix(prefix).split(".", 1)[0]
    return None


def test_cross_integration_imports_use_declared_composition_contracts() -> None:
    violations: list[str] = []
    for path in sorted(_INTEGRATIONS.glob("*.py")):
        source_module = path.stem
        if source_module == "__init__":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            target_module = _integration_import(node)
            if (
                target_module is None
                or target_module == source_module
                or target_module.startswith("_")
                or any({source_module, target_module} <= group for group in _INTERNAL_MODULE_GROUPS)
            ):
                continue
            exports = _declared_exports(target_module)
            for imported in node.names:
                if imported.name != "*" and imported.name not in exports:
                    violations.append(
                        f"{path.name}:{node.lineno} imports undeclared "
                        f"{target_module}.{imported.name}"
                    )
    assert violations == []


def test_requirements_payload_is_composable_but_not_package_level() -> None:
    from pyinc import integrations
    from pyinc.integrations import requirements_txt

    assert "RequirementPayload" in requirements_txt.__all__
    assert "requirements_payload" in requirements_txt.__all__
    assert "RequirementPayload" not in integrations.__all__
    assert "requirements_payload" not in integrations.__all__


# ---------------------------------------------------------------------------
# The documented entrypoint surface
# ---------------------------------------------------------------------------


def _exported_plain_functions() -> frozenset[str]:
    # `inspect.isfunction`, not an identity check against `FunctionType`: one
    # of these names is a context manager built by a decorator, and a narrower
    # predicate silently drops it and compares 37 names to 38.
    return frozenset(
        name for name in integrations.__all__ if inspect.isfunction(getattr(integrations, name))
    )


def _documented_entrypoint_names(document: str) -> frozenset[str]:
    names: set[str] = set()
    for line in document.splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 4 or cells[1] != "Entrypoints":
            continue
        names.update(re.findall(r"`([^`]+)`", cells[2]))
    return frozenset(names)


def _entrypoint_drift(
    document: str, exported: frozenset[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    documented = _documented_entrypoint_names(document)
    return tuple(sorted(documented - exported)), tuple(sorted(exported - documented))


_ROWS_FIXTURE = """\
## A section

| Contract item | Stable surface |
|---|---|
| Purpose | Say what the section covers. |
| Entrypoints | `alpha`, `beta` |
| Result types | `Gamma`, `Delta` |
| Key limits | It does not do the other thing. |
"""

# A name that is a callable part of the surface but is filed under the row kind
# reserved for records. A check that pools every row kind into one set sees it
# documented and reports nothing; reading the entrypoint rows alone is what
# makes it visible.
_ROWS_WITH_AN_ENTRYPOINT_FILED_AS_A_RESULT = """\
| Contract item | Stable surface |
|---|---|
| Entrypoints | `alpha` |
| Result types | `beta`, `Gamma` |
"""

# The same drift in the other direction: a record type listed as something a
# caller invokes.
_ROWS_WITH_A_RESULT_FILED_AS_AN_ENTRYPOINT = """\
| Contract item | Stable surface |
|---|---|
| Entrypoints | `alpha`, `Gamma` |
| Result types | `Delta` |
"""


def test_the_entrypoint_row_parse_separates_the_row_kinds() -> None:
    assert _entrypoint_drift(_ROWS_FIXTURE, frozenset({"alpha", "beta"})) == ((), ())


def test_an_entrypoint_documented_only_as_a_result_type_is_reported() -> None:
    assert _entrypoint_drift(
        _ROWS_WITH_AN_ENTRYPOINT_FILED_AS_A_RESULT, frozenset({"alpha", "beta"})
    ) == ((), ("beta",))


def test_a_result_type_documented_as_an_entrypoint_is_reported() -> None:
    assert _entrypoint_drift(
        _ROWS_WITH_A_RESULT_FILED_AS_AN_ENTRYPOINT, frozenset({"alpha"})
    ) == (("Gamma",), ())


def test_the_entrypoint_rows_name_a_real_entrypoint() -> None:
    # Without this the lock below can pass on an empty parse.
    assert "deep_requirements_analysis" in _documented_entrypoint_names(
        _CONTRACT.read_text(encoding="utf-8")
    )


def test_the_documented_entrypoints_are_the_packages_plain_functions() -> None:
    document = _CONTRACT.read_text(encoding="utf-8")
    exported = _exported_plain_functions()

    assert _entrypoint_drift(document, exported) == ((), ())
    assert len(_documented_entrypoint_names(document)) == 38
    assert len(exported) == 38


# ---------------------------------------------------------------------------
# The composition boundary
# ---------------------------------------------------------------------------

# These three declare and use a request span rather than analyze anything, and
# they are deliberately outside the rule below: one of them is called from
# inside the entrypoints themselves, so refusing them inside a query would
# refuse the entrypoints' own work.
_REQUEST_SCOPING = frozenset({"once_per_request", "request_inputs_changed", "request_scope"})

#: Every high-level entrypoint that refuses a query body. A new entrypoint
#: needs its refusal, a driver below, and its name here; the cell that drives
#: this set checks the three agree, and reports what appeared or went missing.
_GUARDED_ENTRYPOINTS: frozenset[str] = frozenset(
    {
        "applicable_requirements",
        "class_model",
        "config_analysis",
        "csv_analysis",
        "deep_module_resolution_analysis",
        "deep_requirements_analysis",
        "dependency_check_analysis",
        "directory_analysis",
        "env_analysis",
        "evaluate_markers",
        "evaluate_version_specifier",
        "file_analysis",
        "find_references",
        "installed_packages_analysis",
        "json_analysis",
        "module_analysis",
        "module_symbol_table",
        "notebook_analysis",
        "requirements_analysis",
        "resolve_import_name",
        "resolve_module_path",
        "scope_tree",
        "symbol_at",
        "workspace_analysis",
        "workspace_applicable_requirements",
        "workspace_config_analysis",
        "workspace_csv_analysis",
        "workspace_dependency_check",
        "workspace_env_analysis",
        "workspace_json_analysis",
        "workspace_notebook_analysis",
        "workspace_requirements_analysis",
        "workspace_symbol_index",
        "workspace_xml_analysis",
        "xml_analysis",
    }
)

_MODULE_SOURCE = '''\
"""A module the scope and symbol entrypoints have something to say about."""

import json


class Alpha:
    """A class with one method."""

    def beta(self) -> int:
        return len(json.dumps({}))


def gamma() -> int:
    return Alpha().beta()
'''

_NOTEBOOK = {
    "cells": [{"cell_type": "code", "source": ["import json\n"], "metadata": {}}],
    "metadata": {},
    "nbformat": 4,
    "nbformat_minor": 5,
}

# `class Alpha:` is the sixth line of the module source above, and the name
# starts at its seventh column.
_ALPHA = SourcePosition(line=5, character=6)


def _build_workspace(root: Path) -> None:
    """Write a workspace every documented entrypoint has a real answer about."""
    package = root / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "mod.py").write_text(_MODULE_SOURCE, encoding="utf-8")
    (root / "requirements.txt").write_text("flask\n", encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    (root / "package.json").write_text('{"name": "demo"}\n', encoding="utf-8")
    (root / ".env").write_text("TOKEN=1\n", encoding="utf-8")
    (root / "pom.xml").write_text(
        "<project><artifactId>demo</artifactId></project>\n", encoding="utf-8"
    )
    (root / "data.csv").write_text("name,age\nAlice,30\n", encoding="utf-8")
    (root / "book.ipynb").write_text(json.dumps(_NOTEBOOK), encoding="utf-8")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    _build_workspace(tmp_path)
    return tmp_path


def _entrypoint_arguments(db: Database, root: Path) -> dict[str, tuple[object, ...]]:
    """One correct argument list per entrypoint.

    Binding happens before the body, so an argument list of the wrong length
    raises before an entrypoint can refuse anything and the census below
    reports a refusal that never happened. Six entrypoints take three
    arguments and one takes four, and one of those needs a symbol identity --
    which is built here by asking for it outside a query, the only place the
    question can be asked.
    """
    top = str(root)
    module = str(root / "pkg" / "mod.py")
    symbol = symbol_at(db, module, _ALPHA)
    assert symbol is not None
    return {
        "applicable_requirements": (str(root / "requirements.txt"),),
        "class_model": (top, module, "Alpha"),
        "config_analysis": (str(root / "pyproject.toml"),),
        "csv_analysis": (str(root / "data.csv"),),
        "deep_module_resolution_analysis": (),
        "deep_requirements_analysis": (str(root / "requirements.txt"),),
        "dependency_check_analysis": (("flask",),),
        "directory_analysis": (top,),
        "env_analysis": (str(root / ".env"),),
        "evaluate_markers": ("python_version >= '3.8'",),
        "evaluate_version_specifier": (">=1.0", "1.2"),
        "file_analysis": (module,),
        "find_references": (top, symbol),
        "installed_packages_analysis": (),
        "json_analysis": (str(root / "package.json"),),
        "module_analysis": (top, module),
        "module_symbol_table": (top, module),
        "notebook_analysis": (str(root / "book.ipynb"),),
        "requirements_analysis": (str(root / "requirements.txt"),),
        "resolve_import_name": ("json",),
        "resolve_module_path": ("json",),
        "scope_tree": (module,),
        "symbol_at": (module, _ALPHA),
        "workspace_analysis": (top,),
        "workspace_applicable_requirements": (top,),
        "workspace_config_analysis": (top,),
        "workspace_csv_analysis": (top,),
        "workspace_dependency_check": (top, ("flask",)),
        "workspace_env_analysis": (top,),
        "workspace_json_analysis": (top,),
        "workspace_notebook_analysis": (top,),
        "workspace_requirements_analysis": (top,),
        "workspace_symbol_index": (top,),
        "workspace_xml_analysis": (top,),
        "xml_analysis": (str(root / "pom.xml"),),
    }


# One query per entrypoint, each naming its entrypoint the way a user's query
# would: as a module global of the module the query is defined in. Every one
# returns the same marker, so a driver that answers at all is a driver whose
# entrypoint ran.


@query
def _in_query_applicable_requirements(db: Database, path: str) -> str:
    applicable_requirements(db, path)
    return "the entrypoint answered"


@query
def _in_query_class_model(db: Database, root: str, path: str, qualified_name: str) -> str:
    class_model(db, root, path, qualified_name)
    return "the entrypoint answered"


@query
def _in_query_config_analysis(db: Database, path: str) -> str:
    config_analysis(db, path)
    return "the entrypoint answered"


@query
def _in_query_csv_analysis(db: Database, path: str) -> str:
    csv_analysis(db, path)
    return "the entrypoint answered"


@query
def _in_query_deep_module_resolution_analysis(db: Database) -> str:
    deep_module_resolution_analysis(db)
    return "the entrypoint answered"


@query
def _in_query_deep_requirements_analysis(db: Database, path: str) -> str:
    deep_requirements_analysis(db, path)
    return "the entrypoint answered"


@query
def _in_query_dependency_check_analysis(db: Database, declared_deps: tuple[str, ...]) -> str:
    dependency_check_analysis(db, declared_deps)
    return "the entrypoint answered"


@query
def _in_query_directory_analysis(db: Database, root: str) -> str:
    directory_analysis(db, root)
    return "the entrypoint answered"


@query
def _in_query_env_analysis(db: Database, path: str) -> str:
    env_analysis(db, path)
    return "the entrypoint answered"


@query
def _in_query_evaluate_markers(db: Database, marker: str) -> str:
    evaluate_markers(db, marker)
    return "the entrypoint answered"


@query
def _in_query_evaluate_version_specifier(db: Database, specifier: str, version: str) -> str:
    evaluate_version_specifier(db, specifier, version)
    return "the entrypoint answered"


@query
def _in_query_file_analysis(db: Database, path: str) -> str:
    file_analysis(db, path)
    return "the entrypoint answered"


@query
def _in_query_find_references(db: Database, root: str, symbol_id: SymbolId) -> str:
    find_references(db, root, symbol_id)
    return "the entrypoint answered"


@query
def _in_query_installed_packages_analysis(db: Database) -> str:
    installed_packages_analysis(db)
    return "the entrypoint answered"


@query
def _in_query_json_analysis(db: Database, path: str) -> str:
    json_analysis(db, path)
    return "the entrypoint answered"


@query
def _in_query_module_analysis(db: Database, root: str, path: str) -> str:
    module_analysis(db, root, path)
    return "the entrypoint answered"


@query
def _in_query_module_symbol_table(db: Database, root: str, path: str) -> str:
    module_symbol_table(db, root, path)
    return "the entrypoint answered"


@query
def _in_query_notebook_analysis(db: Database, path: str) -> str:
    notebook_analysis(db, path)
    return "the entrypoint answered"


@query
def _in_query_requirements_analysis(db: Database, path: str) -> str:
    requirements_analysis(db, path)
    return "the entrypoint answered"


@query
def _in_query_resolve_import_name(db: Database, import_name: str) -> str:
    resolve_import_name(db, import_name)
    return "the entrypoint answered"


@query
def _in_query_resolve_module_path(db: Database, dotted_name: str) -> str:
    resolve_module_path(db, dotted_name)
    return "the entrypoint answered"


@query
def _in_query_scope_tree(db: Database, path: str) -> str:
    scope_tree(db, path)
    return "the entrypoint answered"


@query
def _in_query_symbol_at(db: Database, path: str, position: SourcePosition) -> str:
    symbol_at(db, path, position)
    return "the entrypoint answered"


@query
def _in_query_workspace_analysis(db: Database, root: str) -> str:
    workspace_analysis(db, root)
    return "the entrypoint answered"


@query
def _in_query_workspace_applicable_requirements(db: Database, root: str) -> str:
    workspace_applicable_requirements(db, root)
    return "the entrypoint answered"


@query
def _in_query_workspace_config_analysis(db: Database, root: str) -> str:
    workspace_config_analysis(db, root)
    return "the entrypoint answered"


@query
def _in_query_workspace_csv_analysis(db: Database, root: str) -> str:
    workspace_csv_analysis(db, root)
    return "the entrypoint answered"


@query
def _in_query_workspace_dependency_check(
    db: Database, root: str, declared_deps: tuple[str, ...]
) -> str:
    workspace_dependency_check(db, root, declared_deps)
    return "the entrypoint answered"


@query
def _in_query_workspace_env_analysis(db: Database, root: str) -> str:
    workspace_env_analysis(db, root)
    return "the entrypoint answered"


@query
def _in_query_workspace_json_analysis(db: Database, root: str) -> str:
    workspace_json_analysis(db, root)
    return "the entrypoint answered"


@query
def _in_query_workspace_notebook_analysis(db: Database, root: str) -> str:
    workspace_notebook_analysis(db, root)
    return "the entrypoint answered"


@query
def _in_query_workspace_requirements_analysis(db: Database, root: str) -> str:
    workspace_requirements_analysis(db, root)
    return "the entrypoint answered"


@query
def _in_query_workspace_symbol_index(db: Database, root: str) -> str:
    workspace_symbol_index(db, root)
    return "the entrypoint answered"


@query
def _in_query_workspace_xml_analysis(db: Database, root: str) -> str:
    workspace_xml_analysis(db, root)
    return "the entrypoint answered"


@query
def _in_query_xml_analysis(db: Database, path: str) -> str:
    xml_analysis(db, path)
    return "the entrypoint answered"


_DRIVERS: dict[str, Query[..., str]] = {
    "applicable_requirements": _in_query_applicable_requirements,
    "class_model": _in_query_class_model,
    "config_analysis": _in_query_config_analysis,
    "csv_analysis": _in_query_csv_analysis,
    "deep_module_resolution_analysis": _in_query_deep_module_resolution_analysis,
    "deep_requirements_analysis": _in_query_deep_requirements_analysis,
    "dependency_check_analysis": _in_query_dependency_check_analysis,
    "directory_analysis": _in_query_directory_analysis,
    "env_analysis": _in_query_env_analysis,
    "evaluate_markers": _in_query_evaluate_markers,
    "evaluate_version_specifier": _in_query_evaluate_version_specifier,
    "file_analysis": _in_query_file_analysis,
    "find_references": _in_query_find_references,
    "installed_packages_analysis": _in_query_installed_packages_analysis,
    "json_analysis": _in_query_json_analysis,
    "module_analysis": _in_query_module_analysis,
    "module_symbol_table": _in_query_module_symbol_table,
    "notebook_analysis": _in_query_notebook_analysis,
    "requirements_analysis": _in_query_requirements_analysis,
    "resolve_import_name": _in_query_resolve_import_name,
    "resolve_module_path": _in_query_resolve_module_path,
    "scope_tree": _in_query_scope_tree,
    "symbol_at": _in_query_symbol_at,
    "workspace_analysis": _in_query_workspace_analysis,
    "workspace_applicable_requirements": _in_query_workspace_applicable_requirements,
    "workspace_config_analysis": _in_query_workspace_config_analysis,
    "workspace_csv_analysis": _in_query_workspace_csv_analysis,
    "workspace_dependency_check": _in_query_workspace_dependency_check,
    "workspace_env_analysis": _in_query_workspace_env_analysis,
    "workspace_json_analysis": _in_query_workspace_json_analysis,
    "workspace_notebook_analysis": _in_query_workspace_notebook_analysis,
    "workspace_requirements_analysis": _in_query_workspace_requirements_analysis,
    "workspace_symbol_index": _in_query_workspace_symbol_index,
    "workspace_xml_analysis": _in_query_workspace_xml_analysis,
    "xml_analysis": _in_query_xml_analysis,
}


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_no_high_level_entrypoint_runs_inside_a_query(mode: str, workspace: Path) -> None:
    measured = _exported_plain_functions() - _REQUEST_SCOPING
    assert measured == _GUARDED_ENTRYPOINTS, (
        f"appeared: {sorted(measured - _GUARDED_ENTRYPOINTS)} | "
        f"gone: {sorted(_GUARDED_ENTRYPOINTS - measured)}"
    )
    assert frozenset(_DRIVERS) == _GUARDED_ENTRYPOINTS

    db = Database(mode=mode)
    arguments = _entrypoint_arguments(db, workspace)
    assert frozenset(arguments) == _GUARDED_ENTRYPOINTS

    # Two refusals reach a caller here and the difference is not part of the
    # rule: one is the refusal the entrypoint owes a query body, the other the
    # kernel's own objection to what such a query captures. Which name gives
    # which is a judgement about the caller's compiled code that moves between
    # interpreter versions, so nothing below records it. What is pinned is
    # that the entrypoint never ran and that both refusals share a base a
    # caller can catch.
    reached: dict[str, str] = {}
    for name in sorted(_GUARDED_ENTRYPOINTS):
        try:
            answer = db.get(_DRIVERS[name], *arguments[name])
        except (CompositionError, UnsupportedValueError) as refusal:
            assert isinstance(refusal, PyIncError)
        except Exception as other:
            reached[name] = f"raised {type(other).__name__}: {other}"
        else:
            reached[name] = f"ran and {answer}"
    assert reached == {}, f"reached from inside a query body: {reached}"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_every_high_level_entrypoint_answers_outside_a_query(mode: str, workspace: Path) -> None:
    db = Database(mode=mode)
    arguments = _entrypoint_arguments(db, workspace)
    assert frozenset(arguments) == _GUARDED_ENTRYPOINTS

    answers = {
        name: getattr(integrations, name)(db, *arguments[name])
        for name in sorted(_GUARDED_ENTRYPOINTS)
    }
    assert [name for name, answer in answers.items() if answer is None] == []

    # The refusal reads the calling context and must read it the right way
    # round, so these pin real answers rather than the absence of a raise.
    assert answers["file_analysis"].path == arguments["file_analysis"][0]
    assert answers["scope_tree"].path == arguments["scope_tree"][0]
    assert answers["symbol_at"].name == "Alpha"
    assert answers["csv_analysis"].row_count == 1
    assert answers["class_model"].qualified_name == "Alpha"
