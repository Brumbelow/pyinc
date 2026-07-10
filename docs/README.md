# pyinc documentation

Start with the project [README](../README.md) for the pitch and a runnable
example. These docs go deeper, in the order below — each builds on the ones
before it.

## Reading order

1. **[migration-v3.md](migration-v3.md)** — required state cleanup and API
   changes when upgrading from pyinc 2.x.
2. **[architecture.md](architecture.md)** — the map: how the kernel, the value
   membrane, the durable cache, integrations, and the consumer layers
   (`pyinc_tools`, `pyinc_codegen`) fit together, and where the boundaries are.
3. **[kernel-contract.md](kernel-contract.md)** — the guarantee
   (*from-scratch consistency*), the three conditions it depends on, the
   `strict` / `checked` / `fast` modes, and the documented limitations and
   escape hatches. Read this before relying on the cache for correctness.
4. **[action-contract.md](action-contract.md)** — the `@action` layer that turns
   query-derived *desired* artifacts into files on disk: atomic writes, the
   ownership ledger, tamper repair, and the dry-run `plan`.
5. **[integration-contract.md](integration-contract.md)** — the stable public
   API surface, integration by integration. What you may import and rely on.
6. **[integration-authoring.md](integration-authoring.md)** — the three-layer
   pattern for writing a new integration, with `python_source` as the worked
   template.
7. **[codegen-guide.md](codegen-guide.md)** — `pyinc_codegen`, the JSON-Schema →
   typed-Python compiler: a public-API-only consumer that shows dependency-
   decomposed file→file generation.
8. **[pyinc-tools-guide.md](pyinc-tools-guide.md)** — the `pyinc-tools` CLI and
   LSP server: capabilities, editor wiring, the overlay/mirror model, and the
   supported-vs.-not-yet feature table.

## By role

- **Using pyinc as a library** → README → kernel-contract → action-contract.
- **Writing an integration** → integration-authoring → integration-contract.
- **Wiring an editor / watcher** → pyinc-tools-guide.
- **Building a compiler on pyinc** → codegen-guide → action-contract.
