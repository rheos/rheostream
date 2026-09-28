"""Digestion and structured-output validation (spec Architecture, System Components
item 7; FR 6, AC 3).

Every batch here is built from synthetic units that carry identity, audience and
purpose, and every test that builds one asserts on the constructed batch that none of
those reached it: the model sees an ordinal and a body, nothing else.
"""

import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

import pytest
from rheo_contracts.source_units import SanitizedEvidence, SourceMention
from rheo_core.evidence.extract import (
    NOOP_EVIDENCE,
    DigestBatch,
    DigestItem,
    ExtractionOutputInvalid,
    digest,
    validate_extraction,
)
from rheo_core.evidence.providers import FAKE_EXTRACTION_MARKER, FakeExtractionProvider


@dataclass(frozen=True)
class _SyntheticUnit:
    """What a claimed unit carries that the model must never see, beside its body."""

    native_key: str
    speaker_id: str
    audience: str
    purpose: str
    body: str


def _units(*bodies: str) -> tuple[_SyntheticUnit, ...]:
    return tuple(
        _SyntheticUnit(
            native_key=f"turn-{ordinal}-7f3a",
            speaker_id=f"account-{ordinal}-example",
            audience=f"member-{ordinal}@example.org",
            purpose="internal_analysis",
            body=body,
        )
        for ordinal, body in enumerate(bodies, start=1)
    )


def _values(value: object) -> list[object]:
    """Every attribute name and value reachable from a digest batch."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        found: list[object] = []
        for field in dataclasses.fields(value):
            found.append(field.name)
            found.extend(_values(getattr(value, field.name)))
        return found
    if isinstance(value, tuple | list):
        return [nested for element in value for nested in _values(element)]
    return [value]


def _assert_model_sees_only_ordinals_and_text(
    batch: DigestBatch, units: tuple[_SyntheticUnit, ...]
) -> None:
    hidden = {
        hidden_value
        for unit in units
        for hidden_value in (
            unit.native_key,
            unit.speaker_id,
            unit.audience,
            unit.purpose,
        )
    }
    reachable = _values(batch)
    assert not hidden & set(reachable)
    assert [field.name for field in dataclasses.fields(DigestBatch)] == ["items"]
    for item in batch.items:
        assert [field.name for field in dataclasses.fields(item)] == ["item_id", "text"]
        assert not hasattr(item, "__dict__")


def _batch(*bodies: str) -> DigestBatch:
    units = _units(*bodies)
    batch = digest([unit.body for unit in units])
    _assert_model_sees_only_ordinals_and_text(batch, units)
    return batch


def _memory(**overrides: object) -> dict[str, object]:
    memory: dict[str, object] = {
        "kind": "decision",
        "title": "Releases move to Thursday",
        "body": "The team agreed releases move to Thursday afternoons.",
        "confidence": 0.8,
    }
    memory.update(overrides)
    return memory


# --- digestion -----------------------------------------------------------------------


def test_digestion_numbers_items_in_claim_order_and_carries_only_text() -> None:
    units = _units("first body", "second body", "third body")
    batch = digest([unit.body for unit in units])
    _assert_model_sees_only_ordinals_and_text(batch, units)
    assert batch == DigestBatch(
        items=(
            DigestItem("u1", "first body"),
            DigestItem("u2", "second body"),
            DigestItem("u3", "third body"),
        )
    )


def test_digestion_is_deterministic() -> None:
    assert _batch("a", "b") == _batch("a", "b")


# --- a well-formed response --------------------------------------------------------


def test_a_well_formed_single_item_response_yields_that_candidate_exactly() -> None:
    batch = _batch("We decided to ship on Thursdays.")
    memory = _memory(
        occurred_at="2026-09-01T15:30:00+00:00",
        mentions=[{"kind": "person", "name": "Avery Example"}],
    )
    result = validate_extraction(batch, {"items": [{"item": "u1", "memory": memory}]})
    assert result == {
        "u1": SanitizedEvidence(
            kind="decision",
            title="Releases move to Thursday",
            body="The team agreed releases move to Thursday afternoons.",
            confidence=0.8,
            occurred_at=datetime(2026, 9, 1, 15, 30, tzinfo=UTC),
            mentions=(SourceMention(kind="person", name="Avery Example"),),
        )
    }


def test_every_batch_item_gets_exactly_one_entry() -> None:
    batch = _batch("one", "two", "three")
    result = validate_extraction(
        batch,
        {
            "items": [
                {"item": "u2", "memory": _memory()},
                {"item": "u1", "memory": None},
            ]
        },
    )
    assert list(result) == ["u1", "u2", "u3"]
    assert result["u1"] is NOOP_EVIDENCE
    assert result["u2"] != NOOP_EVIDENCE
    assert result["u3"] is NOOP_EVIDENCE


def test_the_fake_provider_round_trips_through_the_validator() -> None:
    marked = f"{FAKE_EXTRACTION_MARKER} The team prefers Thursday releases."
    batch = _batch(marked, "nothing to remember here", f"{FAKE_EXTRACTION_MARKER} two")
    fake = FakeExtractionProvider()
    result = validate_extraction(batch, fake.extract(batch))
    assert fake.calls == [batch]
    assert result["u1"] == SanitizedEvidence(
        kind="note", title=marked, body=marked, confidence=1.0
    )
    assert result["u2"] is NOOP_EVIDENCE
    assert result["u3"].body == f"{FAKE_EXTRACTION_MARKER} two"


# --- every "no candidate" path converges on the sentinel ---------------------------


def test_the_sentinel_is_valid_evidence_that_the_explicit_gate_refuses() -> None:
    # 1a1's gate (rheo_recallatron/source_units.py, _evidence_is_explicit) passes only
    # a unit whose title and body both survive strip(); the sentinel has neither.
    assert SanitizedEvidence.model_validate(NOOP_EVIDENCE.model_dump()) == NOOP_EVIDENCE
    assert not NOOP_EVIDENCE.title.strip()
    assert not NOOP_EVIDENCE.body.strip()


def test_a_null_memory_is_no_candidate() -> None:
    batch = _batch("small talk")
    result = validate_extraction(batch, {"items": [{"item": "u1", "memory": None}]})
    assert result == {"u1": NOOP_EVIDENCE}


def test_an_unknown_item_id_is_ignored_and_gives_the_real_item_no_candidate() -> None:
    batch = _batch("a body")
    result = validate_extraction(
        batch, {"items": [{"item": "u9", "memory": _memory()}]}
    )
    assert result == {"u1": NOOP_EVIDENCE}


def test_an_unknown_item_id_does_not_disturb_a_known_one() -> None:
    batch = _batch("a body")
    result = validate_extraction(
        batch,
        {
            "items": [
                {"item": "u0", "memory": _memory(title="Stray")},
                {"item": "u1", "memory": _memory()},
            ]
        },
    )
    assert result["u1"].title == "Releases move to Thursday"


def test_a_duplicate_item_id_is_ambiguous_and_no_candidate() -> None:
    batch = _batch("a body")
    result = validate_extraction(
        batch,
        {
            "items": [
                {"item": "u1", "memory": _memory()},
                {"item": "u1", "memory": _memory()},
            ]
        },
    )
    assert result == {"u1": NOOP_EVIDENCE}


def test_a_duplicate_does_not_spill_onto_other_items() -> None:
    batch = _batch("a body", "b body")
    result = validate_extraction(
        batch,
        {
            "items": [
                {"item": "u1", "memory": _memory()},
                {"item": "u1", "memory": None},
                {"item": "u2", "memory": _memory(title="Second")},
            ]
        },
    )
    assert result["u1"] is NOOP_EVIDENCE
    assert result["u2"].title == "Second"


def test_a_missing_item_is_no_candidate() -> None:
    batch = _batch("a body", "b body")
    result = validate_extraction(
        batch, {"items": [{"item": "u2", "memory": _memory()}]}
    )
    assert result["u1"] is NOOP_EVIDENCE


def test_an_empty_items_list_is_no_candidate_for_every_item() -> None:
    batch = _batch("a body", "b body")
    assert validate_extraction(batch, {"items": []}) == {
        "u1": NOOP_EVIDENCE,
        "u2": NOOP_EVIDENCE,
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"links": [{"ref": "recallatron:memory:1", "relation": "about"}]},
        {"purposes": ["internal_analysis"]},
        {"audience": "workspace"},
    ],
    ids=["links", "purposes", "audience"],
)
def test_a_field_beyond_the_six_is_no_candidate(extra: dict[str, object]) -> None:
    batch = _batch("a body")
    result = validate_extraction(
        batch, {"items": [{"item": "u1", "memory": _memory(**extra)}]}
    )
    assert result == {"u1": NOOP_EVIDENCE}


@pytest.mark.parametrize(
    "override",
    [
        {"kind": "topic_thread"},
        {"kind": "procedural_notes"},
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 201},
        {"body": ""},
        {"body": " \n "},
        {"confidence": 1.5},
        {"confidence": -0.1},
        {"title": 7},
        {"occurred_at": "not a time"},
        {"mentions": [{"kind": "person"}]},
    ],
    ids=[
        "kind-topic-thread",
        "kind-procedural-notes",
        "empty-title",
        "blank-title",
        "oversize-title",
        "empty-body",
        "blank-body",
        "confidence-high",
        "confidence-low",
        "non-string-title",
        "bad-occurred-at",
        "mention-missing-name",
    ],
)
def test_a_memory_failing_field_validation_is_no_candidate(
    override: dict[str, object],
) -> None:
    batch = _batch("a body")
    result = validate_extraction(
        batch, {"items": [{"item": "u1", "memory": _memory(**override)}]}
    )
    assert result == {"u1": NOOP_EVIDENCE}


@pytest.mark.parametrize(
    "memory",
    ["remember this", ["kind", "note"], 42, {}],
    ids=["string", "list", "number", "empty-object"],
)
def test_a_memory_that_is_not_a_memory_object_is_no_candidate(memory: object) -> None:
    batch = _batch("a body")
    result = validate_extraction(batch, {"items": [{"item": "u1", "memory": memory}]})
    assert result == {"u1": NOOP_EVIDENCE}


@pytest.mark.parametrize(
    "entry",
    [
        "u1",
        {"memory": {"kind": "note", "title": "t", "body": "b"}},
        {"item": 1, "memory": {"kind": "note", "title": "t", "body": "b"}},
    ],
    ids=["bare-string", "no-item-id", "non-string-item-id"],
)
def test_an_unattributable_entry_is_ignored(entry: object) -> None:
    batch = _batch("a body")
    assert validate_extraction(batch, {"items": [entry]}) == {"u1": NOOP_EVIDENCE}


def test_a_proposed_mention_with_a_backing_ref_is_no_candidate_for_that_item() -> None:
    """FR 6: model text selects no trusted identity. A backing ref is a visibility
    route to the entity, so a model may not propose one."""
    batch = _batch("a body", "b body")
    result = validate_extraction(
        batch,
        {
            "items": [
                {
                    "item": "u1",
                    "memory": _memory(
                        mentions=[
                            {
                                "kind": "person",
                                "name": "Avery Example",
                                "backing_ref": "rheo://example/record/1",
                            }
                        ]
                    ),
                },
                {"item": "u2", "memory": _memory(title="Second")},
            ]
        },
    )
    assert result["u1"] is NOOP_EVIDENCE
    assert result["u2"].title == "Second"


def test_a_proposed_mention_with_an_unknown_field_is_no_candidate() -> None:
    batch = _batch("a body")
    mention = {"kind": "person", "name": "Avery Example", "entity_id": "e-1"}
    result = validate_extraction(
        batch, {"items": [{"item": "u1", "memory": _memory(mentions=[mention])}]}
    )
    assert result == {"u1": NOOP_EVIDENCE}


def test_a_proposed_mention_keeps_kind_name_and_role_and_no_backing_ref() -> None:
    batch = _batch("a body")
    mention = {"kind": "person", "name": "Avery Example", "role": "owner"}
    result = validate_extraction(
        batch, {"items": [{"item": "u1", "memory": _memory(mentions=[mention])}]}
    )
    assert result["u1"].mentions == (
        SourceMention(kind="person", name="Avery Example", role="owner"),
    )
    assert result["u1"].mentions[0].backing_ref is None


def test_an_entry_with_a_key_beyond_item_and_memory_is_no_candidate() -> None:
    batch = _batch("a body")
    result = validate_extraction(
        batch,
        {"items": [{"item": "u1", "memory": _memory(), "purpose": "marketing"}]},
    )
    assert result == {"u1": NOOP_EVIDENCE}


# --- a malformed whole response raises, never a mass noop --------------------------


@pytest.mark.parametrize(
    "response",
    [
        [{"item": "u1", "memory": None}],
        "no memories here",
        {},
        {"results": [{"item": "u1", "memory": None}]},
        {"items": {"item": "u1", "memory": None}},
        {"items": "u1"},
        {"items": b"u1"},
        {"items": None},
        {"items": [], "note": "extra top-level key"},
        None,
    ],
    ids=[
        "bare-list",
        "string",
        "empty-object",
        "no-items-key",
        "items-is-object",
        "items-is-string",
        "items-is-bytes",
        "items-is-null",
        "extra-top-level-key",
        "none",
    ],
)
def test_a_response_not_shaped_as_an_items_list_raises(response: object) -> None:
    batch = _batch("a body")
    with pytest.raises(ExtractionOutputInvalid) as caught:
        validate_extraction(batch, response)
    # Neither link may lead back to a pydantic error quoting the raw model output.
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "u1" not in str(caught.value)


def test_any_mapping_with_a_tuple_of_items_is_a_well_shaped_response() -> None:
    """The provider protocol promises a Mapping, not a dict; a conforming provider
    must not be treated as a transport fault."""
    batch = _batch("a body", "b body")
    response = MappingProxyType(
        {
            "items": (
                {"item": "u1", "memory": _memory()},
                {"item": "u2", "memory": None},
            )
        }
    )
    result = validate_extraction(batch, response)
    assert result["u1"].title == "Releases move to Thursday"
    assert result["u2"] is NOOP_EVIDENCE


def test_a_dict_with_a_tuple_of_items_is_a_well_shaped_response() -> None:
    batch = _batch("a body")
    result = validate_extraction(
        batch, {"items": ({"item": "u1", "memory": _memory()},)}
    )
    assert result["u1"].title == "Releases move to Thursday"
