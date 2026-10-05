# Leads

Reserved for signals, source observations, opportunity development, qualification,
configurable pipelines, and tracked handoffs. Job search is optional.

Delivery deduplication, party matching, and opportunity matching are separate
decisions. Sources and professional workflows vary independently. No opportunity
must become a project merely because it was ingested or qualified.

The first delivery slice supplies receipt acceptance, manual capture, deterministic
field mapping, the processing consumer, connection health, and an intake erasure
participant. Installation seeds one manual connection, its mapping, and a “General
inquiries” funnel. `leads_capture` requires an explicit funnel reference. Acceptance
queues processing; it does not create a party or opportunity.

A receipt pins mapping version and acquisition attribution. A duplicate event with
the same digest adds no work; a different digest records a conflict. The receipt,
payload, received event, fan-out, and acceptance health update commit together. The
caller acknowledges only after dispatch commits. A best-effort post-commit due mark
wakes the worker; if that mark is lost, the configured reconciliation interval
(default 900 seconds) bounds discovery delay.

The numbered processing steps are connection recheck, pinned mapping, and observation
creation. Future party resolution and routing fit before finalization. Finalization
marks the receipt processed and publishes an event with explicit `party_ref: null`.
Observation and receipt share a UUID under distinct record types, so the erasure
participant can find the receipt even after the owner has deleted the observation.
It removes payload and conflict bodies and retains the delivery identity tombstone.
Neither observation nor receipt exposes an owned-delete operation in this slice.

Health timestamps are null until an actual acceptance or processing occurs. Lag is
the nonnegative whole-second difference between those timestamps, or zero while one
is absent. Failed delivery counts are read from core for this connection's receipts;
they are not a second stored counter. Generic record labels contain no captured facts.

Webhook and file-import connector declarations reserve the service seam; receivers,
signing-secret management, import jobs, configuration screens, relationship matching,
and opportunity routing are later work. This release enables no module in an existing
workspace automatically.

The version-one export describes all thirteen tables. Restore requires a fresh seed,
replaces that seed with the source identities, and strips deployment-local signing
handles; active webhook connections restore as needing credentials. Full operational
export/restore acceptance, including resuming pending deliveries, belongs to the later
platform restore work.
