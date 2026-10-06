"""Deterministic, per-field reconciliation with preserved source provenance."""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import delete, insert, select

from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t


def fields(uow: UnitOfWork, record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        r["target"]: dict(r)
        for r in uow.connection.execute(
            select(p.preset_field).where(
                p.preset_field.c.preset_id == record["preset_id"],
                p.preset_field.c.version == record["preset_version"],
            )
        ).mappings()
    }


def validate_value(spec: dict[str, Any], value: str | None) -> None:
    if value is None:
        return
    try:
        if len(value) > 16000:
            raise ValueError
        kind = spec["type"]
        if kind == "integer":
            int(value)
        elif kind == "decimal" and not Decimal(value).is_finite():
            raise ValueError
        elif kind == "date":
            date.fromisoformat(value)
        elif kind == "boolean" and value not in {"true", "false"}:
            raise ValueError
        elif kind == "enum" and value not in spec["enum_values"]:
            raise ValueError
    except (ValueError, InvalidOperation):
        raise OperationRefused(
            "input_invalid", "invalid extension field value"
        ) from None


def derive(uow: UnitOfWork, record: dict[str, Any]) -> dict[str, Any]:
    """Recompute from the surviving linked set, never from arrival order.

    Replay occurrence order and replace only with evidence at least as complete
    and at least as authoritative. Stable event keys break equal-time ties.
    Explicit clears invalidate older evidence even when the correction is thin.
    User-owned fields are never replayed.
    """
    identifier = record["id"]
    specs = fields(uow, record)
    current = {
        r["target"]: dict(r)
        for r in uow.connection.execute(
            select(p.opportunity_field_state).where(
                p.opportunity_field_state.c.opportunity_id == identifier
            )
        ).mappings()
    }
    candidates: dict[str, list[dict[str, Any]]] = {}
    q = (
        select(
            t.observation_field,
            t.observation.c.source_occurred_at,
            t.observation.c.completeness,
            t.observation.c.source_event_id,
            t.intake_connection.c.priority,
            t.intake_connection.c.source_namespace,
        )
        .join(t.observation, t.observation.c.id == t.observation_field.c.observation_id)
        .join(
            p.opportunity_observation,
            p.opportunity_observation.c.observation_id == t.observation.c.id,
        )
        .join(
            t.intake_connection,
            t.intake_connection.c.id == t.observation.c.connection_id,
        )
        .where(p.opportunity_observation.c.opportunity_id == identifier)
    )
    for raw in uow.connection.execute(q).mappings():
        r = dict(raw)
        target = r["target"]
        if target.startswith("ext."):
            if target not in specs:
                continue
            validate_value(specs[target], r["value_text"])
        candidates.setdefault(target, []).append(r)
    for target in set(current) | set(candidates):
        old = current.get(target)
        if old and (old["owner"] == "user" or old["orphaned"]):
            continue
        values = candidates.get(target, [])
        clears = [v for v in values if v["value_kind"] == "cleared"]
        if clears:
            cutoff = max(v["source_occurred_at"] for v in clears)
            values = [v for v in values if v["source_occurred_at"] >= cutoff]
        uow.connection.execute(
            delete(p.opportunity_field_state).where(
                p.opportunity_field_state.c.opportunity_id == identifier,
                p.opportunity_field_state.c.target == target,
            )
        )
        if not values:
            continue
        ordered = sorted(
            values,
            key=lambda v: (
                v["source_occurred_at"],
                v["priority"],
                v["completeness"],
                v["source_namespace"],
                v["source_event_id"],
            ),
        )
        winner = ordered[0]
        for candidate in ordered[1:]:
            if (
                candidate["completeness"] >= winner["completeness"]
                and candidate["priority"] >= winner["priority"]
            ):
                winner = candidate
        uow.connection.execute(
            insert(p.opportunity_field_state).values(
                opportunity_id=identifier,
                target=target,
                value_kind=winner["value_kind"],
                value_text=winner["value_text"],
                owner="source",
                observation_id=winner["observation_id"],
                occurred_at=winner["source_occurred_at"],
                completeness=winner["completeness"],
                priority=winner["priority"],
                orphaned=False,
            )
        )
    title = uow.connection.execute(
        select(p.opportunity_field_state.c.value_text).where(
            p.opportunity_field_state.c.opportunity_id == identifier,
            p.opportunity_field_state.c.target == "subject",
        )
    ).scalar_one_or_none()
    return {"title": title or "Untitled inquiry"}


def user_field(
    uow: UnitOfWork, opportunity_id: UUID, target: str, value: str | None
) -> None:
    uow.connection.execute(
        delete(p.opportunity_field_state).where(
            p.opportunity_field_state.c.opportunity_id == opportunity_id,
            p.opportunity_field_state.c.target == target,
        )
    )
    uow.connection.execute(
        insert(p.opportunity_field_state).values(
            opportunity_id=opportunity_id,
            target=target,
            value_kind="cleared" if value is None else "value",
            value_text=value,
            owner="user",
            orphaned=False,
        )
    )
