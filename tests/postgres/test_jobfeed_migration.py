"""Migration 0009: an additive upgrade of a populated workspace, and its constraints.

The upgrade runs through the audited operator module upgrade, the path production
uses, on a workspace rebuilt at the exact 0008 predecessor with webhook, import and
manual connections, receipts and an opportunity in it.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from alembic.script import ScriptDirectory
from rheo_contracts import RecordRef
from rheo_core.migrations.orchestrator import build_config, recorded_revisions
from rheo_core.refs import uuid7
from rheo_core.storage.core_tables import module_schema_version
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads.references import ref
from rheo_leads.storage import jobfeed as j
from rheo_leads.storage import tables as t
from sqlalchemy import Connection, delete, insert, select, text
from sqlalchemy.exc import IntegrityError

from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake
from postgres.test_leads_pipeline import capture, ok, one, rewind_leads, setup
from postgres.test_module_upgrade import upgrade

pytestmark = pytest.mark.postgres
REVISION = "0009_jobfeed_collector"
HEAD = "0017_proposal_posting_questions"
NEW_TABLES = {
    "jobfeed_connection",
    "jobfeed_authorization",
    "jobfeed_posting",
    "jobfeed_account_choice",
}
# Created by 0011_job_scoring, after this file's revision, and empty on upgrade too.
SCORING_TABLES = {"jobsearch_profile", "job_score_state"}
# Created by 0012_job_proposals, and empty on upgrade too.
PROPOSAL_TABLES = {
    "proposal_profile",
    "job_proposal",
    "proposal_section",
    "answer_library",
    # 0013_job_pricing
    "job_pricing",
    "proposal_milestone",
    # 0014_job_promotion
    "job_promotion",
    # 0015_model_calls
    "model_call",
    # 0016_jobfeed_runs
    "jobfeed_run",
}


def fingerprint(connection: Connection, schema: str = "leads") -> dict[str, Any]:
    """Columns, constraints and indexes of a schema, schema name normalized away."""

    def rows(sql: str) -> list[tuple[Any, ...]]:
        return sorted(
            tuple(
                str(v).replace(f"{schema}.", "leads.") if v is not None else None
                for v in row
            )
            for row in connection.execute(text(sql), {"schema": schema})
        )

    return {
        "columns": rows(
            "SELECT table_name, column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = :schema"
        ),
        "constraints": rows(
            "SELECT c.relname, k.conname, k.contype, pg_get_constraintdef(k.oid) "
            "FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = :schema"
        ),
        "indexes": rows(
            "SELECT tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = :schema"
        ),
    }


def table_rows(connection: Connection) -> dict[str, list[str]]:
    """Every row of every Leads table as Postgres renders it, version table aside."""
    names = connection.execute(
        text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'leads' "
            "AND tablename <> 'alembic_version_leads'"
        )
    ).scalars()
    return {
        name: list(
            connection.execute(
                text(f'SELECT r::text FROM leads."{name}" r ORDER BY 1')
            ).scalars()
        )
        for name in names
    }


def connector(i: Intake, transport: str, namespace: str) -> UUID:
    """A connection row copied from the seeded manual one, as other suites do."""
    with open_unit_of_work(i.ctx) as uow:
        row = dict(
            uow.connection.execute(
                select(t.intake_connection).where(
                    t.intake_connection.c.id == i.connection_id
                )
            )
            .mappings()
            .one()
        )
        row.update(
            id=uuid7(),
            transport=transport,
            source_namespace=namespace,
            funnel_id=i.funnel_id,
        )
        uow.connection.execute(insert(t.intake_connection).values(**row))
        uow.commit()
    identifier = row["id"]
    assert isinstance(identifier, UUID)
    return identifier


def populate(i: Intake) -> str:
    setup(i)
    capture(
        i,
        "manual-upgrade",
        {"person.email": "sample@example.com", "subject": "Preserved inquiry"},
    )
    webhook = connector(i, "webhook", "fixture-webhook")
    imported = connector(i, "import", "fixture-import")
    i.service_accept(
        connection_ref=ref("intake_connection", webhook),
        source_event_id="webhook-upgrade",
        body='{"subject":"Webhook example"}',
        signing_key_generation=1,
    )
    i.service_accept(
        connection_ref=ref("intake_connection", imported),
        source_event_id="import-upgrade",
        body='{"subject":"Import example"}',
    )
    i.visit()
    return str(one(i).ref)


def test_populated_0008_upgrades_additively_to_the_fresh_schema(
    intake: Intake,
) -> None:
    revision = ScriptDirectory.from_config(build_config("leads")).get_revision(REVISION)
    assert revision is not None and revision.down_revision == "0008_attach_by_field"
    assert revision.module.non_destructive_upgrade is True

    with open_unit_of_work(intake.ctx) as uow:
        fresh = fingerprint(uow.connection)
    reference = populate(intake)
    with open_unit_of_work(intake.ctx) as uow:
        undone = rewind_leads(uow.connection, "0008_attach_by_field")
        assert undone == (
            HEAD,
            "0016_jobfeed_runs",
            "0015_model_calls",
            "0014_job_promotion",
            "0013_job_pricing",
            "0012_job_proposals",
            "0011_job_scoring",
            "0010_job_paste",
            REVISION,
        )
        uow.connection.execute(
            delete(module_schema_version).where(
                module_schema_version.c.module_id == "leads",
                module_schema_version.c.schema_version.in_(undone),
            )
        )
        before = table_rows(uow.connection)
        transports = set(
            uow.connection.execute(select(t.intake_connection.c.transport)).scalars()
        )
        uow.commit()
    # Positive control: the predecessor really is populated and lacks the new tables.
    assert not NEW_TABLES & set(before)
    assert transports == {"webhook", "import", "manual"}
    assert len(before["intake_connection"]) == len(before["delivery_receipt"]) == 3
    assert len(before["observation"]) == 3
    assert len(before["opportunity"]) == 1
    assert intake.call("leads.opportunity.get", ref=reference).state == (
        "module_unavailable"
    )

    result = upgrade(intake, target_schema=HEAD)
    assert result.ok and result.result.changed, result

    with open_unit_of_work(intake.ctx) as uow:
        after = table_rows(uow.connection)
        upgraded = fingerprint(uow.connection)
        assert recorded_revisions(uow.connection, "leads") == {HEAD}
    assert {name: after[name] for name in before} == before
    assert set(after) - set(before) == NEW_TABLES | SCORING_TABLES | PROPOSAL_TABLES
    assert all(
        after[name] == [] for name in NEW_TABLES | SCORING_TABLES | PROPOSAL_TABLES
    )
    assert upgraded == fresh
    assert one(intake).ref == reference


def test_declared_tables_match_the_migrated_schema(intake: Intake) -> None:
    """The SQLAlchemy declarations build exactly what the migrations built."""
    with open_unit_of_work(intake.ctx) as uow:
        migrated = fingerprint(uow.connection)
        uow.connection.execute(text("CREATE SCHEMA leads_declared"))
        t.metadata.create_all(
            uow.connection.execution_options(
                schema_translate_map={"leads": "leads_declared"}
            )
        )
        declared = fingerprint(uow.connection, "leads_declared")
        # Not committed: leaving the unit of work rolls the scratch schema back.

    def owned(found: dict[str, Any], tables: set[str]) -> dict[str, Any]:
        return {key: [r for r in rows if r[0] in tables] for key, rows in found.items()}

    assert owned(declared, NEW_TABLES) == owned(migrated, NEW_TABLES)
    # Positive control: the comparison saw every new table's columns and checks.
    assert len(owned(migrated, NEW_TABLES)["columns"]) == 69
    # The run history (0016) is compared too, column by column and check by check.
    history = {"jobfeed_run"}
    assert owned(declared, history) == owned(migrated, history)
    assert len(owned(migrated, history)["columns"]) == 14
    transport = [
        row
        for row in migrated["constraints"]
        if row[0] == "intake_connection" and row[1] == "connection_transport"
    ]
    assert transport == [
        row
        for row in declared["constraints"]
        if row[0] == "intake_connection" and row[1] == "connection_transport"
    ]
    assert len(transport) == 1 and "'poll'::text" in transport[0][3]


@contextmanager
def refused(i: Intake, constraint: str) -> Iterator[Connection]:
    with pytest.raises(IntegrityError) as raised, open_unit_of_work(i.ctx) as uow:
        yield uow.connection
    assert raised.value.orig.diag.constraint_name == constraint  # type: ignore[union-attr]


def write(i: Intake, statement: Any) -> None:
    with open_unit_of_work(i.ctx) as uow:
        uow.connection.execute(statement)
        uow.commit()


def collector_row(connection_id: UUID, pipeline_id: UUID, **changes: Any) -> Any:
    now = datetime.now(UTC)
    values: dict[str, Any] = dict(
        connection_id=connection_id,
        pipeline_id=pipeline_id,
        authorization_state="authorization_required",
        resource_url="https://mcp.example.com/mcp",
        issuer="https://auth.example.com",
        authorization_endpoint="https://auth.example.com/authorize",
        token_endpoint="https://auth.example.com/token",
        registration_endpoint="https://auth.example.com/register",
        created_at=now,
        updated_at=now,
    )
    values.update(changes)
    return insert(j.jobfeed_connection).values(**values)


def authorization_row(connection_id: UUID, pipeline_id: UUID, state: bytes) -> Any:
    now = datetime.now(UTC)
    return insert(j.jobfeed_authorization).values(
        id=uuid7(),
        singleton=True,
        state_hash=state,
        code_verifier="v" * 86,
        account_id=uuid7(),
        connection_id=connection_id,
        pipeline_id=pipeline_id,
        mode="connect",
        resource_url="https://mcp.example.com/mcp",
        issuer="https://auth.example.com",
        authorization_endpoint="https://auth.example.com/authorize",
        token_endpoint="https://auth.example.com/token",
        registration_endpoint="https://auth.example.com/register",
        redirect_uri="https://auth.example.com/connector/leads/callback",
        client_id="example-client",
        client_auth_method="none",
        client_registered=True,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )


def test_transport_singleton_and_client_constraints(intake: Intake) -> None:
    pipeline = RecordRef.parse(ok(intake, "pipeline.create", name="Job examples").ref)
    first = connector(intake, "poll", "fixture-poll")
    second = connector(intake, "poll", "fixture-poll-second")
    with refused(intake, "connection_transport") as connection:
        row = dict(
            connection.execute(
                select(t.intake_connection).where(t.intake_connection.c.id == first)
            )
            .mappings()
            .one()
        )
        row.update(id=uuid7(), transport="carrier", source_namespace="fixture-x")
        connection.execute(insert(t.intake_connection).values(**row))

    # Client columns: all absent, or id, method and redirect URI together.
    for changes in (
        {"client_id": "example-client"},
        {"client_id": "example-client", "client_auth_method": "none"},
        {
            "client_auth_method": "none",
            "client_redirect_uri": "https://www.example.com/callback",
        },
        {"client_redirect_uri": "https://www.example.com/callback"},
    ):
        with refused(intake, "jobfeed_connection_client_complete") as connection:
            connection.execute(collector_row(first, pipeline.id, **changes))
    with refused(intake, "jobfeed_connection_client_auth_method") as connection:
        connection.execute(
            collector_row(
                first,
                pipeline.id,
                client_id="example-client",
                client_auth_method="private_key_jwt",
                client_redirect_uri="https://www.example.com/callback",
            )
        )
    with refused(intake, "jobfeed_connection_authorization_state") as connection:
        connection.execute(
            collector_row(first, pipeline.id, authorization_state="paused")
        )
    with refused(intake, "jobfeed_connection_singleton_true") as connection:
        connection.execute(collector_row(first, pipeline.id, singleton=False))
    write(
        intake,
        collector_row(
            first,
            pipeline.id,
            client_id="example-client",
            client_auth_method="client_secret_basic",
            client_redirect_uri="https://www.example.com/callback",
        ),
    )
    with refused(intake, "jobfeed_connection_singleton_key") as connection:
        connection.execute(collector_row(second, pipeline.id))

    write(intake, authorization_row(first, pipeline.id, b"\x01" * 32))
    with refused(intake, "jobfeed_authorization_singleton_key") as connection:
        connection.execute(authorization_row(second, pipeline.id, b"\x02" * 32))
    with open_unit_of_work(intake.ctx) as uow:
        assert (
            uow.connection.execute(select(j.jobfeed_connection))
            .mappings()
            .one()["consecutive_failures"]
            == 0
        )


def test_export_carries_the_job_feed_tables_without_secret_or_client_state(
    intake: Intake,
) -> None:
    """Export format 7: the connection row leaves out its token reference, every
    client column, its run claim and its backoff; the pending authorization and the
    run history are never exported; the dedupe rows and account choices are."""
    from types import SimpleNamespace
    from typing import cast

    from rheo_core.exports.artifact import ExportSnapshot
    from rheo_leads.export import export_records

    secrets = {
        "token_ref": "fixture-token-ref-7f3a",
        "client_secret_ref": "fixture-client-secret-ref-7f3a",
        "registration_token_ref": "fixture-registration-ref-7f3a",
        "client_id": "fixture-client-id-7f3a",
        "client_redirect_uri": "https://www.example.com/fixture-callback-7f3a",
    }
    claimed = datetime(2031, 3, 4, 5, 6, 7, 111111, tzinfo=UTC)
    backoff = datetime(2031, 3, 4, 7, 8, 9, 222222, tzinfo=UTC)
    expiry = datetime(2031, 3, 4, 9, 10, 11, 333333, tzinfo=UTC)
    verifier = "fixture-verifier-7f3a" + "v" * 65
    pipeline = RecordRef.parse(ok(intake, "pipeline.create", name="Job examples").ref)
    poll = connector(intake, "poll", "fixture-poll")
    write(
        intake,
        collector_row(
            poll,
            pipeline.id,
            authorization_state="connected",
            client_auth_method="client_secret_post",
            client_secret_expires_at=expiry,
            organization_id="fixture-org-7f3a",
            run_claimed_at=claimed,
            not_before=backoff,
            **secrets,
        ),
    )
    write(intake, authorization_row(poll, pipeline.id, b"\x03" * 32))
    now = datetime.now(UTC)
    write(
        intake,
        j.jobfeed_authorization.update().values(
            code_verifier=verifier,
            client_secret_ref="fixture-pending-secret-ref-7f3a",
            registration_token_ref="fixture-pending-registration-ref-7f3a",
        ),
    )
    write(
        intake,
        insert(j.jobfeed_posting).values(
            connection_id=poll,
            posting_id="fixture-posting-7f3a",
            seq=1,
            field_digests="title=0123456789abcdef",
            last_event_id="fixture-event-7f3a",
            first_delivered_at=now,
            last_delivered_at=now,
        ),
    )
    write(
        intake,
        insert(j.jobfeed_account_choice).values(
            connection_id=poll,
            organization_id="fixture-org-7f3a",
            label="Example organization",
            ordinal=0,
            seen_at=now,
        ),
    )
    # The run history (0016) is the deployment's operating record: written here so
    # the check below proves it stays out of the export, like the pending
    # authorization.
    write(
        intake,
        insert(j.jobfeed_run).values(
            id=uuid7(),
            finished_at=now,
            outcome="partial",
            read_failures={"no_description": 1},
        ),
    )
    with open_unit_of_work(intake.ctx) as uow:
        for table in j.TABLES:
            assert uow.connection.execute(select(table)).first() is not None, table
        rows = export_records(
            cast(ExportSnapshot, SimpleNamespace(connection=uow.connection))
        )
    record_types = {str(row["record_type"]) for row in rows}
    # Positive control: the export ran and carried the poll connection itself.
    assert "leads.intake_connection" in record_types
    assert any(row.get("id") == poll for row in rows)
    assert {name for name in record_types if name.startswith("leads.jobfeed_")} == {
        "leads.jobfeed_connection",
        "leads.jobfeed_posting",
        "leads.jobfeed_account_choice",
    }
    (collector,) = (r for r in rows if r["record_type"] == "leads.jobfeed_connection")
    assert collector["connection_id"] == poll
    assert collector["organization_id"] == "fixture-org-7f3a"
    assert collector["registration_endpoint"] == "https://auth.example.com/register"
    omitted = {
        "token_ref",
        "run_claimed_at",
        "not_before",
        "client_id",
        "client_auth_method",
        "client_redirect_uri",
        "client_secret_expires_at",
        "client_secret_ref",
        "registration_token_ref",
    }
    assert not omitted & set(collector), collector
    assert set(collector) == {"record_type"} | (
        set(j.jobfeed_connection.columns.keys()) - omitted
    )
    (posting,) = (r for r in rows if r["record_type"] == "leads.jobfeed_posting")
    assert posting["posting_id"] == "fixture-posting-7f3a"
    rendered = repr(rows)
    for value in (
        *secrets.values(),
        verifier,
        "fixture-pending-secret-ref-7f3a",
        "fixture-pending-registration-ref-7f3a",
        "client_secret_post",
        "jobfeed_authorization",
        "code_verifier",
        "state_hash",
        (b"\x03" * 32).hex(),
        *(repr(moment) for moment in (claimed, backoff, expiry)),
        *(moment.isoformat() for moment in (claimed, backoff, expiry)),
        *omitted,
    ):
        assert value not in rendered, value
