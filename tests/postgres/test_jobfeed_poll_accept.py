"""A ``poll`` connection accepts deliveries only from its own connection actor.

Every case dispatches ``leads.intake.accept_delivery`` through the real dispatcher,
except the system-actor case: that context has no grant for the operation, so the
dispatcher would refuse it before the handler, and the handler's own check is
exercised directly the way the trusted processor tests call it.
"""

from typing import Any
from uuid import UUID

import pytest
from rheo_contracts import Entry, Role, WorkspaceContext
from rheo_core.boundary.factories import (
    context_for_connection,
    context_for_event_consumer,
)
from rheo_core.events.consumers import HandlerUnitOfWork
from rheo_core.operations import dispatch
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_tables import event_delivery
from rheo_leads.contracts import AcceptDeliveryInput, AcceptedDelivery
from rheo_leads.intake.accept import accept_delivery
from rheo_leads.references import ref
from rheo_leads.storage import tables as t

from postgres.test_jobfeed_migration import connector
from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake

pytestmark = pytest.mark.postgres


def payload(connection_id: UUID, event: str) -> dict[str, Any]:
    return {
        "connection_ref": ref("intake_connection", connection_id),
        "source_event_id": event,
        "body": '{"subject":"Example posting"}',
    }


def poll_context(i: Intake, actor_connection: UUID) -> WorkspaceContext:
    ctx = context_for_connection(i.workspace, actor_connection, "leads", "poll")
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def send(i: Intake, ctx: WorkspaceContext, body: dict[str, Any]) -> OperationOutcome:
    return dispatch(
        ctx,
        "leads.intake.accept_delivery",
        body,
        registry=i.surfaces.operations,
        consumers=i.consumers,
    )


def written(i: Intake) -> int:
    counts = {
        i.count(t.delivery_receipt),
        i.count(t.delivery_payload),
        i.count(event_delivery),
    }
    assert len(counts) == 1, counts
    return counts.pop()


def test_poll_connection_context_grants_only_acceptance(intake: Intake) -> None:
    poll = connector(intake, "poll", "fixture-poll")
    ctx = poll_context(intake, poll)
    assert ctx.operation_set == frozenset({"leads.intake.accept_delivery"})
    assert ctx.role is Role.SERVICE and ctx.entry is Entry.INTAKE
    assert ctx.principal.account_id is None


def test_poll_delivery_is_accepted_only_for_its_own_connection_actor(
    intake: Intake,
) -> None:
    """The guard's entry and actor-kind conditions are defence in depth for
    contexts no boundary factory can build, so only the id is exercised here."""
    poll = connector(intake, "poll", "fixture-poll")
    other_poll = connector(intake, "poll", "fixture-poll-other")

    another_actor = send(intake, poll_context(intake, other_poll), payload(poll, "a"))
    assert another_actor.state == "role_not_permitted", another_actor
    unknown_actor = send(intake, poll_context(intake, uuid7()), payload(poll, "b"))
    assert unknown_actor.state == "role_not_permitted", unknown_actor
    # The trusted system service context an import delivery accepts is not enough.
    with open_unit_of_work(intake.ctx) as uow:
        system = context_for_event_consumer(intake.workspace, uow, request_id=uuid7())
        assert isinstance(system, WorkspaceContext) and system.role is Role.SERVICE
        with pytest.raises(OperationRefused) as raised:
            accept_delivery(
                system,
                HandlerUnitOfWork(uow, consumers=intake.consumers),
                AcceptDeliveryInput(**payload(poll, "d")),
            )
        assert raised.value.state == "role_not_permitted"
    # An owner account is refused too.
    assert intake.call("leads.intake.accept_delivery", **payload(poll, "e")).state == (
        "role_not_permitted"
    )
    assert written(intake) == 0

    accepted = send(intake, poll_context(intake, poll), payload(poll, "own"))
    assert accepted.ok, accepted
    assert isinstance(accepted.result, AcceptedDelivery)
    assert accepted.result.outcome == "accepted"
    assert written(intake) == 1
    # The other connection's own actor is accepted for its own deliveries.
    own_other = send(
        intake, poll_context(intake, other_poll), payload(other_poll, "own")
    )
    assert own_other.ok and own_other.result.outcome == "accepted", own_other
    assert written(intake) == 2
    with open_unit_of_work(intake.ctx) as uow:
        receipts = uow.connection.execute(
            t.delivery_receipt.select().order_by(t.delivery_receipt.c.connection_id)
        ).mappings()
        assert {(r["connection_id"], r["transport"]) for r in receipts} == {
            (poll, "poll"),
            (other_poll, "poll"),
        }
