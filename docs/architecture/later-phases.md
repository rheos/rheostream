# Later phases: architectural sketches

**Part of:** the [architecture specification](README.md). One page per public phase after release
one, written to show that the release-one decisions do not foreclose them. These are sketches;
each phase is planned when its predecessor lands, as the [build plan](../requirements/build-plan.md)
says.

**What has landed.** Phases 4 and 5 are built; each section below opens with a **Built** note
saying what shipped, where it lives, and where the build differs from the sketch. Phases 6 to 8
are still sketches. One module arrived outside this list: Finance
([`modules/finance/`](../../modules/finance/README.md)), a personal ledger and reminder module
that core loads like any other. Its eight read operations are owner-only and
`package_sets = "explicit_only"`, so only a `module_read:finance` token reaches them, and its
`tick` schedule enqueues only once a workspace sets `finance.jobs.enabled` to true. It is not
the invoicing or accounting product phase 5 rules out.

## Phase 4: the optional job-search workflow

**Built.** Job search is a capability inside Leads, documented in the
[Leads README](../../modules/leads/README.md#job-search-capability). Migration
`0007_jobsearch_preset` seeds the Job search preset with sixteen `ext.jobsearch.*` fields, and
`leads.capabilities.jobsearch` gates it. Jobs arrive three ways, all through ordinary intake
and all converging on one opportunity per posting id: the job feed collector, which calls a
provider's remote MCP server on a schedule (`leads.jobfeed.presets` replaces its default
searches, each run writes one content-free `leads.jobfeed_run` row, and invited jobs carry
`ext.jobsearch.invited`); pasted search pages and full postings (`leads.jobfeed.paste`); and
the other release-one transports. On top sit job scoring (a deterministic tier-one filter, an
optional Jev pre-filter, then the model score), the review queue with owner-only
`leads.jobreview.promote`, proposal drafting with its answer library, and pricing with
milestones. Where the build differs from the sketch: proposals have their own tables
(`job_proposal`, `proposal_section`, `answer_library`, `job_pricing`, `proposal_milestone`)
rather than a `proposal` kind in `preset_template`, and no email poller or
`intake_connection.cursor` column was built, because the first job source is the MCP
collector.

**What it adds.** A capability bundle, not a module: pipeline presets for job search, extension
fields under `ext.jobsearch.*` (candidate fit, application questions), job-source connectors, the
multi-observation enrichment where an email alert, a copied search result, and a full posting
converge on one opportunity, and the proposal drafting and answer-library screens. The
`proposal` template kind joins `preset_template` here, in a Leads migration; release one carries
`followup` only.

**How it fits.** Every job-shaped field is a `preset_field` with `ext.jobsearch.` targets, so the
Leads core gains no column (FR 44). Connectors implement the same `Delivery` contract as the three
release-one transports; a job board is a connection with a mapping whose `subject_id_path` yields
the provider's posting identifier, which the routing action `attach_to_open_opportunity` uses to
enrich rather than duplicate. The field-derivation rule already handles thin-then-rich
(completeness) and the provisional score is a qualification with `uncertainty` high until a full
posting arrives. The email transport is a deterministic poller: a connector with a durable cursor
stored on the connection (`intake_connection` gains a `cursor` column in a Leads migration), no
model involved. A workspace setting `leads.capabilities.jobsearch` gates scheduling of its
workers and visibility of its screens; records stay readable and exportable with it off.

**What it must not do.** Add a core column, a provider-specific branch in the intake path, or a
second safety class. Provider licensing is reviewed per source before any hosted use.

## Phase 5: the work-in-motion module

**Built.** Current is in [`modules/current/`](../../modules/current/README.md) and the handoff
works as sketched: `leads.handoff.request` stages `current.item.create_external`, which
creates one standalone Current item (a project is set later, only through Current's own
editor), and `current.source_item_link` keeps the source-to-item-to-project relation. Keyed
idempotency arrived with it: `Idempotency.KEYED`, the `core.idempotency_result` table (core
migration `0014_external_actions`), and the `work.idempotency_retention_days` setting
(default 90, never below 30). Criterion 18's check became an explicit allow list,
`PRODUCTION_EXTERNAL_ALLOWLIST` in `rheo_core/modules/production.py`, whose one entry is
`current.item.create_external`. No `preset_handoff` table was needed: Current is the only
destination.

**What it adds.** `current`: projects, tasks, next actions, status, priority, due dates,
dependencies, commitments. Ported from the predecessor's Next.js screens and moved from a local
SQLite file to the `current` schema of the workspace database.

**How it fits.** A manifest like the others; `current` requires nothing and `leads` declares it
optional. The handoff operation from phase three
([handoffs](confirmation-and-safety.md#the-handoff-operation-fr-45-criterion-64)) gains its first
registered destination: `leads.handoff.request` writes the record `pending` instead of
`unavailable` and stages `current.item.create_external`, the `EXTERNAL`-class execution with
its own approval, `DestinationGuard`, and `external_action` record. Current keeps the lasting
source-to-item-to-project relation itself: one `current.source_item_link` row per approved
source that survives item deletion as a tombstone, updated only by `current.item.update` and
approved item or project deletion, read by `current.item.link_state` and announced by one
`current.source_item_link.changed` event per change
([ADR-0001](../adr/0001-leads-current-project-link.md),
[Current](../../modules/current/README.md)). The external create is the first operation in the
system that must return a *created* reference on a retry whose natural key does not already carry the answer, so **keyed idempotency arrives
here**: the `Idempotency` enum gains `KEYED(field)` (additive within contract version 1), the
core gains `core.idempotency_result(operation_name, key, output, recorded_at, retained_until)`
with its retention setting in a core migration, and the dispatcher stores and returns the output
on a repeat. Release one carries none of it, because every release-one operation that can be
retried is idempotent on a unique index it already has
([idempotency](intake-and-events.md#idempotency)). Leads writes the outcome into
`handoff.state` and `result_ref` from the operation record and displays the work's state through
the record resolver, never a second editable copy. If a preset then needs to name a destination,
a `preset_handoff(preset_id, version, destination_ref)` table arrives in the same Leads
migration; release one ships neither. Completing a work item publishes `current.item.completed`,
which no other module may treat as a domain outcome.

**A CI assertion changes here.** Criterion 18's production-profile check asserts, through phase
three, that the registration set contains no external-class or financial-class operation. The
destination execution is the first production external-class operation, so the phase-five plan
must rewrite that assertion (to an explicit allow list of destination operations, or to an
assertion over the registered destination set) rather than weaken it silently; the
[overview](overview.md#known-tensions) records the tension so it is not mistaken for an
oversight.

**What it must not do.** Become invoicing or accounting; those remain the external back-office
integration's.

## Phase 6: channels and the second runtime

**Not built.** `channels/` holds only a placeholder package, and the OpenRouter and Codex CLI
adapters do not exist. The Jev provider in `runtimes/` (`rheo_runtimes.jev`) is not the second
runtime: it is a tool-free typed-judgment call that Leads scoring uses as a pre-filter, with no
agent loop ([runtimes](../../runtimes/README.md#jev-typed-judgments)).

**What it adds.** The OpenRouter adapter ([shape](runtime-and-mcp.md#the-openrouter-adapter-phase-six-shape-only))
and a Telegram channel.

**How it fits.** The adapter satisfies the same discovery and yields the same events; criterion
15's gate and the run-scoped token are unchanged. A channel is a fifth boundary adapter: it
resolves an enrolled account through an authenticated enrollment record
(`control.channel_enrollment(channel, external_conversation_id, account_id, workspace_id,
audience_kind)`), builds a `WorkspaceContext` with `audience = channel:<conversation>`, and calls
the same services. Group conversations and private conversations are different audiences, so a
runtime session bound to one is never resumed for the other. Approval requests in a channel
return the `approval_required` state as a message with the approval id; approval itself still
happens through the web interface, or through a channel-specific confirmation that is itself an
authenticated action by the enrolled account (not a token) and is recorded with `approved_entry =
channel`. Revocation of an enrollment revokes queued work bound to that audience.

**What it must not do.** Give a channel any operation a web session lacks, or resume a global
latest session. Codex CLI stays a validation target: if its adapter is attempted, it passes the
same capability gate and contract suite before it is called supported, and no phase schedules it.

## Phase 7: framework proof and a v0.1 release

**Not built.** Core's module operations are `core.module.install`, `core.module.enable` and
`core.module.upgrade`; disable, remove, purge and restore do not exist. `packs/` holds one
placeholder directory with no pack schema, and there is no invitation flow. The licence is
settled (AGPL-3.0, see the build plan).

**What it adds.** The rest of the module lifecycle (disable, re-enable, remove, purge, restore)
as written in the [module contract](module-contract.md#upgrade-disable-re-enable-remove-purge-restore-on-paper-d5);
a synthetic specialist module registering records, tools, screens, jobs, migrations, and an
export through the public extension points; domain packs; the invitation flow and self-service
membership; the licence.

**How it fits.** The lifecycle operations act on `core.module_state`, whose state set already
includes `disabled` and `removed`. The registration epoch the module contract names is not built
yet; the tool facade already re-checks a tool's origin module on every listing and call. The synthetic module is a distribution with a manifest and no core change, which
is exactly what criterion 24 already proved for the memory module. Domain packs are data:
a versioned bundle naming modules, presets, mappings, and vocabulary, applied by an operation that
produces an inspectable plan and refuses to overwrite a workspace setting it did not set; the
settings rows gain a `set_by_pack` column for that. Invitations are a control-plane state machine
(`control.invitation`) with an email delivery dependency through a connector, and FR 6 gains its
test with a second member present. Named capability declarations layer over the manifest
(`provides`), giving the capability registry the requirements kept out of release one.

**What it must not do.** Turn the synthetic module into a product for another profession.

## Phase 8: the hosted edition

**Not planned in detail**, as the build plan says. What release one leaves open for it, and what
it leaves settled:

- **Tenancy scheme.** Module subdomains versus per-tenant subdomains, decided deliberately
  ([constraint](identity-and-topology.md#constraint-carried-to-the-hosted-edition)). The routing
  configuration is the single place hosts are named, so either answer is configuration plus a
  per-tenant resolver at the boundary.
- **Storage scale.** Database-per-workspace on one cluster has a ceiling; the hosted edition
  decides between more clusters and a different scheme behind the same seam. A second cluster
  arrives as a `control.cluster(cluster_ref, dsn_secret_ref, state)` table and a
  `workspace.cluster_ref` column in a control-plane migration, replacing the single
  `storage.cluster_dsn_ref` setting release one routes by
  ([storage](storage-and-workspaces.md#the-control-plane)). Pool caching and serial migration
  become real engineering here.
- **Credentials.** Service-owned model credentials through an API adapter; never a customer's CLI
  subscription (idea document). The OpenRouter adapter from phase six is the candidate default.
- **Encryption and keys.** Per-workspace encryption at rest and a key-management design with a
  threat model and a recovery procedure are prerequisites, not additions; the secret store's
  backend protocol is where a managed secret manager plugs in.
- **Billing.** Metadata joins the control plane here (D1, FR 7).
- **Operations.** Backups with point-in-time recovery, replica coordination against one database,
  cross-tenant metrics that leak no tenant data, and abuse controls for any public capture
  endpoint (a separate, limited contract, never the privileged intake API).
- **Provider licensing** for every job source before it is enabled in a public service.
