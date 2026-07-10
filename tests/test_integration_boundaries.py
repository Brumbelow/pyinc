from __future__ import annotations

import ast
from pathlib import Path

_INTEGRATIONS = Path(__file__).parents[1] / "src" / "pyinc" / "integrations"
_INTERNAL_MODULE_GROUPS = (frozenset({"scope_resolution", "symbol_resolution"}),)


def _declared_exports(module: str) -> frozenset[str]:
    tree = ast.parse((_INTEGRATIONS / f"{module}.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            continue
        value = ast.literal_eval(node.value)
        return frozenset(value)
    return frozenset()


def _integration_import(node: ast.ImportFrom) -> str | None:
    if node.module is None:
        return None
    if node.level == 1:
        return node.module.split(".", 1)[0]
    prefix = "pyinc.integrations."
    if node.level == 0 and node.module.startswith(prefix):
        return node.module.removeprefix(prefix).split(".", 1)[0]
    return None


def test_cross_integration_imports_use_declared_composition_contracts() -> None:
    violations: list[str] = []
    for path in sorted(_INTEGRATIONS.glob("*.py")):
        source_module = path.stem
        if source_module == "__init__":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            target_module = _integration_import(node)
            if (
                target_module is None
                or target_module == source_module
                or target_module.startswith("_")
                or any({source_module, target_module} <= group for group in _INTERNAL_MODULE_GROUPS)
            ):
                continue
            exports = _declared_exports(target_module)
            for imported in node.names:
                if imported.name != "*" and imported.name not in exports:
                    violations.append(
                        f"{path.name}:{node.lineno} imports undeclared "
                        f"{target_module}.{imported.name}"
                    )
    assert violations == []


def test_requirements_payload_is_composable_but_not_package_level() -> None:
    from pyinc import integrations
    from pyinc.integrations import requirements_txt

    assert "RequirementPayload" in requirements_txt.__all__
    assert "requirements_payload" in requirements_txt.__all__
    assert "RequirementPayload" not in integrations.__all__
    assert "requirements_payload" not in integrations.__all__
