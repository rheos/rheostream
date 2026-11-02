"""Migration 0018: the Job search preset and the job mappings learn ``invited``.

A populated workspace is rebuilt at 0017 (a Job search pipeline with an
opportunity, a job feed connection on a version-1 job mapping, the seeded manual
connection) and upgraded through the audited operator path production uses.
"""

from typing import Any
from uuid import UUID

import pytest
from harness.jobfeed import deliver, drain, poll_context
from rheo_core.refs import uuid7
from rheo_core.storage.core_tables import module_schema_version
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads import pipeline_common as h
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t
from sqlalchemy import Connection, delete, insert, select, update

from postgres.test_jobfeed_migration import connector
from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake
from postgres.test_leads_pipeline import capture, ok, rewind_leads, setup
from postgres.test_leads_queue import job_pipeline
from postgres.test_module_upgrade import upgrade

pytestmark = pytest.mark.postgres
REVISION = "0018_invited_jobs"
# The package head the upgrade moves to; 0020 follows this file's revision.
HEAD = "0020_current_handoff"
PREVIOUS = "0017_proposal_separate_answers"
FIELD = "ext.jobsearch.invited"
PRESET = h.JOBSEARCH_PRESET_ID


def job_mapping(i: Intake, name: str, transport: str) -> UUID:
    """A connection on a version-1 job mapping named ``name``, as the collector or
    the job paste installed it before 0018; its connection id."""
    connection_id = connector(i, transport, f"fixture-{name}")
    mapping_id = uuid7()
    with open_unit_of_work(i.ctx) as uow:
        c = uow.connection
        c.execute(
            insert(t.field_mapping).values(
                id=mapping_id,
                version=1,
                name=name,
                source_kind="json",
                state="active",
            )
        )
        c.execute(
            insert(t.field_mapping_identity).values(
                mapping_id=mapping_id, version=1, occurred_at_path="/observed_at"
            )
        )
        for target, source in (
            ("subject", "/title"),
            ("ext.jobsearch.posting_id", "/posting_id"),
            ("ext.jobsearch.applied", "/applied"),
        ):
            c.execute(
                insert(t.field_mapping_rule).values(
                    mapping_id=mapping_id,
                    version=1,
                    target=target,
                    source_path=source,
                    required=target == "subject",
                    clear_on_null=False,
                )
            )
        c.execute(
            update(t.intake_connection)
            .where(t.intake_connection.c.id == connection_id)
            .values(mapping_id=mapping_id, mapping_version=1)
        )
        uow.commit()
    return connection_id


def rows(c: Connection, table: Any, **where: Any) -> list[dict[str, Any]]:
    query = select(table)
    for name, value in where.items():
        query = query.where(table.c[name] == value)
    return [dict(r) for r in c.execute(query).mappings()]


def preset_rows(c: Connection, version: int) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of the preset's version, the version column left out."""
    found = {}
    for table in (
        p.preset_stage,
        p.preset_transition,
        p.preset_requirement,
        p.preset_field,
        p.preset_rubric,
        p.preset_template,
    ):
        found[table.name] = sorted(
            tuple(v for k, v in row.items() if k != "version")
            for row in rows(c, table, preset_id=PRESET, version=version)
        )
    return found


def test_a_populated_0017_moves_to_preset_v2_and_mapping_v2(intake: Intake) -> None:
    setup(intake)
    job_pipeline(intake)
    capture(intake, "job", {"subject": "Example job", "posting_id": "~0290"})
    feed = job_mapping(intake, "job_feed", "poll")
    pasted = job_mapping(intake, "job_paste", "manual")
    with open_unit_of_work(intake.ctx) as uow:
        undone = rewind_leads(uow.connection, PREVIOUS)
        assert undone == (HEAD, REVISION)
        uow.connection.execute(
            delete(module_schema_version).where(
                module_schema_version.c.module_id == "leads",
                module_schema_version.c.schema_version.in_(undone),
            )
        )
        c = uow.connection
        # Positive control: the predecessor has one preset version and v1 mappings.
        (preset,) = rows(c, p.pipeline_preset, id=PRESET)
        assert preset["current_version"] == 1
        (job,) = rows(c, p.opportunity, preset_id=PRESET)
        assert job["preset_version"] == 1
        states_before = rows(c, p.opportunity_field_state, opportunity_id=job["id"])
        v1_before = preset_rows(c, 1)
        connections_before = {
            r["id"]: (r["mapping_id"], r["mapping_version"])
            for r in rows(c, t.intake_connection)
        }
        rules_before = rows(c, t.field_mapping_rule)
        uow.commit()
    assert connections_before[feed][1] == connections_before[pasted][1] == 1

    result = upgrade(intake, target_schema=HEAD)
    assert result.ok and result.result.changed, result

    with open_unit_of_work(intake.ctx) as uow:
        c = uow.connection
        (preset,) = rows(c, p.pipeline_preset, id=PRESET)
        assert preset["current_version"] == 2
        (moved,) = rows(c, p.opportunity, id=job["id"])
        assert moved["preset_version"] == 2
        assert {k: v for k, v in moved.items() if k != "preset_version"} == {
            k: v for k, v in job.items() if k != "preset_version"
        }
        states_after = rows(c, p.opportunity_field_state, opportunity_id=job["id"])
        assert states_after == states_before
        assert not any(s["orphaned"] for s in states_after)
        # Version 1 is untouched; version 2 is a copy of it plus one field.
        assert preset_rows(c, 1) == v1_before
        v2 = preset_rows(c, 2)
        added = [r for r in v2["preset_field"] if r[1] == FIELD]
        assert added == [(PRESET, FIELD, "boolean", False, "internal", [])]
        v2["preset_field"] = [r for r in v2["preset_field"] if r[1] != FIELD]
        assert v2 == v1_before
        # The job feed connection moved to its mapping's version 2, a copy of
        # version 1 plus the rule; every other connection stayed.
        connections = {
            r["id"]: (r["mapping_id"], r["mapping_version"])
            for r in rows(c, t.intake_connection)
        }
        mapping_id, _ = connections_before[feed]
        assert connections[feed] == (mapping_id, 2)
        assert connections[pasted] == (connections_before[pasted][0], 2)
        moved_ids = {feed, pasted}
        assert {k: v for k, v in connections.items() if k not in moved_ids} == {
            k: v for k, v in connections_before.items() if k not in moved_ids
        }
        v1_rules = rows(c, t.field_mapping_rule, mapping_id=mapping_id, version=1)
        v2_rules = rows(c, t.field_mapping_rule, mapping_id=mapping_id, version=2)
        assert len(v1_rules) == 3
        assert sorted(r["target"] for r in v2_rules) == sorted(
            [r["target"] for r in v1_rules] + [FIELD]
        )
        (rule,) = [r for r in v2_rules if r["target"] == FIELD]
        assert (rule["source_path"], rule["required"], rule["clear_on_null"]) == (
            "/invited",
            False,
            False,
        )
        assert [r for r in rows(c, t.field_mapping_rule) if r["version"] == 1] == [
            r for r in rules_before if r["version"] == 1
        ]
        (mapping,) = rows(c, t.field_mapping, id=mapping_id, version=2)
        assert (mapping["name"], mapping["state"]) == ("job_feed", "active")
        assert rows(c, t.field_mapping_identity, mapping_id=mapping_id, version=2)


def test_the_migration_is_declared_non_destructive(intake: Intake) -> None:
    from alembic.script import ScriptDirectory
    from rheo_core.migrations.orchestrator import build_config

    revision = ScriptDirectory.from_config(build_config("leads")).get_revision(REVISION)
    assert revision is not None and revision.down_revision == PREVIOUS
    assert revision.module.non_destructive_upgrade is True


def test_a_migrated_feed_connection_delivers_an_invited_job(intake: Intake) -> None:
    """The production path end to end: a connection on a migrated mapping accepts
    an invited delivery, and its opportunity, on preset v2, stores the flag."""
    setup(intake)
    pipeline = job_pipeline(intake)
    feed = job_mapping(intake, "job_feed", "poll")
    with open_unit_of_work(intake.ctx) as uow:
        undone = rewind_leads(uow.connection, PREVIOUS)
        uow.connection.execute(
            delete(module_schema_version).where(
                module_schema_version.c.module_id == "leads",
                module_schema_version.c.schema_version.in_(undone),
            )
        )
        uow.commit()
    assert upgrade(intake, target_schema=HEAD).ok
    ok(
        intake,
        "connection.set_routing",
        connection_id=str(feed),
        rules=[{"action": "create_opportunity", "pipeline_id": pipeline}],
    )
    body = {
        "title": "Invited example",
        "posting_id": "~0295",
        "invited": "true",
        "observed_at": "2026-10-22T18:00:00+00:00",
    }
    accepted = deliver(poll_context(intake, feed), intake, body, "invite-~0295")
    assert accepted.ok and accepted.result.outcome == "accepted", accepted
    drain(intake)
    with open_unit_of_work(intake.ctx) as uow:
        c = uow.connection
        (job,) = rows(c, p.opportunity, title="Invited example")
        assert (job["preset_id"], job["preset_version"]) == (PRESET, 2)
        states = {
            r["target"]: r["value_text"]
            for r in rows(c, p.opportunity_field_state, opportunity_id=job["id"])
        }
    assert states[FIELD] == "true"
    assert states["ext.jobsearch.posting_id"] == "~0295"
