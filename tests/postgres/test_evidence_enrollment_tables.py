"""Revision ``0011_local_evidence``: ``core.evidence_enrollment`` and the widened
``core.evidence_unit.producer_kind`` CHECK.

Seams: the ``producer_kind`` CHECK at the database level, proven by comparing the
live database's constraint text with the one the never-applied sibling
``evidence_unit_live`` renders (both canonicalised by Postgres itself), and by admitted
and refused inserts; each enrollment CHECK and the partial unique index, each proven by
a refused insert; and the downgrade path back to 0010's exact CHECK and up again.
"""

import hashlib
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
from rheo_core.storage.evidence_enrollment_tables import (
    ENROLLMENT_PURPOSES_0011,
    enrollment_metadata,
    evidence_enrollment,
    evidence_unit_live,
)
from rheo_core.storage.evidence_tables import evidence_unit
from sqlalchemy import CheckConstraint, Connection, Engine, Table, insert, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.postgres

HEAD = "0011_local_evidence"
PREVIOUS = "0010_evidence_unit"
PRODUCER_KIND_CHECK = "evidence_unit_producer_kind"


def _engine(cluster: ClusterSession, workspace_id: UUID) -> tuple[str, Engine]:
    row = cluster.registry_row(workspace_id)
    return row.database_name, cluster.backend.pools.engine_for(row.database_name)


def _fingerprint() -> str:
    return hashlib.sha256(uuid7().bytes).hexdigest()


def _unit(**overrides: Any) -> dict[str, Any]:
    """A valid pending evidence unit, synthetic throughout."""
    now = datetime.now(tz=UTC)
    authority_id = uuid7()
    values: dict[str, Any] = {
        "id": uuid7(),
        "producer_kind": "rheo_runtime",
        "authority_id": authority_id,
        "native_key": f"turn:{authority_id}",
        "speaker_account_id": uuid7(),
        "audience_kind": "workspace",
        "audience_id": None,
        "purpose": "internal_analysis",
        "source_recorded_at": now,
        "source_expires_at": now + timedelta(hours=24),
        "body": "A synthetic turn: write to front-desk@example.com.",
        "state": "pending",
        "outcome": None,
        "created_at": now,
        "settled_at": None,
    }
    values.update(overrides)
    return values


def _enrollment(**overrides: Any) -> dict[str, Any]:
    """A valid active enrollment; each probe overrides the columns it attacks."""
    values: dict[str, Any] = {
        "id": uuid7(),
        "account_id": uuid7(),
        "token_id": uuid7(),
        "machine_fingerprint": _fingerprint(),
        "project_fingerprint": _fingerprint(),
        "purpose": "internal_analysis",
        "state": "active",
        "created_at": datetime.now(tz=UTC),
        "revoked_at": None,
    }
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


def _admitted(engine: Engine, table: Table, values: dict[str, Any]) -> None:
    with _probe(engine) as connection:
        connection.execute(insert(table).values(**values))


def _refused(
    engine: Engine, table: Table, constraint: str, values: dict[str, Any]
) -> None:
    with _probe(engine) as connection:
        with pytest.raises(IntegrityError) as excinfo:
            connection.execute(insert(table).values(**values))
    orig = excinfo.value.orig
    assert isinstance(orig, psycopg.errors.CheckViolation), orig
    assert orig.diag.constraint_name == constraint


def _database_checks(connection: Connection) -> dict[str, str]:
    """Every CHECK on ``core.evidence_unit`` as the database holds it."""
    rows = connection.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'core.evidence_unit'::regclass AND contype = 'c'"
        )
    ).all()
    return {str(name): str(definition) for name, definition in rows}


def _canonical(connection: Connection, clause: str) -> str:
    """``clause`` as Postgres itself renders it, via a throwaway temp table.

    The rendered SQLAlchemy text (``producer_kind IN (...)``) and ``pg_catalog``'s
    (``producer_kind = ANY (ARRAY[...])``) differ in spelling, so both sides of a
    comparison go through Postgres. The caller's probe rolls the table back.
    """
    connection.execute(
        text(
            "CREATE TEMP TABLE check_probe (LIKE core.evidence_unit, "
            f"CONSTRAINT check_probe_clause CHECK ({clause}))"
        )
    )
    return str(
        connection.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'pg_temp.check_probe'::regclass "
                "AND conname = 'check_probe_clause'"
            )
        ).scalar_one()
    )


def _clause(table: Table, name: str) -> str:
    for constraint in table.constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == name:
            return str(constraint.sqltext)
    raise AssertionError(f"{table.name} has no CHECK named {name}")


def _canonical_of(engine: Engine, table: Table, name: str) -> str:
    with _probe(engine) as connection:
        return _canonical(connection, _clause(table, name))


def _live_producer_check(engine: Engine) -> str:
    with engine.connect() as connection:
        return _database_checks(connection)[PRODUCER_KIND_CHECK]


# --- the sibling metadata -------------------------------------------------------------


def test_enrollment_metadata_holds_only_the_enrollment_table() -> None:
    assert set(enrollment_metadata.tables) == {"core.evidence_enrollment"}


def test_the_purpose_literal_is_the_context_purposes() -> None:
    assert ENROLLMENT_PURPOSES_0011 == tuple(p.value for p in ContextPurpose), (
        "ContextPurpose and ENROLLMENT_PURPOSES_0011 diverge: a new ContextPurpose "
        "needs its own core migration widening evidence_enrollment_purpose (0011 is "
        "frozen)"
    )


# --- the producer_kind CHECK at the database level ------------------------------------


def test_the_live_checks_equal_the_sibling_evidence_unit_live(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = _engine(cluster, workspace)
    with engine.connect() as connection:
        assert recorded_revisions(connection, CORE_CHAIN) == {HEAD}
        applied = _database_checks(connection)
    sibling = {
        str(constraint.name)
        for constraint in evidence_unit_live.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert set(applied) == sibling
    for name in sibling:
        assert _canonical_of(engine, evidence_unit_live, name) == applied[name], name
    # The one CHECK 0011 moves, and the frozen 0010 object still says otherwise.
    assert "'claude_code_local'" in applied[PRODUCER_KIND_CHECK]
    assert (
        _canonical_of(engine, evidence_unit, PRODUCER_KIND_CHECK)
        != (applied[PRODUCER_KIND_CHECK])
    )


def test_claude_code_local_is_admitted_and_an_unknown_kind_refused(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = _engine(cluster, workspace)
    _admitted(engine, evidence_unit, _unit(producer_kind="rheo_runtime"))
    _admitted(engine, evidence_unit, _unit(producer_kind="claude_code_local"))
    _refused(
        engine, evidence_unit, PRODUCER_KIND_CHECK, _unit(producer_kind="not_a_kind")
    )


# --- the enrollment table's CHECKs and index ------------------------------------------


def test_a_valid_enrollment_is_admitted(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = _engine(cluster, workspace)
    for purpose in ContextPurpose:
        _admitted(engine, evidence_enrollment, _enrollment(purpose=purpose.value))
    _admitted(
        engine,
        evidence_enrollment,
        _enrollment(state="revoked", revoked_at=datetime.now(tz=UTC)),
    )


@pytest.mark.parametrize("column", ["machine_fingerprint", "project_fingerprint"])
@pytest.mark.parametrize(
    "bad",
    [
        "0" * 63,
        "0" * 65,
        "A" * 64,
        "g" * 64,
        "/home/example/project",
    ],
)
def test_fingerprints_are_lowercase_sha256_hex(
    cluster: ClusterSession, workspace: UUID, column: str, bad: str
) -> None:
    _, engine = _engine(cluster, workspace)
    _refused(
        engine,
        evidence_enrollment,
        f"evidence_enrollment_{column}",
        _enrollment(**{column: bad}),
    )


def test_purpose_refuses_a_value_outside_the_context_purposes(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = _engine(cluster, workspace)
    _refused(
        engine,
        evidence_enrollment,
        "evidence_enrollment_purpose",
        _enrollment(purpose="marketing"),
    )


def test_state_is_bounded_and_paired_with_revoked_at(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = _engine(cluster, workspace)
    now = datetime.now(tz=UTC)
    _refused(
        engine,
        evidence_enrollment,
        "evidence_enrollment_state",
        _enrollment(state="paused", revoked_at=now),
    )
    _refused(
        engine,
        evidence_enrollment,
        "evidence_enrollment_revoked_pairing",
        _enrollment(state="active", revoked_at=now),
    )
    _refused(
        engine,
        evidence_enrollment,
        "evidence_enrollment_revoked_pairing",
        _enrollment(state="revoked", revoked_at=None),
    )


def test_one_active_enrollment_per_machine_and_project(
    cluster: ClusterSession, workspace: UUID
) -> None:
    _, engine = _engine(cluster, workspace)
    first = _enrollment()
    pair = {
        "machine_fingerprint": first["machine_fingerprint"],
        "project_fingerprint": first["project_fingerprint"],
    }
    with _probe(engine) as connection:
        connection.execute(insert(evidence_enrollment).values(**first))
        savepoint = connection.begin_nested()
        with pytest.raises(IntegrityError) as excinfo:
            connection.execute(
                insert(evidence_enrollment).values(**_enrollment(**pair))
            )
        savepoint.rollback()
        orig = excinfo.value.orig
        assert isinstance(orig, psycopg.errors.UniqueViolation), orig
        assert orig.diag.constraint_name == "evidence_enrollment_active_pair"

        # Revoke the first: the same pair may enroll again.
        connection.execute(
            evidence_enrollment.update()
            .where(evidence_enrollment.c.id == first["id"])
            .values(state="revoked", revoked_at=datetime.now(tz=UTC))
        )
        connection.execute(insert(evidence_enrollment).values(**_enrollment(**pair)))


# --- the downgrade path ---------------------------------------------------------------


def test_downgrade_restores_0010s_check_and_upgrade_restores_0011(
    cluster: ClusterSession, workspace: UUID
) -> None:
    database_name, engine = _engine(cluster, workspace)
    local = _unit(producer_kind="claude_code_local")
    runtime = _unit(producer_kind="rheo_runtime")
    with engine.begin() as connection:
        connection.execute(insert(evidence_unit).values(**local))
        connection.execute(insert(evidence_unit).values(**runtime))
        connection.execute(insert(evidence_enrollment).values(**_enrollment()))
    widened = _live_producer_check(engine)

    with engine.begin() as connection:
        command.downgrade(build_config(CORE_CHAIN, connection), PREVIOUS)
    try:
        with engine.connect() as connection:
            assert recorded_revisions(connection, CORE_CHAIN) == {PREVIOUS}
            assert (
                connection.execute(
                    text("SELECT to_regclass('core.evidence_enrollment')")
                ).scalar_one()
                is None
            )
            kept = set(
                connection.execute(
                    text("SELECT id FROM core.evidence_unit WHERE id IN (:a, :b)"),
                    {"a": local["id"], "b": runtime["id"]},
                ).scalars()
            )
            assert kept == {runtime["id"]}
        # Exactly the CHECK 0010 creates from its frozen tuple.
        assert _live_producer_check(engine) == _canonical_of(
            engine, evidence_unit, PRODUCER_KIND_CHECK
        )
        _refused(
            engine,
            evidence_unit,
            PRODUCER_KIND_CHECK,
            _unit(producer_kind="claude_code_local"),
        )
        _admitted(engine, evidence_unit, _unit(producer_kind="rheo_runtime"))
    finally:
        # Back up through the orchestrator, the only way the chain runs in production;
        # the test ends at head whatever it asserted.
        with engine.begin() as connection:
            run_chain(connection, CORE_CHAIN, expected_database=database_name)

    with engine.connect() as connection:
        assert recorded_revisions(connection, CORE_CHAIN) == {HEAD}
        assert (
            connection.execute(
                text("SELECT count(*) FROM core.evidence_enrollment")
            ).scalar_one()
            == 0
        )
    assert _live_producer_check(engine) == widened
    _admitted(engine, evidence_unit, _unit(producer_kind="claude_code_local"))
    with engine.begin() as connection:
        connection.execute(
            evidence_unit.delete().where(evidence_unit.c.id == runtime["id"])
        )
