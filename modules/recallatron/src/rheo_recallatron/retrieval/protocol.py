"""The ``RetrievalStrategy`` protocol and the values that cross it.

**A stated deviation from the storage architecture's shorthand.** That document writes
the three methods as ``(ctx, ...)``. Every live handler in this tree takes
``(ctx, uow, input)`` and every SQL read goes through ``uow.connection``, so the real
signature carries the unit of work: a strategy cannot reach the database without one,
and one that opened its own connection could disagree with what this transaction sees.
"""

from dataclasses import dataclass
from typing import Final, Protocol
from uuid import UUID

from rheo_contracts import WorkspaceContext
from rheo_core.refs.resolver import UnitOfWork

from rheo_recallatron.eligibility import MemoryRequest, ReadMode


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """One ranking request, already validated and opened by ``recall()``.

    ``limit`` is the arm's own bound, explicit rather than read from
    :data:`~rheo_recallatron.configuration.CANDIDATE_SCAN_LIMIT` inside the strategy,
    so a caller can ask one arm for fewer rows than another.

    ``k`` is how many items the caller asked ``recall()`` for, which ``limit`` does not
    say: the hybrid dense arm is ``k`` times the over-fetch multiplier wide, while every
    arm's ``limit`` on the way in is the scan bound.
    """

    query: str
    mode: ReadMode
    memory: MemoryRequest
    limit: int
    k: int


ARM_LEXICAL: Final = "lexical"
ARM_DENSE: Final = "dense"
"""The two arm names a :attr:`Hit.arms` set may hold, one per ``ArmCounts`` field."""


@dataclass(frozen=True, slots=True)
class Hit:
    """One ranked candidate. ``ref`` is the memory's id; the canonical reference is
    minted when an eligible hit is rendered, never for one the walk drops.

    ``strategy`` is informational: the name of whatever ranked this hit, which a
    fusing strategy may set per arm. ``recall()`` does **not** label its items from
    it. Every item, and the response's provenance, carry the dispatched strategy's
    ``name``, so the two can never disagree within one response.

    ``arms`` names the arms whose ranked list held this hit: one for a single-arm
    strategy, one or both after fusion. ``recall()`` counts the caller-facing
    ``provenance.arms`` from this set over the items it returns, and never from
    :class:`ArmProvenance`, so a hit the walk drops is never counted.
    """

    ref: UUID
    score: float
    strategy: str
    arms: frozenset[str]


@dataclass(frozen=True, slots=True)
class ArmProvenance:
    """The length of each arm's ranked list as it entered fusion — after the arm's
    own ``LIMIT``, before the permission walk. Zero for an arm that did not run.

    **Internal diagnostics only.** These lengths include candidates the caller may not
    read, so they never reach a response: a non-zero count beside an empty answer
    would tell the caller a hidden memory matched (#121). Tests and operators read
    them from the strategy directly."""

    lexical: int
    dense: int


@dataclass(frozen=True, slots=True)
class SearchResult:
    """The ranked hits, best first, and what produced them."""

    hits: tuple[Hit, ...]
    arms: ArmProvenance
    dense_available: bool


@dataclass(frozen=True, slots=True)
class IndexItem:
    """What a strategy is handed to index one memory."""

    memory_id: UUID
    title: str
    body: str


class RetrievalStrategy(Protocol):
    """A way of ranking the eligible candidate set.

    ``search`` ranks only rows
    :func:`~rheo_recallatron.eligibility.row_local_conditions` admits; it never
    decides eligibility, which the caller's walk does per hit.
    """

    @property
    def name(self) -> str: ...

    def index(self, ctx: WorkspaceContext, uow: UnitOfWork, item: IndexItem) -> None:
        """Bring this strategy's derived index up to date for one memory."""
        ...

    def invalidate(self, ctx: WorkspaceContext, uow: UnitOfWork, ref: str) -> None:
        """Remove every derived index entry this strategy holds for ``ref``."""
        ...

    def search(
        self, ctx: WorkspaceContext, uow: UnitOfWork, request: SearchRequest
    ) -> SearchResult:
        """Rank at most ``request.limit`` candidates, best first."""
        ...
