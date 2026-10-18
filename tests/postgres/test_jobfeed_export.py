"""Export format 7 and restore on a workspace with a connected job feed collector.

Seams under test: the real workspace export job and restore operation (the module's
exporter and importer, the artifact's schema validation and archive format) on a
populated collector workspace whose refresh token, client secret and registration
token are real files written by the real OAuth client. The artifact is read back
whole and searched for every secret marker; the restored copy is checked for what it
can do, including that its first reconnect against the fake authorization server
registers a fresh client exactly once.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import pytest
from conftest import MakeWorkspace
from harness.jobfeed import (
    CLIENT_SECRET,
    REGISTRATION_TOKEN,
    STORED_CLIENT_ID,
    ConnectedCollector,
    FakeAuthorizationServer,
    connected_collector,
    deliver,
    drain,
    owner_web,
)
from harness.modules import install_and_enable_module
from rheo_contracts import Role, WorkspaceContext
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.exports.artifact import (
    ExportSnapshot,
    _validated_module_rows,
    module_entry_name,
    read_archive,
)
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads import MANIFEST
from rheo_leads.export import export_records, import_records
from rheo_leads.storage import jobfeed as j
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t
from sqlalchemy import func, insert, select

from postgres.test_jobfeed_authorization import collect_schedule, endpoints_of, stored
from postgres.test_jobfeed_connect import (
    RECONNECT,
    capability,
    dump,
    file_of,
    pending,
    run,
)
from postgres.test_jobfeed_connect import clock as clock
from postgres.test_jobfeed_connect import root as root
from postgres.test_jobfeed_connect import served as served
from postgres.test_jobfeed_connect import server as server
from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake
from postgres.test_leads_pipeline import export_workspace, ok
from postgres.test_memory_export_restore import _remove_workspace, _restore

pytestmark = pytest.mark.postgres

POSTING = "~02123"
EVENT = f"{POSTING}.1.0a1b2c3d4e5f6071"
ORG_ONE = "org-example-1"
ORG_TWO = "org-example-2"
CHOICES = ((ORG_ONE, "Example organization one"), (ORG_TWO, None))
# Distinct instants nothing else in the workspace carries, so a search finds them.
CLAIMED = datetime(2031, 3, 4, 5, 6, 7, tzinfo=UTC)
BACKOFF = datetime(2031, 3, 5, 6, 7, 8, tzinfo=UTC)
EXPIRES = datetime(2031, 3, 6, 7, 8, 9, tzinfo=UTC)
CLIENT_COLUMNS = (
    "client_id",
    "client_auth_method",
    "client_redirect_uri",
    "client_secret_expires_at",
    "client_secret_ref",
    "registration_token_ref",
)
OMITTED = ("token_ref", "run_claimed_at", "not_before", *CLIENT_COLUMNS)
REPORT = {"fetched": 11, "imported": 7, "skipped_dup": 2, "pages": 3}
KEPT_TABLES = (
    j.jobfeed_posting,
    j.jobfeed_account_choice,
    p.opportunity,
    p.opportunity_observation,
    p.opportunity_field_state,
    t.observation,
    t.delivery_receipt,
)


# --- helpers ------------------------------------------------------------------------


def populate(i: Intake, server: FakeAuthorizationServer) -> ConnectedCollector:
    """A connected collector with a stored client secret and registration token,
    account choices, report counts, a run claim, a backoff, a collected posting with
    its dedupe row, and a pending reconnect."""
    c = connected_collector(
        i,
        server,
        auth_method="client_secret_basic",
        report=REPORT,
        choices=CHOICES,
        organization_id=ORG_ONE,
        run_claimed_at=CLAIMED,
        not_before=BACKOFF,
        client_secret_expires_at=EXPIRES,
        consecutive_failures=2,
    )
    accepted = deliver(
        c.poll,
        i,
        {
            "title": "Example posting",
            "posting_id": POSTING,
            "observed_at": "2026-01-05T09:00:00+00:00",
        },
        EVENT,
    )
    assert accepted.ok, accepted
    drain(i)
    now = datetime.now(UTC)
    with open_unit_of_work(c.owner) as uow:
        uow.connection.execute(
            insert(j.jobfeed_posting).values(
                connection_id=c.connection_id,
                posting_id=POSTING,
                seq=1,
                field_digests="title=00112233",
                last_event_id=EVENT,
                first_delivered_at=now,
                last_delivered_at=now,
            )
        )
        uow.commit()
    # A reconnect in flight: the pending row holds a PKCE verifier and the client.
    started = run(i, c.owner, RECONNECT)
    assert started.ok, started
    assert server.registrations == [], "the stored client is reused"
    assert len(pending(c.owner)) == 1
    return c


def markers(root: Path, c: ConnectedCollector) -> list[str]:
    """Every value that must never leave the workspace in an export."""
    current = stored(c.owner)
    (row,) = pending(c.owner)
    references = [
        c.token_ref,
        current.client_secret_ref,
        current.registration_token_ref,
    ]
    assert all(references), references
    found: list[str] = [
        c.refresh_token,
        CLIENT_SECRET,
        REGISTRATION_TOKEN,
        STORED_CLIENT_ID,
        "client_secret_basic",
        str(root),
        str(current.client_redirect_uri),
        row["code_verifier"],
        row["state_hash"].hex(),
        str(row["id"]),
        *(str(reference) for reference in references),
        *(file_of(root, str(reference)).name for reference in references),
        *(moment.isoformat() for moment in (CLAIMED, BACKOFF, EXPIRES)),
        "2031-03-0",
        "jobfeed_authorization",
        "code_verifier",
    ]
    return found


def kept(ctx: WorkspaceContext) -> dict[str, list[dict[str, Any]]]:
    with open_unit_of_work(ctx) as uow:
        return {
            table.name: sorted(
                (dict(r) for r in uow.connection.execute(select(table)).mappings()),
                key=repr,
            )
            for table in KEPT_TABLES
        }


def archive_text(artifact: Path) -> tuple[dict[str, bytes], str]:
    entries = read_archive(artifact)
    return entries, "\n".join(body.decode("utf-8") for body in entries.values())


def leads_rows(entries: dict[str, bytes]) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in entries[module_entry_name("leads")].decode("utf-8").splitlines()
    ]


def exported(ctx: WorkspaceContext) -> list[dict[str, object]]:
    with open_unit_of_work(ctx) as uow:
        return [
            dict(row)
            for row in export_records(
                cast(ExportSnapshot, SimpleNamespace(connection=uow.connection))
            )
        ]


# --- AC 12, AC 56, AC 78 restore half ------------------------------------------------


def test_populated_round_trip_carries_no_secret_and_needs_a_reconnect(
    intake: Intake,
    root: Path,
    served: FakeAuthorizationServer,
    make_workspace: MakeWorkspace,
    owner_account_id: UUID,
) -> None:
    c = populate(intake, served)
    before = stored(c.owner)
    before_rows = kept(c.owner)
    secret = markers(root, c)
    original_files = {
        file_of(root, str(r))
        for r in (c.token_ref, before.client_secret_ref, before.registration_token_ref)
    }
    assert all(f.is_file() for f in original_files)
    assert MANIFEST.export.format_version == 10

    artifact = export_workspace(intake)
    entries, text = archive_text(artifact)
    manifest = json.loads(entries["manifest.json"])
    (leads,) = (m for m in manifest["modules"] if m["module_id"] == "leads")
    assert leads["export_format_version"] == 10
    rows = leads_rows(entries)
    # The archived rows are the format-10 shape, valid against its schema.
    assert _validated_module_rows(MANIFEST, rows, "leads") == rows
    by_type: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_type.setdefault(row["record_type"], []).append(row)
    # Positive control: the collector's own state is in the artifact.
    (collector,) = by_type["leads.jobfeed_connection"]
    assert collector["connection_id"] == str(c.connection_id)
    assert collector["authorization_state"] == "connected"
    assert collector["registration_endpoint"] == before.registration_endpoint
    assert collector["organization_id"] == ORG_ONE
    assert collector["fetched"] == 11 and collector["pages"] == 3
    assert [r["posting_id"] for r in by_type["leads.jobfeed_posting"]] == [POSTING]
    assert {r["organization_id"] for r in by_type["leads.jobfeed_account_choice"]} == {
        ORG_ONE,
        ORG_TWO,
    }
    assert "leads.jobfeed_authorization" not in by_type
    for column in OMITTED:
        assert column not in collector, column
        assert f'"{column}"' not in entries[module_entry_name("leads")].decode()
    # Nothing secret anywhere in the whole archive, core categories included.
    for value in secret:
        assert value not in text, value

    _remove_workspace(intake.cluster, intake.workspace)
    host = context_for_operator(make_workspace())
    assert isinstance(host, WorkspaceContext)
    _restore(intake.cluster, host, artifact)
    intake.ctx = context_for_harness(intake.workspace, owner_account_id, Role.OWNER)
    assert isinstance(intake.ctx, WorkspaceContext), intake.ctx
    owner = owner_web(intake)

    after = stored(owner)
    assert after.authorization_state == "authorization_required"
    assert after.token_ref is None
    assert after.credential_generation == 0
    assert after.consecutive_failures == 0
    assert after.not_before is None and after.run_claimed_at is None
    for column in CLIENT_COLUMNS:
        assert getattr(after, column) is None, column
    # What a run reads first: no token, no schedule, so it calls nothing upstream.
    assert collect_schedule(owner) is None
    assert pending(owner) == []
    # The non-secret state came back unchanged.
    assert endpoints_of(after) == endpoints_of(before)
    assert after.organization_id == ORG_ONE
    assert (after.fetched, after.imported, after.skipped_dup, after.pages) == (
        11,
        7,
        2,
        3,
    )
    assert kept(owner) == before_rows
    # No row of the restored copy refers to an original secret file.
    restored = dump(owner)
    for value in secret:
        assert value not in restored, value

    # The first reconnect registers a fresh client, exactly once.
    served.requests.clear()
    reconnected = run(intake, owner, RECONNECT)
    assert reconnected.ok, reconnected
    assert len(served.registrations) == 1
    query = parse_qs(urlsplit(reconnected.result.authorization_url).query)  # type: ignore[union-attr]
    (fresh,) = pending(owner)
    assert query["client_id"] == [fresh["client_id"]]
    assert fresh["client_id"] != STORED_CLIENT_ID
    assert fresh["client_registered"] is True
    # The original secret files are still where they were, and nothing uses them.
    assert all(f.is_file() for f in original_files)
    assert not any(
        str(r) in dump(owner)
        for r in (c.token_ref, before.client_secret_ref, before.registration_token_ref)
    )


def test_a_format_six_row_set_restores_with_empty_job_feed_tables(
    intake: Intake,
    root: Path,
    served: FakeAuthorizationServer,
    make_workspace: MakeWorkspace,
) -> None:
    """Format 6 carried every table but the four job feed ones (a poll connection
    included), so its rows are format 7's rows without them; the importer accepts
    them and leaves the job feed tables empty."""
    c = populate(intake, served)
    rows = [
        row
        for row in exported(c.owner)
        if not str(row["record_type"]).startswith("leads.jobfeed_")
    ]
    serialized = _validated_module_rows(MANIFEST, rows, "leads")
    other = make_workspace()
    operator = context_for_operator(other)
    assert isinstance(operator, WorkspaceContext)
    install_and_enable_module(intake.cluster.backend, operator, other, "relationships")
    install_and_enable_module(intake.cluster.backend, operator, other, "leads")
    with open_unit_of_work(operator) as uow:
        import_records(uow, serialized)()
        for table in j.TABLES:
            assert (
                uow.connection.execute(
                    select(func.count()).select_from(table)
                ).scalar_one()
                == 0
            ), table
        transports = sorted(
            uow.connection.execute(select(t.intake_connection.c.transport)).scalars()
        )
        assert transports == ["manual", "poll"]
        assert (
            uow.connection.execute(
                select(func.count()).select_from(t.observation)
            ).scalar_one()
            == 1
        )
        uow.commit()


@pytest.mark.parametrize(
    "table", ["jobfeed_posting", "jobfeed_account_choice", "jobfeed_authorization"]
)
def test_restore_refuses_a_fresh_seed_plus_one_job_feed_row(
    intake: Intake, make_workspace: MakeWorkspace, table: str
) -> None:
    """The fresh-seed check counts the job feed tables too: a target that is a
    fresh seed in every other table is refused for one job feed row."""
    from rheo_core.operations.refusals import OperationRefused

    from postgres.test_jobfeed_migration import authorization_row

    intake.accept()
    intake.visit()
    serialized = _validated_module_rows(MANIFEST, exported(intake.ctx), "leads")
    other = make_workspace()
    operator = context_for_operator(other)
    assert isinstance(operator, WorkspaceContext)
    install_and_enable_module(intake.cluster.backend, operator, other, "relationships")
    install_and_enable_module(intake.cluster.backend, operator, other, "leads")
    now = datetime.now(UTC)
    with open_unit_of_work(operator) as uow:
        manual = uow.connection.execute(select(t.intake_connection.c.id)).scalar_one()
        uow.connection.execute(
            {
                "jobfeed_posting": insert(j.jobfeed_posting).values(
                    connection_id=manual,
                    posting_id=POSTING,
                    seq=1,
                    field_digests="title=00112233",
                    last_event_id=EVENT,
                    first_delivered_at=now,
                    last_delivered_at=now,
                ),
                "jobfeed_account_choice": insert(j.jobfeed_account_choice).values(
                    connection_id=manual,
                    organization_id=ORG_ONE,
                    label=None,
                    ordinal=0,
                    seen_at=now,
                ),
                "jobfeed_authorization": authorization_row(
                    manual, manual, b"\x05" * 32
                ),
            }[table]
        )
        uow.commit()
    with open_unit_of_work(operator) as uow:
        with pytest.raises(OperationRefused) as refused:
            import_records(uow, serialized)
    assert refused.value.state == "restore_target_not_empty"


@pytest.mark.parametrize("column", OMITTED)
def test_the_importer_refuses_a_collector_row_carrying_an_omitted_column(
    intake: Intake, root: Path, served: FakeAuthorizationServer, column: str
) -> None:
    """A hand-edited artifact cannot put a reference or client column back."""
    from rheo_core.operations.refusals import OperationRefused

    c = populate(intake, served)
    rows = exported(c.owner)
    for row in rows:
        if row["record_type"] == "leads.jobfeed_connection":
            row[column] = None
    with open_unit_of_work(c.owner) as uow:
        with pytest.raises(OperationRefused) as refused:
            import_records(uow, rows)
    assert refused.value.state == "artifact_invalid"


# --- AC 28 records half --------------------------------------------------------------


def test_collected_records_stay_readable_and_exportable_while_the_capability_is_off(
    intake: Intake, root: Path, served: FakeAuthorizationServer
) -> None:
    c = populate(intake, served)
    before = kept(c.owner)
    (item,) = ok(intake, "opportunity.list").items
    detail = ok(intake, "opportunity.get", ref=item["ref"])

    capability(intake, c.owner, False)
    (off_item,) = ok(intake, "opportunity.list").items
    assert off_item["ref"] == item["ref"]
    off_detail = ok(intake, "opportunity.get", ref=item["ref"])
    assert off_detail.data == detail.data
    assert kept(c.owner) == before
    entries, _text = archive_text(export_workspace(intake))
    rows = leads_rows(entries)

    def of(kind: str, column: str) -> list[Any]:
        return sorted(row[column] for row in rows if row["record_type"] == kind)

    assert of("leads.jobfeed_posting", "posting_id") == [POSTING]
    assert of("leads.jobfeed_account_choice", "organization_id") == [ORG_ONE, ORG_TWO]
    assert of("leads.opportunity", "id") == [
        str(r["id"]) for r in before["opportunity"]
    ]
    assert of("leads.observation", "id") == [
        str(r["id"]) for r in before["observation"]
    ]

    capability(intake, c.owner, True)
    assert kept(c.owner) == before
    assert ok(intake, "opportunity.get", ref=item["ref"]).data == detail.data
