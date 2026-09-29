"""The ``SourceAuthority`` for ``producer_kind = "claude_code_local"`` (spec
§ Architecture → System Components, item 1; FR 4).

A local Claude Code turn reaches core through an enrolled bridge, so its evidence row's
``authority_id`` is the id of the ``core.evidence_enrollment`` row the bridge ingested
under. :class:`LocalEvidenceAuthority` answers from that evidence row, from that
enrollment, and from the speaker's live membership, never from anything the unit or a
model says about itself.

It is built exactly as :class:`~rheo_core.evidence.authority.RuntimeEvidenceAuthority`
is: bound to the caller's unit of work (the same named exception to AC-11(b)'s "no raw
handle" rule), pending rows only, and ``verify`` its one public attribute. The evidence
row read and the membership read are that authority's own helpers, shared.

**Content-free refusals.** ``AuthorityRefused.reason`` is one of the six words below and
nothing else: no account id, no native key, no machine identifier, no text (AC 3).
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

from rheo_core.evidence.authority import _is_member, _pending_row
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.evidence_enrollment_tables import (
    STATE_ACTIVE,
    evidence_enrollment,
)

LOCAL_PRODUCER_KIND: Final = "claude_code_local"
"""The one producer kind this authority answers for."""

_MEMBER_AUDIENCE: Final = "member"
"""The one audience ceiling an enrollment admits, fixed by construction."""

PRODUCER_MISMATCH: Final = "producer_mismatch"
"""The unit claims a producer kind other than ``claude_code_local``."""
SOURCE_UNAVAILABLE: Final = "source_unavailable"
"""No evidence row for the unit's identity, or the row is no longer ``pending``."""
SPEAKER_MISMATCH: Final = "speaker_mismatch"
"""The unit's principal is not the account the row recorded as the speaker."""
ENROLLMENT_INACTIVE: Final = "enrollment_inactive"
"""No enrollment row for the row's ``authority_id``, or it is no longer ``active``."""
ENROLLMENT_MISMATCH: Final = "enrollment_mismatch"
"""The enrollment's account or purpose is not the row's, or the row's audience is not
the enrolled account's own ``member`` ceiling."""
MEMBERSHIP_REVOKED: Final = "membership_revoked"
"""The speaker no longer holds a ``control.membership`` row for the workspace."""

REFUSAL_REASONS: Final = frozenset(
    {
        PRODUCER_MISMATCH,
        SOURCE_UNAVAILABLE,
        SPEAKER_MISMATCH,
        ENROLLMENT_INACTIVE,
        ENROLLMENT_MISMATCH,
        MEMBERSHIP_REVOKED,
    }
)
"""The closed vocabulary of this authority's refusals."""


class LocalEvidenceAuthority:
    """Verifies a local Claude Code turn against its evidence row and enrollment.

    Construct it with the unit of work whose transaction the seam's acceptance runs
    in, as :class:`~rheo_core.evidence.authority.RuntimeEvidenceAuthority` is.
    """

    __slots__ = ("_uow",)

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def verify(
        self, unit: TrustedSourceUnit, *, workspace_id: UUID, now: datetime
    ) -> AuthorityGrant | AuthorityRefused:
        """Grant ``unit`` for ``workspace_id`` exactly as its row and enrollment stand.

        Checks, in order, each an early exit: the producer kind; the row for
        ``(producer_kind, authority_id, external_source_key)`` exists and is
        ``pending`` (read ``FOR SHARE`` in the caller's transaction); its speaker is
        the unit's principal; the enrollment ``id = row.authority_id`` exists and is
        ``active`` (read ``FOR SHARE``, same connection); the enrollment's account and
        purpose are the row's and the row's audience is ``member`` for that account;
        and that account is still a member of ``workspace_id``. The grant's
        principal, audience and purpose are the row's, never the unit's. ``now`` is
        part of the protocol; the row's own expiry is the drain's to enforce.

        **The machine check.** This authority never compares a machine fingerprint:
        nothing on the unit carries one. The machine is bound transitively, since
        there is one active enrollment per machine and project and the row's
        ``authority_id`` is that enrollment's id. The fingerprint comparison happens
        at ingest, where a presented fingerprint exists. Revoking the enrollment is
        what makes every still-pending row from it settle ``authority_unverified`` at
        its next claim.
        """
        if unit.producer_kind != LOCAL_PRODUCER_KIND:
            return AuthorityRefused(reason=PRODUCER_MISMATCH)
        row = _pending_row(self._uow, unit)
        if row is None:
            return AuthorityRefused(reason=SOURCE_UNAVAILABLE)
        if row.speaker_account_id != unit.principal_account_id:
            return AuthorityRefused(reason=SPEAKER_MISMATCH)
        enrollment = self._uow.connection.execute(
            select(
                evidence_enrollment.c.account_id,
                evidence_enrollment.c.purpose,
                evidence_enrollment.c.state,
            )
            .where(evidence_enrollment.c.id == row.authority_id)
            .with_for_update(read=True)
        ).one_or_none()
        if enrollment is None or enrollment.state != STATE_ACTIVE:
            return AuthorityRefused(reason=ENROLLMENT_INACTIVE)
        if (
            enrollment.account_id != row.speaker_account_id
            or enrollment.purpose != row.purpose
            or row.audience_kind != _MEMBER_AUDIENCE
            or row.audience_id != enrollment.account_id
        ):
            return AuthorityRefused(reason=ENROLLMENT_MISMATCH)
        if not _is_member(row.speaker_account_id, workspace_id):
            return AuthorityRefused(reason=MEMBERSHIP_REVOKED)
        return AuthorityGrant(
            workspace_id=workspace_id,
            producer_kind=LOCAL_PRODUCER_KIND,
            authority_id=row.authority_id,
            principal_account_id=row.speaker_account_id,
            audience_kind=row.audience_kind,
            audience_id=row.audience_id,
            bound_purpose=ContextPurpose(row.purpose),
            source_revision=1,
            required_links=(),
        )
