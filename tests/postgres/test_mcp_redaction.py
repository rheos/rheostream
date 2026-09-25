"""Tool output under the redaction contract (issue #130), through the MCP facade.

A probe tool, ``harness_get_party``, reads the tiered ``harness.note`` probe body and
returns it as a model whose fields carry :class:`Tiered` markers: a public reference
and title, an internal organization, a restricted email, and a nested list of contact
points whose values are restricted. It is registered on local registries under the
harness origin, so nothing here reaches the process-wide tool table.

Two layers are asserted. ``rheo_app_mcp.tools.call_tool`` is the facade the transport
calls, and its ``model_view`` is what may leave; ``transport._on_call_tool`` is the
wire rendering, driven with the real outcome and the real context, and its structured
and text payloads are searched for the email and its local part. ``result`` itself is
checked to *still* hold the email, which is what proves the rendering, not the
operation, is what withheld it.
"""

import json
from typing import Annotated, Any
from uuid import UUID

import mcp.types as types
import pytest
import rheo_app_mcp.transport as transport
from conftest import ClusterSession
from harness.redaction import (
    PROBE_EMAIL,
    PROBE_ORGANIZATION,
    PROBE_PHONE,
    PROBE_TITLE,
    load_note_fields,
    probe_body,
)
from harness.registry import NOTE_WRITE, enable_harness_module, register_harness
from pydantic import BaseModel, ConfigDict
from rheo_app_mcp.tools import call_tool
from rheo_contracts import (
    ContextPurpose,
    Idempotency,
    OperationDeclaration,
    RecordRef,
    Role,
    SafetyClass,
    ToolDeclaration,
    WorkspaceContext,
)
from rheo_core.boundary import context_for_harness
from rheo_core.operations import OperationRegistry, dispatch
from rheo_core.operations.core_ops import register_core_operations
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.operations.refusals import OperationRefused
from rheo_core.redaction.masking import PHONE_MASK
from rheo_core.redaction.tiers import SensitivityTier, Tiered
from rheo_core.settings import TEST_HARNESS_ORIGIN
from rheo_core.storage.backend import UnitOfWork
from rheo_core.tokens.sets import ToolRegistry

pytestmark = pytest.mark.postgres

PARTY_GET = "harness.party.get"
PARTY_TOOL = "harness_get_party"
_LOCAL_PART = PROBE_EMAIL.split("@", 1)[0]


class PartyRefInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ref: str


class ContactPointView(BaseModel):
    kind: Annotated[str, Tiered(SensitivityTier.PUBLIC)]
    value: Annotated[str, Tiered(SensitivityTier.RESTRICTED)]


class PartyView(BaseModel):
    ref: Annotated[str, Tiered(SensitivityTier.PUBLIC)]
    title: Annotated[str, Tiered(SensitivityTier.PUBLIC)]
    organization: Annotated[str, Tiered(SensitivityTier.INTERNAL)]
    email: Annotated[str, Tiered(SensitivityTier.RESTRICTED)]
    contact_points: Annotated[list[ContactPointView], Tiered(SensitivityTier.PUBLIC)]


def _get_party(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: PartyRefInput
) -> PartyView:
    fields = load_note_fields(ctx, uow, RecordRef.parse(model_input.ref))
    if fields is None:
        raise OperationRefused("not_found", "no such party")
    return PartyView(
        ref=model_input.ref,
        title=str(fields["title"]),
        organization=str(fields["organization"]),
        email=str(fields["email"]),
        contact_points=[
            ContactPointView(kind="email", value=str(fields["email"])),
            ContactPointView(kind="phone", value=PROBE_PHONE),
        ],
    )


PARTY_DECLARATION = OperationDeclaration(
    name=PARTY_GET,
    safety_class=SafetyClass.READ,
    roles=frozenset({Role.OWNER, Role.MEMBER, Role.SERVICE}),
    input_model=PartyRefInput,
    output=PartyView,
    idempotency=Idempotency.NONE,
    audit=None,
)
PARTY_TOOL_DECLARATION = ToolDeclaration(
    name=PARTY_TOOL,
    safety_class=SafetyClass.READ,
    operation=PARTY_GET,
    input_model=PartyRefInput,
)


class Surface:
    def __init__(
        self, cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
    ) -> None:
        register_core_operations()
        register_harness()
        row = cluster.registry_row(workspace)
        engine = cluster.backend.pools.engine_for(row.database_name)
        with UnitOfWork(engine, row.database_name) as uow:
            enable_harness_module(uow.connection)
            uow.commit()
        ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(ctx, WorkspaceContext), ctx
        self.ctx = ctx
        self.operations = OperationRegistry()
        self.operations.register(
            PARTY_DECLARATION, _get_party, origin=TEST_HARNESS_ORIGIN
        )
        self.tools = ToolRegistry()
        self.tools.register(PARTY_TOOL_DECLARATION, origin=TEST_HARNESS_ORIGIN)
        written = dispatch(ctx, NOTE_WRITE, {"body": probe_body()})
        assert written.ok, written
        self.ref = written.result.ref  # type: ignore[union-attr]

    def bound(self, purpose: ContextPurpose) -> WorkspaceContext:
        account_id = self.ctx.principal.account_id
        assert account_id is not None
        ctx = context_for_harness(
            self.ctx.workspace_id, account_id, self.ctx.role, bound_purpose=purpose
        )
        assert isinstance(ctx, WorkspaceContext), ctx
        return ctx

    def call(self, ctx: WorkspaceContext | None = None) -> OperationOutcome:
        return call_tool(
            self.ctx if ctx is None else ctx,
            PARTY_TOOL,
            {"ref": self.ref},
            consumers=None,
            tools=self.tools,
            registry=self.operations,
        )


@pytest.fixture
def surface(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> Surface:
    return Surface(cluster, workspace, owner_account_id)


def _assert_no_contact(text: str) -> None:
    assert PROBE_EMAIL not in text
    assert _LOCAL_PART not in text
    assert PROBE_PHONE not in text


def test_the_facade_withholds_restricted_fields_under_the_default_purpose(
    surface: Surface,
) -> None:
    """An MCP token with no purpose renders under ``internal_analysis``."""
    outcome = surface.call()
    assert outcome.ok, outcome
    assert outcome.model_view is not None
    assert outcome.model_view.value == {
        "ref": surface.ref,
        "title": PROBE_TITLE,
        "organization": PROBE_ORGANIZATION,
        "contact_points": [{"kind": "email"}, {"kind": "phone"}],
    }
    _assert_no_contact(json.dumps(outcome.model_view.value))
    # The operation still returned it; the rendering is what withheld it.
    assert isinstance(outcome.result, PartyView)
    assert outcome.result.email == PROBE_EMAIL


def test_a_purpose_without_internal_loses_the_organization(surface: Surface) -> None:
    outcome = surface.call(surface.bound(ContextPurpose.SHARE_WITH_REFERRAL))
    assert outcome.ok, outcome
    assert outcome.model_view is not None
    view = outcome.model_view.value
    assert isinstance(view, dict)
    assert "organization" not in view
    assert view["title"] == PROBE_TITLE
    _assert_no_contact(json.dumps(outcome.model_view.value))


def test_the_allowance_still_releases_no_restricted_field(
    surface: Surface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator's ``true`` with no workspace row makes the allowance effective
    for ``respond``, and structured contact fields are still withheld: only a
    module's own renderer may release them (``rheo_core/redaction/policy.py``)."""
    monkeypatch.setenv("RHEO__redaction__contact_points_to_model", "true")
    outcome = surface.call(surface.bound(ContextPurpose.RESPOND))
    assert outcome.model_view is not None
    _assert_no_contact(json.dumps(outcome.model_view.value))


async def test_the_wire_payload_carries_the_rendering_and_never_the_raw_result(
    surface: Surface, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = surface.call()
    monkeypatch.setattr(transport, "_context_of", lambda ctx: surface.ctx)
    monkeypatch.setattr(transport, "call_tool", lambda *args, **kwargs: outcome)
    result = await transport._on_call_tool(
        None,  # type: ignore[arg-type]
        types.CallToolRequestParams(name=PARTY_TOOL, arguments={"ref": surface.ref}),
        consumers=None,
    )
    assert result.is_error is False
    payload: Any = result.structured_content
    assert outcome.model_view is not None
    assert payload["result"] == outcome.model_view.value
    text = result.content[0].text  # type: ignore[union-attr]
    assert json.loads(text) == payload
    _assert_no_contact(text)


async def test_a_result_that_bypassed_the_facade_is_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: an outcome with a ``result`` and no ``model_view`` (a caller that
    dispatched around the facade) sends no ``result`` at all rather than a raw dump."""
    raw = PartyView(
        ref="r", title="t", organization="o", email=PROBE_EMAIL, contact_points=[]
    )
    monkeypatch.setattr(transport, "_context_of", lambda ctx: None)
    monkeypatch.setattr(
        transport,
        "call_tool",
        lambda *args, **kwargs: OperationOutcome("succeeded", result=raw),
    )
    result = await transport._on_call_tool(
        None,  # type: ignore[arg-type]
        types.CallToolRequestParams(name=PARTY_TOOL),
        consumers=None,
    )
    assert result.structured_content == {"state": "succeeded"}
    _assert_no_contact(result.content[0].text)  # type: ignore[union-attr]


def test_an_error_text_is_masked_before_it_leaves(surface: Surface) -> None:
    """A refusal's detail is free text a handler wrote, sent to the same model."""
    outcome = call_tool(
        surface.ctx,
        f"call {PROBE_PHONE}",
        {},
        consumers=None,
        tools=surface.tools,
        registry=surface.operations,
    )
    assert outcome.state == "not_found"
    assert outcome.error is not None
    assert PHONE_MASK in outcome.error.error_text
    assert PROBE_PHONE not in outcome.error.error_text
