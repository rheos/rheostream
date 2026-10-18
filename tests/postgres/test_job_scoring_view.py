"""The Scoring view (#423): the model call log and the stage of each recent job.

Each sweep runs through the real one-shot runner and a scripted adapter, so the call
rows are written by production code. Every posting and answer is synthetic.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from harness.jobfeed import call
from harness.jobfeed import collector as collector
from harness.registry import add_member
from rheo_contracts import (
    FailureKind,
    FinalOutputEvent,
    Role,
    UsageEvent,
    UsageKind,
    WorkspaceContext,
)
from rheo_core.boundary import context_for_harness
from rheo_core.operations import dispatch
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads.scoring import job as scoring
from rheo_leads.storage import scoring as s
from sqlalchemy import select

from postgres.test_job_scoring import FULL, Scoring, answer, failure, final
from postgres.test_job_scoring import scored as scored
from postgres.test_jobfeed_delivery import rows, the_opportunity
from postgres.test_leads_intake import intake as intake

pytestmark = pytest.mark.postgres

UUID_A = UUID("019bf0b0-0000-7000-8000-00000000c0a1")
UUID_B = UUID("019bf0b0-0000-7000-8000-00000000c0a2")

GOOD = FULL.format(title="Good job", what="pipeline", activity="")
CROWDED = FULL.format(title="Crowded job", what="app", activity="Proposals: 50+")
# Token counts the adapter reports; the cost figure must not reach the log.
USAGE = UsageEvent(input_tokens=1200, output_tokens=345, cost=0.5, kind=UsageKind.EXACT)


def view(scored: Scoring, ctx: WorkspaceContext | None = None) -> dict[str, Any]:
    result = call(scored.i, ctx or scored.c.owner, "leads.ui.scoring")
    data: dict[str, Any] = result.data
    return data


def jobs(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {j["title"]: j for b in data["batches"] for j in b["jobs"]}


def calls(scored: Scoring) -> list[dict[str, Any]]:
    return rows(scored.i, select(s.model_call).order_by(s.model_call.c.started_at))


def test_a_scored_call_is_logged_with_its_tokens_and_the_job_reads_scored(
    scored: Scoring,
) -> None:
    scored.paste("~02100", GOOD)
    scored.paste("~02200", CROWDED)
    scored.model([USAGE, FinalOutputEvent(structured=answer(8))])
    scored.set_profile()
    scored.sweep()

    # Only the job tier 1 let through reached the model.
    (row,) = calls(scored)
    assert row["purpose"] == "scoring" and row["model_id"] == "default"
    assert row["opportunity_id"] == the_opportunity(scored.i, "~02100")["id"]
    assert (row["input_tokens"], row["output_tokens"]) == (1200, 345)
    assert (row["outcome"], row["failure_kind"]) == ("answered", None)
    assert row["finished_at"] >= row["started_at"]
    # Content-free: no text of the posting or the profile is in the row.
    assert all(
        "example pipeline" not in str(v) and "Profile-marker" not in str(v)
        for v in row.values()
    )

    data = view(scored)
    assert data["status"]["state"] == "idle" and data["status"]["last_call_at"]
    assert data["queued_total"] == 0 and data["failed_total"] == 0
    by_title = jobs(data)
    assert by_title["Good job"]["stage"] == "scored"
    assert by_title["Good job"]["score"] == 80
    assert by_title["Crowded job"]["stage"] == "held"
    assert by_title["Crowded job"]["score"] is None
    (batch,) = data["batches"]
    assert batch["sources"] == ["manual"]
    assert (batch["counts"]["scored"], batch["counts"]["held"]) == (1, 1)
    (logged,) = data["calls"]
    assert logged["title"] == "Good job" and logged["outcome"] == "answered"
    assert (logged["input_tokens"], logged["output_tokens"]) == (1200, 345)
    assert logged["ref"] == by_title["Good job"]["ref"]
    (summary,) = data["summary"]
    assert summary == {
        "purpose": "scoring",
        "model_id": "default",
        "calls": 1,
        "input_tokens": 1200,
        "output_tokens": 345,
        "median_seconds": summary["median_seconds"],
        "failed": 0,
        "deferred": 0,
    }
    assert "cost" not in str(data)


def test_a_usage_limit_is_logged_deferred_and_pauses_the_queue(
    scored: Scoring,
) -> None:
    for posting in ("~02100", "~02101"):
        scored.paste(posting, FULL.format(title=posting, what="pipeline", activity=""))
    scored.model(failure(FailureKind.USAGE_LIMITED))
    scored.set_profile()
    scored.sweep()

    (row,) = calls(scored)
    assert (row["outcome"], row["failure_kind"]) == ("deferred", "usage_limited")
    assert row["input_tokens"] is None
    data = view(scored)
    assert data["status"]["state"] == "paused"
    assert data["status"]["failure_kind"] == "usage_limited"
    assert data["status"]["until"] is not None
    assert {j["stage"] for j in jobs(data).values()} == {"paused"}
    assert data["queued_total"] == 0 and data["waiting_total"] == 2
    assert data["calls"][0]["outcome"] == "deferred"
    assert data["summary"][0]["deferred"] == 1


def test_failures_are_logged_and_an_owner_puts_failed_jobs_back_in_line(
    scored: Scoring,
) -> None:
    scored.paste("~02100", GOOD)
    scored.model(failure(FailureKind.OUTPUT_INVALID))
    scored.set_profile()
    at = datetime.now(UTC)
    scored.sweep(at)
    first = view(scored)
    assert jobs(first)["Good job"]["stage"] == "retrying"
    assert jobs(first)["Good job"]["failure_kind"] == "output_invalid"
    for _ in range(scoring.SCORE_ATTEMPTS):
        at = scored.state("~02100")["retry_after"] or at
        at += timedelta(seconds=1)
        scored.sweep(at)
    assert scored.state("~02100")["state"] == "failed"
    logged = calls(scored)
    assert len(logged) == scoring.SCORE_ATTEMPTS
    assert {(r["outcome"], r["failure_kind"]) for r in logged} == {
        ("failed", "output_invalid")
    }
    data = view(scored)
    assert data["failed_total"] == 1
    assert jobs(data)["Good job"]["stage"] == "failed"
    assert data["summary"][0]["failed"] == scoring.SCORE_ATTEMPTS

    # A member sees the view but cannot spend the owner's model time.
    member_id = add_member(
        scored.i.cluster.backend, scored.i.workspace, Role.MEMBER, display_name="m"
    )
    member = context_for_harness(scored.i.workspace, member_id, Role.MEMBER)
    assert isinstance(member, WorkspaceContext)
    assert view(scored, member)["failed_total"] == 1
    refused = dispatch(
        member,
        "leads.jobscoring.retry_failed",
        {},
        registry=scored.i.surfaces.operations,
        consumers=scored.i.consumers,
    )
    assert not refused.ok
    assert scored.state("~02100")["state"] == "failed"

    retried = call(scored.i, scored.c.owner, "leads.jobscoring.retry_failed")
    assert retried.count == 1
    state = scored.state("~02100")
    assert (state["state"], state["attempts"], state["failure_kind"]) == (
        "pending",
        0,
        None,
    )
    again = view(scored)
    assert again["queued_total"] == 1 and again["failed_total"] == 0
    assert jobs(again)["Good job"]["stage"] == "queued"
    assert jobs(again)["Good job"]["position"] == 1
    assert call(scored.i, scored.c.owner, "leads.jobscoring.retry_failed").count == 0

    scored.model(final(7))
    scored.sweep(at + timedelta(hours=1))
    assert jobs(view(scored))["Good job"]["score"] == 70


def test_a_running_call_names_its_job_and_an_old_one_reads_interrupted(
    scored: Scoring,
) -> None:
    scored.paste("~02100", GOOD)
    scored.paste("~02101", FULL.format(title="Next job", what="shop", activity=""))
    scored.set_profile()
    identifier = the_opportunity(scored.i, "~02100")["id"]
    now = datetime.now(UTC)
    # Unseen jobs read as new until a sweep's refresh queues them.
    assert {j["stage"] for j in jobs(view(scored)).values()} == {"untracked"}
    with open_unit_of_work(scored.c.owner) as uow:
        scoring.refresh(uow, cap=50, now=now)
        uow.commit()
    rows_in = [
        {
            "id": UUID_A,
            "purpose": "scoring",
            "opportunity_id": identifier,
            "model_id": "default",
            "started_at": now - timedelta(seconds=30),
            "outcome": "running",
        },
        {
            "id": UUID_B,
            "purpose": "draft",
            "opportunity_id": None,
            "model_id": "default",
            "started_at": now - timedelta(hours=3),
            "outcome": "running",
        },
    ]
    with open_unit_of_work(scored.c.owner) as uow:
        uow.connection.execute(s.model_call.insert(), rows_in)
        uow.commit()
    data = view(scored)
    status = data["status"]
    assert status["state"] == "running" and status["purpose"] == "scoring"
    assert status["title"] == "Good job"
    by_title = jobs(data)
    assert by_title["Good job"]["stage"] == "scoring"
    # The running job is not counted in the queue's positions.
    assert by_title["Next job"]["stage"] == "queued"
    assert by_title["Next job"]["position"] == 1
    outcomes = {c["purpose"]: c["outcome"] for c in data["calls"]}
    assert outcomes == {"scoring": "running", "draft": "interrupted"}


def test_off_and_without_a_profile_the_status_says_so(scored: Scoring) -> None:
    assert view(scored)["status"]["state"] == "no_profile"
    call(
        scored.i,
        scored.c.owner,
        "core.settings.set",
        key="leads.capabilities.jobsearch",
        value=False,
    )
    data = view(scored)
    assert data["status"]["state"] == "off"
    assert data["batches"] == [] and data["calls"] == []
