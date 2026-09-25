"""Recallatron through the MCP facade under the redaction contract (issue #130 review).

The real module, loaded, installed and enabled, driven through ``apps/mcp``'s
``call_tool`` exactly as ``test_memory_mcp.py`` drives it. Three claims, each about what
reaches a model client:

1. **Masking by default.** A memory whose body holds a fictional email and phone comes
   back from ``recallatron_recall`` with both masked in ``model_view``, while the
   operation's own ``result`` still holds them.
2. **The allowance releases them.** With ``redaction.contact_points_to_model`` true at
   the operator's floor and in the workspace, the real values come back, for any
   purpose.
3. **A mask token cannot be written back.** ``recallatron_correct`` carrying
   ``[email withheld]`` is refused ``input_invalid`` and the memory is unchanged.

And ``recallatron.redaction.exclude_types = ["memory"]`` keeps every memory out of a
tool result, for a person's MCP token and for a run's purpose-bound one alike, and out
of a run's context.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import mcp.types as types
import pytest
import rheo_app_mcp.transport as transport
from conftest import ClusterSession
from harness.modules import (
    LoadedSurfaces,
    install_and_enable_module,
    loaded_probe_modules,
)
from harness.redaction import register_module_exclude_key
from rheo_app_mcp.tools import call_tool
from rheo_contracts import (
    ContextPurpose,
    Role,
    WorkspaceContext,
)
from rheo_core.boundary import context_for_harness
from rheo_core.deletion import OWNED_DELETIONS
from rheo_core.events import ConsumerRegistry
from rheo_core.operations.core_ops import register_core_operations
from rheo_core.operations.dispatch import OperationOutcome
from rheo_core.redaction.masking import EMAIL_MASK, PHONE_MASK
from rheo_core.redaction.policy import CONTACT_POINTS_KEY, exclude_types_key
from rheo_core.refs.resolver import register_resolver
from rheo_core.runtime import build_context
from rheo_core.settings import encode_text, spec_for
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.repositories import upsert_workspace_setting
from rheo_core.tokens.sets import register_core_tools
from rheo_recallatron import MANIFEST
from rheo_recallatron.configuration import MEMORY_RECORD_TYPE
from rheo_recallatron.resolvers import resolve_memory

pytestmark = pytest.mark.postgres

_MODULE = MANIFEST.module_id
EMAIL = "jane@example.com"
PHONE = "250-555-0100"
TITLE = "Harbour co-op check-in"
BODY = f"Reach Jane at {EMAIL} or {PHONE} about the harbour order."


@dataclass(frozen=True)
class Memories:
    cluster: ClusterSession
    workspace: UUID
    owner_account_id: UUID
    surfaces: LoadedSurfaces
    database_name: str

    def context(self, purpose: ContextPurpose | None = None) -> WorkspaceContext:
        ctx = context_for_harness(
            self.workspace, self.owner_account_id, Role.OWNER, bound_purpose=purpose
        )
        assert isinstance(ctx, WorkspaceContext), ctx
        return ctx

    def call(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        ctx: WorkspaceContext | None = None,
    ) -> OperationOutcome:
        return call_tool(
            self.context() if ctx is None else ctx,
            name,
            arguments,
            consumers=ConsumerRegistry(),
            tools=self.surfaces.tools,
            registry=self.surfaces.operations,
        )

    def unit_of_work(self) -> UnitOfWork:
        engine = self.cluster.backend.pools.engine_for(self.database_name)
        return UnitOfWork(engine, self.database_name)

    def set_workspace(self, key: str, value: object) -> None:
        with self.unit_of_work() as uow:
            upsert_workspace_setting(
                uow.connection,
                key=key,
                value=encode_text(spec_for(key), value),  # type: ignore[arg-type]
                value_type=spec_for(key).type,
                updated_by=None,
            )
            uow.commit()

    def remember(self) -> str:
        outcome = self.call(
            "recallatron_remember",
            # Every purpose, so a purpose-bound recall below is eligible to see it.
            {
                "kind": "note",
                "title": TITLE,
                "body": BODY,
                "purposes": [purpose.value for purpose in ContextPurpose],
            },
        )
        assert outcome.ok, outcome
        return str(outcome.result.ref)  # type: ignore[union-attr]

    def recall(self, ctx: WorkspaceContext | None = None) -> OperationOutcome:
        outcome = self.call("recallatron_recall", {"query": "harbour"}, ctx=ctx)
        assert outcome.ok, outcome
        return outcome


@pytest.fixture
def memories(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[Memories]:
    register_core_operations()
    register_core_tools()
    register_resolver(_MODULE, MEMORY_RECORD_TYPE, resolve_memory, origin=_MODULE)
    with loaded_probe_modules(
        monkeypatch, _MODULE, deletions=OWNED_DELETIONS
    ) as surfaces:
        register_core_operations(surfaces.operations)
        register_core_tools(surfaces.tools)
        bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(bootstrap, WorkspaceContext), bootstrap
        install_and_enable_module(cluster.backend, bootstrap, workspace, _MODULE)
        yield Memories(
            cluster=cluster,
            workspace=workspace,
            owner_account_id=owner_account_id,
            surfaces=surfaces,
            database_name=cluster.registry_row(workspace).database_name,
        )


def _bodies(outcome: OperationOutcome) -> list[str]:
    assert outcome.model_view is not None
    view: Any = outcome.model_view.value
    return [item["body"] for item in view["items"]]


def test_contact_values_in_a_memory_are_masked_by_default(memories: Memories) -> None:
    memories.remember()
    outcome = memories.recall()
    assert _bodies(outcome) == [
        f"Reach Jane at {EMAIL_MASK} or {PHONE_MASK} about the harbour order."
    ]
    # The operation returned the real body; only the model-bound rendering masked it.
    assert outcome.result.items[0].body == BODY  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "purpose", [None, ContextPurpose.FOLLOW_UP, ContextPurpose.SHARE_WITH_REFERRAL]
)
def test_the_allowance_at_both_floors_releases_the_real_values(
    memories: Memories,
    monkeypatch: pytest.MonkeyPatch,
    purpose: ContextPurpose | None,
) -> None:
    monkeypatch.setenv("RHEO__redaction__contact_points_to_model", "true")
    memories.set_workspace(CONTACT_POINTS_KEY, True)
    memories.remember()
    assert _bodies(memories.recall(memories.context(purpose))) == [BODY]


def test_the_operator_floor_alone_is_not_enough_when_the_workspace_says_no(
    memories: Memories, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RHEO__redaction__contact_points_to_model", "true")
    memories.set_workspace(CONTACT_POINTS_KEY, False)
    memories.remember()
    assert EMAIL not in _bodies(memories.recall())[0]


def test_a_correction_carrying_a_mask_token_is_refused(memories: Memories) -> None:
    ref = memories.remember()
    masked_body = _bodies(memories.recall())[0]
    refused = memories.call(
        "recallatron_correct",
        {"ref": ref, "expected_revision": 1, "title": TITLE, "body": masked_body},
    )
    assert refused.state == "input_invalid", refused
    assert refused.error is not None
    assert EMAIL_MASK in refused.error.error_text
    # The text must not suggest leaving the masked part out: a correction restates the
    # whole record, so that would delete the value the mask stands for.
    assert "omit" not in refused.error.error_text.lower()
    assert "ask a person" in refused.error.error_text
    # Nothing was written: the stored body is still the real one.
    after = memories.recall()
    assert after.result.items[0].body == BODY  # type: ignore[union-attr]
    assert after.result.items[0].revision == 1  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "variant",
    [
        "[EMAIL WITHHELD]",
        "[email\u00a0withheld]",
        "[Email  Withheld]",
        "\uff3bemail withheld\uff3d",
    ],
    ids=["upper", "nbsp", "spaced", "full-width"],
)
def test_a_correction_carrying_a_retyped_mask_token_is_refused(
    memories: Memories, variant: str
) -> None:
    ref = memories.remember()
    refused = memories.call(
        "recallatron_correct",
        {
            "ref": ref,
            "expected_revision": 1,
            "title": TITLE,
            "body": f"Reach Jane at {variant}",
        },
    )
    assert refused.state == "input_invalid", refused


def test_the_operator_floor_alone_with_no_workspace_row_is_masked(
    memories: Memories, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Robin's rule, enforced strictly: the operator's ``true`` is a permission, and a
    workspace that never wrote a row has not opted in."""
    monkeypatch.setenv("RHEO__redaction__contact_points_to_model", "true")
    memories.remember()
    assert EMAIL not in _bodies(memories.recall())[0]


def test_a_workspace_row_under_an_operator_false_is_masked(
    memories: Memories, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RHEO__redaction__contact_points_to_model", "false")
    memories.set_workspace(CONTACT_POINTS_KEY, True)
    memories.remember()
    assert EMAIL not in _bodies(memories.recall())[0]


def test_a_remember_carrying_a_mask_token_is_refused(memories: Memories) -> None:
    refused = memories.call(
        "recallatron_remember",
        {
            "kind": "note",
            "title": "copied from a masked read",
            "body": f"Jane's number is {PHONE_MASK}",
            "purposes": ["respond"],
        },
    )
    assert refused.state == "input_invalid", refused


def test_a_read_may_carry_a_mask_token(memories: Memories) -> None:
    memories.remember()
    outcome = memories.call("recallatron_recall", {"query": EMAIL_MASK})
    assert outcome.ok, outcome


@pytest.mark.parametrize("purpose", [None, ContextPurpose.RESPOND])
async def test_an_excluded_memory_type_never_reaches_a_tool_result(
    memories: Memories,
    monkeypatch: pytest.MonkeyPatch,
    purpose: ContextPurpose | None,
) -> None:
    """``None`` is a person's MCP token; ``respond`` stands in for a run's token,
    which is purpose-bound and reaches the same facade."""
    register_module_exclude_key(_MODULE)
    ref = memories.remember()
    memories.set_workspace(exclude_types_key(_MODULE), [MEMORY_RECORD_TYPE])
    ctx = memories.context(purpose)

    recalled = memories.recall(ctx)
    assert recalled.result.items  # type: ignore[union-attr]
    assert _bodies(recalled) == []

    written = memories.call(
        "recallatron_remember",
        {"kind": "note", "title": "another", "body": "plain", "purposes": ["respond"]},
        ctx=ctx,
    )
    assert written.ok, written
    assert written.model_view is not None
    assert written.model_view.withheld is True

    monkeypatch.setattr(transport, "_context_of", lambda request: ctx)
    monkeypatch.setattr(transport, "call_tool", lambda *args, **kwargs: written)
    wire = await transport._on_call_tool(
        None,  # type: ignore[arg-type]
        types.CallToolRequestParams(name="recallatron_remember", arguments={}),
        consumers=None,
    )
    payload: Any = wire.structured_content
    assert payload["result"] is None
    assert payload["withheld"] == transport.WITHHELD_REASON
    assert TITLE not in wire.content[0].text  # type: ignore[union-attr]

    with memories.unit_of_work() as uow:
        assert build_context(ctx, [ref], uow=uow) == []
