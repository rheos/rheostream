"""Small relevance measures, not a proxy based on titles or nonempty output."""

from collections.abc import Sequence, Set
from dataclasses import dataclass


@dataclass(frozen=True)
class QualityScore:
    precision_at_k: float
    recall_at_k: float | None
    reciprocal_rank: float
    false_positive_count: int


def score(ranked: Sequence[str], relevant: Set[str], *, k: int) -> QualityScore:
    """Precision uses the fixed cutoff, so a short result cannot inflate it.

    A no-answer question has no recall denominator: report ``None``, not a perfect
    recall. Its returned records are counted as false positives. Rank is measured
    only within the cutoff. Duplicate results are a defect, not extra relevance.
    """
    if k < 1:
        raise ValueError("cutoff must be positive")
    if len(ranked) != len(set(ranked)):
        raise ValueError("ranked results must be unique")
    top = ranked[:k]
    hits = len(set(top) & relevant)
    first = next((rank for rank, key in enumerate(top, 1) if key in relevant), None)
    return QualityScore(
        precision_at_k=hits / k,
        recall_at_k=hits / len(relevant) if relevant else None,
        reciprocal_rank=1 / first if first is not None else 0.0,
        false_positive_count=len(top) - hits,
    )
