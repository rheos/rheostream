# Recallatron

## What it is

The memory of rheoStream. A memory is an explicitly stated note, fact, decision or
summary, with its provenance, who may read it, and the purposes it may be used for. It is
recorded by hand, derived from records you may read, or created conservatively from what
you said in a session.

A memory is never the authoritative record. A remembered summary of a lead is not the
lead, and every link back to a record is resolved under the caller's permission at the
moment of reading. Nor is Recallatron a transcript archive: it keeps what was said
explicitly and worth keeping, not every turn.

## What you can do with it

- **Remember** a note, fact, decision or summary, with an audience, purposes, a
  confidence, when it happened, and the records it is about.
- **Derive** a memory from other memories or records. It inherits the narrower of their
  permissions automatically.
- **Recall** the memories relevant to a question, ranked by meaning and by words.
- **Read** a window of memories around one record or one named entity.
- **Correct** a memory that was wrong, or **supersede** one that is out of date. The old
  one stays readable as history.
- **Forget** a memory, with a per-action approval. What was derived from it goes too.
- Browse, search and open memories in the web shell, and see pairs that may be
  duplicates.
- Let it save automatically from your own Claude Code sessions and rheo's runs, and give
  a new Claude Code session what it should know on its first prompt
  ([the bridge](claude-code-bridge.md)).
- Keep memories forever, which is the default, or turn on expiry by age per workspace.

## How it works

**Every memory carries its audience and purposes.** The audience is either the whole
workspace or one member (`member:<account>`). You can only record a member-private memory
for yourself, and nothing a job or a model run writes is ever wider than the person who
started it. The purposes come from a closed list shared with contact permission:
`respond`, `follow_up`, `share_with_referral`, `internal_analysis`. There is no implicit
"everyone" and no default purpose.

**Derivation intersects, never unions.** A derived memory's audience is the meet of its
sources' audiences, and its purposes are the intersection of theirs. A memory made from
a workspace-wide note and a member-private one is member-private. If the sources share no
audience or no purpose, the write is refused: a memory nobody may read for no purpose is
never written.

**Links make provenance and permission one thing.** Each memory links to what it was
`derived_from` and what it is `about`. When a memory mentions a person or organization
that has a record, an `about` link to that record is written in the same transaction.
These links are reference strings, never foreign keys into another module.

**Recall is two stages, and permission comes first.** Stage one is SQL: only live
memories the caller's audience covers, carrying the requested purpose, inside the
workspace's age horizon if it has one. Nothing outside that set is ever scored. Stage
two ranks the survivors and walks the ranked list in order, resolving every link under
the caller's context. A link the caller cannot read, or one to a deleted record, drops
the hit. When Leads is installed, an `about` link to a person whose contact permission
for this purpose is withdrawn or suppressed drops it too.

**Ranking is hybrid and runs on the box.** The lexical arm matches a generated `tsvector`
column, joining the query's words with OR; in a query of three or more terms, a word that
15% or more of the workspace's memories contain is dropped as noise. The dense arm embeds the query with a local model
(fastembed's build of all-MiniLM-L6-v2, 384 dimensions) and ranks by cosine similarity
through an HNSW index. Reciprocal rank fusion merges the two. Then a local cross-encoder
reranks the top window (three times the number asked for, at most 50) by how well each
memory answers the query. No credential, no API call: the only network access is the
first download of each model file. If the dense arm cannot contribute, a hybrid answer
comes back in lexical order and says dense was unavailable. If the reranker fails, the
first-stage order stands and the answer is marked degraded. Nothing pretends.

**Scores cannot reveal hidden memories.** Under hybrid recall, each memory's rank is
counted only among memories the caller may read, before the arms are fused. A score
computed over everything would give away how many hidden memories sit above it.

**One invalidation rule, three entry points.** Correcting, superseding and deleting a
memory all run the same traversal: every memory linked to it, and every memory derived
from those, transitively. Deletion removes them, with their embeddings and index
entries, because a derivative of erased data has no reason to exist. Correction and
supersession mark them invalidated and keep the rows, so a person deciding whether to
re-derive can still see them. Nothing re-derives on its own.

**Correct and supersede are compare-and-set.** Both take the revision you read. If the
memory moved since, you are told `record_stale`, not `not_found`, so the loser of two
concurrent supersessions learns its copy is out of date. A superseding memory's content
is yours; its audience, purposes and restrictions are copied from the predecessor, and
the server writes the lineage link itself.

**Erasing a source erases what came from it.** When a Leads observation or opportunity,
or a Relationships party, is deleted with approval, Recallatron removes every memory
linked to it and every memory derived from those, in the same transaction. Unrelated
memories survive.

**Retention is explicit and off by default.** Two settings are written for every
workspace when the module is enabled: whether to expire memories by age at all (default
no), and after how many days (default 365, between 1 and 3650). With expiry on, a daily
sweep removes workspace-wide memories past the window through the core's scheduled-expiry
path, writing a content-free deletion record. Member-private memories are never swept. A
memory is corrected or superseded, not expired for having got old.

**Embedding happens after the commit.** A write never waits on a model. It enqueues an
embedding job in the same transaction, and the worker embeds the stored title and body
afterwards. Until then the lexical arm can still find the memory. A rebuild re-embeds
the whole workspace in one transaction, after a restore or a model change.

**Automatic memory is conservative.** A person's own turns, from a rheo run or an
enrolled Claude Code session, are recorded as pending evidence and drained by a job. An
extraction step keeps only explicit statements. A turn with nothing explicit settles as
`noop` and creates nothing. Each piece of evidence creates at most one memory, keyed by a
content-free receipt, so a model change or a re-chunking cannot turn consumed evidence
into new memories. An automatic memory carries exactly its producer's verified purpose
and audience.

**Possible duplicates are only proposals.** The duplicate screen pairs memories whose
vectors have a cosine similarity of at least 0.80, among memories the caller may read. Nothing
merges, confirms or supersedes on a pair's strength.

**History stays separate from memory.** Material imported from a predecessor system is
kept as owner-only history records, searchable on their own screen and never offered to
an agent, so old conversation turns are not mistaken for confirmed facts.

## Why it's built this way

The problem with agent memory is that it leaks. A note written in a private context gets
recalled in a shared one; a summary outlives the record it summarized; a correction
leaves stale copies behind. Recallatron stores permissions on the memory itself, at
write time, as the intersection of its sources. A lookup at read time would widen the
moment a source was loosened. A stored intersection cannot.

The one invalidation rule exists for the same reason. Three implementations of "what
depends on this" would agree until someone changed one of them. One traversal, called
from three places, keeps erasure, correction and supersession honest by construction.

And it runs locally. Lexical search, embeddings and reranking all happen inside the
deployment, so a workspace's memory never needs to leave it to be useful.

## Where to look

- Specification: [memory](../architecture/memory.md),
  [relevance ranking](../architecture/recall-relevance-ranking.md),
  [retrieval adapter](../architecture/storage-and-workspaces.md#retrieval-adapter-d9-fr-30),
  [embedding provider](../architecture/runtime-and-mcp.md#the-embedding-provider),
  [search quality](../testing/recallatron-search-quality.md)
- Code: [module](../../modules/recallatron/src/rheo_recallatron),
  [writes](../../modules/recallatron/src/rheo_recallatron/writes.py),
  [eligibility](../../modules/recallatron/src/rheo_recallatron/eligibility.py),
  [retrieval](../../modules/recallatron/src/rheo_recallatron/retrieval),
  [lifecycle](../../modules/recallatron/src/rheo_recallatron/lifecycle.py),
  [automatic memory](../../modules/recallatron/src/rheo_recallatron/automatic.py),
  [retention](../../modules/recallatron/src/rheo_recallatron/retention.py),
  [screens](../../modules/recallatron/web)
- Tests: [memory records](../../tests/postgres/test_memory_records.py),
  [retrieval](../../tests/postgres/test_memory_retrieval.py),
  [lifecycle](../../tests/postgres/test_memory_lifecycle.py),
  [retention](../../tests/postgres/test_memory_retention.py),
  [embedding](../../tests/postgres/test_memory_embedding.py),
  [reranker](../../tests/test_relevance_reranker.py),
  [automatic memory drain](../../tests/postgres/test_automatic_memory_drain.py),
  [export and restore](../../tests/postgres/test_memory_export_restore.py)
