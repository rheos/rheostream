"""AC 4: the seven durable-work tables, their ratified columns, and the two constraints
this run's schema carries rather than leaving each later cohort to re-derive.

Seams: ``rheo_core.storage.work_tables``' DDL as revision ``0002_durable_work`` actually
applies it. Every assertion reads a provisioned workspace database back — through
``information_schema``, through reflection, or through a real insert the database
accepts or refuses. Nothing here compares the module's ``Table`` objects against
themselves, so a column transcribed wrongly from the architecture document fails rather
than agreeing with itself.

Schema only: no row written here outlives its own transaction, and nothing in this file
exercises behaviour, because there is none to exercise yet.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg.errors
import pytest
from conftest import ClusterSession
from rheo_core.refs import uuid7
from rheo_core.storage import work_tables
from sqlalchemy import Engine, Table, insert, inspect, select, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.postgres

TIMESTAMPTZ = "timestamp with time zone"

# Column name -> (Postgres ``data_type``, nullable). Transcribed from
# ``docs/architecture/intake-and-events.md`` rows 293-295 (the outbox trio), 421-422
# (jobs and schedules), 453-464 (operations) and 473-480 (audit), not from the table
# module. Where the document names a column without a type — the operation record's
# progress and timestamps — the shape its own state machine requires is asserted here,
# so a later widening is a visible diff rather than a quiet one.
EXPECTED_COLUMNS: dict[str, dict[str, tuple[str, bool]]] = {
    "outbox_event": {
        "id": ("uuid", False),
        "position": ("bigint", False),
        "type": ("text", False),
        "schema_version": ("integer", False),
        "source": ("text", False),
        "subject_ref": ("text", False),
        "subject_revision": ("integer", False),
        "correlation_id": ("uuid", False),
        "causation_id": ("uuid", True),
        "actor_kind": ("text", False),
        "actor_id": ("uuid", True),
        "occurred_at": (TIMESTAMPTZ, False),
        "data": ("jsonb", False),
    },
    "event_delivery": {
        "event_id": ("uuid", False),
        "consumer_id": ("text", False),
        "subject_ref": ("text", False),
        "position": ("bigint", False),
        "state": ("text", False),
        "attempts": ("integer", False),
        "next_attempt_at": (TIMESTAMPTZ, False),
        "lease_owner": ("text", True),
        "lease_until": (TIMESTAMPTZ, True),
        "last_error": ("text", True),
        "completed_at": (TIMESTAMPTZ, True),
    },
    "consumer_processed": {
        "consumer_id": ("text", False),
        "event_id": ("uuid", False),
        "processed_at": (TIMESTAMPTZ, False),
    },
    "job": {
        "id": ("uuid", False),
        "kind": ("text", False),
        "state": ("text", False),
        "input": ("jsonb", False),
        "operation_id": ("uuid", True),
        "depends_on_ref": ("text", True),
        "attempts": ("integer", False),
        "max_attempts": ("integer", False),
        "next_run_at": (TIMESTAMPTZ, False),
        "lease_owner": ("text", True),
        "lease_until": (TIMESTAMPTZ, True),
        "cancel_requested": ("boolean", False),
        "last_error": ("text", True),
        "created_at": (TIMESTAMPTZ, False),
        "finished_at": (TIMESTAMPTZ, True),
    },
    "schedule": {
        "id": ("uuid", False),
        "module_id": ("text", False),
        "name": ("text", False),
        "job_kind": ("text", False),
        "cron": ("text", False),
        "enabled": ("boolean", False),
        "last_run_at": (TIMESTAMPTZ, True),
        "next_run_at": (TIMESTAMPTZ, False),
    },
    "operation": {
        "id": ("uuid", False),
        "name": ("text", False),
        "safety_class": ("text", False),
        "actor_kind": ("text", False),
        "actor_id": ("uuid", True),
        "entry": ("text", False),
        "audience_kind": ("text", False),
        "audience_id": ("uuid", True),
        "state": ("text", False),
        "approval_id": ("uuid", True),
        "progress_text": ("text", True),
        "progress_fraction": ("double precision", True),
        "result_ref": ("text", True),
        "error_code": ("text", True),
        "error_text": ("text", True),
        "terminal_check_kind": ("text", True),
        "terminal_check_at": (TIMESTAMPTZ, True),
        "provider_request_id": ("text", True),
        "created_at": (TIMESTAMPTZ, False),
        "started_at": (TIMESTAMPTZ, True),
        "terminal_at": (TIMESTAMPTZ, True),
    },
    "audit_record": {
        "id": ("uuid", False),
        "occurred_at": (TIMESTAMPTZ, False),
        "actor_kind": ("text", False),
        "actor_id": ("uuid", True),
        "entry": ("text", False),
        "operation_name": ("text", False),
        "safety_class": ("text", False),
        "operation_id": ("uuid", True),
        "subject_ref": ("text", True),
        "request_digest": ("bytea", False),
        "outcome": ("text", False),
    },
}

EXPECTED_PRIMARY_KEYS: dict[str, list[str]] = {
    "outbox_event": ["id"],
    "event_delivery": ["event_id", "consumer_id"],
    "consumer_processed": ["consumer_id", "event_id"],
    "job": ["id"],
    "schedule": ["id"],
    "operation": ["id"],
    "audit_record": ["id"],
}


def workspace_engine(cluster: ClusterSession, workspace_id: UUID) -> Engine:
    row = cluster.registry_row(workspace_id)
    return cluster.backend.pools.engine_for(row.database_name)


def columns_in(engine: Engine, schema: str, table: str) -> dict[str, tuple[str, bool]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = :table"
            ),
            {"schema": schema, "table": table},
        ).all()
    return {str(name): (str(kind), yes == "YES") for name, kind, yes in rows}


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _operation_row(**overrides: Any) -> dict[str, Any]:
    """A ``pending`` operation record: every not-null column and nothing else."""
    row: dict[str, Any] = {
        "id": uuid7(),
        "name": "core.example.run",
        "safety_class": "read",
        "actor_kind": "account",
        "actor_id": uuid7(),
        "entry": "api",
        "audience_kind": "session",
        "audience_id": uuid7(),
        "state": "pending",
        "created_at": _now(),
    }
    row.update(overrides)
    return row


def _outbox_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid7(),
        "type": "leads.opportunity.created",
        "schema_version": 1,
        "source": "urn:rheo:module:leads",
        "subject_ref": "leads:opportunity:1",
        "subject_revision": 1,
        "correlation_id": uuid7(),
        "actor_kind": "system",
        "occurred_at": _now(),
        "data": {"hello": "there"},
    }
    row.update(overrides)
    return row


def _job_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid7(),
        "kind": "core.exports.sweep",
        "state": "queued",
        "input": {},
        "attempts": 0,
        "max_attempts": 5,
        "next_run_at": _now(),
        "cancel_requested": False,
        "created_at": _now(),
    }
    row.update(overrides)
    return row


def _audit_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid7(),
        "occurred_at": _now(),
        "actor_kind": "account",
        "actor_id": uuid7(),
        "entry": "api",
        "operation_name": "core.example.run",
        "safety_class": "read",
        "request_digest": b"\x00" * 32,
        "outcome": "succeeded",
    }
    row.update(overrides)
    return row


def _refusal(
    engine: Engine, table: Table, values: dict[str, Any]
) -> psycopg.errors.Error:
    """Insert ``values`` expecting the database to refuse, and return what it raised."""
    with engine.connect() as connection:
        with pytest.raises(IntegrityError) as excinfo:
            connection.execute(insert(table).values(**values))
        connection.rollback()
    original = excinfo.value.orig
    assert isinstance(original, psycopg.errors.Error)
    return original


def _accepted(engine: Engine, table: Table, values: dict[str, Any]) -> None:
    """Insert ``values`` expecting the database to accept, then roll it back."""
    with engine.connect() as connection:
        connection.execute(insert(table).values(**values))
        connection.rollback()  # probed, never persisted


# --- the seven tables, as the document ratified them ----------------------------------


def test_the_seven_tables_carry_their_ratified_columns(
    cluster: ClusterSession, workspace: UUID
) -> None:
    engine = workspace_engine(cluster, workspace)
    actual = {table: columns_in(engine, "core", table) for table in EXPECTED_COLUMNS}
    assert actual == EXPECTED_COLUMNS


def test_the_primary_keys_and_the_one_named_index_are_the_only_ones(
    cluster: ClusterSession, workspace: UUID
) -> None:
    """The index the document names exists; no index it does not name was added."""
    inspector = inspect(workspace_engine(cluster, workspace))
    keys = {
        table: inspector.get_pk_constraint(table, schema="core")["constrained_columns"]
        for table in EXPECTED_PRIMARY_KEYS
    }
    assert keys == EXPECTED_PRIMARY_KEYS

    extra = {
        table: [index["name"] for index in inspector.get_indexes(table, schema="core")]
        for table in EXPECTED_COLUMNS
    }
    assert extra == {
        "outbox_event": [],
        "event_delivery": ["event_delivery_order"],
        "consumer_processed": [],
        "job": [],
        "schedule": [],
        "operation": [],
        "audit_record": [],
    }
    ordering = inspector.get_indexes("event_delivery", schema="core")[0]
    assert ordering["column_names"] == ["consumer_id", "subject_ref", "position"]


def test_the_outbox_position_is_assigned_and_increases(
    cluster: ClusterSession, workspace: UUID
) -> None:
    """``position`` is the document's ``bigserial``: the writer never supplies it."""
    engine = workspace_engine(cluster, workspace)
    first, second = _outbox_row(), _outbox_row()
    with engine.connect() as connection:
        connection.execute(insert(work_tables.outbox_event).values(**first))
        connection.execute(insert(work_tables.outbox_event).values(**second))
        positions = connection.execute(
            select(work_tables.outbox_event.c.position).order_by(
                work_tables.outbox_event.c.position
            )
        ).scalars()
        assigned = [int(value) for value in positions]
        connection.rollback()
    assert len(assigned) == 2
    assert assigned[0] < assigned[1]


# --- the two constraints the substrate carries ----------------------------------------


def test_operation_refuses_succeeded_with_either_terminal_check_column_null(
    cluster: ClusterSession, workspace: UUID
) -> None:
    """Criterion 13's schema half, both halves of the ``either`` and both controls."""
    engine = workspace_engine(cluster, workspace)
    checked_at = _now()

    for missing in ("terminal_check_kind", "terminal_check_at"):
        present = {
            "terminal_check_kind": "handler_returned",
            "terminal_check_at": checked_at,
        }
        del present[missing]
        refusal = _refusal(
            engine,
            work_tables.operation,
            _operation_row(state="succeeded", terminal_at=checked_at, **present),
        )
        assert isinstance(refusal, psycopg.errors.CheckViolation), missing
        assert "operation_terminal_check_required" in str(refusal), missing

    # Both present: accepted, so the refusals above are the constraint and not a
    # not-null column or a typo in the row.
    _accepted(
        engine,
        work_tables.operation,
        _operation_row(
            state="succeeded",
            terminal_check_kind="handler_returned",
            terminal_check_at=checked_at,
            terminal_at=checked_at,
        ),
    )
    # And the constraint is scoped to ``succeeded``: a pending record has neither.
    _accepted(engine, work_tables.operation, _operation_row())


def test_consumer_processed_rejects_a_duplicate_consumer_and_event(
    cluster: ClusterSession, workspace: UUID
) -> None:
    """Criterion 11's schema half: the pair is the key, not either column alone."""
    engine = workspace_engine(cluster, workspace)
    event_id, other_event = uuid7(), uuid7()
    row = {"consumer_id": "leads.process_delivery", "event_id": event_id}

    with engine.connect() as connection:
        connection.execute(
            insert(work_tables.consumer_processed).values(**row, processed_at=_now())
        )
        # Same consumer, a different event, and the same event for a different
        # consumer: both are distinct rows, so the key is the pair.
        connection.execute(
            insert(work_tables.consumer_processed).values(
                consumer_id=row["consumer_id"],
                event_id=other_event,
                processed_at=_now(),
            )
        )
        connection.execute(
            insert(work_tables.consumer_processed).values(
                consumer_id="current.ingest", event_id=event_id, processed_at=_now()
            )
        )
        with pytest.raises(IntegrityError) as excinfo:
            connection.execute(
                insert(work_tables.consumer_processed).values(
                    **row, processed_at=_now()
                )
            )
        connection.rollback()
    assert isinstance(excinfo.value.orig, psycopg.errors.UniqueViolation)


# --- the foreign keys, in the direction the document names them -----------------------


def test_only_the_two_operation_id_columns_are_foreign_keys(
    cluster: ClusterSession, workspace: UUID
) -> None:
    """``job`` and ``audit_record`` reference ``operation``; ``approval_id`` does not
    reference anything, because ``approval`` is a later chain step's table."""
    engine = workspace_engine(cluster, workspace)
    unknown = uuid4()

    for table, values in (
        (work_tables.job, _job_row(operation_id=unknown)),
        (work_tables.audit_record, _audit_row(operation_id=unknown)),
    ):
        refusal = _refusal(engine, table, values)
        assert isinstance(refusal, psycopg.errors.ForeignKeyViolation), table.name

    # An id no table holds, accepted: nothing constrains it, which is the point.
    _accepted(engine, work_tables.operation, _operation_row(approval_id=unknown))
