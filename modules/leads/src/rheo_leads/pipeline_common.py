"""Shared lookup, revision and registered-dependency boundaries."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from rheo_contracts import Role, SafetyClass, StaleRecord, WorkspaceContext
from rheo_core.deletion.lifecycle import lock_workspace_lifecycle
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import REGISTRY
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.settings import resolve
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from sqlalchemy import select, update

from rheo_leads import pipeline_contracts as c
from rheo_leads.configuration import JOBSEARCH_KEY
from rheo_leads.events import publish_event
from rheo_leads.references import ref
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t

PRESET_ID = UUID("019bf0b0-0000-7000-8000-000000000001")
JOBSEARCH_PRESET_ID = UUID("019bf0b0-0000-7000-8000-000000000002")
PRESETS = {"inbound_services": PRESET_ID, "job_search": JOBSEARCH_PRESET_ID}
RELATIONSHIPS = "relationships"


def now() -> datetime:
    return datetime.now(UTC)


def lock(uow: UnitOfWork) -> None:
    lock_workspace_lifecycle(uow.connection)


def require(ctx: WorkspaceContext, *, write: bool = False) -> None:
    if "leads" not in ctx.enabled_modules or ctx.role not in {
        Role.OWNER,
        Role.MEMBER,
        Role.SERVICE,
    }:
        raise OperationRefused("not_found", "Leads unavailable")


def row(uow: UnitOfWork, table: Any, identifier: UUID) -> dict[str, Any]:
    value = (
        uow.connection.execute(select(table).where(table.c.id == identifier))
        .mappings()
        .one_or_none()
    )
    if value is None:
        raise OperationRefused("not_found", "record unavailable")
    return dict(value)


def opportunity(
    uow: UnitOfWork, model: c.RefInput, *, writing: bool = False
) -> dict[str, Any]:
    if writing:
        lock(uow)
    result = row(uow, p.opportunity, model.ref.id)
    if isinstance(model, c.RevisionInput) and result["revision"] != model.revision:
        raise StaleRecord("opportunity changed")
    return result


def bump(uow: UnitOfWork, record: dict[str, Any], **changes: Any) -> None:
    revision = record["revision"]
    values = {**changes, "revision": revision + 1, "updated_at": now()}
    found = uow.connection.execute(
        update(p.opportunity)
        .where(p.opportunity.c.id == record["id"], p.opportunity.c.revision == revision)
        .values(**values)
        .returning(p.opportunity.c.id)
    ).scalar_one_or_none()
    if found is None:
        raise StaleRecord("opportunity changed")
    record.update(values)


def output(record: dict[str, Any], kind: str = "opportunity") -> c.RecordOutput:
    return c.RecordOutput(
        ref=ref(kind, record["id"]),
        revision=record.get("revision", 1),
        data={k: (v.hex() if isinstance(v, bytes) else v) for k, v in record.items()},
    )


def actor(ctx: WorkspaceContext, prefix: str = "created") -> dict[str, Any]:
    return {
        f"{prefix}_by_kind": ctx.actor.kind.value,
        f"{prefix}_by_id": ctx.actor.id,
        f"{prefix}_at": now(),
    }


def event(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    kind: str,
    record: dict[str, Any],
    related: str | None = None,
) -> None:
    reference = ref(
        "handoff" if kind.startswith("handoff.") else "opportunity", record["id"]
    )
    publish_event(
        ctx,
        uow,
        event_type="leads." + kind,
        subject=reference,
        data=c.ReferenceEvent(
            ref=reference, related_ref=related, revision=record.get("revision", 1)
        ),
    )


def party_read(
    ctx: WorkspaceContext, uow: UnitOfWork, reference: str, *, aliases: bool = False
) -> Any:
    """Registered READ dependency, preserving caller role and workspace.

    Like Recallatron's contact eligibility check, this internal read does not
    confer a user-facing operation grant or a service mutation capability.
    """
    op = REGISTRY.lookup(f"{RELATIONSHIPS}.party." + ("aliases" if aliases else "get"))
    if (
        op is None
        or op.module_id not in ctx.enabled_modules
        or ctx.role not in op.declaration.roles
        or op.declaration.safety_class is not SafetyClass.READ
    ):
        raise OperationRefused("not_found", "party unavailable")
    return op.handler(
        ctx, uow, op.declaration.input_model.model_validate({"ref": reference})
    )


def observation(uow: UnitOfWork, identifier: UUID) -> dict[str, Any]:
    return row(uow, t.observation, identifier)


def evidence_digest(uow: UnitOfWork, record: dict[str, Any]) -> str:
    """A fingerprint of what an assessment judges: the attached observations, the
    resolved field values, the linked parties and the recorded value. Notes, drafts,
    follow-ups and stage moves change the revision but not this, so they never call
    for a reassessment."""
    attached: list[UUID] = list(
        uow.connection.execute(
            select(p.opportunity_observation.c.observation_id).where(
                p.opportunity_observation.c.opportunity_id == record["id"]
            )
        ).scalars()
    )
    observations = sorted(str(i) for i in attached)
    fields = sorted(
        [r.target, r.value_kind, r.value_text]
        for r in uow.connection.execute(
            select(
                p.opportunity_field_state.c.target,
                p.opportunity_field_state.c.value_kind,
                p.opportunity_field_state.c.value_text,
            ).where(
                p.opportunity_field_state.c.opportunity_id == record["id"],
                p.opportunity_field_state.c.orphaned.is_(False),
            )
        )
    )
    parties = sorted(
        [r.party_ref, r.role]
        for r in uow.connection.execute(
            select(p.opportunity_party.c.party_ref, p.opportunity_party.c.role).where(
                p.opportunity_party.c.opportunity_id == record["id"],
                p.opportunity_party.c.removed_at.is_(None),
            )
        )
    )
    amount = record.get("value_amount")
    value = [
        None if amount is None else format(Decimal(amount).normalize(), "f"),
        record.get("value_currency"),
        record.get("value_basis"),
    ]
    payload = json.dumps(
        {
            "observations": observations,
            "fields": fields,
            "parties": parties,
            "value": value,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def jobsearch_enabled(ctx: WorkspaceContext, uow: UnitOfWork) -> bool:
    """The job-search capability counts only when this workspace turned it on."""
    settings = resolve(
        workspace_id=ctx.workspace_id,
        source=TransactionBoundOverrideSource(uow, workspace_id=ctx.workspace_id),
    )
    return settings.set_by_workspace(JOBSEARCH_KEY) and settings.get_bool(JOBSEARCH_KEY)
