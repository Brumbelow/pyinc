"""What the integration surface is, and where it may be called from.

The first cells lock the shape of the package. Cross-module imports go through
declared contracts, and a payload query stays out of the package-level surface.

The rest lock the calling context. A high-level entrypoint is called from
outside a query, and a query body that reaches one is refused. Every documented
entrypoint is driven both ways here: from inside a real query body, where each
is refused, and from outside one, where each answers. The property harness
reaches the entrypoints only from plain test bodies, so it says nothing about
the calling context. That is why the composition family lives beside the
surface lock and outside the harness.

A further group varies how the query spells the name: through a local import,
through the entrypoint's module, or in a branch the body never takes. The rule
has to hold for every spelling. A rule that covered only one would leave the
boundary where it was. Every cell there pins that the entrypoint does not run.
Which of the two refusals arrives is outside the rule.

The last group reads two entrypoints directly, outside any query, on either
side of a link retargeted underneath them. With the boundary above in place,
that is the only way to ask the question.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    ScopeTree,
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
    python_source,
    requirements_analysis,
    resolve_import_name,
    resolve_module_path,
    scope_tree,
    symbol_at,
    symbol_resolution,
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
from pyinc.integrations._decoding import _reject_in_query
from pyinc.resources import ResolvedPathResource

if TYPE_CHECKING:
    from scripts.check_docs import _INLINE_CODE, table_rows
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from check_docs import _INLINE_CODE, table_rows  # noqa: E402

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
    # Use `inspect.isfunction`. One of these names is a context manager built
    # by a decorator, and an identity check against `FunctionType` silently
    # drops it and compares 37 names to 38.
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

# A callable name from the surface, filed under the row kind reserved for
# records. A check that pools every row kind into one set sees it documented
# and reports nothing. Reading only the entrypoint rows makes it visible.
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
    # Guards the lock below against passing on an empty parse.
    assert "deep_requirements_analysis" in _documented_entrypoint_names(
        _CONTRACT.read_text(encoding="utf-8")
    )


def test_the_documented_entrypoints_are_the_packages_plain_functions() -> None:
    document = _CONTRACT.read_text(encoding="utf-8")
    exported = _exported_plain_functions()

    assert _entrypoint_drift(document, exported) == ((), ())
    assert len(_documented_entrypoint_names(document)) == 38
    assert len(exported) == 38


def test_the_entrypoint_rows_read_the_same_through_the_shared_row_parser() -> None:
    """Two readers of one table have to agree about what the table says.

    The reader above splits the row on pipes and keeps the outer ones. The
    documentation checker reads the same tables through a shared parser that
    strips them and tracks the heading each row sits under. The count pinned
    above checks only a cardinality, so either reader could narrow on its own
    and stay green while the other kept finding the names.
    """
    document = _CONTRACT.read_text(encoding="utf-8")

    shared = {
        name
        for row in table_rows(document)
        if len(row.cells) == 2 and row.cells[0] == "Entrypoints"
        for name in _INLINE_CODE.findall(row.cells[1])
    }

    assert shared, "the shared parser found no entrypoint rows"
    assert _documented_entrypoint_names(document) == shared


# ---------------------------------------------------------------------------
# The composition boundary
# ---------------------------------------------------------------------------

# These three declare and use a request span and analyze nothing. They are
# exempt from the rule below because one of them is called from inside the
# entrypoints themselves, so refusing them inside a query would refuse the
# entrypoints' own work.
_REQUEST_SCOPING = frozenset({"once_per_request", "request_inputs_changed", "request_scope"})

#: Every high-level entrypoint that refuses a query body. A new entrypoint
#: needs its refusal, a driver below, and its name here. The cell that drives
#: this set checks the three agree and reports what appeared or went missing.
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
    raises before an entrypoint can refuse anything, and the census below
    reports a refusal that never happened. Six entrypoints take three
    arguments and one takes four. One of those needs a symbol identity, built
    here by asking for it outside a query, the only place the question can be
    asked.
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

    # Two refusals can reach a caller here, and the rule treats them alike.
    # One is the refusal the entrypoint owes a query body. The other is the
    # kernel's objection to what such a query captures. Which name gets which
    # depends on how each interpreter version compiles the caller, so the test
    # leaves it unrecorded. It pins that the entrypoint never ran and that both
    # refusals share a base a caller can catch.
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
    # round, so these pin real answers. A missing raise alone proves too little.
    assert answers["file_analysis"].path == arguments["file_analysis"][0]
    assert answers["scope_tree"].path == arguments["scope_tree"][0]
    assert answers["symbol_at"].name == "Alpha"
    assert answers["csv_analysis"].row_count == 1
    assert answers["class_model"].qualified_name == "Alpha"


# ---------------------------------------------------------------------------
# The shapes a query can name an entrypoint by
# ---------------------------------------------------------------------------

# What a driver returns when its entrypoint ran, and when the body finished
# before reaching the call. Keeping the two apart lets a clean answer still be
# evidence. An answer that came back is either the one shape that skips the
# call, or a driver that ran what it should not have.
_RAN = "the entrypoint answered"
_NOT_REACHED = "the call was never reached"
_REFUSED = "refused by "

_SPELLINGS = ("dead-code", "function-local-import", "module-attribute")


def _drive(db: Database, driver: Query[..., str], *arguments: object) -> str:
    """Say what a driving query did, without deciding what refused it.

    Two refusals reach a caller across these spellings: the one an entrypoint
    owes a query body, and the kernel's objection to what such a query
    captures. Which one arrives depends on how each interpreter version
    compiles the caller, so the answer records only that a refusal happened.
    The failure message carries its name.
    """
    try:
        return db.get(driver, *arguments)
    except (CompositionError, UnsupportedValueError) as refusal:
        assert isinstance(refusal, PyIncError)
        return _REFUSED + type(refusal).__name__


def _payload_records(db: Database, payload_query: str) -> tuple[str, ...]:
    """The records the entrypoint's payload query would have left behind.

    Every subject below calls a cached query named after itself as its first
    act past the refusal. Whether that query left a record shows whether the
    entrypoint got past its own front door. Each cell proves the read works in
    its own mode: afterwards it calls the entrypoint from outside a query and
    finds the record it left. The name is anchored on both sides because a
    label carries the defining module in front and an argument digest behind.
    """
    anchor = f":{payload_query}["
    return tuple(node.label for node in db.dependency_graph() if anchor in node.label)


def _assert_never_executed(outcome: str, db: Database, payload_query: str) -> None:
    """The whole rule: the entrypoint did not run, whatever came back instead."""
    assert outcome.startswith(_REFUSED) or outcome == _NOT_REACHED, outcome
    assert _payload_records(db, payload_query) == ()


# One driver per spelling per subject. The two subjects cover both things the
# kernel does with an ordinary caller. `directory_analysis` hides its decode
# work inside a generator expression, and the kernel admits a query naming it,
# so the refusal it meets is its own. The kernel turns `workspace_symbol_index`
# away before any body runs. The two supported interpreters read its body
# differently and agree on the verdict only because a name they both see is
# objected to first. Both cells check what happened and leave the capture set
# unread.


@query
def _dead_code_directory_analysis(db: Database, root: str, reach_the_call: bool) -> str:
    # The flag is an argument, so the branch is decided while the body runs.
    # With `if False:` the compiler drops the branch and the name never reaches
    # the caller's code object, so the cell would test the compiler.
    if reach_the_call:
        directory_analysis(db, root)
        return _RAN
    return _NOT_REACHED


@query
def _local_import_directory_analysis(db: Database, root: str) -> str:
    from pyinc.integrations.python_source import directory_analysis

    directory_analysis(db, root)
    return _RAN


@query
def _module_attribute_directory_analysis(db: Database, root: str) -> str:
    python_source.directory_analysis(db, root)
    return _RAN


@query
def _dead_code_workspace_symbol_index(db: Database, root: str, reach_the_call: bool) -> str:
    if reach_the_call:
        workspace_symbol_index(db, root)
        return _RAN
    return _NOT_REACHED


@query
def _local_import_workspace_symbol_index(db: Database, root: str) -> str:
    from pyinc.integrations.symbol_resolution import workspace_symbol_index

    workspace_symbol_index(db, root)
    return _RAN


@query
def _module_attribute_workspace_symbol_index(db: Database, root: str) -> str:
    symbol_resolution.workspace_symbol_index(db, root)
    return _RAN


@query
def _local_import_file_analysis(db: Database, path: str) -> str:
    from pyinc.integrations.python_source import file_analysis

    file_analysis(db, path)
    return _RAN


_BYPASS_DRIVERS: dict[str, dict[str, Query[..., str]]] = {
    "directory_analysis": {
        "dead-code": _dead_code_directory_analysis,
        "function-local-import": _local_import_directory_analysis,
        "module-attribute": _module_attribute_directory_analysis,
    },
    "workspace_symbol_index": {
        "dead-code": _dead_code_workspace_symbol_index,
        "function-local-import": _local_import_workspace_symbol_index,
        "module-attribute": _module_attribute_workspace_symbol_index,
    },
}


def _bypass_arguments(spelling: str, subject_argument: str) -> tuple[object, ...]:
    # Only the dead-code body takes a second argument: the flag that keeps its
    # branch shut. It is passed in so the branch is a run-time decision.
    if spelling == "dead-code":
        return (subject_argument, False)
    return (subject_argument,)


@pytest.mark.parametrize("spelling", _SPELLINGS)
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_no_spelling_of_an_entrypoint_runs_inside_a_query(
    spelling: str, mode: str, workspace: Path
) -> None:
    # The three spellings end in different ways, so the assertion checks the
    # effect and leaves aside what refused it. Naming the entrypoint only in a
    # branch the body never takes leaves the analysis undone either way. Either
    # the mention alone makes the kernel turn the query away, or the query
    # answers having called nothing. Reaching the entrypoint through its module
    # resolves to the same function and ends where the plain name ends. The
    # module plays no part in the outcome, which is why two entrypoints of the
    # same module end differently under this spelling. Importing it inside the
    # body is the one spelling the supported interpreters disagree about, so
    # asserting the refusal class would pin one interpreter's reading. The
    # cell records only that the entrypoint did not run.
    db = Database(mode=mode)
    outcome = _drive(
        db,
        _BYPASS_DRIVERS["directory_analysis"][spelling],
        *_bypass_arguments(spelling, str(workspace)),
    )
    _assert_never_executed(outcome, db, "directory_analysis_payload")

    directory_analysis(db, str(workspace))
    assert _payload_records(db, "directory_analysis_payload") != ()


@pytest.mark.parametrize("spelling", _SPELLINGS)
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_no_spelling_reaches_the_index_the_interpreters_read_differently(
    spelling: str, mode: str, workspace: Path
) -> None:
    # This entrypoint builds part of its answer inside a comprehension. The
    # supported interpreters disagree about whether the names that
    # comprehension uses belong to the body around it. One counts a name the
    # other skips. They still reach the same verdict for an ordinary caller,
    # because a name they both count is objected to first. That agreement rests
    # on something neither the caller nor the entrypoint chose. So this cell
    # asserts what happened, never what was read, and holds whichever way an
    # interpreter reads the body.
    db = Database(mode=mode)
    outcome = _drive(
        db,
        _BYPASS_DRIVERS["workspace_symbol_index"][spelling],
        *_bypass_arguments(spelling, str(workspace)),
    )
    _assert_never_executed(outcome, db, "workspace_symbol_index_payload")

    workspace_symbol_index(db, str(workspace))
    assert _payload_records(db, "workspace_symbol_index_payload") != ()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_local_import_beside_a_module_level_one_never_runs(mode: str, workspace: Path) -> None:
    # The sharpest shape here, so it gets its own cell. This module imports
    # every documented entrypoint at the top, and the body below imports one of
    # them again. The two supported interpreters disagree about that body. One
    # reads the name the local import binds as a global of the body, and the
    # other does not. So the entrypoint refuses this shape on one interpreter
    # and the kernel refuses it on the other. Before the entrypoint had its
    # refusal, this shape ran the analysis on one of them. The cell asserts
    # only that the analysis does not happen either way.
    db = Database(mode=mode)
    module = str(workspace / "pkg" / "mod.py")
    outcome = _drive(db, _local_import_file_analysis, module)
    _assert_never_executed(outcome, db, "file_analysis_payload")

    file_analysis(db, module)
    assert _payload_records(db, "file_analysis_payload") != ()


# Two miniature high-level entrypoints over one payload query. They differ only
# in where the decode step is named. The decode helper is a plain function of
# its argument on purpose. A helper modelled on a real one would reach the
# request memo and the cache the kernel refuses to walk. The kernel would then
# turn the direct spelling away for holding them and admit the hidden one, and
# this control would fail on the difference it exists to rule out.


@query
def _demo_payload(db: Database, text: str) -> tuple[str, ...]:
    return tuple(piece for piece in text.split(",") if piece)


def _demo_decode(piece: str) -> str:
    return piece.strip().upper()


def _demo_named(db: Database, text: str) -> tuple[str, ...]:
    _reject_in_query(db, "_demo_named")
    converted = []
    for piece in db.get(_demo_payload, text):
        converted.append(_demo_decode(piece))
    return tuple(converted)


def _demo_hidden(db: Database, text: str) -> tuple[str, ...]:
    _reject_in_query(db, "_demo_hidden")
    return tuple(_demo_decode(piece) for piece in db.get(_demo_payload, text))


@query
def _in_query_demo_named(db: Database, text: str) -> str:
    _demo_named(db, text)
    return _RAN


@query
def _in_query_demo_hidden(db: Database, text: str) -> str:
    _demo_hidden(db, text)
    return _RAN


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_hiding_the_decode_step_changes_nothing_about_the_refusal(mode: str) -> None:
    db = Database(mode=mode)

    named = _drive(db, _in_query_demo_named, "alpha,beta")
    hidden = _drive(db, _in_query_demo_hidden, "alpha,beta")

    # Both must meet the same refusal. An entrypoint turned away by the kernel
    # and one that refused for itself would both read as "not run", yet differ
    # in the one place they must match.
    assert named == hidden, f"named: {named} | hidden: {hidden}"
    _assert_never_executed(named, db, "_demo_payload")
    _assert_never_executed(hidden, db, "_demo_payload")

    # And both work, so the refusal above is about where they were called from.
    assert _demo_named(db, "alpha,beta") == ("ALPHA", "BETA")
    assert _demo_hidden(db, "alpha,beta") == ("ALPHA", "BETA")
    assert _payload_records(db, "_demo_payload") != ()


# ---------------------------------------------------------------------------
# Reading through a link retargeted underneath the reader
# ---------------------------------------------------------------------------

_FIRST_TARGET = """\
def alpha() -> int:
    return 1
"""

_SECOND_TARGET = """\
def beta() -> int:
    return 2
"""

# Both targets name their definition on the first line at the fifth column, so
# one position asks the same question of whichever of them the link reaches.
_DEFINITION = SourcePosition(line=0, character=4)


@pytest.fixture
def linked_module(tmp_path: Path) -> Path:
    """A link with two candidate targets beside it, pointing at the first."""
    (tmp_path / "a.py").write_text(_FIRST_TARGET, encoding="utf-8")
    (tmp_path / "b.py").write_text(_SECOND_TARGET, encoding="utf-8")
    link = tmp_path / "link.py"
    try:
        link.symlink_to(tmp_path / "a.py")
    except (NotImplementedError, OSError):
        pytest.skip("symlink support is unavailable in this environment")
    return link


def _retarget(link: Path, target: Path) -> None:
    link.unlink()
    link.symlink_to(target)


def _tree_answer(tree: ScopeTree) -> dict[str, object]:
    return {
        "path": tree.path,
        "scopes": tuple(scope.id for scope in tree.scopes),
        "bindings": tuple(sorted(binding.name for binding in tree.bindings)),
    }


def _symbol_answer(symbol: SymbolId | None) -> dict[str, object]:
    assert symbol is not None, "the position names no symbol"
    return {"path": symbol.path, "scope_id": symbol.scope_id, "name": symbol.name}


def _resolved(path: Path) -> str:
    """The canonical spelling of ``path``, read the way the entrypoints read it.

    A temporary directory is reached through a link on some platforms, so a
    string built from the fixture's own idea of where it wrote can differ from
    the one the answers carry. Deriving the expectation from the same tracked
    resolution keeps these cells comparing what the code distinguishes.
    """
    resolved = ResolvedPathResource().read(Database(), os.fspath(path))
    assert resolved is not None
    return resolved


def _canonicalization_key(link: Path) -> str:
    """The label the database files ``link``'s canonicalization under.

    Asked of the canonicalizing resource itself, so only that resource's record
    can answer the arm below, even if another record's label mentions the same
    path.
    """
    return ResolvedPathResource().label(os.fspath(link))


def _canonicalizations(db: Database, key: str) -> dict[str, int]:
    """When the record filed under ``key`` last moved its answer.

    Reaching a file through a link means canonicalizing the link first. This
    is where that step shows up as something the database declared. An
    undeclared filesystem read leaves no record here, and an empty answer is a
    step nothing downstream can depend on.
    """
    return {
        node.label: node.changed_at
        for node in db.dependency_graph()
        if node.kind == "resource" and node.label == key
    }


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_scope_tree_read_through_a_link_follows_the_retarget(
    mode: str, linked_module: Path
) -> None:
    # Read directly, outside any query body. The boundary above makes that the
    # only place to ask this question.
    db = Database(mode=mode)
    beside = linked_module.parent
    key = _canonicalization_key(linked_module)

    before = _tree_answer(scope_tree(db, str(linked_module)))
    declared_before = _canonicalizations(db, key)
    assert before["path"] == _resolved(beside / "a.py")
    assert before["bindings"] == ("alpha",)

    _retarget(linked_module, beside / "b.py")

    warm = _tree_answer(scope_tree(db, str(linked_module)))
    declared_after = _canonicalizations(db, key)
    fresh = _tree_answer(scope_tree(Database(mode=mode), str(linked_module)))

    # The four below preserve a known answer. The shipped tree passes them warm
    # and fresh alike, and so would a tree that canonicalized the link without
    # declaring the step. The block at the end is what covers the step.
    assert warm == fresh
    assert warm != before, "the link was retargeted and the answer did not move"
    assert warm["path"] == _resolved(beside / "b.py")
    assert warm["bindings"] == ("beta",)
    # The two targets are shaped alike, so the scopes are the one part of the
    # answer with no reason to move. Their staying put shows the two parts that
    # moved did so because of the retarget.
    assert warm["scopes"] == before["scopes"]

    # Canonicalizing the link is a declared step. One record, filed under the
    # label the canonicalizing resource gives the link, answered both reads and
    # moved its answer between them.
    assert set(declared_before) == {key}, "canonicalizing the link declared nothing"
    assert set(declared_after) == {key}
    assert declared_after[key] != declared_before[key]


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_a_symbol_read_through_a_link_follows_the_retarget(mode: str, linked_module: Path) -> None:
    # Read directly for the same reason: this entrypoint is also refused inside
    # a query body.
    db = Database(mode=mode)
    beside = linked_module.parent
    key = _canonicalization_key(linked_module)

    before = _symbol_answer(symbol_at(db, str(linked_module), _DEFINITION))
    declared_before = _canonicalizations(db, key)
    assert before["path"] == _resolved(beside / "a.py")
    assert before["name"] == "alpha"

    _retarget(linked_module, beside / "b.py")

    warm = _symbol_answer(symbol_at(db, str(linked_module), _DEFINITION))
    declared_after = _canonicalizations(db, key)
    fresh = _symbol_answer(symbol_at(Database(mode=mode), str(linked_module), _DEFINITION))

    # These preserve a known answer again. The identity under the position
    # follows the link warm and fresh alike, whether or not the step that
    # reached it was declared. The block at the end covers that step.
    assert warm == fresh
    assert warm != before, "the link was retargeted and the symbol did not move"
    assert warm["path"] == _resolved(beside / "b.py")
    assert warm["name"] == "beta"
    assert warm["scope_id"] == before["scope_id"]

    assert set(declared_before) == {key}, "canonicalizing the link declared nothing"
    assert set(declared_after) == {key}
    assert declared_after[key] != declared_before[key]


# Every driver above, and every miniature entrypoint one of them drives, with
# the name its body must still call. Thirty-five drivers of one shape are
# thirty-five chances to paste the wrong name. A driver pointed at another
# entrypoint is refused as flatly as the right one, so its cells stay green
# while its own subject is never driven. The cell below reads the bodies to
# catch that.
_DRIVER_SUBJECTS: dict[str, str] = {
    **{f"_in_query_{name}": name for name in _GUARDED_ENTRYPOINTS},
    **{
        driver.key.rsplit(":", 1)[-1]: subject
        for subject, by_spelling in _BYPASS_DRIVERS.items()
        for driver in by_spelling.values()
    },
    "_local_import_file_analysis": "file_analysis",
    "_in_query_demo_named": "_demo_named",
    "_in_query_demo_hidden": "_demo_hidden",
    "_demo_named": "_reject_in_query",
    "_demo_hidden": "_reject_in_query",
}


def _called_names(source: str) -> dict[str, frozenset[str]]:
    """Every name each top-level function calls, plainly or through a module."""
    called: dict[str, frozenset[str]] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.FunctionDef):
            continue
        names: set[str] = set()
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            if isinstance(inner.func, ast.Name):
                names.add(inner.func.id)
            elif isinstance(inner.func, ast.Attribute):
                names.add(inner.func.attr)
        called[node.name] = frozenset(names)
    return called


def test_every_driver_still_calls_the_thing_it_drives() -> None:
    called = _called_names(Path(__file__).read_text(encoding="utf-8"))

    # The registry must cover the whole guarded surface, and each entry must be
    # the query the surface cell runs. Otherwise one driver could be checked
    # here while a different one is driven there.
    assert {f"_in_query_{name}" for name in _GUARDED_ENTRYPOINTS} <= set(_DRIVER_SUBJECTS)
    for name in sorted(_GUARDED_ENTRYPOINTS):
        assert _DRIVERS[name].key.endswith(f":_in_query_{name}")

    silent = sorted(
        f"{driver} no longer calls {subject}"
        for driver, subject in _DRIVER_SUBJECTS.items()
        if subject not in called.get(driver, frozenset())
    )
    assert silent == []


@pytest.mark.parametrize(
    ("filename", "contents", "analyze"),
    (
        ("data.csv", "name,value\na,1\n", workspace_csv_analysis),
        (".env", "NAME=value\n", workspace_env_analysis),
        ("package.json", "{}\n", workspace_json_analysis),
        ("requirements.txt", "example>=1\n", workspace_requirements_analysis),
        ("pyproject.toml", "[project]\n", workspace_config_analysis),
        ("pom.xml", "<project />\n", workspace_xml_analysis),
    ),
    ids=("csv", "env", "json", "requirements", "toml", "xml"),
)
def test_workspace_scans_past_unrelated_entries(
    tmp_path: Path,
    filename: str,
    contents: str,
    analyze: Any,
) -> None:
    (tmp_path / "!unrelated").write_text("noise", encoding="utf-8")
    (tmp_path / filename).write_text(contents, encoding="utf-8")

    assert analyze(Database(), tmp_path) is not None
