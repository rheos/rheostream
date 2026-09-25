"""The context builder under the redaction contract (issue #130), on a real workspace.

``build_context`` is driven directly on the workspace's own unit of work, with the
tiered ``harness.note`` from ``tests/harness/redaction.py`` registered on a local
rendering table, so each assertion is about what the builder does with one resolved
record under one purpose. The same record through the real ``core.runtime.run`` job is
``test_runtime_orchestrator.py``'s ``..._redacts_a_tiered_record_in_the_job``.

The leak assertions search every item's whole text for the restricted value and for
its local part, so a renderer that printed the address in some other shape, or the
resolver's raw ``display`` (the note's JSON body), reddens here.
"""

from collections.abc import Iterator
from dataclasses import replace
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.redaction import (
    HARNESS_EXCLUDE_TYPES_KEY,
    NOTE_RENDERING,
    PROBE_EMAIL,
    PROBE_ORGANIZATION,
    PROBE_PHONE,
    PROBE_TITLE,
    note_renderings,
    probe_body,
    register_harness_exclude_key,
)
from harness.registry import NOTE_WRITE, enable_harness_module, register_harness
from rheo_contracts import (
    ContextItem,
    ContextPurpose,
    ContextTier,
    WorkspaceContext,
)
from rheo_core.boundary import context_for_harness
from rheo_core.operations import dispatch
from rheo_core.operations.core_ops import register_core_operations
from rheo_core.redaction.masking import EMAIL_MASK, PHONE_MASK
from rheo_core.redaction.registry import RenderingRegistry
from rheo_core.runtime import build_context
from rheo_core.settings import TEST_HARNESS_ORIGIN, encode_text, spec_for
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.repositories import upsert_workspace_setting

pytestmark = pytest.mark.postgres

_LOCAL_PART = PROBE_EMAIL.split("@", 1)[0]


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_harness()
    register_harness_exclude_key()


class Probe:
    def __init__(
        self, cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
    ) -> None:
        self.cluster = cluster
        self.workspace = workspace
        self.database_name = cluster.registry_row(workspace).database_name
        with self.unit_of_work() as uow:
            enable_harness_module(uow.connection)
            uow.commit()
        ctx = context_for_harness(workspace, owner_account_id, "owner")
        assert isinstance(ctx, WorkspaceContext), ctx
        self.ctx = ctx

    def unit_of_work(self) -> UnitOfWork:
        engine = self.cluster.backend.pools.engine_for(self.database_name)
        return UnitOfWork(engine, self.database_name)

    def note(self, body: str) -> str:
        written = dispatch(self.ctx, NOTE_WRITE, {"body": body})
        assert written.ok, written
        ref = written.result.ref  # type: ignore[union-attr]
        assert isinstance(ref, str)
        return ref

    def bound(self, purpose: ContextPurpose) -> WorkspaceContext:
        account_id = self.ctx.principal.account_id
        assert account_id is not None
        ctx = context_for_harness(
            self.ctx.workspace_id, account_id, self.ctx.role, bound_purpose=purpose
        )
        assert isinstance(ctx, WorkspaceContext), ctx
        return ctx

    def build(
        self,
        refs: list[str],
        *,
        ctx: WorkspaceContext | None = None,
        renderings: RenderingRegistry | None = None,
    ) -> list[ContextItem]:
        with self.unit_of_work() as uow:
            return build_context(
                self.ctx if ctx is None else ctx,
                refs,
                uow=uow,
                renderings=RenderingRegistry() if renderings is None else renderings,
            )

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


@pytest.fixture
def probe(
    cluster: ClusterSession, workspace: UUID, owner_account_id: UUID
) -> Iterator[Probe]:
    yield Probe(cluster, workspace, owner_account_id)


def _assert_no_email(items: list[ContextItem]) -> None:
    for item in items:
        assert PROBE_EMAIL not in item.text, item
        assert _LOCAL_PART not in item.text, item


def test_an_unbound_principal_gets_public_and_internal_fields(probe: Probe) -> None:
    ref = probe.note(probe_body())
    items = probe.build([ref], renderings=note_renderings())
    assert [item.ref for item in items] == [ref]
    assert items[0].text == (
        f"title: {PROBE_TITLE}\norganization: {PROBE_ORGANIZATION}"
    )
    assert items[0].tier is ContextTier.INTERNAL
    _assert_no_email(items)


@pytest.mark.parametrize(
    "purpose",
    [
        ContextPurpose.RESPOND,
        ContextPurpose.FOLLOW_UP,
        ContextPurpose.INTERNAL_ANALYSIS,
    ],
)
def test_an_internal_purpose_gets_the_organization(
    probe: Probe, purpose: ContextPurpose
) -> None:
    ref = probe.note(probe_body())
    items = probe.build([ref], ctx=probe.bound(purpose), renderings=note_renderings())
    assert PROBE_ORGANIZATION in items[0].text
    _assert_no_email(items)


def test_share_with_referral_gets_the_title_alone(probe: Probe) -> None:
    ref = probe.note(probe_body())
    items = probe.build(
        [ref],
        ctx=probe.bound(ContextPurpose.SHARE_WITH_REFERRAL),
        renderings=note_renderings(),
    )
    assert items[0].text == f"title: {PROBE_TITLE}"
    assert items[0].tier is ContextTier.PUBLIC
    _assert_no_email(items)


def test_the_contact_allowance_never_sends_a_restricted_field(
    probe: Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator and workspace both on, purpose ``respond``: the free-text mask lifts,
    and the ``restricted`` field still does not go, because the default renderer
    cannot tell a contact value from any other restricted field."""
    monkeypatch.setenv("RHEO__redaction__contact_points_to_model", "true")
    probe.set_workspace("redaction.contact_points_to_model", True)
    ref = probe.note(probe_body(title=f"Call {PROBE_PHONE}"))
    items = probe.build(
        [ref], ctx=probe.bound(ContextPurpose.RESPOND), renderings=note_renderings()
    )
    assert f"Call {PROBE_PHONE}" in items[0].text
    _assert_no_email(items)


def test_a_workspace_that_narrows_internal_purposes_loses_the_organization(
    probe: Probe,
) -> None:
    probe.set_workspace("redaction.internal_purposes", ["respond"])
    ref = probe.note(probe_body())
    items = probe.build(
        [ref], ctx=probe.bound(ContextPurpose.FOLLOW_UP), renderings=note_renderings()
    )
    assert items[0].text == f"title: {PROBE_TITLE}"


def test_contact_values_in_a_public_field_are_masked(probe: Probe) -> None:
    ref = probe.note(probe_body(title=f"Mail {PROBE_EMAIL} or call {PROBE_PHONE}"))
    items = probe.build(
        [ref], ctx=probe.bound(ContextPurpose.RESPOND), renderings=note_renderings()
    )
    assert f"title: Mail {EMAIL_MASK} or call {PHONE_MASK}" in items[0].text
    _assert_no_email(items)


def test_an_untiered_record_still_sends_its_display_masked(probe: Probe) -> None:
    """The Recallatron path: no rendering, so the resolver's ``display`` goes, as it
    did before issue #130, with only its contact values masked.

    Kept under the harness resolver's 40-character label: a resolver that truncates
    its own ``display`` can cut an address in half before the mask sees it, which is
    a residual of the untiered path recorded in ``runtime-and-mcp.md``. Recallatron's
    label is the memory's whole title, so it is not exposed to it.
    """
    ref = probe.note(f"from {PROBE_EMAIL}")
    items = probe.build([ref])
    assert [item.text for item in items] == [f"from {EMAIL_MASK}"]
    assert items[0].tier is ContextTier.PUBLIC


def test_an_excluded_record_type_is_never_sent(
    probe: Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``<module>.redaction.exclude_types`` is honoured before tiering, for tiered
    and untiered types alike, from the operator's value and from a workspace row."""
    tiered = probe.note(probe_body())
    assert probe.build([tiered], renderings=note_renderings())
    monkeypatch.setenv("RHEO__harness__redaction__exclude_types", "note")
    assert probe.build([tiered], renderings=note_renderings()) == []
    assert probe.build([tiered]) == []
    monkeypatch.delenv("RHEO__harness__redaction__exclude_types")
    probe.set_workspace(HARNESS_EXCLUDE_TYPES_KEY, ["note"])
    assert probe.build([tiered]) == []


def test_a_record_with_nothing_allowed_is_skipped_not_sent_raw(probe: Probe) -> None:
    """Only the restricted field is tiered: the record renders to nothing, and the
    builder skips it rather than falling back to the resolver's ``display``."""
    ref = probe.note(probe_body())
    renderings = RenderingRegistry()
    renderings.register(
        replace(NOTE_RENDERING, tiers={"email": NOTE_RENDERING.tiers["email"]}),
        origin=TEST_HARNESS_ORIGIN,
    )
    assert probe.build([ref], renderings=renderings) == []
