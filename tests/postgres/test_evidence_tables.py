"""``core.evidence_unit`` as revision ``0010_evidence_unit`` applies it.

Seams: every CHECK constraint on the table (``purpose``, ``state``, the
``audience_kind``/``audience_id`` pairing, the ``body``/``state`` pairing and the
``outcome``/``state`` pairing), each proven by a refused insert rather than by reading
the DDL; the 0010 indexes and the 0012 retention index; and the revision's downgrade
path, which must leave nothing of the table behind in ``pg_catalog`` and upgrade back
cleanly. Also AC 10's first half: the revision adds a sibling table, not a new
``runtime_transcript.kind``.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg.errors
import pytest
from alembic import command
from conftest import ClusterSession
from rheo_contracts import ContextPurpose
from rheo_core.migrations.orchestrator import (
    CORE_CHAIN,
    build_config,
    recorded_revisions,
    run_chain,
)
from rheo_core.refs import uuid7
from rheo_core.storage import runtime_tables
from rheo_core.storage.evidence_tables import (
    EVIDENCE_PURPOSES,
    EVIDENCE_STATES,
    evidence_unit,
)
from sqlalchemy import Connection, Engine, insert, inspect, select, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.postgres

HEAD = "0012_evidence_retention_index"
PREVIOUS = "0009_runtime_session_policy"

CHECK_CONSTRAINTS = {
    "evidence_unit_producer_kind",
    "evidence_unit_native_key_length",
    "evidence_unit_audience_kind",
    "evidence_unit_audience_pairing",
    "evidence_unit_purpose",
    "evidence_unit_state",
    "evidence_unit_body_pairing",
    "evidence_unit_outcome_pairing",
    "evidence_unit_settled_pairing",
}
INDEXES = {
    "evidence_unit_pkey",
    "evidence_unit_native_key",
    "evidence_unit_pending_claim",
    "evidence_unit_settled_retention",
}


def _engine(cluster: ClusterSession, workspace_id: UUID) -> tuple[str, Engine]:
    row = cluster.registry_row(workspace_id)
    return row.database_name, cluster.backend.pools.engine_for(row.database_name)


def _row(**overrides: Any) -> dict[str, Any]:
    """A valid pending unit; each probe overrides the columns it attacks.

    A probe that moves ``state`` off ``pending`` gets a ``settled_at`` too, unless it
    names one itself, so each probe attacks only the pairing it means to.
    """
    now = datetime.now(tz=UTC)
    operation_id = uuid7()
    values: dict[str, Any] = {
        "id": uuid7(),
        "producer_kind": "rheo_runtime",
        "authority_id": operation_id,
        "native_key": f"turn:{operation_id}",
        "speaker_account_id": uuid7(),
        "audience_kind": "workspace",
        "audience_id": None,
        "purpose": "respond",
        "source_recorded_at": now,
        "source_expires_at": now + timedelta(hours=24),
        "body": "A synthetic turn: please call the front desk at 555-0142.",
        "retry_after": None,
        "state": "pending",
        "outcome": None,
        "created_at": now,
        "settled_at": None,
    }
    if overrides.get("state", "pending") != "pending" and "settled_at" not in overrides:
        values["settled_at"] = now
    values.update(overrides)
    return values


@contextmanager
def _probe(engine: Engine) -> Iterator[Connection]:
    """A connection whose writes are always rolled back: probed, never persisted."""
    with engine.connect() as connection:
        try:
            yield connection
        finally:
            connection.rollback()


def _admitted(engine: Engine, **overrides: Any) -> None:
    with _probe(engine) as connection:
        connection.execute(insert(evidence_unit).values(**_row(**overrides)))


def _refused(engine: Engine, constraint: str, **overrides: Any) -> None:
    with _probe(engine) as connection:
        with pytest.raises(IntegrityError) as excinfo:
            connection.execute(insert(evidence_unit).values(**_row(**overrides)))
    orig = excinfo.value.orig
    assert isinstance(orig, psycopg.errors.CheckViolation), orig
    assert orig.diag.constraint_name == constraint


def _catalog(connection: Connection) -> dict[str, set[str]]:
    """Every ``pg_catalog`` object in ``core`` that names the evidence table."""
    relations = connection.execute(
        text(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'core' AND c.relname LIKE 'evidence_unit%'"
        )
    ).scalars()
    constraints = connection.execute(
        text(
            "SELECT co.conname FROM pg_constraint co "
            "JOIN pg_namespace n ON n.oid = co.connamespace "
            "WHERE n.nspname = 'core' AND co.conname LIKE 'evidence_unit%'"
        )
    ).scalars()
    types = connection.execute(
        text(
            "SELECT t.typname FROM pg_type t "
            "JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE n.nspname = 'core' AND t.typname LIKE '%evidence_unit%'"
        )
    ).scalars()
    return {
        "relations": {str(name) for name in relations},
        "constraints": {str(name) for name in constraints},
        "types": {str(name) for name in types},
    }


# --- the CHECK constraints, each proven by a refused insert ---------------------------


def test_the_check_constraints_are_the_named_set(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = _engine(cluster, workspace)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'core.evidence_unit'::regclass AND contype = 'c'"
            )
        ).all()
    definitions = {str(name): str(definition) for name, definition in rows}
    assert set(definitions) == CHECK_CONSTRAINTS
    # A literal frozen at 0010, pinned to the enum: every member and nothing else.
    assert set(EVIDENCE_PURPOSES) == {purpose.value for purpose in ContextPurpose}, (
        "ContextPurpose and EVIDENCE_PURPOSES diverge: a new ContextPurpose needs its "
        "own core migration widening evidence_unit_purpose (0010 is frozen)"
    )
    for purpose in ContextPurpose:
        assert f"'{purpose.value}'" in definitions["evidence_unit_purpose"]
    for state in EVIDENCE_STATES:
        assert f"'{state}'" in definitions["evidence_unit_state"]


def test_a_valid_row_is_admitted_and_reads_back(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = _engine(cluster, workspace)
    values = _row()
    with engine.begin() as connection:
        connection.execute(insert(evidence_unit).values(**values))
    with engine.connect() as connection:
        stored = connection.execute(
            select(evidence_unit).where(evidence_unit.c.id == values["id"])
        ).one()
    # The column default, not a value the insert supplied.
    assert stored.extraction_attempts == 0
    assert stored.body == values["body"]
    assert stored.state == "pending"


def test_purpose_admits_the_four_context_purposes_and_refuses_a_fifth(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = _engine(cluster, workspace)
    assert len(ContextPurpose) == 4
    for purpose in ContextPurpose:
        _admitted(engine, purpose=purpose.value)
    _refused(engine, "evidence_unit_purpose", purpose="marketing")


def test_state_admits_pending_settled_gap_and_refuses_a_fourth(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = _engine(cluster, workspace)
    _admitted(engine, state="pending")
    _admitted(engine, state="settled", body=None, outcome="active")
    _admitted(engine, state="gap", body=None, outcome="oversize")
    _refused(engine, "evidence_unit_state", state="archived", body=None, outcome="x")


def test_audience_id_is_null_exactly_for_the_workspace_audience(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = _engine(cluster, workspace)
    _admitted(engine, audience_kind="workspace", audience_id=None)
    _admitted(engine, audience_kind="member", audience_id=uuid7())
    _refused(
        engine,
        "evidence_unit_audience_pairing",
        audience_kind="workspace",
        audience_id=uuid7(),
    )
    _refused(
        engine,
        "evidence_unit_audience_pairing",
        audience_kind="member",
        audience_id=None,
    )
    _refused(
        engine, "evidence_unit_audience_kind", audience_kind="session", audience_id=None
    )


def test_body_is_held_exactly_while_pending(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = _engine(cluster, workspace)
    _refused(engine, "evidence_unit_body_pairing", state="pending", body=None)
    _refused(
        engine,
        "evidence_unit_body_pairing",
        state="settled",
        body="text a settled unit must not keep",
        outcome="active",
    )
    _refused(
        engine,
        "evidence_unit_body_pairing",
        state="gap",
        body="text a gap must not keep",
        outcome="expired_pending",
    )


def test_outcome_is_null_exactly_while_pending(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = _engine(cluster, workspace)
    _refused(engine, "evidence_unit_outcome_pairing", state="pending", outcome="active")
    _refused(
        engine,
        "evidence_unit_outcome_pairing",
        state="settled",
        body=None,
        outcome=None,
    )


def test_settled_at_is_null_exactly_while_pending(
    cluster: ClusterSession, workspace: UUID
) -> None:
    """The sweep deletes on ``settled_at``; a non-pending row without one never goes."""
    _, engine = _engine(cluster, workspace)
    now = datetime.now(tz=UTC)
    _refused(engine, "evidence_unit_settled_pairing", state="pending", settled_at=now)
    _refused(
        engine,
        "evidence_unit_settled_pairing",
        state="settled",
        body=None,
        outcome="active",
        settled_at=None,
    )
    _refused(
        engine,
        "evidence_unit_settled_pairing",
        state="gap",
        body=None,
        outcome="expired_pending",
        settled_at=None,
    )


def test_producer_kind_and_native_key_length_are_bounded(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = _engine(cluster, workspace)
    # 1a4b's producer is admitted once 0011_local_evidence widens the check; an
    # unknown kind is still refused, so the CHECK still bounds the column.
    _admitted(engine, producer_kind="claude_code_local")
    _refused(engine, "evidence_unit_producer_kind", producer_kind="not_a_kind")
    _admitted(engine, native_key="k" * 256)
    _refused(engine, "evidence_unit_native_key_length", native_key="k" * 257)


# --- the named indexes ---------------------------------------------------------------


def test_exactly_the_named_indexes_and_the_unit_identity_is_unique(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = _engine(cluster, workspace)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'core' AND tablename = 'evidence_unit'"
            )
        ).all()
    indexes = {str(name): str(definition) for name, definition in rows}
    assert set(indexes) == INDEXES
    native = indexes["evidence_unit_native_key"]
    assert native.startswith("CREATE UNIQUE INDEX")
    assert "(producer_kind, authority_id, native_key)" in native
    claim = indexes["evidence_unit_pending_claim"]
    assert "UNIQUE" not in claim
    assert "(extraction_attempts, source_recorded_at)" in claim
    assert "WHERE (state = 'pending'::text)" in claim
    retention = indexes["evidence_unit_settled_retention"]
    assert retention.startswith("CREATE INDEX")
    assert "(settled_at)" in retention
    assert "WHERE" in retention
    assert "'settled'::text" in retention
    assert "'gap'::text" in retention

    first = _row()
    duplicate = _row(
        producer_kind=first["producer_kind"],
        authority_id=first["authority_id"],
        native_key=first["native_key"],
    )
    with _probe(engine) as connection:
        connection.execute(insert(evidence_unit).values(**first))
        with pytest.raises(IntegrityError) as excinfo:
            connection.execute(insert(evidence_unit).values(**duplicate))
    orig = excinfo.value.orig
    assert isinstance(orig, psycopg.errors.UniqueViolation), orig
    assert orig.diag.constraint_name == "evidence_unit_native_key"


# --- the downgrade path ---------------------------------------------------------------


def test_migration_downgrade_path_leaves_nothing_behind_and_upgrades_again(
    cluster: ClusterSession, workspace: UUID
) -> None:
    database_name, engine = _engine(cluster, workspace)
    with engine.begin() as connection:
        connection.execute(insert(evidence_unit).values(**_row()))
        present = _catalog(connection)
    assert present["relations"] == {"evidence_unit"} | INDEXES
    assert present["constraints"] >= CHECK_CONSTRAINTS | {"evidence_unit_pkey"}

    with engine.begin() as connection:
        command.downgrade(build_config(CORE_CHAIN, connection), "0011_local_evidence")
    with engine.connect() as connection:
        assert recorded_revisions(connection, CORE_CHAIN) == {"0011_local_evidence"}
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM pg_indexes "
                    "WHERE schemaname = 'core' AND tablename = 'evidence_unit' "
                    "AND indexname = 'evidence_unit_settled_retention'"
                )
            ).scalar_one()
            == 0
        )

    with engine.begin() as connection:
        run_chain(connection, CORE_CHAIN, expected_database=database_name)
    with engine.connect() as connection:
        assert recorded_revisions(connection, CORE_CHAIN) == {HEAD}
        assert _catalog(connection) == present

    with engine.begin() as connection:
        command.downgrade(build_config(CORE_CHAIN, connection), PREVIOUS)
    with engine.connect() as connection:
        assert recorded_revisions(connection, CORE_CHAIN) == {PREVIOUS}
        listed = connection.execute(
            text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'core' AND table_name = 'evidence_unit'"
            )
        ).scalar_one()
        assert listed == 0
        assert _catalog(connection) == {
            "relations": set(),
            "constraints": set(),
            "types": set(),
        }

    # Back up through the orchestrator, the only way the chain runs in production.
    with engine.begin() as connection:
        run_chain(connection, CORE_CHAIN, expected_database=database_name)
    with engine.connect() as connection:
        assert recorded_revisions(connection, CORE_CHAIN) == {HEAD}
        assert _catalog(connection) == present
        # Every named CHECK comes back, the settled_at pairing included.
        assert present["constraints"] >= CHECK_CONSTRAINTS
    _admitted(engine)


# --- AC 10, first half: a sibling table, not a new transcript kind --------------------


def test_the_runtime_transcript_is_unchanged(
    cluster: ClusterSession, workspace: UUID
) -> None:
    assert runtime_tables.TRANSCRIPT_KINDS == (
        "model_output",
        "tool_result_summary",
        "progress",
    )
    expected = [
        "id",
        "request_id",
        "ordinal",
        "kind",
        "body",
        "byte_length",
        "recorded_at",
        "retention_until",
    ]
    assert [column.name for column in runtime_tables.runtime_transcript.columns] == (
        expected
    )
    _, engine = _engine(cluster, workspace)
    columns = inspect(engine).get_columns("runtime_transcript", schema="core")
    assert [column["name"] for column in columns] == expected
    with engine.connect() as connection:
        kind_check = connection.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'runtime_transcript_kind'"
            )
        ).scalar_one()
    for kind in runtime_tables.TRANSCRIPT_KINDS:
        assert f"'{kind}'" in kind_check
    assert "evidence" not in kind_check
