"""The collector's first-connect install, the job search mapping-target rule, the
processing-side narrowing, the poll context's authority and the dependency seam.

Every case runs against a real installed workspace (Relationships and Leads) with
real dispatch and the real intake worker. The party table is counted through SQL so
this suite names no other module's package.
"""

import json
import threading
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from harness.jobfeed import (
    ENDPOINTS,
    PUBLIC_ADDRESS,
    Collector,
    FakeClock,
    FakeResolver,
    call,
    deliver,
    drain,
    fakes,
    install_collector,
    jobsearch_pipeline,
    owner_web,
    registered_client,
)
from harness.jobfeed import collector as collector
from rheo_contracts import RecordRef
from rheo_core.operations import dispatch
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.storage.routing import open_unit_of_work
from rheo_leads import pipeline_common as h
from rheo_leads.jobfeed import PRODUCTION, Dependencies, dependencies, use_dependencies
from rheo_leads.jobfeed.install import (
    MAPPING_VERSION,
    RuleTarget,
    check_mapping_targets,
    install_connection,
)
from rheo_leads.jobfeed.state import JobfeedConnection, read_connection
from rheo_leads.references import ref
from rheo_leads.storage import jobfeed as j
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t
from sqlalchemy import delete, insert, select, text

from postgres.test_leads_intake import Intake
from postgres.test_leads_intake import intake as intake

pytestmark = pytest.mark.postgres

JOBSEARCH = (
    "platform",
    "posting_id",
    "posting_url",
    "job_type",
    "budget_min",
    "budget_max",
    "proposal_count",
    "proposal_count_exact",
    "proposal_observed_at",
    "posted_on",
    "client_payment_verified",
    "client_spend",
    "client_rating",
    "client_reviews",
    "client_hire_rate",
    "applied",
    "invited",
)
INSTALLED = (
    t.funnel,
    t.field_mapping,
    t.field_mapping_identity,
    t.field_mapping_rule,
    t.intake_connection,
    t.connection_health,
    j.jobfeed_connection,
    p.routing_rule,
    p.routing_condition,
)
REFUSED = frozenset({"operation_not_permitted", "role_not_permitted"})


def counts(i: Intake) -> dict[str, int]:
    return {table.name: i.count(table) for table in INSTALLED}


def rows(i: Intake, statement: Any) -> list[dict[str, Any]]:
    with open_unit_of_work(i.ctx) as uow:
        return [dict(r) for r in uow.connection.execute(statement).mappings()]


def check(
    i: Intake,
    connection_id: UUID,
    targets: list[str],
    pending: list[RuleTarget] | None = None,
) -> str | None:
    """The refusal state of the target check, or ``None`` when it accepts."""
    owner = owner_web(i)
    with open_unit_of_work(owner) as uow:
        try:
            check_mapping_targets(
                uow,
                owner,
                connection_id=connection_id,
                targets=targets,
                pending_rules=pending,
            )
        except OperationRefused as refused:
            return refused.state
    return None


def route(i: Intake, connection_id: UUID, *rules: dict[str, Any]) -> None:
    call(
        i,
        owner_web(i),
        "leads.connection.set_routing",
        connection_id=str(connection_id),
        rules=list(rules),
    )


def capability(i: Intake, value: bool) -> None:
    call(
        i,
        owner_web(i),
        "core.settings.set",
        key="leads.capabilities.jobsearch",
        value=value,
    )


def services_pipeline(i: Intake) -> UUID:
    created = call(i, owner_web(i), "leads.pipeline.create", name="Inbound examples")
    return RecordRef.parse(created.ref).id


def party_count(i: Intake) -> int:
    with open_unit_of_work(i.ctx) as uow:
        return int(
            uow.connection.execute(
                text("SELECT count(*) FROM relationships.party")
            ).scalar_one()
        )


# AC 20 and the install's stored rows.


def test_install_writes_two_ordered_rules_and_refuses_a_second(
    collector: Collector,
) -> None:
    i, connection = collector.intake, collector.connection_id
    rules = rows(
        i,
        select(p.routing_rule)
        .where(p.routing_rule.c.connection_id == connection)
        .order_by(p.routing_rule.c.ordinal),
    )
    assert [
        (r["ordinal"], r["action"], r["match_field"], r["pipeline_id"], r["enabled"])
        for r in rules
    ] == [
        (0, "attach_by_field", "ext.jobsearch.posting_id", collector.pipeline_id, True),
        (1, "create_opportunity", None, collector.pipeline_id, True),
    ]
    assert i.count(p.routing_condition) == 0

    (row,) = rows(
        i, select(t.intake_connection).where(t.intake_connection.c.id == connection)
    )
    assert row["transport"] == "poll" and row["state"] == "active"
    assert row["priority"] == 100
    assert row["subject_authenticated"] is False and row["email_verified"] is False
    assert row["source_namespace"] == f"urn:rheo:connection:{connection}"
    assert row["mapping_version"] == MAPPING_VERSION == 2
    assert row["campaign_id"] is None
    assert row["signing_secret_ref"] is None
    (funnel,) = rows(i, select(t.funnel).where(t.funnel.c.id == row["funnel_id"]))
    assert funnel["name"] == "Job feed"
    assert rows(
        i,
        select(t.connection_health.c.connection_id).where(
            t.connection_health.c.connection_id == connection
        ),
    ) == [{"connection_id": connection}]
    mapping = (t.field_mapping.c.id == row["mapping_id"]) & (
        t.field_mapping.c.version == MAPPING_VERSION
    )
    assert [
        (m["source_kind"], m["state"])
        for m in rows(i, select(t.field_mapping).where(mapping))
    ] == [("json", "active")]
    (identity,) = rows(
        i,
        select(t.field_mapping_identity).where(
            t.field_mapping_identity.c.mapping_id == row["mapping_id"]
        ),
    )
    assert identity == {
        "mapping_id": row["mapping_id"],
        "version": MAPPING_VERSION,
        "event_id_path": None,
        "occurred_at_path": "/observed_at",
        "subject_id_path": None,
        "verified_email_path": None,
    }
    mapped = {
        r["target"]: (
            r["source_path"],
            r["transform"],
            r["transform_arg"],
            r["required"],
        )
        for r in rows(
            i,
            select(t.field_mapping_rule).where(
                t.field_mapping_rule.c.mapping_id == row["mapping_id"]
            ),
        )
    }
    assert mapped == {
        "subject": ("/title", None, None, True),
        "message": ("/description", None, None, False),
        "source_url": ("/posting_url", None, None, False),
        "ext.jobsearch.platform": ("/platform", "const", "upwork", False),
        **{
            f"ext.jobsearch.{name}": (f"/{name}", None, None, False)
            for name in JOBSEARCH
            if name != "platform"
        },
    }
    assert not any(target.startswith(("person.", "organization.")) for target in mapped)

    before = counts(i)
    with open_unit_of_work(collector.owner) as uow:
        with pytest.raises(OperationRefused) as refused:
            install_connection(
                uow,
                collector.owner,
                connection_id=uuid7(),
                pipeline_id=collector.pipeline_id,
                endpoints=ENDPOINTS,
                client=registered_client(),
            )
        assert refused.value.state == "connection_exists"
        uow.commit()
    assert counts(i) == before


def test_install_stores_pinned_endpoints_and_client_references(intake: Intake) -> None:
    expiry = datetime(2026, 6, 1, tzinfo=UTC)
    client = registered_client(
        client_id="fixture-client-2",
        auth_method="client_secret_post",
        secret_expires_at=expiry,
        client_secret_ref="secret://file/fixture/client-secret",
        registration_token_ref="secret://file/fixture/registration-token",
    )
    clock = FakeClock()
    with fakes(clock=clock):
        installed = install_collector(intake, client=client)
    with open_unit_of_work(intake.ctx) as uow:
        stored = read_connection(uow.connection)
        (raw,) = uow.connection.execute(select(j.jobfeed_connection)).mappings()
    assert stored is not None
    assert {f for f in JobfeedConnection.__dataclass_fields__} == set(
        j.jobfeed_connection.c.keys()
    ) - {"singleton"}
    assert stored.connection_id == installed.connection_id
    assert stored.pipeline_id == installed.pipeline_id
    assert (stored.authorization_state, stored.token_ref) == ("connected", None)
    assert stored.credential_generation == 0
    assert (
        stored.resource_url,
        stored.issuer,
        stored.authorization_endpoint,
        stored.token_endpoint,
        stored.registration_endpoint,
        stored.revocation_endpoint,
    ) == (
        ENDPOINTS.resource_url,
        ENDPOINTS.issuer,
        ENDPOINTS.authorization_endpoint,
        ENDPOINTS.token_endpoint,
        ENDPOINTS.registration_endpoint,
        ENDPOINTS.revocation_endpoint,
    )
    assert (
        stored.client_id,
        stored.client_auth_method,
        stored.client_redirect_uri,
        stored.client_secret_expires_at,
        stored.client_secret_ref,
        stored.registration_token_ref,
    ) == (
        "fixture-client-2",
        "client_secret_post",
        client.redirect_uri,
        expiry,
        client.client_secret_ref,
        client.registration_token_ref,
    )
    assert stored.created_at == stored.updated_at == clock.now()
    # Nothing beyond the given endpoints, client values and references is stored.
    given = {
        "connected",
        *(getattr(ENDPOINTS, name) for name in ENDPOINTS.__dataclass_fields__),
        client.client_id,
        client.auth_method,
        client.redirect_uri,
        client.client_secret_ref,
        client.registration_token_ref,
    }
    assert {v for v in raw.values() if isinstance(v, str)} <= given


def test_install_refuses_with_the_capability_off_and_writes_nothing(
    intake: Intake,
) -> None:
    owner = owner_web(intake)
    pipeline = jobsearch_pipeline(intake, owner)
    capability(intake, False)
    before = counts(intake)
    with open_unit_of_work(owner) as uow:
        with pytest.raises(OperationRefused) as refused:
            install_connection(
                uow,
                owner,
                connection_id=uuid7(),
                pipeline_id=pipeline,
                endpoints=ENDPOINTS,
                client=registered_client(),
            )
        assert refused.value.state == "capability_disabled"
        uow.commit()
    assert counts(intake) == before

    capability(intake, True)
    services = services_pipeline(intake)
    with open_unit_of_work(owner) as uow:
        with pytest.raises(OperationRefused) as refused:
            install_connection(
                uow,
                owner,
                connection_id=uuid7(),
                pipeline_id=services,
                endpoints=ENDPOINTS,
                client=registered_client(),
            )
        assert refused.value.state == "mapping_target_not_allowed"
        uow.commit()
    assert counts(intake) == before


# AC 21 point (a): the target check directly.


def test_mapping_target_check(collector: Collector) -> None:
    i = collector.intake
    manual = i.connection_id
    jobs = collector.pipeline_id
    services = services_pipeline(i)
    jobsearch = ["subject", "ext.jobsearch.posting_id"]

    assert check(i, collector.connection_id, jobsearch) is None
    # Targets outside the namespace are never checked.
    assert check(i, manual, ["subject", "message"]) is None
    # No rules at all, then only a disabled one.
    assert check(i, manual, jobsearch) == "mapping_target_not_allowed"
    route(
        i,
        manual,
        {"action": "create_opportunity", "pipeline_id": str(jobs), "enabled": False},
    )
    assert check(i, manual, jobsearch) == "mapping_target_not_allowed"
    # An enabled rule into a pipeline on another preset.
    route(i, manual, {"action": "create_opportunity", "pipeline_id": str(services)})
    assert check(i, manual, jobsearch) == "mapping_target_not_allowed"
    # Every enabled rule must qualify, not just one.
    route(
        i,
        manual,
        {"action": "create_opportunity", "pipeline_id": str(jobs)},
        {"action": "create_opportunity", "pipeline_id": str(services)},
    )
    assert check(i, manual, jobsearch) == "mapping_target_not_allowed"
    route(
        i,
        manual,
        {"action": "record_only"},
        {"action": "create_opportunity", "pipeline_id": str(jobs)},
    )
    assert check(i, manual, jobsearch) == "mapping_target_not_allowed"
    route(
        i,
        manual,
        {"action": "create_opportunity", "pipeline_id": str(jobs)},
        {
            "action": "create_opportunity",
            "pipeline_id": str(services),
            "enabled": False,
        },
    )
    assert check(i, manual, jobsearch) is None
    # A connection that does not exist yet is judged by the rules about to be written.
    future = uuid7()
    assert check(i, future, jobsearch, [RuleTarget(True, jobs)]) is None
    assert check(i, future, jobsearch, [RuleTarget(True, services)]) == (
        "mapping_target_not_allowed"
    )
    assert check(i, future, jobsearch, [RuleTarget(False, jobs)]) == (
        "mapping_target_not_allowed"
    )
    assert check(i, future, jobsearch, [RuleTarget(True, uuid7())]) == (
        "mapping_target_not_allowed"
    )

    capability(i, False)
    assert check(i, collector.connection_id, jobsearch) == "capability_disabled"
    assert check(i, future, jobsearch, [RuleTarget(True, jobs)]) == (
        "capability_disabled"
    )
    assert check(i, manual, ["subject"]) is None


# AC 21 point (c) and the processing-side narrowing.


def processed(i: Intake, event: str, body: dict[str, Any]) -> dict[str, Any]:
    """Accept on the manual connection, drain, and return the receipt's outcome."""
    accepted = i.accept(source_event_id=event, body=json.dumps(body))
    drain(i)
    receipt = RecordRef.parse(accepted.receipt_ref).id
    (row,) = rows(
        i,
        select(t.delivery_receipt.c.state, t.delivery_receipt.c.state_detail).where(
            t.delivery_receipt.c.id == receipt
        ),
    )
    fields = {
        r["target"]: r["value_text"]
        for r in rows(
            i,
            select(t.observation_field).where(
                t.observation_field.c.observation_id == receipt
            ),
        )
    }
    return {**row, "fields": fields}


def add_rules(i: Intake, *targets: str) -> None:
    with open_unit_of_work(i.ctx) as uow:
        mapping = (
            uow.connection.execute(
                select(
                    t.intake_connection.c.mapping_id,
                    t.intake_connection.c.mapping_version,
                ).where(t.intake_connection.c.id == i.connection_id)
            )
            .mappings()
            .one()
        )
        for target in targets:
            uow.connection.execute(
                insert(t.field_mapping_rule).values(
                    mapping_id=mapping["mapping_id"],
                    version=mapping["mapping_version"],
                    target=target,
                    source_path="/" + target.rsplit(".", 1)[-1],
                    required=False,
                    clear_on_null=False,
                )
            )
        uow.commit()


def test_stored_jobsearch_mapping_is_offered_only_with_a_jobsearch_route(
    intake: Intake,
) -> None:
    owner = owner_web(intake)
    jobs = jobsearch_pipeline(intake, owner)
    services = services_pipeline(intake)
    add_rules(intake, "ext.jobsearch.posting_id")
    route(
        intake,
        intake.connection_id,
        {
            "action": "attach_by_field",
            "pipeline_id": str(jobs),
            "match_field": "ext.jobsearch.posting_id",
        },
        {"action": "create_opportunity", "pipeline_id": str(jobs)},
    )
    # Existing records stay readable with the capability off.
    capability(intake, False)
    readable = processed(intake, "kept", {"subject": "Kept", "posting_id": "~0210"})
    assert readable["state"] == "processed", readable
    assert readable["fields"]["ext.jobsearch.posting_id"] == "~0210"

    # No enabled rule into a Job search pipeline: the targets are not offered.
    route(
        intake,
        intake.connection_id,
        {"action": "create_opportunity", "pipeline_id": str(services)},
    )
    narrowed = processed(intake, "narrow", {"subject": "Narrow", "posting_id": "~0211"})
    assert (narrowed["state"], narrowed["state_detail"]) == (
        "refused",
        "mapping_failed:unknown_target",
    ), narrowed
    route(
        intake,
        intake.connection_id,
        {"action": "create_opportunity", "pipeline_id": str(jobs), "enabled": False},
        {"action": "create_opportunity", "pipeline_id": str(services)},
    )
    disabled = processed(intake, "disabled", {"subject": "Off", "posting_id": "~0212"})
    assert disabled["state"] == "refused", disabled

    # Another extension namespace is still offered to the same connection.
    with open_unit_of_work(intake.ctx) as uow:
        uow.connection.execute(
            delete(t.field_mapping_rule).where(
                t.field_mapping_rule.c.target == "ext.jobsearch.posting_id"
            )
        )
        uow.connection.execute(
            insert(p.preset_field).values(
                preset_id=h.PRESET_ID,
                version=1,
                target="ext.services.count",
                type="integer",
                required=False,
                sensitivity="internal",
                enum_values=[],
            )
        )
        uow.commit()
    add_rules(intake, "ext.services.count")
    other = processed(intake, "other", {"subject": "Other", "count": "7"})
    assert other["state"] == "processed", other
    assert other["fields"]["ext.services.count"] == "7"


# AC 22.


def test_collected_posting_creates_no_party(collector: Collector) -> None:
    i = collector.intake
    body = {
        "title": "Example posting",
        "description": "Build a small example service.",
        "posting_url": "https://www.example.com/jobs/~021234",
        "observed_at": "2026-01-05T09:00:00+00:00",
        "platform": "other",
        "posting_id": "~021234",
        "job_type": "hourly",
        "budget_min": "25",
        "budget_max": "60.5",
        "proposal_count": 12,
        "proposal_count_exact": "true",
        "proposal_observed_at": "2026-01-05T08:30:00+00:00",
        "posted_on": "2026-01-04",
        "client_payment_verified": "true",
        "client_spend": "1200",
        "client_rating": "4.9",
        "client_reviews": 7,
        "client_hire_rate": "0.5",
        "applied": "false",
        "invited": "true",
    }
    parties = party_count(i)
    accepted = deliver(collector.poll, i, body, "posting-~021234")
    assert accepted.ok and accepted.result.outcome == "accepted", accepted
    drain(i)
    assert party_count(i) == parties
    receipt = RecordRef.parse(accepted.result.receipt_ref).id
    (observation,) = rows(i, select(t.observation).where(t.observation.c.id == receipt))
    assert observation["party_ref"] is None
    assert observation["source_occurred_at"] == datetime(2026, 1, 5, 9, tzinfo=UTC)
    fields = {
        r["target"]: r["value_text"]
        for r in rows(
            i,
            select(t.observation_field).where(
                t.observation_field.c.observation_id == receipt
            ),
        )
    }
    assert set(fields) == {
        "subject",
        "message",
        "source_url",
        *(f"ext.jobsearch.{name}" for name in JOBSEARCH),
    }
    assert fields["ext.jobsearch.platform"] == "upwork"
    assert fields["ext.jobsearch.proposal_count"] == "12"
    assert not any(target.startswith(("person.", "organization.")) for target in fields)
    (opportunity,) = rows(i, select(p.opportunity))
    assert opportunity["pipeline_id"] == collector.pipeline_id


# AC 52, the authority half.


def test_poll_context_can_only_accept_its_own_deliveries(collector: Collector) -> None:
    i, poll = collector.intake, collector.poll
    assert poll.operation_set == frozenset({"leads.intake.accept_delivery"})
    attempts: dict[str, dict[str, Any]] = {
        "leads.pipeline.create": {"name": "Rogue", "preset": "job_search"},
        "leads.connection.set_routing": {
            "connection_id": str(collector.connection_id),
            "rules": [{"action": "record_only"}],
        },
        "leads.opportunity.create_from_observation": {
            "observation_ref": ref("observation", uuid7()),
            "pipeline_id": str(collector.pipeline_id),
        },
        "leads.qualification.assess": {"ref": ref("opportunity", uuid7())},
        "relationships.party.create": {"display_name": "Rogue Example"},
        "core.settings.set": {"key": "leads.intake.max_payload_bytes", "value": 1},
    }
    before = counts(i) | {"pipeline": i.count(p.pipeline), "party": party_count(i)}
    for name, payload in attempts.items():
        outcome = dispatch(
            poll, name, payload, registry=i.surfaces.operations, consumers=i.consumers
        )
        assert not outcome.ok and outcome.state in REFUSED, (name, outcome)
    flag = dispatch(
        poll,
        "core.settings.set",
        {"key": "leads.capabilities.jobsearch", "value": False},
        registry=i.surfaces.operations,
        consumers=i.consumers,
    )
    assert not flag.ok and flag.state in REFUSED, flag
    after = counts(i) | {"pipeline": i.count(p.pipeline), "party": party_count(i)}
    assert after == before
    with open_unit_of_work(i.ctx) as uow:
        assert h.jobsearch_enabled(owner_web(i), uow)

    own = deliver(poll, i, {"title": "Own posting", "posting_id": "~0299"}, "own-1")
    assert own.ok and own.result.outcome == "accepted", own


# The dependency seam.


def test_use_dependencies_installs_and_restores_the_fakes() -> None:
    assert dependencies() is PRODUCTION
    clock = FakeClock()
    resolver = FakeResolver()
    seen: list[Dependencies] = []
    with fakes(clock=clock, resolver=resolver) as installed:
        assert dependencies() is installed
        assert dependencies().now() == clock.now()
        assert list(dependencies().resolver("www.example.com")) == [PUBLIC_ADDRESS]
        assert resolver.asked == ["www.example.com"]
        dependencies().sleep(5)
        assert clock.slept == [5] and dependencies().clock() == 1005.0
        worker = threading.Thread(target=lambda: seen.append(dependencies()))
        worker.start()
        worker.join()
        inner = Dependencies(
            transport=None,
            resolver=resolver,
            clock=clock.monotonic,
            sleep=clock.sleep,
            now=lambda: datetime(2026, 2, 1, tzinfo=UTC),
        )
        with use_dependencies(inner):
            assert dependencies() is inner
        assert dependencies() is installed
    assert seen == [installed]
    assert dependencies() is PRODUCTION
    with pytest.raises(RuntimeError):
        with fakes():
            raise RuntimeError("boom")
    assert dependencies() is PRODUCTION
