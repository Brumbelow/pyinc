from __future__ import annotations

import argparse
import json
import sys
import threading
from collections.abc import Sequence
from dataclasses import asdict

from .lsp import LanguageServer, _package_version
from .session import (
    AnalysisDiagnostic,
    FileAnalysisResult,
    PollingWorkspaceWatcher,
    WorkspaceAnalysisResult,
    WorkspaceSession,
)

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_DIAGNOSTICS = 3

AnalysisResult = WorkspaceAnalysisResult | FileAnalysisResult

# Ordered most to least severe; a threshold matches itself and everything above.
_SEVERITY_RANK: dict[str, int] = {"error": 0, "warning": 1, "information": 2, "hint": 3}

_FAIL_ON_NEVER = "none"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pyinc-tools")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_package_version()}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="Run workspace or file analysis.")
    analyze.add_argument("root", help="Workspace root to mirror and analyze.")
    analyze.add_argument("--path", help="Analyze one path under the workspace root.")
    analyze.add_argument(
        "--watch",
        action="store_true",
        help="Poll for file changes and re-run analysis.",
    )
    analyze.add_argument("--debounce-ms", type=int, default=200, help="Watcher debounce window.")
    analyze.add_argument(
        "--poll-interval-ms",
        type=int,
        default=None,
        help="Watcher poll cadence (defaults to half the debounce window).",
    )
    analyze.add_argument("--indent", type=int, default=2, help="JSON indentation level.")
    analyze.add_argument(
        "--format",
        dest="output_format",
        choices=("json", "text"),
        default="json",
        help="Output format. 'text' prints one diagnostic per line.",
    )
    analyze.add_argument(
        "--diagnostics-only",
        action="store_true",
        help="Emit only diagnostics instead of the full analysis result.",
    )
    analyze.add_argument(
        "--fail-on",
        choices=(_FAIL_ON_NEVER, "error", "warning", "information", "hint"),
        default=_FAIL_ON_NEVER,
        help="Exit with status 3 when a diagnostic of at least this severity is reported.",
    )

    lsp = subparsers.add_parser("lsp", help="Start the stdio LSP adapter.")
    lsp.add_argument("--root", help="Fallback workspace root if the client omits one.")
    return parser


def _emit_json(payload: object, *, indent: int) -> None:
    print(json.dumps(payload, indent=indent, sort_keys=True), flush=True)


def _analyze_once(session: WorkspaceSession, path: str | None) -> AnalysisResult:
    if path is None:
        return session.analyze_workspace()
    return session.analyze_file(path)


def _diagnostic_sort_key(diagnostic: AnalysisDiagnostic) -> tuple[str, int, int, str, str]:
    """Order diagnostics by location, placing rangeless ones first within a file."""

    if diagnostic.range is None:
        line, character = -1, -1
    else:
        line, character = diagnostic.range.start.line, diagnostic.range.start.character
    return (diagnostic.path, line, character, diagnostic.code, diagnostic.message)


def _sorted_diagnostics(result: AnalysisResult) -> tuple[AnalysisDiagnostic, ...]:
    return tuple(sorted(result.diagnostics, key=_diagnostic_sort_key))


def _format_diagnostic(diagnostic: AnalysisDiagnostic) -> str:
    """Render one diagnostic as ``path:line:col: severity code message``.

    Source coordinates are zero-based, so both are incremented for display. A
    diagnostic without a range keeps the ``path:`` anchor and omits the position
    rather than pointing at an unrelated line.
    """

    if diagnostic.range is None:
        location = diagnostic.path
    else:
        start = diagnostic.range.start
        location = f"{diagnostic.path}:{start.line + 1}:{start.character + 1}"
    return f"{location}: {diagnostic.severity} {diagnostic.code} {diagnostic.message}"


def _emit_text(diagnostics: Sequence[AnalysisDiagnostic]) -> None:
    for diagnostic in diagnostics:
        print(_format_diagnostic(diagnostic), flush=True)


def _emit_result(
    result: AnalysisResult,
    *,
    output_format: str,
    diagnostics_only: bool,
    indent: int,
) -> None:
    if output_format == "text":
        _emit_text(_sorted_diagnostics(result))
        return
    if diagnostics_only:
        _emit_json([asdict(item) for item in _sorted_diagnostics(result)], indent=indent)
        return
    _emit_json(asdict(result), indent=indent)


def _gate_tripped(result: AnalysisResult, fail_on: str) -> bool:
    if fail_on == _FAIL_ON_NEVER:
        return False
    threshold = _SEVERITY_RANK[fail_on]
    return any(
        _SEVERITY_RANK.get(diagnostic.severity, len(_SEVERITY_RANK)) <= threshold
        for diagnostic in result.diagnostics
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "analyze":
        if args.watch and args.fail_on != _FAIL_ON_NEVER:
            # Watch mode never terminates normally, so there is no run for an
            # exit-code gate to report on.
            print(
                "pyinc-tools: --fail-on cannot be combined with --watch",
                file=sys.stderr,
            )
            return EXIT_USAGE
        try:
            session_context = WorkspaceSession(args.root)
        except (OSError, ValueError) as exc:
            print(f"pyinc-tools: {exc}", file=sys.stderr)
            return EXIT_ERROR
        with session_context as session:
            try:
                result = _analyze_once(session, args.path)
                _emit_result(
                    result,
                    output_format=args.output_format,
                    diagnostics_only=args.diagnostics_only,
                    indent=args.indent,
                )
            except (OSError, ValueError) as exc:
                print(f"pyinc-tools: {exc}", file=sys.stderr)
                return EXIT_ERROR
            if not args.watch:
                if _gate_tripped(result, args.fail_on):
                    return EXIT_DIAGNOSTICS
                return EXIT_SUCCESS

            interval_s = (
                args.poll_interval_ms / 1000.0
                if args.poll_interval_ms is not None
                else max(args.debounce_ms / 1000.0 / 2.0, 0.05)
            )
            watcher = PollingWorkspaceWatcher(session, debounce_ms=args.debounce_ms)
            idle_event = threading.Event()

            def _on_change(changed: tuple[str, ...]) -> None:
                changed_result = _analyze_once(session, args.path)
                if args.output_format == "text":
                    print(f"# changed: {' '.join(changed)}", flush=True)
                    _emit_text(_sorted_diagnostics(changed_result))
                    return
                payload = (
                    [asdict(item) for item in _sorted_diagnostics(changed_result)]
                    if args.diagnostics_only
                    else asdict(changed_result)
                )
                _emit_json(
                    {"changed_paths": list(changed), "analysis": payload},
                    indent=args.indent,
                )

            with watcher:
                watcher.start(_on_change, interval_s=interval_s)
                try:
                    idle_event.wait()
                except KeyboardInterrupt:
                    return EXIT_SUCCESS
            return EXIT_SUCCESS

    server = LanguageServer(default_root=args.root)
    return server.serve()


if __name__ == "__main__":
    raise SystemExit(main())
