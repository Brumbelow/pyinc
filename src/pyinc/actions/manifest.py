"""Ownership manifest: the durable record of what an action owns.

After a successful reconciliation the manifest lists every ``(relative_path,
content_digest)`` the action wrote. The next run consults it to know which files
it may safely delete when a declaration disappears — never touching foreign,
unowned files. The manifest lives in a state directory *outside* the output root
and is serialized as canonical, deterministic JSON bytes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .artifacts import ToolIdentity
from .errors import ActionStateError

MANIFEST_VERSION = 1


def _tool_to_obj(tool: ToolIdentity) -> dict[str, object]:
    return {
        "name": tool.name,
        "version": tool.version,
        "schema_version": tool.schema_version,
        "executable_digest": tool.executable_digest,
        "config_digest": tool.config_digest,
    }


def _tool_from_obj(obj: dict[str, object]) -> ToolIdentity:
    return ToolIdentity(
        name=str(obj["name"]),
        version=str(obj["version"]),
        schema_version=_coerce_int(obj.get("schema_version", 1), "tool.schema_version"),
        executable_digest=_opt_str(obj.get("executable_digest")),
        config_digest=_opt_str(obj.get("config_digest")),
    )


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _coerce_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ActionStateError(f"Action manifest field {field!r} must be an integer.")
    return value


@dataclass(frozen=True)
class ActionManifest:
    """Owned-output record for one action at one output root."""

    action_id: str
    output_root: str
    tool: ToolIdentity
    entries: tuple[tuple[str, str], ...]
    manifest_version: int = MANIFEST_VERSION

    @property
    def owned_paths(self) -> frozenset[str]:
        return frozenset(path for path, _digest in self.entries)

    def to_json_bytes(self) -> bytes:
        """Serialize to canonical bytes: keys sorted, entries sorted, compact
        separators, trailing newline. The same manifest always produces the same
        bytes regardless of insertion order."""
        payload = {
            "manifest_version": self.manifest_version,
            "action_id": self.action_id,
            "output_root": self.output_root,
            "tool": _tool_to_obj(self.tool),
            "entries": [list(entry) for entry in sorted(self.entries)],
        }
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return (text + "\n").encode("utf-8")

    @staticmethod
    def from_json_bytes(payload: bytes) -> ActionManifest:
        try:
            obj = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ActionStateError(f"Action manifest is not valid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise ActionStateError("Action manifest must be a JSON object.")
        version = _coerce_int(obj.get("manifest_version", -1), "manifest_version")
        if version != MANIFEST_VERSION:
            raise ActionStateError(
                f"Unsupported action manifest version {version!r}; expected {MANIFEST_VERSION}."
            )
        raw_entries = obj.get("entries", [])
        if not isinstance(raw_entries, list):
            raise ActionStateError("Action manifest 'entries' must be a list.")
        entries = tuple(
            (str(item[0]), str(item[1]))
            for item in raw_entries
        )
        tool_obj = obj.get("tool")
        if not isinstance(tool_obj, dict):
            raise ActionStateError("Action manifest 'tool' must be an object.")
        return ActionManifest(
            action_id=str(obj["action_id"]),
            output_root=str(obj["output_root"]),
            tool=_tool_from_obj(tool_obj),
            entries=tuple(sorted(entries)),
            manifest_version=version,
        )
