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

# Decided together from one observation, by recency rather than completeness: a
# refresh sighting is thin by design, yet its count is the newest known one.
COMPETITION_GROUP = (
    "ext.jobsearch.proposal_count",
    "ext.jobsearch.proposal_count_exact",
    "ext.jobsearch.proposal_observed_at",
)


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
    User-owned fields are never replayed. The competition fields are the one
    exception to the completeness rule; see ``_derive_competition``.
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
            try:
                validate_value(specs[target], r["value_text"])
            except OperationRefused:
                # Preserve invalid source evidence for inspection, but do not let
                # it poison later reconciliation or approved source erasure.
                continue
        candidates.setdefault(target, []).append(r)
    _derive_competition(uow, identifier, current, candidates)
    for target in (set(current) | set(candidates)) - set(COMPETITION_GROUP):
        if _replayed_elsewhere(current.get(target)):
            continue
        values = candidates.get(target, [])
        cutoff = _clear_cutoff(values)
        if cutoff is not None:
            values = [v for v in values if v["source_occurred_at"] >= cutoff]
        _replace(uow, identifier, target, None)
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
        _replace(uow, identifier, target, winner)
    title = uow.connection.execute(
        select(p.opportunity_field_state.c.value_text).where(
            p.opportunity_field_state.c.opportunity_id == identifier,
            p.opportunity_field_state.c.target == "subject",
        )
    ).scalar_one_or_none()
    return {"title": title or "Untitled inquiry"}


def _replayed_elsewhere(old: dict[str, Any] | None) -> bool:
    """User-owned and orphaned field states are never replayed from evidence."""
    return bool(old and (old["owner"] == "user" or old["orphaned"]))


def _clear_cutoff(values: list[dict[str, Any]]) -> Any:
    """Explicit clears invalidate older evidence even when the correction is thin."""
    clears = [v["source_occurred_at"] for v in values if v["value_kind"] == "cleared"]
    return max(clears, default=None)


def _replace(
    uow: UnitOfWork, identifier: UUID, target: str, fact: dict[str, Any] | None
) -> None:
    uow.connection.execute(
        delete(p.opportunity_field_state).where(
            p.opportunity_field_state.c.opportunity_id == identifier,
            p.opportunity_field_state.c.target == target,
        )
    )
    if fact is None:
        return
    uow.connection.execute(
        insert(p.opportunity_field_state).values(
            opportunity_id=identifier,
            target=target,
            value_kind=fact["value_kind"],
            value_text=fact["value_text"],
            owner="source",
            observation_id=fact["observation_id"],
            occurred_at=fact["source_occurred_at"],
            completeness=fact["completeness"],
            priority=fact["priority"],
            orphaned=False,
        )
    )


def _derive_competition(
    uow: UnitOfWork,
    identifier: UUID,
    current: dict[str, dict[str, Any]],
    candidates: dict[str, list[dict[str, Any]]],
) -> None:
    """Write the competition fields from the one observation with the newest count.

    Only an observation carrying a count value is a candidate, so a sighting without
    one can neither clear a known count nor move its observed-at. Exactness breaks
    equal-time ties only, then the ordinary authority, completeness and stable event
    keys. The exactness and observed-at facts come from the winner itself.
    """
    count, exact, _observed_at = COMPETITION_GROUP
    own: dict[str, dict[Any, dict[str, Any]]] = {
        target: {v["observation_id"]: v for v in candidates.get(target, [])}
        for target in COMPETITION_GROUP
    }
    cutoffs = {
        target: _clear_cutoff(candidates.get(target, []))
        for target in COMPETITION_GROUP
    }

    def current_enough(fact: dict[str, Any], target: str) -> bool:
        cutoff = cutoffs[target]
        return cutoff is None or fact["source_occurred_at"] >= cutoff

    def rank(fact: dict[str, Any]) -> tuple[Any, ...]:
        flag = own[exact].get(fact["observation_id"])
        return (
            fact["source_occurred_at"],
            flag is not None
            and flag["value_kind"] == "value"
            and flag["value_text"] == "true",
            fact["priority"],
            fact["completeness"],
            fact["source_namespace"],
            fact["source_event_id"],
        )

    winner = max(
        (
            v
            for v in candidates.get(count, [])
            if v["value_kind"] == "value" and current_enough(v, count)
        ),
        key=rank,
        default=None,
    )
    for target in COMPETITION_GROUP:
        if _replayed_elsewhere(current.get(target)):
            continue
        fact = None if winner is None else own[target].get(winner["observation_id"])
        if fact is not None and not current_enough(fact, target):
            fact = None
        _replace(uow, identifier, target, fact)


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
