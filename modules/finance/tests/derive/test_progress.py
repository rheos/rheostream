"""Derive beats once per document and once per stage (#546, ADR-0027).

- L4: ``stage1.parse_all`` calls ``progress`` first for every registered document,
  before its bytes are compared; ``run`` calls ``ctx.progress`` before each stage and
  each ``obligations_again`` round, and hands the same callable to ``parse_all``.

No database: every stage in ``rheo_finance.derive.run`` is replaced by a recorder and
the unit of work by a stand-in that records each statement.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.derive import run as derive_run
from rheo_finance.derive import stage1

DAY = dt.date(2063, 1, 18)


class _Uow:
    """Records each statement; a select answers ``rows``."""

    def __init__(self, events: list[str], rows: Sequence[object] = ()) -> None:
        self.events = events
        self.rows = list(rows)

    def execute(self, _statement: object, _parameters: object = None) -> Any:
        self.events.append("sql")
        return SimpleNamespace(all=lambda: self.rows)

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        yield


# --- stage 1 ----------------------------------------------------------------------


def test_stage_one_beats_before_each_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    rows = [
        SimpleNamespace(
            id=f"doc-{n}",
            path=f"alder/2055-0{n}-12.pdf",
            sha256=f"sha-{n}",
            issuer="alder",
            format="pdf",
            status="parsed",
            reject_reason=None,
            parse_inputs="inputs",
        )
        for n in (1, 2, 3)
    ]

    def same_bytes(_archive: Path, path: str, _sha: str) -> bool:
        events.append(f"same {path}")
        return True

    monkeypatch.setattr(stage1, "registry_digest", lambda _uow: "registry")
    monkeypatch.setattr(
        stage1, "docextract_adapter", SimpleNamespace(docextract_version=lambda: "v")
    )
    monkeypatch.setattr(
        stage1, "patterns", SimpleNamespace(patterns_digest=lambda: "p")
    )
    monkeypatch.setattr(stage1, "parse_inputs", lambda *_args: "inputs")
    monkeypatch.setattr(stage1, "_same_bytes", same_bytes)
    monkeypatch.setattr(stage1, "supersede", lambda _uow: 0)

    parsing = stage1.parse_all(
        cast(UnitOfWork, _Uow(events, rows)),
        Path("/synthetic/archive"),
        progress=lambda: events.append("beat"),
    )

    assert parsing.kept == 3
    documents = [e for e in events if e != "sql"]
    assert documents == [
        "beat",
        "same alder/2055-01-12.pdf",
        "beat",
        "same alder/2055-02-12.pdf",
        "beat",
        "same alder/2055-03-12.pdf",
    ]


# --- the run ----------------------------------------------------------------------


class _Stage:
    """A derive stage module: each function records ``<module>.<name>``; a class or
    a constant (a capitalised name) is a stand-in that records nothing."""

    def __init__(
        self, name: str, events: list[str], answers: dict[str, Callable[..., Any]]
    ) -> None:
        self._name = name
        self._events = events
        self._answers = answers

    def __getattr__(self, attr: str) -> Any:
        if attr[:1].isupper():
            return MagicMock(name=attr)
        qualified = f"{self._name}.{attr}"

        def call(*args: Any, **kwargs: Any) -> Any:
            self._events.append(qualified)
            answer = self._answers.get(qualified)
            return answer(*args, **kwargs) if answer else MagicMock(name=qualified)

        return call


STAGES = (
    "stage1",
    "stage2",
    "stage3",
    "stage4",
    "stage5",
    "coverage",
    "stage7",
    "pair_decisions",
    "stage8",
    "stage9",
    "crypto_edges",
    "stage11",
    "merchant_keys",
    "presumed",
    "stage14",
    "stage15",
    "stage15_apply",
    "stage16",
    "orphans",
    "open_items",
    "queue_counts",
)


def test_the_run_beats_before_every_stage_and_every_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    finished = iter([{"plan": DAY}, {"plan": DAY}])
    handed: list[object] = []

    def parse_all(*_args: Any, progress: object, **_kwargs: Any) -> Any:
        handed.append(progress)
        return MagicMock(name="parsing")

    answers: dict[str, Callable[..., Any]] = {
        "stage15_apply.finished_arrangements": lambda _uow: next(finished),
        "stage1.parse_all": parse_all,
    }
    for name in STAGES:
        monkeypatch.setattr(derive_run, name, _Stage(name, events, answers))
    for name in ("_clear", "write_sink_specs"):
        monkeypatch.setattr(
            derive_run,
            name,
            lambda *_a, _name=name, **_k: events.append(_name),
        )
    monkeypatch.setattr(derive_run, "local_today", lambda *_a: DAY)

    def beat() -> None:
        events.append("beat")

    settings = MagicMock(name="settings")
    settings.get_path.return_value = tmp_path
    ctx = cast(
        FinanceContext,
        SimpleNamespace(
            settings=settings,
            clock=MagicMock(name="clock"),
            paths=SimpleNamespace(data=tmp_path),
            progress=beat,
        ),
    )

    derive_run.run(cast(UnitOfWork, _Uow(events)), ctx)

    assert handed == [beat]
    assert events == [
        "sql",  # SET CONSTRAINTS ALL DEFERRED
        "beat",
        "_clear",
        "beat",
        "stage2.load_manual",
        "beat",
        "stage1.parse_all",
        "beat",
        "stage3.build_lines",
        "beat",
        "stage4.convert",
        "beat",
        "stage5.recognize",
        "beat",
        "coverage.build_coverage",
        "beat",
        "stage7.attribute",
        "beat",
        "pair_decisions.read",
        "beat",
        "stage8.pair_reversals",
        "beat",
        "stage9.pair_transfers",
        "beat",
        "crypto_edges.pair_crypto",
        "beat",
        "stage11.resolve",
        "beat",
        "merchant_keys.build",
        "beat",
        "presumed.presumed",
        # The first obligations.
        "beat",
        "stage14.create_obligations",
        "presumed.mark_presumed",
        "beat",
        "stage15.build_payments",
        "beat",
        "stage15_apply.apply_payments",
        "stage15_apply.finished_arrangements",
        # One obligations_again round.
        "beat",
        "sql",
        "sql",
        "stage14.create_obligations",
        "presumed.mark_presumed",
        "stage15_apply.apply_payments",
        "stage15_apply.finished_arrangements",
        "beat",
        "stage15_apply.record_detected",
        "beat",
        "presumed.mark_floor",
        "beat",
        "stage16.build",
        "beat",
        "orphans.build",
        "beat",
        "open_items.build",
        "beat",
        "queue_counts.refresh",
        # The constraint check.
        "beat",
        "sql",
        "sql",
        "beat",
        "write_sink_specs",
    ]
