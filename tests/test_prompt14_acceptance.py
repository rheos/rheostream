"""The Leads-to-Current acceptance authority and disposable-composition contracts."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "leads-current-handoff.json"
OVERRIDE = ROOT / "tests" / "harness" / "compose.prompt14.yaml"
HELPER = ROOT / "tests" / "harness" / "prompt14.sh"
SEED = ROOT / "tests" / "harness" / "prompt14_acceptance.py"

EVIDENCE_MODES = {
    "seeded-durable-url",
    "normal-interaction",
    "harness-runtime-recipe",
    "restricted-token-and-rendered",
}
SEED_TEST = (
    "pytest:tests/postgres/test_prompt14_seed.py::"
    "test_prompt14_seed_reaches_every_seeded_fixture"
)
RESTRICTED_TOKEN_FIXTURES = {
    "handoff-denied-item-live-project",
    "handoff-live-item-denied-project",
}
SIBLING_AUTHORIZATION = (
    "pytest:tests/postgres/test_leads_current_view.py::"
    "test_item_and_project_are_authorized_independently"
)
LEADS_URL = "/leads/detail?ref=<seeded-opportunity-ref>"
SHELL_URL = "/approvals/detail?approval_id=<seeded-approval-uuid>"

REQUIRED_FIXTURES = {
    "qualified-current",
    "qualified-stale",
    "handoff-awaiting-approval",
    "handoff-approved-queued",
    "handoff-executing",
    "handoff-succeeded-live-item-no-project",
    "handoff-succeeded-live-item-live-project",
    "handoff-deleted-item-live-project",
    "handoff-denied-item-live-project",
    "handoff-live-item-denied-project",
    "handoff-unavailable-item-unavailable-project",
    "handoff-cleared-project",
    "handoff-deleted-project",
    "handoff-unresolved",
    "handoff-retryable-no-effect",
    "approval-pending",
    "approval-submitting",
    "approval-approved-queued",
    "approval-executing",
    "approval-succeeded",
    "approval-failed-no-effect",
    "approval-unresolved",
    "approval-refused",
    "approval-expired",
    "approval-stale-binding",
    "approval-denied-or-missing",
    "approval-temporarily-unavailable",
}


def _authority() -> dict[str, object]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def _seeded_labels() -> set[str]:
    """Every label the seed records a URL for: ``x.leads("<label>", ...)`` and
    ``x.approval("<label>", ...)`` calls with a literal label in the harness."""
    tree = ast.parse(SEED.read_text(encoding="utf-8"), filename=str(SEED))
    return {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"leads", "approval"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }


def _helper_commands() -> set[str]:
    """The subcommands ``prompt14.sh`` dispatches (``  name)`` case labels)."""
    lines = HELPER.read_text(encoding="utf-8").splitlines()
    return {
        line.strip()[:-1]
        for line in lines
        if line.startswith("  ") and not line.startswith("   ") and line.endswith(")")
    }


def _split_reference(reference: str) -> tuple[str, str, str, str | None]:
    """``runner:path > title`` or ``runner:path::name``; a table-driven Vitest
    title is written ``template [param]`` and resolves to the template with ``%s``
    replaced by ``param``."""
    runner, target = reference.split(":", 1)
    path_text, test_name = (
        target.split(" > ", 1) if " > " in target else target.split("::", 1)
    )
    param = None
    if runner == "vitest" and test_name.endswith("]") and " [" in test_name:
        test_name, param = test_name[:-1].rsplit(" [", 1)
    return runner, path_text, test_name, param


def _assert_reference_resolves(reference: str) -> None:
    runner, path_text, test_name, param = _split_reference(reference)
    path = ROOT / path_text
    assert path.is_file(), reference
    text = path.read_text(encoding="utf-8")
    if runner == "pytest":
        tree = ast.parse(text, filename=str(path))
        functions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert test_name in functions, reference
    elif runner == "vitest":
        assert json.dumps(test_name) in text, reference
        if param is not None:
            assert "%s" in test_name and json.dumps(param) in text, reference
    else:
        raise AssertionError(f"unsupported acceptance runner: {reference}")


def _names_label(reference: str, label: str) -> bool:
    _runner, _path, test_name, param = _split_reference(reference)
    title = test_name.replace("%s", param) if param is not None else test_name
    return title.startswith(f"{label} ")


def test_acceptance_authority_names_every_manifest_fixture_and_criterion() -> None:
    authority = _authority()
    assert set(authority["fixtures"]) == REQUIRED_FIXTURES
    criteria = authority["criteria"]
    assert isinstance(criteria, list)
    assert [row["criterion"] for row in criteria] == list(range(1, 19))
    for row in criteria:
        assert row["automated"], row
        assert row["prompt15_manual"], row


def test_every_fixture_names_its_own_resolvable_automated_tests() -> None:
    fixtures = _authority()["fixtures"]
    for name, fixture in fixtures.items():
        references = fixture["automated"]
        assert references, name
        for reference in references:
            _assert_reference_resolves(reference)
        assert any(_names_label(reference, name) for reference in references), name


def test_every_fixture_has_one_honest_manual_evidence_mode() -> None:
    authority = _authority()
    fixtures = authority["fixtures"]
    recipes = authority["interaction_recipes"]
    # No reachability gap may pass the acceptance boundary.
    assert authority["seed_gaps"] == {}
    modes = {name: fixture["evidence_mode"] for name, fixture in fixtures.items()}
    assert set(modes.values()) <= EVIDENCE_MODES, modes

    # URL claims track exactly what the disposable seed writes, nothing more, and
    # the seed execution test asserts every one of them.
    seeded = {name for name, mode in modes.items() if mode == "seeded-durable-url"}
    assert seeded == _seeded_labels()
    for name, fixture in fixtures.items():
        if name not in seeded:
            assert "urls" not in fixture, name
            continue
        assert name not in recipes, name
        assert SEED_TEST in fixture["automated"], name
        if fixture["surface"] == "leads":
            assert fixture["urls"] == {"generic": LEADS_URL, "job_search": LEADS_URL}
        else:
            assert fixture["urls"] == {"shell": SHELL_URL}

    # A runtime recipe starts from a seeded URL and names real helper commands.
    commands = _helper_commands()
    for name, mode in modes.items():
        if mode != "harness-runtime-recipe":
            continue
        recipe = recipes[name]
        assert modes[recipe["base_fixture"]] == "seeded-durable-url", name
        assert recipe["harness"], name
        for command in recipe["harness"]:
            assert command.split()[0] == "tests/harness/prompt14.sh", command
            assert command.split()[1] in commands, command

    # Only the two approved sibling-denial rows use the restricted-token exception.
    restricted = {
        name for name, mode in modes.items() if mode == "restricted-token-and-rendered"
    }
    assert restricted == RESTRICTED_TOKEN_FIXTURES
    for name, fixture in fixtures.items():
        exempt = fixture.get("prompt15_manual") == "not-applicable-approved-contract"
        assert exempt is (name in RESTRICTED_TOKEN_FIXTURES), name
        if name not in RESTRICTED_TOKEN_FIXTURES:
            continue
        assert "urls" not in fixture and name not in recipes, name
        assert fixture["restricted_token_evidence"] == SIBLING_AUTHORIZATION
        render = fixture["render_evidence"]
        assert render.startswith(
            f"vitest:modules/leads/web/src/handoff.test.tsx > {name} "
        ), render
        _assert_reference_resolves(fixture["restricted_token_evidence"])
        _assert_reference_resolves(render)

    # Everything else is an interaction or harness recipe, never a seeded URL.
    unseeded = set(fixtures) - seeded - RESTRICTED_TOKEN_FIXTURES
    assert set(recipes) == unseeded
    for name in unseeded:
        recipe = recipes[name]
        assert recipe["kind"] == modes[name], name
        assert recipe["steps"], name

    denied = recipes["approval-denied-or-missing"]
    assert modes["approval-denied-or-missing"] == "normal-interaction"
    assert denied["flow"] == "same-human-workspace-a-b-a"
    assert len(denied["steps"]) == 4
    assert denied["workspace_b"] == "seeded"


def test_acceptance_authority_uses_only_public_safe_synthetic_content() -> None:
    authority = _authority()
    assert authority["scenario"] == {
        "organization": "Northwind",
        "opportunity": "Northwind onboarding workshop",
        "requester": "Avery Chen",
        "destination": "Current",
        "operation": "current.item.create_external",
        "item_title": "Prepare Northwind onboarding workshop",
        "item_body": (
            "Confirm agenda, facilitators, and access requirements from the "
            "qualified opportunity."
        ),
        "priority": "normal",
        "area": "Client delivery",
        "due_date": None,
        "project": "Northwind launch",
    }


def test_acceptance_authority_automated_references_resolve() -> None:
    authority = _authority()
    for row in authority["criteria"]:
        for reference in row["automated"]:
            _assert_reference_resolves(reference)


def test_prompt14_helper_has_a_read_only_static_contract() -> None:
    result = subprocess.run(
        [str(HELPER), "static-check"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "prompt14 static contract: ok"
    text = OVERRIDE.read_text(encoding="utf-8")
    assert "RHEO_CORE_INTERNAL_API_URL: http://core:8100" in text
    assert "8100:8100" not in text
    assert "RHEO_PROMPT14_CORE_IMAGE" in text
    assert "RHEO_PROMPT14_WEB_IMAGE" in text
    # Core and worker load the modules the stack serves; the seed controls worker
    # ordering, so the stack starts no worker service.
    modules = (
        "RHEO__modules__installed: "
        "${RHEO_PROMPT14_MODULES:-relationships,leads,current}"
    )
    assert text.count(modules) == 2
    helper = HELPER.read_text(encoding="utf-8")
    assert "compose up -d --wait --no-build postgres core web front" in helper
    assert "compose up -d --wait --no-build worker" not in helper
    assert {"current-omitted", "current-restored", "core-stop", "core-start"} <= (
        _helper_commands()
    )


# --- the Compose ownership guard -----------------------------------------------

HEAD = "0123456789abcdef0123456789abcdef01234567"
PROJECT = "rheo-stream-p14-0123456789ab"


def _guarded_config() -> dict[str, object]:
    """A resolved config the guard accepts: the unique project only."""
    image = "rheo-stream-p14-{}:0123456789ab"
    modules = {"RHEO__modules__installed": "relationships,leads,current"}
    tests_bind = {"type": "bind", "source": "/x/tests", "read_only": True}
    data = {"type": "volume", "source": "rheodata"}
    return {
        "name": PROJECT,
        "services": {
            "core": {
                "image": image.format("core"),
                "ports": [{"published": "8000", "target": 8000}],
                "environment": dict(modules),
                "volumes": [dict(data), dict(tests_bind)],
            },
            "worker": {
                "image": image.format("core"),
                "environment": dict(modules),
                "volumes": [dict(data), dict(tests_bind)],
            },
            "front": {
                "image": image.format("web"),
                "ports": [{"published": "3000", "target": 3000}],
                "volumes": [
                    {"type": "bind", "source": "/x/harness", "read_only": True}
                ],
            },
            "web": {
                "image": image.format("web"),
                "environment": {
                    "RHEO_CORE_INTERNAL_API_URL": "http://core:8100",
                    "RHEO_CORE_INTERNAL_URL": "http://core:8000",
                    "RHEO_CORE_PUBLIC_URL": "http://core:8000",
                    "RHEO_INTERNAL_SECRET": "rheo_dev_only_internal_secret",
                },
            },
            "postgres": {
                "image": "pgvector/pgvector:pg16",
                "volumes": [{"type": "volume", "source": "pgdata"}],
            },
        },
        "volumes": {
            "pgdata": {"name": f"{PROJECT}_pgdata"},
            "rheodata": {"name": f"{PROJECT}_rheodata"},
        },
    }


def _verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: dict[str, object]
) -> None:
    from harness.prompt14_acceptance import verify_compose

    monkeypatch.setenv("RHEO_PROMPT14_PROJECT", PROJECT)
    monkeypatch.setenv("RHEO_PROMPT14_SOURCE_HEAD", HEAD)
    monkeypatch.delenv("RHEO_PROMPT14_MODULES", raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    verify_compose(path)


def _services(config: dict[str, object]) -> dict[str, dict[str, object]]:
    services = config["services"]
    assert isinstance(services, dict)
    return services


def test_compose_guard_accepts_only_the_unique_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _verify(tmp_path, monkeypatch, _guarded_config())


@pytest.mark.parametrize(
    "breach",
    [
        "foreign-volume",
        "published-8100",
        "writable-bind",
        "wrong-tag",
        "worker-port",
        "web-bypasses-front",
    ],
)
def test_compose_guard_refuses_every_breach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, breach: str
) -> None:
    config = _guarded_config()
    services = _services(config)
    if breach == "foreign-volume":
        config["volumes"] = {
            "pgdata": {"name": "rheo-stream_pgdata"},
            "rheodata": {"name": f"{PROJECT}_rheodata"},
        }
    elif breach == "published-8100":
        services["core"]["ports"] = [
            {"published": "8000", "target": 8000},
            {"published": "8100", "target": 8100},
        ]
    elif breach == "writable-bind":
        services["core"]["volumes"] = [
            {"type": "bind", "source": "/x/tests", "read_only": False}
        ]
    elif breach == "wrong-tag":
        services["core"]["image"] = "rheo-stream-core"
    elif breach == "web-bypasses-front":
        services["web"]["ports"] = [{"published": "3001", "target": 3000}]
    else:
        services["worker"]["ports"] = [{"published": "9000", "target": 9000}]
    with pytest.raises(SystemExit):
        _verify(tmp_path, monkeypatch, config)


# --- a build names HEAD only when HEAD is what it builds --------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        + list(args),
        check=True,
        capture_output=True,
    )


def test_build_refuses_a_dirty_tree(tmp_path: Path) -> None:
    from harness.prompt14_acceptance import require_clean

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "seed")
    require_clean(repo)

    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        require_clean(repo)
    _git(repo, "checkout", "-q", "--", "tracked.txt")
    (repo / "untracked.txt").write_text("new\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        require_clean(repo)

    helper = HELPER.read_text(encoding="utf-8")
    build = helper.split("  build)", 1)[1].split(";;", 1)[0]
    assert "require-clean" in build


# --- consuming rows each have their own pending approval ---------------------------


def test_every_consuming_row_has_its_own_seeded_pending_approval() -> None:
    from harness.prompt14_acceptance import SPARE_PENDING

    authority = _authority()
    spares = authority["pending_spares"]
    assert set(spares["rows"]) == set(SPARE_PENDING)
    assert spares["reseed"], spares
    for row in spares["rows"]:
        assert row in authority["fixtures"] or row.startswith("decision-"), row


# --- manual states that are not fixture labels are classified -----------------------


def test_criterion_manual_states_outside_the_labels_are_classified() -> None:
    criteria = {row["criterion"]: row for row in _authority()["criteria"]}
    expected = {9: {"unqualified"}, 12: {"route absence"}, 14: {"moved"}}
    for criterion, states in expected.items():
        coverage = criteria[criterion]["manual_coverage"]
        assert {entry["state"] for entry in coverage} == states, criterion
        for entry in coverage:
            assert entry["mode"] in {"automated-only", "normal-interaction"}, entry
            assert entry["automated"], entry
            for reference in entry["automated"]:
                _assert_reference_resolves(reference)
            if entry["mode"] == "normal-interaction":
                assert entry["steps"], entry


# --- the audit browser session ----------------------------------------------------


def test_audit_browser_session_keeps_the_secret_between_processes() -> None:
    commands = _helper_commands()
    assert {"browser-start", "browser-stop"} <= commands
    helper = HELPER.read_text(encoding="utf-8")
    start = helper.split("browser_start() {", 1)[1].split("\n}\n", 1)[0]
    # The secret goes from the minting process straight into the cookie setter.
    assert "mint-session \\\n    | node" in start
    assert "trap cleanup EXIT INT TERM" in start
    assert "--user-data-dir=" in start
    seed = SEED.read_text(encoding="utf-8")
    assert "isatty()" in seed.split("def mint_session", 1)[1].split("\ndef ", 1)[0]
    driver = (ROOT / "tests" / "harness" / "audit_browser.mjs").read_text(
        encoding="utf-8"
    )
    imports = [line for line in driver.splitlines() if line.startswith("import ")]
    assert all('from "node:' in line for line in imports), imports
    assert "httpOnly: true" in driver
