"""Positive and negative controls for the synthetic relevance oracle."""

import pytest
from harness.search_quality import QualityScore, score


def test_nonempty_wrong_results_do_not_pass_for_relevance() -> None:
    measured = score(["distractor"], {"answer"}, k=3)
    assert measured == QualityScore(0.0, 0.0, 0.0, 1)


def test_rank_coverage_and_fixed_cutoff_are_independent() -> None:
    measured = score(
        ["distractor", "answer-a", "answer-b"], {"answer-a", "answer-b"}, k=3
    )
    assert measured == QualityScore(2 / 3, 1.0, 0.5, 1)
    assert score(["answer-a"], {"answer-a", "answer-b"}, k=3) == QualityScore(
        1 / 3, 0.5, 1.0, 0
    )
    assert score(["distractor", "answer-a"], {"answer-a"}, k=1).recall_at_k == 0.0


def test_no_answer_is_not_perfect_recall() -> None:
    assert score([], set(), k=3) == QualityScore(0.0, None, 0.0, 0)
    assert score(["distractor"], set(), k=3) == QualityScore(0.0, None, 0.0, 1)


def test_invalid_measurement_inputs_are_not_silently_scored() -> None:
    with pytest.raises(ValueError, match="positive"):
        score([], set(), k=0)
    with pytest.raises(ValueError, match="unique"):
        score(["answer", "answer"], {"answer"}, k=3)
