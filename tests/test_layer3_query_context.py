from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable, Iterator
from typing import Any, NoReturn, cast

import pytest

import pyinc.integrations as integration_api
import pyinc.integrations._decoding as _decoding
import pyinc.integrations.csv_data as csv_data
import pyinc.integrations.deep_module_resolution as deep_module_resolution
import pyinc.integrations.dependency_check as dependency_check
import pyinc.integrations.env_file as env_file
import pyinc.integrations.installed_packages as installed_packages
import pyinc.integrations.json_config as json_config
import pyinc.integrations.notebook as notebook
import pyinc.integrations.python_source as python_source
import pyinc.integrations.requirement_evaluation as requirement_evaluation
import pyinc.integrations.requirements_txt as requirements_txt
import pyinc.integrations.scope_resolution as scope_resolution
import pyinc.integrations.symbol_resolution as symbol_resolution
import pyinc.integrations.toml_config as toml_config
import pyinc.integrations.xml_config as xml_config
from pyinc import Database, InMemoryArtifactStore, Input, QueryContextError, query

Mode = str
Entrypoint = Callable[..., Any]
Layer3Case = tuple[str, str, Entrypoint, int]

_MODES = ("strict", "checked", "fast")
_IMPORT_STYLES = ("direct", "aggregate", "runtime")
_HELPER_NAMES = {"once_per_request", "request_inputs_changed", "request_scope"}

# name, defining module, direct-module reference, hostile positional arguments
_LAYER3_CASES: tuple[Layer3Case, ...] = (
    (
        "applicable_requirements",
        "requirement_evaluation",
        requirement_evaluation.applicable_requirements,
        1,
    ),
    ("class_model", "symbol_resolution", symbol_resolution.class_model, 3),
    ("config_analysis", "toml_config", toml_config.config_analysis, 1),
    ("csv_analysis", "csv_data", csv_data.csv_analysis, 1),
    (
        "deep_module_resolution_analysis",
        "deep_module_resolution",
        deep_module_resolution.deep_module_resolution_analysis,
        0,
    ),
    (
        "deep_requirements_analysis",
        "requirements_txt",
        requirements_txt.deep_requirements_analysis,
        1,
    ),
    (
        "dependency_check_analysis",
        "dependency_check",
        dependency_check.dependency_check_analysis,
        1,
    ),
    ("directory_analysis", "python_source", python_source.directory_analysis, 1),
    ("env_analysis", "env_file", env_file.env_analysis, 1),
    (
        "evaluate_markers",
        "requirement_evaluation",
        requirement_evaluation.evaluate_markers,
        1,
    ),
    (
        "evaluate_version_specifier",
        "requirement_evaluation",
        requirement_evaluation.evaluate_version_specifier,
        2,
    ),
    ("file_analysis", "python_source", python_source.file_analysis, 1),
    ("find_references", "symbol_resolution", symbol_resolution.find_references, 2),
    (
        "installed_packages_analysis",
        "installed_packages",
        installed_packages.installed_packages_analysis,
        0,
    ),
    ("json_analysis", "json_config", json_config.json_analysis, 1),
    ("module_analysis", "python_source", python_source.module_analysis, 2),
    (
        "module_symbol_table",
        "symbol_resolution",
        symbol_resolution.module_symbol_table,
        2,
    ),
    ("notebook_analysis", "notebook", notebook.notebook_analysis, 1),
    (
        "requirements_analysis",
        "requirements_txt",
        requirements_txt.requirements_analysis,
        1,
    ),
    (
        "resolve_import_name",
        "installed_packages",
        installed_packages.resolve_import_name,
        1,
    ),
    (
        "resolve_module_path",
        "deep_module_resolution",
        deep_module_resolution.resolve_module_path,
        1,
    ),
    ("scope_tree", "scope_resolution", scope_resolution.scope_tree, 1),
    ("symbol_at", "scope_resolution", scope_resolution.symbol_at, 2),
    ("workspace_analysis", "python_source", python_source.workspace_analysis, 1),
    (
        "workspace_applicable_requirements",
        "requirement_evaluation",
        requirement_evaluation.workspace_applicable_requirements,
        1,
    ),
    (
        "workspace_config_analysis",
        "toml_config",
        toml_config.workspace_config_analysis,
        1,
    ),
    ("workspace_csv_analysis", "csv_data", csv_data.workspace_csv_analysis, 1),
    (
        "workspace_dependency_check",
        "dependency_check",
        dependency_check.workspace_dependency_check,
        2,
    ),
    ("workspace_env_analysis", "env_file", env_file.workspace_env_analysis, 1),
    (
        "workspace_json_analysis",
        "json_config",
        json_config.workspace_json_analysis,
        1,
    ),
    (
        "workspace_notebook_analysis",
        "notebook",
        notebook.workspace_notebook_analysis,
        1,
    ),
    (
        "workspace_requirements_analysis",
        "requirements_txt",
        requirements_txt.workspace_requirements_analysis,
        1,
    ),
    (
        "workspace_symbol_index",
        "symbol_resolution",
        symbol_resolution.workspace_symbol_index,
        1,
    ),
    ("workspace_xml_analysis", "xml_config", xml_config.workspace_xml_analysis, 1),
    ("xml_analysis", "xml_config", xml_config.xml_analysis, 1),
)
_CASE_IDS = tuple(case[0] for case in _LAYER3_CASES)


class _Hostile:
    def __init__(self) -> None:
        self.touched = False

    def _fail(self) -> NoReturn:
        self.touched = True
        raise AssertionError("Layer-3 argument was inspected before rejection")

    def __fspath__(self) -> str:
        return self._fail()

    def __iter__(self) -> Iterator[Any]:
        return self._fail()

    def __len__(self) -> int:
        return self._fail()

    def __getitem__(self, _key: object) -> Any:
        return self._fail()

    def __hash__(self) -> int:
        return self._fail()

    def __eq__(self, _other: object) -> bool:
        return self._fail()

    def __bool__(self) -> bool:
        return self._fail()

    def __str__(self) -> str:
        return self._fail()

    def __repr__(self) -> str:
        return self._fail()

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        return self._fail()


_CAUGHT_INPUT = Input[int]("layer3-query-context-caught-input")


def _dynamic(name: str) -> Any:
    return globals()[name]


def _case(name: str) -> Layer3Case:
    cases = cast(tuple[Layer3Case, ...], _dynamic("_LAYER3_CASES"))
    return next(case for case in cases if case[0] == name)


def _selected() -> tuple[Entrypoint, int]:
    name = cast(str, _dynamic("_SELECTED_ENTRYPOINT"))
    style = cast(str, _dynamic("_SELECTED_IMPORT_STYLE"))
    case_name, module_name, direct, arity = _case(name)
    if style == "direct":
        entrypoint = direct
    elif style == "aggregate":
        entrypoint = cast(Entrypoint, getattr(_dynamic("integration_api"), case_name))
    else:
        module = importlib.import_module(f"pyinc.integrations.{module_name}")
        entrypoint = cast(Entrypoint, getattr(module, case_name))
    return entrypoint, arity


def _invoke_selected(db: Database) -> Any:
    entrypoint, arity = _selected()
    hostile = cast(_Hostile, _dynamic("_SELECTED_HOSTILE"))
    return entrypoint(db, *(hostile for _ in range(arity)))


def _make_direct_query(case: Layer3Case) -> Any:
    name, _module_name, entrypoint, arity = case

    @query(key=f"layer3-query-context-direct-{name}")
    def attempt(db: Database) -> Any:
        hostile = cast(_Hostile, _dynamic("_SELECTED_HOSTILE"))
        return entrypoint(db, *(hostile for _ in range(arity)))

    return attempt


_DIRECT_QUERIES = {case[0]: _make_direct_query(case) for case in _LAYER3_CASES}


@query(key="layer3-query-context-uncaught")
def _uncaught_layer3(db: Database) -> Any:
    return _invoke_selected(db)


@query(key="layer3-query-context-caught")
def _caught_layer3(db: Database) -> int:
    try:
        _invoke_selected(db)
    except QueryContextError:
        return _CAUGHT_INPUT.read(db)
    raise AssertionError("Layer-3 entrypoint unexpectedly executed inside a query")


@query(key="layer3-query-context-helper")
def _invoke_helper(db: Database) -> None:
    helper = cast(str, _dynamic("_SELECTED_ENTRYPOINT"))
    hostile = cast(_Hostile, _dynamic("_SELECTED_HOSTILE"))
    decoding = cast(Any, _dynamic("_decoding"))
    if helper == "decoded":
        decoding.decoded(
            db,
            cast(str, hostile),
            cast(tuple[Any, ...], hostile),
            cast(Callable[[], Any], hostile),
        )
    elif helper == "once_per_request":
        decoding.once_per_request(
            db,
            cast(str, hostile),
            cast(tuple[Any, ...], hostile),
            cast(Callable[[], Any], hostile),
        )
    elif helper == "request_scope":
        with decoding.request_scope(cast(Database, hostile)):
            pass
    else:
        decoding.request_inputs_changed()


def _make_direct_helper_query(helper_name: str, helper: Callable[..., Any]) -> Any:
    @query(key=f"layer3-query-context-helper-direct-{helper_name}")
    def attempt(db: Database) -> None:
        hostile = cast(_Hostile, _dynamic("_SELECTED_HOSTILE"))
        if helper_name in {"decoded", "once_per_request"}:
            helper(
                db,
                cast(str, hostile),
                cast(tuple[Any, ...], hostile),
                cast(Callable[[], Any], hostile),
            )
        elif helper_name == "request_scope":
            with helper(cast(Database, hostile)):
                pass
        else:
            helper()

    return attempt


_DIRECT_HELPER_QUERIES = {
    "decoded": _make_direct_helper_query("decoded", _decoding.decoded),
    "once_per_request": _make_direct_helper_query("once_per_request", _decoding.once_per_request),
    "request_scope": _make_direct_helper_query("request_scope", _decoding.request_scope),
    "request_inputs_changed": _make_direct_helper_query(
        "request_inputs_changed", _decoding.request_inputs_changed
    ),
}


def _select(name: str, style: str, hostile: _Hostile) -> None:
    globals()["_SELECTED_ENTRYPOINT"] = name
    globals()["_SELECTED_IMPORT_STYLE"] = style
    globals()["_SELECTED_HOSTILE"] = hostile


def test_layer3_inventory_covers_exactly_35_stable_aggregate_entrypoints() -> None:
    expected = set(_CASE_IDS)
    exported_functions = {
        name
        for name in integration_api.__all__
        if inspect.isfunction(getattr(integration_api, name))
    }

    assert len(_LAYER3_CASES) == len(expected) == 35
    assert exported_functions == expected | _HELPER_NAMES
    for name, module_name, direct, _arity in _LAYER3_CASES:
        assert getattr(integration_api, name) is direct
        runtime_module = importlib.import_module(f"pyinc.integrations.{module_name}")
        assert getattr(runtime_module, name) is direct
        assert direct.__name__ == name
        assert not hasattr(direct, "__wrapped__")


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("style", _IMPORT_STYLES)
@pytest.mark.parametrize("case", _LAYER3_CASES, ids=_CASE_IDS)
def test_every_layer3_entrypoint_rejects_before_arguments_or_memos_are_touched(
    mode: Mode, style: str, case: Layer3Case
) -> None:
    name = case[0]
    db = Database(mode=mode)
    hostile = _Hostile()
    _select(name, style, hostile)
    attempted_query = _DIRECT_QUERIES[name] if style == "direct" else _uncaught_layer3

    sentinel = object()
    with _decoding.request_scope(db):
        assert _decoding.once_per_request(db, "sentinel", (), lambda: sentinel) is sentinel
        scope = _decoding._REQUEST.get()
        assert scope is not None
        memo_before = dict(scope[1])

        with pytest.raises(QueryContextError, match=r"Layer-3.*Layer-2"):
            db.get(attempted_query)

        assert scope[1] == memo_before

    assert hostile.touched is False
    assert _decoding._CACHES.get(db) is None
    statistics = db.statistics()
    assert statistics.resource_loads == 0
    assert statistics.resource_probe_hits == 0


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("case", _LAYER3_CASES, ids=_CASE_IDS)
def test_caught_layer3_errors_remain_warm_fresh_consistent(mode: Mode, case: Layer3Case) -> None:
    name = case[0]
    hostile = _Hostile()
    _select(name, "aggregate", hostile)
    warm = Database(mode=mode)
    warm.set(_CAUGHT_INPUT, 1)
    assert warm.get(_caught_layer3) == 1

    warm.set(_CAUGHT_INPUT, 2)
    fresh = Database(mode=mode)
    fresh.set(_CAUGHT_INPUT, 2)

    assert warm.get(_caught_layer3) == fresh.get(_caught_layer3) == 2
    assert hostile.touched is False


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("style", _IMPORT_STYLES)
def test_caught_layer3_error_is_sound_after_same_mode_checkpoint(mode: Mode, style: str) -> None:
    hostile = _Hostile()
    _select("scope_tree", style, hostile)
    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    writer.set(_CAUGHT_INPUT, 3)
    assert writer.get(_caught_layer3) == 3
    checkpoint = writer.save_checkpoint()

    warmed = Database(mode=mode, store=store)
    warmed.set(_CAUGHT_INPUT, 3)
    warmed.load_checkpoint(checkpoint)
    assert warmed.get(_caught_layer3) == 3
    warmed.set(_CAUGHT_INPUT, 4)

    fresh = Database(mode=mode)
    fresh.set(_CAUGHT_INPUT, 4)
    assert warmed.get(_caught_layer3) == fresh.get(_caught_layer3) == 4
    assert hostile.touched is False


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize(
    "helper",
    ("decoded", "once_per_request", "request_scope", "request_inputs_changed"),
)
def test_layer3_helpers_reject_before_request_or_decode_memos_are_touched(
    mode: Mode, helper: str
) -> None:
    db = Database(mode=mode)
    hostile = _Hostile()
    _select(helper, "direct", hostile)
    attempted_query = _DIRECT_HELPER_QUERIES[helper]
    sentinel = object()

    with _decoding.request_scope(db):
        assert _decoding.once_per_request(db, "sentinel", (), lambda: sentinel) is sentinel
        scope = _decoding._REQUEST.get()
        assert scope is not None
        memo_before = dict(scope[1])

        with pytest.raises(QueryContextError, match=r"Layer-3.*Layer-2"):
            db.get(attempted_query)

        assert scope[1] == memo_before

    assert hostile.touched is False
    assert _decoding._CACHES.get(db) is None
