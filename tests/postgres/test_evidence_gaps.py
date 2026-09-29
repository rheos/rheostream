"""``record_gaps``: content-free gap rows for enrolled sources (spec § System Components
item 3, § Data Models "Outcome words").

Seams under test:

- **Content-free write.** Each gap is one ``gap`` row with ``body`` null, the passed
  reason as ``outcome``, the passed identity and audience, and ``settled_at == now``.
- **Idempotency.** A native key already held is returned again and never re-inserted.
- **Context binding.** A batch with one gap whose speaker or purpose is not the
  context's is refused whole, before any write, so its valid first gap is absent too.
- **No event, retention-eligible.** No ``outbox_event`` row is written, and the
  retention sweep's purge deletes a gap row on ``settled_at`` like any settled row.

Every identifier is generated; no text is ever passed.
"""

import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final, get_args
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.evidence import EvidenceWorkspace, handler_uow, probe_registry
from rheo_contracts import ContextPurpose, WorkspaceContext
from rheo_core.evidence.record import GapReason, NewGap, record_gaps
from rheo_core.evidence.retention import purge_evidence
from rheo_core.operations import register_core_operations
from rheo_core.refs import uuid7
from rheo_core.storage import work_tables
from rheo_core.storage.evidence_tables import (
    OUTCOME_EXPIRED_PENDING,
    OUTCOME_SOURCE_TRUNCATED,
    STATE_GAP,
    evidence_unit,
)
from sqlalchemy import Row, delete, func, select

pytestmark = pytest.mark.postgres

_PURPOSE: Final = ContextPurpose.INTERNAL_ANALYSIS
_AT: Final = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()


@pytest.fixture
def ev(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> Iterator[EvidenceWorkspace]:
    """The workspace, and teardown of every evidence row this file wrote (#238's
    rule: no settled gap outlives the test for ``rheo doctor`` to read)."""
    evidence = EvidenceWorkspace(cluster, workspace, owner_account_id)
    yield evidence
    with evidence.unit_of_work() as uow:
        uow.connection.execute(delete(evidence_unit))
        uow.commit()


def _gap(
    ctx: WorkspaceContext,
    *,
    authority_id: UUID,
    reason: GapReason = "source_truncated",
    speaker_account_id: UUID | None = None,
    purpose: ContextPurpose | None = None,
) -> NewGap:
    account_id = ctx.principal.account_id
    bound = ctx.principal.bound_purpose
    assert account_id is not None and bound is not None
    return NewGap(
        producer_kind="claude_code_local",
        authority_id=authority_id,
        native_key=f"cc1g:{secrets.token_hex(32)}",
        speaker_account_id=speaker_account_id or account_id,
        audience_kind="member",
        audience_id=account_id,
        purpose=purpose or bound,
        recorded_at=_AT,
        reason=reason,
    )


def _record(
    ev: EvidenceWorkspace, gaps: list[NewGap], *, now: datetime
) -> tuple[str, ...]:
    with ev.handler(probe_registry()) as uow:
        return record_gaps(ev.context(_PURPOSE), uow, gaps, now=now)


def _rows(ev: EvidenceWorkspace, native_key: str) -> list[Row[Any]]:
    with ev.unit_of_work() as uow:
        return list(
            uow.connection.execute(
                select(evidence_unit).where(evidence_unit.c.native_key == native_key)
            )
        )


def _outbox_count(ev: EvidenceWorkspace) -> int:
    with ev.unit_of_work() as uow:
        return int(
            uow.connection.execute(
                select(func.count()).select_from(work_tables.outbox_event)
            ).scalar_one()
        )


def test_the_gap_reasons_are_the_two_outcome_words() -> None:
    assert get_args(GapReason) == (OUTCOME_SOURCE_TRUNCATED, OUTCOME_EXPIRED_PENDING)


def test_record_gaps_writes_content_free_gap_rows(ev: EvidenceWorkspace) -> None:
    ctx = ev.context(_PURPOSE)
    authority_id = uuid7()
    truncated = _gap(ctx, authority_id=authority_id)
    expired = _gap(ctx, authority_id=authority_id, reason="expired_pending")
    now = _AT + timedelta(minutes=5)

    assert _record(ev, [truncated, expired], now=now) == (
        truncated.native_key,
        expired.native_key,
    )

    for gap in (truncated, expired):
        (row,) = _rows(ev, gap.native_key)
        assert row.body is None
        assert row.state == STATE_GAP
        assert row.outcome == gap.reason
        assert row.producer_kind == "claude_code_local"
        assert row.authority_id == authority_id
        assert row.speaker_account_id == ev.owner_account_id
        assert row.audience_kind == "member"
        assert row.audience_id == ev.owner_account_id
        assert row.purpose == _PURPOSE.value
        assert row.source_recorded_at == _AT
        assert row.settled_at == now


def test_record_gaps_is_idempotent_on_the_native_key(ev: EvidenceWorkspace) -> None:
    gap = _gap(ev.context(_PURPOSE), authority_id=uuid7())
    first = _AT + timedelta(minutes=1)

    assert _record(ev, [gap], now=first) == (gap.native_key,)
    assert _record(ev, [gap], now=first + timedelta(minutes=1)) == (gap.native_key,)

    (row,) = _rows(ev, gap.native_key)
    assert row.settled_at == first


def test_record_gaps_publishes_no_event(ev: EvidenceWorkspace) -> None:
    before = _outbox_count(ev)
    _record(ev, [_gap(ev.context(_PURPOSE), authority_id=uuid7())], now=_AT)
    assert _outbox_count(ev) == before


def test_a_gap_of_another_speaker_refuses_the_whole_batch(
    ev: EvidenceWorkspace,
) -> None:
    ctx = ev.context(_PURPOSE)
    authority_id = uuid7()
    own = _gap(ctx, authority_id=authority_id)
    other = _gap(ctx, authority_id=authority_id, speaker_account_id=uuid7())

    # Counted on the refused call's own open connection, before anything could roll
    # back: nothing was written, not written and then undone.
    with ev.unit_of_work() as uow:
        with pytest.raises(ValueError, match="speaker"):
            record_gaps(ctx, handler_uow(uow, probe_registry()), [own, other], now=_AT)
        written = uow.connection.execute(
            select(func.count())
            .select_from(evidence_unit)
            .where(evidence_unit.c.native_key.in_([own.native_key, other.native_key]))
        ).scalar_one()
    assert written == 0

    assert _rows(ev, own.native_key) == []
    assert _rows(ev, other.native_key) == []


def test_a_gap_of_another_purpose_refuses_the_whole_batch(
    ev: EvidenceWorkspace,
) -> None:
    ctx = ev.context(_PURPOSE)
    authority_id = uuid7()
    own = _gap(ctx, authority_id=authority_id)
    other = _gap(ctx, authority_id=authority_id, purpose=ContextPurpose.RESPOND)

    with pytest.raises(ValueError, match="purpose"):
        _record(ev, [own, other], now=_AT)

    assert _rows(ev, own.native_key) == []
    assert _rows(ev, other.native_key) == []


def test_the_retention_purge_deletes_a_gap_row_on_settled_at(
    ev: EvidenceWorkspace,
) -> None:
    gap = _gap(ev.context(_PURPOSE), authority_id=uuid7())
    _record(ev, [gap], now=_AT)

    with ev.unit_of_work() as uow:
        kept = purge_evidence(uow.connection, now=_AT, settled_before=_AT)
        uow.commit()
    assert kept.deleted == 0
    assert len(_rows(ev, gap.native_key)) == 1

    later = _AT + timedelta(seconds=1)
    with ev.unit_of_work() as uow:
        purged = purge_evidence(uow.connection, now=later, settled_before=later)
        uow.commit()
    assert purged.deleted == 1
    assert _rows(ev, gap.native_key) == []
