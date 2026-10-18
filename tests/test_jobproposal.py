"""Proposal drafting (run 3c2a) without a cluster: the draft text form, the style lint
and its repair instruction, the accuracy check, prompt assembly and the pipeline's
stages through a scripted model. Every profile, posting and answer is synthetic."""

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import pytest
from rheo_leads.proposals import gate, prompts
from rheo_leads.proposals.pipeline import (
    STAGES,
    AnswerUnusable,
    JobInput,
    generate,
)
from rheo_leads.proposals.profile import (
    Anecdote,
    ProposalPolicy,
    StoredProposalProfile,
)
from rheo_leads.proposals.render import (
    Answer,
    Draft,
    parse_draft,
    render_draft,
    strip_model_chatter,
)

PROFILE = (
    "Profile-marker-91c2: a fictional developer.\n\n"
    "Portfolio:\n"
    "- Example Shop (example.com): A storefront in Next.js with Stripe billing.\n"
    "- Example Forms: Renders questionnaires from declarative data with Postgres.\n"
)
STORY = Anecdote(
    id="silent-webhook",
    title="A webhook that answered 200 and did nothing",
    story=(
        "My example bot stopped replying while its webhook kept answering 200. "
        "I reproduced the call on the box and found an expired token."
    ),
    tags=["failure", "debugging"],
)


def profile(**policy: Any) -> StoredProposalProfile:
    return StoredProposalProfile(
        profile=PROFILE,
        policy=ProposalPolicy(**policy),
        version=3,
        drafting_account_id=uuid4(),
    )


# --- The text form ----------------------------------------------------------------


def test_a_draft_round_trips_and_escapes_its_own_structure() -> None:
    draft = Draft(
        letter="Opening line.\n---\nA body line that is a bare divider.",
        answers=(
            Answer("What is your rate?", "**Bold** start, then the rate."),
            Answer("When can you start?", "Monday."),
        ),
    )
    text = render_draft(draft)
    assert text.count("\n---\n") == 1
    assert parse_draft(text) == draft


def test_a_cover_letter_label_folds_into_the_letter_and_a_bare_tail_is_kept() -> None:
    folded = parse_draft("\n---\n**Cover Letter:**\nThe letter.\n\n**Rate?**\n$50.")
    assert folded.letter == "The letter."
    assert folded.answers == (Answer("Rate?", "$50."),)
    tail = parse_draft("Letter.\n\n---\n\nSome answers without headings.")
    assert tail.answers == (Answer("Answers", "Some answers without headings."),)


def test_model_chatter_around_the_letter_is_dropped() -> None:
    assert (
        strip_model_chatter(
            "Here's the proposal:\n---\nLetter.", expect_questions=False
        )
        == "Letter."
    )
    assert strip_model_chatter("Letter.\n---", expect_questions=True) == "Letter."
    notes = "Letter.\n\n---\nA couple of notes on choices I made."
    assert strip_model_chatter(notes, expect_questions=False) == "Letter."
    # With questions asked, a heading-less tail may be answers, so it stays.
    assert strip_model_chatter(notes, expect_questions=True) == notes


# --- The style lint ---------------------------------------------------------------


def test_the_lint_finds_tells_per_section_and_across_the_draft() -> None:
    letter = (
        "This is exactly the kind of work I like — and happy to help. "
        "The storefront handles payments and refunds for every order it takes."
    )
    answer = "The storefront handles payments and refunds for every order it takes."
    findings = gate.style_lint(Draft(letter=letter, answers=(Answer("Q?", answer),)))
    rules = {(f["rule"], f["location"]) for f in findings}
    assert ("em_dash", "cover_letter") in rules
    assert ("tic_phrase", "cover_letter") in rules
    assert ("duplicate_answer", "cover_letter") in rules
    block = gate.repair_block(findings)
    # One instruction per rule and place, carrying every phrase it needs.
    assert block.count("[cover_letter] Replace each flagged phrase") == 1
    assert '"exactly the kind of"' in block and '"happy to"' in block
    long_letter = " ".join(["word"] * 360)
    assert any(
        f["rule"] == "long_letter" for f in gate.style_lint(Draft(letter=long_letter))
    )


def test_a_clean_draft_has_no_findings_and_no_repair() -> None:
    clean = Draft(
        letter="The storefront takes payments. Which checkout do you run today?"
    )
    assert gate.style_lint(clean) == []
    assert gate.repair_block([]) == ""


# --- The accuracy check -----------------------------------------------------------


def test_claims_are_checked_against_the_profile_and_the_offered_stories() -> None:
    evidence = gate.Evidence(
        profile=PROFILE, terms=("Example CLI",), stories=(STORY.story,)
    )
    draft = "Example Shop runs Next.js and Kubernetes."
    found = gate.check_claims(
        draft,
        {
            "claims": ["Next.js", "Kubernetes", "Example CLI", "Example SDK"],
            "attributions": [
                {"project": "Example Shop", "attribute": "Stripe billing"},
                {
                    "project": "Example Shop",
                    "attribute": "six-service Kubernetes stack",
                },
                {"project": "Unknown Project", "attribute": "anything"},
            ],
            "process_claims": [
                "I reproduced the call on the box and found an expired token.",
                "I traced a memory leak to a cache.",
            ],
        },
        evidence,
    )
    verdicts = {(f["kind"], f["claim"]): f["verdict"] for f in found}
    assert verdicts == {
        ("entity", "Next.js"): "whitelisted",
        ("entity", "Kubernetes"): "flagged",
        ("entity", "Example CLI"): "whitelisted",
        ("entity", "Example SDK"): "flagged",
        ("attribution", "Example Shop: Stripe billing"): "whitelisted",
        ("attribution", "Example Shop: six-service Kubernetes stack"): "flagged",
        (
            "process",
            "I reproduced the call on the box and found an expired token.",
        ): "whitelisted",
        ("process", "I traced a memory leak to a cache."): "flagged",
    }
    assert all(f["hint"] for f in found if f["verdict"] == "flagged")
    retry = gate.retry_instruction(found)
    assert "<flagged_claim>Kubernetes</flagged_claim>" in retry
    assert "Next.js</flagged_claim>" not in retry


def test_a_reply_of_the_wrong_shape_is_not_a_pass() -> None:
    with pytest.raises(ValueError):
        gate.check_claims("x", {"claims": "React"}, gate.Evidence(profile=PROFILE))


# --- Prompt assembly --------------------------------------------------------------


def test_the_analysis_is_coerced_onto_its_contract() -> None:
    got = prompts.normalize_analysis(
        {
            "must_address": "Fixed price only",
            "asks_quote": "yes",
            "extra": 1,
            "gaps": [None, " ", "Rust"],
        }
    )
    assert set(got) == set(prompts.ANALYSIS_KEYS)
    assert got["must_address"] == ["Fixed price only"]
    assert got["asks_quote"] is True
    assert got["gaps"] == ["Rust"]
    assert prompts.normalize_analysis("not a mapping") == prompts.empty_analysis()


def test_the_system_prompt_carries_the_owner_policy_and_only_what_the_job_earns() -> (
    None
):
    policy = ProposalPolicy(
        writing_notes=["Example Shop was built solo."],
        example_letters="EXAMPLE LETTER marker-5e1",
        platform_notes={"braintrust": "Vetted network note."},
        anecdotes=[STORY],
    )
    analysis = {
        **prompts.empty_analysis(),
        "angle": "Their checkout is the case.",
        "client_values": "orange",
    }
    plain = prompts.system_prompt(
        platform="upwork",
        profile=PROFILE,
        policy=policy,
        analysis=analysis,
        anecdotes=[],
        pricing_mode=None,
        questions=False,
    )
    assert plain.startswith(prompts.PLATFORM_OPENERS["upwork"])
    assert "Profile-marker-91c2" in plain
    assert "- Example Shop was built solo." in plain
    assert "EXAMPLE LETTER marker-5e1" in plain
    assert "ANGLE: Their checkout is the case." in plain
    assert prompts.PRICING_UNKNOWN_INSTRUCTION in plain
    assert "SCREENING QUESTIONS" not in plain
    assert (
        "Vetted network note." not in plain
        and "REAL anecdotes you may draw on" not in plain
    )
    other = prompts.system_prompt(
        platform="braintrust",
        profile=PROFILE,
        policy=policy,
        analysis=analysis,
        anecdotes=[STORY],
        pricing_mode="hourly",
        questions=True,
    )
    assert "PLATFORM: Vetted network note." in other
    assert "REAL anecdotes you may draw on" in other and STORY.story in other
    assert prompts.PRICING_INSTRUCTIONS["hourly"] in other
    assert "SCREENING QUESTIONS" in other
    assert prompts.system_prompt(
        platform="elsewhere",
        profile=PROFILE,
        policy=ProposalPolicy(),
        analysis=analysis,
        anecdotes=[],
        pricing_mode=None,
        questions=False,
    ).startswith(prompts.GENERIC_OPENER)


def test_anecdotes_are_offered_only_when_the_posting_asks_for_a_story() -> None:
    assert prompts.select_anecdotes([STORY], "Build a storefront.", []) == []
    assert prompts.select_anecdotes([STORY], "Tell us about a time it broke.", []) == [
        STORY
    ]
    assert prompts.select_anecdotes(
        [STORY], "Build a storefront.", ["an example you debugged"]
    ) == [STORY]


def test_the_cover_letter_field_label_is_not_a_question() -> None:
    assert (
        prompts.real_questions("Cover Letter:\nWhat is your rate?\n\n")
        == "What is your rate?"
    )


# --- The pipeline -----------------------------------------------------------------


class Script:
    """A model that answers each role from a queue; the last answer repeats."""

    def __init__(self, **answers: list[Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self, role: str, task: str, schema: Mapping[str, Any]
    ) -> Mapping[str, object]:
        self.calls.append((role, task))
        queue = self.answers[role]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


ANALYSIS = {
    **prompts.empty_analysis(),
    "angle": "The storefront proves checkout work.",
    "embedded_questions": ["What is your rate?"],
}
LETTER = (
    "Checkout is where carts are lost. Example Shop takes payments in Next.js."
    "\n\nWhich processor do you use?"
)
WITH_ANSWER = (
    LETTER + "\n\n---\n\n**What is your rate?**\nFifty an hour, to confirm on a call."
)
CLAIMS_OK = {"claims": ["Next.js"], "attributions": [], "process_claims": []}
CLAIMS_BAD = {"claims": ["Kubernetes"], "attributions": [], "process_claims": []}
JOB = JobInput(
    title="Fix our checkout",
    description="Our checkout drops carts. Please include your rate.",
)


def run(
    script: Script, job: JobInput = JOB, **policy: Any
) -> tuple[Any, list[tuple[str, int, int]]]:
    reports: list[tuple[str, int, int]] = []
    result = generate(job, profile(**policy), script, lambda *a: reports.append(a))
    return result, reports


def test_a_draft_runs_every_stage_and_seeds_the_postings_own_questions() -> None:
    script = Script(
        analysis=[ANALYSIS],
        draft=[{"text": WITH_ANSWER}],
        gate=[CLAIMS_OK],
        repair=[{"text": "x"}],
    )
    result, reports = run(script)
    assert [r[0] for r in reports] == list(STAGES)
    assert result.seeded_questions == "What is your rate?"
    assert result.draft.letter == LETTER
    assert result.draft.answers == (
        Answer("What is your rate?", "Fifty an hour, to confirm on a call."),
    )
    assert (
        result.findings["retried"] is False
        and result.findings["accuracy_checked"] is True
    )
    assert result.findings["analysis"]["angle"] == ANALYSIS["angle"]
    assert [role for role, _ in script.calls] == ["analysis", "draft", "gate"]
    draft_task = script.calls[1][1]
    assert "Profile-marker-91c2" in draft_task
    assert (
        "SCREENING QUESTIONS (answer each after the proposal):\nWhat is your rate?"
        in draft_task
    )
    # The posting sits after every rule, introduced as untrusted data.
    assert draft_task.index(prompts.POSTING_INTRO) > draft_task.index("WRITING QUALITY")


def test_typed_questions_win_over_the_postings_own() -> None:
    script = Script(analysis=[ANALYSIS], draft=[{"text": LETTER}], gate=[CLAIMS_OK])
    result, _ = run(
        script,
        JobInput(
            title=JOB.title,
            description=JOB.description,
            questions="Can you start Monday?",
        ),
    )
    assert result.seeded_questions is None
    assert "Can you start Monday?" in script.calls[1][1]


def test_flagged_claims_get_exactly_one_corrective_redraft() -> None:
    script = Script(
        analysis=[ANALYSIS],
        draft=[{"text": LETTER + " Kubernetes."}, {"text": LETTER}],
        gate=[CLAIMS_BAD, CLAIMS_BAD],
    )
    result, _ = run(script)
    roles = [role for role, _ in script.calls]
    assert roles == ["analysis", "draft", "gate", "draft", "gate"]
    assert "<flagged_claim>Kubernetes</flagged_claim>" in script.calls[3][1]
    findings = result.findings
    assert findings["retried"] is True
    assert findings["flagged_pre_retry"] == ["Kubernetes"]
    assert findings["accuracy"][0]["flagged_pre_retry"] is True


def test_a_repair_is_kept_only_when_it_leaves_fewer_findings() -> None:
    tell = (
        "Checkout is where carts are lost — and I am happy to help. "
        "Which processor do you use?"
    )
    better = Script(
        analysis=[ANALYSIS],
        draft=[{"text": tell}],
        gate=[CLAIMS_OK],
        repair=[{"text": LETTER}],
    )
    result, _ = run(
        better, JobInput(title=JOB.title, description="Fix our checkout.", questions="")
    )
    assert result.findings["style_repaired"] is True and result.findings["style"] == []
    assert result.findings["style_pre_repair"]
    assert result.draft.letter == LETTER
    worse = Script(
        analysis=[ANALYSIS],
        draft=[{"text": tell}],
        gate=[CLAIMS_OK],
        repair=[{"text": tell + " Happy to chat — wheelhouse."}],
    )
    kept, _ = run(worse, JobInput(title=JOB.title, description="Fix our checkout."))
    assert kept.findings["style_repaired"] is False and kept.draft.letter == tell


def test_unusable_side_answers_are_absorbed_but_an_unusable_draft_is_not() -> None:
    script = Script(
        analysis=[AnswerUnusable("output_invalid")],
        draft=[{"text": LETTER}],
        gate=[AnswerUnusable("output_invalid")],
    )
    result, _ = run(script)
    assert result.findings["analysed"] is False
    assert (
        result.findings["accuracy_checked"] is False
        and result.findings["accuracy"] == []
    )
    broken = Script(analysis=[ANALYSIS], draft=[{"text": " "}], gate=[CLAIMS_OK])
    with pytest.raises(AnswerUnusable):
        run(broken)


def test_an_account_failure_passes_straight_through() -> None:
    class Limited(Exception):
        pass

    script = Script(analysis=[Limited()], draft=[{"text": LETTER}], gate=[CLAIMS_OK])
    with pytest.raises(Limited):
        run(script)


def test_a_repair_that_drops_an_answer_is_not_kept() -> None:
    tell = "Carts are lost — and I am happy to help. Which processor do you use?"
    with_answer = tell + "\n\n---\n\n**What is your rate?**\nFifty an hour."
    script = Script(
        analysis=[ANALYSIS],
        draft=[{"text": with_answer}],
        gate=[CLAIMS_OK],
        repair=[{"text": LETTER}],
    )
    result, _ = run(script)
    assert result.findings["style_repaired"] is False
    assert result.draft.answers == (Answer("What is your rate?", "Fifty an hour."),)
