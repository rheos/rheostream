"""The reminder job's result for each sync outcome (#548, ADR-0027).

- L15: a ``partial`` sync (keys set aside, the rest committed) is recorded like a
  failed one, ``ok=false, failed``; the other outcomes keep their details.

No database: the owner transaction, the gate and the sync are stand-ins.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import cast

import pytest

from rheo_finance.context import FinanceContext
from rheo_finance.jobs import registry
from rheo_finance.sinks.current import SyncResult


@contextmanager
def _transaction(_ctx: object) -> Iterator[None]:
    yield None


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (SyncResult("synced"), registry.JobResult(ok=True)),
        (
            SyncResult("partial", "refused"),
            registry.JobResult(ok=False, detail_code="failed"),
        ),
        (
            SyncResult("failed", "current_unavailable"),
            registry.JobResult(ok=False, detail_code="failed"),
        ),
        (
            SyncResult("pending", "sink_off"),
            registry.JobResult(ok=None, detail_code="skipped: sink off"),
        ),
        (
            SyncResult("pending", "not_configured"),
            registry.JobResult(ok=None, detail_code="pending: current not configured"),
        ),
        (
            SyncResult("pending", "configuration_error"),
            registry.JobResult(ok=None, detail_code="pending"),
        ),
    ],
    ids=["synced", "partial", "failed", "sink-off", "not-configured", "pending"],
)
def test_each_sync_outcome_maps_to_its_job_result(
    monkeypatch: pytest.MonkeyPatch,
    outcome: SyncResult,
    expected: registry.JobResult,
) -> None:
    monkeypatch.setattr(registry, "owner_transaction", _transaction)
    monkeypatch.setattr(registry, "is_enabled", lambda _uow, _sink: True)
    monkeypatch.setattr(
        registry, "current", SimpleNamespace(sync_reminders=lambda _ctx: outcome)
    )
    spec = registry.JOBS["finance.reminders_sync"]
    assert spec.callback(cast(FinanceContext, None)) == expected


def test_a_closed_gate_skips_before_any_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    def sync(_ctx: object) -> SyncResult:
        raise AssertionError("no sync behind a closed gate")

    monkeypatch.setattr(registry, "owner_transaction", _transaction)
    monkeypatch.setattr(registry, "is_enabled", lambda _uow, _sink: False)
    monkeypatch.setattr(registry, "current", SimpleNamespace(sync_reminders=sync))
    spec = registry.JOBS["finance.reminders_sync"]
    assert spec.callback(cast(FinanceContext, None)) == registry.JobResult(
        ok=None, detail_code="skipped: sink off"
    )
