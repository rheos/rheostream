"""Issue #436: what a Leads read tool sends to a model, through the real tool facade.

Leads tiered its whole generic output ``restricted``, so every Leads tool rendered to
``{}`` for a model while the web, which reads ``result``, looked fine. These tests
read ``model_view``, the half the MCP transport sends, for a direct call and for the
same call made through ``operations_call``, and check both what arrives and what must
not: contact facts, the verified sender address and the source's subject key, an
extension field a preset declares ``restricted``, a handoff's outbound payload, and
records of a type the workspace excludes.
"""

import json
from typing import Any

import pytest
from harness.redaction import AREA, EXCHANGE, LINE, phone
from rheo_contracts import ContextPurpose, RecordRef, WorkspaceContext
from rheo_core.operations.core_ops import TOOL_CALL
from rheo_core.operations.tool_facade import call_registered_tool
from rheo_core.redaction.policy import exclude_types_spec
from rheo_core.settings import resolver as settings_resolver
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.tokens.sets import register_core_tools
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t
from sqlalchemy import update

from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake
from postgres.test_leads_pipeline import capture, ok
from postgres.test_leads_queue import job_pipeline

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.parametrize("intake", [False], indirect=True),
]

EMAIL = "client@example.com"
PHONE = "+1 " + phone(AREA, EXCHANGE, LINE, sep=" ")
SPEND = "48213"
"""A value distinctive enough that finding it in a rendering means it leaked."""
SUBJECT = phone(AREA, EXCHANGE, LINE, sep="")
"""A bare-digit subject key, which the free-text phone mask does not catch."""


def posting(i: Intake) -> dict[str, Any]:
    job_pipeline(i)
    # The job search preset declares its fields internal; a workspace may mark one
    # restricted, and then no model sees it.
    with open_unit_of_work(i.ctx) as uow:
        uow.connection.execute(
            update(p.preset_field)
            .where(p.preset_field.c.target == "ext.jobsearch.client_spend")
            .values(sensitivity="restricted")
        )
        # The source proves its sender's email and keys the sender by a phone number.
        uow.connection.execute(
            update(t.intake_connection)
            .where(t.intake_connection.c.id == i.connection_id)
            .values(email_verified=True, subject_authenticated=True)
        )
        uow.connection.execute(
            update(t.field_mapping_identity).values(
                verified_email_path="/person.email", subject_id_path="/subject_key"
            )
        )
        uow.commit()
    capture(
        i,
        "animator",
        {
            "subject": "2D animator for an explainer",
            "message": "Short explainer, cutout style.",
            "person.name": "Sample Client",
            "person.email": EMAIL,
            "person.phone": PHONE,
            "budget_max": "900",
            "client_spend": SPEND,
            "subject_key": SUBJECT,
        },
    )
    (item,) = ok(i, "opportunity.list").items
    return item


def tool(
    i: Intake, tool_name: str, ctx: WorkspaceContext | None = None, /, **payload: Any
) -> Any:
    outcome = call_registered_tool(
        ctx or i.ctx,
        tool_name,
        payload,
        consumers=i.consumers,
        tools=i.surfaces.tools,
        registry=i.surfaces.operations,
    )
    assert outcome.ok, outcome
    assert outcome.model_view is not None and not outcome.model_view.withheld
    return outcome.model_view.value


def rows(view: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    return {r["target"]: r for r in view["data"][key]}


def assert_nothing_restricted(view: object) -> None:
    text = json.dumps(view)
    for value in (EMAIL, PHONE, SPEND, SUBJECT):
        assert value not in text, value


def test_leads_reads_reach_a_model_without_restricted_values(intake: Intake) -> None:
    item = posting(intake)

    got = tool(intake, "leads_get", ref=item["short_id"])
    assert got["ref"] == item["ref"] and got["revision"] == item["revision"]
    assert got["data"]["title"] == "2D animator for an explainer"
    fields = rows(got, "opportunity_field_state")
    assert fields["ext.jobsearch.budget_max"]["value_text"] == "900"
    assert fields["person.name"]["value_text"] == "Sample Client"
    for hidden in ("person.email", "person.phone", "ext.jobsearch.client_spend"):
        assert "value_text" not in fields[hidden], fields[hidden]
        assert fields[hidden]["value_withheld"] is True
    assert_nothing_restricted(got)

    (link,) = got["data"]["opportunity_observation"]
    observation = RecordRef(
        module="leads", record_type="observation", id=link["observation_id"]
    )
    seen = tool(intake, "leads_get_observation", observation_ref=observation.format())
    full_observation = ok(
        intake, "observation.get", observation_ref=observation.format()
    )
    assert full_observation.data["verified_email"] == EMAIL
    assert full_observation.data["external_subject_id"] == SUBJECT
    assert "verified_email" not in seen["data"]
    assert "external_subject_id" not in seen["data"]
    assert (
        rows(seen, "fields")["message"]["value_text"]
        == "Short explainer, cutout style."
    )
    assert_nothing_restricted(seen)

    found = tool(intake, "leads_search", query="animator")
    assert [r["ref"] for r in found["items"]] == [item["ref"]]
    pipelines = tool(intake, "leads_pipelines")
    assert [r["name"] for r in pipelines["items"]] == ["Job leads"]

    # The web and API read ``result``, which keeps every value.
    full = ok(intake, "opportunity.get", ref=item["ref"])
    assert {r["target"]: r["value_text"] for r in full.data["opportunity_field_state"]}[
        "person.email"
    ] == EMAIL


def test_operations_call_returns_the_same_view(intake: Intake) -> None:
    item = posting(intake)
    register_core_tools(intake.surfaces.tools)
    discover = intake.ctx.model_copy(
        update={
            "operation_set": frozenset(
                {TOOL_CALL, "leads.opportunity.get", "leads.pipeline.list"}
            )
        }
    )
    direct = tool(intake, "leads_get", ref=item["short_id"])
    called = tool(
        intake,
        "operations_call",
        discover,
        name="leads_get",
        input={"ref": item["short_id"]},
    )
    assert called == direct
    assert called["data"]["title"] == "2D animator for an explainer"
    listed = tool(intake, "operations_call", discover, name="leads_pipelines", input={})
    assert [r["name"] for r in listed["items"]] == ["Job leads"]


def test_a_purpose_without_internal_gets_only_the_reference(intake: Intake) -> None:
    item = posting(intake)
    principal = intake.ctx.principal.model_copy(
        update={"bound_purpose": ContextPurpose.RESPOND}
    )
    narrow = intake.ctx.model_copy(update={"principal": principal})
    ok(
        intake,
        "core.settings.set",
        key="redaction.internal_purposes",
        value=["follow_up"],
    )
    got = tool(intake, "leads_get", narrow, ref=item["short_id"])
    assert got == {"ref": item["ref"], "revision": item["revision"]}


def test_contact_values_reach_a_model_when_both_levels_allow_them(
    intake: Intake, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = posting(intake)
    monkeypatch.setenv("RHEO__redaction__contact_points_to_model", "true")
    ok(intake, "core.settings.set", key="redaction.contact_points_to_model", value=True)
    got = tool(intake, "leads_get", ref=item["short_id"])
    fields = rows(got, "opportunity_field_state")
    assert fields["person.email"]["value_text"] == EMAIL
    # The allowance is for contact values only, never a restricted extension field.
    assert "value_text" not in fields["ext.jobsearch.client_spend"]


def test_excluded_record_types_stay_out_of_lists_and_nested_rows(
    intake: Intake,
) -> None:
    item = posting(intake)
    # The loader declares this key per module; the intake fixture resolves against an
    # isolated registry built from manifests, which does not carry it.
    settings_resolver.REGISTRY.register(exclude_types_spec("leads"), origin="leads")
    ok(
        intake,
        "core.settings.set",
        key="leads.redaction.exclude_types",
        value=["qualification", "draft", "pipeline"],
    )
    got = tool(intake, "leads_get", ref=item["short_id"])
    assert got["data"]["title"] == "2D animator for an explainer"
    assert "qualification" not in got["data"] and "drafts" not in got["data"]
    assert tool(intake, "leads_pipelines") == {"items": []}

    ok(
        intake,
        "core.settings.set",
        key="leads.redaction.exclude_types",
        value=["opportunity"],
    )
    assert tool(intake, "leads_list") == {"items": []}
    assert tool(intake, "leads_search", query="animator") == {"items": []}


def test_a_handoff_s_outbound_payload_stays_out(intake: Intake) -> None:
    item = posting(intake)
    handoff = ok(
        intake,
        "handoff.request",
        ref=item["ref"],
        revision=item["revision"],
        purpose="respond",
        payload={"note": SPEND},
        idempotency_key="model-view",
    )
    handoff_id = str(RecordRef.parse(handoff.ref).id)
    assert ok(intake, "handoff.get", handoff_id=handoff_id).data["snapshot"]
    seen = tool(intake, "leads_get_handoff", handoff_id=handoff_id)
    assert seen["ref"] == handoff.ref and "snapshot" not in seen["data"]
    assert_nothing_restricted(seen)
