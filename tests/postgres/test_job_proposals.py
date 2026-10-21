"""Proposal drafting (run 3c2a): the profile operation, a draft request, the sweep
through a scripted model, structured sections and findings, the hand-edit guard,
edits, the answer library, the usage-limit deferral, failures, deletion, and evidence
loss.

Jobs arrive as pasted postings that the intake consumer turns into opportunities. The
model is a scripted adapter behind the real one-shot runner, answering by the kind of
task it is handed, so the requests, throwaway directories and token handling are
production code. Every profile, posting and answer is synthetic.
"""

import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from harness.extraction import ScriptedAdapter, ScriptedHandle, forbid_real_processes
from harness.jobfeed import Collector, call, configure_connector, drain
from harness.jobfeed import collector as collector
from rheo_contracts import (
    AdapterSpawn,
    FailureEvent,
    FailureKind,
    FinalOutputEvent,
    RecordRef,
    RuntimeEvent,
    RuntimeRequest,
)
from rheo_core.operations import dispatch
from rheo_core.runtime.oneshot import OneShotRunner
from rheo_core.runtime.registry import AdapterRegistry
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_tables import job, schedule
from rheo_leads.proposals import job as proposing
from rheo_leads.proposals.job import PROPOSE_JOB_KIND, ProposePayload, propose_job
from rheo_leads.references import ref
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import proposals as s
from sqlalchemy import select

from postgres.test_jobfeed_delivery import rows, the_opportunity
from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake
from postgres.test_leads_pipeline import approve_delete

pytestmark = pytest.mark.postgres

PROFILE = (
    "Proposal-marker-3b8e: a fictional developer.\n\n"
    "- Example Shop (example.com): A storefront in Next.js with Stripe billing.\n"
    "- Example Forms: Renders questionnaires from declarative data with Postgres.\n"
)
POLICY = {
    "writing_notes": ["Example Shop was built solo."],
    "example_letters": "Example-letter-marker-77a0.",
    "anecdotes": [
        {
            "id": "silent-webhook",
            "title": "A webhook that answered 200 and did nothing",
            "story": "My example bot stopped replying while its webhook said 200.",
            "tags": ["failure"],
        }
    ],
}
URL = "https://www.example.com/jobs/Example-posting_{}?ref=x"
POSTING = (
    "Fix our checkout\n\nOur example checkout drops carts. Please include your rate."
)
ANALYSIS = {
    "client_voice": "plain",
    "client_values": "orange",
    "hiring_for": "A checkout fix.",
    "relevant_portfolio": ["Example Shop"],
    "proof_points": ["Example Shop: takes payments"],
    "must_address": [],
    "gaps": [],
    "angle": "The storefront proves checkout work.",
    "opening_shape": "problem",
    "story_asks": [],
    "embedded_questions": ["What is your rate?"],
    "asks_quote": False,
    "scope_size": "week",
}
LETTER = (
    "Carts are lost at checkout. Example Shop takes payments in Next.js.\n\n"
    "Which processor do you use?"
)
DRAFT = LETTER + "\n\n---\n\n**What is your rate?**\nFifty an hour, to confirm."
CLAIMS = {"claims": ["Next.js"], "attributions": [], "process_claims": []}
PRICING = {
    "day_rate": "200",
    "hours_per_day": "5",
    "hourly_default": "60",
    "hourly_floor": "30",
    "calibration": "Calibration-marker-5c1: a fictional delivery record.",
}
SCHEDULE = {
    "milestones": [
        {
            "title": "Discovery",
            "description": "Read the example code.",
            "amount": 200,
            "due_date_offset_days": 1,
        },
        {
            "title": "Checkout fix",
            "description": "",
            "amount": 600,
            "due_date_offset_days": 4,
        },
    ],
    "too_vague": False,
    "reason": None,
}


def final(answer: Mapping[str, Any]) -> list[RuntimeEvent]:
    return [FinalOutputEvent(structured=dict(answer))]


def failure(kind: FailureKind) -> list[RuntimeEvent]:
    return [FailureEvent(kind=kind, detail="synthetic")]


class Model(ScriptedAdapter):
    """Answers by the task it is handed: the analysis, the claim extraction, the
    repair, or the draft. ``override`` replaces every answer while it is set."""

    def __init__(self, draft: str = DRAFT) -> None:
        super().__init__()
        self.draft = draft
        self.override: list[RuntimeEvent] | None = None
        self.analysis: dict[str, Any] = dict(ANALYSIS)
        self.roles: list[str] = []

    @staticmethod
    def role(task: str) -> str:
        if task.startswith("You are a brief analyst"):
            return "analysis"
        if task.startswith("You are a claim extractor"):
            return "gate"
        if task.startswith("You are a copy editor"):
            return "repair"
        if task.startswith("You break a freelance job posting"):
            return "milestones"
        return "draft"

    def start(self, request: RuntimeRequest, *, spawn: AdapterSpawn) -> ScriptedHandle:
        role = self.role(request.task)
        self.roles.append(role)
        answers = {
            "milestones": SCHEDULE,
            "analysis": self.analysis,
            "gate": CLAIMS,
            "draft": {"text": self.draft},
            "repair": {"text": self.draft},
        }
        self.events = self.override or final(answers[role])
        return super().start(request, spawn=spawn)


@dataclass
class Tokens:
    issued: list[dict[str, Any]] = field(default_factory=list)
    revoked: list[UUID] = field(default_factory=list)

    def issue(self, **kwargs: Any) -> tuple[UUID, str]:
        self.issued.append(kwargs)
        return UUID(int=len(self.issued)), "rheo_rt_synthetic_not_a_token"

    def revoke(self, token_id: UUID) -> None:
        self.revoked.append(token_id)


class Beat:
    def checkpoint(self, now: datetime | None = None, *, force: bool = False) -> None:
        pass


@dataclass
class Drafting:
    c: Collector
    tokens: Tokens
    model: Model = field(default_factory=Model)

    @property
    def i(self) -> Intake:
        return self.c.intake

    def install(self) -> None:
        registry = AdapterRegistry()
        registry.register("claude_cli", self.model)
        runner = OneShotRunner(
            registry, issue_token=self.tokens.issue, revoke_token=self.tokens.revoke
        )
        proposing.HOOKS.runner = lambda: runner

    def paste(self, posting: str, text: str = POSTING) -> str:
        call(
            self.i,
            self.c.owner,
            "leads.jobfeed.paste",
            pipeline_ref=ref("pipeline", self.c.pipeline_id),
            kind="posting",
            content=text,
            posting_url=URL.format(posting),
        )
        drain(self.i)
        return ref("opportunity", the_opportunity(self.i, posting)["id"])

    def set_profile(self, **policy: Any) -> Any:
        return call(
            self.i,
            self.c.owner,
            "leads.proposalprofile.set",
            profile=PROFILE,
            policy={**POLICY, **policy},
        )

    def op(self, name: str, **payload: Any) -> Any:
        return call(self.i, self.c.owner, name, **payload)

    def refused(self, name: str, **payload: Any) -> str:
        outcome = dispatch(
            self.c.owner,
            name,
            payload,
            registry=self.i.surfaces.operations,
            consumers=self.i.consumers,
        )
        assert not outcome.ok, outcome
        assert outcome.error is not None
        return outcome.error.error_code

    def sweep(self, at: datetime | None = None) -> None:
        when = at or datetime.now(UTC)
        proposing.HOOKS.clock = lambda: when
        with open_unit_of_work(self.c.owner) as uow:
            propose_job(
                HandlerUnitOfWork(uow, consumers=self.i.consumers),
                ProposePayload(workspace_id=self.i.workspace),
                Beat(),  # type: ignore[arg-type]
            )
            uow.commit()

    def row(self, opportunity: str) -> dict[str, Any]:
        identifier = RecordRef.parse(opportunity).id
        (found,) = rows(
            self.i,
            select(s.job_proposal).where(s.job_proposal.c.opportunity_id == identifier),
        )
        return found


@pytest.fixture
def drafting(
    collector: Collector,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    owner_account_id: UUID,
) -> Iterator[Drafting]:
    configure_connector(monkeypatch, tmp_path, job_hosts="www.example.com,example.com")
    forbid_real_processes(monkeypatch, tmp_path)
    monkeypatch.setenv("RHEO__runtime__claude_cli__credential_kind", "login")
    monkeypatch.setenv(
        "RHEO__runtime__claude_cli__credential_account_id", str(owner_account_id)
    )
    monkeypatch.setattr(proposing, "HOOKS", proposing.Hooks())
    d = Drafting(collector, Tokens())
    d.install()
    yield d


def sweeps(i: Intake) -> list[dict[str, Any]]:
    return rows(i, select(job).where(job.c.kind == PROPOSE_JOB_KIND))


def test_the_profile_operation_stores_the_policy_and_never_echoes_the_text(
    drafting: Drafting,
) -> None:
    first = drafting.set_profile()
    assert (first.version, first.profile, first.policy) == (1, None, None)
    assert first.profile_chars == len(PROFILE)
    second = drafting.set_profile(writing_notes=["A changed note."])
    assert second.version == 2
    (row,) = rows(
        drafting.i, select(schedule).where(schedule.c.name == "jobsearch_propose")
    )
    assert row["enabled"] is True
    got = drafting.op("leads.proposalprofile.get")
    assert got.profile == PROFILE
    assert got.policy.writing_notes == ["A changed note."]
    assert got.policy.anecdotes[0].id == "silent-webhook"


def test_a_request_needs_a_profile_and_a_job_search_opportunity(
    drafting: Drafting,
) -> None:
    opportunity = drafting.paste("~025100")
    assert drafting.refused("leads.jobproposal.request", ref=opportunity) == (
        "profile_required"
    )
    idle = drafting.op("leads.jobproposal.get", ref=opportunity)
    assert (idle.state, idle.sections) == ("idle", [])


def test_a_draft_is_stored_as_sections_with_findings_and_progress(
    drafting: Drafting, caplog: pytest.LogCaptureFixture
) -> None:
    drafting.set_profile()
    opportunity = drafting.paste("~025200")
    queued = drafting.op(
        "leads.jobproposal.request", ref=opportunity, posting_questions=True
    )
    assert queued.state == "queued" and queued.posting_questions is True
    assert sweeps(drafting.i)
    caplog.set_level(logging.DEBUG)
    drafting.sweep()

    got = drafting.op("leads.jobproposal.get", ref=opportunity)
    assert got.state == "ready"
    assert (got.stage, got.step, got.total) == (None, None, None)
    assert [(x.kind, x.question, x.body) for x in got.sections] == [
        ("cover_letter", None, LETTER),
        ("answer", "What is your rate?", "Fifty an hour, to confirm."),
    ]
    assert got.draft == DRAFT
    assert got.edited is False
    # The posting's own "please include" item was answered on its own, but the
    # Questions field still holds only what the owner typed.
    assert got.questions is None
    assert (
        "SCREENING QUESTIONS (answer each after the proposal):\nWhat is your rate?"
        in (drafting.model.starts[1].request.task)
    )
    assert got.findings["analysis"]["angle"] == ANALYSIS["angle"]
    assert got.findings["accuracy"][0]["verdict"] == "whitelisted"
    assert got.findings["style"] == []
    assert got.model_id == "claude_cli:default"
    assert got.prompt_version == "jobproposal.2/profile.1"
    assert drafting.model.roles == ["analysis", "draft", "gate"]
    # Every call ran as the profile's owner, tool-less, and left nothing behind.
    assert {t["account_id"] for t in drafting.tokens.issued} == {
        drafting.c.owner.actor.id
    }
    assert len(drafting.tokens.revoked) == 3
    for start in drafting.model.starts:
        assert start.request.permitted_tools == []
        assert not Path(start.spawn.work_dir).parent.exists()
    draft_task = drafting.model.starts[1].request.task
    assert "Proposal-marker-3b8e" in draft_task
    assert "- Example Shop was built solo." in draft_task
    assert "Example-letter-marker-77a0." in draft_task
    assert "Proposal-marker-3b8e" not in caplog.text
    assert LETTER not in caplog.text

    queue = drafting.op(
        "leads.ui.queue", pipeline_id=str(drafting.c.pipeline_id), sort="newest"
    )
    assert queue.items[0]["has_draft"] is True


def test_by_default_the_postings_own_questions_stay_in_the_letter(
    drafting: Drafting,
) -> None:
    drafting.set_profile()
    opportunity = drafting.paste("~025250")
    queued = drafting.op("leads.jobproposal.request", ref=opportunity)
    assert queued.posting_questions is False
    drafting.sweep()
    task = drafting.model.starts[1].request.task
    assert "SCREENING QUESTIONS" not in task
    assert "ASKED IN THE LETTER" in task and "- What is your rate?" in task
    got = drafting.op("leads.jobproposal.get", ref=opportunity)
    assert got.questions is None and got.posting_questions is False

    # The choice is stored with the request and kept when a later one omits it.
    drafting.op(
        "leads.jobproposal.request",
        ref=opportunity,
        posting_questions=True,
        force=True,
    )
    drafting.sweep()
    kept = drafting.op("leads.jobproposal.request", ref=opportunity, force=True)
    assert kept.posting_questions is True


def test_an_exported_proposal_is_valid_against_the_export_schema(
    drafting: Drafting,
) -> None:
    """The schema forbids unknown keys, so a new column must be in it."""
    from types import SimpleNamespace

    from rheo_core.exports.artifact import _validated_module_rows
    from rheo_leads import MANIFEST
    from rheo_leads.export import export_records

    drafting.set_profile()
    opportunity = drafting.paste("~025260")
    drafting.op("leads.jobproposal.request", ref=opportunity, posting_questions=True)
    drafting.sweep()
    with open_unit_of_work(drafting.c.owner) as uow:
        rows = [
            dict(r)
            for r in export_records(SimpleNamespace(connection=uow.connection))  # type: ignore[arg-type]
        ]
    rows = [
        {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()}
        for r in rows
    ]
    (proposal,) = [r for r in rows if r["record_type"] == "leads.job_proposal"]
    assert proposal["posting_questions"] is True
    assert len(_validated_module_rows(MANIFEST, [proposal], "leads")) == 1


def test_a_regenerate_refuses_over_hand_edits_unless_forced(drafting: Drafting) -> None:
    drafting.set_profile()
    opportunity = drafting.paste("~025300")
    drafting.op("leads.jobproposal.request", ref=opportunity)
    drafting.sweep()
    got = drafting.op("leads.jobproposal.get", ref=opportunity)
    answer = got.sections[1]
    edited = drafting.op(
        "leads.proposalsection.set",
        ref=opportunity,
        section_id=str(answer.id),
        body="Sixty an hour.",
    )
    assert edited.sections[1].body == "Sixty an hour." and edited.edited is True
    assert drafting.refused("leads.jobproposal.request", ref=opportunity) == (
        "draft_edited"
    )
    forced = drafting.op(
        "leads.jobproposal.request",
        ref=opportunity,
        force=True,
        questions="Can you start Monday?",
    )
    assert forced.state == "queued" and forced.questions == "Can you start Monday?"
    assert drafting.refused("leads.jobproposal.request", ref=opportunity) == (
        "draft_in_progress"
    )
    drafting.sweep()
    again = drafting.op("leads.jobproposal.get", ref=opportunity)
    assert again.state == "ready" and again.edited is False
    # Typed questions win over the posting's own.
    assert again.questions == "Can you start Monday?"
    assert "Can you start Monday?" in drafting.model.starts[-2].request.task


def test_the_whole_draft_writes_through_to_its_sections(drafting: Drafting) -> None:
    drafting.set_profile()
    opportunity = drafting.paste("~025400")
    drafting.op("leads.jobproposal.request", ref=opportunity)
    drafting.sweep()
    letter_only = drafting.op(
        "leads.jobproposal.update", ref=opportunity, draft="A new letter."
    )
    assert [x.body for x in letter_only.sections] == [
        "A new letter.",
        "Fifty an hour, to confirm.",
    ]
    whole = drafting.op(
        "leads.jobproposal.update",
        ref=opportunity,
        draft="Letter.\n\n---\n\n**What is your rate?**\nFifty.\n\n**Start?**\nMonday.",
    )
    assert [(x.question, x.body) for x in whole.sections] == [
        (None, "Letter."),
        ("What is your rate?", "Fifty."),
        ("Start?", "Monday."),
    ]
    fewer = drafting.op(
        "leads.jobproposal.update",
        ref=opportunity,
        draft="Letter.\n\n---\n\n**What is your rate?**\nFifty.",
    )
    assert len(fewer.sections) == 2


def test_the_answer_library_fills_a_section_and_counts_as_an_edit(
    drafting: Drafting,
) -> None:
    drafting.set_profile()
    opportunity = drafting.paste("~025500")
    drafting.op("leads.jobproposal.request", ref=opportunity)
    drafting.sweep()
    saved = drafting.op(
        "leads.answerlibrary.save",
        question="What is your rate?",
        answer="A library rate.",
    )
    again = drafting.op(
        "leads.answerlibrary.save",
        question="  what is your RATE ",
        answer="A newer library rate.",
    )
    assert again.id == saved.id
    match = drafting.op("leads.answerlibrary.match", question="What is your rate")
    assert match.entry.answer == "A newer library rate."
    got = drafting.op("leads.jobproposal.get", ref=opportunity)
    answer = got.sections[1]
    assert answer.library_match_id == saved.id
    used = drafting.op(
        "leads.proposalsection.use_library",
        ref=opportunity,
        section_id=str(answer.id),
    )
    assert used.sections[1].body == "A newer library rate."
    assert used.sections[1].library_answer_id == saved.id
    assert used.edited is True
    assert drafting.refused(
        "leads.proposalsection.use_library",
        ref=opportunity,
        section_id=str(used.sections[0].id),
    ) == ("not_found")


def test_a_usage_limit_waits_without_spending_an_attempt(drafting: Drafting) -> None:
    drafting.set_profile()
    first = drafting.paste("~025600")
    second = drafting.paste(
        "~025601", "Second job\n\nAn example form for a fictional clinic."
    )
    drafting.op("leads.jobproposal.request", ref=first)
    drafting.op("leads.jobproposal.request", ref=second)
    drafting.model.override = failure(FailureKind.USAGE_LIMITED)
    now = datetime.now(UTC)
    drafting.sweep(now)
    for opportunity in (first, second):
        row = drafting.row(opportunity)
        assert (row["state"], row["attempts"], row["failure_kind"]) == (
            "queued",
            0,
            "usage_limited",
        )
        assert row["retry_after"] == now + proposing.USAGE_DEFER
    # One model call, then the sweep stopped and queued its follow-up.
    assert drafting.model.roles == ["analysis"]
    due = now + proposing.USAGE_DEFER
    # Rounded up to the sweep's ten-second granularity.
    assert any(
        due <= r["next_run_at"] < due + timedelta(seconds=10)
        for r in sweeps(drafting.i)
    )
    drafting.model.override = None
    drafting.sweep(now + proposing.USAGE_DEFER + timedelta(seconds=1))
    assert drafting.row(first)["state"] == "ready"
    assert drafting.row(second)["state"] == "ready"


def test_unusable_drafts_back_off_then_fail_visibly(drafting: Drafting) -> None:
    drafting.set_profile()
    opportunity = drafting.paste("~025700")
    drafting.op("leads.jobproposal.request", ref=opportunity)
    drafting.model.draft = " "
    when = datetime.now(UTC)
    for attempt in range(1, proposing.DRAFT_ATTEMPTS + 1):
        drafting.sweep(when)
        row = drafting.row(opportunity)
        assert row["attempts"] == attempt
        assert row["failure_kind"] == "output_invalid"
        when = (row["retry_after"] or when) + timedelta(seconds=1)
    assert drafting.row(opportunity)["state"] == "failed"
    got = drafting.op("leads.jobproposal.get", ref=opportunity)
    assert (got.state, got.failure_kind) == ("failed", "output_invalid")
    # A failed draft can be requested again.
    drafting.model.draft = DRAFT
    assert drafting.op("leads.jobproposal.request", ref=opportunity).state == "queued"


def test_a_lapsed_claim_is_taken_again_and_a_deleted_draft_is_not_written(
    drafting: Drafting,
) -> None:
    drafting.set_profile()
    opportunity = drafting.paste("~025800")
    drafting.op("leads.jobproposal.request", ref=opportunity)
    now = datetime.now(UTC)
    with open_unit_of_work(drafting.c.owner) as uow:
        claimed = proposing.claim(uow, now=now, window=timedelta(minutes=10))
        uow.commit()
    assert claimed is not None
    assert drafting.row(opportunity)["state"] == "drafting"
    # Within the window, nothing else can take it.
    drafting.sweep(now + timedelta(minutes=1))
    assert drafting.model.roles == []
    # Deleting it while it drafts makes that sweep's result stale.
    deleted = drafting.op("leads.jobproposal.delete", ref=opportunity)
    assert (deleted.state, deleted.sections) == ("idle", [])
    with open_unit_of_work(drafting.c.owner) as uow:
        assert proposing._current(uow, claimed) is None
    # A worker that died leaves a claim that lapses, and the next sweep retakes it.
    drafting.op("leads.jobproposal.request", ref=opportunity)
    with open_unit_of_work(drafting.c.owner) as uow:
        held = proposing.claim(uow, now=now, window=timedelta(minutes=10))
        uow.commit()
    assert held is not None
    drafting.sweep(now + timedelta(minutes=11))
    assert drafting.row(opportunity)["state"] == "ready"


def test_deleting_the_draft_keeps_the_questions(drafting: Drafting) -> None:
    drafting.set_profile()
    opportunity = drafting.paste("~025900")
    drafting.op("leads.jobproposal.request", ref=opportunity, questions="Rate?")
    drafting.sweep()
    deleted = drafting.op("leads.jobproposal.delete", ref=opportunity)
    assert (deleted.state, deleted.questions, deleted.findings) == (
        "idle",
        "Rate?",
        None,
    )
    assert rows(drafting.i, select(s.proposal_section)) == []


def test_losing_the_posting_evidence_removes_the_proposal(drafting: Drafting) -> None:
    drafting.set_profile()
    opportunity = drafting.paste("~025990")
    drafting.op("leads.jobproposal.request", ref=opportunity, questions="Rate?")
    drafting.sweep()
    assert rows(drafting.i, select(s.proposal_section))
    identifier = RecordRef.parse(opportunity).id
    (link,) = rows(
        drafting.i,
        select(p.opportunity_observation).where(
            p.opportunity_observation.c.opportunity_id == identifier
        ),
    )
    approve_delete(drafting.i, ref("observation", link["observation_id"]))
    # A proposal quotes the posting, so it does not outlive the evidence.
    assert rows(drafting.i, select(s.job_proposal)) == []
    assert rows(drafting.i, select(s.proposal_section)) == []


def test_a_claim_from_an_earlier_request_never_lands_on_a_newer_one(
    drafting: Drafting,
) -> None:
    drafting.set_profile()
    opportunity = drafting.paste("~025991")
    drafting.op("leads.jobproposal.request", ref=opportunity)
    now = datetime.now(UTC)
    with open_unit_of_work(drafting.c.owner) as uow:
        first = proposing.claim(uow, now=now, window=timedelta(minutes=10))
        uow.commit()
    drafting.op("leads.jobproposal.delete", ref=opportunity)
    drafting.op("leads.jobproposal.request", ref=opportunity, questions="New?")
    with open_unit_of_work(drafting.c.owner) as uow:
        second = proposing.claim(uow, now=now, window=timedelta(minutes=10))
        uow.commit()
    assert first is not None and second is not None
    # Same attempt count, different request: only the newer claim is current.
    assert first.attempts == second.attempts == 1
    with open_unit_of_work(drafting.c.owner) as uow:
        assert proposing._current(uow, first) is None
        assert proposing._current(uow, second) is not None


def test_a_crashed_run_counts_and_a_lapsed_claim_stops_at_the_attempt_cap(
    drafting: Drafting,
) -> None:
    drafting.set_profile()
    opportunity = drafting.paste("~025992")
    drafting.op("leads.jobproposal.request", ref=opportunity)
    drafting.model.override = failure(FailureKind.RUNTIME_ERROR)
    drafting.sweep()
    row = drafting.row(opportunity)
    assert (row["state"], row["attempts"], row["failure_kind"]) == (
        "queued",
        1,
        "runtime_error",
    )
    # A worker that dies every time: claims lapse until the attempts are spent.
    when = row["retry_after"] + timedelta(seconds=1)
    for _ in range(proposing.DRAFT_ATTEMPTS - 1):
        with open_unit_of_work(drafting.c.owner) as uow:
            assert proposing.claim(uow, now=when, window=timedelta(minutes=1))
            uow.commit()
        when += timedelta(minutes=2)
    with open_unit_of_work(drafting.c.owner) as uow:
        assert proposing.claim(uow, now=when, window=timedelta(minutes=1)) is None
        uow.commit()
    row = drafting.row(opportunity)
    assert (row["state"], row["failure_kind"]) == ("failed", "claim_lapsed")


def test_a_lapsed_draft_can_be_requested_again(drafting: Drafting) -> None:
    drafting.set_profile()
    opportunity = drafting.paste("~025993")
    drafting.op("leads.jobproposal.request", ref=opportunity)
    past = datetime.now(UTC) - timedelta(hours=1)
    with open_unit_of_work(drafting.c.owner) as uow:
        assert proposing.claim(uow, now=past, window=timedelta(minutes=10))
        uow.commit()
    again = drafting.op("leads.jobproposal.request", ref=opportunity)
    assert (again.state, again.attempts) == ("queued", 0)


def test_removing_answers_counts_as_a_hand_edit(drafting: Drafting) -> None:
    drafting.set_profile()
    opportunity = drafting.paste("~025994")
    drafting.op("leads.jobproposal.request", ref=opportunity)
    drafting.sweep()
    fewer = drafting.op(
        "leads.jobproposal.update", ref=opportunity, draft=LETTER + "\n\n---\n"
    )
    assert len(fewer.sections) == 1 and fewer.edited is True
    assert drafting.refused("leads.jobproposal.request", ref=opportunity) == (
        "draft_edited"
    )


def test_the_one_job_queue_read_carries_the_whole_posting(drafting: Drafting) -> None:
    opportunity = drafting.paste("~025995")
    one = drafting.op("leads.ui.queue", ref=opportunity)
    assert one.items[0]["description"] == POSTING
    listed = drafting.op(
        "leads.ui.queue", pipeline_id=str(drafting.c.pipeline_id), sort="newest"
    )
    # The list keeps its preview only.
    assert "description" not in listed.items[0]


def test_pricing_needs_a_policy_then_drafts_a_schedule_the_owner_can_edit(
    drafting: Drafting,
) -> None:
    drafting.set_profile()
    opportunity = drafting.paste("~025996")
    assert drafting.refused("leads.jobpricing.request", ref=opportunity) == (
        "pricing_not_set"
    )
    drafting.set_profile(pricing=PRICING)
    queued = drafting.op("leads.jobpricing.request", ref=opportunity)
    assert (queued.pricing.state, queued.pricing.mode) == ("queued", "milestones")
    assert drafting.refused("leads.jobpricing.request", ref=opportunity) == (
        "pricing_in_progress"
    )
    drafting.sweep()
    got = drafting.op("leads.jobproposal.get", ref=opportunity)
    pricing = got.pricing
    assert pricing.available is True and pricing.state == "ready"
    assert [(m.title, m.amount) for m in pricing.milestones] == [
        ("Discovery", "200.00"),
        ("Checkout fix", "600.00"),
    ]
    assert pricing.milestones[0].due_on is not None
    # The form rate matches the fixed total at the day rate spread over the day.
    assert (pricing.suggestion["rate"], pricing.suggestion["hours"]) == (
        "40.00",
        "20.0",
    )
    assert drafting.model.roles == ["milestones"]
    task = drafting.model.starts[0].request.task
    assert "Calibration-marker-5c1" in task and "$200.00 PER WORKING-DAY" in task
    edited = drafting.op(
        "leads.proposalmilestone.set",
        ref=opportunity,
        milestone_id=str(pricing.milestones[1].id),
        amount="800",
        title="Checkout fix and tests",
    )
    assert [(m.title, m.amount) for m in edited.pricing.milestones][1] == (
        "Checkout fix and tests",
        "800.00",
    )
    assert edited.pricing.suggestion["fixed_total"] == "1000.00"


def test_a_draft_that_is_asked_for_a_quote_prices_the_job_first(
    drafting: Drafting,
) -> None:
    drafting.set_profile(pricing=PRICING)
    opportunity = drafting.paste("~025997")
    drafting.model.analysis = {**ANALYSIS, "asks_quote": True}
    drafting.op("leads.jobproposal.request", ref=opportunity)
    drafting.sweep()
    assert drafting.model.roles == ["analysis", "milestones", "draft", "gate"]
    draft_task = drafting.model.starts[2].request.task
    assert "PRICING FIGURES" in draft_task
    assert "- Total: $800.00 for 4 working-days" in draft_task
    got = drafting.op("leads.jobproposal.get", ref=opportunity)
    assert got.state == "ready"
    assert (got.pricing.state, got.pricing.mode) == ("ready", "milestones")
    assert len(got.pricing.milestones) == 2
    assert got.findings["priced"] is True
    # The next draft uses the stored milestones: no Pricing stage again.
    drafting.model.roles.clear()
    drafting.op("leads.jobproposal.request", ref=opportunity, force=True)
    drafting.sweep()
    assert drafting.model.roles == ["analysis", "draft", "gate"]


def test_a_usage_limit_holds_pricing_without_spending_an_attempt(
    drafting: Drafting,
) -> None:
    drafting.set_profile(pricing=PRICING)
    opportunity = drafting.paste("~025998")
    drafting.op("leads.jobpricing.request", ref=opportunity)
    drafting.model.override = failure(FailureKind.USAGE_LIMITED)
    now = datetime.now(UTC)
    drafting.sweep(now)
    identifier = RecordRef.parse(opportunity).id
    (row,) = rows(
        drafting.i,
        select(s.job_pricing).where(s.job_pricing.c.opportunity_id == identifier),
    )
    assert (row["state"], row["attempts"], row["failure_kind"]) == (
        "queued",
        0,
        "usage_limited",
    )
    drafting.model.override = None
    drafting.sweep(now + timedelta(minutes=16))
    got = drafting.op("leads.jobproposal.get", ref=opportunity)
    assert got.pricing.state == "ready"


def test_losing_the_posting_evidence_removes_the_pricing(drafting: Drafting) -> None:
    drafting.set_profile(pricing=PRICING)
    opportunity = drafting.paste("~025999")
    drafting.op("leads.jobpricing.request", ref=opportunity)
    drafting.sweep()
    assert rows(drafting.i, select(s.proposal_milestone))
    identifier = RecordRef.parse(opportunity).id
    (link,) = rows(
        drafting.i,
        select(p.opportunity_observation).where(
            p.opportunity_observation.c.opportunity_id == identifier
        ),
    )
    approve_delete(drafting.i, ref("observation", link["observation_id"]))
    assert rows(drafting.i, select(s.job_pricing)) == []
    assert rows(drafting.i, select(s.proposal_milestone)) == []


def test_a_draft_waits_for_its_jobs_pricing_and_quotes_the_stored_rows(
    drafting: Drafting,
) -> None:
    drafting.set_profile(pricing=PRICING)
    opportunity = drafting.paste("~025981")
    drafting.model.analysis = {**ANALYSIS, "asks_quote": True}
    drafting.op("leads.jobpricing.request", ref=opportunity)
    drafting.op("leads.jobproposal.request", ref=opportunity)
    now = datetime.now(UTC)
    # A draft claimed while the pricing is queued would quote its own figures.
    with open_unit_of_work(drafting.c.owner) as uow:
        assert proposing.claim(uow, now=now, window=timedelta(minutes=10)) is None
        uow.rollback()
    drafting.sweep(now)
    # Pricing ran first; the draft then found the rows and needed no Pricing stage.
    assert drafting.model.roles == ["milestones", "analysis", "draft", "gate"]
    draft_task = drafting.model.starts[2].request.task
    assert "- Total: $800.00 for 4 working-days" in draft_task
    got = drafting.op("leads.jobproposal.get", ref=opportunity)
    assert got.state == "ready" and got.findings["priced"] is False


def test_a_drafts_schedule_never_replaces_newer_pricing(drafting: Drafting) -> None:
    drafting.set_profile(pricing=PRICING)
    opportunity = drafting.paste("~025982")
    drafting.model.analysis = {**ANALYSIS, "asks_quote": True}
    drafting.op("leads.jobproposal.request", ref=opportunity)
    now = datetime.now(UTC)
    with open_unit_of_work(drafting.c.owner) as uow:
        claimed = proposing.claim(uow, now=now, window=timedelta(minutes=10))
        uow.commit()
    assert claimed is not None and claimed.priced.state is None
    # Pricing arrives while the draft runs.
    drafting.op("leads.jobpricing.request", ref=opportunity)
    drafting.sweep(now)
    assert drafting.op("leads.jobproposal.get", ref=opportunity).pricing.state == (
        "ready"
    )
    from rheo_leads.proposals.milestones import Schedule, Stage
    from rheo_leads.proposals.pipeline import Result
    from rheo_leads.proposals.render import Draft

    stale = Result(
        draft=Draft(letter="A letter."),
        findings={"accuracy": [], "style": []},
        schedule=Schedule(
            stages=(Stage("Other", "", Decimal(999), now.date()),),
        ),
    )
    with open_unit_of_work(drafting.c.owner) as uow:
        assert proposing.record_draft(
            uow, claimed, stale, profile_version=1, model_id="m", now=now
        )
        uow.commit()
    got = drafting.op("leads.jobproposal.get", ref=opportunity)
    assert [m.title for m in got.pricing.milestones] == ["Discovery", "Checkout fix"]
    assert got.findings["figures_stale"] is True


def test_a_milestone_edit_changes_only_what_it_names(drafting: Drafting) -> None:
    drafting.set_profile(pricing=PRICING)
    opportunity = drafting.paste("~025983")
    drafting.op("leads.jobpricing.request", ref=opportunity)
    drafting.sweep()
    first = drafting.op("leads.jobproposal.get", ref=opportunity).pricing.milestones[0]
    after = drafting.op(
        "leads.proposalmilestone.set",
        ref=opportunity,
        milestone_id=str(first.id),
        due_on="2026-12-01",
    ).pricing.milestones[0]
    assert (after.title, after.amount, str(after.due_on)) == (
        first.title,
        first.amount,
        "2026-12-01",
    )
    assert drafting.refused(
        "leads.proposalmilestone.set",
        ref=opportunity,
        milestone_id=str(first.id),
        title=None,
    ) == ("input_invalid")
