"""The newest known proposal count wins; every other field keeps the old rule.

Seams under test: opportunity derivation through the real poll acceptance, intake
worker and routing, observed in the opportunity's field states after deliveries in
different orders and thicknesses. No collector code runs: each body is built by
hand the way a collector sighting arrives (flat strings, booleans as
``"true"``/``"false"``), with distinct event ids and observation times.
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import pytest
from harness.jobfeed import Collector, deliver, drain, install_collector
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads.pipeline_evidence import COMPETITION_GROUP
from rheo_leads.references import ref
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t
from sqlalchemy import select, update

from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake

pytestmark = pytest.mark.postgres

COUNT, EXACT, OBSERVED_AT = COMPETITION_GROUP
POSTING = "~02777"
T1 = "2026-01-05T09:00:00+00:00"
T2 = "2026-01-06T09:00:00+00:00"
T3 = "2026-01-07T09:00:00+00:00"

ORDERS = pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])

Sighting = tuple[str, dict[str, Any]]


def sighting(observed_at: str, **fields: Any) -> dict[str, Any]:
    """A posting body; a ``proposal_count`` sighting also carries its observed-at."""
    body: dict[str, Any] = {
        "title": "Example posting",
        "posting_id": POSTING,
        "observed_at": observed_at,
    }
    if fields.get("proposal_count") is not None:
        body["proposal_observed_at"] = observed_at
    body.update(fields)
    return body


def rich(observed_at: str, proposals: int, exact: str = "true") -> dict[str, Any]:
    """A first full sighting: description, listing facts and the client record."""
    return sighting(
        observed_at,
        description="Build an example data pipeline.",
        posting_url="https://www.example.com/jobs/example",
        job_type="fixed",
        budget_min="500",
        budget_max="1500",
        posted_on="2026-01-04",
        client_payment_verified="true",
        client_spend="25000",
        client_rating="4.9",
        client_reviews="31",
        client_hire_rate="0.8",
        proposal_count=str(proposals),
        proposal_count_exact=exact,
    )


def refresh(observed_at: str, proposals: int, exact: str = "false") -> dict[str, Any]:
    """A refresh-only sighting: title, URL and count, nothing else."""
    return sighting(
        observed_at,
        posting_url="https://www.example.com/jobs/example",
        proposal_count=str(proposals),
        proposal_count_exact=exact,
    )


def collect(
    i: Intake, sightings: Sequence[Sighting], *, reverse: bool = False
) -> Collector:
    c = install_collector(i)
    send(c, sightings, reverse=reverse)
    return c


def send(c: Collector, sightings: Sequence[Sighting], *, reverse: bool = False) -> None:
    for event_id, body in reversed(sightings) if reverse else sightings:
        accepted = deliver(c.poll, c.intake, body, event_id)
        assert accepted.ok and accepted.result.outcome == "accepted", accepted  # type: ignore[union-attr]
        drain(c.intake)


def opportunity(i: Intake) -> dict[str, Any]:
    with open_unit_of_work(i.ctx) as uow:
        rows = uow.connection.execute(select(p.opportunity)).mappings().all()
    assert len(rows) == 1, rows
    return dict(rows[0])


def states(i: Intake) -> dict[str, dict[str, Any]]:
    """The one opportunity's field states by target."""
    identifier = opportunity(i)["id"]
    with open_unit_of_work(i.ctx) as uow:
        return {
            r["target"]: dict(r)
            for r in uow.connection.execute(
                select(p.opportunity_field_state).where(
                    p.opportunity_field_state.c.opportunity_id == identifier
                )
            ).mappings()
        }


def value(i: Intake, target: str) -> str | None:
    state = states(i).get(target)
    return None if state is None else state["value_text"]


def competition(i: Intake) -> tuple[str | None, str | None, str | None]:
    return value(i, COUNT), value(i, EXACT), value(i, OBSERVED_AT)


def observation_of(i: Intake, event_id: str) -> UUID:
    with open_unit_of_work(i.ctx) as uow:
        return UUID(
            str(
                uow.connection.execute(
                    select(t.observation.c.id).where(
                        t.observation.c.source_event_id == event_id
                    )
                ).scalar_one()
            )
        )


def event(seq: int) -> str:
    return f"{POSTING}.{seq}.{seq:016x}"


# --- AC 42: every changed count is a sighting; the newest count wins -----------------


@ORDERS
def test_counts_five_six_five_derive_the_newest(intake: Intake, reverse: bool) -> None:
    sightings = [
        (event(1), refresh(T1, 5)),
        (event(2), refresh(T2, 6)),
        (event(3), refresh(T3, 5)),
    ]
    collect(intake, sightings, reverse=reverse)
    current = states(intake)
    assert intake.count(p.opportunity_observation) == 3
    assert competition(intake) == ("5", "false", T3)
    third = observation_of(intake, event(3))
    assert {current[target]["observation_id"] for target in COMPETITION_GROUP} == {
        third
    }


# --- AC 43: a thin refresh updates the count and nothing else ------------------------


@ORDERS
def test_thin_refresh_moves_the_count_and_keeps_the_rich_fields(
    intake: Intake, reverse: bool
) -> None:
    first = rich(T1, 5, exact="true")
    collect(
        intake,
        [(event(1), first), (event(2), refresh(T2, 9, exact="false"))],
        reverse=reverse,
    )
    assert competition(intake) == ("9", "false", T2)
    assert value(intake, "message") == first["description"]
    for name in (
        "client_payment_verified",
        "client_spend",
        "client_rating",
        "client_reviews",
        "client_hire_rate",
        "budget_min",
        "budget_max",
        "job_type",
        "posted_on",
    ):
        assert value(intake, "ext.jobsearch." + name) == first[name], name


# --- AC 44: the competition rule's properties ----------------------------------------


@ORDERS
def test_a_lower_newer_count_wins(intake: Intake, reverse: bool) -> None:
    collect(
        intake,
        [(event(1), rich(T1, 12)), (event(2), refresh(T2, 9))],
        reverse=reverse,
    )
    assert competition(intake) == ("9", "false", T2)


@ORDERS
@pytest.mark.parametrize("absent", ["null", "missing"])
def test_a_sighting_without_a_count_clears_and_moves_nothing(
    intake: Intake, reverse: bool, absent: str
) -> None:
    later = sighting(T2, description="Build an example data pipeline, revised.")
    if absent == "null":
        later.update(
            proposal_count=None, proposal_count_exact=None, proposal_observed_at=None
        )
    collect(
        intake,
        [(event(1), refresh(T1, 9, exact="true")), (event(2), later)],
        reverse=reverse,
    )
    assert competition(intake) == ("9", "true", T1)
    first = observation_of(intake, event(1))
    assert states(intake)[COUNT]["observation_id"] == first


@ORDERS
def test_an_exact_count_beats_a_band_at_the_same_time(
    intake: Intake, reverse: bool
) -> None:
    # The band is the richer sighting with the higher event id, so only the exact
    # flag can put the exact count first.
    band = rich(T2, 10, exact="false")
    collect(
        intake,
        [(event(1), refresh(T2, 12, exact="true")), (event(2), band)],
        reverse=reverse,
    )
    assert competition(intake) == ("12", "true", T2)


@ORDERS
def test_an_earlier_band_never_overwrites_a_later_exact_count(
    intake: Intake, reverse: bool
) -> None:
    collect(
        intake,
        [(event(1), rich(T1, 10, exact="false")), (event(2), refresh(T2, 12, "true"))],
        reverse=reverse,
    )
    assert competition(intake) == ("12", "true", T2)


@ORDERS
def test_an_older_richer_sighting_never_overwrites_a_newer_count(
    intake: Intake, reverse: bool
) -> None:
    collect(
        intake,
        [(event(1), refresh(T2, 7, exact="false")), (event(2), rich(T1, 20, "true"))],
        reverse=reverse,
    )
    assert competition(intake) == ("7", "false", T2)


# --- AC 45: every other field keeps the completeness-and-priority rule ---------------

THIN_CASES = [
    ("subject", "title", "A shorter title"),
    ("message", "description", "Short."),
    ("ext.jobsearch.budget_min", "budget_min", "50"),
    ("ext.jobsearch.budget_max", "budget_max", "150"),
    ("ext.jobsearch.job_type", "job_type", "hourly"),
    ("ext.jobsearch.posted_on", "posted_on", "2026-01-06"),
    ("ext.jobsearch.client_payment_verified", "client_payment_verified", "false"),
    ("ext.jobsearch.client_spend", "client_spend", "10"),
    ("ext.jobsearch.client_rating", "client_rating", "3.1"),
    ("ext.jobsearch.client_reviews", "client_reviews", "2"),
    ("ext.jobsearch.client_hire_rate", "client_hire_rate", "0.1"),
]


@ORDERS
@pytest.mark.parametrize("target,key,thin_value", THIN_CASES)
def test_a_thinner_newer_sighting_keeps_the_richer_value(
    intake: Intake, reverse: bool, target: str, key: str, thin_value: str
) -> None:
    first = rich(T1, 5)
    thin = sighting(T2, **{key: thin_value})
    collect(intake, [(event(1), first), (event(2), thin)], reverse=reverse)
    assert value(intake, target) == first[key]
    # The thin sighting carried no count, so the competition fields stand too.
    assert competition(intake) == ("5", "true", T1)


# --- user-owned and orphaned competition fields --------------------------------------


@ORDERS
def test_a_user_owned_count_is_never_replayed(intake: Intake, reverse: bool) -> None:
    c = collect(intake, [(event(1), rich(T1, 5))])
    current = intake.call(
        "leads.opportunity.get", ref=ref("opportunity", opportunity(intake)["id"])
    )
    assert current.ok, current
    edited = intake.call(
        "leads.opportunity.update",
        ref=current.result.ref,
        revision=current.result.revision,
        fields={COUNT: "3"},
    )
    assert edited.ok, edited
    send(c, [(event(2), refresh(T2, 9)), (event(3), refresh(T3, 11))], reverse=reverse)
    current_states = states(intake)
    assert current_states[COUNT]["owner"] == "user"
    assert current_states[COUNT]["value_text"] == "3"
    # The fields the user does not own still follow the newest count's sighting.
    assert (value(intake, EXACT), value(intake, OBSERVED_AT)) == ("false", T3)


def test_an_orphaned_competition_field_is_never_replayed(intake: Intake) -> None:
    c = collect(intake, [(event(1), rich(T1, 5))])
    identifier = opportunity(intake)["id"]
    with open_unit_of_work(intake.ctx) as uow:
        uow.connection.execute(
            update(p.opportunity_field_state)
            .where(
                p.opportunity_field_state.c.opportunity_id == identifier,
                p.opportunity_field_state.c.target == COUNT,
            )
            .values(orphaned=True)
        )
        uow.commit()
    send(c, [(event(2), refresh(T2, 9))])
    current_states = states(intake)
    assert current_states[COUNT]["orphaned"] is True
    assert current_states[COUNT]["value_text"] == "5"
    assert (value(intake, EXACT), value(intake, OBSERVED_AT)) == ("false", T2)


# --- a refresh never touches description, drafts, status or stage --------------------


@pytest.mark.parametrize("stage", ["qualified", "passed"])
def test_a_refresh_leaves_description_drafts_status_and_stage(
    intake: Intake, stage: str
) -> None:
    first = rich(T1, 5)
    c = collect(intake, [(event(1), first)])
    reference = ref("opportunity", opportunity(intake)["id"])
    current = intake.call("leads.opportunity.get", ref=reference)
    assert current.ok, current
    drafted = intake.call(
        "leads.followup.draft",
        ref=reference,
        revision=current.result.revision,
        body="Thanks for considering my application.",
    )
    assert drafted.ok, drafted
    current = intake.call("leads.opportunity.get", ref=reference)
    moved = intake.call(
        "leads.opportunity.transition",
        ref=reference,
        revision=current.result.revision,
        to_stage_id=stage,
        note="Manual stage decision",
    )
    assert moved.ok, moved
    before = opportunity(intake)
    with open_unit_of_work(intake.ctx) as uow:
        drafts_before = [
            dict(r) for r in uow.connection.execute(select(p.draft)).mappings()
        ]
    send(c, [(event(2), refresh(T2, 9))])
    after = opportunity(intake)
    with open_unit_of_work(intake.ctx) as uow:
        drafts_after = [
            dict(r) for r in uow.connection.execute(select(p.draft)).mappings()
        ]
    assert competition(intake) == ("9", "false", T2)
    assert value(intake, "message") == first["description"]
    assert drafts_after == drafts_before and len(drafts_after) == 1
    assert after["stage_id"] == before["stage_id"] == stage
    assert after["disposition_outcome"] == before["disposition_outcome"]
    assert after["title"] == before["title"]
