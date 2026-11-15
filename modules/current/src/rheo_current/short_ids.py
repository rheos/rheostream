"""Short IDs for items and projects (#345, #587): the UUID's random tail.

A link or a person can name an item by ``b50a1e99`` (or any 8-32 hex tail, such as
the UUID's last dash group) instead of the full ``current.item:<uuid>``. Display
lengthens a tail two records of one type share, like a git short hash.
"""

from collections.abc import Iterable, Sequence
from uuid import UUID

from rheo_core.refs.short import TailLookup, short_ids
from rheo_core.storage.backend import UnitOfWork
from sqlalchemy import String, Table, cast, func, select

from rheo_current.storage import tables as t


def _tails(table: Table) -> TailLookup:
    def lookup(uow: UnitOfWork, length: int, tails: Sequence[str]) -> list[UUID]:
        # A scan, not an index: a workspace holds thousands of items at most, and an
        # expression index would be a module migration.
        tail = func.right(func.replace(cast(table.c.id, String), "-", ""), length)
        return list(
            uow.connection.execute(
                select(table.c.id).where(tail.in_(list(tails)))
            ).scalars()
        )

    return lookup


item_tails = _tails(t.item)
project_tails = _tails(t.project)


def item_short_ids(uow: UnitOfWork, ids: Iterable[UUID]) -> dict[UUID, str]:
    return short_ids(uow, item_tails, ids)
