# Framework core

`rheo_core`, in Python: workspace identity, sessions and tokens, routing, settings
and secrets, policy enforcement and redaction, approvals and standing grants, module
registration, install, enable and upgrade, durable work and events, operations and
audit, record references and short IDs, export and restore, and deletion. It also
holds the OAuth front door for MCP connector clients, the connector code (the signed
webhook receiver, plus an outbound OAuth client and an allowlisted read-only MCP
client that both send through one bounded egress function), the automatic-memory
evidence path, and runtime dispatch with the typed-judgment runner. Storage is Postgres, one database per workspace
([architecture](../../docs/architecture/storage-and-workspaces.md)).

The core must remain independent of professions and optional domain modules.
Modules request scoped private storage; they do not write user data into source
packages.
