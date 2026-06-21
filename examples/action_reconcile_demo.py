"""Demonstrate the action / reconciliation layer.

A payload query computes the *desired* output bytes (pure, no filesystem writes).
A small entrypoint decodes the tuple payload into a `DesiredArtifactSet`, and a
`FilesystemReconciler` reconciles it to disk outside query evaluation: cold
creation, zero-write reruns, selective rewrites, tamper repair, and
ownership-aware stale deletion.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TypeAlias, cast

from pyinc import Database, Input, query, thaw
from pyinc.actions import (
    ActionIdentity,
    DesiredArtifact,
    DesiredArtifactSet,
    FilesystemReconciler,
    ToolIdentity,
)

# Input: a mapping of logical module name -> exported symbol.
MODULES = Input[tuple[tuple[str, str], ...]]("modules")

_TOOL = ToolIdentity(name="demo-codegen", version="1.0.0", schema_version=1)

# Payload shape: (relative_path, content_bytes) per generated file.
ArtifactsPayload: TypeAlias = tuple[tuple[str, bytes], ...]


@query
def rendered_payload(db: Database) -> ArtifactsPayload:
    """Pure payload query: render one file per module. No filesystem access."""
    return tuple(
        (f"{name}.py", f"# generated\n{name.upper()} = {symbol!r}\n".encode())
        for name, symbol in MODULES.read(db)
    )


def desired_artifacts(db: Database, output_root: str) -> DesiredArtifactSet:
    """Entrypoint: decode the tuple payload into a typed desired-artifact set."""
    payload = cast(ArtifactsPayload, thaw(db.get(rendered_payload)))
    artifacts = tuple(DesiredArtifact(path, content) for path, content in payload)
    identity = ActionIdentity(action_id="demo-codegen", output_root=output_root, tool=_TOOL)
    return DesiredArtifactSet(identity, artifacts)


def main() -> None:
    with tempfile.TemporaryDirectory() as root:
        out = Path(root) / "generated"
        state = Path(root) / "state"
        db = Database()
        rec = FilesystemReconciler(out, state_dir=state)

        # ---- Run 1: cold generation -------------------------------------
        db.set(MODULES, (("alpha", "A"), ("beta", "B")))
        result = rec.apply(desired_artifacts(db, str(out)))
        print(f"run1_writes={result.writes}")
        print(f"run1_files={sorted(p.name for p in out.glob('*.py'))}")

        # ---- Run 2: identical rerun -> zero writes ----------------------
        mtime = (out / "alpha.py").stat().st_mtime_ns
        result = rec.apply(desired_artifacts(db, str(out)))
        print(f"run2_writes={result.writes}")
        print(f"run2_unchanged={result.unchanged}")
        print(f"run2_mtime_stable={(out / 'alpha.py').stat().st_mtime_ns == mtime}")

        # ---- Run 3: change one input -> only that file rewrites ---------
        db.set(MODULES, (("alpha", "A2"), ("beta", "B")))
        result = rec.apply(desired_artifacts(db, str(out)))
        print(f"run3_writes={result.writes}")

        # ---- Run 4: external tamper is repaired -------------------------
        (out / "beta.py").write_text("CORRUPTED")
        result = rec.apply(desired_artifacts(db, str(out)))
        print(f"run4_writes={result.writes}")

        # ---- Run 5: remove a module -> stale deletion (owned only) ------
        (out / "handwritten.py").write_text("# not owned by the action\n")
        db.set(MODULES, (("alpha", "A2"),))
        result = rec.apply(desired_artifacts(db, str(out)))
        print(f"run5_deletions={result.deletions}")
        print(f"run5_foreign_preserved={(out / 'handwritten.py').exists()}")


if __name__ == "__main__":
    main()
