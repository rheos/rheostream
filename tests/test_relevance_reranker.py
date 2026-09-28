"""Runtime guards: malformed scores degrade without leaking exception contents."""

import socket
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

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


@pytest.mark.parametrize(
    ("k", "window"), [(1, 3), (3, 9), (10, 30), (20, 50), (50, 50)]
)
def test_relevance_window_is_capped_without_shortening_requested_k(k, window) -> None:
    assert rerank.candidate_window(k) == window
    assert k <= window <= rerank.WINDOW_LIMIT


def test_failed_load_is_shared_backed_off_and_retries_after_failure(
    monkeypatch,
) -> None:
    model = rerank.LocalReranker()
    clock = [100.0]
    attempts = []
    monkeypatch.setattr(rerank, "monotonic", lambda: clock[0])

    def fail():
        attempts.append("load")
        # A slow failed fetch: the retry deadline must start after this time.
        clock[0] = 150.0
        raise OSError("synthetic artifact outage")

    monkeypatch.setattr(model, "_create_model", fail)

    def request(_):
        try:
            model._load()
        except Exception as exc:
            return type(exc)
        raise AssertionError("load should fail during this outage")

    with ThreadPoolExecutor(max_workers=4) as pool:
        failures = list(pool.map(request, range(8)))
    assert attempts == ["load"]
    assert failures.count(OSError) == 1
    assert failures.count(rerank.RerankerCoolingDown) == 7
    clock[0] = 150.0 + rerank.LOAD_RETRY_SECONDS - 1
    assert request(None) is rerank.RerankerCoolingDown
    recovered = object()
    monkeypatch.setattr(model, "_create_model", lambda: recovered)
    clock[0] += 1
    assert model._load() is recovered
    # A resident healthy model requires no artifact fetch or repeated initialization.
    monkeypatch.setattr(model, "_create_model", fail)
    assert model._load() is recovered
    assert attempts == ["load"]
