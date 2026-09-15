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

Every mutate operation here declares ``AuditSpec(subject_field=None)``: C1 fixes
``AuditSpec`` at exactly that one field, and none of these six operations acts on
a record (``core.work.failures`` is ``read``-class and declares no ``AuditSpec``
at all, so it moves the count and nothing else in this paragraph). Declaring it
is required so 0c2's audit dispatcher re-declares nothing; acting on it (writing
an audit row) is 0c2's and does not happen in this run.

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

from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import (
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

CORE_OPERATIONS: Final[tuple[tuple[OperationDeclaration, Handler], ...]] = (
    (WORKSPACE_STATUS_DECLARATION, _workspace_status),
    (SETTINGS_SET_DECLARATION, _settings_set),
    (SETTINGS_SET_MEMBER_DECLARATION, _settings_set_member),
    (WORK_FAILURES_DECLARATION, failures_handler),
)
"""The three 0b1 operations, plus 0c1's ``core.work.failures``. The two token
operations are not here: see :func:`register_core_operations`'s own docstring for
why they are built inside the function instead of as module-level ``Final``
declarations like these four. ``core.work.failures`` needs no such deferral —
``rheo_core.work`` closes no cycle with ``rheo_core.operations`` — so it belongs
in this tuple."""


def register_core_operations(
    registry: OperationRegistry = REGISTRY,
) -> tuple[RegisteredOperation, ...]:
    """Register the six core operations (idempotent) under the ``core`` origin.

    The two token declarations are built **here**, inside the function, rather
    than as module-level constants like :data:`CORE_OPERATIONS`'s four:
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
    """
    from rheo_core.tokens.issue import (
        TokenIssued,
        TokenIssueInput,
        TokenRevoked,
        TokenRevokeInput,
        issue_handler,
        revoke_handler,
    )

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
        for declaration, handler in CORE_OPERATIONS + token_operations
    )
