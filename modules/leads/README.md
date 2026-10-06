# Leads

Signals, source observations, opportunities, configurable pipelines, qualification,
contact-purpose permissions and tracked handoffs. Job search is optional and is not
part of this module's core workflow. Leads requires Relationships; Recallatron is
optional.

Installation seeds a manual connection, mapping, “General inquiries” funnel and
version one of the inbound-services preset. An owner explicitly creates a pipeline
and configures its connection's ordered routing rules. With no matching rule, intake
records the observation without creating an opportunity. Rules can create a new
opportunity or attach to the newest open opportunity for the same canonical party
in a selected pipeline. Delivery deduplication, party matching and opportunity
matching remain separate decisions.

Acceptance queues processing; it does not synchronously create a party or opportunity.
The worker rechecks the connection, applies its pinned mapping, creates an observation,
resolves identity through Relationships' registered service operation, applies routing,
and publishes the result in one transaction. Its operation grants come only from the
registered subscription. Trusted subject/email declarations control automatic identity;
ordinary source text is evidence and never an instruction or permission grant.

The opportunity pins its preset version and stable stage ID. Display labels can change
without changing that version's transition rules or terminal outcome. Preset edits
publish a new immutable version; migration is an explicit revision-checked operation
on one opportunity with a stage map. Removed extension fields remain readable and
exportable as orphaned fields. User edits, stages and notes survive new intake. Source
fields retain their winning observation and are deterministically re-derived from the
linked set; see [field derivation](../../docs/architecture/intake-and-events.md#field-derivation-fr-37).

The `leads_*` MCP tools cover capture, ingest-link, get/list/search, evidence reads,
updates, stage transitions, deterministic qualification, drafts and tracked handoffs.
Qualification records the objective, evidence, input revision and rubric/preset version.
No model participates in the shipped assessor. Drafts are internal records. A handoff
stores a bounded snapshot and idempotency binding to its exact body, destination and
purpose; without a destination it records `unavailable`, creates no external effect and
does not change the opportunity's disposition.

Permission records are explicit and scoped by party, purpose and channel. Withdrawal
and suppression deny subsequent permission checks. Recallatron's existing eligibility
check consumes the registered permission operation when installed. `ContactPermissionGuard`
rechecks permission under the lifecycle lock at execution; a test-only recording sink
proves a held action cannot execute after withdrawal. No production destination or
external-action operation is registered in this slice.

Observation and opportunity erasure require owner approval through core. Deleting an
observation clears its intake payload/conflict bodies, retains a delivery tombstone,
removes affected derived assessments/drafts/handoffs and re-derives surviving source
fields while preserving user edits. Deleting a party removes its links and permission
content. Core cancels reference-bound queued work and held approvals and invalidates
held exports; artifact deletion runs after commit with durable retry.

With Recallatron enabled, confirmed source erasure also removes directly linked
memories and their derived descendants, embeddings and index entries. The cascade
uses canonical references and Recallatron's own deletion rules; unrelated memories
survive and any participant failure rolls back the transaction. Leads also works in
workspaces where Recallatron was never installed.

Export format two covers all 35 owned tables. Restore preserves source identities,
replaces only a fresh seed and strips deployment-local signing handles. The populated
round-trip test also exercises Relationships through the real archive format. Webhook
receivers, import runners, authenticated transport work, UI, live module activation and
the first external handoff destination remain separate slices.
