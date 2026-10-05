"""Real install, dispatch, worker, and erasure checks for the intake spine."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.modules import (
    LoadedSurfaces,
    install_and_enable_module,
    loaded_probe_modules,
)
from rheo_contracts import RecordRef, Role, WorkspaceContext
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.deletion import Disposition
from rheo_core.events import ConsumerRegistry
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.settings import resolver as settings_resolver
from rheo_core.settings.schema import REGISTRY, SettingsRegistry
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.storage.work_tables import event_delivery, outbox_event
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from rheo_leads import MANIFEST
from rheo_leads.configuration import SETTINGS
from rheo_leads.contracts import AcceptedDelivery, ConnectionHealth
from rheo_leads.lifecycle import on_observation_deleted
from rheo_leads.references import ref
from rheo_leads.storage import tables as t
from sqlalchemy import delete, func, inspect, select, update

pytestmark = pytest.mark.postgres


@dataclass
class Intake:
    cluster: ClusterSession
    workspace: UUID
    ctx: WorkspaceContext
    surfaces: LoadedSurfaces
    consumers: ConsumerRegistry
    connection_id: UUID
    funnel_id: UUID

    def call(self, name: str, **payload: Any) -> OperationOutcome:
        return dispatch(
            self.ctx,
            name,
            payload,
            registry=self.surfaces.operations,
            consumers=self.consumers,
        )

    def accept(
        self,
        *,
        source_event_id: str = "fixture-1",
        body: str = '{"subject":"Example inquiry"}',
    ) -> AcceptedDelivery:
        outcome = self.call(
            "leads.intake.accept_delivery",
            connection_ref=ref("intake_connection", self.connection_id),
            source_event_id=source_event_id,
            body=body,
            funnel_ref=ref("funnel", self.funnel_id),
        )
        assert outcome.ok, outcome
        assert isinstance(outcome.result, AcceptedDelivery)
        return outcome.result

    def visit(self) -> None:
        at = datetime.now(UTC) + timedelta(seconds=2)
        visit_workspace(
            DueWorkspace(workspace_id=self.workspace, observed_due_at=None),
            kinds=JobKindRegistry(),
            consumers=self.consumers,
            backend=self.cluster.backend,
            owner="leads-intake-test",
            clock=lambda: at,
            jitter=None,
        )

    def count(self, table: Any) -> int:
        with open_unit_of_work(self.ctx) as uow:
            return int(
                uow.connection.execute(
                    select(func.count()).select_from(table)
                ).scalar_one()
            )


@pytest.fixture
def intake(
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Intake]:
    with loaded_probe_modules(monkeypatch, "leads") as surfaces:
        # The shared loader harness deliberately isolates its registration registry.
        # Resolve against an equally isolated copy of core + the loaded module keys.
        settings = SettingsRegistry()
        for spec in REGISTRY.specs():
            settings.register(spec, origin=REGISTRY.origin_of(spec.key))
        for spec in SETTINGS:
            settings.register(spec, origin="leads")
        monkeypatch.setattr(settings_resolver, "REGISTRY", settings)
        register_core_operations()
        operator = context_for_operator(workspace)
        assert isinstance(operator, WorkspaceContext)
        install_and_enable_module(cluster.backend, operator, workspace, "leads")
        ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(ctx, WorkspaceContext)
        consumers = ConsumerRegistry()
        for subscription in MANIFEST.subscriptions:
            consumers.register(subscription)
        with open_unit_of_work(ctx) as uow:
            connection_id = uow.connection.execute(
                select(t.intake_connection.c.id)
            ).scalar_one()
            funnel_id = uow.connection.execute(select(t.funnel.c.id)).scalar_one()
        yield Intake(
            cluster, workspace, ctx, surfaces, consumers, connection_id, funnel_id
        )


def test_install_manual_capture_worker_and_health(intake: Intake) -> None:
    with open_unit_of_work(intake.ctx) as uow:
        assert set(inspect(uow.connection).get_table_names(schema="leads")) == {
            "alembic_version_leads",
            "funnel",
            "campaign",
            "field_mapping",
            "field_mapping_identity",
            "field_mapping_rule",
            "intake_connection",
            "connection_health",
            "delivery_receipt",
            "delivery_payload",
            "delivery_conflict",
            "observation",
            "observation_field",
            "import_batch",
        }
    for _ in range(2):
        accepted = intake.call(
            "leads.intake.capture",
            funnel_ref=ref("funnel", intake.funnel_id),
            body={"person.email": " SAMPLE@EXAMPLE.COM ", "subject": "Example inquiry"},
        )
        assert accepted.ok, accepted
    assert intake.count(t.delivery_receipt) == 2
    assert intake.count(t.observation) == 0
    intake.visit()
    assert intake.count(t.observation) == 2
    with open_unit_of_work(intake.ctx) as uow:
        observations = uow.connection.execute(select(t.observation)).mappings().all()
        assert all(
            row["party_ref"] is None and row["funnel_id"] == intake.funnel_id
            for row in observations
        )
        assert len({row["source_event_id"] for row in observations}) == 2
        facts = (
            uow.connection.execute(
                select(t.observation_field.c.value_text).where(
                    t.observation_field.c.target == "person.email"
                )
            )
            .scalars()
            .all()
        )
        assert facts == ["sample@example.com"] * 2
        events = (
            uow.connection.execute(
                select(outbox_event.c.data).where(
                    outbox_event.c.type == "leads.observation.accepted"
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 2 and all(
            "party_ref" in e and e["party_ref"] is None for e in events
        )
        assert all("Example inquiry" not in str(e) for e in events)
    health = intake.call(
        "leads.connection.health",
        connection_ref=ref("intake_connection", intake.connection_id),
    )
    assert health.ok and isinstance(health.result, ConnectionHealth)
    assert health.result.last_accepted_at is not None
    assert health.result.last_processed_at is not None
    assert (
        health.result.lag_seconds
        == health.result.unresolved_failures
        == health.result.failed_delivery_count
        == 0
    )


def test_duplicate_conflict_and_erasure_after_owned_row_removal(intake: Intake) -> None:
    first = intake.accept()
    duplicate = intake.accept()
    conflict = intake.accept(body='{"subject":"Different inquiry"}')
    assert (
        first.outcome == "accepted"
        and duplicate.outcome == "duplicate"
        and conflict.outcome == "conflict"
    )
    assert first.receipt_ref == duplicate.receipt_ref == conflict.receipt_ref
    assert (
        intake.count(t.delivery_receipt)
        == intake.count(t.delivery_payload)
        == intake.count(t.delivery_conflict)
        == 1
    )
    intake.visit()
    receipt_id = RecordRef.parse(first.receipt_ref).id
    with open_unit_of_work(intake.ctx) as uow:
        observation_id = uow.connection.execute(select(t.observation.c.id)).scalar_one()
        # The future owner deletes first, exactly as the existing coordinator orders it.
        uow.connection.execute(delete(t.observation_field))
        uow.connection.execute(delete(t.observation))
        on_observation_deleted(
            intake.ctx,
            uow,
            RecordRef.parse(ref("observation", observation_id)),
            disposition=Disposition.USER_ERASURE,
        )
        uow.commit()
    assert intake.accept().outcome == "conflict"
    assert intake.accept(body='{"subject":"Third inquiry"}').outcome == "conflict"
    intake.visit()
    assert intake.count(t.observation) == intake.count(t.delivery_payload) == 0
    with open_unit_of_work(intake.ctx) as uow:
        bodies = (
            uow.connection.execute(select(t.delivery_conflict.c.body)).scalars().all()
        )
        assert bodies == [None, None, None]
        receipt = (
            uow.connection.execute(
                select(t.delivery_receipt).where(t.delivery_receipt.c.id == receipt_id)
            )
            .mappings()
            .one()
        )
        assert receipt["state_detail"] == "observation_deleted"


def test_missing_funnel_foreign_funnel_and_missing_registry_write_nothing(
    intake: Intake, make_workspace: MakeWorkspace
) -> None:
    before = intake.count(t.delivery_receipt)
    assert (
        intake.call("leads.intake.capture", body={"subject": "Example"}).state
        == "input_invalid"
    )
    other = make_workspace()
    assert (
        intake.call(
            "leads.intake.capture", body={}, funnel_ref=ref("funnel", other)
        ).state
        == "not_found"
    )
    missing = dispatch(
        intake.ctx,
        "leads.intake.capture",
        {"body": {}, "funnel_ref": ref("funnel", intake.funnel_id)},
        registry=intake.surfaces.operations,
    )
    assert not missing.ok
    assert intake.count(t.delivery_receipt) == before
    assert intake.count(t.delivery_payload) == 0
    assert intake.count(event_delivery) == 0


@pytest.mark.parametrize("malformed", [False, True])
def test_revocation_or_bad_payload_refuses_without_observation(
    intake: Intake, malformed: bool
) -> None:
    intake.accept(body="not json" if malformed else "{}")
    if not malformed:
        with open_unit_of_work(intake.ctx) as uow:
            uow.connection.execute(update(t.intake_connection).values(state="revoked"))
            uow.commit()
    intake.visit()
    assert intake.count(t.observation) == 0
    with open_unit_of_work(intake.ctx) as uow:
        receipt = uow.connection.execute(select(t.delivery_receipt)).mappings().one()
        assert receipt["state"] == "refused"
        assert receipt["state_detail"] == (
            "payload_invalid" if malformed else "connection_revoked"
        )
        assert (
            uow.connection.execute(
                select(t.connection_health.c.unresolved_failures)
            ).scalar_one()
            == 1
        )


def test_receipt_pins_mapping_and_explicit_clear_is_not_absence(intake: Intake) -> None:
    from sqlalchemy import insert

    with open_unit_of_work(intake.ctx) as uow:
        mapping = uow.connection.execute(select(t.field_mapping)).mappings().one()
        uow.connection.execute(
            update(t.field_mapping_rule)
            .where(t.field_mapping_rule.c.target.in_(["person.name", "person.phone"]))
            .values(clear_on_null=True)
        )
        uow.commit()
    intake.accept(body='{"subject":" Original ","person.name":null}')
    with open_unit_of_work(intake.ctx) as uow:
        uow.connection.execute(
            insert(t.field_mapping).values(**{**mapping, "version": 2})
        )
        uow.connection.execute(update(t.intake_connection).values(mapping_version=2))
        uow.commit()
    intake.visit()
    with open_unit_of_work(intake.ctx) as uow:
        row = uow.connection.execute(select(t.observation)).mappings().one()
        assert row["mapping_version"] == 1 and row["completeness"] == 1
        facts = {
            r["target"]: r
            for r in uow.connection.execute(select(t.observation_field)).mappings()
        }
        assert facts["subject"]["value_text"] == "Original"
        assert facts["person.name"]["value_kind"] == "cleared"
        assert facts["person.name"]["value_text"] is None
        assert "person.phone" not in facts


def test_consumer_failure_rolls_back_observation_and_sanitizes_storage_error(
    intake: Intake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rheo_leads.intake import process
    from sqlalchemy.exc import StatementError

    def fail(*args: object, **kwargs: object) -> None:
        raise StatementError(
            "sensitive sample",
            "SQL",
            {"message": "private sample"},
            ValueError("private sample"),
        )

    intake.accept()
    original_publish = process.publish_event
    monkeypatch.setattr(process, "publish_event", fail)
    intake.visit()
    assert intake.count(t.observation) == intake.count(t.observation_field) == 0
    with open_unit_of_work(intake.ctx) as uow:
        assert (
            uow.connection.execute(select(t.delivery_receipt.c.state)).scalar_one()
            == "pending"
        )
        error = uow.connection.execute(select(event_delivery.c.last_error)).scalar_one()
        assert error == "intake_storage_failed"
        assert (
            uow.connection.execute(
                select(t.connection_health.c.last_processed_at)
            ).scalar_one()
            is None
        )
    monkeypatch.setattr(process, "publish_event", original_publish)
    with open_unit_of_work(intake.ctx) as uow:
        uow.connection.execute(
            update(event_delivery).values(next_attempt_at=datetime.now(UTC))
        )
        uow.commit()
    intake.visit()
    assert intake.count(t.observation) == 1


def test_acceptance_publication_failure_rolls_back_every_write(
    intake: Intake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rheo_leads.intake import accept

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("synthetic outbox failure")

    monkeypatch.setattr(accept, "publish_event", fail)
    result = intake.call(
        "leads.intake.capture",
        body={"subject": "Example inquiry"},
        funnel_ref=ref("funnel", intake.funnel_id),
    )
    assert not result.ok
    for table in (t.delivery_receipt, t.delivery_payload, event_delivery):
        assert intake.count(table) == 0
    with open_unit_of_work(intake.ctx) as uow:
        assert (
            uow.connection.execute(
                select(t.connection_health.c.last_accepted_at)
            ).scalar_one()
            is None
        )


def test_health_counts_terminal_failures_for_only_this_connection(
    intake: Intake,
) -> None:
    from rheo_core.refs import uuid7
    from sqlalchemy import insert

    first = intake.accept()
    now = datetime.now(UTC)
    with open_unit_of_work(intake.ctx) as uow:
        uow.connection.execute(
            update(t.connection_health).values(
                last_accepted_at=now,
                last_processed_at=now - timedelta(seconds=37),
                unresolved_failures=3,
            )
        )
        uow.connection.execute(update(event_delivery).values(state="failed"))
        other = dict(
            uow.connection.execute(select(t.intake_connection)).mappings().one()
        )
        other.update(
            id=uuid7(),
            transport="import",
            source_namespace="fixture-import",
            funnel_id=intake.funnel_id,
        )
        uow.connection.execute(insert(t.intake_connection).values(**other))
        uow.commit()
    result = intake.call(
        "leads.connection.health",
        connection_ref=ref("intake_connection", intake.connection_id),
    )
    assert result.ok and isinstance(result.result, ConnectionHealth)
    assert result.result.failed_delivery_count == 1
    assert result.result.unresolved_failures == 3 and result.result.lag_seconds == 37
    fresh = intake.call(
        "leads.connection.health", connection_ref=ref("intake_connection", other["id"])
    )
    assert fresh.ok and isinstance(fresh.result, ConnectionHealth)
    assert (
        fresh.result.failed_delivery_count
        == fresh.result.unresolved_failures
        == fresh.result.lag_seconds
        == 0
    )
    assert fresh.result.last_accepted_at is fresh.result.last_processed_at is None
    with open_unit_of_work(intake.ctx) as uow:
        uow.connection.execute(
            update(event_delivery)
            .where(event_delivery.c.subject_ref == first.receipt_ref)
            .values(state="pending")
        )
        uow.commit()
    retried = intake.call(
        "leads.connection.health",
        connection_ref=ref("intake_connection", intake.connection_id),
    )
    assert (
        isinstance(retried.result, ConnectionHealth)
        and retried.result.failed_delivery_count == 0
    )


def test_concurrent_duplicate_acceptance_writes_one_receipt(intake: Intake) -> None:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(intake.accept) for _ in range(2)]
        results = [f.result(timeout=20) for f in futures]
    assert sorted(r.outcome for r in results) == ["accepted", "duplicate"]
    assert (
        intake.count(t.delivery_receipt)
        == intake.count(t.delivery_payload)
        == intake.count(event_delivery)
        == 1
    )


def test_tool_dispatch_and_member_health_refusal(
    intake: Intake, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.registry import add_member
    from rheo_core.operations.tool_facade import call_registered_tool

    tool = call_registered_tool(
        intake.ctx,
        "leads_capture",
        {"body": {"subject": "Example"}, "funnel_ref": ref("funnel", intake.funnel_id)},
        consumers=intake.consumers,
        tools=intake.surfaces.tools,
        registry=intake.surfaces.operations,
    )
    assert tool.ok, tool
    member = add_member(
        intake.cluster.backend,
        intake.workspace,
        Role.MEMBER,
        display_name="Sample member",
    )
    ctx = context_for_harness(intake.workspace, member, Role.MEMBER)
    assert isinstance(ctx, WorkspaceContext)
    denied = dispatch(
        ctx,
        "leads.connection.health",
        {"connection_ref": ref("intake_connection", intake.connection_id)},
        registry=intake.surfaces.operations,
    )
    assert denied.state == "role_not_permitted"


def test_payload_bound_and_mapping_failure_are_content_free(intake: Intake) -> None:
    too_large = intake.call(
        "leads.intake.capture",
        body={"message": "x" * 262144},
        funnel_ref=ref("funnel", intake.funnel_id),
    )
    assert (
        too_large.state == "payload_too_large" and intake.count(t.delivery_receipt) == 0
    )
    with open_unit_of_work(intake.ctx) as uow:
        uow.connection.execute(
            update(t.field_mapping_rule)
            .where(t.field_mapping_rule.c.target == "subject")
            .values(required=True)
        )
        uow.commit()
    intake.accept(body='{"message":"Private sample"}')
    intake.visit()
    with open_unit_of_work(intake.ctx) as uow:
        assert (
            uow.connection.execute(
                select(t.delivery_receipt.c.state_detail)
            ).scalar_one()
            == "mapping_failed:subject"
        )
    assert intake.count(t.observation) == 0


def test_export_seed_reconciliation_preserves_erasure_identity(
    intake: Intake, make_workspace: MakeWorkspace
) -> None:
    from types import SimpleNamespace
    from typing import cast

    from rheo_core.exports.artifact import ExportSnapshot, _validated_module_rows
    from rheo_leads.export import export_records, import_records

    intake.accept()
    intake.visit()
    with open_unit_of_work(intake.ctx) as uow:
        rows = export_records(
            cast(ExportSnapshot, SimpleNamespace(connection=uow.connection))
        )
        serialized = _validated_module_rows(MANIFEST, rows, "leads")
    other = make_workspace()
    operator = context_for_operator(other)
    assert isinstance(operator, WorkspaceContext)
    install_and_enable_module(intake.cluster.backend, operator, other, "leads")
    with open_unit_of_work(operator) as uow:
        finish = import_records(uow, serialized)
        finish()
        assert (
            uow.connection.execute(
                select(func.count()).select_from(t.intake_connection)
            ).scalar_one()
            == 1
        )
        row = uow.connection.execute(select(t.observation)).mappings().one()
        assert row["id"] == row["receipt_id"]
        assert row["connection_id"] == intake.connection_id
        assert (
            uow.connection.execute(select(t.funnel.c.id)).scalar_one()
            == intake.funnel_id
        )
        uow.commit()


@pytest.mark.parametrize(
    "generation,overlap,expected",
    [
        (2, False, "processed"),
        (1, True, "processed"),
        (1, False, "refused"),
        (None, True, "refused"),
    ],
)
def test_signing_generation_is_rechecked(
    intake: Intake, generation: int | None, overlap: bool, expected: str
) -> None:
    from rheo_core.refs import uuid7
    from sqlalchemy import insert

    with open_unit_of_work(intake.ctx) as uow:
        connection = dict(
            uow.connection.execute(select(t.intake_connection)).mappings().one()
        )
        connection.update(
            id=uuid7(),
            transport="webhook",
            source_namespace="fixture-webhook",
            funnel_id=intake.funnel_id,
            signing_key_generation=2,
            previous_secret_ref="fixture-secret" if overlap else None,
            previous_valid_until=datetime.now(UTC) + timedelta(minutes=5)
            if overlap
            else None,
        )
        uow.connection.execute(insert(t.intake_connection).values(**connection))
        uow.commit()
    outcome = intake.call(
        "leads.intake.accept_delivery",
        connection_ref=ref("intake_connection", connection["id"]),
        source_event_id="signed-fixture",
        body="{}",
        signing_key_generation=generation,
    )
    assert outcome.ok
    intake.visit()
    with open_unit_of_work(intake.ctx) as uow:
        assert (
            uow.connection.execute(select(t.delivery_receipt.c.state)).scalar_one()
            == expected
        )
    assert intake.count(t.observation) == (1 if expected == "processed" else 0)


def test_campaign_pairing_is_enforced_by_service_and_database(intake: Intake) -> None:
    from rheo_core.refs import uuid7
    from sqlalchemy import insert
    from sqlalchemy.exc import IntegrityError

    other_funnel, campaign = uuid7(), uuid7()
    with open_unit_of_work(intake.ctx) as uow:
        uow.connection.execute(
            insert(t.funnel).values(
                id=other_funnel,
                name="Sample funnel",
                description="Synthetic attribution fixture",
                created_at=datetime.now(UTC),
            )
        )
        uow.connection.execute(
            insert(t.campaign).values(
                id=campaign,
                funnel_id=other_funnel,
                name="Sample campaign",
                created_at=datetime.now(UTC),
            )
        )
        uow.commit()
    refused = intake.call(
        "leads.intake.capture",
        body={},
        funnel_ref=ref("funnel", intake.funnel_id),
        campaign_ref=ref("campaign", campaign),
    )
    assert refused.state == "not_found" and intake.count(t.delivery_receipt) == 0
    with pytest.raises(IntegrityError), open_unit_of_work(intake.ctx) as uow:
        uow.connection.execute(
            update(t.intake_connection).values(
                funnel_id=intake.funnel_id, campaign_id=campaign
            )
        )
    with pytest.raises(IntegrityError), open_unit_of_work(intake.ctx) as uow:
        uow.connection.execute(
            update(t.intake_connection).values(funnel_id=None, campaign_id=campaign)
        )
    accepted = intake.call(
        "leads.intake.capture",
        body={},
        funnel_ref=ref("funnel", other_funnel),
        campaign_ref=ref("campaign", campaign),
    )
    assert accepted.ok
    intake.visit()
    with open_unit_of_work(intake.ctx) as uow:
        row = uow.connection.execute(select(t.observation)).mappings().one()
        assert row["funnel_id"] == other_funnel and row["campaign_id"] == campaign
