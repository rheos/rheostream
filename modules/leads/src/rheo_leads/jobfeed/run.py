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
minted token and discards its file. A compare-and-set that fails is retried once;
if that fails too, the minted token is kept on disk, never revoked, and the run ends
``refresh_token_unsaved`` (:func:`_rotate`). A rotated token whose file could not be
written leaves the stored one presumed dead, so that is terminal. A terminal answer
makes the connection need a reconnect; a rejected client is also cleared, and the
worker never registers one.

Then the MCP session: ``initialize``, the ``initialized`` notification and
``list_accounts``, each between a forced and an ordinary checkpoint. The fresh
account choices are stored before any decision; the account is selected without
any fallback (:func:`select_organization`), else ``account_selection_required``
ends the run with no ``find_jobs`` call. An MCP 401 or 403 is terminal, the stored
client kept; any other failure is transient and nothing is retried.

Then collection on the open session: the three default searches, paced and capped
at 80 raw rows, with the capability re-read in a short unit between pages (read off,
the run stops and writes nothing more); the overlapping sightings merged into one
per posting; the detail reads, where a failed or omitted read downgrades its
sighting. When every page call failed, the run ends as an upstream failure with
the backoff; a later failure, a failed or omitted detail or a deadline cut leaves
the run complete and partial.

Last, the deliveries and the report on the worker's transaction, committed with the
job's ``succeeded`` row: a forced lease extension, the lifecycle lock and the
connection row locked, then nothing at all unless the connection is still the one
the run claimed. Each delivery has its own savepoint below the lock, so a refused
one costs that row only. The report records the nine counts and the completed
check's time, resets the backoff, and sets ``collection_partial`` or clears the
collector's codes.

Expected failures never raise. Anything unexpected is logged by its type name and
raised as the fixed ``internal_error``, with no chained exception.
"""

import logging
from collections.abc import Sequence
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
from rheo_core.connectors.mcp_client import McpSession
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
from rheo_leads.jobfeed.collect import (
    Sighting,
    collect_presets,
    merge_sightings,
    read_details,
)
from rheo_leads.jobfeed.delivery import deliver
from rheo_leads.jobfeed.presets import DEFAULT_PRESETS
from rheo_leads.jobfeed.state import SCHEDULE_NAME as SCHEDULE_NAME
from rheo_leads.jobfeed.state import (
    JobfeedConnection,
    RunReport,
    claim_run,
    claimed_connection,
    clear_rejected_client,
    current_token,
    read_connection,
    replace_account_choices,
    store_selection,
    switch_token,
    write_success,
    write_terminal,
    write_transient,
)
from rheo_leads.jobfeed.upstream import (
    READ_ONLY_POLICY,
    Account,
    RunCut,
    Upstream,
    UpstreamFailure,
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
UPSTREAM_UNAVAILABLE: Final = "upstream_unavailable"
ACCOUNT_SELECTION_REQUIRED: Final = "account_selection_required"
COLLECTION_PARTIAL: Final = "collection_partial"
REFRESH_TOKEN_UNSAVED: Final = "refresh_token_unsaved"
TOKEN_SWITCH_FAILED_LOG: Final = "jobfeed_token_switch_failed %s"


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


Outcome = Terminal | Transient


@dataclass(frozen=True)
class Collected:
    """What collection hands to the final write step: one merged sighting per
    posting, and the report's counts that collection decides."""

    sightings: tuple[Sighting, ...]
    fetched: int
    """Raw rows, valid or not, before the merge."""
    pages: int
    detail_reads: int
    errors: int
    """Invalid rows plus downgraded sightings."""
    partial: bool
    """A page or detail failed, a detail was omitted, or the deadline cut the run."""


@dataclass
class _Run:
    """One run's facts. ``generation`` is what every run-ending write is guarded
    on: the one read at the start, the claimed one, then the rotated one."""

    job_ctx: WorkspaceContext
    conn_ctx: WorkspaceContext
    connection: JobfeedConnection
    client: ClientAuth | None
    generation: int
    deadline: float = 0.0

    def auth(self) -> ClientAuth:
        """The stored client; a run reaches a request only with one."""
        if self.client is None:
            raise TypeError("the run has no stored client")
        return self.client


def select_organization(accounts: Sequence[Account], stored: str | None) -> str | None:
    """The organization the run collects as, or ``None`` when the owner must pick.

    A stored organization is used only when it is listed exactly once and that
    entry is a TALENT account; with none stored, the only TALENT entry is used.
    Nothing ever falls back to another account.
    """
    if stored is not None:
        listed = [a for a in accounts if a.organization_id == stored]
        return stored if len(listed) == 1 and listed[0].is_talent else None
    talent = [a.organization_id for a in accounts if a.is_talent]
    return talent[0] if len(talent) == 1 else None


def collect_phase(
    upstream: Upstream, run: _Run, organization_id: str
) -> Collected | Outcome | None:
    """Collection as ``organization_id`` on the open session (module docstring).

    Returns what to deliver; the backoff outcome when no page call succeeded and
    one failed; ``None`` when the capability was read off between pages, and the
    run then writes nothing more. A refused credential raises.
    """
    deps = dependencies()
    job_hosts = resolve().get_list(JOB_HOSTS_KEY)
    collected = collect_presets(
        upstream,
        DEFAULT_PRESETS,
        organization_id,
        job_hosts,
        clock=deps.now,
        capability_on=lambda: _capability_on(run.job_ctx),
    )
    if collected.capability_lost:
        return None
    failures = collected.upstream_failures
    if collected.pages_ok == 0 and failures:
        limited = [f for f in failures if f.code == "rate_limited"]
        return _stopped(limited[0] if limited else failures[0])
    details = read_details(
        upstream,
        merge_sightings(collected.rows),
        organization_id,
        job_hosts,
        deadline=run.deadline,
    )
    return Collected(
        sightings=details.sightings,
        fetched=len(collected.rows) + collected.invalid_rows,
        pages=collected.pages_used,
        detail_reads=details.detail_reads,
        errors=collected.invalid_rows + details.failures,
        partial=bool(
            collected.search_failures
            or collected.cut
            or details.failures
            or details.cut
        ),
    )


def _capability_on(ctx: WorkspaceContext) -> bool:
    """The capability, re-read in a short unit of its own that ends at once."""
    with open_unit_of_work(ctx) as short:
        enabled = jobsearch_enabled(ctx, short)
        short.commit()
    return enabled


def session_phase(
    uow: HandlerUnitOfWork,
    token: CancellationToken,
    run: _Run,
    access: AccessToken,
) -> None:
    """The MCP session after a successful refresh: the set-up calls, the stored
    account choices, the selection, collection, then the deliveries and the
    report once the session is closed."""
    deps = dependencies()
    session = McpSession(
        run.connection.resource_url,
        access,
        READ_ONLY_POLICY,
        deadline=run.deadline,
        transport=deps.transport,
        resolver=deps.resolver,
        monotonic=deps.clock,
    )
    selection: str | Transient | None = None
    collected: Collected | Outcome | None = None
    stopped: UpstreamFailure | RunCut | None = None
    try:
        with Upstream(
            session,
            token,
            deadline=run.deadline,
            monotonic=deps.clock,
            sleep=deps.sleep,
        ) as upstream:
            selection = _select(run, upstream.list_accounts())
            if isinstance(selection, str):
                collected = collect_phase(upstream, run, selection)
    except (UpstreamFailure, RunCut) as error:
        stopped = error
    # Every write happens outside the except block, so nothing upstream is
    # chained to it.
    if stopped is not None:
        _end(uow, run, _stopped(stopped), backoff=not isinstance(stopped, RunCut))
    elif isinstance(selection, Transient):
        # Not a failure to back off from: the owner's choice makes the next run
        # due at the end of this claim's window.
        _end(uow, run, selection, backoff=False)
    elif isinstance(collected, Collected):
        _deliver_and_report(uow, token, run, collected)
    elif collected is not None:
        _end(uow, run, collected)


def _deliver_and_report(
    uow: HandlerUnitOfWork, token: CancellationToken, run: _Run, collected: Collected
) -> None:
    """The deliveries and the report, the run's first statements on the worker's
    transaction (module docstring). The lifecycle lock is taken before the first
    savepoint, so rolling one back never releases it."""
    deps = dependencies()
    token.checkpoint(force=True)
    current = claimed_connection(
        uow, connection_id=run.connection.connection_id, generation=run.generation
    )
    if current is None:
        return
    delivered = deliver(
        uow,
        run.conn_ctx,
        connection_id=current.connection_id,
        pipeline_id=current.pipeline_id,
        sightings=collected.sightings,
        now=deps.now(),
        checkpoint=token.checkpoint,
    )
    write_success(
        uow,
        current,
        now=deps.now(),
        report=RunReport(
            fetched=collected.fetched,
            imported=delivered.imported,
            skipped_dup=delivered.skipped_dup,
            refreshed=delivered.refreshed,
            filtered=delivered.filtered,
            skipped_untracked=delivered.skipped_untracked,
            errors=collected.errors + delivered.errors,
            detail_reads=collected.detail_reads,
            pages=collected.pages,
        ),
        partial=collected.partial or delivered.partial,
    )


def _select(run: _Run, accounts: Sequence[Account]) -> str | Transient | None:
    """Store the fresh choices, then decide: the organization to collect as, the
    ``account_selection_required`` outcome when the owner must pick one, or
    ``None`` when the connection moved on (an owner revoked, reconnected or
    selected meanwhile) and the run writes nothing.

    The choices are stored before any decision, so the owner can pick from them.
    No network call happens while either unit is open.
    """
    deps = dependencies()
    connection_id = run.connection.connection_id
    current = replace_account_choices(
        run.job_ctx,
        connection_id=connection_id,
        generation=run.generation,
        choices=[(a.organization_id, a.label) for a in accounts],
        now=deps.now(),
    )
    if current is None:
        return None
    selected = select_organization(accounts, current.organization_id)
    if selected is None:
        return Transient(ACCOUNT_SELECTION_REQUIRED)
    if current.organization_id is None and not store_selection(
        run.job_ctx,
        connection_id=connection_id,
        generation=run.generation,
        organization_id=selected,
        now=deps.now(),
    ):
        return None
    return selected


def _stopped(stopped: UpstreamFailure | RunCut) -> Outcome:
    """The outcome of an upstream call that failed, or of a run cut short."""
    if isinstance(stopped, RunCut):
        return Transient(COLLECTION_PARTIAL)
    if stopped.terminal:
        return Terminal()
    if stopped.code == "rate_limited":
        return Transient(UPSTREAM_RATE_LIMITED, retry_after=stopped.retry_after)
    if stopped.code == ENDPOINT_MISCONFIGURED:
        return Transient(ENDPOINT_MISCONFIGURED)
    return Transient(UPSTREAM_UNAVAILABLE)


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
    if not enabled or connection.authorization_state != "connected":
        return
    run = _Run(
        job_ctx=job_ctx,
        conn_ctx=conn_ctx,
        connection=connection,
        client=_client(connection),
        generation=connection.credential_generation,
    )
    if connection.token_ref is None or run.client is None:
        # Connected but unable to authenticate (a cleared client whose terminal
        # write never landed): make it terminal, so it does not stay connected.
        _end(uow, run, Terminal())
        return
    if connection.not_before is not None and now < connection.not_before:
        return
    settings = resolve()
    resource_url = settings.get_str(RESOURCE_URL_KEY)
    if not resource_url or not settings.get_list(JOB_HOSTS_KEY):
        return
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
    # Step 4 on: the session, the account, collection, deliveries and the report.
    session_phase(uow, token, run, access)


def _failed(failure: OAuthFailure) -> Outcome:
    if failure.terminal:
        return Terminal(client_rejected=failure.code == "client_rejected")
    if failure.code == "secret_write_failed":
        # A refresh raises it only after the provider rotated: the stored token
        # was spent on this refresh and is presumed dead, and the new one is lost.
        return Terminal()
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
            client=run.auth(),
            workspace_id=run.job_ctx.workspace_id,
            connection_id=connection.connection_id,
            resolver=deps.resolver,
            clock=deps.clock,
            transport=deps.transport,
            now=deps.now(),
        )
    except OAuthFailure as error:
        failure = error
    # A cancelled or lost job still finishes what the refresh started: a rotated
    # token is switched to, or revoked and discarded, before the stop propagates.
    stopped = _checkpoint(token)
    if failure is not None:
        _reraise(stopped)
        _end(uow, run, _failed(failure))
        return None
    if access is None:  # refresh either returns a token or raises
        if rotated is not None:
            oauth.discard(rotated)
        raise TypeError("refresh returned no access token")
    goes_on = rotated is None or _rotate(uow, token, run, current, rotated)
    _reraise(stopped)
    return access if goes_on else None


def _checkpoint(token: CancellationToken, *, force: bool = False) -> Exception | None:
    """Checkpoint, returning what it raised instead of raising it, so the caller
    can finish a cleanup first and then raise it with :func:`_reraise`."""
    try:
        token.checkpoint(force=force)
    except Exception as error:
        return error
    return None


def _reraise(*raised: Exception | None) -> None:
    """Raise the first captured exception, outside any except block."""
    for error in raised:
        if error is not None:
            raise error


def _rotate(
    uow: HandlerUnitOfWork, token: CancellationToken, run: _Run, old: str, new: str
) -> bool:
    """Switch to the rotated reference; whether the run goes on.

    Won: the old file goes only after the switch committed, and the run's guard
    moves to the new generation. Lost: the minted token is referenced by nothing,
    so it is revoked once and its file discarded; the run ends writing nothing.
    A checkpoint that raises on the way stops neither the revocation nor the
    discard; it is raised once both are done.

    Failed: the switch raised, and its commit may or may not have applied. What
    the row references, re-read in a fresh unit, decides. The new file: the switch
    took, and the run goes on as won. The old file at the run's generation: nothing
    moved, so the switch is tried once more in a fresh unit, and its answer counts
    as the first one's would have. Anything else (a revoke or reconnect won): the
    minted token is revoked and its file discarded, the old file is discarded only
    when the row no longer references it, and the failure is raised.

    Stranded: the retry raised too, or the re-read did. A provider that rotates on
    use has already spent the old token, so revoking the new one could leave the
    connection holding nothing that works. The minted file is kept and nothing is
    revoked or discarded; the run ends ``refresh_token_unsaved`` through
    :func:`_end`, whose guard writes nothing if the switch did take after all.
    """
    deps = dependencies()
    connection_id = run.connection.connection_id

    def switch() -> bool:
        return switch_token(
            run.job_ctx,
            connection_id=connection_id,
            old_ref=old,
            new_ref=new,
            generation=run.generation,
            now=deps.now(),
        )

    def won() -> bool:
        oauth.discard(old)
        run.generation += 1
        return True

    try:
        switched = switch()
    except Exception as error:
        failure = error
    else:
        if switched:
            return won()
        _reraise(*_retire_minted(token, run, new))
        return False
    try:
        current = current_token(run.job_ctx, connection_id=connection_id)
    except Exception:
        return _stranded(uow, run, failure)
    if current is not None and current[0] == new:
        oauth.discard(old)
        run.generation = current[1]
        return True
    if current == (old, run.generation):
        try:
            switched = switch()
        except Exception:
            return _stranded(uow, run, failure)
        if switched:
            return won()
        _reraise(*_retire_minted(token, run, new))
        return False
    stops = _retire_minted(token, run, new)
    if current is None or current[0] != old:
        oauth.discard(old)
    _reraise(failure, *stops)
    return False


def _stranded(uow: HandlerUnitOfWork, run: _Run, failure: Exception) -> bool:
    """End a run whose rotated token could not be switched to, keeping its file."""
    _log.warning(TOKEN_SWITCH_FAILED_LOG, type(failure).__name__)
    _end(uow, run, Transient(REFRESH_TOKEN_UNSAVED))
    return False


def _retire_minted(
    token: CancellationToken, run: _Run, new: str
) -> tuple[Exception | None, ...]:
    """Revoke a minted token nothing references, once, then discard its file:
    a forced checkpoint before the call and the ordinary one after it. Neither
    a raising checkpoint nor a failed revocation skips the discard; what they
    raised is returned for the caller to raise afterwards."""
    deps = dependencies()
    connection = run.connection
    before_call = _checkpoint(token, force=True)
    failed: Exception | None = None
    try:
        oauth.revoke(
            connection.revocation_endpoint,
            refresh_ref=new,
            client=run.auth(),
            workspace_id=run.job_ctx.workspace_id,
            connection_id=connection.connection_id,
            resolver=deps.resolver,
            clock=deps.clock,
            transport=deps.transport,
            now=deps.now(),
        )
    except Exception as error:  # revoke never raises; the discard runs regardless
        failed = error
    after_call = _checkpoint(token)
    oauth.discard(new)
    return (before_call, failed, after_call)


def _end(
    uow: HandlerUnitOfWork, run: _Run, outcome: Outcome, *, backoff: bool = True
) -> None:
    """A run-ending failure write, on the worker's transaction (module docstring).

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
            client_id=run.auth().client_id,
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
    elif backoff:
        write_transient(
            uow,
            current,
            code=outcome.code,
            now=now,
            retry_after=outcome.retry_after,
        )
    else:
        health_write(uow, current.connection_id, error=outcome.code)
