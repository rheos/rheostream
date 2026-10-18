"""Proposal pricing (run 3c2c) without a cluster: the budget's shape, the scope-fit
warning, the hourly bridge and the ladder, the figures a draft may state, the
milestone prompt and its reader, and the draft's Pricing stage. Synthetic values."""

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from rheo_leads.proposals import milestones, pricing, prompts
from rheo_leads.proposals.pipeline import STAGES, AnswerUnusable, JobInput, generate
from rheo_leads.proposals.profile import (
    PricingPolicy,
    ProposalPolicy,
    StoredProposalProfile,
)

POLICY = PricingPolicy(
    day_rate=Decimal(200),
    hours_per_day=Decimal(5),
    hourly_default=Decimal(60),
    hourly_floor=Decimal(30),
    rate_context="Rate-context-marker-41: a fictional developer's rate stance.",
    calibration="Calibration-marker-92: a fictional delivery record.",
)
TODAY = date(2026, 10, 18)


def D(value: str | int) -> Decimal:
    return Decimal(value)


def test_the_budget_shape_decides_whether_scope_fit_means_anything() -> None:
    assert pricing.budget_is_project_total(D(1000), D(2000), None)
    assert not pricing.budget_is_project_total(None, D(2000), None)
    assert not pricing.budget_is_project_total(D(1000), None, None)
    assert not pricing.budget_is_project_total(D(1000), D(2000), "hourly")
    assert not pricing.budget_is_project_total(D(20), D(150), None)
    assert (
        pricing.scope_fit_warning([D(1000), D(1400)], D(2000), D(2000), None)
        == pricing.SCOPE_FIT_WARNING
    )
    assert pricing.scope_fit_warning([D(1000), D(1300)], D(2000), D(2000), None) is None
    assert pricing.scope_fit_warning([D(9000)], D(30), D(60), None) is None


def test_the_ladder_and_the_bridge_follow_the_policy() -> None:
    assert pricing.day_rate_hourly(POLICY) == D(40)
    # Unset multi-week rung: halfway between the day-rate hourly and the floor.
    assert pricing.weeks_rate(POLICY) == D(35)
    assert pricing.ladder_rate(POLICY, D(1), "")[0] == D(60)
    assert pricing.ladder_rate(POLICY, D(5), "")[0] == D(40)
    assert pricing.ladder_rate(POLICY, D(15), "")[0] == D(35)
    assert pricing.ladder_rate(POLICY, D(30), "")[0] == D(30)
    rate, why = pricing.ladder_rate(POLICY, None, "ongoing")
    assert (rate, why) == (
        D(30),
        "an open-ended engagement (scope read from the posting)",
    )
    fixed = pricing.suggest(
        POLICY, listing_type="hourly", amounts=[D(400), D(600)], analysis={}
    )
    assert (fixed.rate, fixed.hours, fixed.matches_fixed_quote) == (D(40), D(25), True)
    assert fixed.working_days == D(5)
    assert fixed.note and "the two agree" in fixed.note
    asked = pricing.suggest(
        POLICY,
        listing_type="hourly",
        amounts=[],
        analysis={"asks_quote": True, "scope_size": "day"},
    )
    assert (asked.rate, asked.hours) == (D(60), None)
    assert asked.note and "Suggest milestones" in asked.note
    assert pricing.suggest(POLICY, listing_type="fixed", amounts=[], analysis={}).note


def test_the_figures_are_the_only_amounts_a_draft_may_state() -> None:
    block = pricing.figures_block(
        POLICY, [("Discovery", D(200)), ("Build", D(700))], "hourly"
    )
    assert "- Discovery: $200.00 (1 working-days)" in block
    assert "- Build: $700.00 (3.5 working-days)" in block
    assert "- Total: $900.00 for 4.5 working-days, about 1 calendar week" in block
    assert "- Hourly equivalent: $40/hr, about 22.5 tracked hours." in block
    assert "the form shows $40/hr" in block
    assert "form shows" not in pricing.figures_block(POLICY, [("A", D(200))], "fixed")


def test_the_milestone_task_carries_the_owner_rate_and_reads_the_posting_as_data() -> (
    None
):
    text = milestones.task(
        POLICY,
        title="Example storefront",
        description="Build an example storefront.",
        budget_min="1000",
        budget_max="1500",
    )
    assert "HARD-CAPPED AT $200.00 PER WORKING-DAY" in text
    assert "is $100.00" in text
    assert "roughly 5 productive hours" in text
    assert "Rate-context-marker-41" in text and "Calibration-marker-92" in text
    assert "STATED BUDGET RANGE: $1000 - $1500" in text
    assert text.index("untrusted source material") < text.index("Example storefront")
    bare = milestones.task(
        PricingPolicy(day_rate=D(100), hourly_default=D(30), hourly_floor=D(20)),
        title="t",
        description="d",
        budget_min=None,
        budget_max=None,
    )
    assert "No budget figure is stated" in bare
    assert "THE DEVELOPER" not in bare


def test_the_reader_holds_the_model_to_its_contract() -> None:
    schedule = milestones.read(
        {
            "milestones": [
                {
                    "title": "Discovery",
                    "description": "",
                    "amount": 200,
                    "due_date_offset_days": 1,
                },
                {
                    "title": "Build",
                    "description": "The store.",
                    "amount": 700.5,
                    "due_date_offset_days": 5,
                },
            ],
            "too_vague": False,
            "reason": None,
        },
        today=TODAY,
    )
    assert [(s.title, s.amount, s.due_on) for s in schedule.stages] == [
        ("Discovery", D("200.00"), date(2026, 10, 19)),
        ("Build", D("700.50"), date(2026, 10, 23)),
    ]
    vague = milestones.read(
        {"milestones": [], "too_vague": True, "reason": "No deliverable."}, today=TODAY
    )
    assert (vague.stages, vague.too_vague) == ((), "No deliverable.")
    for broken in (
        {"milestones": [{"title": "A", "amount": 1}], "too_vague": False},
        {"milestones": [{"title": "A", "amount": -1, "due_date_offset_days": 1}]},
        {"milestones": [{"title": "", "amount": 1, "due_date_offset_days": 1}]},
        {"milestones": [], "too_vague": False},
        {"milestones": "no"},
    ):
        with pytest.raises(ValueError):
            milestones.read(broken, today=TODAY)


class Script:
    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self, role: str, task: str, schema: Mapping[str, Any]
    ) -> Mapping[str, object]:
        self.calls.append((role, task))
        answer = self.answers[role]
        if isinstance(answer, Exception):
            raise answer
        return answer


ANALYSIS = {**prompts.empty_analysis(), "asks_quote": True, "angle": "A case."}
SCHEDULE = {
    "milestones": [
        {
            "title": "Discovery",
            "description": "",
            "amount": 200,
            "due_date_offset_days": 1,
        },
        {"title": "Build", "description": "", "amount": 600, "due_date_offset_days": 4},
    ],
    "too_vague": False,
    "reason": None,
}
LETTER = {"text": "Carts are lost at checkout. Which processor do you use?"}
CLAIMS = {"claims": [], "attributions": [], "process_claims": []}
JOB = JobInput(title="Example checkout", description="Fix our checkout. Quote a price.")


def profile(pricing_policy: PricingPolicy | None = POLICY) -> StoredProposalProfile:
    return StoredProposalProfile(
        profile="Profile-marker: a fictional developer.",
        policy=ProposalPolicy(pricing=pricing_policy),
        version=1,
        drafting_account_id=uuid4(),
    )


def run(
    script: Script, job: JobInput = JOB, prof: StoredProposalProfile | None = None
) -> tuple[Any, list[tuple[str, int, int]]]:
    reports: list[tuple[str, int, int]] = []
    result = generate(
        job, prof or profile(), script, lambda *a: reports.append(a), today=TODAY
    )
    return result, reports


def test_a_quote_ask_is_priced_before_drafting_and_the_draft_gets_the_figures() -> None:
    script = Script(analysis=ANALYSIS, milestones=SCHEDULE, draft=LETTER, gate=CLAIMS)
    result, reports = run(script)
    assert [r[0] for r in reports] == [
        "Analysis",
        "Pricing",
        "Drafting",
        "Checking",
        "Tidying",
    ]
    assert reports[1] == ("Pricing", 2, 5)
    assert [role for role, _ in script.calls] == [
        "analysis",
        "milestones",
        "draft",
        "gate",
    ]
    assert result.schedule is not None and len(result.schedule.stages) == 2
    draft_task = dict(script.calls)["draft"]
    assert "PRICING FIGURES" in draft_task
    assert "- Total: $800.00 for 4 working-days" in draft_task
    assert prompts.PRICING_UNKNOWN_INSTRUCTION not in draft_task
    assert result.findings["figures"] is True and result.findings["priced"] is True


def test_existing_milestones_give_figures_without_a_pricing_stage() -> None:
    script = Script(analysis=ANALYSIS, draft=LETTER, gate=CLAIMS)
    job = JobInput(
        title=JOB.title,
        description=JOB.description,
        milestones=(("Whole job", D(400)),),
        pricing_mode="milestones",
    )
    result, reports = run(script, job)
    assert [r[0] for r in reports] == list(STAGES)
    assert "- Whole job: $400.00 (2 working-days)" in dict(script.calls)["draft"]
    assert result.schedule is None and result.findings["priced"] is False


def test_pricing_never_sinks_a_draft_and_needs_a_policy() -> None:
    broken = Script(
        analysis=ANALYSIS,
        milestones=AnswerUnusable("output_invalid"),
        draft=LETTER,
        gate=CLAIMS,
    )
    result, _ = run(broken)
    assert result.schedule is None
    assert prompts.PRICING_UNKNOWN_INSTRUCTION in dict(broken.calls)["draft"]
    invalid = Script(
        analysis=ANALYSIS, milestones={"milestones": "no"}, draft=LETTER, gate=CLAIMS
    )
    assert run(invalid)[0].schedule is None
    unpriced = Script(analysis=ANALYSIS, draft=LETTER, gate=CLAIMS)
    _, reports = run(unpriced, prof=profile(None))
    assert "Pricing" not in [r[0] for r in reports]
    mode = Script(analysis=prompts.empty_analysis(), draft=LETTER, gate=CLAIMS)
    run(mode, JobInput(title="t", description="d", pricing_mode="hourly"))
    assert prompts.PRICING_INSTRUCTIONS["hourly"] in dict(mode.calls)["draft"]
