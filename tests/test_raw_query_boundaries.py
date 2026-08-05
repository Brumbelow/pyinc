from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any, Literal

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLES = _PROJECT_ROOT / "examples"
if str(_EXAMPLES) not in sys.path:
    sys.path.insert(0, str(_EXAMPLES))

from calc.engine import calc_source, parse_calc  # noqa: E402
from correctness_demo import read_source  # noqa: E402

from pyinc import Database, InMemoryArtifactStore, query  # noqa: E402
from pyinc.integrations.csv_data import csv_file_text  # noqa: E402
from pyinc.integrations.deep_module_resolution import _pth_file_text  # noqa: E402
from pyinc.integrations.env_file import env_file_text  # noqa: E402
from pyinc.integrations.installed_packages import _metadata_text  # noqa: E402
from pyinc.integrations.json_config import (  # noqa: E402
    json_file_text,
    json_sections_payload,
)
from pyinc.integrations.notebook import notebook_text  # noqa: E402
from pyinc.integrations.python_source import (  # noqa: E402
    import_statements_for_file,
    source_text,
)
from pyinc.integrations.requirements_txt import requirements_file_text  # noqa: E402
from pyinc.integrations.toml_config import config_file_text  # noqa: E402
from pyinc.integrations.xml_config import xml_file_text  # noqa: E402
from pyinc_codegen.schema import schema_text  # noqa: E402

_MODES = ("strict", "checked", "fast")

_RAW_QUERY_CASES = (
    pytest.param(source_text, ".py", id="python-source"),
    pytest.param(json_file_text, ".json", id="json"),
    pytest.param(config_file_text, ".toml", id="toml"),
    pytest.param(xml_file_text, ".xml", id="xml"),
    pytest.param(csv_file_text, ".csv", id="csv"),
    pytest.param(env_file_text, ".env", id="env"),
    pytest.param(notebook_text, ".ipynb", id="notebook"),
    pytest.param(requirements_file_text, ".txt", id="requirements"),
    pytest.param(_metadata_text, ".metadata", id="installed-metadata"),
    pytest.param(_pth_file_text, ".pth", id="pth"),
    pytest.param(schema_text, ".schema.json", id="codegen-schema"),
    pytest.param(read_source, ".demo.py", id="correctness-demo"),
    pytest.param(calc_source, ".calc", id="calc-demo"),
)

_EXPECTED_RAW_BOUNDARIES = {
    "examples/calc/engine.py": {"calc_source"},
    "examples/correctness_demo.py": {"read_source"},
    "src/pyinc/integrations/csv_data.py": {"csv_file_text"},
    "src/pyinc/integrations/deep_module_resolution.py": {"_pth_file_text"},
    "src/pyinc/integrations/env_file.py": {"env_file_text"},
    "src/pyinc/integrations/installed_packages.py": {"_metadata_text"},
    "src/pyinc/integrations/json_config.py": {"json_file_text"},
    "src/pyinc/integrations/notebook.py": {"notebook_text"},
    "src/pyinc/integrations/python_source.py": {"source_text"},
    "src/pyinc/integrations/requirements_txt.py": {"requirements_file_text"},
    "src/pyinc/integrations/toml_config.py": {"config_file_text"},
    "src/pyinc/integrations/xml_config.py": {"xml_file_text"},
    "src/pyinc_codegen/schema.py": {"schema_text"},
}


def _query_decorator(function: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.expr | None:
    for decorator in function.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if (isinstance(target, ast.Name) and target.id == "query") or (
            isinstance(target, ast.Attribute) and target.attr == "query"
        ):
            return decorator
    return None


def _returns_raw_scalar(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    annotation = function.returns
    return isinstance(annotation, ast.Name) and annotation.id in {"str", "bytes"}


def _comparison_overrides(decorator: ast.expr) -> set[str]:
    if not isinstance(decorator, ast.Call):
        return set()
    return {keyword.arg for keyword in decorator.keywords if keyword.arg in {"cutoff", "eq"}}


@pytest.mark.parametrize(
    ("decorator", "expected"),
    [
        ("@query", set()),
        ("@query(cutoff=token)", {"cutoff"}),
        ("@query(eq=equal)", {"eq"}),
        ("@query(eq=equal, cutoff=token)", {"cutoff", "eq"}),
    ],
)
def test_raw_query_comparison_override_detector(
    decorator: str,
    expected: set[str],
) -> None:
    tree = ast.parse(f"{decorator}\ndef raw(db: object) -> str:\n    return ''\n")
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    query_decorator = _query_decorator(function)
    assert query_decorator is not None
    assert _comparison_overrides(query_decorator) == expected


def test_static_inventory_requires_default_equality_for_raw_queries() -> None:
    discovered: dict[str, set[str]] = {}
    violations: list[str] = []

    for source_root in (_PROJECT_ROOT / "src", _PROJECT_ROOT / "examples"):
        for path in sorted(source_root.rglob("*.py")):
            relative = path.relative_to(_PROJECT_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                decorator = _query_decorator(node)
                if decorator is None or not _returns_raw_scalar(node):
                    continue
                discovered.setdefault(relative, set()).add(node.name)
                overrides = _comparison_overrides(decorator)
                if overrides:
                    violations.append(
                        f"{relative}:{node.lineno}:{node.name}:{','.join(sorted(overrides))}"
                    )

    missing = {
        path: sorted(names - discovered.get(path, set()))
        for path, names in _EXPECTED_RAW_BOUNDARIES.items()
        if names - discovered.get(path, set())
    }
    assert missing == {}
    assert violations == []


def test_shipped_queries_have_no_unregistered_explicit_cutoffs() -> None:
    """A new production cutoff must arrive with a dedicated congruence property."""
    violations: list[str] = []
    roots = (
        _PROJECT_ROOT / "src/pyinc/integrations",
        _PROJECT_ROOT / "src/pyinc_codegen",
        _PROJECT_ROOT / "src/pyinc_tools",
        _PROJECT_ROOT / "examples",
    )
    for source_root in roots:
        for path in sorted(source_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                is_query = (isinstance(target, ast.Name) and target.id in {"query", "Query"}) or (
                    isinstance(target, ast.Attribute) and target.attr in {"query", "Query"}
                )
                if is_query and any(keyword.arg == "cutoff" for keyword in node.keywords):
                    relative = path.relative_to(_PROJECT_ROOT).as_posix()
                    violations.append(f"{relative}:{node.lineno}")
    assert violations == []


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(("raw_query", "suffix"), _RAW_QUERY_CASES)
def test_every_raw_boundary_publishes_exact_changes(
    mode: str,
    raw_query: Any,
    suffix: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"raw{suffix}"
    path.write_text("old spelling\n", encoding="utf-8")
    warm_db = Database(mode=mode)

    assert raw_query.eq is None
    assert raw_query.cutoff is None
    assert warm_db.get(raw_query, str(path)) == "old spelling\n"

    path.write_text("new spelling\n", encoding="utf-8")
    warm = warm_db.get(raw_query, str(path))
    fresh = Database(mode=mode).get(raw_query, str(path))

    assert warm == fresh == "new spelling\n"
    assert warm_db.inspect(raw_query, str(path)).last_recompute == "executed"


_SURROGATE_CATEGORY: tuple[Literal["Cs"], ...] = ("Cs",)
_RAW_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=_SURROGATE_CATEGORY),
    min_size=0,
    max_size=80,
)


@pytest.mark.parametrize(("raw_query", "suffix"), _RAW_QUERY_CASES)
@settings(
    max_examples=3,
    deadline=None,
    suppress_health_check=(HealthCheck.function_scoped_fixture,),
)
@given(mode=st.sampled_from(_MODES), initial=_RAW_TEXT, updated=_RAW_TEXT)
def test_exact_raw_boundary_congruence_property(
    raw_query: Any,
    suffix: str,
    mode: str,
    initial: str,
    updated: str,
    tmp_path: Path,
) -> None:
    """Distinct raw values never share a reusable result in any mode."""
    assume(initial != updated)
    path = tmp_path / f"property{suffix}"
    path.write_text(initial, encoding="utf-8", newline="")
    warm_db = Database(mode=mode)
    initial_warm = warm_db.get(raw_query, str(path))
    initial_fresh = Database(mode=mode).get(raw_query, str(path))
    assert initial_warm == initial_fresh

    path.write_text(updated, encoding="utf-8", newline="")
    warm = warm_db.get(raw_query, str(path))
    fresh = Database(mode=mode).get(raw_query, str(path))

    # A Python file with an invalid PEP 263 declaration (including a NUL byte)
    # has the documented empty decoded-text payload plus a separate diagnostic.
    # The cutoff law is about the complete public value, so compare the warm
    # observation with the independently decoded fresh observation here. The
    # deterministic cases above pin exact text for decodable files.
    assert warm == fresh
    assert warm_db.inspect(raw_query, str(path)).last_recompute == "executed"


_SEMANTIC_CASES = (
    pytest.param(
        source_text,
        import_statements_for_file,
        "import os\n",
        "import os  # comment\n",
        "import sys\n",
        id="python",
    ),
    pytest.param(
        json_file_text,
        json_sections_payload,
        '{"project":{"name":"demo"}}\n',
        '{\n  "project": {"name": "demo"}\n}\n',
        '{"project":{"name":"changed"}}\n',
        id="json",
    ),
    pytest.param(
        calc_source,
        parse_calc,
        "let value = 1\nemit value\n",
        "# comment\nlet value = 1\nemit value\n",
        "let value = 2\nemit value\n",
        id="calc",
    ),
)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    ("raw_query", "semantic_query", "initial", "equivalent", "changed"),
    _SEMANTIC_CASES,
)
def test_semantic_payload_backdates_only_for_equal_complete_output(
    mode: str,
    raw_query: Any,
    semantic_query: Any,
    initial: str,
    equivalent: str,
    changed: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "input.txt"
    path.write_text(initial, encoding="utf-8")
    warm_db = Database(mode=mode)
    first = warm_db.get(semantic_query, str(path))

    path.write_text(equivalent, encoding="utf-8")
    equal_warm = warm_db.get(semantic_query, str(path))
    equal_fresh = Database(mode=mode).get(semantic_query, str(path))

    assert equal_warm == equal_fresh == first
    assert warm_db.inspect(raw_query, str(path)).last_recompute == "executed"
    assert warm_db.inspect(semantic_query, str(path)).last_recompute == "backdated"

    path.write_text(changed, encoding="utf-8")
    changed_warm = warm_db.get(semantic_query, str(path))
    changed_fresh = Database(mode=mode).get(semantic_query, str(path))

    assert changed_warm == changed_fresh
    assert changed_warm != first
    assert warm_db.inspect(raw_query, str(path)).last_recompute == "executed"
    assert warm_db.inspect(semantic_query, str(path)).last_recompute == "executed"


_CHECKPOINT_CASES = (
    pytest.param(
        source_text,
        "import os\n",
        "import os\n# checkpoint comment\n",
        id="python",
    ),
    pytest.param(
        json_file_text,
        '{"project":{"name":"demo"}}\n',
        '{\n  "project": {"name": "demo"}\n}\n',
        id="json",
    ),
    pytest.param(
        config_file_text,
        "project = 'demo'\n",
        "# checkpoint comment\nproject = 'demo'\n",
        id="toml",
    ),
    pytest.param(
        xml_file_text,
        "<project/>\n",
        "<project><!-- checkpoint comment --></project>\n",
        id="xml",
    ),
    pytest.param(
        csv_file_text,
        "name,value\ndemo,1\n",
        "name,value\r\ndemo,1\r\n",
        id="csv",
    ),
    pytest.param(
        env_file_text,
        "# old comment\nNAME=demo\n",
        "# new comment\nNAME=demo\n",
        id="env",
    ),
    pytest.param(
        notebook_text,
        '{"cells":[],"metadata":{},"nbformat":4,"nbformat_minor":5}\n',
        '{\n  "cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5\n}\n',
        id="notebook",
    ),
    pytest.param(
        requirements_file_text,
        "# old comment\nrequests>=2\n",
        "# new comment\nrequests>=2\n",
        id="requirements",
    ),
    pytest.param(
        _metadata_text,
        "Name: demo\nVersion: 1\nX-Review: old\n",
        "Name: demo\nVersion: 1\nX-Review: new\n",
        id="installed-metadata",
    ),
    pytest.param(
        _pth_file_text,
        "# old comment\nlib\n",
        "# new comment\nlib\n",
        id="pth",
    ),
    pytest.param(
        schema_text,
        '{"type":"object","properties":{}}\n',
        '{\n  "type": "object",\n  "properties": {}\n}\n',
        id="schema",
    ),
    pytest.param(
        read_source,
        "import os\n",
        "import os\n# checkpoint comment\n",
        id="correctness-demo",
    ),
    pytest.param(
        calc_source,
        "let value = 1\nemit value\n",
        "# checkpoint comment\nlet value = 1\nemit value\n",
        id="calc-demo",
    ),
)


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(("raw_query", "initial", "updated"), _CHECKPOINT_CASES)
def test_checkpoint_loaded_raw_consumer_observes_later_exact_edit(
    mode: str,
    raw_query: Any,
    initial: str,
    updated: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / "checkpoint-input.txt"
    path.write_text(initial, encoding="utf-8")

    @query(key=f"raw-checkpoint-consumer-{raw_query.key}-{mode}")
    def raw_consumer(db: Database, input_path: str) -> tuple[str, str]:
        return ("raw", raw_query(db, input_path))

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    assert writer.get(raw_consumer, str(path)) == ("raw", initial)
    checkpoint = writer.save_checkpoint()

    reader = Database(mode=mode, store=store)
    reader.load_checkpoint(checkpoint)
    assert reader.get(raw_consumer, str(path)) == ("raw", initial)
    assert reader.statistics().query_executions == 0
    assert reader.inspect(raw_consumer, str(path)).last_decision == "reused"

    path.write_text(updated, encoding="utf-8")
    warm = reader.get(raw_consumer, str(path))
    fresh = Database(mode=mode).get(raw_consumer, str(path))

    assert warm == fresh == ("raw", updated)
    assert reader.inspect(raw_query, str(path)).last_recompute == "executed"
