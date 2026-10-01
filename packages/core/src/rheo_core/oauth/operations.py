"""``core.oauth_event.list``'s models and handler (issue #287, FR 20).

The supported read of the control plane's ``oauth_event`` trail. The declaration is
built and registered inside ``operations/core_ops.register_core_operations``, the
split ``core.audit.list`` follows; the models and the handler live here. Nothing in
this module imports ``rheo_core.operations``: that package's ``__init__`` imports
``core_ops`` first, so the reverse import would close a cycle, and the registrar
imports this module at call time instead.

Not an extension of ``core.audit.list``: that operation reads the workspace
database's ``core.audit_record``, while ``oauth_event`` is a control-plane table that
also holds rows bound to no workspace. Those unbound rows (registrations, a refusal
before a workspace was chosen) can name another account, so only the host operator
sees them; an owner, through a web session or a ``cli`` token, sees only the rows of
their own workspace. ``dispatch()``'s role check refuses everyone else before this
handler runs.
"""

from datetime import datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from rheo_contracts import ActorKind, WorkspaceContext

from rheo_core.storage import oauth_store
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.oauth_store import OAuthEventRow
from rheo_core.storage.postgres import get_backend

OAUTH_EVENT_LIST: Final = "core.oauth_event.list"


class OAuthEventListInput(BaseModel):
    """How many of the most recent events to return.

    Bounded like ``AuditListInput.limit`` and for its reason: a negative ``limit``
    would reach the ``SELECT`` and come back ``handler_failed`` instead of
    ``input_invalid``, and nothing prunes ``oauth_event``, so an unbounded read
    grows for the life of the deployment. Extra fields are ignored, as every other
    core input does; the workspace is the context's and no field can name one.
    """

    model_config = ConfigDict(extra="ignore")

    limit: int = Field(default=50, ge=1, le=500)


class OAuthEventRecord(BaseModel):
    """One ``oauth_event`` row as this operation publishes it.

    Every column of the table, with ``id`` published as ``event_id`` (the renaming
    ``AuditRecord`` makes for ``audit_id``). The table holds no secret, no
    client-supplied text and no URL, so there is nothing further to withhold: no
    client name, redirect URI, registration source address or hash is reachable
    from here. ``token_id`` is the grant's id (a grant is one access-token row).
    """

    model_config = ConfigDict(frozen=True)

    event_id: UUID
    occurred_at: datetime
    event: str
    outcome: str
    account_id: UUID | None
    workspace_id: UUID | None
    client_id: str | None
    token_id: UUID | None


class OAuthEventList(BaseModel):
    """The most recent events, newest first (``occurred_at`` then ``event_id``,
    both descending). A named collection field, like ``AuditList.records``, so a
    later collection beside it is an additive change."""

    model_config = ConfigDict(frozen=True)

    events: list[OAuthEventRecord]


def _published(row: OAuthEventRow) -> OAuthEventRecord:
    return OAuthEventRecord(
        event_id=row.id,
        occurred_at=row.occurred_at,
        event=row.event,
        outcome=row.outcome,
        account_id=row.account_id,
        workspace_id=row.workspace_id,
        client_id=row.client_id,
        token_id=row.token_id,
    )


def oauth_event_list_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: OAuthEventListInput
) -> OAuthEventList:
    """The context's workspace's events, plus the workspace-less ones for the
    operator alone.

    A control-plane read on its own connection, the read ``tokens.issue``'s
    ``revoke_handler`` makes; ``uow`` is the workspace database's and holds none of
    these rows. ``include_unbound`` keys on the actor kind, not the role: an owner
    is never shown a row that may name another account.
    """
    with get_backend().control_engine.connect() as connection:
        rows = oauth_store.list_oauth_events(
            connection,
            workspace_id=ctx.workspace_id,
            include_unbound=ctx.actor.kind is ActorKind.OPERATOR,
            limit=model_input.limit,
        )
    return OAuthEventList(events=[_published(row) for row in rows])
