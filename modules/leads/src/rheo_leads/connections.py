"""Owner-managed website connections; secret bytes stay in core's presenter."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import Field
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    ToolDeclaration,
    WorkspaceContext,
)
from rheo_core.connectors.contracts import ConnectionState
from rheo_core.connectors.credentials import create_key
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import Handler
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.settings import ResolvedSettings, resolve
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from sqlalchemy import insert, select, update

from rheo_leads.contracts import ConnectionInput, Strict
from rheo_leads.intake.accept import attribution
from rheo_leads.pipeline_common import lock
from rheo_leads.references import identifier, ref
from rheo_leads.storage import tables as t


class CreateWebhook(Strict):
    name: str = Field(min_length=1, max_length=200)
    funnel_ref: str
    campaign_ref: str | None = None


class RotateSecret(ConnectionInput):
    overlap_seconds: int = Field(default=0, ge=0)


class ConnectionOutput(Strict):
    connection_ref: str
    state: str
    signing_key_generation: int


def settings(workspace_id: UUID, uow: UnitOfWork) -> ResolvedSettings:
    return resolve(
        workspace_id=workspace_id,
        source=TransactionBoundOverrideSource(uow, workspace_id=workspace_id),
    )


def read_connection(
    workspace_id: UUID, uow: UnitOfWork, connection_id: UUID
) -> ConnectionState | None:
    row = (
        uow.connection.execute(
            select(t.intake_connection).where(
                t.intake_connection.c.id == connection_id,
                t.intake_connection.c.transport == "webhook",
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    config = settings(workspace_id, uow)
    return ConnectionState(
        active=row["state"] == "active",
        generation=row["signing_key_generation"],
        current_ref=row["signing_secret_ref"],
        previous_ref=row["previous_secret_ref"],
        previous_until=row["previous_valid_until"],
        max_bytes=config.get_int("leads.intake.max_payload_bytes"),
        replay_seconds=config.get_int("leads.intake.replay_window_seconds"),
    )


def record_refusal(workspace_id: UUID, uow: UnitOfWork, connection_id: UUID) -> None:
    now = datetime.now(UTC)
    interval = settings(workspace_id, uow).get_int(
        "leads.intake.health_write_interval_seconds"
    )
    uow.connection.execute(
        update(t.connection_health)
        .where(
            t.connection_health.c.connection_id == connection_id,
            (t.connection_health.c.last_unauthenticated_write_at.is_(None))
            | (
                t.connection_health.c.last_unauthenticated_write_at
                < now - timedelta(seconds=interval)
            ),
        )
        .values(
            unresolved_failures=t.connection_health.c.unresolved_failures + 1,
            last_error="authentication_refused",
            last_error_at=now,
            last_unauthenticated_write_at=now,
        )
    )


def create(
    ctx: WorkspaceContext, uow: UnitOfWork, m: CreateWebhook
) -> ConnectionOutput:
    lock(uow)
    name = m.name.strip()
    if not name:
        raise OperationRefused("input_invalid", "name is required")
    funnel = identifier(m.funnel_ref, "funnel")
    campaign = identifier(m.campaign_ref, "campaign") if m.campaign_ref else None
    attribution(uow, funnel, campaign)
    mapping = (
        uow.connection.execute(
            select(t.intake_connection).where(
                t.intake_connection.c.transport == "manual",
            )
        )
        .mappings()
        .one()
    )
    connection_id = uuid7()
    credential_ref = create_key(ctx, connection_id, "leads")
    uow.connection.execute(
        insert(t.intake_connection).values(
            id=connection_id,
            name=name,
            transport="webhook",
            source_namespace=f"urn:rheo:connection:{connection_id}",
            mapping_id=mapping["mapping_id"],
            mapping_version=mapping["mapping_version"],
            funnel_id=funnel,
            campaign_id=campaign,
            priority=100,
            subject_authenticated=False,
            email_verified=False,
            signing_secret_ref=credential_ref,
            signing_key_generation=1,
            state="active",
            created_at=datetime.now(UTC),
        )
    )
    uow.connection.execute(
        insert(t.connection_health).values(connection_id=connection_id)
    )
    return ConnectionOutput(
        connection_ref=ref("intake_connection", connection_id),
        state="active",
        signing_key_generation=1,
    )


def _change(
    ctx: WorkspaceContext, uow: UnitOfWork, m: ConnectionInput, action: str
) -> ConnectionOutput:
    lock(uow)
    connection_id = identifier(m.connection_ref, "intake_connection")
    row = (
        uow.connection.execute(
            select(t.intake_connection)
            .where(
                t.intake_connection.c.id == connection_id,
                t.intake_connection.c.transport == "webhook",
            )
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise OperationRefused("not_found", "website connection unavailable")
    values: dict[str, object] = {
        "previous_secret_ref": None,
        "previous_valid_until": None,
    }
    generation = row["signing_key_generation"]
    state = row["state"]
    if action == "rotate":
        if state == "revoked":
            raise OperationRefused("connection_revoked", "create a new connection")
        assert isinstance(m, RotateSecret)
        maximum = settings(ctx.workspace_id, uow).get_int(
            "leads.intake.max_rotation_overlap_seconds"
        )
        if m.overlap_seconds > maximum:
            raise OperationRefused(
                "input_invalid", "overlap exceeds configured maximum"
            )
        generation += 1
        values.update(
            signing_secret_ref=create_key(ctx, connection_id, "leads"),
            signing_key_generation=generation,
            state="active",
        )
        if m.overlap_seconds and state == "active":
            values.update(
                previous_secret_ref=row["signing_secret_ref"],
                previous_valid_until=datetime.now(UTC)
                + timedelta(seconds=m.overlap_seconds),
            )
        state = "active"
    elif action == "revoke":
        state = "revoked"
        values.update(
            state=state, revoked_at=datetime.now(UTC), signing_secret_ref=None
        )
    uow.connection.execute(
        update(t.intake_connection)
        .where(t.intake_connection.c.id == connection_id)
        .values(**values)
    )
    return ConnectionOutput(
        connection_ref=m.connection_ref, state=state, signing_key_generation=generation
    )


def rotate(ctx: WorkspaceContext, uow: UnitOfWork, m: RotateSecret) -> ConnectionOutput:
    return _change(ctx, uow, m, "rotate")


def revoke_previous(
    ctx: WorkspaceContext, uow: UnitOfWork, m: ConnectionInput
) -> ConnectionOutput:
    return _change(ctx, uow, m, "previous")


def revoke(
    ctx: WorkspaceContext, uow: UnitOfWork, m: ConnectionInput
) -> ConnectionOutput:
    return _change(ctx, uow, m, "revoke")


OPERATIONS: tuple[tuple[OperationDeclaration, Handler], ...] = tuple(
    (
        OperationDeclaration(
            name=f"leads.connection.{name}",
            safety_class=SafetyClass.MUTATE,
            roles=frozenset({Role.OWNER}),
            input_model=model,
            output=ConnectionOutput,
            idempotency=Idempotency.NONE,
            audit=AuditSpec(
                subject_field=None if name == "create_webhook" else "connection_ref"
            ),
        ),
        handler,
    )
    for name, model, handler in (
        ("create_webhook", CreateWebhook, create),
        ("rotate_secret", RotateSecret, rotate),
        ("revoke_secret", ConnectionInput, revoke_previous),
        ("revoke", ConnectionInput, revoke),
    )
)


TOOLS = tuple(
    ToolDeclaration(
        name="leads_" + declaration.name.removeprefix("leads.").replace(".", "_"),
        operation=declaration.name,
        safety_class=SafetyClass.MUTATE,
        input_model=declaration.input_model,
        description=(
            "Owner administration of a signed website connection. "
            "Creation and rotation are not idempotent; inspect connections "
            "before retrying "
            "an uncertain result. Keys are handed to the website server "
            "by the host operator "
            "using rheo connector export-key; never place them in browser code. "
            "Zero-overlap rotation or revocation refuses previously queued deliveries. "
            + declaration.name
        ),
    )
    for declaration, _ in OPERATIONS
)
