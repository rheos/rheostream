"""Replay-safe write of an already reviewed history extract into one workspace.

The caller supplies a routed workspace connection and transaction; this module
neither opens a database nor commits. A changed source row refuses, and an erased
row is never resurrected. The importer does not decide whether private content has
passed review, or whether this is the final migration window.
"""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Final

from rheo_core.refs import uuid7
from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from rheo_recallatron.migration.history_extract import (
    HistoryExtract,
    HistoryExtractUnit,
)
from rheo_recallatron.storage.tables import history_record

HISTORY_IMPORT_REFUSED: Final = "history_import_refused"
SOURCE_NAMESPACE: Final = "predecessor"


class HistoryImportRefusal(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(f"{HISTORY_IMPORT_REFUSED}: {detail}")


@dataclass(frozen=True)
class HistoryImportReport:
    source_units: int
    inserted: int
    identical: int
    erased: int


def opaque_history_source_key(source_key: str) -> str:
    """Stable anti-resurrection identity; never keep raw source IDs in tombstones."""
    return "sha256:" + sha256(source_key.encode("utf-8")).hexdigest()


def _values(unit: HistoryExtractUnit) -> dict[str, object]:
    return {
        "source_namespace": SOURCE_NAMESPACE,
        "external_source_key": opaque_history_source_key(unit.external_source_key),
        "source_reference": unit.source_reference,
        "kind": unit.kind,
        "status": unit.status,
        "title": unit.title,
        "body": unit.body,
        "occurred_at": unit.occurred_at,
        "session_key": unit.session_key,
        "chat_key": unit.chat_key,
        "source_role": unit.source_role,
        "source_category": unit.source_category,
        "source_created_at": unit.source_created_at,
        "confirmed_at": unit.confirmed_at,
        "superseded_by_source_key": unit.superseded_by_source_key,
    }


def import_history(
    conn: Connection, extract: HistoryExtract, *, imported_at: datetime
) -> HistoryImportReport:
    if imported_at.tzinfo is None:
        raise ValueError("imported_at must be timezone-aware")
    if extract.header.unresolved_count:
        raise HistoryImportRefusal("extract contains unresolved source rows")
    inserted = identical = erased = 0
    for unit in extract.units:
        values = _values(unit)
        new_id = uuid7()
        statement = (
            pg_insert(history_record)
            .values(id=new_id, imported_at=imported_at, erased_at=None, **values)
            .on_conflict_do_nothing(
                index_elements=(
                    history_record.c.source_namespace,
                    history_record.c.external_source_key,
                )
            )
            .returning(history_record.c.id)
        )
        created = conn.execute(statement).scalar_one_or_none()
        if created is not None:
            inserted += 1
            continue
        existing = conn.execute(
            select(history_record).where(
                history_record.c.source_namespace == SOURCE_NAMESPACE,
                history_record.c.external_source_key
                == opaque_history_source_key(unit.external_source_key),
            )
        ).one()
        if existing.erased_at is not None:
            erased += 1
            continue
        if any(getattr(existing, name) != value for name, value in values.items()):
            # No private field or value is included in the refusal.
            raise HistoryImportRefusal("source key already has different content")
        identical += 1
    return HistoryImportReport(
        source_units=len(extract.units),
        inserted=inserted,
        identical=identical,
        erased=erased,
    )
