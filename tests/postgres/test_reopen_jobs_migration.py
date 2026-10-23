"""Migration 0019: a passed or lost job reopens to Review.

A populated workspace is rebuilt at 0018 (a Job search pipeline with an open job and
a passed one) and upgraded through the audited operator path production uses. Then,
at head, a dismissed job is reopened through the ordinary transition operation, and
the queue says where an undo goes.
"""

from typing import Any

import pytest
from rheo_core.storage.core_tables import module_schema_version
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_tables import audit_record
from rheo_leads import pipeline_common as h
from rheo_leads.storage import pipeline as p
from sqlalchemy import Connection, delete, select

from postgres.test_invited_jobs_migration import preset_rows, rows
from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake
from postgres.test_leads_pipeline import capture, ok, rewind_leads, setup
from postgres.test_leads_queue import cards, job_pipeline
from postgres.test_module_upgrade import upgrade

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.parametrize("intake", [False], indirect=True),
]
REVISION = "0019_reopen_closed_jobs"
PREVIOUS = "0018_invited_jobs"
PRESET = h.JOBSEARCH_PRESET_ID
REOPEN = sorted(
    (PRESET, start, end)
    for start, end in (
        ("passed", "review"),
        ("passed", "qualified"),
        ("lost", "review"),
        ("lost", "qualified"),
        ("lost", "applied"),
        ("lost", "interviewing"),
    )
)


def move(i: Intake, card: dict[str, Any], to: str) -> Any:
    return i.call(
        "leads.opportunity.transition",
        ref=card["ref"],
        revision=card["revision"],
        to_stage_id=to,
    )


def transitions(c: Connection, version: int) -> list[tuple[Any, ...]]:
    return preset_rows(c, version)["preset_transition"]


def test_a_populated_0018_moves_to_preset_v3_with_the_reopen_moves(
    intake: Intake,
) -> None:
    setup(intake)
    job_pipeline(intake)
    capture(intake, "open", {"subject": "Open posting"})
    capture(intake, "passed", {"subject": "Passed posting"})
    assert move(intake, cards(intake)["Passed posting"], "passed").ok
    with open_unit_of_work(intake.ctx) as uow:
        undone = rewind_leads(uow.connection, PREVIOUS)
        assert undone == (REVISION,)
        uow.connection.execute(
            delete(module_schema_version).where(
                module_schema_version.c.module_id == "leads",
                module_schema_version.c.schema_version.in_(undone),
            )
        )
        c = uow.connection
        # Positive control: the predecessor's current version is 2, with no move
        # out of a closed stage.
        (preset,) = rows(c, p.pipeline_preset, id=PRESET)
        assert preset["current_version"] == 2
        jobs = {r["title"]: r for r in rows(c, p.opportunity, preset_id=PRESET)}
        assert {r["preset_version"] for r in jobs.values()} == {2}
        assert jobs["Passed posting"]["disposition_outcome"] == "disqualified"
        v2_before = preset_rows(c, 2)
        assert not [r for r in transitions(c, 2) if r[1] in {"passed", "lost"}]
        uow.commit()

    result = upgrade(intake, target_schema=REVISION)
    assert result.ok and result.result.changed, result

    with open_unit_of_work(intake.ctx) as uow:
        c = uow.connection
        (preset,) = rows(c, p.pipeline_preset, id=PRESET)
        assert preset["current_version"] == 3
        for title, before in jobs.items():
            (after,) = rows(c, p.opportunity, id=before["id"])
            assert after["preset_version"] == 3, title
            # Stage, disposition and revision are kept: nothing an owner sees moves.
            assert {k: v for k, v in after.items() if k != "preset_version"} == {
                k: v for k, v in before.items() if k != "preset_version"
            }
        # Version 2 is untouched; version 3 is a copy of it plus the two moves.
        assert preset_rows(c, 2) == v2_before
        v3 = preset_rows(c, 3)
        added = sorted(
            set(v3["preset_transition"]) - set(v2_before["preset_transition"])
        )
        assert added == REOPEN
        v3["preset_transition"] = [r for r in v3["preset_transition"] if r not in added]
        assert v3 == v2_before


def test_the_migration_is_declared_non_destructive(intake: Intake) -> None:
    from alembic.script import ScriptDirectory
    from rheo_core.migrations.orchestrator import build_config

    revision = ScriptDirectory.from_config(build_config("leads")).get_revision(REVISION)
    assert revision is not None and revision.down_revision == PREVIOUS
    assert revision.module.non_destructive_upgrade is True


def test_a_dismissed_job_reopens_through_an_audited_transition(intake: Intake) -> None:
    setup(intake)
    job_pipeline(intake)
    capture(intake, "a", {"subject": "Example posting"})
    card = cards(intake)["Example posting"]
    assert card["dismiss"] == {
        "stage_id": "passed",
        "label": "Passed",
        "undo": {"stage_id": "review", "label": "Review"},
    }
    dismissed = move(intake, card, "passed")
    assert dismissed.ok, dismissed
    closed = ok(intake, "opportunity.get", ref=card["ref"])
    assert closed.data["disposition_outcome"] == "disqualified"
    assert closed.data["disposition_at"] is not None

    # A closed card offers only the way back; Dismiss is gone.
    shown = cards(intake, ref=card["ref"])["Example posting"]
    assert shown["stage_kind"] == "terminal" and shown["dismiss"] is None
    assert shown["transitions"] == [
        {"stage_id": "review", "label": "Review", "kind": "open"},
        {"stage_id": "qualified", "label": "Qualified", "kind": "open"},
    ]
    # The reopen is revision-checked and only to the stage the preset allows.
    assert move(intake, card, "review").state == "record_stale"
    closed_card = {**card, "revision": closed.revision}
    assert move(intake, closed_card, "applied").state == "transition_not_allowed"

    reopened = move(intake, closed_card, "review")
    assert reopened.ok, reopened
    after = ok(intake, "opportunity.get", ref=card["ref"])
    assert after.data["stage_id"] == "review"
    assert after.data["disposition_outcome"] is None
    assert after.data["disposition_at"] is None
    assert after.revision == closed.revision + 1
    assert set(cards(intake)) == {"Example posting"}
    with open_unit_of_work(intake.ctx) as uow:
        audited = (
            uow.connection.execute(
                select(audit_record.c.outcome)
                .where(audit_record.c.operation_name == "leads.opportunity.transition")
                .order_by(audit_record.c.occurred_at, audit_record.c.id)
            )
            .scalars()
            .all()
        )
    # The dismiss, the two refusals and the reopen are all on the record.
    assert len(audited) == 4 and audited[0] == audited[-1], audited


def test_lost_reopens_and_hired_stays_closed(intake: Intake) -> None:
    setup(intake)
    job_pipeline(intake)
    capture(intake, "a", {"subject": "Applied posting"})
    capture(intake, "b", {"subject": "Hired posting"})
    for title, path in (
        ("Applied posting", ("qualified", "applied")),
        ("Hired posting", ("qualified", "applied", "hired")),
    ):
        card = cards(intake)[title]
        for stage in path:
            moved = move(intake, card, stage)
            assert moved.ok, moved
            card = {**card, "revision": moved.result.revision}
    # From Applied, a dismiss is a loss, and its undo goes back to Applied.
    applied = cards(intake)["Applied posting"]
    assert applied["dismiss"] == {
        "stage_id": "lost",
        "label": "Lost",
        "undo": {"stage_id": "applied", "label": "Applied"},
    }
    lost = move(intake, applied, "lost")
    assert lost.ok, lost
    back = move(intake, {**applied, "revision": lost.result.revision}, "applied")
    assert back.ok, back
    assert (
        ok(intake, "opportunity.get", ref=applied["ref"]).data["stage_id"] == "applied"
    )
    hired = cards(intake, stage_id="hired")["Hired posting"]
    assert hired["transitions"] == []
    assert move(intake, hired, "review").state == "transition_not_allowed"


def test_a_service_dismiss_has_no_undo(intake: Intake) -> None:
    setup(intake)
    capture(
        intake, "service", {"person.email": "sample@example.com", "subject": "Service"}
    )
    assert cards(intake)["Service"]["dismiss"]["undo"] is None


def test_preset_edit_accepts_a_reopen_and_refuses_closed_to_closed(
    intake: Intake,
) -> None:
    setup(intake)
    with open_unit_of_work(intake.ctx) as uow:
        c = uow.connection
        stages = [
            {k: r[k] for k in ("stage_id", "label", "kind", "outcome")}
            for r in c.execute(
                select(p.preset_stage)
                .where(
                    p.preset_stage.c.version == 1,
                    p.preset_stage.c.preset_id == h.PRESET_ID,
                )
                .order_by(p.preset_stage.c.ordinal)
            ).mappings()
        ]
        moves = [
            (r["from_stage_id"], r["to_stage_id"])
            for r in c.execute(
                select(p.preset_transition).where(
                    p.preset_transition.c.preset_id == h.PRESET_ID,
                    p.preset_transition.c.version == 1,
                )
            ).mappings()
        ]
    closed = [s["stage_id"] for s in stages if s["kind"] == "terminal"]
    entry = next(s["stage_id"] for s in stages if s["kind"] == "open")
    edit = dict(preset_id=str(h.PRESET_ID), requirements={}, fields=[])
    refused = intake.call(
        "leads.preset.edit",
        **edit,
        from_version=1,
        stages=stages,
        transitions=[*moves, (closed[0], closed[1])],
    )
    assert refused.state == "preset_invalid"
    ok(
        intake,
        "preset.edit",
        **edit,
        from_version=1,
        stages=stages,
        transitions=[*moves, (closed[0], entry)],
    )
