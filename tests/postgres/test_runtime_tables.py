"""The four runtime tables as revision ``0006_runtime`` applies them.

Seams: inserts into ``runtime_request``, ``runtime_request_context``,
``runtime_transcript``, and ``runtime_session`` with the architecture columns
(``native_handle`` non-null on the session). No column is named like a secret,
tool-argument, or raw-context field, and no stored value holds a secret, a
``secret://`` reference, a tool argument, or raw context text.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import ClusterSession
from rheo_core.refs import uuid7
from rheo_core.storage import runtime_tables, work_tables
from sqlalchemy import Engine, Table, insert, select, text

pytestmark = pytest.mark.postgres

FORBIDDEN_NAME_PARTS = (
    "secret",
    "password",
    "tool_arg",
    "arguments",
    "raw_context",
    "context_text",
    "run_token",
    "access_token",
)
FORBIDDEN_VALUE_MARKERS = ("secret://", "rheo_runtime_", "ANTHROPIC_API_KEY")
RAW_CONTEXT = "the raw context the model was shown and must never be stored"
TOOL_ARGUMENTS = '{"path": "/etc/passwd", "command": "rm -rf /"}'


def workspace_engine(cluster: ClusterSession, workspace_id: UUID) -> Engine:
    row = cluster.registry_row(workspace_id)
    return cluster.backend.pools.engine_for(row.database_name)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes | memoryview):
        return bytes(value).hex()
    if isinstance(value, list | tuple):
        return ",".join(_cell_text(item) for item in value)
    return str(value)


def _assert_no_secret_shape(table: Table) -> None:
    for column in table.columns:
        name = column.name.lower()
        for part in FORBIDDEN_NAME_PARTS:
            assert part not in name, f"{table.name}.{column.name}"


def test_runtime_tables_accept_architecture_rows_without_secrets(
    cluster: ClusterSession, workspace: UUID
) -> None:
    engine = workspace_engine(cluster, workspace)
    for table in runtime_tables.RUNTIME_TABLES:
        _assert_no_secret_shape(table)

    request_id = uuid7()
    digest = hashlib.sha256(RAW_CONTEXT.encode()).digest()
    started = _now()
    recorded = started
    request_values = {
        "id": request_id,
        "operation_id": uuid7(),
        "runtime_id": "claude_cli",
        "model_id": "sonnet",
        "purpose": "respond",
        "context_digest": digest,
        "permitted_tools": ["core.note.read"],
        "requirements": ["streaming"],
        "deadline_seconds": 600,
        "max_iterations": 40,
        "max_output_bytes": 1_000_000,
        "terminal_event": None,
        "failure_kind": None,
        "usage_input_tokens": None,
        "usage_output_tokens": None,
        "usage_cost": None,
        "usage_kind": None,
        "started_at": started,
        "ended_at": None,
    }
    context_values = {
        "request_id": request_id,
        "ref": "notes:note:018f0000-0000-7000-8000-000000000009",
    }
    transcript_body = "model output summary"
    transcript_values = {
        "id": uuid7(),
        "request_id": request_id,
        "ordinal": 0,
        "kind": "model_output",
        "body": transcript_body,
        "byte_length": len(transcript_body.encode()),
        "recorded_at": recorded,
        "retention_until": recorded + timedelta(days=90),
    }
    native_handle = "sess_cli_not_a_uuid"
    session_values = {
        "id": uuid7(),
        "runtime_id": "claude_cli",
        "credential_scope": "api_key",
        "actor_kind": "account",
        "actor_id": uuid7(),
        "audience_kind": "session",
        "audience_id": uuid7(),
        "native_handle": native_handle,
        "created_at": started,
        "last_used_at": started,
        "expires_at": started + timedelta(hours=72),
    }

    with engine.begin() as connection:
        connection.execute(insert(runtime_tables.runtime_request).values(**request_values))
        connection.execute(
            insert(runtime_tables.runtime_request_context).values(**context_values)
        )
        connection.execute(
            insert(runtime_tables.runtime_transcript).values(**transcript_values)
        )
        connection.execute(insert(runtime_tables.runtime_session).values(**session_values))

        request_row = connection.execute(
            select(runtime_tables.runtime_request).where(
                runtime_tables.runtime_request.c.id == request_id
            )
        ).one()
        context_row = connection.execute(
            select(runtime_tables.runtime_request_context).where(
                runtime_tables.runtime_request_context.c.request_id == request_id
            )
        ).one()
        transcript_row = connection.execute(
            select(runtime_tables.runtime_transcript).where(
                runtime_tables.runtime_transcript.c.request_id == request_id
            )
        ).one()
        session_row = connection.execute(
            select(runtime_tables.runtime_session).where(
                runtime_tables.runtime_session.c.native_handle == native_handle
            )
        ).one()

    assert session_row.native_handle == native_handle
    assert session_row.native_handle is not None
    assert request_row.context_digest == digest
    assert request_row.permitted_tools == ["core.note.read"]

    for row in (request_row, context_row, transcript_row, session_row):
        mapping = row._mapping
        for value in mapping.values():
            rendered = _cell_text(value)
            assert RAW_CONTEXT not in rendered
            assert TOOL_ARGUMENTS not in rendered
            for marker in FORBIDDEN_VALUE_MARKERS:
                assert marker not in rendered


def test_retention_sweep_schedule_is_inserted_with_the_runtime_revision(
    cluster: ClusterSession, workspace: UUID
) -> None:
    engine = workspace_engine(cluster, workspace)
    with engine.connect() as connection:
        row = connection.execute(
            select(work_tables.schedule).where(
                work_tables.schedule.c.job_kind == "core.retention_sweep"
            )
        ).one()
        count = connection.execute(
            text(
                "SELECT count(*) FROM core.schedule "
                "WHERE job_kind = 'core.retention_sweep'"
            )
        ).scalar_one()
    assert count == 1
    assert (row.module_id, row.name, row.job_kind, row.cron, row.enabled) == (
        "core",
        "retention_sweep",
        "core.retention_sweep",
        "0 3 * * *",
        True,
    )
    assert row.next_run_at > _now()
