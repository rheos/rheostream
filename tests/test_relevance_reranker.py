"""Runtime guards: malformed scores degrade without leaking exception contents."""

import socket
from collections.abc import Iterator

import pytest
from rheo_recallatron.retrieval import rerank


@pytest.mark.parametrize("scores", [(1.0,), (float("nan"), 2.0), (1.0, float("inf"))])
def test_malformed_scores_are_not_used(monkeypatch: pytest.MonkeyPatch, scores) -> None:
    class Model:
        def rerank(self, query: str, documents, **kwargs) -> Iterator[float]:
            yield from scores

    model = rerank.LocalReranker()
    monkeypatch.setattr(model, "_load", lambda: Model())
    with pytest.raises(ValueError, match="malformed"):
        model.scores("invented query", ["one", "two"])


def test_failure_is_explicit_and_log_is_content_free(monkeypatch, caplog) -> None:
    def fail(query, documents):
        raise RuntimeError("SENSITIVE_QUERY_AND_NOTE")

    monkeypatch.setattr(rerank.LOCAL_RERANKER, "scores", fail)
    assert rerank.relevance_scores("invented query", ["invented note"]) is None
    assert "RuntimeError" in caplog.text
    assert "SENSITIVE_QUERY_AND_NOTE" not in caplog.text


def test_empty_batch_never_loads_model(monkeypatch) -> None:
    def forbidden():
        raise AssertionError("empty results must not load a model")

    model = rerank.LocalReranker()
    monkeypatch.setattr(model, "_load", forbidden)
    assert model.scores("invented query", []) == ()


def test_warm_real_relevance_inference_needs_no_socket(monkeypatch) -> None:
    model = rerank.LOCAL_RERANKER
    model._load()

    def forbidden(*args, **kwargs):
        raise AssertionError("warm relevance inference must stay on-box")

    monkeypatch.setattr(socket, "socket", forbidden)
    scores = model.scores(
        "Where is the potting shed key kept?",
        [
            "The potting shed key is in the red tin beside the seed trays.",
            "The front door key is on the kitchen shelf. "
            "It does not open the potting shed.",
        ],
    )
    assert scores[0] > scores[1]
