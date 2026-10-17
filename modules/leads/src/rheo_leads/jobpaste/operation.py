"""``leads.jobsearch.paste``: pasted jobs become intake deliveries.

A copied search-results page or one full posting is parsed (:mod:`.parse`) into the
job feed's body shape and accepted through ``leads.intake.accept_delivery`` on the
pipeline's own *job paste* connection, created on first use. That connection reads
the job mapping and routes like the job feed: attach by ``ext.jobsearch.posting_id``
into the pipeline, else create. Matching is per pipeline, so a paste converges on the
opportunity the job feed or an earlier paste created, and derivation decides each
field from every linked observation.

The person pasting is the actor: the connection's transport is ``manual``, which
takes the caller's own context and a funnel per delivery. Each delivery's event id
is ``paste.<posting_id>.<body digest>``, the digest leaving out both observation
times. Pasting identical content again finds that receipt and counts a duplicate
without delivering: the same id with new times would otherwise reach intake as a
conflicting delivery.
"""

from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import Handler
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from rheo_core.settings import resolve
from sqlalchemy import insert, select

from rheo_leads.contracts import AcceptDeliveryInput, PastedJobs, PasteJobsInput
from rheo_leads.intake.accept import accept_delivery
from rheo_leads.jobfeed.delivery import canonical_body_digest, encode_body
from rheo_leads.jobfeed.install import (
    RULES,
    RuleTarget,
    check_mapping_targets,
    write_funnel,
    write_job_mapping,
    write_job_routing,
)
from rheo_leads.jobfeed.normalize import EVENT_ID_SEPARATOR
from rheo_leads.jobpaste.parse import ParsedPaste, parse_posting, parse_search_page
from rheo_leads.pipeline_common import JOBSEARCH_PRESET_ID, jobsearch_enabled, lock
from rheo_leads.references import identifier, ref
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t

PASTE_MAPPING: Final = "job_paste"
JOB_HOSTS_KEY: Final = "leads.jobfeed.job_hosts"
EVENT_PREFIX: Final = "paste"


def _paste_connection(uow: UnitOfWork, pipeline_id: UUID) -> tuple[UUID, UUID] | None:
    """The pipeline's active job paste connection and its funnel, if it has one."""
    found = uow.connection.execute(
        select(t.intake_connection.c.id, t.intake_connection.c.funnel_id)
        .join(
            t.field_mapping,
            (t.field_mapping.c.id == t.intake_connection.c.mapping_id)
            & (t.field_mapping.c.version == t.intake_connection.c.mapping_version),
        )
        .join(
            p.routing_rule, p.routing_rule.c.connection_id == t.intake_connection.c.id
        )
        .where(
            t.intake_connection.c.transport == "manual",
            t.intake_connection.c.state == "active",
            t.field_mapping.c.name == PASTE_MAPPING,
            p.routing_rule.c.pipeline_id == pipeline_id,
        )
        .order_by(t.intake_connection.c.created_at)
        .limit(1)
    ).one_or_none()
    return None if found is None else (found.id, found.funnel_id)


def _received(uow: UnitOfWork, connection_id: UUID, event_id: str) -> Any | None:
    """The receipt this connection already holds under ``event_id``, if any."""
    return uow.connection.execute(
        select(t.delivery_receipt.c.id, t.delivery_receipt.c.state_detail).where(
            t.delivery_receipt.c.connection_id == connection_id,
            t.delivery_receipt.c.source_event_id == event_id,
        )
    ).one_or_none()


def _install(
    uow: UnitOfWork, ctx: WorkspaceContext, pipeline_id: UUID, now: datetime
) -> tuple[UUID, UUID]:
    """Create the pipeline's job paste connection in the caller's transaction."""
    connection_id = uuid7()
    check_mapping_targets(
        uow,
        ctx,
        connection_id=connection_id,
        targets=[rule.target for rule in RULES],
        pending_rules=[RuleTarget(enabled=True, pipeline_id=pipeline_id)],
    )
    funnel_id = write_funnel(
        uow, name="Job paste", description="Jobs pasted by hand", now=now
    )
    mapping_id = write_job_mapping(uow, name=PASTE_MAPPING)
    uow.connection.execute(
        insert(t.intake_connection).values(
            id=connection_id,
            name="Job paste",
            transport="manual",
            source_namespace=f"urn:rheo:connection:{connection_id}",
            mapping_id=mapping_id,
            mapping_version=1,
            funnel_id=funnel_id,
            campaign_id=None,
            # The job feed's priority: a paste wins a field only on completeness.
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
    write_job_routing(uow, connection_id=connection_id, pipeline_id=pipeline_id)
    return connection_id, funnel_id


def _parsed(m: PasteJobsInput, job_hosts: list[str], now: datetime) -> ParsedPaste:
    if m.kind == "search_page":
        return parse_search_page(m.content, job_hosts, today=now.date())
    if m.posting_url is None or not m.posting_url.strip():
        raise OperationRefused("input_invalid", "a posting needs its link")
    return parse_posting(m.posting_url, m.content, job_hosts)


def paste(ctx: WorkspaceContext, uow: UnitOfWork, m: PasteJobsInput) -> PastedJobs:
    pipeline_id = identifier(m.pipeline_ref, "pipeline")
    if not jobsearch_enabled(ctx, uow):
        raise OperationRefused("capability_disabled", "job search is not enabled")
    preset = uow.connection.execute(
        select(p.pipeline.c.preset_id).where(p.pipeline.c.id == pipeline_id)
    ).scalar_one_or_none()
    if preset is None:
        raise OperationRefused("not_found", "pipeline unavailable")
    if preset != JOBSEARCH_PRESET_ID:
        raise OperationRefused("preset_not_allowed", "choose a Job search pipeline")
    job_hosts = list(resolve().get_list(JOB_HOSTS_KEY))
    if not job_hosts:
        raise OperationRefused("job_hosts_unset", "no job hosts are configured")
    now = datetime.now(UTC)
    parsed = _parsed(m, job_hosts, now)
    receipts: list[str] = []
    warnings = list(parsed.warnings)
    accepted = duplicate = 0
    if parsed.jobs:
        lock(uow)
        connection = _paste_connection(uow, pipeline_id) or _install(
            uow, ctx, pipeline_id, now
        )
        connection_id, funnel_id = connection
        for job in parsed.jobs:
            body = job.to_body(now)
            event_id = EVENT_ID_SEPARATOR.join(
                (EVENT_PREFIX, job.posting_id, canonical_body_digest(body))
            )
            earlier = _received(uow, connection_id, event_id)
            if earlier is not None:
                duplicate += 1
                receipts.append(ref("delivery_receipt", earlier.id))
                if earlier.state_detail == "observation_deleted":
                    warnings.append(
                        f"{job.posting_url} was pasted and then deleted before; "
                        "this paste was not recorded"
                    )
                continue
            answer = accept_delivery(
                ctx,
                uow,
                AcceptDeliveryInput(
                    connection_ref=ref("intake_connection", connection_id),
                    source_event_id=event_id,
                    body=encode_body(body),
                    funnel_ref=ref("funnel", funnel_id),
                ),
            )
            receipts.append(answer.receipt_ref)
            if answer.outcome == "duplicate":
                duplicate += 1
            else:
                accepted += 1
    return PastedJobs(
        found=len(parsed.jobs),
        accepted=accepted,
        duplicate=duplicate,
        skipped=parsed.skipped,
        warnings=warnings,
        receipt_refs=receipts,
    )


PASTE_DECLARATION = OperationDeclaration(
    name="leads.jobsearch.paste",
    safety_class=SafetyClass.MUTATE,
    roles=frozenset({Role.OWNER, Role.MEMBER}),
    input_model=PasteJobsInput,
    output=PastedJobs,
    # Each job's event id is derived from its content, so a retry is a duplicate.
    idempotency=Idempotency.NATURAL,
    audit=AuditSpec(subject_field="pipeline_ref"),
)
OPERATIONS: tuple[tuple[OperationDeclaration, Handler], ...] = (
    (PASTE_DECLARATION, paste),
)
