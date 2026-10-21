# Leads

## What it is

The opportunity-development module. It takes signals from wherever your work comes from,
keeps the evidence, works out who is behind it, and carries the opportunities worth
pursuing through a pipeline you choose, to an outcome.

"Leads" is deliberately broader than jobs. A consultation request, a referral, an
assessment result, a product signup and a freelance posting all fit, without an
employment-shaped schema. Leads keeps these ideas apart:

| Concept | Meaning |
| --- | --- |
| Connection | One configured source: its transport, field mapping, routing rules, credential and health |
| Funnel | Where the person came from: a site, a campaign, an event, a referral programme. Separate from how the data was delivered |
| Observation | Evidence that something happened, with per-field provenance |
| Party | A person or organization, owned by [Relationships](relationships.md) |
| Opportunity | A possible outcome worth pursuing, in one pipeline, with an owner, a stage and its evidence |
| Qualification | A versioned assessment of an opportunity against an objective and a rubric |
| Handoff | A tracked request to create related work elsewhere, with an approved payload snapshot |

An observation need not become an opportunity, and an opportunity need not become a
project. It might end in a referral, a nurture stage or a closed outcome.

## What you can do with it

- Connect your own funnel: a signed webhook from your website's server, a CSV or JSON
  import, an email poller, a scheduled feed, or a pasted email or conversation in the
  capture screen.
- Route what arrives: create an opportunity in a pipeline, attach the evidence to the
  newest open opportunity for the same person, or just keep the record.
- Work opportunities on a list or a pipeline board: change stage, add notes, record a
  qualification, schedule one dated next follow-up, save a draft follow-up.
- Hand an opportunity to another destination, such as new work in [Current](current.md),
  with an approved snapshot of exactly what goes.
- Record, withdraw and check contact permission per person, purpose and channel, or
  suppress all contact with someone.
- Turn on the optional job-search capability: a job feed, scoring against your profile,
  and application drafts.
- Do all of it from an agent through the `leads_*` tools, or in the web shell.

## How it works

**Three decisions stay separate.** Has this delivery been seen before? Is this person
someone we already know? Does this evidence belong to an existing opportunity? Each has
its own rule and its own scope, and none is allowed to stand in for another.

**Acceptance is one transaction, and the sender's retry is safe.** Every transport ends
in one operation, `leads.intake.accept_delivery`:

1. Refuse a body over the workspace's size limit (256 KiB by default) and store nothing.
2. Check the connection is active and belongs to this workspace.
3. Insert a receipt, unique on the connection and the sender's event id. A repeat with
   the same content is a `duplicate`: nothing is stored and the sender is acknowledged
   as if fresh, because its first attempt already succeeded. A repeat with different
   content is a `conflict`: it is kept for review and shown on the connection's health,
   and the existing observation is untouched.
4. Store a bounded copy of the payload.
5. Write an outbox event for processing.
6. Commit, and only then acknowledge (`202` for a webhook).

**Processing happens on the worker, with no model anywhere.** The worker rechecks the
connection and its key generation, applies the mapping version pinned on the receipt
(never the current one), and creates the observation with one row per field naming its
source path and transform. It asks Relationships to resolve the party through a
registered operation. Then the connection's routing rules run in order: the first
enabled match wins, and no match means the observation is kept unlinked.

**The envelope does not trust the sender.** Every transport produces the same
CloudEvents-compatible observation envelope. Its `source` is assigned by the server from
the connection, never taken from the payload. A payload that claims a tenant, a
destination or a permission set is kept as evidence and read for nothing else.

**Field values are derived, not overwritten.** An opportunity's fields are rebuilt from
all of its linked observations, replayed in the order they happened, so arrival order
does not matter. A new value replaces a source-owned field only if its observation is at
least as complete and its connection's priority at least as high. A thin, late email cannot
overwrite a rich posting. An explicit clear does invalidate older evidence. Anything a
person edits is marked as theirs and intake never touches it again; stage and notes live
in columns intake does not write.

**Pipelines are versioned presets.** A preset defines stages, allowed transitions, fields
required before entering a stage, extension fields (`ext.<namespace>.<field>` with a type
and a sensitivity tier), the qualification rubric and draft templates. That is the whole
surface: a preset cannot run scripts, write another module's records, change a safety
class or name a model. Editing a preset makes a new immutable version. An opportunity
keeps the version it was created under for everything with meaning; only stage labels
are read from the newest version, so a rename shows everywhere at once. Moving an
opportunity to a new version is an explicit operation with a stage map, and fields the
new version dropped stay readable as orphaned.

**Qualification is a record, not a score.** An assessment records its objective, rubric
version, the evidence it read, the opportunity revision it read, and four separate
dimensions: fit, intent, evidence completeness and urgency, each `high`, `medium`, `low`
or `needs_information`, with an uncertainty and an explanation. There is no combined
score. If a model helped, its model id and prompt version are recorded. An evidence
digest tells the interface when new evidence has actually arrived, so notes and stage
changes never prompt a reassessment. Earlier assessments are never replaced.

**Handoffs are tracked, and safe to retry.** `leads.handoff.request` writes a handoff
with its purpose, destination and a bounded snapshot of the payload, unique on the
opportunity and an idempotency key. The same key with the same payload returns the same
handoff; with a different payload it is refused. With no destination the handoff is
recorded as `unavailable` and nothing happens anywhere. With one, the handoff goes
`pending` and calls the destination's own operation, which is external class, needs its
own approval, and writes its outcome back. Several intentional handoffs from one
opportunity are several records. There is no `converted` flag.

**Contact permission is its own record.** Showing interest is not permission to
contact. A grant names the person, the purpose, the channel, the recipient scope and the
disclosure version, and is created only by an explicit operation. A withdrawal is
recorded even when nothing was ever granted. A suppression means never contact, for any
purpose. A check resolves the person through every merge alias and answers
`suppressed`, `withdrawn`, `permitted` or `absent`. An external action to a person
requires `permitted`; a memory about a person is hidden for that purpose on `withdrawn`
or `suppressed`.

**Erasure leaves a tombstone, not a hole.** Deleting an observation removes its fields
and payload, re-derives the opportunity's fields from what remains, and keeps the
receipt with only its identifier, digest, length and times. If the sender delivers the
same event again, the receipt catches it and records a conflict with no body, so the
erased person's data cannot land a second time.

**Job search is a capability, not a module.** An owner turns it on per workspace. It adds
a job-search preset (review, qualified, applied, interviewing, then hired, lost or
passed) with sixteen `ext.jobsearch.*` fields, so the Leads core gains no job column. A
job feed collects postings from a provider's remote MCP server every two hours, with the
owner's one-time consent, and delivers them through ordinary intake. Scoring runs fixed
client filters first, then asks a model to judge the posting against the owner's profile
under a fixed contract whose explanation is built only from the evidence it cites.
Proposal drafts go through analysis, drafting, an accuracy check against the profile and
a style pass. None of it sends anything; applying is always yours.

## Why it's built this way

Any funnel works without a code change in the core. A funnel is a mapping plus routing
rules against one contract, so two unrelated sources are two mappings, not two branches.
Adding a source never adds a column.

The intake path is also the most hostile input the system sees. It authenticates before
it writes, stores before it acknowledges, and treats everything inside the payload as
evidence rather than instruction. Senders retry; the unique receipt makes that harmless.
Sources disagree; derivation makes the winner explainable and keeps your own edits on
top.

## Where to look

- Specification: [intake contract](../architecture/intake-and-events.md#part-one-the-intake-contract),
  [contact permission](../architecture/intake-and-events.md#contact-permission-records-fr-46),
  [pipelines and opportunity records](../architecture/confirmation-and-safety.md#pipeline-presets-and-their-limits),
  [job search design](../architecture/later-phases.md#phase-4-the-optional-job-search-workflow),
  [module guide](../../modules/leads/README.md)
- Code: [module](../../modules/leads/src/rheo_leads),
  [intake](../../modules/leads/src/rheo_leads/intake),
  [pipeline service](../../modules/leads/src/rheo_leads/pipeline_service.py),
  [contact permission](../../modules/leads/src/rheo_leads/permissions.py),
  [job feed](../../modules/leads/src/rheo_leads/jobfeed),
  [scoring](../../modules/leads/src/rheo_leads/scoring),
  [screens](../../modules/leads/web)
- Tests: [intake](../../tests/postgres/test_leads_intake.py),
  [website intake](../../tests/postgres/test_website_intake.py),
  [pipelines](../../tests/postgres/test_leads_pipeline.py),
  [follow-ups](../../tests/postgres/test_leads_followups.py),
  [job scoring](../../tests/postgres/test_job_scoring.py),
  [job feed delivery](../../tests/postgres/test_jobfeed_delivery.py),
  [module tests](../../modules/leads/tests)
