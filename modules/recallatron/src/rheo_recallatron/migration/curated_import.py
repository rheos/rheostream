"""Transactional import of reviewed predecessor units through the source seam.

This is a library boundary, not a CLI or a cutover switch. Its caller must verify
the source snapshot, select reviewed units, provide a stable authority id, and
commit only after the complete batch and its private verification pass.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from rheo_contracts import ContextPurpose, Role, WorkspaceContext
from rheo_contracts.source_units import (
    AuthorityGrant,
    AuthorityRefused,
    SanitizedEvidence,
    SourceMention,
    TrustedSourceUnit,
)
from rheo_core.events import ConsumerRegistry
from rheo_core.refs.resolver import UnitOfWork

from rheo_recallatron.migration.extract import Extract, ExtractUnit
from rheo_recallatron.refusals import SOURCE_UNAVAILABLE
from rheo_recallatron.source_units import (
    STATE_ACTIVE,
    accept_source_unit,
)

CURATED_IMPORT_REFUSED: Final = "curated_import_refused"


class CuratedImportRefusal(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(f"{CURATED_IMPORT_REFUSED}: {reason}")


def _trusted(
    source: ExtractUnit, authority_id: UUID, owner_id: UUID
) -> TrustedSourceUnit:
    return TrustedSourceUnit(
        producer_kind="migration",
        authority_id=authority_id,
        principal_account_id=owner_id,
        audience_kind="member",
        audience_id=owner_id,
        bound_purpose=None,
        # The predecessor has a recorded instant but no source expiry window.
        # The original instant is carried as evidence and becomes memory.recorded_at.
        source_recorded_at=None,
        source_expires_at=None,
        external_source_key=source.external_source_key,
        evidence=SanitizedEvidence(
            kind=source.kind,
            title=source.title,
            body=source.body,
            confidence=source.confidence,
            occurred_at=source.recorded_at,
            purposes=(ContextPurpose.INTERNAL_ANALYSIS,),
            mentions=tuple(
                SourceMention(
                    kind=mention.kind,
                    name=mention.name,
                    role=mention.role,
                )
                for mention in source.mentions
            ),
        ),
    )


@dataclass(frozen=True)
class ExtractAuthority:
    workspace_id: UUID
    authority_id: UUID
    owner_id: UUID
    digests: dict[str, bytes]

    def verify(
        self, unit: TrustedSourceUnit, *, workspace_id: UUID, now: datetime
    ) -> AuthorityGrant | AuthorityRefused:
        if (
            workspace_id != self.workspace_id
            or unit.producer_kind != "migration"
            or unit.authority_id != self.authority_id
            or unit.principal_account_id != self.owner_id
            or unit.audience_kind != "member"
            or unit.audience_id != self.owner_id
            or unit.bound_purpose is not None
            or self.digests.get(unit.external_source_key) != unit.payload_digest()
        ):
            return AuthorityRefused(reason="source_unverified")
        return AuthorityGrant(
            workspace_id=self.workspace_id,
            producer_kind="migration",
            authority_id=self.authority_id,
            principal_account_id=self.owner_id,
            audience_kind="member",
            audience_id=self.owner_id,
            bound_purpose=None,
            source_revision=1,
            required_links=(),
        )


@dataclass(frozen=True)
class CuratedImportReport:
    source_units: int
    active: int
    unavailable: int


def import_curated(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    extract: Extract,
    *,
    authority_id: UUID,
    owner_id: UUID,
    consumers: ConsumerRegistry,
    imported_at: datetime,
) -> CuratedImportReport:
    if ctx.role is not Role.OWNER or ctx.principal.account_id != owner_id:
        raise CuratedImportRefusal("owner context required")
    if imported_at.tzinfo is None:
        raise CuratedImportRefusal("imported_at must be timezone-aware")
    units = tuple(_trusted(item, authority_id, owner_id) for item in extract.units)
    if len({unit.external_source_key for unit in units}) != len(units):
        raise CuratedImportRefusal("duplicate source key")
    authority = ExtractAuthority(
        ctx.workspace_id,
        authority_id,
        owner_id,
        {unit.external_source_key: unit.payload_digest() for unit in units},
    )
    active = unavailable = 0
    for unit in units:
        outcome = accept_source_unit(
            ctx, uow, unit, authority=authority, consumers=consumers, now=imported_at
        )
        if outcome.state == STATE_ACTIVE:
            active += 1
        elif outcome.state == SOURCE_UNAVAILABLE:
            # A prior erasure or changed replay must not resurrect the row. The
            # caller's verification decides whether this is an expected omission.
            unavailable += 1
        else:
            raise CuratedImportRefusal(f"source unit refused: {outcome.state}")
    return CuratedImportReport(len(units), active, unavailable)
