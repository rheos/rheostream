"""Job scoring's pure parts: the tier-1 clauses, the tier-2 prompt, parse and render,
the digest that decides a rescore, and one assessment through a fake runner. No
database. Every profile, policy and posting here is synthetic."""

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from jsonschema import Draft202012Validator
from rheo_core.runtime.oneshot import (
    OneShotCredentialNotOwned,
    OneShotDeferred,
    OneShotFailed,
)
from rheo_leads.model_calls import Call
from rheo_leads.scoring import tier1, tier2
from rheo_leads.scoring.job import (
    ASSESSMENT_INVALID,
    HELD_LINE,
    PROMOTED_LINE,
    Outcome,
    assess,
    input_digest,
)
from rheo_leads.scoring.profile import ScoringPolicy, StoredProfile, Tier1Policy
from rheo_leads.scoring.tier1 import EXTENSION, JobFacts, run_tier1

PROFILE = (
    "Example career facts.\n\nPortfolio:\n\n"
    "- Booking app: Agent automation is not built.\n\n"
    "- Video app: FFmpeg editing and cloud drive uploads."
)
POLICY = ScoringPolicy(
    known_gaps=["No production example-platform experience."],
    exclusions=["Skip roles centred on an example vendor stack."],
    platform_notes={"upwork": "Upwork note.", "other": "Other note."},
)
TEXT = "Budget $550. We prefer a paid proof of concept before full development."
FIXED = JobFacts(title="Example", description=TEXT, job_type="fixed")
CATALOG = tier2.evidence_catalog(PROFILE, POLICY)
ACCOUNT = UUID("018f7a10-0000-7000-8000-00000000b001")
WORKSPACE = UUID("018f7a10-0000-7000-8000-00000000b002")


def established(**changes: Any) -> JobFacts:
    values: dict[str, Any] = {
        "title": "Example posting",
        "description": "Build an example pipeline.",
        "payment_verified": True,
        "client_spend": Decimal("12000"),
        "client_rating": Decimal("4.9"),
        "client_reviews": 12,
        "client_hire_rate": Decimal("0.75"),
        "proposal_count": 15,
        "proposal_count_exact": False,
    }
    values.update(changes)
    return JobFacts(**values)


# --- tier 1 ---------------------------------------------------------------------------


def test_an_established_client_with_good_metrics_passes() -> None:
    result = run_tier1(established(), Tier1Policy())
    assert result.passed and not result.soft and not result.new_client


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        ({"payment_verified": False}, "not verified"),
        ({"client_spend": Decimal("120")}, "$120, below the $500"),
        ({"client_rating": Decimal("4.2")}, "4.2, below 4.5"),
        ({"client_hire_rate": Decimal("0.2")}, "20% of their jobs, below 40%"),
        ({"proposal_count": 51}, "More than 50 proposals"),
    ],
)
def test_each_known_bad_value_fails_its_clause(
    changes: dict[str, Any], words: str
) -> None:
    result = run_tier1(established(**changes), Tier1Policy())
    assert not result.passed
    assert any(words in reason for reason in result.failed), result.failed


def test_every_failing_clause_is_named() -> None:
    result = run_tier1(
        established(payment_verified=False, client_rating=Decimal("3")), Tier1Policy()
    )
    assert len(result.failed) == 2


@pytest.mark.parametrize(
    "unknown",
    ["client_spend", "client_rating", "client_hire_rate", "proposal_count"],
)
def test_an_unknown_value_never_fails(unknown: str) -> None:
    assert run_tier1(established(**{unknown: None}), Tier1Policy()).passed


def test_a_zero_rating_is_no_rating() -> None:
    job = JobFacts.from_fields(
        "Example",
        {
            EXTENSION + "client_rating": "0",
            EXTENSION + "client_reviews": "0",
            EXTENSION + "client_payment_verified": "true",
        },
    )
    assert job.client_rating is None
    assert run_tier1(job, Tier1Policy()).passed
    rated = JobFacts.from_fields("Example", {EXTENSION + "client_rating": "3.9"})
    assert rated.client_rating == Decimal("3.9")
    assert not run_tier1(rated, Tier1Policy()).passed


def test_a_zero_hire_rate_without_reviews_is_no_hire_rate() -> None:
    empty = JobFacts.from_fields(
        "Example",
        {
            EXTENSION + "client_hire_rate": "0",
            EXTENSION + "client_reviews": "0",
            EXTENSION + "client_payment_verified": "true",
        },
    )
    assert empty.client_hire_rate is None
    assert run_tier1(empty, Tier1Policy()).passed
    reviewed = JobFacts.from_fields(
        "Example",
        {EXTENSION + "client_hire_rate": "0", EXTENSION + "client_reviews": "4"},
    )
    assert reviewed.client_hire_rate == Decimal(0)
    assert not run_tier1(reviewed, Tier1Policy()).passed


def test_unknown_payment_passes_as_a_soft_clause() -> None:
    result = run_tier1(established(payment_verified=None), Tier1Policy())
    assert result.passed
    assert result.soft == (
        "Payment verification is unknown; confirm it before applying.",
    )


def test_a_new_client_needs_only_a_verified_payment() -> None:
    new = established(
        client_spend=Decimal(0),
        client_reviews=0,
        client_rating=None,
        client_hire_rate=Decimal("0"),
    )
    result = run_tier1(new, Tier1Policy())
    assert result.passed and result.new_client
    assert not run_tier1(
        established(client_spend=Decimal(0), client_reviews=0, payment_verified=False),
        Tier1Policy(),
    ).passed


def test_unknown_spend_is_not_a_new_client() -> None:
    result = run_tier1(
        established(client_spend=None, client_reviews=0, client_rating=None),
        Tier1Policy(),
    )
    assert result.passed and not result.new_client


def test_thresholds_come_from_the_policy() -> None:
    strict = Tier1Policy(min_client_spend=Decimal("20000"), max_proposals=10)
    result = run_tier1(established(), strict)
    assert len(result.failed) == 2


def test_another_platform_passes_untouched() -> None:
    assert run_tier1(
        established(platform="example-board", payment_verified=False), Tier1Policy()
    ).passed


def test_facts_read_the_extension_fields() -> None:
    job = JobFacts.from_fields(
        "Example",
        {
            "message": "Body",
            EXTENSION + "job_type": "hourly",
            EXTENSION + "budget_max": "40.00",
            EXTENSION + "proposal_count": "15",
            EXTENSION + "proposal_count_exact": "false",
            EXTENSION + "client_payment_verified": "true",
            EXTENSION + "client_hire_rate": "0.5",
            EXTENSION + "client_reviews": "not a number",
        },
    )
    assert job.description == "Body"
    assert job.job_type == "hourly" and job.budget_max == Decimal("40.00")
    assert (job.proposal_count, job.proposal_count_exact) == (15, False)
    assert job.payment_verified is True and job.client_hire_rate == Decimal("0.5")
    assert job.client_reviews is None and job.client_spend is None


# --- tier 2: prompt ------------------------------------------------------------------


def test_the_prompt_holds_rules_profile_contract_then_the_posting() -> None:
    job = established(
        budget_min=Decimal(9999998), budget_max=Decimal(9999999), job_type="hourly"
    )
    task = tier2.build_task(job, profile=PROFILE, policy=POLICY, new_client=False)
    rules = task.index("Evaluate this opportunity")
    profile = task.index("Candidate factual evidence:\nExample career facts.")
    contract = task.index("OUTPUT CONTRACT")
    posting = task.index("Title: Example posting")
    assert rules < profile < contract < posting
    assert "Upwork note." in task and "Other note." not in task
    assert "min_client_spend" not in task and "tier1" not in task
    assert "Rate: $9999998-$9999999" in task
    assert "(HOURLY rate, not a project total)" in task
    assert "Competing proposals: 10 to 15 (Upwork's own band" in task
    assert '"client_review_count": 12' in task
    assert "NEW CLIENT" not in task
    assert '"job:0": "Example posting"' in task


def test_a_new_client_gets_the_addendum() -> None:
    task = tier2.build_task(
        established(), profile=PROFILE, policy=POLICY, new_client=True
    )
    assert "NEW CLIENT with no Upwork history" in task
    assert "[New client - no prior Upwork history]" in task


@pytest.mark.parametrize(
    ("changes", "line"),
    [
        ({"proposal_count": None}, "Competing proposals: unknown"),
        ({"proposal_count": 12, "proposal_count_exact": True}, "12 (exact count)"),
        ({"proposal_count": 51}, "50+ (genuinely crowded"),
        ({"proposal_count": 4}, "under 5"),
    ],
)
def test_competition_reads_as_the_band(changes: dict[str, Any], line: str) -> None:
    assert line in tier2.competition_line(established(**changes), 50)


@pytest.mark.parametrize(
    ("changes", "line"),
    [
        ({"budget_max": Decimal(1500), "job_type": "fixed"}, "Budget: $1500 (fixed"),
        ({"budget_max": Decimal(900)}, "Budget: $900 (fixed"),
        ({}, None),
    ],
)
def test_the_budget_line_tells_a_rate_from_a_total(
    changes: dict[str, Any], line: str | None
) -> None:
    found = tier2.budget_line(established(**changes))
    assert found == line or (line is not None and found and line in found)


def test_a_small_untyped_top_end_reads_as_an_hourly_rate() -> None:
    line = tier2.budget_line(established(budget_max=Decimal(60)))
    assert line is not None and line.startswith("Rate: up to $60")
    assert "(HOURLY rate, not a project total)" in line


def test_the_prompt_version_names_the_fixed_text() -> None:
    assert tier2.PROMPT_VERSION.startswith("jobscore.1.")
    assert len(tier2.PROMPT_VERSION) == len("jobscore.1.") + 10


# --- tier 2: parse and render (ported contract tests) ---------------------------------


def answer(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "score": 7,
        "fit": "strong",
        "economics": "uncertain",
        "evidence": ["project:1"],
        "requirements": [],
        "gaps": [],
        "conditions": [],
    }
    data.update(changes)
    return data


def test_a_proof_of_concept_budget_never_becomes_a_confirmed_allocation() -> None:
    rendered = tier2.render_assessment(answer(), TEXT, CATALOG, FIXED)
    assert rendered.model_score == 7 and rendered.score == 70
    text = "\n".join(rendered.lines)
    assert "phase allocation is unverified" in text
    assert "Video app" in text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("score", True),
        ("score", 11),
        ("score", "7"),
        ("fit", "perfect"),
        ("evidence", ["project:999"]),
        ("evidence", ["gap:0"]),
        ("gaps", ["project:0"]),
        ("requirements", ["Eight hours of preparation are required."]),
        ("requirements", ["job:999"]),
        ("conditions", ["30-40 billable hours"]),
    ],
)
def test_an_unsupported_assessment_is_rejected(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        tier2.render_assessment(answer(**{field: value}), TEXT, CATALOG, FIXED)


def test_prose_fields_cannot_bypass_the_contract() -> None:
    with pytest.raises(ValueError):
        tier2.render_assessment(
            answer(reason="Booking app ships agent automation."), TEXT, CATALOG, FIXED
        )


def test_references_do_not_transfer_project_capabilities() -> None:
    rendered = tier2.render_assessment(
        answer(evidence=["project:0"]), TEXT, CATALOG, FIXED
    )
    text = "\n".join(rendered.lines)
    assert "Booking app" in text
    assert "FFmpeg" not in text


def test_exact_requirements_and_boundaries_are_quoted() -> None:
    rendered = tier2.render_assessment(
        answer(
            score=2,
            fit="weak",
            requirements=["job:0"],
            gaps=["gap:0"],
            conditions=["eligibility"],
        ),
        "Must have production example-platform.",
        CATALOG,
        FIXED,
    )
    assert rendered.lines == (
        "Fit: weak. Economics: uncertain.",
        "Candidate evidence to review: Video app.",
        'Posting excerpt: "Must have production example-platform.".',
        "Candidate boundary: No production example-platform experience.",
        "Confirm eligibility against the quoted mandatory requirements.",
    )
    assert tier2.fit_band(rendered.score) == "low"


def test_a_strong_fit_needs_evidence() -> None:
    with pytest.raises(ValueError):
        tier2.render_assessment(answer(evidence=[]), TEXT, CATALOG, FIXED)


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [("", ""), ("```json\n", "\n```"), ("", "\nUnverified claims about 40 hours.")],
)
def test_model_commentary_is_never_rendered(prefix: str, suffix: str) -> None:
    data = tier2.parse_assessment(prefix + json.dumps(answer()) + suffix)
    rendered = tier2.render_assessment(data, TEXT, CATALOG, FIXED)
    assert "40 hours" not in "\n".join(rendered.lines)


@pytest.mark.parametrize("content", ["not JSON", '{"score":', "{}\n{}"])
def test_malformed_or_multiple_objects_fail_closed(content: str) -> None:
    with pytest.raises(ValueError):
        tier2.render_assessment(tier2.parse_assessment(content), TEXT, CATALOG, FIXED)


def test_a_profile_without_a_portfolio_is_one_career_entry() -> None:
    catalog = tier2.evidence_catalog("Just career text.", ScoringPolicy())
    assert catalog == {
        "career": {"label": "Career and skills", "text": "Just career text."}
    }


def test_the_schema_admits_a_contract_answer_and_closes_the_ids() -> None:
    schema = tier2.response_schema(CATALOG, tier2.job_excerpts(TEXT))
    validator = Draft202012Validator(schema)
    assert validator.is_valid(answer(requirements=["job:0"]))
    assert not validator.is_valid(answer(evidence=["project:9"]))
    assert not validator.is_valid(answer(gaps=["gap:3"]))
    assert not validator.is_valid({**answer(), "reason": "prose"})
    no_gaps = tier2.response_schema(
        tier2.evidence_catalog(PROFILE, ScoringPolicy()), {}
    )
    assert not Draft202012Validator(no_gaps).is_valid(answer(gaps=["gap:0"]))


@pytest.mark.parametrize(
    ("score", "band"), [(100, "high"), (80, "high"), (79, "medium"), (60, "medium")]
)
def test_the_fit_band_follows_the_score(score: int, band: str) -> None:
    assert tier2.fit_band(score) == band
    assert tier2.fit_band(59) == "low"


# --- the digest -----------------------------------------------------------------------


def fields(**changes: str) -> dict[str, str]:
    values = {
        "message": "Body",
        EXTENSION + "proposal_count": "12",
        EXTENSION + "client_spend": "12000",
        EXTENSION + "client_rating": "4.93",
    }
    values.update(
        {EXTENSION + k if k != "message" else k: v for k, v in changes.items()}
    )
    return values


def test_small_moves_in_competition_and_metrics_keep_the_digest() -> None:
    base = input_digest("Example", fields(), 50)
    assert input_digest("Example", fields(proposal_count="14"), 50) == base
    assert input_digest("Example", fields(client_spend="15000"), 50) == base
    assert input_digest("Example", fields(client_rating="4.94"), 50) == base


@pytest.mark.parametrize(
    "changes",
    [
        {"message": "A different body"},
        {"proposal_count": "18"},
        {"proposal_count": "60"},
        {"client_spend": "300"},
        {"client_payment_verified": "false"},
        {"budget_max": "900"},
    ],
)
def test_a_new_description_or_competition_band_changes_it(
    changes: dict[str, str],
) -> None:
    assert input_digest("Example", fields(**changes), 50) != input_digest(
        "Example", fields(), 50
    )


# --- one assessment through a fake runner ---------------------------------------------


class FakeRunner:
    def __init__(self, answer: object = None, error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> Mapping[str, object]:
        self.calls.append(kwargs)
        kwargs["heartbeat"]()
        if self.error is not None:
            raise self.error
        assert isinstance(self.answer, dict)
        return self.answer


STORED = StoredProfile(
    profile=PROFILE, policy=POLICY, version=3, scoring_account_id=ACCOUNT
)


def run(runner: Any, job: JobFacts) -> tuple[Outcome, list[int]]:
    beats: list[int] = []
    outcome = assess(
        job,
        STORED,
        runner=runner,
        workspace_id=WORKSPACE,
        heartbeat=lambda: beats.append(1),
    )
    return outcome, beats


def test_a_passing_job_is_scored_ten_times_the_model_answer() -> None:
    runner = FakeRunner(answer(score=8, requirements=["job:1"]))
    outcome, beats = run(runner, established(payment_verified=None))
    assert (outcome.fit, outcome.score, outcome.model) == ("high", 80, True)
    assert outcome.lines[-1].startswith("Payment verification is unknown")
    [call] = runner.calls
    assert call["account_id"] == ACCOUNT and call["workspace_id"] == WORKSPACE
    assert call["model_id_key"] == "leads.scoring.model_id"
    assert "Candidate factual evidence:\nExample career facts." in call["task"]
    assert beats == [1]


def test_a_held_job_never_reaches_the_model() -> None:
    runner = FakeRunner(answer())
    outcome, _ = run(runner, established(client_rating=Decimal("3.1")))
    assert outcome.fit == "low" and outcome.score is None and not outcome.model
    assert "3.1, below 4.5" in outcome.lines[1]
    assert runner.calls == []


def test_each_failed_clause_carries_its_code() -> None:
    decided = run_tier1(
        established(
            payment_verified=False,
            client_spend=Decimal("10"),
            client_rating=Decimal("3.1"),
            client_hire_rate=Decimal("0.1"),
            proposal_count=80,
        ),
        Tier1Policy(),
    )
    assert decided.codes == (
        "payment_unverified",
        "low_spend",
        "low_rating",
        "low_hire_rate",
        "high_proposals",
    )
    assert len(decided.codes) == len(decided.failed)
    new = {"client_spend": Decimal(0), "client_reviews": 0}
    unverified = established(**new, payment_verified=False)
    assert run_tier1(unverified, Tier1Policy()).codes == ("payment_unverified",)
    crowded = established(**new, proposal_count=80)
    assert run_tier1(crowded, Tier1Policy()).codes == ("high_proposals",)
    assert run_tier1(established(), Tier1Policy()).codes == ()


def test_a_promoted_job_is_scored_with_its_held_clauses_as_conditions() -> None:
    runner = FakeRunner(answer(score=7, requirements=["job:1"]))
    held = established(client_rating=Decimal("3.1"), proposal_count=80)
    outcome = assess(
        held,
        STORED,
        runner=runner,
        workspace_id=WORKSPACE,
        heartbeat=lambda: None,
        promoted=True,
    )
    assert outcome.model and outcome.score == 70
    assert outcome.lines[-3] == PROMOTED_LINE
    assert "3.1, below 4.5" in outcome.lines[-2]
    assert outcome.lines[-1].startswith("More than 50 proposals")
    assert HELD_LINE not in outcome.lines
    assert outcome.uncertainty in ("medium", "high")
    assert len(runner.calls) == 1
    # A promotion never makes up a missing description.
    bare = assess(
        established(client_rating=Decimal("3.1"), description=None),
        STORED,
        runner=runner,
        workspace_id=WORKSPACE,
        heartbeat=lambda: None,
        promoted=True,
    )
    assert bare.fit == "needs_information" and len(runner.calls) == 1


def test_a_job_without_its_description_needs_information() -> None:
    runner = FakeRunner(answer())
    outcome, _ = run(runner, established(description=None))
    assert outcome.fit == "needs_information" and "Paste the full posting" in "".join(
        outcome.lines
    )
    assert runner.calls == []


def test_a_usage_limit_defers_and_other_failures_count() -> None:
    deferred, _ = run(FakeRunner(error=OneShotDeferred("usage_limited")), established())
    assert deferred.deferred == "usage_limited" and deferred.failure is None
    failed, _ = run(FakeRunner(error=OneShotFailed("output_invalid")), established())
    assert failed.failure == "output_invalid" and not failed.deferred
    invalid, _ = run(FakeRunner(answer(score=42)), established())
    assert invalid.failure == ASSESSMENT_INVALID
    missing, _ = run(None, established())
    assert missing.deferred == "adapter_unavailable" and missing.failure is None


@pytest.mark.parametrize(
    "kind",
    [
        "credential_invalid",
        "executable_unavailable",
        "runtime_error",
        "deadline_exceeded",
    ],
)
def test_an_account_or_deployment_failure_pauses_instead_of_counting(
    kind: str,
) -> None:
    paused, _ = run(FakeRunner(error=OneShotFailed(kind)), established())
    assert paused.deferred == kind and paused.failure is None


def test_a_login_owned_by_another_account_pauses() -> None:
    paused, _ = run(FakeRunner(error=OneShotCredentialNotOwned("x")), established())
    assert paused.deferred == "credential_not_owned"


# --- the model call log's view of one assessment ----------------------------------


class RecordingTracker:
    """Stands in for the call log: what each tracked call reported."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Call]] = []

    @contextmanager
    def call(
        self, purpose: str, model_id_key: str, defers: Any = None
    ) -> Iterator[Call]:
        reported = Call()
        self.calls.append((purpose, model_id_key, reported))
        yield reported


class UsageRunner(FakeRunner):
    def run(self, **kwargs: Any) -> Mapping[str, object]:
        kwargs["on_usage"](900, 120)
        return super().run(**kwargs)


def tracked(runner: Any, job: JobFacts) -> tuple[Outcome, RecordingTracker]:
    tracker = RecordingTracker()
    outcome = assess(
        job,
        STORED,
        runner=runner,
        workspace_id=WORKSPACE,
        heartbeat=lambda: None,
        track=tracker,  # type: ignore[arg-type]
    )
    return outcome, tracker


def test_a_model_call_reports_its_tokens_and_a_held_job_makes_none() -> None:
    outcome, tracker = tracked(UsageRunner(answer(score=8)), established())
    assert outcome.score == 80
    [(purpose, key, reported)] = tracker.calls
    assert (purpose, key) == ("scoring", "leads.scoring.model_id")
    assert (reported.input_tokens, reported.output_tokens) == (900, 120)
    assert reported.unusable_kind is None
    _, none = tracked(UsageRunner(answer()), established(client_rating=Decimal("3.1")))
    assert none.calls == []


def test_an_answer_that_fails_its_checks_is_logged_as_unusable() -> None:
    outcome, tracker = tracked(FakeRunner(answer(score=42)), established())
    assert outcome.failure == ASSESSMENT_INVALID
    [(_, _, reported)] = tracker.calls
    assert reported.unusable_kind == ASSESSMENT_INVALID


# --- invited jobs ---------------------------------------------------------------------


def test_an_invited_job_is_never_held_and_its_clauses_go_to_the_model() -> None:
    policy = Tier1Policy()
    crowded = established(
        proposal_count=80, client_spend=Decimal("10"), payment_verified=False
    )
    held = run_tier1(crowded, policy)
    assert not held.passed and held.codes
    invited = run_tier1(JobFacts(**{**crowded.__dict__, "invited": True}), policy)
    assert invited.passed
    assert invited.codes == ()
    assert invited.soft == held.failed


def test_an_invited_new_client_is_not_held_either() -> None:
    policy = Tier1Policy()
    fresh = established(
        client_spend=Decimal("0"), client_reviews=0, payment_verified=False
    )
    assert not run_tier1(fresh, policy).passed
    result = run_tier1(JobFacts(**{**fresh.__dict__, "invited": True}), policy)
    assert result.passed and result.new_client and result.soft


def test_an_invited_job_that_passes_is_unchanged() -> None:
    policy = Tier1Policy()
    job = established()
    assert run_tier1(JobFacts(**{**job.__dict__, "invited": True}), policy) == (
        run_tier1(job, policy)
    )


def test_the_invited_field_is_read_from_the_job_fields() -> None:
    assert JobFacts.from_fields("Example", fields(invited="true")).invited
    assert not JobFacts.from_fields("Example", fields(invited="false")).invited
    assert not JobFacts.from_fields("Example", fields()).invited


def test_the_model_reads_that_the_job_is_an_invitation() -> None:
    line = "[Invited - the client invited the freelancer to this job]"
    invited = JobFacts(**{**established().__dict__, "invited": True})
    task = tier2.build_task(invited, profile=PROFILE, policy=POLICY, new_client=False)
    assert line in task
    plain = tier2.build_task(
        established(), profile=PROFILE, policy=POLICY, new_client=False
    )
    assert line not in plain


def test_only_an_invitation_moves_the_digest() -> None:
    base = input_digest("Example", fields(), 50)
    assert input_digest("Example", fields(invited="false"), 50) == base
    assert input_digest("Example", fields(invited="true"), 50) != base


def test_an_invited_job_keeps_the_unknown_payment_condition() -> None:
    policy = Tier1Policy()
    job = established(payment_verified=None, proposal_count=80)
    held = run_tier1(job, policy)
    assert not held.passed and held.soft == ()
    invited = run_tier1(JobFacts(**{**job.__dict__, "invited": True}), policy)
    assert invited.passed
    assert invited.soft == (*held.failed, tier1.UNKNOWN_PAYMENT)


def test_an_invited_new_client_keeps_every_condition() -> None:
    policy = Tier1Policy()
    fresh = established(
        client_spend=Decimal("0"),
        client_reviews=0,
        payment_verified=False,
        proposal_count=80,
    )
    held = run_tier1(fresh, policy)
    assert held.codes == ("payment_unverified",)
    invited = run_tier1(JobFacts(**{**fresh.__dict__, "invited": True}), policy)
    assert invited.passed
    assert invited.soft == (
        *held.failed,
        f"More than {policy.max_proposals} proposals already.",
    )
