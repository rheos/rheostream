# Public contracts

`rheo_contracts`: the types modules and adapters share with the core. It holds the
operation and tool declarations a module manifest is built from, record references,
the event and observation envelopes, purposes, repository and source-unit interfaces,
the safety class names, and the runtime adapter contract. The manifest type itself
lives in `rheo_core.modules.manifest`; the
[module contract](../../docs/architecture/module-contract.md) describes it.

Define behavior, ownership, authorization, compatibility, and failure semantics
before changing a published contract.
