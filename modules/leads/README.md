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
updates, stage transitions, qualification, drafts and tracked handoffs.
Qualification records the objective, evidence, input revision and rubric/preset version.
Calling `leads_qualify` without `assessment` runs the deterministic completeness
check; fit, intent and urgency remain `needs_information`. To record a judgment,
pass `assessment` with `fit`, `intent`, `urgency`, `evidence_completeness` (each
`high`, `medium`, `low` or `needs_information`), `uncertainty` (`high`, `medium`,
`low`), a nonblank `explanation`, and `author` (`human` or `model`). Model-assisted
callers must include both `model_id` and `prompt_version`; these are caller-declared
provenance, not proof of a server-side model invocation. The service invokes no
model and binds actor, linked evidence, input revision and rubric itself. Stale
submissions are refused. Every assessment remains in history; saving one neither
moves the stage nor grants contact permission. The detail screen supports recording
a person's judgment and reviewing prior assessments.

Drafts are internal records. A handoff
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
removes affected derived assessments/drafts/follow-up reminders/handoffs and re-derives surviving source
fields while preserving user edits. Deleting a party removes its links and permission
content. Core cancels reference-bound queued work and held approvals and invalidates
held exports; artifact deletion runs after commit with durable retry.

With Recallatron enabled, confirmed source erasure also removes directly linked
memories and their derived descendants, embeddings and index entries. The cascade
uses canonical references and Recallatron's own deletion rules; unrelated memories
survive and any participant failure rolls back the transaction. Leads also works in
workspaces where Recallatron was never installed.

Export format seven covers the owned tables, including dated follow-up history,
each assessment's evidence digest and the job feed's connection status, posting
dedupe rows and account choices. It never carries the job feed's token reference,
registered client, run claim, backoff or pending authorization: a restored collector
reads "authorization required", has no schedule, and registers a fresh client when the
owner reconnects. Older-format archives require a matching older
host for restore; the core refuses format mismatches. Take a full database backup
before upgrading from a previous Leads schema; rollback means restoring that backup
with the previous image, not downgrading the additive 0005_followup_reminders or
0006_assessment_evidence_digest migrations. Restore preserves source identities,
replaces only a fresh seed and strips deployment-local signing handles. The populated
round-trip test also exercises Relationships through the real archive format. Signed website intake is available as described below. Import runners, email adapters,
live source activation and the first external handoff destination remain separate work.

## Job-search capability

Job search is an optional capability, not a separate module. It is off unless the
workspace turns it on: an owner sets `leads.capabilities.jobsearch` to `true` with
`core.settings.set`. The deployment default permits that opt-in, and an operator who
sets the key to `false` vetoes it for every workspace. While it is off,
`leads.pipeline.create` refuses the `job_search` preset with `capability_disabled`, and
the pipeline form doesn't offer it at all. Job-search pipelines and their records stay
readable and exportable either way.

Migration `0007_jobsearch_preset` seeds the **Job search** preset:
- stages review → qualified → applied → interviewing, plus terminal hired, lost and
  passed;
- a `job_search` rubric;
- sixteen `ext.jobsearch.*` extension fields: platform, posting id and URL, job type,
  budget range, proposal count (with exactness and observation time), posting date,
  client payment verification, spend, rating, reviews and hire rate, and applied.

Provider-specific parsing belongs to a source's mapping, never to core columns.

## Job feed

The job feed collects job postings from a provider's remote MCP server into one Job
search pipeline. It needs the job search capability on, a Job search pipeline, and a
host operator who has configured the provider and turned the connector client on (see
[Job feed collector](../../deploy/README.md#job-feed-collector) for the operator
side). Each workspace has at most one job feed connection.

**What it collects.** Every two hours the worker runs three searches as your chosen
account: best matches, the newest postings, and postings above 50 proposals. A posting
with 50 or fewer proposals that you have not applied to is a discovery: the collector
reads its full description and delivers it, and routing either attaches it to the
opportunity that already holds that posting id or creates a new one. A posting no
opportunity tracks is otherwise skipped. A tracked posting is delivered again only
when one of its fields changed, so its proposal count, budget and client record stay
current. Collected values fill the `ext.jobsearch.*` fields; no person or organization
is created. Postings arrive through intake like any other source, so receipts,
observations and processing work as described above.

**Connections.** Open **Connections** in Leads. Before a connection exists, the Job
feed block offers **Connect job feed** with a choice of Job search pipeline. Connecting
sends you to the provider to sign in and consent, then back to a page that says "Job
feed connected"; return to Leads from there. Once connected, the block shows the state,
the last completed check, the earliest next check, health, the account, and the last
run's counts under **Last run**. **Reconnect** and **Revoke access** are there too.
These controls work only in your own browser session; there is no MCP tool for them.

**Finishing by pasting.** Some providers only send you back to an address on your own
computer, and the host operator then sets the deployment to the loopback redirect. On
such a deployment, after you approve at the provider your browser shows a page that
cannot be reached; that is expected. Copy the full address from the address bar (it
starts with `http://127.0.0.1`), come back to Connections, and paste it into
**Finish connecting** on the Job feed card, then select the button. The form appears
while a sign-in you started is waiting, for 10 minutes after you select Connect or
Reconnect. A refused paste changes nothing and says why; an address that is not the
one the provider sent you to can be pasted again, while an expired, declined or already
used sign-in needs a fresh Connect or Reconnect. The address is used once and not kept.

**Choosing an account.** Each run lists the accounts your login can act as. If exactly
one is a freelancer account and none is chosen yet, it is used. Otherwise health shows
**Choose an account** and the run collects nothing. Pick the freelancer account whose
jobs the feed should collect; the next check is scheduled no earlier than 60 minutes
after the last run started. The collector never falls back to another account: if the chosen
one disappears, appears twice or stops being a freelancer account, the run stops and
asks again. If no freelancer account is listed, reconnect with the login you use for
job search.

**Reconnecting.** Reconnect when the state reads "Authorization required" (the provider
refused the stored authorization, renewed it in a way that could not be saved, or
rejected this app's registration) or after the
operator changes the provider's address. A rejected registration is replaced at the
next reconnect, once. If a reconnect's registration fails or you decline at the
provider, a connected feed keeps collecting with its existing authorization, and health says the last
reconnect did not finish.

**Revoking.** **Revoke access** stops collection, ends this app's access here, and asks
the provider to revoke the token. The result says whether the provider confirmed it.
Collected opportunities are kept. It stays available while authorization is required,
and while job search is turned off: the Job feed card then says collection is paused
and offers **Revoke access** and nothing else.
The card keeps showing the earlier state until you reload the page.

**Close versus erase.** Closing an opportunity (moving it to hired, lost or passed)
keeps the posting attached: later sightings refresh the closed opportunity and never
open a new one. Erasing it removes the collector's memory of that posting, so a later
sighting is collected again and, if the posting still qualifies as a discovery, creates
a new opportunity. Erasing only an observation also removes that memory; the next
sighting is delivered again and refreshes the opportunity that still tracks it.

## Job scoring

Every open opportunity in a Job search pipeline gets a score you can triage by: the
job queue's pill shows it, and the queue's **Score** sort ranks by it.

**The profile.** Scoring starts once an owner sets the workspace's job-search profile
with `leads.jobprofile.set`: the candidate's evidence as plain text (career, skills,
and an optional `Portfolio:` section with one `- Name: text` paragraph per project)
and a scoring policy (goals, exclusions, preferences, economics, known gaps,
per-platform notes, and the tier-1 thresholds). The profile is the workspace's own
data. No package ships one, it is exported with Leads, and no log line or audit record
carries its text. `leads.jobprofile.get` returns it to an owner. Setting it again
bumps its version and turns the scoring schedule on. The scorer runs as the owner who
set it, on that owner's model login.

**Tier 1** applies the client filters before any model is asked: an established
client needs a verified payment method, at least $500 spent, a rating of 4.5 or more
and a hire rate of 40% or more; a new client (no spend and no reviews, both known)
needs only the payment method; more than 50 proposals holds any job back. An unknown
value never fails a clause, a rating of 0 (no rating yet) is unknown, and so is a 0% hire rate from a client with no reviews. A job held back is recorded as fit **low** with the
failing clauses as its reasons, and no score. Unknown payment is a condition to
confirm, not a failure.

**Tier 2** asks the model, through the `claude_cli` runtime with no tools, to judge
the posting against the profile under a fixed response contract: it picks candidate
evidence, numbered excerpts of the posting, recorded experience boundaries and fixed
review conditions by ID, and the explanation is written from the sources those IDs
name, never from model prose. The posting is untrusted evidence. The model answers 1
to 10 and the stored score is ten times that, so scores run 10 to 100. The fit band
follows the score: 80 to 100 high, 60 to 79 medium, below 60 low. A job whose full
description is missing is recorded as **needs information** with a pointer to paste
the full posting, and is scored once one arrives. Each score is a qualification record
with its model and prompt versions.

**When it runs.** A worker sweep (`leads.jobsearch_score`) runs every 30 minutes, after
each delivery into a Job search pipeline, and when the profile is set. It scores up to
`leads.scoring.max_per_run` jobs (default 10), newest first, and queues a follow-up
while more wait. A job is scored again when its title, description, price or payment
status changes, or when its proposal count, spend, rating, reviews or hire rate moves
into another bracket; a profile change does not rescore jobs that already have a score.
`leads.scoring.model_id` picks the model (empty uses the CLI's default).

**Failures.** A failure of the model login or the deployment (a usage limit, an
expired or refused login, a missing executable, a crashed or timed-out run) makes
every waiting job wait 15 minutes without spending an attempt. A failure about the job
itself, such as an answer outside the contract, retries with backoff; the fifth
attempt records a **needs information** assessment saying scoring failed, and the job
is tried again when the posting changes. Setting the profile again re-queues failed
jobs and every job without a score yet, so changed thresholds apply to held jobs.

**Who sees what.** The profile itself is owner-only. A score's reasons name the
projects and quote the recorded experience boundaries they cite, and those reasons
sit on the job's qualification record, which every workspace member can read.

**The review queue.** Leads > Review queue lists the open Job search jobs tier 1 holds
back, newest first, each with a badge for every clause that held it (unverified
payment, client spend, rating, hire rate, too many proposals), decided again against
the current profile. `leads.ui.queue` with `held_only` is its read; each item carries
a `hold` list of `{code, text}`. From a card you can open the posting, copy the ID,
dismiss the job, or **promote** it: `leads.jobreview.promote` records an override on
that job (`job_promotion`), and the next sweep sends it to the model like a job that
passed, with the clauses that held it kept on its assessment as conditions. The
override is its own row, so a rescore, a changed posting or a new profile never undoes
it; it is exported with the workspace and goes when the job is deleted. Only a job the
filters hold can be promoted (`not_held` otherwise), and promoting twice does nothing.

## Job proposals

A Job search opportunity can have a draft application: a cover letter and one answer
per screening question, kept as separate sections because an application form takes
each separately. Every proposal operation prepares text; none sends anything. Submitting
is always yours, on the platform.

**The proposal profile.** Drafting needs an owner to set the workspace's proposal
profile with `leads.proposalprofile.set`: the candidate's evidence for applications as
plain text (projects as `- Name (link): text` lines, capability areas, links) and a
policy: writing notes the drafter must follow (which project a tool belongs to, which
links to give), example letters to learn editorial moves from, vouched anecdotes it may
tell when a posting asks for a story, per-platform notes, and extra named terms the
accuracy check accepts. Like the scoring profile it is the workspace's own data: no
package ships one, it is exported with Leads, and no log line or audit record carries
its text. Drafts run on the model login of the owner who set it.

**Asking for a draft.** `leads.jobproposal.request` queues one, optionally with the
screening questions (one per line). A worker sweep (`leads.jobsearch_propose`) drafts
it in four stages that `leads.jobproposal.get` reports while it runs: **Analysis** reads
the posting against the full profile (what the client is hiring for, the angle, the
opening, firm requirements, gaps, story asks, embedded questions); **Drafting** writes
the letter and answers; **Checking** extracts what the draft claims and checks each
named tool, each property tied to a project, and each narrated incident against the
profile and the offered anecdotes, with one corrective redraft when something is
flagged; **Tidying** runs a style lint and one repair pass, kept only when it leaves
fewer findings. The posting is untrusted source material. When no questions were
entered, items the posting asks applicants to include become the questions.
`leads.proposals.model_id` picks the model for the letter and its repair, and
`leads.proposals.analysis_model_id` the one for the analysis and the check (empty uses
the CLI's default).

**Reading and editing.** `leads.jobproposal.get` returns the state, progress,
questions, sections, the whole draft as text, and the findings (accuracy, style, and
the analysis). `leads.proposalsection.set` edits one section and
`leads.jobproposal.update` replaces the whole draft from its text form (a text without
a `---` divider replaces only the letter). A new request refuses `draft_edited` when a
section was edited or taken from the library since the last draft, unless `force` is
set. `leads.jobproposal.delete` removes the draft, keeps the questions, and stops a
draft that is queued or running.

**The answer library.** `leads.answerlibrary.save` keeps an answer under its question,
normalised (case, spacing and surrounding punctuation), so one question has one entry.
`leads.answerlibrary.match` finds one, each section in `leads.jobproposal.get` names a
matching entry when there is one, and `leads.proposalsection.use_library` puts it into
the section.

**Pricing.** With a pricing section in the proposal policy (a day rate, the hours in a
working day, the hourly rate the profile lists and the lowest one you bid, optionally
the rate for a multi-week engagement, and your own rate context and delivery
calibration as text), `leads.jobpricing.request` asks the model for a milestone
schedule: each stage priced at the day rate per honest working-day, never from the
budget, with a due date; or "too vague" with the reason when the posting names no
concrete deliverable. Generating milestones chooses milestone pricing for the job. The
scope-fit warning is decided in code: only when the budget is a project total and the
priced scope reaches 1.2 times it. `leads.proposalmilestone.set` edits a milestone.
`leads.jobproposal.get` carries the pricing, the milestones, and the hourly rate for an
hourly apply form with its basis: the day rate spread over the working day when there
are milestones (so the form agrees with the letter's total), else a ladder by the
scope the analysis read (a day or two: your listed rate; a week: the day rate's
hourly equivalent; weeks: the multi-week rate; ongoing: your floor). A draft whose
posting asks for a quote and which has no milestones yet runs a **Pricing** stage
first; a draft of a job with milestones is given them as the only amounts it may
state. Milestone work runs in the same sweep as drafts, with the same failure rules.

**Failures.** A usage limit, a refused login or a missing runtime makes every queued
draft wait 15 minutes without spending an attempt. Any other failure (an unusable
answer, a crashed or truncated run, a run past its deadline) retries with backoff, and
the third attempt marks the proposal failed with its reason; requesting it again starts
over. A sweep is queued by each request and by its own follow-ups; a schedule every 30
minutes, on once a proposal profile is set, picks up anything left waiting. Deleting the evidence an opportunity rests on
removes its proposal, since a proposal quotes the posting.

## Dated next follow-up

Each opportunity has at most one pending reminder, owned by Leads. Call
`leads_schedule_followup` with the opportunity reference and current revision,
an action (1–2,000 characters), and a `due_on` calendar date (`YYYY-MM-DD`).
Rescheduling preserves the old reminder as superseded. `leads_get` returns
`followup` plus up to 20 resolved reminders in `followup_history`, newest first.
All reminder history remains in storage and exports.

`leads_resolve_followup` requires the opportunity reference/revision, pending
`reminder_id`, and `outcome` (`completed` or `cancelled`). Both operations allow
owners, members and appropriately scoped services; ordinary operation grants still
apply. Stale revisions cannot overwrite a newer reminder. Moving to a terminal
stage cancels the pending reminder; closed opportunities refuse scheduling.
Later source evidence leaves the user-selected reminder unchanged.

Pass `followup_due_by` to `leads_list` / `leads_search` for pending reminders due
on or before that date, ordered by due date then stable opportunity creation/ID.
The list and board expose the same cutoff and preserve it through pagination.
Dates have no time or timezone; callers choose their own calendar cutoff. With no
cutoff, the usual opportunity list includes records with or without a reminder.
These are actions to check in Leads, not notifications: scheduling sends no message,
enqueues no agent run and creates no Current task or contact permission.

## Workspace UI and manual intake

The composed Leads surface provides a paginated opportunity list, pipeline board, source
and note detail, allowed stage changes, qualification, dated follow-ups, saved drafts, manual capture
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
   choose an allowed next stage, schedule a dated next action and optionally save a draft.

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
idempotent; inspect `leads.ui.catalog` (owner view includes the current signing-key
generation) before retrying an uncertain administrative result.
A restored connection needs a fresh credential. Rotation can supply it in the same
workspace; a copy restored into another workspace must create a new connection.
