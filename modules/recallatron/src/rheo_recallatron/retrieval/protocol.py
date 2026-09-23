"""The ``RetrievalStrategy`` protocol and the values that cross it.

**A stated deviation from the storage architecture's shorthand.** That document writes
the three methods as ``(ctx, ...)``. Every live handler in this tree takes
``(ctx, uow, input)`` and every SQL read goes through ``uow.connection``, so the real
signature carries the unit of work: a strategy cannot reach the database without one,
and one that opened its own connection could disagree with what this transaction sees.
"""

from dataclasses import dataclass
from typing import Protocol
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
    """

    query: str
    mode: ReadMode
    memory: MemoryRequest
    limit: int


@dataclass(frozen=True, slots=True)
class Hit:
    """One ranked candidate. ``ref`` is the memory's id; the canonical reference is
    minted when an eligible hit is rendered, never for one the walk drops."""

    ref: UUID
    score: float
    strategy: str


@dataclass(frozen=True, slots=True)
class ArmProvenance:
    """The length of each arm's ranked list as it entered fusion — after the arm's
    own ``LIMIT``, before the permission walk. Zero for an arm that did not run."""

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
