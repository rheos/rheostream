"""Version-eleven intake, pipeline, job feed, job scoring, review, proposal and pricing
snapshot; restore preserves identities.

Signing secret handles are deployment-local and are never exported. Imported
webhook connections need new credentials. The job feed connection is exported
without its token reference, its registered client, its run claim and its backoff,
and the pending authorization is never exported; a restored collector needs the
owner to reconnect, which registers a fresh client. The job-search profile and each
job's scoring state are exported whole, and so are the owner's promotions from the
review queue; a restored scoring schedule stays off until the owner sets the profile
again. The model call log and the job feed's run history are never exported. The proposal profile, each job's
proposal with its sections, its pricing and milestones, and the answer library are
exported whole; a proposal or pricing request that was queued or running is restored
as failed, since its sweep did not come with it. Restore replaces only the fresh
seed; existing intake records cause a refusal, never a destructive merge.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import ValidationError
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import (
    ARRAY,
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    LargeBinary,
    Numeric,
    Table,
    Uuid,
    delete,
    func,
    insert,
    select,
)

from rheo_leads.proposals.profile import ProposalPolicy
from rheo_leads.scoring.profile import ScoringPolicy
from rheo_leads.storage import jobfeed
from rheo_leads.storage import pipeline as _pipeline  # noqa: F401
from rheo_leads.storage import proposals as _proposals
from rheo_leads.storage import scoring as _scoring
from rheo_leads.storage import tables as t

if TYPE_CHECKING:
    from rheo_core.exports.artifact import ExportSnapshot

TABLES = tuple(t.metadata.sorted_tables)
"""Every owned table: restore checks each against the fresh seed and replaces it."""
# The pending authorization holds a PKCE verifier and fresh client references. The
# model call log and the collector's run history are this deployment's operating
# record, not workspace data; a restored workspace starts with empty ones.
EXPORTED = tuple(
    table
    for table in TABLES
    if table
    not in (jobfeed.jobfeed_authorization, _scoring.model_call, jobfeed.jobfeed_run)
)
SECRET_COLUMNS = {"signing_secret_ref", "previous_secret_ref", "previous_valid_until"}
# Left out of the export entirely, not nulled: a secret reference, the registered
# client (a restored copy may sit behind another redirect URI), the run claim and the
# backoff. Restore writes RESTORED_COLLECTOR over them.
OMITTED_COLUMNS = {
    jobfeed.jobfeed_connection.name: frozenset(
        {
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
    )
}
RESTORED_COLLECTOR: Mapping[str, object] = {
    "authorization_state": "authorization_required",
    "token_ref": None,
    "credential_generation": 0,
    "run_claimed_at": None,
    "consecutive_failures": 0,
    "not_before": None,
    "client_id": None,
    "client_auth_method": None,
    "client_redirect_uri": None,
    "client_secret_expires_at": None,
    "client_secret_ref": None,
    "registration_token_ref": None,
}


# A queued or running draft's sweep is not exported, so nothing would ever run it.
RESTORED_PROPOSAL: Mapping[str, object] = {
    "state": "failed",
    "stage": None,
    "step": None,
    "total": None,
    "retry_after": None,
    "failure_kind": "restored",
}


RESTORED_PRICING: Mapping[str, object] = {
    "state": "failed",
    "retry_after": None,
    "failure_kind": "restored",
}


def _columns(table: Table) -> list[Column[Any]]:
    """The columns a table's exported rows carry."""
    omitted = OMITTED_COLUMNS.get(table.name, frozenset())
    return [column for column in table.columns if column.name not in omitted]


def export_records(snapshot: "ExportSnapshot") -> Sequence[Mapping[str, object]]:
    """Export the owned tables in foreign-key order."""
    rows: list[Mapping[str, object]] = []
    for table in EXPORTED:
        columns = _columns(table)
        for row in snapshot.connection.execute(
            select(*columns).order_by(*table.primary_key.columns)
        ).mappings():
            values = dict(row)
            for column in columns:
                value = values[column.name]
                if column.name in SECRET_COLUMNS:
                    values[column.name] = None
                elif isinstance(value, bytes):
                    values[column.name] = value.hex()
            rows.append({"record_type": f"leads.{table.name}", **values})
    return rows


def _decode(rows: Sequence[Mapping[str, object]]) -> dict[str, list[dict[str, Any]]]:
    """Reject unknown fields and validate scalar types before issuing any write."""
    grouped: dict[str, list[dict[str, Any]]] = {table.name: [] for table in TABLES}
    by_type = {f"leads.{table.name}": table for table in EXPORTED}
    try:
        for row in rows:
            table = by_type[str(row["record_type"])]
            columns = _columns(table)
            if set(row) != {"record_type", *(column.name for column in columns)}:
                raise ValueError
            values: dict[str, Any] = {}
            for column in columns:
                value = row[column.name]
                if value is None:
                    if not column.nullable:
                        raise ValueError
                elif isinstance(column.type, Uuid):
                    value = UUID(str(value))
                elif isinstance(column.type, DateTime):
                    value = datetime.fromisoformat(str(value))
                    if value.tzinfo is None:
                        raise ValueError
                elif isinstance(column.type, Date):
                    value = date.fromisoformat(str(value))
                elif isinstance(column.type, LargeBinary):
                    value = bytes.fromhex(str(value))
                elif isinstance(column.type, Numeric):
                    value = Decimal(str(value))
                    if not value.is_finite():
                        raise ValueError
                elif isinstance(column.type, ARRAY):
                    if not isinstance(value, list) or any(
                        not isinstance(v, str) for v in value
                    ):
                        raise ValueError
                elif isinstance(column.type, JSON):
                    if not isinstance(value, dict):
                        raise ValueError
                elif isinstance(column.type, Boolean):
                    if not isinstance(value, bool):
                        raise ValueError
                elif isinstance(column.type, Integer):
                    if type(value) is not int:
                        raise ValueError
                elif not isinstance(value, str):
                    raise ValueError
                if column.name in SECRET_COLUMNS:
                    if value is not None:
                        raise ValueError
                values[column.name] = value
            if (
                table is t.intake_connection
                and values["transport"] == "webhook"
                and values["state"] == "active"
            ):
                values["state"] = "needs_credential"
            if table is _scoring.jobsearch_profile:
                # The scorer reads the policy whole, so a malformed one is refused
                # here rather than stalling scoring after the restore.
                ScoringPolicy.model_validate(values["policy"])
            if table is _proposals.proposal_profile:
                ProposalPolicy.model_validate(values["policy"])
            if table is _proposals.job_proposal and values["state"] in (
                "queued",
                "drafting",
            ):
                values.update(RESTORED_PROPOSAL)
            if table is _proposals.job_pricing and values["state"] in (
                "queued",
                "running",
            ):
                values.update(RESTORED_PRICING)
            if table is jobfeed.jobfeed_connection:
                # No token, no client, no claim, no backoff: the owner reconnects.
                values.update(RESTORED_COLLECTOR)
            grouped[table.name].append(values)
    except (KeyError, ValueError, TypeError, InvalidOperation, ValidationError):
        raise OperationRefused("artifact_invalid", "invalid Leads intake row") from None
    connections = grouped["intake_connection"]
    if (
        sum(
            row["transport"] == "manual"
            and row["source_namespace"] == t.MANUAL_CAPTURE_NAMESPACE
            for row in connections
        )
        != 1
    ):
        raise OperationRefused(
            "artifact_invalid", "exactly one manual capture connection is required"
        )
    # The deletion participant locates the retained receipt after owner deletion.
    if any(row["id"] != row["receipt_id"] for row in grouped["observation"]):
        raise OperationRefused(
            "artifact_invalid", "observation identity differs from receipt"
        )
    erased = {
        row["id"]
        for row in grouped["delivery_receipt"]
        if row["state_detail"] == "observation_deleted"
    }
    if any(row["receipt_id"] in erased for row in grouped["delivery_payload"]) or any(
        row["receipt_id"] in erased and row["body"] is not None
        for row in grouped["delivery_conflict"]
    ):
        raise OperationRefused("artifact_invalid", "erased intake contains payload")
    return grouped


def import_records(
    uow: UnitOfWork, rows: Sequence[Mapping[str, object]]
) -> Callable[[], None]:
    """Reconcile a fresh seed before inserting the preserved source identities."""
    grouped = _decode(rows)
    counts = {
        table.name: uow.connection.execute(
            select(func.count()).select_from(table)
        ).scalar_one()
        for table in TABLES
    }
    expected = {table.name: 0 for table in TABLES}
    expected.update(
        funnel=1,
        field_mapping=1,
        field_mapping_identity=1,
        field_mapping_rule=13,
        intake_connection=1,
        connection_health=1,
        pipeline_preset=2,
        preset_version=2,
        preset_stage=16,
        preset_transition=35,
        preset_requirement=0,
        preset_field=16,
        preset_rubric=2,
        preset_template=2,
    )
    if counts != expected:
        raise OperationRefused(
            "restore_target_not_empty", "Leads restore requires a fresh seed"
        )
    manual = uow.connection.execute(select(t.intake_connection)).mappings().one()
    if (
        manual["transport"] != "manual"
        or manual["source_namespace"] != "manual_capture"
    ):
        raise OperationRefused(
            "restore_target_not_empty", "Leads restore requires a fresh seed"
        )
    # All content tables were proved empty; only the generated seed is removed.
    for table in reversed(TABLES):
        uow.connection.execute(delete(table))
    for table in TABLES:
        if grouped[table.name]:
            uow.connection.execute(insert(table), grouped[table.name])

    def finish() -> None:
        """No cross-module references are materialized in the intake-only release."""

    return finish
