"""``rheo doctor``'s evidence-acceptance levels, without a cluster (#221).

Seam: ``_acceptance_check`` over hand-built ``SettlementCounts``. The counts come off a
real workspace database in ``tests/postgres/test_doctor_evidence_acceptance.py``; this
file pins the tier boundaries:

* ``ok`` with no failure, on an empty window, and when the workspace has no evidence
  table at all (not applicable, never an error);
* ``warn`` at one failure among other settlements;
* ``FAIL`` at :data:`ACCEPTANCE_FAILED_FAIL_AT`, and when every unit settled in the
  window failed: the quiet workspace whose one unit was lost.
"""

import pytest
from rheo_app_cli.commands.doctor import (
    ACCEPTANCE_FAILED_FAIL_AT,
    _acceptance_check,
)
from rheo_core.evidence.health import SettlementCounts

NAME = "evidence acceptance probe"


def _level(settled: int, failed: int) -> str:
    return _acceptance_check(NAME, SettlementCounts(settled, failed)).level


def test_no_evidence_table_is_ok_and_not_applicable() -> None:
    check = _acceptance_check(NAME, None)
    assert check.level == "ok", check.line()
    assert "not applicable" in check.detail


def test_an_empty_window_is_ok() -> None:
    check = _acceptance_check(NAME, SettlementCounts(settled=0, acceptance_failed=0))
    assert check.level == "ok", check.line()
    assert check.detail.startswith("0 of 0 evidence units settled in the last 24 h")


def test_settlements_without_a_failure_are_ok() -> None:
    assert _level(settled=12, failed=0) == "ok"


def test_one_failure_among_others_warns() -> None:
    check = _acceptance_check(NAME, SettlementCounts(settled=3, acceptance_failed=1))
    assert check.level == "warn", check.line()
    assert check.detail.startswith("1 of 3 evidence units"), check.detail


@pytest.mark.parametrize("failed", [1, ACCEPTANCE_FAILED_FAIL_AT - 1])
def test_below_the_threshold_with_other_settlements_warns(failed: int) -> None:
    assert _level(settled=100, failed=failed) == "warn"


@pytest.mark.parametrize("failed", [ACCEPTANCE_FAILED_FAIL_AT, 40])
def test_the_threshold_fails(failed: int) -> None:
    assert _level(settled=100, failed=failed) == "FAIL"


def test_every_settled_unit_failing_fails_even_for_one() -> None:
    """The quiet workspace: a batch of one, deferred five times, and lost."""
    check = _acceptance_check(NAME, SettlementCounts(settled=1, acceptance_failed=1))
    assert check.level == "FAIL", check.line()
    assert "1 of 1" in check.detail
