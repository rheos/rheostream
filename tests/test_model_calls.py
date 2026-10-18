"""The model call log never changes what a call does (#423): a row that cannot be
written is skipped, and the call's answer or exception goes on unchanged."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from rheo_core.runtime.oneshot import OneShotDeferred, OneShotFailed
from rheo_leads import model_calls
from rheo_leads.model_calls import Tracker
from sqlalchemy.exc import OperationalError

WORKSPACE = UUID("019bf0b0-0000-7000-8000-00000000aa01")


class Ctx:
    workspace_id = WORKSPACE


@contextmanager
def broken(ctx: Any) -> Iterator[Any]:
    raise OperationalError("statement", {}, Exception("synthetic"))
    yield  # pragma: no cover


@pytest.fixture
def tracker(monkeypatch: pytest.MonkeyPatch) -> Tracker:
    monkeypatch.setattr(model_calls, "open_unit_of_work", broken)
    return Tracker(Ctx(), None, lambda: datetime.now(UTC))  # type: ignore[arg-type]


def test_an_answer_survives_a_log_that_cannot_be_written(
    tracker: Tracker, caplog: pytest.LogCaptureFixture
) -> None:
    with tracker.call("scoring", "leads.scoring.model_id") as call:
        call.usage(10, 2)
        answer = {"score": 8}
    assert answer == {"score": 8}
    assert model_calls.LOG_WRITE_FAILED in caplog.text
    assert "synthetic" not in caplog.text


@pytest.mark.parametrize(
    "error", [OneShotDeferred("usage_limited"), OneShotFailed("output_invalid")]
)
def test_the_calls_own_exception_is_never_replaced(
    tracker: Tracker, error: Exception
) -> None:
    with (
        pytest.raises(type(error)),
        tracker.call("scoring", "leads.scoring.model_id"),
    ):
        raise error
