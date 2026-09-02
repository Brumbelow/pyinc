"""Validate the repository's Markdown documentation without network access."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from collections.abc import Iterator
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
# matches the surrounding sentence so a stale number cannot survive unnoticed,
# and each entry carries the name of the sentence it guards: one document can
# hold two of them, and without the name the two errors read identically.
_CHECKPOINT_VERSION_PROSE = (
    (
        Path("docs/kernel-contract.md"),
        "the manifest-key sentence",
        re.compile(r"content-addressed manifest \(schema v(?P<version>\d+)\)"),
    ),
    (
        Path("docs/architecture.md"),
        "the load-acceptance sentence",
        re.compile(r"accepts manifest schema v(?P<version>\d+) only"),
    ),
    (
        Path("docs/kernel-contract.md"),
        "the rejection sentence",
        re.compile(r"Manifest schema v(?P<version>\d+) rejects"),
    ),
)
_ACTION_VERSION_NAME = "_MANIFEST_VERSION"
_ACTION_VERSION_SOURCE = Path("src/pyinc/action.py")
# Prose that states the action ledger's manifest schema version. The two
# documents spell it differently -- `Schema v3` against `schema-v3` -- so one
# pattern cannot serve both.
_ACTION_VERSION_PROSE = (
    (
        Path("docs/action-contract.md"),
        "the schema-records sentence",
        re.compile(r"Schema v(?P<version>\d+) records exactly"),
    ),
    (
        Path("docs/action-contract.md"),
        "the incompatibility sentence",
        re.compile(r"not compatible with v(?P<version>\d+)'s ledger semantics"),
    ),
    (
        Path("docs/architecture.md"),
        "the ledger-publication clause",
        re.compile(r"the schema-v(?P<version>\d+) ledger is published last"),
    ),
)
# Public-surface rows whose description cell must carry a `Fields:` sentence
# listing the dataclass's own annotated fields, in declaration order.
_DATACLASS_FIELD_SOURCES = {
    "DatabaseStatistics": Path("src/pyinc/runtime.py"),
    "DependencyGraphNode": Path("src/pyinc/runtime.py"),
    "InspectionNode": Path("src/pyinc/explain.py"),
    "QueryProfile": Path("src/pyinc/runtime.py"),
}
_FIELDS_SENTENCE = re.compile(r"Fields:(?P<names>[^.]*)\.")
_API_FILES = {
    "pyinc": Path("src/pyinc/__init__.py"),
    "pyinc.integrations": Path("src/pyinc/integrations/__init__.py"),
    "pyinc_codegen": Path("src/pyinc_codegen/__init__.py"),
    "pyinc_tools": Path("src/pyinc_tools/__init__.py"),
}
_LSP_DOCUMENT = Path("docs/lsp-reference.md")
_LSP_SOURCE = Path("src/pyinc_tools/lsp.py")
# The language server decides what it supports by comparing `method` against a
# string in one of these three functions, and it publishes diagnostics of its
# own accord; a method the reference names that appears in neither place is one
# the server never sees.
_LSP_DISPATCH_FUNCTIONS = ("_handle_request", "_dispatch_request", "_handle_notification")
_LSP_NOTIFICATION_SENDER = "_send_notification"


@dataclass(frozen=True)
class Fence:
    path: Path
    line: int
    info: tuple[str, ...]
    content: str


@dataclass(frozen=True)
class TableRow:
    line: int
    raw: str
    cells: tuple[str, ...]
    section: str | None
    closed: bool


@dataclass(frozen=True)
class ConsumerSurface:
    module: str
    document: Path
    labels: frozenset[str]


# Each consumer package documents its exported names as grouped rows, and the
# group labels are what tells such a row from the other two-cell tables the
# codegen guide carries. The document belongs to the key as much as the labels
# do, so a label may repeat across guides without one guide answering for the
# other.
_CONSUMER_SURFACES = (
    ConsumerSurface(
        "pyinc_tools",
        Path("docs/pyinc-tools-guide.md"),
        frozenset(
            {
                "Entrypoints",
                "Analysis results",
                "Navigation results",
                "Editing results",
                "Kind aliases",
            }
        ),
    ),
    ConsumerSurface(
        "pyinc_codegen",
        Path("docs/codegen-guide.md"),
        frozenset({"Entrypoints", "Result types", "Errors and enumerations"}),
    ),
)


def _is_fence_close(line: str, marker: str) -> bool:
    candidate = line.strip()
    return (
        len(candidate) >= len(marker)
        and candidate.startswith(marker[0])
        and not candidate.strip(marker[0])
    )


def markdown_files(root: Path) -> tuple[Path, ...]:
    """Return the checked Markdown files in deterministic order.

    The changelog and the issue templates are public Markdown the project
    ships, so a link that stops resolving in one of them is worth as much as a
    link in a guide. The template entries come from a glob and heal themselves
    when a template is added or renamed; the changelog is named outright, like
    the four files above it.
    """
    return (
        root / "README.md",
        root / "CONTRIBUTING.md",
        root / "SECURITY.md",
        root / "bench/README.md",
        *sorted((root / "docs").glob("*.md")),
        root / "CHANGELOG.md",
        *sorted((root / ".github/ISSUE_TEMPLATE").glob("*.md")),
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


def table_rows(text: str, *, max_indent: int = 3) -> Iterator[TableRow]:
    """Yield every Markdown table row, with the heading it sits under.

    The documented tables come in two shapes -- one names a category and lists
    the names in its second cell, the other names one thing per row -- so the
    consumers differ in which cell they read and which rows they want. What
    they must not differ in is what counts as a row, which is what this yields:
    header and separator rows included, because each caller recognises its own.

    A row indented further than `max_indent` is not yielded. At the default of
    three that is the four spaces where the renderer stops seeing a table and
    starts seeing an indented code block, so reading one would mean checking
    text nobody renders as a table. `closed` reports whether the row also ends
    in a pipe, which is how a row wrapped across two lines is told from a whole
    one.
    """
    section: str | None = None
    for number, line in enumerate(text.splitlines(), 1):
        heading = _HEADING.match(line)
        if heading is not None:
            section = heading.group("title")
            continue
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if len(line) - len(line.lstrip(" ")) > max_indent:
            continue
        yield TableRow(
            line=number,
            raw=line,
            cells=tuple(cell.strip() for cell in stripped.strip("|").split("|")),
            section=section,
            closed=stripped.endswith("|"),
        )


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
    for row in table_rows(contract_path.read_text(encoding="utf-8")):
        if len(row.cells) != 2 or row.cells[0] not in _PUBLIC_ROW_NAMES:
            continue
        documented.update(_INLINE_CODE.findall(row.cells[1]))
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
    for row in table_rows(contract_path.read_text(encoding="utf-8")):
        if row.section != "Public Surface" or len(row.cells) != 2:
            continue
        if row.cells[0] in {"Name", ""} or set(row.cells[0]) <= {"-"}:
            continue
        documented.update(_INLINE_CODE.findall(row.cells[0]))
    exported = _read_exports(root, "pyinc")
    errors: list[str] = []
    missing = sorted(exported - documented)
    extra = sorted(documented - exported)
    if missing:
        errors.append("docs/kernel-contract.md: undocumented exports: " + ", ".join(missing))
    if extra:
        errors.append("docs/kernel-contract.md: names absent from __all__: " + ", ".join(extra))
    return tuple(errors)


def check_documented_consumer_api(root: Path) -> tuple[str, ...]:
    """Compare each consumer guide's public-surface rows with the package's exports.

    A row is recognised by the group its first cell names, not by the section
    it sits under. The codegen guide carries other two-cell tables whose first
    cells are schema tokens, and reading those as documented names would accuse
    that package of exporting `format` and `pattern`; the tools guide's only
    other table is four-cell, so it cannot reach a two-cell gate at all. A
    heading gate would keep the schema tables out as well, but it would also
    lose any row a fenced comment beginning with `#` had cut loose from its
    heading, and both guides contain fences. The labels themselves are the
    gate, and they are paired with the document that carries them so the same
    label may appear in both guides.

    The groups are the guides' own editorial arrangement, so nothing here
    compares a name against the group it was filed under. What is compared is
    the union: every exported name appears in some row, and every name the
    rows carry is exported.
    """
    errors: list[str] = []
    for surface in _CONSUMER_SURFACES:
        document = root / surface.document
        if not document.is_file():
            errors.append(
                f"{surface.document.as_posix()}: missing document named by the consumer surface check"
            )
            continue
        documented: set[str] = set()
        for row in table_rows(document.read_text(encoding="utf-8")):
            if len(row.cells) != 2 or row.cells[0] not in surface.labels:
                continue
            documented.update(_INLINE_CODE.findall(row.cells[1]))
        exported = _read_exports(root, surface.module)
        missing = sorted(exported - documented)
        extra = sorted(documented - exported)
        if missing:
            errors.append(
                f"{surface.document.as_posix()}: undocumented exports: " + ", ".join(missing)
            )
        if extra:
            errors.append(
                f"{surface.document.as_posix()}: names absent from __all__: " + ", ".join(extra)
            )
    return tuple(errors)


def _read_int_constant(root: Path, source: Path, name: str) -> int:
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


def _check_version_prose(
    root: Path,
    *,
    subject: str,
    constant: str,
    expected: int,
    entries: tuple[tuple[Path, str, re.Pattern[str]], ...],
) -> tuple[str, ...]:
    """Compare each documented schema version against the constant that decides it.

    A named document that is not there is reported rather than read: a check that
    raises on a removed file takes every other check down with it and prints no
    result line at all, which is worse than the stale sentence it was looking for.
    """
    errors: list[str] = []
    for relative, label, pattern in entries:
        document = root / relative
        if not document.is_file():
            errors.append(
                f"{relative.as_posix()}: missing document named by the {subject} check"
            )
            continue
        prose = re.sub(r"\s+", " ", " ".join(_prose_lines(document)))
        match = pattern.search(prose)
        if match is None:
            errors.append(
                f"{relative.as_posix()}: no {subject} statement matching {label} "
                f"({pattern.pattern!r})"
            )
            continue
        documented = int(match.group("version"))
        if documented != expected:
            errors.append(
                f"{relative.as_posix()}: {label} documents {subject} v{documented}, "
                f"but {constant} is {expected}"
            )
    return tuple(errors)


def check_checkpoint_manifest_version(root: Path) -> tuple[str, ...]:
    """Pin the documented manifest schema version to the kernel's own constant."""
    return _check_version_prose(
        root,
        subject="checkpoint manifest schema",
        constant=_CHECKPOINT_VERSION_NAME,
        expected=_read_int_constant(root, _CHECKPOINT_VERSION_SOURCE, _CHECKPOINT_VERSION_NAME),
        entries=_CHECKPOINT_VERSION_PROSE,
    )


def check_action_manifest_version(root: Path) -> tuple[str, ...]:
    """Pin the documented action ledger schema version to the ledger's own constant."""
    return _check_version_prose(
        root,
        subject="action manifest schema",
        constant=_ACTION_VERSION_NAME,
        expected=_read_int_constant(root, _ACTION_VERSION_SOURCE, _ACTION_VERSION_NAME),
        entries=_ACTION_VERSION_PROSE,
    )


def _read_dataclass_fields(root: Path, name: str) -> tuple[str, ...]:
    path = root / _DATACLASS_FIELD_SOURCES[name]
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for statement in tree.body:
        if not isinstance(statement, ast.ClassDef) or statement.name != name:
            continue
        fields = [
            entry.target.id
            for entry in statement.body
            if isinstance(entry, ast.AnnAssign) and isinstance(entry.target, ast.Name)
        ]
        return tuple(fields)
    raise ValueError(f"{path} does not define a class named {name}")


def _first_field_difference(documented: tuple[str, ...], declared: tuple[str, ...]) -> str:
    # The two lists differ in length whenever a name was dropped or invented, so
    # the common prefix is compared first and the ragged tail is reported below.
    for position, (written, actual) in enumerate(zip(documented, declared, strict=False), start=1):
        if written != actual:
            return f"field {position} is documented as `{written}` but is declared `{actual}`"
    if len(documented) < len(declared):
        return f"the sentence stops before the declared field `{declared[len(documented)]}`"
    return f"the sentence names `{documented[len(declared)]}`, which is not a declared field"


def check_documented_dataclass_fields(root: Path) -> tuple[str, ...]:
    """Compare the public-surface field lists with the dataclasses they describe."""
    contract_path = root / "docs/kernel-contract.md"
    errors: list[str] = []
    for row in table_rows(contract_path.read_text(encoding="utf-8")):
        if row.section != "Public Surface":
            continue
        names = _INLINE_CODE.findall(row.cells[0])
        if len(names) != 1:
            continue
        name = names[0]
        tracked = name in _DATACLASS_FIELD_SOURCES
        # A wrapped row keeps a well-formed head line, so a `Fields:` sentence
        # that does not open and close on the same line as its name cell is
        # malformed rather than absent.
        if not row.closed or len(row.cells) != 2:
            if tracked:
                errors.append(
                    f"docs/kernel-contract.md: the {name} row is not a single two-cell row "
                    "on one line"
                )
            continue
        description = row.cells[1]
        sentence = _FIELDS_SENTENCE.search(description)
        if not tracked:
            # A row that lists fields for a name no source is recorded for is a
            # sentence nothing compares. Without this, dropping an entry from
            # the mapping above leaves its row documented, unchecked and green.
            if sentence is not None:
                errors.append(
                    f"docs/kernel-contract.md: the {name} row lists fields, but "
                    f"{name} is not one of the types whose fields are checked"
                )
            continue
        if sentence is None:
            detail = (
                "no `Fields:` sentence ending in a period"
                if "Fields:" in description
                else "no `Fields:` sentence"
            )
            errors.append(f"docs/kernel-contract.md: the {name} row has {detail}")
            continue
        documented = tuple(_INLINE_CODE.findall(sentence.group("names")))
        declared = _read_dataclass_fields(root, name)
        if documented != declared:
            errors.append(
                f"docs/kernel-contract.md: {name} field list disagrees with the dataclass: "
                + _first_field_difference(documented, declared)
            )
    return tuple(errors)


def _lsp_dispatched_methods(function: ast.AsyncFunctionDef | ast.FunctionDef) -> set[str]:
    """Return every string the function tests `method` for equality against."""
    names: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1 or len(node.comparators) != 1:
            continue
        left = node.left
        comparator = node.comparators[0]
        if not isinstance(left, ast.Name) or left.id != "method":
            continue
        if not isinstance(node.ops[0], ast.Eq):
            continue
        if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
            names.add(comparator.value)
    return names


def _lsp_published_methods(tree: ast.AST) -> set[str]:
    """Return every method name the server sends as a notification of its own."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        called = node.func
        if not isinstance(called, ast.Attribute) or called.attr != _LSP_NOTIFICATION_SENDER:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
    return names


def check_documented_lsp_methods(root: Path, *, minimum: int = 30) -> tuple[str, ...]:
    """Compare the reference's method matrix with the methods the server handles.

    The matrix is the only description of the protocol surface, and nothing
    compares it with the dispatch chain, so a method the server gains or loses
    drifts away from the document silently. Every inline-code span in the
    Method column counts as a documented method: the lifecycle methods carry no
    slash and neither does an abbreviated spelling, so no shape rule separates
    a real name from a wrong one, and one would hide the abbreviations this
    comparison exists to report.

    `minimum` guards the extraction rather than the surface. It counts the
    distinct method strings: a function can compare `method` against the same
    name twice, and the published notification is collected apart from the
    chain. The default sits well under what the dispatch chain yields, so a
    walk that matches nothing or nearly nothing is reported rather than passing
    on an empty harvest; a single dropped method is the comparison's own job.
    """
    document = root / _LSP_DOCUMENT
    if not document.is_file():
        return (f"{_LSP_DOCUMENT.as_posix()}: missing document named by the LSP method check",)
    documented: set[str] = set()
    for row in table_rows(document.read_text(encoding="utf-8")):
        if row.section != "Method matrix" or len(row.cells) != 3:
            continue
        if row.cells[0] == "Method" or set(row.cells[0]) <= {"-"}:
            continue
        documented.update(_INLINE_CODE.findall(row.cells[0]))

    source = root / _LSP_SOURCE
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        and node.name in _LSP_DISPATCH_FUNCTIONS
    }
    implemented = _lsp_published_methods(tree)
    unreadable: list[str] = []
    for name in _LSP_DISPATCH_FUNCTIONS:
        function = functions.get(name)
        if function is None:
            unreadable.append(f"{_LSP_SOURCE.as_posix()}: no {name} to read LSP methods from")
            continue
        implemented |= _lsp_dispatched_methods(function)
    if unreadable:
        # A harvest known to be short is not compared: every method the missing
        # function dispatched would be reported as documented but unhandled,
        # which blames the reference for a rename in the server.
        return tuple(unreadable)
    if len(implemented) < minimum:
        return (
            f"{_LSP_SOURCE.as_posix()}: found {len(implemented)} LSP method strings, "
            f"too few to compare against the documented matrix (expected at least {minimum})",
        )

    errors: list[str] = []
    undocumented = sorted(implemented - documented)
    unhandled = sorted(documented - implemented)
    if undocumented:
        errors.append(
            f"{_LSP_DOCUMENT.as_posix()}: undocumented methods: " + ", ".join(undocumented)
        )
    if unhandled:
        errors.append(
            f"{_LSP_DOCUMENT.as_posix()}: methods the server does not handle: "
            + ", ".join(unhandled)
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
        *check_documented_dataclass_fields(root),
        *check_action_manifest_version(root),
        *check_documented_lsp_methods(root),
        *check_documented_consumer_api(root),
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
