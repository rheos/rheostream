"""Job paste: copied postings converge on the job feed's opportunity (run 3b).

The job-search regression scenario, with a job feed sighting standing in for the
retired email alert: a pasted search result, a feed sighting and a pasted full
posting of one posting end on one opportunity, and the user's own decisions made in
between survive the later evidence. Every posting, page and text is synthetic.
"""

from pathlib import Path
from typing import Any

import pytest
from harness.jobfeed import (
    Collector,
    call,
    configure_connector,
    drain,
    refreshed,
    run_delivery,
)
from harness.jobfeed import collector as collector
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads.jobpaste.operation import PASTE_MAPPING
from rheo_leads.references import ref
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t
from sqlalchemy import select

from postgres.test_jobfeed_delivery import (
    drafts,
    linked_observations,
    opportunities,
    rows,
    states,
    the_opportunity,
)
from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake

pytestmark = pytest.mark.postgres

POSTING = "~02100"
SEARCH_PAGE = """
<article data-test="JobTile">
  <a data-test="job-tile-title-link"
     href="/jobs/Example-posting_~02100/">Example posting</a>
  <span data-test="job-type-label">Fixed price</span>
  <span data-test="is-fixed-price">Est. budget: $1,500.00</span>
  <div data-test="payment-verified">Verified Payment verified</div>
  <span data-test="proposals-tier">Proposals: 10 to 15</span>
</article>
"""
FULL_TEXT = (
    "Example posting\n\nBuild a small example pipeline for a fictional shop.\n\n"
    "Activity on this job\nProposals: 20 to 50"
)


@pytest.fixture
def hosts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    configure_connector(monkeypatch, tmp_path, job_hosts="www.example.com,example.com")


def paste(c: Collector, kind: str, content: str, **extra: Any) -> Any:
    return call(
        c.intake,
        c.owner,
        "leads.jobsearch.paste",
        pipeline_ref=ref("pipeline", c.pipeline_id),
        kind=kind,
        content=content,
        **extra,
    )


def refused(c: Collector, **payload: Any) -> str:
    from rheo_core.operations.dispatch import dispatch

    outcome = dispatch(
        c.owner,
        "leads.jobsearch.paste",
        {"pipeline_ref": ref("pipeline", c.pipeline_id), **payload},
        registry=c.intake.surfaces.operations,
        consumers=c.intake.consumers,
    )
    assert not outcome.ok, outcome
    return str(outcome.state)


def provenance(i: Intake, target: str) -> Any:
    identifier = the_opportunity(i, POSTING)["id"]
    (row,) = rows(
        i,
        select(p.opportunity_field_state).where(
            p.opportunity_field_state.c.opportunity_id == identifier,
            p.opportunity_field_state.c.target == target,
        ),
    )
    return row["observation_id"]


def paste_connections(i: Intake) -> list[dict[str, Any]]:
    return rows(
        i,
        select(t.intake_connection)
        .join(
            t.field_mapping,
            (t.field_mapping.c.id == t.intake_connection.c.mapping_id)
            & (t.field_mapping.c.version == t.intake_connection.c.mapping_version),
        )
        .where(t.field_mapping.c.name == PASTE_MAPPING),
    )


def test_search_result_feed_sighting_and_full_posting_converge(
    collector: Collector, hosts: None
) -> None:
    i = collector.intake
    first = paste(collector, "search_page", SEARCH_PAGE)
    assert (first.found, first.accepted, first.duplicate, first.skipped) == (1, 1, 0, 0)
    drain(i)
    created = the_opportunity(i, POSTING)
    assert created["pipeline_id"] == collector.pipeline_id
    assert "message" not in states(i, POSTING)
    assert states(i, "~02100")["ext.jobsearch.proposal_count"] == "15"

    # The user's own decisions, made before the later evidence arrives.
    reference = ref("opportunity", created["id"])
    current = i.call("leads.opportunity.get", ref=reference)
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
    drafts_before = drafts(i)

    # The job feed sees the same posting: it attaches, never duplicates.
    run_delivery(collector, [refreshed(POSTING, proposal_count=9)])
    drain(i)
    assert len(opportunities(i, POSTING)) == 1
    assert linked_observations(i, POSTING) == 2

    # The full posting supplies the description the other two never carried.
    full = paste(
        collector,
        "posting",
        FULL_TEXT,
        posting_url="https://www.example.com/jobs/Example-posting_~02100?ref=x",
    )
    assert (full.found, full.accepted) == (1, 1)
    drain(i)
    after = the_opportunity(i, POSTING)
    assert after["id"] == created["id"]
    assert linked_observations(i, POSTING) == 3
    current_states = states(i, POSTING)
    assert current_states["message"] == FULL_TEXT
    # Competition by recency: the full posting is the newest observation.
    assert current_states["ext.jobsearch.proposal_count"] == "50"
    assert current_states["ext.jobsearch.proposal_count_exact"] == "false"
    # Per-field provenance names the observation each value came from.
    observations = {
        r["observation_id"]
        for r in rows(
            i,
            select(p.opportunity_observation.c.observation_id).where(
                p.opportunity_observation.c.opportunity_id == after["id"]
            ),
        )
    }
    assert provenance(i, "message") in observations
    assert provenance(i, "message") != provenance(i, "ext.jobsearch.client_reviews")
    # The user's decisions survive every later observation.
    assert after["stage_id"] == "qualified"
    assert drafts(i) == drafts_before and len(drafts_before) == 1
    # One paste connection for the pipeline, reused by the second paste.
    (connection,) = paste_connections(i)
    assert connection["transport"] == "manual" and connection["state"] == "active"


def test_pasting_the_same_page_again_is_a_duplicate(
    collector: Collector, hosts: None
) -> None:
    i = collector.intake
    paste(collector, "search_page", SEARCH_PAGE)
    drain(i)
    again = paste(collector, "search_page", SEARCH_PAGE)
    assert (again.found, again.accepted, again.duplicate) == (1, 0, 1)
    drain(i)
    assert linked_observations(i, POSTING) == 1
    assert i.count(t.observation) == 1
    # Not a conflicting delivery: no conflict record and no health error.
    assert i.count(t.delivery_conflict) == 0
    health = rows(i, select(t.connection_health.c.last_error))
    assert all(row["last_error"] is None for row in health)


def test_a_tile_without_a_posting_link_is_skipped_and_writes_nothing(
    collector: Collector, hosts: None
) -> None:
    i = collector.intake
    stray = SEARCH_PAGE.replace('href="/jobs/', 'href="https://jobs.example.net/jobs/')
    result = paste(collector, "search_page", stray)
    assert (result.found, result.accepted, result.skipped) == (0, 0, 1)
    assert result.warnings and result.receipt_refs == []
    assert paste_connections(i) == []
    assert i.count(t.delivery_receipt) == 0


def test_a_posting_needs_its_link(collector: Collector, hosts: None) -> None:
    assert refused(collector, kind="posting", content=FULL_TEXT) == "input_invalid"


def test_paste_refuses_a_pipeline_that_is_not_job_search(
    collector: Collector, hosts: None
) -> None:
    services = call(
        collector.intake,
        collector.owner,
        "leads.pipeline.create",
        name="Inquiries",
        preset="inbound_services",
    )
    from rheo_core.operations.dispatch import dispatch

    outcome = dispatch(
        collector.owner,
        "leads.jobsearch.paste",
        {"pipeline_ref": services.ref, "kind": "search_page", "content": SEARCH_PAGE},
        registry=collector.intake.surfaces.operations,
        consumers=collector.intake.consumers,
    )
    assert outcome.state == "preset_not_allowed"


def test_paste_refuses_with_job_search_off(collector: Collector, hosts: None) -> None:
    call(
        collector.intake,
        collector.owner,
        "core.settings.set",
        key="leads.capabilities.jobsearch",
        value=False,
    )
    assert (
        refused(collector, kind="search_page", content=SEARCH_PAGE)
        == "capability_disabled"
    )


def test_paste_refuses_without_job_hosts(
    collector: Collector, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure_connector(monkeypatch, tmp_path, job_hosts=None)
    assert (
        refused(collector, kind="search_page", content=SEARCH_PAGE) == "job_hosts_unset"
    )


def test_capture_and_webhooks_still_use_the_seeded_manual_connection(
    collector: Collector, hosts: None
) -> None:
    i = collector.intake
    paste(collector, "search_page", SEARCH_PAGE)
    drain(i)
    with open_unit_of_work(i.ctx) as uow:
        seeded = uow.connection.execute(
            select(t.intake_connection).where(
                t.intake_connection.c.source_namespace == t.MANUAL_CAPTURE_NAMESPACE
            )
        ).one()
        manual = uow.connection.execute(
            select(t.intake_connection.c.id).where(
                t.intake_connection.c.transport == "manual"
            )
        ).all()
    assert len(manual) == 2
    funnel = ref("funnel", i.funnel_id)
    captured = i.call(
        "leads.intake.capture",
        funnel_ref=funnel,
        body={"subject": "A pasted inquiry", "event_id": "capture-1"},
    )
    assert captured.ok, captured
    receipt = rows(
        i,
        select(t.delivery_receipt.c.connection_id).where(
            t.delivery_receipt.c.connection_id == seeded.id
        ),
    )
    assert len(receipt) == 1
    webhook = i.call(
        "leads.connection.create_webhook", name="Site form", funnel_ref=funnel
    )
    assert webhook.ok, webhook
