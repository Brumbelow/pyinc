"""The shipped queries that hand back raw text under a coarser comparison.

A ``cutoff=`` token is sound only when it determines the value the query
returns. Take a query that returns a file's text and compares by a projection
of that text. It can report "nothing changed" while handing back different
bytes, because the fresh snapshot is stored before the comparison decides.
The set below is empty because no shipped query has that shape today. This
file makes a reintroduction visible.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCANNED_ROOTS = ("src", "examples")

#: Every shipped query that returns raw `str` and declares a `cutoff=` token
#: that does not determine that text. A token coarser than the value it guards
#: lets a recomputation report "nothing changed" while handing back different
#: bytes, so this set is empty. Remove an entry in the commit that removes the
#: token. An addition needs a reason this file can state.
_STR_QUERIES_WITH_A_CUTOFF: frozenset[tuple[str, str]] = frozenset()

_PREDICATE_FIXTURE = '''
from __future__ import annotations


def _token(text: str) -> tuple[str, str]:
    return ("t", text)


@query(cutoff=_token)
def carries_a_cutoff(db: Database, path: str) -> str:
    return ""


@query
def carries_none(db: Database, path: str) -> str:
    return ""


@query(cutoff=None)
def declares_no_policy(db: Database, path: str) -> str:
    return ""
'''


def _module_files() -> tuple[Path, ...]:
    # Located relative to this file, because pytest's rootdir and the process
    # cwd can differ between invocations. `tests/` is out of scope on purpose.
    # Its decorated cutoff sites are fixtures, and it is the one tree where two
    # same-named decorated functions share a file.
    files: list[Path] = []
    for name in _SCANNED_ROOTS:
        files.extend(sorted((_ROOT / name).rglob("*.py")))
    return tuple(files)


def _decorator_name(decorator: ast.expr) -> str | None:
    callee = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(callee, ast.Attribute):
        return callee.attr
    if isinstance(callee, ast.Name):
        return callee.id
    return None


def _names_query(decorator: ast.expr) -> bool:
    # Both spellings: the bare `@query` and the `@query(...)` call.
    return _decorator_name(decorator) == "query"


def _cutoff_argument(decorator: ast.expr) -> ast.expr | None:
    if not isinstance(decorator, ast.Call) or not _names_query(decorator):
        return None
    for keyword in decorator.keywords:
        if keyword.arg == "cutoff":
            return keyword.value
    return None


def _is_a_cutoff(value: ast.expr | None) -> bool:
    # An explicit `cutoff=None` declares no policy, so it counts as no cutoff.
    return value is not None and not (isinstance(value, ast.Constant) and value.value is None)


def _returns_str(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    # Read the annotation as a node. Every file carrying one of these queries
    # also has `from __future__ import annotations`, so its runtime annotation
    # is the string `'str'` and would never equal the type. Some files the walk
    # visits lack that import, and the node reads the same either way. The scan
    # also stays independent of import order and of whether a module imports
    # cleanly.
    return node.returns is not None and ast.unparse(node.returns) == "str"


def _classify(module: ast.Module) -> tuple[frozenset[str], frozenset[str]]:
    """Split a module's `str`-returning queries by whether they declare a cutoff.

    Underscore-private names are included. Two entries in the set above are
    private, so the scan applies no name filter.
    """
    with_cutoff: set[str] = set()
    without: set[str] = set()
    for node in ast.walk(module):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _returns_str(node):
            continue
        if not any(_names_query(decorator) for decorator in node.decorator_list):
            continue
        carries = any(
            _is_a_cutoff(_cutoff_argument(decorator)) for decorator in node.decorator_list
        )
        (with_cutoff if carries else without).add(node.name)
    return frozenset(with_cutoff), frozenset(without)


def _walk(want_cutoff: bool | None) -> frozenset[tuple[str, str]]:
    # The recorded identity is the repo-relative path and the function name. A
    # line-number literal would go red on any unrelated edit to these files and
    # teach readers to update it blindly.
    #
    # `want_cutoff=None` means both classes, unioned inside this loop. A second
    # walk would re-read and re-parse every file under the scanned roots for an
    # answer this pass already holds.
    found: set[tuple[str, str]] = set()
    for path in _module_files():
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        with_cutoff, without = _classify(module)
        if want_cutoff is None:
            names: frozenset[str] = with_cutoff | without
        else:
            names = with_cutoff if want_cutoff else without
        relative = path.relative_to(_ROOT).as_posix()
        found.update((relative, name) for name in names)
    return frozenset(found)


def _str_queries_with_a_cutoff() -> frozenset[tuple[str, str]]:
    return _walk(want_cutoff=True)


def _str_returning_queries() -> frozenset[tuple[str, str]]:
    return _walk(want_cutoff=None)


def test_the_cutoff_inventory_is_exact() -> None:
    found = _str_queries_with_a_cutoff()
    # Single line, difference first. The suite runs with `--tb=no`, which shows
    # one truncated line, and a long node id can fill it. That is also why this
    # cell's name is short. A longer name costs message space.
    assert found == _STR_QUERIES_WITH_A_CUTOFF, (
        f"appeared: {sorted(found - _STR_QUERIES_WITH_A_CUTOFF)} | "
        f"gone: {sorted(_STR_QUERIES_WITH_A_CUTOFF - found)} | "
        "remove an entry in the commit that removes its token; "
        "an addition needs a reason"
    )


def test_the_scan_reaches_the_raw_text_queries_it_is_scoped_over() -> None:
    # If the decorator predicate stops matching, the inventory above compares
    # empty to empty and passes vacuously. This names a query the scan must see
    # either way. It returns raw text and has never carried a token, so it
    # belongs to the denominator in every revision of the set.
    assert (
        "src/pyinc/integrations/installed_packages.py",
        "_top_level_text",
    ) in _str_returning_queries()


def test_the_predicate_separates_the_two_decorator_spellings() -> None:
    # Independent of the tree, so it still catches a broken predicate when the
    # literal above is empty. A predicate that stopped recognizing the call form
    # would empty the inventory and pass the tree-based guard vacuously.
    #
    # `declares_no_policy` is a third case, written in the call form like
    # `carries_a_cutoff`. It must be classified with the bare form, because
    # `cutoff=None` declares no policy. The scanned tree has no such site, so
    # this fixture is the only test of that clause of the predicate.
    with_cutoff, without = _classify(ast.parse(_PREDICATE_FIXTURE))
    assert with_cutoff == {"carries_a_cutoff"}
    assert without == {"carries_none", "declares_no_policy"}
