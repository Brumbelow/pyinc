"""Import statements inside the code the kernel folds into identity.

The kernel folds a query function's code, the code of every function folded
into its identity, the methods of a resource it reads, and the code nested in
any of them. It does not see what an import statement in that code binds. A
module imported there could change while the query kept its identity, so a
warm database answered from the old module and a fresh one from the new. Such
an import is refused unless it names a standard-library module. The same
import at module scope is a capture and stays accepted.
"""

from __future__ import annotations

import ast
import dis
import importlib
import importlib.util
import pkgutil
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import CodeType, FunctionType, ModuleType
from typing import Any

import pytest

import pyinc.integrations
import pyinc.resources
import pyinc_codegen
from pyinc import Database, Query, Resource, UnsupportedValueError, explain_query_captures, query

_ROOT = Path(__file__).resolve().parent.parent

_QUERIES_SOURCE = '''
from dataclasses import dataclass

from pyinc import Database, Resource, query


@query
def import_statement(db: Database) -> int:
    import pyinc_body_import_limits

    return pyinc_body_import_limits.LIMIT


@query
def dotted_import(db: Database) -> int:
    import pyinc_body_import_pkg.sub.leaf

    return pyinc_body_import_pkg.sub.leaf.LIMIT


@query
def aliased_import(db: Database) -> int:
    import pyinc_body_import_limits as limits

    return limits.LIMIT


@query
def from_import(db: Database) -> int:
    from pyinc_body_import_limits import LIMIT

    return LIMIT


@query
def relative_import(db: Database) -> int:
    from . import sibling

    return sibling.LIMIT


@query
def relative_standard_library_name(db: Database) -> int:
    from .json import LIMIT

    return LIMIT


@query
def read_in_lambda(db: Database) -> int:
    import pyinc_body_import_limits

    read = lambda: pyinc_body_import_limits.LIMIT
    return read()


@query
def read_in_generator(db: Database) -> int:
    import pyinc_body_import_limits

    return sum(pyinc_body_import_limits.LIMIT for _ in range(1))


@query
def import_in_inner_function(db: Database) -> int:
    def read() -> int:
        import pyinc_body_import_limits

        return pyinc_body_import_limits.LIMIT

    return read()


@query
def import_in_class_body(db: Database) -> int:
    class Holder:
        from pyinc_body_import_limits import LIMIT

    return Holder.LIMIT


def helper_that_imports() -> int:
    import pyinc_body_import_limits

    return pyinc_body_import_limits.LIMIT


@query
def calls_helper(db: Database) -> int:
    return helper_that_imports()


@dataclass(frozen=True)
class ImportingResource(Resource[str, int, int]):
    def label(self, key: str) -> str:
        return f"importing[{key}]"

    def probe(self, key: str) -> int:
        import pyinc_body_import_limits

        return pyinc_body_import_limits.LIMIT

    def load(self, db: Database, key: str) -> int:
        return self.probe(key)


IMPORTING = ImportingResource()


@query
def reads_importing_resource(db: Database) -> int:
    return IMPORTING.read(db, "limit")


def importing_evaluator(format: int) -> dict[str, int]:
    import pyinc_body_import_limits

    return {"limit": pyinc_body_import_limits.LIMIT}


@query
def reads_handle_annotations(db: Database) -> int:
    return reads_handle_annotations.__annotate__(1)["limit"]


reads_handle_annotations.__annotate__ = importing_evaluator
'''

_REMEDY_SOURCE = '''
import pyinc_body_import_limits

from pyinc import Database, query


@query
def module_scope_import(db: Database) -> int:
    return pyinc_body_import_limits.LIMIT
'''

# The modules the refused bodies import. None of them may be loaded after a
# refusal, because the kernel refuses before the body runs.
_IMPORT_TARGETS = frozenset(
    {
        "pyinc_body_import_limits",
        "pyinc_body_import_pkg.json",
        "pyinc_body_import_pkg.sibling",
        "pyinc_body_import_pkg.sub",
        "pyinc_body_import_pkg.sub.leaf",
    }
)


@pytest.fixture
def fixture_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """Write a user module and a package beside it, and import the package's queries."""

    (tmp_path / "pyinc_body_import_limits.py").write_text("LIMIT = 1\n", encoding="utf-8")
    package = tmp_path / "pyinc_body_import_pkg"
    (package / "sub").mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "sibling.py").write_text("LIMIT = 1\n", encoding="utf-8")
    (package / "json.py").write_text("LIMIT = 1\n", encoding="utf-8")
    (package / "sub" / "__init__.py").write_text("", encoding="utf-8")
    (package / "sub" / "leaf.py").write_text("LIMIT = 1\n", encoding="utf-8")
    (package / "queries.py").write_text(_QUERIES_SOURCE, encoding="utf-8")
    (package / "remedy.py").write_text(_REMEDY_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    try:
        yield importlib.import_module("pyinc_body_import_pkg.queries")
    finally:
        for name in [
            name
            for name in sys.modules
            if name.startswith(("pyinc_body_import_limits", "pyinc_body_import_pkg"))
        ]:
            del sys.modules[name]


@pytest.mark.parametrize(
    ("name", "statement"),
    [
        ("import_statement", "import pyinc_body_import_limits"),
        ("dotted_import", "import pyinc_body_import_pkg.sub.leaf"),
        ("aliased_import", "import pyinc_body_import_limits"),
        ("from_import", "from pyinc_body_import_limits import LIMIT"),
        ("relative_import", "from . import sibling"),
        ("relative_standard_library_name", "from .json import LIMIT"),
        ("read_in_lambda", "import pyinc_body_import_limits"),
        ("read_in_generator", "import pyinc_body_import_limits"),
        ("import_in_inner_function", "import pyinc_body_import_limits"),
        ("import_in_class_body", "from pyinc_body_import_limits import LIMIT"),
        ("calls_helper", "import pyinc_body_import_limits"),
        ("reads_importing_resource", "import pyinc_body_import_limits"),
    ],
)
def test_an_import_statement_in_folded_code_is_refused(
    name: str, statement: str, fixture_package: ModuleType
) -> None:
    """A non-standard-library import in folded code is refused before the body runs.

    The statement can sit in the query body, in a function or class nested in
    it, in a helper the query calls, or in a method of a resource the query
    reads. A relative import is refused even when its name is a
    standard-library one, such as a package's own `json` module. A lambda and
    a generator expression hold no statements, so their cases import in the
    body and read the module only inside the nested code. The capture preview
    reports the refusal with the database's own message.
    """

    captured = getattr(fixture_package, name)

    with pytest.raises(UnsupportedValueError, match=re.escape(repr(statement))) as refusal:
        Database().get(captured)

    message = str(refusal.value)
    assert "Move the import to module scope" in message
    assert _IMPORT_TARGETS.isdisjoint(sys.modules)
    rejected = [info for info in explain_query_captures(captured) if not info.accepted]
    assert [info.rejection_reason for info in rejected] == [message]


def test_an_import_statement_in_a_handle_annotation_evaluator_is_refused(
    fixture_package: ModuleType,
) -> None:
    """An annotation evaluator written on a query handle is folded code too.

    The query reads its own handle's evaluator, which imports a user module.
    Without the refusal a warm database kept the old value after the module
    changed. The preview reports the refusal twice with the database's
    message: on the global that names the handle, and on the handle itself.
    """

    captured = fixture_package.reads_handle_annotations
    statement = "import pyinc_body_import_limits"

    with pytest.raises(UnsupportedValueError, match=re.escape(repr(statement))) as refusal:
        Database().get(captured)

    message = str(refusal.value)
    assert "importing_evaluator" in message
    assert "Move the import to module scope" in message
    assert _IMPORT_TARGETS.isdisjoint(sys.modules)
    rejected = [
        (info.name, info.rejection_reason)
        for info in explain_query_captures(captured)
        if not info.accepted
    ]
    assert rejected == [("reads_handle_annotations", message), ("handle[*]", message)]


def test_the_same_import_at_module_scope_is_captured(
    fixture_package: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remedy the refusal names works: a module-scope import moves identity."""

    remedy = importlib.import_module("pyinc_body_import_pkg.remedy")
    limits = importlib.import_module("pyinc_body_import_limits")
    db = Database()
    assert db.get(remedy.module_scope_import) == 1

    monkeypatch.setattr(limits, "LIMIT", 2)

    assert db.get(remedy.module_scope_import) == 2
    assert db.get(remedy.module_scope_import) == Database().get(remedy.module_scope_import)


def test_a_standard_library_import_statement_is_accepted() -> None:
    """A standard-library import passes. The function's own code carries the names it reads.

    A dotted name passes when its first component is a standard-library module.
    """

    @query
    def standard_library_imports(db: Database) -> str:
        import json
        import os.path
        from concurrent.futures import ThreadPoolExecutor
        from os import path

        return json.dumps(
            [
                path.basename("directory/name.txt"),
                os.path.splitext("name.txt")[1],
                ThreadPoolExecutor.__name__,
            ]
        )

    db = Database()
    assert db.get(standard_library_imports) == '["name.txt", ".txt", "ThreadPoolExecutor"]'
    assert db.get(standard_library_imports) == Database().get(standard_library_imports)
    assert db.statistics().query_executions == 1
    assert all(info.accepted for info in explain_query_captures(standard_library_imports))


def test_a_standard_library_function_with_a_relative_import_is_folded_as_before() -> None:
    """A function the interpreter shipped is not scanned for import statements.

    `multiprocessing.dummy.Pool` runs `from ..pool import ThreadPool` in its
    body. The build identity already pins that code, so a capture of it
    folds as it did before import statements were refused.
    """

    pool = importlib.import_module("multiprocessing.dummy").Pool

    @query
    def names_the_pool(db: Database) -> str:
        return str(pool.__name__)

    assert any(
        instruction.opname == "IMPORT_NAME" for instruction in dis.get_instructions(pool)
    )
    assert Database().get(names_the_pool) == "Pool"
    assert all(info.accepted for info in explain_query_captures(names_the_pool))


def _code_tree(code: CodeType) -> Iterator[CodeType]:
    yield code
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            yield from _code_tree(constant)


def _refusable_imports(code: CodeType) -> list[str]:
    """The import statements in `code` that are relative or name a non-standard module."""

    found: list[str] = []
    for item in _code_tree(code):
        instructions = [
            instruction
            for instruction in dis.get_instructions(item)
            if instruction.opname != "EXTENDED_ARG"
        ]
        for index, instruction in enumerate(instructions):
            if instruction.opname != "IMPORT_NAME":
                continue
            level = instructions[index - 2].argval
            if level == 0 and instruction.argval.partition(".")[0] in sys.stdlib_module_names:
                continue
            found.append(f"level {level} {instruction.argval}")
    return found


def _shipped_modules(monkeypatch: pytest.MonkeyPatch) -> list[ModuleType]:
    """Every module of the integrations, codegen, examples and bench trees, imported."""

    monkeypatch.syspath_prepend(str(_ROOT))
    monkeypatch.syspath_prepend(str(_ROOT / "examples"))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    modules: list[ModuleType] = [pyinc.resources]
    for package in (
        pyinc.integrations,
        pyinc_codegen,
        importlib.import_module("bench"),
        importlib.import_module("calc"),
    ):
        modules.append(package)
        for info in pkgutil.walk_packages(package.__path__, f"{package.__name__}."):
            modules.append(importlib.import_module(info.name))
    for path in sorted((_ROOT / "examples").glob("*.py")):
        # A name of its own, so the module body runs and the demo under the
        # `__main__` guard does not.
        name = f"_pyinc_shipped_example_{path.stem}"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules.append(module)
    return modules


def test_no_shipped_query_or_resource_folds_a_refusable_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every function the kernel folds for a shipped query or resource imports at module scope.

    The walk asks the kernel itself which functions it folds, by recording
    each one its two definition folds receive while it fingerprints every
    query and resource the shipped trees define at module scope. A resource
    class with no module-level instance is folded through a new one. Each
    resource is folded once. Its fold is the same wherever a query captures
    it, and folding it again for every capture would cost most of the walk.
    Queries defined inside a function are read from their source.
    """

    modules = _shipped_modules(monkeypatch)
    probe = Database()
    queries: dict[int, Any] = {}
    resources: dict[type[Any], Any] = {}
    resource_classes: set[type[Any]] = set()
    for module in modules:
        for value in vars(module).values():
            if isinstance(value, Query):
                queries[id(value)] = value
            elif isinstance(value, type):
                if issubclass(value, Resource) and value is not Resource:
                    resource_classes.add(value)
            elif probe._is_resource_handle(value):
                resources.setdefault(type(value), value)
    for resource_class in resource_classes - set(resources):
        resources[resource_class] = resource_class()
    # Floors, so a walk that found nothing cannot pass.
    assert len(queries) >= 100
    assert len(resources) >= 15

    folded: list[FunctionType] = []
    resource_folds: dict[int, Any] = {}
    definition_fold = Database._function_definition_payload
    source_pinned_fold = Database._source_pinned_function_payload
    resource_fold = Database._resource_identity_payload

    def record_definition(self: Database, fn: FunctionType, seen: set[int]) -> Any:
        folded.append(fn)
        return definition_fold(self, fn, seen)

    def record_source_pinned(self: Database, fn: FunctionType, seen: set[int]) -> Any:
        folded.append(fn)
        return source_pinned_fold(self, fn, seen)

    def fold_resource_once(self: Database, resource: Any) -> Any:
        if id(resource) not in resource_folds:
            resource_folds[id(resource)] = resource_fold(self, resource)
        return resource_folds[id(resource)]

    monkeypatch.setattr(Database, "_function_definition_payload", record_definition)
    monkeypatch.setattr(Database, "_source_pinned_function_payload", record_source_pinned)
    monkeypatch.setattr(Database, "_resource_identity_payload", fold_resource_once)
    db = Database()
    for captured in queries.values():
        db._query_fingerprint(captured)
    for resource in resources.values():
        db._resource_identity_payload(resource)

    offenders = sorted(
        {
            f"{fn.__module__}.{fn.__qualname__}: {statement}"
            for fn in folded
            if not Database._is_runtime_pinned_function(fn)
            for statement in _refusable_imports(fn.__code__)
        }
    )
    assert len({id(fn) for fn in folded}) >= 400
    assert offenders == []

    nested_offenders: list[str] = []
    for root in ("src/pyinc/integrations", "src/pyinc_codegen", "examples", "bench"):
        for path in sorted((_ROOT / root).rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not any(_names_query(decorator) for decorator in node.decorator_list):
                    continue
                for statement in ast.walk(node):
                    if isinstance(statement, (ast.Import, ast.ImportFrom)) and (
                        _is_refusable_import(statement)
                    ):
                        nested_offenders.append(f"{path}:{statement.lineno}")
    assert nested_offenders == []


def _names_query(decorator: ast.expr) -> bool:
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(target, ast.Attribute):
        return target.attr == "query"
    return isinstance(target, ast.Name) and target.id == "query"


def _is_refusable_import(node: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(node, ast.Import):
        return any(
            alias.name.partition(".")[0] not in sys.stdlib_module_names for alias in node.names
        )
    module = node.module or ""
    return node.level > 0 or module.partition(".")[0] not in sys.stdlib_module_names
