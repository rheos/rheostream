# Intake, events, durable work, and audit

**Part of:** the [architecture specification](README.md). Designs against FR 15 to FR 18,
FR 31 to FR 39, FR 46, R2, R4, and criteria 11 to 14 and 38 to 49. Answers idea-document question
6 (event schemas, ordering, retry limits, replay) and fixes the intake contract's shapes.
**Decisions:** A7 (intake lives in Leads; transports are connectors), A8 (outbox, envelope,
worker, audit shapes). See the [decision list](README.md#architecture-decisions).

## Part one: the intake contract

### Who owns what

Intake is the Leads module's front half. Connections, mappings, routing rules, funnels, receipts,
and observations are `leads.*` record types in the `leads` schema. The three release-one
transports are **connectors**: thin adapters in `connectors/` that authenticate a delivery, turn
it into a `Delivery` value, and call `leads.intake.accept_delivery`. A connector owns transport
and translation; it holds no domain rule (connectors README). The webhook receiver's HTTP route is
registered by the Leads module under its `connector_bindings`, on the `api` surface.

The requirements' "generic intake" is generic because the contract below has no provider branch:
a funnel is a mapping plus routing rules, and two unrelated funnels (criterion 55) are two
mappings against one schema.

### Entities

All in the `leads` schema. Every table has `id uuid` (UUIDv7) unless noted.

| Table | Columns | Notes |
| --- | --- | --- |
| `funnel` | `name text`, `description text`, `created_at`, `archived_at null` | Acquisition context: a site, an event, a referral programme. |
| `campaign` | `funnel_id`, `name text`, `created_at`, `archived_at null` | Optional refinement of a funnel. |
| `intake_connection` | `name`, `transport text`, `source_namespace text unique`, `mapping_id`, `mapping_version integer`, `funnel_id null`, `campaign_id null`, `priority integer`, `subject_authenticated boolean`, `email_verified boolean`, `signing_secret_ref text null`, `signing_key_generation integer`, `previous_secret_ref text null`, `previous_valid_until timestamptz null`, `state text`, `created_at`, `revoked_at null` | `transport` in `webhook`, `import`, `manual`. `state` in `active`, `needs_credential`, `revoked`. `source_namespace` is a URI (`urn:rheo:connection:<uuid>`) used as the CloudEvents `source`. `funnel_id` is null only on the `manual` connection, where each capture names its funnel (check constraint). `subject_authenticated` and `email_verified` are the operator's declaration of what this source proves, which R4 needs. `signing_key_generation` starts at 1 and increments on every rotation; it is the only rotation mechanism (see [transports](#transports)). |
| `connection_health` | `connection_id pk`, `last_accepted_at`, `last_processed_at`, `pending_count`, `unresolved_failures integer`, `last_error text null`, `last_error_at timestamptz null`, `last_unauthenticated_write_at timestamptz null`, `lag_seconds integer` | Maintained by the intake worker; read by `leads.connection.health` (FR 38). `last_error_at` is set by every path that records a failure. `last_unauthenticated_write_at` is written by the webhook receiver alone and is what bounds its pre-authentication write ([transports](#transports)); it is a separate column precisely so an authenticated path's write can never suppress the receiver's, which criterion 42 requires to be visible. The operation's `failed_delivery_count` is **not** a column here: it is computed at read time from `core.work.failures` ([retry](#retry)), so there is no second counter that could disagree with the delivery table. |
| `field_mapping` | `id`, `version integer`, `name`, `source_kind text`, `state text` | `source_kind` in `json`, `csv`. Primary key `(id, version)`. Versions are immutable once a receipt pins them. |
| `field_mapping_identity` | `mapping_id`, `version`, `event_id_path`, `occurred_at_path`, `subject_id_path null`, `verified_email_path null` | Where identity lives in a payload. Paths are JSON Pointers for JSON, column names for CSV. |
| `field_mapping_rule` | `mapping_id`, `version`, `target text`, `source_path text`, `transform text null`, `transform_arg text null`, `required boolean`, `clear_on_null boolean` | `target` is a core fact name or `ext.<namespace>.<field>`. `transform` from a closed set: `trim`, `lower`, `email_normalize`, `phone_normalize`, `datetime(format)`, `const(value)`. |
| `routing_rule` | `id`, `connection_id`, `ordinal integer`, `action text`, `pipeline_id null`, `enabled boolean` | `action` in `create_opportunity`, `attach_to_open_opportunity`, `record_only`. First enabled match by `ordinal` wins; no match means `record_only`. |
| `routing_condition` | `rule_id`, `field text`, `op text`, `value text null` | All conditions of a rule must hold. `op` in `present`, `absent`, `equals`, `contains`. |
| `delivery_receipt` | `id`, `connection_id`, `source_event_id text`, `content_digest bytea`, `byte_length integer`, `transport text`, `received_at`, `source_occurred_at null`, `mapping_id`, `mapping_version`, `signing_key_generation integer null`, `state text`, `state_detail text null`, `processed_at null` | **Unique `(connection_id, source_event_id)`.** `state` in `pending`, `processed`, `refused`; a duplicate never inserts a receipt, so there is no duplicate state. `signing_key_generation` is the connection generation whose secret verified the signature, null for the import and manual transports. `state_detail` holds `observation_deleted` after R5 deletion. The receipt survives observation deletion holding identifier, digest, length, and timestamps only (R5). |
| `delivery_payload` | `receipt_id pk`, `body bytea`, `content_type text`, `byte_length integer` | The bounded source copy (FR 36). Deleted with the observation; the receipt's digest remains. |
| `delivery_conflict` | `id`, `receipt_id`, `kind text`, `content_digest bytea`, `byte_length integer`, `received_at`, `body bytea null`, `resolved_at null`, `resolved_by_id null`, `resolution_note text null` | `kind` in `digest_differs` (FR 34, criterion 45) and `deleted_observation` (a re-delivery of a deleted observation's event, criterion 65). **A `deleted_observation` conflict never stores `body`**: digest, length, and timestamps only, so a retrying sender cannot land an erased person's payload back. A `digest_differs` body is review material, held for exactly as long as the conflict is open: `leads.conflict.resolve` nulls it as it sets `resolved_at`, and R5 deletion of the observation nulls it at once ([deletion](deletion-export-migration.md#the-cascade)). There is no scheduled expiry of a conflict body, because R5 permits none beyond the memory retention setting; an open conflict is an unresolved failure on the connection's health until a person resolves it. |
| `observation` | `id`, `receipt_id unique`, `connection_id`, `funnel_id`, `campaign_id null`, `transport text`, `source_event_id text`, `external_subject_id text null`, `verified_email text null`, `source_occurred_at`, `received_at`, `mapping_version integer`, `completeness integer`, `party_ref text null`, `created_at` | The normalized evidence record (FR 39). `party_ref` is a record reference into `relationships`, nulled on party deletion. `verified_email` is set only when the connection declares `email_verified`. |
| `observation_field` | `observation_id`, `target text`, `value_kind text`, `value_text text null`, `source_path text`, `transform text null`, `mapping_version integer` | Per-field provenance (FR 36). `value_kind` in `value`, `cleared`. Primary key `(observation_id, target)`. |
| `import_batch` | `id`, `connection_id`, `file_ref`, `row_count`, `accepted`, `refused`, `state`, `state_detail text null`, `created_at`, `finished_at null` | One per CSV or JSON import. `state` in `queued`, `running`, `completed`, `refused` (the whole file, before any row, with the first missing column in `state_detail`), `failed`. |

### The observation envelope

The normalized envelope every transport produces and every consumer of intake events sees. It is
CloudEvents-compatible in the attributes that matter and carries Rheo-specific extensions in
`data`:

| Attribute | Source | Notes |
| --- | --- | --- |
| `specversion` | constant `1.0` | |
| `source` | the connection's `source_namespace` | Server-assigned; never the sender's claim. |
| `id` | the source event identifier | From `event_id_path`, else the `X-Rheo-Event-Id` header, else the SHA-256 of the body. Event identity is `(source, id)`, distinct from subject identity. |
| `type` | `leads.delivery.received` | |
| `time` | source occurrence time from `occurred_at_path`, else receipt time | Both are stored; `time` is the source's. |
| `subject` | the external subject id when the mapping yields one | Subject identity, separate from event identity. |
| `datacontenttype` | `application/json` | |
| `data.transport` | `webhook`, `import`, `manual` | Delivery transport (FR 36). |
| `data.funnel_ref`, `data.campaign_ref` | resolved from the connection, or chosen at manual capture | Acquisition attribution, stored in columns distinct from transport (criterion 46). |
| `data.facts` | list of `{ target, value_kind, value, source_path, transform }` | What the mapping produced. |
| `data.completeness` | count of `value` facts | Used by field derivation. |
| `data.payload_ref` | the receipt id | The bounded source copy is reachable, not embedded. |
| `data.permission_evidence` | list of `{ purpose, disclosure_version, path }` | Contact-permission evidence the mapping was told to look for (FR 46). Evidence, not a permission record. |

Authenticated workspace context is not in the envelope. It is carried by the receipt's connection
and by the delivery record's context, so a payload that claims a tenant, a destination, or a
permission set (criterion 41) is preserved inside `delivery_payload` as evidence and read for
nothing else.

The core fact vocabulary, closed in release one and extended only through `ext.*` (FR 44):
`person.name`, `person.email`, `person.phone`, `organization.name`, `organization.domain`,
`subject`, `message`, `interest`, `source_url`, `locale`, `value.amount`, `value.currency`,
`value.basis`. Extension targets are validated against the pipeline's declared extension fields
([confirmation and safety](confirmation-and-safety.md#pipeline-presets-and-their-limits)); an
unknown target refuses the mapping version at save time, not at delivery time.

### Acceptance: receipt, dedup, conflict, acknowledgement

`leads.intake.accept_delivery(ctx, delivery)` runs in one transaction:

1. **Bound.** `byte_length > intake.max_payload_bytes` (default 262144, operator floor `min`)
   refuses with `payload_too_large` and stores nothing.
2. **Connection.** Must be `active` and belong to `ctx`'s workspace; the receiver built `ctx`
   from the connection's credential, so this is a consistency check, not a lookup by claim.
3. **Insert receipt** with `ON CONFLICT (connection_id, source_event_id) DO NOTHING`.
   - Inserted: continue to step 4.
   - Not inserted, the existing receipt's `state_detail` is `observation_deleted`: insert a
     `delivery_conflict` of `kind = deleted_observation` carrying the digest, the byte length,
     and the receipt time and **no body**, whatever the digest; increment
     `connection_health.unresolved_failures`; return `conflict`. The observation is never
     recreated and the surviving receipt holds no payload (FR 51, criterion 65).
   - Not inserted, existing digest equals this digest: store nothing, return `duplicate`. The
     transport acknowledges as it would a fresh accept, because the sender's retry succeeded
     the first time.
   - Not inserted, digest differs: insert a `delivery_conflict` of `kind = digest_differs` with
     the body, held until the conflict is resolved, increment `unresolved_failures`, return `conflict`. The
     transport acknowledges (the delivery was durably recorded) and the conflict is visible on
     the connection (FR 34, criterion 45). The existing observation is not touched.
4. **Store payload** in `delivery_payload`.
5. **Queue processing**: write outbox event `leads.delivery.received` with the receipt as subject.
6. Commit. Only after commit does the transport acknowledge (webhook `202`, import row counted,
   manual capture confirmed). Criterion 43's failing processing step leaves the receipt because
   processing is a later transaction.

Two concurrent deliveries with the same identifier serialise on the unique index: one inserts, the
other sees the conflict path and finds an equal digest (criterion 44).

### Processing

The worker consumes `leads.delivery.received` (consumer `leads.process_delivery`, not replay-safe)
in one transaction per receipt:

1. **Recheck the connection** (edge case 15, criterion 42). Refuse when the connection is not
   `active`, or when the receipt's `signing_key_generation` is older than the connection's
   current generation and the previous secret is no longer valid (`previous_secret_ref` null or
   `previous_valid_until` past), or older by more than one generation. A refusal sets the
   receipt `refused` with detail `connection_revoked`, counts an unresolved failure, and stops.
   No observation.
2. **Apply the pinned mapping version** from the receipt, never the connection's current one.
   A payload that does not fit its mapping (a required target absent, a transform failing) sets
   the receipt `refused` with the target named (criterion 39) and stops.
3. **Create the observation and its fields.** `value_kind = cleared` when the source path is
   present with a null value and the rule has `clear_on_null`; absent paths produce no row
   (criterion 48).
4. **Resolve the party** through `relationships.party.resolve_or_create`
   ([relationships](relationships.md#relationshipspartyresolve_or_create)) with the evidence the
   connection declares: `external_subject_id` when `subject_authenticated`, `verified_email`
   when `email_verified`, everything else as hints. Automatic linking happens only on the two
   authenticated classes (R4, criteria 50 to 52); with hints only, the operation creates a new
   party and open review candidates and links nothing. The observation's `party_ref` is set to
   whatever party came back, and is never rewritten afterward: an accepted review candidate
   merges that party into the existing one and the reference resolves through the alias. The
   call is a registered operation, so Leads never touches the relationships schema.
5. **Route.** Evaluate the connection's rules in order. `create_opportunity` calls
   `leads.opportunity.create_from_observation` ([opportunity records](confirmation-and-safety.md#opportunities-parties-qualifications-and-handoffs));
   `attach_to_open_opportunity` attaches to the newest open opportunity in the target pipeline
   whose party has the same `canonical_ref` (`relationships.party.get`), recording the decision
   in `opportunity_observation`, else creates one; `record_only` stops here with the observation
   unlinked (FR 39).
6. **Derive fields** (below), set the receipt `processed`, update `connection_health`, and
   write `leads.observation.accepted` plus, when created, `leads.opportunity.created`.

No model is involved anywhere in this path (FR 35, criterion 38).

### Field derivation (FR 37)

`leads.opportunity_field_state` holds the current value of each opportunity field with why it is
the current value:

| Column | Meaning |
| --- | --- |
| `opportunity_id`, `target` | Primary key. |
| `value_kind`, `value_text` | The current value or an explicit clear. |
| `owner` | `source` or `user`. Set to `user` by any hand edit; never set back by intake. |
| `observation_id`, `occurred_at`, `completeness`, `priority` | The evidence that set it. |
| `orphaned` | `false` by default; `true` for an `ext.*` target the opportunity's current preset version no longer declares after a [version migration](confirmation-and-safety.md#the-migration-operation). Readable, exportable, not writable. |

A new observation may write a field only when all hold: `owner = source`; the observation's
`occurred_at` is not older than the current one; and, when `occurred_at` is equal, the
observation's `completeness` is not lower and its connection `priority` is not lower. A thinner
or older observation therefore never overwrites (criterion 47); a user-set field is `owner =
user` and survives everything, and a user-set stage and a note live in columns intake never
writes (`opportunity.stage_id` and
[`opportunity_note`](confirmation-and-safety.md#opportunities-parties-qualifications-and-handoffs)),
which is criterion 49; a `cleared` fact overwrites under the same rule and is distinguishable
from absence (criterion 48). The observation itself is stored regardless, so nothing is lost,
only not applied.

**After an observation is deleted.** The Leads owning delete removes the field-state rows whose
`observation_id` is the erased observation, then re-derives each affected `(opportunity_id,
target)` from the opportunity's remaining observations (`opportunity_observation`, the erased
one excluded) by applying the rule above to them in `occurred_at` order, inside the deletion's
transaction ([the cascade](deletion-export-migration.md#the-cascade)). A target with no
remaining evidence stays absent; `owner = user` rows name no observation and are untouched. The
opportunity therefore shows what its surviving evidence supports and nothing the erased record
alone supported.

### Transports

The Leads manifest declares one `ConnectorBinding` per transport, and every binding names the
same operation:

| Transport | Binding | Actor when `accept_delivery` runs |
| --- | --- | --- |
| `webhook` | Route `POST /api/v1/intake/webhook/<connection_id>` on the `api` surface, calling `leads.intake.accept_delivery` | A `connection` actor, role `service`, built after the signature verified |
| `import` | No route. The `leads.import.run` job hands each row to the import connector, which calls `leads.intake.accept_delivery` | The job's `system` actor, role `service` |
| `manual` | No route. `leads.intake.capture` hands the form to the manual connector, which calls `leads.intake.accept_delivery` | The capturing account, in its own context |

**Signed webhook.** Headers: `X-Rheo-Timestamp` (Unix seconds), `X-Rheo-Signature` (`v1=<hex
HMAC-SHA256 of "<timestamp>.<body>">`), optional `X-Rheo-Event-Id`. The receiver runs these
steps in this order, and stops at the first that fails:

1. Resolve the connection by the path id. Unknown: `401`, nothing recorded. Known and not
   `active`: `401`, no receipt, an unresolved failure counted on that connection. The state is
   checked **before** any signature work, so a revoked connection refuses even a correctly
   signed delivery.
2. Refuse a timestamp outside `intake.replay_window_seconds` (default 300) the same way.
3. Resolve the signing secret through the receiver's own secret scope and verify the signature
   in constant time against the current secret and, while `previous_valid_until` is in the
   future, the previous one. A failure is `401`, no receipt, no acknowledgement, and an
   unresolved failure counted (criterion 42).
4. Record which generation matched, build the `WorkspaceContext` from the connection, and call
   `accept_delivery`.

The failure count is the one Leads write that happens before a context exists: the route handler
increments `connection_health.unresolved_failures` and sets `last_error` and `last_error_at` in
its own short transaction, keyed by the path id, because an unauthenticated request has no actor
to dispatch for, and criterion 42 wants the refusal visible on that connection's health. The
connection id in the path is a lookup key, not authorization; the signature is.

**That write is debounced, because it is reachable without authentication.** The connection id is
a path segment and is explicitly not a credential, so anyone who has seen a webhook URL can drive
this handler, and a per-request write would make a bad-signature flood into unbounded write
amplification and row-lock contention on one primary key. The handler therefore issues the update
only when `last_unauthenticated_write_at` is null or older than
`intake.health_write_interval_seconds` (package default 60, operator floor `min`), as a single
conditional statement:

```sql
UPDATE leads.connection_health
SET unresolved_failures = unresolved_failures + 1,
    last_error = $2, last_error_at = now(), last_unauthenticated_write_at = now()
WHERE connection_id = $1
  AND (last_unauthenticated_write_at IS NULL
       OR last_unauthenticated_write_at < now() - $3::interval);
```

One statement, no read-then-write, so concurrent refusals collapse onto the row lock and past the
first each does nothing.

**The debounce is keyed on its own column, and that is load-bearing.** Criterion 42's test
produces two refusals for one connection in quick succession: a processing-time refusal in the
worker (which has a context and writes through the ordinary path) and a receiver-time refusal on
the next delivery signed with the old secret. If both paths shared one timestamp, the worker's
write would suppress the receiver's and the second refusal would not be visible, failing the
criterion. Only the receiver writes `last_unauthenticated_write_at`, so an authenticated path can
never consume the receiver's budget, and both refusals land in `unresolved_failures` as the
criterion requires.

What is deliberately given up is an exact count of *unauthenticated* refusals: a flood registers
as one increment per interval rather than one per request. An exact count of requests an attacker
controls is a number the deployment should not pay to keep, and a caller that wants the request
rate reads the proxy's logs, which is where unauthenticated traffic belongs.

The same budget covers the state-check refusal in step 1 and the timestamp refusal in step 2,
since all three are pre-authentication. A refusal that gets past the signature happens inside
`accept_delivery` under a real context and is written by the ordinary audited path, not here.

**Rotation and revocation** are connection-level and there is no other mechanism.
`leads.connection.rotate_secret(connection_ref, overlap_seconds)` (mutate, owner) generates a
new secret under a new reference, moves the old reference to `previous_secret_ref`, sets
`previous_valid_until = now() + overlap_seconds` (package default 0, maximum
`intake.max_rotation_overlap_seconds`, default 3600, floor `min`), and increments
`signing_key_generation`. `leads.connection.revoke_secret` sets `previous_secret_ref` null and
`previous_valid_until` null at once, and `leads.connection.revoke` sets the connection `revoked`.
From that moment a delivery signed with the old secret fails at the receiver, and a receipt
already accepted under the old generation fails at processing step 1. Criterion 42's test calls
`leads.connection.rotate_secret` with `overlap_seconds = 0` for the rotation case and
`leads.connection.revoke` for the revocation case; in each, the delivery accepted before the call
is refused at processing with no observation, the delivery signed with the old secret afterward
is refused at the receiver with no receipt, and both count on the connection's health.

**CSV and JSON import.** An owner uploads a file against a connection with `transport = import`.
The importer streams rows, derives each row's `source_event_id` from `event_id_path` (for CSV a
column; absent, the SHA-256 of the row), and calls `accept_delivery` per row inside the batch.
A file whose columns do not match the mapping version's rules is refused whole, naming the first
missing column, before any row is accepted (criterion 39).

**Manual capture.** A form on the Leads surface and the `leads_capture` tool. Each workspace has
one connection with `transport = manual`, created at Leads enable with `funnel_id` null,
`priority` 100, both evidence flags false, and the package mapping `manual_capture` version 1,
also written at enable: its identity paths are `/event_id` and `/occurred_at`, and its rules map
each core fact name to the JSON Pointer of the same name in the capture body (`/person.email`
to `person.email`, and so on) with no transform beyond `trim` and the two normalizers. Extension
values a person enters on the form are applied by `leads.opportunity.update` after routing, with
`owner = user`, not by the mapping, so the seeded mapping never needs a new version. The capture
must name a funnel from the workspace's funnels; a capture with none is refused before a receipt
exists (criterion 40). The core generates the `source_event_id` (a UUIDv7) and the capture goes
through `accept_delivery` and processing like any delivery, so criterion 39's "identical apart
from transport" holds by construction.

## Part two: events, durable work, operations, audit

### Events and the outbox (FR 15)

All in the `core` schema of each workspace database.

| Table | Columns | Notes |
| --- | --- | --- |
| `outbox_event` | `id uuid`, `position bigserial`, `type text`, `schema_version integer`, `source text`, `subject_ref text`, `subject_revision integer`, `correlation_id uuid`, `causation_id uuid null`, `actor_kind text`, `actor_id uuid null`, `occurred_at`, `data jsonb` | `data` is the declared event model serialised; it is never queried, only delivered, which is the one place the house rule allows a JSON column. `source` is `urn:rheo:module:<module_id>`. |
| `event_delivery` | `event_id`, `consumer_id text`, `subject_ref text`, `position bigint`, `state text`, `attempts integer`, `next_attempt_at`, `lease_owner text null`, `lease_until timestamptz null`, `last_error text null`, `completed_at null` | Primary key `(event_id, consumer_id)`. `subject_ref` and `position` are copied from the event at fan-out so the ordering index `(consumer_id, subject_ref, position)` is local to this table. `state` in `pending`, `leased`, `delivered`, `failed`, `skipped`. |
| `consumer_processed` | `consumer_id`, `event_id`, `processed_at` | Primary key `(consumer_id, event_id)`; written inside the consumer's transaction. |

**Envelope.** The `EventEnvelope` in `packages/contracts` is the row above plus the delivery
context, versioned by `schema_version` per event type. Producers may only add optional fields
within a version; a removal or a meaning change is a new version and a consumer declares which
versions it accepts. `correlation_id` is the originating operation's id; `causation_id` is the
event that led to this one when there is one.

**Fan-out at write.** `UnitOfWork.publish(event)` inserts the outbox row and one
`event_delivery` row per consumer that is subscribed to `type` and belongs to a module enabled in
the workspace at that moment. A consumer that is not enabled gets no row and never sees the event.

**Ordering.** Deliveries for the same `(consumer_id, subject_ref)` are delivered in `position`
order, one in flight at a time, and a later delivery is never attempted while an earlier one for
the same consumer and subject is anything but `delivered` or `skipped`. The lease query is the
whole rule:

```sql
UPDATE core.event_delivery d
SET state = 'leased', lease_owner = $1, lease_until = now() + interval '60 seconds'
WHERE (d.event_id, d.consumer_id) = (
  SELECT c.event_id, c.consumer_id
  FROM core.event_delivery c
  WHERE ((c.state = 'pending' AND c.next_attempt_at <= now())
      OR (c.state = 'leased' AND c.lease_until < now()))
    AND NOT EXISTS (
      SELECT 1 FROM core.event_delivery earlier
      WHERE earlier.consumer_id = c.consumer_id
        AND earlier.subject_ref = c.subject_ref
        AND earlier.position < c.position
        AND earlier.state NOT IN ('delivered', 'skipped'))
  ORDER BY c.position
  FOR UPDATE SKIP LOCKED
  LIMIT 1)
RETURNING d.event_id, d.consumer_id;
```

A `failed` head therefore blocks every later delivery for its consumer and subject until a person
acts: `core.work.retry(delivery)` resets attempts, or `core.work.skip(delivery)` (mutate, owner or
operator, audited, with a note) sets it `skipped` and releases the queue. Order is never broken
silently; `core.work.failures` shows the blocked count behind each failed head. Across subjects
there is no ordering promise.

**Exactly-once effect.** A consumer handler runs inside a `UnitOfWork`; the worker inserts
`consumer_processed` in that same transaction and commits. A redelivery after a crash hits the
primary key, and the worker marks the delivery `delivered` without running the handler
(criterion 11). Consumers are duplicate-safe because the dedup insert is part of the contract,
not left to each handler.

**Retry.** Attempts back off as `5s, 20s, 80s, 320s, 1280s` then cap at 3600s, with jitter, for
`work.max_attempts` (default 8, about two and a half hours end to end). After the last attempt the delivery is `failed`
with its error and appears in `core.work.failures` (criterion 12). An operator or owner may
`retry` a failed delivery, which resets attempts.

**A failed head is surfaced, not merely recorded.** Head-of-line blocking is deliberate — order is
never broken silently — but every route out of it needs a person, and a design where the only
route *in* is a screen nobody has opened is a queue that stops for two and a half hours and then
stays stopped. So the failure is pushed to the two places a person already is:

- **The shell.** `core.work.failure_summary` (read class, roles `owner`, `operator`) returns
  `{ failed_count, blocked_count, oldest_failed_at }` for the session's active workspace, and the
  application shell reads it in its layout and renders a persistent banner on every screen while
  `failed_count` is above zero, linking to the work-failures view. It is the shell's own surface,
  not a module contribution, so it is present in a workspace with no module enabled and it covers
  every consumer rather than only Leads. One read per navigation, cached for
  `work.failure_summary_cache_seconds` (default 30).
- **The connection.** `leads.connection.health` (FR 38) additionally reports
  `failed_delivery_count`, obtained by calling `core.work.failures` filtered to the receipts of
  that connection at read time. It is computed, not stored: there is no new column, no new
  callback from the worker into a module, and no duplicated counter that could disagree with the
  delivery table. Intake failures therefore appear where an owner already looks for the health of
  a funnel.

Neither is a notification in the sense of leaving the deployment. Release one has no outbound
channel — no email, no push — and inventing one here would be new machinery for a problem the
banner solves for a tool its owner opens daily. Phase six's channel is the first surface that can
reach a person who is *not* looking, and the escalation belongs there
([later phases](later-phases.md#phase-6-channels-and-the-second-runtime)) rather than in a core
component release one would carry unused.

The same summary counts operations in state `unresolved`, which has the identical shape: a
terminal record state that is an open item for a person, listed by `core.work.failures`, and
cleared only by an explicit `core.operation.resolve` call.

**Replay.** `core.work.replay(consumer_id, from_position)` re-creates `pending` deliveries for a
consumer from the outbox. It is permitted only for consumers whose subscription declares
`replay_safe = true` (read-model builders, index maintainers). Consumers that cause external
effects or create domain records declare `replay_safe = false` and the operation refuses to target
them. Replay therefore rebuilds derived state and never resends anything (idea document).

**Retention.** Outbox rows and their deliveries are kept for `work.outbox_retention_days`
(package default 30, floor `min`); the retention sweep removes older rows whose deliveries are all
terminal. Replay reaches back only as far as retained rows, and `replay` refuses a
`from_position` older than the oldest retained row rather than replaying a gap. The outbox is
outside the R5 cascade because of the rule below, not despite it.

**Cross-module reach, and what an event may carry.** An event carries references and
non-personal facts, never a copy of a record and never a name, an address, a contact value, or
a message body. A consumer that needs the record resolves the reference under its own context
([identifiers](identifiers.md#resolution-under-permission)). The release-one event types and
their `data` fields, each declared as a model in the owning manifest:

| Event type | `data` fields |
| --- | --- |
| `leads.delivery.received` | `receipt_ref`, `connection_ref`, `transport` |
| `leads.observation.accepted` | `observation_ref`, `receipt_ref`, `connection_ref`, `funnel_ref`, `campaign_ref null`, `party_ref null`, `completeness` |
| `leads.opportunity.created` | `opportunity_ref`, `observation_ref`, `pipeline_ref`, `preset_version`, `stage_id` |
| `leads.opportunity.transitioned` | `opportunity_ref`, `from_stage_id`, `to_stage_id`, `outcome null` |
| `leads.opportunity.migrated` | `opportunity_ref`, `from_version`, `to_version` |
| `leads.handoff.requested` | `handoff_ref`, `opportunity_ref`, `purpose`, `destination_ref null`, `state` |
| `leads.contact_permission.withdrawn` | `party_ref`, `purpose`, `channel null` |
| `leads.contact_permission.suppressed` | `party_ref` |
| `relationships.party.merged`, `.unmerged` | `survivor_ref`, `merged_ref`, `merge_record_ref` |
| `recallatron.memory.recorded`, `.invalidated` | `memory_ref`, `kind`, `reason null` |
| `core.record.deleted` | `ref`, `deletion_record_ref` |

A manifest whose event model declares a field of a `restricted` tier fails registration, which is
the static half of the rule. Release one's only production consumer is `leads.process_delivery`;
the other types are published so that the phase-one deduplicating test consumer and the later
modules (work-in-motion, channels) have real events to subscribe to, and an event with no enabled
consumer is complete on commit with zero deliveries.

### Jobs and the worker (FR 16)

| Table | Columns | Notes |
| --- | --- | --- |
| `job` | `id uuid`, `kind text`, `state text`, `input jsonb`, `operation_id uuid null`, `depends_on_ref text null`, `attempts`, `max_attempts`, `next_run_at`, `lease_owner null`, `lease_until null`, `cancel_requested boolean`, `last_error null`, `created_at`, `finished_at null` | `state` in `queued`, `leased`, `succeeded`, `failed`, `cancelled`. `input` is the job kind's declared model serialised and never queried. `depends_on_ref` lets the deletion coordinator cancel dependents. |
| `schedule` | `id`, `module_id`, `name`, `job_kind`, `cron text`, `enabled`, `last_run_at null`, `next_run_at` | Created at module enable from the manifest. |

Worker loop, with a one-second idle interval. It visits the active workspaces that **have due
work** — a `job` that is `queued` with `next_run_at <= now()` or a lease past expiry, an
`event_delivery` in the same condition, or a `schedule` due — not every active workspace in the
registry. An idle workspace is never opened, which is what keeps the worker from holding every
workspace's connection pool hot and putting the engine cache's count cap in charge of eviction
([pools](storage-and-workspaces.md#a1-the-per-workspace-unit-is-a-database)). Release one finds
the due set by asking each registered workspace database in a bounded sweep and closing the ones
with nothing to do; a deployment large enough for that sweep to cost more than it saves is the
hosted edition's problem, and the notify-based alternative is recorded there.

The loop, per workspace with due work:

1. Lease: `UPDATE ... SET state = 'leased', lease_owner = $worker, lease_until = now() + 60s`
   for one row selected `FOR UPDATE SKIP LOCKED` where `state = 'queued' AND next_run_at <= now()`
   or `state = 'leased' AND lease_until < now()` (an expired lease is a crashed worker; the retry
   counts an attempt).
2. Heartbeat every 20 seconds extends `lease_until`.
3. The handler receives a `CancellationToken` and checks it at its own checkpoints; a `queued`
   job with `cancel_requested` becomes `cancelled` without running; a `leased` job whose token
   fires ends `cancelled`, not succeeded and not retried (criterion 12).
4. Success sets `succeeded`; an exception sets `queued` with backoff or `failed` when the budget
   is spent.
5. Schedules: a due `schedule` enqueues its job kind and advances `next_run_at`; a job already
   queued for the same schedule is not duplicated.

The same loop serves event deliveries, using the delivery table instead of the job table.
Restart recovery is the expired-lease path and nothing else.

The core declares one scheduled job of its own, created for every workspace at provisioning:
`core.retention_sweep` (daily), which removes runtime transcripts and `ClaudeCliRuntime` session
files past `runtime.transcript_retention_days` and outbox rows past
`work.outbox_retention_days`. `core.exports.sweep` is a job kind and never a schedule: the
deletion coordinator enqueues it when an artifact could not be removed
([deletion](deletion-export-migration.md#the-cascade)), and the job table's attempts and backoff
are its retry. Module sweeps (the memory retention sweep) are the module's own schedules.

### Operations (FR 17)

| Column | Meaning |
| --- | --- |
| `id uuid` | Returned to the caller before completion for long-running operations. |
| `name text`, `safety_class text` | The registered operation. |
| `actor_kind`, `actor_id`, `entry`, `audience_kind`, `audience_id` | From the context. |
| `state text` | `pending`, `running`, `approval_required`, `succeeded`, `failed`, `cancelled`, `unresolved`. |
| `approval_id uuid null` | Set with `approval_required`. |
| `progress_text`, `progress_fraction` | Updated by the job. |
| `result_ref text null`, `error_code text null`, `error_text text null` | Outcome. |
| `terminal_check_kind text null`, `terminal_check_at null` | **Required for `succeeded`**: a database check constraint refuses `succeeded` with these null (criterion 13). The kind names what was checked: `record_exists`, `provider_status`, `sink_recorded`, `handler_returned`. |
| `provider_request_id text null` | For external effects whose provider gives one. |
| `created_at`, `started_at`, `terminal_at` | |

`unresolved` is a terminal state for the operation record and an open item for the person: it is
set when an external effect's outcome is unknown after a timeout and the provider offers neither
idempotency nor status lookup. It is listed by `core.work.failures` and cleared only by
`core.operation.resolve` with an explicit outcome and a note, which is audited.

### Audit (FR 18)

| Column | Meaning |
| --- | --- |
| `id uuid`, `occurred_at` | |
| `actor_kind`, `actor_id`, `entry` | From the context. |
| `operation_name`, `safety_class`, `operation_id null` | What ran. |
| `subject_ref text null` | From the operation's `AuditSpec`. |
| `request_digest bytea` | SHA-256 of the canonical input, so "what exactly was requested" is answerable without storing the input. |
| `outcome text` | `succeeded`, `failed`, `refused`. |

The audit row is written by the service registry's dispatcher inside the operation's transaction
for every operation above the read class. A module never writes it and cannot skip it: an
operation is only callable through the dispatcher, and registration refuses a non-read operation
with no `AuditSpec` (criterion 14). `core.audit.list` (owner only) is the supported read. The
row holds no secret and no reference to one, by the same rule that keeps them out of operation
records ([secrets](storage-and-workspaces.md#a4-the-secret-store-fr-13-guardrail-14)).

### Idempotency

Release one has natural idempotency only. An operation declared `NATURAL` names the unique index
under which a repeat is the same row, and its handler returns that row on a repeat:
`leads.intake.accept_delivery` on `(connection_id, source_event_id)` (the branch table above),
`leads.handoff.request` on `(opportunity_id, idempotency_key)`
([handoffs](confirmation-and-safety.md#the-handoff-operation-fr-45-criterion-64)), and
`relationships.party.resolve_or_create` through the per-receipt consumer dedup. There is no
keyed idempotency and no stored-result table, because nothing in release one must return a
*created* reference on a retry whose natural key does not already carry the answer. Phase five's
`current.work.create_from_handoff` is the first operation that must, and `KEYED` idempotency
with its result table and retention setting arrive with it
([later phases](later-phases.md#phase-5-the-work-in-motion-module)).

### External actions

Introduced here because they are durable work; their approval is in
[confirmation and safety](confirmation-and-safety.md).

| Column | Meaning |
| --- | --- |
| `id uuid`, `operation_id`, `approval_id` | |
| `destination_ref text` | A party reference, a connector destination, or the recording sink. |
| `payload_digest bytea`, `payload_ref` | The approved payload; the digest is rechecked at execution. |
| `state` | `pending`, `executing`, `succeeded`, `failed`, `refused`, `unresolved`, `cancelled`. |
| `provider_request_id null`, `attempts` | |

Execution is a job of kind `core.external_action.execute` with `depends_on_ref = destination_ref`,
so withdrawing a destination party's permission or deleting the party cancels or refuses it
(criteria 61 and 65). Concurrent execution of one action is prevented by the job lease; a retry
after an unknown outcome first reconciles by `provider_request_id` when the provider allows, and
otherwise sets `unresolved`.

## Contact permission records (FR 46)

Leads owns two tables, separate from any observation:

| Table | Columns | Notes |
| --- | --- | --- |
| `contact_permission` | `id`, `party_ref`, `purpose text`, `channel text null`, `recipient_scope text`, `disclosure_version text null`, `evidence_observation_id null`, `granted_at null`, `granted_by_kind null`, `granted_by_id null`, `withdrawn_at null`, `withdrawn_reason text null`, `withdrawn_by_kind null`, `withdrawn_by_id null` | `purpose` from the closed vocabulary `respond`, `follow_up`, `share_with_referral`, `internal_analysis`; `channel` in `email`, `phone`, `message`, or null meaning every channel; `recipient_scope` in `workspace`, `named_referral`. A row is one of three things: a grant (`granted_at` set), a grant later withdrawn (both set), or **a withdrawal with no prior grant** (`granted_at` null, `withdrawn_at` set), which is the common form of edge case 16 and is a record in its own right. An observation's evidence becomes a grant only through `leads.contact_permission.record`, a mutate-class operation a person or a routing rule invokes explicitly; recording an inbound submission creates no permission by itself (criterion 62). |
| `contact_suppression` | `party_ref pk`, `suppressed_at`, `reason text null`, `suppressed_by_kind`, `suppressed_by_id` | The person asked never to be contacted, for any purpose on any channel. One row per party; a second `suppress` updates it. |

The operations, all with roles `owner`, `member` so that a member handling an inquiry can act
without waiting for the owner:

- `leads.contact_permission.record(party_ref, purpose, channel null, recipient_scope,
  disclosure_version, evidence_observation_id null)`, mutate: writes a grant row.
- `leads.contact_permission.withdraw(party_ref, purpose, channel null, reason)`, mutate: sets
  `withdrawn_*` on every live grant row for the purpose and channel (a null channel withdraws
  every channel), and **when no grant row exists it inserts a row with `granted_at` null and
  `withdrawn_at = now()`**, so a withdrawal by a person who never granted anything is recorded
  rather than silently ignored and `check` returns `withdrawn` from then on. Publishes
  `leads.contact_permission.withdrawn`.
- `leads.contact_permission.suppress(party_ref, reason)`, mutate: writes or updates the
  `contact_suppression` row. Publishes `leads.contact_permission.suppressed`.
- `leads.contact_permission.check(party_ref, purpose, channel null)`, read, roles `owner`,
  `member`, `service`: resolves `party_ref` to its canonical party and every alias
  (`relationships.party.aliases`), so a record written against a party before a merge still
  counts, and returns the first that holds: `suppressed` (a suppression row for any of them);
  `withdrawn` (the newest permission row for the purpose whose channel is the requested one or
  null has `withdrawn_at` set); `permitted` (a grant row for the purpose and channel with
  `withdrawn_at` null); `absent` (no row at all).

The two callers read the result differently on purpose: the `ContactPermissionGuard` on external
actions requires `permitted`
([confirmation and safety](confirmation-and-safety.md#execution-guards)), while the memory
module's retrieval filter drops a memory on `withdrawn` or `suppressed` and not on `absent`
([memory](memory.md#retrieval-fr-27-fr-30-criteria-27-and-31)).

**The purpose vocabulary is a closed enum** in `packages/contracts` in release one: `respond`,
`follow_up`, `share_with_referral`, `internal_analysis`. Modules cannot extend it and no setting
widens it; a specialist module needing a new purpose before the framework-proof phase decides the
extension mechanism is blocked, and this is recorded as accepted. The external correction flow for
copies held by other systems is out of release one, as the requirements' disposition of question
19 records.
