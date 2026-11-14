from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sys
import threading
from collections.abc import Iterator, Sequence
from dataclasses import asdict
from pathlib import Path
from types import FrameType

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

# Ordered most to least severe. A threshold matches itself and everything above.
_SEVERITY_RANK: dict[str, int] = {"error": 0, "warning": 1, "information": 2, "hint": 3}

_FAIL_ON_NEVER = "none"


def _int_at_least(text: str, minimum: int) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {text!r}") from None
    if value < minimum:
        raise argparse.ArgumentTypeError(f"must be at least {minimum}, got {value}")
    return value


def _non_negative_int(text: str) -> int:
    return _int_at_least(text, 0)


def _positive_int(text: str) -> int:
    # A zero poll interval would make the watcher spin on the CPU.
    return _int_at_least(text, 1)


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
    analyze.add_argument(
        "--debounce-ms", type=_non_negative_int, default=200, help="Watcher debounce window."
    )
    analyze.add_argument(
        "--poll-interval-ms",
        type=_positive_int,
        default=None,
        help="Watcher poll cadence (defaults to half the debounce window).",
    )
    analyze.add_argument(
        "--indent", type=_non_negative_int, default=2, help="JSON indentation level."
    )
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


def _discard_stdout() -> None:
    """Point stdout at the null device once the reader has closed the pipe.

    The interpreter flushes stdout on exit. A flush into the closed pipe would
    fail again and print the error to stderr.
    """

    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, sys.stdout.fileno())
    finally:
        os.close(devnull)


def _require_file(session: WorkspaceSession, path: str) -> None:
    # Analysis of a path with no file behind it returns an empty result, which
    # would let a mistyped path pass a CI gate.
    real_path = session._normalize_real_path(path)
    if not Path(real_path).is_file():
        raise FileNotFoundError(f"{real_path} is not an existing file.")


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
    diagnostic without a range keeps the ``path:`` anchor and omits the
    position, since any position would point at an unrelated line.
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


@contextlib.contextmanager
def _exit_on_sigterm() -> Iterator[None]:
    """Raise SystemExit on SIGTERM while the language server runs.

    The default action ends the process at once, so the workspace mirror stays
    in the temp directory. The exception unwinds serve(), whose teardown closes
    the session and removes the mirror. The exit status is 128 plus the signal
    number. A later SIGTERM is ignored, so it cannot cut the teardown short.
    """

    if threading.current_thread() is not threading.main_thread():
        # Only the main thread may set a signal handler.
        yield
        return

    def _handle(signum: int, _frame: FrameType | None) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    previous = signal.signal(signal.SIGTERM, _handle)
    try:
        yield
    finally:
        # None means the handler was not set from Python. Nothing better than
        # the default can be put back.
        signal.signal(signal.SIGTERM, signal.SIG_DFL if previous is None else previous)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "analyze":
        if args.watch and args.fail_on != _FAIL_ON_NEVER:
            # Watch mode runs until interrupted, so an exit-code gate has no
            # finished run to report on.
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
                if args.path is not None:
                    _require_file(session, args.path)
                result = _analyze_once(session, args.path)
                _emit_result(
                    result,
                    output_format=args.output_format,
                    diagnostics_only=args.diagnostics_only,
                    indent=args.indent,
                )
            except BrokenPipeError:
                _discard_stdout()
                return EXIT_ERROR
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
            stop_event = threading.Event()
            exit_status = EXIT_SUCCESS

            def _on_change(changed: tuple[str, ...]) -> None:
                nonlocal exit_status
                changed_result = _analyze_once(session, args.path)
                try:
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
                except BrokenPipeError:
                    # Nothing can read later results, so watching ends here.
                    _discard_stdout()
                    exit_status = EXIT_ERROR
                    stop_event.set()

            with watcher:
                watcher.start(_on_change, interval_s=interval_s)
                try:
                    stop_event.wait()
                except KeyboardInterrupt:
                    return EXIT_SUCCESS
            return exit_status

    server = LanguageServer(default_root=args.root)
    with _exit_on_sigterm():
        return server.serve()


if __name__ == "__main__":
    raise SystemExit(main())
