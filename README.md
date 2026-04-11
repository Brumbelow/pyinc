# pyfoundinc

`pyfoundinc` is a correctness-first incremental computation engine for Python: a Python-native query kernel in the design space of Salsa, Jane Street Incremental, and Bazel/Skyframe.

Current scope:

- `@query` for derived values
- `Input` for explicit base leaves
- `ValueAdapter` for custom snapshot-safe boundary types
- `FileResource`, `EnvResource`, and `DirectoryResource` for explicit external reads
- pull-based recomputation with revisions, dependency capture, red-green verification, and backdating
- `strict`, `checked`, and `fast` execution modes with explicit boundary semantics
- explanation output for reuse vs recompute decisions

The core contract is intentionally narrow: values crossing cached boundaries must be snapshot-safe, and hidden reads are treated as correctness violations rather than “best effort” cache misses.

Development:

```bash
python -m pip install -e '.[dev]'
pytest
```

The runtime contract is summarized in [docs/kernel-contract.md](docs/kernel-contract.md).
