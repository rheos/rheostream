"""The outbox: the event a producer hands in, the write, the fan-out, and every read
and write against a delivery.

Three modules, split the way ``rheo_core.work`` is: ``consumers`` holds the registry
of who is subscribed, ``publish`` holds the write and its fan-out, and ``deliveries``
holds the repository a deliverer drives. Nothing here commits, nothing here reads the
process clock, and nothing here knows about the worker — the deliverer is the worker
package's.
"""

from rheo_core.events.consumers import (
    ConsumerHandler,
    ConsumerRegistry,
    ConsumerSubscription,
    ConsumerUnknown,
)
from rheo_core.events.deliveries import (
    DELIVERED,
    FAILED,
    LEASED,
    PENDING,
    SKIPPED,
    FailedDeliveryRow,
    LeasedDelivery,
    already_processed,
    earliest_delivery_due_at,
    fail_delivery,
    lease_delivery,
    list_failed_deliveries,
    mark_delivered,
    record_processed,
    requeue_delivery,
    skip_delivery,
)
from rheo_core.events.publish import NewEvent, mark_due_after_publish, publish

__all__ = [
    "DELIVERED",
    "FAILED",
    "LEASED",
    "PENDING",
    "SKIPPED",
    "ConsumerHandler",
    "ConsumerRegistry",
    "ConsumerSubscription",
    "ConsumerUnknown",
    "FailedDeliveryRow",
    "LeasedDelivery",
    "NewEvent",
    "already_processed",
    "earliest_delivery_due_at",
    "fail_delivery",
    "lease_delivery",
    "list_failed_deliveries",
    "mark_delivered",
    "mark_due_after_publish",
    "publish",
    "record_processed",
    "requeue_delivery",
    "skip_delivery",
]
