"""The owner's job feed authorization operations: connect and reconnect.

Both are for an owner on a browser session only and have no tool. Each one reads
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
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final
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
from rheo_core.connectors.oauth import Discovered, OAuthFailure
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import Handler
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.settings import resolve
from rheo_core.storage.routing import open_unit_of_work
from sqlalchemy import delete, insert, select

from rheo_leads.contracts import Strict
from rheo_leads.intake.accept import health_write
from rheo_leads.jobfeed import dependencies
from rheo_leads.jobfeed.state import JobfeedConnection, read_connection
from rheo_leads.pipeline_common import JOBSEARCH_PRESET_ID, jobsearch_enabled, lock
from rheo_leads.references import identifier
from rheo_leads.storage import jobfeed as j
from rheo_leads.storage import pipeline as p

MODULE_ID: Final = "leads"
RESOURCE_URL_KEY: Final = "leads.jobfeed.resource_url"
CLIENT_NAME_KEY: Final = "connectors.oauth_client.client_name"
REGISTRATION_FAILED: Final = "registration_failed"

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
}


class ConnectInput(Strict):
    pipeline_ref: str


class ReconnectInput(Strict):
    pass


class AuthorizationStart(Strict):
    authorization_url: str
    expires_at: datetime


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
    try:
        now = dependencies().now()
        started = oauth.new_authorization(
            discovered, client_id=client.client_id, redirect_uri=redirect, now=now
        )
        with open_unit_of_work(ctx) as uow:
            lock(uow)
            if mode == "connect" and read_connection(uow.connection) is not None:
                raise _refused("connection_exists")
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
            uow.commit()
            # A reused client's files belong to the connection, never to the row.
            retired = [
                (row.client_secret_ref, row.registration_token_ref)
                for row in replaced
                if row.client_registered
            ]
    except BaseException:
        if client.registered:
            _discard_files(client.client_secret_ref, client.registration_token_ref)
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
        connection.client_id is None
        or connection.client_auth_method is None
        or connection.issuer != discovered.issuer
        or connection.registration_endpoint != discovered.registration_endpoint
        or connection.client_redirect_uri != redirect
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


OPERATIONS: tuple[tuple[OperationDeclaration, Handler], ...] = tuple(
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
)
