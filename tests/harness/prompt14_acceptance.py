"""Disposable Prompt 14 composition guard and synthetic acceptance seed.

The file is mounted read-only into the Prompt 14 core container.  Its seed path
uses the incumbent provisioning/session services and the same installed-module
dispatch helpers as the PostgreSQL acceptance suites.  It writes only public-safe
record identifiers and configured URLs to the project-owned rheodata volume.

The same :func:`seeded` body runs in two places:

- ``tests/postgres/test_prompt14_seed.py`` runs it against a disposable workspace
  database on the local test cluster and asserts every seeded state;
- ``seed`` below runs it inside the unique Prompt 14 Compose project.

Worker ordering is controlled by the harness, never by a fault proxy.  The seed
drives every worker visit itself, in-process, and the Compose ``up`` command starts
no ``worker`` service, so an approved action stays durably queued and an action a
busy destination answered with a typed retryable error stays durably
``executing`` until something runs the worker.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

STATE = Path("/var/lib/rheo-stream/prompt14-fixtures.json")
PROVIDER = "prompt14-local"
SUBJECT = "avery-chen-synthetic"
HOST = "localhost"
MODULES = "relationships,leads,current"
MODULES_WITHOUT_CURRENT = "relationships,leads"
VARIANTS = ("generic", "job_search")

#: Labels whose Leads detail URL the seed writes, once per variant.
LEADS_LABELS = (
    "qualified-current",
    "qualified-stale",
    "handoff-awaiting-approval",
    "handoff-approved-queued",
    "handoff-executing",
    "handoff-succeeded-live-item-no-project",
    "handoff-succeeded-live-item-live-project",
    "handoff-deleted-item-live-project",
    "handoff-cleared-project",
    "handoff-deleted-project",
    "handoff-unresolved",
    "handoff-retryable-no-effect",
)
#: Labels whose shell approval URL the seed writes (generic variant only).
APPROVAL_LABELS = (
    "approval-pending",
    "approval-approved-queued",
    "approval-executing",
    "approval-succeeded",
    "approval-failed-no-effect",
    "approval-unresolved",
    "approval-refused",
)


def _fail(message: str) -> None:
    raise SystemExit(message)


def _ports(service: dict[str, Any]) -> set[tuple[int, int]]:
    found: set[tuple[int, int]] = set()
    for value in service.get("ports", []):
        if not isinstance(value, dict):
            _fail(f"unexpected port shape: {value!r}")
        found.add((int(value["published"]), int(value["target"])))
    return found


def verify_compose(path: Path) -> None:
    config = json.loads(path.read_text(encoding="utf-8"))
    project = os.environ.get("RHEO_PROMPT14_PROJECT", "")
    head = os.environ.get("RHEO_PROMPT14_SOURCE_HEAD", "")
    short = head[:12]
    if (
        not project.startswith("rheo-stream-p14-")
        or project != f"rheo-stream-p14-{short}"
    ):
        _fail("Compose project is not bound to the exact Prompt 14 source head")
    if config.get("name") != project:
        _fail("resolved Compose project name differs from the guarded project")

    services = config.get("services", {})
    if set(services) != {"core", "postgres", "web", "worker"}:
        _fail(f"unexpected service set: {sorted(services)}")
    expected_images = {
        "core": f"rheo-stream-p14-core:{short}",
        "worker": f"rheo-stream-p14-core:{short}",
        "web": f"rheo-stream-p14-web:{short}",
    }
    for service, image in expected_images.items():
        if services[service].get("image") != image:
            _fail(f"{service} image is not the source-head-specific Prompt 14 tag")
    if services["postgres"].get("image") != "pgvector/pgvector:pg16":
        _fail("Prompt 14 must retain the incumbent pinned Postgres image")

    published = {name: _ports(service) for name, service in services.items()}
    if published != {
        "core": {(8000, 8000)},
        "postgres": set(),
        "web": {(3000, 3000)},
        "worker": set(),
    }:
        _fail(f"only host 8000/3000 may be published: {published}")
    if any(target == 8100 for ports in published.values() for _, target in ports):
        _fail("Core 8100 must remain private")

    volumes = config.get("volumes", {})
    expected_volumes = {
        f"{project}_pgdata",
        f"{project}_rheodata",
    }
    resolved_volumes = {str(value.get("name")) for value in volumes.values()}
    if resolved_volumes != expected_volumes:
        _fail(f"writable volumes are not project-owned: {resolved_volumes}")
    forbidden = {"rheo-stream_pgdata", "rheo-stream_rheodata"}
    if resolved_volumes & forbidden:
        _fail("resolved configuration reaches an incumbent rheo-stream volume")

    for service_name, service in services.items():
        for mount in service.get("volumes", []):
            if not isinstance(mount, dict):
                _fail(f"unexpected mount shape on {service_name}: {mount!r}")
            if mount.get("type") == "volume" and mount.get("source") not in volumes:
                _fail(f"{service_name} has an unowned writable volume")
            if mount.get("type") == "bind" and not mount.get("read_only", False):
                _fail(f"{service_name} has a writable bind mount")

    web_env = services["web"].get("environment", {})
    expected_env = {
        "RHEO_CORE_INTERNAL_API_URL": "http://core:8100",
        "RHEO_CORE_INTERNAL_URL": "http://core:8000",
        "RHEO_CORE_PUBLIC_URL": "http://core:8000",
        "RHEO_INTERNAL_SECRET": "rheo_dev_only_internal_secret",
    }
    for key, value in expected_env.items():
        if web_env.get(key) != value:
            _fail(f"web {key} does not match the incumbent local-development seam")

    # The normal stack loads all three modules; the Current-omitted recipe is the
    # only other allowed value, and it is requested explicitly by prompt14.sh.
    modules = os.environ.get("RHEO_PROMPT14_MODULES", MODULES)
    if modules not in {MODULES, MODULES_WITHOUT_CURRENT}:
        _fail(f"unsupported Prompt 14 module set: {modules}")
    for service in ("core", "worker"):
        env = services[service].get("environment", {})
        if env.get("RHEO__modules__installed") != modules:
            _fail(f"{service} does not load the expected module set {modules}")
    print(
        "Prompt 14 ownership proof: "
        f"project={project} images={sorted(expected_images.values())} "
        f"volumes={sorted(resolved_volumes)} ports=8000,3000 internal_8100=private "
        f"modules={modules}"
    )


def write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def reset_state(path: Path) -> bool:
    """Forget the seeded identifiers. Database rows go with the disposable project
    (``prompt14.sh reset`` removes its volumes) or with the test's own database."""
    if not path.exists():
        return False
    path.unlink()
    return True


def leads_url(reference: str) -> str:
    return "/leads/detail?" + urlencode({"ref": reference})


def approval_url(identifier: Any) -> str:
    return "/approvals/detail?" + urlencode({"approval_id": str(identifier)})


@dataclass
class Seeded:
    """What the seed made: public-safe URLs plus the records tests assert on."""

    workspaces: dict[str, str]
    fixtures: dict[str, dict[str, str]] = field(default_factory=dict)
    opportunities: dict[str, dict[str, Any]] = field(default_factory=dict)
    handoffs: dict[str, dict[str, Any]] = field(default_factory=dict)
    approvals: dict[str, dict[str, Any]] = field(default_factory=dict)

    def leads(self, label: str, variant: str, opportunity: Any) -> None:
        self.opportunities.setdefault(label, {})[variant] = opportunity
        self.fixtures.setdefault(label, {})[variant] = leads_url(opportunity.ref)

    def approval(self, label: str, row: dict[str, Any]) -> None:
        self.approvals[label] = row
        self.fixtures[label] = {"shell": approval_url(row["approval_id"])}

    def state(self, account_id: Any) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "synthetic": True,
            "account_id": str(account_id),
            "workspaces": self.workspaces,
            "routing_mode": "path",
            "base_url": "http://localhost:3000",
            "fixtures": self.fixtures,
        }


class _Seeder:
    def __init__(self, routing: Any, monkeypatch: Any, scenario: dict[str, Any]):
        from postgres.test_leads_current_links import (
            delete,
            item_update,
            move_to_project,
            project,
            reconcile,
        )
        from postgres.test_leads_pipeline import ok

        self.reconcile = reconcile
        self.routing = routing
        self.monkeypatch = monkeypatch
        self.scenario = scenario
        self.ok = ok
        self.delete = delete
        self.item_update = item_update
        self.move_to_project = move_to_project
        self.project = project
        self.payload = {
            "title": scenario["item_title"],
            "body": scenario["item_body"],
            "priority": scenario["priority"],
            "area": "client_delivery",
            "due_on": scenario["due_date"],
        }

    def qualified(self, title: str) -> Any:
        opportunity = self.routing.opportunity(title)
        self.routing.qualify(opportunity)
        return opportunity

    def handoff(self, opportunity: Any, key: str) -> Any:
        return self.routing.handoff(opportunity, key, payload=self.payload)

    def delivered(self, opportunity: Any, key: str) -> Any:
        handoff = self.handoff(opportunity, key)
        self.routing.approve(self.routing.row(handoff)["approval_id"])
        self.routing.run_worker()
        # The repeated keyed request is the public lost-response recovery path; it
        # copies core's result ref into Leads before Current lifecycle changes.
        return self.handoff(opportunity, key)

    def busy(self, ctx: Any, uow: Any, data: Any) -> Any:
        from rheo_core.operations.continuation import (
            ExternalDestinationError,
            ExternalFailureClass,
        )

        raise ExternalDestinationError(
            ExternalFailureClass.RETRYABLE_NO_EFFECT,
            "synthetic_busy",
            "Current is busy",
        )


def _title(base: str, variant: str, state: str) -> str:
    suffix = "" if variant == "generic" else " (job search)"
    return f"{base}{suffix} — {state}" if state else f"{base}{suffix}"


def _job_search_pipeline(seeder: _Seeder) -> None:
    """A Job search pipeline fed by the same synthetic connection, set the way an
    owner sets it: the capability setting, the preset, and the routing rule."""
    routing = seeder.routing
    seeder.ok(
        routing.i, "core.settings.set", key="leads.capabilities.jobsearch", value=True
    )
    jobs = seeder.ok(
        routing.i, "pipeline.create", name="Northwind job search", preset="job_search"
    )
    seeder.ok(
        routing.i,
        "connection.set_routing",
        connection_id=str(routing.i.connection_id),
        rules=[{"action": "create_opportunity", "pipeline_id": str(jobs.data["id"])}],
    )


def _seed_settled(seeder: _Seeder, seeded: Seeded, variant: str) -> None:
    """Every state the seed settles with its own in-process worker visits."""
    routing = seeder.routing
    base = seeder.scenario["opportunity"]
    key = f"prompt14-{variant}"

    current = seeder.qualified(_title(base, variant, ""))
    seeded.leads("qualified-current", variant, current)

    stale = seeder.qualified(_title(base, variant, "stale"))
    latest = routing.get(stale.ref)
    seeder.ok(
        routing.i,
        "opportunity.update",
        ref=latest.ref,
        revision=latest.revision,
        value_amount="1200.00",
        value_currency="USD",
        value_basis="project_fee",
    )
    seeded.leads("qualified-stale", variant, stale)

    awaiting = seeder.qualified(_title(base, variant, "awaiting approval"))
    held = seeder.handoff(awaiting, f"{key}-awaiting")
    seeded.leads("handoff-awaiting-approval", variant, awaiting)
    seeded.handoffs.setdefault("handoff-awaiting-approval", {})[variant] = held
    if variant == "generic":
        seeded.approval("approval-pending", routing.row(held))

    live = seeder.qualified(_title(base, variant, "live item"))
    delivered = seeder.delivered(live, f"{key}-live")
    seeded.leads("handoff-succeeded-live-item-no-project", variant, live)
    seeded.handoffs.setdefault("handoff-succeeded-live-item-no-project", {})[
        variant
    ] = delivered
    if variant == "generic":
        seeded.approval("approval-succeeded", routing.row(delivered))

    linked_opp = seeder.qualified(_title(base, variant, "live project"))
    linked = seeder.delivered(linked_opp, f"{key}-linked")
    seeder.move_to_project(routing, linked, seeder.scenario["project"])
    seeded.leads("handoff-succeeded-live-item-live-project", variant, linked_opp)
    seeded.handoffs.setdefault("handoff-succeeded-live-item-live-project", {})[
        variant
    ] = linked

    deleted_opp = seeder.qualified(_title(base, variant, "deleted item"))
    deleted = seeder.delivered(deleted_opp, f"{key}-deleted-item")
    seeder.move_to_project(routing, deleted, seeder.scenario["project"])
    seeder.delete(routing, routing.row(deleted)["result_ref"])
    seeded.leads("handoff-deleted-item-live-project", variant, deleted_opp)
    seeded.handoffs.setdefault("handoff-deleted-item-live-project", {})[variant] = (
        deleted
    )

    cleared_opp = seeder.qualified(_title(base, variant, "cleared project"))
    cleared = seeder.delivered(cleared_opp, f"{key}-cleared")
    seeder.move_to_project(routing, cleared, seeder.scenario["project"])
    seeder.item_update(
        routing, routing.row(cleared)["result_ref"], clear=["project_ref"]
    )
    seeded.leads("handoff-cleared-project", variant, cleared_opp)
    seeded.handoffs.setdefault("handoff-cleared-project", {})[variant] = cleared

    gone_opp = seeder.qualified(_title(base, variant, "deleted project"))
    gone = seeder.delivered(gone_opp, f"{key}-project-deleted")
    project_ref = seeder.project(routing, seeder.scenario["project"])
    seeder.item_update(
        routing, routing.row(gone)["result_ref"], project_ref=project_ref
    )
    seeder.delete(routing, project_ref)
    seeded.leads("handoff-deleted-project", variant, gone_opp)
    seeded.handoffs.setdefault("handoff-deleted-project", {})[variant] = gone

    # The incumbent PostgreSQL helpers below swap Current's registered destination
    # in this seed process only, then restore it. Nothing is injected at audit time.
    unresolved_opp = seeder.qualified(_title(base, variant, "unresolved"))
    unresolved = seeder.handoff(unresolved_opp, f"{key}-unresolved")
    routing.unresolve(seeder.monkeypatch, unresolved)
    seeded.leads("handoff-unresolved", variant, unresolved_opp)
    seeded.handoffs.setdefault("handoff-unresolved", {})[variant] = unresolved
    if variant == "generic":
        seeded.approval("approval-unresolved", routing.row(unresolved))

    failed_opp = seeder.qualified(_title(base, variant, "safe retry"))
    failed = seeder.handoff(failed_opp, f"{key}-retryable")
    routing.exhaust(seeder.monkeypatch, failed)
    seeded.leads("handoff-retryable-no-effect", variant, failed_opp)
    seeded.handoffs.setdefault("handoff-retryable-no-effect", {})[variant] = failed
    if variant == "generic":
        seeded.approval("approval-failed-no-effect", routing.row(failed))

        refused_opp = seeder.qualified(_title(base, variant, "refused"))
        refused = seeder.handoff(refused_opp, f"{key}-refused")
        refused_row = routing.row(refused)
        answer = routing.call(
            "core.approval.refuse", approval_id=str(refused_row["approval_id"])
        )
        if not answer.ok:
            _fail(f"could not refuse synthetic approval: {answer}")
        seeded.approval("approval-refused", refused_row)


@contextmanager
def seeded(
    cluster: Any,
    account_id: UUID,
    workspace_a: UUID,
    workspace_b: UUID,
    monkeypatch: Any,
    authority_path: Path,
) -> Iterator[tuple[Any, Seeded]]:
    """Seed workspace A; workspace B only needs the same human's membership.

    Order matters and is the whole of the worker control: every state that needs
    a worker visit settles first, then one visit leaves the executing rows busy,
    and the approved-queued rows are approved last, after the final visit.
    """
    from postgres.test_leads_current_handoff import routing_harness

    authority = json.loads(authority_path.read_text(encoding="utf-8"))
    scenario = authority["scenario"]
    result = Seeded(workspaces={"a": str(workspace_a), "b": str(workspace_b)})
    with routing_harness(
        cluster, workspace_a, account_id, monkeypatch, with_current=True
    ) as routing:
        seeder = _Seeder(routing, monkeypatch, scenario)
        area = routing.call(
            "current.area.create", key="client_delivery", label=scenario["area"]
        )
        if not area.ok:
            _fail(f"could not create the synthetic Current area: {area}")

        executing: dict[str, Any] = {}
        queued: dict[str, Any] = {}
        for variant in VARIANTS:
            if variant == "job_search":
                _job_search_pipeline(seeder)
            _seed_settled(seeder, result, variant)
            # Created now, so they share this variant's pipeline; decided below.
            executing[variant] = seeder.qualified(
                _title(scenario["opportunity"], variant, "executing")
            )
            queued[variant] = seeder.qualified(
                _title(scenario["opportunity"], variant, "approved and queued")
            )

        # Leads' own repair (the current_links reconcile job's function, run by the
        # harness) copies the settled unresolved and failed outcomes into Leads.
        seeder.reconcile(routing, page_size=100)

        # One real visit with Current answering a typed retryable error: core marks
        # each action executing and requeues its job with a backoff. No worker runs
        # after this in the seed or in the Prompt 14 composition.
        busy_rows = {}
        for variant, opportunity in executing.items():
            handoff = seeder.handoff(opportunity, f"prompt14-{variant}-executing")
            row = routing.row(handoff)
            routing.approve(row["approval_id"])
            busy_rows[variant] = (opportunity, handoff, row)
        routing.swap_destination(monkeypatch, execute=seeder.busy)
        routing.run_worker()
        routing.swap_destination(monkeypatch)
        for variant, (opportunity, handoff, row) in busy_rows.items():
            result.leads("handoff-executing", variant, opportunity)
            result.handoffs.setdefault("handoff-executing", {})[variant] = handoff
            if variant == "generic":
                result.approval("approval-executing", row)

        # Approved after the last visit: queued until a worker runs.
        for variant, opportunity in queued.items():
            handoff = seeder.handoff(opportunity, f"prompt14-{variant}-queued")
            row = routing.row(handoff)
            routing.approve(row["approval_id"])
            result.leads("handoff-approved-queued", variant, opportunity)
            result.handoffs.setdefault("handoff-approved-queued", {})[variant] = handoff
            if variant == "generic":
                result.approval("approval-approved-queued", row)
        yield routing, result


def session_context_check(account_id: UUID, workspace_a: UUID) -> tuple[UUID, Any]:
    """The ordinary host-bound session resolves the owner in workspace A."""
    from rheo_contracts import Role, WorkspaceContext
    from rheo_core.boundary.factories import context_from_session
    from rheo_core.sessions import create_session, mint_host_secret, switch_workspace

    session = create_session(account_id)
    secret = mint_host_secret(session.id, HOST)
    if switch_workspace(session.id, workspace_a) is not None:
        _fail("could not make workspace A the session's active workspace")
    context = context_from_session(secret, HOST)
    if not isinstance(context, WorkspaceContext):
        _fail(f"seeded session did not resolve: {context}")
    if (
        context.actor.id != account_id
        or context.workspace_id != workspace_a
        or context.role is not Role.OWNER
        or not {"current", "leads", "relationships"} <= context.enabled_modules
    ):
        _fail(
            "ordinary session context lacks the expected "
            "account/workspace authorization"
        )
    return session.id, secret


def _auth_continue_check(secret: Any) -> None:
    """Compose only: the same-host /auth/continue cookie path writes no grant."""
    import httpx
    from rheo_core.storage import control_tables
    from rheo_core.storage.postgres import get_backend
    from sqlalchemy import func, select

    backend = get_backend()
    with backend.control_engine.connect() as connection:
        before = connection.execute(
            select(func.count()).select_from(control_tables.session_grant)
        ).scalar_one()
    target = "http://localhost:3000/leads"
    with httpx.Client(follow_redirects=False) as client:
        response = client.get(
            "http://127.0.0.1:8000/auth/continue",
            params={"return": target},
            headers={"Host": HOST},
            cookies={"rheo_session": secret.hex()},
        )
    if response.status_code != 302 or response.headers.get("location") != "/leads":
        _fail(
            "same-host /auth/continue did not use the incumbent path-mode cookie flow"
        )
    with backend.control_engine.connect() as connection:
        after = connection.execute(
            select(func.count()).select_from(control_tables.session_grant)
        ).scalar_one()
    if after != before:
        _fail("path-mode fixture flow unexpectedly wrote a session grant")


def seed() -> None:
    if STATE.exists():
        _fail("Prompt 14 fixtures already exist; run the project-scoped reset first")
    # This test-only process loads modules the way the PostgreSQL suites do (the
    # routing harness), not from the deployment module list the core service uses.
    os.environ.pop("RHEO__modules__installed", None)
    sys.path.insert(0, "/app/tests")

    import pytest
    from rheo_app_cli.context import bootstrap
    from rheo_core.refs import uuid7
    from rheo_core.storage.control_plane import (
        get_identity,
        insert_account,
        insert_identity,
    )
    from rheo_core.storage.provisioning import provision

    boot = bootstrap()
    with boot.backend.control_engine.begin() as connection:
        if get_identity(connection, provider_id=PROVIDER, provider_subject=SUBJECT):
            _fail(
                "Prompt 14 synthetic identity already exists; "
                "reset the disposable project"
            )
        account = insert_account(connection, display_name="Avery Chen")
        insert_identity(
            connection,
            account_id=account.id,
            provider_id=PROVIDER,
            provider_subject=SUBJECT,
        )
    workspace_a = uuid7()
    workspace_b = uuid7()
    provision(workspace_a, owner_account_id=account.id, slug="prompt14-northwind")
    provision(workspace_b, owner_account_id=account.id, slug="prompt14-review-b")

    monkeypatch = pytest.MonkeyPatch()
    try:
        with seeded(
            SimpleNamespace(backend=boot.backend),
            account.id,
            workspace_a,
            workspace_b,
            monkeypatch,
            Path("/app/tests/fixtures/leads-current-handoff.json"),
        ) as (_routing, result):
            pass
    finally:
        monkeypatch.undo()

    _session, secret = session_context_check(account.id, workspace_a)
    _auth_continue_check(secret)
    write_state(STATE, result.state(account.id))
    print(
        f"seeded {len(result.fixtures)} public-safe fixture labels "
        f"for workspaces {workspace_a} and {workspace_b}; "
        "path-mode session context and grant-free /auth/continue verified"
    )


def urls() -> None:
    if not STATE.exists():
        _fail("Prompt 14 fixtures are not seeded")
    payload = json.loads(STATE.read_text(encoding="utf-8"))
    base = payload["base_url"]
    for name, variants in sorted(payload["fixtures"].items()):
        for variant, url in sorted(variants.items()):
            print(f"{name}\t{variant}\t{base}{url}")
    for name, workspace in sorted(payload["workspaces"].items()):
        print(f"workspace-{name}\t{workspace}")


def main() -> None:
    if len(sys.argv) < 2:
        _fail("usage: prompt14_acceptance.py verify-compose CONFIG | seed | urls")
    command = sys.argv[1]
    if command == "verify-compose" and len(sys.argv) == 3:
        verify_compose(Path(sys.argv[2]))
    elif command == "seed" and len(sys.argv) == 2:
        seed()
    elif command == "urls" and len(sys.argv) == 2:
        urls()
    else:
        _fail("invalid Prompt 14 acceptance command")


if __name__ == "__main__":
    main()
