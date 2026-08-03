---
name: Feature request
about: Propose a capability or an API addition
title: ''
labels: enhancement
---

**The problem**

What are you trying to build, and where does pyinc make it awkward today?

**What you have tried**

Existing API, escape hatches, or workarounds you have already used.

**Proposal**

**Which layer does this belong to?**

The kernel stays domain-agnostic; consumer concerns live in `pyinc_tools` and
`pyinc_codegen`. See
[the architecture overview](https://github.com/Brumbelow/pyinc/blob/main/docs/architecture.md).

- [ ] `pyinc` kernel — this would widen the kernel contract
- [ ] `pyinc.integrations` — a new or extended integration
- [ ] `pyinc_tools` — CLI, LSP, or watcher
- [ ] `pyinc_codegen` — JSON Schema to Python
- [ ] Not sure

If this widens the kernel contract, what is the trade-off you would accept?
