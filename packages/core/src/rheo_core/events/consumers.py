"""The consumer registry: who is subscribed to an event type, and as what.

Shaped on ``rheo_core.work.kinds.JobKindRegistry`` (``kinds.py:55-82``): a plain
class with **no module-level instance**. The fan-out takes a
:class:`ConsumerRegistry` as a parameter, so a test builds its own and there is no
global to reset between cases. Production's one process-wide instance is built in
the worker's composition root beside ``JOB_KINDS``, not here.

**``module_id`` is a field of the subscription rather than something the fan-out
looks up.** The ratified ``Subscription(event_type, consumer_id, replay_safe)``
(``docs/architecture/module-contract.md:37``) is a module's own declaration, so the
owning module is implicit there; a registry that holds subscriptions from every
module has to carry it, because fan-out tests "belongs to a module enabled in the
workspace" against it (``intake-and-events.md:305``).

**``replay_safe`` has one reader: ``core.work.replay``** (``work/operations.py``),
which refuses ``replay_unsafe`` for a subscription that declares ``False``. The fan-out
and the worker ignore it, because a first delivery is never a replay.
"""

from collections.abc import Callable
from dataclasses import dataclass

from rheo_contracts import EventEnvelope

# Re-exported explicitly (``X as X``), for the same contract reason
# ``refs/resolver.py`` re-exports ``UnitOfWork``: a module distribution may not import
# the core's storage package — ``tests/postgres/test_module_storage_ownership.py``
# scans ``modules/`` for exactly that — and a module handler that publishes has to
# *name* this type to read the registry off the sealed view ``dispatch()`` handed it.
# The guard ``operations/dispatch.py`` prescribes is ``uow.consumers if isinstance(uow,
# HandlerUnitOfWork) else None``, and without this line a module could not write it.
# So the package that declares the publish contract publishes the names in it, and
# nothing else from the storage package is published here.
from rheo_core.storage.backend import HandlerUnitOfWork as HandlerUnitOfWork

ConsumerHandler = Callable[[HandlerUnitOfWork, EventEnvelope], None]
"""``(uow, envelope) -> None``: what one consumer actually does with one delivery.

Two parameters and **no clock**, mirroring ``work.kinds.JobHandler``. ``uow`` is the
**sealed** view, :class:`~rheo_core.storage.backend.HandlerUnitOfWork`, because the
handler's own effects, the dedup ledger row and the delivery's ``delivered`` write
commit together in the deliverer's one transaction — a handler calling ``commit()``
is precisely what would separate them. The envelope carries the attempt number, so a
side effect a consumer cannot make idempotent is at least distinguishable from a
first run.
"""


class ConsumerUnknown(Exception):
    """Raised by :meth:`ConsumerRegistry.lookup` for an unregistered consumer."""


@dataclass(frozen=True, slots=True)
class ConsumerSubscription:
    """One consumer's subscription to one event type.

    The ratified three fields plus the ``module_id`` fan-out needs and the handler
    the deliverer runs. Frozen: a registration is a declaration already made.
    """

    consumer_id: str
    event_type: str
    module_id: str
    replay_safe: bool
    handler: ConsumerHandler


class ConsumerRegistry:
    """``consumer_id -> subscription``, and nothing else.

    **One dict keyed by ``consumer_id``, with :meth:`for_type` filtering it**, rather
    than a second index keyed by event type. A consumer is registered once and the
    set is small; a second index would be a second thing to keep in step with the
    first, and re-registering a consumer under a different event type is exactly the
    case that would leave the two disagreeing.

    Registration is last-writer-wins rather than a refusal, for the reason
    ``JobKindRegistry`` gives: the one production instance is populated once at
    import of the composition root, and a test that re-registers a consumer inside a
    single case is expressing intent rather than colliding with another caller.
    """

    __slots__ = ("_consumers",)

    def __init__(self) -> None:
        self._consumers: dict[str, ConsumerSubscription] = {}

    def register(self, subscription: ConsumerSubscription) -> None:
        """Declare ``subscription``, replacing any earlier one for its consumer.

        All three identifiers are rejected empty. ``consumer_id`` and ``event_type``
        are the two this registry is read by; ``module_id`` is checked for the same
        reason, one level down — an empty one is neither the core segment nor a
        member of any ``enabled_modules``, so the consumer would register cleanly and
        then silently never receive a delivery.
        """
        for label, value in (
            ("consumer_id", subscription.consumer_id),
            ("event_type", subscription.event_type),
            ("module_id", subscription.module_id),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"a subscription's {label} is a non-empty string")
        self._consumers[subscription.consumer_id] = subscription

    def for_type(self, event_type: str) -> tuple[ConsumerSubscription, ...]:
        """Every subscription to ``event_type``, in registration order.

        The enabled-module test is **not** applied here: this registry knows nothing
        about any workspace, and the set of enabled modules is a property of the
        context the fan-out holds.
        """
        return tuple(
            subscription
            for subscription in self._consumers.values()
            if subscription.event_type == event_type
        )

    def lookup(self, consumer_id: str) -> ConsumerSubscription:
        """The subscription for ``consumer_id``, or :class:`ConsumerUnknown`.

        A delivery row names a consumer that was subscribed when the event was
        published; a deliverer that finds no subscription is looking at work for a
        consumer that has since gone away, which is a decision for the caller and not
        a silent skip here.
        """
        found = self._consumers.get(consumer_id)
        if found is None:
            raise ConsumerUnknown(f"no subscription is registered for {consumer_id!r}")
        return found
