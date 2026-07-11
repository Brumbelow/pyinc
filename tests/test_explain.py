from __future__ import annotations

import os
import sys
from dataclasses import FrozenInstanceError, dataclass
from types import ModuleType
from typing import cast

import pytest

import pyinc
from pyinc import (
    CaptureInfo,
    Database,
    FileResource,
    Input,
    explain_query_captures,
    query,
)
from pyinc.errors import UnsupportedValueError
from pyinc.explain import InspectionNode, format_explanation


def test_new_capture_diagnostics_are_exported_from_package() -> None:
    for name in ("CaptureInfo", "explain_query_captures"):
        assert name in pyinc.__all__
        assert hasattr(pyinc, name)


def _leaf(label: str = "leaf", reason: str = "set by user") -> InspectionNode:
    return InspectionNode(
        label=label,
        kind="input",
        changed_at=1,
        verified_at=1,
        last_decision="green",
        last_recompute="never",
        reason=reason,
    )


def test_format_explanation_single_node() -> None:
    node = _leaf()
    output = format_explanation(node)
    assert "leaf: green" in output
    assert "reason: set by user" in output
    assert output.count("\n") == 1


def test_format_explanation_with_dependencies() -> None:
    child = _leaf(label="child_input")
    parent = InspectionNode(
        label="parent_query",
        kind="query",
        changed_at=2,
        verified_at=2,
        last_decision="recomputed",
        last_recompute="r2",
        reason="",
        dependencies=(child,),
    )
    output = format_explanation(parent)
    lines = output.split("\n")
    assert lines[0].startswith("- parent_query:")
    assert lines[1].startswith("  - child_input:")


def test_format_explanation_with_untracked_reasons() -> None:
    node = InspectionNode(
        label="impure_query",
        kind="query",
        changed_at=1,
        verified_at=1,
        last_decision="recomputed",
        last_recompute="r1",
        reason="",
        untracked_reasons=("dynamic __all__", "os.getenv call"),
    )
    output = format_explanation(node)
    assert "untracked: dynamic __all__" in output
    assert "untracked: os.getenv call" in output


def test_inspection_node_is_untracked_property() -> None:
    clean = _leaf()
    assert not clean.is_untracked

    impure = InspectionNode(
        label="q",
        kind="query",
        changed_at=1,
        verified_at=1,
        last_decision="green",
        last_recompute="r1",
        reason="",
        untracked_reasons=("raw read",),
    )
    assert impure.is_untracked


_IMMUTABLE_TUPLE = ("a", "b")
_IMMUTABLE_FROZENSET = frozenset({1, 2, 3})
_MUTABLE_DICT = {"x": 1}
_MUTABLE_LIST = [1, 2, 3]


@dataclass(frozen=True)
class _FrozenConfig:
    name: str
    limit: int


@dataclass
class _MutableConfig:
    name: str


_FROZEN_CONFIG = _FrozenConfig(name="alpha", limit=10)
_MUTABLE_CONFIG = _MutableConfig(name="beta")


def test_explain_query_captures_reports_metadata_without_ambient_captures() -> None:
    @query
    def bare(db: Database) -> int:
        return 1

    infos = explain_query_captures(bare)
    assert {item.name for item in infos} == {"annotation[db]", "annotation[return]"}
    assert all(item.accepted and item.kind == "annotation" for item in infos)


def test_explain_query_captures_accepts_query_decorator_or_plain_function() -> None:
    suffix = ("!",)

    @query
    def decorated(db: Database) -> str:
        return suffix[0]

    def plain(db: Database) -> str:
        return suffix[0]

    decorated_infos = explain_query_captures(decorated)
    plain_infos = explain_query_captures(plain)
    decorated_by_name = {info.name: info for info in decorated_infos}
    plain_by_name = {info.name: info for info in plain_infos}
    assert "suffix" in decorated_by_name
    assert "suffix" in plain_by_name
    assert decorated_by_name["suffix"].accepted
    assert plain_by_name["suffix"].accepted
    assert decorated_by_name["suffix"].kind == "value"


def test_explain_query_captures_reports_accepted_function_custom_state() -> None:
    def raw(db: Database) -> int:
        return 1

    raw.build_flag = 7  # type: ignore[attr-defined]
    info = {item.name: item for item in explain_query_captures(raw)}["attribute[build_flag]"]

    assert info.accepted
    assert info.origin == "attribute"
    assert info.kind == "value"


def test_explain_query_captures_classifies_accepted_kinds() -> None:
    count = Input[int]("count")
    file_resource = FileResource()

    @query
    def upstream(db: Database) -> int:
        return count.read(db)

    @query
    def consumer(db: Database, path: str) -> int:
        file_resource.read(db, path)
        return count.read(db) + upstream(db)

    infos = explain_query_captures(consumer)
    by_name = {info.name: info for info in infos}

    assert by_name["upstream"].kind == "query"
    assert by_name["upstream"].accepted
    assert by_name["count"].kind == "input"
    assert by_name["count"].accepted
    assert by_name["file_resource"].kind == "resource"
    assert by_name["file_resource"].accepted


def test_explain_query_captures_classifies_rejected_mutable_closure() -> None:
    box = {"x": 1}

    @query
    def read_box(db: Database) -> int:
        return box["x"]

    infos = explain_query_captures(read_box)
    by_name = {info.name: info for info in infos}
    box_info = by_name["box"]
    assert not box_info.accepted
    assert box_info.kind == "rejected"
    assert box_info.rejection_reason


def test_explain_query_captures_classifies_rejected_mutable_global() -> None:
    @query
    def read_dict(db: Database) -> int:
        return _MUTABLE_DICT["x"]

    infos = explain_query_captures(read_dict)
    by_name = {info.name: info for info in infos}
    assert not by_name["_MUTABLE_DICT"].accepted
    assert by_name["_MUTABLE_DICT"].kind == "rejected"


def test_explain_query_captures_classifies_rejected_mutable_list() -> None:
    @query
    def read_list(db: Database) -> int:
        return _MUTABLE_LIST[0]

    infos = explain_query_captures(read_list)
    by_name = {info.name: info for info in infos}
    assert not by_name["_MUTABLE_LIST"].accepted


def test_explain_query_captures_accepts_frozen_dataclass_rejects_mutable_dataclass() -> None:
    @query
    def frozen_ok(db: Database) -> str:
        return _FROZEN_CONFIG.name

    @query
    def mutable_bad(db: Database) -> str:
        return _MUTABLE_CONFIG.name

    frozen_info = {i.name: i for i in explain_query_captures(frozen_ok)}["_FROZEN_CONFIG"]
    mutable_info = {i.name: i for i in explain_query_captures(mutable_bad)}["_MUTABLE_CONFIG"]
    assert frozen_info.accepted
    assert frozen_info.kind == "value"
    assert not mutable_info.accepted
    assert "Mutable dataclass" in mutable_info.rejection_reason


def test_explain_query_captures_accepts_tuple_and_frozenset() -> None:
    @query
    def uses_immutable(db: Database) -> str:
        return f"{_IMMUTABLE_TUPLE[0]}-{sorted(_IMMUTABLE_FROZENSET)[0]}"

    infos = explain_query_captures(uses_immutable)
    by_name = {i.name: i for i in infos}
    assert by_name["_IMMUTABLE_TUPLE"].accepted
    assert by_name["_IMMUTABLE_FROZENSET"].accepted


def test_explain_query_captures_accepts_module_and_function_captures() -> None:
    def helper(value: int) -> int:
        return value + 1

    @query
    def uses_module_and_func(db: Database) -> int:
        return helper(len(os.sep))

    infos = explain_query_captures(uses_module_and_func)
    by_name = {i.name: i for i in infos}
    assert by_name["os"].kind == "module"
    assert by_name["helper"].kind == "function"


def test_explain_query_captures_matches_dynamic_module_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("pyinc_explain_dynamic_module")
    module.answer = 1  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)

    @query
    def uses_dynamic_module(db: Database) -> int:
        return cast(int, module.answer)

    info = {item.name: item for item in explain_query_captures(uses_dynamic_module)}["module"]
    assert not info.accepted
    assert info.kind == "rejected"
    assert "stable source identity" in info.rejection_reason

    with pytest.raises(UnsupportedValueError, match="stable source identity"):
        Database().get(uses_dynamic_module)


def test_explain_query_captures_rejects_local_type_capture() -> None:
    class LocalHelper:
        value = 1

    @query
    def uses_local_type(db: Database) -> int:
        return LocalHelper.value

    info = {item.name: item for item in explain_query_captures(uses_local_type)}["LocalHelper"]
    assert not info.accepted
    assert info.kind == "rejected"
    assert "Local type" in info.rejection_reason


def test_explain_query_captures_rejects_non_function() -> None:
    with pytest.raises(TypeError):
        explain_query_captures("not a function")


def test_explain_query_captures_matches_fingerprint_rejections() -> None:
    box = {"y": 5}

    @query
    def fail_on_get(db: Database) -> int:
        return box["y"]

    diagnostic = {i.name: i for i in explain_query_captures(fail_on_get)}["box"]
    assert not diagnostic.accepted

    with pytest.raises(UnsupportedValueError):
        Database().get(fail_on_get)


def test_runtime_capture_error_points_to_preflight_diagnostics() -> None:
    box = {"z": 7}

    @query
    def fail_on_get(db: Database) -> int:
        return box["z"]

    with pytest.raises(UnsupportedValueError, match="explain_query_captures"):
        Database().get(fail_on_get)


def test_capture_info_is_frozen() -> None:
    info = CaptureInfo(name="x", origin="closure", type_name="int", accepted=True, kind="value")
    with pytest.raises(FrozenInstanceError):
        info.accepted = False  # type: ignore[misc]


def test_explain_query_captures_reports_origin_for_closure_vs_global() -> None:
    local_tuple = ("inside",)

    @query
    def mixed(db: Database) -> str:
        return f"{local_tuple[0]}-{_IMMUTABLE_TUPLE[0]}"

    infos = explain_query_captures(mixed)
    by_name = {i.name: i for i in infos}
    assert by_name["local_tuple"].origin == "closure"
    assert by_name["_IMMUTABLE_TUPLE"].origin == "global"
