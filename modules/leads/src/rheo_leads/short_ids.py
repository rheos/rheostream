"""Short IDs for opportunities (#345): the UUID's random tail, lengthened on a clash."""

from collections.abc import Iterable, Sequence
from typing import Any
from uuid import UUID

from rheo_core.refs.resolver import UnitOfWork
from rheo_core.refs.short import short_ids
from sqlalchemy import String, cast, func, select

from rheo_leads.storage import pipeline as p


def opportunity_tails(uow: UnitOfWork, length: int, tails: Sequence[str]) -> list[UUID]:
    """Opportunities whose last ``length`` hex characters are one of ``tails``.

    A scan, not an index: a workspace holds hundreds of opportunities, and an
    expression index would be a module migration (#345).
    """
    o = p.opportunity
    tail = func.right(func.replace(cast(o.c.id, String), "-", ""), length)
    return list(
        uow.connection.execute(select(o.c.id).where(tail.in_(list(tails)))).scalars()
    )


def opportunity_short_ids(uow: UnitOfWork, ids: Iterable[UUID]) -> dict[UUID, str]:
    return short_ids(uow, opportunity_tails, ids)


def with_short_ids(
    uow: UnitOfWork, items: Sequence[dict[str, Any]], key: str = "id"
) -> None:
    """Set ``short_id`` on each item from its opportunity id under ``key``."""
    ids = [UUID(str(i[key])) for i in items if i.get(key) is not None]
    shorts = opportunity_short_ids(uow, ids)
    for item in items:
        if item.get(key) is not None:
            item["short_id"] = shorts[UUID(str(item[key]))]
