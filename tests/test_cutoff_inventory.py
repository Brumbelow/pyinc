"""The shipped queries that hand back raw text under a coarser comparison.

A ``cutoff=`` token is sound only when it determines the value the query
returns. A query that returns a file's text and compares by a projection of
that text can report "nothing changed" while handing back different bytes,
because the fresh snapshot is stored before the comparison decides. The set
below is what is left of that shape, and it is being emptied; this file is
how a reintroduction becomes visible.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCANNED_ROOTS = ("src", "examples")

#: Every shipped query that returns raw `str` while declaring a `cutoff=`
#: token that does not determine that text. A token coarser than the value it
#: guards lets a recomputation report "nothing changed" while handing back
#: different bytes, so this set is being emptied. Removing an entry belongs in
#: the commit that removes the token; adding one needs a reason this file can
#: state.
_STR_QUERIES_WITH_A_CUTOFF: frozenset[tuple[str, str]] = frozenset(
    {
        ("examples/calc/engine.py", "calc_source"),
        ("examples/correctness_demo.py", "read_source"),
        ("src/pyinc/integrations/csv_data.py", "csv_file_text"),
        ("src/pyinc/integrations/deep_module_resolution.py", "_pth_file_text"),
        ("src/pyinc/integrations/env_file.py", "env_file_text"),
        ("src/pyinc/integrations/installed_packages.py", "_metadata_text"),
        ("src/pyinc/integrations/json_config.py", "json_file_text"),
        ("src/pyinc/integrations/requirements_txt.py", "requirements_file_text"),
        ("src/pyinc/integrations/xml_config.py", "xml_file_text"),
        ("src/pyinc_codegen/schema.py", "schema_text"),
    }
)

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
    # Located from this file rather than from the working directory: pytest's
    # rootdir and the process cwd are not the same thing under every
    # invocation. `tests/` is out of scope on purpose — its decorated cutoff
    # sites are fixtures rather than shipped code, and it is the one tree where
    # two same-named decorated functions share a file.
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
    # An explicit `cutoff=None` declares no policy and is not a cutoff site.
    return value is not None and not (isinstance(value, ast.Constant) and value.value is None)


def _returns_str(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    # The annotation is read as a node rather than resolved at runtime. Every
    # file carrying one of these queries also carries `from __future__ import
    # annotations`, so its runtime annotation is the string `'str'` and a
    # comparison against the type would never match; the files the walk visits
    # do not all carry that import, and reading the node answers the same way
    # either way. It also keeps the scan independent of import order and of
    # whether a module imports cleanly at all.
    return node.returns is not None and ast.unparse(node.returns) == "str"


def _classify(module: ast.Module) -> tuple[frozenset[str], frozenset[str]]:
    """Split a module's `str`-returning queries by whether they declare a cutoff.

    Underscore-private names are included: two entries in the set above are
    private, so no name filter of any kind is applied.
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
    # The recorded identity is the repo-relative path and the function name,
    # never a line number: a line-number literal would go red on any unrelated
    # edit to these files and would teach a reader to update it blindly.
    #
    # `want_cutoff=None` means both classes, unioned inside this loop rather
    # than by calling the walk twice: a second call would re-read and re-parse
    # every file under the scanned roots to produce an answer this pass already
    # holds.
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
    # Single line, difference first: the suite runs with `--tb=no`, which shows
    # one truncated line, and a long node id can eat all of it. That is also why
    # this cell's name is short — lengthening it costs message, not clarity.
    assert found == _STR_QUERIES_WITH_A_CUTOFF, (
        f"appeared: {sorted(found - _STR_QUERIES_WITH_A_CUTOFF)} | "
        f"gone: {sorted(_STR_QUERIES_WITH_A_CUTOFF - found)} | "
        "remove an entry in the commit that removes its token; "
        "an addition needs a reason"
    )


def test_the_scan_reaches_the_raw_text_queries_it_is_scoped_over() -> None:
    # If the decorator predicate stops matching, the inventory above compares
    # empty to empty and passes while saying nothing. This names a query the
    # scan must see either way: it returns raw text and has never carried a
    # token, so it belongs to the denominator in every revision of the set.
    assert (
        "src/pyinc/integrations/installed_packages.py",
        "_top_level_text",
    ) in _str_returning_queries()


def test_the_predicate_separates_the_two_decorator_spellings() -> None:
    # Tree-independent, so it still has teeth when the literal above is empty:
    # a predicate that stops recognizing the call form would empty the
    # inventory and pass the tree-based guard while saying nothing.
    #
    # `declares_no_policy` is the third case rather than a third spelling: it is
    # written in the call form, like `carries_a_cutoff`, and must still be
    # classified with the bare form, because naming `cutoff=None` declares no
    # policy at all. No such site exists in the scanned tree, so this fixture is
    # the only thing holding that clause of the predicate down.
    with_cutoff, without = _classify(ast.parse(_PREDICATE_FIXTURE))
    assert with_cutoff == {"carries_a_cutoff"}
    assert without == {"carries_none", "declares_no_policy"}
