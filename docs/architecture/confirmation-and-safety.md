# Confirmation, safety classes, pipeline presets, and the opportunity records

**Part of:** the [architecture specification](README.md). Designs against R3, R5, FR 18, FR 24,
FR 25, FR 39, FR 42 to FR 46, and criteria 18, 19, 55 to 58, 60, 61, 64. Fixes the final
confirmation rules R3 outlined, closes idea-document question 17 (preset limits and record
migration), and gives the opportunity, qualification, draft, and handoff records their columns.
**Decisions:** A15 (preset versioning). The safety classes themselves are R3, accepted; this
document gives them their mechanism. See the [decision list](README.md#architecture-decisions).

## The six classes and what the dispatcher does with each

Every operation and every tool declares exactly one (FR 24); registration refuses otherwise
([module contract](module-contract.md#operations-tools-events)). The service registry's
dispatcher applies the class before the handler runs:

| Class | Dispatcher behaviour | Audit | Standing grant may cover |
| --- | --- | --- | --- |
| `READ` | Authorization check, run. | No | Yes |
| `DRAFT` | Authorization check, run. The result is a record; nothing leaves. | Yes | Yes |
| `MUTATE` | Authorization check, run inside a unit of work with an audit row. | Yes | Yes |
| `DESTRUCTIVE` | Requires an `approval` in state `approved` bound to this exact call; otherwise returns `approval_required`. Guards rerun at execution. | Yes | Never |
| `EXTERNAL` | As destructive, and the effect is an `external_action` executed by a job with its own outcome record. | Yes | Never |
| `FINANCIAL` | As external, and the approval's `payload_ref` must contain `amount`, `currency`, and `payee_ref`, which the interface restates verbatim at the point of confirmation. | Yes | Never |

Workspace policy may tighten (a workspace may require confirmation for a named mutate
operation via `approvals.confirm_operations`, a list under a `union` floor that can only
grow) and can never loosen (the three upper classes cannot be moved to a standing grant by any
setting; the class-to-behaviour table is code, not configuration). `approvals.confirm_operations`
is not a declared setting yet, so no lower-class call requires confirmation today.

The external half of the table is not built yet either. No core migration creates
`external_action`, no production operation is external or financial class, and the one
external-class registration, the test harness's recording sink, executes inside
`core.approval.approve`'s transaction exactly as a destructive operation does. The financial
`payload_ref` check is not written.

## The approval record

`core.approval`, one row per confirmation requirement:

| Column | Meaning |
| --- | --- |
| `id uuid` | Returned in the `approval_required` state. |
| `actor_kind`, `actor_id` | Who requested (the actor whose call was gated). |
| `operation_name`, `operation_id` | What. |
| `destination_ref text null` | Recipient or destination: a party reference, a connector destination, the recording sink. Null only for destructive operations on a record, where `subject_ref` is the binding. |
| `subject_ref text null`, `subject_revision integer null` | The record acted on, and its `RecordHead.revision` at the moment the approval was created, which `RecordStateGuard` compares at execution ([guards](#execution-guards)). |
| `payload_digest bytea` | SHA-256 of the canonical JSON of the operation input as it will execute. |
| `payload_ref` | The `core.approval_payload(approval_id pk, body jsonb, byte_length integer)` row holding the snapshot, so the interface can show what is being approved. `body` is never queried and is bounded by `approvals.max_payload_bytes` (default 65536). The row is deleted when its approval is invalidated by the [deletion cascade](deletion-export-migration.md#the-cascade), because a snapshot of a request about a deleted record is a copy of that record. |
| `purpose text` | From the purpose vocabulary. |
| `window_start`, `window_end` | Execution window. Default length `approvals.default_window_seconds` 900; maximum `approvals.max_window_seconds` 86400, floor `min`. |
| `state` | `pending`, `approved`, `executed`, `expired`, `invalidated`, `refused`, `requires_reapproval`. The approval path writes `pending`, `approved`, `executed`, and `refused`; restore writes `requires_reapproval`; nothing writes `expired` or `invalidated` yet. |
| `approved_by_kind`, `approved_by_id`, `approved_at`, `approved_entry` | Who approved and through which entry: `web` in release one, `channel` from phase six; never `mcp`, `api`, or `cli`, because no token carries the approval operation. |
| `executed_at`, `invalidated_reason text null` | |

**The binding tuple** (R3 item 3) is `(actor, workspace, operation_name, destination_ref or
subject_ref, payload_digest, purpose, window)`. Execution recomputes the digest from the payload
it is about to run and compares; a differing byte, a differing destination, or a time outside the
window refuses with `invalid_approval` and records nothing at the destination (criterion 60). The
workspace is implicit in which database the row lives in.

**Who may approve.** An authenticated account holding a role the operation allows, through the
web interface. `core.approval.approve` and `.refuse` are non-token-issuable: no `cli`, `mcp`, or
`runtime` token's operation set can contain them, enforced at issuance and again at presentation
([tokens](identity-and-topology.md#what-a-token-can-never-carry-and-what-it-holds-for-a-gated-operation)),
so a model cannot approve what it proposed and a person approves from a session, never from a
credential that could sit in an agent's environment. Untrusted evidence never approves: no path
reads a payload, a memory, or a message to set `state = approved` (FR 25, R3 item 8), and
criterion 59's instructing text has no operation to reach.

**Flow.**

1. A gated operation is called. The dispatcher creates the approval `pending`, the operation
   record `approval_required`, and returns both ids. Nothing else happens; a headless caller
   gets the state within the deadline and the call ends (criterion 19).
2. A person approves: `core.approval.approve(approval_id)`, a mutate-class operation with its own
   audit row. State `approved`.
3. Execution: for destructive, the dispatcher runs the original operation now, with the approval
   id, inside the approve operation's own transaction; for external and financial, it enqueues
   the `external_action` job (not built yet, see above). Either path reruns the guards first.
4. On success, `executed`; on a guard failure, `refused` with the guard named. Nothing moves an
   approval to `expired` yet: one past its window stays `pending`, and approving it refuses
   `invalid_approval` from the binding check.

**Superseded in run 1a1: the executed operation now runs as the held caller.**
`rheo_core.approvals.gate.execute_approved` rebuilds the original caller's context
(`context_for_approved_execution`, from the approval's `actor_*` fields and the held operation
record's audience and entry), and the handler and the gated operation's audit row both run under
it. The approver's act is the separate `core.approval.approve` audit row, plus `approved_by_*`
and `approved_entry` on the approval. The four paragraphs below record the release-one decision
that change reversed and no longer describe the code.

**The executed operation runs under the approver's identity. Decided, not defaulted.** Step 3
says the dispatcher "runs the original operation now" without naming a context; the context it
uses is the **approving** actor's, for both the handler and the audit row it writes. So
`core.audit.list` reports the approver as the actor of the destructive effect, with `entry` set
to however the approver arrived.

The reasoning is that nothing is lost by it. The gated actor survives in two places — the
`approval_required` audit row written when the call was held, and `core.approval`'s
`actor_kind`/`actor_id` sitting beside `approved_by_kind`/`approved_by_id` — so either row leads
to the other, and a reader asking "who asked for this, and who let it happen" can answer both
from the record. The alternative was weighed rather than skipped: attributing the effect to the
gated actor would require the execution path to rebuild a context for an actor who is not the one
calling, and that cost was judged not worth paying for release one.

**Two ratified facts pull the other way. They were weighed and do not overturn it.** The
[`ActorPermissionGuard` row](#execution-guards) distinguishes the *gated* actor from the
*approving* actor and says "the two differ for a headless token call approved by a person" —
this design's central case; and the approval record restricts `approved_entry` to `web` in
release one, so attributing the effect to the approver makes every gated call's success row read
`entry = web` however the original call arrived. Both are real costs and both are consequences a
reader should expect, which is why they stay written down here. Neither loses information the
approval record does not already hold, and the second is bounded by a release-one restriction
rather than by this choice.

What would reopen it is a change in what the attribution *costs*: once a module's own records
carry `created_by_id` columns stamped from the executing context, the approver's identity starts
propagating into domain data rather than staying in the audit trail, and that is a different
question from the one settled here.

## Standing grants

| Table | Columns | Notes |
| --- | --- | --- |
| `core.standing_grant` | `id`, `actor_kind`, `actor_id`, `granted_by_id`, `created_at`, `expires_at`, `revoked_at null` | One grant per actor and period. |
| `core.standing_grant_operation` | `grant_id`, `operation_name text` | Primary key both columns. The operations the grant covers, written from the explicit list `core.standing_grant.create` receives. Every listed operation must be of class `read`, `draft`, or `mutate`; a destructive, external, or financial operation is refused at grant time naming it (criterion 19, R3 item 5). |

A standing grant lets a token or session run the listed operations without a per-call
confirmation the workspace would otherwise require through `approvals.confirm_operations`. It
never satisfies destructive, external, or financial: the dispatcher does not consult grants for
those classes at all. Because that key is not built, nothing consults a grant yet; the two
operations record, revoke, and report what a grant covers. `core.standing_grant.create` and
`.revoke` are non-token-issuable, so a grant is always a web session's act.

## Execution guards

Guards are rechecks run at execution time, after approval, before effect (R3 item 4). The core
attaches its guards to every destructive, external, and financial operation at registration;
a module adds domain guards through the operation declaration, and there is no minimum number
it must add, because the core's are always present.

| Guard | Attached to | Refuses when |
| --- | --- | --- |
| `ActorPermissionGuard` | every upper-class operation, by the core | Either actor no longer permits the operation: the **gated actor** (`approval.actor_*`, whose call was held) has lost the membership, role, or token that permitted it, or the **approving actor** (`approved_by_*`) has lost the membership or role that let it approve. The two differ for a headless token call approved by a person, and R3 item 4 wants a revocation of either to bite. |
| `WindowGuard` | every upper-class operation, by the core | Outside `window_start` to `window_end`. |
| `RecordStateGuard` | every upper-class operation whose input names a `subject_ref`, by the core, calling the owning module's resolver | The subject resolves to `deleted` or `unavailable`, or its `RecordHead.revision` ([identifiers](identifiers.md#resolution-under-permission)) differs from `approval.subject_revision`. A mutable type's `revision` is advanced only by a compare-and-set write ([storage adapter seam](storage-and-workspaces.md#revision-is-a-compare-and-set-not-a-counter)), never by a bare increment; an immutable type (an observation, a receipt) reports the constant `1`, so the guard on it can only fail by deletion. |
| `ContactPermissionGuard` | external operations with a party destination, by Leads | `leads.contact_permission.check(destination party, purpose, channel)` returns anything but `permitted` (criterion 61). |
| `DestinationGuard` | external operations that execute a handoff or send through a connector, by the connector owning the destination | The destination is not registered, or the connector's credential is unavailable. Release one has no such operation outside the test harness: `leads.handoff.request` is mutate class and records `unavailable` itself ([handoffs](#opportunities-parties-qualifications-and-handoffs)), and the guard's first production use is the phase-five execution operation. |

Only the first three are built (`rheo_core.approvals.guards`). `ContactPermissionGuard` and
`DestinationGuard` are domain guards that arrive with Leads and the first connector.

A refused guard leaves the external action `refused`, the operation `failed` with the guard's
code, and the sink or provider untouched. An earlier approval never overrides a later revocation
because the approval only gates whether execution may *start*; the guards decide whether it may
*proceed*.

## The recording sink and the destructive fixture

Test-harness registrations, never in the production set (criterion 18). The sink is an
external-class operation whose handler appends what it would have sent to a
harness table and sends nothing. The fixture is a destructive-class operation
whose handler records the request and deletes nothing. Both exist so the approval binding, the
guards, the headless state, the export of a pending approval, and the deletion cascade's
cancellation of a dependent action have a real gated operation to run against (criteria 19, 21,
60, 61, 65, 67).

**They ship as `harness.sink.send` and `harness.fixture.act`, not under the `test.` prefix this
section first gave them.** An operation name's first segment is its module id, and a registration
under the `test_harness` origin may claim only the module id `harness` ([module
contract](module-contract.md#operations-tools-events)). A `test.` prefix would have to register under
an origin named `test`, which is not the test-harness origin and so is not gated to
`profile = test` — the gate criterion 18's production-registration assertion rests on. The prefix
moved because the profile gate is the part that carries a criterion.

## Pipeline presets and their limits

Answers idea-document question 17: what a preset can configure, and how records move when it
changes. Leads is phase-three work and not built yet (`modules/leads` is a placeholder), so
everything from here to the end of this document is design.

### Entities

All in the `leads` schema.

| Table | Columns | Notes |
| --- | --- | --- |
| `pipeline_preset` | `id uuid`, `name`, `current_version integer`, `created_at` | A named preset. Release one ships one: inbound services. |
| `preset_version` | `preset_id`, `version integer`, `created_at`, `created_by_id`, `note text` | Primary key `(preset_id, version)`. Immutable once created. |
| `preset_stage` | `preset_id`, `version`, `stage_id slug`, `label text`, `ordinal`, `kind text`, `outcome text null` | `kind` in `open`, `terminal`. Terminal stages map to `outcome` in `succeeded`, `lost`, `disqualified`. `stage_id` is stable across versions; `label` is free. |
| `preset_transition` | `preset_id`, `version`, `from_stage_id`, `to_stage_id` | Allowed moves. |
| `preset_requirement` | `preset_id`, `version`, `to_stage_id`, `field text` | Fields that must hold a value before entering `to_stage_id`. Presence only; no expressions. |
| `preset_field` | `preset_id`, `version`, `target text`, `type text`, `required boolean`, `sensitivity text` | Extension fields `ext.<namespace>.<field>` with `type` from `text`, `integer`, `decimal`, `date`, `boolean`, `enum(values)`. |
| `preset_rubric` | `preset_id`, `version`, `rubric_slug text`, `rubric_version integer` | The qualification rubric this version uses, named from the package rubrics ([below](#opportunities-parties-qualifications-and-handoffs)). |
| `preset_template` | `preset_id`, `version`, `kind text`, `body text` | Draft templates with named placeholders; `kind` in `followup` only. Phase four adds `proposal` in a Leads migration when the drafting screens arrive. |
| `pipeline` | `id uuid`, `preset_id`, `name`, `owner_id`, `created_at` | A working instance. New opportunities enter it at the preset's `current_version`. |

Handoff destinations are not preset configuration in release one: there are none, and the
phase-five destination is registered by the destination module, not chosen per preset. A
`preset_handoff` table arrives with phase five if a preset then needs to name one.

**Seeded at Leads enable.** The enable step writes, when absent: the package preset
`inbound_services` as `pipeline_preset` plus `preset_version 1` with its stages, transitions,
requirements, fields, `preset_rubric` pin (`inbound_services`, 1), and `followup` template, from
`modules/leads/presets/inbound_services/1.toml`; one `pipeline` named `inbound` on it, owned by
the workspace's owner account; and the manual-capture connection with its mapping
([intake](intake-and-events.md#transports)). Rubrics are package data and need no seeding. A
funnel is not seeded, because criterion 40 wants a capture with no funnel refused and the owner
to name the first one; the demo fixtures under `examples/` create theirs.

Opportunities and qualifications pin `preset_version` at creation; their full rows are in
[the opportunity records](#opportunities-parties-qualifications-and-handoffs).

### What a preset can configure

Stages, their labels, order, and terminal outcomes; allowed transitions; presence requirements per
transition; extension fields from the closed type list with a sensitivity tier; the rubric
slug and version; draft templates. That is the whole surface. A preset cannot: run scripts or expressions; write another module's records; add
a core column; change a safety class or a permission; name a runtime, model, or credential; or
define a new purpose. Unusual behaviour goes in a module, as the idea document directs.

### Editing and pinning (FR 43)

Editing a preset creates `preset_version n+1` by copying `n` and applying the change, then sets
`current_version`. Rows of earlier versions are never modified. An opportunity created under
version `n` keeps `preset_version = n`; its stages, transitions, requirements, and field schema
are read from version `n` for as long as it lives (criterion 57). Renaming a stage's label changes
`preset_stage.label` in the new version only, and the opportunity's `stage_id` reference is a
slug that the rename does not touch.

**Labels are the one thing read from the current version.** When the interface or a tool
displays an opportunity's stage, the label is looked up in the preset's `current_version` by
`stage_id`, falling back to the pinned version's label when the current version no longer has
that stage. Everything with meaning (transitions, requirements, field schema, terminal outcome)
comes from the pinned version. A rename is therefore visible everywhere at once, which is what a
rename is for, and criterion 57 still holds because it asserts identifiers and semantics, not
display text.

### The migration operation

`leads.pipeline.migrate_version(pipeline_id, opportunity_ids, to_version, stage_map)`, mutate
class, owner only, audited per opportunity:

- `stage_map` maps every `stage_id` in use among the selected opportunities to a `stage_id` in
  `to_version`; a missing entry refuses the whole call naming the stage.
- Extension fields present in the old version and absent in the new are kept in
  `opportunity_field_state` with `orphaned = true`, readable and exportable, not writable.
- A field required in the new version and empty on an opportunity does not block the migration;
  the next transition into a stage that requires it does.
- Each migrated opportunity's `preset_version` is updated and an `leads.opportunity.migrated`
  event is published with both versions.

Qualification records are never migrated; they record the version and rubric they were made
under, and a new assessment under the new rubric is a new record.

## Opportunities, parties, qualifications, and handoffs

The Leads module's back half, at column level. All in the `leads` schema; every table has
`id uuid` (UUIDv7) unless noted. The front half (connections, receipts, observations, field
state) is in [intake](intake-and-events.md#entities).

| Table | Columns | Notes |
| --- | --- | --- |
| `opportunity` | `pipeline_id`, `preset_id`, `preset_version integer`, `stage_id text`, `title text`, `owner_id uuid`, `funnel_id`, `campaign_id null`, `source_observation_id`, `value_amount numeric null`, `value_currency text null`, `value_basis text null`, `disposition_outcome text null`, `disposition_at null`, `revision integer`, `created_by_kind`, `created_by_id`, `created_at`, `updated_at` | FR 42 in columns: pipeline identity and pinned version; a stable `stage_id` separate from its editable label; an owner (an account; the pipeline's `owner_id` when routing creates the opportunity); provenance (funnel, campaign, the first observation, and every later one through `opportunity_observation`); a terminal disposition, set only by a transition into a `terminal` stage and mapped to the pinned version's `outcome` (`succeeded`, `lost`, `disqualified`), readable through `leads.opportunity.get` (criterion 56). `value_basis` in `project_fee`, `annual_contract`, `hourly_rate`, `referral_fee`. There is no other core column; every workflow-shaped value is an `ext.*` target in `opportunity_field_state` (FR 44, criterion 55). `revision` is advanced only by a compare-and-set write ([storage adapter seam](storage-and-workspaces.md#revision-is-a-compare-and-set-not-a-counter)), never by a bare increment, and is what `RecordStateGuard` and a qualification's `input_revision` compare. |
| `opportunity_party` | `opportunity_id`, `party_ref text`, `role text`, `added_at`, `added_by_kind`, `added_by_id`, `removed_at null` | Primary key `(opportunity_id, party_ref, role)`. `role` in `contact`, `organization`, `referrer`. `party_ref` is a reference string and is never rewritten; "the same party" is decided by `canonical_ref` from `relationships.party.get` ([relationships](relationships.md#merge-and-unmerge-r4-criterion-53)). `leads.on_party_deleted` sets `removed_at`. |
| `opportunity_observation` | `opportunity_id`, `observation_id`, `decision text`, `decided_by_kind`, `decided_by_id`, `decided_at` | Primary key `(opportunity_id, observation_id)`. FR 39's recorded decision that an observation supports an opportunity: `decision` is `routing_rule:<rule id>` or `manual`. One observation may appear under several opportunities, one row each. |
| `qualification` | `opportunity_id`, `preset_version integer`, `rubric_slug text`, `rubric_version integer`, `objective text`, `evidence_refs text[]`, `input_revision integer`, `fit text`, `intent text`, `evidence_completeness text`, `urgency text`, `explanation text`, `uncertainty text`, `model_id text null`, `prompt_version text null`, `created_by_kind`, `created_by_id`, `created_at` | A record of its own, never a column on the opportunity (criterion 58). The four dimensions are separate, each in `high`, `medium`, `low`, `needs_information`, with no combined score (idea document); `uncertainty` in `low`, `medium`, `high`. `input_revision` is the opportunity revision the assessment read. A later assessment never replaces an earlier one; the current view is the newest by `input_revision`, then `created_at`, and an assessment whose `input_revision` is older than the current view's is stored and never shown as current. `model_id` and `prompt_version` are set when a model participated. |
| `draft` | `opportunity_id`, `kind text`, `body text`, `template_version integer null`, `runtime_request_id uuid null`, `created_by_kind`, `created_by_id`, `created_at` | The draft class's result is a record (R3): `leads.followup.draft` writes one with `kind = followup`. Nothing leaves. |
| `handoff` | `opportunity_id`, `purpose text`, `destination_ref text null`, `idempotency_key text`, `snapshot_digest bytea`, `state text`, `result_ref text null`, `result_detail text null`, `approval_id uuid null`, `external_action_id uuid null`, `requested_by_kind`, `requested_by_id`, `requested_at`, `resolved_at null` | The six FR 45 and criterion 64 facts: durable identifier (`id`), source opportunity, purpose, destination, approved payload snapshot (`handoff_snapshot` plus `snapshot_digest`), and result (`state`, `result_ref`, `result_detail`). Unique `(opportunity_id, idempotency_key)`. `state` in `unavailable`, `pending`, `succeeded`, `failed`, `unresolved`, `cancelled`. Release one writes only `unavailable` with `result_detail = no_destination`; `approval_id` and `external_action_id` are filled by the phase-five execution. Several intentional handoffs from one opportunity are several rows with distinct purposes; there is no `converted` flag (idea document). |
| `handoff_snapshot` | `handoff_id pk`, `body jsonb`, `byte_length integer` | The payload as requested, never queried, bounded by `approvals.max_payload_bytes`. Its digest is what a phase-five approval binds. Removed with the opportunity. |
| `opportunity_note` | `opportunity_id`, `body text`, `stage_id text null`, `created_by_kind`, `created_by_id`, `created_at` | A note a person wrote on the opportunity. `leads.opportunity.transition` writes one when its `note` argument is non-empty, with the `stage_id` it accompanied; `leads.opportunity.add_note` writes one without a stage. Intake never writes or reads this table, which is how criterion 49's note survives any later observation: there is no rule under which a source could touch it. Removed with the opportunity. |

There is no stage-transition history table. Every transition is a mutate operation with the
opportunity as its audit subject, so `core.audit.list` filtered by the subject lists each
transition's actor, time, and request digest, and the text a person wrote at the transition is in
`opportunity_note` with its `stage_id`; the audit row holds a digest and never the note.

**Where a rubric lives.** A rubric is **package data referenced by slug and version**
(`modules/leads/rubrics/<slug>/<version>.toml`, listing the objective and the four dimensions'
criteria), not a workspace record. The reason is that the scoring code that interprets a rubric
ships in the package and changes with it, so a rubric's identity is a package fact, and release
one has exactly one (`inbound_services`, version 1) that no workspace edits. `preset_rubric`
pins the slug and version; the qualification result is a record either way, and a rubric that a
workspace could edit would arrive as a `leads.rubric` record type in a later migration without
changing `qualification`'s columns.

### The handoff operation (FR 45, criterion 64)

`leads.handoff.request(opportunity_ref, purpose, destination_ref null, payload,
idempotency_key)`, **mutate class**, `NATURAL` idempotency on the unique index
`(opportunity_id, idempotency_key)`, roles `owner`, `member`:

1. Resolve the opportunity under the caller's context; refuse `not_found` otherwise.
2. Look up `handoff` by `(opportunity_id, idempotency_key)`. A row exists: this is a repeat.
   Return that row unchanged when the request's payload digest equals its `snapshot_digest`, and
   refuse `idempotency_key_reused` naming the handoff when it differs. The natural key carries the
   opportunity, so the same key on a different opportunity is a different request and a different
   row, and no stored-result table is needed to make a repeat safe.
3. Otherwise write `handoff` and `handoff_snapshot` with the digest.
4. If `destination_ref` is null or names no registered destination (release one registers
   none), set `state = unavailable`, `result_detail = no_destination`, `resolved_at = now()`,
   publish `leads.handoff.requested`, and return the record with its explicit `unavailable`
   state. Nothing is created anywhere and no opportunity outcome changes (criterion 64,
   guardrail 20).
5. Otherwise (phase five onward) set `state = pending` and call the destination's execution
   operation, which is the external-class step with its own approval, `DestinationGuard`, and
   `external_action` record; its outcome is written back to `state`, `result_ref`, and
   `result_detail` through the operation record it returns, never by a cross-schema write.

The request is mutate class because writing the record is an internal, reversible state change
with no external effect; the external effect, when a destination exists, is the destination's
operation. That is what keeps criterion 18's production registration set free of external-class
operations while the handoff operation still exists and is callable.

### Operations and roles

| Operation | Class | Roles | Tool |
| --- | --- | --- | --- |
| `leads.opportunity.create_from_observation` | mutate | service, owner, member | none (routing calls it) |
| `leads.opportunity.get`, `.list`, `.search` | read | owner, member, service | `leads_get`, `leads_list`, `leads_search` |
| `leads.opportunity.update` (title, owner, value, extension fields; sets field `owner = user`) | mutate | owner, member | `leads_update` |
| `leads.opportunity.transition(ref, to_stage_id, note)` | mutate | owner, member | `leads_transition` |
| `leads.opportunity.add_note(ref, body)`, `.list_notes` | mutate, read | owner, member | none in release one |
| `leads.opportunity.attach_observation`, `.add_party`, `.remove_party` | mutate | owner, member | none in release one |
| `leads.qualification.assess` (long-running when a model participates), `.list` | mutate, read | owner, member, service | `leads_qualify` |
| `leads.followup.draft` | draft | owner, member, service | `leads_prepare_followup` |
| `leads.handoff.request`, `.get` | mutate, read | owner, member | `leads_handoff`, `leads_get_handoff` |
| `leads.pipeline.create`, `.migrate_version` | mutate | owner | none in release one |
| `leads.preset.edit` (creates the next version) | mutate | owner | none in release one |
| `leads.funnel.create`, `.archive`, `leads.campaign.create`, `.archive` | mutate | owner | none in release one |
| `leads.connection.create`, `.update_mapping`, `.set_routing`, `.rotate_secret`, `.revoke_secret`, `.revoke` | mutate | owner | none in release one |
| `leads.connection.health`, `.list` | read | owner | `leads_connection_health` |
| `leads.conflict.list(connection_ref, state)`, `.resolve(conflict_ref, note)` | read, mutate | owner | none in release one |
| `leads.import.run` | mutate, long-running | owner | none |
| `leads.intake.capture` | mutate | owner, member | `leads_capture` |
| `leads.intake.accept_delivery` | mutate; `AuditSpec(subject = the receipt)` | service, owner, member (called by the three connectors only: the webhook route as a `connection` actor, the import job as a `system` actor, and `leads.intake.capture` in the capturing person's context) | none |
| `leads.contact_permission.record`, `.withdraw`, `.suppress` | mutate | owner, member | none in release one |
| `leads.contact_permission.check` | read | owner, member, service | none |
| `core.record.delete` for `leads.observation`, `leads.opportunity` | destructive | owner | `leads_delete` |

Configuration (connections, mappings, routing, funnels, campaigns, presets, pipelines) is
`owner` only, and record work is `owner` or `member`, which is R1's split applied to Leads.
