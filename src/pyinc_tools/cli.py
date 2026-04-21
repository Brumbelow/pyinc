from __future__ import annotations

import argparse
import json
import time
from collections.abc import Sequence
from dataclasses import asdict

from .lsp import LanguageServer
from .session import PollingWorkspaceWatcher, WorkspaceSession


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pyinc-tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="Run workspace or file analysis.")
    analyze.add_argument("root", help="Workspace root to mirror and analyze.")
    analyze.add_argument("--path", help="Analyze one path under the workspace root.")
    analyze.add_argument("--watch", action="store_true", help="Poll for file changes and re-run analysis.")
    analyze.add_argument("--debounce-ms", type=int, default=200, help="Watcher debounce window.")
    analyze.add_argument("--indent", type=int, default=2, help="JSON indentation level.")

    lsp = subparsers.add_parser("lsp", help="Start the stdio LSP adapter.")
    lsp.add_argument("--root", help="Fallback workspace root if the client omits one.")
    return parser


def _emit_json(payload: object, *, indent: int) -> None:
    print(json.dumps(payload, indent=indent, sort_keys=True))


def _analyze_once(session: WorkspaceSession, path: str | None) -> object:
    if path is None:
        return asdict(session.analyze_workspace())
    return asdict(session.analyze_file(path))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "analyze":
        with WorkspaceSession(args.root) as session:
            _emit_json(_analyze_once(session, args.path), indent=args.indent)
            if not args.watch:
                return 0

            watcher = PollingWorkspaceWatcher(session, debounce_ms=args.debounce_ms)
            try:
                while True:
                    changed = watcher.poll()
                    if changed:
                        _emit_json(
                            {
                                "changed_paths": changed,
                                "analysis": _analyze_once(session, args.path),
                            },
                            indent=args.indent,
                        )
                    time.sleep(max(args.debounce_ms / 1000.0 / 2.0, 0.05))
            except KeyboardInterrupt:
                return 0

    server = LanguageServer(default_root=args.root)
    return server.serve()
