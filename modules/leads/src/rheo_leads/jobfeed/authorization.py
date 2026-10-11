"""The owner's job feed authorization operations: connect, reconnect and the
completion the identity-host callback dispatches.

All three are for an owner on a browser session only and have no tool. Connect and
reconnect each read
what it needs in a short transaction that commits at once, then discovers the
authorization server and registers (or, on a reconnect, reuses) the OAuth client
with no transaction of its own open and without touching the dispatcher's, and
only then writes the pending authorization in a second short transaction of its
own. That second transaction replaces the previous pending row, so the files a
replaced row's freshly registered client owns are discarded only after it has
committed: the dispatcher offers a handler no after-commit hook and its own commit
comes later. The dispatcher's transaction carries nothing but its audit row; if
that later commit fails, the committed pending row simply expires or is replaced.

Registration happens here and nowhere else, at most once per call and never
retried. A first connect that fails at any point leaves no connection, health,
pending or secret-file row; a reconnect whose registration fails records only the
health code ``registration_failed`` on its connection.

``complete_authorization`` consumes the pending row in a committed unit of its own
first, then checks it with no network, then exchanges the code as the row's client
with no transaction open, then installs or re-pins in a second unit of its own that
commits before any file it retires (the old refresh token, a replaced client's
secrets) is discarded. A failure before that commit discards the new token file and
a freshly registered client's files instead, and nothing the connection references.
An ``invalid_client`` answer never leads to a registration here: a reused client is
cleared from the connection, which goes terminal; a fresh one is discarded. The
dispatcher's transaction carries only its audit row and, for a provider error on a
reconnect, the content-free health code, which retires nothing.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal, cast, get_args
from uuid import UUID

from rheo_contracts import (
    ActorKind,
    AuditSpec,
    Entry,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.connectors import oauth
from rheo_core.connectors.oauth import (
    ClientAuth,
    ClientAuthMethod,
    Discovered,
    OAuthFailure,
    RegisteredClient,
)
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import Handler
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.settings import resolve
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.work.schedules import ensure_module_schedule
from sqlalchemy import delete, insert, select, update

from rheo_leads.contracts import Strict
from rheo_leads.intake.accept import health_write
from rheo_leads.jobfeed import dependencies
from rheo_leads.jobfeed.install import install_connection
from rheo_leads.jobfeed.run import SCHEDULE_NAME
from rheo_leads.jobfeed.state import (
    RUN_CLAIM_WINDOW,
    JobfeedConnection,
    PendingAuthorization,
    clear_collector_health,
    read_connection,
    take_pending,
)
from rheo_leads.pipeline_common import JOBSEARCH_PRESET_ID, jobsearch_enabled, lock
from rheo_leads.references import identifier
from rheo_leads.storage import jobfeed as j
from rheo_leads.storage import pipeline as p

MODULE_ID: Final = "leads"
RESOURCE_URL_KEY: Final = "leads.jobfeed.resource_url"
CLIENT_NAME_KEY: Final = "connectors.oauth_client.client_name"
REGISTRATION_FAILED: Final = "registration_failed"
AUTHORIZATION_REQUIRED: Final = "authorization_required"
COMPLETE_AUTHORIZATION: Final = "leads.jobfeed.complete_authorization"
_AUTH_METHODS: Final[frozenset[str]] = frozenset(get_args(ClientAuthMethod))

# One fixed, content-free text per refusal code.
_DETAILS: Final[dict[str, str]] = {
    "role_not_permitted": "only the owner's browser session can do this",
    "capability_disabled": "job search is not enabled",
    "not_found": "record unavailable",
    "preset_not_allowed": "the pipeline does not use the Job search preset",
    "connection_exists": "the job feed is already connected",
    "endpoint_misconfigured": "the job feed endpoint is not configured",
    "registration_failed": "client registration failed",
    "registration_unsupported": "client registration is not supported",
    "record_stale": "the job feed changed; start again",
}


class ConnectInput(Strict):
    pipeline_ref: str


class ReconnectInput(Strict):
    pass


class AuthorizationStart(Strict):
    authorization_url: str
    expires_at: datetime


CompletionReason = Literal[
    "state_unknown",
    "state_expired",
    "account_mismatch",
    "capability_disabled",
    "provider_error",
    "issuer_mismatch",
    "exchange_failed",
    "client_rejected",
    "no_refresh_token",
    "endpoint_misconfigured",
]


class CompleteInput(Strict):
    """The four callback parameters; ``error_description`` is not one of them."""

    state: str
    code: str | None = None
    error: str | None = None
    iss: str | None = None


class AuthorizationCompletion(Strict):
    state: Literal["connected", "refused"]
    reason: CompletionReason | None = None


@dataclass(frozen=True)
class _Client:
    """The client the authorization URL is built with; ``registered`` says whether
    this call registered it (and so owns its secret files) or reused the stored one."""

    client_id: str
    auth_method: str
    secret_expires_at: datetime | None
    client_secret_ref: str | None
    registration_token_ref: str | None
    registered: bool


def _refused(code: str) -> OperationRefused:
    return OperationRefused(code, _DETAILS[code])


def _discard_files(*refs: str | None) -> None:
    """Best-effort; ``oauth.discard`` logs only fixed codes and never raises."""
    for ref in refs:
        if ref is not None:
            oauth.discard(ref)


def _require_owner_browser(ctx: WorkspaceContext) -> None:
    if ctx.actor.kind is not ActorKind.ACCOUNT or ctx.entry is not Entry.WEB:
        raise _refused("role_not_permitted")


def _require_capability(ctx: WorkspaceContext, uow: UnitOfWork) -> None:
    if not jobsearch_enabled(ctx, uow):
        raise _refused("capability_disabled")


def _configured_resource() -> tuple[str, str]:
    """The resource URL and the redirect URI, before any request is made. Refused
    ``endpoint_misconfigured`` while the connector client is off, the identity path
    is not the served one or the resource URL is unset."""
    resource_url = resolve().get_str(RESOURCE_URL_KEY)
    redirect: str | None = None
    try:
        redirect = oauth.redirect_uri(MODULE_ID)
    except OAuthFailure:
        redirect = None
    if redirect is None or not resource_url:
        raise _refused("endpoint_misconfigured")
    return resource_url, redirect


def _discover(resource_url: str) -> Discovered:
    deps = dependencies()
    discovered: Discovered | None = None
    try:
        discovered = oauth.discover(
            resource_url,
            resolver=deps.resolver,
            clock=deps.clock,
            transport=deps.transport,
        )
    except OAuthFailure:
        discovered = None
    if discovered is None:
        raise _refused("endpoint_misconfigured")
    return discovered


def _register(
    ctx: WorkspaceContext, discovered: Discovered, redirect: str, connection_id: UUID
) -> _Client | str:
    """One registration request; the client, or the refusal code it failed with."""
    deps = dependencies()
    try:
        registered = oauth.register(
            discovered.registration_endpoint,
            redirect,
            resolve().get_str(CLIENT_NAME_KEY),
            workspace_id=ctx.workspace_id,
            connection_id=connection_id,
            resolver=deps.resolver,
            clock=deps.clock,
            transport=deps.transport,
        )
    except OAuthFailure as failure:
        return failure.code if failure.code in _DETAILS else REGISTRATION_FAILED
    return _Client(
        client_id=registered.client_id,
        auth_method=registered.auth_method,
        secret_expires_at=registered.secret_expires_at,
        client_secret_ref=registered.client_secret_ref,
        registration_token_ref=registered.registration_token_ref,
        registered=True,
    )


def _start(
    ctx: WorkspaceContext,
    *,
    mode: str,
    connection_id: UUID,
    pipeline_id: UUID,
    discovered: Discovered,
    redirect: str,
    client: _Client,
) -> AuthorizationStart:
    """Replace the pending row in a short transaction of this handler's own, then
    discard the replaced row's freshly registered files, after that commit.

    Anything that fails before the commit discards the files this call registered
    and leaves the previous row and its files as they were. A connect re-checks
    under the lock that no connection was completed while it was registering.
    """
    retired: list[tuple[str | None, str | None]] = []
    committed = False
    try:
        now = dependencies().now()
        started = oauth.new_authorization(
            discovered, client_id=client.client_id, redirect_uri=redirect, now=now
        )
        with open_unit_of_work(ctx) as uow:
            lock(uow)
            if mode == "connect" and read_connection(uow.connection) is not None:
                raise _refused("connection_exists")
            if not client.registered and (
                _stored_client(read_connection(uow.connection)) != client
            ):
                # The reused client changed while this call was discovering (a
                # completed reconnect stored and retired clients): refuse the
                # existing retry code rather than point at retired files.
                raise _refused("record_stale")
            table = j.jobfeed_authorization
            # Only what this statement really deleted: a row a concurrent completion
            # already consumed is not returned, and its files are not touched.
            replaced = uow.connection.execute(
                delete(table).returning(
                    table.c.client_registered,
                    table.c.client_secret_ref,
                    table.c.registration_token_ref,
                )
            ).all()
            uow.connection.execute(
                insert(table).values(
                    id=uuid7(),
                    singleton=True,
                    state_hash=started.state_hash,
                    code_verifier=started.code_verifier,
                    account_id=ctx.actor.id,
                    connection_id=connection_id,
                    pipeline_id=pipeline_id,
                    mode=mode,
                    resource_url=discovered.resource_url,
                    issuer=discovered.issuer,
                    authorization_endpoint=discovered.authorization_endpoint,
                    token_endpoint=discovered.token_endpoint,
                    registration_endpoint=discovered.registration_endpoint,
                    revocation_endpoint=discovered.revocation_endpoint,
                    redirect_uri=redirect,
                    client_id=client.client_id,
                    client_auth_method=client.auth_method,
                    client_secret_expires_at=client.secret_expires_at,
                    client_secret_ref=client.client_secret_ref,
                    registration_token_ref=client.registration_token_ref,
                    client_registered=client.registered,
                    created_at=now,
                    expires_at=started.expires_at,
                )
            )
            # A reused client's files belong to the connection, never to the row.
            retired = [
                (row.client_secret_ref, row.registration_token_ref)
                for row in replaced
                if row.client_registered
            ]
            uow.commit()
            committed = True
    except BaseException:
        # Once committed, the new row references this call's files: keep them. A
        # failure while the unit closes still leaves the replaced row's files
        # unreferenced, so they go.
        if not committed and client.registered:
            _discard_files(client.client_secret_ref, client.registration_token_ref)
        if committed:
            for secret_ref, token_ref in retired:
                _discard_files(secret_ref, token_ref)
        raise
    for secret_ref, token_ref in retired:
        _discard_files(secret_ref, token_ref)
    return AuthorizationStart(
        authorization_url=started.authorization_url, expires_at=started.expires_at
    )


def connect(
    ctx: WorkspaceContext, uow: UnitOfWork, m: ConnectInput
) -> AuthorizationStart:
    _require_owner_browser(ctx)
    pipeline_id = identifier(m.pipeline_ref, "pipeline")
    with open_unit_of_work(ctx) as short:
        _require_capability(ctx, short)
        preset = short.connection.execute(
            select(p.pipeline.c.preset_id).where(p.pipeline.c.id == pipeline_id)
        ).scalar_one_or_none()
        if preset is None:
            raise _refused("not_found")
        if preset != JOBSEARCH_PRESET_ID:
            raise _refused("preset_not_allowed")
        if read_connection(short.connection) is not None:
            raise _refused("connection_exists")
        short.commit()
    resource_url, redirect = _configured_resource()
    discovered = _discover(resource_url)
    connection_id = uuid7()
    client = _register(ctx, discovered, redirect, connection_id)
    if isinstance(client, str):
        raise _refused(client)
    return _start(
        ctx,
        mode="connect",
        connection_id=connection_id,
        pipeline_id=pipeline_id,
        discovered=discovered,
        redirect=redirect,
        client=client,
    )


def _reusable(
    connection: JobfeedConnection, discovered: Discovered, redirect: str
) -> _Client | None:
    """The stored client, exactly when it was registered against the freshly
    discovered issuer and registration endpoint for the current redirect URI."""
    if (
        connection.issuer != discovered.issuer
        or connection.registration_endpoint != discovered.registration_endpoint
        or connection.client_redirect_uri != redirect
    ):
        return None
    return _stored_client(connection)


def _stored_client(connection: JobfeedConnection | None) -> _Client | None:
    """The connection's stored client as a reused one, or ``None`` when it has none."""
    if (
        connection is None
        or connection.client_id is None
        or connection.client_auth_method is None
    ):
        return None
    return _Client(
        client_id=connection.client_id,
        auth_method=connection.client_auth_method,
        secret_expires_at=connection.client_secret_expires_at,
        client_secret_ref=connection.client_secret_ref,
        registration_token_ref=connection.registration_token_ref,
        registered=False,
    )


def reconnect(
    ctx: WorkspaceContext, uow: UnitOfWork, m: ReconnectInput
) -> AuthorizationStart:
    _require_owner_browser(ctx)
    with open_unit_of_work(ctx) as short:
        _require_capability(ctx, short)
        connection = read_connection(short.connection)
        if connection is None:
            raise _refused("not_found")
        short.commit()
    resource_url, redirect = _configured_resource()
    discovered = _discover(resource_url)
    client = _reusable(connection, discovered, redirect)
    if client is None:
        registered = _register(ctx, discovered, redirect, connection.connection_id)
        if isinstance(registered, str):
            # After the last network call, in a transaction of its own: the health
            # code is the only thing a failed reconnect registration changes.
            with open_unit_of_work(ctx) as short:
                health_write(short, connection.connection_id, error=REGISTRATION_FAILED)
                short.commit()
            raise _refused(registered)
        client = registered
    return _start(
        ctx,
        mode="reconnect",
        connection_id=connection.connection_id,
        pipeline_id=connection.pipeline_id,
        discovered=discovered,
        redirect=redirect,
        client=client,
    )


# --- completion -----------------------------------------------------------------------

_EXCHANGE_REASONS: Final[dict[str, CompletionReason]] = {
    "no_refresh_token": "no_refresh_token",
    "endpoint_misconfigured": "endpoint_misconfigured",
    "client_rejected": "client_rejected",
}


def _refusal(reason: CompletionReason) -> AuthorizationCompletion:
    return AuthorizationCompletion(state="refused", reason=reason)


def _discard_fresh(pending: PendingAuthorization) -> None:
    """Discard the pending client's files when its start registered it; a reused
    client's files belong to the connection."""
    if pending.client_registered:
        _discard_files(pending.client_secret_ref, pending.registration_token_ref)


def _consume(
    ctx: WorkspaceContext, state: str
) -> tuple[PendingAuthorization, bool] | None:
    """Take the pending row for ``state`` and read the capability, committed before
    the caller acts on either; ``None`` when no row has that state."""
    state_hash = hashlib.sha256(state.encode("utf-8")).digest()
    with open_unit_of_work(ctx) as short:
        pending = take_pending(short.connection, state_hash)
        if pending is None:
            return None
        enabled = jobsearch_enabled(ctx, short)
        short.commit()
    return pending, enabled


def _check(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    pending: PendingAuthorization,
    m: CompleteInput,
    *,
    enabled: bool,
    now: datetime,
) -> CompletionReason | None:
    """The checks that need no network, in order; the first failing one's reason.

    A provider error on a reconnect records the health code on the dispatcher's
    transaction and nothing else: it retires no file and is never a terminal loss.
    """
    if pending.expires_at <= now:
        return "state_expired"
    if pending.account_id != ctx.actor.id:
        return "account_mismatch"
    if not enabled:
        return "capability_disabled"
    if m.error is not None:
        if pending.mode == "reconnect":
            current = read_connection(uow.connection)
            if current is not None and current.connection_id == pending.connection_id:
                health_write(uow, current.connection_id, error=AUTHORIZATION_REQUIRED)
        return "provider_error"
    if m.iss is not None and m.iss != pending.issuer:
        return "issuer_mismatch"
    if m.code is None:
        return "exchange_failed"
    return None


def _auth_method(pending: PendingAuthorization) -> ClientAuthMethod:
    """The pending row's client authentication method, checked against the three
    the OAuth client supports. The column's CHECK constraint admits only those,
    so the failure is unreachable; it keeps the narrowing honest."""
    method = pending.client_auth_method
    if method not in _AUTH_METHODS:
        raise ValueError("unsupported client authentication method")
    return cast(ClientAuthMethod, method)


@dataclass(frozen=True)
class _NewToken:
    """The reference to the refresh token the exchange just wrote."""

    reference: str = field(repr=False)


def _exchange(
    ctx: WorkspaceContext, pending: PendingAuthorization, code: str, now: datetime
) -> _NewToken | CompletionReason:
    """Exchange the code as the pending row's client: the new refresh token's
    reference, or the refusal reason. No failure text is ever kept."""
    deps = dependencies()
    client = ClientAuth(
        client_id=pending.client_id,
        auth_method=_auth_method(pending),
        client_secret_ref=pending.client_secret_ref,
        secret_expires_at=pending.client_secret_expires_at,
    )
    failed: str | None = None
    try:
        reference, _access = oauth.exchange_code(
            pending.token_endpoint,
            code=code,
            redirect_uri=pending.redirect_uri,
            code_verifier=pending.code_verifier,
            resource=pending.resource_url,
            client=client,
            workspace_id=ctx.workspace_id,
            connection_id=pending.connection_id,
            resolver=deps.resolver,
            clock=deps.clock,
            transport=deps.transport,
            now=now,
        )
    except OAuthFailure as failure:
        failed = failure.code
    if failed is not None:
        return _EXCHANGE_REASONS.get(failed, "exchange_failed")
    return _NewToken(reference)


def _clear_rejected_client(
    ctx: WorkspaceContext, pending: PendingAuthorization, now: datetime
) -> None:
    """The token endpoint rejected the connection's own stored client: clear it and
    make the connection terminal, in a unit of this handler's own, and discard the
    client's files only after that commits.

    A compare-and-set on ``client_id``: a connection that has since stored another
    client is left alone and nothing is discarded. No audit row is written here;
    the dispatcher's one completion row stands.
    """
    table = j.jobfeed_connection
    retired: tuple[str | None, ...] = ()
    committed = False
    try:
        with open_unit_of_work(ctx) as unit:
            lock(unit)
            current = read_connection(unit.connection, for_update=True)
            if (
                current is not None
                and current.connection_id == pending.connection_id
                and current.client_id == pending.client_id
            ):
                retired = (current.client_secret_ref, current.registration_token_ref)
                unit.connection.execute(
                    update(table)
                    .where(table.c.connection_id == current.connection_id)
                    .values(
                        client_id=None,
                        client_auth_method=None,
                        client_redirect_uri=None,
                        client_secret_expires_at=None,
                        client_secret_ref=None,
                        registration_token_ref=None,
                        authorization_state=AUTHORIZATION_REQUIRED,
                        updated_at=now,
                    )
                )
                ensure_module_schedule(
                    unit.connection,
                    module_id=MODULE_ID,
                    name=SCHEDULE_NAME,
                    enabled=False,
                    due_at=now,
                )
                health_write(unit, current.connection_id, error=AUTHORIZATION_REQUIRED)
            unit.commit()
            committed = True
    except BaseException:
        # Committed but failed while closing: the cleared files are unreferenced.
        if committed:
            _discard_files(*retired)
        raise
    _discard_files(*retired)


def _pinned(pending: PendingAuthorization) -> Discovered:
    """The endpoints the start discovered and pinned on the pending row."""
    return Discovered(
        resource_url=pending.resource_url,
        issuer=pending.issuer,
        authorization_endpoint=pending.authorization_endpoint,
        token_endpoint=pending.token_endpoint,
        registration_endpoint=pending.registration_endpoint,
        revocation_endpoint=pending.revocation_endpoint,
    )


def _registered_client(pending: PendingAuthorization) -> RegisteredClient:
    """The pending row's client as a registration result."""
    return RegisteredClient(
        client_id=pending.client_id,
        auth_method=_auth_method(pending),
        redirect_uri=pending.redirect_uri,
        secret_expires_at=pending.client_secret_expires_at,
        client_secret_ref=pending.client_secret_ref,
        registration_token_ref=pending.registration_token_ref,
    )


def _write_connection(
    ctx: WorkspaceContext,
    unit: UnitOfWork,
    pending: PendingAuthorization,
    reference: str,
    now: datetime,
) -> list[str | None]:
    """Install or re-pin under the lifecycle lock and enable the schedule; the
    files the write retires, to discard once it has committed."""
    table = j.jobfeed_connection
    lock(unit)
    retired: list[str | None] = []
    claimed: datetime | None = None
    if pending.mode == "connect":
        install_connection(
            unit,
            ctx,
            connection_id=pending.connection_id,
            pipeline_id=pending.pipeline_id,
            endpoints=_pinned(pending),
            client=_registered_client(pending),
        )
        unit.connection.execute(
            update(table)
            .where(table.c.connection_id == pending.connection_id)
            .values(token_ref=reference, credential_generation=1, updated_at=now)
        )
    else:
        current = read_connection(unit.connection, for_update=True)
        if current is None or current.connection_id != pending.connection_id:
            raise _refused("not_found")
        if not pending.client_registered and current.client_id != pending.client_id:
            # The reused client was replaced or cleared since the start: the new
            # token belongs to a client the connection no longer stores.
            raise _refused("record_stale")
        retired.append(current.token_ref)
        pinned = _pinned(pending)
        values: dict[str, object] = {
            "resource_url": pinned.resource_url,
            "issuer": pinned.issuer,
            "authorization_endpoint": pinned.authorization_endpoint,
            "token_endpoint": pinned.token_endpoint,
            "registration_endpoint": pinned.registration_endpoint,
            "revocation_endpoint": pinned.revocation_endpoint,
            "token_ref": reference,
            "credential_generation": current.credential_generation + 1,
            "consecutive_failures": 0,
            "not_before": None,
            "authorization_state": "connected",
            "updated_at": now,
        }
        if pending.client_registered:
            # The new client replaces the stored one, whose files then go.
            retired += [current.client_secret_ref, current.registration_token_ref]
            client = _registered_client(pending)
            values.update(
                client_id=client.client_id,
                client_auth_method=client.auth_method,
                client_redirect_uri=client.redirect_uri,
                client_secret_expires_at=client.secret_expires_at,
                client_secret_ref=client.client_secret_ref,
                registration_token_ref=client.registration_token_ref,
            )
        unit.connection.execute(
            update(table)
            .where(table.c.connection_id == current.connection_id)
            .values(**values)
        )
        clear_collector_health(unit.connection, current.connection_id)
        claimed = current.run_claimed_at
    due = now if claimed is None else max(now, claimed + RUN_CLAIM_WINDOW)
    ensure_module_schedule(
        unit.connection,
        module_id=MODULE_ID,
        name=SCHEDULE_NAME,
        enabled=True,
        due_at=due,
    )
    return retired


def _store(
    ctx: WorkspaceContext, pending: PendingAuthorization, reference: str, now: datetime
) -> AuthorizationCompletion:
    """Commit the writes in a unit of this handler's own, then discard what they
    retired. If the unit fails before its commit nothing it would have retired is
    touched, and the new token file and a freshly registered client's files go."""
    retired: list[str | None] = []
    committed = False
    try:
        with open_unit_of_work(ctx) as unit:
            retired = _write_connection(ctx, unit, pending, reference, now)
            unit.commit()
            committed = True
    except BaseException as error:
        if committed:
            # The committed connection references the new files; the retired
            # ones are unreferenced.
            _discard_files(*retired)
            raise
        oauth.discard(reference)
        _discard_fresh(pending)
        if isinstance(error, OperationRefused) and error.state == "capability_disabled":
            return _refusal("capability_disabled")
        raise
    _discard_files(*retired)
    return AuthorizationCompletion(state="connected")


def complete_authorization(
    ctx: WorkspaceContext, uow: UnitOfWork, m: CompleteInput
) -> AuthorizationCompletion:
    """Finish the owner's authorization from the callback (see the module docstring).

    The state is consumed first, in a committed unit of its own, so no later
    refusal or failure can make it usable again. Every refusal from then on is a
    ``refused`` result, so the dispatcher commits its one audit row normally.
    """
    _require_owner_browser(ctx)
    consumed = _consume(ctx, m.state)
    if consumed is None:
        return _refusal("state_unknown")
    pending, enabled = consumed
    now = dependencies().now()
    try:
        exchanged: _NewToken | CompletionReason | None = _check(
            ctx, uow, pending, m, enabled=enabled, now=now
        )
        if exchanged is None and m.code is not None:
            exchanged = _exchange(ctx, pending, m.code, now)
    except BaseException:
        _discard_fresh(pending)
        raise
    if isinstance(exchanged, _NewToken):
        return _store(ctx, pending, exchanged.reference, now)
    reason: CompletionReason = exchanged or "exchange_failed"
    if reason == "client_rejected" and not pending.client_registered:
        _clear_rejected_client(ctx, pending, now)
    else:
        _discard_fresh(pending)
    return _refusal(reason)


OPERATIONS: tuple[tuple[OperationDeclaration, Handler], ...] = (
    *(
        (
            OperationDeclaration(
                name=f"leads.jobfeed.{name}",
                safety_class=SafetyClass.MUTATE,
                roles=frozenset({Role.OWNER}),
                input_model=model,
                output=AuthorizationStart,
                idempotency=Idempotency.NONE,
                audit=AuditSpec(subject_field=None),
            ),
            handler,
        )
        for name, model, handler in (
            ("connect", ConnectInput, connect),
            ("reconnect", ReconnectInput, reconnect),
        )
    ),
    (
        OperationDeclaration(
            name=COMPLETE_AUTHORIZATION,
            safety_class=SafetyClass.MUTATE,
            roles=frozenset({Role.OWNER}),
            input_model=CompleteInput,
            output=AuthorizationCompletion,
            idempotency=Idempotency.NONE,
            audit=AuditSpec(subject_field=None),
        ),
        complete_authorization,
    ),
)
