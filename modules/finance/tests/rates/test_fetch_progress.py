"""The rates fetch beats once per planned series (#546, ADR-0027).

- L5: with the gate open, ``fetch`` calls ``ctx.progress`` before each request, so
  the events are a beat and a fetch for each of three series.

No database and no network: the plan, the gate, the client and the request are
stand-ins. Series names are synthetic.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any, cast

import pytest

from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.rates import rowan

DAY = dt.date(2063, 1, 18)


def test_the_fetch_beats_before_every_series(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    planned = [
        rowan.Planned(currency=f"S{n}X", series=f"SAMPLE{n}", start=DAY, end=None)
        for n in (1, 2, 3)
    ]

    def fetch_one(
        _uow: Any, _client: Any, each: rowan.Planned, end: dt.date, _at: Any
    ) -> rowan.SeriesFetched:
        events.append(f"fetch {each.series}")
        return rowan.SeriesFetched(
            series=each.series,
            currency=each.currency,
            start_date=each.start,
            end_date=end,
            outcome="empty",
        )

    monkeypatch.setattr(rowan, "plan", lambda _uow, **_kwargs: planned)
    monkeypatch.setattr(rowan, "local_today", lambda *_a: DAY)
    monkeypatch.setattr(rowan, "is_enabled", lambda _uow, _sink: True)
    monkeypatch.setattr(rowan, "current_spec", lambda _uow, _sink: object())
    monkeypatch.setattr(rowan, "make_client", lambda _uow, _spec: object())
    monkeypatch.setattr(rowan, "_fetch_one", fetch_one)
    clock = SimpleNamespace(now_utc=lambda: dt.datetime(2063, 1, 18, tzinfo=dt.UTC))
    ctx = cast(
        FinanceContext,
        SimpleNamespace(
            clock=clock,
            settings=SimpleNamespace(timezone=None),
            progress=lambda: events.append("beat"),
        ),
    )

    out = rowan.fetch(ctx, cast(UnitOfWork, None), rowan.FetchIn())

    assert events == [
        "beat",
        "fetch SAMPLE1",
        "beat",
        "fetch SAMPLE2",
        "beat",
        "fetch SAMPLE3",
    ]
    assert (out.requests, out.empty) == (3, 3)
