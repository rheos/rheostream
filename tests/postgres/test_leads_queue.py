"""The job queue's card projection through real synthetic dispatch."""

from typing import Any

import pytest
from harness.registry import add_member
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads.queue_view import PREVIEW_CHARS, SUMMARY_CHARS, QueueInput, queue
from rheo_leads.storage import tables as t
from sqlalchemy import insert, select

from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake
from postgres.test_leads_pipeline import capture, ok, setup

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.parametrize("intake", [False], indirect=True),
]

FIELDS = (
    "posting_id",
    "posting_url",
    "job_type",
    "budget_min",
    "budget_max",
    "proposal_count",
    "proposal_count_exact",
    "posted_on",
    "client_payment_verified",
    "client_spend",
    "client_rating",
    "client_reviews",
)


def job_pipeline(i: Intake) -> str:
    """A Job search pipeline fed by the test connection, with the job fields mapped."""
    ok(i, "core.settings.set", key="leads.capabilities.jobsearch", value=True)
    jobs = ok(i, "pipeline.create", name="Job leads", preset="job_search")
    with open_unit_of_work(i.ctx) as uow:
        mapping = uow.connection.execute(select(t.field_mapping)).mappings().one()
        for name in FIELDS:
            uow.connection.execute(
                insert(t.field_mapping_rule).values(
                    mapping_id=mapping["id"],
                    version=mapping["version"],
                    target=f"ext.jobsearch.{name}",
                    source_path=f"/{name}",
                    required=False,
                    clear_on_null=False,
                )
            )
        uow.commit()
    ok(
        i,
        "connection.set_routing",
        connection_id=str(i.connection_id),
        rules=[{"action": "create_opportunity", "pipeline_id": str(jobs.data["id"])}],
    )
    return str(jobs.data["id"])


def cards(i: Intake, **query: Any) -> dict[str, dict[str, Any]]:
    return {r["title"]: r for r in ok(i, "ui.queue", **query).items}


def test_cards_carry_full_and_sparse_facts(intake: Intake) -> None:
    setup(intake)
    capture(
        intake,
        "service",
        {
            "person.email": "sample@example.com",
            "person.name": "Example Person",
            "subject": "Service inquiry",
            "message": "Line one",
        },
    )
    pipeline = job_pipeline(intake)
    long = "Build a data pipeline. " * 30
    capture(
        intake,
        "full",
        {
            "subject": "Full posting",
            "message": long,
            "posting_id": "~0201",
            "posting_url": "https://jobs.example.com/postings/0201",
            "job_type": "fixed",
            "budget_min": "500",
            "budget_max": "1500",
            "proposal_count": "50",
            "proposal_count_exact": "false",
            "posted_on": "2026-10-01",
            "client_payment_verified": "true",
            "client_spend": "12000",
            "client_rating": "4.9",
            "client_reviews": "12",
        },
    )
    capture(intake, "sparse", {"subject": "Sparse posting"})

    found = cards(intake, pipeline_id=pipeline)
    assert set(found) == {"Full posting", "Sparse posting"}
    full, sparse = found["Full posting"], found["Sparse posting"]
    assert full["fields"]["proposal_count"] == "50"
    assert full["fields"]["client_spend"] == "12000"
    assert full["fields"]["posting_url"].endswith("/postings/0201")
    assert "posting_id" not in full["fields"], "only what a card shows"
    assert full["preview"].endswith("…") and len(full["preview"]) <= PREVIEW_CHARS + 1
    assert full["stage_id"] == "review" and full["stage_kind"] == "open"
    assert full["stage_label"] == "Review"
    assert full["dismiss"] == {"stage_id": "passed", "label": "Passed"}
    assert [m["stage_id"] for m in full["transitions"]] == [
        "qualified",
        "lost",
        "passed",
    ]
    assert full["transitions"][0] == {
        "stage_id": "qualified",
        "label": "Qualified",
        "kind": "open",
    }
    assert isinstance(full["source"], str) and full["source"]
    assert sparse["fields"] == {} and sparse["preview"] is None
    for card in (full, sparse):
        assert card["assessment"] is None
        assert card["followup"] is None
        assert card["party"] is None, "a posting has no contact"
        assert card["has_draft"] is False and card["has_notes"] is False

    service = cards(intake)["Service inquiry"]
    assert service["fields"] == {}
    assert service["preview"] == "Line one"
    assert service["party"] is not None and service["party"]["name"]
    assert set(service["party"]) == {"kind", "name"}, "never contact points"
    assert service["dismiss"]["stage_id"] == "disqualified"


def test_assessment_followup_notes_and_draft_extras(intake: Intake) -> None:
    setup(intake)
    pipeline = job_pipeline(intake)
    capture(intake, "extra", {"subject": "Assessed posting"})
    reference = cards(intake)["Assessed posting"]["ref"]
    current = ok(intake, "opportunity.get", ref=reference)
    ok(
        intake,
        "qualification.assess",
        ref=current.ref,
        revision=current.revision,
        objective="Job fit",
        assessment={
            "fit": "high",
            "intent": "medium",
            "urgency": "low",
            "evidence_completeness": "medium",
            "uncertainty": "low",
            "explanation": "\nStrong match for the stack.\n\nSecond line stays in.",
            "author": "human",
        },
    )
    current = ok(intake, "opportunity.get", ref=reference)
    ok(
        intake,
        "opportunity.add_note",
        ref=current.ref,
        revision=current.revision,
        body="N",
    )
    current = ok(intake, "opportunity.get", ref=current.ref)
    ok(intake, "followup.draft", ref=current.ref, revision=current.revision, body="D")
    current = ok(intake, "opportunity.get", ref=current.ref)
    ok(
        intake,
        "followup.schedule",
        ref=current.ref,
        revision=current.revision,
        action="Check the reply",
        due_on="2026-10-20",
    )
    card = cards(intake, pipeline_id=pipeline)["Assessed posting"]
    assert card["assessment"]["fit"] == "high"
    assert card["assessment"]["summary"] == (
        "Strong match for the stack. Second line stays in."
    )
    assert str(card["followup"]["due_on"]) == "2026-10-20"
    assert card["followup"]["action"] == "Check the reply"
    assert card["has_notes"] is True and card["has_draft"] is True


def test_filters_closed_stages_and_single_ref(intake: Intake) -> None:
    services = setup(intake)
    capture(
        intake, "service", {"person.email": "sample@example.com", "subject": "Service"}
    )
    pipeline = job_pipeline(intake)
    capture(intake, "a", {"subject": "Open posting"})
    capture(intake, "b", {"subject": "Passed posting"})
    passed = cards(intake)["Passed posting"]
    ok(
        intake,
        "opportunity.transition",
        ref=passed["ref"],
        revision=passed["revision"],
        to_stage_id="passed",
    )
    assert set(cards(intake)) == {"Service", "Open posting"}
    assert set(cards(intake, jobsearch_only=True)) == {"Open posting"}
    assert set(cards(intake, pipeline_id=str(services))) == {"Service"}
    assert set(cards(intake, include_closed=True)) == {
        "Service",
        "Open posting",
        "Passed posting",
    }
    assert set(cards(intake, stage_id="passed")) == {"Passed posting"}
    closed = cards(intake, ref=passed["ref"])["Passed posting"]
    assert closed["stage_kind"] == "terminal" and closed["dismiss"] is None
    assert set(cards(intake, pipeline_id=pipeline, limit=1)) == {"Open posting"}


def test_member_reads_the_queue_and_dismiss_follows_the_stage(intake: Intake) -> None:
    setup(intake)
    job_pipeline(intake)
    capture(intake, "a", {"subject": "Applied posting"})
    card = cards(intake)["Applied posting"]
    for stage in ("qualified", "applied"):
        moved = ok(
            intake,
            "opportunity.transition",
            ref=card["ref"],
            revision=card["revision"],
            to_stage_id=stage,
        )
        card = {**card, "revision": moved.revision}
    assert cards(intake)["Applied posting"]["dismiss"] == {
        "stage_id": "lost",
        "label": "Lost",
    }
    member = add_member(
        intake.cluster.backend,
        intake.workspace,
        Role.MEMBER,
        display_name="Example member",
    )
    ctx = context_for_harness(intake.workspace, member, Role.MEMBER)
    assert isinstance(ctx, WorkspaceContext)
    with open_unit_of_work(ctx) as uow:
        titles = [r["title"] for r in queue(ctx, uow, QueueInput()).items]
    assert titles == ["Applied posting"]


def assess(
    i: Intake, reference: str, fit: str, explanation: str = "Synthetic."
) -> None:
    current = ok(i, "opportunity.get", ref=reference)
    ok(
        i,
        "qualification.assess",
        ref=current.ref,
        revision=current.revision,
        objective="Job fit",
        assessment={
            "fit": fit,
            "intent": "medium",
            "urgency": "low",
            "evidence_completeness": "medium",
            "uncertainty": "low",
            "explanation": explanation,
            "author": "human",
        },
    )


def test_score_sort_and_cap_filter_apply_before_the_limit(intake: Intake) -> None:
    """An older high-fit posting heads the first page, and crowded postings are
    left out before the limit, so neither depends on the newest rows alone."""
    setup(intake)
    job_pipeline(intake)
    capture(intake, "old", {"subject": "Older high fit"})
    capture(intake, "low", {"subject": "Low fit", "proposal_count": "10"})
    capture(intake, "crowd", {"subject": "Crowded", "proposal_count": "51"})
    capture(intake, "new", {"subject": "Newest unassessed"})
    titles = cards(intake)
    assess(intake, titles["Older high fit"]["ref"], "high", "A " + "long " * 300)
    assess(intake, titles["Low fit"]["ref"], "low")
    assess(intake, titles["Crowded"]["ref"], "high")

    def order(**query: Any) -> list[str]:
        return [r["title"] for r in ok(intake, "ui.queue", **query).items]

    assert order(limit=2) == ["Newest unassessed", "Crowded"]
    assert order(limit=2, sort="score") == ["Crowded", "Older high fit"]
    assert order(limit=2, sort="score", hide_full=True) == [
        "Older high fit",
        "Low fit",
    ]
    assert order(sort="score", hide_full=True) == [
        "Older high fit",
        "Low fit",
        "Newest unassessed",
    ]
    assert len(order(hide_full=True)) == 3
    crowded = titles["Crowded"]["ref"]
    assert order(ref=crowded, hide_full=True) == ["Crowded"], "a ref ignores filters"
    summary = cards(intake)["Older high fit"]["assessment"]["summary"]
    assert len(summary) == SUMMARY_CHARS + 1 and summary.endswith("…")
