"""Jev holds survive real persistence, review, promotion and configuration changes.

Only synthetic postings/profiles; both inference providers are scripted.
"""

import pytest
from harness.jobfeed import call
from harness.jobfeed import collector as collector
from rheo_contracts.judgment import JudgmentFailed
from rheo_core.runtime.judgment import JudgmentRunner
from rheo_leads.scoring import job, prefilter
from rheo_leads.storage import scoring as s
from sqlalchemy import select
from test_job_prefilter import Provider

from postgres.test_job_review import GOOD, promote, review
from postgres.test_job_scoring import final
from postgres.test_job_scoring import scored as scored
from postgres.test_job_scoring_view import jobs, view
from postgres.test_jobfeed_delivery import rows
from postgres.test_leads_intake import intake as intake

pytestmark = pytest.mark.postgres


def setting(scored, key, value):
    call(scored.i, scored.c.owner, "core.settings.set", key=key, value=value)


def prepare(scored, monkeypatch, *, enabled=True, provider=None):
    monkeypatch.setenv("RHEO__runtime__jev__enabled", "true")
    provider = provider or Provider()
    job.HOOKS.judgment = lambda: JudgmentRunner(provider)
    scored.paste("~02100", GOOD)
    full = scored.model(final(8))
    scored.set_profile()
    if enabled:
        setting(scored, prefilter.ENABLED_KEY, True)
    return provider, full


def test_hold_persists_without_final_score_then_promote_bypasses_jev(
    scored, monkeypatch
):
    provider, full = prepare(scored, monkeypatch)
    scored.sweep()
    (assessment,) = scored.assessments("~02100")
    assert assessment["score"] is None
    assert assessment["model_id"] == "jev:jev-test"
    assert assessment["prompt_version"].endswith("/profile.1")
    assert scored.state("~02100")["state"] == "scored"
    assert jobs(view(scored))["Good job"]["stage"] == "held"
    assert not full.starts and len(provider.calls) == 1
    (item,) = review(scored)
    assert item["hold"][0]["code"] == "prefilter"
    assert "40/100" in item["hold"][0]["text"]
    (logged,) = rows(scored.i, select(s.model_call))
    assert logged["purpose"] == "prefilter" and logged["outcome"] == "answered"
    assert (logged["input_tokens"], logged["output_tokens"]) == (123, 4)
    scored.sweep()
    assert len(provider.calls) == 1  # No recurring charge for the same hold.
    promote(scored, "~02100")
    scored.sweep()
    assert scored.assessments("~02100")[-1]["score"] == 80
    assert len(provider.calls) == 1 and len(full.starts) == 1
    assert not review(scored)


@pytest.mark.parametrize(
    "change", ["workspace_off", "deployment_off", "threshold", "model", "profile"]
)
def test_old_holds_are_reconsidered_after_configuration_changes(
    scored, monkeypatch, change
):
    provider, full = prepare(scored, monkeypatch)
    scored.sweep()
    if change == "workspace_off":
        setting(scored, prefilter.ENABLED_KEY, False)
    elif change == "deployment_off":
        monkeypatch.setenv("RHEO__runtime__jev__enabled", "false")
    elif change == "threshold":
        setting(scored, prefilter.THRESHOLD_KEY, 40)
    elif change == "model":
        monkeypatch.setenv("RHEO__runtime__jev__model_id", "jev-next")
        provider.values["experience"] = 3
    else:
        scored.set_profile(goals=["A different fictional goal."])
        provider.values["experience"] = 3
    scored.sweep()
    assert scored.assessments("~02100")[-1]["score"] == 80
    assert len(full.starts) == 1
    assert len(provider.calls) == (1 if change.endswith("off") else 2)


def test_provider_failure_logs_fixed_reason_and_runs_full_scorer(scored, monkeypatch):
    provider, full = prepare(
        scored, monkeypatch, provider=Provider(error=JudgmentFailed("rate_limited"))
    )
    scored.sweep()
    assert scored.assessments("~02100")[-1]["score"] == 80
    assert len(provider.calls) == len(full.starts) == 1
    logged = rows(scored.i, select(s.model_call))
    by_purpose = {r["purpose"]: r for r in logged}
    assert by_purpose["prefilter"]["outcome"] == "failed"
    assert by_purpose["prefilter"]["failure_kind"] == "rate_limited"
    assert by_purpose["scoring"]["outcome"] == "answered"
    assert scored.state("~02100")["attempts"] == 1


def test_deployment_setting_cannot_opt_a_workspace_in(scored, monkeypatch):
    provider, full = prepare(scored, monkeypatch, enabled=False)
    monkeypatch.setenv("RHEO__leads__scoring__prefilter_enabled", "true")
    scored.sweep()
    assert not provider.calls and len(full.starts) == 1
    assert scored.assessments("~02100")[-1]["score"] == 80


def test_profile_changed_during_inference_does_not_leave_a_stale_hold(
    scored, monkeypatch
):
    class UpdatingProvider(Provider):
        async def evaluate(self, request):
            if not self.calls:
                scored.set_profile(goals=["A revised fictional goal."])
            return await super().evaluate(request)

    provider, full = prepare(scored, monkeypatch, provider=UpdatingProvider())
    scored.sweep()
    assert scored.assessments("~02100")[-1]["prompt_version"].endswith("/profile.1")
    provider.values["experience"] = 3
    scored.sweep()
    assert scored.assessments("~02100")[-1]["score"] == 80
    assert len(provider.calls) == 2 and len(full.starts) == 1
