from __future__ import annotations

from pathlib import Path

import pytest

from pyinc import Database
from pyinc.integrations import (
    SourcePosition,
    find_references,
    scope_tree,
    symbol_at,
)
from pyinc.integrations.python_source import file_analysis, source_text


def test_python_source_uses_pep263_and_codepoint_ranges(tmp_path: Path) -> None:
    path = tmp_path / "latin1.py"
    path.write_bytes(
        b"# coding: latin-1\nlabel = 'caf\xe9'\ndef r\xe9sum\xe9():\n    return label\n"
    )
    db = Database()

    assert "café" in source_text(db, str(path))
    analysis = file_analysis(db, path)
    definition = analysis.definitions[0]
    assert definition.name == "résumé"
    assert definition.range.start == SourcePosition(2, 4)
    assert definition.range.end == SourcePosition(2, 10)


def test_python_source_ranges_handle_mixed_line_endings(tmp_path: Path) -> None:
    path = tmp_path / "mixed.py"
    path.write_bytes("é = 1\r\n\ndef value():\r    return é\r\n".encode())
    db = Database()

    definition = file_analysis(db, path).definitions[0]
    declaration = symbol_at(db, path, SourcePosition(0, 0))
    use = symbol_at(db, path, SourcePosition(3, 11))

    assert definition.range.start == SourcePosition(2, 4)
    assert definition.range.end == SourcePosition(2, 9)
    assert declaration is not None
    assert use == declaration


def test_lexical_ranges_after_non_ascii_prefix_are_codepoint_based(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unicode_columns.py"
    source = "café = 1\nrésultat = café\n"
    path.write_text(source, encoding="utf-8")
    db = Database()

    declaration = symbol_at(db, path, SourcePosition(0, 1))
    use_start = source.splitlines()[1].index("café")
    use = symbol_at(db, path, SourcePosition(1, use_start))
    occurrence = next(
        item
        for item in scope_tree(db, path).occurrences
        if not item.is_declaration and item.name == "café"
    )

    assert declaration is not None
    assert use == declaration
    assert occurrence.range.start == SourcePosition(1, use_start)
    assert occurrence.range.end == SourcePosition(1, use_start + len("café"))


def test_symbol_identity_normalizes_relative_and_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "mod.py"
    path.write_text("value = 1\nprint(value)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    db = Database()

    relative = symbol_at(db, Path("mod.py"), SourcePosition(0, 0))
    absolute = symbol_at(db, path, SourcePosition(0, 0))

    assert relative is not None
    assert relative == absolute
    assert relative.path == str(path.resolve())


def test_scope_tree_distinguishes_shadowed_and_comprehension_bindings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mod.py"
    path.write_text(
        "value = 1\ndef use(value):\n    return [value for value in range(value)]\nprint(value)\n",
        encoding="utf-8",
    )
    db = Database()
    tree = scope_tree(db, path)

    module_value = symbol_at(db, path, SourcePosition(0, 0))
    parameter_value = symbol_at(db, path, SourcePosition(1, 9))
    comprehension_value = symbol_at(db, path, SourcePosition(2, 12))
    assert module_value is not None
    assert parameter_value is not None
    assert comprehension_value is not None
    assert len({module_value, parameter_value, comprehension_value}) == 3
    assert {scope.kind for scope in tree.scopes} >= {
        "module",
        "function",
        "comprehension",
    }

    references = find_references(db, tmp_path, module_value)
    assert [item.range.start.line for item in references.references] == [0, 3]


def test_scope_tree_respects_global_and_nonlocal(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text(
        "top = 0\n"
        "def outer():\n"
        "    enclosed = 1\n"
        "    def inner():\n"
        "        global top\n"
        "        nonlocal enclosed\n"
        "        top = enclosed\n",
        encoding="utf-8",
    )
    db = Database()

    top_declaration = symbol_at(db, path, SourcePosition(0, 0))
    top_assignment = symbol_at(db, path, SourcePosition(6, 8))
    enclosed_declaration = symbol_at(db, path, SourcePosition(2, 5))
    enclosed_use = symbol_at(db, path, SourcePosition(6, 14))
    assert top_declaration == top_assignment
    assert enclosed_declaration == enclosed_use


def test_directive_and_target_ranges_use_exact_identifier_tokens(tmp_path: Path) -> None:
    path = tmp_path / "ranges.py"
    source = (
        "foobar = 0\n"
        "foo = 0\n"
        "def directives():\n"
        "    global foobar, foo\n"
        "def outer():\n"
        "    élan = 1\n"
        "    é = 2\n"
        "    def inner():\n"
        "        nonlocal élan, é\n"
        "def handlers(value):\n"
        "    try:\n"
        "        pass\n"
        "    except Foobar as foo:\n"
        "        pass\n"
        "    match value:\n"
        '        case {"rest": foo, **rest}:\n'
        "            pass\n"
    )
    path.write_text(source, encoding="utf-8")
    tree = scope_tree(Database(), path)

    directive_uses = {
        (item.range.start.line, item.name): item.range
        for item in tree.occurrences
        if not item.is_declaration and item.range.start.line in {3, 8}
    }
    assert directive_uses[(3, "foobar")].start == SourcePosition(3, 11)
    assert directive_uses[(3, "foo")].start == SourcePosition(3, 19)
    assert directive_uses[(8, "élan")].start == SourcePosition(8, 17)
    assert directive_uses[(8, "é")].start == SourcePosition(8, 23)

    target_occurrences = [
        item
        for item in tree.occurrences
        if item.is_declaration and item.range.start.line in {12, 15}
    ]
    assert {(item.name, item.range.start.character) for item in target_occurrences} == {
        ("foo", 21),
        ("foo", 22),
        ("rest", 29),
    }
    lines = source.splitlines()
    for (line, name), source_range in directive_uses.items():
        assert lines[line][source_range.start.character : source_range.end.character] == name
    for item in target_occurrences:
        assert (
            lines[item.range.start.line][item.range.start.character : item.range.end.character]
            == item.name
        )


def test_invalid_nonlocal_never_falls_back_to_module_binding(tmp_path: Path) -> None:
    module_only = tmp_path / "module_only.py"
    module_only.write_text("value = 1\nnonlocal value\n", encoding="utf-8")
    missing = tmp_path / "missing.py"
    missing.write_text(
        "value = 1\ndef use():\n    nonlocal value\n    return value\n",
        encoding="utf-8",
    )
    db = Database()

    assert symbol_at(db, module_only, SourcePosition(1, 10)) is None
    module_value = symbol_at(db, missing, SourcePosition(0, 1))
    assert module_value is not None
    assert symbol_at(db, missing, SourcePosition(2, 13)) is None
    assert symbol_at(db, missing, SourcePosition(3, 11)) is None


def test_nonlocal_uses_nearest_enclosing_function_binding(tmp_path: Path) -> None:
    path = tmp_path / "nearest.py"
    path.write_text(
        "def outer():\n"
        "    value = 1\n"
        "    def middle():\n"
        "        value = 2\n"
        "        def inner():\n"
        "            nonlocal value\n"
        "            return value\n",
        encoding="utf-8",
    )
    db = Database()

    outer_value = symbol_at(db, path, SourcePosition(1, 5))
    middle_value = symbol_at(db, path, SourcePosition(3, 9))
    directive_value = symbol_at(db, path, SourcePosition(5, 22))
    inner_value = symbol_at(db, path, SourcePosition(6, 19))
    assert outer_value is not None
    assert middle_value is not None
    assert outer_value != middle_value
    assert directive_value == middle_value
    assert inner_value == middle_value


def test_class_body_lookup_uses_execution_order_not_final_locals(tmp_path: Path) -> None:
    path = tmp_path / "class_lookup.py"
    path.write_text(
        "value = 0\nclass Container:\n    before = value\n    value = value\n    after = value\n",
        encoding="utf-8",
    )
    db = Database()

    module_value = symbol_at(db, path, SourcePosition(0, 1))
    before = symbol_at(db, path, SourcePosition(2, 13))
    assignment_rhs = symbol_at(db, path, SourcePosition(3, 12))
    class_value = symbol_at(db, path, SourcePosition(3, 5))
    after = symbol_at(db, path, SourcePosition(4, 12))
    assert module_value is not None
    assert class_value is not None
    assert module_value != class_value
    assert before == module_value
    assert assignment_rhs == module_value
    assert after == class_value


def test_self_and_cls_attributes_require_a_proven_method_receiver(tmp_path: Path) -> None:
    path = tmp_path / "receivers.py"
    source = (
        "class Container:\n"
        "    value = 1\n"
        "    def method(self):\n"
        "        return self.value\n"
        "    def closure(self):\n"
        "        def inner():\n"
        "            return self.value\n"
        "        def shadow(self):\n"
        "            return self.value\n"
        "    @staticmethod\n"
        "    def static(self):\n"
        "        return self.value\n"
        "    @classmethod\n"
        "    def construct(cls):\n"
        "        return cls.value\n"
        "    def fake_class(cls):\n"
        "        return cls.value\n"
        "    def rebound(self):\n"
        "        self = object()\n"
        "        return self.value\n"
    )
    path.write_text(source, encoding="utf-8")
    db = Database()

    target = symbol_at(db, path, SourcePosition(1, 5))
    assert target is not None

    def attribute_at(line: int) -> object:
        character = source.splitlines()[line].rindex("value")
        return symbol_at(db, path, SourcePosition(line, character))

    assert attribute_at(3) == target
    assert attribute_at(6) == target
    assert attribute_at(8) is None
    assert attribute_at(11) is None
    assert attribute_at(14) == target
    assert attribute_at(16) is None
    assert attribute_at(19) is None


def test_workspace_reference_resolution_is_conservative_for_attributes(
    tmp_path: Path,
) -> None:
    provider = tmp_path / "provider.py"
    consumer = tmp_path / "consumer.py"
    provider.write_text("def run():\n    pass\n", encoding="utf-8")
    consumer.write_text(
        "import provider\nprovider.run()\nunknown.run()\n",
        encoding="utf-8",
    )
    db = Database()
    target = symbol_at(db, tmp_path, provider, SourcePosition(0, 5))
    proven = symbol_at(db, tmp_path, consumer, SourcePosition(1, 10))
    unproven = symbol_at(db, tmp_path, consumer, SourcePosition(2, 9))

    assert target is not None
    assert proven == target
    assert unproven is None


def test_symbol_at_resolves_static_wildcard_import(tmp_path: Path) -> None:
    provider = tmp_path / "provider.py"
    consumer = tmp_path / "consumer.py"
    provider.write_text("__all__ = ['run']\ndef run():\n    pass\n", encoding="utf-8")
    consumer.write_text("from provider import *\nrun()\n", encoding="utf-8")
    db = Database()

    target = symbol_at(db, tmp_path, provider, SourcePosition(1, 5))
    imported = symbol_at(db, tmp_path, consumer, SourcePosition(1, 1))

    assert target is not None
    assert imported == target


def test_symbol_at_resolves_only_proven_nested_attribute_chains(
    tmp_path: Path,
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    provider = package / "sub.py"
    provider.write_text(
        "def run():\n    pass\nclass Worker:\n    def go(self):\n        pass\n",
        encoding="utf-8",
    )
    imported = tmp_path / "imported.py"
    imported.write_text(
        "import pkg.sub\npkg.sub.run()\npkg.sub.Worker.go(None)\n",
        encoding="utf-8",
    )
    aliased = tmp_path / "aliased.py"
    aliased.write_text("import pkg.sub as sub\nsub.run()\n", encoding="utf-8")
    unimported = tmp_path / "unimported.py"
    unimported.write_text("pkg.sub.run()\n", encoding="utf-8")
    db = Database()

    run_target = symbol_at(db, tmp_path, provider, SourcePosition(0, 5))
    method_target = symbol_at(db, tmp_path, provider, SourcePosition(3, 9))
    nested_run = symbol_at(db, tmp_path, imported, SourcePosition(1, 9))
    nested_method = symbol_at(db, tmp_path, imported, SourcePosition(2, 16))
    aliased_run = symbol_at(db, tmp_path, aliased, SourcePosition(1, 5))
    speculative = symbol_at(db, tmp_path, unimported, SourcePosition(0, 9))

    assert run_target is not None
    assert method_target is not None
    assert nested_run == run_target
    assert nested_method == method_target
    assert aliased_run == run_target
    assert speculative is None
    references = find_references(db, tmp_path, run_target)
    assert [(Path(item.path).name, item.range.start.line) for item in references.references] == [
        ("aliased.py", 1),
        ("imported.py", 1),
        ("sub.py", 0),
    ]


def test_attribute_resolution_respects_local_receiver_shadowing(
    tmp_path: Path,
) -> None:
    provider = tmp_path / "provider.py"
    consumer = tmp_path / "consumer.py"
    rebound = tmp_path / "rebound.py"
    provider.write_text("def run():\n    pass\n", encoding="utf-8")
    consumer.write_text(
        "import provider as mod\ndef use():\n    mod = object()\n    return mod.run()\n",
        encoding="utf-8",
    )
    rebound.write_text(
        "import provider as mod\nmod = object()\nmod.run()\n",
        encoding="utf-8",
    )
    db = Database()

    target = symbol_at(db, tmp_path, provider, SourcePosition(0, 5))
    shadowed = symbol_at(db, tmp_path, consumer, SourcePosition(3, 16))
    rebound_use = symbol_at(db, tmp_path, rebound, SourcePosition(2, 5))

    assert target is not None
    assert shadowed is None
    assert rebound_use is None


def test_attribute_resolution_stops_at_same_scope_rebinding(
    tmp_path: Path,
) -> None:
    provider = tmp_path / "provider.py"
    consumer = tmp_path / "consumer.py"
    provider.write_text("def foo():\n    pass\n", encoding="utf-8")
    consumer.write_text(
        "from provider import foo\nimport provider as mod\nmod.foo()\nmod = object()\nmod.foo()\n",
        encoding="utf-8",
    )
    db = Database()

    target = symbol_at(db, tmp_path, provider, SourcePosition(0, 5))
    before_rebinding = symbol_at(db, tmp_path, consumer, SourcePosition(2, 5))
    after_rebinding = symbol_at(db, tmp_path, consumer, SourcePosition(4, 5))

    assert target is not None
    assert before_rebinding == target
    assert after_rebinding is None
    references = find_references(db, tmp_path, target)
    assert {(Path(item.path).name, item.range.start.line) for item in references.references} == {
        ("consumer.py", 2),
        ("provider.py", 0),
    }


def test_attribute_resolution_does_not_escape_lexical_receiver_shadowing(
    tmp_path: Path,
) -> None:
    provider = tmp_path / "provider.py"
    consumer = tmp_path / "consumer.py"
    provider.write_text("def foo():\n    pass\n", encoding="utf-8")
    consumer.write_text(
        "from provider import foo\n"
        "import provider as mod\n"
        "def parameter_shadow(mod):\n"
        "    return mod.foo()\n"
        "def local_shadow():\n"
        "    mod = object()\n"
        "    return mod.foo()\n"
        "def closure_shadow():\n"
        "    mod = object()\n"
        "    def nested():\n"
        "        return mod.foo()\n"
        "    return nested()\n"
        "values = [mod.foo() for mod in (object(),)]\n"
        "object().foo()\n",
        encoding="utf-8",
    )
    db = Database()

    target = symbol_at(db, tmp_path, provider, SourcePosition(0, 5))
    parameter_use = symbol_at(db, tmp_path, consumer, SourcePosition(3, 16))
    local_use = symbol_at(db, tmp_path, consumer, SourcePosition(6, 16))
    closure_use = symbol_at(db, tmp_path, consumer, SourcePosition(10, 20))
    comprehension_use = symbol_at(db, tmp_path, consumer, SourcePosition(12, 15))
    unproven_receiver_use = symbol_at(db, tmp_path, consumer, SourcePosition(13, 10))

    assert target is not None
    assert parameter_use is None
    assert local_use is None
    assert closure_use is None
    assert comprehension_use is None
    assert unproven_receiver_use is None
    references = find_references(db, tmp_path, target)
    assert [(Path(item.path).name, item.range.start.line) for item in references.references] == [
        ("provider.py", 0)
    ]


def test_augmented_assignment_and_delete_have_read_write_occurrences(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mod.py"
    path.write_text("value = 1\nvalue += 2\ndel value\n", encoding="utf-8")

    db = Database()
    occurrences = [item for item in scope_tree(db, path).occurrences if item.name == "value"]

    declarations = [item for item in occurrences if item.is_declaration]
    reads = [item for item in occurrences if not item.is_declaration]
    assert [item.range.start.line for item in declarations] == [0, 1]
    assert [item.range.start.line for item in reads] == [1, 2]

    target = symbol_at(db, path, SourcePosition(0, 1))
    assert target is not None
    with_declaration = find_references(db, tmp_path, target)
    without_declaration = find_references(db, tmp_path, target, include_declaration=False)
    assert [item.range.start.line for item in with_declaration.references] == [
        0,
        1,
        2,
    ]
    assert [item.is_declaration for item in with_declaration.references] == [
        True,
        False,
        False,
    ]
    assert [item.range.start.line for item in without_declaration.references] == [
        1,
        2,
    ]
    assert all(not item.is_declaration for item in without_declaration.references)
