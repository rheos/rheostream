"""Export-shaped ``core.runtime.run`` dispatch and the worker job handler.

Dispatch validates actor, runtime, and model, then enqueues. The job reconstructs
context, gates, binds login credentials, builds the request, and polls. Named
runtime failures call ``finish_failed`` (or the matching records helper) then
return — they do not raise into the worker loop.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, TypeAdapter
from rheo_contracts import (
    ActorKind,
    AdapterSpawn,
    ApprovalRequiredEvent,
    Audience,
    CancelledEvent,
    ContextItem,
    ContextPurpose,
    ContextTier,
    FailureEvent,
    FinalOutputEvent,
    ProgressEvent,
    RuntimeCapabilities,
    RuntimeGate,
    RuntimeHandle,
    RuntimeLimits,
    RuntimeOutput,
    RuntimeRequest,
    ToolCallEvent,
    ToolResultEvent,
    UnmetCapability,
    UsageEvent,
    WorkspaceContext,
)
from sqlalchemy import insert, select, update

from rheo_core.boundary.context import Refusal
from rheo_core.boundary.factories import context_from_operation
from rheo_core.operations.records import (
    finish_cancelled,
    finish_failed,
    mark_approval_required,
    set_progress,
)
from rheo_core.operations.refusals import OperationRefused
from rheo_core.redaction.masking import mask_for_model
from rheo_core.redaction.policy import TierPolicy, purpose_of
from rheo_core.redaction.render import RenderedRecord, render_record
from rheo_core.redaction.tiers import SensitivityTier
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import LIVE, RecordHead, Unavailable, resolve_in
from rheo_core.routing.config import MCP, RoutingConfig
from rheo_core.routing.url_for import url_for
from rheo_core.runtime.registry import AdapterRegistry
from rheo_core.settings import ResolvedSettings, resolve
from rheo_core.settings.storage_source import PostgresOverrideSource
from rheo_core.storage import runtime_tables
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.storage.control_plane import delete_access_token, get_access_token
from rheo_core.storage.data_root import Purpose, run_dir_for, workspace_dir_for
from rheo_core.storage.postgres import get_backend
from rheo_core.tokens.issue import issue_runtime_token
from rheo_core.tokens.policy import NON_TOKEN_ISSUABLE
from rheo_core.tokens.sets import TOOL_REGISTRY
from rheo_core.work.cancellation import CancellationToken
from rheo_core.work.jobs import enqueue_job

if TYPE_CHECKING:  # pragma: no cover - see ``build_context`` on the call-time import
    from rheo_core.redaction.registry import RenderingRegistry

RUNTIME_RUN: Final = "core.runtime.run"
RUNTIME_ACTOR_REQUIRED: Final = "runtime_actor_required"
PURPOSE_MISMATCH: Final = "purpose_mismatch"
"""A stored caller's purpose could not be turned into the binding its rebuilt context
would carry: an unparseable string, or -- for a ``TOKEN`` actor -- a supplied purpose
that disagrees with the ``access_token.purpose`` the token was minted with.

Raised by ``boundary/factories.py:context_from_operation`` and reached through
``context_for_approved_execution``. It lives here beside
:data:`RUNTIME_ACTOR_REQUIRED` because it is the same kind of thing -- a rebuilt
context refusing what the stored payload claims -- and that factory spells the other
one as a literal for the cycle this module's own import of it creates."""
RUNTIME_UNKNOWN: Final = "runtime_unknown"
MODEL_UNKNOWN: Final = "model_unknown"
CREDENTIAL_NOT_OWNED: Final = "credential_not_owned"
DEADLINE_EXCEEDED: Final = "deadline_exceeded"
STREAM_TRUNCATED: Final = "stream_truncated"
HARD_DEADLINE_SECONDS: Final = 3600
CREDENTIAL_SLOT: Final = "model"
_IGNORE_EXTRA: Final = ConfigDict(extra="ignore")
_OUTPUT_ADAPTER: TypeAdapter[RuntimeOutput] = TypeAdapter(RuntimeOutput)


class RuntimeRunInput(BaseModel):
    model_config = _IGNORE_EXTRA

    task: str
    purpose: ContextPurpose
    refs: list[str] = []
    requirements: list[str] = []
    output: RuntimeOutput
    continuation: UUID | None = None
    runtime_id: str | None = None
    model_id: str | None = None
    permitted_tools: list[str] = []
    deadline_seconds: int | None = None
    max_iterations: int = RuntimeLimits.DEFAULT_MAX_ITERATIONS


class RuntimeRunScheduled(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: UUID


class RuntimeJobPayload(BaseModel):
    """Refs only — never item text, a secret, a token, or ``native_handle``."""

    workspace_id: UUID
    operation_id: UUID
    actor_kind: str
    actor_id: UUID | None
    audience_kind: str
    audience_id: UUID | None
    entry: str
    purpose: str
    task: str
    refs: list[str]
    requirements: list[str]
    output: dict[str, object]
    continuation: UUID | None
    runtime_id: str
    model_id: str
    permitted_tools: list[str]
    deadline_seconds: int
    max_iterations: int = RuntimeLimits.DEFAULT_MAX_ITERATIONS


def check_runtime_gate(
    requirements: Sequence[str], capabilities: RuntimeCapabilities
) -> None:
    """Thin wrapper: ``RuntimeGate.check`` from the contract."""
    RuntimeGate.check(requirements, capabilities)


def build_runtime_request(
    *,
    operation_id: UUID,
    actor: object,
    workspace_id: UUID,
    audience: Audience,
    purpose: ContextPurpose,
    task: str,
    context_items: list[ContextItem],
    permitted_tools: list[str],
    output: RuntimeOutput,
    requirements: list[str],
    limits: RuntimeLimits,
    continuation: UUID | None,
    credential_slot: str,
    runtime_id: str,
    model_id: str,
) -> RuntimeRequest:
    """The sole production constructor of ``RuntimeRequest``."""
    from rheo_contracts import Actor

    assert isinstance(actor, Actor)
    return RuntimeRequest(
        operation_id=operation_id,
        actor=actor,
        workspace_id=workspace_id,
        audience=audience,
        purpose=purpose,
        task=task,
        context_items=context_items,
        permitted_tools=permitted_tools,
        output=output,
        requirements=requirements,
        limits=limits,
        continuation=continuation,
        credential_slot=credential_slot,
        runtime_id=runtime_id,
        model_id=model_id,
    )


def build_context(
    ctx: WorkspaceContext,
    refs: Sequence[str],
    *,
    uow: UnitOfWork,
    renderings: RenderingRegistry | None = None,
) -> list[ContextItem]:
    """Resolve live, readable refs on the sealed handler UoW, render each for the
    model under the redaction contract, and truncate at the cap.

    No ``purpose`` parameter: the binding is on ``ctx.principal.bound_purpose``, put
    there by the factory that rebuilt the job's context, so a caller cannot hand this
    function a purpose the context was not resolved under. It took one until 1a1 and
    discarded it (``del purpose``), which is the shape that made the parameter worth
    removing rather than wiring up. An unbound principal renders under
    ``internal_analysis`` (``rheo_core/redaction/policy.py``).

    Per reference, in order (``runtime-and-mcp.md`` § The context builder):

    1. Resolve under ``ctx``; skip anything not live and readable.
    2. Skip a record type ``<module>.redaction.exclude_types`` lists, before tiering.
    3. **Untiered type** (no entry in :data:`~rheo_core.redaction.registry.RENDERINGS`,
       which is every Recallatron type): the resolver's ``display`` label, tagged
       ``public``, since a module that tiers none of a type's fields has declared
       nothing withheld (the tier table puts titles in ``public``).
    4. **Tiered type**: the module's ``load_for_model`` reads the fields, and its own
       ``render_for_model`` or the default renderer drops every field above what the
       policy allows. A record that renders to nothing is skipped; the resolver's
       ``display`` is never the fallback, because for a tiered type its tier is
       exactly what nobody declared.
    5. Mask free text (secret references always, contact values unless the operator
       and the workspace have both opted in) on every path, then apply the byte cap.

    ``renderings`` exists for tests that register a probe rendering without touching
    the process-global table; production passes nothing.
    """
    settings = resolve(workspace_id=ctx.workspace_id, source=PostgresOverrideSource())
    cap = settings.get_int("runtime.max_context_bytes")
    policy = TierPolicy.from_settings(purpose_of(ctx), settings)
    # Imported at call time: the registry imports ``rheo_core.operations`` for its
    # origin rule, whose package ``__init__`` imports this module, so a module-level
    # import here closes that cycle for whichever of the two is imported first.
    from rheo_core.redaction.registry import RENDERINGS

    table = RENDERINGS if renderings is None else renderings
    items: list[ContextItem] = []
    used = 0
    for ref in refs:
        if used >= cap:
            break
        head = resolve_in(ref, ctx, uow)
        if isinstance(head, Unavailable) or not head.readable or head.state != LIVE:
            continue
        if head.ref.record_type in policy.excluded_types(head.ref.module):
            continue
        rendered = _render_for_context(ctx, uow, head, table, policy)
        if rendered is None:
            continue
        text = rendered.text
        remaining = cap - used
        raw = text.encode("utf-8")
        if len(raw) > remaining:
            text = raw[:remaining].decode("utf-8", errors="ignore")
            raw = text.encode("utf-8")
        items.append(
            ContextItem(
                ref=head.ref.format(), tier=ContextTier(rendered.tier.value), text=text
            )
        )
        used += len(raw)
    return items


def _render_for_context(
    ctx: WorkspaceContext,
    uow: UnitOfWork,
    head: RecordHead,
    table: RenderingRegistry,
    policy: TierPolicy,
) -> RenderedRecord | None:
    """One resolved head as context text, or ``None`` to send nothing for it."""
    rendering = table.lookup(head.ref.module, head.ref.record_type)
    if rendering is None:
        return RenderedRecord(
            text=mask_for_model(head.display, policy), tier=SensitivityTier.PUBLIC
        )
    record = rendering.load(ctx, uow, head.ref)
    if record is None:
        return None
    if rendering.render is None:
        return render_record(record, rendering.tiers, policy)
    text = rendering.render(record, policy)
    if not text:
        return None
    return RenderedRecord(text=mask_for_model(text, policy), tier=policy.ceiling)


def _backing_account_id(ctx: WorkspaceContext) -> UUID:
    if ctx.actor.kind is ActorKind.ACCOUNT:
        if ctx.actor.id is None:
            raise OperationRefused(
                RUNTIME_ACTOR_REQUIRED,
                "an account actor needs an id",
            )
        return ctx.actor.id
    if ctx.actor.kind is ActorKind.TOKEN:
        if ctx.actor.id is None:
            raise OperationRefused(
                RUNTIME_ACTOR_REQUIRED,
                "a token actor needs an id",
            )
        with get_backend().control_engine.connect() as connection:
            row = get_access_token(connection, ctx.actor.id)
        if row is None:
            raise OperationRefused(
                RUNTIME_ACTOR_REQUIRED,
                "the token has no backing account",
            )
        return row.account_id
    raise OperationRefused(
        RUNTIME_ACTOR_REQUIRED,
        f"core.runtime.run cannot run as actor kind {ctx.actor.kind.value!r}",
    )


def _sole_or_refuse(
    value: str | None, allowed: list[str], unknown: str, label: str
) -> str:
    if value is None:
        if len(allowed) == 1:
            return allowed[0]
        raise OperationRefused(
            unknown,
            f"omitted {label} needs a sole allowed value",
        )
    if value not in allowed:
        raise OperationRefused(unknown, f"unknown {label} {value!r}")
    return value


def runtime_run_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: RuntimeRunInput
) -> RuntimeRunScheduled:
    _backing_account_id(ctx)
    if not isinstance(uow, HandlerUnitOfWork):
        raise OperationRefused(
            "output_invalid",
            "core.runtime.run is long-running and needs a minted operation",
        )
    minted_id = uow.operation_id
    if minted_id is None:
        raise OperationRefused(
            "output_invalid",
            "core.runtime.run is long-running and needs a minted operation",
        )
    settings = resolve(workspace_id=ctx.workspace_id, source=PostgresOverrideSource())
    runtime_id = _sole_or_refuse(
        model_input.runtime_id,
        settings.get_list("runtime.allowed_runtimes"),
        RUNTIME_UNKNOWN,
        "runtime_id",
    )
    model_id = _sole_or_refuse(
        model_input.model_id,
        settings.get_list("runtime.allowed_models"),
        MODEL_UNKNOWN,
        "model_id",
    )
    max_deadline = settings.get_int("runtime.max_deadline_seconds")
    deadline = (
        max_deadline
        if model_input.deadline_seconds is None
        else model_input.deadline_seconds
    )
    deadline = min(deadline, max_deadline, HARD_DEADLINE_SECONDS)
    audience_kind = "none" if ctx.audience is None else ctx.audience.kind.value
    audience_id = None if ctx.audience is None else ctx.audience.id
    payload = RuntimeJobPayload(
        workspace_id=ctx.workspace_id,
        operation_id=minted_id,
        actor_kind=ctx.actor.kind.value,
        actor_id=ctx.actor.id,
        audience_kind=audience_kind,
        audience_id=audience_id,
        entry=ctx.entry.value,
        purpose=model_input.purpose.value,
        task=model_input.task,
        refs=list(model_input.refs),
        requirements=list(model_input.requirements),
        output=model_input.output.model_dump(mode="json"),
        continuation=model_input.continuation,
        runtime_id=runtime_id,
        model_id=model_id,
        permitted_tools=list(model_input.permitted_tools),
        deadline_seconds=deadline,
        max_iterations=model_input.max_iterations,
    )
    enqueue_job(
        uow.connection,
        kind=RUNTIME_RUN,
        payload=payload.model_dump(mode="json"),
        now=datetime.now(UTC),
        max_attempts=resolve().get_int("work.max_attempts"),
        operation_id=minted_id,
    )
    return RuntimeRunScheduled(operation_id=minted_id)


def _actor_may_call(ctx: WorkspaceContext, operation: str) -> bool:
    if operation in NON_TOKEN_ISSUABLE:
        return False
    permitted = ctx.operation_set
    if not isinstance(permitted, frozenset):
        return True
    return operation in permitted


def _permitted_tools(ctx: WorkspaceContext, requested: Sequence[str]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for name in requested:
        if name in seen:
            continue
        tool = TOOL_REGISTRY.lookup(name)
        operation = tool.declaration.operation if tool is not None else name
        if _actor_may_call(ctx, operation):
            names.append(name)
            seen.add(name)
    return names


def _snapshot_operations(ctx: WorkspaceContext, tool_names: Sequence[str]) -> list[str]:
    operations: set[str] = set()
    for name in tool_names:
        tool = TOOL_REGISTRY.lookup(name)
        operations.add(tool.declaration.operation if tool is not None else name)
    return sorted(op for op in operations if _actor_may_call(ctx, op))


def _credential_scope(settings: ResolvedSettings) -> str:
    kind = settings.get_str("runtime.claude_cli.credential_kind")
    if kind == "login":
        account_id = settings.get_str("runtime.claude_cli.credential_account_id")
        return f"login:{account_id}"
    return "api_key"


def _job_account_id(payload: RuntimeJobPayload) -> UUID | None:
    if payload.actor_kind == ActorKind.ACCOUNT.value:
        return payload.actor_id
    if payload.actor_kind == ActorKind.TOKEN.value and payload.actor_id is not None:
        with get_backend().control_engine.connect() as connection:
            row = get_access_token(connection, payload.actor_id)
        return None if row is None else row.account_id
    return None


def _fail(
    uow: HandlerUnitOfWork,
    payload: RuntimeJobPayload,
    *,
    now: datetime,
    error_code: str,
    error_text: str,
    request_id: UUID | None = None,
) -> None:
    finish_failed(
        uow.connection,
        operation_id=payload.operation_id,
        now=now,
        error_code=error_code,
        error_text=error_text,
    )
    if request_id is not None:
        uow.connection.execute(
            update(runtime_tables.runtime_request)
            .where(runtime_tables.runtime_request.c.id == request_id)
            .values(
                terminal_event="failure",
                failure_kind=error_code,
                ended_at=now,
            )
        )


def _context_digest(items: Sequence[ContextItem]) -> bytes:
    return hashlib.sha256("".join(item.text for item in items).encode("utf-8")).digest()


def _lookup_continuation(
    uow: HandlerUnitOfWork,
    payload: RuntimeJobPayload,
    *,
    credential_scope: str,
    now: datetime,
) -> tuple[UUID | None, str | None]:
    if payload.continuation is None:
        return None, None
    if payload.actor_id is None or payload.audience_id is None:
        return None, None
    row = (
        uow.connection.execute(
            select(runtime_tables.runtime_session).where(
                runtime_tables.runtime_session.c.id == payload.continuation,
                runtime_tables.runtime_session.c.runtime_id == payload.runtime_id,
                runtime_tables.runtime_session.c.credential_scope == credential_scope,
                runtime_tables.runtime_session.c.actor_kind == payload.actor_kind,
                runtime_tables.runtime_session.c.actor_id == payload.actor_id,
                runtime_tables.runtime_session.c.audience_kind == payload.audience_kind,
                runtime_tables.runtime_session.c.audience_id == payload.audience_id,
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None, None
    expires_at = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        return None, None
    return row["id"], str(row["native_handle"])


def _write_session(
    uow: HandlerUnitOfWork,
    payload: RuntimeJobPayload,
    *,
    native_handle: str,
    matched_session_id: UUID | None,
    credential_scope: str,
    now: datetime,
    ttl_hours: int,
) -> None:
    if not native_handle:
        return
    if payload.actor_id is None or payload.audience_id is None:
        return
    expires_at = now + timedelta(hours=ttl_hours)
    if matched_session_id is not None:
        uow.connection.execute(
            update(runtime_tables.runtime_session)
            .where(runtime_tables.runtime_session.c.id == matched_session_id)
            .values(
                native_handle=native_handle,
                last_used_at=now,
                expires_at=expires_at,
            )
        )
        return
    uow.connection.execute(
        insert(runtime_tables.runtime_session).values(
            id=uuid7(),
            runtime_id=payload.runtime_id,
            credential_scope=credential_scope,
            actor_kind=payload.actor_kind,
            actor_id=payload.actor_id,
            audience_kind=payload.audience_kind,
            audience_id=payload.audience_id,
            native_handle=native_handle,
            created_at=now,
            last_used_at=now,
            expires_at=expires_at,
        )
    )


def _insert_transcript(
    uow: HandlerUnitOfWork,
    *,
    request_id: UUID,
    ordinal: int,
    kind: str,
    body: str,
    now: datetime,
    retention_days: int,
) -> None:
    encoded = body.encode("utf-8")
    uow.connection.execute(
        insert(runtime_tables.runtime_transcript).values(
            id=uuid7(),
            request_id=request_id,
            ordinal=ordinal,
            kind=kind,
            body=body,
            byte_length=len(encoded),
            recorded_at=now,
            retention_until=now + timedelta(days=retention_days),
        )
    )


def _poll_loop(
    uow: HandlerUnitOfWork,
    payload: RuntimeJobPayload,
    token: CancellationToken,
    handle: RuntimeHandle,
    *,
    request_id: UUID,
    clock: Callable[[], datetime],
    deadline_at: datetime,
    matched_session_id: UUID | None,
    credential_scope: str,
    ttl_hours: int,
    retention_days: int,
) -> None:
    ordinal = 1
    while True:
        token.checkpoint()
        now = clock()
        if now >= deadline_at and not handle.finished():
            handle.cancel()
            _fail(
                uow,
                payload,
                now=now,
                error_code=DEADLINE_EXCEEDED,
                error_text="the run exceeded its deadline",
                request_id=request_id,
            )
            return
        event = handle.poll()
        if event is None:
            if handle.finished():
                _fail(
                    uow,
                    payload,
                    now=now,
                    error_code=STREAM_TRUNCATED,
                    error_text="the run ended without a terminal event",
                    request_id=request_id,
                )
                return
            continue
        if isinstance(event, ProgressEvent):
            _insert_transcript(
                uow,
                request_id=request_id,
                ordinal=ordinal,
                kind="progress",
                body=event.text,
                now=now,
                retention_days=retention_days,
            )
            set_progress(
                uow.connection,
                operation_id=payload.operation_id,
                progress_text=event.text,
                progress_fraction=event.fraction,
            )
            ordinal += 1
            continue
        if isinstance(event, ToolResultEvent):
            _insert_transcript(
                uow,
                request_id=request_id,
                ordinal=ordinal,
                kind="tool_result_summary",
                body=event.outcome.status,
                now=now,
                retention_days=retention_days,
            )
            ordinal += 1
            continue
        if isinstance(event, ToolCallEvent):
            continue
        if isinstance(event, UsageEvent):
            uow.connection.execute(
                update(runtime_tables.runtime_request)
                .where(runtime_tables.runtime_request.c.id == request_id)
                .values(
                    usage_input_tokens=event.input_tokens,
                    usage_output_tokens=event.output_tokens,
                    usage_cost=event.cost,
                    usage_kind=event.kind.value,
                )
            )
            continue
        if isinstance(event, FinalOutputEvent):
            if "structured" in event.model_fields_set:
                body = json.dumps(event.structured, default=str)
            else:
                body = "" if event.text is None else event.text
            _insert_transcript(
                uow,
                request_id=request_id,
                ordinal=ordinal,
                kind="model_output",
                body=body,
                now=now,
                retention_days=retention_days,
            )
            native = handle.native_handle or ""
            _write_session(
                uow,
                payload,
                native_handle=native,
                matched_session_id=matched_session_id,
                credential_scope=credential_scope,
                now=now,
                ttl_hours=ttl_hours,
            )
            uow.connection.execute(
                update(runtime_tables.runtime_request)
                .where(runtime_tables.runtime_request.c.id == request_id)
                .values(terminal_event="final_output", ended_at=now)
            )
            return
        if isinstance(event, FailureEvent):
            _fail(
                uow,
                payload,
                now=now,
                error_code=event.kind.value,
                error_text=event.detail,
                request_id=request_id,
            )
            return
        if isinstance(event, ApprovalRequiredEvent):
            mark_approval_required(
                uow.connection,
                operation_id=payload.operation_id,
                approval_id=event.approval_id,
            )
            uow.connection.execute(
                update(runtime_tables.runtime_request)
                .where(runtime_tables.runtime_request.c.id == request_id)
                .values(terminal_event="approval_required", ended_at=now)
            )
            return
        if isinstance(event, CancelledEvent):
            finish_cancelled(uow.connection, operation_id=payload.operation_id, now=now)
            uow.connection.execute(
                update(runtime_tables.runtime_request)
                .where(runtime_tables.runtime_request.c.id == request_id)
                .values(terminal_event="cancelled", ended_at=now)
            )
            return


def make_run_runtime_job(
    registry: AdapterRegistry, clock: Callable[[], datetime]
) -> Callable[[HandlerUnitOfWork, BaseModel, CancellationToken], None]:
    """Close the job handler over a process registry and the worker clock."""

    def run_runtime_job(
        uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
    ) -> None:
        assert isinstance(payload, RuntimeJobPayload)
        token_id: UUID | None = None
        handle: RuntimeHandle | None = None
        work_dir_path = run_dir_for(payload.workspace_id, payload.operation_id)
        try:
            ctx = context_from_operation(
                payload.workspace_id,
                actor_kind=payload.actor_kind,
                actor_id=payload.actor_id,
                audience_kind=payload.audience_kind,
                audience_id=payload.audience_id,
                entry=payload.entry,
                purpose=payload.purpose,
            )
            if isinstance(ctx, Refusal):
                _fail(
                    uow,
                    payload,
                    now=clock(),
                    error_code=ctx.state,
                    error_text=str(ctx),
                )
                return
            account_id = _job_account_id(payload)
            adapter = registry.lookup(payload.runtime_id)
            if adapter is None:
                _fail(
                    uow,
                    payload,
                    now=clock(),
                    error_code=RUNTIME_UNKNOWN,
                    error_text=f"unknown runtime {payload.runtime_id!r}",
                )
                return
            try:
                check_runtime_gate(payload.requirements, adapter.capabilities())
            except UnmetCapability as exc:
                _fail(
                    uow,
                    payload,
                    now=clock(),
                    error_code=exc.name,
                    error_text=f"unmet capability {exc.name}",
                )
                return
            settings = resolve(
                workspace_id=payload.workspace_id, source=PostgresOverrideSource()
            )
            if settings.get_str("runtime.claude_cli.credential_kind") == "login":
                wanted = settings.get_str("runtime.claude_cli.credential_account_id")
                if account_id is None or str(account_id) != wanted:
                    _fail(
                        uow,
                        payload,
                        now=clock(),
                        error_code=CREDENTIAL_NOT_OWNED,
                        error_text="the originating account does not own the login",
                    )
                    return
            max_deadline = settings.get_int("runtime.max_deadline_seconds")
            deadline = min(
                payload.deadline_seconds, max_deadline, HARD_DEADLINE_SECONDS
            )
            if deadline <= 0:
                _fail(
                    uow,
                    payload,
                    now=clock(),
                    error_code=DEADLINE_EXCEEDED,
                    error_text="deadline_seconds is not positive",
                )
                return
            items = build_context(ctx, payload.refs, uow=uow)
            digest = _context_digest(items)
            output = _OUTPUT_ADAPTER.validate_python(payload.output)
            purpose = ContextPurpose(payload.purpose)
            tools = _permitted_tools(ctx, payload.permitted_tools)
            audience = ctx.audience
            if audience is None:
                _fail(
                    uow,
                    payload,
                    now=clock(),
                    error_code=RUNTIME_ACTOR_REQUIRED,
                    error_text="a runtime run needs an audience",
                )
                return
            limits = RuntimeLimits(
                deadline_seconds=deadline,
                max_iterations=payload.max_iterations,
                max_output_bytes=RuntimeLimits.DEFAULT_MAX_OUTPUT_BYTES,
            )
            request = build_runtime_request(
                operation_id=payload.operation_id,
                actor=ctx.actor,
                workspace_id=ctx.workspace_id,
                audience=audience,
                purpose=purpose,
                # The task text goes to the provider like any context item, so it is
                # masked under the same policy: secret references always, contact
                # values unless the allowance is on. Masked here rather than in an
                # adapter, so no adapter can forget it.
                task=mask_for_model(
                    payload.task, TierPolicy.from_settings(purpose_of(ctx), settings)
                ),
                context_items=items,
                permitted_tools=tools,
                output=output,
                requirements=list(payload.requirements),
                limits=limits,
                continuation=payload.continuation,
                credential_slot=CREDENTIAL_SLOT,
                runtime_id=payload.runtime_id,
                model_id=payload.model_id,
            )
            now = clock()
            request_id = uuid7()
            uow.connection.execute(
                insert(runtime_tables.runtime_request).values(
                    id=request_id,
                    operation_id=payload.operation_id,
                    runtime_id=payload.runtime_id,
                    model_id=payload.model_id,
                    purpose=payload.purpose,
                    context_digest=digest,
                    permitted_tools=tools,
                    requirements=list(payload.requirements),
                    deadline_seconds=deadline,
                    max_iterations=payload.max_iterations,
                    max_output_bytes=RuntimeLimits.DEFAULT_MAX_OUTPUT_BYTES,
                    terminal_event=None,
                    started_at=now,
                )
            )
            if payload.refs:
                uow.connection.execute(
                    insert(runtime_tables.runtime_request_context),
                    [{"request_id": request_id, "ref": ref} for ref in payload.refs],
                )
            credential_scope = _credential_scope(settings)
            matched_id, native_handle = _lookup_continuation(
                uow,
                payload,
                credential_scope=credential_scope,
                now=now,
            )
            snapshot = _snapshot_operations(ctx, tools)
            if account_id is None:
                _fail(
                    uow,
                    payload,
                    now=clock(),
                    error_code=RUNTIME_ACTOR_REQUIRED,
                    error_text="the originating actor has no backing account",
                    request_id=request_id,
                )
                return
            expires_at = now + timedelta(seconds=deadline)
            token_id, run_token = issue_runtime_token(
                account_id=account_id,
                workspace_id=payload.workspace_id,
                purpose=payload.purpose,
                expires_at=expires_at,
                operations=snapshot,
            )
            config_dir = (
                workspace_dir_for(payload.workspace_id, Purpose.SCRATCH) / "claude-cli"
            )
            work_dir_path.mkdir(parents=True, exist_ok=True)
            spawn = AdapterSpawn(
                mcp_url=url_for(RoutingConfig.from_settings(resolve()), MCP, "/"),
                run_token=run_token,
                work_dir=str(work_dir_path),
                config_dir=str(config_dir),
                native_handle=native_handle,
            )
            handle = adapter.start(request, spawn=spawn)
            _poll_loop(
                uow,
                payload,
                token,
                handle,
                request_id=request_id,
                clock=clock,
                deadline_at=expires_at,
                matched_session_id=matched_id,
                credential_scope=credential_scope,
                ttl_hours=settings.get_int("runtime.session_ttl_hours"),
                retention_days=settings.get_int("runtime.transcript_retention_days"),
            )
        finally:
            # However the loop ended (the deadline, a cancelled job raising out of
            # ``token.checkpoint()``, a lost lease, any other exception), a process
            # still running is killed before its token and working directory go
            # (#128). ``cancel`` is a no-op once the process has exited.
            if handle is not None and not handle.finished():
                handle.cancel()
            if token_id is not None:
                with get_backend().control_engine.begin() as connection:
                    delete_access_token(connection, token_id)
            if work_dir_path.exists():
                shutil.rmtree(work_dir_path, ignore_errors=True)

    return run_runtime_job
