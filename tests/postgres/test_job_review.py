"""The review queue (run 3c2e): held jobs listed with the clauses that held them, the
owner's Promote override, and what the override survives: rescoring, a moved
posting, a new profile, and an export and restore. Synthetic postings and answers."""

from typing import Any
from uuid import UUID

import pytest
from harness.jobfeed import call
from harness.jobfeed import collector as collector
from harness.registry import add_member
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.operations import dispatch
from rheo_leads.references import ref
from rheo_leads.scoring import job as scoring
from rheo_leads.scoring.job import PROMOTED_LINE
from rheo_leads.storage import scoring as s
from sqlalchemy import select

from postgres.test_job_scoring import FULL, Scoring, final, queued_sweeps
from postgres.test_job_scoring import scored as scored
from postgres.test_jobfeed_delivery import rows, the_opportunity
from postgres.test_leads_intake import intake as intake
from postgres.test_leads_pipeline import export_workspace

pytestmark = pytest.mark.postgres

CROWDED = FULL.format(title="Crowded job", what="app", activity="Proposals: 50+")
GOOD = FULL.format(title="Good job", what="pipeline", activity="")


def review(scored: Scoring) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = call(
        scored.i,
        scored.c.owner,
        "leads.ui.queue",
        pipeline_id=str(scored.c.pipeline_id),
        held_only=True,
    ).items
    return items


def promote(scored: Scoring, posting: str) -> Any:
    reference = ref("opportunity", the_opportunity(scored.i, posting)["id"])
    return call(scored.i, scored.c.owner, "leads.jobreview.promote", ref=reference)


def refused(scored: Scoring, posting: str, ctx: WorkspaceContext | None = None) -> str:
    reference = ref("opportunity", the_opportunity(scored.i, posting)["id"])
    outcome = dispatch(
        ctx or scored.c.owner,
        "leads.jobreview.promote",
        {"ref": reference},
        registry=scored.i.surfaces.operations,
        consumers=scored.i.consumers,
    )
    assert not outcome.ok and outcome.error is not None
    return outcome.error.error_code


def held(scored: Scoring) -> None:
    scored.paste("~02200", CROWDED)
    scored.paste("~02100", GOOD)
    scored.model(final(8), final(6), final(5), final(4))
    scored.set_profile()
    scored.sweep()


def test_the_review_queue_lists_held_jobs_with_their_clauses(scored: Scoring) -> None:
    held(scored)
    (item,) = review(scored)
    assert item["title"] == "Crowded job"
    assert item["hold"] == [
        {"code": "high_proposals", "text": "More than 50 proposals already."}
    ]
    assert item["dismiss"] is not None
    # The ordinary queue does not carry the hold list.
    queue = call(
        scored.i,
        scored.c.owner,
        "leads.ui.queue",
        pipeline_id=str(scored.c.pipeline_id),
    )
    assert all("hold" not in i for i in queue.items) and len(queue.items) == 2
    # Dismissed, it leaves the review list, closed stages asked for or not.
    call(
        scored.i,
        scored.c.owner,
        "leads.opportunity.transition",
        ref=item["ref"],
        revision=item["revision"],
        to_stage_id=item["dismiss"]["stage_id"],
    )
    assert review(scored) == []
    assert (
        call(
            scored.i,
            scored.c.owner,
            "leads.ui.queue",
            pipeline_id=str(scored.c.pipeline_id),
            held_only=True,
            include_closed=True,
        ).items
        == []
    )


def test_promote_lets_the_model_score_a_held_job_and_survives_rescoring(
    scored: Scoring,
) -> None:
    held(scored)
    before = len(queued_sweeps(scored.i))
    first = promote(scored, "~02200")
    assert first.ref == ref("opportunity", the_opportunity(scored.i, "~02200")["id"])
    assert review(scored) == []
    assert scored.state("~02200")["state"] == "pending"
    assert len(queued_sweeps(scored.i)) >= before
    (row,) = rows(scored.i, select(s.job_promotion))
    assert row["promoted_by_kind"] == scored.c.owner.actor.kind.value
    assert row["promoted_by_id"] == scored.c.owner.actor.id
    # Promoting again answers the first promotion.
    assert promote(scored, "~02200").promoted_at == first.promoted_at

    scored.sweep()
    latest = scored.assessments("~02200")[-1]
    assert (latest["fit"], latest["score"]) == ("medium", 60)
    lines = latest["explanation"].splitlines()
    assert PROMOTED_LINE in lines and "More than 50 proposals already." in lines
    assert latest["model_id"] == "claude_cli:default"

    # A moved posting is rescored by the model, not held again.
    scored.paste("~02200", CROWDED + "\n\nWe also need an admin page.")
    scored.sweep()
    assert scored.assessments("~02200")[-1]["score"] == 50
    # A new profile leaves the override and the score in place.
    scored.set_profile(goals=["A changed goal."])
    scored.sweep()
    assert scored.assessments("~02200")[-1]["score"] == 50
    assert len(rows(scored.i, select(s.job_promotion))) == 1
    assert review(scored) == []


def test_only_a_held_job_is_promoted_and_only_by_an_owner(scored: Scoring) -> None:
    held(scored)
    assert refused(scored, "~02100") == "not_held"
    member_id = add_member(
        scored.i.cluster.backend, scored.i.workspace, Role.MEMBER, display_name="m"
    )
    member = context_for_harness(scored.i.workspace, member_id, Role.MEMBER)
    assert isinstance(member, WorkspaceContext)
    assert refused(scored, "~02200", member) != ""
    assert rows(scored.i, select(s.job_promotion)) == []


def test_a_promote_during_a_sweep_is_not_overwritten_by_its_hold(
    scored: Scoring, monkeypatch: pytest.MonkeyPatch
) -> None:
    held(scored)
    # A new profile puts the held job back in line; it is claimed unpromoted.
    scored.set_profile(goals=["A changed goal."])
    original = scoring.assess
    promoted: list[bool] = []

    def assess_then_promote(*args: Any, **kwargs: Any) -> Any:
        outcome = original(*args, **kwargs)
        if not promoted:
            promoted.append(kwargs["promoted"])
            promote(scored, "~02200")
        return outcome

    monkeypatch.setattr(scoring, "assess", assess_then_promote)
    scored.sweep()
    assert promoted == [False]
    # The stale hold was dropped and the promoted job scored in the same sweep.
    latest = scored.assessments("~02200")[-1]
    assert (latest["fit"], latest["score"]) == ("medium", 60)
    assert scored.state("~02200")["state"] == "scored"


def test_a_promotion_is_exported_and_restored(
    scored: Scoring, make_workspace: Any, owner_account_id: UUID
) -> None:
    from postgres.test_memory_export_restore import _remove_workspace, _restore

    held(scored)
    promote(scored, "~02200")
    (before,) = rows(scored.i, select(s.job_promotion))
    artifact = export_workspace(scored.i)
    host = context_for_operator(make_workspace())
    assert isinstance(host, WorkspaceContext)
    _remove_workspace(scored.i.cluster, scored.i.workspace)
    _restore(scored.i.cluster, host, artifact)
    scored.i.ctx = context_for_harness(scored.i.workspace, owner_account_id, Role.OWNER)
    assert isinstance(scored.i.ctx, WorkspaceContext)
    assert rows(scored.i, select(s.job_promotion)) == [before]
