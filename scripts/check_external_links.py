"""Validate external links in every public Markdown document."""

from __future__ import annotations

import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from scripts.check_docs import (
    _IMAGE_LINK,
    _INLINE_CODE,
    _INLINE_LINK,
    PROJECT_ROOT,
    _prose_lines,
    markdown_files,
)

_REACHABLE_HTTP_ERRORS = frozenset({401, 403, 406, 429})


@dataclass(frozen=True)
class Link:
    source: Path
    target: str


def external_links(root: Path = PROJECT_ROOT) -> tuple[Link, ...]:
    """Collect unique HTTP(S) Markdown links with fragments removed."""
    links: dict[str, Path] = {}
    for path in markdown_files(root):
        prose = _INLINE_CODE.sub("", "\n".join(_prose_lines(path)))
        for pattern in (_INLINE_LINK, _IMAGE_LINK):
            for match in pattern.finditer(prose):
                raw = match.group("target").strip("<>")
                parsed = urllib.parse.urlsplit(raw)
                if parsed.scheme not in {"http", "https"}:
                    continue
                target = urllib.parse.urlunsplit(parsed._replace(fragment=""))
                links.setdefault(target, path)
    return tuple(Link(source=source, target=target) for target, source in sorted(links.items()))


def _open(target: str, method: str) -> int:
    request = urllib.request.Request(
        target,
        method=method,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.8,*/*;q=0.5",
            "User-Agent": "pyinc-link-check/1 (+https://github.com/Brumbelow/pyinc)",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return int(response.status)


def _local_current_version_target(target: str, root: Path) -> Path | None:
    """Resolve a same-repository version URL before its tag is published."""

    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        version = project["version"]
    except (KeyError, OSError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(version, str):
        return None

    parsed = urllib.parse.urlsplit(target)
    parts = tuple(urllib.parse.unquote(parsed.path).lstrip("/").split("/"))
    tag = f"v{version}"
    relative: tuple[str, ...] | None = None
    if parsed.netloc.lower() == "github.com" and parts[:4] == ("Brumbelow", "pyinc", "blob", tag):
        relative = parts[4:]
    elif parsed.netloc.lower() == "raw.githubusercontent.com" and parts[:3] == (
        "Brumbelow",
        "pyinc",
        tag,
    ):
        relative = parts[3:]
    if not relative or any(part in {"", ".", ".."} for part in relative):
        return None

    candidate = root.joinpath(*relative)
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def check_link(link: Link, root: Path = PROJECT_ROOT) -> str | None:
    """Return an error for one unreachable link, tolerating access controls."""
    for method in ("HEAD", "GET"):
        try:
            status = _open(link.target, method)
        except urllib.error.HTTPError as exc:
            if exc.code in _REACHABLE_HTTP_ERRORS:
                return None
            if method == "HEAD" and exc.code in {400, 404, 405, 501}:
                continue
            if exc.code == 404 and _local_current_version_target(link.target, root) is not None:
                return None
            return f"{link.source}: {link.target} returned HTTP {exc.code}"
        except (OSError, ValueError) as exc:
            if method == "HEAD":
                continue
            return f"{link.source}: {link.target} failed: {type(exc).__name__}: {exc}"
        if 200 <= status < 400:
            return None
        if method == "GET":
            return f"{link.source}: {link.target} returned HTTP {status}"
    return f"{link.source}: {link.target} could not be checked"


def main() -> int:
    links = external_links()
    errors = tuple(error for link in links if (error := check_link(link)) is not None)
    if errors:
        for error in errors:
            print(f"external-link check: {error}", file=sys.stderr)
        return 1
    print(f"external-link check passed: {len(links)} unique links")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
