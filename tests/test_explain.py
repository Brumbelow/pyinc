from __future__ import annotations

import functools
import importlib
import os
import sys
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, cast

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


def _wrapped_target(value: int) -> int:
    return value


class _ExplainScaler:
    def __init__(self, k: int) -> None:
        self.k = k
        functools.wraps(_wrapped_target)(self)

    def __call__(self, value: int) -> int:
        return self.k * value


_explain_scaler = _ExplainScaler(2)


class _ExplainUnsafeScaler:
    def __init__(self) -> None:
        self.state = {"mutable": True}
        functools.wraps(_wrapped_target)(self)

    def __call__(self) -> int:
        return 1


_explain_unsafe = _ExplainUnsafeScaler()


class _ExplainWrappedClass:
    __wrapped__ = _wrapped_target


class _ExplainMethodHolder:
    step = 5
    factor = 4

    @classmethod
    def scaled(cls, value: int) -> int:
        return cls.step + value

    @classmethod
    def times(cls, value: int) -> int:
        return cls.factor * value


# wraps() is applied to the underlying function instead of decorating the method
# with it: the runtime object is the same either way, but as a decorator the type
# checker reads the result as carrying _wrapped_target's signature.
functools.wraps(_wrapped_target)(vars(_ExplainMethodHolder)["scaled"].__func__)

_explain_bound_method = _ExplainMethodHolder.scaled
_explain_plain_method = _ExplainMethodHolder.times


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


def test_dynamically_read_module_is_rejected_by_explain_and_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_explain_dynamic_attribute"
    (tmp_path / f"{module_name}.py").write_text("VALUE = 3\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)

    @query
    def reads_dynamically(db: Database) -> int:
        return cast(int, getattr(module, "VALUE"))  # noqa: B009 - the shape under test

    # The module has a real source file, so its identity payload alone accepts
    # it; what refuses it is the fold over the attribute paths the body reads
    # statically, of which this body has none.
    info = {item.name: item for item in explain_query_captures(reads_dynamically)}["module"]
    assert not info.accepted
    assert info.kind == "rejected"
    assert "dynamically" in info.rejection_reason
    with pytest.raises(UnsupportedValueError, match="dynamically"):
        Database().get(reads_dynamically)


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


def test_wrapped_callable_capture_is_classified_as_callable() -> None:
    @query
    def scaled(db: Database) -> int:
        return _explain_scaler(10)

    report = {item.name: item for item in explain_query_captures(scaled)}
    info = report["_explain_scaler"]
    assert info.accepted is True
    assert info.kind == "callable"
    # The kernel accepts the same capture: parity in the accepting direction.
    assert Database().get(scaled) == 20


def test_unsafe_wrapped_callable_is_rejected_by_explain_and_kernel() -> None:
    @query
    def broken(db: Database) -> int:
        return _explain_unsafe()

    report = {item.name: item for item in explain_query_captures(broken)}
    info = report["_explain_unsafe"]
    assert info.accepted is False
    assert info.kind == "rejected"
    with pytest.raises(UnsupportedValueError):
        Database().get(broken)


def test_wrapped_bound_method_capture_is_classified_as_method() -> None:
    @query
    def offset(db: Database) -> int:
        return _explain_bound_method(10)

    report = {item.name: item for item in explain_query_captures(offset)}
    info = report["_explain_bound_method"]
    # A bound method carrying __wrapped__ is dispatched as a method on both
    # surfaces: the kernel tests for one before probing __wrapped__, so the
    # report must not describe it as a callable object it cannot fingerprint.
    assert info.accepted is True
    assert info.kind == "method"
    assert Database().get(offset) == 15


def test_bound_method_capture_is_classified_as_method() -> None:
    @query
    def multiplied(db: Database) -> int:
        return _explain_plain_method(3)

    report = {item.name: item for item in explain_query_captures(multiplied)}
    info = report["_explain_plain_method"]
    assert info.accepted is True
    assert info.kind == "method"
    assert Database().get(multiplied) == 12


def test_wrapped_class_capture_is_still_classified_as_type() -> None:
    @query
    def read_class(db: Database) -> str:
        return _ExplainWrappedClass.__name__

    report = {item.name: item for item in explain_query_captures(read_class)}
    assert report["_ExplainWrappedClass"].kind == "type"


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


def test_reflective_namespace_reads_surface_in_explain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_name = "pyinc_explain_reflective"
    (tmp_path / f"{module_name}.py").write_text(
        'CONFIG_MODE = "A"\n\n\ndef reader():\n    return globals()["CONFIG_MODE"]\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    module = importlib.import_module(module_name)
    reader = module.reader

    @query
    def read_config(db: Database) -> str:
        return cast(str, reader())

    report = explain_query_captures(read_config)
    rejected = [item for item in report if not item.accepted]
    assert rejected, "the reflective capture must not be reported clean"
    reasons = " ".join(item.rejection_reason for item in rejected)
    assert "reflective" in reasons


def test_reflective_read_in_the_query_body_itself_surfaces_in_explain() -> None:
    @query
    def direct(db: Database) -> Any:
        return globals().get("does_not_matter")

    report = explain_query_captures(direct)
    names = {item.name for item in report if not item.accepted}
    assert "reflective[globals]" in names


def test_query_handle_state_is_reported_and_accepted_by_both_surfaces() -> None:
    @query
    def keeps_handle_state(db: Database) -> int:
        return 1

    cast(Any, keeps_handle_state).revision = 3

    # The refusing shape below is only half the parity claim: handle state the
    # fold accepts has to be reported as carried and accepted, not omitted.
    report = {item.name: item for item in explain_query_captures(keeps_handle_state)}
    info = report["handle[revision]"]
    assert info.accepted is True
    assert info.origin == "handle"
    assert Database().get(keeps_handle_state) == 1


def test_unsafe_query_handle_state_is_rejected_by_explain_and_kernel() -> None:
    @query
    def holds_mutable_handle_state(db: Database) -> int:
        return 1

    cast(Any, holds_mutable_handle_state).cache = {"seen": 1}

    report = {item.name: item for item in explain_query_captures(holds_mutable_handle_state)}
    info = report["handle[cache]"]
    assert info.accepted is False
    assert info.kind == "rejected"
    with pytest.raises(UnsupportedValueError):
        Database().get(holds_mutable_handle_state)


def test_rebound_wrapped_on_a_query_handle_is_rejected_by_explain_and_kernel() -> None:
    @query
    def rebinds_wrapped(db: Database) -> int:
        return 1

    # `__wrapped__` is a contract name, so the per-entry walk never looks at it;
    # the fold of the whole handle is the only thing that reaches this refusal,
    # and without it explain would call a query the kernel refuses accepted.
    cast(Any, rebinds_wrapped).__wrapped__ = {"seen": 1}

    report = {item.name: item for item in explain_query_captures(rebinds_wrapped)}
    info = report["handle[*]"]
    assert info.accepted is False
    assert info.kind == "rejected"
    assert info.origin == "handle"
    assert "__wrapped__" in info.rejection_reason
    with pytest.raises(UnsupportedValueError):
        Database().get(rebinds_wrapped)


def test_non_string_handle_state_key_is_rejected_by_explain_and_kernel() -> None:
    @query
    def keyed_by_a_number(db: Database) -> int:
        return 1

    # The second shape the per-entry walk cannot report: a name that is not a
    # string is skipped by the walk -- it has no entry to be reported under --
    # and the fold of the whole handle is what refuses it.
    state: dict[Any, Any] = vars(keyed_by_a_number)
    state[42] = 1

    report = {item.name: item for item in explain_query_captures(keyed_by_a_number)}
    info = report["handle[*]"]
    assert info.accepted is False
    assert info.kind == "rejected"
    assert info.origin == "handle"
    assert "invalid custom state" in info.rejection_reason
    with pytest.raises(UnsupportedValueError, match="invalid custom state"):
        Database().get(keyed_by_a_number)


def test_invalid_type_parameters_on_a_query_handle_are_rejected_by_explain_and_kernel() -> None:
    @query
    def holds_type_parameters(db: Database) -> int:
        return 1

    # The third: `__type_params__` is a contract name the walk skips, and the
    # fold refuses anything but a tuple there rather than folding whatever the
    # handle carries.
    cast(Any, holds_type_parameters).__type_params__ = [1]

    report = {item.name: item for item in explain_query_captures(holds_type_parameters)}
    info = report["handle[*]"]
    assert info.accepted is False
    assert info.kind == "rejected"
    assert info.origin == "handle"
    assert "invalid type parameters" in info.rejection_reason
    with pytest.raises(UnsupportedValueError, match="invalid type parameters"):
        Database().get(holds_type_parameters)


class _Outer:
    class Nested:
        pass


def test_explain_reports_defaults_kwdefaults_attributes_and_type_parameters() -> None:
    def configured(
        db: Database,
        value: tuple[int, ...] = (1, 2),
        *,
        window: range = range(3),
    ) -> int:
        return value[0] + len(window)

    configured_metadata = cast(Any, configured)
    configured_metadata.helper = len
    configured_metadata.captured_type = _Outer.Nested
    configured_metadata.__type_params__ = (int,)

    infos = explain_query_captures(configured)
    by_name = {info.name: info for info in infos}
    assert by_name["default[0]"].origin == "default"
    assert by_name["kwdefault[window]"].origin == "kwdefault"
    assert by_name["attribute[helper]"].kind == "builtin"
    assert by_name["attribute[captured_type]"].kind == "type"
    assert by_name["type_parameter[0]"].kind == "annotation"


@pytest.mark.skipif(sys.version_info < (3, 14), reason="lazy annotations require Python 3.14")
def test_explain_falls_back_to_lazy_annotation_evaluator() -> None:
    namespace: dict[str, object] = {}
    code = compile(
        "def annotated(value: MissingType): return value",
        "<lazy-annotation-test>",
        "exec",
        dont_inherit=True,
    )
    exec(code, namespace)
    annotated = cast(Any, namespace["annotated"])

    infos = explain_query_captures(annotated)
    assert len(infos) == 1
    assert infos[0].name == "annotations"
    assert infos[0].origin == "annotation_evaluator"
    assert infos[0].kind == "annotation"
    assert infos[0].accepted


@pytest.mark.skipif(sys.version_info < (3, 14), reason="lazy annotations require Python 3.14")
def test_explain_ignores_nonfunction_lazy_annotation_evaluator() -> None:
    class BrokenAnnotations:
        def __call__(self, format: int) -> object:
            raise RuntimeError(format)

    def target() -> None:
        return None

    cast(Any, target).__annotate__ = BrokenAnnotations()
    assert explain_query_captures(target) == ()
