"""The job feed collector's declared tables: singletons, closed values, and no JSON
apart from the run history's reason tally."""

from rheo_leads.storage import jobfeed as j
from rheo_leads.storage import tables as t
from sqlalchemy import JSON, CheckConstraint, Table, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

AUTH_METHODS = "('none','client_secret_basic','client_secret_post')"
TABLES = {
    "jobfeed_connection": j.jobfeed_connection,
    "jobfeed_authorization": j.jobfeed_authorization,
    "jobfeed_posting": j.jobfeed_posting,
    "jobfeed_account_choice": j.jobfeed_account_choice,
}


def checks(table: Table) -> dict[str | None, str]:
    return {
        constraint.name: str(constraint.sqltext)  # type: ignore[misc]
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }


def unique_columns(table: Table) -> list[set[str]]:
    found = [
        set(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    ]
    return found + [{column.name} for column in table.columns if column.unique]


def test_four_tables_are_registered_on_the_leads_metadata() -> None:
    for name, table in TABLES.items():
        assert t.metadata.tables[f"leads.{name}"] is table
        assert table.schema == "leads"
        for foreign_key in table.foreign_keys:
            assert foreign_key.column.table.schema == "leads"


def test_no_column_is_json() -> None:
    # Positive control: the walk sees every column of all four tables.
    assert sum(len(table.columns) for table in TABLES.values()) == 69
    for table in TABLES.values():
        for column in table.columns:
            assert not isinstance(column.type, JSON | JSONB), column


def test_the_run_history_holds_json_only_in_its_reason_tally() -> None:
    """``read_failures`` is the one JSON column: a tally of fixed reason codes read
    whole (#377); every count and code the history is filtered by is a column."""
    run = j.jobfeed_run
    assert t.metadata.tables["leads.jobfeed_run"] is run
    assert run in j.TABLES
    assert len(run.columns) == 14
    assert [c.name for c in run.columns if isinstance(c.type, JSON | JSONB)] == [
        "read_failures"
    ]
    assert checks(run) == {
        "jobfeed_run_outcome": "outcome IN ('completed','partial','stopped')"
    }


def test_singletons_are_unique_and_true() -> None:
    for table in (j.jobfeed_connection, j.jobfeed_authorization):
        assert {"singleton"} in unique_columns(table)
        assert not table.c.singleton.nullable
        assert "singleton" in checks(table).values()
    default = j.jobfeed_connection.c.singleton.server_default
    assert default is not None and str(default.arg) == "true"  # type: ignore[attr-defined]
    assert {"state_hash"} in unique_columns(j.jobfeed_authorization)
    # The other two tables are keyed per connection, not singletons.
    assert "singleton" not in j.jobfeed_posting.c
    assert "singleton" not in j.jobfeed_account_choice.c


def test_closed_values_and_client_completeness() -> None:
    connection = checks(j.jobfeed_connection)
    authorization = checks(j.jobfeed_authorization)
    assert connection["jobfeed_connection_authorization_state"] == (
        "authorization_state IN ('connected','authorization_required','revoked')"
    )
    assert connection["jobfeed_connection_client_auth_method"] == (
        f"client_auth_method IN {AUTH_METHODS}"
    )
    assert authorization["jobfeed_authorization_client_auth_method"] == (
        f"client_auth_method IN {AUTH_METHODS}"
    )
    assert authorization["jobfeed_authorization_mode"] == (
        "mode IN ('connect','reconnect')"
    )
    assert connection["jobfeed_connection_client_complete"] == (
        "(client_id IS NULL) = (client_auth_method IS NULL) "
        "AND (client_id IS NULL) = (client_redirect_uri IS NULL)"
    )
    assert checks(t.intake_connection)["connection_transport"] == (
        "transport IN ('webhook','import','manual','poll')"
    )


def test_keys_nullability_and_references() -> None:
    assert list(j.jobfeed_connection.primary_key.columns.keys()) == ["connection_id"]
    assert list(j.jobfeed_posting.primary_key.columns.keys()) == [
        "connection_id",
        "posting_id",
    ]
    assert list(j.jobfeed_account_choice.primary_key.columns.keys()) == [
        "connection_id",
        "organization_id",
    ]
    references = {
        (table.name, fk.parent.name): fk.target_fullname
        for table in TABLES.values()
        for fk in table.foreign_keys
    }
    assert references == {
        ("jobfeed_connection", "connection_id"): "leads.intake_connection.id",
        ("jobfeed_connection", "pipeline_id"): "leads.pipeline.id",
        ("jobfeed_posting", "connection_id"): "leads.intake_connection.id",
        ("jobfeed_account_choice", "connection_id"): "leads.intake_connection.id",
    }, "a first connect pre-allocates the pending authorization's connection id"
    nullable = {
        "jobfeed_connection": {
            "token_ref",
            "revocation_endpoint",
            "client_id",
            "client_auth_method",
            "client_redirect_uri",
            "client_secret_expires_at",
            "client_secret_ref",
            "registration_token_ref",
            "organization_id",
            "run_claimed_at",
            "last_completed_at",
            "not_before",
        },
        "jobfeed_authorization": {
            "revocation_endpoint",
            "client_secret_expires_at",
            "client_secret_ref",
            "registration_token_ref",
        },
        "jobfeed_posting": set(),
        "jobfeed_account_choice": {"label"},
    }
    for name, table in TABLES.items():
        assert {c.name for c in table.columns if c.nullable} == nullable[name], name
    counts = (
        "fetched",
        "imported",
        "skipped_dup",
        "refreshed",
        "filtered",
        "skipped_untracked",
        "errors",
        "detail_reads",
        "pages",
        "consecutive_failures",
        "credential_generation",
    )
    for name in counts:
        column = j.jobfeed_connection.c[name]
        assert column.server_default is not None
        assert str(column.server_default.arg) == "0", name  # type: ignore[attr-defined]
