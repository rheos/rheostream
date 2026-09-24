# Memory: records, provenance, audience, and invalidation

**Part of:** the [architecture specification](README.md). Designs against D9, D11, FR 26 to
FR 30, FR 28's cascade, and criteria 24 to 33, 36, and the memory clause of 61. The retrieval
adapter's storage is in [storage](storage-and-workspaces.md#retrieval-adapter-d9-fr-30); this
document owns the records it indexes.
**Decision:** A17 (explicit audience and purposes with intersection on derivation; one
invalidation rule). See the [decision list](README.md#architecture-decisions).

## What a memory is, and is not

A memory is an explicitly stated note, fact, decision or summary, recorded manually, derived from
authorized records, or created conservatively from eligible sanitized transcript evidence by a
trusted automatic producer. Release one includes ambient automatic creation without telling
Recallatron to remember and without a per-note administered review. 1a1 supplies foundations; the
dedicated automatic-memory carrier follows retrieval and must complete before cutover. A memory is
never the authoritative domain record or an unbounded conversation archive.

It carries who may read it, for which purposes it may reach a model, where it came from, and
whether an age bound applies to it. A summary of an opportunity is not the opportunity, and every
link back is a reference resolved under the caller's permission (idea document).

## Entities

All in the `recallatron` schema. Every independently addressable table has `id uuid` (UUIDv7);
the four association tables (`memory_purpose`, `memory_mention`, `memory_link`,
`memory_embedding`) and `source_receipt` carry only their listed composite primary keys, with no
surrogate id column on any of them, and `embedding_state` is a single row keyed by a boolean.

| Table | Columns | Notes |
| --- | --- | --- |
| `memory` | `kind text`, `title text`, `body text`, `search_tsv tsvector`, `audience_kind text`, `audience_id uuid null`, `confidence numeric null`, `occurred_at timestamptz null`, `recorded_at`, `recorded_by_kind`, `recorded_by_id`, `origin text`, `revision integer`, `corrected_at null`, `superseded_by_id uuid null`, `invalidated_at null`, `invalidation_reason text null`, `source_namespace text null`, `external_source_key text null` | `kind` in `note`, `fact`, `decision`, `summary`. `search_tsv` is generated from `title` and `body` with a GIN index (the lexical index). `audience_kind` in `workspace`, `member`; `audience_id` is the account when `member` and null otherwise (check constraint). `confidence` is nullable and bounded 0 to 1. `origin` in `told`, `derived`, `migrated`. `revision` is at least 1. `superseded_by_id` is a nullable self-reference with `ON DELETE SET NULL` and names the memory that replaced this one. `invalidated_at` and `invalidation_reason` are written as a pair; the reason is one of `source_deleted`, `source_corrected`, `source_superseded`, and normal APIs never retain `source_deleted` content. The nullable pair `source_namespace`/`external_source_key` is trusted-ingest provenance, not a reference. Retention is not a column: a memory is retained while the workspace's age gate is off, and while the gate is on, a `workspace`-audience memory is retained while `recorded_at` is within `recallatron.retention.days` ([retention](#retention-fr-29-criterion-30)). |
| `memory_purpose` | `memory_id`, `purpose text` | Primary key both columns. The memory's purpose set, a non-empty subset of the closed vocabulary; a child table rather than an array because retrieval filters by it (house rule). |
| `memory_entity` | `kind text`, `name text`, `normalized_name text`, `ref text null`, `created_at`, `source_namespace text null`, `external_source_key text null` | `kind` in `person`, `organization`, `project`, `topic`, `place`, `thing`. `name` is required and `normalized_name` is derived from it deterministically. `ref` is an immutable optional canonical backing reference, set when the entity is created and resolved under permission on read. The nullable pair `source_namespace`/`external_source_key` is trusted-ingest provenance; **the columns and their export path exist and nothing in release one writes them** — every entity insert leaves both null, so an entity-keyed `source_receipt` is a shape the schema admits and no code path produces yet. Entity identity is its globally safe UUIDv7, not its name. Equal normalized names are permitted. Creation always creates an independent entity; selecting an existing entity requires an authorized canonical ref. Entity reads expose a label only through an eligible mention or readable backing ref. Deleting the final mention also deletes an entity with no backing ref. There is no uniqueness constraint on name or normalized name and no global name probe; the nonunique `(kind, normalized_name, id)` index only filters an entity set the caller is already eligible for. |
| `memory_mention` | `memory_id`, `entity_id`, `role text null` | Primary key `(memory_id, entity_id)`, with owning foreign keys to both sides. Which entities a memory is about; `role` is bounded. A mention of an entity that carries a `ref` is always accompanied by an `about` link to that ref in the same transaction ([provenance](#provenance-and-links)). |
| `memory_link` | `memory_id`, `ref text`, `relation text`, `created_at`, `supersession_lineage boolean` | Primary key `(memory_id, ref, relation)`; the memory foreign key is `ON DELETE CASCADE`. `relation` in `derived_from` (a source this memory was made from: another memory, or a record) and `about` (a record this memory concerns, including every party a source involves and every mentioned entity's record, written at write time). `supersession_lineage` defaults false, is **server-owned and immutable** — no caller sets or clears it — and is allowed only on a `derived_from` link to a memory reference. A marked row retains ancestry, not content, and does not require the source it names to still be live. No link is a foreign key into another schema. Provenance, the permission check, and the contact-permission filter all read this table and nothing else. |
| `memory_embedding` | `memory_id`, `model_id text`, `dimensions integer`, `vector vector(384)`, `embedded_at` | Primary key `(memory_id, model_id)` and a memory foreign key `ON DELETE CASCADE`; no surrogate id. The dense index: migration `0003_dense_retrieval` narrows the column to `vector(384)` and adds the HNSW cosine index `memory_embedding_vector_hnsw`. Rows exist only for live, non-invalidated memories and are written by the [embedding job](#the-embedding-job) and the rebuild, never inside the writing transaction. |
| `embedding_state` | `id boolean`, `embed_input_version integer` | One row per workspace (`CHECK (id)`), added by migration `0003_dense_retrieval` and written by the first [rebuild](#the-embedding-job), not by the migration. It records which composition of a memory's text the stored vectors were made from; a rebuild that finds it missing or different deletes every vector first. Internal and not exported. |
| `source_receipt` | `representation_type text`, `source_namespace text`, `external_source_key text`, `record_id uuid null`, `payload_digest bytea`, `state text`, `producer_kind text`, `authority_id uuid`, `principal_account_id uuid null`, `audience_kind text`, `audience_id uuid null`, `bound_purpose text null`, `source_recorded_at`, `source_expires_at` | Internal and `exportable`. Primary key `(representation_type, source_namespace, external_source_key)`; `representation_type` in `memory`, `entity`. `record_id` is the nullable original UUIDv7 of the representation the unit created. `payload_digest` is a 32-byte SHA-256. `state` in `active`, `noop`, `denied`, `erased`, `expired`, `orphaned`. The authority columns are immutable and normalized: `producer_kind` in `migration`, `rheo_runtime`, `claude_code_local`; `authority_id` a UUIDv7; `audience_kind` in `workspace`, `member` paired with `audience_id`; `bound_purpose` a nullable member of the closed vocabulary; `source_recorded_at` and `source_expires_at` paired. The table holds no source body, label, filename, transcript or session id, model prompt or output, raw payload, or prior value. |

1a1 provided memory_embedding's table, composite primary key and memory foreign key only. 1a2
owns dimension/provider/index strategy, dense fill/rebuild and retrieval behavior, and its
migration `0003_dense_retrieval` adds the width and the HNSW index. The width lives in the
column type and in the module's `EMBEDDING_DIMENSIONS` constant; no index definition and no
table acts as a persisted model-dimension registry.

Those two are the retrieval index criterion 29 queries: `search_tsv` carries its GIN index and
`memory_embedding` its HNSW index. A memory holds no secret and no contact point value; a
party's contact points live in the relationships module and are `restricted` in the redaction
tiers.

## Audience and purposes (FR 27, criteria 27 and 28)

Every memory carries an explicit audience and an explicit purpose set from the day it is
written. There is no implicit "everyone".

**Audience** is a two-level lattice in release one: `workspace` (every member of the workspace)
above `member:<account>` (that account only). `recallatron.memory.remember(kind, title, body,
audience, purposes, refs)` takes `audience` as a selection, `workspace` or `member`, default
`workspace`. `member` resolves to the account behind the caller's
[`WorkspaceContext.audience`](overview.md#the-workspace-context): a `session` audience is the
session's account, a `token` audience is the token's account, and a `job` audience is the
originating operation's audience carried onto the job. A context with no account behind it (a
`system` actor on a schedule, a `connection` actor) may write `workspace` memories only, and
`audience = member` is refused `audience_unavailable`. So a memory recorded with `audience =
member` is always the recording account's own; nobody can record a memory private to someone
else, and nothing a job or a model run writes is ever wider than the person or token that
started it. Phase six's channel audiences are a later value of the same column, not a new
mechanism.

**Purposes** are the closed vocabulary shared with contact permission (`respond`, `follow_up`,
`share_with_referral`, `internal_analysis`), stored in `memory_purpose`. A `remember` call names
them; the default is `recallatron.default_purposes` (package default `respond, follow_up,
internal_analysis`, workspace scope, floor `subset`). A run with purpose `p` can be given a
memory only when `p` is in its purposes ([context builder](runtime-and-mcp.md#the-context-builder)).

**Derivation intersects, never unions.** `recallatron.memory.derive(sources, kind, title, body)`
writes a memory whose `audience` is the meet of its sources' audiences (`workspace` and
`member:A` give `member:A`; `member:A` and `member:B` give nothing) and whose purpose set is the
intersection of its sources' purposes. An empty audience refuses with `audience_empty`; an empty
purpose set refuses with `purposes_empty`; a memory nobody may read for no purpose is not
written. A record source (an opportunity, an observation, a party) contributes `workspace` and the
full vocabulary, because records carry neither column in release one; the person behind a record
constrains use through contact permission at retrieval instead (below). So a memory derived from a
broadly readable memory and a member-private one is member-private (criterion 28), and a memory
stored under one member's audience is invisible to another member's query (criterion 27).

Purpose comes from the authenticated binding when present: omission uses it, disagreement refuses,
and writes cannot store a wider purpose set. Unbound authenticated people and services have no
purpose gate. Remember refuses memory refs; derive performs the source intersection. Correct and
supersede require expected_revision and return record_stale for an authorized stale revision,
including a concurrent supersede loser. Explicit include_invalidated reads return retained
non-erasure history under all actual source/audience/purpose/retention restrictions. Generic
resolution remains current-only.

A well-formed purpose that is not the bound one refuses `purpose_mismatch`, distinct from
`input_invalid` for a value that is not a purpose at all. An audience wider than the caller's own
ceiling refuses `audience_unavailable` rather than being quietly narrowed; a derive whose sources
meet nowhere refuses `audience_empty` and one whose purposes intersect nowhere refuses
`purposes_empty`, both before any row or entity is written. A `remember` handed a memory reference
refuses `use_derive`. A write carries at most 64 refs and 64 mentions, a derive between 1 and 64
sources, a title of 1 to 200 trimmed characters, and a body of at most 65536 UTF-8 bytes; an
entity name is 1 to 200 characters and a mention role at most 100. Every one of those bounds is
a named constant in the module's own `configuration` module, not a literal at a call site.

## Provenance and links

`memory_link` rows are written by the operation that creates the memory and are never edited:

- `derived_from`: each source of a `derive`, each record a `remember` cites, and the predecessor
  record of a migrated memory.
- `about`: each record the memory concerns, plus every `relationships.party` a source involves at
  the time of writing (an opportunity's linked parties through `leads.opportunity.get`; an
  observation's `party_ref`), plus the `ref` of every entity the memory mentions that has one,
  expanded once at write time so retrieval has a bounded set to check. The mention rule is what
  keeps a memory tied to a person only through `memory_mention` inside the reach of that
  person's withdrawal: the contact-permission filter reads `about` links and nothing else, so a
  mention that produced no link would be a memory the filter could not see.

A link is a reference string, never a foreign key into another schema (FR 11). A memory may link
to Leads records only when Leads is enabled in the workspace; the memory module declares Leads
and relationships as optional dependencies and reaches them through registered operations by
name, which is what lets criterion 66's no-memory run and a memory-without-Leads workspace both
exist.

Automatic memories retain origin=derived and the existing four memory kinds. A private trusted
source-unit contract supplies immutable producer_kind, authority_id, original
principal/audience/bound-purpose and source-time bounds; ordinary derive still requires authorized
canonical refs and cannot select trusted keys. The content-free receipt identifies an evidence
unit, not a session/conversation record. One unit creates at most one representation; model
changes or re-chunking cannot invent new identities for consumed evidence.
Denied/noop/erased/expired outcomes suppress replay without source content; restore preserves
receipts but restores no live enrollment or worker authority. Acceptance workers may suppress a
verified unit with no live representation; erasing a live memory uses the existing approved
deletion coordinator or its exact verified retention exception. 1a1 implements no transcript
reader, extractor or hook.

Every non-replayable receipt state answers one word, `source_unavailable`: a changed digest, a
noncurrent or missing representation, and an erased, expired, noop, denied or orphaned receipt
are deliberately indistinguishable, because telling them apart would report that a source was
once accepted and then erased. A unit whose authority cannot be verified refuses
`authority_unverified` and writes no receipt at all, so an unauthorized caller cannot terminalize
an identity it merely claimed.

## Retrieval (FR 27, FR 30, criteria 27 and 31)

`recallatron.memory.recall(query, k, include_invalidated, purpose)`, read class, roles `owner`,
`member`, `service`. Two stages: the workspace's configured `RetrievalStrategy.search` ranks,
and the operation's own handler then decides what may be returned. Which strategy ranks is the
workspace's `recallatron.retrieval.strategy` row, one of `lexical`, `dense` or `hybrid` (package
default `hybrid`, written at enable), read on the caller's own transaction. A row that will not
parse, or names a value outside those three, resolves to `lexical`, the one strategy that cannot
answer from an index the workspace never filled. Criterion 31 runs the module's behavioural
suite under `lexical` and under `dense`. The two stages, in order:

1. **Candidate set in SQL, before ranking.** Live memories only: `audience_kind = workspace`, or
   `audience_kind = member` with `audience_id` equal to the caller's account (an `account` actor's
   id; a `token` actor resolves to its account; a `system` or `connection` actor has no account
   and sees `workspace` memories only), a `memory_purpose` row for `purpose` exists,
   `invalidated_at is null`, `superseded_by_id is null`, the row is inside the workspace's age
   horizon when one applies at all ([retention](#retention-fr-29-criterion-30)), plus the
   caller's kind and time filters. Nothing outside this set is scored.
2. **Ranking, then the per-link permission check.** The strategy hands back one ranked list of
   at most 500 positions ([storage](storage-and-workspaces.md#retrieval-adapter-d9-fr-30)), and
   the call walks that list in order until it has `k` eligible hits or reaches its end, which is
   the scan bound of 500. This permission walk has no over-fetch multiplier: no setting widens
   or narrows how far it looks. The one over-fetch setting,
   `recallatron.retrieval.overfetch_multiplier` (default 3, bounds 1 to 10), acts at an earlier
   stage and on the dense arm alone: inside `search`, before fusion, the `hybrid` dense arm
   fetches `min(k × multiplier, 500)` rows. The lexical arm fetches 500 under `lexical` and
   `hybrid` alike, and `dense`'s single arm fetches 500 too. Fusion needs more than `k` rows
   from the dense arm: cut at `k`, a memory the dense arm ranks just below that gets nothing
   from it, however high the lexical arm ranks it. On the lexical arm a multiplier could only
   shorten the list the walk gets. For each hit, every `memory_link.ref` is resolved under
   `ctx`: a reference that is unreadable or `deleted` drops the hit. When Leads is enabled,
   every `about` link to a party runs `leads.contact_permission.check(party_ref, purpose)`; a
   result of `withdrawn` or `suppressed` drops the hit (criterion 61); `absent` does not,
   because no permission record is the normal state of every party (criterion 62) and a memory
   is the workspace's own note about its work, while a withdrawal or suppression is a person's
   expressed refusal and must bite. The first `k` survivors are returned; if the scan bound is
   reached first, the call returns what it has.

Each item carries `ref`, `kind`, `title`, `body`, `score`, `strategy`, `occurred_at`, and the
resolved heads of its links. The response carries `provenance`: `strategy`, the strategy that
answered, which every item's `strategy` equals; `arms.lexical` and `arms.dense`, the length of
each arm's ranked list as it entered fusion, after the arm's own limit and before the walk,
zero for an arm that did not run; and `dense_available`, whether a dense arm could contribute
to this answer at all. `score` is on the answering strategy's own scale and is comparable only
among the items of one response: `ts_rank_cd` under `lexical`, cosine similarity under `dense`,
and the fused sum `Σ 1 / (60 + rank)` under `hybrid`, at most `2/61`. A score compared across
strategies, or stored and compared later, compares nothing. The candidate SQL and the link
check are strategy-independent; 1a1 owns the records, the eligibility rules both stages apply,
and the bounds below.

**How each strategy ranks.** The lexical arm matches the query against `search_tsv` with an
**OR** join, not an AND: a memory needs one of the query's surviving terms, not all of them.
The query is reduced to its content lexemes by the `english` configuration that built the
column, so stopwords go and words are stemmed. From `LEXICAL_MIN_TERMS_FOR_DF` (three) lexemes
up, a lexeme at least 15% of the workspace's memories contain (`LEXICAL_DF_THRESHOLD`) is
dropped, read from the column's `pg_stats` sample; a lexeme the statistics do not know counts
as rare and is kept, and if every lexeme would be dropped the five rarest are kept instead. A
double-quoted phrase in the query is matched as a phrase, OR-ed in, and never filtered.
Ranking is `ts_rank_cd` cover density, which carries no inverse-document-frequency term, so a
match on a rare word and a match on a common one can score alike; IDF-aware lexical ranking is
run 1b's. The `pg_trgm`
title index exists, and no recall statement reads it. The dense arm embeds the bare query with
the configured provider and ranks by cosine similarity, keeping only rows at or above
`recallatron.retrieval.dense_floor_percent` (an integer percent, default 30, so cosine 0.30).
That default was measured on the shipped provider. The predecessor's floor, M.O.T.'s L2 bound
of 0.76 (cosine 0.71), came from a different embedding space and does not transfer; run 1b
confirms the number on a migrated corpus. `hybrid` runs both arms, fuses them by reciprocal
rank (constant 60), and within an equal fused score puts the newer memory first.

**When the dense arm cannot contribute** (no provider resolves, the provider raises, the dense
statement fails, or the workspace holds no vector for the provider's model), it answers no rows
and `dense_available` is false. It never refuses and never hands back lexical rows under its
own name, so `dense` then answers no items, and `hybrid` answers the lexical arm's rows in the
lexical order, labelled `hybrid`. The dense statement runs inside a savepoint, so a failure
there leaves the recall's transaction usable. Each cause logs one line naming the provider and
the failure class, never memory text or the query: no provider logs at DEBUG, because it is the
shipped default and would otherwise log on every `hybrid` recall, and the other three log at
WARNING.

`recallatron.memory.read(container_ref, target_ref, context, include_invalidated, purpose)`,
read class, roles `owner`, `member`, `service`, is the second read: one authorized target and a
bounded window of its neighbours in a named container. Its precedence is fixed. Target eligibility is decided first, then the
container's own eligibility, then membership: an authorized target not linked to the named
container refuses `container_membership_required` before a single neighbour is selected, so a
caller cannot learn a container's population through a target that does not belong to it. At most
500 row-locally eligible candidates are evaluated and the 501st identifier is an existence test
only; past it the call refuses `window_scan_limit`, a fixed, content-free refusal carrying no
target content, candidate count, eligible or hidden count, identity, position, total, bounds,
`has_more` flag or partial item. One request-wide budget of 4096 distinct references and depth 64
is shared by the target, every candidate and every link either reaches; exhausting it refuses
`reference_scan_limit` with no partial content and no window metadata. `context` is 0 to 10
neighbours either side (default 2); `recall` takes a query of 1 to 1000 characters and a `k` of
1 to 50 (default 10), and entity listing a limit of 1 to 50 (default 10). Every read path refuses
`retention_unavailable` before any container scan or content resolution when the workspace states
a retention window it cannot read.

### Near-duplicate candidates

`recallatron.memory.dedup_candidates(limit, purpose)`, read class, roles `owner`, `member`,
`service`, and no MCP tool: like the two entity reads it is service-only in release one. It
returns at most `limit` pairs (1 to 50, default 10), each `{ref_a, ref_b, score}`, where
`score` is the cosine similarity of the two stored vectors and is at least 0.80
(`DEDUP_PAIR_FLOOR`). It only proposes. Nothing merges, confirms or supersedes on a pair's
strength, and the output carries no field a caller could act on.

Eligibility runs after the bound and before any distance is computed. The call takes at most
500 of the newest memories (`recorded_at` descending) that pass the candidate SQL above and hold
a vector for the provider's model, runs the full eligibility check, links included, on each of
those, and pairs only the ones the caller may read. So a memory the caller cannot read never
takes a pair slot: it cannot become a readable memory's nearest neighbour, which would hide the
real pair and hint at the hidden one. It can still take one of the 500 places. A memory the
candidate SQL admits and eligibility then denies, such as one with a link the caller cannot
resolve, counts toward the bound, and each such memory can push one readable memory past the
500th place, where it is not compared. That residual is accepted; recall's scan bound behaves
the same way. Both sides of every pair are checked again before it is returned, and a denial
on either side drops the whole pair. The call reads no retrieval-strategy setting: with a
provider and stored vectors it answers under `lexical` too, and with no provider or no vectors
for the model it answers no pairs rather than refusing. A call at the full bound measured about
1.1 to 1.25 seconds on the test cluster, nearly all of it the 500 eligibility checks, and it
grows with the links each candidate carries. The 501st-newest memory that qualifies for the
bound is never compared, so a duplicate pair straddling that position is not proposed.

## Correction and supersession (FR 28, criterion 29)

Two operations, one rule underneath.

- `recallatron.memory.correct(ref, expected_revision, title, body, confidence)`, mutate, roles
  `owner`, `member`: the memory was wrong and is fixed in place. `revision` increments, `corrected_at` is set, the
  memory is re-indexed (`search_tsv` regenerates in the transaction; its embedding rows are
  deleted and the [embedding job](#the-embedding-job) re-creates them after commit), and
  the rule below runs for `recallatron.memory:<id>` with reason `source_corrected`.
- `recallatron.memory.supersede(ref, expected_revision, kind, title, body, confidence,
  occurred_at)`, mutate, roles `owner`, `member`: a new memory replaces the old one, which stays
  readable as history. The replacement's content is the caller's and its audience, purposes and
  restrictions are not: it takes the source meet and the predecessor's copied restrictions, so
  the input carries no audience, purposes or reference fields, and no field for the ancestry
  marker — the server writes the `derived_from` link to the old memory and marks
  `supersession_lineage` itself. The old memory's `superseded_by_id` is set, its embedding rows
  are deleted, and the rule runs for it with `source_superseded`.

Both take `expected_revision` and are a compare-and-set judged under the workspace lifecycle
lock. An authorized caller whose revision is not the one the row holds is told `record_stale`
rather than `not_found`, which is also what the loser of two concurrent supersessions receives:
the winner has already incremented the predecessor's revision, so the loser learns its copy has
moved rather than that the record is gone.

A replacement stores its own retention clock, inherited audience/purposes, and independent copies
of every actual source restriction. Its immutable marked ancestry is content-free and need not
resolve an expired predecessor. User erasure traverses ordinary dependencies and ancestry;
scheduled predecessor expiry traverses ordinary dependencies and preserves a replacement reached
only by ancestry. Copied ancestry preserves erasure reachability across expired intermediate
rows. No removal revives a superseded row. Expiry evidence and those restrictions travel through
validated export/restore.

**The one invalidation rule.** One traversal, `dependent_closure(ref)`, with two dispositions
over what it finds: every memory with a `memory_link` to `ref` in either relation, and,
transitively, every memory with a `derived_from` link to a memory so found. For each:

- delete its `memory_embedding` rows, so the dense index no longer holds it;
- erasing: delete the memory row with its links, mentions and embeddings, so neither index holds
  it; a derivative of erased data has no reason to exist, and R5 wants derivatives gone, not
  marked. Because the row goes rather than being marked, no live row ever carries
  `invalidation_reason = source_deleted`; the value stays in the column's vocabulary so a
  restored artifact that claims it can be refused rather than silently accepted;
- marking, for `source_corrected` and `source_superseded`: delete the `memory_embedding` rows so
  the dense index no longer holds it, then set `invalidated_at` and `invalidation_reason` and
  keep the row. The candidate SQL excludes it, `recall(include_invalidated = true)` shows it to a
  person deciding whether to re-derive, and nothing re-derives on its own.

The same traversal runs from `correct`, from `supersede`, and from the deletion participant
below. It is one traversal so that criterion 29's "in the same operation" is a property of the
transaction the caller already holds, not of three implementations agreeing.

## Deletion (FR 28, R5, criterion 65)

`recallatron.memory` is a deletable record type. `recallatron_forget` is `core.record.delete` for
it: the owning delete removes the memory row, its embedding rows, links, and mentions; then the
participant `recallatron.on_record_deleted` runs the closure below for memories derived from it.
**In release one that participant is registered for `recallatron.memory` and nothing else**,
because no other deletable record type exists yet: `leads.observation`, `leads.opportunity` and
`relationships.party` arrive in phase three, and registering the participant for them is that
phase's, inside the coordinator's transaction
([deletion](deletion-export-migration.md#the-cascade)). The deletion record's
`invalidated_memory_count` is the number of memory rows removed.

The module reaches the coordinator through the typed owned-delete pair its record type declares
([module contract](module-contract.md#owned-record-types)). The authorizer is content-free by
shape: it answers a reference and a revision, or a refusal, and never the record, so nothing
about a memory the caller may not read travels back through an authorization. It applies the
module's own rules — the module enabled here, one of the lifecycle roles, the row's audience, and
a bound context inside the row's purpose set — and deliberately applies **no** retention horizon,
no lifecycle mode and no link resolution, because an expired row is exactly what expiry exists to
remove, a retained corrected or superseded row is still somebody's to forget, and a memory whose
linked record was deleted must not become unerasable because of it.

Both the authorizer and each participant handler are told which disposition is running — user
erasure or scheduled expiry — because the two follow different edges and carry different
authority, and neither callable can read the sealed capability the core verified.

**A member-audience memory's only removal path in release one is the member's own erasure.**
The authorizer admits a workspace owner for one, on the reasoning that erasure authority and read
authority are distinct and a departed member's memories must not be unremovable by anyone. That
route is **not reachable end to end in this release**: the destructive class's subject recheck
resolves the subject under the reconstructed original caller, and the resolver answers the same
`not_found` for "you may not read this" as for "it is gone", so an owner's approval for a memory
they cannot read is invalidated before the coordinator runs. The gap is named here and owned by
no run; nothing in release one should be read as claiming the owner route works.

## Retention (FR 29, criterion 30)

**Recorded change of direction (the maintainer, 2026-09-21, run 1a1).** This section used to
describe one setting, `recallatron.retention.days`, written at enable with a default of 365 and
the explicit statement that no workspace inherits indefinite retention — which made age-based
expiry mandatory everywhere, with no opt-out. That is superseded. The authority being restored is
FR 29's own text, "Memory retention is explicit per workspace. Nothing is retained permanently
**by default**", which forbids accidental indefinite hoarding and does not forbid a workspace
choosing to keep its memories. The mandatory reading was an architecture-stage over-tightening
ratified with the A1–A17 bundle in PR #7 (2026-09-09); this run supersedes that ratification for
retention alone. A memory is corrected or superseded, not expired for having got old, and
permanent memory is the product's point. Category-scoped retention — expiring one class of
subject matter and not another — is a separate feature with no schema to hang it on yet, and is
explicitly deferred.

**Two settings, and the first is a gate.** `recallatron.retention.expire_by_age`
(`ValueType.BOOL`, `explicit_per_workspace`, package default **false**) says whether this
workspace deletes memories by age at all. Off means there is no horizon: no read filters by age,
no write or acceptance rechecks a source clock, no export counts unswept content, and the sweep
does nothing. `recallatron.retention.days` (`ValueType.INT`, `explicit_per_workspace`, package
default 365, hard bounds **minimum 1, maximum 3650**) is the window, and it is read **only while
the gate is on**. Bounds rather than a floor, because a workspace may legitimately choose a
longer window than its neighbour; what must not be settable at any layer is zero days or a
millennium. There is no indefinite sentinel on this key: "no bound at all" is the gate's answer
and never a magic number on the window.

Both rows are written at enable
([module contract](module-contract.md#install-and-enable-release-one-in-code)), so every
workspace's retention posture is a stored, readable value rather than an inference from an absent
row, and turning expiry on later is one settings write against a schedule that already exists.
The asymmetry between the two keys is deliberate: an absent or unparseable gate row resolves
`false`, because falling back to the retaining default is the safe direction, while an absent,
unparseable or out-of-range window row refuses `retention_unavailable` on every read, write and
sweep, because substituting a default there would silently widen a policy at the moment it went
missing. Retention is still explicit per workspace and still has no per-kind policy, no stored
expiry column and no recomputation when a setting changes: the candidate SQL and the sweep both
read the live values, so an owner tightening the window through `core.settings.set` takes effect
on the next query and the next sweep.

**`member`-audience memories sit outside the mechanism entirely, in either gate state.** They are
never selected by the sweep, never age-filtered on read, and never counted by export's
unswept-content refusal. The sweep runs under an accountless `system` context that could not
authorize removing member-private content, and a workspace operator's data-minimization setting
is not
authority over one member's own record; given the sweep will never remove such a row,
age-filtering it on read would strand it permanently unreadable with no deletion record and no
way back except turning the gate off.

The scheduled job `recallatron.retention_sweep` (daily, `system` actor) runs in both states and
reads the gate first, before a root is selected, a window resolved or the lifecycle lock taken; a
gated-off invocation is an ordinary success with no work in it, not a failure, so a workspace
that wants no expiry costs no retry budget and no failure entry. With the gate on it takes at
most a hundred expired roots per leased batch, each through the core's verified scheduled-expiry
entry, writes a `core.deletion_record` with `actor_kind = system` and no approval — R5's one
permitted scheduled expiry — and asks for another batch only when a remainder survives a
re-read at batch end. A batch that found a remainder and removed nothing fails the attempt rather
than rearming, so a stuck workspace reaches the ordinary failure list instead of enqueueing a job
a second for ever. Scheduled expiry follows ordinary dependencies and **not** marked ancestry, so
a fresh replacement survives its expired predecessor's sweep on its own clock.

## The embedding job

Embedding is a provider call and never runs inside a writing transaction. A write enqueues a
`recallatron.embed` job only when the workspace's strategy is `dense` or `hybrid` **and** an
embedding provider resolves for the deployment; the strategy alone never enqueues. No provider
configured means no job is enqueued: under the shipped defaults, `hybrid` with provider `none`,
a write leaves no job row, no worker wake and no failure entry. The job row is written in the
writer's own transaction, so it commits with the write, and the provider call runs after that
commit, in the worker. Two writers call the one enqueue helper: the insert that `remember`,
`derive`, `supersede` and trusted acceptance share, and `correct`, which rewrites its row in
place, never reaches that insert, and enqueues through its own call.

The job reads the live memory row `FOR SHARE`, embeds `embed_input(title, body)` (the stored
title, one newline, the stored body) through the
[embedding seam](runtime-and-mcp.md#the-embedding-provider), and writes the `memory_embedding`
row for the provider's `model_id`. A correction or invalidation takes the row's lock before it
deletes the row's vectors, so the two cannot interleave and the vector that commits was made
from the text the row then holds. A memory invalidated, superseded or deleted before its job
runs is skipped, as is one that already has a row for the model. A provider gone by the time
the job runs is a success that writes nothing; a provider that raises is a real failure,
retried under the core's backoff, and after five attempts the job lands in the core failure
list. Until the job has run, a just-written memory has no vector, and the module accepts that
rather than blocking the write on the provider. Under `hybrid` the lexical arm can still find it;
under `dense` recall does not return it at all. That gap can last up to
`work.due_reconcile_seconds` (900 s by default). An ordinary write marks no workspace due; only
a long-running dispatch does. So the job waits until the worker next visits the workspace, and
the reconcile floor bounds that wait at the interval.

`recallatron.embedding.rebuild`, mutate class, roles `owner`, long-running, is the whole-index
form and the only backfill: turning a provider on embeds nothing that already exists. It refuses
`embedding_provider_unavailable` when no provider resolves. Otherwise it enqueues one job, and
that job does the whole walk in one transaction: it deletes embedding rows whose `model_id` is
not the provider's, deletes every row when the workspace's stamped embed-input version
(`embedding_state`, [entities](#entities)) is missing or not the current one and stamps the
current one, then embeds every live memory without a row for the model in batches of
`recallatron.embedding.batch_size` (default 32), reporting progress on its operation record.
One transaction means the progress becomes visible at commit rather than batch by batch, a
crash rolls the whole walk back, and a rerun is safe because the walk selects only memories
lacking a row. It also means a correction, invalidation or deletion of a memory the walk has
already embedded waits for the rebuild to commit, holding the workspace lifecycle lock while it
waits. The walk skips a row another writer holds rather than waiting on it, and before it
commits it queues an embed job for every live memory still without a vector. Nothing schedules
it: an owner must run it after a restore (embeddings are not exported, so the rebuild recreates
them), after the provider or its model changes, and whenever else it is needed. The lexical
index needs no rebuild; it is a generated column.

## Migration from the predecessor (FR 53, criterion 32)

`recallatron.migration.import(source_label, extract_file_ref)`, mutate, roles `owner`,
long-running: creates a `recallatron.migration_batch(id, source_label text, state text,
verification_id uuid null, created_at)` row (`state` in `importing`, `verified`, `live`,
`failed`; a declared record type whose resolver returns a label and nothing else), writes each
predecessor record as a memory with `origin = migrated`,
`audience = workspace`, the default purposes, and a `derived_from` link to the batch, then
writes the `core.migration_verification` row
([migration verification](deletion-export-migration.md#migration-verification-fr-53)).
Migrated memories get their vectors from the [embedding rebuild](#the-embedding-job) as it
ships: it covers the whole workspace rather than one batch, runs only when a provider resolves,
and refuses without one. Whether and when the import runs it, like the rest of the import's
shape, is run 1b's to decide.
Memories of a batch in state `verified` are excluded from the candidate SQL until
`recallatron.migration.switch_over(batch_ref)` (mutate, roles `owner`) sets the batch `live`.
The extract file and the report are workspace files under the data root, never in the
repository.

## Web contribution

Memory browse, search, and entity screens (the phase-two port) plus the unified navigation and
theme. The screens call `recall`, `read`, `remember`, `derive`, `correct`, `supersede`, and the
entity reads through the internal API like every other screen; both retention settings are read
and set through the core's settings operations, not a module operation.

## Export

`memory`, `memory_purpose`, `memory_entity`, `memory_mention`, `memory_link`, `source_receipt`,
source keys, and marked-ancestry (`supersession_lineage`) rows are `exportable`;
`memory_embedding` and `embedding_state` are not (the [embedding rebuild](#the-embedding-job)
recreates both after a restore, embedding each memory's title and body again, since the
embedding model may differ). The retention setting travels with the workspace settings.
Content-free deletion evidence (`cause`, `retained_successor_ref`) travels
with the core deletion category. Criterion 36's comparison is over this exportable set.

An export refuses `export_requires_retention_sweep` when the snapshot still holds content the
sweep should already have removed: the gate and the window are read through the snapshot's own
settings source and the horizon is computed from the snapshot instant rather than a clock
sampled at the check, and the refusal carries a bounded count of expired roots and the sweep
schedule's own state, so an operator can tell "the sweep has not run yet" from "the sweep is
stuck". With the gate off there is no horizon and the refusal is unreachable, which is the
ordinary case; `member`-audience rows are never counted at any age, since the sweep may never
remove one and counting it would make export permanently impossible for a workspace holding a
single old private memory. `core.workspace.digest` reads the module's exporter through the same
sealed snapshot, so the same refusal reaches a digest of a workspace with unswept content, and
the operator remedy for both is to let the sweep run.

Restore is two passes with the core's own deletion-evidence import between them: the module's
importer inserts the parents with their self-references null and hands back a continuation the
core calls once the ledger has landed, which is when the self-references resolve and the marked
ancestry is validated against that evidence. The module's exporter reads through one sealed
snapshot of the source transaction — a fixed instant, the transaction-bound settings source and
that connection alone — so it cannot put a second, later view into the same artifact, and its
importer never commits, because the whole restore is one transaction.

## Operations and roles

| Operation | Class | Roles | Tool |
| --- | --- | --- | --- |
| `recallatron.memory.recall` | read | owner, member, service | `recallatron_recall` |
| `recallatron.memory.read` | read | owner, member, service | `recallatron_read` |
| `recallatron.memory.remember` | mutate | owner, member, service | `recallatron_remember` |
| `recallatron.memory.derive` | mutate | owner, member, service | `recallatron_derive` |
| `recallatron.memory.correct` | mutate | owner, member | `recallatron_correct` |
| `recallatron.memory.supersede` | mutate | owner, member | `recallatron_supersede` |
| `core.record.delete` for `recallatron.memory` | destructive | owner, member (a member only for a memory its audience lets it read; the owner's route to a `member`-audience memory is declared and not reachable end to end, [deletion](#deletion-fr-28-r5-criterion-65)) | `recallatron_forget` |
| `recallatron.entity.list`, `.get` | read | owner, member, service | none in release one (service-only: no MCP tool is registered for either, so no model reaches them) |
| `recallatron.memory.dedup_candidates` | read | owner, member, service | none in release one (service-only, like the entity reads; [near-duplicate candidates](#near-duplicate-candidates)) |
| `recallatron.embedding.rebuild` | mutate, long-running | owner | none |
| `recallatron.migration.import`, `.switch_over` | mutate | owner | none (operator and web only) |

`service` on `remember` and `derive` is what lets a job or a model run write a memory on the
actor's behalf; the audience of anything a `service` actor writes is the audience carried onto
the job from the operation that started it, never wider, and a `service` actor with no account
behind it can write `workspace` memories only ([audience](#audience-and-purposes-fr-27-criteria-27-and-28)).

## A17. Explicit audience and purposes, intersection, one invalidation rule

**Decision.** Audience is a column on every memory and purposes are its child rows, both
required at write; derivation takes the meet and the intersection and refuses when either is
empty; one function invalidates derivatives for deletion, correction, and supersession, with
deletion removing rows and the other two marking them.

**Why stored and not a policy lookup.** FR 27 and criterion 28 ask what a memory *carries*,
because a derived memory outlives the conditions under which its sources were readable. A lookup
at read time against the sources would widen the moment a source was loosened; a stored
intersection cannot.

**Why one rule with two outcomes.** Criterion 29 and R5 ask for the same traversal from
three entry points; three implementations would agree until one was changed. The outcome
differs because the reasons differ: erased personal data must not survive in a derivative, while
a corrected fact leaves a derivative wrong but not forbidden. (This is a different axis from the
`Disposition` the deletion coordinator passes an owner, which distinguishes user erasure from
scheduled expiry, not removal from marking; the two words met in one document and this one gave
way.)
