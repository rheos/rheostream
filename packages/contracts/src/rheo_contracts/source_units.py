"""The trusted source-unit value and the authority protocol, at the core/module
boundary (Architecture § A5's "smallest source seam").

**Declared shape only.** There is no registry here, no endpoint, no operation and no
producer implementation — the two real producer adapters are the later automatic-memory
carrier's and the migration run's. What lives here is the pair of names both sides of
the boundary have to be able to *say*: ``rheo_core`` and ``rheo_recallatron`` can each
import this module without either depending on the other's concrete adapters.

**A unit is one immutable attributable evidence unit with at most one destination
representation.** Its boundary is fixed by the source's own structure before any model
runs — one native message, or one importer row — never by a downstream count of facts a
model finds inside it. A message carrying two or more *separable* explicit facts is
therefore one unit and yields at most one representation. That ceiling is ratified into
the receipt's composite primary key, so it is not revisable without a migration: see
:class:`TrustedSourceUnit`'s own note.

**What a unit deliberately does not carry.** No source body beyond the sanitized
evidence, no label, filename, transcript or session id, no model prompt or output, no
raw payload, no prior value, and no ``source_namespace``. The namespace is *minted* at
the boundary from the verified workspace, producer authority, original principal,
audience ceiling and purpose partition — a producer that could choose it could write
into another producer's partition.
"""

import json
from datetime import datetime
from hashlib import sha256
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from rheo_contracts.purposes import ContextPurpose

ProducerKind = Literal["migration", "rheo_runtime", "claude_code_local"]
"""§ A3's closed ``source_receipt.producer_kind`` vocabulary.

Spelled as a ``Literal`` rather than imported from the owning module's DDL, for the
reason this package cannot import that module at all. The database's own
``CHECK`` constraint is the enforcer; this is the shape a caller is typed against.
"""

SourceAudienceKind = Literal["workspace", "member"]
"""The audience ceiling a unit was authorized under. ``audience_id`` is null exactly
for ``workspace``, the same pairing the receipt and the memory row both check."""

MemoryKind = Literal["note", "fact", "decision", "summary"]
"""The four ratified memory kinds. ``topic_thread`` and ``procedural_notes`` are not
members, here or anywhere: both remain deferred pending a substrate neither has."""

LinkRelation = Literal["derived_from", "about"]

SOURCE_KEY_MAX_LENGTH = 256
"""§ A13's bound on an adapter-owned opaque key within its minted partition."""

SOURCE_NAMESPACE_MAX_LENGTH = 128
"""§ A13's bound on the minted namespace. Declared beside the key's bound so the two
halves of the receipt's identity are read together; the minting itself belongs to the
module that knows the routed workspace."""


class _Frozen(BaseModel):
    """Frozen, extra forbidden. Every value below is one."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceLink(_Frozen):
    """One actual canonical reference the evidence carries, and its relation.

    A reference is a value, never a join: the row it names may live in another
    module's schema. The seam copies these onto the created memory as its permission-
    bearing links, so each one is re-authorized on every later read.
    """

    ref: str = Field(min_length=1)
    relation: LinkRelation


class SourceMention(_Frozen):
    """One entity the evidence mentions, by kind and name.

    Always a *creation*: a trusted producer cannot select an existing entity by id,
    because selecting one would be a probe across an audience boundary it never
    proved it holds. ``backing_ref`` is an independently readable canonical
    reference, and it is what makes the mention checkable rather than a name.
    """

    kind: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    backing_ref: str | None = None
    role: str | None = Field(default=None, min_length=1, max_length=100)


class SanitizedEvidence(_Frozen):
    """The sanitized evidence one unit presents, and the whole of what may become a
    memory's content.

    ``purposes`` is populated only by the migration producer, which "preserves each
    imported memory's ratified purposes" (§ A3). The two automatic producer kinds
    leave it empty: their acceptance binds exactly ``internal_analysis`` and a unit
    presenting any other or additional purpose is refused before a write.
    """

    kind: MemoryKind
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    occurred_at: datetime | None = None
    purposes: tuple[ContextPurpose, ...] = ()
    links: tuple[SourceLink, ...] = ()
    mentions: tuple[SourceMention, ...] = ()


class TrustedSourceUnit(_Frozen):
    """One immutable attributable evidence unit, with its authority context.

    **At most one destination representation, and the grain is fixed here.** The
    receipt's composite primary key is ``(representation_type, source_namespace,
    external_source_key)``, so the unit identity a producer chooses *is* the
    idempotency key. A producer wanting finer grain must treat each separately
    identifiable unit already present in the raw source structure — a distinct native
    message, a distinct importer row — as its own unit; it may never manufacture a
    within-message split, because the boundary between facts would then be identified
    by the model whose output the receipt exists to make replay-safe.

    The cost of that ceiling is charged here rather than deferred: every fact drawn
    from one message shares that message's one kind, audience, purpose set and
    confidence, and a later correction acts on the whole combined memory.

    ``external_source_key`` is adapter-owned **within the minted partition** and is
    the only identity field a producer supplies. Identity must come from stable native
    message/span or importer-row identity — never from output text, candidate order,
    or a content hash alone, because a re-chunk, sanitizer upgrade, retry or
    re-enrollment must not rename a unit that was already observed.
    """

    producer_kind: ProducerKind
    authority_id: UUID
    principal_account_id: UUID | None
    audience_kind: SourceAudienceKind
    audience_id: UUID | None
    bound_purpose: ContextPurpose | None
    source_recorded_at: datetime | None
    source_expires_at: datetime | None
    external_source_key: str = Field(min_length=1, max_length=SOURCE_KEY_MAX_LENGTH)
    evidence: SanitizedEvidence

    def payload_digest(self) -> bytes:
        """The 32-byte SHA-256 § A3 stores on the receipt.

        Over the canonical sanitized evidence, the authority context and the source
        instants together — the envelope, not the evidence alone, so a replay that
        kept the text and moved the audience ceiling is a *changed* digest and is
        suppressed rather than accepted. Canonical means sorted keys and no
        whitespace, so two encodings of one unit cannot answer two digests.

        It is computed from the unit and never recomputed from a corrected memory's
        text: correction preserves identity, origin and this digest.
        """
        canonical = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return sha256(canonical.encode("utf-8")).digest()


class AuthorityGrant(_Frozen):
    """What a :class:`SourceAuthority` verified, as values the seam then enforces.

    A bare boolean would make the protocol unfalsifiable: the seam would have nothing
    to compare the unit against, and an adapter that verified nothing would be
    indistinguishable from one that verified everything. So the authority returns the
    facts it checked — the routed workspace, the producer identity, the original
    principal, the immutable audience ceiling, the bound purpose, the source's own
    revision, and the canonical links the unit is *required* to carry — and the seam
    refuses a unit that disagrees with any of them.
    """

    workspace_id: UUID
    producer_kind: ProducerKind
    authority_id: UUID
    principal_account_id: UUID | None
    audience_kind: SourceAudienceKind
    audience_id: UUID | None
    bound_purpose: ContextPurpose | None
    source_revision: int = Field(ge=1)
    required_links: tuple[SourceLink, ...] = ()


class AuthorityRefused(_Frozen):
    """Unverifiable authority, content-free.

    ``reason`` is a fixed vocabulary word for an operator's log, never the evidence,
    the source's identity or which check failed for this caller. An unverifiable
    authority writes no receipt at all, so it cannot poison another partition.
    """

    reason: str = Field(min_length=1)


class SourceAuthority(Protocol):
    """Whether this unit may become a memory in this workspace, right now.

    One method, because the checks are one decision taken under one lifecycle lock:
    the original source's availability and revision, current enrollment, membership
    and delegation, the same routed workspace, the audience ceiling, the purpose, the
    source's own retention, and the actual canonical links the unit must carry. An
    adapter that cannot answer every one of them refuses.

    **Called before a receipt is looked up, and again under the lifecycle lock
    immediately before creation** — the same authority, twice, because enrollment and
    membership can be revoked between a model's input and its output.

    Implementations are the later carrier's, the migration run's, and this run's two
    synthetic test adapters. Nothing in production implements it in 1a1.
    """

    def verify(
        self, unit: TrustedSourceUnit, *, workspace_id: UUID, now: datetime
    ) -> AuthorityGrant | AuthorityRefused:
        """Verify ``unit`` for ``workspace_id`` at ``now``."""
        ...
