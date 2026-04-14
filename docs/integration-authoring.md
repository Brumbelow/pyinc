## Integration Authoring Guide

An integration is a domain-specific query graph built on the pyfoundinc kernel. The kernel
provides revisions, dependency tracking, red-green verification, and backdating. The
integration provides domain types, query decomposition, and external-state resources.

This guide extracts the shared patterns from the shipped integrations, using
`pyfoundinc.integrations.python_source` as the reference template and
`toml_config` / `requirements_txt` as smaller companion examples. Read
[kernel-contract.md](kernel-contract.md) for the soundness envelope and
[integration-contract.md](integration-contract.md) for the current public boundary.

### Three-Layer Query Structure

Integrations use a layered query architecture:

**Layer 1 -- Payload queries.** `@query`-decorated functions that return tuple-typed
payloads. These are the kernel-level cached nodes. They read resources, parse data, and
return simple hashable structures. Examples: `source_text` (python_source.py:486),
`imports_for_file`, `definitions_for_file`.

**Layer 2 -- Composition queries.** Queries that call other queries and assemble richer
composite payloads. Example: `workspace_analysis_payload` calls `module_analysis_payload`
in a loop over discovered Python files.

**Layer 3 -- High-level entrypoints.** Non-query functions that call `db.get()` and
decode tuple payloads into frozen dataclasses. These are the public API. Examples:
`file_analysis` (python_source.py:773), `workspace_analysis` (python_source.py:798).

**Why this layering?** The kernel caches and compares tuple payloads efficiently (they are
snapshot-safe and hashable by default). The decode layer converts to ergonomic dataclasses
only at the public boundary. Internal graph nodes stay cheap to hash and compare; the
external API stays user-friendly.

### Result Types

All public result types must be `@dataclass(frozen=True)` with snapshot-safe fields:

- Use `str`, `int`, `bool`, `None`, and `tuple` of these types.
- Use `tuple[T, ...]` instead of `list[T]` for collections (tuples are hashable and
  immutable).
- No `list`, `dict`, or `set` in result type fields.
- Reference: `ImportRef` (python_source.py:59), `PythonFileAnalysis`
  (python_source.py:81), `PythonWorkspaceAnalysis` (python_source.py:118).

**Why?** Frozen dataclasses satisfy the kernel's value boundary ownership condition
(kernel-contract.md condition 1). The kernel's `freeze`/`thaw` cycle handles them
automatically without custom `ValueAdapter` registration.

### Payload Type Aliases

Define a `TypeAlias` for each internal payload shape as a tuple matching the corresponding
dataclass's field order:

```python
ImportPayload: TypeAlias = tuple[str, ImportKind, int]
#                                 module, kind,  lineno  → matches ImportRef fields
```

Each layer has a `_decode_*` function that reconstructs the dataclass from its payload.
Reference: `ImportPayload` (python_source.py:24), `FileAnalysisPayload`
(python_source.py:49), `_decode_import` (python_source.py:705), `_decode_file_analysis`
(python_source.py:750).

**Why?** Tuples are snapshot-safe and hashable by default -- zero-cost for the kernel's
caching and comparison. The `TypeAlias` makes the bidirectional conversion self-documenting.

### Resources

All reads of external state inside a query must go through the Resource API. The kernel
intercepts `open()`, `os.getenv`, `os.listdir`, `os.scandir`, and `Path.iterdir` during
query execution and raises `UntrackedReadError` otherwise.

**Built-in resources:** `FileResource`, `FileStatResource`, `EnvResource`,
`DirectoryResource` cover common cases.

**Custom resources:** When built-in resources do not fit, define a custom resource as a
frozen dataclass (or class with `identity()`) implementing four methods:

- `read(db, key)` -- public read method; delegates to `db._read_resource(self, key)`.
- `label(key)` -- returns a human-readable string for provenance display.
- `probe(key)` -- returns a cheap, snapshot-safe fingerprint for change detection.
  Must not require `db`. Called on every request.
- `load(db, key)` -- performs the actual I/O under `db._allow_raw_open()`. Called only
  when `probe` detects a change.

Reference: `_SourceTextResource` (python_source.py:124-145) uses SHA-256 content hashing
in `probe` for precise invalidation beyond stat-based detection.

Instantiate resources as **module-level singletons**: `_FILES = _SourceTextResource()`,
`_DIRECTORIES = DirectoryResource()` (python_source.py:147-148).

**Why?** Resources are how the kernel enforces tracked ambient reads (kernel-contract.md
condition 2). The `probe`/`load` separation keeps cheap change detection on the fast path.
Resource configuration is part of the node key, so the resource must be snapshot-safe.

### Conservative Resolution and Untracked Reads

Two principles for maintaining the soundness guarantee:

**Prefer conservative outcomes over optimistic reuse.** When your integration cannot
determine a dependency statically, return `ambiguous` or `missing` rather than guessing.
Optimistic reuse risks from-scratch inconsistency. Reference:
`_resolve_workspace_module` (python_source.py:386) returns `"ambiguous"` when multiple
paths match a module prefix.

**Mark unsupported cases as untracked.** When static analysis hits a pattern it cannot
handle deterministically, call `db.report_untracked_read(reason)`. This forces
re-execution on every request but preserves correctness. Reference:
`module_export_surface` (python_source.py:639) marks dynamic `__all__` as untracked.

**Why?** From-scratch consistency is the kernel's primary guarantee. Re-execution is
always safe; stale reuse is never safe. An integration that guesses wrong about reuse
causes silent staleness that violates the soundness envelope.

### Cutoff Functions

Use `@query(cutoff=fn)` when semantic equivalence is cheaper than comparing full output
values. The cutoff function maps a query result to a snapshot-safe comparison token:

```python
def _source_cutoff_token(source: str) -> tuple[str, str]:
    try:
        return ("ast", ast.dump(ast.parse(source), include_attributes=True))
    except SyntaxError:
        return ("source", source)
```

Reference: `source_text` (python_source.py:486) uses `_source_cutoff_token`
(python_source.py:172). A comment-only edit produces the same AST dump, so the kernel
backdates `source_text` and downstream queries are reused without re-execution.

**Why?** Cutoff functions are the mechanism that enables backdating -- the Salsa/Skyframe
optimization that prevents false ripple when recomputation yields a semantically equivalent
result.

### Cycle-Safe Traversal

When your integration traverses directory trees or recursive structures:

- Track a `visited` set of canonical (resolved) paths.
- Use `Path.resolve()` to canonicalize before comparing.
- Check root containment before recursing to prevent escaping the workspace.
- Reference: `_collect_python_files` (python_source.py:449-482) uses
  `visited_directories`, `_canonical_path`, and `_is_within_root` for safe traversal.

### Stable API Surface

Define the public boundary explicitly:

1. Add `__all__` to your integration module listing only stable dataclass types and
   high-level entrypoints. Reference: python_source.py:811-824 lists 8 types and
   4 functions.
2. Add re-exports in `src/pyfoundinc/integrations/__init__.py` for only those stable
   names.
3. Experimental helpers (payload queries, decode functions, internal utilities) stay
   importable from the submodule but are **not** re-exported from
   `pyfoundinc.integrations`.

### Testing

Three categories of tests for an integration:

**Contract lock tests.** Verify that `__all__` has not drifted and that experimental
helpers are not re-exported. Reference:
`test_package_namespace_exports_only_stable_api` in `tests/test_python_source.py`.

**Mode-parametrized correctness tests.** Verify results across `strict`, `checked`, and
`fast` modes. Verify backdating explicitly: non-semantic edits should trigger backdating
and downstream reuse. Reference: `test_file_analysis_reports_top_level_symbols_by_mode`
in `tests/test_python_source.py`.

**From-scratch consistency tests.** The gold standard: compare incremental results against
fresh-database recomputation over a sequence of state changes. Reference:
`test_workspace_analysis_matches_fresh_recomputation_over_changes` in
`tests/test_python_source.py`.

### Checklist

A new integration needs:

- [ ] All public result types are `@dataclass(frozen=True)` with snapshot-safe fields
- [ ] All ambient reads go through resources or `db.report_untracked_read()`
- [ ] Payload queries return `TypeAlias`-typed tuples matching dataclass field order
- [ ] High-level entrypoints decode payloads into frozen dataclasses
- [ ] Custom resources implement `read`/`label`/`probe`/`load` as frozen dataclasses
- [ ] Resource instances are module-level singletons
- [ ] Uncertain resolution cases return conservative outcomes, not optimistic reuse
- [ ] Dynamic or unsupported cases call `db.report_untracked_read(reason)`
- [ ] Recursive traversal uses canonical visited sets and root containment checks
- [ ] `@query(cutoff=fn)` is used where semantic equivalence is cheaper than full comparison
- [ ] `__all__` lists only stable types and entrypoints
- [ ] `integrations/__init__.py` re-exports only the stable surface
- [ ] Contract lock test verifies `__all__` and non-export of experimental helpers
- [ ] Mode-parametrized correctness tests cover `strict`, `checked`, and `fast`
- [ ] From-scratch consistency test compares incremental vs fresh over edit sequences
