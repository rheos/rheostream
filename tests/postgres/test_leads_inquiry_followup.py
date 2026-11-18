"""Mail inquiries through manual capture: caller-keyed repeats, folding by source
link, and the first follow-up a routed inquiry gets (rheos/rheostream#593)."""

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import pytest
from rheo_contracts import RecordRef
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads.pipeline_intake import FOLLOWUP_ACTION, business_days_after
from rheo_leads.references import ref
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t
from sqlalchemy import select

from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake
from postgres.test_leads_pipeline import ok

pytestmark = pytest.mark.postgres

KEY = "leads.intake.followup_business_days"


def route(i: Intake, preset: str | None = None, *, fold: bool = False) -> UUID:
    extra = {"preset": preset} if preset else {}
    pipeline = RecordRef.parse(
        ok(i, "pipeline.create", name="Service inquiries", **extra).ref
    ).id
    rules: list[dict[str, Any]] = [
        {"action": "create_opportunity", "pipeline_id": str(pipeline)}
    ]
    if fold:
        rules.insert(
            0,
            {
                "action": "attach_by_field",
                "match_field": "source_url",
                "pipeline_id": str(pipeline),
            },
        )
    ok(i, "connection.set_routing", connection_id=str(i.connection_id), rules=rules)
    return pipeline


def capture(i: Intake, body: dict[str, Any], event: str | None = None) -> Any:
    extra = {"source_event_id": event} if event else {}
    result = ok(
        i,
        "intake.capture",
        funnel_ref=ref("funnel", i.funnel_id),
        body=body,
        **extra,
    )
    i.visit()
    return result


def reminders(i: Intake) -> list[dict[str, Any]]:
    with open_unit_of_work(i.ctx) as uow:
        return [
            dict(r)
            for r in uow.connection.execute(select(p.followup_reminder)).mappings()
        ]


def received_on(i: Intake) -> date:
    with open_unit_of_work(i.ctx) as uow:
        at = uow.connection.execute(
            select(t.observation.c.received_at).limit(1)
        ).scalar_one()
    assert isinstance(at, datetime)
    return at.astimezone(UTC).date()


def test_a_caller_keyed_repeat_is_a_duplicate_of_the_first(intake: Intake) -> None:
    route(intake)
    first = capture(
        intake, {"subject": "Website build", "person.name": "Sam Example"}, "msg-1"
    )
    # A rescan writes its own wording; the first capture stands.
    again = capture(
        intake, {"subject": "Website rebuild", "person.name": "Sam"}, "msg-1"
    )
    assert first.outcome == "accepted"
    assert again.outcome == "duplicate"
    assert again.receipt_ref == first.receipt_ref
    assert intake.count(t.delivery_receipt) == 1
    assert intake.count(t.delivery_conflict) == 0
    assert intake.count(p.opportunity) == 1
    # A different id is a new event; leaving the id out still makes each capture new.
    capture(intake, {"subject": "Second inquiry"}, "msg-2")
    capture(intake, {"subject": "Pasted inquiry"})
    capture(intake, {"subject": "Pasted inquiry"})
    assert intake.count(t.delivery_receipt) == 4
    assert intake.count(p.opportunity) == 4


def test_source_link_folds_a_thread_and_keeps_distinct_leads_apart(
    intake: Intake,
) -> None:
    route(intake, fold=True)
    thread = "https://mail.example.com/thread/t-100"
    capture(intake, {"subject": "App quote", "source_url": thread}, "m-1")
    capture(intake, {"subject": "App quote, follow-up", "source_url": thread}, "m-2")
    assert intake.count(t.observation) == 2
    assert intake.count(p.opportunity) == 1
    # Another link (another lead stacked in a form relay thread) is its own lead;
    # no link at all falls through to the create rule.
    capture(intake, {"subject": "Logo work", "source_url": thread + "/m-3"}, "m-3")
    capture(intake, {"subject": "No link"}, "m-4")
    assert intake.count(p.opportunity) == 3


def test_routed_inquiry_gets_its_first_followup_and_a_repeat_keeps_it(
    intake: Intake,
) -> None:
    route(intake, fold=True)
    ok(intake, "core.settings.set", key=KEY, value=1)
    link = "https://mail.example.com/thread/t-200"
    capture(intake, {"subject": "Booking tool", "source_url": link}, "m-1")
    [reminder] = reminders(intake)
    assert reminder["state"] == "pending"
    assert reminder["action"] == FOLLOWUP_ACTION
    assert reminder["due_on"] == business_days_after(received_on(intake), 1)
    listed = ok(intake, "opportunity.list").items
    assert listed[0]["followup_due_on"] == reminder["due_on"]

    # The client writes again: attached, and the owner's follow-up is untouched.
    capture(intake, {"subject": "Booking tool, any news?", "source_url": link}, "m-2")
    assert [(r["id"], r["state"]) for r in reminders(intake)] == [
        (reminder["id"], "pending")
    ]


def test_no_followup_by_default_or_on_a_job_search_pipeline(intake: Intake) -> None:
    route(intake)
    capture(intake, {"subject": "Default workspace"}, "m-1")
    assert intake.count(p.opportunity) == 1
    assert reminders(intake) == []

    ok(intake, "core.settings.set", key="leads.capabilities.jobsearch", value=True)
    ok(intake, "core.settings.set", key=KEY, value=2)
    route(intake, "job_search")
    capture(intake, {"subject": "A posting"}, "m-2")
    assert intake.count(p.opportunity) == 2
    assert reminders(intake) == []


@pytest.mark.parametrize(
    "start,days,due",
    [
        (date(2026, 11, 16), 1, date(2026, 11, 17)),  # Monday to Tuesday
        (date(2026, 11, 20), 1, date(2026, 11, 23)),  # Friday to Monday
        (date(2026, 11, 21), 1, date(2026, 11, 23)),  # Saturday to Monday
        (date(2026, 11, 22), 1, date(2026, 11, 23)),  # Sunday to Monday
        (date(2026, 11, 19), 3, date(2026, 11, 24)),  # Thursday plus three
    ],
)
def test_business_days_skip_the_weekend(start: date, days: int, due: date) -> None:
    assert business_days_after(start, days) == due
