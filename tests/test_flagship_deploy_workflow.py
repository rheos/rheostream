"""The flagship deploy workflow (Prompt 6): `.github/workflows/deploy-flagship.yml`.

Seams under test: the parsed workflow's inert-until-secrets structure — the
job-level `needs`/`if` guard on `deploy`, and that the only `curl` in the file
lives in `deploy`, never `gate`. This is the shape the workflow must keep so
that, absent the three repository secrets, every run is a green no-op.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "deploy-flagship.yml"

RAW_TEXT = WORKFLOW_PATH.read_text(encoding="utf-8")
# PyYAML reads the bare `on:` key as the boolean `True`, not the string "on".
WORKFLOW: dict[Any, Any] = yaml.safe_load(RAW_TEXT)

JOBS = WORKFLOW["jobs"]
GATE = JOBS["gate"]
DEPLOY = JOBS["deploy"]

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def _all_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return job.get("steps", [])


def _step_runs(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in _all_steps(job) if "run" in step]


# --- triggers, permissions, concurrency --------------------------------------------


def test_triggers_are_exactly_push_to_main_and_workflow_dispatch() -> None:
    on = WORKFLOW[True]  # PyYAML: the bare `on:` key parses as boolean True
    assert set(on.keys()) == {"push", "workflow_dispatch"}
    assert on["push"] == {"branches": ["main"]}


def test_permissions_are_contents_read_only() -> None:
    assert WORKFLOW["permissions"] == {"contents": "read"}


def test_concurrency_group_prevents_overlapping_deploys() -> None:
    concurrency = WORKFLOW["concurrency"]
    assert concurrency["group"] == "deploy-flagship"
    assert concurrency["cancel-in-progress"] is False


# --- job-level guard: deploy needs gate and checks its ready output ----------------


def test_deploy_needs_gate_and_requires_ready_true() -> None:
    assert DEPLOY["needs"] == "gate"
    condition = " ".join(DEPLOY["if"].split())
    assert condition == (
        "github.repository == 'rheos/rheostream' && needs.gate.outputs.ready == 'true'"
    )


def test_gate_guards_on_the_repository_too() -> None:
    assert "github.repository == 'rheos/rheostream'" in GATE["if"]


def test_gate_exposes_a_ready_output() -> None:
    assert "ready" in GATE["outputs"]


# --- curl lives only in deploy, never in gate ---------------------------------------


def test_the_only_curl_in_the_file_is_in_the_deploy_job() -> None:
    assert not any("curl" in run for run in _step_runs(GATE))
    deploy_runs = _step_runs(DEPLOY)
    assert any("curl" in run for run in deploy_runs)
    assert RAW_TEXT.count("curl") == sum(run.count("curl") for run in deploy_runs)


def test_gate_pulls_the_secret_value_only_through_env() -> None:
    """`secrets.COOLIFY_BASE_URL` (the actual secret expression) is read into an
    `env:` mapping and nowhere else — never inlined into a job-level `if:` or a
    bare `run:` string. The plain word `COOLIFY_BASE_URL` still appears in the
    gate step's shell (as a naked env-var reference and in its notice text);
    it is the *secret expression* this test confines to `env:`."""
    secret_expr = "secrets.COOLIFY_BASE_URL"
    for job_name, job in JOBS.items():
        assert secret_expr not in job.get("if", ""), (
            f"{job_name}.if references the secret directly"
        )
        for step in _all_steps(job):
            assert secret_expr not in step.get("run", ""), (
                f"{job_name} step's run references the secret directly"
            )
            assert secret_expr not in step.get("if", ""), (
                f"{job_name} step's if references the secret directly"
            )
    assert RAW_TEXT.count(secret_expr) == sum(
        1
        for job in JOBS.values()
        for step in _all_steps(job)
        if secret_expr in step.get("env", {}).get("COOLIFY_BASE_URL", "")
    )


# --- no infrastructure literal anywhere in the file ---------------------------------


def test_no_infrastructure_literal_in_the_file() -> None:
    assert not IPV4_RE.search(RAW_TEXT), "an IPv4 address literal is in the workflow"
    assert not UUID_RE.search(RAW_TEXT), "a UUID literal is in the workflow"
    assert "coolify.rheo" not in RAW_TEXT
    assert "rheo.ca" not in RAW_TEXT
