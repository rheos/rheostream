"""The collector's first-connect install and the job search mapping-target rule.

``install_connection`` is the only runtime write path to the three mapping tables in
this module (migrations and export restore are the others), so the rule that guards
``ext.jobsearch.*`` targets is enforced here, before anything is written.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from rheo_contracts import WorkspaceContext
from rheo_core.connectors.oauth import Discovered, RegisteredClient
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import insert, select

from rheo_leads.intake.process import JOBSEARCH_TARGET_PREFIX
from rheo_leads.jobfeed import dependencies
from rheo_leads.jobfeed.state import read_connection
from rheo_leads.pipeline_common import JOBSEARCH_PRESET_ID, jobsearch_enabled, lock
from rheo_leads.storage import jobfeed as j
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t

MAPPING_VERSION = 2
"""The job mapping's version. 2 adds ``invited``; migration ``0018_invited_jobs``
moves every installed job feed and job paste mapping from 1 to 2."""
POSTING_ID = JOBSEARCH_TARGET_PREFIX + "posting_id"
# The const value is the Job search preset's existing platform enum value.
PLATFORM = "upwork"
# Ordinal order: attach a repeat sighting by posting id, else create.
ROUTING: tuple[tuple[str, str | None], ...] = (
    ("attach_by_field", POSTING_ID),
    ("create_opportunity", None),
)


@dataclass(frozen=True)
class RuleTarget:
    """A routing rule as the mapping-target rule sees it."""

    enabled: bool
    pipeline_id: UUID | None


@dataclass(frozen=True)
class MappingRule:
    target: str
    source_path: str
    transform: str | None = None
    transform_arg: str | None = None
    required: bool = False


# Values arrive flat (canonical strings, integers, "true"/"false", ISO dates), so no
# rule transforms them. No person.* or organization.* target: no party is created.
RULES: tuple[MappingRule, ...] = (
    MappingRule("subject", "/title", required=True),
    MappingRule("message", "/description"),
    MappingRule("source_url", "/posting_url"),
    MappingRule(JOBSEARCH_TARGET_PREFIX + "platform", "/platform", "const", PLATFORM),
    *(
        MappingRule(JOBSEARCH_TARGET_PREFIX + name, "/" + name)
        for name in (
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
    ),
)


def _connection_rules(uow: UnitOfWork, connection_id: UUID) -> list[RuleTarget]:
    return [
        RuleTarget(enabled=r.enabled, pipeline_id=r.pipeline_id)
        for r in uow.connection.execute(
            select(p.routing_rule.c.enabled, p.routing_rule.c.pipeline_id).where(
                p.routing_rule.c.connection_id == connection_id
            )
        )
    ]


def check_mapping_targets(
    uow: UnitOfWork,
    ctx: WorkspaceContext,
    *,
    connection_id: UUID,
    targets: Iterable[str],
    pending_rules: Sequence[RuleTarget] | None = None,
) -> None:
    """Refuse a mapping with an ``ext.jobsearch.*`` target unless the capability is
    on and every enabled routing rule of its connection, at least one, targets a
    Job search pipeline.

    ``pending_rules`` are the rules an install is about to write for a connection
    that does not exist yet; otherwise the connection's stored rules are read.
    """
    if not any(target.startswith(JOBSEARCH_TARGET_PREFIX) for target in targets):
        return
    if not jobsearch_enabled(ctx, uow):
        raise OperationRefused("capability_disabled", "job search is not enabled")
    rules = (
        _connection_rules(uow, connection_id)
        if pending_rules is None
        else pending_rules
    )
    # A record_only rule has no pipeline, so it never qualifies.
    pipelines = {rule.pipeline_id for rule in rules if rule.enabled}
    allowed: set[UUID | None] = set(
        uow.connection.execute(
            select(p.pipeline.c.id).where(
                p.pipeline.c.id.in_([i for i in pipelines if i is not None]),
                p.pipeline.c.preset_id == JOBSEARCH_PRESET_ID,
            )
        ).scalars()
    )
    if not pipelines or pipelines - allowed:
        raise OperationRefused(
            "mapping_target_not_allowed",
            "job search fields need routing into a Job search pipeline only",
        )


def install_connection(
    uow: UnitOfWork,
    ctx: WorkspaceContext,
    *,
    connection_id: UUID,
    pipeline_id: UUID,
    endpoints: Discovered,
    client: RegisteredClient,
) -> None:
    """Create everything the collector writes through, in the caller's transaction.

    The token reference and its generation are left for the authorization
    completion to set before it commits.
    """
    lock(uow)
    if read_connection(uow.connection) is not None:
        raise OperationRefused("connection_exists", "the job feed is already connected")
    check_mapping_targets(
        uow,
        ctx,
        connection_id=connection_id,
        targets=[rule.target for rule in RULES],
        pending_rules=[RuleTarget(enabled=True, pipeline_id=pipeline_id)]
        * len(ROUTING),
    )
    now = dependencies().now()
    funnel_id = write_funnel(
        uow, name="Job feed", description="Postings collected by the job feed", now=now
    )
    mapping_id = write_job_mapping(uow, name="job_feed")
    uow.connection.execute(
        insert(t.intake_connection).values(
            id=connection_id,
            name="Job feed",
            transport="poll",
            source_namespace=f"urn:rheo:connection:{connection_id}",
            mapping_id=mapping_id,
            mapping_version=MAPPING_VERSION,
            funnel_id=funnel_id,
            campaign_id=None,
            priority=100,
            subject_authenticated=False,
            email_verified=False,
            state="active",
            created_at=now,
        )
    )
    uow.connection.execute(
        insert(t.connection_health).values(connection_id=connection_id)
    )
    uow.connection.execute(
        insert(j.jobfeed_connection).values(
            connection_id=connection_id,
            pipeline_id=pipeline_id,
            authorization_state="connected",
            token_ref=None,
            credential_generation=0,
            resource_url=endpoints.resource_url,
            issuer=endpoints.issuer,
            authorization_endpoint=endpoints.authorization_endpoint,
            token_endpoint=endpoints.token_endpoint,
            registration_endpoint=endpoints.registration_endpoint,
            revocation_endpoint=endpoints.revocation_endpoint,
            client_id=client.client_id,
            client_auth_method=client.auth_method,
            client_redirect_uri=client.redirect_uri,
            client_secret_expires_at=client.secret_expires_at,
            client_secret_ref=client.client_secret_ref,
            registration_token_ref=client.registration_token_ref,
            created_at=now,
            updated_at=now,
        )
    )
    write_job_routing(uow, connection_id=connection_id, pipeline_id=pipeline_id)


def write_funnel(
    uow: UnitOfWork, *, name: str, description: str, now: datetime
) -> UUID:
    """A new funnel a job connection owns; its id."""
    funnel_id = uuid7()
    uow.connection.execute(
        insert(t.funnel).values(
            id=funnel_id, name=name, description=description, created_at=now
        )
    )
    return funnel_id


def write_job_mapping(uow: UnitOfWork, *, name: str) -> UUID:
    """A new active copy of the job mapping (:data:`RULES`) under ``name``; its id.

    The job feed and the job paste connections read the same flat body, so they
    share these rules; ``name`` (``job_feed`` or ``job_paste``) tells them apart.
    """
    mapping_id = uuid7()
    uow.connection.execute(
        insert(t.field_mapping).values(
            id=mapping_id,
            version=MAPPING_VERSION,
            name=name,
            source_kind="json",
            state="active",
        )
    )
    uow.connection.execute(
        insert(t.field_mapping_identity).values(
            mapping_id=mapping_id,
            version=MAPPING_VERSION,
            event_id_path=None,
            occurred_at_path="/observed_at",
            subject_id_path=None,
            verified_email_path=None,
        )
    )
    uow.connection.execute(
        insert(t.field_mapping_rule),
        [
            {
                "mapping_id": mapping_id,
                "version": MAPPING_VERSION,
                "target": rule.target,
                "source_path": rule.source_path,
                "transform": rule.transform,
                "transform_arg": rule.transform_arg,
                "required": rule.required,
                "clear_on_null": False,
            }
            for rule in RULES
        ],
    )
    return mapping_id


def write_job_routing(
    uow: UnitOfWork, *, connection_id: UUID, pipeline_id: UUID
) -> None:
    """Attach a repeat sighting by posting id, else create; both into the pipeline."""
    for ordinal, (action, match_field) in enumerate(ROUTING):
        uow.connection.execute(
            insert(p.routing_rule).values(
                id=uuid7(),
                connection_id=connection_id,
                ordinal=ordinal,
                action=action,
                pipeline_id=pipeline_id,
                enabled=True,
                match_field=match_field,
            )
        )
