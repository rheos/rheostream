"""``FakeEmbeddingProvider``: a deterministic hashed bag of words, for tests only.

It exists for the module's fast unit tests, which must not load a model, and for the
failure cases a working model cannot produce on demand. It is **not** a stand-in for the
real embedding space: nothing that asserts retrieval quality runs against it, and it
carries no synonym table that could make such an assertion pass.

Registered only under the ``test`` profile (see ``registry.py``), so a production
deployment naming ``fake`` resolves no provider and degrades, rather than filling
``memory_embedding`` with vectors that mean nothing. It lives in this package rather
than under the test harness because the harness is imported by the checkout that has
this module deleted, and must not name it.
"""

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Final

from rheo_recallatron.configuration import EMBEDDING_DIMENSIONS

FAKE_MODEL_ID: Final = "rheo-test/hashed-bag-of-words-384"
"""Not a model anybody publishes, so a stored row of it cannot pass for a real one."""

_TOKEN: Final = re.compile(r"\w+")


def _bucket(token: str) -> int:
    """A stable bucket for one token.

    A digest rather than ``hash()``, which Python salts per process: two processes (a
    test and the worker it drives) must agree on every vector.
    """
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % EMBEDDING_DIMENSIONS


_EMPTY_TEXT_VECTOR: Final = (1.0,) + (0.0,) * (EMBEDDING_DIMENSIONS - 1)
"""What a text with no tokens embeds to: a fixed unit vector, never a zero one, which
has no direction and no cosine against anything."""


class FakeEmbeddingProvider:
    """384 dimensions, in-process, no artifact, no socket, one answer per text."""

    @property
    def model_id(self) -> str:
        return FAKE_MODEL_ID

    @property
    def dimensions(self) -> int:
        return EMBEDDING_DIMENSIONS

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        return [_one(text) for text in texts]


def _one(text: str) -> tuple[float, ...]:
    counts = [0.0] * EMBEDDING_DIMENSIONS
    for token in _TOKEN.findall(text.casefold()):
        counts[_bucket(token)] += 1.0
    norm = math.sqrt(math.fsum(value * value for value in counts))
    if norm == 0.0:
        return _EMPTY_TEXT_VECTOR
    return tuple(value / norm for value in counts)
