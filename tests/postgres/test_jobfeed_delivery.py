"""Delivery: classification, dedupe, never-reused sequence numbers, savepoints.

Seams under test: ``deliver`` through the real intake acceptance and the real intake
consumer (classification counts, dedupe against stored digests, event ids, one
savepoint per acceptance, sequence numbers that survive erasure); ``next_seq``'s
identifier-only receipt read; ``compose_event_id`` and the digest helpers as pure
functions. Every sighting is synthetic and every body is the real
``NormalizedPosting.to_body()``.
"""

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from harness.jobfeed import (
    FULL_DESCRIPTION,
    Collector,
    call,
    deliver,
    discovered,
    drain,
    over_cap,
    posting,
    refreshed,
    run_delivery,
)
from harness.jobfeed import collector as collector
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_tables import outbox_event
from rheo_leads.contracts import AcceptedDelivery
from rheo_leads.intake.accept import health_write
from rheo_leads.jobfeed import delivery
from rheo_leads.jobfeed.collect import Sighting
from rheo_leads.jobfeed.delivery import (
    DeliveryResult,
    canonical_body_digest,
    compose_event_id,
    delivered_posting,
    encode_body,
    field_digests,
    format_digests,
    parse_digests,
    unchanged,
)
from rheo_leads.jobfeed.install import POSTING_ID
from rheo_leads.jobfeed.normalize import COMPARABLE_FIELDS
from rheo_leads.jobfeed.state import next_seq
from rheo_leads.pipeline_evidence import COMPETITION_GROUP
from rheo_leads.references import ref
from rheo_leads.storage import jobfeed as j
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t
from sqlalchemy import insert, select, text

from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake
from postgres.test_leads_pipeline import approve_delete

pytestmark = pytest.mark.postgres

T1 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
T2 = datetime(2026, 1, 6, 9, 0, tzinfo=UTC)
T3 = datetime(2026, 1, 7, 9, 0, tzinfo=UTC)
COUNT, EXACT, OBSERVED_AT = COMPETITION_GROUP
DEFAULT_PAYLOAD_LIMIT = 262144


# --- helpers --------------------------------------------------------------------------


def rows(i: Intake, statement: Any) -> list[dict[str, Any]]:
    with open_unit_of_work(i.ctx) as uow:
        return [dict(r) for r in uow.connection.execute(statement).mappings()]


def written(i: Intake) -> dict[str, int]:
    """Rows a delivery or its processing adds."""
    return {
        "receipts": i.count(t.delivery_receipt),
        "payloads": i.count(t.delivery_payload),
        "observations": i.count(t.observation),
    }


def event_ids(i: Intake, posting_id: str) -> list[str]:
    return [
        r["source_event_id"]
        for r in rows(
            i,
            select(t.delivery_receipt.c.source_event_id)
            .where(t.delivery_receipt.c.source_event_id.startswith(posting_id + "."))
            .order_by(t.delivery_receipt.c.received_at),
        )
    ]


def sequence(event_id: str) -> int:
    return int(event_id.split(".")[1])


def dedupe_row(c: Collector, posting_id: str) -> dict[str, Any] | None:
    found = rows(
        c.intake,
        select(j.jobfeed_posting).where(
            j.jobfeed_posting.c.connection_id == c.connection_id,
            j.jobfeed_posting.c.posting_id == posting_id,
        ),
    )
    return found[0] if found else None


def opportunities(i: Intake, posting_id: str) -> list[dict[str, Any]]:
    return rows(
        i,
        select(p.opportunity)
        .join(
            p.opportunity_field_state,
            p.opportunity_field_state.c.opportunity_id == p.opportunity.c.id,
        )
        .where(
            p.opportunity_field_state.c.target == POSTING_ID,
            p.opportunity_field_state.c.value_text == posting_id,
        ),
    )


def the_opportunity(i: Intake, posting_id: str) -> dict[str, Any]:
    found = opportunities(i, posting_id)
    assert len(found) == 1, found
    return found[0]


def states(i: Intake, posting_id: str) -> dict[str, str | None]:
    identifier = the_opportunity(i, posting_id)["id"]
    return {
        r["target"]: r["value_text"]
        for r in rows(
            i,
            select(p.opportunity_field_state).where(
                p.opportunity_field_state.c.opportunity_id == identifier
            ),
        )
    }


def competition(i: Intake, posting_id: str) -> tuple[str | None, ...]:
    current = states(i, posting_id)
    return tuple(current.get(target) for target in COMPETITION_GROUP)


def linked_observations(i: Intake, posting_id: str) -> int:
    identifier = the_opportunity(i, posting_id)["id"]
    return len(
        rows(
            i,
            select(p.opportunity_observation.c.observation_id).where(
                p.opportunity_observation.c.opportunity_id == identifier
            ),
        )
    )


def last_error(c: Collector) -> str | None:
    (health,) = rows(
        c.intake,
        select(t.connection_health.c.last_error).where(
            t.connection_health.c.connection_id == c.connection_id
        ),
    )
    return health["last_error"]


def party_count(i: Intake) -> int:
    with open_unit_of_work(i.ctx) as uow:
        return int(
            uow.connection.execute(
                text("SELECT count(*) FROM relationships.party")
            ).scalar_one()
        )


def drafts(i: Intake) -> list[dict[str, Any]]:
    return rows(i, select(p.draft).order_by(p.draft.c.id))


def delivered(c: Collector, sightings: list[Sighting]) -> DeliveryResult:
    """One run: deliver, then let the intake consumer process what was accepted."""
    result = run_delivery(c, sightings).result
    drain(c.intake)
    return result


def result(**counts: Any) -> DeliveryResult:
    return DeliveryResult(**counts)


def body_of(sighting: Sighting) -> dict[str, str | int]:
    return delivered_posting(sighting).to_body()


def event_id_of(sighting: Sighting, seq: int) -> str:
    return compose_event_id(
        sighting.posting.posting_id, seq, canonical_body_digest(body_of(sighting))
    )


def digests_of(sighting: Sighting) -> str:
    return format_digests(field_digests(delivered_posting(sighting)))


AcceptCall = Callable[..., Any]


def substitute(monkeypatch: pytest.MonkeyPatch, fake: AcceptCall) -> None:
    monkeypatch.setattr(delivery, "call_in_transaction", fake)


# --- pure: event id and digests (AC 41, O2) -------------------------------------------


CHANGES: dict[str, dict[str, Any]] = {
    "proposal count": {"proposal_count": 6},
    "exactness": {"exact": False},
    "budget": {"budget": ("400", "1500")},
    "client spend": {"spend": "24000"},
    "client rating": {"rating": "4.1"},
    "client reviews": {"reviews": 2},
    "client hire rate": {"hire_rate": "0.5"},
    "verification": {"verified": False},
    "title": {"title": "Another example posting"},
    "description": {"description": "A different full description."},
}


def test_identical_sightings_at_one_sequence_share_an_event_id() -> None:
    first = discovered("~02900", observed_at=T1)
    later = discovered("~02900", observed_at=T3)
    first_body, later_body = body_of(first), body_of(later)
    # Both observation times are in the bodies, and they differ.
    for name in ("observed_at", "proposal_observed_at"):
        assert first_body[name] != later_body[name], name
    assert canonical_body_digest(first_body) == canonical_body_digest(later_body)
    assert event_id_of(first, 3) == event_id_of(later, 3)
    assert re.fullmatch(r"~02900\.3\.[0-9a-f]{16}", event_id_of(first, 3))


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_every_compared_field_changes_the_event_id(change: str) -> None:
    base = discovered("~02900", observed_at=T1)
    other = discovered("~02900", observed_at=T1, **CHANGES[change])
    assert event_id_of(other, 3) != event_id_of(base, 3)
    assert field_digests(delivered_posting(other)) != field_digests(
        delivered_posting(base)
    )


@pytest.mark.parametrize(
    "posting_id", ["~02123.1", "~021.5", "~02", "02123", "~02abc", "~02123 ", ""]
)
def test_compose_event_id_refuses_a_posting_id_that_is_not_an_identifier(
    posting_id: str,
) -> None:
    with pytest.raises(ValueError):
        compose_event_id(posting_id, 1, "0" * 16)


def test_compose_event_id_refuses_a_bad_sequence_or_digest() -> None:
    assert compose_event_id("~02123", 1, "0123456789abcdef") == (
        "~02123.1.0123456789abcdef"
    )
    for seq in (0, -1):
        with pytest.raises(ValueError):
            compose_event_id("~02123", seq, "0" * 16)
    for digest in ("0" * 15, "0" * 17, "0123456789ABCDEF", "0123456789abcde."):
        with pytest.raises(ValueError):
            compose_event_id("~02123", 1, digest)


def test_digest_text_round_trips_and_ignores_malformed_pairs() -> None:
    digests = field_digests(delivered_posting(discovered("~02900")))
    assert set(digests) <= set(COMPARABLE_FIELDS)
    assert "observed_at" not in digests and "proposal_observed_at" not in digests
    stored = format_digests(digests)
    assert stored.split(";") == sorted(stored.split(";"))
    assert parse_digests(stored) == digests
    assert parse_digests("") == {}
    assert parse_digests("title=zz;nonsense;unknown=0123456789abcdef") == {}


def test_unchanged_compares_only_the_fields_a_sighting_carries() -> None:
    rich = field_digests(delivered_posting(discovered("~02900", proposal_count=5)))
    thin = field_digests(delivered_posting(refreshed("~02900", proposal_count=5)))
    assert "description" in rich and "description" not in thin
    assert unchanged(thin, rich)
    assert unchanged(rich, rich)
    assert not unchanged(rich, thin)  # description never stored: a change
    assert not unchanged(thin, None)  # no stored row: a change
    six = field_digests(delivered_posting(refreshed("~02900", proposal_count=6)))
    # A revert (5, 6, 5) is a change against the 6 stored last.
    assert not unchanged(thin, {**rich, **six})


def test_a_feed_snippet_is_never_delivered() -> None:
    snippet = Sighting(
        posting("~02900", description="A feed snippet.", hire_rate="0.4"),
        detail_ok=False,
    )
    body = body_of(snippet)
    assert "description" not in body and "client_hire_rate" not in body
    assert body_of(discovered("~02900"))["description"] == FULL_DESCRIPTION


# --- AC 37 and AC 22: what discovery creates ------------------------------------------


def test_discovery_creates_one_opportunity_and_excluded_untracked_rows_none(
    collector: Collector,
) -> None:
    i = collector.intake
    parties = party_count(i)
    run = run_delivery(
        collector,
        [
            discovered("~02101", proposal_count=12),
            discovered("~02102", proposal_count=50),
            # Excluded from discovery and never tracked: skipped, never inserted.
            Sighting(posting("~02103", proposal_count=51), detail_ok=True),
            over_cap("~02104"),
            Sighting(posting("~02105", applied=True), detail_ok=False),
            refreshed("~02106"),  # downgraded: its detail read failed
        ],
    )
    assert run.result == result(imported=2, filtered=3, skipped_untracked=4)
    assert run.checkpoints == 1  # between the two deliveries
    drain(i)
    for posting_id in ("~02101", "~02102"):
        (created,) = opportunities(i, posting_id)
        assert created["pipeline_id"] == collector.pipeline_id
        stored = dedupe_row(collector, posting_id)
        assert stored is not None and stored["seq"] == 1
    for posting_id in ("~02103", "~02104", "~02105", "~02106"):
        assert opportunities(i, posting_id) == []
        assert event_ids(i, posting_id) == []
        assert dedupe_row(collector, posting_id) is None
    assert i.count(p.opportunity) == 2
    assert written(i) == {"receipts": 2, "payloads": 2, "observations": 2}
    # AC 22: no party or contact.
    assert party_count(i) == parties
    assert not any(
        target.startswith(("person.", "organization."))
        for target in states(i, "~02101")
    )


# --- AC 38: an over-cap refresh of a tracked posting ----------------------------------


def test_over_cap_refresh_updates_tracked_and_an_unchanged_repeat_adds_nothing(
    collector: Collector,
) -> None:
    i, posting_id = collector.intake, "~02200"
    assert delivered(
        collector, [discovered(posting_id, proposal_count=40, observed_at=T1)]
    ) == result(imported=1)
    second = delivered(collector, [over_cap(posting_id, observed_at=T2)])
    assert second == result(refreshed=1, filtered=1)
    assert len(opportunities(i, posting_id)) == 1
    assert linked_observations(i, posting_id) == 2
    assert competition(i, posting_id) == ("51", "false", T2.isoformat())
    before = written(i)
    third = delivered(collector, [over_cap(posting_id, observed_at=T3)])
    assert third == result(skipped_dup=1, filtered=1)
    assert written(i) == before
    assert competition(i, posting_id) == ("51", "false", T2.isoformat())
    assert len(opportunities(i, posting_id)) == 1


# --- AC 39: a refresh never touches description, drafts, status or stage --------------


def test_a_refresh_leaves_description_drafts_status_and_stage(
    collector: Collector,
) -> None:
    i, posting_id = collector.intake, "~02210"
    delivered(collector, [discovered(posting_id, proposal_count=5, observed_at=T1)])
    reference = ref("opportunity", the_opportunity(i, posting_id)["id"])
    current = i.call("leads.opportunity.get", ref=reference)
    assert current.ok, current
    drafted = i.call(
        "leads.followup.draft",
        ref=reference,
        revision=current.result.revision,
        body="Thanks for considering my application.",
    )
    assert drafted.ok, drafted
    current = i.call("leads.opportunity.get", ref=reference)
    moved = i.call(
        "leads.opportunity.transition",
        ref=reference,
        revision=current.result.revision,
        to_stage_id="qualified",
        note="Manual stage decision",
    )
    assert moved.ok, moved
    before, drafts_before = the_opportunity(i, posting_id), drafts(i)
    assert delivered(
        collector, [refreshed(posting_id, proposal_count=9, observed_at=T2)]
    ) == result(refreshed=1)
    after = the_opportunity(i, posting_id)
    assert competition(i, posting_id) == ("9", "true", T2.isoformat())
    assert states(i, posting_id)["message"] == FULL_DESCRIPTION
    assert drafts(i) == drafts_before and len(drafts_before) == 1
    for column in ("stage_id", "disposition_outcome", "title"):
        assert after[column] == before[column], column
    assert after["stage_id"] == "qualified"


# --- AC 42: a reverting count is three observations -----------------------------------


def test_counts_five_six_five_across_three_runs_are_three_observations(
    collector: Collector,
) -> None:
    i, posting_id = collector.intake, "~02220"
    assert delivered(
        collector, [discovered(posting_id, proposal_count=5, observed_at=T1)]
    ) == result(imported=1)
    assert delivered(
        collector, [refreshed(posting_id, proposal_count=6, observed_at=T2)]
    ) == result(refreshed=1)
    assert delivered(
        collector, [refreshed(posting_id, proposal_count=5, observed_at=T3)]
    ) == result(refreshed=1)
    assert [sequence(e) for e in event_ids(i, posting_id)] == [1, 2, 3]
    assert linked_observations(i, posting_id) == 3
    assert competition(i, posting_id) == ("5", "true", T3.isoformat())


# --- AC 46: repeat sightings join the one opportunity, open or closed -----------------


def test_repeat_sightings_never_create_a_second_opportunity(
    collector: Collector,
) -> None:
    i, posting_id = collector.intake, "~02230"
    assert delivered(
        collector, [discovered(posting_id, proposal_count=5, observed_at=T1)]
    ) == result(imported=1)
    assert delivered(
        collector, [discovered(posting_id, proposal_count=7, observed_at=T2)]
    ) == result(refreshed=1)
    assert len(opportunities(i, posting_id)) == 1
    reference = ref("opportunity", the_opportunity(i, posting_id)["id"])
    current = i.call("leads.opportunity.get", ref=reference)
    closed = i.call(
        "leads.opportunity.transition",
        ref=reference,
        revision=current.result.revision,
        to_stage_id="passed",
        note="Not a fit",
    )
    assert closed.ok, closed
    assert delivered(
        collector, [discovered(posting_id, proposal_count=9, observed_at=T3)]
    ) == result(refreshed=1)
    (only,) = opportunities(i, posting_id)
    assert only["disposition_outcome"] == "disqualified"
    assert i.count(p.opportunity) == 1
    assert linked_observations(i, posting_id) == 3


# --- AC 66: thin refreshes and feed snippets ------------------------------------------


def test_thin_refreshes_and_snippets_are_duplicates_and_a_new_description_is_not(
    collector: Collector,
) -> None:
    i, posting_id = collector.intake, "~02240"
    delivered(collector, [discovered(posting_id, proposal_count=5, observed_at=T1)])
    before = written(i)
    thin = delivered(
        collector, [refreshed(posting_id, proposal_count=5, observed_at=T2)]
    )
    assert thin == result(skipped_dup=1)
    assert written(i) == before
    for day in (8, 9, 10):
        snippet = Sighting(
            posting(
                posting_id,
                description="A feed snippet that differs from the full text.",
                purpose="refresh_only",
                observed_at=datetime(2026, 1, day, 9, 0, tzinfo=UTC),
            ),
            detail_ok=False,
        )
        assert delivered(collector, [snippet]) == result(skipped_dup=1)
        assert written(i) == before
    assert states(i, posting_id)["message"] == FULL_DESCRIPTION
    revised = "A revised full description from a successful detail read."
    changed = delivered(
        collector,
        [discovered(posting_id, description=revised, observed_at=T3)],
    )
    assert changed == result(refreshed=1)
    assert written(i) == {name: count + 1 for name, count in before.items()}


# --- W6: boolean fields derive end to end ---------------------------------------------


def test_boolean_fields_pass_evidence_validation_end_to_end(
    collector: Collector,
) -> None:
    i = collector.intake
    # An applied posting is delivered only when tracked, so track it first.
    seeded = deliver(
        collector.poll,
        i,
        {
            "title": "Example posting",
            "posting_id": "~02301",
            "observed_at": T1.isoformat(),
        },
        "seed-posting",
    )
    assert seeded.ok and seeded.result.outcome == "accepted", seeded  # type: ignore[union-attr]
    drain(i)
    assert len(opportunities(i, "~02301")) == 1
    run = delivered(
        collector,
        [
            refreshed(
                "~02301",
                proposal_count=12,
                exact=True,
                verified=True,
                applied=True,
                observed_at=T2,
            ),
            discovered(
                "~02302",
                proposal_count=50,
                exact=False,
                band="20 to 50",
                verified=False,
                applied=False,
                observed_at=T2,
            ),
        ],
    )
    assert run == result(imported=1, refreshed=1, filtered=1)
    for posting_id, expected in (("~02301", "true"), ("~02302", "false")):
        current = states(i, posting_id)
        for name in ("proposal_count_exact", "client_payment_verified", "applied"):
            assert current.get("ext.jobsearch." + name) == expected, (posting_id, name)


# --- AC 67, I23 and O1: a sequence number is never reused, even after erasure ---------


def receipt_reads(statements: list[str]) -> list[str]:
    return [
        s
        for s in statements
        if s.lstrip().upper().startswith("SELECT")
        and re.search(r"\bFROM\s+leads\.delivery_receipt\b", s)
    ]


@pytest.mark.parametrize("follow", ["identical", "changed"])
@pytest.mark.parametrize("erased", ["opportunity", "observation"])
def test_after_erasure_the_next_sighting_takes_the_next_unused_number(
    collector: Collector, erased: str, follow: str
) -> None:
    i, posting_id = collector.intake, "~02400"
    delivered(collector, [discovered(posting_id, proposal_count=5, observed_at=T1)])
    delivered(collector, [discovered(posting_id, proposal_count=6, observed_at=T2)])
    used = event_ids(i, posting_id)
    assert [sequence(e) for e in used] == [1, 2]
    if erased == "opportunity":
        approve_delete(i, ref("opportunity", the_opportunity(i, posting_id)["id"]))
    else:
        (observation,) = rows(
            i,
            select(t.observation.c.id).where(
                t.observation.c.source_event_id == used[1]
            ),
        )
        approve_delete(i, ref("observation", observation["id"]))
    assert dedupe_row(collector, posting_id) is None
    conflicts = i.count(t.delivery_conflict)
    count = 6 if follow == "identical" else 7
    sighting = discovered(posting_id, proposal_count=count, observed_at=T3)
    run = run_delivery(collector, [sighting])
    drain(i)
    assert run.result.errors == 0 and run.result.skipped_dup == 0, run.result
    assert run.result.imported + run.result.refreshed == 1, run.result
    assert event_ids(i, posting_id) == [*used, event_id_of(sighting, 3)]
    stored = dedupe_row(collector, posting_id)
    assert stored is not None and stored["seq"] == 3
    assert i.count(t.delivery_conflict) == conflicts == 0
    assert last_error(collector) not in {"digest_differs", "deleted_observation"}
    # The fallback read selects the identifier column and nothing else.
    (read,) = receipt_reads(run.statements)
    select_list = re.match(r"\s*SELECT\s+(.*?)\s+FROM\s", read, re.S | re.I)
    assert select_list is not None
    assert select_list.group(1).strip() == "leads.delivery_receipt.source_event_id"


def test_next_seq_reads_only_valid_identifiers_of_the_exact_posting(
    collector: Collector,
) -> None:
    i = collector.intake
    for event_id in (
        "~02500.2.aaaaaaaaaaaaaaaa",
        "~02500.x.bbbbbbbbbbbbbbbb",
        "~02500.10a.cccccccccccccccc",
        "~02500.",
        "~02500",
        "~025001.9.dddddddddddddddd",
        "~02502.9.eeeeeeeeeeeeeeee",
    ):
        accepted = deliver(collector.poll, i, {"title": "Example posting"}, event_id)
        assert accepted.ok, accepted
    with open_unit_of_work(i.ctx) as uow:
        uow.connection.execute(
            insert(j.jobfeed_posting).values(
                connection_id=collector.connection_id,
                posting_id="~02502",
                seq=4,
                field_digests="",
                last_event_id="~02502.4.ffffffffffffffff",
                first_delivered_at=T1,
                last_delivered_at=T1,
            )
        )
        uow.commit()
    with open_unit_of_work(i.ctx) as uow:
        numbers = next_seq(
            uow,
            connection_id=collector.connection_id,
            posting_ids=["~02500", "~025001", "~02501", "~02502"],
        )
    assert numbers == {"~02500": 3, "~025001": 10, "~02501": 1, "~02502": 5}


# --- AC 71 and I25: one savepoint per acceptance --------------------------------------


def set_payload_limit(c: Collector, value: int) -> None:
    call(
        c.intake,
        c.owner,
        "core.settings.set",
        key="leads.intake.max_payload_bytes",
        value=value,
    )


def test_one_refused_row_of_three_costs_that_row_only(collector: Collector) -> None:
    i = collector.intake
    small, big, other = (
        discovered("~02601", observed_at=T1),
        discovered("~02602", observed_at=T1, title="Example posting " + "x" * 2000),
        discovered("~02603", observed_at=T1),
    )
    sizes = [len(encode_body(body_of(s)).encode()) for s in (small, big, other)]
    limit = max(sizes[0], sizes[2]) + 16
    assert sizes[1] > limit
    set_payload_limit(collector, limit)
    run = run_delivery(collector, [small, big, other])
    assert run.result == result(imported=2, errors=1, partial=True)
    assert run.checkpoints == 2
    assert event_ids(i, "~02602") == []
    assert dedupe_row(collector, "~02602") is None
    for sighting in (small, other):
        posting_id = sighting.posting.posting_id
        assert event_ids(i, posting_id) == [event_id_of(sighting, 1)]
        stored = dedupe_row(collector, posting_id)
        assert stored is not None
        assert (stored["seq"], stored["last_event_id"], stored["field_digests"]) == (
            1,
            event_id_of(sighting, 1),
            digests_of(sighting),
        )
    assert written(i)["receipts"] == 2
    # The refused row's number is still free: the next run uses 1.
    set_payload_limit(collector, DEFAULT_PAYLOAD_LIMIT)
    assert run_delivery(collector, [big]).result == result(imported=1)
    assert event_ids(i, "~02602") == [event_id_of(big, 1)]


def test_a_refusal_after_intake_wrote_rolls_back_only_its_savepoint(
    collector: Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    i = collector.intake
    real = delivery.call_in_transaction

    def refuse_after_writing(*args: Any, **kwargs: Any) -> Any:
        accepted = real(*args, **kwargs)
        if args[3]["source_event_id"].startswith("~02702."):
            raise OperationRefused("fixture_refusal", "Synthetic refusal")
        return accepted

    substitute(monkeypatch, refuse_after_writing)
    events = i.count(outbox_event)
    run = run_delivery(
        collector,
        [discovered("~02701"), discovered("~02702"), discovered("~02703")],
    )
    assert run.result == result(imported=2, errors=1, partial=True)
    assert event_ids(i, "~02702") == []
    assert dedupe_row(collector, "~02702") is None
    assert written(i) == {"receipts": 2, "payloads": 2, "observations": 0}
    assert i.count(outbox_event) == events + 2
    for posting_id in ("~02701", "~02703"):
        assert len(event_ids(i, posting_id)) == 1
        assert dedupe_row(collector, posting_id) is not None


def answering(outcome: str, before: Callable[[Any], None] | None = None) -> AcceptCall:
    def fake(ctx: Any, uow: Any, name: str, payload: Mapping[str, Any]) -> Any:
        if before is not None:
            before(uow)
        return AcceptedDelivery(
            receipt_ref=ref("delivery_receipt", uuid7()), outcome=outcome
        )

    return fake


def test_a_duplicate_outcome_records_the_dedupe_row_and_counts_skipped_dup(
    collector: Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    posting_id = "~02800"
    first = discovered(posting_id, proposal_count=5, observed_at=T1)
    assert run_delivery(collector, [first]).result == result(imported=1)
    substitute(monkeypatch, answering("duplicate"))
    second = discovered(posting_id, proposal_count=6, observed_at=T2)
    assert run_delivery(collector, [second]).result == result(skipped_dup=1)
    stored = dedupe_row(collector, posting_id)
    assert stored is not None
    assert (stored["seq"], stored["last_event_id"], stored["field_digests"]) == (
        2,
        event_id_of(second, 2),
        digests_of(second),
    )


def test_a_conflict_outcome_consumes_the_number_and_keeps_the_digests(
    collector: Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    i, posting_id = collector.intake, "~02810"
    first = discovered(posting_id, proposal_count=5, observed_at=T1)
    assert run_delivery(collector, [first]).result == result(imported=1)

    def intake_signal(uow: Any) -> None:
        # What intake writes on a conflict: its own sticky health code.
        health_write(uow, collector.connection_id, error="digest_differs")

    with monkeypatch.context() as patch:
        substitute(patch, answering("conflict", intake_signal))
        second = discovered(posting_id, proposal_count=6, observed_at=T2)
        run = run_delivery(collector, [second])
    assert run.result == result(errors=1, partial=True)
    stored = dedupe_row(collector, posting_id)
    assert stored is not None
    assert (stored["seq"], stored["last_event_id"], stored["field_digests"]) == (
        2,
        event_id_of(second, 2),
        digests_of(first),
    )
    assert last_error(collector) == "digest_differs"
    # The real acceptance then continues from the next number.
    assert run_delivery(collector, [second]).result == result(imported=1)
    assert [sequence(e) for e in event_ids(i, posting_id)] == [1, 3]


def test_any_other_exception_rolls_back_the_whole_work_transaction(
    collector: Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    i = collector.intake
    real = delivery.call_in_transaction
    calls: list[str] = []

    def fail_second(*args: Any, **kwargs: Any) -> Any:
        calls.append(args[3]["source_event_id"])
        if len(calls) == 2:
            raise RuntimeError("synthetic failure")
        return real(*args, **kwargs)

    substitute(monkeypatch, fail_second)
    with pytest.raises(RuntimeError):
        run_delivery(collector, [discovered("~02901"), discovered("~02902")])
    assert len(calls) == 2
    assert written(i) == {"receipts": 0, "payloads": 0, "observations": 0}
    assert rows(i, select(j.jobfeed_posting)) == []


def test_deliver_refuses_two_sightings_of_one_posting(collector: Collector) -> None:
    with pytest.raises(ValueError):
        run_delivery(collector, [discovered("~02950"), refreshed("~02950")])
    assert written(collector.intake)["receipts"] == 0
