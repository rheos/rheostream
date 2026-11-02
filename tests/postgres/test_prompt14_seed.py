"""Executes the acceptance-stack synthetic seed on disposable test-cluster workspaces.

The seed body is the one ``tests/harness/prompt14_acceptance.py seed`` runs inside
the unique acceptance Compose project. Here it runs against workspace databases the
cluster fixtures create and drop, and every seeded label is asserted through
incumbent public reads. The Compose-only parts (the same-host ``/auth/continue``
request and volume removal on reset) are not exercised here.
"""

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.prompt14_acceptance import (
    APPROVAL_LABELS,
    LEADS_LABELS,
    SPARE_PENDING,
    VARIANTS,
    Seeded,
    approval_url,
    leads_url,
    reset_state,
    seeded,
    session_context_check,
    write_state,
)
from rheo_contracts import Role, WorkspaceContext
from rheo_core.approvals import REDACTED_PREVIEW
from rheo_core.boundary.factories import context_from_session
from rheo_core.operations import REGISTRY, dispatch
from rheo_core.sessions import switch_workspace
from rheo_core.storage.control_plane import list_memberships
from rheo_leads.pipeline_common import JOBSEARCH_PRESET_ID

from postgres.test_leads_current_handoff import Routing

pytestmark = pytest.mark.postgres

AUTHORITY = (
    Path(__file__).resolve().parents[1] / "fixtures" / "leads-current-handoff.json"
)

#: (handoff state, live action phase) each seeded Leads label must list.
HANDOFF_PHASES = {
    "handoff-awaiting-approval": ("pending", "awaiting_approval"),
    "handoff-approved-queued": ("pending", "approved_queued"),
    "handoff-executing": ("pending", "executing"),
    "handoff-succeeded-live-item-no-project": ("succeeded", None),
    "handoff-succeeded-live-item-live-project": ("succeeded", None),
    "handoff-deleted-item-live-project": ("succeeded", None),
    "handoff-cleared-project": ("succeeded", None),
    "handoff-deleted-project": ("succeeded", None),
    "handoff-unresolved": ("unresolved", None),
    "handoff-retryable-no-effect": ("failed", None),
}
#: (item state, project state) the viewer aggregate must return.
CURRENT_VIEWS = {
    "handoff-succeeded-live-item-no-project": ("live", "never_assigned"),
    "handoff-succeeded-live-item-live-project": ("live", "live"),
    "handoff-deleted-item-live-project": ("deleted", "live"),
    "handoff-cleared-project": ("live", "cleared"),
    "handoff-deleted-project": ("live", "deleted"),
}
#: (preview status, durable approval state, external action state).
APPROVALS = {
    "approval-pending": ("pending", "pending", "pending"),
    "approval-approved-queued": ("resolved", "approved", "pending"),
    "approval-executing": ("resolved", "approved", "executing"),
    "approval-succeeded": ("resolved", "executed", "succeeded"),
    "approval-failed-no-effect": ("resolved", "executed", "failed"),
    "approval-unresolved": ("resolved", "executed", "unresolved"),
    "approval-refused": ("resolved", "refused", "refused"),
    "approval-pending-long-payload": ("pending", "pending", "pending"),
}


def _listed(routing: Routing, opportunity: Any) -> Any:
    outcome = routing.call("leads.handoff.list", ref=opportunity.ref)
    assert outcome.ok, outcome
    (item,) = outcome.result.items
    return item


def _view(routing: Routing, handoff: Any) -> Any:
    outcome = routing.call("leads.handoff.current_view", handoff_ref=handoff.ref)
    assert outcome.ok, outcome
    return outcome.result


def _assert_seeded(routing: Routing, result: Seeded, workspace_b: UUID) -> None:
    authority = json.loads(AUTHORITY.read_text(encoding="utf-8"))
    durable = {
        name
        for name, fixture in authority["fixtures"].items()
        if fixture["evidence_mode"] == "seeded-durable-url"
    }
    assert set(result.fixtures) == durable == set(LEADS_LABELS) | set(APPROVAL_LABELS)
    assert {"unqualified", "approval-pending-long-payload"} <= durable

    for label in LEADS_LABELS:
        assert set(result.fixtures[label]) == set(VARIANTS), label
        for variant in VARIANTS:
            opportunity = result.opportunities[label][variant]
            assert result.fixtures[label][variant] == leads_url(opportunity.ref)
            record = routing.get(opportunity.ref)
            is_job = str(record.data["preset_id"]) == str(JOBSEARCH_PRESET_ID)
            assert is_job is (variant == "job_search"), (label, variant)
            if label in HANDOFF_PHASES:
                item = _listed(routing, opportunity)
                assert (item.state, item.action_phase) == HANDOFF_PHASES[label], (
                    label,
                    variant,
                )
            else:
                outcome = routing.call("leads.handoff.list", ref=opportunity.ref)
                assert outcome.ok and outcome.result.items == [], (label, variant)
            if label in CURRENT_VIEWS:
                handoff = result.handoffs[label][variant]
                shown = _view(routing, handoff)
                assert (shown.item.state, shown.project.state) == CURRENT_VIEWS[label]

    stale = routing.get(result.opportunities["qualified-stale"]["generic"].ref)
    assert str(stale.data["value_amount"]) == "1200.00"

    # Unqualified in both variants: no qualification, so no handoff can be asked for.
    assert "unqualified" in LEADS_LABELS
    for variant in VARIANTS:
        unqualified = result.opportunities["unqualified"][variant]
        listed = routing.call("leads.qualification.list", ref=unqualified.ref)
        assert listed.ok and listed.result.items == [], variant

    # The long-payload approval: every long field reaches the stored payload whole.
    long = authority["long_payload"]
    long_row = result.approvals["approval-pending-long-payload"]
    assert long_row["idempotency_key"] == long["request_key"]
    long_preview = routing.call(
        "core.approval.preview", approval_id=str(long_row["approval_id"])
    )
    assert long_preview.ok, long_preview
    payload = long_preview.result.payload
    assert payload["title"] == long["item_title"]
    assert payload["body"] == long["item_body"]
    assert payload["due_on"] == long["due_on"]
    assert payload["idempotency_key"].startswith("leads.handoff:")
    assert len(long_preview.result.payload_digest) == 64
    assert long_preview.result.actionable is True

    approvals = {}
    for label in APPROVAL_LABELS:
        row = result.approvals[label]
        assert result.fixtures[label] == {"shell": approval_url(row["approval_id"])}
        preview = routing.call(
            "core.approval.preview", approval_id=str(row["approval_id"])
        )
        assert preview.ok, preview
        action = routing.call(
            "core.external_action.get",
            external_action_id=str(row["external_action_id"]),
        )
        assert action.ok, action
        # A handoff creates a new Current record, so the approval has no subject,
        # and its requester is unbound, so it has no purpose binding either.
        assert preview.result.subject_ref is None, label
        assert preview.result.purpose is None, label
        approvals[label] = (
            preview.result.status,
            preview.result.durable_state,
            action.result.state,
        )
    assert approvals == APPROVALS

    # Each consuming audit row has its own undecided approval.
    assert set(result.spares) == set(SPARE_PENDING)
    spare_ids = {str(row["approval_id"]) for row in result.spares.values()}
    assert len(spare_ids) == len(SPARE_PENDING)
    for row_name, row in result.spares.items():
        spare = routing.call(
            "core.approval.preview", approval_id=str(row["approval_id"])
        )
        assert spare.ok, spare
        assert (spare.result.status, spare.result.actionable) == ("pending", True), (
            row_name
        )

    # Workspace B is the same human's second disposable workspace (A to B to A).
    with routing.i.cluster.backend.control_engine.connect() as connection:
        memberships = list_memberships(connection, account_id=routing.ctx.actor.id)
    owned = {row.workspace_id for row in memberships if row.role is Role.OWNER}
    assert {routing.i.workspace, workspace_b} <= owned


def test_prompt14_seed_reaches_every_seeded_fixture(
    cluster: ClusterSession,
    make_workspace: MakeWorkspace,
    owner_account_id: UUID,
    workspace: UUID,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_b = make_workspace(owner=owner_account_id, slug="prompt14-review-b")
    state = tmp_path / "prompt14-fixtures.json"
    with seeded(
        cluster, owner_account_id, workspace, workspace_b, monkeypatch, AUTHORITY
    ) as (routing, result):
        _assert_seeded(routing, result, workspace_b)

        # The seeded approval URL redacts in B for the same session and returns in A.
        session_id, secret = session_context_check(owner_account_id, workspace)
        approval_id = result.approvals["approval-pending"]["approval_id"]
        assert switch_workspace(session_id, workspace_b) is None
        context_b = context_from_session(secret, "localhost")
        assert isinstance(context_b, WorkspaceContext)
        assert context_b.workspace_id == workspace_b
        redacted = dispatch(
            context_b, "core.approval.preview", {"approval_id": str(approval_id)}
        )
        assert redacted.ok and redacted.result == REDACTED_PREVIEW
        assert switch_workspace(session_id, workspace) is None

        # Current omitted from the serving process (prompt14.sh current-omitted):
        # the viewer aggregate redacts both branches of a live handoff.
        handoff = result.handoffs["handoff-succeeded-live-item-live-project"]["generic"]
        monkeypatch.setattr(
            REGISTRY,
            "_operations",
            {
                name: operation
                for name, operation in REGISTRY._operations.items()
                if not name.startswith("current.")
            },
        )
        omitted = _view(routing, handoff)
        assert (omitted.item.state, omitted.project.state) == (
            "unavailable",
            "unavailable",
        )

    write_state(state, result.state(owner_account_id))
    written = json.loads(state.read_text(encoding="utf-8"))
    assert written["fixtures"] == result.fixtures
    assert written["workspaces"] == {"a": str(workspace), "b": str(workspace_b)}
    assert set(written["pending_spares"]) == set(SPARE_PENDING)
    assert state.stat().st_mode & 0o777 == 0o600
    # Synthetic, disposable identifiers only (no session secret is stored); shown
    # with ``-s`` so a run's exact seeded URLs can be recorded as evidence.
    print(state.read_text(encoding="utf-8"))
    assert reset_state(state) is True
    assert not state.exists() and reset_state(state) is False
