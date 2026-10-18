"""Job scoring (run 3c1): the profile operation, the sweep job through a scripted
model, the usage-limit deferral, retries, rescoring and the queue's score sort.

Jobs arrive the way real ones do, as pasted postings that the intake consumer turns
into opportunities. The model is a scripted adapter behind the real one-shot runner,
so the request, the throwaway directory and the token handling are production code;
only the process is not. Every profile, posting and answer is synthetic.
"""

import logging
import threading
import time
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from harness.extraction import ScriptedAdapter, ScriptedHandle, forbid_real_processes
from harness.jobfeed import Collector, call, collect_kinds, configure_connector
from harness.jobfeed import collector as collector
from rheo_contracts import (
    AdapterSpawn,
    FailureEvent,
    FailureKind,
    FinalOutputEvent,
    RuntimeEvent,
    RuntimeRequest,
)
from rheo_core.runtime.oneshot import OneShotRunner
from rheo_core.runtime.registry import AdapterRegistry
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.storage.work_tables import job, schedule
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from rheo_leads.references import ref
from rheo_leads.scoring import job as scoring
from rheo_leads.scoring.job import SCORE_JOB_KIND, ScorePayload, score_job
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import scoring as s
from sqlalchemy import select, text, update

from postgres.test_jobfeed_delivery import rows, the_opportunity
from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake

pytestmark = pytest.mark.postgres

PROFILE = (
    "Profile-marker-7d41: example career facts for a fictional candidate.\n\n"
    "Portfolio:\n\n"
    "- Example shop: A fictional storefront with a payment flow.\n\n"
    "- Example pipeline: A fictional data pipeline."
)
POLICY = {
    "goals": ["Prefer steady remote work."],
    "exclusions": ["Skip roles centred on an example vendor stack."],
    "known_gaps": ["No production example-platform experience."],
    "platform_notes": {"upwork": "Upwork note for the example candidate."},
}
URL = "https://www.example.com/jobs/Example-posting_{}?ref=x"
FULL = "{title}\n\nBuild a small example {what} for a fictional shop.\n\n{activity}"
SEARCH_TILE = """
<article data-test="JobTile">
  <a data-test="job-tile-title-link"
     href="/jobs/Example-tile_~02300/">Example tile</a>
  <span data-test="job-type-label">Fixed price</span>
  <span data-test="is-fixed-price">Est. budget: $1,500.00</span>
  <div data-test="payment-verified">Verified Payment verified</div>
  <span data-test="proposals-tier">Proposals: 10 to 15</span>
</article>
"""


def answer(score: int) -> dict[str, Any]:
    return {
        "score": score,
        "fit": "strong",
        "economics": "favorable",
        "evidence": ["project:1"],
        "requirements": ["job:1"],
        "gaps": [],
        "conditions": ["scope"],
    }


class Answers(ScriptedAdapter):
    """Each start replays the next scripted run; the last one repeats."""

    def __init__(self, *runs: Sequence[RuntimeEvent]) -> None:
        super().__init__()
        self.runs = [list(run) for run in runs]

    def start(self, request: RuntimeRequest, *, spawn: AdapterSpawn) -> ScriptedHandle:
        self.events = self.runs.pop(0) if len(self.runs) > 1 else list(self.runs[0])
        return super().start(request, spawn=spawn)


def final(score: int) -> list[RuntimeEvent]:
    return [FinalOutputEvent(structured=answer(score))]


def failure(kind: FailureKind) -> list[RuntimeEvent]:
    return [FailureEvent(kind=kind, detail="synthetic")]


@dataclass
class Tokens:
    issued: list[dict[str, Any]] = field(default_factory=list)
    revoked: list[UUID] = field(default_factory=list)

    def issue(self, **kwargs: Any) -> tuple[UUID, str]:
        self.issued.append(kwargs)
        return UUID(int=len(self.issued)), "rheo_rt_synthetic_not_a_token"

    def revoke(self, token_id: UUID) -> None:
        self.revoked.append(token_id)


@dataclass
class Scoring:
    c: Collector
    tokens: Tokens
    adapter: Answers | None = None

    @property
    def i(self) -> Intake:
        return self.c.intake

    def model(self, *runs: Sequence[RuntimeEvent]) -> Answers:
        self.adapter = Answers(*runs)
        registry = AdapterRegistry()
        registry.register("claude_cli", self.adapter)
        runner = OneShotRunner(
            registry, issue_token=self.tokens.issue, revoke_token=self.tokens.revoke
        )
        scoring.HOOKS.runner = lambda: runner
        return self.adapter

    def paste(self, posting: str, text: str) -> None:
        call(
            self.i,
            self.c.owner,
            "leads.jobfeed.paste",
            pipeline_ref=ref("pipeline", self.c.pipeline_id),
            kind="posting",
            content=text,
            posting_url=URL.format(posting),
        )
        self.drain()

    def drain(self) -> None:
        """Process accepted deliveries on the clock the consumer itself reads.

        ``Intake.visit`` runs two seconds ahead of that clock with no job kinds
        registered, so a sweep that fell due in those two seconds was leased and
        failed on the spot, and the next paste queued the same minute again (#404).
        """
        visit_workspace(
            DueWorkspace(workspace_id=self.i.workspace, observed_due_at=None),
            kinds=JobKindRegistry(),
            consumers=self.i.consumers,
            backend=self.i.cluster.backend,
            owner="job-scoring-test",
            clock=lambda: datetime.now(UTC),
            jitter=None,
        )

    def set_profile(self, **policy: Any) -> Any:
        return call(
            self.i,
            self.c.owner,
            "leads.jobprofile.set",
            profile=PROFILE,
            policy={**POLICY, **policy},
        )

    def sweep(self, at: datetime | None = None) -> None:
        """One sweep, run as the worker runs it, at ``at``."""
        when = at or datetime.now(UTC)
        scoring.HOOKS.clock = lambda: when
        with open_unit_of_work(self.c.owner) as uow:
            score_job(
                HandlerUnitOfWork(uow, consumers=self.i.consumers),
                ScorePayload(workspace_id=self.i.workspace),
                Beat(),  # type: ignore[arg-type]
            )
            uow.commit()

    def assessments(self, posting: str) -> list[dict[str, Any]]:
        identifier = the_opportunity(self.i, posting)["id"]
        return rows(
            self.i,
            select(p.qualification)
            .where(p.qualification.c.opportunity_id == identifier)
            .order_by(p.qualification.c.created_at),
        )

    def state(self, posting: str) -> dict[str, Any]:
        identifier = the_opportunity(self.i, posting)["id"]
        (found,) = rows(
            self.i,
            select(s.job_score_state).where(
                s.job_score_state.c.opportunity_id == identifier
            ),
        )
        return found


class Beat:
    """The cancellation token's surface the handler uses, and nothing else."""

    def __init__(self) -> None:
        self.beats = 0

    def checkpoint(self, now: datetime | None = None, *, force: bool = False) -> None:
        self.beats += 1


@pytest.fixture
def scored(
    collector: Collector,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    owner_account_id: UUID,
) -> Iterator[Scoring]:
    configure_connector(monkeypatch, tmp_path, job_hosts="www.example.com,example.com")
    forbid_real_processes(monkeypatch, tmp_path)
    monkeypatch.setenv("RHEO__runtime__claude_cli__credential_kind", "login")
    monkeypatch.setenv(
        "RHEO__runtime__claude_cli__credential_account_id", str(owner_account_id)
    )
    monkeypatch.setattr(scoring, "HOOKS", scoring.Hooks())
    yield Scoring(collector, Tokens())


def queued_sweeps(i: Intake) -> list[dict[str, Any]]:
    return rows(i, select(job).where(job.c.kind == SCORE_JOB_KIND))


def test_the_profile_operation_turns_scoring_on_and_never_echoes_the_text(
    scored: Scoring,
) -> None:
    first = scored.set_profile()
    assert (first.version, first.profile, first.profile_chars) == (
        1,
        None,
        len(PROFILE),
    )
    assert first.policy.tier1.max_proposals == 50
    (row,) = rows(
        scored.i, select(schedule).where(schedule.c.name == "jobsearch_score")
    )
    assert row["enabled"] is True
    assert queued_sweeps(scored.i)
    second = scored.set_profile(goals=["A changed goal."])
    assert second.version == 2
    got = call(scored.i, scored.c.owner, "leads.jobprofile.get")
    assert got.profile == PROFILE and got.policy.goals == ["A changed goal."]


def test_a_sweep_holds_scores_and_asks_for_the_full_posting(
    scored: Scoring, caplog: pytest.LogCaptureFixture
) -> None:
    scored.paste("~02100", FULL.format(title="Good job", what="pipeline", activity=""))
    scored.paste(
        "~02200",
        FULL.format(title="Crowded job", what="app", activity="Proposals: 50+"),
    )
    call(
        scored.i,
        scored.c.owner,
        "leads.jobfeed.paste",
        pipeline_ref=ref("pipeline", scored.c.pipeline_id),
        kind="search_page",
        content=SEARCH_TILE,
    )
    scored.drain()
    adapter = scored.model(final(8))
    scored.set_profile()
    caplog.set_level(logging.DEBUG)
    later = datetime.now(UTC) + timedelta(seconds=65)
    scoring.HOOKS.clock = lambda: later
    # The real worker visit runs the sweep the profile operation queued.
    visit_workspace(
        DueWorkspace(workspace_id=scored.i.workspace, observed_due_at=None),
        kinds=collect_kinds(),
        consumers=scored.i.consumers,
        backend=scored.i.cluster.backend,
        owner="job-scoring-test",
        clock=lambda: later,
        jitter=None,
    )

    (good,) = scored.assessments("~02100")
    assert (good["fit"], good["score"]) == ("high", 80)
    assert good["model_id"] == "claude_cli:default"
    assert good["prompt_version"].startswith("jobscore.1.")
    assert good["prompt_version"].endswith("/profile.1")
    assert good["explanation"].splitlines() == [
        "Fit: strong. Economics: favorable.",
        "Candidate evidence to review: Example pipeline.",
        'Posting excerpt: "Build a small example pipeline for a fictional shop.".',
        "Clarify scope and acceptance criteria.",
        "Payment verification is unknown; confirm it before applying.",
    ]
    (crowded,) = scored.assessments("~02200")
    assert (crowded["fit"], crowded["score"], crowded["model_id"]) == (
        "low",
        None,
        None,
    )
    assert "More than 50 proposals already." in crowded["explanation"]
    (tile,) = scored.assessments("~02300")
    assert (tile["fit"], tile["score"]) == ("needs_information", None)
    assert "Paste the full posting" in tile["explanation"]
    # One model call, for the one job the filters let through with a description,
    # run as the owner, with the profile in the task and nowhere else.
    [start] = adapter.starts
    assert "Profile-marker-7d41" in start.request.task
    assert start.request.permitted_tools == []
    assert [t["account_id"] for t in scored.tokens.issued] == [scored.c.owner.actor.id]
    assert len(scored.tokens.revoked) == 1
    assert not Path(start.spawn.work_dir).parent.exists()
    assert "Profile-marker-7d41" not in caplog.text
    assert all(
        r["state"] == "scored" for r in rows(scored.i, select(s.job_score_state))
    )

    queue = call(
        scored.i,
        scored.c.owner,
        "leads.ui.queue",
        pipeline_id=str(scored.c.pipeline_id),
        sort="score",
    )
    assert [item["assessment"]["score"] for item in queue.items] == [80, None, None]
    assert queue.items[0]["title"] == "Good job"


def test_a_usage_limit_leaves_jobs_waiting_without_spending_attempts(
    scored: Scoring,
) -> None:
    for posting in ("~02100", "~02101"):
        scored.paste(posting, FULL.format(title=posting, what="pipeline", activity=""))
    scored.model(failure(FailureKind.USAGE_LIMITED))
    scored.set_profile()
    now = datetime.now(UTC)
    scored.sweep(now)

    assert scored.adapter is not None and len(scored.adapter.starts) == 1
    for posting in ("~02100", "~02101"):
        waiting = scored.state(posting)
        assert (waiting["state"], waiting["attempts"]) == ("pending", 0)
        assert waiting["failure_kind"] == "usage_limited"
        assert waiting["retry_after"] == now + scoring.USAGE_DEFER
        assert scored.assessments(posting) == []
    # The follow-up waits for the limit rather than probing every minute.
    due = [r["next_run_at"] for r in queued_sweeps(scored.i)]
    assert max(due) >= now + scoring.USAGE_DEFER

    scored.model(final(6))
    scored.sweep(now + scoring.USAGE_DEFER + timedelta(seconds=1))
    assert [a["score"] for a in scored.assessments("~02100")] == [60]
    assert [a["fit"] for a in scored.assessments("~02101")] == ["medium"]


def test_failures_back_off_then_record_that_scoring_failed(scored: Scoring) -> None:
    scored.paste("~02100", FULL.format(title="Job", what="pipeline", activity=""))
    scored.model(failure(FailureKind.OUTPUT_INVALID))
    scored.set_profile()
    at = datetime.now(UTC)
    for attempt in range(1, scoring.SCORE_ATTEMPTS):
        scored.sweep(at)
        waiting = scored.state("~02100")
        assert (waiting["state"], waiting["attempts"]) == ("pending", attempt)
        assert waiting["failure_kind"] == "output_invalid"
        assert scored.assessments("~02100") == []
        at = waiting["retry_after"] + timedelta(seconds=1)
    scored.sweep(at)
    (recorded,) = scored.assessments("~02100")
    assert recorded["fit"] == "needs_information" and recorded["score"] is None
    assert (
        "Scoring failed after 5 attempts (output_invalid)." in recorded["explanation"]
    )
    assert scored.state("~02100")["state"] == "failed"


def test_a_broken_login_pauses_every_job_and_a_profile_set_requeues_failures(
    scored: Scoring,
) -> None:
    for posting in ("~02100", "~02101"):
        scored.paste(posting, FULL.format(title=posting, what="pipeline", activity=""))
    scored.model(failure(FailureKind.CREDENTIAL_INVALID))
    scored.set_profile()
    now = datetime.now(UTC)
    for minute in range(6):
        scored.sweep(now + minute * (scoring.USAGE_DEFER + timedelta(seconds=1)))
    for posting in ("~02100", "~02101"):
        waiting = scored.state(posting)
        assert (waiting["state"], waiting["attempts"]) == ("pending", 0)
        assert waiting["failure_kind"] == "credential_invalid"
        assert scored.assessments(posting) == []

    # A job-specific failure exhausts its attempts; setting the profile again
    # puts it back in line, and it is scored once the model answers.
    scored.model(failure(FailureKind.OUTPUT_INVALID))
    at = datetime.now(UTC) + timedelta(hours=2)
    for _ in range(scoring.SCORE_ATTEMPTS * 2):
        scored.sweep(at)
        at += timedelta(hours=1)
    assert scored.state("~02100")["state"] == "failed"
    scored.model(final(7))
    scored.set_profile()
    assert scored.state("~02100")["state"] == "pending"
    scored.sweep(at)
    assert scored.assessments("~02100")[-1]["score"] == 70


def test_a_profile_change_reapplies_the_filters_to_held_jobs(scored: Scoring) -> None:
    scored.paste(
        "~02200",
        FULL.format(title="Crowded job", what="app", activity="Proposals: 50+"),
    )
    scored.model(final(6))
    scored.set_profile()
    scored.sweep()
    assert [a["fit"] for a in scored.assessments("~02200")] == ["low"]
    scored.set_profile(tier1={"max_proposals": 100})
    scored.sweep()
    assert [(a["fit"], a["score"]) for a in scored.assessments("~02200")] == [
        ("low", None),
        ("medium", 60),
    ]


def test_a_job_held_after_an_earlier_score_is_requeued_by_a_profile_change(
    scored: Scoring,
) -> None:
    text = FULL.format(title="Job", what="pipeline", activity="")
    scored.paste("~02100", text)
    scored.model(final(8))
    scored.set_profile()
    scored.sweep()
    # The competition rises past the cap: the next sweep holds the job back.
    scored.paste("~02100", text + "\n\nProposals: 50+")
    scored.sweep()
    assert [(a["fit"], a["score"]) for a in scored.assessments("~02100")] == [
        ("high", 80),
        ("low", None),
    ]
    scored.set_profile(tier1={"max_proposals": 100})
    assert scored.state("~02100")["state"] == "pending"
    scored.sweep()
    assert scored.assessments("~02100")[-1]["score"] == 80


def test_a_changed_description_is_rescored_and_an_unchanged_one_is_not(
    scored: Scoring,
) -> None:
    text = FULL.format(title="Job", what="pipeline", activity="")
    scored.paste("~02100", text)
    scored.model(final(5), final(9))
    scored.set_profile()
    scored.sweep()
    scored.sweep()
    assert [a["score"] for a in scored.assessments("~02100")] == [50]

    scored.paste("~02100", text + "\n\nNow with a clear scope and a larger budget.")
    scored.sweep()
    assert [a["score"] for a in scored.assessments("~02100")] == [50, 90]
    assert scored.adapter is not None and len(scored.adapter.starts) == 2

    queue = call(
        scored.i,
        scored.c.owner,
        "leads.ui.queue",
        pipeline_id=str(scored.c.pipeline_id),
        sort="score",
    )
    assert queue.items[0]["assessment"]["score"] == 90


def test_a_processed_job_delivery_queues_one_sweep_a_minute(scored: Scoring) -> None:
    scored.set_profile()
    # The first visit fires the schedule the profile turned on. That sweep is due
    # at the tick itself, not on a minute, and it is the schedule's, not a
    # delivery's, so it is left out. The profile's own sweep is retired as if it
    # had run, or it would hold the pastes' minute and they would queue nothing.
    scored.drain()
    with open_unit_of_work(scored.c.owner) as uow:
        uow.connection.execute(
            update(job)
            .where(job.c.kind == SCORE_JOB_KIND, job.c.state == "queued")
            .values(state="succeeded", finished_at=datetime.now(UTC))
        )
        uow.commit()
    before = {r["id"] for r in queued_sweeps(scored.i)}
    for posting in ("~02100", "~02101", "~02102"):
        scored.paste(posting, FULL.format(title=posting, what="pipeline", activity=""))
    added = [r for r in queued_sweeps(scored.i) if r["id"] not in before]
    assert added
    # The pastes may straddle a minute boundary, so there can be two rows. What is
    # fixed is that each lands on a minute and no minute is queued twice.
    instants = Counter(r["next_run_at"] for r in added)
    assert all(n == 1 for n in instants.values()), instants
    assert all(at == at.replace(second=0, microsecond=0) for at in instants), instants


def test_two_triggers_racing_for_one_minute_queue_one_sweep(scored: Scoring) -> None:
    """Two transactions queue the same minute at once, say a delivery beside an
    owner's retry. The interleaving is forced: the first holds its uncommitted row
    until the second is seen waiting on the advisory lock, then commits; the second
    must then find that row and insert nothing."""
    now = (datetime.now(UTC) + timedelta(hours=1)).replace(second=30, microsecond=0)
    due = now.replace(second=0) + timedelta(minutes=1)
    queued, release = threading.Event(), threading.Event()
    second_pid: list[int] = []
    errors: list[BaseException] = []

    def first() -> None:
        try:
            with open_unit_of_work(scored.c.owner) as uow:
                scoring.queue_sweep(uow, scored.i.workspace, now=now)
                queued.set()
                assert release.wait(10)
                uow.commit()
        except BaseException as exc:
            errors.append(exc)
            queued.set()

    def second() -> None:
        try:
            with open_unit_of_work(scored.c.owner) as uow:
                second_pid.append(
                    uow.connection.execute(text("select pg_backend_pid()")).scalar_one()
                )
                scoring.queue_sweep(uow, scored.i.workspace, now=now)
                uow.commit()
        except BaseException as exc:
            errors.append(exc)

    a = threading.Thread(target=first)
    a.start()
    assert queued.wait(10)
    b = threading.Thread(target=second)
    b.start()
    waited = False
    deadline = time.monotonic() + 10
    while b.is_alive() and time.monotonic() < deadline:
        if second_pid:
            with open_unit_of_work(scored.c.owner) as uow:
                waited = bool(
                    uow.connection.execute(
                        text(
                            "select count(*) from pg_locks where pid = :pid"
                            " and locktype = 'advisory' and not granted"
                        ),
                        {"pid": second_pid[0]},
                    ).scalar_one()
                )
            if waited:
                break
        time.sleep(0.01)
    release.set()
    a.join(10)
    b.join(10)
    assert not errors, errors
    assert [r["next_run_at"] for r in queued_sweeps(scored.i)].count(due) == 1
    assert waited, "the second enqueue never waited on the first one's lock"


def test_without_a_profile_nothing_is_scored(scored: Scoring) -> None:
    scored.paste("~02100", FULL.format(title="Job", what="pipeline", activity=""))
    adapter = scored.model(final(8))
    assert queued_sweeps(scored.i) == []
    scored.sweep()
    assert adapter.starts == []
    assert rows(scored.i, select(s.job_score_state)) == []
