from __future__ import annotations

import argparse
import json
import sys
import threading
from collections.abc import Sequence
from dataclasses import asdict

from .lsp import LanguageServer, _package_version
from .session import PollingWorkspaceWatcher, WorkspaceSession

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


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

    lsp = subparsers.add_parser("lsp", help="Start the stdio LSP adapter.")
    lsp.add_argument("--root", help="Fallback workspace root if the client omits one.")
    return parser


def _emit_json(payload: object, *, indent: int) -> None:
    print(json.dumps(payload, indent=indent, sort_keys=True), flush=True)


def _analyze_once(session: WorkspaceSession, path: str | None) -> object:
    if path is None:
        return asdict(session.analyze_workspace())
    return asdict(session.analyze_file(path))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "analyze":
        try:
            session_context = WorkspaceSession(args.root)
        except (OSError, ValueError) as exc:
            print(f"pyinc-tools: {exc}", file=sys.stderr)
            return EXIT_ERROR
        with session_context as session:
            try:
                _emit_json(_analyze_once(session, args.path), indent=args.indent)
            except (OSError, ValueError) as exc:
                print(f"pyinc-tools: {exc}", file=sys.stderr)
                return EXIT_ERROR
            if not args.watch:
                return EXIT_SUCCESS

            interval_s = (
                args.poll_interval_ms / 1000.0
                if args.poll_interval_ms is not None
                else max(args.debounce_ms / 1000.0 / 2.0, 0.05)
            )
            watcher = PollingWorkspaceWatcher(session, debounce_ms=args.debounce_ms)
            idle_event = threading.Event()

            def _on_change(changed: tuple[str, ...]) -> None:
                _emit_json(
                    {
                        "changed_paths": list(changed),
                        "analysis": _analyze_once(session, args.path),
                    },
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
