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
- Turn on the optional job-search capability: a job feed, pasted postings, scoring
  against your profile, a review queue for held jobs, and application drafts with
  priced milestones. Leads opens on its job queue.
- Refer to an opportunity by its short ID, the last 8 characters of its id (longer when
  two share a tail), in links, screens and tool calls.
- Do all of it from an agent through the `leads_*` tools, or in the web shell.

## How it works

### Three decisions stay separate

Has this delivery been seen before? Is this person someone we already know? Does this
evidence belong to an existing opportunity? Each has its own rule and its own scope, and
none is allowed to stand in for another.

### Acceptance in one transaction

Every transport ends in one operation, `leads.intake.accept_delivery`:

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

### Processing happens on the worker, with no model anywhere

The worker rechecks the connection and its key generation, applies the mapping version
pinned on the receipt (never the current one), and creates the observation with one row
per field naming its source path and transform. It asks Relationships to resolve the party
through a registered operation. Then the connection's routing rules run in order: the
first enabled match wins, and no match means the observation is kept unlinked.

### The envelope does not trust the sender

Every transport produces the same CloudEvents-compatible observation envelope. Its
`source` is assigned by the server from the connection, never taken from the payload. A
payload that claims a tenant, a destination or a permission set is kept as evidence and
read for nothing else.

### Field derivation

An opportunity's fields are rebuilt from all of its linked observations, replayed in the
order they happened, so arrival order does not matter. A new value replaces a source-owned
field only if its observation is at least as complete and its connection's priority at
least as high. A thin, late email cannot overwrite a rich posting. An explicit clear does
invalidate older evidence. Anything a person edits is marked as theirs and intake never
touches it again; stage and notes live in columns intake does not write.

### Pipelines are versioned presets

A preset defines stages, allowed transitions, fields required before entering a stage,
extension fields (`ext.<namespace>.<field>` with a type and a sensitivity tier), the
qualification rubric and draft templates. That is the whole surface: a preset cannot run
scripts, write another module's records, change a safety class or name a model. Editing a
preset makes a new immutable version. An opportunity keeps the version it was created
under for everything with meaning; only stage labels are read from the newest version, so
a rename shows everywhere at once. Moving an opportunity to a new version is an explicit
operation with a stage map, and fields the new version dropped stay readable as orphaned.

### Qualification

An assessment records its objective, rubric version, the evidence it read, the opportunity
revision it read, and four separate dimensions: fit, intent, evidence completeness and
urgency, each `high`, `medium`, `low` or `needs_information`, with an uncertainty and an
explanation. There is no combined score, except that job scoring adds a 10 to 100
score to the assessments it writes ([below](#job-scoring)). If a model helped, its
model id and prompt version are recorded. An evidence digest tells the interface when new evidence has
actually arrived, so notes and stage changes never prompt a reassessment. Earlier
assessments are never replaced.

### Handoffs are tracked, and safe to retry

`leads.handoff.request` writes a handoff with its purpose, destination and a bounded
snapshot of the payload, unique on the opportunity and an idempotency key. It needs a
qualification first, and records which one and whether its evidence has since changed.
The same key with the same payload returns the same handoff; with a different payload it
is refused. With no destination the handoff is recorded as `unavailable` and nothing
happens anywhere. With Current as the destination, the handoff goes `pending` and asks
you to approve one new Current item; nothing is created until you approve. If Current is
off or unavailable, the handoff is recorded as `unavailable` and you can restage it once
Current is back. A handoff that failed with no effect can be restaged too, each time with
a new approval; one that succeeded, was refused, was cancelled or has an unknown outcome
cannot. Leads remembers which Current project the item was filed under, even after the
item is deleted. Several intentional handoffs from one opportunity are several records.
There is no `converted` flag. On screen the request form and its history sit in a
closed "Send to Current" section at the foot of an opportunity's page; job-search
opportunities do not offer it.

Maintainers can run this journey on a disposable synthetic stack; see
[the acceptance stack](../testing/leads-current-acceptance.md).

### Contact permission is its own record

Showing interest is not permission to contact. A grant names the person, the purpose, the
channel, the recipient scope and the disclosure version, and is created only by an
explicit operation. A withdrawal is recorded even when nothing was ever granted. A
suppression means never contact, for any purpose. A check resolves the person through
every merge alias and answers `suppressed`, `withdrawn`, `permitted` or `absent`. An
external action to a person requires `permitted`; a memory about a person is hidden for
that purpose on `withdrawn` or `suppressed`.

### Erasure and tombstones

Deleting an observation removes its fields and payload, re-derives the opportunity's
fields from what remains, and keeps the receipt with only its identifier, digest, length
and times. If the sender delivers the same event again, the receipt catches it and records
a conflict with no body, so the erased person's data cannot land a second time.

### The job-search capability

An owner turns it on per workspace (`leads.capabilities.jobsearch`). It adds a
job-search preset (review, qualified, applied, interviewing, then hired, lost or passed)
with seventeen `ext.jobsearch.*` fields, such as the posting id and link, budget,
proposal count, the client's spend, rating and hire rate, and whether the client invited
you, so the Leads core gains no job column. None of it sends anything; applying is
always yours.

### The job feed

A job feed collects postings from a provider's remote MCP server every two hours, with
the owner's one-time consent, and delivers them through ordinary intake. By default a
run reads best matches, the newest postings, and one page of postings above 50
proposals to keep tracked jobs current; an owner can replace that list, keyword
searches included, with the `leads.jobfeed.presets` setting. A posting at or under 50
proposals that you have not applied to is read in full and delivered; it attaches to
the opportunity that already holds its posting id, or creates one. A run makes at most
60 full reads. A posting it could not read (that limit, the run's deadline, or a failed
read) only refreshes an opportunity that already holds it; a later run that finds it
again can read it then. Each run also reads one page of your pending invitations. An
invited job is always delivered, whatever its competition, and is tagged Invited in the
queue.

A run that finishes or stops leaves one row of counts and fixed failure codes, never a
posting, in `leads.jobfeed_run`, kept 90 days, so a stretch of failed reads can be
diagnosed after the worker's logs are gone. A run whose connection was revoked or
reconnected meanwhile, or that hit an internal error, leaves none. You can also paste a
copied search page or one full posting (`leads.jobfeed.paste`); it converges on the same
opportunity the feed made.

### Job scoring

Scoring starts once an owner sets the job-search profile (`leads.jobprofile.set`): the
candidate's evidence and a scoring policy, kept as the workspace's own data. A sweep
every 30 minutes, and after each delivery, works in up to three steps:

1. Tier 1 applies fixed client filters (verified payment, spend, rating, hire rate, at
   most 50 proposals). An unknown value never fails a clause. A job it holds gets fit
   `low` with the failing clauses and no score.
2. An optional pre-filter, off unless both the operator and the workspace owner turn it
   on, asks Jev typed questions about the posting and holds a job only when its
   confident estimate falls below the workspace threshold (50 by default). A provider
   failure falls through to the next step. Invited and promoted jobs skip it.
3. Tier 2 asks a model, with no tools, to judge the posting against the profile under a
   fixed contract whose explanation is built only from the evidence it cites. It
   answers 1 to 10 and the stored score is ten times that, so scores run 10 to 100.

Held jobs appear in Leads > Review queue with a badge for each clause that held them.
An owner can promote one (`leads.jobreview.promote`): the override is its own row, and
the next sweep scores the job like one that passed. Leads > Scoring shows what scoring
is doing: whether a call is running or paused on a usage limit, the queue, recent
imports by stage, the job feed's last runs, and a log of every model call with its
tokens, time and outcome but no text and no cost figure. From there an owner can end a
usage-limit wait early (`leads.jobscoring.retry_waiting`, attempts unchanged) or put
failed jobs back in line (`leads.jobscoring.retry_failed`).

### Proposals and pricing

With a proposal profile set (`leads.proposalprofile.set`), `leads.jobproposal.request`
queues a draft cover letter, by default one letter that answers every screening
question. A worker drafts it in four stages: analysis, drafting, an accuracy check
against the profile with one corrective redraft, and a style pass. With a pricing
section in the policy, `leads.jobpricing.request` turns the posting into priced
milestones at your day rate, never from the client's budget, or says the posting is too
vague to price. You edit sections, keep reusable answers in an answer library, and
reach the same operations from an agent through the ten `leads_*` proposal tools. The
profiles themselves stay off MCP.

## Why it's built this way

Any funnel works without a code change in the core. A funnel is a mapping plus routing
rules against one contract, so two unrelated sources are two mappings against one schema.
Adding a source never adds a column.

The intake path is also the most hostile input the system sees. It authenticates before it
writes, stores before it acknowledges, and treats everything inside the payload as
evidence rather than instruction. The unique receipt makes a sender's retry harmless, and
derivation shows which source won while keeping your own edits on top.

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
  [job paste](../../modules/leads/src/rheo_leads/jobpaste),
  [scoring](../../modules/leads/src/rheo_leads/scoring),
  [proposals](../../modules/leads/src/rheo_leads/proposals),
  [short IDs](../../modules/leads/src/rheo_leads/short_ids.py),
  [Jev setup](../../runtimes/README.md#jev-typed-judgments),
  [screens](../../modules/leads/web)
- Tests: [intake](../../tests/postgres/test_leads_intake.py),
  [website intake](../../tests/postgres/test_website_intake.py),
  [pipelines](../../tests/postgres/test_leads_pipeline.py),
  [follow-ups](../../tests/postgres/test_leads_followups.py),
  [job scoring](../../tests/postgres/test_job_scoring.py),
  [pre-filter](../../tests/postgres/test_job_prefilter.py),
  [review queue](../../tests/postgres/test_job_review.py),
  [proposals](../../tests/postgres/test_job_proposals.py),
  [job feed delivery](../../tests/postgres/test_jobfeed_delivery.py),
  [job feed runs](../../tests/postgres/test_jobfeed_run.py),
  [Current handoff](../../tests/postgres/test_leads_current_handoff.py),
  [module tests](../../modules/leads/tests)
