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
round-trip test also exercises Relationships through the real archive format. Signed website intake is available as described below. Import runners, email adapters,
live source activation and the first external handoff destination remain separate work.

## Workspace UI and manual intake

The composed Leads surface provides a paginated opportunity list, pipeline board, source
and note detail, allowed stage changes, qualification, saved follow-up drafts, manual capture
and processing receipts. The main navigation has one entry per module; list, pipeline,
capture and connection controls stay inside Leads. Board counts describe the current page,
not the entire pipeline. Drafts never send a message.

In a workspace where an owner has installed and enabled Relationships and Leads:

1. Open **Connections**, create a pipeline, and route the seeded manual connection to it.
   Leaving routing at record-only retains evidence without creating opportunities.
2. Open **Capture inquiry** and paste an email, message or conversation. Review and
   correct the suggested title and contact details, choose the funnel, then capture.
   Suggestions use explicit labels and unambiguous email addresses, not a model;
   ambiguous details stay blank. Original text remains in the intake payload.
   The detailed form remains available under **Enter details manually instead**.
   Acceptance returns a receipt immediately; a running worker processes it asynchronously.
3. Open the receipt to check processing and follow the actual opportunity link. A record-only
   result offers an explicit opportunity-creation form instead of claiming creation occurred.
4. Review the original source separately from notes, assess evidence against an objective,
   choose an allowed next stage and save a follow-up draft.

Connection settings are owner-only and expose no signing handles. The simple routing form
edits only an empty rule set or one unconditional create/record-only rule; advanced rules
remain readable and require the operation API. Routing edits include the ordered rule IDs
so a stale form cannot overwrite newer configuration. Opportunity edits use record revisions.

Appearance offers GreenStream dark (default), Novadiem dark and Novadiem light. A host-local
cookie remembers the selection. The Novadiem pair shares Sora typography and corner geometry;
confirmation chrome stays identical across all themes.

The flagship overlay loads the Leads and Relationships packages and routes the Leads host.
This only makes the surface available: deployment does not install or enable either module
in an existing workspace, select a pipeline, or connect live inquiry traffic. Authenticated
transport activation remains a separate operator step.

The interface participates in the existing routing-literal, platform-only, legacy-name,
fixture-provenance and module-web-boundary gates (criterion 68's regression scope). These
checks do not claim that the complete phase-three acceptance matrix or a live funnel has
been demonstrated.

Agents can submit the same flat inquiry facts through `leads_capture`, keeping
original text in `body.message`. To add only a person or organization, use
`relationships_create_contact` instead; contact creation is independent of Leads.
Website and email adapters call intake directly and do not require an agent runtime;
those transports are not implemented by the paste UI.


## Signed website intake

An owner can create a connection through `leads_connection_create_webhook` (operation
`leads.connection.create_webhook`) with a name, existing `funnel_ref` and optional
`campaign_ref`. The connection pins the seeded flat JSON mapping. Configure its
routing in **Connections** to create opportunities in a chosen pipeline; otherwise
accepted requests produce observations only. Creation does not assert verified email,
authenticated subject identity, or permission to contact someone.

Creation returns the connection reference and key generation, never key material or a
secret-store handle. The host operator hands the key to the website's **server**:

```sh
mkdir -m 700 /tmp/website-handoff
rheo connector export-key CONNECTION_UUID --output /tmp/website-handoff/website-key
```

Run this on the configured core host with its private data root and database access.
The destination directory must already be owned by that OS user with mode 0700.
The export writes a new mode-0600 file and refuses to overwrite an existing file.
Transfer its exact ASCII bytes into the website server's private secret store; do not
hex-decode the value. Remove the temporary handoff file after configuring the sender.
Never embed this key in JavaScript sent to visitors. A static website needs a server
or edge function to sign submissions. No agent runtime is involved in delivery.

POST UTF-8 JSON to `/api/v1/intake/webhook/CONNECTION_UUID` on the configured API
host. Path-mode deployments replace `/api` with their configured API prefix. Send
`Content-Type: application/json`, no content encoding, and no query parameters.
The hard body limit is 256 KiB; a lower workspace payload limit also applies.
Use a stable event ID in the signed body and the mapping's literal dotted keys:

```json
{"event_id":"submission-123","subject":"Website inquiry","person.name":"Example Person","person.email":"person@example.com","message":"Please send more information."}
```

For example, the website server can construct the signature in Python:

```python
import hashlib
import hmac
import json
import time

body = json.dumps(submission, separators=(",", ":")).encode("utf-8")
timestamp = str(int(time.time()))
signature = hmac.new(
    key_bytes, timestamp.encode("ascii") + b"." + body, hashlib.sha256
).hexdigest()
headers = {
    "Content-Type": "application/json",
    "X-Rheo-Timestamp": timestamp,
    "X-Rheo-Signature": "v1=" + signature,
}
# Send these exact body bytes with these headers from the server.
```

Timestamps must be within the replay window (default 300 seconds). The optional
`X-Rheo-Event-Id` header must equal the signed `event_id`; it cannot override it.
Without `event_id`, the receiver uses the SHA-256 of the exact body as identity.
JSON objects with duplicate keys or non-finite numbers are refused.

HTTP 202 returns `receipt_ref` and `outcome` (`accepted` or `duplicate`); it means
acceptance is committed and asynchronous processing is queued. Read the receipt to
confirm processing. On a lost response, retry with the same event ID and exact body,
and a fresh timestamp/signature. A changed body under the same event ID returns
409 and records a conflict. Authentication refusals return 401, malformed input 422,
oversized bodies 413, unsupported media 415, and temporary storage failures 503.

Owners use `leads_connection_rotate_secret` with `connection_ref` and optional
`overlap_seconds` (default zero; bounded by workspace settings), then the operator
exports the new generation to a new file. Rotation retains at most one previous key.
`leads_connection_revoke_secret` ends that previous-key overlap immediately;
`leads_connection_revoke` revokes the whole connection permanently. Acceptance and
worker processing recheck the generation: zero-overlap rotation, expired overlap or
revocation can refuse already queued deliveries. Creation and rotation are not
idempotent; inspect connections before retrying an uncertain administrative result.
A restored connection needs a fresh credential. Rotation can supply it in the same
workspace; a copy restored into another workspace must create a new connection.
