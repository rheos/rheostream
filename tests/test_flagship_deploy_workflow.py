"""The flagship deploy workflow: `.github/workflows/deploy-flagship.yml`.

Seams under test, on the parsed workflow:

- Inert until secrets: `deploy` needs `gate` and runs only on its `ready`
  output, and the only `curl` in the file lives in `deploy`, never `gate`.
- CI first (issue #154): the workflow runs after "Repository checks" completes
  on main, never on a bare push, and the gate waits for every push-event CI
  workflow on the SHA before it sets `ready`.
- A green run means deployed (issue #154): the deploy step polls the Coolify
  deployment it started until `finished`, fails on `failed`/`cancelled*`, and has
  a deadline. A refused poll (401/403) fails at once and names the missing `read`
  ability (issue #172); 429 and 5xx stay transient.
- Preview deployments off (issue #164): before deploying, the job reads the
  application and refuses unless `settings.is_preview_deployments_enabled` is
  exactly false.
- Public logs: the base URL, the host and the deployment id stay out of them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
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


def _step(job: dict[str, Any], step_id: str) -> dict[str, Any]:
    matches = [step for step in _all_steps(job) if step.get("id") == step_id]
    assert len(matches) == 1, f"expected one step with id {step_id!r}"
    return matches[0]


def _squash(text: str) -> str:
    return " ".join(text.split())


DEPLOY_RUN = "\n".join(_step_runs(DEPLOY))
LEAKY_VARS = ("$COOLIFY_BASE_URL", "$base", "$COOLIFY_APP_UUID", "$COOLIFY_TOKEN")


# --- triggers, permissions, concurrency --------------------------------------------


def test_triggers_are_ci_completion_on_main_and_workflow_dispatch() -> None:
    on = WORKFLOW[True]  # PyYAML: the bare `on:` key parses as boolean True
    assert set(on.keys()) == {"workflow_run", "workflow_dispatch"}
    assert on["workflow_run"] == {
        "workflows": ["Repository checks"],
        "types": ["completed"],
        "branches": ["main"],
    }


def test_the_triggering_workflow_name_matches_repository_checks() -> None:
    """A renamed CI workflow would silently stop every deploy."""
    checks_path = ROOT / ".github" / "workflows" / "repository-checks.yml"
    checks = yaml.safe_load(checks_path.read_text(encoding="utf-8"))
    assert checks["name"] in WORKFLOW[True]["workflow_run"]["workflows"]


def test_permissions_are_contents_read_only_at_the_top() -> None:
    assert WORKFLOW["permissions"] == {"contents": "read"}
    assert GATE["permissions"] == {"actions": "read", "contents": "read"}
    assert "permissions" not in DEPLOY


def test_concurrency_group_prevents_overlapping_deploys() -> None:
    concurrency = WORKFLOW["concurrency"]
    assert concurrency["group"] == "deploy-flagship"
    assert concurrency["cancel-in-progress"] is False


def test_jobs_that_read_the_secrets_use_the_production_environment() -> None:
    assert GATE["environment"] == "production"
    assert DEPLOY["environment"] == "production"


# --- gate: only a successful CI run for a push to this repository's main ---------


def test_gate_runs_only_for_successful_push_ci_on_main_or_dispatch_on_main() -> None:
    condition = _squash(GATE["if"])
    assert "github.repository == 'rheos/rheostream'" in condition
    assert (
        "(github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main')"
        in condition
    )
    for clause in (
        "github.event_name == 'workflow_run'",
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
    ):
        assert clause in condition, clause


def test_gate_waits_for_every_push_ci_workflow_on_the_sha() -> None:
    ci = _step(GATE, "ci")
    assert ci["if"] == "steps.secrets.outputs.set == 'true'"
    run = ci["run"]
    assert ci["env"]["RUN_HEAD_SHA"] == "${{ github.event.workflow_run.head_sha }}"
    assert "actions/runs" in run
    assert "-f head_sha=" in run
    assert "-f event=push" in run
    # Fails on any completed run that did not succeed, waits for pending ones,
    # requires Repository checks itself, and gives up at a deadline.
    assert 'IN("success", "skipped", "neutral") | not' in run
    assert "exit 1" in run
    assert 'select(.status != "completed")' in run
    assert "repository-checks.yml" in run
    assert "deadline" in run
    assert int(ci["env"]["CI_WAIT_SECONDS"]) < GATE["timeout-minutes"] * 60


_CI_SHA = "0123456789abcdef0123456789abcdef01234567"
_GH_STUB = """#!/usr/bin/env bash
# Stub gh: apply the --jq filter to the fixture file.
q=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--jq" ]; then q="$2"; shift; fi
  shift
done
exec jq -c "$q" "$GH_FIXTURE"
"""


def _run_ci_gate(tmp_path: Path, conclusions: dict[str, str]) -> tuple[int, str, str]:
    """Run the gate's CI step against a stubbed `gh` returning completed runs.

    Returns (exit code, combined output, contents of $GITHUB_OUTPUT)."""
    bash, jq = shutil.which("bash"), shutil.which("jq")
    assert bash and jq, "bash and jq are needed to exercise the CI gate script"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(_GH_STUB, encoding="utf-8")
    gh.chmod(0o755)
    runs = [
        {
            "id": 100 + index,
            "workflow_id": index + 1,
            "path": f".github/workflows/{name}",
            "status": "completed",
            "conclusion": conclusion,
        }
        for index, (name, conclusion) in enumerate(conclusions.items())
    ]
    fixture = tmp_path / "runs.json"
    fixture.write_text(json.dumps({"workflow_runs": runs}), encoding="utf-8")
    output = tmp_path / "github_output"
    output.touch()
    script = tmp_path / "ci.sh"
    script.write_text(_step(GATE, "ci")["run"], encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "GH_FIXTURE": str(fixture),
        "GITHUB_OUTPUT": str(output),
        "GITHUB_REPOSITORY": "example/repo",
        "EVENT_NAME": "workflow_run",
        "RUN_HEAD_SHA": _CI_SHA,
        "CI_WAIT_SECONDS": "2",
        "CI_POLL_SECONDS": "1",
    }
    proc = subprocess.run(
        [bash, str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr, output.read_text()


def test_ci_gate_passes_only_when_every_workflow_succeeded(tmp_path: Path) -> None:
    code, log, outputs = _run_ci_gate(
        tmp_path, {"repository-checks.yml": "success", "other-ci.yml": "success"}
    )
    assert code == 0, log
    assert "passed=true" in outputs


@pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
def test_ci_gate_steps_aside_quietly_on_skipped_or_neutral(
    tmp_path: Path, conclusion: str
) -> None:
    """A skipped or neutral CI run proves nothing about the commit: no deploy,
    but a notice rather than a red run."""
    code, log, outputs = _run_ci_gate(
        tmp_path, {"repository-checks.yml": "success", "other-ci.yml": conclusion}
    )
    assert code == 0, log
    assert "passed=true" not in outputs
    assert "passed=false" in outputs
    assert "::notice::deploy skipped" in log
    assert "::error::" not in log


@pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
def test_ci_gate_steps_aside_when_repository_checks_itself_is_not_success(
    tmp_path: Path, conclusion: str
) -> None:
    code, log, outputs = _run_ci_gate(tmp_path, {"repository-checks.yml": conclusion})
    assert code == 0, log
    assert "passed=true" not in outputs
    assert "::notice::deploy skipped" in log


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_ci_gate_fails_loudly_on_a_failed_workflow(
    tmp_path: Path, conclusion: str
) -> None:
    code, log, outputs = _run_ci_gate(
        tmp_path, {"repository-checks.yml": "success", "other-ci.yml": conclusion}
    )
    assert code == 1, log
    assert "passed=true" not in outputs
    assert "::error::CI did not succeed" in log


def test_gate_steps_aside_when_main_has_moved_on() -> None:
    tip = _step(GATE, "tip")
    assert tip["if"] == "steps.ci.outputs.passed == 'true'"
    assert "commits/main" in tip["run"]
    assert GATE["outputs"]["ready"] == "${{ steps.tip.outputs.ready }}"


# --- job-level guard: deploy needs gate and checks its ready output ----------------


def test_deploy_needs_gate_and_requires_ready_true() -> None:
    assert DEPLOY["needs"] == "gate"
    assert _squash(DEPLOY["if"]) == (
        "github.repository == 'rheos/rheostream' && needs.gate.outputs.ready == 'true'"
    )


# --- deploy: poll the deployment to a final status ----------------------------------


def test_deploy_polls_the_started_deployment_until_it_finishes() -> None:
    assert "/api/v1/deploy?uuid=" in DEPLOY_RUN
    assert ".deployments[0].deployment_uuid" in DEPLOY_RUN
    assert '/api/v1/deployments/$deployment"' in DEPLOY_RUN
    assert "finished)" in DEPLOY_RUN
    assert "failed|cancelled*)" in DEPLOY_RUN
    # The success exit sits under `finished`, the failure exit under failed/cancelled.
    finished = DEPLOY_RUN.index("finished)")
    failed = DEPLOY_RUN.index("failed|cancelled*)")
    assert DEPLOY_RUN.index("exit 0", finished) < failed
    assert DEPLOY_RUN.index("exit 1", failed) < DEPLOY_RUN.index("queued|in_progress)")


def test_deploy_poll_has_a_deadline_inside_the_job_timeout() -> None:
    step = _step(DEPLOY, "deploy")
    wait = int(step["env"]["DEPLOY_WAIT_SECONDS"])
    assert "deadline" in DEPLOY_RUN
    # Room for the 60 s deploy request and one last 30 s poll.
    assert wait + 120 < DEPLOY["timeout-minutes"] * 60


def test_deploy_poll_tolerates_transient_errors() -> None:
    assert 'state="transient: curl exit $curl_exit"' in DEPLOY_RUN
    assert 'state="transient: status $code"' in DEPLOY_RUN


# --- running the deploy job's steps against a stubbed Coolify ----------------------

# Stub curl: answers by URL, records each request's method and path (never a
# header), and replays a scripted list of status codes for the status poll.
_CURL_STUB = """#!/usr/bin/env bash
out=""; url=""; method=GET
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -w|-H|--connect-timeout|--max-time) shift ;;
    -X) method="$2"; shift ;;
    -*) ;;
    *) url="$1" ;;
  esac
  shift
done
echo "$method ${url#*://*/}" >> "$CURL_LOG"
case "$url" in
  */api/v1/deploy\\?uuid=*)
    code=200
    body='{"deployments":[{"deployment_uuid":"deployment-example"}]}'
    ;;
  */api/v1/applications/*)
    code="$APP_CODE"
    body="$APP_BODY"
    ;;
  */api/v1/deployments/*)
    n=$(( $(cat "$POLL_COUNT" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$POLL_COUNT"
    code=$(echo "$POLL_CODES" | awk -v n="$n" '{print (n <= NF) ? $n : $NF}')
    body='{"message":"no"}'
    if [ "$code" = 200 ]; then body='{"status":"finished"}'; fi
    ;;
  *)
    code=404
    body='{}'
    ;;
esac
printf '%s' "$body" > "$out"
printf '%s' "$code"
"""

_EXAMPLE_BASE_URL = "https://coolify.example.org"
_EXAMPLE_APP_ID = "app-example"


def _run_deploy_step(
    tmp_path: Path, step_id: str, extra_env: dict[str, str]
) -> tuple[int, str, list[str]]:
    """Run one deploy-job step against the stub curl.

    Returns (exit code, combined output, the requests the stub saw)."""
    bash, jq = shutil.which("bash"), shutil.which("jq")
    assert bash and jq, "bash and jq are needed to exercise the deploy steps"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(_CURL_STUB, encoding="utf-8")
    curl.chmod(0o755)
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    curl_log = tmp_path / "curl.log"
    curl_log.touch()
    script = tmp_path / f"{step_id}.sh"
    script.write_text(_step(DEPLOY, step_id)["run"], encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "RUNNER_TEMP": str(runner_temp),
        "CURL_LOG": str(curl_log),
        "POLL_COUNT": str(tmp_path / "poll-count"),
        "COOLIFY_TOKEN": "token-example",
        "COOLIFY_BASE_URL": _EXAMPLE_BASE_URL,
        "COOLIFY_APP_UUID": _EXAMPLE_APP_ID,
        "DEPLOY_WAIT_SECONDS": "20",
        "DEPLOY_POLL_SECONDS": "0",
        **extra_env,
    }
    proc = subprocess.run(
        [bash, str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    log = proc.stdout + proc.stderr
    # The ::add-mask:: lines name the host on purpose; Actions hides them.
    unmasked = "\n".join(
        line for line in log.splitlines() if not line.startswith("::add-mask::")
    )
    assert "token-example" not in log
    assert "example.org" not in unmasked
    assert _EXAMPLE_APP_ID not in unmasked
    return proc.returncode, log, curl_log.read_text().splitlines()


@pytest.mark.parametrize("code", ["401", "403"])
def test_a_refused_status_poll_fails_at_once_naming_the_read_ability(
    tmp_path: Path, code: str
) -> None:
    exit_code, log, requests = _run_deploy_step(
        tmp_path, "deploy", {"POLL_CODES": f"{code} 200"}
    )
    assert exit_code == 1, log
    assert f"::error::Coolify refused the status poll (status {code})" in log
    assert "'read' ability" in log
    # One poll, no retry: the scripted 200 after it was never asked for.
    polls = [line for line in requests if line.startswith("GET api/v1/deployments/")]
    assert len(polls) == 1, requests
    assert "Deployment finished" not in log


@pytest.mark.parametrize("code", ["429", "500", "502", "503"])
def test_rate_limits_and_server_errors_stay_transient(
    tmp_path: Path, code: str
) -> None:
    exit_code, log, requests = _run_deploy_step(
        tmp_path, "deploy", {"POLL_CODES": f"{code} {code} 200"}
    )
    assert exit_code == 0, log
    assert f"Deployment: transient: status {code}" in log
    assert "Deployment finished" in log
    assert "::error::" not in log
    polls = [line for line in requests if line.startswith("GET api/v1/deployments/")]
    assert len(polls) == 3, requests


# --- issue #164: preview deployments must be off before a deploy ------------------


def test_the_preview_check_runs_before_the_deploy_step() -> None:
    ids = [step.get("id") for step in _all_steps(DEPLOY)]
    assert ids == ["preview", "deploy"]
    preview = _step(DEPLOY, "preview")
    assert "if" not in preview  # never skipped
    assert '"$base/api/v1/applications/$COOLIFY_APP_UUID"' in preview["run"]
    # Read-only: a plain GET, no method override.
    assert "-X" not in preview["run"]
    assert "is_preview_deployments_enabled" in preview["run"]


def _app_body(settings: object) -> str:
    return json.dumps({"name": "example", "settings": settings})


def test_the_preview_check_passes_only_when_the_setting_is_false(
    tmp_path: Path,
) -> None:
    exit_code, log, requests = _run_deploy_step(
        tmp_path,
        "preview",
        {
            "APP_CODE": "200",
            "APP_BODY": _app_body({"is_preview_deployments_enabled": False}),
        },
    )
    assert exit_code == 0, log
    assert "Preview deployments are off" in log
    assert requests == [f"GET api/v1/applications/{_EXAMPLE_APP_ID}"]


def test_the_preview_check_refuses_when_preview_deployments_are_on(
    tmp_path: Path,
) -> None:
    exit_code, log, _ = _run_deploy_step(
        tmp_path,
        "preview",
        {
            "APP_CODE": "200",
            "APP_BODY": _app_body({"is_preview_deployments_enabled": True}),
        },
    )
    assert exit_code == 1, log
    assert "::error::preview deployments are enabled" in log


@pytest.mark.parametrize(
    "body",
    [
        _app_body({}),
        _app_body({"is_preview_deployments_enabled": None}),
        _app_body({"is_preview_deployments_enabled": 0}),
        _app_body({"is_preview_deployments_enabled": "false"}),
        _app_body(None),
        json.dumps({"name": "example"}),
        "not json",
    ],
)
def test_the_preview_check_fails_closed_when_it_cannot_confirm(
    tmp_path: Path, body: str
) -> None:
    exit_code, log, _ = _run_deploy_step(
        tmp_path, "preview", {"APP_CODE": "200", "APP_BODY": body}
    )
    assert exit_code == 1, log
    assert "::error::could not confirm preview deployments are off" in log
    assert "Preview deployments are off" not in log


@pytest.mark.parametrize("code", ["401", "403"])
def test_the_preview_check_names_the_read_ability_when_refused(
    tmp_path: Path, code: str
) -> None:
    exit_code, log, _ = _run_deploy_step(
        tmp_path, "preview", {"APP_CODE": code, "APP_BODY": "{}"}
    )
    assert exit_code == 1, log
    assert f"(status {code})" in log
    assert "'read' ability" in log


@pytest.mark.parametrize("code", ["404", "429", "500"])
def test_the_preview_check_fails_on_any_other_error(tmp_path: Path, code: str) -> None:
    exit_code, log, _ = _run_deploy_step(
        tmp_path, "preview", {"APP_CODE": code, "APP_BODY": "{}"}
    )
    assert exit_code == 1, log
    assert f"::error::application settings request failed (status {code})" in log


# --- curl lives only in deploy, never in gate ---------------------------------------


def test_the_only_curl_in_the_file_is_in_the_deploy_job() -> None:
    assert not any("curl" in run for run in _step_runs(GATE))
    deploy_runs = _step_runs(DEPLOY)
    assert any("curl" in run for run in deploy_runs)
    code_lines = [
        line for line in RAW_TEXT.splitlines() if not line.lstrip().startswith("#")
    ]
    assert sum(line.count("curl") for line in code_lines) == sum(
        run.count("curl") for run in deploy_runs
    )


# --- public logs: no URL, host, deployment id or response body ---------------------


def test_deploy_masks_the_host_and_the_deployment_id() -> None:
    assert 'echo "::add-mask::$host"' in DEPLOY_RUN
    assert 'echo "::add-mask::${host%%:*}"' in DEPLOY_RUN  # the hostname without a port
    assert 'echo "::add-mask::$deployment"' in DEPLOY_RUN
    assert DEPLOY_RUN.index("::add-mask::$host") < DEPLOY_RUN.index("curl ")


def test_deploy_never_echoes_the_url_host_id_or_response_body() -> None:
    assert "set -x" not in RAW_TEXT
    # `-S` would print curl's own error text, which names the host.
    assert not re.search(r"curl\s+-\w*S", DEPLOY_RUN)
    for line in DEPLOY_RUN.splitlines():
        stripped = line.strip()
        if not stripped.startswith("echo"):
            continue
        for leak in LEAKY_VARS:
            assert leak not in stripped, f"echo prints {leak}: {stripped}"
        if "::add-mask::" not in stripped:
            assert "$host" not in stripped
            assert "$deployment" not in stripped
    assert "cat " not in DEPLOY_RUN
    assert 'cat "$resp"' not in DEPLOY_RUN
    # Only status-shaped characters of the deployment status reach the log.
    assert "tr -cd 'a-z_-'" in DEPLOY_RUN


def test_gate_pulls_the_secret_value_only_through_env() -> None:
    """`secrets.COOLIFY_BASE_URL` (the actual secret expression) is read into an
    `env:` mapping and nowhere else — never inlined into a job-level `if:` or a
    bare `run:` string."""
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
