"""The two in-memory shapes chunk 03 builds against: ``ConsumerRegistry`` and
``NewEvent``. No database.

Seams: the public methods of :class:`~rheo_core.events.consumers.ConsumerRegistry`
and the declared field list of :class:`~rheo_core.events.publish.NewEvent`.

**Why a field-list assertion is not a tautology here.** ``NewEvent``'s six fields are
a contract, not an implementation detail: the two the model must *not* carry —
``occurred_at`` and ``correlation_id`` — are exactly the two a later reader would
reach for, because both are NOT NULL columns of the row this model produces. A
producer that could supply either would be a producer that could stamp its own
instant or name its own correlation, which is what the publish signature and its
derivation exist to prevent. The assertion pins the absence, which no behavioural
test at this layer can.
"""

from dataclasses import fields

import pytest
from rheo_contracts import EventEnvelope
from rheo_core.events import (
    ConsumerRegistry,
    ConsumerSubscription,
    ConsumerUnknown,
    NewEvent,
)
from rheo_core.storage.backend import HandlerUnitOfWork

WRITTEN = "harness.note.written"
ARCHIVED = "harness.note.archived"


def _handler(uow: HandlerUnitOfWork, envelope: EventEnvelope) -> None:
    """A consumer handler that does nothing; this file never delivers anything."""


def _subscription(
    consumer_id: str,
    *,
    event_type: str = WRITTEN,
    module_id: str = "harness",
    replay_safe: bool = True,
) -> ConsumerSubscription:
    return ConsumerSubscription(
        consumer_id=consumer_id,
        event_type=event_type,
        module_id=module_id,
        replay_safe=replay_safe,
        handler=_handler,
    )


# --- the registry ---------------------------------------------------------------------


def test_for_type_returns_only_that_types_subscriptions_in_order() -> None:
    registry = ConsumerRegistry()
    first = _subscription("harness.first")
    second = _subscription("harness.second")
    other = _subscription("harness.other", event_type=ARCHIVED)
    for subscription in (first, second, other):
        registry.register(subscription)

    assert registry.for_type(WRITTEN) == (first, second)
    assert registry.for_type(ARCHIVED) == (other,)
    assert registry.for_type("harness.note.deleted") == ()


def test_lookup_finds_a_registered_consumer_and_refuses_an_unknown_one() -> None:
    registry = ConsumerRegistry()
    subscription = _subscription("harness.first")
    registry.register(subscription)

    assert registry.lookup("harness.first") is subscription
    with pytest.raises(ConsumerUnknown):
        registry.lookup("harness.missing")


def test_registering_a_consumer_twice_is_last_writer_wins() -> None:
    """The ``JobKindRegistry`` rule, and the case the single dict keeps consistent.

    The second registration moves the consumer to another event type. With a second
    index keyed by event type this is precisely where the two would disagree — the
    consumer would stay visible under its old type — so ``for_type`` is asserted on
    both types, not only the new one.
    """
    registry = ConsumerRegistry()
    registry.register(_subscription("harness.first"))
    moved = _subscription("harness.first", event_type=ARCHIVED)
    registry.register(moved)

    assert registry.lookup("harness.first") is moved
    assert registry.for_type(WRITTEN) == ()
    assert registry.for_type(ARCHIVED) == (moved,)


@pytest.mark.parametrize("field", ["consumer_id", "event_type", "module_id"])
def test_registration_refuses_an_empty_identifier(field: str) -> None:
    """An empty ``module_id`` is refused for the same reason as the two keys.

    It is neither the core segment nor a member of any enabled-module set, so a
    consumer registered with one would be silently unreachable by fan-out rather than
    visibly wrong.
    """
    values = {
        "consumer_id": "harness.first",
        "event_type": WRITTEN,
        "module_id": "harness",
        field: "",
    }
    registry = ConsumerRegistry()
    with pytest.raises(ValueError, match=field):
        registry.register(
            ConsumerSubscription(replay_safe=True, handler=_handler, **values)
        )


def test_a_subscription_carries_replay_safe_even_though_nothing_reads_it() -> None:
    """The ratified three fields are all present, including the one with no reader.

    Nothing this run does consults it; the point of the assertion is that the shape a
    later run needs is already there to be consulted.
    """
    assert _subscription("harness.first", replay_safe=False).replay_safe is False
    assert _subscription("harness.first").replay_safe is True


# --- NewEvent -------------------------------------------------------------------------


def test_new_event_declares_exactly_its_six_fields() -> None:
    assert [field.name for field in fields(NewEvent)] == [
        "type",
        "schema_version",
        "subject_ref",
        "subject_revision",
        "data",
        "causation_id",
    ]


def test_new_event_carries_neither_a_correlation_id_nor_an_instant() -> None:
    """Both are ``publish``'s own concern, and a caller must have no way to set one."""
    names = {field.name for field in fields(NewEvent)}
    assert "correlation_id" not in names
    assert names.isdisjoint({"occurred_at", "now", "next_attempt_at"})

    event = NewEvent(
        type=WRITTEN,
        schema_version=1,
        subject_ref="harness.note:one",
        subject_revision=1,
        data={"body": "one"},
    )
    assert event.causation_id is None
    with pytest.raises(TypeError):
        NewEvent(  # type: ignore[call-arg]
            type=WRITTEN,
            schema_version=1,
            subject_ref="harness.note:one",
            subject_revision=1,
            data={},
            correlation_id=None,
        )
