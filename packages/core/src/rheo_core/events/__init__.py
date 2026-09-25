"""The outbox: the event a producer hands in, the write, the fan-out, and every read
and write against a delivery.

Three modules, split the way ``rheo_core.work`` is: ``consumers`` holds the registry
of who is subscribed, ``publish`` holds the write and its fan-out, and ``deliveries``
holds the repository a deliverer drives.

**Nothing here commits inside the caller's transaction, and nothing here reads the
process clock.** ``publish`` and every delivery write take the caller's connection and
the caller's chosen instant, and leave the commit to it.

``mark_due_after_publish`` is the one deliberate exception to both, and it is a named
function rather than a hole in the rule: no transaction can span a workspace database
and the control database, so it opens and commits its own against the control plane,
and the ``updated_at`` stamped there comes from ``storage.work_index``'s process clock
rather than from its ``at`` argument. Its own docstring carries the reasoning, and
``work.jobs.enqueue`` is the same shape for the same reason.

Nothing here drains a delivery — that loop is the worker's. The single import from this
package into ``rheo_core.work`` is ``deliveries``' use of ``backoff_for``, because one
retry policy governs jobs and deliveries alike and this package is a caller of that
schedule, never a second copy of it.
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
    DeliveryFailureCounts,
    FailedDeliveryRow,
    LeasedDelivery,
    ReplayCounts,
    already_processed,
    delivery_failure_counts,
    delivery_state,
    earliest_delivery_due_at,
    fail_delivery,
    lease_delivery,
    list_failed_deliveries,
    lock_for_replay,
    mark_delivered,
    oldest_retained_position,
    record_processed,
    replay_deliveries,
    requeue_delivery,
    retry_delivery,
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
    "DeliveryFailureCounts",
    "FailedDeliveryRow",
    "LeasedDelivery",
    "NewEvent",
    "ReplayCounts",
    "already_processed",
    "delivery_failure_counts",
    "delivery_state",
    "earliest_delivery_due_at",
    "fail_delivery",
    "lease_delivery",
    "list_failed_deliveries",
    "lock_for_replay",
    "mark_delivered",
    "mark_due_after_publish",
    "oldest_retained_position",
    "publish",
    "record_processed",
    "replay_deliveries",
    "requeue_delivery",
    "retry_delivery",
    "skip_delivery",
]
