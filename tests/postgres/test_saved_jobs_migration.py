"""Migration 0023: the Job search preset and the job mappings learn ``saved``.

A populated workspace is rebuilt at 0022 (a Job search pipeline with an
opportunity on preset v4, a job feed connection on a version-2 job mapping that
already reads ``invited``) and upgraded through the audited operator path
production uses.
"""

from typing import Any
from uuid import UUID

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
REVISION = "0023_saved_jobs"
HEAD = REVISION
PREVIOUS = "0022_reopen_closed_jobs"
FIELD = "ext.jobsearch.saved"


def rewound(i: Intake) -> tuple[str, ...]:
    """Rebuild the workspace at 0022 and forget the undone revisions."""
    with open_unit_of_work(i.ctx) as uow:
        undone = rewind_leads(uow.connection, PREVIOUS)
        uow.connection.execute(
            delete(module_schema_version).where(
                module_schema_version.c.module_id == "leads",
                module_schema_version.c.schema_version.in_(undone),
            )
        )
        uow.commit()
    return undone


def at_v2(i: Intake, name: str, transport: str) -> UUID:
    """A connection on a job mapping at version 2 (version 1 plus ``invited``), as
    0018 left it."""
    connection_id = job_mapping(i, name, transport)
    rewind_rule = {"target": "ext.jobsearch.invited", "source": "/invited"}
    with open_unit_of_work(i.ctx) as uow:
        c = uow.connection
        (conn,) = rows(c, t.intake_connection, id=connection_id)
        mapping_id = conn["mapping_id"]
        for table, column in (
            (t.field_mapping, "id"),
            (t.field_mapping_identity, "mapping_id"),
            (t.field_mapping_rule, "mapping_id"),
        ):
            for row in rows(c, table, **{column: mapping_id, "version": 1}):
                c.execute(table.insert().values({**row, "version": 2}))
        c.execute(
            t.field_mapping_rule.insert().values(
                mapping_id=mapping_id,
                version=2,
                target=rewind_rule["target"],
                source_path=rewind_rule["source"],
                required=False,
                clear_on_null=False,
            )
        )
        c.execute(
            t.intake_connection.update()
            .where(t.intake_connection.c.id == connection_id)
            .values(mapping_version=2)
        )
        uow.commit()
    return connection_id


def test_a_populated_0022_moves_to_preset_v5_and_the_next_mapping(
    intake: Intake,
) -> None:
    setup(intake)
    job_pipeline(intake)
    capture(intake, "job", {"subject": "Example job", "posting_id": "~0296"})
    assert rewound(intake) == (HEAD,)
    feed = at_v2(intake, "job_feed", "poll")
    pasted = at_v2(intake, "job_paste", "manual")
    with open_unit_of_work(intake.ctx) as uow:
        c = uow.connection
        # Positive control: the predecessor is on preset v4 and mapping v2.
        (preset,) = rows(c, p.pipeline_preset, id=PRESET)
        assert preset["current_version"] == 4
        (job,) = rows(c, p.opportunity, preset_id=PRESET)
        assert job["preset_version"] == 4
        states_before = rows(c, p.opportunity_field_state, opportunity_id=job["id"])
        v4_before = preset_rows(c, 4)
        connections_before = {
            r["id"]: (r["mapping_id"], r["mapping_version"])
            for r in rows(c, t.intake_connection)
        }
        rules_before = rows(c, t.field_mapping_rule)
        uow.commit()
    assert connections_before[feed][1] == connections_before[pasted][1] == 2

    result = upgrade(intake, target_schema=HEAD)
    assert result.ok and result.result.changed, result

    with open_unit_of_work(intake.ctx) as uow:
        c = uow.connection
        (preset,) = rows(c, p.pipeline_preset, id=PRESET)
        assert preset["current_version"] == 5
        (moved,) = rows(c, p.opportunity, id=job["id"])
        assert moved["preset_version"] == 5
        assert {k: v for k, v in moved.items() if k != "preset_version"} == {
            k: v for k, v in job.items() if k != "preset_version"
        }
        states_after = rows(c, p.opportunity_field_state, opportunity_id=job["id"])
        assert states_after == states_before
        assert not any(s["orphaned"] for s in states_after)
        # Version 4 is untouched; version 5 is a copy of it plus one field.
        assert preset_rows(c, 4) == v4_before
        v5 = preset_rows(c, 5)
        added = [r for r in v5["preset_field"] if r[1] == FIELD]
        assert added == [(PRESET, FIELD, "boolean", False, "internal", [])]
        v5["preset_field"] = [r for r in v5["preset_field"] if r[1] != FIELD]
        assert v5 == v4_before
        connections = {
            r["id"]: (r["mapping_id"], r["mapping_version"])
            for r in rows(c, t.intake_connection)
        }
        mapping_id, _ = connections_before[feed]
        assert connections[feed] == (mapping_id, 3)
        assert connections[pasted] == (connections_before[pasted][0], 3)
        moved_ids = {feed, pasted}
        assert {k: v for k, v in connections.items() if k not in moved_ids} == {
            k: v for k, v in connections_before.items() if k not in moved_ids
        }
        v2_rules = rows(c, t.field_mapping_rule, mapping_id=mapping_id, version=2)
        v3_rules = rows(c, t.field_mapping_rule, mapping_id=mapping_id, version=3)
        assert sorted(r["target"] for r in v3_rules) == sorted(
            [r["target"] for r in v2_rules] + [FIELD]
        )
        (rule,) = [r for r in v3_rules if r["target"] == FIELD]
        assert (rule["source_path"], rule["required"], rule["clear_on_null"]) == (
            "/saved",
            False,
            False,
        )
        assert [r for r in rows(c, t.field_mapping_rule) if r["version"] < 3] == [
            r for r in rules_before if r["version"] < 3
        ]

    # A second upgrade finds nothing to do.
    again = upgrade(intake, target_schema=HEAD)
    assert again.ok and not again.result.changed, again


def test_the_migration_is_declared_non_destructive(intake: Intake) -> None:
    from alembic.script import ScriptDirectory
    from rheo_core.migrations.orchestrator import build_config

    revision = ScriptDirectory.from_config(build_config("leads")).get_revision(REVISION)
    assert revision is not None and revision.down_revision == PREVIOUS
    assert revision.module.non_destructive_upgrade is True


def _states(c: Any, title: str) -> dict[str, str]:
    (job,) = rows(c, p.opportunity, title=title)
    assert (job["preset_id"], job["preset_version"]) == (PRESET, 5)
    return {
        r["target"]: r["value_text"]
        for r in rows(c, p.opportunity_field_state, opportunity_id=job["id"])
    }


def test_a_migrated_feed_connection_stores_saved(
    intake: Intake,
) -> None:
    """The production path end to end: a connection on a migrated mapping accepts
    a saved delivery, and its opportunity, on preset v5, stores the flag."""
    setup(intake)
    pipeline = job_pipeline(intake)
    rewound(intake)
    feed = at_v2(intake, "job_feed", "poll")
    assert upgrade(intake, target_schema=HEAD).ok
    ok(
        intake,
        "connection.set_routing",
        connection_id=str(feed),
        rules=[{"action": "create_opportunity", "pipeline_id": pipeline}],
    )
    body = {
        "title": "Saved example",
        "posting_id": "~0297",
        "saved": "true",
        "observed_at": "2026-11-16T18:00:00+00:00",
    }
    accepted = deliver(poll_context(intake, feed), intake, body, "saved-~0297")
    assert accepted.ok and accepted.result.outcome == "accepted", accepted
    drain(intake)
    with open_unit_of_work(intake.ctx) as uow:
        states = _states(uow.connection, "Saved example")
    assert states[FIELD] == "true"
