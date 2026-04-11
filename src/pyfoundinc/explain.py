from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import Database, NodeKey


def format_explanation(db: "Database", key: "NodeKey") -> str:
    lines: list[str] = []
    visited: set[NodeKey] = set()

    def walk(current: "NodeKey", depth: int) -> None:
        record = db._records[current]
        indent = "  " * depth
        lines.append(
            f"{indent}- {record.label}: {record.last_decision}"
            f" [last_recompute={record.last_recompute}]"
            f" (changed_at={record.changed_at}, verified_at={record.verified_at})"
        )
        if record.reason:
            lines.append(f"{indent}  reason: {record.reason}")
        for item in record.untracked_reasons:
            lines.append(f"{indent}  untracked: {item}")
        if current in visited:
            lines.append(f"{indent}  cycle-cut: already visited")
            return
        visited.add(current)
        for dep in sorted(record.dependencies, key=lambda item: item.label):
            walk(dep, depth + 1)

    walk(key, 0)
    return "\n".join(lines)
