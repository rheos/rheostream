"""The module's identity, its settings keys, and Architecture § A13's fixed limits.

**A leaf module, and that is the whole reason it exists.** ``manifest.py`` declares the
operations, so it imports ``operations.py``, which imports ``eligibility.py`` — and
eligibility has to read the retention keys to answer anything at all. Leaving them on
the manifest would close that loop. Everything here imports nothing from this package,
so every other module in it can reach these values whichever one is imported first.

§ A13 asks for the module's fixed limits "centralized as named constants". This is that
one place: the read-side bounds, the traversal budget and — since the write half
exists — the write-side bounds on title, body, entity name, refs, mentions and the
trusted-ingest identity.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from rheo_core.settings import KeySpec, Scope, ValueType

MODULE_ID: Final = "recallatron"

MEMORY_RECORD_TYPE: Final = "memory"
"""The one record type this run's resolver answers for; a memory reference reads
``<module id>.<this>:<uuid>``."""

ENTITY_RECORD_TYPE: Final = "memory_entity"
"""The second addressable record type, and the one with **no** resolver.

An entity is reachable only through an eligible mention or an independently readable
backing ref, so generic resolution has nothing to answer for it: a resolver would be a
second route to an entity, reachable by anyone holding a well-formed id.
"""

RETENTION_EXPIRE_BY_AGE_KEY: Final = f"{MODULE_ID}.retention.expire_by_age"
"""Whether this workspace deletes memories by age **at all** (§ A9).

**Off by default, and off means there is no horizon.** Recallatron never auto-forgets a
memory by age: a memory is corrected or superseded, not expired for having got old. A
workspace that wants an age bound says so by setting this row to true, and until it
does no read, write, acceptance, export or sweep applies an age predicate of any kind.

``explicit_per_workspace``, like the window below, so every workspace's retention
posture is a stored readable value rather than an inference from an absent row: the
module-enable step writes **both** rows, and turning expiry on is then one settings
write against a schedule that already exists.

**Absent or unparseable means ``false``, and that asymmetry with the window is
deliberate.** For this key, falling back to the package default *is* the safe
direction, because the default retains; for the window, falling back to a default
would silently widen a policy at the moment it went missing. So a pre-correction
workspace holding no row at all resolves ``false`` with no backfill, while a missing
``days`` row still refuses — but only in the one state that reads it.
"""

RETENTION_EXPIRE_BY_AGE_DEFAULT: Final = False

RETENTION_EXPIRE_BY_AGE_SPEC: Final = KeySpec(
    key=RETENTION_EXPIRE_BY_AGE_KEY,
    type=ValueType.BOOL,
    scope=Scope.WORKSPACE,
    floor=None,
    explicit_per_workspace=True,
    default=RETENTION_EXPIRE_BY_AGE_DEFAULT,
)

RETENTION_DAYS_KEY: Final = f"{MODULE_ID}.retention.days"
"""How long a memory stays readable in a workspace, in days — **while the gate is on**.

Read only when :data:`RETENTION_EXPIRE_BY_AGE_KEY` resolves true. With the gate off
this key is not a mandatory setting: nothing reads it, so nothing can refuse for it.

``explicit_per_workspace`` because a workspace's retention is its own stated policy
from the moment the module is enabled, not an inherited deployment value it happens to
share: the module-enable step writes the row from the default below, and the core's own
settings-write operation moves it from there. (Named in prose rather than spelled out,
because the schema-ownership scan reads a ``<core-schema>.<name>`` literal in this tree
as a foreign table reference and is right to — this module must not name one, even in a
docstring.)

**Bounded rather than floored, and the two are different tools.** A floor combines a
workspace's override with the deployment's value under a comparator — which is right
for an approval window, where a workspace may only shorten its own. Retention is not
narrowable-only-downward: a workspace may legitimately choose a longer window than its
neighbour. What must not be settable at *any* layer is zero days (every read empty the
instant it commits) or a millennium, and that is what ``minimum``/``maximum`` express
and a floor cannot. "No bound at all" is the *gate's* answer and never a magic number
on this key.

**Missing or corrupt is not "use the default".** Every read, write and sweep refuses
``retention_unavailable`` when the stored row is absent, unparseable or outside the
range below (AC 8) — while the gate is on. The default is what *enable* writes; it is
never what a reader substitutes for a row somebody deleted, because substituting it
would silently widen a workspace's retention window at exactly the moment its policy
went missing.
"""

RETENTION_DAYS_DEFAULT: Final = 365
RETENTION_DAYS_MINIMUM: Final = 1
RETENTION_DAYS_MAXIMUM: Final = 3650

RETENTION_DAYS_SPEC: Final = KeySpec(
    key=RETENTION_DAYS_KEY,
    type=ValueType.INT,
    scope=Scope.WORKSPACE,
    floor=None,
    explicit_per_workspace=True,
    default=RETENTION_DAYS_DEFAULT,
    minimum=RETENTION_DAYS_MINIMUM,
    maximum=RETENTION_DAYS_MAXIMUM,
)
"""The declaration the manifest publishes and every reader validates a row against.

Readers validate against **this spec**, not against whatever the process-wide settings
registry happens to hold: the registry's contents depend on which modules a process
loaded, and a retention bound must not.
"""

# --- the retrieval strategy -----------------------------------------------------------

STRATEGY_LEXICAL: Final = "lexical"
STRATEGY_DENSE: Final = "dense"
STRATEGY_HYBRID: Final = "hybrid"
"""The three retrieval strategy names, and what every recall hit is marked with.

All three are offered by :data:`RETRIEVAL_STRATEGY_SPEC` and registered in the strategy
registry. The two move together: the registry refuses at import to start with a name
offered here that nothing serves.
"""

RETRIEVAL_STRATEGIES: Final = (STRATEGY_LEXICAL, STRATEGY_DENSE, STRATEGY_HYBRID)

RETRIEVAL_STRATEGY_KEY: Final = f"{MODULE_ID}.retrieval.strategy"
"""Which :class:`~rheo_recallatron.retrieval.protocol.RetrievalStrategy` ranks this
workspace's recall.

``explicit_per_workspace``, so enable writes the row and a workspace's retrieval
posture is a stored readable value. A value in ``choices`` is a value a workspace may
configure, so every one of the three has an implementation behind it.

**The default is ``hybrid``, and on a deployment with no embedding provider it reads
exactly like ``lexical``.** With nothing dense configured the dense arm contributes
nothing, and hybrid then answers the lexical arm's rows in the lexical arm's order,
labelled ``hybrid`` with ``dense_available=false``, never a refusal. What moves is
``score``, which is on the fused scale.

Read through the transaction-bound override source, like the retention keys. An
absent row resolves to this spec's default; a present row that will not parse
resolves to ``lexical`` whatever the default is, because ``lexical`` is the strategy
that cannot start answering from an index the workspace never filled.
"""

RETRIEVAL_STRATEGY_SPEC: Final = KeySpec(
    key=RETRIEVAL_STRATEGY_KEY,
    type=ValueType.STR,
    scope=Scope.WORKSPACE,
    floor=None,
    explicit_per_workspace=True,
    default=STRATEGY_HYBRID,
    choices=(STRATEGY_LEXICAL, STRATEGY_DENSE, STRATEGY_HYBRID),
)

DENSE_FLOOR_PERCENT_KEY: Final = f"{MODULE_ID}.retrieval.dense_floor_percent"
"""The dense relevance floor: the lowest cosine similarity a dense hit may have.

**A cosine similarity stored as an integer percent,** because the settings system has
no float type and only an ``INT`` key carries bounds (spec decision 2). ``30`` here is
cosine ``0.30``; the dense statement divides by 100 itself. A hit below the floor never
surfaces, and when every candidate sits below it the dense arm answers nothing rather
than its least-bad row.

**30 was measured on the shipped provider, not on a memory corpus.** It is requirement
9's stated rule (the lowest integer floor that blocks at least 95% of an off-topic query
population while keeping every measured memory-shaped relevant pair by at least 0.09)
applied to fastembed's mean-pooled MiniLM-L6-v2 in spec evidence E2. No memory corpus
exists here to measure it on; run 1b migrates one and confirms the number. A green
floor test proves the mechanism, never the value.

It has no relationship to :data:`LEXICAL_DF_THRESHOLD`, which is a document-frequency
proportion. Not ``explicit_per_workspace``: an absent row, one that will not decode and
one outside the declared range all read as the default, as the batch size does.
"""

DENSE_FLOOR_PERCENT_DEFAULT: Final = 30
DENSE_FLOOR_PERCENT_MINIMUM: Final = 0
DENSE_FLOOR_PERCENT_MAXIMUM: Final = 100

DENSE_FLOOR_PERCENT_SPEC: Final = KeySpec(
    key=DENSE_FLOOR_PERCENT_KEY,
    type=ValueType.INT,
    scope=Scope.WORKSPACE,
    floor=None,
    explicit_per_workspace=False,
    default=DENSE_FLOOR_PERCENT_DEFAULT,
    minimum=DENSE_FLOOR_PERCENT_MINIMUM,
    maximum=DENSE_FLOOR_PERCENT_MAXIMUM,
)

OVERFETCH_MULTIPLIER_KEY: Final = f"{MODULE_ID}.retrieval.overfetch_multiplier"
"""How much wider than ``k`` the **dense arm** of a ``hybrid`` recall is.

The dense arm asks for ``min(k * this, CANDIDATE_SCAN_LIMIT)`` rows. Fusion needs more
than ``k`` from an arm, or it loses exactly the row that ranks ``k + 1`` in one arm and
first in the other, which is the row hybrid exists to find.

**The dense arm only.** The lexical arm already fetches ``CANDIDATE_SCAN_LIMIT``, so a
multiplier there could only narrow it, and would cut how deep the permission walk can
look for ``k`` readable rows on the default path. ``dense``-only recall does not read
it either: its one arm fetches the scan bound.

The maximum is what keeps the ``min`` a belt rather than a live bound:
``RECALL_K_MAX * 10 = 50 * 10 = 500 = CANDIDATE_SCAN_LIMIT``, so no legal ``k`` and
multiplier ask for more than the scan bound. Not ``explicit_per_workspace``: an absent
row, one that will not decode and one outside the range all read as the default.
"""

OVERFETCH_MULTIPLIER_DEFAULT: Final = 3
OVERFETCH_MULTIPLIER_MINIMUM: Final = 1
OVERFETCH_MULTIPLIER_MAXIMUM: Final = 10

OVERFETCH_MULTIPLIER_SPEC: Final = KeySpec(
    key=OVERFETCH_MULTIPLIER_KEY,
    type=ValueType.INT,
    scope=Scope.WORKSPACE,
    floor=None,
    explicit_per_workspace=False,
    default=OVERFETCH_MULTIPLIER_DEFAULT,
    minimum=OVERFETCH_MULTIPLIER_MINIMUM,
    maximum=OVERFETCH_MULTIPLIER_MAXIMUM,
)


# --- the lexical query builder --------------------------------------------------------

LEXICAL_DF_THRESHOLD: Final = 0.15
"""A **document-frequency proportion**: the fraction of memories whose ``search_tsv``
contains a lexeme, at or above which a lexeme is dropped from a multi-lexeme query.

It has no relationship to any other threshold in this module — not to the dense
relevance floor, which is a cosine-similarity bound, and not to any score-gap ratio.
The ``LEXICAL_`` prefix is there so nobody reads it as either.
"""

LEXICAL_MIN_TERMS_FOR_DF: Final = 3
"""A query with fewer content lexemes than this is not frequency-filtered at all: it is
already specific. Counted after Postgres has stemmed and stopword-stripped it."""

LEXICAL_RAREST_KEPT: Final = 5
"""When every lexeme is at or above :data:`LEXICAL_DF_THRESHOLD`, keep this many of the
rarest rather than run an empty query."""


# --- the embedding pipeline -----------------------------------------------------------

EMBEDDING_PROVIDER_KEY: Final = f"{MODULE_ID}.embedding.provider"
"""Which registered :class:`~rheo_recallatron.embedding.protocol.EmbeddingProvider`
embeds this deployment's memories, by registry name.

**Deployment scope, and no ``choices``.** Which model runs on the box is the
deployment's decision, and the name is looked up in the module's provider registry
rather than validated against a list: an absent, unregistered or ``none`` name
resolves to no provider, which is the degraded path, never a refusal. The cost is that
a typo degrades silently; ``dense_available=false`` on every recall and a coverage
figure that stays at zero are the two things that report it.

There is deliberately no model key beside it. The model is the resolved provider's
property — its ``model_id`` is the one authority for which rows are current — and the
width is pinned into the column type, so a settable model would have exactly one
workable value. Nor is there a credential key: no shipped provider reads a secret.
"""

EMBEDDING_PROVIDER_NONE: Final = "none"

EMBEDDING_PROVIDER_SPEC: Final = KeySpec(
    key=EMBEDDING_PROVIDER_KEY,
    type=ValueType.STR,
    scope=Scope.DEPLOYMENT,
    floor=None,
    explicit_per_workspace=False,
    default=EMBEDDING_PROVIDER_NONE,
)

EMBEDDING_BATCH_SIZE_KEY: Final = f"{MODULE_ID}.embedding.batch_size"
"""How many memories ``recallatron.embedding.rebuild`` embeds per provider call.

Workspace scope, read by the rebuild's dispatch handler — which holds the context —
and carried to the job in its payload, because a worker job holds none. A stored
value that will not decode, or sits outside the declared range, reads as the default:
a read degrades, it never raises on a bad stored value.
"""

EMBEDDING_BATCH_SIZE_DEFAULT: Final = 32
EMBEDDING_BATCH_SIZE_MINIMUM: Final = 1
EMBEDDING_BATCH_SIZE_MAXIMUM: Final = 256

EMBEDDING_BATCH_SIZE_SPEC: Final = KeySpec(
    key=EMBEDDING_BATCH_SIZE_KEY,
    type=ValueType.INT,
    scope=Scope.WORKSPACE,
    floor=None,
    explicit_per_workspace=False,
    default=EMBEDDING_BATCH_SIZE_DEFAULT,
    minimum=EMBEDDING_BATCH_SIZE_MINIMUM,
    maximum=EMBEDDING_BATCH_SIZE_MAXIMUM,
)

EMBEDDING_DIMENSIONS: Final = 384
"""The one vector width this release stores, and the dimension authority.

Migration ``0003_dense_retrieval`` pins it into the column type, ``vector(384)``, so
Postgres refuses a row of any other width; the provider registry refuses to register a
provider declaring another. Changing it is delete, alter, rebuild — a new migration
and a new provider together — never a setting.
"""

EMBED_INPUT_VERSION: Final = 1
"""Which composition of a memory's text the stored vectors were produced from.

Version 1 is :func:`rheo_recallatron.embedding.embed_input`: the title, one newline,
the body. Any change to what that function returns bumps this number, and the rebuild
then deletes every stored vector in a workspace stamped with an older one, because a
vector of the old composition is stale whatever model wrote it.
"""

EMBED_JOB_KIND: Final = f"{MODULE_ID}.embed"
REBUILD_JOB_KIND: Final = f"{MODULE_ID}.embedding_rebuild"

EMBED_MAX_ATTEMPTS: Final = 5
"""Attempts one memory's embed job gets before it lands in the core failure list.

A provider that raises is a real failure, retried under the ordinary backoff: five
attempts span about seven minutes of it, long enough to ride out a transient fault and
short enough that a provider that is simply broken is reported rather than retried for
an afternoon.
"""

REBUILD_MAX_ATTEMPTS: Final = 3
"""Attempts one rebuild job gets. Fewer than an embed job's, because each attempt is
the whole walk: a rebuild that failed three times is a provider or a corpus problem an
owner has to look at, not a blip."""


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """What the two rows together say: a window, or no window at all (§ A9).

    **A value rather than an ``int | None``, because the ``None`` would have meant two
    opposite things.** The reader this replaces answered ``int | None``, where ``None``
    was ``retention_unavailable`` — fail closed, refuse everything. "No age bound"
    needed a third answer, and reusing ``None`` for it would have made every existing
    branch mean the opposite of what it says. So the *unavailable* answer keeps the
    ``None``, and this value carries the two usable policies; a branch that forgets one
    of them is a ``make typecheck`` failure rather than a sweep.

    ``days is None`` is the default posture: no horizon exists, so no read filters by
    age, no sweep selects a root, and no acceptance rechecks a source clock.
    """

    days: int | None

    @classmethod
    def unbounded(cls) -> "RetentionPolicy":
        """The gate is off. Nothing in this workspace expires by age."""
        return cls(days=None)

    @classmethod
    def of_days(cls, days: int) -> "RetentionPolicy":
        """The gate is on, against a validated in-range window."""
        return cls(days=days)

    @property
    def expires_by_age(self) -> bool:
        return self.days is not None

    def horizon(self, now: datetime) -> datetime | None:
        """The oldest ``recorded_at`` still readable, or ``None`` for no horizon.

        Equality is retained (§ A9), so every comparison against a horizon is ``<``
        for expired and ``>=`` for kept.
        """
        return None if self.days is None else now - timedelta(days=self.days)


# --- Architecture § A13: recall and read input bounds ---------------------------------

RECALL_QUERY_MIN_LENGTH: Final = 1
RECALL_QUERY_MAX_LENGTH: Final = 1000
RECALL_K_MIN: Final = 1
RECALL_K_MAX: Final = 50
RECALL_K_DEFAULT: Final = 10

READ_CONTEXT_MIN: Final = 0
READ_CONTEXT_MAX: Final = 10
READ_CONTEXT_DEFAULT: Final = 2

# --- Architecture § A13: the bounded scan and the shared traversal budget ------------

CANDIDATE_SCAN_LIMIT: Final = 500
"""How many ordered candidates a read may resolve, and how far recall may scan.

The read window's cap is § A6's verbatim: at most 500 candidates are fully evaluated.
Recall reuses the same number as the bound on how deep into the scored candidate list
it will look for ``k`` eligible rows, rather than declaring a second limit that would
have to be justified separately and kept in step with this one.
"""

CANDIDATE_SENTINEL_LIMIT: Final = CANDIDATE_SCAN_LIMIT + 1
"""``LIMIT 501``. The 501st identifier is an existence test and never a content read:
its presence is the whole of the ``window_scan_limit`` signal, and no part of it is
resolved, counted or described."""

# --- the dense index -----------------------------------------------------------------

HNSW_M: Final = 16
HNSW_EF_CONSTRUCTION: Final = 64
"""The HNSW build parameters migration ``0003_dense_retrieval`` writes into the index.

pgvector's own defaults, written down so a later change to either is a diff and a new
revision rather than a silent difference between two deployments' builds.
"""

HNSW_EF_SEARCH_MULTIPLIER: Final = 4
HNSW_EF_SEARCH_MIN: Final = 64
HNSW_EF_SEARCH_MAX: Final = 1000
"""``hnsw.ef_search`` for a dense statement: the arm's ``LIMIT`` times the multiplier,
never below the minimum. The maximum is a hard clamp, not a tuning value: it is the
setting's own range ceiling, and asking for more is an error from Postgres rather than
a wider search."""

HNSW_MAX_SCAN_TUPLES: Final = CANDIDATE_SCAN_LIMIT * 40
"""``hnsw.max_scan_tuples`` for an iterative scan: 20,000, pgvector's default written
down. It bounds the work a filtered dense statement does when too few rows clear the
relevance floor to fill its ``LIMIT``."""

MAX_DISTINCT_REFERENCES: Final = 4096
MAX_REFERENCE_DEPTH: Final = 64
"""One request-wide budget, shared by the target, every candidate, and every link
either of them reaches. Exceeding it refuses ``reference_scan_limit`` with no partial
content and no window metadata."""

# --- Architecture § A13: the write-side bounds ---------------------------------------

TITLE_MIN_LENGTH: Final = 1
TITLE_MAX_LENGTH: Final = 200
"""Trimmed characters. A title of spaces is not a short title, it is a missing one."""

BODY_MAX_BYTES: Final = 65536
"""UTF-8 **bytes**, not characters, and nonblank.

Bytes because that is what the column and every transport actually cost; a
character bound would admit a body four times the size it was meant to.
"""

ENTITY_NAME_MIN_LENGTH: Final = 1
ENTITY_NAME_MAX_LENGTH: Final = 200
MENTION_ROLE_MAX_LENGTH: Final = 100
"""The typed half of the bound ``memory_mention_role_length`` already holds in DDL."""

MAX_REFS_PER_WRITE: Final = 64
MAX_MENTIONS_PER_WRITE: Final = 64
DERIVE_SOURCES_MIN: Final = 1
DERIVE_SOURCES_MAX: Final = 64
"""§ A13's per-write ceilings. They bound what a caller *supplies*; they deliberately
do not bound what derive **inherits**, which is checked against the shared reference
budget instead — a copied graph too large to read back refuses
``reference_scan_limit`` rather than being silently truncated."""

MINIMUM_REVISION: Final = 1
"""§ A7's floor on a compare-and-set, and the database's own floor on the column.

``memory_revision_positive`` refuses a row below it, so a caller supplying a smaller
``expected_revision`` is naming a revision no row can ever hold — which is an input
error rather than a stale copy, and is refused as one.
"""

ENTITY_LIST_LIMIT_MIN: Final = 1
ENTITY_LIST_LIMIT_MAX: Final = 50
ENTITY_LIST_LIMIT_DEFAULT: Final = 10

# --- dedup candidates -----------------------------------------------------------------

DEDUP_PAIR_FLOOR: Final = 0.80
"""The lowest cosine similarity at which two memories are proposed as a duplicate pair.

**A cosine similarity, not a percent,** in the same space as the dense relevance floor:
``1 - (a.vector <=> b.vector)`` on the shipped provider. A package constant rather than
a settings key, because no caller exists to tune it; run 1b's measurement pass over the
migrated corpus is where it moves.

**Where 0.80 came from** (spec evidence E2 § 4, fastembed's MiniLM-L6-v2): paraphrase
duplicates land 0.78-0.91 (the ``rent due`` / ``rent`` pair 0.829, the dentist
paraphrase 0.781, one body under two titles 0.908); distinct notes on one template land
about 0.71 (``apples`` / ``pears`` 0.707); and among real predecessor turns the nearest
*other* turn sits at p50 0.618, p90 0.802. So 0.80 admits every measured paraphrase but
one and excludes the template pairs by 0.09. It sits at the foot of the paraphrase band
on purpose: the operation only *proposes* pairs for a person to decide on, so a floor
set low costs review noise and never a wrong merge. The pairs are authored fixtures,
not a corpus study, which is why 1b confirms the number.
"""

DEDUP_SCAN_LIMIT: Final = CANDIDATE_SCAN_LIMIT
"""How many of the newest eligible embedded memories one dedup call compares.

The same bound every other scan here uses, not a second number to keep in step. It is
also the whole cost argument for ``dedup_candidates`` being a ``READ`` operation: see
:func:`rheo_recallatron.storage.repository.nearest_embedding_pairs` before raising it.
"""

DEDUP_LIMIT_MIN: Final = 1
DEDUP_LIMIT_MAX: Final = 50
DEDUP_LIMIT_DEFAULT: Final = 10
"""How many pairs one call returns, mirroring ``RECALL_K``."""

AUTOMATIC_BOUND_PURPOSE: Final = "internal_analysis"
"""§ A5: automatic acceptance binds exactly one purpose, for both producer kinds.

Named here rather than inlined at the seam because the product consequence is the
reason it is a constant: a memory recorded under it is invisible to any bound read
requiring a different purpose, and visible to an unbound browse and to a read bound to
it. Changing this value changes that visibility for every automatic memory, which is a
spec amendment and not a local edit.
"""

AUTOMATIC_PRODUCER_KINDS: Final = ("rheo_runtime", "claude_code_local")
"""The two producer kinds whose acceptance binds :data:`AUTOMATIC_BOUND_PURPOSE`.

``migration`` is the third receipt producer kind and is deliberately absent: it may be
unbound and preserves each imported memory's own ratified purposes, under its
separately verified import contract.
"""
