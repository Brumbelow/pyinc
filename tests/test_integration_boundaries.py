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

The last group varies how the query spells the name -- through a local import,
through the entrypoint's module, or in a branch the body never takes -- because
a rule that held for one spelling and not the others would leave the boundary
where it was found. What every cell there pins is that the entrypoint does not
run; which of the two refusals arrives is not part of the rule.
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


# ---------------------------------------------------------------------------
# The shapes a query can name an entrypoint by
# ---------------------------------------------------------------------------

# What a driver says when its entrypoint ran, and what it says when the body
# finished without ever reaching the call. Keeping the two apart is what lets a
# clean answer still be evidence: an answer that came back is either the one
# shape that never made the call, or a driver that ran what it should not have.
_RAN = "the entrypoint answered"
_NOT_REACHED = "the call was never reached"
_REFUSED = "refused by "

_SPELLINGS = ("dead-code", "function-local-import", "module-attribute")


def _drive(db: Database, driver: Query[..., str], *arguments: object) -> str:
    """Say what a driving query did, without deciding what refused it.

    Two refusals reach a caller across these spellings: the one an entrypoint
    owes a query body, and the kernel's own objection to what such a query
    captures. Which one arrives is a judgement about the caller's compiled code
    that moves between interpreter versions, so the answer records only that a
    refusal happened and leaves the name of it to the failure message.
    """
    try:
        return db.get(driver, *arguments)
    except (CompositionError, UnsupportedValueError) as refusal:
        assert isinstance(refusal, PyIncError)
        return _REFUSED + type(refusal).__name__


def _payload_records(db: Database, payload_query: str) -> tuple[str, ...]:
    """The records the entrypoint's payload query would have left behind.

    Every subject below asks a cached query named after itself as its first act
    past the refusal, so whether that query left a record is whether the
    entrypoint got any further than its own front door. Each cell proves the
    read has teeth in its own mode by asking the entrypoint from outside a
    query afterwards and finding the record it left. The name is anchored on
    both sides because a label carries the defining module in front of it and
    an argument digest behind.
    """
    anchor = f":{payload_query}["
    return tuple(node.label for node in db.dependency_graph() if anchor in node.label)


def _assert_never_executed(outcome: str, db: Database, payload_query: str) -> None:
    """The whole rule: the entrypoint did not run, whatever came back instead."""
    assert outcome.startswith(_REFUSED) or outcome == _NOT_REACHED, outcome
    assert _payload_records(db, payload_query) == ()


# One driver per spelling per subject. The two subjects are the two sides of
# what the kernel makes of an ordinary caller. `directory_analysis` hides the
# work it decodes with inside a generator expression and a query naming it is
# admitted, so the refusal it meets is its own. `workspace_symbol_index` is
# turned away before any body runs -- and the two supported interpreters do not
# even read its body the same way, agreeing on the verdict only because a name
# they both see is objected to first. Neither cell reads a capture set: both
# read what happened.


@query
def _dead_code_directory_analysis(db: Database, root: str, reach_the_call: bool) -> str:
    # The flag is an argument, so the branch is decided while the body runs.
    # Written as `if False:` the compiler drops the branch and the name never
    # reaches the caller's code object, which would test the compiler instead.
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
    # branch shut, passed rather than written in so the branch is a run-time
    # decision.
    if spelling == "dead-code":
        return (subject_argument, False)
    return (subject_argument,)


@pytest.mark.parametrize("spelling", _SPELLINGS)
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_no_spelling_of_an_entrypoint_runs_inside_a_query(
    spelling: str, mode: str, workspace: Path
) -> None:
    # The three spellings do not all end the same way, and that is the reason
    # the assertion is about the effect rather than about what refused it.
    # Mentioning the entrypoint only in a branch the body never takes leaves
    # the analysis undone either way: either the mention alone is enough for
    # the kernel to turn the query away, or the query answers having called
    # nothing. Reaching the entrypoint through its module resolves to the same
    # function and ends exactly where the plain name ends -- the module is not
    # what decides it, which is why two entrypoints of the same module end
    # differently under this spelling. Importing it inside the body is the one
    # spelling the supported interpreters disagree about, so it is the one that
    # would pin an interpreter's reading if the class were asserted. None of
    # that is recorded below; only that the entrypoint did not run.
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
    # This entrypoint builds part of its answer inside a comprehension, and the
    # supported interpreters disagree about whether the names that comprehension
    # uses belong to the body around it: one of them counts a name the other
    # does not. The two nonetheless reach the same verdict for an ordinary
    # caller, because a name they both count is objected to first -- an
    # agreement that rests on something neither the caller nor the entrypoint
    # chose. So this cell asserts what happened and never what was read, and
    # stays true whichever way an interpreter reads the body.
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
    # The sharpest shape available here, and the reason it is its own cell:
    # this module imports every documented entrypoint at the top, and the body
    # below imports one of them again inside itself. The two supported
    # interpreters disagree about that body -- one reads the name the local
    # import binds as a global of the body and the other does not -- so this
    # exact shape is refused by the entrypoint on one of them and by the kernel
    # on the other, and before the entrypoint refused anything it ran the
    # analysis on one of them. Neither outcome is asserted; what is asserted is
    # that the analysis does not happen either way.
    db = Database(mode=mode)
    module = str(workspace / "pkg" / "mod.py")
    outcome = _drive(db, _local_import_file_analysis, module)
    _assert_never_executed(outcome, db, "file_analysis_payload")

    file_analysis(db, module)
    assert _payload_records(db, "file_analysis_payload") != ()


# Two miniature high-level entrypoints over one payload query, differing only
# in where the decode step is named. The decode helper is a plain function of
# its argument on purpose: modelled on a real one it would reach the request
# memo and the cache the kernel refuses to walk, the direct spelling would be
# turned away for holding them while the hidden one was not, and this control
# would go red for the difference it exists to rule out.


@query
def _demo_payload(db: Database, text: str) -> tuple[str, ...]:
    return tuple(piece for piece in text.split(",") if piece)


def _demo_decode(piece: str) -> str:
    return piece.strip().upper()


def _demo_named(db: Database, text: str) -> tuple[str, ...]:
    _reject_in_query(db, "_demo_named")
    decoded = []
    for piece in db.get(_demo_payload, text):
        decoded.append(_demo_decode(piece))
    return tuple(decoded)


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

    # Same refusal, not merely two refusals: an entrypoint that was turned away
    # by the kernel and one that refused for itself would both read as "not run"
    # while differing exactly where they must not.
    assert named == hidden, f"named: {named} | hidden: {hidden}"
    _assert_never_executed(named, db, "_demo_payload")
    _assert_never_executed(hidden, db, "_demo_payload")

    # And both work, so the refusal above is about where they were called from.
    assert _demo_named(db, "alpha,beta") == ("ALPHA", "BETA")
    assert _demo_hidden(db, "alpha,beta") == ("ALPHA", "BETA")
    assert _payload_records(db, "_demo_payload") != ()


# Every driver above, and every miniature one of them drives, with the name its
# body must still call. Thirty-five drivers of one shape are thirty-five
# chances to paste the wrong name into one of them, and a driver pointed at
# some other entrypoint is refused just as flatly as the right one -- so the
# cells that drive it stay green while its own subject is never driven at all.
# The bodies are read here rather than trusted.
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

    # The registry has to cover the whole guarded surface, and each entry has
    # to be the query the surface cell actually runs -- otherwise a driver
    # could be checked here and a different one driven there.
    assert {f"_in_query_{name}" for name in _GUARDED_ENTRYPOINTS} <= set(_DRIVER_SUBJECTS)
    for name in sorted(_GUARDED_ENTRYPOINTS):
        assert _DRIVERS[name].key.endswith(f":_in_query_{name}")

    silent = sorted(
        f"{driver} no longer calls {subject}"
        for driver, subject in _DRIVER_SUBJECTS.items()
        if subject not in called.get(driver, frozenset())
    )
    assert silent == []
