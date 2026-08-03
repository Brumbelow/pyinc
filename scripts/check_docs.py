"""Validate the repository's Markdown documentation without network access."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
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
_API_FILES = {
    "pyinc": Path("src/pyinc/__init__.py"),
    "pyinc.integrations": Path("src/pyinc/integrations/__init__.py"),
    "pyinc_codegen": Path("src/pyinc_codegen/__init__.py"),
    "pyinc_tools": Path("src/pyinc_tools/__init__.py"),
}


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
    """Return the checked Markdown files in deterministic order."""
    return (
        root / "README.md",
        root / "CONTRIBUTING.md",
        root / "bench/README.md",
        *sorted((root / "docs").glob("*.md")),
    )


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


def _read_exports(root: Path, module: str) -> frozenset[str]:
    path = root / _API_FILES[module]
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in statement.targets):
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
                errors.append(
                    f"{fence.path}:{fence.line}: executable example failed: {detail}"
                )
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
    if version_result.returncode != 0 or re.fullmatch(
        r"pyinc-tools\s+\S+\n?", version_result.stdout
    ) is None:
        errors.append("pyinc-tools --version must print 'pyinc-tools <installed-version>'")
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
        errors.append("docs/integration-contract.md: names absent from __all__: " + ", ".join(extra))
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


def _read_checkpoint_manifest_version(root: Path) -> int:
    path = root / _CHECKPOINT_VERSION_SOURCE
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == _CHECKPOINT_VERSION_NAME
            for target in statement.targets
        ):
            continue
        value = ast.literal_eval(statement.value)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        break
    raise ValueError(f"{path} does not assign an integer {_CHECKPOINT_VERSION_NAME}")


def check_checkpoint_manifest_version(root: Path) -> tuple[str, ...]:
    """Pin the documented manifest schema version to the kernel's own constant."""
    expected = _read_checkpoint_manifest_version(root)
    errors: list[str] = []
    for relative, pattern in _CHECKPOINT_VERSION_PROSE:
        prose = re.sub(r"\s+", " ", " ".join(_prose_lines(root / relative)))
        match = pattern.search(prose)
        if match is None:
            errors.append(
                f"{relative}: no checkpoint manifest version statement "
                f"matching {pattern.pattern!r}"
            )
            continue
        documented = int(match.group("version"))
        if documented != expected:
            errors.append(
                f"{relative}: documents checkpoint manifest schema v{documented}, "
                f"but {_CHECKPOINT_VERSION_NAME} is {expected}"
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
        *check_documented_integration_api(root),
        *check_documented_kernel_api(root),
        *check_checkpoint_manifest_version(root),
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
        f"{executable_count} executable examples"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
