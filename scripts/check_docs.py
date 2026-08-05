"""Validate the repository's Markdown documentation without network access."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
from dataclasses import dataclass
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_FENCE_OPEN = re.compile(r"^(?P<marker>`{3,}|~{3,})\s*(?P<info>.*)$")
_HEADING = re.compile(r"^(?P<level>#{1,6})\s+(?P<title>.+?)\s*#*\s*$")
_INLINE_LINK = re.compile(r"(?<!!)\[[^]]*\]\((?P<target>[^)\s]+)")
_IMAGE_LINK = re.compile(r"!\[[^]]*\]\((?P<target>[^)\s]+)")
_INLINE_CODE = re.compile(r"`([^`]+)`")
_GITHUB_LOCAL_PREFIX = "/Brumbelow/pyinc/blob/main/"
_PUBLIC_ROW_NAMES = frozenset({"Entrypoints", "Result types", "Shared types"})
_CHECKPOINT_VERSION_NAME = "_CHECKPOINT_MANIFEST_VERSION"
_CHECKPOINT_VERSION_SOURCE = Path("src/pyinc/runtime.py")
# Prose that states the durable checkpoint manifest schema version. Each pattern
# matches the surrounding sentence so a stale number cannot survive unnoticed.
_CHECKPOINT_VERSION_PROSE = (
    (
        Path("docs/kernel-contract.md"),
        re.compile(r"content-addressed manifest \(schema v(?P<version>\d+)\)"),
    ),
    (
        Path("docs/architecture.md"),
        re.compile(r"accepts manifest schema v(?P<version>\d+) only"),
    ),
    (
        Path("docs/migration-v3.md"),
        re.compile(r"Manifest schema v(?P<version>\d+) rejects"),
    ),
)
_ACTION_VERSION_NAME = "_MANIFEST_VERSION"
_ACTION_VERSION_SOURCE = Path("src/pyinc/action.py")
_ACTION_VERSION_PROSE = (
    (
        Path("docs/action-contract.md"),
        re.compile(r"Schema v(?P<version>\d+) records"),
    ),
    (
        Path("docs/architecture.md"),
        re.compile(r"schema-v(?P<version>\d+) ledger"),
    ),
    (
        Path("docs/migration-v3.md"),
        re.compile(r"schema v(?P<version>\d+) manifests"),
    ),
)
_API_FILES = {
    "pyinc": Path("src/pyinc/__init__.py"),
    "pyinc.integrations": Path("src/pyinc/integrations/__init__.py"),
    "pyinc_codegen": Path("src/pyinc_codegen/__init__.py"),
    "pyinc_tools": Path("src/pyinc_tools/__init__.py"),
    "pyinc.integrations.csv_data": Path("src/pyinc/integrations/csv_data.py"),
    "pyinc.integrations.deep_module_resolution": Path(
        "src/pyinc/integrations/deep_module_resolution.py"
    ),
    "pyinc.integrations.dependency_check": Path("src/pyinc/integrations/dependency_check.py"),
    "pyinc.integrations.env_file": Path("src/pyinc/integrations/env_file.py"),
    "pyinc.integrations.installed_packages": Path("src/pyinc/integrations/installed_packages.py"),
    "pyinc.integrations.json_config": Path("src/pyinc/integrations/json_config.py"),
    "pyinc.integrations.notebook": Path("src/pyinc/integrations/notebook.py"),
    "pyinc.integrations.python_source": Path("src/pyinc/integrations/python_source.py"),
    "pyinc.integrations.requirement_evaluation": Path(
        "src/pyinc/integrations/requirement_evaluation.py"
    ),
    "pyinc.integrations.requirements_txt": Path("src/pyinc/integrations/requirements_txt.py"),
    "pyinc.integrations.scope_resolution": Path("src/pyinc/integrations/scope_resolution.py"),
    "pyinc.integrations.symbol_resolution": Path("src/pyinc/integrations/symbol_resolution.py"),
    "pyinc.integrations.toml_config": Path("src/pyinc/integrations/toml_config.py"),
    "pyinc.integrations.xml_config": Path("src/pyinc/integrations/xml_config.py"),
}
_PUBLIC_API_TABLE_MODULES = frozenset(_API_FILES) - {"pyinc", "pyinc.integrations"}
_RELATED_WORK_DATE = "2026-08-04"
_POSITIONING_PHRASE = f"Among systems surveyed as of {_RELATED_WORK_DATE}, `pyinc` combines"
_RELATED_WORK_PINS = {
    "IncPy": ("TaPP 2010",),
    "Adapton": ("PLDI 2014",),
    "Loman": ("0.6.0", "82670779ba7c48113c46b2fe4c583a9827ce2a84"),
    "Cascade Query": ("0.2.4", "52bb5b40b249cccae507dfc884b33646062f1121"),
    "Calyxos": (
        "0.4.1",
        "90d7d5216e84752930bc974a00e820a397991a662447ae97aea7bce4da96b933",
    ),
}
_COMPETITIVE_SUPERLATIVE = re.compile(
    r"(?:\bpyinc\s+(?:is|was|remains|offers|provides|delivers|represents)\s+"
    r"(?:(?:the|an?)\s+)?(?:world(?:'s)?\s+)?(?:first|only|unique)\b|"
    r"\b(?:the|an?)\s+(?:world(?:'s)?\s+)?(?:first|only|unique)\s+"
    r"(?:Python\s+)?(?:incremental\s+)?(?:computation\s+)?"
    r"(?:engine|framework|library|runtime|system|implementation|project|"
    r"approach|contribution|combination|surface)\b)",
    re.IGNORECASE,
)
_ABSOLUTE_CLAIMS = (
    re.compile(r"\balways safe\b", re.IGNORECASE),
    re.compile(r"\bzero overhead\b", re.IGNORECASE),
    re.compile(r"\bfull provenance\b", re.IGNORECASE),
    re.compile(r"\bbyte-for-byte verified\b", re.IGNORECASE),
)
_LSP_PROVIDER_METHODS = {
    "documentSymbolProvider": frozenset({"textDocument/documentSymbol"}),
    "workspaceSymbolProvider": frozenset({"workspace/symbol"}),
    "hoverProvider": frozenset({"textDocument/hover"}),
    "completionProvider": frozenset({"textDocument/completion"}),
    "definitionProvider": frozenset({"textDocument/definition"}),
    "declarationProvider": frozenset({"textDocument/declaration"}),
    "typeDefinitionProvider": frozenset({"textDocument/typeDefinition"}),
    "referencesProvider": frozenset({"textDocument/references"}),
    "documentHighlightProvider": frozenset({"textDocument/documentHighlight"}),
    "linkedEditingRangeProvider": frozenset({"textDocument/linkedEditingRange"}),
    "renameProvider": frozenset({"textDocument/prepareRename", "textDocument/rename"}),
    "codeActionProvider": frozenset({"textDocument/codeAction"}),
    "signatureHelpProvider": frozenset({"textDocument/signatureHelp"}),
    "foldingRangeProvider": frozenset({"textDocument/foldingRange"}),
    "selectionRangeProvider": frozenset({"textDocument/selectionRange"}),
    "documentLinkProvider": frozenset({"textDocument/documentLink"}),
    "codeLensProvider": frozenset({"textDocument/codeLens"}),
    "callHierarchyProvider": frozenset(
        {
            "textDocument/prepareCallHierarchy",
            "callHierarchy/incomingCalls",
            "callHierarchy/outgoingCalls",
        }
    ),
    "typeHierarchyProvider": frozenset(
        {
            "textDocument/prepareTypeHierarchy",
            "typeHierarchy/supertypes",
            "typeHierarchy/subtypes",
        }
    ),
    "inlayHintProvider": frozenset({"textDocument/inlayHint"}),
    "semanticTokensProvider": frozenset(
        {"textDocument/semanticTokens/full", "textDocument/semanticTokens/range"}
    ),
    "diagnosticProvider": frozenset({"textDocument/diagnostic", "workspace/diagnostic"}),
}
_LSP_SYNC_METHODS = frozenset(
    {
        "textDocument/didOpen",
        "textDocument/didChange",
        "textDocument/didSave",
        "textDocument/didClose",
    }
)
_LSP_FILE_OPERATION_METHODS = frozenset({"workspace/willRenameFiles", "workspace/willDeleteFiles"})
_LSP_LIFECYCLE_METHODS = frozenset({"initialize", "initialized", "shutdown", "exit"})
_LSP_NON_CAPABILITY_METHODS = frozenset(
    {"workspace/didChangeWatchedFiles", "textDocument/publishDiagnostics"}
)


@dataclass(frozen=True)
class Fence:
    path: Path
    line: int
    info: tuple[str, ...]
    content: str


def _is_fence_close(line: str, marker: str) -> bool:
    candidate = line.strip()
    return (
        len(candidate) >= len(marker)
        and candidate.startswith(marker[0])
        and not candidate.strip(marker[0])
    )


def markdown_files(root: Path) -> tuple[Path, ...]:
    """Return every public Markdown file in deterministic order."""
    files = {
        *root.glob("*.md"),
        *(root / "bench").glob("*.md"),
        *(root / "docs").rglob("*.md"),
        *(root / ".github").rglob("*.md"),
    }
    return tuple(sorted(files))


def parse_fences(path: Path) -> tuple[tuple[Fence, ...], tuple[str, ...]]:
    """Extract fenced blocks and report unterminated fences."""
    fences: list[Fence] = []
    errors: list[str] = []
    marker = ""
    info: tuple[str, ...] = ()
    content: list[str] = []
    start_line = 0

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not marker:
            match = _FENCE_OPEN.match(line)
            if match is None:
                continue
            marker = match.group("marker")
            info = tuple(match.group("info").split())
            start_line = line_number + 1
            content = []
            continue
        if _is_fence_close(line, marker):
            fences.append(Fence(path=path, line=start_line, info=info, content="\n".join(content)))
            marker = ""
            info = ()
            content = []
            continue
        content.append(line)

    if marker:
        errors.append(f"{path}: unterminated fence opened on line {start_line - 1}")
    return tuple(fences), tuple(errors)


def _prose_lines(path: Path) -> tuple[str, ...]:
    lines: list[str] = []
    marker = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if marker:
            if _is_fence_close(line, marker):
                marker = ""
            continue
        match = _FENCE_OPEN.match(line)
        if match is not None:
            marker = match.group("marker")
            continue
        lines.append(line)
    return tuple(lines)


def _slug(title: str) -> str:
    title = re.sub(r"<[^>]+>", "", title.replace("`", "")).casefold()
    title = re.sub(r"[^\w\- ]", "", title)
    return re.sub(r"\s", "-", title.strip())


def heading_anchors(path: Path) -> frozenset[str]:
    """Return GitHub-style heading anchors, including duplicate suffixes."""
    counts: dict[str, int] = {}
    anchors: set[str] = set()
    for line in _prose_lines(path):
        match = _HEADING.match(line)
        if match is None:
            continue
        base = _slug(match.group("title"))
        duplicate = counts.get(base, 0)
        counts[base] = duplicate + 1
        anchors.add(base if duplicate == 0 else f"{base}-{duplicate}")
    return frozenset(anchors)


def _local_target(root: Path, source: Path, raw_target: str) -> tuple[Path, str] | None:
    target = raw_target.strip("<>")
    parsed = urllib.parse.urlsplit(target)
    if parsed.scheme in {"http", "https"}:
        if parsed.netloc.casefold() != "github.com":
            return None
        if not parsed.path.startswith(_GITHUB_LOCAL_PREFIX):
            return None
        relative = urllib.parse.unquote(parsed.path[len(_GITHUB_LOCAL_PREFIX) :])
        return root / relative, urllib.parse.unquote(parsed.fragment)
    if parsed.scheme or parsed.netloc:
        return None
    relative_path = urllib.parse.unquote(parsed.path)
    destination = source if not relative_path else source.parent / relative_path
    return destination, urllib.parse.unquote(parsed.fragment)


def check_local_links(root: Path, files: tuple[Path, ...]) -> tuple[str, ...]:
    """Check local Markdown and image targets without requesting external URLs.

    Images are checked for existence only; an external image URL is left alone,
    exactly as an external link is.
    """
    errors: list[str] = []
    anchors: dict[Path, frozenset[str]] = {}
    resolved_root = root.resolve()
    for path in files:
        prose = "\n".join(_prose_lines(path))
        prose = _INLINE_CODE.sub("", prose)
        for kind, pattern in (("link", _INLINE_LINK), ("image", _IMAGE_LINK)):
            for match in pattern.finditer(prose):
                raw_target = match.group("target")
                local = _local_target(root, path, raw_target)
                if local is None:
                    continue
                destination, fragment = local
                resolved = destination.resolve()
                try:
                    resolved.relative_to(resolved_root)
                except ValueError:
                    errors.append(f"{path}: local {kind} escapes the repository: {raw_target}")
                    continue
                if not resolved.is_file():
                    errors.append(f"{path}: missing local {kind} target: {raw_target}")
                    continue
                if fragment and kind == "link":
                    destination_anchors = anchors.setdefault(resolved, heading_anchors(resolved))
                    if fragment.casefold() not in destination_anchors:
                        errors.append(f"{path}: missing anchor #{fragment} in {resolved}")
    return tuple(errors)


def _claim_is_qualified(text: str, match: re.Match[str]) -> bool:
    before = text[max(0, match.start() - 80) : match.start()].casefold()
    after = text[match.end() : match.end() + 48].casefold()
    if re.search(r"\b(?:not|never|misleading|rejects?|forbids?)\b[^.!?]{0,64}$", before):
        return True
    return (
        match.group(0).casefold() == "zero overhead"
        and re.match(r"\s+(?:for|when|while|if|in|under|on|with)\b", after) is not None
    )


def check_public_claims(root: Path, files: tuple[Path, ...] | None = None) -> tuple[str, ...]:
    """Reject unqualified priority, safety, provenance, and overhead claims."""
    if files is None:
        files = (
            *markdown_files(root),
            *sorted((root / "examples").rglob("*.py")),
        )
    errors: list[str] = []
    for path in files:
        if path.suffix.casefold() == ".md":
            text = "\n".join(_prose_lines(path))
            text = _INLINE_CODE.sub("", text)
        else:
            text = path.read_text(encoding="utf-8")
        for pattern in (_COMPETITIVE_SUPERLATIVE, *_ABSOLUTE_CLAIMS):
            for match in pattern.finditer(text):
                if _claim_is_qualified(text, match):
                    continue
                line = text.count("\n", 0, match.start()) + 1
                errors.append(f"{path}:{line}: unqualified public claim {match.group(0)!r}")
    return tuple(errors)


def check_related_work_positioning(root: Path) -> tuple[str, ...]:
    """Pin the dated related-work evidence and scoped positioning sentence."""
    related_path = root / "docs/related-work.md"
    if not related_path.is_file():
        return ("docs/related-work.md: missing dated related-work matrix",)
    related = related_path.read_text(encoding="utf-8")
    errors: list[str] = []
    for path in (root / "README.md", related_path):
        prose = re.sub(r"\s+", " ", " ".join(_prose_lines(path)))
        if _POSITIONING_PHRASE not in prose:
            errors.append(f"{path}: missing scoped positioning phrase {_POSITIONING_PHRASE!r}")
    for system, pins in _RELATED_WORK_PINS.items():
        if system not in related:
            errors.append(f"docs/related-work.md: missing survey target {system}")
        for pin in pins:
            if pin not in related:
                errors.append(f"docs/related-work.md: missing {system} pin {pin}")
    return tuple(errors)


def _read_exports(root: Path, module: str) -> frozenset[str]:
    path = root / _API_FILES[module]
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in statement.targets
        ):
            continue
        value = ast.literal_eval(statement.value)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            break
        return frozenset(value)
    raise ValueError(f"{path} does not contain a literal __all__ list")


def _check_public_imports(root: Path, fence: Fence, tree: ast.AST) -> tuple[str, ...]:
    errors: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module not in _API_FILES:
            continue
        exports = _read_exports(root, node.module)
        for imported in node.names:
            if imported.name == "*" or imported.name not in exports:
                errors.append(
                    f"{fence.path}:{fence.line + node.lineno - 1}: "
                    f"{imported.name!r} is not exported by {node.module}.__all__"
                )
    return tuple(errors)


def check_python_fences(root: Path, files: tuple[Path, ...]) -> tuple[str, ...]:
    """Compile Python fences, validate public imports, and run docs-check examples."""
    errors: list[str] = []
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.fspath(root / "src"),
        }
    )
    for path in files:
        fences, fence_errors = parse_fences(path)
        errors.extend(fence_errors)
        for fence in fences:
            if not fence.info or fence.info[0] != "python":
                continue
            try:
                tree = ast.parse(fence.content, filename=f"{fence.path}:{fence.line}")
            except SyntaxError as exc:
                errors.append(f"{fence.path}:{fence.line}: invalid Python fence: {exc.msg}")
                continue
            errors.extend(_check_public_imports(root, fence, tree))
            if "docs-check" not in fence.info[1:]:
                continue
            with tempfile.TemporaryDirectory(prefix="pyinc-docs-") as directory:
                try:
                    result = subprocess.run(
                        [sys.executable, "-c", fence.content],
                        cwd=directory,
                        env=environment,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                except subprocess.TimeoutExpired:
                    errors.append(f"{fence.path}:{fence.line}: executable example timed out")
                    continue
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip() or "no output"
                errors.append(f"{fence.path}:{fence.line}: executable example failed: {detail}")
    return tuple(errors)


def check_cli_examples(root: Path) -> tuple[str, ...]:
    """Verify the documented CLI help/version examples against the local module."""
    guide = (root / "docs/pyinc-tools-guide.md").read_text(encoding="utf-8")
    errors: list[str] = []
    required = (
        "pyinc-tools --help",
        "pyinc-tools --version",
        "python -m pyinc_tools --help",
        "python -m pyinc_tools --version",
        "usage: pyinc-tools [-h] [--version] {analyze,lsp} ...",
        "pyinc-tools <installed-version>",
    )
    for text in required:
        if text not in guide:
            errors.append(f"docs/pyinc-tools-guide.md: missing CLI example {text!r}")

    # argparse colorizes help output from 3.14 on, and honours FORCE_COLOR even
    # when it is writing to a pipe. The comparison below is against the literal
    # usage line the guide documents, so ask for plain text explicitly.
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.fspath(root / "src"),
            "PYTHON_COLORS": "0",
            "NO_COLOR": "1",
        }
    )
    help_result = subprocess.run(
        [sys.executable, "-m", "pyinc_tools", "--help"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    expected_usage = "usage: pyinc-tools [-h] [--version] {analyze,lsp} ..."
    if help_result.returncode != 0 or help_result.stdout.splitlines()[:1] != [expected_usage]:
        errors.append("pyinc-tools --help no longer matches the documented usage line")

    version_result = subprocess.run(
        [sys.executable, "-m", "pyinc_tools", "--version"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if (
        version_result.returncode != 0
        or re.fullmatch(r"pyinc-tools\s+\S+\n?", version_result.stdout) is None
    ):
        errors.append("pyinc-tools --version must print 'pyinc-tools <installed-version>'")
    return tuple(errors)


def check_installed_examples(root: Path) -> tuple[str, ...]:
    """Run every shipped example and keep both wheel gates exhaustive."""
    examples = tuple(sorted((root / "examples").glob("*.py")))
    errors: list[str] = []
    if not examples:
        return ("examples: no installed-wheel examples found",)

    ci_workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    if "for example in examples/*.py; do" not in ci_workflow:
        errors.append(
            ".github/workflows/ci.yml: installed-wheel validation must run every examples/*.py file"
        )
    release_workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
    evidence_source = (root / "scripts/demo_evidence.py").read_text(encoding="utf-8")
    if (
        "python -m scripts.demo_evidence" not in release_workflow
        or '.glob("*.py")' not in evidence_source
    ):
        errors.append(
            ".github/workflows/release.yml: release demo evidence must run every "
            "installed-wheel example"
        )

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    sdist = (
        project.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("sdist", {})
    )
    includes = sdist.get("include") if isinstance(sdist, dict) else None
    if not isinstance(includes, list) or "/examples" not in includes:
        errors.append("pyproject.toml: sdist must include the shipped examples directory")

    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONHASHSEED": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.fspath(root / "src"),
        }
    )
    for example in examples:
        try:
            result = subprocess.run(
                [sys.executable, os.fspath(example)],
                cwd=root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            errors.append(f"{example.relative_to(root)}: installed-wheel example timed out")
            continue
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            errors.append(f"{example.relative_to(root)}: installed-wheel example failed: {detail}")
    return tuple(errors)


def check_documented_integration_api(root: Path) -> tuple[str, ...]:
    """Compare the integration contract's stable-name rows with package exports."""
    contract_path = root / "docs/integration-contract.md"
    documented: set[str] = set()
    for line in contract_path.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 2 or cells[0] not in _PUBLIC_ROW_NAMES:
            continue
        documented.update(_INLINE_CODE.findall(cells[1]))
    exported = _read_exports(root, "pyinc.integrations")
    errors: list[str] = []
    missing = sorted(exported - documented)
    extra = sorted(documented - exported)
    if missing:
        errors.append("docs/integration-contract.md: undocumented exports: " + ", ".join(missing))
    if extra:
        errors.append(
            "docs/integration-contract.md: names absent from __all__: " + ", ".join(extra)
        )
    return tuple(errors)


def check_documented_kernel_api(root: Path) -> tuple[str, ...]:
    """Compare the kernel contract's public-surface tables with package exports."""
    contract_path = root / "docs/kernel-contract.md"
    documented: set[str] = set()
    in_section = False
    for line in contract_path.read_text(encoding="utf-8").splitlines():
        heading = _HEADING.match(line)
        if heading is not None:
            in_section = heading.group("title") == "Public Surface"
            continue
        if not in_section or not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 2 or cells[0] in {"Name", ""} or set(cells[0]) <= {"-"}:
            continue
        documented.update(_INLINE_CODE.findall(cells[0]))
    exported = _read_exports(root, "pyinc")
    errors: list[str] = []
    missing = sorted(exported - documented)
    extra = sorted(documented - exported)
    if missing:
        errors.append("docs/kernel-contract.md: undocumented exports: " + ", ".join(missing))
    if extra:
        errors.append("docs/kernel-contract.md: names absent from __all__: " + ", ".join(extra))
    return tuple(errors)


def check_documented_module_api(root: Path) -> tuple[str, ...]:
    """Compare consumer and integration-module API rows with every ``__all__``."""
    contract_path = root / "docs/public-api.md"
    documented: dict[str, set[str]] = {}
    for line in contract_path.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 2:
            continue
        module_names = _INLINE_CODE.findall(cells[0])
        if len(module_names) != 1 or module_names[0] not in _PUBLIC_API_TABLE_MODULES:
            continue
        module = module_names[0]
        if module in documented:
            return (f"docs/public-api.md: duplicate public module row for {module}",)
        documented[module] = set(_INLINE_CODE.findall(cells[1]))

    errors: list[str] = []
    for module in sorted(_PUBLIC_API_TABLE_MODULES):
        names = documented.get(module)
        if names is None:
            errors.append(f"docs/public-api.md: missing public module row for {module}")
            continue
        exported = _read_exports(root, module)
        missing = sorted(exported - names)
        extra = sorted(names - exported)
        if missing:
            errors.append(
                f"docs/public-api.md: undocumented {module} exports: " + ", ".join(missing)
            )
        if extra:
            errors.append(
                f"docs/public-api.md: names absent from {module}.__all__: " + ", ".join(extra)
            )
    return tuple(errors)


def _class_method(tree: ast.Module, class_name: str, method_name: str) -> ast.FunctionDef:
    for statement in tree.body:
        if not isinstance(statement, ast.ClassDef) or statement.name != class_name:
            continue
        for member in statement.body:
            if isinstance(member, ast.FunctionDef) and member.name == method_name:
                return member
    raise ValueError(f"{class_name}.{method_name} was not found")


def _method_comparisons(function: ast.FunctionDef) -> frozenset[str]:
    methods: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.left, ast.Name) or node.left.id != "method":
            continue
        if not isinstance(node.ops[0], ast.Eq) or len(node.comparators) != 1:
            continue
        compared = node.comparators[0]
        if isinstance(compared, ast.Constant) and isinstance(compared.value, str):
            methods.add(compared.value)
    return frozenset(methods)


def _dictionary_value(node: ast.Dict, key: str) -> ast.AST | None:
    for candidate, value in zip(node.keys, node.values, strict=True):
        if isinstance(candidate, ast.Constant) and candidate.value == key:
            return value
    return None


def _initialize_capability_keys(function: ast.FunctionDef) -> frozenset[str]:
    for node in ast.walk(function):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        capabilities = _dictionary_value(node.value, "capabilities")
        if not isinstance(capabilities, ast.Dict):
            continue
        return frozenset(
            key.value
            for key in capabilities.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        )
    raise ValueError("LanguageServer._initialize does not return a capabilities table")


def _documented_lsp_methods(root: Path) -> frozenset[str]:
    path = root / "docs/lsp-reference.md"
    methods: set[str] = set()
    in_matrix = False
    for line in path.read_text(encoding="utf-8").splitlines():
        heading = _HEADING.match(line)
        if heading is not None:
            in_matrix = heading.group("title") == "Method matrix"
            continue
        if not in_matrix or not line.startswith("|"):
            continue
        first_cell = line.strip().strip("|").split("|", 1)[0]
        methods.update(_INLINE_CODE.findall(first_cell))
    return frozenset(methods)


def check_lsp_method_parity(root: Path) -> tuple[str, ...]:
    """Keep LSP capabilities, handlers, and the public method table in lockstep."""
    source_path = root / "src/pyinc_tools/lsp.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    requests = _method_comparisons(_class_method(tree, "LanguageServer", "_dispatch_request"))
    notifications = _method_comparisons(
        _class_method(tree, "LanguageServer", "_handle_notification")
    )
    handled = requests | notifications
    documented = _documented_lsp_methods(root)
    errors: list[str] = []

    expected_documented = handled | {"textDocument/publishDiagnostics"}
    missing_docs = sorted(expected_documented - documented)
    extra_docs = sorted(documented - expected_documented)
    if missing_docs:
        errors.append("docs/lsp-reference.md: undocumented LSP methods: " + ", ".join(missing_docs))
    if extra_docs:
        errors.append("docs/lsp-reference.md: methods without handlers: " + ", ".join(extra_docs))

    capability_keys = _initialize_capability_keys(
        _class_method(tree, "LanguageServer", "_initialize")
    )
    structural_keys = {"positionEncoding", "textDocumentSync", "workspace"}
    unknown_keys = sorted(capability_keys - structural_keys - _LSP_PROVIDER_METHODS.keys())
    if unknown_keys:
        errors.append(
            "src/pyinc_tools/lsp.py: unmapped advertised capability keys: "
            + ", ".join(unknown_keys)
        )
    missing_keys = sorted(_LSP_PROVIDER_METHODS.keys() - capability_keys)
    if missing_keys:
        errors.append(
            "src/pyinc_tools/lsp.py: expected capability keys are absent: "
            + ", ".join(missing_keys)
        )

    advertised = set(_LSP_SYNC_METHODS | _LSP_FILE_OPERATION_METHODS)
    for key in capability_keys & _LSP_PROVIDER_METHODS.keys():
        advertised.update(_LSP_PROVIDER_METHODS[key])
    expected_advertised = handled - _LSP_LIFECYCLE_METHODS - _LSP_NON_CAPABILITY_METHODS
    missing_capabilities = sorted(expected_advertised - advertised)
    extra_capabilities = sorted(advertised - handled)
    if missing_capabilities:
        errors.append(
            "src/pyinc_tools/lsp.py: handled methods lack capabilities: "
            + ", ".join(missing_capabilities)
        )
    if extra_capabilities:
        errors.append(
            "src/pyinc_tools/lsp.py: capabilities lack handlers: " + ", ".join(extra_capabilities)
        )
    return tuple(errors)


def _read_integer_assignment(root: Path, source: Path, name: str) -> int:
    path = root / source
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == name for target in statement.targets
        ):
            continue
        value = ast.literal_eval(statement.value)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        break
    raise ValueError(f"{path} does not assign an integer {name}")


def _read_checkpoint_manifest_version(root: Path) -> int:
    return _read_integer_assignment(root, _CHECKPOINT_VERSION_SOURCE, _CHECKPOINT_VERSION_NAME)


def check_checkpoint_manifest_version(root: Path) -> tuple[str, ...]:
    """Pin the documented manifest schema version to the kernel's own constant."""
    expected = _read_checkpoint_manifest_version(root)
    errors: list[str] = []
    for relative, pattern in _CHECKPOINT_VERSION_PROSE:
        prose = re.sub(r"\s+", " ", " ".join(_prose_lines(root / relative)))
        match = pattern.search(prose)
        if match is None:
            errors.append(
                f"{relative}: no checkpoint manifest version statement matching {pattern.pattern!r}"
            )
            continue
        documented = int(match.group("version"))
        if documented != expected:
            errors.append(
                f"{relative}: documents checkpoint manifest schema v{documented}, "
                f"but {_CHECKPOINT_VERSION_NAME} is {expected}"
            )
    return tuple(errors)


def check_action_manifest_version(root: Path) -> tuple[str, ...]:
    """Pin every documented action schema version to the implementation."""
    expected = _read_integer_assignment(root, _ACTION_VERSION_SOURCE, _ACTION_VERSION_NAME)
    errors: list[str] = []
    for relative, pattern in _ACTION_VERSION_PROSE:
        prose = re.sub(r"\s+", " ", " ".join(_prose_lines(root / relative)))
        match = pattern.search(prose)
        if match is None:
            errors.append(
                f"{relative}: no action manifest version statement matching {pattern.pattern!r}"
            )
            continue
        documented = int(match.group("version"))
        if documented != expected:
            errors.append(
                f"{relative}: documents action manifest schema v{documented}, "
                f"but {_ACTION_VERSION_NAME} is {expected}"
            )
    return tuple(errors)


def check_versioned_release_links(root: Path) -> tuple[str, ...]:
    """Keep packaged documentation links pinned to the project release tag."""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project["version"]
    if not isinstance(version, str):
        return ("pyproject.toml: project.version must be a string",)
    expected_prefix = f"https://github.com/Brumbelow/pyinc/blob/v{version}/"
    errors: list[str] = []
    urls = project.get("urls")
    if not isinstance(urls, dict):
        errors.append("pyproject.toml: project.urls must be a table")
    else:
        expected_urls = {
            "Documentation": expected_prefix + "docs/README.md",
            "Changelog": expected_prefix + "CHANGELOG.md",
        }
        for name, expected in expected_urls.items():
            if urls.get(name) != expected:
                errors.append(f"pyproject.toml: {name} must be pinned to {expected}")

    readme = (root / "README.md").read_text(encoding="utf-8")
    references = re.findall(r"https://github\.com/Brumbelow/pyinc/blob/([^/]+)/", readme)
    if not references:
        errors.append("README.md: no tag-pinned repository documentation links found")
    unexpected = sorted(set(references) - {f"v{version}"})
    if unexpected:
        errors.append(
            "README.md: documentation links use refs other than "
            f"v{version}: {', '.join(unexpected)}"
        )

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = re.findall(
        rf"(?m)^## \[{re.escape(version)}\] - (?P<date>\d{{4}}-\d{{2}}-\d{{2}})$",
        changelog,
    )
    if len(headings) != 1:
        errors.append(f"CHANGELOG.md: expected exactly one dated release heading for {version}")
    else:
        try:
            date.fromisoformat(headings[0])
        except ValueError:
            errors.append(f"CHANGELOG.md: {version} release date is not a calendar date")
    expected_reference = f"[{version}]: https://github.com/Brumbelow/pyinc/releases/tag/v{version}"
    if changelog.splitlines().count(expected_reference) != 1:
        errors.append(f"CHANGELOG.md: expected exactly one canonical {version} release reference")
    return tuple(errors)


def check_external_link_workflow(root: Path) -> tuple[str, ...]:
    """Require the network link check to run on a continuing schedule."""
    relative = Path(".github/workflows/external-links.yml")
    path = root / relative
    if not path.is_file():
        return (f"missing external-link workflow: {relative}",)
    workflow = path.read_text(encoding="utf-8")
    required = (
        "schedule:",
        "cron:",
        "workflow_dispatch:",
        "python -m scripts.check_external_links",
    )
    return tuple(
        f"{relative}: missing scheduled external-link gate {text!r}"
        for text in required
        if text not in workflow
    )


def check_release_assurance_gate(root: Path) -> tuple[str, ...]:
    """Keep the fail-closed assurance record and publication gates in sync."""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = project.get("version")
    errors: list[str] = []
    record_path = root / "release/assurance.json"
    try:
        record: object = json.loads(record_path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return (f"release/assurance.json: cannot read assurance record: {exc}",)
    if not isinstance(record, dict):
        errors.append("release/assurance.json: assurance record must be an object")
    else:
        if record.get("schema_version") != 3:
            errors.append("release/assurance.json: schema_version must be 3")
        if record.get("version") != version:
            errors.append(
                "release/assurance.json: version must match pyproject.toml project.version"
            )

    release_workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
    for required in (
        "python scripts/check_release_assurance.py",
        "gh issue list --state open --label",
        "gh run list",
        "--workflow release-candidate.yml",
        '--commit "$GITHUB_SHA"',
        "--event workflow_dispatch",
        "--status success",
        'test "$count" -gt 0',
    ):
        if required not in release_workflow:
            errors.append(f".github/workflows/release.yml: missing release gate {required!r}")

    candidate_relative = Path(".github/workflows/release-candidate.yml")
    candidate_path = root / candidate_relative
    if not candidate_path.is_file():
        errors.append(f"missing pre-tag candidate workflow: {candidate_relative}")
    else:
        candidate_workflow = candidate_path.read_text(encoding="utf-8")
        for required in (
            "workflow_dispatch:",
            "REQUESTED_RELEASE_COMMIT",
            'test "$GITHUB_REF" = "refs/heads/main"',
            'test "$REQUESTED_RELEASE_COMMIT" = "$GITHUB_SHA"',
            "uses: ./.github/workflows/ci.yml",
            "uses: ./.github/workflows/codeql.yml",
            "uses: ./.github/workflows/benchmark.yml",
            "uses: ./.github/workflows/mutation-testing.yml",
            "EXPECTED_FINGERPRINT",
            "verify_expected_signature",
            "git rev-list --min-parents=2",
            "python scripts/verify_release_metadata.py",
            "python scripts/check_release_assurance.py",
            '"release/assurance.json"',
            "gh issue list --state open --label",
        ):
            if required not in candidate_workflow:
                errors.append(f"{candidate_relative}: missing pre-tag release gate {required!r}")

    published_workflow = (root / ".github/workflows/published-artifacts.yml").read_text(
        encoding="utf-8"
    )
    for required in ("release:", "types:", "- published", "verify-published"):
        if required not in published_workflow:
            errors.append(
                ".github/workflows/published-artifacts.yml: missing automatic "
                f"post-publication gate {required!r}"
            )
    return tuple(errors)


def check_docs(root: Path = PROJECT_ROOT) -> tuple[str, ...]:
    """Run every offline documentation check."""
    files = markdown_files(root)
    missing = tuple(f"missing documentation file: {path}" for path in files if not path.is_file())
    if missing:
        return missing
    return (
        *check_local_links(root, files),
        *check_python_fences(root, files),
        *check_cli_examples(root),
        *check_installed_examples(root),
        *check_documented_integration_api(root),
        *check_documented_kernel_api(root),
        *check_documented_module_api(root),
        *check_lsp_method_parity(root),
        *check_checkpoint_manifest_version(root),
        *check_action_manifest_version(root),
        *check_versioned_release_links(root),
        *check_external_link_workflow(root),
        *check_release_assurance_gate(root),
        *check_related_work_positioning(root),
        *check_public_claims(root),
    )


def main() -> int:
    errors = check_docs()
    if errors:
        for error in errors:
            print(f"documentation check: {error}", file=sys.stderr)
        return 1
    executable_count = sum(
        1
        for path in markdown_files(PROJECT_ROOT)
        for fence in parse_fences(path)[0]
        if fence.info[:2] == ("python", "docs-check")
    )
    print(
        f"documentation check passed: {len(markdown_files(PROJECT_ROOT))} files, "
        f"{executable_count} executable documentation examples, "
        f"{len(tuple((PROJECT_ROOT / 'examples').glob('*.py')))} installed-wheel examples"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
