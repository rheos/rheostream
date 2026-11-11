"""The tick job's failures (L3): each class and a subclass give its fixed code, anything
else the generic one, no exception text survives, and cancellation, a lost lease and a
module that is not ready pass through. No database.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from rheo_core.modules.readiness import ModuleNotReady
from rheo_core.storage.backend import StorageRefusal
from rheo_core.work.cancellation import JobCancelled, JobLeaseLost
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from rheo_finance.db import LayoutRefused, WriterBusy
from rheo_finance.jobs.tick import SchemaBehind, SchemaUnknown
from rheo_finance.operations import OperationError, RoleRefused
from rheo_finance.settings import SettingsError
from rheo_finance.wiring import tick_job

SECRET = "synthetic-secret-path /data/finance/keys/token.env"


class LayoutSubclass(LayoutRefused):
    pass


CASES = [
    (SchemaBehind(SECRET), "finance_schema_behind"),
    (SchemaUnknown(SECRET), "finance_schema_unknown"),
    (WriterBusy(SECRET), "finance_writer_busy"),
    (SettingsError(SECRET), "finance_settings_invalid"),
    (LayoutRefused(SECRET), "finance_layout_invalid"),
    (LayoutSubclass(SECRET), "finance_layout_invalid"),
    (OperationError(SECRET), "finance_operation_refused"),
    (RoleRefused(SECRET), "finance_operation_refused"),
    (StorageRefusal("workspace_unavailable", SECRET), "finance_workspace_unavailable"),
    (SQLAlchemyError(SECRET), "finance_storage_failed"),
    (OperationalError(SECRET, {}, Exception(SECRET)), "finance_storage_failed"),
    (ValueError(SECRET), "finance_tick_failed"),
    (KeyError(SECRET), "finance_tick_failed"),
]


@pytest.mark.parametrize(("exc", "code"), CASES)
def test_each_failure_class_has_its_code(exc: BaseException, code: str) -> None:
    assert tick_job.failure_code(exc) == code


def test_subclasses_come_before_their_bases() -> None:
    codes = tick_job.failure_codes()
    for index, (kind, _) in enumerate(codes):
        for later, _ in codes[index + 1 :]:
            assert not issubclass(later, kind) or later is kind, (later, kind)


class Token:
    def __init__(self) -> None:
        self.checks = 0

    def checkpoint(self) -> None:
        self.checks += 1


def _raising(exc: BaseException) -> Any:
    def run(_workspace_id: uuid.UUID, _token: object) -> None:
        raise exc

    return run


def _payload() -> tick_job.TickPayload:
    return tick_job.TickPayload(workspace_id=uuid.UUID(int=1))


@pytest.mark.parametrize(("exc", "code"), CASES)
def test_a_failure_leaves_as_its_code_alone(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException, code: str
) -> None:
    monkeypatch.setattr(tick_job, "_run", _raising(exc))
    with pytest.raises(tick_job.FinanceJobFailed) as caught:
        tick_job.finance_tick_job(object(), _payload(), Token())  # type: ignore[arg-type]
    failed = caught.value
    assert str(failed) == code
    assert SECRET not in repr(failed) and "synthetic" not in str(failed)
    assert failed.__cause__ is None
    assert failed.__suppress_context__ is True
    assert failed.__context__ is None


@pytest.mark.parametrize(
    "exc",
    [
        JobCancelled(),
        JobLeaseLost(),
        ModuleNotReady("module_unavailable", "synthetic retry"),
    ],
    ids=["cancelled", "lease-lost", "not-ready"],
)
def test_cancellation_lease_and_readiness_pass_through(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    monkeypatch.setattr(tick_job, "_run", _raising(exc))
    with pytest.raises(type(exc)) as caught:
        tick_job.finance_tick_job(object(), _payload(), Token())  # type: ignore[arg-type]
    assert caught.value is exc
    assert tick_job.PASS_THROUGH == (JobCancelled, JobLeaseLost, ModuleNotReady)


def test_the_tick_is_handed_the_pass_through_classes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_run`` gives the tick core's cancellation and lease classes, so a job's model
    call that raises one stops the tick instead of being recorded as a failed job."""
    from rheo_finance import db
    from rheo_finance.jobs import tick as tick_module
    from rheo_finance.wiring import core as wiring_core
    from rheo_finance.wiring import model as wiring_model

    handed: list[dict[str, object]] = []
    layout = db.layout_under(Path("/synthetic/uploads"))
    monkeypatch.setattr(tick_job, "context_for_scheduled_job", lambda _id: object())
    monkeypatch.setattr(wiring_core, "layout_for", lambda _core: layout)
    monkeypatch.setattr(db, "check_layout", lambda given: given)
    monkeypatch.setattr(wiring_core, "context_over_core", lambda *_a, **_k: object())
    monkeypatch.setattr(wiring_model, "runtime_model_client", lambda *_a, **_k: None)
    monkeypatch.setattr(
        tick_module, "tick", lambda _ctx, **kwargs: handed.append(kwargs)
    )
    token = Token()
    tick_job.finance_tick_job(object(), _payload(), token)  # type: ignore[arg-type]
    assert len(handed) == 1
    assert handed[0]["pass_through"] == tick_job.PASS_THROUGH
    assert handed[0]["checkpoint"] == token.checkpoint


def test_the_handler_refuses_another_payload_and_checks_the_token_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = Token()
    with pytest.raises(TypeError):
        tick_job.finance_tick_job(object(), object(), token)  # type: ignore[arg-type]
    assert token.checks == 0
    ran: list[uuid.UUID] = []
    monkeypatch.setattr(tick_job, "_run", lambda workspace, _t: ran.append(workspace))
    tick_job.finance_tick_job(object(), _payload(), token)  # type: ignore[arg-type]
    assert token.checks == 1 and ran == [uuid.UUID(int=1)]
