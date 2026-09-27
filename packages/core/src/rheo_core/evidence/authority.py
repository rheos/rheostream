"""The ``SourceAuthority`` for ``producer_kind = "rheo_runtime"`` (spec § System
Components, item 4).

1a1's seam asks one question of a unit, twice: may this become a memory in this
workspace, right now? For a runtime turn the answer lives in core's own
``core.evidence_unit`` row, which the recorder wrote from the verified run's context.
So :class:`RuntimeEvidenceAuthority` answers from that row and from the speaker's live
membership, and never from anything the unit or a model says about itself.

**Bound to the caller's unit of work.** ``SourceAuthority.verify(unit, *,
workspace_id, now)`` takes no unit of work, and the seam calls it with exactly those
arguments. The authority therefore holds the unit of work it was built with, privately,
and reads the evidence row ``FOR SHARE`` on that connection: the caller's own
transaction, so a row the drain is about to settle cannot move under the check. Only
core's claim path constructs one, with the unit of work the drain job already holds,
so it grants the caller nothing it did not already have. That private handle is the
one named exception to AC-11(b)'s "no raw handle" rule; ``verify`` is the only public
attribute.

**Pending only, deliberately.** A ``settled`` or ``gap`` row refuses
``source_unavailable``, exactly as a missing row does. Replaying an already-settled
unit through this authority is unreachable by design, and must stay so.

**Content-free refusals.** ``AuthorityRefused.reason`` is one of the four words below
and nothing else: no key, no account id, no text.
"""

from datetime import datetime
from typing import Final
from uuid import UUID

from rheo_contracts import ContextPurpose
from rheo_contracts.source_units import (
    AuthorityGrant,
    AuthorityRefused,
    TrustedSourceUnit,
)
from sqlalchemy import select

from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import get_membership
from rheo_core.storage.evidence_tables import STATE_PENDING, evidence_unit
from rheo_core.storage.postgres import get_backend

RUNTIME_PRODUCER_KIND: Final = "rheo_runtime"
"""The one producer kind this authority answers for."""

PRODUCER_MISMATCH: Final = "producer_mismatch"
"""The unit claims a producer kind other than ``rheo_runtime``."""
SOURCE_UNAVAILABLE: Final = "source_unavailable"
"""No evidence row for the unit's identity, or the row is no longer ``pending``."""
SPEAKER_MISMATCH: Final = "speaker_mismatch"
"""The unit's principal is not the account the row recorded as the speaker."""
MEMBERSHIP_REVOKED: Final = "membership_revoked"
"""The speaker no longer holds a ``control.membership`` row for the workspace."""

REFUSAL_REASONS: Final = frozenset(
    {PRODUCER_MISMATCH, SOURCE_UNAVAILABLE, SPEAKER_MISMATCH, MEMBERSHIP_REVOKED}
)
"""The closed vocabulary of this authority's refusals."""


class RuntimeEvidenceAuthority:
    """Verifies a runtime turn against its own ``core.evidence_unit`` row.

    Construct it with the unit of work whose transaction the seam's acceptance runs
    in; see this module's docstring for why it holds one.
    """

    __slots__ = ("_uow",)

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def verify(
        self, unit: TrustedSourceUnit, *, workspace_id: UUID, now: datetime
    ) -> AuthorityGrant | AuthorityRefused:
        """Grant ``unit`` for ``workspace_id`` exactly as its evidence row stands.

        Checks, in order: the producer kind; the row for ``(producer_kind,
        authority_id, external_source_key)`` exists and is ``pending`` (read ``FOR
        SHARE`` in the caller's transaction); its speaker is the unit's principal;
        and that account is still a member of ``workspace_id``. The grant's
        principal, audience and purpose are the row's, never the unit's. ``now`` is
        part of the protocol; the row's own expiry is the drain's to enforce.
        """
        if unit.producer_kind != RUNTIME_PRODUCER_KIND:
            return AuthorityRefused(reason=PRODUCER_MISMATCH)
        row = self._uow.connection.execute(
            select(
                evidence_unit.c.authority_id,
                evidence_unit.c.speaker_account_id,
                evidence_unit.c.audience_kind,
                evidence_unit.c.audience_id,
                evidence_unit.c.purpose,
                evidence_unit.c.state,
            )
            .where(
                evidence_unit.c.producer_kind == unit.producer_kind,
                evidence_unit.c.authority_id == unit.authority_id,
                evidence_unit.c.native_key == unit.external_source_key,
            )
            .with_for_update(read=True)
        ).one_or_none()
        if row is None or row.state != STATE_PENDING:
            return AuthorityRefused(reason=SOURCE_UNAVAILABLE)
        if row.speaker_account_id != unit.principal_account_id:
            return AuthorityRefused(reason=SPEAKER_MISMATCH)
        # The control plane is its own database, read on its own connection exactly
        # as ``context_from_operation`` reads it: the same window, the same accepted
        # risk, nothing new.
        with get_backend().control_engine.connect() as connection:
            membership = get_membership(
                connection,
                account_id=row.speaker_account_id,
                workspace_id=workspace_id,
            )
        if membership is None:
            return AuthorityRefused(reason=MEMBERSHIP_REVOKED)
        return AuthorityGrant(
            workspace_id=workspace_id,
            producer_kind=RUNTIME_PRODUCER_KIND,
            authority_id=row.authority_id,
            principal_account_id=row.speaker_account_id,
            audience_kind=row.audience_kind,
            audience_id=row.audience_id,
            bound_purpose=ContextPurpose(row.purpose),
            source_revision=1,
            required_links=(),
        )
