"""Approved erasure removes the job feed's dedupe row and keeps every receipt.

Seams under test: approved erasure through ``core.record.delete`` and the approval
flow, on an opportunity and on an observation collected through the real poll
acceptance and intake worker. The dedupe row goes in the deletion's own transaction
(a failure later in that transaction leaves it), and every delivery receipt keeps
its ``source_event_id``, which is what keeps a posting's sequence from restarting.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from harness.jobfeed import Collector, deliver, drain, install_collector
from rheo_contracts import RecordRef
from rheo_core.operations.refusals import OperationRefused
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads import lifecycle
from rheo_leads.jobfeed.install import POSTING_ID
from rheo_leads.references import ref
from rheo_leads.storage import jobfeed as j
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t
from sqlalchemy import func, insert, select

from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake
from postgres.test_leads_pipeline import approve_delete

pytestmark = pytest.mark.postgres

POSTING = "~02123"
OTHER = "~02999"
FIRST = f"{POSTING}.1.0a1b2c3d4e5f6071"
SECOND = f"{POSTING}.2.9f8e7d6c5b4a3921"
OTHER_FIRST = f"{OTHER}.1.1122334455667788"


@dataclass
class Collected:
    collector: Collector
    observations: dict[str, str]
    """Observation reference by event id."""
    opportunities: dict[str, str]
    """Opportunity reference by posting id."""


def posting_body(posting_id: str, title: str, proposals: int) -> dict[str, Any]:
    return {
        "title": title,
        "posting_id": posting_id,
        "proposal_count": str(proposals),
        "observed_at": "2026-01-05T09:00:00+00:00",
    }


def remember(c: Collector, posting_id: str, seq: int, event_id: str) -> None:
    """Write the dedupe row the way the collector does after a delivery."""
    now = datetime.now(UTC)
    with open_unit_of_work(c.owner) as uow:
        uow.connection.execute(
            insert(j.jobfeed_posting).values(
                connection_id=c.connection_id,
                posting_id=posting_id,
                seq=seq,
                field_digests="title=00112233;proposal_count=44556677",
                last_event_id=event_id,
                first_delivered_at=now,
                last_delivered_at=now,
            )
        )
        uow.commit()


def collect(i: Intake) -> Collected:
    """Two sightings of one posting (sequence 1, then a changed 2) and one sighting of
    another, collected through the poll connection and the intake worker."""
    c = install_collector(i)
    for event_id, body in (
        (FIRST, posting_body(POSTING, "Example posting", 5)),
        (SECOND, posting_body(POSTING, "Example posting", 9)),
        (OTHER_FIRST, posting_body(OTHER, "Another example posting", 1)),
    ):
        accepted = deliver(c.poll, i, body, event_id)
        assert accepted.ok and accepted.result.outcome == "accepted", accepted  # type: ignore[union-attr]
        drain(i)
    remember(c, POSTING, 2, SECOND)
    remember(c, OTHER, 1, OTHER_FIRST)
    with open_unit_of_work(i.ctx) as uow:
        observations = {
            row.source_event_id: ref("observation", row.id)
            for row in uow.connection.execute(
                select(t.delivery_receipt.c.id, t.delivery_receipt.c.source_event_id)
                .join(t.observation, t.observation.c.id == t.delivery_receipt.c.id)
                .where(t.delivery_receipt.c.connection_id == c.connection_id)
            )
        }
        opportunities = {
            row.value_text: ref("opportunity", row.opportunity_id)
            for row in uow.connection.execute(
                select(
                    p.opportunity_field_state.c.opportunity_id,
                    p.opportunity_field_state.c.value_text,
                ).where(p.opportunity_field_state.c.target == POSTING_ID)
            )
        }
    assert set(observations) == {FIRST, SECOND, OTHER_FIRST}, observations
    # The repeat sighting attached to the posting's one opportunity.
    assert set(opportunities) == {POSTING, OTHER}, opportunities
    assert i.count(p.opportunity) == 2
    return Collected(c, observations, opportunities)


def postings(i: Intake) -> set[tuple[UUID, str]]:
    with open_unit_of_work(i.ctx) as uow:
        return {
            (row.connection_id, row.posting_id)
            for row in uow.connection.execute(
                select(
                    j.jobfeed_posting.c.connection_id, j.jobfeed_posting.c.posting_id
                )
            )
        }


def receipts(i: Intake) -> dict[UUID, dict[str, Any]]:
    """Every delivery receipt, whole, by id."""
    with open_unit_of_work(i.ctx) as uow:
        return {
            row["id"]: dict(row)
            for row in uow.connection.execute(select(t.delivery_receipt)).mappings()
        }


def event_ids_for(i: Intake, connection_id: UUID, posting_id: str) -> list[str]:
    """The read the next sequence number depends on: receipt identifiers only,
    filtered by the text before the first dot."""
    with open_unit_of_work(i.ctx) as uow:
        return sorted(
            uow.connection.execute(
                select(t.delivery_receipt.c.source_event_id).where(
                    t.delivery_receipt.c.connection_id == connection_id,
                    func.split_part(t.delivery_receipt.c.source_event_id, ".", 1)
                    == posting_id,
                )
            ).scalars()
        )


def failing_after_forget(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Make the deletion fail right after its dedupe-row delete has run, in the same
    transaction. Returns the posting ids the delete ran for."""
    original: Callable[..., None] = lifecycle.forget_posting
    ran: list[str] = []

    def forget_then_fail(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        ran.append(kwargs["posting_id"])
        raise OperationRefused("fixture_failure", "Synthetic deletion failure")

    monkeypatch.setattr(lifecycle, "forget_posting", forget_then_fail)
    return ran


def receipt_id(reference: str) -> UUID:
    return RecordRef.parse(reference).id


# --- (a) opportunity erasure ------------------------------------------------------


def test_opportunity_erasure_deletes_only_its_postings_dedupe_row(
    intake: Intake,
) -> None:
    collected = collect(intake)
    connection_id = collected.collector.connection_id
    before = receipts(intake)
    approve_delete(intake, collected.opportunities[POSTING])
    assert postings(intake) == {(connection_id, OTHER)}
    with open_unit_of_work(intake.ctx) as uow:
        remaining = uow.connection.execute(select(p.opportunity.c.id)).scalars().all()
    assert remaining == [receipt_id(collected.opportunities[OTHER])]
    # O1: an opportunity erasure leaves every receipt exactly as it was.
    assert receipts(intake) == before
    assert event_ids_for(intake, connection_id, POSTING) == [FIRST, SECOND]


# --- (b) observation erasure -------------------------------------------------------


def test_observation_erasure_deletes_only_its_postings_dedupe_row(
    intake: Intake,
) -> None:
    collected = collect(intake)
    connection_id = collected.collector.connection_id
    before = receipts(intake)
    erased = collected.observations[FIRST]
    approve_delete(intake, erased)
    assert postings(intake) == {(connection_id, OTHER)}
    after = receipts(intake)
    # O1: every receipt survives with its event id byte-identical; only the erased
    # observation's receipt is marked, and nothing else about it changes.
    assert after.keys() == before.keys()
    for identifier, row in before.items():
        if identifier == receipt_id(erased):
            assert after[identifier]["state_detail"] == "observation_deleted"
            assert {
                k: v for k, v in after[identifier].items() if k != "state_detail"
            } == {k: v for k, v in row.items() if k != "state_detail"}
        else:
            assert after[identifier] == row
        assert after[identifier]["source_event_id"].encode() == (
            row["source_event_id"].encode()
        )
    assert event_ids_for(intake, connection_id, POSTING) == [FIRST, SECOND]


def test_erasing_a_manual_observation_never_touches_the_dedupe_memory(
    intake: Intake,
) -> None:
    """Only an observation of the collector connection forgets a posting, even when
    another connection's event id happens to share the posting id's prefix."""
    collected = collect(intake)
    accepted = intake.accept(
        source_event_id=f"{OTHER}.1.manual", body='{"subject":"Example inquiry"}'
    )
    intake.visit()
    before = postings(intake)
    approve_delete(intake, ref("observation", receipt_id(accepted.receipt_ref)))
    assert postings(intake) == before
    assert (collected.collector.connection_id, OTHER) in before


# --- (c) a failed deletion keeps the row ------------------------------------------


@pytest.mark.parametrize("kind", ["opportunity", "observation"])
def test_a_failed_erasure_leaves_the_dedupe_row(
    intake: Intake, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    collected = collect(intake)
    target = (
        collected.opportunities[POSTING]
        if kind == "opportunity"
        else collected.observations[FIRST]
    )
    before_postings, before_receipts = postings(intake), receipts(intake)
    held = intake.call("core.record.delete", ref=target)
    assert held.state == "approval_required", held
    with monkeypatch.context() as patch:
        ran = failing_after_forget(patch)
        failed = intake.call("core.approval.approve", approval_id=str(held.approval_id))
    assert ran == [POSTING], ran
    assert not failed.ok, failed
    assert postings(intake) == before_postings
    assert receipts(intake) == before_receipts
    assert intake.count(p.opportunity) == 2
    assert intake.count(t.observation) == 3


# --- (d) O1: after both kinds of erasure, every identifier is still there ---------


def test_both_erasures_keep_every_event_id_the_posting_ever_used(
    intake: Intake,
) -> None:
    collected = collect(intake)
    connection_id = collected.collector.connection_id
    before = receipts(intake)
    approve_delete(intake, collected.observations[SECOND])
    approve_delete(intake, collected.opportunities[POSTING])
    assert postings(intake) == {(connection_id, OTHER)}
    after = receipts(intake)
    assert after.keys() == before.keys()
    assert {i: r["source_event_id"] for i, r in after.items()} == {
        i: r["source_event_id"] for i, r in before.items()
    }
    assert after[receipt_id(collected.observations[SECOND])]["state_detail"] == (
        "observation_deleted"
    )
    assert (
        after[receipt_id(collected.observations[FIRST])]
        == (before[receipt_id(collected.observations[FIRST])])
    )
    assert event_ids_for(intake, connection_id, POSTING) == [FIRST, SECOND]
    # The highest sequence among them is what a new sighting continues from.
    assert (
        max(int(e.split(".")[1]) for e in event_ids_for(intake, connection_id, POSTING))
        == 2
    )
