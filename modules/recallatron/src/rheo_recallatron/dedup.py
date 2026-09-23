"""``recallatron.memory.dedup_candidates``: memory pairs that may say the same thing.

``READ``, owner/member/service (the set ``recall`` declares), no audit row, and **no MCP
tool**: like the two entity reads it is a service-only operation until a caller story
exists for it. It stores nothing and returns bounded ``{ref_a, ref_b, score}`` pairs.

**It only proposes.** A pair is a question for a person, never an instruction. Nothing
in this file writes, and no code path added with it acts on a pair: the output models
carry no field and no method a caller could act on, and
``modules/recallatron/tests/test_memory_contracts.py`` scans this file's source to keep
it that way.

**Eligibility runs before any distance is computed.** The row-local SQL conditions
select the bounded candidate set, at most ``DEDUP_SCAN_LIMIT`` of the newest; every
one of those goes through :func:`~rheo_recallatron.eligibility.eligible_memory`, links
included; and pairs are formed only among the ones the caller may read. Pairing first
and filtering after would leak: a memory the caller cannot read would take a readable
memory's nearest-neighbour place, the real pair beside it would never form, and the
missing pair would tell the caller that a hidden near-copy exists.

**Both sides of every pair are checked again before it is returned**, as defence in
depth, and a denial on either side drops the **whole pair**. Returning the readable
side alone would still tell the caller that something they cannot see resembles it,
so there is no redacted or blank half, ever.

**Empty, never refused, when there is nothing dense to compare.** With no provider
configured, or no rows for its model (a workspace that only ever ran ``lexical``), the
answer is no pairs. The only refusals are the ones every read has: the purpose binding,
the retention policy, and a spent reference budget.
"""

from typing import Final

from pydantic import Field
from rheo_contracts import (
    Idempotency,
    OperationDeclaration,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs.resolver import UnitOfWork

from rheo_recallatron.configuration import (
    DEDUP_LIMIT_DEFAULT,
    DEDUP_LIMIT_MAX,
    DEDUP_LIMIT_MIN,
    DEDUP_PAIR_FLOOR,
    DEDUP_SCAN_LIMIT,
    MEMORY_RECORD_TYPE,
    MODULE_ID,
)
from rheo_recallatron.contracts import Strict
from rheo_recallatron.eligibility import (
    READ_ROLES,
    Denied,
    MemoryRequest,
    ReadMode,
    eligible_memory,
    evaluate_all,
    memory_reference,
    row_local_conditions,
)
from rheo_recallatron.embedding.registry import resolve_provider
from rheo_recallatron.refusals import REFERENCE_SCAN_LIMIT
from rheo_recallatron.storage.repository import (
    EmbeddingPair,
    dedup_candidate_ids,
    nearest_embedding_pairs,
)
from rheo_recallatron.writes import checked_purpose, opened

MEMORY_DEDUP_CANDIDATES: Final = f"{MODULE_ID}.{MEMORY_RECORD_TYPE}.dedup_candidates"


class DedupCandidatesInput(Strict):
    limit: int = Field(
        default=DEDUP_LIMIT_DEFAULT, ge=DEDUP_LIMIT_MIN, le=DEDUP_LIMIT_MAX
    )
    purpose: str | None = None


class DedupPair(Strict):
    """Two memories that may be duplicates, and how close they are.

    ``ref_a`` sorts before ``ref_b``. ``score`` is **cosine similarity**, ``1 -
    (a.vector <=> b.vector)`` over the two stored vectors: the definition the dense
    recall arm's score uses, at or above ``DEDUP_PAIR_FLOOR``, and comparable across the
    pairs of one response because one expression produces all of them.
    """

    ref_a: str
    ref_b: str
    score: float


class DedupCandidates(Strict):
    pairs: tuple[DedupPair, ...]


def _both_eligible(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    pair: EmbeddingPair,
    *,
    request: MemoryRequest,
) -> bool:
    """Both sides through the one eligibility function in ``current`` mode, or no pair.

    Defence in depth: the scan only pairs memories already found eligible, and the
    request caches each decision, so this costs no query and denies nothing unless the
    scan returned a memory it was not given.
    """
    for memory_id in (pair.first, pair.second):
        decision = eligible_memory(
            ctx, uow, memory_id, mode=ReadMode.CURRENT, request=request
        )
        if isinstance(decision, Denied):
            if decision.state == REFERENCE_SCAN_LIMIT:
                raise _budget_spent()
            return False
    return True


def _budget_spent() -> OperationRefused:
    """A spent reference budget refuses the whole call, as it does in ``recall()``:
    quietly skipping the rest would return a shorter list the caller cannot tell was
    cut."""
    return OperationRefused(
        REFERENCE_SCAN_LIMIT, "this read exceeded its reference budget"
    )


def dedup_candidates(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: DedupCandidatesInput
) -> DedupCandidates:
    """At most ``limit`` near-duplicate pairs this caller may read both sides of.

    Purpose binding and the one retention read first, as ``recall()`` does. Then the
    bounded candidate set, full eligibility over each of its members, and the pair scan
    over the configured provider's vectors for the readable ones only. Last the walk,
    in the scan's order (score descending, then references): each pair is kept only
    when both of its memories are eligible, until ``limit`` pairs are kept or the
    scan's pairs run out.
    """
    checked_purpose(ctx, model_input.purpose)
    request = opened(ctx, uow)
    provider = resolve_provider()
    if provider is None:
        return DedupCandidates(pairs=())

    bounded = dedup_candidate_ids(
        uow.connection,
        conditions=row_local_conditions(request, ReadMode.CURRENT),
        model_id=provider.model_id,
        scan_limit=DEDUP_SCAN_LIMIT,
    )
    readable, overflowed = evaluate_all(
        ctx, uow, bounded, mode=ReadMode.CURRENT, request=request
    )
    if overflowed is not None:
        raise _budget_spent()
    scanned = nearest_embedding_pairs(
        uow.connection,
        memory_ids=[memory_id for memory_id, _ in readable],
        model_id=provider.model_id,
        floor=DEDUP_PAIR_FLOOR,
    )
    pairs: list[DedupPair] = []
    for candidate in scanned:
        if len(pairs) == model_input.limit:
            break
        if not _both_eligible(ctx, uow, candidate, request=request):
            continue
        pairs.append(
            DedupPair(
                ref_a=memory_reference(candidate.first),
                ref_b=memory_reference(candidate.second),
                score=candidate.score,
            )
        )
    return DedupCandidates(pairs=tuple(pairs))


DEDUP_DECLARATION: Final = OperationDeclaration(
    name=MEMORY_DEDUP_CANDIDATES,
    safety_class=SafetyClass.READ,
    roles=READ_ROLES,
    input_model=DedupCandidatesInput,
    output=DedupCandidates,
    idempotency=Idempotency.NONE,
    # ``READ`` needs no audit spec, and this read has nothing an audit row should hold.
    # ``READ`` also rests on the scan being bounded: see ``nearest_embedding_pairs``.
    audit=None,
)
"""Service-only in this release, like ``recallatron.entity.list`` and ``.get``: no MCP
tool names it, so no model reaches it (spec requirement 18)."""
