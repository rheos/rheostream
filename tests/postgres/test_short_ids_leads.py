"""Short IDs (#345) through real dispatch, the tool façade and the record resolver."""

from typing import Any
from uuid import UUID

import pytest
from rheo_contracts import RecordRef
from rheo_core.operations.tool_facade import call_registered_tool
from rheo_core.refs.resolver import RecordHead, Unavailable, resolve
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads.storage import pipeline as p
from sqlalchemy import insert, select

from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake
from postgres.test_leads_pipeline import capture, ok
from postgres.test_leads_queue import job_pipeline

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.parametrize("intake", [False], indirect=True),
]


def jobs(i: Intake) -> dict[str, dict[str, Any]]:
    job_pipeline(i)
    capture(i, "first", {"subject": "First posting"})
    capture(i, "second", {"subject": "Second posting"})
    return {r["title"]: r for r in ok(i, "ui.queue", jobsearch_only=True).items}


def clash(i: Intake, original: UUID) -> UUID:
    """A copy of ``original`` whose id shares its last 8 hex characters, not 10."""
    ninth = "0" if original.hex[-10] != "0" else "1"
    clone = UUID(original.hex[:22] + ninth + original.hex[-9:])
    assert clone.hex[-8:] == original.hex[-8:] and clone.hex[-10:] != original.hex[-10:]
    with open_unit_of_work(i.ctx) as uow:
        row = dict(
            uow.connection.execute(
                select(p.opportunity).where(p.opportunity.c.id == original)
            )
            .mappings()
            .one()
        )
        row.update(id=clone, title="Clashing posting", source_observation_id=None)
        uow.connection.execute(insert(p.opportunity).values(**row))
        uow.commit()
    return clone


def test_short_ids_show_resolve_and_refuse_when_ambiguous(intake: Intake) -> None:
    found = jobs(intake)
    first, second = found["First posting"], found["Second posting"]
    a, b = UUID(str(first["id"])), UUID(str(second["id"]))
    # The timestamp trap: two jobs made a moment apart share their head, not their tail.
    assert a.hex[:8] == b.hex[:8]
    assert first["short_id"] == a.hex[-8:] != second["short_id"] == b.hex[-8:]

    full = ok(intake, "opportunity.get", ref=first["ref"])
    bare = ok(intake, "opportunity.get", ref=first["short_id"])
    typed = ok(intake, "opportunity.get", ref=f"leads.opportunity:{first['short_id']}")
    assert full.ref == bare.ref == typed.ref == first["ref"]
    # Writes take it too, and record the full reference.
    noted = ok(
        intake,
        "opportunity.add_note",
        ref=first["short_id"],
        revision=full.revision,
        body="Synthetic note.",
    )
    assert noted.ref == first["ref"]

    missing = intake.call("leads.opportunity.get", ref="00000000")
    assert missing.state == "input_invalid", missing
    assert "no record has this short ID" in missing.error.error_text  # type: ignore[union-attr]
    malformed = intake.call("leads.opportunity.get", ref="not-an-id")
    assert malformed.state == "input_invalid", malformed

    clone = clash(intake, a)
    listed = {r["title"]: r for r in ok(intake, "opportunity.list").items}
    assert listed["First posting"]["short_id"] == a.hex[-10:]
    assert listed["Clashing posting"]["short_id"] == clone.hex[-10:]
    assert listed["Second posting"]["short_id"] == b.hex[-8:]
    queued = {r["title"]: r for r in ok(intake, "ui.queue", jobsearch_only=True).items}
    assert queued["First posting"]["short_id"] == a.hex[-10:]

    ambiguous = intake.call("leads.opportunity.get", ref=a.hex[-8:])
    assert ambiguous.state == "input_invalid", ambiguous
    assert "give more characters" in ambiguous.error.error_text  # type: ignore[union-attr]
    longer = ok(intake, "opportunity.get", ref=a.hex[-10:])
    assert longer.ref == first["ref"]
    assert ok(intake, "opportunity.get", ref=first["ref"]).ref == first["ref"]


def test_tools_and_the_resolver_take_short_ids(intake: Intake) -> None:
    first = jobs(intake)["First posting"]
    a = UUID(str(first["id"]))

    def tool(name: str, **payload: Any) -> Any:
        return call_registered_tool(
            intake.ctx,
            name,
            payload,
            consumers=intake.consumers,
            tools=intake.surfaces.tools,
            registry=intake.surfaces.operations,
        )

    got = tool("leads_get", ref=first["short_id"])
    assert got.ok, got
    assert got.result.ref == first["ref"]

    head = resolve(f"leads.opportunity:{first['short_id']}", intake.ctx)
    assert isinstance(head, RecordHead), head
    assert head.ref == RecordRef.parse(first["ref"])
    nothing = resolve("leads.opportunity:00000000", intake.ctx)
    assert isinstance(nothing, Unavailable) and nothing.reason == "not_found"
    untyped = resolve("leads.observation:00000000", intake.ctx)
    assert isinstance(untyped, Unavailable)
    assert untyped.reason == "reference_malformed"

    clash(intake, a)
    refused = tool("leads_get", ref=a.hex[-8:])
    assert refused.state == "input_invalid", refused
    ambiguous = resolve(f"leads.opportunity:{a.hex[-8:]}", intake.ctx)
    assert isinstance(ambiguous, Unavailable)
    assert ambiguous.reason == "reference_ambiguous"
