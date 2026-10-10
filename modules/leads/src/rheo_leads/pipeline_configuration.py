"""Owner-authored, bounded configuration and explicit version migration."""

from uuid import UUID

from rheo_contracts import StaleRecord, WorkspaceContext
from rheo_core.operations.refusals import OperationRefused
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import UnitOfWork
from sqlalchemy import delete, insert, select, update

from rheo_leads import pipeline_common as h
from rheo_leads import pipeline_contracts as c
from rheo_leads.storage import pipeline as p
from rheo_leads.storage import tables as t

VERSION_TABLES = (
    p.preset_stage,
    p.preset_transition,
    p.preset_requirement,
    p.preset_field,
    p.preset_rubric,
    p.preset_template,
)


def pipeline_create(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.PipelineCreate
) -> c.RecordOutput:
    h.lock(uow)
    if ctx.principal.account_id is None:
        raise OperationRefused("account_required", "pipeline owner must be an account")
    if m.preset == "job_search" and not h.jobsearch_enabled(ctx, uow):
        raise OperationRefused(
            "capability_disabled", "job search is not enabled for this workspace"
        )
    record = dict(
        id=uuid7(),
        preset_id=h.PRESETS[m.preset],
        name=m.name,
        owner_id=ctx.principal.account_id,
        created_at=h.now(),
    )
    uow.connection.execute(insert(p.pipeline).values(**record))
    return h.output(record, "pipeline")


def pipeline_list(ctx: WorkspaceContext, uow: UnitOfWork, m: c.Empty) -> c.ItemsOutput:
    return c.ItemsOutput(
        items=[
            dict(r)
            for r in uow.connection.execute(
                select(p.pipeline).order_by(p.pipeline.c.id)
            ).mappings()
        ]
    )


def preset_edit(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.PresetEdit
) -> c.RecordOutput:
    h.lock(uow)
    preset = h.row(uow, p.pipeline_preset, m.preset_id)
    if preset["current_version"] != m.from_version:
        raise StaleRecord("preset changed")
    stages = {s.stage_id: s for s in m.stages}
    fields = {f.target: f for f in m.fields}
    if (
        len(stages) != len(m.stages)
        or len(fields) != len(m.fields)
        or not any(s.kind == "open" for s in m.stages)
    ):
        raise OperationRefused(
            "preset_invalid", "duplicate keys or missing entry stage"
        )
    for start, end in m.transitions:
        if start not in stages or end not in stages or stages[start].kind == "terminal":
            raise OperationRefused("preset_invalid", "invalid transition")
    if len(set(m.transitions)) != len(m.transitions):
        raise OperationRefused("preset_invalid", "duplicate transitions")
    for stage, targets in m.requirements.items():
        if (
            stage not in stages
            or len(set(targets)) != len(targets)
            or any(
                target not in fields
                and target not in {"title", "subject", "message", "interest"}
                for target in targets
            )
        ):
            raise OperationRefused("preset_invalid", "invalid requirement")
    for f in m.fields:
        if f.type == "enum" and not f.enum_values:
            raise OperationRefused("preset_invalid", "enum has no values")
    version = m.from_version + 1
    keys = dict(preset_id=m.preset_id, version=version)
    uow.connection.execute(
        insert(p.preset_version).values(
            **keys,
            created_at=h.now(),
            created_by_id=ctx.principal.account_id,
            note=m.note,
        )
    )
    for ordinal, stage_definition in enumerate(m.stages):
        uow.connection.execute(
            insert(p.preset_stage).values(
                **keys, ordinal=ordinal, **stage_definition.model_dump()
            )
        )
    for start, end in m.transitions:
        uow.connection.execute(
            insert(p.preset_transition).values(
                **keys, from_stage_id=start, to_stage_id=end
            )
        )
    for stage, targets in m.requirements.items():
        for target in targets:
            uow.connection.execute(
                insert(p.preset_requirement).values(
                    **keys, to_stage_id=stage, field=target
                )
            )
    for field in m.fields:
        uow.connection.execute(
            insert(p.preset_field).values(**keys, **field.model_dump())
        )
    for table in (p.preset_rubric, p.preset_template):
        for r in uow.connection.execute(
            select(table).where(
                table.c.preset_id == m.preset_id, table.c.version == m.from_version
            )
        ).mappings():
            uow.connection.execute(
                insert(table).values(**{**dict(r), "version": version})
            )
    uow.connection.execute(
        update(p.pipeline_preset)
        .where(
            p.pipeline_preset.c.id == m.preset_id,
            p.pipeline_preset.c.current_version == m.from_version,
        )
        .values(current_version=version)
    )
    return h.output({**preset, "current_version": version}, "pipeline_preset")


def migrate(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.MigrateInput
) -> c.RecordOutput:
    record = h.opportunity(uow, m, writing=True)
    target = m.stage_map.get(record["stage_id"])
    stage = (
        uow.connection.execute(
            select(p.preset_stage).where(
                p.preset_stage.c.preset_id == record["preset_id"],
                p.preset_stage.c.version == m.to_version,
                p.preset_stage.c.stage_id == target,
            )
        )
        .mappings()
        .one_or_none()
    )
    if stage is None:
        raise OperationRefused(
            "stage_map_required",
            "stage mapping must name a stage in the target version",
        )
    # Configuration migration cannot silently change business outcome.
    if stage["outcome"] != record["disposition_outcome"]:
        raise OperationRefused(
            "outcome_mismatch", "migration must preserve disposition"
        )
    specs = {
        r["target"]: dict(r)
        for r in uow.connection.execute(
            select(p.preset_field).where(
                p.preset_field.c.preset_id == record["preset_id"],
                p.preset_field.c.version == m.to_version,
            )
        ).mappings()
    }
    from rheo_leads.pipeline_evidence import validate_value

    for field in uow.connection.execute(
        select(p.opportunity_field_state).where(
            p.opportunity_field_state.c.opportunity_id == record["id"]
        )
    ).mappings():
        if field["target"].startswith("ext."):
            missing = field["target"] not in specs
            if not missing:
                validate_value(specs[field["target"]], field["value_text"])
            uow.connection.execute(
                update(p.opportunity_field_state)
                .where(
                    p.opportunity_field_state.c.opportunity_id == record["id"],
                    p.opportunity_field_state.c.target == field["target"],
                )
                .values(orphaned=missing)
            )
    h.bump(uow, record, preset_version=m.to_version, stage_id=target)
    h.event(ctx, uow, "opportunity.migrated", record)
    return h.output(record)


def set_routing(
    ctx: WorkspaceContext, uow: UnitOfWork, m: c.RoutingInput
) -> c.ItemsOutput:
    h.lock(uow)
    h.row(uow, t.intake_connection, m.connection_id)
    if m.expected_rule_ids is not None:
        current: list[UUID] = list(
            uow.connection.execute(
                select(p.routing_rule.c.id)
                .where(p.routing_rule.c.connection_id == m.connection_id)
                .order_by(p.routing_rule.c.ordinal)
            ).scalars()
        )
        if current != m.expected_rule_ids:
            raise StaleRecord("connection routing changed; reload before saving")
    for rule in m.rules:
        if rule.pipeline_id:
            h.row(uow, p.pipeline, rule.pipeline_id)
    ids = select(p.routing_rule.c.id).where(
        p.routing_rule.c.connection_id == m.connection_id
    )
    uow.connection.execute(
        delete(p.routing_condition).where(p.routing_condition.c.rule_id.in_(ids))
    )
    uow.connection.execute(
        delete(p.routing_rule).where(p.routing_rule.c.connection_id == m.connection_id)
    )
    result = []
    for ordinal, rule in enumerate(m.rules):
        identifier = uuid7()
        r = dict(
            id=identifier,
            connection_id=m.connection_id,
            ordinal=ordinal,
            action=rule.action,
            pipeline_id=rule.pipeline_id,
            enabled=rule.enabled,
        )
        uow.connection.execute(insert(p.routing_rule).values(**r))
        result.append(r)
        for position, condition in enumerate(rule.conditions):
            uow.connection.execute(
                insert(p.routing_condition).values(
                    rule_id=identifier, ordinal=position, **condition.model_dump()
                )
            )
    return c.ItemsOutput(items=result)
