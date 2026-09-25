# Deletion, export and restore, and migration verification

**Part of:** the [architecture specification](README.md). Designs against R5, FR 28, FR 46,
FR 51 to FR 53, and criteria 21, 29, 32, 36, 65, 67. Also answers the disaster-recovery half of
idea-document question 13 for release one.
**Decision:** A14 (deletion coordinator with synchronous participants). See the
[decision list](README.md#architecture-decisions).

## Record-level deletion (R5, FR 51)

### The operation

`core.record.delete(ref)`, destructive class, per-action confirmation, no standing grant. Any
module can call it for a record type whose manifest marks `deletable = true`; release one has
four: the three R5 types (`leads.observation`, `relationships.party`, `leads.opportunity`) and
`recallatron.memory`, whose `recallatron_forget` tool is this same coordinator. Only
`recallatron.memory` is built so far; the other three arrive with Leads and Relationships.
Who may call it per type is the manifest's `delete_roles`
([module contract](module-contract.md#operations-tools-events)).

### The cascade

The coordinator runs the whole cascade in **one transaction** against the workspace database,
which the one-database-per-workspace layout makes possible without a distributed step. Order:

1. **Guards.** The approval is rechecked (window, both actors); the record must still exist at
   the approved revision.
2. **Owning module.** The owner's registered `delete(ref)` removes the record and every row it
   owns for that record and returns the set of references it removed:
   - an observation: its fields and payload, its `opportunity_observation` rows, and the
     `opportunity_field_state` rows that name it as their evidence, after which Leads re-derives
     each affected field from the opportunity's remaining observations in the same transaction
     ([field derivation](intake-and-events.md#field-derivation-fr-37)), so no value whose only
     evidence was the erased observation stays on an opportunity;
   - an opportunity: its field state, notes, observation links, party links, qualifications,
     drafts, handoffs and snapshots;
   - a party: its contact points and affiliations, and the free-text `evidence` of every
     `merge_record` that names it as survivor or merged (set null; `evidence_refs` stay), because
     a reviewer's note about why two records were one person can restate that person
     ([relationships](relationships.md#deletion-r5));
   - a memory: its purposes, embeddings, links, and mentions.

   That set is normally the one reference; a party that is a merge survivor also returns its
   alias rows, which it removes with it ([relationships](relationships.md#deletion-r5)). Steps 3
   to 6 run once per reference in the set.
3. **Deletion participants.** Every module enabled in the workspace that declared a
   `DeletionParticipant` for the type runs its handler in the same transaction:
   - `recallatron.on_record_deleted`: run the memory module's one invalidation rule with reason
     `source_deleted`, which removes every memory linked to the reference and every memory
     derived from those, with their embeddings and index entries
     ([memory](memory.md#correction-and-supersession-fr-28-criterion-29); FR 28, criterion 29).
   - `leads.on_party_deleted`: null `observation.party_ref` for every observation that named the
     reference, so they remain as unlinked evidence (R5); set `opportunity_party.removed_at`
     for every link that named it.
   - `leads.on_observation_deleted`: keep the `delivery_receipt` row; delete its
     `delivery_payload`; null `body` on every `delivery_conflict` row of that receipt; set the
     receipt's `state_detail = observation_deleted`.
4. **Core participants.** Queued jobs and pending external actions with
   `depends_on_ref = ref` (or whose destination is the deleted party) are set `cancelled`
   (criterion 65). Approvals bound to the reference go `invalidated` and their
   `approval_payload` rows are deleted. Every `runtime_transcript` row of a run whose
   `runtime_request_context` names the reference is deleted, because a transcript can restate
   what the model was shown; the `runtime_request` row stays, holding references and a digest
   ([runtime](runtime-and-mcp.md#what-the-core-records-about-a-run)). Outbox rows are not
   touched, because event `data` never carries personal content
   ([events](intake-and-events.md#events-and-the-outbox-fr-15)); only references, which now
   resolve to `deleted`.
5. **Held exports.** Every `export_record` whose `export_record_ref` rows include the reference is
   marked `state = removed_by_deletion` with `removed_at` and the deletion record id.
6. **Deletion record.** `core.deletion_record` is written.
7. **Commit.** After commit, the coordinator deletes the marked export artifacts from the data
   root; a failure to remove a file leaves the export record marked and enqueues
   `core.exports.sweep`, which retries until the path is gone. The record says why the artifact
   is gone before the artifact is gone, never the reverse.

A participant that raises aborts the whole deletion; the record survives untouched and the
operation reports the participant's error. Deletion never cascades to another workspace, to
shared credentials, or to unrelated records (idea document).

### The deletion record

| `core.deletion_record` column | Meaning |
| --- | --- |
| `id uuid`, `deleted_at` | |
| `record_type text`, `record_id uuid` | What, by identifier only. |
| `actor_kind`, `actor_id`, `approval_id uuid null` | Who, and the confirmation. `approval_id` is null only when `actor_kind = system` and the deletion is the memory retention sweep, which is R5's one permitted scheduled expiry ([memory](memory.md#retention-fr-29-criterion-30)). |
| `participants text[]` | Which participants ran. |
| `cancelled_job_count integer`, `cancelled_action_count integer`, `removed_export_count integer`, `invalidated_memory_count integer` | Counts only, and restricted audit data rather than caller output: a count of what a deletion reached can describe records across audiences the caller may not read, so the four are readable through the owner/operator audit surface (`core.audit.list`) and are never part of the operation's own result. |
| `cause text` | user_erasure or retention_expiry; never a caller-selectable approval bypass. |
| `retained_successor_ref text null` | Canonical successor captured only for scheduled expiry of a superseded memory, so restore can validate content-free ancestry after physical predecessor removal. Never source content. |

No column can hold content from the deleted record; the model has no field for it, and the test
in criterion 65 asserts no field value from the deleted record appears anywhere in the row. The
record resolver is to return `state = deleted` for the reference from this table; that lookup is
not built yet, so today a deleted reference resolves to `Unavailable` like any absent row.

The persisted outcome still has exactly four counters. 1a1 implements the owning memory delete,
Recallatron invalidation, deletion evidence and transactional audit only; its first three
counters are zero. The caller receives deletion_ref only. Exact counts are restricted
owner/operator audit data. Phase-three 2c owns the remaining job/action/transcript/held-export
cascade and criterion 65. Deletion cause/successor metadata is exported and validated with the
ledger; it is not an erasure bypass for restoration.

### Deletion is not withdrawal

`leads.contact_permission.withdraw(party_ref, purpose, channel, reason)` sets `withdrawn_at` on
the permission, or writes a withdrawal row when no permission was ever recorded
([contact permission](intake-and-events.md#contact-permission-records-fr-46)), and leaves every
record in place; queued actions fail closed at their guard and derived memory stops being
retrievable for the purpose (criterion 61). `core.record.delete` removes the record and leaves
permissions and suppressions untouched. The interface offers them as two actions on a party with
two confirmations, and neither operation calls the other (FR 46, R5).

### Receipts and re-delivery

The receipt outlives the observation with `source_event_id`, `content_digest`, `byte_length`,
`received_at`, `source_occurred_at`, and no payload. A later delivery with the same identifier
hits the unique index; the intake path finds the receipt's `state_detail = observation_deleted`
and records a `delivery_conflict` of `kind = deleted_observation` whatever the digest, holding
the new delivery's digest, byte length, and receipt time and **no body**, so the observation is
never recreated, the erased person's payload never lands again, and the conflict is visible on
the connection (criterion 65, FR 51). A `digest_differs` conflict recorded before the deletion
had its body nulled by the cascade, so after deletion no row under that receipt holds a payload.
Only an operator command that deletes the *receipt* would allow the event to be accepted again,
and release one ships no such command.

## Export and restore (FR 52)

### The artifact

One zstd-compressed tar, `<data_root>/workspaces/<id>/exports/<export_id>/<export_id>.tar.zst`,
written by the export job from one read-only `REPEATABLE READ` snapshot of the workspace:

```text
manifest.json          core version, contract version, workspace id and slug, owner account id,
                       created_at,
                       modules: [{ module_id, package_version, state, schema_version,
                                   export_format_version }],
                       settings_digest, record_counts per category, approval_count
settings.jsonl         workspace and member settings, one row each; secret references, never values
core/
  approvals.jsonl      every non-terminal approval, written with state = requires_reapproval
  operations.jsonl     non-terminal operations, written as unresolved
  audit.jsonl          the audit log
  deletions.jsonl      deletion records
modules/
  <module_id>.jsonl    one line per row, each carrying a `record` discriminator, in the module's
                       declared export format, validated against its JSON Schema before the
                       export completes
```

Every exportable module's rows are written by its `exporter` through a core writer that
enforces the schema. The core writes its own tables. Secret values are absent by construction:
no exporter can resolve one. Copies of workspace files are to travel with the records that
reference them; no module stores files yet, so the artifact carries none.

### Export records

| Table | Columns |
| --- | --- |
| `core.export_record` | `id uuid`, `kind` (`export`, `restore`), `artifact_path null`, `source_digest bytea null`, `created_at`, `created_by_id`, `state` (`in_progress`, `complete`, `failed`, `removed_by_deletion`), `removed_at null`, `deletion_record_id null`, `byte_length null`. A `restore` row records the digest of the artifact restored from and holds no path; the reference index below is written only for `export` rows. |
| `core.export_record_ref` | `export_id`, `record_ref` for every record of a deletable type in the artifact. The table exists but nothing writes it yet, including for exported `recallatron.memory` rows; it arrives with the held-export step of the cascade (2c). |

The reference index exists for one reason: criterion 65's "the export artifact is gone from the
data root and its export record says why" needs the coordinator to find which artifacts carry a
record without opening them. It is bounded by the number of deletable records times the number
of held exports, and the sweep removes rows with their artifacts.

### Restore

`rheo workspace restore <artifact>` on the operator command, or `core.workspace.restore` for an
owner into a workspace the control plane does not yet know:

1. Read and validate `manifest.json`; refuse if the contract version is unsupported, or if any
   listed module is not loaded by this host or declares an `export_format_version` other than
   the host's, naming it.
2. Create the workspace row with the recorded id and slug (identifiers are globally safe, so the
   id is kept and comparison in criteria 21, 36, 67 is by id), provision the database, apply the
   core chain, and hold the row in state `restoring`.
3. In one transaction from here to the commit: create each loadable module's extensions and run
   its whole chain to the host's head, then write its `module_state` row from the artifact.
4. Import settings, approvals, operations and the audit log; then each module's rows through its
   `importer`, parents first; then the deletion records, which a module's ancestry check reads;
   then each module's second pass, which resolves self-references.
5. Every approval lands `requires_reapproval`. An operation bound to a restored approval lands
   `approval_required` and any other lands `unresolved`, so nothing executes until a person
   approves again (criterion 21). Every pending external action is to land `pending` with
   `approval_id` pointing at an approval that is not `approved`, every connection
   `needs_credential` and every member credential `needs_value`; none of the three is built yet.
6. Write an `export_record` of `kind = restore` with the source artifact's digest, commit, and
   set the workspace `active`.

The comparison the criteria require is a supported operation, `core.workspace.digest`, that
returns a line count and a SHA-256 digest per category of the export serialisation (the core
categories plus one per exportable module), read through the same snapshot an export uses, so
"match by comparison" is one call on each side.

## Migration verification (FR 53)

The predecessor memory store's migration in phase two, and any later predecessor migration, is
verified before switchover and the result kept where the data lives. None of this section is
built yet: the table, the switchover operation and the CI check below arrive with run 1b.

| `core.migration_verification` column | Meaning |
| --- | --- |
| `id`, `source_name text`, `module_id`, `run_at`, `run_by_id` | `source_name` is a label chosen by the operator; the public repository never carries a real source's name. |
| `source_count integer`, `destination_count integer`, `missing_count integer` | Every source record must have a destination record. |
| `sample_size integer`, `sample_matched integer`, `tolerance_fraction numeric`, `passed boolean` | A sampled set of retrieval queries compared under the old and new retrieval layers; `passed` when `sample_matched / sample_size >= 1 - tolerance_fraction`. Default tolerance 0.05, recorded on the row. |
| `report_file_ref null` | An optional fuller report under the workspace's private files. |

The migration job writes the row and stops. Switchover (pointing the workspace's memory module at
the migrated records as its live set) is `recallatron.migration.switch_over`, a separate mutate
operation a person invokes after reading the verification (criterion 32). A CI check asserts no
file matching a migration-report pattern is tracked in the repository.

## Disaster recovery for release one

Answers the remainder of idea-document question 13 at release-one scale.

- **What is backed up.** The control-plane database and every workspace database by `pg_dump`,
  plus the data root (secrets directory, workspace files, exports). Postgres backups are the
  operator's, on the operator's schedule; `deploy/` is to ship a generic example and does not
  yet.
- **What restore means.** Two paths, both exercised: a database-level restore of a cluster
  backup, which is Postgres's own procedure; and the application-level export and restore above,
  which is the path the acceptance criteria drill at the end of each phase.
- **How it is tested.** Criteria 21, 36, and 67 are the drill: export, restore into an empty
  deployment, compare by digest. Running them in CI at every phase end is the release-one
  disaster-recovery test plan; a cluster-backup restore drill is to be an operator runbook in
  `deploy/` (not written yet) and is not automated in release one.
- **What is not covered.** Point-in-time recovery, replica failover, and per-workspace encryption
  keys are the hosted edition's ([later phases](later-phases.md#phase-8-the-hosted-edition)).
