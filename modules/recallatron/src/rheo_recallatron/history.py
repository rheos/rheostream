"""Owner-only search and read of imported historical evidence.

These records are not memories. In particular, an unconfirmed predecessor note is
searchable evidence, never a statement Recallatron has accepted as true. No ambient
producer writes this table; the one-time importer supplies its source identities.
"""

from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from pydantic import Field, field_validator
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import func, select, update
from sqlalchemy.engine import RowMapping

from rheo_recallatron.configuration import MODULE_ID
from rheo_recallatron.contracts import Strict
from rheo_recallatron.retrieval.lexical_query import lexical_tsquery
from rheo_recallatron.storage.tables import history_record

HISTORY_SEARCH: Final = f"{MODULE_ID}.history.search"
HISTORY_GET: Final = f"{MODULE_ID}.history.get"
HISTORY_ERASE: Final = f"{MODULE_ID}.history.erase"
HISTORY_READ_ROLES: Final = frozenset({Role.OWNER})


class HistorySearchInput(Strict):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=20, ge=1, le=100)

    @field_validator("query")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class HistoryGetInput(Strict):
    id: UUID


class HistoryEraseInput(Strict):
    id: UUID


class HistoryErased(Strict):
    id: UUID
    erased_at: datetime


class HistoryItem(Strict):
    id: UUID
    kind: str
    status: str
    title: str
    body: str
    occurred_at: datetime
    source_namespace: str
    external_source_key: str
    session_key: str | None
    source_role: str | None
    source_category: str | None


class HistorySearchHit(HistoryItem):
    score: float


class HistorySearchResult(Strict):
    items: tuple[HistorySearchHit, ...]
    strategy: str = "lexical"


def _owner(ctx: WorkspaceContext) -> None:
    # The registry gates the operation too. Keeping the check in the handler means
    # a direct internal call cannot turn the private archive into a member read.
    if ctx.role is not Role.OWNER:
        raise OperationRefused("not_found", "history is owner-only")


def _item(row: RowMapping) -> HistoryItem:
    return HistoryItem(
        id=row["id"],
        kind=row["kind"],
        status=row["status"],
        title=row["title"],
        body=row["body"],
        occurred_at=row["occurred_at"],
        source_namespace=row["source_namespace"],
        external_source_key=row["source_reference"] or row["external_source_key"],
        session_key=row["session_key"],
        source_role=row["source_role"],
        source_category=row["source_category"],
    )


def search(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: HistorySearchInput
) -> HistorySearchResult:
    _owner(ctx)
    query = lexical_tsquery(uow.connection, model_input.query)
    score = func.ts_rank_cd(history_record.c.search_tsv, query)
    statement = (
        select(history_record, score.label("score"))
        .where(
            history_record.c.erased_at.is_(None),
            history_record.c.search_tsv.bool_op("@@")(query),
        )
        .order_by(
            score.desc(), history_record.c.occurred_at.desc(), history_record.c.id
        )
        .limit(model_input.limit)
    )
    rows = uow.connection.execute(statement).all()
    return HistorySearchResult(
        items=tuple(
            HistorySearchHit(**_item(row._mapping).model_dump(), score=float(row.score))
            for row in rows
        )
    )


def get(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: HistoryGetInput
) -> HistoryItem:
    _owner(ctx)
    row = uow.connection.execute(
        select(history_record).where(
            history_record.c.id == model_input.id,
            history_record.c.erased_at.is_(None),
        )
    ).first()
    if row is None:
        raise OperationRefused("not_found", "history record is unavailable")
    return _item(row._mapping)


def erase(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: HistoryEraseInput
) -> HistoryErased:
    """Erase content after the dispatcher's per-action destructive approval.

    Keep only the source identity as a tombstone so a migration replay cannot
    recreate the content. Metadata that could identify a person is wiped too.
    """
    _owner(ctx)
    erased_at = datetime.now(UTC)
    erased_id = uow.connection.execute(
        update(history_record)
        .where(
            history_record.c.id == model_input.id,
            history_record.c.erased_at.is_(None),
        )
        .values(
            title="",
            body="",
            source_reference=None,
            session_key=None,
            chat_key=None,
            source_role=None,
            source_category=None,
            source_created_at=None,
            confirmed_at=None,
            superseded_by_source_key=None,
            erased_at=erased_at,
        )
        .returning(history_record.c.id)
    ).scalar_one_or_none()
    if erased_id is None:
        raise OperationRefused("not_found", "history record is unavailable")
    return HistoryErased(id=erased_id, erased_at=erased_at)


HISTORY_SEARCH_DECLARATION: Final = OperationDeclaration(
    name=HISTORY_SEARCH,
    safety_class=SafetyClass.READ,
    roles=HISTORY_READ_ROLES,
    input_model=HistorySearchInput,
    output=HistorySearchResult,
    idempotency=Idempotency.NONE,
    audit=None,
)

HISTORY_GET_DECLARATION: Final = OperationDeclaration(
    name=HISTORY_GET,
    safety_class=SafetyClass.READ,
    roles=HISTORY_READ_ROLES,
    input_model=HistoryGetInput,
    output=HistoryItem,
    idempotency=Idempotency.NONE,
    audit=None,
)

HISTORY_ERASE_DECLARATION: Final = OperationDeclaration(
    name=HISTORY_ERASE,
    safety_class=SafetyClass.DESTRUCTIVE,
    roles=HISTORY_READ_ROLES,
    input_model=HistoryEraseInput,
    output=HistoryErased,
    idempotency=Idempotency.NONE,
    audit=AuditSpec(subject_field=None),
)

HISTORY_OPERATIONS: Final = (
    (HISTORY_SEARCH_DECLARATION, search),
    (HISTORY_GET_DECLARATION, get),
    (HISTORY_ERASE_DECLARATION, erase),
)
