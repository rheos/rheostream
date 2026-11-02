"""Synthetic judgments test bypass, abstention and final-score separation."""

import pytest
from rheo_contracts.judgment import JudgmentFailed, JudgmentResult
from rheo_core.runtime.judgment import JudgmentRunner
from rheo_leads.scoring import prefilter
from rheo_leads.scoring.job import assess
from rheo_leads.scoring.profile import ScoringPolicy
from test_jobscore import STORED, WORKSPACE, FakeRunner, answer, established


class Provider:
    def __init__(self, *, error=None, confidence=1, **values):
        self.calls = []
        self.error = error
        self.confidence = confidence
        self.values = values

    async def evaluate(self, request):
        self.calls.append(request)
        if self.error:
            raise self.error
        values = {key: 0.1 for key in request.questions}
        values.update(
            experience=1,
            mandatory=0.1,
            meets=0.9,
            priced=0.1,
            pay_workable=0.5,
            goals=0.9,
        )
        values.update(self.values)
        return JudgmentResult(
            values, "jev-test", 123, 4, {"experience": self.confidence}
        )


ENABLED = prefilter.Config(enabled=True)


def evaluate(
    provider,
    *,
    config=ENABLED,
    job=None,
    promoted=False,
    profile=STORED,
):
    full = FakeRunner(answer(score=8, requirements=["job:1"]))
    outcome = assess(
        job or established(),
        profile,
        runner=full,
        workspace_id=WORKSPACE,
        heartbeat=lambda: None,
        promoted=promoted,
        filter_config=config,
        filter_runner=JudgmentRunner(provider),
    )
    return outcome, full


def test_hold_is_not_a_final_score_and_names_its_provenance():
    outcome, full = evaluate(Provider())
    assert outcome.score is None and not outcome.model and not full.calls
    assert outcome.lines[0] == prefilter.HELD_LINE
    assert "40/100" in outcome.lines[1] and "50/100" in outcome.lines[1]
    assert outcome.prefilter_model == "jev:jev-test"
    assert outcome.prefilter_version.startswith("jobprefilter.1.config.")


@pytest.mark.parametrize(
    "changes",
    [
        {"config": prefilter.Config()},
        {"promoted": True},
        {"job": established(invited=True)},
    ],
)
def test_disabled_invited_and_promoted_jobs_bypass_jev(changes):
    provider = Provider()
    outcome, full = evaluate(provider, **changes)
    assert outcome.score == 80 and len(full.calls) == 1 and not provider.calls


@pytest.mark.parametrize(
    "job", [established(payment_verified=False), established(description=None)]
)
def test_tier1_and_missing_description_run_before_jev(job):
    provider = Provider()
    outcome, full = evaluate(provider, job=job)
    assert outcome.score is None and not provider.calls and not full.calls


@pytest.mark.parametrize(
    "kind",
    [
        "credential_invalid",
        "rate_limited",
        "provider_unavailable",
        "output_invalid",
        "input_too_large",
        "deadline_exceeded",
    ],
)
def test_provider_failures_fall_through_to_full_scoring(kind):
    outcome, full = evaluate(Provider(error=JudgmentFailed(kind)))
    assert outcome.score == 80 and len(full.calls) == 1


def test_threshold_equality_and_uncertainty_pass_to_full_scorer():
    for provider, config in [
        (Provider(), prefilter.Config(enabled=True, threshold=40)),
        (Provider(confidence=0.2), prefilter.Config(enabled=True)),
        (Provider(experience=2), prefilter.Config(enabled=True)),
    ]:
        outcome, full = evaluate(provider, config=config)
        assert outcome.score == 80 and len(full.calls) == 1


def test_met_requirement_is_not_a_blocker_and_unknown_price_does_not_penalize():
    outcome, _ = evaluate(
        Provider(experience=3, mandatory=0.9, meets=0.9, priced=0.1, pay_workable=0.1)
    )
    assert outcome.score == 80
    outcome, _ = evaluate(Provider(experience=3, mandatory=0.9, meets=0.1))
    assert outcome.score is None and "application requirement" in outcome.lines[2]


def test_workspace_policy_drives_questions_without_personal_defaults():
    profile = STORED.model_copy(
        update={
            "policy": ScoringPolicy(
                goals=["Make fictional animated explainers, not footage edits."],
                exclusions=["No fictional-platform work."],
                economics=[
                    "Use the fictional candidate's negotiated fixed-scope reference."
                ],
            )
        }
    )
    request = prefilter.request(established(), profile, "jev-latest")
    assert "exclusion_0" in request.questions and "goals" in request.questions
    assert "fictional-platform" in request.questions["exclusion_0"].instructions
    assert request.state["candidate_policy"]["economics"] == profile.policy.economics
    bare = prefilter.request(
        established(),
        STORED.model_copy(update={"policy": ScoringPolicy()}),
        "jev-latest",
    )
    assert "goals" not in bare.questions and not any(
        k.startswith("exclusion_") for k in bare.questions
    )
    assert prefilter.config_version(prefilter.Config()) != prefilter.config_version(
        prefilter.Config(threshold=40)
    )


def test_missing_registered_adapter_falls_through_to_full_scorer():
    full = FakeRunner(answer(score=8, requirements=["job:1"]))
    result = assess(
        established(),
        STORED,
        runner=full,
        workspace_id=WORKSPACE,
        heartbeat=lambda: None,
        filter_config=ENABLED,
        filter_runner=None,
    )
    assert result.score == 80 and len(full.calls) == 1
