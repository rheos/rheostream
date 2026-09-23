"""``LocalEmbeddingProvider``: MiniLM-L6-v2 through fastembed's ONNX runtime, on-box.

No credential, no socket at inference time, and no memory text leaves the deployment.
The model artifact is fetched once into ``<data_root>/models/fastembed/`` and read from
there afterwards.

**Lazy, and resident wherever it is first used.** The model loads on the first
:meth:`LocalEmbeddingProvider.embed` call and is held for the life of the process. The
embed job and the rebuild run in the worker, but ``DenseStrategy.search()`` embeds the
*query* in whichever process served the request, so a deployment serving dense recall
holds the model in its web and MCP process as well as its worker, and the first dense
query after a restart pays the load (tens of megabytes, of the order of a second).

**An optional extra.** ``fastembed`` is imported inside the load path and nowhere else,
so importing this file never needs the ``local-embeddings`` extra; the registry
registers this provider only when the extra is importable.

**One ``embed()`` for memories and queries alike, and never ``query_embed()``.** On this
model fastembed's query path adds no prefix and returns the identical vector, so a
second path would be a second place the space could drift. Nor is any text redacted or
rewritten on the way in: the input is exactly what the caller passed — the memory's
``embed_input(title, body)``, or the bare query. Recallatron tiers no memory field, and
there is no redaction helper in this module to route text through.
"""

import math
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from rheo_core.modules import model_cache_dir

from rheo_recallatron.configuration import EMBEDDING_DIMENSIONS

if TYPE_CHECKING:
    from fastembed import TextEmbedding

MODEL_ID: Final = "sentence-transformers/all-MiniLM-L6-v2"
"""The model, as fastembed 0.8.1's ``TextEmbedding.list_supported_models()`` names it:
dimension 384, sourced from ``qdrant/all-MiniLM-L6-v2-onnx``. Confirmed against the
installed package, not copied from a document.

``.github/workflows/repository-checks.yml`` caches the test model directory under a key
that spells this identifier out. Change the model and change that key with it, or CI
restores the old artifact for the new name."""

MODEL_CACHE_COMPONENT: Final = "fastembed"
"""The segment under ``<data_root>/models/`` this provider caches its artifact in."""

UNIT_NORM_TOLERANCE: Final = 1e-3
"""How far a returned vector's Euclidean norm may sit from 1.0.

The relevance floor is a cosine bound, and reading it as ``1 - distance`` is only valid
over unit vectors. The model normalises today, to within float32 rounding (about 1e-7);
this tolerance is loose enough never to trip on rounding and tight enough that a library
default changing under a version bump fails loudly instead of silently moving the floor.
"""


class EmbeddingNotNormalised(RuntimeError):
    """A returned vector was not unit length. A provider failure, not a warning."""


def require_unit_norm(vectors: Sequence[Sequence[float]]) -> None:
    """Raise :class:`EmbeddingNotNormalised` unless every vector is unit length.

    A real ``raise`` rather than an ``assert``: ``python -O`` strips assertions, and
    this check is the only thing between a changed library default and a silently
    invalid relevance floor.

    **A NaN or an infinity is refused too.** Every comparison with NaN is false, so a
    check written as "raise when too far from 1" lets a NaN norm through. This one
    requires a finite norm and then asks the positive question — within tolerance —
    and raises when that is not true, which a NaN cannot satisfy.
    """
    for position, vector in enumerate(vectors):
        norm = math.sqrt(math.fsum(value * value for value in vector))
        if not math.isfinite(norm) or not abs(norm - 1.0) <= UNIT_NORM_TOLERANCE:
            raise EmbeddingNotNormalised(
                f"{MODEL_ID} returned a vector of norm {norm:.6f} at batch position "
                f"{position}; the dense relevance floor holds only over unit vectors"
            )


class LocalEmbeddingProvider:
    """fastembed's MiniLM-L6-v2, 384 dimensions, ONNX on the CPU, in this process."""

    def __init__(self) -> None:
        self._model: TextEmbedding | None = None
        self._loading = threading.Lock()

    @property
    def model_id(self) -> str:
        return MODEL_ID

    @property
    def dimensions(self) -> int:
        return EMBEDDING_DIMENSIONS

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """One unit vector per text, as plain Python floats.

        Raises :class:`EmbeddingNotNormalised` for a batch that is not unit length, and
        lets anything the runtime raises propagate: the embed job's retry and the dense
        read's degraded path are where those are handled.
        """
        if not texts:
            return []
        model = self._load()
        vectors = [
            tuple(float(value) for value in array) for array in model.embed(list(texts))
        ]
        require_unit_norm(vectors)
        return vectors

    def _load(self) -> "TextEmbedding":
        """The model, loaded once under a lock and held for the process's life.

        The artifact goes where core says model artifacts go — never a library default
        under a home directory the deployment does not own, and never a path this
        module composes itself.
        """
        with self._loading:
            if self._model is None:
                from fastembed import TextEmbedding

                cache = model_cache_dir(MODEL_CACHE_COMPONENT)
                cache.mkdir(parents=True, exist_ok=True)
                self._model = TextEmbedding(model_name=MODEL_ID, cache_dir=str(cache))
            return self._model
