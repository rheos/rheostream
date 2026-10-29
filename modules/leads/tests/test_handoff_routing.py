"""Leads' Current handoff contract without a database: the closed restage matrix,
the attempt keys, the request Leads stages, the newer-revision rule, and the
declarations that wire the consumer and the repair job."""

import ast
from pathlib import Path

import pytest
from rheo_core.operations.refusals import OperationRefused
from rheo_leads import MANIFEST
from rheo_leads import current_links as links
from rheo_leads import handoff_routing as r

HANDOFF = "leads.handoff:00000000-0000-7000-8000-0000000000c1"

ALLOWED = (
    # Request-time unavailable: a local handoff with no action, approval or job.
    ("unavailable", "destination_unavailable", "current"),
    # Core refused before any effect because the destination was unavailable.
    ("refused", "destination_unavailable_no_effect", "current"),
    # Proven failed: an exhausted retryable no-effect, or a person's resolution.
    ("failed", "retryable_no_effect", "current"),
    ("failed", None, "current"),
)
FORBIDDEN = (
    ("pending", None, "current"),
    ("succeeded", None, "current"),
    ("refused", "permanent_refusal_no_effect", "current"),
    ("refused", None, "current"),
    ("cancelled", "cancelled_no_effect", "current"),
    ("unresolved", "outcome_unknown", "current"),
    ("unresolved", "external_action_missing", "current"),
    # No destination was named, so there is nothing to retry towards.
    ("unavailable", "no_destination", None),
)


@pytest.mark.parametrize(("state", "detail", "destination"), ALLOWED)
def test_handoff_restage_matrix_allows_only_its_four_classes(
    state: str, detail: str | None, destination: str | None
) -> None:
    assert r.restage_allowed(state, detail, destination) is True


@pytest.mark.parametrize(("state", "detail", "destination"), FORBIDDEN)
def test_handoff_restage_matrix_refuses_every_other_state(
    state: str, detail: str | None, destination: str | None
) -> None:
    assert r.restage_allowed(state, detail, destination) is False


def test_handoff_restage_follows_cores_published_answer_for_a_staged_attempt() -> None:
    staged = {
        "destination_ref": "current",
        "state": "failed",
        "result_detail": "retryable_no_effect",
    }
    fresh = r.ActionRead({"restage_eligible": True}, missing=False)
    closed = r.ActionRead({"restage_eligible": False}, missing=False)
    assert r.may_restage(staged, fresh) is True
    assert r.may_restage(staged, closed) is False
    assert r.may_restage({**staged, "destination_ref": None}, fresh) is False
    # No fresh record (none staged yet, or proven absent): the matrix decides.
    assert r.may_restage(staged, r.ActionRead(None, missing=True)) is True
    assert r.may_restage({**staged, "state": "unresolved"}, None) is False
    unstaged = {
        "destination_ref": "current",
        "state": "unavailable",
        "result_detail": "destination_unavailable",
    }
    assert r.may_restage(unstaged, None) is True


def test_handoff_attempt_key_is_the_handoff_ref_and_its_attempt() -> None:
    assert r.attempt_key(HANDOFF, 1) == HANDOFF + "#1"
    assert r.attempt_key(HANDOFF, 3) == HANDOFF + "#3"
    with pytest.raises(ValueError):
        r.attempt_key(HANDOFF, 0)


def test_handoff_current_request_keeps_one_source_ref_across_attempts() -> None:
    body = {"title": "Synthetic delivery", "priority": "high"}
    first = r.current_request(HANDOFF, 1, body)
    second = r.current_request(HANDOFF, 2, body)
    assert first == {
        "destination_ref": "current",
        "source_ref": HANDOFF,
        "idempotency_key": HANDOFF + "#1",
        "title": "Synthetic delivery",
        "priority": "high",
    }
    assert second["source_ref"] == first["source_ref"] == HANDOFF
    assert second["idempotency_key"] == HANDOFF + "#2"


@pytest.mark.parametrize(
    "reserved", ["destination_ref", "source_ref", "idempotency_key"]
)
def test_handoff_payload_cannot_name_a_routing_field(reserved: str) -> None:
    with pytest.raises(OperationRefused) as refused:
        r.current_request(HANDOFF, 1, {"title": "x", reserved: "other"})
    assert refused.value.state == "input_invalid"


def _link(revision: int, **changes: object) -> dict[str, object]:
    return {
        "source_ref": HANDOFF,
        "item_ref": "current.item:00000000-0000-7000-8000-0000000000d1",
        "project_ref": None,
        "last_project_ref": None,
        "item_state": "available",
        "project_state": "never_assigned",
        "revision": revision,
        "changed_at": "2030-01-02T03:04:05+00:00",
        **changes,
    }


def test_current_links_event_model_is_the_link_shape_and_nothing_else() -> None:
    parsed = links.CurrentLink.model_validate(_link(2))
    assert parsed.revision == 2 and parsed.source_ref == HANDOFF
    with pytest.raises(ValueError):
        links.CurrentLink.model_validate({**_link(2), "title": "copied content"})
    with pytest.raises(ValueError):
        links.CurrentLink.model_validate(_link(0))


@pytest.mark.parametrize(
    "change",
    [
        {"item_state": "archived"},
        {"project_state": "archived"},
        {"item_state": ""},
        {"project_state": "LINKED"},
    ],
)
def test_current_links_refuse_a_state_outside_currents_contract(
    change: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        links.CurrentLink.model_validate(_link(2, **change))


def test_current_links_unresolved_rows_known_to_have_no_action_leave_the_scan() -> None:
    from rheo_leads.storage import pipeline

    assert set(pipeline.ACTION_ABSENT_DETAILS) == {
        r.RESTORED_UNRESOLVED,
        r.EXTERNAL_ACTION_MISSING,
    }


@pytest.mark.parametrize(
    ("stored", "incoming", "newer"),
    [(None, 1, True), (1, 2, True), (2, 2, False), (3, 2, False)],
)
def test_current_links_copy_only_a_strictly_newer_revision(
    stored: int | None, incoming: int, newer: bool
) -> None:
    assert links.is_newer(stored, incoming) is newer


def test_current_links_consumer_job_and_schedule_are_declared() -> None:
    (consumer,) = [
        s for s in MANIFEST.subscriptions if s.event_type == links.LINK_CHANGED
    ]
    assert consumer.event_type == "current.source_item_link.changed"
    assert consumer.module_id == "leads" and consumer.operation_names == frozenset()
    (job,) = [j for j in MANIFEST.jobs if j.name == links.RECONCILE_JOB_KIND]
    assert job.name == "leads.current_links.reconcile"
    (schedule,) = [s for s in MANIFEST.schedules if s.job_kind == job.name]
    # Turned on by the first staged handoff, never by a setting.
    assert schedule.enabled_by_default is False and schedule.gate_setting is None
    assert links.REPAIR_OPERATIONS == frozenset(
        {"core.external_action.get", "current.item.link_state"}
    )


def test_handoff_export_format_moves_to_fourteen() -> None:
    assert MANIFEST.export is not None and MANIFEST.export.format_version == 14


def test_handoff_leads_never_imports_current() -> None:
    root = Path(links.__file__).parent
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not any(n.split(".")[0] == "rheo_current" for n in names), path
