"""The embedding seam's pure halves: no database, no model artifact, no network.

Seams: ``FakeEmbeddingProvider.embed`` (AC 5 — width, unit norm, determinism, and that
it touches neither a socket nor the real runtime); ``LocalEmbeddingProvider.embed``'s
unit-norm refusal, driven through a stand-in model so the check itself is what is under
test; the enqueue predicate; the provider name reader's undeclared-key branch; and the
rebuild's batch-size read from stored rows.

The real provider's own tests — the model, its vectors, its missing-extra degrade —
live in ``tests/postgres/test_memory_embedding.py``, because they need the extra and
this file must run without it.
"""

import math
import socket
import sys
from collections.abc import Iterable, Sequence

import pytest
from rheo_core.settings.schema import REGISTRY
from rheo_recallatron.configuration import (
    EMBEDDING_BATCH_SIZE_DEFAULT,
    EMBEDDING_BATCH_SIZE_KEY,
    EMBEDDING_BATCH_SIZE_MAXIMUM,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_PROVIDER_KEY,
    STRATEGY_DENSE,
    STRATEGY_HYBRID,
    STRATEGY_LEXICAL,
)
from rheo_recallatron.embedding import registry
from rheo_recallatron.embedding.enqueue import should_enqueue_embed
from rheo_recallatron.embedding.fake import FakeEmbeddingProvider
from rheo_recallatron.embedding.local import (
    EmbeddingNotNormalised,
    LocalEmbeddingProvider,
)
from rheo_recallatron.embedding.operations import batch_size_in


def _norm(vector: Sequence[float]) -> float:
    return math.sqrt(math.fsum(value * value for value in vector))


class _SocketRefused(Exception):
    pass


def _refuse_sockets(*_args: object, **_kwargs: object) -> socket.socket:
    raise _SocketRefused("a socket was opened")


# --- AC 5: the fake provider ----------------------------------------------------------


def test_the_fake_provider_is_384_wide_unit_normalised_and_deterministic() -> None:
    provider = FakeEmbeddingProvider()
    texts = ["apples\na note about apples", "pears", "", "   "]

    first = provider.embed(texts)
    second = provider.embed(texts)

    assert provider.dimensions == EMBEDDING_DIMENSIONS == 384
    assert len(first) == len(texts)
    for vector in first:
        assert len(vector) == EMBEDDING_DIMENSIONS
        assert abs(_norm(vector) - 1.0) < 1e-9
    assert first == second
    # Different text, different direction; and a text with no tokens is still a unit
    # vector, never the zero vector that has no cosine against anything.
    assert first[0] != first[1]
    assert first[2] == first[3]


def test_the_fake_provider_loads_no_runtime_and_opens_no_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch ``socket.socket`` to raise for the whole call, and make sure the real
    runtime is not imported by it. The runtime may already be loaded by an earlier test
    in the session, so it is taken out of ``sys.modules`` for the duration first, which
    makes "absent before" true here rather than an assumption."""
    monkeypatch.delitem(sys.modules, "fastembed", raising=False)
    monkeypatch.setattr(socket, "socket", _refuse_sockets)
    # The patch's own positive control: a deliberate socket under it raises.
    with pytest.raises(_SocketRefused):
        socket.socket()

    assert "fastembed" not in sys.modules
    vectors = FakeEmbeddingProvider().embed(["apples", "pears"])
    assert len(vectors) == 2
    assert "fastembed" not in sys.modules


# --- the local provider's unit-norm refusal -------------------------------------------


class _StandInModel:
    """Answers fixed vectors in place of the ONNX model, so the provider's own check is
    the only thing between them and the caller."""

    def __init__(self, vector: tuple[float, ...]) -> None:
        self._vector = vector

    def embed(self, documents: list[str]) -> Iterable[tuple[float, ...]]:
        return [self._vector for _ in documents]


def _one_hot(scale: float) -> tuple[float, ...]:
    return (scale,) + (0.0,) * (EMBEDDING_DIMENSIONS - 1)


def test_the_local_provider_refuses_a_batch_that_is_not_unit_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = LocalEmbeddingProvider()
    monkeypatch.setattr(provider, "_load", lambda: _StandInModel(_one_hot(5.0)))
    with pytest.raises(EmbeddingNotNormalised):
        provider.embed(["apples"])

    # Positive control: the same path with a unit vector answers it, as floats.
    monkeypatch.setattr(provider, "_load", lambda: _StandInModel(_one_hot(1.0)))
    assert provider.embed(["apples"]) == [_one_hot(1.0)]


def test_the_local_provider_loads_nothing_for_an_empty_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = LocalEmbeddingProvider()

    def _no_load() -> _StandInModel:
        raise AssertionError("an empty batch must not load the model")

    monkeypatch.setattr(provider, "_load", _no_load)
    assert provider.embed([]) == []


# --- the enqueue predicate ------------------------------------------------------------


@pytest.mark.parametrize(
    ("strategy", "with_provider", "expected"),
    [
        (STRATEGY_DENSE, True, True),
        (STRATEGY_HYBRID, True, True),
        (STRATEGY_LEXICAL, True, False),
        (STRATEGY_DENSE, False, False),
        (STRATEGY_HYBRID, False, False),
        (STRATEGY_LEXICAL, False, False),
    ],
)
def test_an_embed_job_needs_a_dense_reading_strategy_and_a_provider(
    strategy: str, with_provider: bool, expected: bool
) -> None:
    provider = FakeEmbeddingProvider() if with_provider else None
    assert should_enqueue_embed(strategy, provider) is expected


# --- the provider name reader ---------------------------------------------------------


def test_an_undeclared_provider_key_reads_as_no_provider_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the harness the module's keys never reach the process-wide registry, and
    resolving an undeclared key raises. The reader must answer ``none`` instead."""
    assert REGISTRY.lookup(EMBEDDING_PROVIDER_KEY) is None
    assert registry.configured_provider_name() == "none"
    assert registry.resolve_provider() is None

    # Positive control: the one mechanism a test selects a provider by reaches the
    # resolver, so the ``None`` above is the reader's answer and not an empty registry.
    monkeypatch.setitem(
        registry.PROVIDERS, registry.FAKE_PROVIDER, FakeEmbeddingProvider()
    )
    monkeypatch.setattr(registry, "configured_provider_name", lambda: "fake")
    assert isinstance(registry.resolve_provider(), FakeEmbeddingProvider)


def test_an_unregistered_provider_name_resolves_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        registry, "configured_provider_name", lambda: "no-such-provider"
    )
    assert registry.resolve_provider() is None


# --- the rebuild's batch size ---------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, EMBEDDING_BATCH_SIZE_DEFAULT),
        ("64", 64),
        (str(EMBEDDING_BATCH_SIZE_MAXIMUM), EMBEDDING_BATCH_SIZE_MAXIMUM),
        ("not-a-number", EMBEDDING_BATCH_SIZE_DEFAULT),
        ("0", EMBEDDING_BATCH_SIZE_DEFAULT),
        (str(EMBEDDING_BATCH_SIZE_MAXIMUM + 1), EMBEDDING_BATCH_SIZE_DEFAULT),
    ],
)
def test_the_batch_size_read_degrades_to_the_default(
    stored: str | None, expected: int
) -> None:
    rows = {} if stored is None else {EMBEDDING_BATCH_SIZE_KEY: stored}
    assert batch_size_in(rows) == expected
