---
name: Soundness violation
about: An incremental result differs from a fresh evaluation
title: ''
labels: soundness
---

**What differed**

The incremental result:

The fresh-database result on the same inputs and resources:

**Reproducer**

A minimal script. If it needs files on disk, please include their creation.

```python
```

**The three conditions**

Please confirm that each condition holds for the reproducer. See
[the kernel contract](https://github.com/Brumbelow/pyinc/blob/main/docs/kernel-contract.md#conditions-for-from-scratch-consistency).

- [ ] Owned value boundaries: every value crossing a boundary is snapshot-safe
      or has a registered `ValueAdapter`
- [ ] Tracked ambient reads: external state goes through a `Resource`, or is
      declared with `db.report_untracked_read(reason)`
- [ ] Deterministic queries: the same tracked dependencies produce a
      semantically equal result

**Diagnostics**

Output of `db.explain(<query>)` where the results diverge:

```
```

Output of `db.statistics()`:

```
```

**Environment**

- pyinc version:
- Python version and build (free-threaded? `python -VV`):
- OS:
- Mode (`strict` / `checked` / `fast`):
- Checkpoints or an artifact store involved?
