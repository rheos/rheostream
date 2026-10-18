"""One proposal draft, stage by stage, with no storage of its own.

1. **Analysis**: one model reading of the posting against the full profile (the
   thirteen-field contract in :mod:`.prompts`). An unusable answer leaves the draft
   to run without it, as the ported drafter does.
2. **Drafting**: the letter, and the answer blocks when screening questions are asked,
   under the rules, the owner's notes and examples, the analysis, any anecdotes the
   posting earns, the platform note and the pricing instruction.
3. **Checking**: a model extracts what the draft claims; :func:`.gate.check_claims`
   decides each against the profile. Flagged claims get exactly one corrective redraft,
   then one more check for display; the first pass's flags are kept.
4. **Tidying**: the style lint, then one repair pass when it found something
   repairable, adopted only when it leaves fewer findings.

The caller supplies :class:`Ask`, which runs one model call, and a progress callback.
A failure that is the account's (a usage limit, a refused login) is the caller's to
raise and passes straight through every stage. Only an unusable answer
(:class:`AnswerUnusable`) is absorbed, and only by the stages that can do without one;
the draft itself cannot.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal, Protocol

from rheo_leads.proposals import gate, milestones, pricing, prompts
from rheo_leads.proposals.milestones import Schedule
from rheo_leads.proposals.profile import StoredProposalProfile
from rheo_leads.proposals.render import (
    Draft,
    parse_draft,
    render_draft,
    strip_model_chatter,
)

Role = Literal["analysis", "draft", "gate", "repair", "milestones"]
STAGES = ("Analysis", "Drafting", "Checking", "Tidying")


class AnswerUnusable(Exception):
    """The model answered, but not with something a stage can use."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class Ask(Protocol):
    def __call__(
        self, role: Role, task: str, schema: Mapping[str, Any]
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class JobInput:
    title: str
    description: str
    platform: str = "upwork"
    budget_min: str | None = None
    budget_max: str | None = None
    questions: str | None = None
    pricing_mode: str | None = None
    # The listing's own type (hourly or fixed) and the job's milestones, if any.
    listing_type: str | None = None
    milestones: tuple[tuple[str, Decimal], ...] = ()


@dataclass
class Result:
    draft: Draft
    findings: dict[str, Any]
    # Set when the posting's own "please include" items became the questions.
    seeded_questions: str | None = None
    notes: list[str] = field(default_factory=list)
    # Set when the Pricing stage priced the job for this draft.
    schedule: Schedule | None = None


def _text(answer: Mapping[str, object]) -> str:
    text = answer.get("text")
    if not isinstance(text, str) or not text.strip():
        raise AnswerUnusable("output_invalid")
    return text


def generate(
    job: JobInput,
    profile: StoredProposalProfile,
    ask: Ask,
    report: Callable[[str, int, int], None],
    *,
    today: date | None = None,
) -> Result:
    policy = profile.policy
    context = prompts.job_context(
        job.title,
        job.description,
        platform=job.platform,
        budget_min=job.budget_min,
        budget_max=job.budget_max,
    )
    stages = list(STAGES)

    def at(name: str) -> None:
        report(name, stages.index(name) + 1, len(stages))

    at("Analysis")
    try:
        analysis = prompts.normalize_analysis(
            ask(
                "analysis",
                prompts.analysis_task(profile.profile, context),
                prompts.analysis_schema(),
            )
        )
        analysed = True
    except AnswerUnusable:
        analysis = prompts.empty_analysis()
        analysed = False

    # A "please include" list in the posting is answered like typed questions, but
    # only when none were entered: a hand-entered list always wins.
    seeded: str | None = None
    questions = job.questions
    if not (questions or "").strip() and analysis["embedded_questions"]:
        seeded = "\n".join(analysis["embedded_questions"])
        questions = seeded
    asked = prompts.real_questions(questions)

    anecdotes = prompts.select_anecdotes(
        policy.anecdotes,
        f"{job.title}\n{job.description}",
        analysis["story_asks"],
    )
    # Pricing: a posting that asks for a quote, and no milestones yet, is priced
    # first, so the letter can state the figure. A failure here never sinks the
    # draft: it is written without figures.
    rows = job.milestones
    schedule: Schedule | None = None
    if analysis["asks_quote"] and not rows and policy.pricing is not None:
        stages.insert(1, "Pricing")
        at("Pricing")
        try:
            schedule = milestones.read(
                ask(
                    "milestones",
                    milestones.task(
                        policy.pricing,
                        title=job.title,
                        description=job.description,
                        budget_min=job.budget_min,
                        budget_max=job.budget_max,
                    ),
                    milestones.SCHEMA,
                ),
                today=today or date.today(),
            )
            rows = tuple((s.title, s.amount) for s in schedule.stages)
        except (AnswerUnusable, ValueError):
            schedule = None
    figures = (
        pricing.figures_block(policy.pricing, rows, job.listing_type)
        if rows and policy.pricing is not None
        else ""
    )
    system = prompts.system_prompt(
        platform=job.platform,
        profile=profile.profile,
        policy=policy,
        analysis=analysis,
        anecdotes=anecdotes,
        pricing_mode="milestones" if rows else job.pricing_mode,
        figures=figures,
        questions=bool(asked),
    )
    evidence = gate.Evidence(
        profile=profile.profile,
        terms=tuple(policy.entity_terms),
        stories=tuple(a.story for a in anecdotes),
    )

    def draft(extra: str = "") -> str:
        text = _text(
            ask(
                "draft",
                prompts.draft_task(system, context, asked, extra),
                prompts.TEXT_SCHEMA,
            )
        )
        return strip_model_chatter(text, expect_questions=bool(asked))

    at("Drafting")
    result = draft()

    at("Checking")
    accuracy, checked = _check(ask, result, evidence)
    flagged_first = [f["claim"] for f in accuracy if f["verdict"] == "flagged"]
    retried = False
    if flagged_first:
        retried = True
        result = draft(gate.retry_instruction(accuracy))
        # Checked again for display only: a retry that is still flagged never
        # triggers a second one.
        accuracy, checked = _check(ask, result, evidence)
    earlier = {c.strip().lower() for c in flagged_first}
    for f in accuracy:
        f["flagged_pre_retry"] = f["claim"].strip().lower() in earlier

    at("Tidying")
    style = gate.lint_text(result)
    style_first = list(style)
    repaired = False
    block = gate.repair_block(style)
    if block:
        try:
            candidate = strip_model_chatter(
                _text(
                    ask(
                        "repair",
                        prompts.repair_task(result, block),
                        prompts.TEXT_SCHEMA,
                    )
                ),
                expect_questions=bool(asked),
            )
            after = gate.lint_text(candidate)
            # A repair that trades one tell for another, or loses or renames a
            # section, is a regression, so the original stands.
            same_sections = _questions(candidate) == _questions(result)
            if same_sections and len(after) < len(style):
                result, style, repaired = candidate, after, True
        except AnswerUnusable:
            pass

    parsed = parse_draft(result)
    findings: dict[str, Any] = {
        "accuracy": accuracy,
        "accuracy_checked": checked,
        "flagged_pre_retry": flagged_first,
        "retried": retried,
        "style": style,
        "style_pre_repair": style_first,
        "style_repaired": repaired,
        "analysis": analysis,
        "analysed": analysed,
        "anecdotes": [a.id for a in anecdotes],
        "gate_version": prompts.GATE_VERSION,
        # Whether the letter was given figures to state, and whether this draft
        # priced the job itself.
        "figures": bool(figures),
        "priced": schedule is not None and bool(schedule.stages),
    }
    # The stored sections must be what the person sees; a draft that does not
    # survive its own round trip is stored as parsed.
    if render_draft(parsed).strip() != result.strip():
        findings["normalized"] = True
    return Result(
        draft=parsed, findings=findings, seeded_questions=seeded, schedule=schedule
    )


def _questions(text: str) -> tuple[str, ...]:
    return tuple(a.question for a in parse_draft(text).answers)


def _check(
    ask: Ask, text: str, evidence: gate.Evidence
) -> tuple[list[dict[str, Any]], bool]:
    """The accuracy findings, and whether the check ran at all. A check that could
    not run returns no findings and says so, never a pass."""
    try:
        extracted = ask(
            "gate",
            gate.EXTRACTOR_RULES + text + gate.EXTRACTOR_CLOSING,
            gate.EXTRACTOR_SCHEMA,
        )
        return gate.check_claims(text, extracted, evidence), True
    except (AnswerUnusable, ValueError):
        return [], False
