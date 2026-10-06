"""Content-free event publication inside the caller's transaction."""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel
from rheo_contracts import WorkspaceContext
from rheo_core.events import NewEvent, publish
from rheo_core.events.consumers import HandlerUnitOfWork
from rheo_core.modules.manifest import EventDeclaration
from rheo_core.operations import CONSUMERS_MISSING
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs.resolver import UnitOfWork

from rheo_leads.contracts import DeliveryReceived, ObservationAccepted
from rheo_leads.pipeline_contracts import ReferenceEvent

DELIVERY_RECEIVED = "leads.delivery.received"
OBSERVATION_ACCEPTED = "leads.observation.accepted"
CONSUMER_ID = "leads.process_delivery"
EVENTS = (
    *(
        EventDeclaration(type="leads." + name, schema_version=1, data=ReferenceEvent)
        for name in (
            "opportunity.created",
            "opportunity.transitioned",
            "opportunity.qualified",
            "opportunity.migrated",
            "handoff.requested",
            "contact_permission.withdrawn",
            "contact_permission.suppressed",
        )
    ),
    EventDeclaration(type=DELIVERY_RECEIVED, schema_version=1, data=DeliveryReceived),
    EventDeclaration(
        type=OBSERVATION_ACCEPTED, schema_version=1, data=ObservationAccepted
    ),
)


def publish_event(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    *,
    event_type: str,
    subject: str,
    data: BaseModel,
    causation_id: UUID | None = None,
) -> None:
    if not isinstance(uow, HandlerUnitOfWork) or uow.consumers is None:
        raise OperationRefused(CONSUMERS_MISSING, "consumer registry is required")
    publish(
        ctx,
        uow,
        NewEvent(
            type=event_type,
            schema_version=1,
            subject_ref=subject,
            subject_revision=1,
            data=data.model_dump(mode="json"),
            causation_id=causation_id,
        ),
        now=datetime.now(UTC),
        consumers=uow.consumers,
    )
