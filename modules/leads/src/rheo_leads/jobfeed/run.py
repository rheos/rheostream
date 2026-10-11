"""The scheduled job feed collection, ``leads.jobfeed_collect``.

The ticker enqueues the job for a workspace whose collector is connected and whose
job search capability is on. The handler runs inside the worker's one transaction
and keeps it inert until the run ends: every read and write before then is a short
unit of its own, opened with the scheduled-job context, that commits at once, and
no network call happens while one is open. Only a write that ends the run goes on
the worker's transaction, and only through :func:`_end`, which first checks under
the lifecycle lock that the connection is still the one this run claimed (an owner
may revoke or reconnect while the run waits on the network); otherwise the run
writes nothing at all.

The steps: read the connection and decide whether to run; claim the run; refresh
the access token once, as the stored client, with a forced lease extension before
the call. A rotated refresh token is switched by compare-and-set before the old file
is discarded; a lost compare-and-set (a revoke or reconnect won) revokes the freshly
minted token and discards its file. A terminal answer makes the connection need a
reconnect; a rejected client is also cleared, and the worker never registers one.
Expected failures never raise. Anything unexpected is logged by its type name and
raised as the fixed ``internal_error``, with no chained exception.
"""

import logging
from dataclasses import dataclass
from typing import Final, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from rheo_contracts import WorkspaceContext
from rheo_core.boundary.context import Refusal
from rheo_core.boundary.factories import (
    context_for_connection,
    context_for_scheduled_job,
)
from rheo_core.connectors import oauth
from rheo_core.connectors.oauth import (
    AccessToken,
    ClientAuth,
    ClientAuthMethod,
    OAuthFailure,
)
from rheo_core.settings import resolve
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.work.cancellation import CancellationToken, JobCancelled, JobLeaseLost

from rheo_leads.intake.accept import health_write
from rheo_leads.jobfeed import dependencies
from rheo_leads.jobfeed.state import SCHEDULE_NAME as SCHEDULE_NAME
from rheo_leads.jobfeed.state import (
    JobfeedConnection,
    claim_run,
    claimed_connection,
    clear_rejected_client,
    read_connection,
    switch_token,
    write_success,
    write_terminal,
    write_transient,
)
from rheo_leads.pipeline_common import jobsearch_enabled

_log = logging.getLogger(__name__)

COLLECT_JOB_KIND: Final = "leads.jobfeed_collect"
COLLECT_MAX_ATTEMPTS: Final = 8
"""The retry budget, on the precedent of the other scheduled sweeps."""
RUN_DEADLINE_SECONDS: Final = 420.0
"""The whole run's budget on the monotonic clock, started at the claim."""
RESOURCE_URL_KEY: Final = "leads.jobfeed.resource_url"
JOB_HOSTS_KEY: Final = "leads.jobfeed.job_hosts"
INTERNAL_ERROR: Final = "internal_error"
INTERNAL_ERROR_LOG: Final = "jobfeed_internal_error %s"

ENDPOINT_MISCONFIGURED: Final = "endpoint_misconfigured"
TOKEN_REFRESH_FAILED: Final = "token_refresh_failed"
UPSTREAM_RATE_LIMITED: Final = "upstream_rate_limited"


class CollectPayload(BaseModel):
    """What the ticker enqueues: the workspace it visited, and nothing else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_id: UUID


class JobfeedFailure(Exception):
    """An unexpected failure of the run, carrying a fixed code only."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        return self.code


@dataclass(frozen=True)
class Terminal:
    """Authorization is lost; ``client_rejected`` also clears the stored client."""

    client_rejected: bool = False


@dataclass(frozen=True)
class Transient:
    """A failed run that waits out the backoff before the next one."""

    code: str
    retry_after: int | None = None


@dataclass(frozen=True)
class Succeeded:
    """The run completed."""


Outcome = Terminal | Transient | Succeeded


@dataclass(frozen=True)
class SessionResult:
    """What the session phase reports to the run's end."""


@dataclass
class _Run:
    """One run's facts. ``generation`` is what every run-ending write is guarded
    on: the one read at the start, the claimed one, then the rotated one."""

    job_ctx: WorkspaceContext
    conn_ctx: WorkspaceContext
    connection: JobfeedConnection
    client: ClientAuth
    generation: int
    deadline: float = 0.0


def session_phase(
    uow: HandlerUnitOfWork,
    token: CancellationToken,
    *,
    job_ctx: WorkspaceContext,
    conn_ctx: WorkspaceContext,
    connection: JobfeedConnection,
    access: AccessToken,
    deadline: float,
) -> SessionResult:
    """The MCP session after a successful refresh. Does nothing yet."""
    return SessionResult()


def collect_job(
    uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
) -> None:
    """Run the collector once (module docstring). Never raises for an expected
    failure; cancellation and a lost lease pass through untouched."""
    unexpected: str | None = None
    try:
        _collect(uow, payload, token)
    except (JobCancelled, JobLeaseLost):
        raise
    except Exception as error:
        unexpected = type(error).__name__
    if unexpected is not None:
        # Raised after the except block has ended, so nothing is chained to it.
        _log.warning(INTERNAL_ERROR_LOG, unexpected)
        raise JobfeedFailure(INTERNAL_ERROR)


def _client(connection: JobfeedConnection) -> ClientAuth | None:
    """The stored client as the token request authenticates, or ``None`` when the
    connection stores none. The column's CHECK constraint admits only the three
    methods the OAuth client supports, which rejects any other before sending."""
    if connection.client_id is None or connection.client_auth_method is None:
        return None
    return ClientAuth(
        client_id=connection.client_id,
        auth_method=cast(ClientAuthMethod, connection.client_auth_method),
        client_secret_ref=connection.client_secret_ref,
        secret_expires_at=connection.client_secret_expires_at,
    )


def _collect(
    uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
) -> None:
    if not isinstance(payload, CollectPayload):
        raise TypeError("unexpected payload")
    deps = dependencies()
    now = deps.now()
    job_ctx = context_for_scheduled_job(payload.workspace_id)
    if isinstance(job_ctx, Refusal):
        return
    # Step 1: one short read; no lock.
    with open_unit_of_work(job_ctx) as short:
        connection = read_connection(short.connection)
        enabled = jobsearch_enabled(job_ctx, short)
        short.commit()
    if connection is None:
        return
    conn_ctx = context_for_connection(
        payload.workspace_id, connection.connection_id, "leads", "poll"
    )
    if isinstance(conn_ctx, Refusal):
        return
    client = _client(connection)
    if (
        not enabled
        or connection.authorization_state != "connected"
        or connection.token_ref is None
        or client is None
        or (connection.not_before is not None and now < connection.not_before)
    ):
        return
    settings = resolve()
    resource_url = settings.get_str(RESOURCE_URL_KEY)
    if not resource_url or not settings.get_list(JOB_HOSTS_KEY):
        return
    run = _Run(
        job_ctx=job_ctx,
        conn_ctx=conn_ctx,
        connection=connection,
        client=client,
        generation=connection.credential_generation,
    )
    if resource_url != connection.resource_url:
        _end(uow, run, Transient(ENDPOINT_MISCONFIGURED), backoff=False)
        return
    # Step 2: the claim.
    claimed = claim_run(
        job_ctx,
        connection_id=connection.connection_id,
        generation=connection.credential_generation,
        now=now,
    )
    if claimed is None:
        return
    run.generation = claimed.generation
    run.deadline = deps.clock() + RUN_DEADLINE_SECONDS
    # Step 3: the one refresh.
    access = _refresh(uow, token, run, claimed.token_ref)
    if access is None:
        return
    session_phase(
        uow,
        token,
        job_ctx=job_ctx,
        conn_ctx=conn_ctx,
        connection=connection,
        access=access,
        deadline=run.deadline,
    )
    _end(uow, run, Succeeded())


def _failed(failure: OAuthFailure) -> Outcome:
    if failure.terminal:
        return Terminal(client_rejected=failure.code == "client_rejected")
    if failure.code == ENDPOINT_MISCONFIGURED:
        return Transient(ENDPOINT_MISCONFIGURED)
    if failure.code == "rate_limited":
        return Transient(UPSTREAM_RATE_LIMITED, retry_after=failure.retry_after)
    return Transient(TOKEN_REFRESH_FAILED)


def _refresh(
    uow: HandlerUnitOfWork, token: CancellationToken, run: _Run, current: str
) -> AccessToken | None:
    """The one refresh, never retried. Returns the access token when the run goes
    on; ``None`` when the run has already ended."""
    deps = dependencies()
    connection = run.connection
    failure: OAuthFailure | None = None
    access: AccessToken | None = None
    rotated: str | None = None
    token.checkpoint(force=True)
    try:
        access, rotated = oauth.refresh(
            connection.token_endpoint,
            refresh_ref=current,
            resource=connection.resource_url,
            client=run.client,
            workspace_id=run.job_ctx.workspace_id,
            connection_id=connection.connection_id,
            resolver=deps.resolver,
            clock=deps.clock,
            transport=deps.transport,
            now=deps.now(),
        )
    except OAuthFailure as error:
        failure = error
    token.checkpoint()
    if failure is not None:
        _end(uow, run, _failed(failure))
        return None
    if access is None:  # refresh either returns a token or raises
        raise TypeError("refresh returned no access token")
    if rotated is not None and not _rotate(token, run, current, rotated):
        return None
    return access


def _rotate(token: CancellationToken, run: _Run, old: str, new: str) -> bool:
    """Switch to the rotated reference; whether the run goes on.

    Won: the old file goes only after the switch committed, and the run's guard
    moves to the new generation. Lost: the minted token is referenced by nothing,
    so it is revoked once and its file discarded; the run ends writing nothing.
    """
    deps = dependencies()
    connection = run.connection
    try:
        won = switch_token(
            run.job_ctx,
            connection_id=connection.connection_id,
            old_ref=old,
            new_ref=new,
            generation=run.generation,
            now=deps.now(),
        )
    except BaseException:
        # Never committed: nothing references the new file.
        oauth.discard(new)
        raise
    if won:
        oauth.discard(old)
        run.generation += 1
        return True
    token.checkpoint(force=True)
    oauth.revoke(
        connection.revocation_endpoint,
        refresh_ref=new,
        client=run.client,
        workspace_id=run.job_ctx.workspace_id,
        connection_id=connection.connection_id,
        resolver=deps.resolver,
        clock=deps.clock,
        transport=deps.transport,
        now=deps.now(),
    )
    token.checkpoint()
    oauth.discard(new)
    return False


def _end(
    uow: HandlerUnitOfWork, run: _Run, outcome: Outcome, *, backoff: bool = True
) -> None:
    """The one run-ending write, on the worker's transaction (module docstring).

    ``backoff`` false records a transient code alone, with no backoff: the
    configuration check before the claim.
    """
    deps = dependencies()
    connection_id = run.connection.connection_id
    if isinstance(outcome, Terminal) and outcome.client_rejected:
        # Cleared first, in a unit of its own, then the files after its commit;
        # a connection that has moved on is left alone and the run writes nothing.
        retired = clear_rejected_client(
            run.job_ctx,
            connection_id=connection_id,
            client_id=run.client.client_id,
            generation=run.generation,
            now=deps.now(),
        )
        if retired is None:
            return
        for reference in retired:
            if reference is not None:
                oauth.discard(reference)
    current = claimed_connection(
        uow, connection_id=connection_id, generation=run.generation
    )
    if current is None:
        return
    now = deps.now()
    if isinstance(outcome, Terminal):
        write_terminal(uow, current, run.conn_ctx, now=now)
    elif isinstance(outcome, Transient):
        if backoff:
            write_transient(
                uow,
                current,
                code=outcome.code,
                now=now,
                retry_after=outcome.retry_after,
            )
        else:
            health_write(uow, current.connection_id, error=outcome.code)
    else:
        write_success(uow, current, now=now)
