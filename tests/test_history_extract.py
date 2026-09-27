"""The history extract is bounded, replay-identifiable, and content-safe on refusal."""

from datetime import UTC, datetime

import pytest
from rheo_recallatron.migration.history_extract import (
    HistoryExtractRefusal,
    HistoryExtractUnit,
    dump_history_extract,
    parse_history_extract,
)

AT = datetime(2026, 9, 1, tzinfo=UTC)
CHECKSUM = "a" * 64


def test_round_trip_preserves_history_metadata() -> None:
    units = [
        HistoryExtractUnit(
            external_source_key="store.conversation:1",
            source_reference="store.conversation:1",
            kind="conversation",
            status="turn",
            title="Conversation turn",
            body="A synthetic example about gardens.",
            occurred_at=AT,
            source_role="user",
            session_key="session-1",
        ),
        HistoryExtractUnit(
            external_source_key="store.procedural_note:2",
            kind="procedural_note",
            status="unconfirmed",
            title="Procedural note",
            body="A synthetic unconfirmed method.",
            occurred_at=AT,
            source_category="sample",
        ),
        HistoryExtractUnit(
            external_source_key="graph.op:line:3",
            kind="graph_event",
            status="event",
            title="Graph relation event",
            body="Synthetic relation evidence.",
            occurred_at=AT,
            source_category="relate",
        ),
    ]
    lines = dump_history_extract(units, source_sha256=CHECKSUM, unresolved_count=0)
    parsed = parse_history_extract(lines)
    assert parsed.header.unit_count == 3
    assert parsed.units == tuple(units)


def test_duplicate_source_key_is_refused() -> None:
    unit = HistoryExtractUnit(
        external_source_key="store.conversation:1",
        kind="conversation",
        status="turn",
        title="Turn",
        body="Synthetic body.",
        occurred_at=AT,
    )
    with pytest.raises(HistoryExtractRefusal, match="duplicate source key"):
        parse_history_extract(
            dump_history_extract(
                [unit, unit], source_sha256=CHECKSUM, unresolved_count=0
            )
        )


def test_refusal_does_not_echo_private_values() -> None:
    lines = dump_history_extract([], source_sha256=CHECKSUM, unresolved_count=0)
    lines.append('{"body":"private-example-value","extra":"private-example-value"}')
    with pytest.raises(HistoryExtractRefusal) as error:
        parse_history_extract(lines)
    assert "private-example-value" not in str(error.value)
