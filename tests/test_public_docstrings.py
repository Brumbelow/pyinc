"""Every public class, function and `Database` method carries its own docstring."""

from __future__ import annotations

import dataclasses
import functools
import importlib
import inspect
from typing import Any

import pyinc
from pyinc import Database

# Exports that cannot hold a docstring of their own. `BUILTIN_ADAPTERS` is a
# read-only mapping, and the two observer types are aliases of
# `collections.abc.Callable`. The kernel contract's public surface tables
# describe all three.
_EXEMPT_EXPORTS = frozenset({"BUILTIN_ADAPTERS", "ObserverCallback", "ObserverErrorHook"})

_MODULES_WITH_DOCSTRINGS = (
    "pyinc",
    "pyinc.runtime",
    "pyinc.integrations",
    "pyinc_tools",
    "pyinc_codegen",
)


def _is_function(obj: Any) -> bool:
    """Return whether `obj` is a Python function or a builtin."""
    return inspect.isfunction(obj) or inspect.isbuiltin(obj)


def _own_docstring(obj: Any) -> str:
    """Return the docstring written on `obj` itself, or an empty string.

    A class reads its docstring from its own namespace, so one inherited from a
    base does not count. A dataclass with none gets one generated from its
    signature, `Name(field, ...)`, and that does not count either. A function,
    a builtin and a module return their `__doc__`. Any other object returns an
    empty string. A callable instance such as a partial reads its type's
    `__doc__` unless one is assigned to it, so the helper does not count it.
    """
    if inspect.isclass(obj):
        doc = vars(obj).get("__doc__")
        if not isinstance(doc, str):
            return ""
        if dataclasses.is_dataclass(obj) and doc.startswith(f"{obj.__name__}("):
            return ""
        return doc.strip()
    if not (_is_function(obj) or inspect.ismodule(obj)):
        return ""
    doc = getattr(obj, "__doc__", None)
    return doc.strip() if isinstance(doc, str) else ""


def test_a_callable_instance_has_no_docstring_of_its_own() -> None:
    # `inspect.isroutine` accepts any method descriptor. On 3.14 that includes
    # a partial, and its `__doc__` is the partial type's.
    assert _own_docstring(functools.partial(len)) == ""


def test_every_exported_class_and_function_has_its_own_docstring() -> None:
    exports = {name: getattr(pyinc, name) for name in pyinc.__all__ if name not in _EXEMPT_EXPORTS}
    not_documentable = [
        name
        for name, value in exports.items()
        if not (inspect.isclass(value) or _is_function(value))
    ]
    undocumented = [name for name, value in exports.items() if not _own_docstring(value)]

    assert not not_documentable, (
        "exports that are neither a class nor a function and are not exempt: "
        + ", ".join(not_documentable)
    )
    assert not undocumented, "exports without a docstring: " + ", ".join(undocumented)


def test_every_exempt_export_is_one_that_cannot_hold_a_docstring() -> None:
    for name in sorted(_EXEMPT_EXPORTS):
        assert name in pyinc.__all__, name
        value = getattr(pyinc, name)
        assert not inspect.isclass(value), name
        assert not _is_function(value), name


def test_every_public_database_method_has_a_docstring() -> None:
    public = {name: value for name, value in vars(Database).items() if not name.startswith("_")}
    assert {"get", "set", "set_many", "read_input", "read_resource", "revision"} <= set(public)

    undocumented: list[str] = []
    for name, value in sorted(public.items()):
        function = value.fget if isinstance(value, property) else value
        function = getattr(function, "__func__", function)
        assert inspect.isfunction(function), f"Database.{name} is not a method"
        if not _own_docstring(function):
            undocumented.append(name)

    assert not undocumented, "Database methods without a docstring: " + ", ".join(undocumented)


def test_the_packages_and_the_runtime_module_have_module_docstrings() -> None:
    undocumented = [
        name
        for name in _MODULES_WITH_DOCSTRINGS
        if not _own_docstring(importlib.import_module(name))
    ]

    assert not undocumented, "modules without a docstring: " + ", ".join(undocumented)
