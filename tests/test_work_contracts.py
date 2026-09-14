"""The two durable-work contract seams: the event envelope and the repository shape.

Both modules were bare docstring stubs until this run filled them, and both are read by
runs that do not exist yet — the envelope by whatever delivers events, the protocol by
the first module to own a mutable record type. Nothing in release one consumes either,
so this file is the only thing standing between a wrong shape and a run that inherits
it. Every assertion here is therefore against the ratified text rather than against a
caller: the envelope's field set comes from ``intake-and-events.md``'s ``outbox_event``
row plus its delivery context, and ``save``'s signature from
``storage-and-workspaces.md``'s compare-and-set rule.

The tests import the real names and construct real instances on purpose. An
import-only smoke test would pass against a module that re-exported nothing, and both
of these modules shipped for two runs as exactly that.
"""

import inspect
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import get_type_hints
from uuid import UUID

import pytest
from pydantic import ValidationError
from rheo_contracts import EventEnvelope, Repository, StaleRecord

_EVENT_ID = UUID("018f6b30-0000-7000-8000-000000000001")
_CORRELATION_ID = UUID("018f6b30-0000-7000-8000-000000000002")
_CAUSATION_ID = UUID("018f6b30-0000-7000-8000-000000000003")
_ACTOR_ID = UUID("018f6b30-0000-7000-8000-000000000004")

_RATIFIED_OUTBOX_COLUMNS = {
    "id",
    "position",
    "type",
    "schema_version",
    "source",
    "subject_ref",
    "subject_revision",
    "correlation_id",
    "causation_id",
    "actor_kind",
    "actor_id",
    "occurred_at",
    "data",
}
"""``core.outbox_event``'s columns, transcribed from ``intake-and-events.md``."""

_DELIVERY_CONTEXT = {"consumer_id", "attempt"}
"""What the envelope carries beyond the row: which consumer, which attempt."""


def _envelope(**overrides: object) -> EventEnvelope:
    fields: dict[str, object] = {
        "id": _EVENT_ID,
        "position": 41,
        "type": "leads.opportunity.created",
        "schema_version": 1,
        "source": "urn:rheo:module:leads",
        "subject_ref": "leads.opportunity:018f6b2e-8c1a-7d3e-9a4b-1c2d3e4f5a6b",
        "subject_revision": 3,
        "correlation_id": _CORRELATION_ID,
        "causation_id": _CAUSATION_ID,
        "actor_kind": "account",
        "actor_id": _ACTOR_ID,
        "occurred_at": datetime(2026, 9, 13, 18, 30, tzinfo=UTC),
        "data": {"opportunity_id": str(_EVENT_ID), "title": "A lead"},
        "consumer_id": "recallatron.opportunity_indexer",
        "attempt": 2,
    }
    fields.update(overrides)
    return EventEnvelope(**fields)  # type: ignore[arg-type]


def test_event_envelope_declares_the_ratified_row_plus_delivery_context() -> None:
    assert (
        set(EventEnvelope.model_fields) == _RATIFIED_OUTBOX_COLUMNS | _DELIVERY_CONTEXT
    )


def test_event_envelope_carries_the_values_it_was_given() -> None:
    """Construction, not import: this is what a bare-docstring module fails."""
    envelope = _envelope()
    assert envelope.id == _EVENT_ID
    assert envelope.position == 41
    assert envelope.type == "leads.opportunity.created"
    assert envelope.schema_version == 1
    assert envelope.source == "urn:rheo:module:leads"
    assert envelope.subject_ref.startswith("leads.opportunity:")
    assert envelope.subject_revision == 3
    assert envelope.correlation_id == _CORRELATION_ID
    assert envelope.causation_id == _CAUSATION_ID
    assert envelope.actor_kind == "account"
    assert envelope.actor_id == _ACTOR_ID
    assert envelope.occurred_at == datetime(2026, 9, 13, 18, 30, tzinfo=UTC)
    assert envelope.data == {"opportunity_id": str(_EVENT_ID), "title": "A lead"}
    assert envelope.consumer_id == "recallatron.opportunity_indexer"
    assert envelope.attempt == 2


def test_event_envelope_is_frozen() -> None:
    envelope = _envelope()
    with pytest.raises(ValidationError):
        envelope.attempt = 3


def test_only_the_two_nullable_columns_are_optional() -> None:
    """The document marks exactly ``causation_id`` and ``actor_id`` null.

    Everything else is required, so a producer that forgets one is refused at
    construction rather than delivering an envelope with a silent default in it.
    """
    fields = EventEnvelope.model_fields
    for name in ("causation_id", "actor_id"):
        assert fields[name].is_required() is False, name
        assert fields[name].default is None, name
    for name in sorted(set(fields) - {"causation_id", "actor_id"}):
        assert fields[name].is_required() is True, name


def test_event_envelope_refuses_a_missing_required_field() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(  # type: ignore[call-arg]
            id=_EVENT_ID,
            position=1,
            type="leads.opportunity.created",
            schema_version=1,
            source="urn:rheo:module:leads",
        )


def test_stale_record_is_a_constructible_exception() -> None:
    assert issubclass(StaleRecord, Exception)
    raised = StaleRecord("opportunity 3 is no longer 3")
    assert isinstance(raised, Exception)
    assert str(raised) == "opportunity 3 is no longer 3"


def test_repository_save_takes_a_keyword_only_expected_revision() -> None:
    """The signature is the contract: the protocol has no implementation to test.

    A ``save(record)`` with no revision parameter, or one that is positional, or one
    carrying a default, would each let a module write without naming what it read —
    which is the lost update ``RecordStateGuard`` then compares against.
    """
    parameters = inspect.signature(Repository.save).parameters
    assert list(parameters) == ["self", "record", "expected_revision"]
    expected = parameters["expected_revision"]
    assert expected.kind is inspect.Parameter.KEYWORD_ONLY
    assert expected.default is inspect.Parameter.empty
    assert get_type_hints(Repository.save)["expected_revision"] == int | None


def test_repository_declares_get_and_list_beside_save() -> None:
    get_parameters = inspect.signature(Repository.get).parameters
    assert list(get_parameters) == ["self", "record_id"]
    assert get_type_hints(Repository.get)["record_id"] is UUID
    list_parameters = inspect.signature(Repository.list).parameters
    assert list_parameters["filter"].kind is inspect.Parameter.VAR_KEYWORD


class _Note:
    """A record with a revision, standing in for a type release one does not have."""

    def __init__(self, note_id: UUID, body: str, revision: int) -> None:
        self.id = note_id
        self.body = body
        self.revision = revision


class _NoteRepository:
    """The smallest thing that satisfies :class:`Repository` for ``_Note``.

    Its ``save`` is the compare-and-set the protocol's docstring describes, written
    against a dict instead of SQL: it refuses unless the caller names the stored
    revision, and it increments in the same step it compares. It exists to prove the
    protocol is implementable as specified and that ``StaleRecord`` is the refusal a
    conforming implementation raises — not to be a reusable double.
    """

    def __init__(self, rows: dict[UUID, _Note]) -> None:
        self._rows = rows

    def get(self, record_id: UUID) -> _Note | None:
        return self._rows.get(record_id)

    def list(self, **filter: object) -> Sequence[_Note]:
        body = filter.get("body")
        return [row for row in self._rows.values() if body in (None, row.body)]

    def save(self, record: _Note, *, expected_revision: int | None) -> _Note:
        stored = self._rows.get(record.id)
        if stored is None or stored.revision != expected_revision:
            raise StaleRecord(f"{record.id} is not at revision {expected_revision}")
        record.revision = stored.revision + 1
        self._rows[record.id] = record
        return record


_PROTOCOL_CHECK: type[Repository[_Note]] = _NoteRepository
"""``_NoteRepository`` conforms structurally, mirroring ``harness.identity``'s own
protocol-conformance line."""


def _one_note() -> tuple[_NoteRepository, _Note]:
    note = _Note(_EVENT_ID, "first", 1)
    return _NoteRepository({note.id: note}), note


def test_a_conforming_save_writes_when_the_revision_is_the_one_read() -> None:
    repository, note = _one_note()
    note.body = "second"
    saved = repository.save(note, expected_revision=1)
    assert saved.revision == 2
    fetched = repository.get(note.id)
    assert fetched is not None
    assert fetched.body == "second"
    assert [row.body for row in repository.list()] == ["second"]


def test_a_conforming_save_refuses_a_stale_revision_with_stale_record() -> None:
    """The second of two writers that both read revision 1 is refused, not discarded.

    This is the lost update the compare-and-set exists to stop: under a bare
    ``revision = revision + 1`` both writes would succeed and the record would land at
    2 either way, with the approval guard comparing equal against a state its approver
    never saw.
    """
    repository, note = _one_note()
    first = _Note(note.id, "writer one", 1)
    repository.save(first, expected_revision=1)

    second = _Note(note.id, "writer two", 1)
    with pytest.raises(StaleRecord):
        repository.save(second, expected_revision=1)

    survivor = repository.get(note.id)
    assert survivor is not None
    assert survivor.body == "writer one"
    assert survivor.revision == 2
