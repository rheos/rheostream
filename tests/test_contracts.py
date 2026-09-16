"""Contract-type seams: record references, the context types, and UUIDv7.

Every later chunk imports these types, so the field names, the enum members and the
reference grammar are pinned here rather than left to the first consumer to discover.

One deliberate constraint on this file: it never builds a workspace context. A
repository-wide AST scan asserts that a context is constructed only under
``packages/core/src/rheo_core/boundary/``. The class-level assertions below
(``model_config``, ``model_fields``) are attribute access, not construction, and are
what belongs here; immutability of a real instance belongs beside the boundary that
builds it.
"""

import pickle
import time
from typing import get_args
from uuid import UUID

import pytest
from pydantic import BaseModel, ValidationError
from rheo_contracts import (
    ALL_OPERATIONS,
    RESERVED_INPUT_FIELDS,
    RESERVED_MODULE_SEGMENT,
    Actor,
    ActorKind,
    AllOperations,
    Audience,
    AudienceKind,
    AuditSpec,
    Entry,
    Idempotency,
    OperationDeclaration,
    RecordRef,
    RecordRefMalformed,
    Role,
    SafetyClass,
    ToolDeclaration,
    WorkspaceContext,
    is_reserved_module,
)
from rheo_core.operations.refusals import RegistrationRefused
from rheo_core.refs import uuid7
from rheo_core.settings import CORE_ORIGIN
from rheo_core.tokens.sets import ToolRegistry

_VALID_REF = "leads.opportunity:018f6b2e-8c1a-7d3e-9a4b-1c2d3e4f5a6b"


def test_record_ref_round_trips() -> None:
    ref = RecordRef.parse(_VALID_REF)
    assert ref.module == "leads"
    assert ref.record_type == "opportunity"
    assert ref.id == UUID("018f6b2e-8c1a-7d3e-9a4b-1c2d3e4f5a6b")
    assert ref.format() == _VALID_REF
    assert str(ref) == _VALID_REF
    assert RecordRef.parse(ref.format()) == ref


@pytest.mark.parametrize(
    "value",
    [
        "leads.opportunity018f6b2e-8c1a-7d3e-9a4b-1c2d3e4f5a6b",  # no colon
        "leads:018f6b2e-8c1a-7d3e-9a4b-1c2d3e4f5a6b",  # no dot
        "Leads.opportunity:018f6b2e-8c1a-7d3e-9a4b-1c2d3e4f5a6b",  # uppercase segment
        "leads.Opportunity:018f6b2e-8c1a-7d3e-9a4b-1c2d3e4f5a6b",  # uppercase segment
        "leads.opportunity:not-a-uuid",  # non-UUID id
        "leads.opportunity:018f6b2e8c1a7d3e9a4b1c2d3e4f5a6b",  # non-canonical uuid
        ".opportunity:018f6b2e-8c1a-7d3e-9a4b-1c2d3e4f5a6b",  # empty module segment
        "leads.:018f6b2e-8c1a-7d3e-9a4b-1c2d3e4f5a6b",  # empty type segment
        "leads.opportunity:",  # empty id
        "",  # empty string
    ],
)
def test_record_ref_rejects_malformed(value: str) -> None:
    with pytest.raises(RecordRefMalformed) as caught:
        RecordRef.parse(value)
    assert caught.value.value == value
    assert str(caught.value) == value


def test_record_ref_core_segment_is_reserved_not_refused() -> None:
    ref = RecordRef.parse("core.operation:018f6b2f-0000-7000-8000-000000000002")
    assert ref.module == RESERVED_MODULE_SEGMENT
    assert is_reserved_module(ref.module)
    assert not is_reserved_module("leads")


def test_uuid7_version_and_variant_bits() -> None:
    """Many mints, not one.

    With the version or variant step removed those bits are whatever
    ``secrets.token_bytes`` produced, so a single sample would still pass one time in
    sixteen (version) or one in four (variant), and the mutation that proves this test
    would come back green at random.
    """
    for _ in range(64):
        value = uuid7()
        assert value.version == 7
        assert (value.int >> 76) & 0xF == 0x7
        assert (value.int >> 62) & 0b11 == 0b10


def test_uuid7_timestamp_is_wall_clock_milliseconds() -> None:
    """Pin the top 48 bits, which the monotonic counter would otherwise mask.

    Ordering alone cannot see a timestamp written little-endian, in seconds, or not at
    all: the same-millisecond increment keeps the sequence rising whatever those bits
    hold. B-tree locality is the whole reason for v7 over v4, so it gets its own lock.
    """
    before = time.time_ns() // 1_000_000
    stamp = uuid7().int >> 80
    after = time.time_ns() // 1_000_000
    assert before - 2 <= stamp <= after + 2
    time.sleep(0.002)
    assert uuid7().int >> 80 > stamp


def test_uuid7_is_strictly_increasing() -> None:
    minted = [uuid7().int for _ in range(1000)]
    pairs = zip(minted, minted[1:], strict=False)
    assert all(later > earlier for earlier, later in pairs)
    assert len(set(minted)) == len(minted)


def test_enum_members_are_exactly_the_ratified_sets() -> None:
    assert {member.value for member in Role} == {
        "owner",
        "member",
        "operator",
        "service",
    }
    assert {member.value for member in ActorKind} == {
        "account",
        "token",
        "operator",
        "system",
        "connection",
    }
    assert {member.value for member in Entry} == {
        "web",
        "api",
        "mcp",
        "cli",
        "job",
        "intake",
        "channel",
    }
    assert {member.value for member in AudienceKind} == {
        "session",
        "token",
        "job",
        "channel",
    }
    assert {member.value for member in SafetyClass} == {
        "read",
        "draft",
        "mutate",
        "destructive",
        "external",
        "financial",
    }
    assert {member.name for member in Idempotency} == {"NONE", "NATURAL"}


def test_role_rejects_an_unknown_value() -> None:
    with pytest.raises(ValueError):
        Role("nope")


def test_actor_and_audience_reject_out_of_enum_kinds() -> None:
    with pytest.raises(ValidationError):
        Actor(kind="nope", id=None)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Audience(kind="session-ish", id=None)  # type: ignore[arg-type]


def test_actor_and_audience_are_frozen() -> None:
    actor = Actor(kind=ActorKind.SYSTEM, id=None)
    with pytest.raises(ValidationError):
        actor.kind = ActorKind.ACCOUNT
    audience = Audience(kind=AudienceKind.JOB, id=None)
    with pytest.raises(ValidationError):
        audience.id = UUID("018f6b2e-8c1a-7d3e-9a4b-1c2d3e4f5a6b")


def test_all_operations_is_a_singleton() -> None:
    assert isinstance(ALL_OPERATIONS, AllOperations)
    assert repr(ALL_OPERATIONS) == "ALL_OPERATIONS"
    with pytest.raises(RuntimeError):
        AllOperations()
    assert pickle.loads(pickle.dumps(ALL_OPERATIONS)) is ALL_OPERATIONS


def test_workspace_context_declares_exactly_the_ratified_fields() -> None:
    assert WorkspaceContext.model_config["frozen"] is True
    assert set(WorkspaceContext.model_fields) == {
        "workspace_id",
        "actor",
        "role",
        "entry",
        "audience",
        "operation_set",
        "enabled_modules",
        "request_id",
    }


def test_workspace_context_audience_is_optional_and_the_rest_required() -> None:
    fields = WorkspaceContext.model_fields
    audience = fields["audience"]
    assert audience.is_required() is False
    assert audience.default is None
    assert set(get_args(audience.annotation)) == {Audience, type(None)}
    for name in ("workspace_id", "actor", "role", "entry", "request_id"):
        assert fields[name].is_required() is True, name


def test_reserved_input_fields_is_the_ratified_twelve() -> None:
    assert RESERVED_INPUT_FIELDS == frozenset(
        {
            "workspace_id",
            "workspace",
            "actor_id",
            "actor",
            "tenant_id",
            "database",
            "schema",
            "connection_string",
            "dsn",
            "sql",
            "table_name",
            "statement",
        }
    )


def test_audit_spec_carries_only_the_subject_field() -> None:
    assert set(AuditSpec.model_fields) == {"subject_field"}
    assert AuditSpec(subject_field=None).subject_field is None
    assert AuditSpec(subject_field="subject_ref").subject_field == "subject_ref"


class _Input(BaseModel):
    subject_ref: str


class _Output(BaseModel):
    ok: bool


class _ReservedInput(BaseModel):
    """One of the ratified twelve, for the tool-registration refusal below."""

    sql: str


def test_operation_declaration_requires_a_safety_class() -> None:
    with pytest.raises(ValidationError):
        OperationDeclaration(  # type: ignore[call-arg]
            name="core.workspace.status",
            input_model=_Input,
            output=_Output,
            idempotency=Idempotency.NONE,
        )


def test_operation_declaration_defaults_match_the_module_contract() -> None:
    decl = OperationDeclaration(
        name="core.workspace.status",
        safety_class=SafetyClass.READ,
        input_model=_Input,
        output=_Output,
        idempotency=Idempotency.NONE,
    )
    assert decl.roles == frozenset({Role.OWNER, Role.MEMBER})
    assert decl.audit is None
    # Default ``False``, which is what makes the field additive: every declaration
    # shipped before run 0c2 mints no operation record and needed no edit.
    assert decl.long_running is False
    assert "handler" not in OperationDeclaration.model_fields
    with pytest.raises(ValidationError):
        decl.name = "core.workspace.other"


def test_tool_declaration_requires_a_safety_class() -> None:
    """The contract half of AC 3, beside the operation-side case above.

    An ordinary construction cannot omit the class at all; the field is required
    with no default, exactly as ``OperationDeclaration``'s is.
    """
    with pytest.raises(ValidationError):
        ToolDeclaration(  # type: ignore[call-arg]
            name="workspace_status",
            operation="core.workspace.status",
            input_model=_Input,
        )


def test_tool_registration_refuses_a_declaration_with_no_safety_class() -> None:
    """The refusable registration path AC 3's production assertion is written
    against: a ``model_construct``-ed declaration carrying no class is refused,
    and the refusal names the tool.

    ``model_construct`` is the whole point — it skips validation, so it is the one
    way a class-less declaration can exist at all, and therefore the only shape the
    registry's own check can be the last line of defence against. A private
    registry rather than ``TOOL_REGISTRY``, so the assertion leaves no trace in the
    process-wide table other tests read.
    """
    registry = ToolRegistry()
    classless = ToolDeclaration.model_construct(
        name="no_class_tool",
        operation="core.workspace.status",
        input_model=_Input,
    )
    with pytest.raises(RegistrationRefused) as raised:
        registry.register(classless, origin=CORE_ORIGIN)
    assert "no_class_tool" in str(raised.value)
    assert raised.value.operation_name == "no_class_tool"
    assert "safety class" in raised.value.detail
    assert registry.declarations() == ()


def test_tool_registration_refuses_a_reserved_input_field_naming_the_tool() -> None:
    """The reserved-field half, sharing one check with the operation registry.

    ``_ReservedInput`` declares ``sql``; the refusal names both the tool and the
    offending field, because a refusal that says only "refused" leaves the author
    of the next module guessing which of twelve names they tripped over.
    """
    registry = ToolRegistry()
    declaration = ToolDeclaration(
        name="reserved_field_tool",
        safety_class=SafetyClass.READ,
        operation="core.workspace.status",
        input_model=_ReservedInput,
    )
    with pytest.raises(RegistrationRefused) as raised:
        registry.register(declaration, origin=CORE_ORIGIN)
    assert raised.value.operation_name == "reserved_field_tool"
    assert "sql" in raised.value.detail
    assert registry.declarations() == ()


def test_tool_registration_is_idempotent_for_the_identical_declaration() -> None:
    """Startup, the CLI and two test fixtures all call ``register_core_tools``."""
    registry = ToolRegistry()
    declaration = ToolDeclaration(
        name="repeat_tool",
        safety_class=SafetyClass.READ,
        operation="core.workspace.status",
        input_model=_Input,
    )
    first = registry.register(declaration, origin=CORE_ORIGIN)
    assert registry.register(declaration, origin=CORE_ORIGIN) is first
    assert registry.declarations() == (declaration,)


def test_long_running_is_declarable_and_frozen() -> None:
    """The field the dispatcher's minting is gated on, asserted where the rest of
    the declaration's shape is."""
    decl = OperationDeclaration(
        name="harness.note.schedule",
        safety_class=SafetyClass.MUTATE,
        input_model=_Input,
        output=_Output,
        idempotency=Idempotency.NONE,
        audit=AuditSpec(subject_field=None),
        long_running=True,
    )
    assert decl.long_running is True
    assert "long_running" in OperationDeclaration.model_fields
    with pytest.raises(ValidationError):
        decl.long_running = False
