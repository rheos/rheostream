"""The bridge worker end to end against the real ``core.evidence.ingest`` operation
(spec § Architecture, System Components item 9 steps 6-7; AC 1's synthetic half,
AC 5, AC 6).

The worker drains one synthetic session through a client that speaks the same
``IngestClient`` protocol as ``HttpIngestClient`` but calls ``dispatch()`` in
process: the request body is the one ``client.request_body`` builds, parsed the
way the API route parses it, and the token is a real bridge token from
``core.evidence_enrollment.create``. No socket is opened. The server runs the
``fake`` extraction provider, so each human turn carrying its marker becomes one
memory.

``bridge_home`` and ``projects_root`` are ``tmp_path`` directories; every
transcript line, fingerprint and text is synthetic. Every evidence and
enrollment row is removed in teardown (#238's rule).
"""

import json
import os
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.evidence import EvidenceWorkspace, enable_recording, probe_registry
from harness.modules import install_and_enable_module, loaded_probe_modules
from rheo_bridge import config as bridge_config
from rheo_bridge import keys, paths, state, transcript, worker
from rheo_bridge.client import (
    REFUSAL_CODES,
    IngestGap,
    IngestRecord,
    IngestRefused,
    IngestResponse,
    IngestTransportError,
    request_body,
)
from rheo_contracts import ContextPurpose, Role, WorkspaceContext
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.boundary.factories import context_from_token
from rheo_core.evidence.enrollment import ENROLLMENT_CREATE
from rheo_core.evidence.ingest import EVIDENCE_INGEST
from rheo_core.evidence.local_authority import LOCAL_PRODUCER_KIND
from rheo_core.evidence.providers import FAKE_EXTRACTION_MARKER
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.storage.evidence_enrollment_tables import evidence_enrollment
from rheo_core.storage.evidence_tables import evidence_unit
from rheo_recallatron.configuration import MODULE_ID
from rheo_recallatron.storage import tables as memory_tables
from sqlalchemy import delete, inspect

from postgres.test_automatic_memory_acceptance import _enqueue_drain, _rows
from postgres.test_automatic_memory_limits import _at

pytestmark = pytest.mark.postgres

_SESSION_ID: Final = "7e57ab1e-0000-4000-8000-00000000c0de"
_SESSION_HASH: Final = "c0ffee00" * 8
_TURNS: Final = (
    f"{FAKE_EXTRACTION_MARKER} The spare kettle is in the blue box.",
    f"{FAKE_EXTRACTION_MARKER} The bike pump lives under the stairs.",
)


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()


@pytest.fixture(autouse=True)
def clean_evidence_rows(cluster: ClusterSession, workspace: UUID) -> Iterator[None]:
    """Remove every evidence and enrollment row this test wrote (#238's rule)."""
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
def rec_ev(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[EvidenceWorkspace]:
    """Recording on, the ``fake`` provider, and Recallatron enabled."""
    with loaded_probe_modules(monkeypatch, MODULE_ID):
        bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(bootstrap, WorkspaceContext), bootstrap
        install_and_enable_module(cluster.backend, bootstrap, workspace, MODULE_ID)
        evidence = EvidenceWorkspace(cluster, workspace, owner_account_id)
        enable_recording(monkeypatch, evidence)
        yield evidence


class DispatchIngestClient:
    """The ``IngestClient`` protocol over ``dispatch()``, in process."""

    def __init__(self, token: str) -> None:
        self._token = token
        self.calls = 0

    def post(
        self,
        machine_fingerprint: str,
        project_fingerprint: str,
        records: Sequence[IngestRecord],
        gaps: Sequence[IngestGap],
    ) -> IngestResponse:
        self.calls += 1
        body = request_body(machine_fingerprint, project_fingerprint, records, gaps)
        ctx = context_from_token(self._token, "api")
        assert isinstance(ctx, WorkspaceContext), ctx
        outcome = dispatch(
            ctx, EVIDENCE_INGEST, json.loads(body), consumers=probe_registry(MODULE_ID)
        )
        if not outcome.ok:
            for http_status, codes in REFUSAL_CODES.items():
                if outcome.state in codes:
                    raise IngestRefused(outcome.state, http_status)
            raise IngestTransportError(f"ingest answered {outcome.state}")
        result: Any = outcome.result
        dumped = result.model_dump()
        return IngestResponse(
            accepted=tuple(dumped["accepted"]),
            deferred=tuple(dumped["deferred"]),
            gapped=tuple(dumped["gapped"]),
            dropped=tuple(dumped["dropped"]),
        )


def _human(text: str, when: datetime) -> dict[str, object]:
    return {
        "type": "user",
        "origin": {"kind": "human"},
        "sessionId": _SESSION_ID,
        "uuid": str(uuid.uuid4()),
        "timestamp": when.isoformat(),
        "entrypoint": "cli",
        "message": {"role": "user", "content": text},
    }


def _assistant(text: str, when: datetime) -> dict[str, object]:
    return {
        "type": "assistant",
        "sessionId": _SESSION_ID,
        "uuid": str(uuid.uuid4()),
        "timestamp": when.isoformat(),
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _enroll(workspace: UUID, account_id: UUID, machine: str, project: str) -> str:
    operator = context_for_operator(workspace)
    assert isinstance(operator, WorkspaceContext), operator
    outcome = dispatch(
        operator,
        ENROLLMENT_CREATE,
        {
            "machine_fingerprint": machine,
            "project_fingerprint": project,
            "account_id": str(account_id),
        },
    )
    assert outcome.ok, outcome
    created: Any = outcome.result
    value: str = created.value
    return value


def test_the_worker_drains_a_session_into_one_memory_per_turn(
    monkeypatch: pytest.MonkeyPatch, rec_ev: EvidenceWorkspace, tmp_path: Path
) -> None:
    ev = rec_ev
    user_home = tmp_path / "home"
    enrolled = user_home / "code" / "example-project"
    enrolled.mkdir(parents=True)
    enrolled_dir = os.path.realpath(enrolled)
    projects_root = user_home / ".claude" / "projects"
    slug_dir = projects_root / transcript.slug(enrolled_dir)
    slug_dir.mkdir(parents=True)
    bridge_home = paths.ensure_bridge_home(user_home / ".rheo-bridge")
    machine_key = keys.load_or_create_machine_key(bridge_home)
    token = _enroll(
        ev.workspace_id,
        ev.owner_account_id,
        keys.machine_fingerprint(machine_key),
        keys.project_fingerprint(enrolled_dir),
    )
    bridge_config.save(
        bridge_home,
        bridge_config.Config(
            api_url="https://api.example.org",
            enrollment_id=str(uuid.uuid4()),
            token_expires_at=(datetime.now(UTC) + timedelta(days=90)).isoformat(),
            enrolled_dir=enrolled_dir,
            worker_argv=["/tmp/rheo-synthetic/venv/bin/rheo-bridge", "drain"],
            install_salt="synthetic-install-salt-0013",
        ),
    )

    now = datetime.now(UTC)
    clocks = (now - timedelta(minutes=6), now - timedelta(minutes=4))
    lines = [
        _human(_TURNS[0], clocks[0]),
        _assistant("Noted.", clocks[0] + timedelta(seconds=5)),
        _human(_TURNS[1], clocks[1]),
    ]
    session = slug_dir / "session-0013.jsonl"
    session.write_text("".join(json.dumps(line) + "\n" for line in lines))
    spool_dir = paths.spool_dir(bridge_home)
    spool_dir.mkdir(mode=0o700)
    spool_file = spool_dir / f"{now.date().isoformat()}.jsonl"
    locator = {
        "v": 1,
        "event": "Stop",
        "session_hash": _SESSION_HASH,
        "transcript_path": str(session),
        "at": int(now.timestamp()) - 60,
    }
    spool_file.write_text(json.dumps(locator) + "\n")
    os.chmod(spool_file, 0o600)

    client = DispatchIngestClient(token)

    def run() -> int:
        return worker.drain(
            bridge_home, projects_root=projects_root, client=client, now=lambda: now
        )

    assert run() == worker.EXIT_OK
    assert client.calls == 1
    units = _rows(ev, evidence_unit)
    assert len(units) == 2
    for unit in units:
        assert unit.producer_kind == LOCAL_PRODUCER_KIND
        assert (unit.audience_kind, unit.audience_id) == ("member", ev.owner_account_id)
        assert unit.purpose == ContextPurpose.INTERNAL_ANALYSIS.value
    assert sorted(u.source_recorded_at for u in units) == sorted(clocks)
    connection = state.connect(paths.state_path(bridge_home))
    try:
        row = state.get_session(connection, _SESSION_HASH)
        assert row is not None and row.cursor_offset == session.stat().st_size
        assert state.get_ledger(connection)["accepted:cli"].count == 2
    finally:
        state.close(connection)

    drain_at = datetime.now(UTC)
    _enqueue_drain(ev, at=drain_at)
    _at(monkeypatch, ev, drain_at)
    assert len(_rows(ev, memory_tables.memory)) == 2

    # Nothing new in the spool or the transcript: no request, nothing changes.
    held = _rows(ev, evidence_unit)
    assert run() == worker.EXIT_OK
    assert client.calls == 1
    assert _rows(ev, evidence_unit) == held
    assert len(_rows(ev, memory_tables.memory)) == 2

    # A lost cursor (say, a rebuilt state.sqlite) resends the range; the server
    # answers from what it already holds and writes nothing new (AC 6).
    connection = state.connect(paths.state_path(bridge_home))
    try:
        state.advance_cursor(connection, _SESSION_HASH, offset=0, inode=None)
    finally:
        state.close(connection)
    assert run() == worker.EXIT_OK
    assert client.calls == 2
    assert _rows(ev, evidence_unit) == held
    assert len(_rows(ev, memory_tables.memory)) == 2
