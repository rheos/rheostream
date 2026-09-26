# Framework core

`rheo_core`, in Python: workspace identity, sessions and tokens, routing, settings
and secrets, policy enforcement and redaction, module registration, install and
enable, durable work and events, operations and audit, export and restore, and
deletion. Storage is Postgres, one database per workspace
([architecture](../../docs/architecture/storage-and-workspaces.md)).

The core must remain independent of professions and optional domain modules.
Modules request scoped private storage; they do not write user data into source
packages.
