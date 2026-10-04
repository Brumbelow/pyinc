"""A query may capture a name the ambient-read guard replaced, by any route.

Once a `Database` exists, `from os import getcwd` binds the guard's wrapper in
place of the builtin. The wrapper is a closure over pyinc's own state (the
active guards, the working-directory flag, the original it calls), which the
capture walk cannot fold. So the kernel recognises every callable the guard
installed. It pins a capture of one by the standard-library callable it guards:
that callable's module and qualified name, its module's identity, and the
interpreter build. These cells pin the registry, every route a capture takes,
and the preview. They also pin that a name bound before the first `Database`
(the unguarded original) fingerprints as it did before.
"""

from __future__ import annotations

import builtins
import importlib
import io
import json
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import FunctionType, MethodType, ModuleType
from typing import Any

import pytest

import pyinc
from pyinc import Database, InMemoryArtifactStore, UntrackedReadError, explain_query_captures
from pyinc import runtime as pyinc_runtime
from pyinc.runtime import _GUARDED_NAMES, _guarded_name, _GuardedEnviron, _is_guarded_name
from pyinc.value import fingerprint_snapshot

# Each guarded callable maps to three strings: the line that binds it as `W`, a
# call through `{f}` whatever route reached it, and the same call spelled
# through its module. The guard refuses or answers that last spelling today.
# `arg` is the query's argument: either a directory holding `sample.txt`, or
# `relative`.
_GUARDED: dict[str, tuple[str, str, str]] = {
    "builtins.open": (
        "from builtins import open as W",
        "{f}(os.path.join(arg, 'sample.txt')).read()",
        "builtins.open",
    ),
    "io.open": (
        "from io import open as W",
        "{f}(os.path.join(arg, 'sample.txt')).read()",
        "io.open",
    ),
    "os.getenv": ("from os import getenv as W", "{f}('PYINC_GUARDED_NAME')", "os.getenv"),
    "os.listdir": ("from os import listdir as W", "sorted({f}(arg))", "os.listdir"),
    "os.scandir": (
        "from os import scandir as W",
        "sorted(entry.name for entry in {f}(arg))",
        "os.scandir",
    ),
    "os.getcwd": ("from os import getcwd as W", "{f}()", "os.getcwd"),
    "os.getcwdb": ("from os import getcwdb as W", "{f}()", "os.getcwdb"),
    "os.path.realpath": ("from os.path import realpath as W", "{f}(arg)", "os.path.realpath"),
    "os.path.abspath": ("from os.path import abspath as W", "{f}(arg)", "os.path.abspath"),
    "Path.iterdir": (
        "from pathlib import Path\nW = Path.iterdir",
        "sorted(child.name for child in {f}(pathlib.Path(arg)))",
        "pathlib.Path.iterdir",
    ),
    "Path.cwd": ("from pathlib import Path\nW = Path.cwd", "str({f}())", "pathlib.Path.cwd"),
    "Thread.start": (
        "from threading import Thread\nW = Thread.start",
        "_listed_in_a_thread({f}, arg)",
        "threading.Thread.start",
    ),
}
if sys.platform != "win32":
    _GUARDED["os.getenvb"] = (
        "from os import getenvb as W",
        "{f}(b'PYINC_GUARDED_NAME')",
        "os.getenvb",
    )

# The prelude of every generated query module. It imports the modules the
# baseline spellings name, and defines a helper that starts a thread through
# whatever `start` it is handed. The helper reports what a raw read inside the
# thread met.
_PRELUDE = """\
import builtins
import io
import os
import pathlib
import threading

from pyinc import query


def _listed_in_a_thread(start, path):
    seen = []

    def body():
        try:
            seen.append(sorted(os.listdir(path)))
        except Exception as exc:
            seen.append(type(exc).__name__ + ": " + str(exc))

    thread = threading.Thread(target=body)
    start(thread)
    thread.join()
    return seen[0]
"""

# Every route a capture can take to the query's identity. `{call}` is the
# guarded call with the route's handle in place of `{f}`.
_ROUTES: dict[str, str] = {
    "global": "@query(key=KEY)\ndef q(db, arg):\n    return {call}\n",
    "default": "@query(key=KEY)\ndef q(db, arg, f=W):\n    return {call}\n",
    "kwdefault": "@query(key=KEY)\ndef q(db, arg, *, f=W):\n    return {call}\n",
    "closure": (
        "def make():\n    f = W\n\n    @query(key=KEY)\n    def q(db, arg):\n"
        "        return {call}\n\n    return q\n\n\nq = make()\n"
    ),
    "tuple": "T = (W, 1)\n\n\n@query(key=KEY)\ndef q(db, arg):\n    f = T[0]\n    return {call}\n",
    "helper": (
        "def helper():\n    return W\n\n\n@query(key=KEY)\ndef q(db, arg):\n"
        "    f = helper()\n    return {call}\n"
    ),
    # A mutable module global makes the helper's definition fold refuse, so
    # the kernel pins it by its source and folds each global separately.
    "source-pinned": (
        "CACHE = {{'k': 1}}\n\n\ndef helper():\n    CACHE['k']\n    return W\n\n\n"
        "@query(key=KEY)\ndef q(db, arg):\n    f = helper()\n    return {call}\n"
    ),
    "module-attribute": (
        "import {binding_module} as H\n\n\n@query(key=KEY)\ndef q(db, arg):\n    return {call}\n"
    ),
    # Folded with the handle the body reads it off.
    "handle-attribute": (
        "@query(key=KEY)\ndef q(db, arg):\n    f = q.f\n    return {call}\n\n\nq.f = W\n"
    ),
    # Folded with the class, whose body holds it as a static method.
    "class-attribute": (
        "class Holder:\n    f = staticmethod(W)\n\n\n@query(key=KEY)\ndef q(db, arg):\n"
        "    f = Holder.f\n    return {call}\n"
    ),
}
_ROUTE_HANDLES = {
    "global": "W",
    "default": "f",
    "kwdefault": "f",
    "closure": "f",
    "tuple": "f",
    "helper": "f",
    "source-pinned": "f",
    "module-attribute": "H.W",
    "handle-attribute": "f",
    "class-attribute": "f",
}
# Every name on every route, except `Path.cwd` held in a class body. `Path.cwd`
# reads as a bound method. A class body is folded through a Python function's
# descriptor for any method, and `staticmethod` of a bound method is no such
# descriptor.
_CAPTURES = [
    (label, route)
    for label in sorted(_GUARDED)
    for route in sorted(_ROUTES)
    if (label, route) != ("Path.cwd", "class-attribute")
]


@pytest.fixture
def sample_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    (directory / "sample.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("PYINC_GUARDED_NAME", "value")
    return directory


@pytest.fixture
def module_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[str], ModuleType]]:
    """Write a module and import it, once a `Database` has installed the guard."""
    Database()
    source_root = tmp_path / "modules"
    source_root.mkdir()
    monkeypatch.syspath_prepend(str(source_root))
    created: list[str] = []

    def make(source: str) -> ModuleType:
        name = f"pyinc_guarded_capture_{uuid.uuid4().hex}"
        (source_root / f"{name}.py").write_text(source, encoding="utf-8")
        created.append(name)
        return importlib.import_module(name)

    yield make
    for name in created:
        sys.modules.pop(name, None)


def _outcome(query: Any, arg: str) -> tuple[str, Any]:
    """What a fresh database answers, or the refusal it raises."""
    try:
        return ("answered", Database().get(query, arg))
    except UntrackedReadError as exc:
        return ("refused", str(exc))


def _baseline(label: str, module_factory: Callable[[str], ModuleType]) -> Any:
    _binding, call, spelled = _GUARDED[label]
    source = _PRELUDE + (
        f"\n\n@query(key={f'baseline:{label}'!r})\ndef q(db, arg):\n"
        f"    return {call.format(f=spelled)}\n"
    )
    return module_factory(source).q


@pytest.mark.parametrize(("label", "route"), _CAPTURES)
def test_a_captured_guard_wrapper_fingerprints_and_still_guards(
    module_factory: Callable[[str], ModuleType],
    sample_directory: Path,
    label: str,
    route: str,
) -> None:
    """Every guarded callable, captured by every route, fingerprints and behaves as the guarded call.

    Before, each of these raised `UnsupportedValueError` at fingerprinting.
    That included `getcwd` and `getcwdb`, which fingerprinted as builtins until
    the working-directory guard replaced them. A capture now answers, or is
    refused, as the same call through its module is.
    """
    binding, call, _spelled = _GUARDED[label]
    binding_module = module_factory("from pyinc import query\n" + binding + "\n")
    source = (
        _PRELUDE
        + binding
        + "\n\nKEY = "
        + repr(f"guarded-capture:{label}:{route}")
        + "\n\n\n"
        + _ROUTES[route].format(
            call=call.format(f=_ROUTE_HANDLES[route]), binding_module=binding_module.__name__
        )
    )
    captured = module_factory(source).q
    baseline = _baseline(label, module_factory)

    arguments = [str(sample_directory)]
    if label in {"os.path.realpath", "os.path.abspath"}:
        arguments.append("relative")
    for arg in arguments:
        assert _outcome(captured, arg) == _outcome(baseline, arg)


# What the guard does with each call made through its module. The refusal
# names the call, and `realpath` and `abspath` answer for a fully qualified
# path. A thread a query starts meets the guard on its own raw read.
_REFUSED_AS = {
    "builtins.open": "Raw open() inside a query",
    "io.open": "Raw open() inside a query",
    "os.getenv": "Raw os.getenv() inside a query",
    "os.getenvb": "Raw os.getenvb() inside a query",
    "os.listdir": "Raw os.listdir() inside a query",
    "os.scandir": "Raw os.scandir() inside a query",
    "os.getcwd": "Raw os.getcwd() inside a query",
    "os.getcwdb": "Raw os.getcwdb() inside a query",
    "Path.iterdir": "Raw Path.iterdir() inside a query",
    "Path.cwd": "Raw Path.cwd() inside a query",
}


@pytest.mark.parametrize("label", sorted(_GUARDED))
def test_the_outcomes_compared_above_are_the_guards_own(
    module_factory: Callable[[str], ModuleType], sample_directory: Path, label: str
) -> None:
    """Pin the guarded call's own outcome, which the cells above compare captures with.

    Two refusals for the same wrong reason would also compare equal.
    """
    baseline = _baseline(label, module_factory)
    outcome = _outcome(baseline, str(sample_directory))
    if label in _REFUSED_AS:
        assert outcome[0] == "refused"
        assert _REFUSED_AS[label] in outcome[1]
    elif label == "Thread.start":
        assert outcome[0] == "answered"
        assert outcome[1].startswith("UntrackedReadError: Raw os.listdir() inside a query")
    else:
        name = label.rpartition(".")[2]
        assert outcome == ("answered", getattr(os.path, name)(str(sample_directory)))
        refused = _outcome(baseline, "relative")
        assert refused[0] == "refused"
        assert f"Raw os.path.{name}() of a relative path" in refused[1]


def test_every_callable_the_guard_installs_is_registered(tmp_path: Path) -> None:
    """The registry matches the callables the guard put in place, each beside its original.

    Every function defined in `pyinc.runtime` that sits where a standard-library
    callable did is a registered wrapper. Its entry names the original's own
    module and qualified name. The only other objects the guard installs are
    the two environment mappings. They are state, so they stay out of the
    registry.
    """
    Database()
    runtime_file = pyinc_runtime.__file__

    def defined_in_runtime(value: Any) -> bool:
        function = value.__func__ if isinstance(value, (classmethod, staticmethod)) else value
        return isinstance(function, FunctionType) and function.__code__.co_filename == runtime_file

    found: dict[int, Any] = {}
    for namespace in (
        vars(builtins),
        vars(io),
        vars(os),
        vars(os.path),
        vars(Path),
        vars(threading.Thread),
    ):
        for value in namespace.values():
            if defined_in_runtime(value):
                function = (
                    value.__func__ if isinstance(value, (classmethod, staticmethod)) else value
                )
                found[id(function)] = function
    assert set(found) == set(_GUARDED_NAMES)

    live = {
        label: wrapper.__func__ if isinstance(wrapper, MethodType) else wrapper
        for label, wrapper in _live_wrappers().items()
    }
    assert set(live) == set(_GUARDED)
    for label, wrapper in live.items():
        entry = _guarded_name(wrapper)
        assert entry is not None, label
        assert entry.wrapper is wrapper
        assert _guarded_name(entry.original) is None
        assert (entry.module, entry.qualname) == (
            entry.original.__module__,
            entry.original.__qualname__,
        )
    assert {id(wrapper) for wrapper in live.values()} == set(_GUARDED_NAMES)

    assert isinstance(os.environ, _GuardedEnviron)
    assert not _is_guarded_name(os.environ)
    if sys.platform != "win32":
        assert isinstance(os.environb, _GuardedEnviron)
        assert not _is_guarded_name(os.environb)


def _owner() -> None:
    """Stands in for the query function a payload builder names in its refusals."""


def _payload_routes(db: Database, value: Any) -> list[Any]:
    """The payload a value folds to on each route that folds one directly.

    A direct capture (a global, a default, a closure cell, a handle attribute
    or a source-pinned function's global), a module attribute, and a container
    member fold it whole. A function's definition, folded for a class body, a
    policy or a dataclass default factory, carries the same payload inside each
    route's own outer layer.
    """
    owner: Any = _owner
    member = db._freeze_captured_immutable("T[0]", value, set(), owner=owner, active_ids=set())
    assert member[0] == "captured-dependency"
    payloads = [
        db._captured_dependency_digest("W", value, set(), owner=owner),
        db._module_attribute_payload(value, set(), owner=owner, capture_name="module.W"),
        member[1],
    ]
    if isinstance(value, FunctionType):
        payloads.append(db._function_definition_payload(value, set()))
        policy = db._policy_definition_payload(value)
        factory = db._dataclass_default_factory_payload(value)
        assert policy[0] == factory[0] == "function"
        payloads.extend((policy[1], factory[1]))
    return payloads


@pytest.mark.parametrize("label", sorted(_GUARDED))
def test_a_guard_wrapper_folds_one_payload_naming_the_original(label: str) -> None:
    """Every route folds the same payload, and it names the original callable.

    The routes agree, as they do for a builtin, so `import m; m.getcwd` and
    `from m import getcwd` share an identity. The payload holds nothing of
    pyinc's own. The cross-process cell checks that it carries no id or address.
    """
    db = Database()
    wrapper = _live_wrappers()[label]
    payloads = _payload_routes(db, wrapper)
    assert all(payload == payloads[0] for payload in payloads)

    function = wrapper.__func__ if isinstance(wrapper, MethodType) else wrapper
    entry = _guarded_name(function)
    assert entry is not None
    guarded = db._guarded_name_payload(function)
    flattened = repr(guarded)
    assert entry.module in flattened
    assert entry.qualname in flattened
    assert "pyinc" not in flattened
    if isinstance(wrapper, MethodType):
        assert payloads[0][0] == "bound-guarded-standard-name"
        assert payloads[0][1] == guarded
    else:
        assert payloads[0] == guarded


def test_only_a_captured_pyinc_object_folds_a_file_of_pyincs(
    module_factory: Callable[[str], ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contract's account of which identities move with pyinc's own code.

    An annotation evaluated to `Database` folds `pyinc.runtime`. A captured
    resource folds the module its type is defined in and the modules its code
    reaches. Both fold those files' bytes, as any captured module is folded. A
    captured guard wrapper folds no module of pyinc's, so it moves with pyinc's
    code as little as the same call spelled through `os` does.
    """
    module = module_factory(
        "import os\nfrom os import getcwd\n\nfrom pyinc import Database, FileResource, query\n\n"
        "files = FileResource()\n\n\n"
        "@query(key='kernel-identity:annotated')\n"
        "def annotated(db: Database, x: int) -> int:\n    return x\n\n\n"
        "@query(key='kernel-identity:resource')\n"
        "def resource(db, x):\n    return files.read(db, x)\n\n\n"
        "@query(key='kernel-identity:guarded')\n"
        "def guarded(db, x):\n    return getcwd is not None and x\n\n\n"
        "@query(key='kernel-identity:through-os')\n"
        "def through_os(db, x):\n    return os.getcwd() if x else os.path.realpath('/')\n"
    )
    folded: list[str] = []
    identity = Database._module_identity_payload

    def recording(self: Database, value: ModuleType) -> Any:
        folded.append(value.__name__)
        return identity(self, value)

    monkeypatch.setattr(Database, "_module_identity_payload", recording)

    def pyinc_modules_folded(query: Any) -> set[str]:
        db = Database()
        folded.clear()
        db._query_fingerprint(query)
        return {name for name in folded if name.partition(".")[0] == "pyinc"}

    assert pyinc_modules_folded(module.annotated) == {"pyinc.runtime"}
    assert "pyinc.resources" in pyinc_modules_folded(module.resource)
    assert pyinc_modules_folded(module.guarded) == set()
    assert pyinc_modules_folded(module.through_os) == set()


def _live_wrappers() -> dict[str, Any]:
    """Each guarded callable where the guard installed it, as a capture reads it."""
    Database()
    live: dict[str, Any] = {
        "builtins.open": builtins.open,
        "io.open": io.open,
        "os.getenv": os.getenv,
        "os.listdir": os.listdir,
        "os.scandir": os.scandir,
        "os.getcwd": os.getcwd,
        "os.getcwdb": os.getcwdb,
        "os.path.realpath": os.path.realpath,
        "os.path.abspath": os.path.abspath,
        "Path.iterdir": Path.iterdir,
        # A classmethod: reading it binds the wrapper to the class.
        "Path.cwd": Path.cwd,
        "Thread.start": threading.Thread.start,
    }
    if sys.platform != "win32":
        live["os.getenvb"] = os.getenvb
    return live


def test_different_originals_fold_differently_and_one_original_folds_once() -> None:
    """The payload separates what the originals separate, and only that.

    `builtins.open` and `io.open` are two wrappers around one function, so
    they fold alike. `getcwd` and `getcwdb` are two functions. An original is
    never taken for its wrapper.
    """
    db = Database()
    digest = fingerprint_snapshot
    assert digest(db._guarded_name_payload(builtins.open)) == digest(
        db._guarded_name_payload(io.open)
    )
    assert digest(db._guarded_name_payload(os.getcwd)) != digest(
        db._guarded_name_payload(os.getcwdb)
    )
    for entry in _GUARDED_NAMES.values():
        assert db._guarded_name_payload(entry.original) is None


def test_a_bound_guard_wrapper_folds_the_class_it_is_bound_to() -> None:
    """`Path.cwd` binds the wrapper to the class it was read from, and that class is folded.

    A standard-library class is pinned by its module and name, and a subclass
    the caller defines by its body, as an implementation dependency is.
    """
    db = Database()

    class LocalPath(type(Path())):  # type: ignore[misc]
        pass

    owner: Any = _owner
    standard = db._captured_dependency_digest("W", Path.cwd, set(), owner=owner)
    local = db._captured_dependency_digest("W", LocalPath.cwd, set(), owner=owner)
    assert standard[1] == local[1]
    assert standard[2] != local[2]


@pytest.mark.parametrize("label", sorted(_GUARDED))
def test_the_capture_preview_agrees_with_the_kernel(
    module_factory: Callable[[str], ModuleType], sample_directory: Path, label: str
) -> None:
    """`explain_query_captures` accepts a captured wrapper, as the kernel now does, as `guarded`."""
    binding, call, _spelled = _GUARDED[label]
    module = module_factory(
        _PRELUDE
        + binding
        + f"\n\nKEY = {f'guarded-preview:{label}'!r}\n\n\n"
        + _ROUTES["default"].format(call=call.format(f="f"), binding_module="")
    )
    by_name = {info.name: info for info in explain_query_captures(module.q)}
    assert (by_name["default[0]"].accepted, by_name["default[0]"].kind) == (True, "guarded")
    # The kernel agrees. The query answers or the guard refuses it, and it
    # never raises `UnsupportedValueError`.
    _outcome(module.q, str(sample_directory))


@pytest.mark.parametrize(("label", "route"), _CAPTURES)
def test_the_capture_preview_accepts_a_wrapper_on_every_route_the_kernel_does(
    module_factory: Callable[[str], ModuleType],
    sample_directory: Path,
    label: str,
    route: str,
) -> None:
    """The preview reports every capture of a query accepted when the kernel fingerprints it.

    The preview used to fold a container member with a stricter walk than the
    kernel's, and a helper without the kernel's fallback to its source. So a
    wrapper held in a tuple, or returned by a helper that reads a mutable
    global, was reported refused while the kernel fingerprinted the query.
    """
    binding, call, _spelled = _GUARDED[label]
    binding_module = module_factory("from pyinc import query\n" + binding + "\n")
    module = module_factory(
        _PRELUDE
        + binding
        + "\n\nKEY = "
        + repr(f"guarded-preview:{label}:{route}")
        + "\n\n\n"
        + _ROUTES[route].format(
            call=call.format(f=_ROUTE_HANDLES[route]), binding_module=binding_module.__name__
        )
    )
    refused = [
        (info.name, info.rejection_reason)
        for info in explain_query_captures(module.q)
        if not info.accepted
    ]
    assert refused == []
    # The kernel agrees. The query answers or the guard refuses it, and it
    # never raises `UnsupportedValueError`.
    _outcome(module.q, str(sample_directory))


def test_a_standard_library_function_that_calls_a_wrapper_by_name_fingerprints(
    module_factory: Callable[[str], ModuleType], sample_directory: Path
) -> None:
    """`relpath` and `ismount` reach the guarded `abspath` and `realpath` through their module.

    Captured, each is folded as a function, and its globals hold the wrappers.
    Both the preview and the kernel accept them.
    """
    module = module_factory(
        "from os.path import relpath, ismount\nfrom pyinc import query\n\n\n"
        "@query(key='guarded-preview:stdlib-callers')\n"
        "def q(db, path, start):\n    return relpath(path, start), ismount(path)\n"
    )
    by_name = {info.name: info for info in explain_query_captures(module.q)}
    assert (by_name["relpath"].accepted, by_name["relpath"].kind) == (True, "function")
    assert (by_name["ismount"].accepted, by_name["ismount"].kind) == (True, "function")
    sample = str(sample_directory / "sample.txt")
    assert Database().get(module.q, sample, str(sample_directory)) == ("sample.txt", False)


def test_a_wrapper_capture_reuses_its_identity_and_warms_from_a_checkpoint(
    module_factory: Callable[[str], ModuleType], sample_directory: Path
) -> None:
    """The memo reuses a stored identity, and a checkpoint warms another database.

    The memo observes a wrapper as a leaf, as the payload folds it. The warm
    path's walk of pinned captures stops at it.
    """
    module = module_factory(
        "from os.path import realpath\nfrom pyinc import query\n\n\n"
        "@query(key='guarded-capture:warm')\n"
        "def q(db, path):\n    return realpath(path)\n"
    )
    store = InMemoryArtifactStore()
    db = Database(store=store)
    answer = db.get(module.q, str(sample_directory))
    assert db.get(module.q, str(sample_directory)) == answer
    assert db.inspect(module.q, str(sample_directory)).last_decision == "reused"

    warm = Database(store=store)
    warm.load_checkpoint(db.save_checkpoint())
    assert warm.get(module.q, str(sample_directory)) == answer
    assert warm.inspect(module.q, str(sample_directory)).last_recompute == "reused"
    assert warm.statistics().query_executions == 0


def test_the_memo_sees_an_edit_to_the_class_a_bound_wrapper_is_read_off(
    module_factory: Callable[[str], ModuleType],
) -> None:
    """A bound wrapper reached as a module attribute is observed with the class it is bound to.

    `H.W`, where `W = LocalPath.cwd` on a class of the caller's own, folds
    that class's body beside the wrapper's pin. The memo reuses a stored
    identity only while the definitions behind a module attribute hold still,
    and the bound wrapper is among the landings it observes. So a class
    attribute written in place moves a warm database's identity as it moves a
    fresh one's, and the query runs again.
    """
    holder = module_factory(
        "from pathlib import Path\n\n\nclass LocalPath(type(Path())):\n    X = 1\n\n\n"
        "W = LocalPath.cwd\n"
    )
    module = module_factory(
        f"import {holder.__name__} as H\nfrom pyinc import query\n\n\n"
        "@query(key='guarded-capture:bound-memo')\n"
        "def q(db, x):\n    return H.W is not None and x\n"
    )
    db = Database()
    assert db.get(module.q, 1) == 1
    assert db.get(module.q, 1) == 1
    assert db.inspect(module.q, 1).last_decision == "reused"
    before = db._query_fingerprint(module.q)

    holder.LocalPath.X = 2
    fresh = Database()._query_fingerprint(module.q)
    assert fresh != before
    assert db._query_fingerprint(module.q) == fresh
    assert db.get(module.q, 1) == 1
    assert db.inspect(module.q, 1).last_decision != "reused"


_BEFORE_THE_FIRST_DATABASE = '''\
"""Bind guarded names before any Database exists, then fingerprint captures of them."""

import json
import os
import sys
import tempfile

directory = tempfile.mkdtemp()
sys.path.insert(0, directory)
with open(os.path.join(directory, "bound_early.py"), "w", encoding="utf-8") as handle:
    handle.write(
        "from os import getcwd, getenv\\n"
        "from pyinc import query\\n"
        "@query(key='early:getcwd')\\n"
        "def q_getcwd(db):\\n    return getcwd()\\n"
        "@query(key='early:getenv')\\n"
        "def q_getenv(db):\\n    return getenv('PYINC_GUARDED_NAME')\\n"
    )
import bound_early

from pyinc import Database, UnsupportedValueError, UntrackedReadError
from pyinc.runtime import _guarded_name

db = Database()
out = {}
for name in ("getcwd", "getenv"):
    query = getattr(bound_early, "q_" + name)
    captured = vars(bound_early)[name]
    try:
        db.get(query)
        out[name] = ["answered", _guarded_name(captured) is not None]
    except UnsupportedValueError:
        out[name] = ["unsupported", _guarded_name(captured) is not None]
    except UntrackedReadError:
        out[name] = ["refused", _guarded_name(captured) is not None]
out["getcwd_payload"] = db._captured_dependency_digest(
    "getcwd", bound_early.getcwd, set(), owner=bound_early.q_getcwd.fn
)[0]
print("JSON " + json.dumps(out))
'''


def test_a_name_bound_before_the_first_database_keeps_the_unguarded_original(
    tmp_path: Path,
) -> None:
    """The documented limitation still holds: a name bound early is the original, fingerprinted as before.

    `from os import getcwd` before any `Database` keeps the builtin. It reads
    the working directory unrefused and fingerprints as a builtin. `os.getenv`
    stays refused at fingerprinting, because its global `environ` is the
    guard's mapping, which the kernel does not recognise. Neither is a
    registered wrapper. The test runs in a fresh process, the only place where
    no `Database` exists yet.
    """
    script = tmp_path / "bound_early_fixture.py"
    script.write_text(_BEFORE_THE_FIRST_DATABASE, encoding="utf-8")
    src = str(Path(pyinc.__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": src, "PYTHONDONTWRITEBYTECODE": "1"}
    env["PYINC_GUARDED_NAME"] = "value"
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, env=env, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("JSON ")][-1]
    assert json.loads(line[len("JSON ") :]) == {
        "getcwd": ["answered", False],
        "getenv": ["unsupported", False],
        "getcwd_payload": "builtin",
    }


_REPLACED_BEFORE_THE_FIRST_DATABASE = '''\
"""Put something else in guarded names' places, then create the first two Databases."""

import functools
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

directory = tempfile.mkdtemp()
with open(os.path.join(directory, "sample.txt"), "w", encoding="utf-8") as handle:
    handle.write("hello")
os.environ["PYINC_GUARDED_NAME"] = "value"
real_getenv, real_listdir, real_cwd = os.getenv, os.listdir, Path.cwd
real_realpath = os.path.realpath
# The standard library's own name for realpath's parameter.
keyword = "path" if os.name == "nt" else "filename"


def own_getcwd():
    return directory


class Opaque:
    """A callable with no signature to read, as a C function may have none."""

    def __init__(self, function):
        self.function = function

    def __call__(self, *args, **kwargs):
        return self.function(*args, **kwargs)

    @property
    def __signature__(self):
        raise ValueError("no signature")


# Each is what a test suite or a tool might hold in a guarded name's place: a
# mock has no __qualname__, a partial has neither name, a function of the
# caller's own is no standard-library callable, Path.cwd patched on the class
# is no classmethod, and `Opaque` has no signature to read a keyword name from.
replacements = {
    "os.getenv": mock.MagicMock(side_effect=real_getenv),
    "os.listdir": functools.partial(real_listdir),
    "os.getcwd": own_getcwd,
    "Path.cwd": mock.MagicMock(side_effect=real_cwd),
    "os.path.realpath": Opaque(real_realpath),
}
os.getenv = replacements["os.getenv"]
os.listdir = replacements["os.listdir"]
os.getcwd = replacements["os.getcwd"]
Path.cwd = replacements["Path.cwd"]
os.path.realpath = replacements["os.path.realpath"]
sys.path.insert(0, directory)

from pyinc import Database, UnsupportedValueError, UntrackedReadError, query
from pyinc import runtime

out = {}
try:
    Database()
    out["first"] = "constructed"
except Exception as exc:
    out["first"] = type(exc).__name__ + ": " + str(exc)
out["installed"] = runtime._GUARD_INSTALLED
Database()

live = {
    "os.getenv": os.getenv,
    "os.listdir": os.listdir,
    "os.getcwd": os.getcwd,
    "Path.cwd": vars(Path)["cwd"].__func__,
    "os.path.realpath": os.path.realpath,
}


def closed_over(function):
    return [cell.cell_contents for cell in function.__closure__ or ()]


# Wrapped once, around the replacement itself, and never recorded as a
# standard-library callable; the names nobody replaced still are.
out["wrapped_once"] = {
    label: function.__code__.co_filename == runtime.__file__
    and any(item is replacements[label] for item in closed_over(function))
    and not any(
        getattr(getattr(item, "__code__", None), "co_filename", None) == runtime.__file__
        for item in closed_over(function)
    )
    for label, function in live.items()
}
out["recorded"] = {label: runtime._guarded_name(function) is not None for label, function in live.items()}
out["recorded_untouched"] = [
    runtime._guarded_name(os.scandir) is not None,
    runtime._guarded_name(os.path.abspath) is not None,
    runtime._guarded_name(vars(Path)["iterdir"]) is not None,
]
out["registry_size"] = len(runtime._GUARDED_NAMES)


@query(key="replaced:calls")
def calls(db, name):
    try:
        if name == "os.getenv":
            os.getenv("PYINC_GUARDED_NAME")
        elif name == "os.listdir":
            os.listdir(directory)
        elif name == "os.getcwd":
            os.getcwd()
        elif name == "Path.cwd":
            Path.cwd()
        else:
            os.path.realpath(**{keyword: "relative"})
    except UntrackedReadError:
        return "refused"
    return "answered"


db = Database()
out["inside"] = {label: db.get(calls, label) for label in live}
out["outside"] = [
    os.getenv("PYINC_GUARDED_NAME"),
    os.listdir(directory),
    os.getcwd() == directory,
    Path.cwd() == real_cwd(),
    os.path.realpath(**{keyword: "relative"}) == real_realpath("relative"),
]

with open(os.path.join(directory, "captures_replaced.py"), "w", encoding="utf-8") as handle:
    handle.write(
        "from os import getenv\\n"
        "from pyinc import query\\n"
        "@query(key='replaced:capture')\\n"
        "def q(db):\\n    return getenv is not None\\n"
    )
import captures_replaced

try:
    db.get(captures_replaced.q)
    out["capture"] = "fingerprinted"
except UnsupportedValueError:
    out["capture"] = "refused"
print("JSON " + json.dumps(out))
'''


def test_the_guard_installs_whole_around_whatever_holds_a_guarded_name(tmp_path: Path) -> None:
    """A mock, a partial or a caller's function in a guarded name's place is wrapped once and left unpinned.

    Before, recording the replaced callable read its `__qualname__` after every
    wrapper was in place and before the guard was marked installed. A
    replacement without one failed the first `Database` with `AttributeError`,
    and the next one wrapped every name a second time. `Path.cwd` replaced by
    anything but a classmethod failed it on its missing `__func__`, and a
    `realpath` without a signature failed it on its parameters. Now the guard
    installs whole around what it finds and still refuses the call inside a
    query. It records a wrapper only around the standard-library callable named
    by its module and qualified name, so a capture of a wrapper around anything
    else is refused as before. The test runs in a fresh process, the only place
    where no `Database` exists yet.
    """
    script = tmp_path / "replaced_before_first_database.py"
    script.write_text(_REPLACED_BEFORE_THE_FIRST_DATABASE, encoding="utf-8")
    src = str(Path(pyinc.__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": src, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, env=env, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("JSON ")][-1]
    out = json.loads(line[len("JSON ") :])
    labels = ["os.getenv", "os.listdir", "os.getcwd", "Path.cwd", "os.path.realpath"]
    assert out["first"] == "constructed"
    assert out["installed"] is True
    assert out["wrapped_once"] == dict.fromkeys(labels, True)
    assert out["recorded"] == dict.fromkeys(labels, False)
    assert out["recorded_untouched"] == [True, True, True]
    assert out["registry_size"] == len(_GUARDED) - len(labels)
    assert out["inside"] == dict.fromkeys(labels, "refused")
    assert out["outside"][0] == "value"
    assert out["outside"][1] == ["sample.txt"]
    # The keyword is the standard library's name for the parameter.
    assert out["outside"][2:] == [True, True, True]
    assert out["capture"] == "refused"


_PATCHED_WHILE_THE_FIRST_DATABASE_IS_CREATED = '''\
"""Patch every guarded name, create the first Database, end the patches, then query."""

import builtins
import contextlib
import inspect
import io
import json
import os
import pickle
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

root = tempfile.mkdtemp()
directory = os.path.join(root, "data")
os.mkdir(directory)
with open(os.path.join(directory, "sample.txt"), "w", encoding="utf-8") as handle:
    handle.write("hello")
os.environ["PYINC_GUARDED_NAME"] = "value"

owners = {
    "builtins.open": (builtins, "open"),
    "io.open": (io, "open"),
    "os.getenv": (os, "getenv"),
    "os.listdir": (os, "listdir"),
    "os.scandir": (os, "scandir"),
    "os.environ": (os, "environ"),
    "os.getcwd": (os, "getcwd"),
    "os.getcwdb": (os, "getcwdb"),
    "os.path.realpath": (os.path, "realpath"),
    "os.path.abspath": (os.path, "abspath"),
    "Path.iterdir": (Path, "iterdir"),
    "Path.cwd": (Path, "cwd"),
    "Thread.start": (threading.Thread, "start"),
}
if sys.platform != "win32":
    owners["os.environb"] = (os, "environb")
    owners["os.getenvb"] = (os, "getenvb")
originals = {label: inspect.getattr_static(*place) for label, place in owners.items()}


def stand_in(label):
    """What a test might patch in: something that answers as the original does."""
    original = originals[label]
    if label in ("os.environ", "os.environb"):
        return type(original)(
            original._data, original.encodekey, original.decodekey,
            original.encodevalue, original.decodevalue,
        )
    if label == "Path.cwd":
        return classmethod(lambda cls: original.__func__(cls))
    if label in ("Path.iterdir", "Thread.start"):
        return lambda self, *args, **kwargs: original(self, *args, **kwargs)
    return mock.MagicMock(side_effect=original)


PUT_BACK_CALLS = """\\
import builtins
import io
import os
import threading
from pathlib import Path

from pyinc import UntrackedReadError, query

directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


# Reads through the guarded name `label`.
def call(label):
    if label in ("builtins.open", "io.open"):
        opener = builtins.open if label == "builtins.open" else io.open
        with opener(os.path.join(directory, "sample.txt"), encoding="utf-8") as handle:
            return handle.read()
    if label == "os.getenv":
        return os.getenv("PYINC_GUARDED_NAME")
    if label == "os.getenvb":
        return os.getenvb(b"PYINC_GUARDED_NAME").decode()
    if label == "os.environ":
        return os.environ["PYINC_GUARDED_NAME"]
    if label == "os.environb":
        return os.environb[b"PYINC_GUARDED_NAME"].decode()
    if label == "os.listdir":
        return os.listdir(directory)
    if label == "os.scandir":
        return sorted(entry.name for entry in os.scandir(directory))
    if label == "os.getcwd":
        return os.getcwd()
    if label == "os.getcwdb":
        return os.fsdecode(os.getcwdb())
    if label == "os.path.realpath":
        return os.path.realpath("relative")
    if label == "os.path.abspath":
        return os.path.abspath("relative")
    if label == "Path.iterdir":
        return sorted(child.name for child in Path(directory).iterdir())
    if label == "Path.cwd":
        return str(Path.cwd())
    # A thread started inside a query runs in the query's context, so its read
    # is refused there too.
    seen = []

    def read():
        try:
            os.getenv("PYINC_GUARDED_NAME")
            seen.append("answered")
        except UntrackedReadError:
            seen.append("refused")

    thread = threading.Thread(target=read)
    thread.start()
    thread.join()
    if seen == ["refused"]:
        raise UntrackedReadError("refused in the thread")
    return seen


@query(key="put-back:calls")
def calls(db, label):
    try:
        call(label)
    except UntrackedReadError:
        return "refused"
    return "answered"


"""


out = {}
with contextlib.ExitStack() as patches:
    for label, (owner, attribute) in owners.items():
        patches.enter_context(mock.patch.object(owner, attribute, stand_in(label)))
    from pyinc import Database, UntrackedReadError, query
    from pyinc import runtime

    Database()
out["put_back"] = {
    label: inspect.getattr_static(*place) is originals[label] for label, place in owners.items()
}


# The helper and the query live in a module of their own: the kernel pins a
# function that reads `os.environ` by its module's source, and `__main__` has
# none it can pin.
with open(os.path.join(root, "put_back_calls.py"), "w", encoding="utf-8") as handle:
    handle.write(PUT_BACK_CALLS)
sys.path.insert(0, root)
from put_back_calls import call, calls

db = Database()
out["inside"] = {label: db.get(calls, label) for label in owners}
out["outside"] = {label: call(label) for label in owners}
cwd = originals["os.getcwd"]()
out["expected"] = [cwd, originals["os.path.realpath"]("relative"), originals["os.path.abspath"]("relative")]
live = {label: inspect.getattr_static(*place) for label, place in owners.items()}
out["guarded"] = {
    label: value is not originals[label]
    and (
        isinstance(value, runtime._GuardedEnviron)
        if label in ("os.environ", "os.environb")
        else runtime._guarded_name(runtime._guarded_callable(value)) is not None
    )
    for label, value in live.items()
}
out["pickles"] = {
    label: pickle.loads(pickle.dumps(getattr(*owners[label]))) is getattr(*owners[label])
    for label in owners
    if label not in ("os.environ", "os.environb", "Path.cwd")
}

# A capture of a name wrapped again fingerprints as the standard-library callable.
with open(os.path.join(root, "captures_put_back.py"), "w", encoding="utf-8") as handle:
    handle.write(
        "from os import getcwd\\n"
        "from pyinc import query\\n"
        "@query(key='put-back:capture')\\n"
        "def q(db):\\n    return getcwd is not None\\n"
    )
import captures_put_back

out["capture"] = db.get(captures_put_back.q)

# A fake set in a guarded name's place keeps it. The original put back is
# wrapped again. Each check runs on a new Database, so the query executes.
fake = lambda key, default=None: "fake"  # noqa: E731
os.getenv = fake
out["fake"] = [Database().get(calls, "os.getenv"), os.getenv is fake]
os.getenv = originals["os.getenv"]
out["original_again"] = Database().get(calls, "os.getenv")
print("JSON " + json.dumps(out))
'''


def test_a_guarded_name_put_back_after_the_first_database_is_guarded_again(
    tmp_path: Path,
) -> None:
    """A patch active while the first `Database` is created leaves no name unguarded.

    The guard wraps whatever holds each name when the first `Database` is
    created. A `mock.patch` active then saved the standard-library callable,
    so ending it put the original back unwrapped, and that name answered
    inside every query for the rest of the process. Each query execution now
    wraps such a name again before the body runs. The new wrapper is recorded,
    pickles by reference, and a capture of it fingerprints. A fake set in a
    guarded name's place keeps it. A fresh process is the only place no
    `Database` exists yet.
    """
    script = tmp_path / "patched_while_first_database_is_created.py"
    script.write_text(_PATCHED_WHILE_THE_FIRST_DATABASE_IS_CREATED, encoding="utf-8")
    src = str(Path(pyinc.__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": src, "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, env=env, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("JSON ")][-1]
    out = json.loads(line[len("JSON ") :])
    labels = list(out["put_back"])
    expected = 15 if sys.platform != "win32" else 13
    assert len(labels) == expected
    assert out["put_back"] == dict.fromkeys(labels, True)
    assert out["inside"] == dict.fromkeys(labels, "refused")
    outside = out["outside"]
    assert [outside["builtins.open"], outside["io.open"]] == ["hello", "hello"]
    assert [outside[label] for label in labels if "env" in label] == ["value"] * sum(
        "env" in label for label in labels
    )
    assert outside["os.listdir"] == ["sample.txt"]
    assert outside["os.scandir"] == ["sample.txt"]
    assert outside["Path.iterdir"] == ["sample.txt"]
    cwd, realpath, abspath = out["expected"]
    assert [outside["os.getcwd"], outside["os.getcwdb"], outside["Path.cwd"]] == [cwd] * 3
    assert [outside["os.path.realpath"], outside["os.path.abspath"]] == [realpath, abspath]
    assert outside["Thread.start"] == ["answered"]
    assert out["guarded"] == dict.fromkeys(labels, True)
    assert out["pickles"] == dict.fromkeys(
        [label for label in labels if label not in ("os.environ", "os.environb", "Path.cwd")],
        True,
    )
    assert out["capture"] is True
    assert out["fake"] == ["answered", True]
    assert out["original_again"] == "refused"
