"""Issue #436: what a Leads read tool sends to a model, through the real tool facade.

Leads tiered its whole generic output ``restricted``, so every Leads tool rendered to
``{}`` for a model while the web, which reads ``result``, looked fine. These tests
read ``model_view``, the half the MCP transport sends, for a direct call and for the
same call made through ``operations_call``, and check both what arrives and what must
not: contact facts, the verified sender address, and an extension field a preset
declares ``restricted``.
"""

import json
from typing import Any

import pytest
from rheo_contracts import ContextPurpose, RecordRef, WorkspaceContext
from rheo_core.operations.core_ops import TOOL_CALL
from rheo_core.operations.tool_facade import call_registered_tool
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.tokens.sets import register_core_tools
from rheo_leads.storage import pipeline as p
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
PHONE = "+1 250 555 0142"
SPEND = "48213"
"""A value distinctive enough that finding it in a rendering means it leaked."""


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
    for value in (EMAIL, PHONE, SPEND):
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
    assert "verified_email" not in seen["data"]
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
