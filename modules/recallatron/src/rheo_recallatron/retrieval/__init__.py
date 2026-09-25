"""The retrieval adapter: the one seam every recall ranks through.

``recall()`` keeps input validation, purpose binding, retention and the permission
walk; ranking is whatever :func:`~rheo_recallatron.retrieval.dispatch.resolve_strategy`
hands back for this workspace. The candidate set stays
:func:`~rheo_recallatron.eligibility.row_local_conditions` for every strategy, so the
SQL that decides what may be ranked is strategy-independent by construction.
"""

from rheo_recallatron.retrieval.dispatch import STRATEGY_REGISTRY, resolve_strategy
from rheo_recallatron.retrieval.lexical import LexicalStrategy
from rheo_recallatron.retrieval.protocol import (
    ARM_DENSE,
    ARM_LEXICAL,
    ArmProvenance,
    Hit,
    IndexItem,
    RetrievalStrategy,
    SearchRequest,
    SearchResult,
)

__all__ = [
    "ARM_DENSE",
    "ARM_LEXICAL",
    "STRATEGY_REGISTRY",
    "ArmProvenance",
    "Hit",
    "IndexItem",
    "LexicalStrategy",
    "RetrievalStrategy",
    "SearchRequest",
    "SearchResult",
    "resolve_strategy",
]
