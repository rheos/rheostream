"""Shared job feed collector scaffolding: fakes for the ``dependencies()`` seam and an
installed collector over the real intake workspace.

The fakes reach handlers only through ``rheo_leads.jobfeed.use_dependencies``; no
module global is patched. The installed workspace reuses the Leads intake fixture
(Relationships and Leads enabled) and its real dispatcher and worker.
"""

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from postgres.test_leads_intake import Intake
from rheo_contracts import Entry, RecordRef, Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.boundary.factories import context_for_connection
from rheo_core.connectors.oauth import Discovered, RegisteredClient
from rheo_core.operations import dispatch
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.refs import uuid7
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads.jobfeed import Dependencies, use_dependencies
from rheo_leads.jobfeed.install import install_connection
from rheo_leads.references import ref

# A global IPv6 literal, as the egress tests use: the provenance gate flags any
# public IPv4 literal, and the outbound check refuses a non-public address.
PUBLIC_ADDRESS = "2600::1"
BASE = "https://www.example.com"
ENDPOINTS = Discovered(
    resource_url=f"{BASE}/mcp",
    issuer=BASE,
    authorization_endpoint=f"{BASE}/oauth/authorize",
    token_endpoint=f"{BASE}/oauth/token",
    registration_endpoint=f"{BASE}/oauth/register",
    revocation_endpoint=f"{BASE}/oauth/revoke",
)


def registered_client(**changes: Any) -> RegisteredClient:
    values: dict[str, Any] = {
        "client_id": "fixture-client",
        "auth_method": "none",
        "redirect_uri": "https://auth.example.com/connectors/leads/callback",
        "secret_expires_at": None,
        "client_secret_ref": None,
        "registration_token_ref": None,
    }
    values.update(changes)
    return RegisteredClient(**values)


@dataclass
class FakeClock:
    """A monotonic clock, a wall clock and a sleep that only advances both."""

    monotonic_at: float = 1000.0
    wall_at: datetime = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    slept: list[float] = field(default_factory=list)

    def advance(self, seconds: float) -> None:
        self.monotonic_at += seconds
        self.wall_at += timedelta(seconds=seconds)

    def monotonic(self) -> float:
        return self.monotonic_at

    def now(self) -> datetime:
        return self.wall_at

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds)


@dataclass
class FakeResolver:
    """Resolves every host to fixed addresses and records what it was asked."""

    addresses: dict[str, Sequence[str]] = field(default_factory=dict)
    default: Sequence[str] = (PUBLIC_ADDRESS,)
    asked: list[str] = field(default_factory=list)

    def __call__(self, host: str) -> Sequence[str]:
        self.asked.append(host)
        return self.addresses.get(host, self.default)


@contextmanager
def fakes(
    *,
    clock: FakeClock | None = None,
    resolver: FakeResolver | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Iterator[Dependencies]:
    clock = clock or FakeClock()
    with use_dependencies(
        Dependencies(
            transport=transport,
            resolver=resolver or FakeResolver(),
            clock=clock.monotonic,
            sleep=clock.sleep,
            now=clock.now,
        )
    ) as deps:
        yield deps


@dataclass
class Collector:
    intake: Intake
    pipeline_id: UUID
    connection_id: UUID
    owner: WorkspaceContext
    poll: WorkspaceContext


def owner_web(i: Intake) -> WorkspaceContext:
    ctx = context_for_harness(i.workspace, i.ctx.actor.id, Role.OWNER, Entry.WEB)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def call(i: Intake, ctx: WorkspaceContext, name: str, /, **payload: Any) -> Any:
    outcome = dispatch(
        ctx, name, payload, registry=i.surfaces.operations, consumers=i.consumers
    )
    assert outcome.ok, outcome
    return outcome.result


def jobsearch_pipeline(i: Intake, owner: WorkspaceContext) -> UUID:
    """Turn the capability on and create a Job search pipeline."""
    call(i, owner, "core.settings.set", key="leads.capabilities.jobsearch", value=True)
    created = call(
        i, owner, "leads.pipeline.create", name="Job leads", preset="job_search"
    )
    return RecordRef.parse(created.ref).id


def poll_context(i: Intake, connection_id: UUID) -> WorkspaceContext:
    ctx = context_for_connection(i.workspace, connection_id, "leads", "poll")
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def install_collector(
    i: Intake, *, client: RegisteredClient | None = None
) -> Collector:
    owner = owner_web(i)
    pipeline_id = jobsearch_pipeline(i, owner)
    connection_id = uuid7()
    with open_unit_of_work(owner) as uow:
        install_connection(
            uow,
            owner,
            connection_id=connection_id,
            pipeline_id=pipeline_id,
            endpoints=ENDPOINTS,
            client=client or registered_client(),
        )
        uow.commit()
    return Collector(
        i, pipeline_id, connection_id, owner, poll_context(i, connection_id)
    )


@pytest.fixture
def collector(intake: Intake) -> Iterator[Collector]:
    yield install_collector(intake)


def deliver(
    poll_ctx: WorkspaceContext,
    i: Intake,
    body: dict[str, Any] | str,
    event_id: str,
    occurred_at: datetime | None = None,
) -> OperationOutcome:
    """Dispatch acceptance for the poll context's own connection."""
    payload: dict[str, Any] = {
        "connection_ref": ref("intake_connection", poll_ctx.actor.id),
        "source_event_id": event_id,
        "body": body if isinstance(body, str) else json.dumps(body),
    }
    if occurred_at is not None:
        payload["source_occurred_at"] = occurred_at.isoformat()
    return dispatch(
        poll_ctx,
        "leads.intake.accept_delivery",
        payload,
        registry=i.surfaces.operations,
        consumers=i.consumers,
    )


def drain(i: Intake) -> None:
    """Run the intake consumer so accepted deliveries become observations."""
    i.visit()
