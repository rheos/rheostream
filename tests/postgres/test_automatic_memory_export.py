"""Automatic memory survives a real export and restore with its own bound purpose.

Not an acceptance criterion: cheap insurance for the one seam behaviour this run
changed (spec § Purpose binding). Automatic memories now carry their run's own bound
purpose rather than a fixed ``internal_analysis``, so the restore's re-check,
``_validate_automatic`` in Recallatron's ``export.py``, has to accept any of them.

Seam under test: Recallatron's real export/import pair, driven through
``core.workspace.export`` and ``core.workspace.restore`` and a real worker visit each.
The two memories are made the way production makes them: a pending evidence row bound
to one purpose, drained by Recallatron's own job kind off its ``MANIFEST``. One is bound
``internal_analysis`` and one ``share_with_referral``. After the source database is
dropped and rebuilt from the archive alone, each memory is back, still ``derived``,
still the speaker's own, and carrying exactly its one purpose, and each receipt still
binds that purpose. The importer-seam branches (a widened audience, a purpose that is
not its binding) are in ``test_memory_export_restore.py``; this file is the whole round
trip. Every text and identifier is synthetic.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import pytest
from conftest import ClusterSession, MakeWorkspace
from harness.evidence import EvidenceWorkspace, enable_recording
from harness.modules import install_and_enable_module, loaded_probe_modules
from harness.registry import register_harness
from harness.runtime_matrix import job_row
from rheo_contracts import ContextPurpose, Role, WorkspaceContext
from rheo_core.boundary import context_for_harness
from rheo_core.deletion import OWNED_DELETIONS
from rheo_core.evidence.providers import FAKE_EXTRACTION_MARKER
from rheo_core.exports import WORKSPACE_RESTORE
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.settings.schema import REGISTRY as SETTINGS_REGISTRY
from rheo_core.storage.evidence_tables import evidence_unit
from rheo_recallatron import automatic
from rheo_recallatron.configuration import (
    MODULE_ID,
    RETENTION_DAYS_SPEC,
    RETENTION_EXPIRE_BY_AGE_SPEC,
)
from rheo_recallatron.storage import tables as memory_tables
from sqlalchemy import select
from sqlalchemy.engine import Row

from postgres.test_automatic_memory_acceptance import _enqueue_drain, _record, _visit
from postgres.test_memory_export_restore import (
    Deployment,
    _export,
    _remove_workspace,
    _slug,
)
from postgres.test_memory_export_restore import _visit as _visit_exports

pytestmark = pytest.mark.postgres

_PURPOSES: Final = (
    ContextPurpose.INTERNAL_ANALYSIS,
    ContextPurpose.SHARE_WITH_REFERRAL,
)
_FACTS: Final = {
    ContextPurpose.INTERNAL_ANALYSIS: "The folding tables are behind the stage.",
    ContextPurpose.SHARE_WITH_REFERRAL: "The spare door key hangs in the blue cabinet.",
}


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_harness()


@pytest.fixture
def loaded(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Recallatron loaded across export **and** restore, with its retention keys in the
    process-global settings registry, for the reasons the ``recallatron`` fixture in
    ``test_memory_export_restore.py`` gives; swapped and restored the same way."""
    monkeypatch.setattr(SETTINGS_REGISTRY, "_specs", dict(SETTINGS_REGISTRY._specs))
    monkeypatch.setattr(SETTINGS_REGISTRY, "_origins", dict(SETTINGS_REGISTRY._origins))
    SETTINGS_REGISTRY.register(RETENTION_DAYS_SPEC, origin=MODULE_ID)
    SETTINGS_REGISTRY.register(RETENTION_EXPIRE_BY_AGE_SPEC, origin=MODULE_ID)
    with loaded_probe_modules(monkeypatch, MODULE_ID, deletions=OWNED_DELETIONS):
        yield


@pytest.fixture
def ev(
    loaded: None,
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    make_workspace: MakeWorkspace,
    owner_account_id: UUID,
) -> EvidenceWorkspace:
    """The source workspace: Recallatron enabled, recording conditions 1 and 2 on."""
    workspace = make_workspace(slug=_slug("automatic-source"))
    bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(bootstrap, WorkspaceContext), bootstrap
    install_and_enable_module(cluster.backend, bootstrap, workspace, MODULE_ID)
    evidence = EvidenceWorkspace(cluster, workspace, owner_account_id)
    enable_recording(monkeypatch, evidence)
    return evidence


@pytest.fixture
def host(
    loaded: None,
    make_workspace: MakeWorkspace,
    owner_account_id: UUID,
) -> WorkspaceContext:
    """A second workspace to dispatch the restore from once the source is gone."""
    workspace = make_workspace(slug=_slug("automatic-host"))
    ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """The drain's clock, pinned a few seconds ahead so every visit below is due."""
    at = datetime.now(UTC) + timedelta(seconds=5)
    monkeypatch.setattr(automatic, "_now", lambda: at)
    return at


def _deployment(ev: EvidenceWorkspace) -> Deployment:
    return Deployment(
        cluster=ev.cluster,
        workspace=ev.workspace_id,
        owner_account_id=ev.owner_account_id,
        database_name=ev.database_name,
        engine=ev.cluster.backend.pools.engine_for(ev.database_name),
    )


def _restore(cluster: ClusterSession, host: WorkspaceContext, artifact: Path) -> None:
    """Dispatch the restore from ``host``, run it, and require the job to succeed.

    Checked on the job row itself, so a restore the importer refuses reds here with
    the job's own failure (``last_error`` names the refusal) rather than later, as a
    restored workspace that never became available.
    """
    outcome = dispatch(host, WORKSPACE_RESTORE, {"artifact_path": str(artifact)})
    assert outcome.state == "pending", outcome
    assert outcome.operation_id is not None
    _visit_exports(cluster, host.workspace_id)
    engine = cluster.backend.pools.engine_for(
        cluster.registry_row(host.workspace_id).database_name
    )
    job = job_row(engine, outcome.operation_id)
    assert (job["state"], job["last_error"]) == ("succeeded", None), (
        f"the restore job did not succeed: state={job['state']!r}, "
        f"last_error={job['last_error']!r}"
    )


def _all(deployment: Deployment, table: Any) -> list[Row[Any]]:
    with deployment.reading() as uow:
        return list(uow.connection.execute(select(table)).all())


def _shape(deployment: Deployment) -> dict[str, tuple[Any, ...]]:
    """Each automatic memory's restorable shape, keyed by its bound purpose."""
    memories = {row.id: row for row in _all(deployment, memory_tables.memory)}
    purposes: dict[UUID, set[str]] = {}
    for row in _all(deployment, memory_tables.memory_purpose):
        purposes.setdefault(row.memory_id, set()).add(row.purpose)
    shape: dict[str, tuple[Any, ...]] = {}
    for receipt in _all(deployment, memory_tables.source_receipt):
        memory = memories[receipt.record_id]
        shape[receipt.bound_purpose] = (
            memory.id,
            receipt.state,
            receipt.producer_kind,
            memory.origin,
            (memory.audience_kind, memory.audience_id),
            memory.recorded_at == receipt.source_recorded_at,
            frozenset(purposes[memory.id]),
        )
    assert len(shape) == len(memories), "every memory has exactly one receipt"
    return shape


def test_automatic_memories_keep_their_own_bound_purpose_across_export_and_restore(
    ev: EvidenceWorkspace,
    host: WorkspaceContext,
    cluster: ClusterSession,
    owner_account_id: UUID,
    pinned: datetime,
) -> None:
    for purpose in _PURPOSES:
        _record(ev, purpose, f"{FAKE_EXTRACTION_MARKER} {_FACTS[purpose]}", at=pinned)
        _enqueue_drain(ev, at=pinned)
        _visit(ev, at=pinned)
    source = _deployment(ev)
    assert {(row.state, row.outcome) for row in _all(source, evidence_unit)} == {
        ("settled", "active")
    }
    before = _shape(source)
    assert set(before) == {purpose.value for purpose in _PURPOSES}
    for purpose, shape in before.items():
        _id, state, producer, origin, audience, clock, kept = shape
        assert (state, producer, origin) == ("active", "rheo_runtime", "derived")
        assert audience == ("member", owner_account_id)
        assert clock, "the memory carries its source clock"
        assert kept == {purpose}

    artifact = _export(source)
    _remove_workspace(cluster, source.workspace)
    _restore(cluster, host, artifact)

    restored = context_for_harness(source.workspace, owner_account_id, Role.OWNER)
    assert isinstance(restored, WorkspaceContext), restored
    rebuilt = EvidenceWorkspace(cluster, source.workspace, owner_account_id)
    assert _shape(_deployment(rebuilt)) == before
