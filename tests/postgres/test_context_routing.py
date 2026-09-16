"""B2's storage half (criterion 6): storage routing and actor identity come from the
authenticated context and from nothing a caller supplies.

- ``route()`` and ``open_unit_of_work()`` accept exactly one parameter (introspection).
- Registering an operation whose input model declares any name in
  ``RESERVED_INPUT_FIELDS`` — by field name or by alias, ``actor_id`` included — is
  refused naming the field; so is ``extra = "allow"``; and so is a declaration above
  the read class carrying no ``AuditSpec`` (AC 21, criterion 14's declaration layer,
  which lives in this file because ``register`` is what enforces it).
- A ``dispatch`` whose payload carries ``workspace_id``, ``database``, ``dsn``,
  ``connection_string`` and ``schema`` values naming workspace B lands its write in
  workspace A (the context's) and leaves B unchanged; the resolver seam then finds
  the record from A and not from B.
- The same names presented as workspace-setting rows through ``core.settings.set``
  are refused ``setting_undeclared`` and no row is written.
- ``actor_id`` in a payload is ignored at dispatch: ``core.settings.set_member``
  writes the context's account, never the payload's.
- B1 re-proved through the registry: a ``core.settings.set`` in A leaves B's row
  count and resolved value unchanged.

The HTTP JSON-body and query-string channels are the ``api`` surface (C8, 0b2) and
are not claimed here.
"""

import inspect
import logging
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.records import ensure_note_table, get_note, list_notes
from harness.registry import (
    NOTE_EXPLODE,
    NOTE_GET,
    NOTE_WRITE,
    NoteWriteInput,
    add_member,
    enable_harness_module,
    probe_declaration,
    probe_handler,
    register_harness,
    reserved_field_models,
    resolve_note,
)
from harness.settings_keys import HARNESS_MEMBER
from pydantic import BaseModel, ConfigDict
from rheo_contracts import (
    RESERVED_INPUT_FIELDS,
    RecordRef,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.operations import (
    FAILED,
    HANDLER_FAILED,
    MODULE_DISABLED,
    REGISTRY,
    SETTINGS_SET,
    SETTINGS_SET_MEMBER,
    OperationError,
    OperationRegistry,
    RegistrationRefused,
    dispatch,
    register_core_operations,
)
from rheo_core.refs import uuid7
from rheo_core.refs.resolver import (
    NOT_FOUND,
    REFERENCE_MALFORMED,
    UNRESOLVABLE,
    RecordHead,
    ResolverRegistry,
    Unavailable,
    resolve,
)
from rheo_core.settings import CORE_ORIGIN, TEST_HARNESS_ORIGIN
from rheo_core.settings import resolve as resolve_settings
from rheo_core.settings.storage_source import PostgresOverrideSource
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.control_plane import WorkspaceRow
from rheo_core.storage.repositories import member_settings, workspace_settings
from rheo_core.storage.routing import open_unit_of_work, route

pytestmark = pytest.mark.postgres

TOKEN_DAYS_CLI = "identity.token_max_days.cli"


@pytest.fixture(autouse=True)
def registrations(harness_keys: None) -> None:
    register_core_operations()
    register_harness()


@pytest.fixture
def two_workspaces(
    cluster: ClusterSession, make_workspace: MakeWorkspace
) -> tuple[WorkspaceRow, WorkspaceRow]:
    a, b = (
        cluster.registry_row(make_workspace()),
        cluster.registry_row(make_workspace()),
    )
    for row in (a, b):
        engine = cluster.backend.pools.engine_for(row.database_name)
        with UnitOfWork(engine, row.database_name) as uow:
            ensure_note_table(uow.connection)
            enable_harness_module(uow.connection)
            uow.commit()
    return a, b


def _uow(cluster: ClusterSession, row: WorkspaceRow) -> UnitOfWork:
    engine = cluster.backend.pools.engine_for(row.database_name)
    return UnitOfWork(engine, row.database_name)


def _operator(workspace_id: UUID) -> WorkspaceContext:
    ctx = context_for_operator(workspace_id)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _harness(workspace_id: UUID, account_id: UUID, role: Role) -> WorkspaceContext:
    ctx = context_for_harness(workspace_id, account_id, role)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


# --- the signature --------------------------------------------------------------------


@pytest.mark.parametrize("function", [route, open_unit_of_work])
def test_routing_accepts_exactly_one_parameter(function: object) -> None:
    parameters = inspect.signature(function).parameters  # type: ignore[arg-type]
    assert list(parameters) == ["ctx"]
    (only,) = parameters.values()
    assert only.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert only.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        route(uuid7())  # type: ignore[arg-type]


# --- the registration channel ---------------------------------------------------------


def test_the_reserved_list_is_the_ratified_one() -> None:
    assert RESERVED_INPUT_FIELDS == {
        "workspace_id", "workspace", "actor_id", "actor", "tenant_id", "database",
        "schema", "connection_string", "dsn", "sql", "table_name", "statement",
    }  # fmt: skip


@pytest.mark.parametrize("field_name", sorted(RESERVED_INPUT_FIELDS))
def test_registering_a_reserved_input_field_is_refused_naming_it(
    field_name: str,
) -> None:
    models = reserved_field_models(field_name)
    assert models, field_name
    for model in models:
        registry = OperationRegistry()
        name = "harness.probe.reserved"
        with pytest.raises(RegistrationRefused) as excinfo:
            registry.register(
                probe_declaration(name, model),
                probe_handler,
                origin=TEST_HARNESS_ORIGIN,
            )
        assert excinfo.value.operation_name == name
        assert field_name in excinfo.value.detail, (model, excinfo.value.detail)
        assert name not in registry


@pytest.mark.parametrize(
    "safety_class",
    [
        SafetyClass.DRAFT,
        SafetyClass.MUTATE,
        SafetyClass.DESTRUCTIVE,
        SafetyClass.EXTERNAL,
        SafetyClass.FINANCIAL,
    ],
)
def test_registering_a_non_read_operation_with_no_audit_spec_is_refused(
    safety_class: SafetyClass,
) -> None:
    """AC 21 (criterion 14's declaration layer): an operation above the read class
    cannot be registered without an ``AuditSpec``, and the refusal names it.

    **Every non-``READ`` member, not only ``MUTATE``.** ``SafetyClass`` is an unordered
    ``StrEnum``, so "above read" is ``is not SafetyClass.READ`` — there is no ordering
    to compare against — and a rule written as ``is SafetyClass.MUTATE`` would let a
    ``destructive`` or ``financial`` declaration through unaudited while every shipped
    operation, all of which are ``mutate``, kept passing. No operation in the tree
    carries one of the three classes above ``mutate`` yet, which is exactly why the
    parametrisation is here rather than left to the first run that declares one.

    A ``READ`` declaration with ``audit = None`` still registers — the case below — so
    the rule is pinned in both directions: a refusal widened to every class would break
    ``core.workspace.status`` and the rest of the read roster.
    """
    registry = OperationRegistry()
    name = "harness.probe.unaudited"
    declaration = probe_declaration(name, NoteWriteInput).model_copy(
        update={"safety_class": safety_class, "audit": None}
    )

    with pytest.raises(RegistrationRefused) as excinfo:
        registry.register(declaration, probe_handler, origin=TEST_HARNESS_ORIGIN)

    assert excinfo.value.operation_name == name
    assert "audit" in excinfo.value.detail, excinfo.value.detail
    assert safety_class.value in excinfo.value.detail, excinfo.value.detail
    assert name not in registry


def test_registering_a_read_operation_with_no_audit_spec_still_works() -> None:
    """AC 21's other direction: ``audit = None`` stays legal at the read class.

    Driven through the same private registry and the same probe declaration, so the
    only difference from the cases above is the safety class itself.
    """
    registry = OperationRegistry()
    name = "harness.probe.readonly"
    declaration = probe_declaration(name, NoteWriteInput).model_copy(
        update={"safety_class": SafetyClass.READ, "audit": None}
    )

    registry.register(declaration, probe_handler, origin=TEST_HARNESS_ORIGIN)

    assert name in registry


def test_registering_an_input_model_that_allows_extra_keys_is_refused() -> None:
    class Leaky(BaseModel):
        model_config = ConfigDict(extra="allow")

        body: str

    registry = OperationRegistry()
    with pytest.raises(RegistrationRefused, match="extra"):
        registry.register(
            probe_declaration("harness.probe.leaky", Leaky),
            probe_handler,
            origin=TEST_HARNESS_ORIGIN,
        )
    assert "harness.probe.leaky" not in registry


def test_the_harness_origin_is_test_profile_only_in_both_registries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declaration = probe_declaration("harness.probe.gate", NoteWriteInput)
    # Under the test profile the test_harness origin is accepted by both registries.
    OperationRegistry().register(declaration, probe_handler, origin=TEST_HARNESS_ORIGIN)
    ResolverRegistry().register(
        "harness", "probe", resolve_note, origin=TEST_HARNESS_ORIGIN
    )
    # Under any other profile it is refused, and so is an origin that merely names
    # the harness module id for itself: ``harness`` is reserved for ``test_harness``.
    for profile in ("production", "development"):
        monkeypatch.setenv("RHEO_PROFILE", profile)
        for origin in (TEST_HARNESS_ORIGIN, "harness"):
            with pytest.raises(RegistrationRefused) as excinfo:
                OperationRegistry().register(declaration, probe_handler, origin=origin)
            assert excinfo.value.operation_name in {"harness.probe.gate", origin}
            with pytest.raises(RegistrationRefused):
                ResolverRegistry().register(
                    "harness", "probe", resolve_note, origin=origin
                )
    monkeypatch.setenv("RHEO_PROFILE", "test")
    # Reserved even under the test profile: only ``test_harness`` owns ``harness``.
    with pytest.raises(RegistrationRefused, match="reserved"):
        OperationRegistry().register(declaration, probe_handler, origin="harness")
    with pytest.raises(RegistrationRefused, match="reserved"):
        ResolverRegistry().register("harness", "probe", resolve_note, origin="harness")
    # The core origin cannot take a harness prefix, and a module cannot take core's.
    with pytest.raises(RegistrationRefused, match="prefix"):
        OperationRegistry().register(declaration, probe_handler, origin=CORE_ORIGIN)
    with pytest.raises(RegistrationRefused, match="prefix"):
        ResolverRegistry().register("core", "probe", resolve_note, origin="leads")
    with pytest.raises(RegistrationRefused, match="prefix"):
        OperationRegistry().register(
            probe_declaration("core.probe.gate", NoteWriteInput),
            probe_handler,
            origin="leads",
        )


# --- the dispatch channel -------------------------------------------------------------


def test_a_dispatch_payload_naming_workspace_b_lands_in_a(
    cluster: ClusterSession, two_workspaces: tuple[WorkspaceRow, WorkspaceRow]
) -> None:
    a, b = two_workspaces
    ctx_a = _operator(a.id)
    assert "harness" in ctx_a.enabled_modules
    payload = {
        "body": "routed by the context, not the payload",
        "workspace_id": str(b.id),
        "workspace": b.slug,
        "database": b.database_name,
        "dsn": f"postgresql://rheo:rheo_dev_only@localhost/{b.database_name}",
        "connection_string": f"dbname={b.database_name}",
        "schema": "harness",
        "tenant_id": str(b.id),
        "actor_id": str(uuid7()),
    }
    outcome = dispatch(ctx_a, NOTE_WRITE, payload)
    assert outcome.ok, outcome
    assert outcome.result is not None
    ref = RecordRef.parse(outcome.result.ref)  # type: ignore[attr-defined]
    assert ref.module == "harness" and ref.record_type == "note"

    with _uow(cluster, a) as uow:
        note = get_note(uow.connection, ref.id)
        assert note is not None and note.body == payload["body"]
    with _uow(cluster, b) as uow:
        assert list_notes(uow.connection) == ()
        assert get_note(uow.connection, ref.id) is None

    # The resolver seam: the caller's own database, and nothing else.
    head = resolve(ref, ctx_a)
    assert isinstance(head, RecordHead)
    assert head.ref == ref and head.state == "live" and head.readable
    from_b = resolve(ref, _operator(b.id))
    assert from_b == Unavailable(ref.format(), NOT_FOUND)
    unknown = f"harness.note:{uuid7()}"
    assert resolve(unknown, ctx_a) == Unavailable(unknown, NOT_FOUND)
    # Through the operation as well.
    via_b = dispatch(_operator(b.id), NOTE_GET, {"ref": ref.format()})
    assert via_b.state == NOT_FOUND
    via_a = dispatch(ctx_a, NOTE_GET, {"ref": ref.format(), "workspace_id": str(b.id)})
    assert via_a.ok and via_a.result is not None
    assert via_a.result.ref == ref.format()  # type: ignore[attr-defined]


def test_a_harness_operation_is_module_disabled_where_harness_is_not_enabled(
    cluster: ClusterSession, workspace: UUID
) -> None:
    ctx = _operator(workspace)
    assert ctx.enabled_modules == frozenset()
    outcome = dispatch(ctx, NOTE_WRITE, {"body": "never written"})
    assert outcome.state == MODULE_DISABLED
    row = cluster.registry_row(workspace)
    with _uow(cluster, row) as uow:
        ensure_note_table(uow.connection)
        assert list_notes(uow.connection) == ()
    # The resolver applies the same rule before any lookup.
    reference = f"harness.note:{uuid7()}"
    assert resolve(reference, ctx) == Unavailable(reference, MODULE_DISABLED)
    unregistered = f"nobody.knows:{uuid7()}"
    assert resolve(unregistered, ctx) == Unavailable(unregistered, UNRESOLVABLE)
    malformed = resolve("not a reference", ctx)
    assert malformed == Unavailable("not a reference", REFERENCE_MALFORMED)


def test_a_handler_exception_rolls_back_and_yields_a_fixed_failure_code(
    cluster: ClusterSession,
    two_workspaces: tuple[WorkspaceRow, WorkspaceRow],
    caplog: pytest.LogCaptureFixture,
) -> None:
    a, _ = two_workspaces
    ctx = _operator(a.id)
    caplog.set_level(logging.ERROR, logger="rheo_core.operations")
    message = "[SQL: SELECT 1] [parameters: {'token': 'hunter2'}]"
    outcome = dispatch(
        ctx, NOTE_EXPLODE, {"body": "written, then rolled back", "message": message}
    )
    assert outcome.state == FAILED
    assert outcome.result is None
    # A fixed code from the refusal vocabulary and the class name only: the message
    # (a driver error would carry the statement and its parameters) never reaches
    # the outcome.
    assert outcome.error == OperationError(HANDLER_FAILED, "RuntimeError")
    assert "hunter2" not in outcome.error.error_text
    # The write before the raise was rolled back.
    with _uow(cluster, a) as uow:
        assert list_notes(uow.connection) == ()
    # The exception went to the log, tagged with the request id and the exception's
    # class name only — never a formatted traceback (fit-check.md Q2b, closed in
    # run 0b2/C7b: ``logger.exception`` implied ``exc_info=True``, which would have
    # carried this same driver-shaped message into the log record).
    (record,) = [r for r in caplog.records if r.getMessage() == "operation_failed"]
    assert record.request_id == str(ctx.request_id)  # type: ignore[attr-defined]
    assert record.operation == NOTE_EXPLODE  # type: ignore[attr-defined]
    assert record.exception_type == "RuntimeError"  # type: ignore[attr-defined]
    assert record.exc_info is None
    assert "hunter2" not in caplog.text


def test_a_settings_write_in_a_leaves_b_unchanged_through_the_registry(
    cluster: ClusterSession,
    two_workspaces: tuple[WorkspaceRow, WorkspaceRow],
    owner_account_id: UUID,
) -> None:
    a, b = two_workspaces
    source = PostgresOverrideSource()
    with _uow(cluster, b) as uow:
        rows_before = workspace_settings(uow.connection)
    value_before = resolve_settings(workspace_id=b.id, source=source)[TOKEN_DAYS_CLI]
    assert value_before == 90

    ctx_a = _harness(a.id, owner_account_id, Role.OWNER)
    outcome = dispatch(
        ctx_a,
        SETTINGS_SET,
        {"key": TOKEN_DAYS_CLI, "value": 45, "workspace_id": str(b.id)},
    )
    assert outcome.ok, outcome

    assert resolve_settings(workspace_id=a.id, source=source)[TOKEN_DAYS_CLI] == 45
    assert resolve_settings(workspace_id=b.id, source=source)[TOKEN_DAYS_CLI] == 90
    with _uow(cluster, b) as uow:
        assert workspace_settings(uow.connection) == rows_before
    with _uow(cluster, a) as uow:
        assert workspace_settings(uow.connection)[TOKEN_DAYS_CLI] == "45"


@pytest.mark.parametrize(
    "name", ["workspace_id", "database", "dsn", "connection_string", "schema"]
)
def test_reserved_names_as_setting_rows_are_refused_setting_undeclared(
    cluster: ClusterSession,
    two_workspaces: tuple[WorkspaceRow, WorkspaceRow],
    owner_account_id: UUID,
    name: str,
) -> None:
    a, b = two_workspaces
    ctx_a = _harness(a.id, owner_account_id, Role.OWNER)
    with _uow(cluster, a) as uow:
        rows_before = workspace_settings(uow.connection)
    outcome = dispatch(ctx_a, SETTINGS_SET, {"key": name, "value": b.database_name})
    assert outcome.state == "setting_undeclared", outcome
    assert outcome.error is not None and name in outcome.error.error_text
    with _uow(cluster, a) as uow:
        assert workspace_settings(uow.connection) == rows_before


def test_actor_id_in_a_payload_is_ignored_at_dispatch(
    cluster: ClusterSession, workspace: UUID
) -> None:
    member = add_member(
        cluster.backend, workspace, Role.MEMBER, display_name="member-two"
    )
    other = uuid7()
    ctx = _harness(workspace, member, Role.MEMBER)
    outcome = dispatch(
        ctx,
        SETTINGS_SET_MEMBER,
        {
            "key": HARNESS_MEMBER,
            "value": "mine",
            "actor_id": str(other),
            "actor": str(other),
        },
    )
    assert outcome.ok, outcome
    row = cluster.registry_row(workspace)
    with _uow(cluster, row) as uow:
        assert member_settings(uow.connection, member) == {HARNESS_MEMBER: "mine"}
        assert member_settings(uow.connection, other) == {}
    assert REGISTRY.lookup(SETTINGS_SET_MEMBER) is not None
