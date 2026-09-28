"""Bounded, on-box query/document relevance, after full eligibility only.

This does not retrieve, widen permissions, change stored vectors or remove hits by a
new floor. The caller supplies only admitted text. Scores are raw cross-encoder
logits, not probabilities or calibrated answer confidence. A failed model preserves
the original ordering and is explicitly reported; exception messages can contain
input text, so logs name only their class.
"""

import logging
import math
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from rheo_core.modules import model_cache_dir

if TYPE_CHECKING:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

MODEL_ID: Final = "Xenova/ms-marco-MiniLM-L-6-v2"
MODEL_REVISION: Final = "a09144355adeed5f58c8ed011d209bf8ee5a1fec"
"""Immutable public Apache-2.0 artifact; update the CI cache key with either pin."""
WINDOW_MULTIPLIER: Final = 3
"""At most three times the requested k (150 at k=50), never all 500 candidates."""

_log = logging.getLogger(__name__)


class LocalReranker:
    def __init__(self) -> None:
        self._model: TextCrossEncoder | None = None
        self._lock = threading.Lock()

    def _load(self) -> "TextCrossEncoder":
        with self._lock:
            if self._model is None:
                from fastembed.rerank.cross_encoder import TextCrossEncoder
                from huggingface_hub import snapshot_download

                cache = model_cache_dir("fastembed")
                cache.mkdir(parents=True, exist_ok=True)
                # Do not resolve a moving main branch at serving time. Only public
                # artifacts are downloaded; query/document inference stays local.
                artifact = snapshot_download(
                    repo_id=MODEL_ID,
                    revision=MODEL_REVISION,
                    cache_dir=cache,
                    allow_patterns=["*.json", "*.txt", "onnx/model.onnx"],
                )
                self._model = TextCrossEncoder(
                    model_name=MODEL_ID,
                    specific_model_path=artifact,
                    providers=["CPUExecutionProvider"],
                    threads=2,
                )
            return self._model

    def scores(self, query: str, documents: Sequence[str]) -> tuple[float, ...]:
        if not documents:
            return ()
        scores = tuple(self._load().rerank(query, documents, batch_size=16))
        if len(scores) != len(documents) or not all(map(math.isfinite, scores)):
            raise ValueError("reranker returned malformed scores")
        return scores


LOCAL_RERANKER: Final = LocalReranker()


def relevance_scores(query: str, documents: Sequence[str]) -> tuple[float, ...] | None:
    """None means degraded: keep the unmodified first-stage rank and score."""
    try:
        return LOCAL_RERANKER.scores(query, documents)
    except Exception as exc:
        _log.warning("local relevance ranking unavailable (%s)", type(exc).__name__)
        return None
