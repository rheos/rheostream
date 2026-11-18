"""Migration 0021: the Job search preset and the job mappings learn the stated
price (#503).

A populated workspace is rebuilt at 0020 (a Job search pipeline with an
opportunity, job feed and job paste connections on version-1 job mappings, the
seeded manual connection) and upgraded through the audited operator path
production uses.
"""

import pytest
from harness.jobfeed import deliver, drain, poll_context
from rheo_core.storage.core_tables import module_schema_version
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t
from sqlalchemy import delete

from postgres.test_invited_jobs_migration import PRESET, job_mapping, preset_rows, rows
from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake
from postgres.test_leads_pipeline import capture, ok, rewind_leads, setup
from postgres.test_leads_queue import job_pipeline
from postgres.test_module_upgrade import upgrade

pytestmark = pytest.mark.postgres
REVISION = "0021_stated_price"
HEAD = REVISION
PREVIOUS = "0020_current_handoff"
FIELDS = {
    "ext.jobsearch.stated_price_min": ("decimal", [], "/stated_price_min"),
    "ext.jobsearch.stated_price_max": ("decimal", [], "/stated_price_max"),
    "ext.jobsearch.stated_price_unit": (
        "enum",
        ["hour", "total", "item"],
        "/stated_price_unit",
    ),
}


def rewind(i: Intake) -> None:
    with open_unit_of_work(i.ctx) as uow:
        undone = rewind_leads(uow.connection, PREVIOUS)
        assert undone == (REVISION,)
        uow.connection.execute(
            delete(module_schema_version).where(
                module_schema_version.c.module_id == "leads",
                module_schema_version.c.schema_version.in_(undone),
            )
        )
        uow.commit()


def test_a_populated_0020_moves_to_preset_v3_and_new_mappings(intake: Intake) -> None:
    setup(intake)
    job_pipeline(intake)
    capture(intake, "job", {"subject": "Example job", "posting_id": "~0390"})
    feed = job_mapping(intake, "job_feed", "poll")
    pasted = job_mapping(intake, "job_paste", "manual")
    rewind(intake)
    with open_unit_of_work(intake.ctx) as uow:
        c = uow.connection
        # Positive control: the predecessor's current preset version is 2.
        (preset,) = rows(c, p.pipeline_preset, id=PRESET)
        assert preset["current_version"] == 2
        (job,) = rows(c, p.opportunity, preset_id=PRESET)
        assert job["preset_version"] == 2
        states_before = rows(c, p.opportunity_field_state, opportunity_id=job["id"])
        v2_before = preset_rows(c, 2)
        connections_before = {
            r["id"]: (r["mapping_id"], r["mapping_version"])
            for r in rows(c, t.intake_connection)
        }
        rules_before = rows(c, t.field_mapping_rule)
    assert connections_before[feed][1] == connections_before[pasted][1] == 1

    result = upgrade(intake, target_schema=HEAD)
    assert result.ok and result.result.changed, result

    with open_unit_of_work(intake.ctx) as uow:
        c = uow.connection
        (preset,) = rows(c, p.pipeline_preset, id=PRESET)
        assert preset["current_version"] == 3
        (moved,) = rows(c, p.opportunity, id=job["id"])
        assert moved["preset_version"] == 3
        assert {k: v for k, v in moved.items() if k != "preset_version"} == {
            k: v for k, v in job.items() if k != "preset_version"
        }
        # No stored job gains a value: the price is read from new jobs only.
        assert rows(c, p.opportunity_field_state, opportunity_id=job["id"]) == (
            states_before
        )
        # Version 2 is untouched; version 3 is a copy of it plus three fields.
        assert preset_rows(c, 2) == v2_before
        v3 = preset_rows(c, 3)
        added = sorted(r for r in v3["preset_field"] if r[1] in FIELDS)
        assert added == sorted(
            (PRESET, target, kind, False, "internal", values)
            for target, (kind, values, _) in FIELDS.items()
        )
        v3["preset_field"] = [r for r in v3["preset_field"] if r[1] not in FIELDS]
        assert v3 == v2_before
        # Both job connections moved to a new mapping version carrying the rules;
        # every other connection stayed, and old rule versions are untouched.
        connections = {
            r["id"]: (r["mapping_id"], r["mapping_version"])
            for r in rows(c, t.intake_connection)
        }
        for connection in (feed, pasted):
            mapping_id, before = connections_before[connection]
            assert connections[connection] == (mapping_id, before + 1)
            old = rows(c, t.field_mapping_rule, mapping_id=mapping_id, version=before)
            new = rows(
                c, t.field_mapping_rule, mapping_id=mapping_id, version=before + 1
            )
            assert sorted(r["target"] for r in new) == sorted(
                [r["target"] for r in old] + list(FIELDS)
            )
            for rule in new:
                if rule["target"] in FIELDS:
                    assert (
                        rule["source_path"],
                        rule["required"],
                        rule["clear_on_null"],
                    ) == (FIELDS[rule["target"]][2], False, False)
        moved_ids = {feed, pasted}
        assert {k: v for k, v in connections.items() if k not in moved_ids} == {
            k: v for k, v in connections_before.items() if k not in moved_ids
        }
        assert sorted(
            (r["mapping_id"], r["version"], r["target"])
            for r in rows(c, t.field_mapping_rule)
            if (r["mapping_id"], r["version"]) in {connections_before[feed]}
        ) == sorted(
            (r["mapping_id"], r["version"], r["target"])
            for r in rules_before
            if (r["mapping_id"], r["version"]) in {connections_before[feed]}
        )

    # Running it again on an upgraded workspace changes nothing.
    again = upgrade(intake, target_schema=HEAD)
    assert again.ok


def test_the_migration_is_declared_non_destructive(intake: Intake) -> None:
    from alembic.script import ScriptDirectory
    from rheo_core.migrations.orchestrator import build_config

    revision = ScriptDirectory.from_config(build_config("leads")).get_revision(REVISION)
    assert revision is not None and revision.down_revision == PREVIOUS
    assert revision.module.non_destructive_upgrade is True


def test_a_migrated_feed_connection_stores_a_stated_price(intake: Intake) -> None:
    """The production path end to end: a connection on a migrated mapping accepts
    a delivery with a stated price, and its opportunity, on preset v3, stores it
    apart from the budget fields."""
    setup(intake)
    pipeline = job_pipeline(intake)
    feed = job_mapping(intake, "job_feed", "poll")
    rewind(intake)
    assert upgrade(intake, target_schema=HEAD).ok
    ok(
        intake,
        "connection.set_routing",
        connection_id=str(feed),
        rules=[{"action": "create_opportunity", "pipeline_id": pipeline}],
    )
    body = {
        "title": "Stated price example",
        "posting_id": "~0395",
        "stated_price_min": "3",
        "stated_price_unit": "item",
        "observed_at": "2026-11-18T18:00:00+00:00",
    }
    accepted = deliver(poll_context(intake, feed), intake, body, "stated-~0395")
    assert accepted.ok and accepted.result.outcome == "accepted", accepted
    drain(intake)
    with open_unit_of_work(intake.ctx) as uow:
        c = uow.connection
        (job,) = rows(c, p.opportunity, title="Stated price example")
        assert (job["preset_id"], job["preset_version"]) == (PRESET, 3)
        states = {
            r["target"]: r["value_text"]
            for r in rows(c, p.opportunity_field_state, opportunity_id=job["id"])
        }
    assert states["ext.jobsearch.stated_price_min"] == "3"
    assert states["ext.jobsearch.stated_price_unit"] == "item"
    assert "ext.jobsearch.budget_min" not in states
