# Public contracts

`rheo_contracts`: the types modules and adapters share with the core. It holds the
`WorkspaceContext` type every boundary resolves, the operation and tool declarations a
module manifest is built from, record references, the event and observation envelopes,
purposes, repository and source-unit interfaces, the safety class names, the runtime
adapter contract, and the separate typed-judgment contract (`judgment`) that tool-free
providers such as Jev answer. The manifest type itself
lives in `rheo_core.modules.manifest`; the
[module contract](../../docs/architecture/module-contract.md) describes it.

Define behavior, ownership, authorization, compatibility, and failure semantics
before changing a published contract.
