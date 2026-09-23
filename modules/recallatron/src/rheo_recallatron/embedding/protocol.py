"""The ``EmbeddingProvider`` protocol: text in, one unit vector per text out."""

from collections.abc import Sequence
from typing import Protocol


class EmbeddingProvider(Protocol):
    """Something that turns text into vectors in one fixed space.

    ``model_id`` names that space. It is what ``memory_embedding.model_id`` stores, and
    it is the one authority for which stored rows are current: a row whose model id is
    not the resolved provider's is from another space and is pruned, never compared.

    ``dimensions`` is the width every returned vector has. The registry refuses a
    provider whose width is not the column's.
    """

    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """One vector per text, in order. Memories and queries go through this same
        call: there is no separate query path."""
        ...
