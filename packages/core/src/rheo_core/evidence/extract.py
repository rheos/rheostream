"""Digestion and structured-output validation: what the model sees, and what of its
answer is believed (spec Architecture, System Components item 7; FR 6, AC 3).

**Digestion is deterministic and carries nothing but text.** :func:`digest` turns the
claimed units' bodies, in claim order, into one :class:`DigestBatch` of
:class:`DigestItem` values whose ids are per-batch ordinals, ``"u1"`` to ``"uN"``. It
takes the bodies alone, so a unit's ``native_key``, speaker, audience and purpose
cannot reach a model through it: the model is never told who said a thing, who may
read it, or what it may be used for, and so cannot be steered into choosing any of
them. The attribute names ``item_id`` and ``text`` are read by
:class:`~rheo_core.evidence.providers.FakeExtractionProvider`; renaming them breaks the
fake at run time, not at type-check time.

**The provider is not trusted to have followed the schema.** :func:`validate_extraction`
checks the raw response client-side and gives every batch item exactly one
:class:`~rheo_contracts.source_units.SanitizedEvidence`:

- A response that is not shaped ``{"items": [...]}`` raises
  :class:`ExtractionOutputInvalid`. The caller handles that exactly as it handles a
  provider raise, a transport-shaped fault with per-row backoff, never as a batch-wide
  ``noop``: a response that cannot be read says nothing about any one item.
- Per item, everything short of one well-formed memory for a known id is "no
  candidate": an entry whose id the batch never issued is ignored; an id answered twice
  is ambiguous; ``memory: null``; a memory that fails the evidence fields' own checks
  (a kind outside the four, a blank or oversize title, a blank body, a confidence
  outside ``0..1``) or carries any field beyond ``kind``, ``title``, ``body``,
  ``confidence``, ``occurred_at`` and ``mentions``; and an id the response leaves out.
  The extra-field refusal is what keeps a model from proposing ``purposes`` or
  ``links``: the evidence value accepts both, for the migration producer, so they are
  refused here, before one is built.
- "No candidate" is :data:`NOOP_EVIDENCE`. It passes the evidence value's own
  ``min_length`` checks and fails 1a1's explicit-evidence gate (a blank title and
  body), so the unchanged seam writes a terminal ``noop`` receipt for it. No new seam
  vocabulary is needed for "the model found nothing".

The provider call itself, and what a fault does to the claimed rows, belong to the
eligible-evidence service; this module never imports the provider registry.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError
from rheo_contracts.source_units import MemoryKind, SanitizedEvidence, SourceMention

ITEM_ID_PREFIX: Final = "u"

NOOP_EVIDENCE: Final = SanitizedEvidence(kind="note", title=" ", body=" ")
"""The "no candidate" sentinel: valid as a value, refused as explicit evidence."""


class ExtractionOutputInvalid(Exception):
    """The provider's response as a whole is not ``{"items": [...]}``.

    Carries no part of the response: model output is unvalidated text and does not
    belong in a log line or an audit row.
    """


@dataclass(frozen=True, slots=True)
class DigestItem:
    """One unit as the model sees it: a per-batch ordinal and the sanitized body."""

    item_id: str
    text: str


@dataclass(frozen=True, slots=True)
class DigestBatch:
    """One provider call's input, in claim order."""

    items: tuple[DigestItem, ...]


def digest(bodies: Sequence[str]) -> DigestBatch:
    """The claimed units' bodies, in claim order, as one batch of ordinal items."""
    return DigestBatch(
        items=tuple(
            DigestItem(item_id=f"{ITEM_ID_PREFIX}{ordinal}", text=body)
            for ordinal, body in enumerate(bodies, start=1)
        )
    )


class _ExtractionResponse(BaseModel):
    """The whole response's shape. Entries stay unvalidated here, so one bad entry
    costs its own item a candidate rather than failing every item in the batch."""

    model_config = ConfigDict(extra="forbid", strict=True)

    items: list[object]


class _ProposedMemory(BaseModel):
    """The fields a model may propose, and no others.

    ``SanitizedEvidence`` then applies its own bounds (title length, confidence range);
    this model exists for the closed field set, which that value is wider than.
    """

    model_config = ConfigDict(extra="forbid")

    kind: MemoryKind
    title: str
    body: str
    confidence: float | None = None
    occurred_at: datetime | None = None
    mentions: tuple[SourceMention, ...] = ()


def _candidate(memory: object) -> SanitizedEvidence | None:
    """One entry's memory as evidence, or ``None`` for every way it is not one."""
    if memory is None:
        return None
    try:
        proposed = _ProposedMemory.model_validate(memory)
        evidence = SanitizedEvidence.model_validate(proposed.model_dump())
    except ValidationError:
        return None
    if not evidence.title.strip() or not evidence.body.strip():
        return None
    return evidence


def validate_extraction(
    batch: DigestBatch, response: object
) -> dict[str, SanitizedEvidence]:
    """Every batch item id mapped to its one candidate, or to :data:`NOOP_EVIDENCE`.

    Raises :class:`ExtractionOutputInvalid` when the response as a whole is not
    ``{"items": [...]}``.
    """
    try:
        parsed = _ExtractionResponse.model_validate(response)
    except ValidationError:
        raise ExtractionOutputInvalid(
            "extraction response is not an object holding an items list"
        ) from None

    issued = {item.item_id for item in batch.items}
    answers: dict[str, list[Mapping[object, object]]] = {}
    for entry in parsed.items:
        if not isinstance(entry, Mapping):
            continue
        item_id = entry.get("item")
        if not isinstance(item_id, str) or item_id not in issued:
            continue
        answers.setdefault(item_id, []).append(entry)

    result: dict[str, SanitizedEvidence] = {}
    for item in batch.items:
        entries = answers.get(item.item_id, [])
        candidate: SanitizedEvidence | None = None
        if len(entries) == 1 and set(entries[0]) <= {"item", "memory"}:
            candidate = _candidate(entries[0].get("memory"))
        result[item.item_id] = candidate if candidate is not None else NOOP_EVIDENCE
    return result
