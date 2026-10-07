"""Browser projections and stale settings through real synthetic dispatch."""

import pytest
from harness.registry import add_member
from rheo_contracts import RecordRef, Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads.storage import tables as t
from sqlalchemy import select

from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake
from postgres.test_leads_pipeline import capture, ok, one, setup

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.parametrize("intake", [False], indirect=True),
]


def test_catalog_never_exposes_connection_credentials_and_member_gets_no_settings(
    intake: Intake,
) -> None:
    setup(intake)
    catalog = ok(intake, "ui.catalog").items
    connection = next(r for r in catalog if r["kind"] == "connection")
    assert set(connection) == {
        "kind",
        "ref",
        "id",
        "name",
        "transport",
        "state",
        "funnel_id",
        "mapping_version",
        "rules",
    }
    member = add_member(
        intake.cluster.backend,
        intake.workspace,
        Role.MEMBER,
        display_name="Example member",
    )
    ctx = context_for_harness(intake.workspace, member, Role.MEMBER)
    assert isinstance(ctx, WorkspaceContext)
    from rheo_leads.pipeline_contracts import Empty
    from rheo_leads.web_reads import catalog as handler

    with open_unit_of_work(ctx) as uow:
        assert not any(
            r["kind"] == "connection" for r in handler(ctx, uow, Empty()).items
        )


def test_receipt_progress_links_and_pinned_transitions(intake: Intake) -> None:
    setup(intake)
    accepted = intake.accept(
        source_event_id="browser", body='{"subject":"Synthetic inquiry"}'
    )
    pending = ok(intake, "intake.receipt", receipt_ref=accepted.receipt_ref)
    assert pending.data["state"] == "pending"
    assert pending.data["opportunity_refs"] == []
    intake.visit()
    processed = ok(intake, "intake.receipt", receipt_ref=accepted.receipt_ref)
    opportunity = one(intake)
    assert processed.data["opportunity_refs"] == [opportunity.ref]
    assert processed.data["observation_ref"] == str(
        RecordRef(
            module="leads",
            record_type="observation",
            id=RecordRef.parse(accepted.receipt_ref).id,
        )
    )
    assert "discovery" in {r["stage_id"] for r in opportunity.data["transitions"]}
    assert "won" not in {r["stage_id"] for r in opportunity.data["transitions"]}


def test_stale_routing_form_cannot_replace_newer_rules(intake: Intake) -> None:
    pipeline = setup(intake)
    catalog = ok(intake, "ui.catalog").items
    connection = next(
        r
        for r in catalog
        if r["kind"] == "connection" and r["id"] == intake.connection_id
    )
    old = [r["id"] for r in connection["rules"]]
    ok(
        intake,
        "connection.set_routing",
        connection_id=str(intake.connection_id),
        rules=[],
    )
    result = intake.call(
        "leads.connection.set_routing",
        connection_id=str(intake.connection_id),
        expected_rule_ids=old,
        rules=[{"action": "create_opportunity", "pipeline_id": str(pipeline)}],
    )
    assert not result.ok and result.state == "record_stale"


def test_detail_keeps_drafts_and_notes_separate_from_source(intake: Intake) -> None:
    setup(intake)
    capture(
        intake, "draft", {"subject": "Original inquiry", "message": "Original evidence"}
    )
    opportunity = one(intake)
    ok(
        intake,
        "followup.draft",
        ref=opportunity.ref,
        revision=opportunity.revision,
        body="Synthetic draft",
    )
    refreshed = one(intake)
    assert refreshed.data["drafts"][0]["body"] == "Synthetic draft"
    with open_unit_of_work(intake.ctx) as uow:
        assert (
            uow.connection.execute(
                select(t.observation_field.c.value_text).where(
                    t.observation_field.c.target == "message"
                )
            ).scalar_one()
            == "Original evidence"
        )
