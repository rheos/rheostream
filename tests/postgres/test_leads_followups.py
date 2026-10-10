"""Follow-ups through installed modules, real revisions, export and erasure."""

from datetime import date
from typing import Any

import pytest
from rheo_core.operations.tool_facade import call_registered_tool
from rheo_leads.storage import pipeline as p

from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake
from postgres.test_leads_pipeline import (
    approve_delete,
    capture,
    ok,
    one,
    remove_jobsearch_preset,
    setup,
)

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.parametrize("intake", [False, True], indirect=True),
]


def schedule(i: Intake, record: Any, action: str, due_on: str) -> Any:
    result = call_registered_tool(
        i.ctx,
        "leads_schedule_followup",
        {
            "ref": record.ref,
            "revision": record.revision,
            "action": action,
            "due_on": due_on,
        },
        consumers=i.consumers,
        tools=i.surfaces.tools,
        registry=i.surfaces.operations,
    )
    assert result.ok, result
    return ok(i, "opportunity.get", ref=record.ref)


def test_reschedule_resolve_and_explicit_due_queue(intake: Intake) -> None:
    setup(intake, "create_opportunity")
    capture(intake, "one", {"subject": "First"})
    original = one(intake)
    current = schedule(intake, original, "Ask about scope", "2026-10-12")
    assert current.revision == original.revision + 1
    pending = current.data["followup"]
    assert pending["due_on"] == date(2026, 10, 12)
    assert pending["created_by_id"] == intake.ctx.actor.id
    stale = intake.call(
        "leads.followup.schedule",
        ref=original.ref,
        revision=original.revision,
        action="Stale",
        due_on="2026-10-13",
    )
    assert stale.state == "record_stale"
    current = schedule(intake, current, "Confirm scope", "2026-10-15")
    assert current.data["followup_history"][0]["state"] == "superseded"
    assert current.data["followup_history"][0]["resolved_by_id"] == intake.ctx.actor.id
    assert not ok(intake, "opportunity.list", followup_due_by="2026-10-14").items
    due = ok(intake, "opportunity.list", followup_due_by="2026-10-15").items
    assert len(due) == 1 and due[0]["followup_action"] == "Confirm scope"
    capture(intake, "two", {"subject": "Second"})
    second_row = next(
        r for r in ok(intake, "opportunity.list").items if r["ref"] != current.ref
    )
    second = ok(intake, "opportunity.get", ref=second_row["ref"])
    second = schedule(intake, second, "Earlier action", "2026-10-11")
    assert [
        r["ref"]
        for r in ok(intake, "opportunity.list", followup_due_by="2026-10-15").items
    ] == [second.ref, current.ref]
    wrong = intake.call(
        "leads.followup.resolve",
        ref=second.ref,
        revision=second.revision,
        reminder_id=str(current.data["followup"]["id"]),
        outcome="completed",
    )
    assert wrong.state == "followup_not_pending"
    result = call_registered_tool(
        intake.ctx,
        "leads_resolve_followup",
        {
            "ref": current.ref,
            "revision": current.revision,
            "reminder_id": str(current.data["followup"]["id"]),
            "outcome": "completed",
        },
        consumers=intake.consumers,
        tools=intake.surfaces.tools,
        registry=intake.surfaces.operations,
    )
    assert result.ok, result
    assert [
        r["ref"]
        for r in ok(intake, "opportunity.list", followup_due_by="2026-10-15").items
    ] == [second.ref]
    read = ok(intake, "opportunity.get", ref=current.ref)
    assert read.data["followup"] is None
    assert read.data["followup_history"][0]["state"] == "completed"
    cancelled = ok(
        intake,
        "followup.resolve",
        ref=second.ref,
        revision=second.revision,
        reminder_id=str(second.data["followup"]["id"]),
        outcome="cancelled",
    )
    assert not ok(intake, "opportunity.list", followup_due_by="2026-10-15").items
    repeated = intake.call(
        "leads.followup.resolve",
        ref=second.ref,
        revision=cancelled.revision,
        reminder_id=str(second.data["followup"]["id"]),
        outcome="completed",
    )
    assert repeated.state == "followup_not_pending"
    assert intake.count(p.draft) == 0 and intake.count(p.handoff) == 0


def test_later_evidence_preserves_reminder_and_closure_cancels_it(
    intake: Intake,
) -> None:
    setup(intake)
    capture(intake, "one", {"person.email": "a@example.com", "subject": "Inquiry"})
    current = schedule(intake, one(intake), "Discuss next steps", "2026-10-12")
    reminder = current.data["followup"]["id"]
    capture(
        intake, "two", {"person.email": "a@example.com", "message": "More evidence"}
    )
    current = one(intake)
    assert current.data["followup"]["id"] == reminder
    current = ok(
        intake,
        "opportunity.transition",
        ref=current.ref,
        revision=current.revision,
        to_stage_id="disqualified",
    )
    read = one(intake)
    assert read.data["followup"] is None
    assert read.data["followup_history"][0]["state"] == "cancelled"
    refused = intake.call(
        "leads.followup.schedule",
        ref=current.ref,
        revision=current.revision,
        action="Again",
        due_on="2026-10-12",
    )
    assert refused.state == "opportunity_closed"


@pytest.mark.parametrize("kind", ["observation", "opportunity"])
def test_erasure_removes_pending_and_historical_reminders(
    intake: Intake, kind: str
) -> None:
    setup(intake)
    obs = capture(intake, "one", {"subject": "Inquiry"})
    current = schedule(intake, one(intake), "Original content", "2026-10-12")
    current = schedule(intake, current, "Replacement content", "2026-10-13")
    assert intake.count(p.followup_reminder) == 2
    approve_delete(intake, obs if kind == "observation" else current.ref)
    assert intake.count(p.followup_reminder) == 0


def test_existing_opportunity_survives_frozen_upgrade(intake: Intake) -> None:
    from rheo_core.migrations.orchestrator import run_chain
    from rheo_core.storage.routing import open_unit_of_work
    from sqlalchemy import text

    setup(intake)
    capture(intake, "one", {"subject": "Before migration"})
    before = one(intake)
    # This disposable workspace starts at head. Reconstruct the predecessor by
    # removing only the new, empty table (and 0006's column) and resetting Alembic's
    # version marker.
    with open_unit_of_work(intake.ctx) as uow:
        remove_jobsearch_preset(uow.connection)
        uow.connection.execute(text("DROP TABLE leads.followup_reminder"))
        uow.connection.execute(
            text("ALTER TABLE leads.qualification DROP COLUMN evidence_digest")
        )
        uow.connection.execute(
            text(
                "UPDATE leads.alembic_version_leads "
                "SET version_num = '0004_opportunities'"
            )
        )
        database = uow.connection.execute(
            text("SELECT current_database()")
        ).scalar_one()
        run_chain(uow.connection, "leads", expected_database=database)
        run_chain(uow.connection, "leads", expected_database=database)
        uow.commit()
    after = one(intake)
    assert after.ref == before.ref and after.revision == before.revision
    assert after.data["title"] == "Before migration"
    assert schedule(intake, after, "After upgrade", "2026-10-15").data["followup"]


def test_connection_grant_cannot_schedule_followups(intake: Intake) -> None:
    from rheo_contracts import WorkspaceContext
    from rheo_core.boundary.factories import context_for_connection
    from rheo_core.operations.dispatch import dispatch

    setup(intake)
    capture(intake, "one", {"subject": "Inquiry"})
    current = one(intake)
    ctx = context_for_connection(
        intake.workspace, intake.connection_id, "leads", "manual"
    )
    assert isinstance(ctx, WorkspaceContext)
    denied = dispatch(
        ctx,
        "leads.followup.schedule",
        {
            "ref": current.ref,
            "revision": current.revision,
            "action": "Outside intake grant",
            "due_on": "2026-10-15",
        },
        registry=intake.surfaces.operations,
        consumers=intake.consumers,
    )
    assert denied.state == "operation_not_permitted"
    assert intake.count(p.followup_reminder) == 0
