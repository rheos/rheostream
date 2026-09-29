"""The ``claude_cli`` extraction provider in a real drain, on a scripted adapter
(spec § Architecture, System Components item 7; FR 13, FR 17; AC 12).

Seams under test:

- **A local turn becomes one memory end to end.** With
  ``automatic_memory.extraction.provider = "claude_cli"`` (this test's own
  environment) and the provider registered on a registry whose ``claude_cli`` adapter
  is scripted, one ``claude_code_local`` evidence row drains to one live memory. The
  provider's real token path runs: at the adapter's start the workspace holds exactly
  one runtime token, with no operations, and after the drain it holds none.
- **A failing run is contained like any provider fault.** A scripted failure event
  makes the provider raise; the unit stays ``pending``, counts one attempt and backs
  off 5 seconds, the drain job succeeds with a follow-up at the unit's
  ``retry_after``, no memory exists, and neither the evidence text nor the adapter's
  detail reaches a log line or a job row (the poison-row shape of
  ``test_automatic_memory_local_poison.py``).

No process is started: the shared guard makes ``subprocess.Popen`` raise, and every
test asserts it was never reached. Every text is synthetic, and every evidence and
enrollment row is removed in teardown (#238's rule).
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.evidence import PROVIDER_ENV, EvidenceWorkspace, enable_recording
from harness.extraction import (
    PopenGuard,
    ScriptedAdapter,
    ScriptedHandle,
    forbid_real_processes,
)
from harness.modules import install_and_enable_module, loaded_probe_modules
from harness.registry import register_harness
from rheo_contracts import (
    AdapterSpawn,
    ContextPurpose,
    FailureEvent,
    FailureKind,
    FinalOutputEvent,
    Role,
    RuntimeEvent,
    RuntimeRequest,
    WorkspaceContext,
)
from rheo_core.boundary import context_for_harness
from rheo_core.evidence.local_authority import LOCAL_PRODUCER_KIND
from rheo_core.evidence.providers import providers
from rheo_core.operations import register_core_operations
from rheo_core.runtime import AdapterRegistry
from rheo_core.runtime.extraction import PROVIDER_NAME, ClaudeCliExtractionProvider
from rheo_core.storage import control_tables
from rheo_core.storage.data_root import Purpose, workspace_dir_for
from rheo_core.storage.evidence_enrollment_tables import evidence_enrollment
from rheo_core.storage.evidence_tables import (
    OUTCOME_ACTIVE,
    STATE_PENDING,
    STATE_SETTLED,
    evidence_unit,
)
from rheo_recallatron.configuration import MODULE_ID
from rheo_recallatron.storage import tables as memory_tables
from sqlalchemy import delete, func, inspect, select
from sqlalchemy.engine import Row

from postgres.test_automatic_memory_acceptance import _enqueue_drain, _rows
from postgres.test_automatic_memory_drain import _jobs
from postgres.test_automatic_memory_limits import _at
from postgres.test_automatic_memory_mixed_partition import (
    enroll,
    local_evidence,
    record,
)

pytestmark = pytest.mark.postgres

_ACCOUNT_ENV: Final = "RHEO__runtime__claude_cli__credential_account_id"
_BODY: Final = "I have decided the bees move to the east orchard in spring."
_DETAIL: Final = "adapter detail naming Example Apiary, 555-0117"
_ANSWER: Final[dict[str, object]] = {
    "items": [
        {
            "item": "u1",
            "memory": {
                "kind": "decision",
                "title": "Bees move to the east orchard",
                "body": _BODY,
                "confidence": 0.9,
            },
        }
    ]
}


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_harness()


@pytest.fixture(autouse=True)
def popen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PopenGuard:
    return forbid_real_processes(monkeypatch, tmp_path)


@pytest.fixture(autouse=True)
def clean_evidence_rows(cluster: ClusterSession, workspace: UUID) -> Iterator[None]:
    """#238's ``clean_acceptance_failure``, widened: every evidence and enrollment row
    this file wrote, so a pending row left by the failure case never reaches
    ``rheo doctor``."""
    yield
    database = cluster.registry_row(workspace).database_name
    engine = cluster.backend.pools.engine_for(database)
    inspector = inspect(engine)
    with engine.begin() as connection:
        if inspector.has_table("evidence_unit", schema="core"):
            connection.execute(delete(evidence_unit))
        if inspector.has_table("evidence_enrollment", schema="core"):
            connection.execute(delete(evidence_enrollment))


class _TokenWatchingAdapter(ScriptedAdapter):
    """Also reads, at each start, the workspace's runtime tokens and how many
    operations each carries."""

    def __init__(
        self, cluster: ClusterSession, workspace: UUID, events: list[RuntimeEvent]
    ) -> None:
        super().__init__(events)
        self._cluster = cluster
        self._workspace = workspace
        self.tokens_at_start: list[list[int]] = []

    def start(self, request: RuntimeRequest, *, spawn: AdapterSpawn) -> ScriptedHandle:
        self.tokens_at_start.append(
            _runtime_token_operations(self._cluster, self._workspace)
        )
        return super().start(request, spawn=spawn)


def _runtime_token_operations(cluster: ClusterSession, workspace: UUID) -> list[int]:
    """Each live runtime token of ``workspace``, as its operation count."""
    token = control_tables.access_token
    operation = control_tables.access_token_operation
    with cluster.backend.control_engine.connect() as connection:
        rows = connection.execute(
            select(token.c.id, func.count(operation.c.token_id))
            .select_from(token.outerjoin(operation, operation.c.token_id == token.c.id))
            .where(token.c.workspace_id == workspace, token.c.kind == "runtime")
            .group_by(token.c.id)
        ).all()
    return [int(row[1]) for row in rows]


@pytest.fixture
def ev(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[EvidenceWorkspace]:
    """Recallatron installed and enabled, recording on, and ``claude_cli`` the
    configured provider with the login bound to the owner."""
    with loaded_probe_modules(monkeypatch, MODULE_ID):
        bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(bootstrap, WorkspaceContext), bootstrap
        install_and_enable_module(cluster.backend, bootstrap, workspace, MODULE_ID)
        evidence = EvidenceWorkspace(cluster, workspace, owner_account_id)
        enable_recording(monkeypatch, evidence)
        monkeypatch.setenv(PROVIDER_ENV, PROVIDER_NAME)
        monkeypatch.setenv(_ACCOUNT_ENV, str(owner_account_id))
        yield evidence


def _install(monkeypatch: pytest.MonkeyPatch, adapter: ScriptedAdapter) -> None:
    """``claude_cli`` on a registry holding only ``adapter``, undone at teardown."""
    registry = AdapterRegistry()
    registry.register(PROVIDER_NAME, adapter)
    monkeypatch.setitem(
        providers(), PROVIDER_NAME, ClaudeCliExtractionProvider(registry)
    )


def _local_turn(ev: EvidenceWorkspace, *, now: datetime) -> str:
    """One recorded ``claude_code_local`` turn of the owner; its native key."""
    enrollment_id = enroll(ev)
    turn = local_evidence(
        ev.context(ContextPurpose.RESPOND),
        _BODY,
        enrollment_id=enrollment_id,
        at=now - timedelta(seconds=5),
    )
    record(ev, [turn], at=now)
    return turn.native_key


def _unit(ev: EvidenceWorkspace, native_key: str) -> Row[Any]:
    [row] = [u for u in _rows(ev, evidence_unit) if u.native_key == native_key]
    return row


def _extract_dirs(ev: EvidenceWorkspace) -> list[Path]:
    root = workspace_dir_for(ev.workspace_id, Purpose.SCRATCH) / "extract"
    return sorted(root.iterdir()) if root.exists() else []


def test_a_local_turn_becomes_one_memory_through_the_claude_cli_provider(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    ev: EvidenceWorkspace,
    popen: PopenGuard,
) -> None:
    now = datetime.now(UTC)
    adapter = _TokenWatchingAdapter(
        cluster, ev.workspace_id, [FinalOutputEvent(structured=_ANSWER)]
    )
    _install(monkeypatch, adapter)
    native_key = _local_turn(ev, now=now)
    first = _enqueue_drain(ev, at=now)
    _at(monkeypatch, ev, now)

    # The scripted adapter, not a process, served the one extraction.
    assert len(adapter.starts) == 1
    assert popen.calls == []
    [start] = adapter.starts
    assert start.spawn.disable_builtin_tools is True
    assert _BODY in start.request.task

    row = _unit(ev, native_key)
    assert (row.state, row.outcome) == (STATE_SETTLED, OUTCOME_ACTIVE)
    [memory] = _rows(ev, memory_tables.memory)
    assert memory.title == "Bees move to the east orchard"
    receipts = {
        r.external_source_key: r.producer_kind
        for r in _rows(ev, memory_tables.source_receipt)
    }
    assert receipts == {native_key: LOCAL_PRODUCER_KIND}
    [drain] = [job for job in _jobs(ev) if job.id == first]
    assert (drain.state, drain.attempts, drain.last_error) == ("succeeded", 1, None)

    # One empty-snapshot runtime token while the run was live, none after; and the
    # throwaway directory is gone.
    assert adapter.tokens_at_start == [[0]]
    assert _runtime_token_operations(cluster, ev.workspace_id) == []
    assert _extract_dirs(ev) == []


def test_a_failing_run_backs_the_partition_off_like_any_provider_fault(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    ev: EvidenceWorkspace,
    popen: PopenGuard,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime.now(UTC)
    adapter = ScriptedAdapter(
        [FailureEvent(kind=FailureKind.RUNTIME_ERROR, detail=_DETAIL)]
    )
    _install(monkeypatch, adapter)
    native_key = _local_turn(ev, now=now)
    first = _enqueue_drain(ev, at=now)
    _at(monkeypatch, ev, now)

    assert len(adapter.starts) == 1
    assert popen.calls == []

    held = _unit(ev, native_key)
    assert (held.state, held.outcome) == (STATE_PENDING, None)
    assert held.extraction_attempts == 1
    assert held.retry_after == now + timedelta(seconds=5)
    assert _rows(ev, memory_tables.memory) == []
    assert _rows(ev, memory_tables.source_receipt) == []
    [drain] = [job for job in _jobs(ev) if job.id == first]
    assert (drain.state, drain.attempts, drain.last_error) == ("succeeded", 1, None)
    queued = [job for job in _jobs(ev) if job.state == "queued"]
    assert [job.next_run_at for job in queued] == [held.retry_after]
    for quoted in (_BODY, _DETAIL):
        assert quoted not in caplog.text
    assert all(job.last_error is None for job in _jobs(ev))

    assert _runtime_token_operations(cluster, ev.workspace_id) == []
    assert _extract_dirs(ev) == []
