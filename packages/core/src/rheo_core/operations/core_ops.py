"""The core operations registered under the ``core`` origin, matching
``docs/architecture/module-contract.md:96-105`` for names, classes and roles.
0b1 (C4) shipped the first three below; C8 (09, run 0b2) adds the two token
operations at the end of this module; C3 (run 0c1) adds ``core.work.failures``,
whose own row is the ``:105`` the cited range now reaches.

- ``core.workspace.status`` — ``read``; roles ``owner, member, operator``. Reads
  ``core.workspace_composition``, ``core.module_state`` and
  ``core.module_schema_version`` through the storage repositories, never a table
  named ``alembic_version%``. ``audit = None``: audit is required only above ``READ``.
- ``core.settings.set`` — ``mutate``; roles ``owner`` **only**. C2's
  ``validate_override`` then the ``core.workspace_setting`` row; a refusal state is
  returned as the outcome's state.
- ``core.settings.set_member`` — ``mutate``; roles ``owner, member``; writes the
  calling account's own ``core.member_setting`` row — ``account_id`` comes from
  ``ctx.actor.id`` and never from the payload.
- ``core.token.issue`` / ``core.token.revoke`` — ``mutate``; roles ``owner,
  member, operator`` (see :data:`TOKEN_ISSUE_DECLARATION`'s own docstring for why
  ``operator`` is safe to declare here). Handlers live in
  ``rheo_core.tokens.issue`` — this module only registers them.
- ``core.work.failures`` — ``read``; roles ``owner, operator``, and **not** the
  declaration default ``owner, member``. Reads the failed jobs through C1's
  ``rheo_core.work.jobs.list_failed_jobs``, never the ``core.job`` table
  directly. Its models and handler live in ``rheo_core.work.operations``, beside
  that repository; only the declaration is here. ``audit = None`` for the same
  reason ``core.workspace.status`` carries it.
- ``core.operation.get`` / ``core.operation.list`` — ``read``; roles ``owner,
  member, operator``. The supported reads of an operation record (run 0c2, C4).
- ``core.operation.resolve`` — ``mutate``; roles ``owner, operator``. Clears an
  ``unresolved`` record to a named outcome with a note, and refuses a record in
  any other state. The models and handlers of all three live in
  ``rheo_core.operations.operation_ops``, beside the repository they read; only
  the declarations are here, the same split ``core.work.failures`` uses.
- ``core.approval.approve`` / ``core.approval.refuse`` — ``mutate``; roles
  ``owner, member`` (``module-contract.md``'s own row for the pair). Both are
  members of ``rheo_core.tokens.policy.NON_TOKEN_ISSUABLE``, so no token of any
  kind can carry either and a model cannot approve what it proposed. Their
  declarations, models and handlers all live in ``rheo_core.approvals.operations``
  — the one pair whose *declaration* is not in this module, because the import
  direction forbids it (see :func:`register_core_operations`); this module only
  registers them.
- ``core.audit.list`` — ``read``; roles ``owner, operator``, ratified at
  ``module-contract.md:105``. The supported read of the audit record (run 0c2,
  C5). Its models and handler live in ``rheo_core.audit.operations``, beside the
  repository they read — the same split again, and here it is also an
  import-direction constraint, since ``rheo_core.audit`` may not import this
  package.

Every mutate operation here declares ``AuditSpec(subject_field=None)``: C1 fixes
``AuditSpec`` at exactly that one field, and none of the mutate operations in
this module acts on a *record type* — a ``RecordRef`` in an input model is what
``subject_field`` names, and none of them has one. The read-class declarations
carry no ``AuditSpec`` at all, which stays legal — and is now enforced from the
other side too: registration refuses a non-``read`` declaration that carries
none. Acting on the spec is this run's: ``dispatch()`` writes a
``core.audit_record`` row for every one of these operations above the read class,
through the sink :func:`register_core_operations` installs below.

Registration is explicit (:func:`register_core_operations`), not an import side
effect, mirroring the settings harness.
"""

from typing import Final

from pydantic import BaseModel, ConfigDict
from rheo_contracts import (
    ActorKind,
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)

# ``AUDIT_LIST as AUDIT_LIST`` is a deliberate re-export, spelled the way
# ``refusals.py`` re-exports ``HANDLER_MAY_NOT_COMMIT``: the operation name belongs
# beside the rest of the core roster for a caller reading ``rheo_core.operations``,
# and the redundant alias is what tells mypy strict this is not an implicitly public
# import. The name itself is declared in ``rheo_core.audit.operations``, beside the
# models and handler it names, which may not import this package.
from rheo_core.audit import (
    AUDIT_LIST as AUDIT_LIST,
)
from rheo_core.audit import (
    CORE_AUDIT_SINK,
    AuditList,
    AuditListInput,
    audit_list_handler,
    install_sink,
)
from rheo_core.operations.operation_ops import (
    OPERATION_GET,
    OPERATION_LIST,
    OPERATION_RESOLVE,
    OperationList,
    OperationListInput,
    OperationRecord,
    OperationRef,
    OperationResolveInput,
    get_handler,
    list_handler,
    resolve_handler,
)
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import (
    CORE_MODULE_ID,
    REGISTRY,
    Handler,
    OperationRegistry,
    RegisteredOperation,
)
from rheo_core.settings import (
    CORE_ORIGIN,
    Scope,
    SettingAccepted,
    SettingRefusal,
    SettingValue,
    resolve,
    validate_override,
)
from rheo_core.settings import (
    REGISTRY as SETTINGS_REGISTRY,
)
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.repositories import (
    list_module_schema_versions,
    list_module_states,
    read_composition,
    upsert_member_setting,
    upsert_workspace_setting,
)
from rheo_core.work.operations import (
    FailureList,
    FailureListInput,
    failures_handler,
)

WORKSPACE_STATUS: Final = "core.workspace.status"
SETTINGS_SET: Final = "core.settings.set"
SETTINGS_SET_MEMBER: Final = "core.settings.set_member"
TOKEN_ISSUE: Final = "core.token.issue"
TOKEN_REVOKE: Final = "core.token.revoke"
"""``TOKEN_ISSUE``/``TOKEN_REVOKE`` are literal strings, matching
``rheo_core.tokens.policy.NON_TOKEN_ISSUABLE``'s own two hardcoded members — see
that module's docstring for why they are duplicated rather than imported from
each other (a real import cycle either way)."""

WORK_FAILURES: Final = "core.work.failures"

_TOKEN_OPERATION_ROLES: Final = frozenset({Role.OWNER, Role.MEMBER, Role.OPERATOR})
"""Shared by both token declarations, built inside :func:`register_core_operations`
— see that function's own docstring for why ``operator`` is safe to declare, and
``00-index.md``'s coupling notes for the same reasoning stated for the run."""

COMPOSITION_MISSING: Final = "composition_missing"
ACTOR_REQUIRED: Final = "actor_required"

# ``extra = "ignore"`` is pydantic's default, stated explicitly on every input model
# here because it is load-bearing for criterion 6 (B2): a dispatch payload that also
# carries ``workspace_id``, ``database``, ``dsn``, ``connection_string`` or ``schema``
# values naming another workspace must have those keys **dropped** and the operation
# must proceed against the context's workspace. ``extra = "forbid"`` looks stricter
# but would turn that payload into ``input_invalid``, and the "ignored, and the
# authenticated context is used" half of criterion 6 would be unreachable — the test
# would pass a refusal and prove nothing about routing. Criterion 6's refusal channel
# is the registration-time reserved-field check in ``registry.py``, a different
# mechanism; both are required. (``extra = "allow"`` is refused at registration.)
_IGNORE_EXTRA: Final = ConfigDict(extra="ignore")


class WorkspaceStatusInput(BaseModel):
    """No fields: the workspace comes from the context."""

    model_config = _IGNORE_EXTRA


class ModuleStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    module_id: str
    package_version: str
    state: str
    schema_version: str | None


class WorkspaceStatus(BaseModel):
    """FR-9's readable product data for one workspace."""

    model_config = ConfigDict(frozen=True)

    core_version: str
    core_contract_version: int
    modules: list[ModuleStatus]


class SettingWrite(BaseModel):
    """One key and its natively typed value (a list for ``list[str]`` keys)."""

    model_config = _IGNORE_EXTRA

    key: str
    value: bool | int | str | list[str]


class SettingWritten(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    value: bool | int | str | list[str]
    scope: str


def _workspace_status(
    ctx: WorkspaceContext, uow: UnitOfWork, _input: WorkspaceStatusInput
) -> WorkspaceStatus:
    connection = uow.connection
    composition = read_composition(connection)
    if composition is None:
        raise OperationRefused(
            COMPOSITION_MISSING, "the workspace has no core.workspace_composition row"
        )
    states = list_module_states(connection)
    latest: dict[str, str] = {}
    for version in list_module_schema_versions(connection):
        # Rows come ordered by applied_at, so the last write per module wins.
        latest[version.module_id] = version.schema_version
    return WorkspaceStatus(
        core_version=composition.core_version,
        core_contract_version=composition.core_contract_version,
        modules=[
            ModuleStatus(
                module_id=state.module_id,
                package_version=state.package_version,
                state=state.state,
                schema_version=latest.get(state.module_id),
            )
            for state in states
        ],
    )


def _validated(key: str, value: SettingValue, scope: Scope) -> SettingAccepted:
    # The deployment value is what the deployment layer resolves for the key (its
    # package default when the deployment sets nothing); an undeclared key has none
    # and ``validate_override`` refuses it first.
    deployment_value: object = resolve()[key] if key in SETTINGS_REGISTRY else None
    verdict = validate_override(key, value, deployment_value, scope=scope)
    if isinstance(verdict, SettingRefusal):
        raise OperationRefused(verdict.state, verdict.detail)
    return verdict


def _settings_set(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: SettingWrite
) -> SettingWritten:
    accepted = _validated(model_input.key, model_input.value, Scope.WORKSPACE)
    upsert_workspace_setting(
        uow.connection,
        key=accepted.key,
        value=accepted.encoded,
        value_type=accepted.value_type,
        updated_by=ctx.actor.id,
    )
    return SettingWritten(
        key=accepted.key, value=model_input.value, scope=Scope.WORKSPACE.value
    )


def _settings_set_member(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: SettingWrite
) -> SettingWritten:
    # The row is the caller's own: the account id is the context's actor, never a
    # payload field (``actor_id`` is a reserved input field and cannot be declared).
    if ctx.actor.kind is not ActorKind.ACCOUNT or ctx.actor.id is None:
        raise OperationRefused(
            ACTOR_REQUIRED,
            "core.settings.set_member writes the calling account's row; this "
            "context carries no account",
        )
    accepted = _validated(model_input.key, model_input.value, Scope.MEMBER)
    upsert_member_setting(
        uow.connection,
        account_id=ctx.actor.id,
        key=accepted.key,
        value=accepted.encoded,
        value_type=accepted.value_type,
    )
    return SettingWritten(
        key=accepted.key, value=model_input.value, scope=Scope.MEMBER.value
    )


WORKSPACE_STATUS_DECLARATION: Final = OperationDeclaration(
    name=WORKSPACE_STATUS,
    safety_class=SafetyClass.READ,
    roles=frozenset({Role.OWNER, Role.MEMBER, Role.OPERATOR}),
    input_model=WorkspaceStatusInput,
    output=WorkspaceStatus,
    idempotency=Idempotency.NONE,
    audit=None,
)

SETTINGS_SET_DECLARATION: Final = OperationDeclaration(
    name=SETTINGS_SET,
    safety_class=SafetyClass.MUTATE,
    roles=frozenset({Role.OWNER}),
    input_model=SettingWrite,
    output=SettingWritten,
    # The ``core.workspace_setting`` primary key on ``key`` makes a repeat the same
    # row, which is what ``NATURAL`` names.
    idempotency=Idempotency.NATURAL,
    audit=AuditSpec(subject_field=None),
)

SETTINGS_SET_MEMBER_DECLARATION: Final = OperationDeclaration(
    name=SETTINGS_SET_MEMBER,
    safety_class=SafetyClass.MUTATE,
    roles=frozenset({Role.OWNER, Role.MEMBER}),
    input_model=SettingWrite,
    output=SettingWritten,
    idempotency=Idempotency.NATURAL,
    audit=AuditSpec(subject_field=None),
)

WORK_FAILURES_DECLARATION: Final = OperationDeclaration(
    name=WORK_FAILURES,
    safety_class=SafetyClass.READ,
    # ``docs/architecture/module-contract.md:105`` ratifies ``owner, operator``.
    # ``OperationDeclaration``'s own default is ``{OWNER, MEMBER}``, so leaving
    # this field off would silently ship the wrong pair rather than fail.
    roles=frozenset({Role.OWNER, Role.OPERATOR}),
    input_model=FailureListInput,
    output=FailureList,
    idempotency=Idempotency.NONE,
    # Audit is required only above ``READ`` — this module's own top-of-file
    # docstring makes the point, and ``WORKSPACE_STATUS_DECLARATION`` above
    # relies on the same sentence rather than on a comment of its own.
    audit=None,
)

OPERATION_GET_DECLARATION: Final = OperationDeclaration(
    name=OPERATION_GET,
    safety_class=SafetyClass.READ,
    # ``docs/architecture/module-contract.md:96`` ratifies ``owner, member,
    # operator``; the declaration default is ``{OWNER, MEMBER}``, so leaving this
    # field off would silently ship the wrong set rather than fail.
    roles=frozenset({Role.OWNER, Role.MEMBER, Role.OPERATOR}),
    input_model=OperationRef,
    output=OperationRecord,
    idempotency=Idempotency.NONE,
    audit=None,
)

OPERATION_LIST_DECLARATION: Final = OperationDeclaration(
    name=OPERATION_LIST,
    safety_class=SafetyClass.READ,
    roles=frozenset({Role.OWNER, Role.MEMBER, Role.OPERATOR}),
    input_model=OperationListInput,
    output=OperationList,
    idempotency=Idempotency.NONE,
    audit=None,
)

OPERATION_RESOLVE_DECLARATION: Final = OperationDeclaration(
    name=OPERATION_RESOLVE,
    safety_class=SafetyClass.MUTATE,
    # ``docs/architecture/module-contract.md:107`` ratifies ``owner, operator``.
    roles=frozenset({Role.OWNER, Role.OPERATOR}),
    input_model=OperationResolveInput,
    output=OperationRecord,
    # Each resolution is its own act on its own record; nothing makes a repeat the
    # same row, and the second call refuses because the record is no longer
    # ``unresolved``.
    idempotency=Idempotency.NONE,
    # ``MUTATE``, so this is required and not optional: 0c2's registration rule
    # refuses a non-``READ`` declaration carrying ``audit = None``, and
    # ``register_core_operations()`` would then raise on every call, startup
    # included. ``subject_field=None`` because ``AuditSpec.subject_field`` names an
    # *input-model field carrying a RecordRef* (``manifest.py:60-72``) and
    # ``core.operation`` is not a registered record type with a resolver — this
    # operation's input names an operation id, which is not one.
    audit=AuditSpec(subject_field=None),
)

AUDIT_LIST_DECLARATION: Final = OperationDeclaration(
    name=AUDIT_LIST,
    safety_class=SafetyClass.READ,
    # ``docs/architecture/module-contract.md:105`` ratifies ``owner, operator`` —
    # the same row ``core.work.failures`` reads. ``intake-and-events.md`` said
    # "(owner only)" in running prose and loses to the table that owns the core
    # operation roster; C5 amends that line. The substantive reason is that
    # ``operator`` already holds ``core.workspace.export`` and ``.digest``
    # (``module-contract.md:107``), so withholding the *listing* from the role
    # most likely to be reading it during an incident protects nothing.
    roles=frozenset({Role.OWNER, Role.OPERATOR}),
    input_model=AuditListInput,
    output=AuditList,
    idempotency=Idempotency.NONE,
    # ``READ``, so this stays ``None``; audit is required only above the read class.
    audit=None,
)

CORE_OPERATIONS: Final[tuple[tuple[OperationDeclaration, Handler], ...]] = (
    (WORKSPACE_STATUS_DECLARATION, _workspace_status),
    (SETTINGS_SET_DECLARATION, _settings_set),
    (SETTINGS_SET_MEMBER_DECLARATION, _settings_set_member),
    (WORK_FAILURES_DECLARATION, failures_handler),
    (OPERATION_GET_DECLARATION, get_handler),
    (OPERATION_LIST_DECLARATION, list_handler),
    (OPERATION_RESOLVE_DECLARATION, resolve_handler),
    (AUDIT_LIST_DECLARATION, audit_list_handler),
)
"""The three 0b1 operations, 0c1's ``core.work.failures``, and 0c2's three
``core.operation`` operations plus ``core.audit.list``. The two token operations
are not here: see
:func:`register_core_operations`'s own docstring for why they are built inside the
function instead of as module-level ``Final`` declarations like these. None of the
others needs that deferral — ``rheo_core.work`` and ``rheo_core.audit`` each close
no cycle with ``rheo_core.operations`` (both are imported *by* it and import none
of it back), and ``operation_ops`` is inside this package — so every one of them
belongs in this tuple."""


def register_core_operations(
    registry: OperationRegistry = REGISTRY,
) -> tuple[RegisteredOperation, ...]:
    """Register every core operation (idempotent) under the ``core`` origin.

    Every declaration in :data:`CORE_OPERATIONS`, plus the two token operations
    built below — deliberately not a count, because a count in this sentence goes
    false the next time a run adds a declaration and nothing checks it.

    The two token declarations are built **here**, inside the function, rather
    than as module-level constants like :data:`CORE_OPERATIONS`'s:
    building them needs ``rheo_core.tokens.issue``'s handlers and models, and a
    module-level import of that module here would close a real cycle —
    ``rheo_core.operations``'s own ``__init__.py`` imports this module as its
    first statement, and ``tokens.issue`` (via ``tokens.sets``) reaches back
    into ``rheo_core.operations.registry``, so the two modules would each be
    only partially initialised by the time the other needed it, whichever
    happened to be imported first. This function is never called as an import
    side effect (only explicitly, at startup or by a test's own fixture), so
    deferring the import to here avoids the cycle regardless of import order.

    Declaring ``operator`` in :data:`_TOKEN_OPERATION_ROLES` is safe: both
    operations are themselves members of ``NON_TOKEN_ISSUABLE``
    (``rheo_core.tokens.policy``), so no token can ever carry either and
    present it back at a bearer surface — the only way to reach these two
    under an operator role is ``context_for_operator``, constructed nowhere
    but the ``rheo`` CLI's own bootstrap. Omitting ``operator`` here would
    instead make every ``rheo token issue``/``revoke`` invocation refuse
    ``role_not_permitted`` before its handler ever ran (mirroring
    ``WORKSPACE_STATUS_DECLARATION``'s own reason for declaring it).

    **The two approval operations are imported here too, and for a second reason.**
    ``rheo_core.approvals`` imports this package's submodules at module level —
    ``approvals/gate.py`` needs the operation-record repository, the registry and the
    refusal vocabulary — so the import must not run the other way while this module
    is still loading. Deferring it to call time avoids that regardless of import
    order, exactly as the token import above does. Their declarations live beside
    their handlers in ``rheo_core.approvals.operations`` rather than in this file for
    the same reason: a declaration here would have to be built inside this function.

    **It also installs the core's audit sink** (D4). The core is a module with no
    manifest, so ``modules/loader.py`` never loads a sink for it and the
    registration that declares the operations is the only place that knows the
    ``core`` module needs one. Installed *before* the registrations rather than
    after: ``register`` refuses a declaration above the read class with no
    ``AuditSpec``, so a partial failure mid-registration would otherwise leave a
    registry holding mutate operations whose module has no sink — which is exactly
    the state ``check_audit_paths`` exists to refuse. ``install_sink`` is a no-op
    for the identical object, which is what lets this run twice in one process.
    """
    from rheo_core.approvals.operations import APPROVAL_OPERATIONS
    from rheo_core.tokens.issue import (
        TokenIssued,
        TokenIssueInput,
        TokenRevoked,
        TokenRevokeInput,
        issue_handler,
        revoke_handler,
    )

    install_sink(CORE_MODULE_ID, CORE_AUDIT_SINK)
    token_operations: tuple[tuple[OperationDeclaration, Handler], ...] = (
        (
            OperationDeclaration(
                name=TOKEN_ISSUE,
                safety_class=SafetyClass.MUTATE,
                roles=_TOKEN_OPERATION_ROLES,
                input_model=TokenIssueInput,
                output=TokenIssued,
                idempotency=Idempotency.NONE,
                audit=AuditSpec(subject_field=None),
            ),
            issue_handler,
        ),
        (
            OperationDeclaration(
                name=TOKEN_REVOKE,
                safety_class=SafetyClass.MUTATE,
                roles=_TOKEN_OPERATION_ROLES,
                input_model=TokenRevokeInput,
                output=TokenRevoked,
                idempotency=Idempotency.NONE,
                audit=AuditSpec(subject_field=None),
            ),
            revoke_handler,
        ),
    )
    return tuple(
        registry.register(declaration, handler, origin=CORE_ORIGIN)
        for declaration, handler in CORE_OPERATIONS
        + token_operations
        + APPROVAL_OPERATIONS
    )
