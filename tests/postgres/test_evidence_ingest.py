"""``core.evidence.ingest`` through the real dispatch path, with real minted bridge
tokens (spec § Architecture → System Components, item 4; FR 6, FR 7, FR 8, FR 15;
AC 1, AC 2, AC 6, AC 7, AC 15).

Seams under test (``ingest_handler``):

- **The auth chain.** A token no active enrollment holds (an orphan, a revoked
  enrollment, the old token after a rotate) refuses ``enrollment_inactive``; a wrong
  machine or project fingerprint, account or purpose refuses ``enrollment_mismatch``;
  after a rotate the new token ingests. No refusal writes a row.
- **The clock clamp.** A record and a gap stamped ahead of the server are stored at
  the server's clock, and the record drains to a memory rather than settling
  ``denied``; a replay carrying the future clock lands on the held row.
- **Inertness (AC 15).** Each server-side recording condition off answers every key
  ``deferred`` with the same shape, and writes and publishes nothing.
- **Replay (AC 6)** and **the budget suffix (FR 7).**
- **Boundary failure containment (AC 7).** A database error on the enrollment lookup
  and an audit-write fault each leave no row, event, memory or receipt, and a retry
  succeeds.
- **HTTP**: 200, 401 for a revoked token, 400 ``enrollment_inactive``.

Every fingerprint, key and text is synthetic. Every evidence and enrollment row is
removed in teardown (#238's rule).
"""

import json
import secrets
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import httpx
import pytest
from conftest import ClusterSession
from harness.evidence import (
    ENABLED_ENV,
    PROVIDER_ENV,
    EvidenceWorkspace,
    enable_recording,
    probe_registry,
)
from harness.modules import install_and_enable_module, loaded_probe_modules
from harness.registry import add_member
from rheo_app_core import api_routes
from rheo_app_core.main import public_app
from rheo_contracts import ContextPurpose, Role, WorkspaceContext
from rheo_core.audit import AUDIT_SUCCEEDED
from rheo_core.audit.core_sink import CoreAuditSink
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.boundary.context import TOKEN_REVOKED, Refusal
from rheo_core.boundary.factories import context_from_token
from rheo_core.events import ConsumerRegistry
from rheo_core.evidence import EVIDENCE_RECORDED
from rheo_core.evidence import enrollment as enrollment_module
from rheo_core.evidence import ingest as ingest_module
from rheo_core.evidence.enrollment import (
    ENROLLMENT_CREATE,
    ENROLLMENT_REVOKE,
    ENROLLMENT_ROTATE,
)
from rheo_core.evidence.ingest import (
    EVIDENCE_INGEST,
    INGEST_MAX_GAPS,
    INGEST_MAX_RECORDS,
    IngestInput,
    ingest_handler,
)
from rheo_core.evidence.local_authority import (
    ENROLLMENT_INACTIVE,
    ENROLLMENT_MISMATCH,
    LOCAL_PRODUCER_KIND,
)
from rheo_core.evidence.providers import FAKE_EXTRACTION_MARKER
from rheo_core.evidence.record import ENABLED_KEY, MAX_RECORDS_KEY
from rheo_core.evidence.sanitize import MAX_INPUT_CHARS
from rheo_core.operations import (
    INPUT_INVALID,
    OPERATION_NOT_PERMITTED,
    WORKSPACE_STATUS,
    OperationOutcome,
    dispatch,
    register_core_operations,
)
from rheo_core.operations.core_ops import TOKEN_ISSUE
from rheo_core.operations.refusals import OUTPUT_INVALID, OperationRefused
from rheo_core.storage import work_tables
from rheo_core.storage.evidence_enrollment_tables import evidence_enrollment
from rheo_core.storage.evidence_tables import (
    OUTCOME_ACTIVE,
    STATE_GAP,
    STATE_PENDING,
    STATE_SETTLED,
    evidence_unit,
)
from rheo_core.tokens.issue import issue_bridge_token
from rheo_recallatron.configuration import MODULE_ID
from rheo_recallatron.storage import tables as memory_tables
from sqlalchemy import delete, inspect, update
from sqlalchemy.engine import Row
from sqlalchemy.exc import OperationalError

from postgres.test_automatic_memory_acceptance import _enqueue_drain, _rows
from postgres.test_automatic_memory_limits import _at

pytestmark = pytest.mark.postgres

_TURN: Final = f"{FAKE_EXTRACTION_MARKER} The spare kettle is in the blue box."
_PLAIN: Final = "Remember that the bike pump lives under the stairs."
_TEN_MINUTES: Final = timedelta(minutes=10)


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()


@pytest.fixture(autouse=True)
def clean_evidence_rows(cluster: ClusterSession, workspace: UUID) -> Iterator[None]:
    """Remove every evidence and enrollment row this test wrote, so no settled row or
    enrollment with a revoked token outlives it for ``rheo doctor`` (#238's rule)."""
    yield
    database = cluster.registry_row(workspace).database_name
    engine = cluster.backend.pools.engine_for(database)
    inspector = inspect(engine)
    with engine.begin() as connection:
        if inspector.has_table("evidence_unit", schema="core"):
            connection.execute(delete(evidence_unit))
        if inspector.has_table("evidence_enrollment", schema="core"):
            connection.execute(delete(evidence_enrollment))


@pytest.fixture
def ev(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> EvidenceWorkspace:
    """Recording on, the ``fake`` provider resolving, the probe subscriber enabled."""
    evidence = EvidenceWorkspace(cluster, workspace, owner_account_id)
    enable_recording(monkeypatch, evidence)
    return evidence


@pytest.fixture
def rec_ev(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[EvidenceWorkspace]:
    """Recallatron installed and enabled as well, so a drain can make a memory."""
    with loaded_probe_modules(monkeypatch, MODULE_ID):
        bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(bootstrap, WorkspaceContext), bootstrap
        install_and_enable_module(cluster.backend, bootstrap, workspace, MODULE_ID)
        evidence = EvidenceWorkspace(cluster, workspace, owner_account_id)
        enable_recording(monkeypatch, evidence)
        yield evidence


# --- helpers ----------------------------------------------------------------------


def _hex() -> str:
    return secrets.token_hex(32)


def _record_key() -> str:
    return f"cc1:{_hex()}"


def _gap_key() -> str:
    return f"cc1g:{_hex()}"


class Enrolled:
    """One enrollment made through ``core.evidence_enrollment.create``; its token."""

    def __init__(self, workspace: UUID, account_id: UUID) -> None:
        self.workspace = workspace
        self.machine, self.project = _hex(), _hex()
        outcome = dispatch(
            _operator(workspace),
            ENROLLMENT_CREATE,
            {
                "machine_fingerprint": self.machine,
                "project_fingerprint": self.project,
                "account_id": str(account_id),
            },
        )
        assert outcome.ok, outcome
        created: Any = outcome.result
        self.enrollment_id: UUID = created.enrollment_id
        self.token_id: UUID = created.token_id
        self.value: str = created.value

    def ctx(self, value: str | None = None) -> WorkspaceContext:
        ctx = context_from_token(self.value if value is None else value, "api")
        assert isinstance(ctx, WorkspaceContext), ctx
        return ctx

    def payload(
        self,
        records: Sequence[tuple[str, datetime, str]] = (),
        gaps: Sequence[tuple[str, datetime]] = (),
        *,
        machine: str | None = None,
        project: str | None = None,
    ) -> dict[str, object]:
        return {
            "machine_fingerprint": machine or self.machine,
            "project_fingerprint": project or self.project,
            "records": [
                {"native_key": k, "recorded_at": at.isoformat(), "text": body}
                for k, at, body in records
            ],
            "gaps": [
                {
                    "native_key": k,
                    "reason": "source_truncated",
                    "recorded_at": at.isoformat(),
                }
                for k, at in gaps
            ],
        }


def _operator(workspace_id: UUID) -> WorkspaceContext:
    ctx = context_for_operator(workspace_id)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _ingest(
    ctx: WorkspaceContext,
    payload: dict[str, object],
    *,
    consumers: ConsumerRegistry | None = None,
) -> OperationOutcome:
    registry = probe_registry() if consumers is None else consumers
    return dispatch(ctx, EVIDENCE_INGEST, payload, consumers=registry)


def _four(outcome: OperationOutcome) -> dict[str, tuple[str, ...]]:
    assert outcome.ok, outcome
    result: Any = outcome.result
    dumped = result.model_dump()
    assert set(dumped) == {"accepted", "deferred", "gapped", "dropped"}
    return {name: tuple(keys) for name, keys in dumped.items()}


def _units(ev: EvidenceWorkspace) -> list[Row[Any]]:
    return _rows(ev, evidence_unit)


def _unit(ev: EvidenceWorkspace, native_key: str) -> Row[Any]:
    [row] = [u for u in _units(ev) if u.native_key == native_key]
    return row


def _recorded_events(ev: EvidenceWorkspace) -> int:
    return sum(
        row.type == EVIDENCE_RECORDED for row in _rows(ev, work_tables.outbox_event)
    )


def _one_record(enrolled: Enrolled) -> tuple[str, dict[str, object]]:
    key = _record_key()
    at = datetime.now(UTC) - timedelta(seconds=5)
    return key, enrolled.payload([(key, at, _PLAIN)])


def _assert_refused(
    ev: EvidenceWorkspace, outcome: OperationOutcome, state: str
) -> None:
    assert outcome.state == state, outcome
    assert _units(ev) == []
    assert _recorded_events(ev) == 0


# --- AC 1: the accepted row's shape ------------------------------------------------


def test_an_accepted_record_carries_the_enrollments_identity(
    ev: EvidenceWorkspace,
) -> None:
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    at = datetime.now(UTC) - timedelta(seconds=30)
    key = _record_key()

    four = _four(_ingest(enrolled.ctx(), enrolled.payload([(key, at, _PLAIN)])))

    assert four == {"accepted": (key,), "deferred": (), "gapped": (), "dropped": ()}
    row = _unit(ev, key)
    assert row.producer_kind == LOCAL_PRODUCER_KIND
    assert row.authority_id == enrolled.enrollment_id
    assert row.speaker_account_id == ev.owner_account_id
    assert (row.audience_kind, row.audience_id) == ("member", ev.owner_account_id)
    assert row.purpose == ContextPurpose.INTERNAL_ANALYSIS.value
    assert row.source_recorded_at == at
    assert row.state == STATE_PENDING
    assert _recorded_events(ev) == 1


# --- AC 2: the auth chain ----------------------------------------------------------


def test_a_token_no_enrollment_holds_is_refused(ev: EvidenceWorkspace) -> None:
    """An orphan bridge token: minted, but no enrollment row names it."""
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    _, orphan = issue_bridge_token(
        account_id=ev.owner_account_id,
        workspace_id=ev.workspace_id,
        purpose=ContextPurpose.INTERNAL_ANALYSIS.value,
        issued_from="operator",
    )
    _, payload = _one_record(enrolled)
    _assert_refused(ev, _ingest(enrolled.ctx(orphan), payload), ENROLLMENT_INACTIVE)


def test_a_live_token_under_a_revoked_enrollment_is_refused(
    ev: EvidenceWorkspace,
) -> None:
    """The row is revoked while its token stays live (revoke's half-done state)."""
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    ctx = enrolled.ctx()
    with ev.unit_of_work() as uow:
        uow.connection.execute(
            update(evidence_enrollment)
            .where(evidence_enrollment.c.id == enrolled.enrollment_id)
            .values(state="revoked", revoked_at=datetime.now(UTC))
        )
        uow.commit()
    _, payload = _one_record(enrolled)
    _assert_refused(ev, _ingest(ctx, payload), ENROLLMENT_INACTIVE)


def test_a_revoked_enrollments_token_is_refused_at_the_boundary(
    ev: EvidenceWorkspace,
) -> None:
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    revoked = dispatch(
        _operator(ev.workspace_id),
        ENROLLMENT_REVOKE,
        {
            "enrollment_id": str(enrolled.enrollment_id),
            "account_id": str(ev.owner_account_id),
        },
    )
    assert revoked.ok, revoked
    assert context_from_token(enrolled.value, "api") == Refusal(TOKEN_REVOKED)
    assert _units(ev) == []


@pytest.mark.parametrize("wrong", ["machine", "project"])
def test_a_wrong_fingerprint_is_refused(ev: EvidenceWorkspace, wrong: str) -> None:
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    key = _record_key()
    payload = enrolled.payload([(key, datetime.now(UTC), _PLAIN)], **{wrong: _hex()})
    _assert_refused(ev, _ingest(enrolled.ctx(), payload), ENROLLMENT_MISMATCH)


@pytest.mark.parametrize("column", ["account_id", "purpose"])
def test_an_enrollment_whose_account_or_purpose_moved_is_refused(
    cluster: ClusterSession, ev: EvidenceWorkspace, column: str
) -> None:
    """A row edited under a live token: its account or purpose is no longer the
    token's, so the ingest refuses rather than recording under either."""
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    ctx = enrolled.ctx()
    value: object = (
        add_member(cluster.backend, ev.workspace_id, Role.MEMBER, display_name="m")
        if column == "account_id"
        else ContextPurpose.RESPOND.value
    )
    with ev.unit_of_work() as uow:
        uow.connection.execute(
            update(evidence_enrollment)
            .where(evidence_enrollment.c.id == enrolled.enrollment_id)
            .values({column: value})
        )
        uow.commit()
    _, payload = _one_record(enrolled)
    _assert_refused(ev, _ingest(ctx, payload), ENROLLMENT_MISMATCH)


def test_after_a_rotate_the_new_token_ingests_and_the_old_one_is_refused(
    ev: EvidenceWorkspace,
) -> None:
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    rotated = dispatch(
        _operator(ev.workspace_id),
        ENROLLMENT_ROTATE,
        {
            "enrollment_id": str(enrolled.enrollment_id),
            "account_id": str(ev.owner_account_id),
        },
    )
    assert rotated.ok, rotated
    new_value: str = rotated.result.value  # type: ignore[union-attr]

    assert context_from_token(enrolled.value, "api") == Refusal(TOKEN_REVOKED)
    key, payload = _one_record(enrolled)
    assert _four(_ingest(enrolled.ctx(new_value), payload))["accepted"] == (key,)
    assert _unit(ev, key).authority_id == enrolled.enrollment_id


def test_after_a_rotate_whose_old_revoke_failed_the_live_old_token_is_refused(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace
) -> None:
    """The old token is still live, but no active enrollment holds it any more."""
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    monkeypatch.setattr(
        enrollment_module, "_revoke_token_if_live", lambda token_id: None
    )
    rotated = dispatch(
        _operator(ev.workspace_id),
        ENROLLMENT_ROTATE,
        {
            "enrollment_id": str(enrolled.enrollment_id),
            "account_id": str(ev.owner_account_id),
        },
    )
    assert rotated.ok, rotated
    new_value: str = rotated.result.value  # type: ignore[union-attr]

    _, payload = _one_record(enrolled)
    _assert_refused(ev, _ingest(enrolled.ctx(), payload), ENROLLMENT_INACTIVE)
    key, payload = _one_record(enrolled)
    assert _four(_ingest(enrolled.ctx(new_value), payload))["accepted"] == (key,)


# --- FR 2: the bridge token calls nothing else ------------------------------------


@pytest.mark.parametrize("operation", [TOKEN_ISSUE, WORKSPACE_STATUS])
def test_the_bridge_token_is_refused_every_other_operation(
    ev: EvidenceWorkspace, operation: str
) -> None:
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    payload = (
        {"kind": "cli", "set_name": "cli_full"} if operation == TOKEN_ISSUE else {}
    )
    outcome = dispatch(enrolled.ctx(), operation, payload)
    assert outcome.state == OPERATION_NOT_PERMITTED


# --- AC 6 and FR 7 -----------------------------------------------------------------


def test_a_replayed_batch_answers_the_same_and_writes_nothing_new(
    ev: EvidenceWorkspace,
) -> None:
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    at = datetime.now(UTC) - timedelta(minutes=1)
    records = [(_record_key(), at, _PLAIN), (_record_key(), at, _TURN)]
    gaps = [(_gap_key(), at)]
    first = _four(_ingest(enrolled.ctx(), enrolled.payload(records, gaps)))
    rows = {(u.native_key, u.id, u.source_recorded_at) for u in _units(ev)}
    events = _recorded_events(ev)

    later = at + timedelta(seconds=20)
    replay_records = [(k, later, body) for k, _, body in records]
    replay_gaps = [(k, later) for k, _ in gaps]
    again = _four(
        _ingest(enrolled.ctx(), enrolled.payload(replay_records, replay_gaps))
    )

    assert again == first
    assert first["accepted"] == tuple(k for k, _, _ in records)
    assert first["gapped"] == (gaps[0][0],)
    assert {(u.native_key, u.id, u.source_recorded_at) for u in _units(ev)} == rows
    assert (events, _recorded_events(ev)) == (1, 1)


# --- 8-A: NUL and lone surrogates cannot sink a batch (FR 6, AC 7) -------------------

_NEIGHBOUR: Final = "The garden hose hangs on the second hook."


def _scrub_batch(
    enrolled: Enrolled, middle: str
) -> tuple[dict[str, object], list[str], str]:
    """Good records either side of ``middle`` and one gap: the payload and its keys."""
    at = datetime.now(UTC) - timedelta(seconds=5)
    keys = [_record_key() for _ in range(3)]
    gap = _gap_key()
    bodies = [_PLAIN, middle, _NEIGHBOUR]
    payload = enrolled.payload(
        list(zip(keys, [at] * 3, bodies, strict=True)), [(gap, at)]
    )
    return payload, keys, gap


def _holds_unstorable(body: str) -> bool:
    return any(c == "\x00" or 0xD800 <= ord(c) <= 0xDFFF for c in body)


def _assert_scrubbed_batch_landed(
    ev: EvidenceWorkspace,
    four: dict[str, tuple[str, ...]],
    keys: list[str],
    gap: str,
) -> None:
    assert four == {
        "accepted": tuple(keys),
        "deferred": (),
        "gapped": (gap,),
        "dropped": (),
    }
    assert _unit(ev, keys[1]).body == "tea over"
    assert not any(_holds_unstorable(u.body) for u in _units(ev) if u.body is not None)
    assert _unit(ev, gap).state == STATE_GAP


def test_a_nul_record_does_not_sink_its_batch(ev: EvidenceWorkspace) -> None:
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    payload, keys, gap = _scrub_batch(enrolled, "tea\x00 over")
    _assert_scrubbed_batch_landed(
        ev, _four(_ingest(enrolled.ctx(), payload)), keys, gap
    )


def test_a_lone_surrogate_record_does_not_sink_its_batch(ev: EvidenceWorkspace) -> None:
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    payload, keys, gap = _scrub_batch(enrolled, "tea\ud800 over")
    _assert_scrubbed_batch_landed(
        ev, _four(_ingest(enrolled.ctx(), payload)), keys, gap
    )


def test_a_record_that_is_only_nul_and_surrogates_is_dropped(
    ev: EvidenceWorkspace,
) -> None:
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    payload, keys, gap = _scrub_batch(enrolled, "\x00\ud800\x00")
    four = _four(_ingest(enrolled.ctx(), payload))
    assert four == {
        "accepted": (keys[0], keys[2]),
        "deferred": (),
        "gapped": (gap,),
        "dropped": (keys[1],),
    }
    assert {u.native_key for u in _units(ev)} == {keys[0], keys[2], gap}


@pytest.mark.parametrize(
    "middle",
    ["tea\x00 over", "tea\ud800 over", "\x00\ud800\x00"],
    ids=["nul", "lone_surrogate", "only_unstorable"],
)
def test_a_resent_scrubbed_batch_answers_the_same_tuples(
    ev: EvidenceWorkspace, middle: str
) -> None:
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    payload, _, _ = _scrub_batch(enrolled, middle)
    first = _four(_ingest(enrolled.ctx(), payload))
    rows = {(u.native_key, u.id, u.body) for u in _units(ev)}
    events = _recorded_events(ev)

    again = _four(_ingest(enrolled.ctx(), payload))

    assert again == first
    assert {(u.native_key, u.id, u.body) for u in _units(ev)} == rows
    assert _recorded_events(ev) == events


@pytest.mark.parametrize(
    "middle",
    [
        "word " * (MAX_INPUT_CHARS // 5 + 1),
        "word " * ((MAX_INPUT_CHARS - 520) // 5) + " secret://a/b" * 40,
    ],
    ids=["raw_over_the_cap", "masked_over_the_cap"],
)
def test_an_over_cap_record_is_dropped_and_its_neighbours_land(
    ev: EvidenceWorkspace, middle: str
) -> None:
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    payload, keys, gap = _scrub_batch(enrolled, middle)
    four = _four(_ingest(enrolled.ctx(), payload))
    assert four == {
        "accepted": (keys[0], keys[2]),
        "deferred": (),
        "gapped": (gap,),
        "dropped": (keys[1],),
    }
    assert {u.native_key for u in _units(ev)} == {keys[0], keys[2], gap}


def test_the_budget_defers_a_tail_in_submission_order(ev: EvidenceWorkspace) -> None:
    ev.set_workspace(MAX_RECORDS_KEY, 2)
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    at = datetime.now(UTC) - timedelta(seconds=5)
    keys = [_record_key() for _ in range(4)]
    four = _four(
        _ingest(enrolled.ctx(), enrolled.payload([(k, at, _PLAIN) for k in keys]))
    )
    assert four["accepted"] == tuple(keys[:2])
    assert four["deferred"] == tuple(keys[2:])
    assert {u.native_key for u in _units(ev)} == set(keys[:2])


def test_gaps_land_in_gapped_as_content_free_rows(ev: EvidenceWorkspace) -> None:
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    at = datetime.now(UTC) - timedelta(seconds=5)
    gap = _gap_key()
    four = _four(_ingest(enrolled.ctx(), enrolled.payload([], [(gap, at)])))
    assert four == {"accepted": (), "deferred": (), "gapped": (gap,), "dropped": ()}
    row = _unit(ev, gap)
    assert (row.state, row.outcome, row.body) == (STATE_GAP, "source_truncated", None)
    assert row.authority_id == enrolled.enrollment_id
    assert _recorded_events(ev) == 0


@pytest.mark.parametrize(
    "shape",
    [
        "bad_record_key",
        "bad_gap_key",
        "naive_clock",
        "over_the_cap",
        "over_the_gap_cap",
    ],
)
def test_a_malformed_request_is_input_invalid_before_any_row(
    ev: EvidenceWorkspace, shape: str
) -> None:
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    at = datetime.now(UTC) - timedelta(seconds=5)
    if shape == "bad_record_key":
        payload = enrolled.payload([(f"cc1g:{_hex()}", at, _PLAIN)])
    elif shape == "bad_gap_key":
        payload = enrolled.payload([], [(f"cc1:{_hex()}", at)])
    elif shape == "naive_clock":
        payload = enrolled.payload([(_record_key(), at.replace(tzinfo=None), _PLAIN)])
    elif shape == "over_the_cap":
        payload = enrolled.payload(
            [(_record_key(), at, _PLAIN) for _ in range(INGEST_MAX_RECORDS + 1)]
        )
    else:
        payload = enrolled.payload(
            [], [(_gap_key(), at) for _ in range(INGEST_MAX_GAPS + 1)]
        )
    _assert_refused(ev, _ingest(enrolled.ctx(), payload), INPUT_INVALID)


def test_a_miswired_unit_of_work_is_refused_not_answered_inert(
    ev: EvidenceWorkspace,
) -> None:
    """A plain unit of work, not the dispatcher's handler view: a loud
    ``output_invalid``, never the all-deferred answer an opted-out deployment gives."""
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    _, payload = _one_record(enrolled)
    with ev.unit_of_work() as uow, pytest.raises(OperationRefused) as refused:
        ingest_handler(enrolled.ctx(), uow, IngestInput.model_validate(payload))
    assert refused.value.state == OUTPUT_INVALID
    assert _units(ev) == []


# --- the clock clamp ---------------------------------------------------------------


def test_a_future_clock_is_clamped_and_the_record_drains_to_a_memory(
    monkeypatch: pytest.MonkeyPatch, rec_ev: EvidenceWorkspace
) -> None:
    ev = rec_ev
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    future = datetime.now(UTC) + _TEN_MINUTES
    key, gap = _record_key(), _gap_key()
    payload = enrolled.payload([(key, future, _TURN)], [(gap, future)])

    first = _four(_ingest(enrolled.ctx(), payload, consumers=probe_registry(MODULE_ID)))
    assert first == {
        "accepted": (key,),
        "deferred": (),
        "gapped": (gap,),
        "dropped": (),
    }
    for native_key in (key, gap):
        row = _unit(ev, native_key)
        assert row.source_recorded_at == row.created_at < future

    drain_at = datetime.now(UTC)
    _enqueue_drain(ev, at=drain_at)
    _at(monkeypatch, ev, drain_at)
    row = _unit(ev, key)
    assert (row.state, row.outcome) == (STATE_SETTLED, OUTCOME_ACTIVE)
    assert len(_rows(ev, memory_tables.memory)) == 1

    held = _units(ev)
    again = _four(_ingest(enrolled.ctx(), payload, consumers=probe_registry(MODULE_ID)))
    assert again == first
    assert _units(ev) == held


# --- AC 15: inertness --------------------------------------------------------------

_INERT: Final[dict[str, Callable[[pytest.MonkeyPatch, EvidenceWorkspace], None]]] = {
    "operator_disabled": lambda mp, ev: mp.setenv(ENABLED_ENV, "false"),
    "workspace_row_false": lambda mp, ev: ev.set_workspace(ENABLED_KEY, False),
    "provider_none": lambda mp, ev: mp.setenv(PROVIDER_ENV, "none"),
    "module_not_subscribed": lambda mp, ev: None,
}


def _assert_inert(
    ev: EvidenceWorkspace, outcome: OperationOutcome, keys: list[str]
) -> None:
    assert _four(outcome) == {
        "accepted": (),
        "deferred": tuple(keys),
        "gapped": (),
        "dropped": (),
    }
    assert _units(ev) == []
    assert _recorded_events(ev) == 0


@pytest.mark.parametrize("condition", list(_INERT))
def test_an_inert_deployment_defers_every_key(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, condition: str
) -> None:
    """Each condition alone; the answer carries no discriminator between them. For
    ``module_not_subscribed`` the only subscriber is on Recallatron, which this
    workspace has not enabled."""
    _INERT[condition](monkeypatch, ev)
    consumers = (
        probe_registry(MODULE_ID) if condition == "module_not_subscribed" else None
    )
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    at = datetime.now(UTC) - timedelta(seconds=5)
    key, gap = _record_key(), _gap_key()
    outcome = _ingest(
        enrolled.ctx(),
        enrolled.payload([(key, at, _PLAIN)], [(gap, at)]),
        consumers=consumers,
    )
    _assert_inert(ev, outcome, [key, gap])


def test_an_inert_deployment_with_no_workspace_row_defers_every_key(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> None:
    """The operator says yes and the provider resolves, but the workspace never
    opted in: no ``automatic_memory.enabled`` row at all."""
    ev = EvidenceWorkspace(cluster, workspace, owner_account_id)
    monkeypatch.setenv(ENABLED_ENV, "true")
    monkeypatch.setenv(PROVIDER_ENV, "fake")
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    at = datetime.now(UTC) - timedelta(seconds=5)
    key, gap = _record_key(), _gap_key()
    outcome = _ingest(
        enrolled.ctx(), enrolled.payload([(key, at, _PLAIN)], [(gap, at)])
    )
    _assert_inert(ev, outcome, [key, gap])


# --- AC 7: boundary failure containment --------------------------------------------


def _nothing_landed(monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace) -> None:
    """No evidence row, no recorded event, and a drain finds nothing to remember."""
    assert _units(ev) == []
    assert _recorded_events(ev) == 0
    drain_at = datetime.now(UTC)
    _enqueue_drain(ev, at=drain_at)
    _at(monkeypatch, ev, drain_at)
    assert _rows(ev, memory_tables.memory) == []
    assert _rows(ev, memory_tables.source_receipt) == []


def test_a_database_error_on_the_enrollment_lookup_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    rec_ev: EvidenceWorkspace,
) -> None:
    """The failure is clean: nothing lands, no log line quotes the text, and the
    same batch succeeds on retry."""
    ev = rec_ev
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    key, payload = _one_record(enrolled)

    def broken_lookup(uow: Any, token_id: UUID) -> None:
        raise OperationalError("SELECT 1", {}, Exception("synthetic outage"))

    with monkeypatch.context() as patch:
        patch.setattr(ingest_module, "_active_enrollment", broken_lookup)
        failed = _ingest(enrolled.ctx(), payload, consumers=probe_registry(MODULE_ID))
    assert not failed.ok
    _nothing_landed(monkeypatch, ev)

    retried = _ingest(enrolled.ctx(), payload, consumers=probe_registry(MODULE_ID))
    assert _four(retried)["accepted"] == (key,)
    assert _PLAIN not in caplog.text


def test_an_audit_write_fault_rolls_the_ingest_back(
    monkeypatch: pytest.MonkeyPatch, rec_ev: EvidenceWorkspace
) -> None:
    ev = rec_ev
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    key, payload = _one_record(enrolled)
    real_record = CoreAuditSink.record

    def faulty_record(self: CoreAuditSink, ctx: Any, uow: Any, **kw: Any) -> None:
        if kw["operation"] == EVIDENCE_INGEST and kw["outcome"] == AUDIT_SUCCEEDED:
            raise RuntimeError("synthetic audit-write fault")
        real_record(self, ctx, uow, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(CoreAuditSink, "record", faulty_record)
        failed = _ingest(enrolled.ctx(), payload, consumers=probe_registry(MODULE_ID))
    assert not failed.ok
    _nothing_landed(monkeypatch, ev)

    retried = _ingest(enrolled.ctx(), payload, consumers=probe_registry(MODULE_ID))
    assert _four(retried)["accepted"] == (key,)


# --- over HTTP ---------------------------------------------------------------------


async def _post(value: str, payload: dict[str, object]) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=public_app), base_url="http://test"
    ) as client:
        return await client.post(
            f"/api/v1/operations/{EVIDENCE_INGEST}",
            headers={"Authorization": f"Bearer {value}"},
            json=payload,
        )


async def test_a_lone_surrogate_record_does_not_sink_its_batch_over_http(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace
) -> None:
    """The path a real bridge takes. ``json.dumps`` ASCII-escapes the lone surrogate, so
    the six characters ``\\ud800`` go on the wire; a UTF-8 encoder could not encode the
    surrogate itself, which is why the body is sent as pre-encoded bytes. The route's
    registry is swapped for the probe's, as every ``dispatch()`` test here passes it, so
    recording is on and the batch is not answered inert."""
    monkeypatch.setattr(api_routes, "CONSUMERS", probe_registry())
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    payload, keys, gap = _scrub_batch(enrolled, "tea\ud800 over")
    wire = json.dumps(payload).encode("ascii")
    assert b"\\ud800" in wire
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=public_app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/operations/{EVIDENCE_INGEST}",
            headers={
                "Authorization": f"Bearer {enrolled.value}",
                "Content-Type": "application/json",
            },
            content=wire,
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "succeeded"
    four = {name: tuple(found) for name, found in body["result"].items()}
    _assert_scrubbed_batch_landed(ev, four, keys, gap)


async def test_ingest_over_http(ev: EvidenceWorkspace) -> None:
    enrolled = Enrolled(ev.workspace_id, ev.owner_account_id)
    _, payload = _one_record(enrolled)

    ok = await _post(enrolled.value, payload)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["state"] == "succeeded"
    assert set(body["result"]) == {"accepted", "deferred", "gapped", "dropped"}

    _, orphan = issue_bridge_token(
        account_id=ev.owner_account_id,
        workspace_id=ev.workspace_id,
        purpose=ContextPurpose.INTERNAL_ANALYSIS.value,
        issued_from="operator",
    )
    inactive = await _post(orphan, payload)
    assert inactive.status_code == 400
    assert inactive.json()["state"] == ENROLLMENT_INACTIVE

    revoked = dispatch(
        _operator(ev.workspace_id),
        ENROLLMENT_REVOKE,
        {
            "enrollment_id": str(enrolled.enrollment_id),
            "account_id": str(ev.owner_account_id),
        },
    )
    assert revoked.ok, revoked
    refused = await _post(enrolled.value, payload)
    assert refused.status_code == 401
    assert refused.json()["state"] == TOKEN_REVOKED
