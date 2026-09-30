"""``core.evidence_enrollment.create``, ``.rotate`` and ``.revoke`` (spec § Architecture
→ System Components, item 5; FR 1, FR 2).

An enrollment binds one account's local Claude Code bridge, on one machine and one
directory, to a ``kind='cli'`` bridge token whose snapshot is exactly
``["core.evidence.ingest"]``. Its id is every local evidence row's ``authority_id``, so
rotating the token keeps the id and every still-pending row stays verifiable.

**Target-account rule, shared by all three** (:func:`_target_account`). A session
actor acts only for its own account; a token actor only for the token's own account;
an operator names the account in the payload. A payload ``account_id`` from a session
or token actor is ignored, as ``core.token.issue`` ignores it for a session issuer. An
enrollment id that is absent, or belongs to another account than the target, answers
``not_found``, the ``tokens/issue.py`` non-disclosure rule. That covers a token caller
of ``revoke`` too, the same scoping ``core.token.revoke`` applies to a token actor.

**The fingerprint pair follows the same rule** (#244). ``create`` answers
``enrollment_exists`` only when the target account itself holds the pair. A pair held
by another account's active enrollment answers a ``not_found`` refusal instead, so
``enrollment_exists`` never tells one account that another has enrolled. The
active-pair index stays global (one live enrollment per machine and directory), so a
create on a pair another account holds still fails. The refusal names no account and
no enrollment, but a caller can still tell that the pair is unavailable; hiding even
that needs the index scoped by account, a migration.

**Membership** is checked by ``create`` and ``rotate``, the two operations that mint a
token, before anything is read or written. ``revoke`` does not check it, so an account
that has left the workspace can still have its enrollment revoked.

**Two databases, no shared transaction.** The token lives in the control plane and the
enrollment row in the workspace database, which ``dispatch()`` commits after the
handler returns (the limitation ``tokens/issue.py``'s module docstring names). Every
handler orders its two writes so that either half alone fails closed; each says how.

Content-free: no handler logs a token value, and the raw value appears once, in the
``create``/``rotate`` response.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict, Field
from rheo_contracts import ActorKind, ContextPurpose, WorkspaceContext
from sqlalchemy import Connection, insert, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from rheo_core.boundary.context import MEMBERSHIP_MISSING
from rheo_core.evidence.local_authority import ENROLLMENT_INACTIVE
from rheo_core.operations.refusals import OPERATION_NOT_PERMITTED, OperationRefused
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import NOT_FOUND
from rheo_core.storage import control_tables
from rheo_core.storage.backend import StorageRefusal, UnitOfWork
from rheo_core.storage.control_plane import (
    get_access_token,
    get_membership,
    revoke_access_token,
)
from rheo_core.storage.evidence_enrollment_tables import (
    FINGERPRINT_PATTERN,
    STATE_ACTIVE,
    STATE_REVOKED,
    evidence_enrollment,
)
from rheo_core.storage.postgres import get_backend
from rheo_core.tokens.issue import ACCOUNT_REQUIRED, issue_bridge_token

logger = logging.getLogger("rheo_core.evidence")

ENROLLMENT_CREATE: Final = "core.evidence_enrollment.create"
ENROLLMENT_ROTATE: Final = "core.evidence_enrollment.rotate"
ENROLLMENT_REVOKE: Final = "core.evidence_enrollment.revoke"
"""Literal strings; ``tokens/policy.py`` spells the first two again in
``NON_TOKEN_ISSUABLE`` for the import-cycle reason that module gives."""

ENROLLMENT_EXISTS: Final = "enrollment_exists"
"""An active enrollment of the target account already holds the machine and project
fingerprint pair. Another account's holds answer :data:`PAIR_UNAVAILABLE_TEXT` under
``not_found`` instead."""

PAIR_HELD_TEXT: Final = "this machine and directory are already enrolled"
PAIR_UNAVAILABLE_TEXT: Final = (
    "no enrollment is available for this machine and directory"
)
"""``not_found``'s text for a pair another account holds: no account, no id."""

ENROLLMENT_CONTENDED: Final = "enrollment_contended"
"""A concurrent create won the pair and was revoked again before this create could say
whose it was. Nothing was enrolled and the minted token is revoked; retry the create."""

MINTED_TOKEN_REVOKE_FAILED_LOG: Final = "evidence_enrollment_minted_token_revoke_failed"
"""Logged, with the error type only, when a create that lost the race could not
revoke the token it had minted."""

ACTIVE_PAIR_INDEX: Final = "evidence_enrollment_active_pair"
"""The partial unique index behind :data:`ENROLLMENT_EXISTS`."""

BRIDGE_PURPOSE: Final = ContextPurpose.INTERNAL_ANALYSIS
"""The one purpose v1 enrolls under (spec § Architect calls, C5)."""

OLD_TOKEN_REVOKE_FAILED_LOG: Final = "evidence_enrollment_old_token_revoke_failed"
"""Logged, with the error type only, when ``rotate`` could not revoke the old token."""

_IGNORE_EXTRA: Final = ConfigDict(extra="ignore")
_FINGERPRINT: Final = Field(pattern=FINGERPRINT_PATTERN)


class EnrollmentCreateInput(BaseModel):
    model_config = _IGNORE_EXTRA

    machine_fingerprint: str = _FINGERPRINT
    project_fingerprint: str = _FINGERPRINT
    account_id: UUID | None = None
    """Required for an operator, ignored for a session actor."""


class EnrollmentRotateInput(BaseModel):
    model_config = _IGNORE_EXTRA

    enrollment_id: UUID
    account_id: UUID | None = None


class EnrollmentRevokeInput(BaseModel):
    model_config = _IGNORE_EXTRA

    enrollment_id: UUID
    account_id: UUID | None = None


class EnrollmentCreated(BaseModel):
    """``create``'s and ``rotate``'s answer. ``value`` is the raw bridge token,
    returned once, never stored or logged."""

    model_config = ConfigDict(frozen=True)

    enrollment_id: UUID
    token_id: UUID
    value: str
    expires_at: datetime


class EnrollmentRevoked(BaseModel):
    model_config = ConfigDict(frozen=True)

    enrollment_id: UUID


def _target_account(ctx: WorkspaceContext, payload_account_id: UUID | None) -> UUID:
    """The account an enrollment operation acts for, by the actor's kind alone."""
    if ctx.actor.kind is ActorKind.ACCOUNT:
        assert ctx.actor.id is not None
        return ctx.actor.id
    if ctx.actor.kind is ActorKind.TOKEN:
        if ctx.principal.account_id is None:
            raise OperationRefused(ACCOUNT_REQUIRED, "this token carries no account")
        return ctx.principal.account_id
    if ctx.actor.kind is ActorKind.OPERATOR:
        if payload_account_id is None:
            raise OperationRefused(
                ACCOUNT_REQUIRED, "an operator enrollment call needs an account_id"
            )
        return payload_account_id
    raise OperationRefused(
        ACCOUNT_REQUIRED,
        f"no enrollment account for actor kind {ctx.actor.kind.value!r}",
    )


def _refuse_token_actor(ctx: WorkspaceContext, operation: str) -> None:
    """``create`` and ``rotate`` mint a token, so no token may call them. Both are in
    ``NON_TOKEN_ISSUABLE``, so dispatch already refuses; this holds without it."""
    if ctx.actor.kind is ActorKind.TOKEN:
        raise OperationRefused(
            OPERATION_NOT_PERMITTED, f"{operation} is not callable with a token"
        )


def _issued_from(ctx: WorkspaceContext) -> str:
    """``session`` for an account actor, ``operator`` for an operator, as
    ``tokens.issue._issuer_permitted_set`` chooses it."""
    return "session" if ctx.actor.kind is ActorKind.ACCOUNT else "operator"


def _require_membership(account_id: UUID, workspace_id: UUID) -> None:
    with get_backend().control_engine.connect() as connection:
        membership = get_membership(
            connection, account_id=account_id, workspace_id=workspace_id
        )
    if membership is None:
        raise OperationRefused(
            MEMBERSHIP_MISSING, "the account has no membership in this workspace"
        )


def _mint(
    ctx: WorkspaceContext, account_id: UUID, purpose: str
) -> tuple[UUID, str, datetime]:
    """A new bridge token for ``account_id``: ``(token_id, value, expires_at)``.

    The expiry is read back from the row just written rather than recomputed, so the
    answer is the stored value."""
    token_id, value = issue_bridge_token(
        account_id=account_id,
        workspace_id=ctx.workspace_id,
        purpose=purpose,
        issued_from=_issued_from(ctx),
    )
    with get_backend().control_engine.connect() as connection:
        row = get_access_token(connection, token_id)
    assert row is not None  # written and committed by issue_bridge_token just now
    return token_id, value, row.expires_at


def _constraint_name(violation: IntegrityError) -> str:
    """The violated constraint's name, as ``storage/control_plane.py`` reads it."""
    orig = violation.orig
    if isinstance(orig, psycopg.Error):
        return orig.diag.constraint_name or ""
    return ""


def _owned_enrollment(
    uow: UnitOfWork, enrollment_id: UUID, account_id: UUID
) -> tuple[UUID, str, str]:
    """``(token_id, state, purpose)`` of the target's own enrollment, locked for
    update; an absent id and another account's id both answer ``not_found``."""
    row = uow.connection.execute(
        select(
            evidence_enrollment.c.account_id,
            evidence_enrollment.c.token_id,
            evidence_enrollment.c.state,
            evidence_enrollment.c.purpose,
        )
        .where(evidence_enrollment.c.id == enrollment_id)
        .with_for_update()
    ).one_or_none()
    if row is None or row.account_id != account_id:
        raise OperationRefused(NOT_FOUND, f"no enrollment {enrollment_id}")
    return row.token_id, row.state, row.purpose


def _refuse_held_pair(
    uow: UnitOfWork,
    model_input: EnrollmentCreateInput,
    account_id: UUID,
    *,
    cause: IntegrityError | None = None,
) -> None:
    """Refuse if an active enrollment holds the pair: ``enrollment_exists`` when it is
    ``account_id``'s own, ``not_found`` when it is another account's. Returns when no
    active enrollment holds it."""
    holder: UUID | None = uow.connection.execute(
        select(evidence_enrollment.c.account_id).where(
            evidence_enrollment.c.machine_fingerprint
            == model_input.machine_fingerprint,
            evidence_enrollment.c.project_fingerprint
            == model_input.project_fingerprint,
            evidence_enrollment.c.state == STATE_ACTIVE,
        )
    ).scalar_one_or_none()
    if holder is None:
        return
    if holder == account_id:
        raise OperationRefused(ENROLLMENT_EXISTS, PAIR_HELD_TEXT) from cause
    raise OperationRefused(NOT_FOUND, PAIR_UNAVAILABLE_TEXT) from cause


def create_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: EnrollmentCreateInput
) -> EnrollmentCreated:
    """Mint the bridge token, then insert the enrollment holding its id.

    The token is minted first and committed in the control plane. A create that
    loses the race on the active-pair index revokes that token before it refuses, so
    it leaves no orphan. If any other insert failure happens, or that revoke itself
    fails, an orphan token remains whose only operation, ``core.evidence.ingest``,
    finds no enrollment for it and refuses ``enrollment_inactive``. Accepted, not a
    bug: the orphan can do nothing.
    """
    _refuse_token_actor(ctx, ENROLLMENT_CREATE)
    account_id = _target_account(ctx, model_input.account_id)
    _require_membership(account_id, ctx.workspace_id)
    _refuse_held_pair(uow, model_input, account_id)
    token_id, value, expires_at = _mint(ctx, account_id, BRIDGE_PURPOSE.value)
    enrollment_id = uuid7()
    # A concurrent create can pass the pre-check above and lose the race on the
    # partial unique index; it gets the pre-check's answer, read again after the
    # violation. The savepoint keeps the handler's transaction usable for that read.
    try:
        with uow.connection.begin_nested():
            uow.connection.execute(
                insert(evidence_enrollment).values(
                    id=enrollment_id,
                    account_id=account_id,
                    token_id=token_id,
                    machine_fingerprint=model_input.machine_fingerprint,
                    project_fingerprint=model_input.project_fingerprint,
                    purpose=BRIDGE_PURPOSE.value,
                    state=STATE_ACTIVE,
                    created_at=datetime.now(UTC),
                    revoked_at=None,
                )
            )
    except IntegrityError as violation:
        if _constraint_name(violation) != ACTIVE_PAIR_INDEX:
            raise
        _discard_minted(token_id)
        _refuse_held_pair(uow, model_input, account_id, cause=violation)
        # The winner was revoked between the violation and the re-read.
        raise OperationRefused(
            ENROLLMENT_CONTENDED,
            "a concurrent change moved this machine and directory; retry",
        ) from violation
    return EnrollmentCreated(
        enrollment_id=enrollment_id,
        token_id=token_id,
        value=value,
        expires_at=expires_at,
    )


def _set_token_id(uow: UnitOfWork, enrollment_id: UUID, token_id: UUID) -> None:
    uow.connection.execute(
        update(evidence_enrollment)
        .where(evidence_enrollment.c.id == enrollment_id)
        .values(token_id=token_id)
    )


def _discard_minted(token_id: UUID) -> None:
    """Revoke the token a create minted and then could not use. Logged, not raised,
    on failure: the refusal the caller is about to get matters more, and the orphan
    fails closed anyway."""
    try:
        _revoke_token_if_live(token_id)
    except (SQLAlchemyError, StorageRefusal) as failure:
        logger.warning(
            MINTED_TOKEN_REVOKE_FAILED_LOG,
            extra={"error_type": type(failure).__name__},
        )


def _revoke_token_if_live(token_id: UUID) -> None:
    """Revoke ``token_id`` in the control plane unless it is gone or already revoked."""
    with get_backend().control_engine.begin() as connection:
        row = get_access_token(connection, token_id)
        if row is not None and row.revoked_at is None:
            revoke_access_token(connection, token_id)


def rotate_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: EnrollmentRotateInput
) -> EnrollmentCreated:
    """Mint a new bridge token, point the enrollment at it, then revoke the old one.

    The enrollment id is unchanged, so every pending row's ``authority_id`` stays
    valid. Both partial failures fail closed:

    - the row update fails after the mint: the handler raises, the row keeps the old
      token id, and the new token matches no enrollment (``enrollment_inactive``);
    - the old-token revoke fails: it is logged and **not** raised, so the row update
      still commits. Raising would roll that update back and leave the old token
      live and still matching; not raising leaves it live but matching no enrollment,
      so its one operation refuses ``enrollment_inactive``.

    The workspace row update commits after the old token is already revoked in the
    control plane. If that commit fails, the active row points at a revoked token,
    which fails closed: ``rheo doctor`` shows ``FAIL`` and a retried rotate repairs
    the enrollment. It does not repair rows already lost: since #244 the authority
    refuses an enrollment whose token is revoked, so any claim that runs before the
    retry settles the enrollment's pending rows ``authority_unverified``, which is
    terminal. A failed rotate of this kind therefore costs those rows. The new token
    carries the row's stored purpose, not a constant.

    Membership is checked first, exactly as ``create`` checks it, so no token is
    minted for an account that has left the workspace (#244).
    """
    _refuse_token_actor(ctx, ENROLLMENT_ROTATE)
    account_id = _target_account(ctx, model_input.account_id)
    _require_membership(account_id, ctx.workspace_id)
    old_token_id, state, purpose = _owned_enrollment(
        uow, model_input.enrollment_id, account_id
    )
    if state != STATE_ACTIVE:
        raise OperationRefused(ENROLLMENT_INACTIVE, "the enrollment is revoked")
    token_id, value, expires_at = _mint(ctx, account_id, purpose)
    _set_token_id(uow, model_input.enrollment_id, token_id)
    try:
        _revoke_token_if_live(old_token_id)
    except (SQLAlchemyError, StorageRefusal) as failure:
        logger.warning(
            OLD_TOKEN_REVOKE_FAILED_LOG, extra={"error_type": type(failure).__name__}
        )
    return EnrollmentCreated(
        enrollment_id=model_input.enrollment_id,
        token_id=token_id,
        value=value,
        expires_at=expires_at,
    )


def revoke_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: EnrollmentRevokeInput
) -> EnrollmentRevoked:
    """Revoke the bridge token, then mark the enrollment ``revoked``.

    Either half alone fails closed: an ``active`` row whose token is revoked fails
    ingest's token check before the enrollment is read, and a live token under a
    ``revoked`` row fails the enrollment check. A token caller may revoke its own
    account's enrollment; another account's answers ``not_found``.
    """
    account_id = _target_account(ctx, model_input.account_id)
    token_id, state, _ = _owned_enrollment(uow, model_input.enrollment_id, account_id)
    if state != STATE_ACTIVE:
        raise OperationRefused(ENROLLMENT_INACTIVE, "the enrollment is already revoked")
    _revoke_token_if_live(token_id)
    uow.connection.execute(
        update(evidence_enrollment)
        .where(evidence_enrollment.c.id == model_input.enrollment_id)
        .values(state=STATE_REVOKED, revoked_at=datetime.now(UTC))
    )
    return EnrollmentRevoked(enrollment_id=model_input.enrollment_id)


# --- the doctor's read -----------------------------------------------------------

EXPIRY_WARN: Final = timedelta(days=14)
EXPIRY_FAIL: Final = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class EnrollmentTokenCounts:
    """Active enrollments by their bridge token's standing. Counts only."""

    active: int
    revoked: int
    """The token is revoked, or has no control-plane row, while the row is active."""
    expired: int
    within_fail: int
    """Expires within :data:`EXPIRY_FAIL`."""
    within_warn: int
    """Expires within :data:`EXPIRY_WARN` but not :data:`EXPIRY_FAIL`."""


def enrollment_token_counts(
    workspace_connection: Connection, control_connection: Connection, *, now: datetime
) -> EnrollmentTokenCounts | None:
    """Count one workspace's active enrollments by token standing; ``None`` when the
    database has no ``core.evidence_enrollment`` (not yet at revision 0011).

    Each enrollment lands in one bucket, the worst it qualifies for. The token ids
    stay inside this function; nothing it returns names a row.
    """
    present: str | None = workspace_connection.execute(
        text("SELECT to_regclass('core.evidence_enrollment')")
    ).scalar_one()
    if present is None:
        return None
    token_ids: list[UUID] = list(
        workspace_connection.execute(
            select(evidence_enrollment.c.token_id).where(
                evidence_enrollment.c.state == STATE_ACTIVE
            )
        ).scalars()
    )
    tokens = {
        row.id: row
        for row in control_connection.execute(
            select(
                control_tables.access_token.c.id,
                control_tables.access_token.c.expires_at,
                control_tables.access_token.c.revoked_at,
            ).where(control_tables.access_token.c.id.in_(token_ids))
        )
    }
    revoked = expired = within_fail = within_warn = 0
    for token_id in token_ids:
        token = tokens.get(token_id)
        if token is None or token.revoked_at is not None:
            revoked += 1
        elif token.expires_at <= now:
            expired += 1
        elif token.expires_at <= now + EXPIRY_FAIL:
            within_fail += 1
        elif token.expires_at <= now + EXPIRY_WARN:
            within_warn += 1
    return EnrollmentTokenCounts(
        active=len(token_ids),
        revoked=revoked,
        expired=expired,
        within_fail=within_fail,
        within_warn=within_warn,
    )
