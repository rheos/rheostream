"""The public free text a finance read hands a token carries no figure (#568, ADR-0031).

ADR-0031 keeps the status lines and the open item and coverage reasons public, on
the ground that they are built from codes, counts, dates and labels alone. These tests
pin that ground: the columns each reason expression reads, subqueries included, and
the columns each branch of the status facts view projects into its subject, count
and time. A filter may read an amount (a bill with a manual mismatch is counted by
``manual_mismatch is not null``); only what a branch projects becomes text. A new
column in any of these is a deliberate change to this file, made with a look at
whether it can carry an amount, and a raw SQL fragment, which the walk cannot read,
refuses. The status lines themselves are checked on a fixture ledger in
``test_status_ops``. No database here.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import CompoundSelect
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import ColumnClause, ColumnElement, TextClause

from rheo_finance.reports import views


def _columns_read(roots: Iterable[ColumnElement[object]]) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for root in roots:
        for element in visitors.iterate(root):
            assert not isinstance(element, TextClause), str(element)
            if not isinstance(element, ColumnClause):
                continue
            if element.table is None:
                # ``count(*)`` is the one table-less column; ``literal_column`` is
                # raw SQL the walk cannot see into.
                assert element.name == "*", element.name
                continue
            found.add((str(getattr(element.table, "name", "")), element.name))
    return found


def _status_branches() -> CompoundSelect[tuple[object, ...]]:
    (union,) = views.report_status_select().get_final_froms()
    element = getattr(union, "element", union)
    assert isinstance(element, CompoundSelect)
    return element


def test_the_open_item_reason_reads_no_amount() -> None:
    reason = views.report_open_item_select().selected_columns["reason"]
    assert _columns_read([reason]) == {
        ("decision_orphan", "candidate_txn_id"),
        ("decision_orphan", "decision_id"),
        ("obligation", "id"),
        ("obligation", "period_label"),
        ("open_item", "decision_id"),
        ("open_item", "kind"),
        ("open_item", "payment_id"),
        ("open_item", "reason_code"),
        ("open_item", "reason_date"),
        ("open_item", "txn_id"),
        ("pair_edge", "a_txn_id"),
        ("pair_edge", "applied"),
        ("pair_edge", "b_txn_id"),
        ("payee", "display_label"),
        ("payment_application", "obligation_id"),
        ("payment_application", "payment_id"),
        ("reason_account", "label"),
        ("txn", "posting_date"),
    }


def test_the_coverage_reason_reads_its_code_alone() -> None:
    reason = views.report_coverage_select().selected_columns["reason"]
    assert _columns_read([reason]) == {("coverage", "reason_code")}


def test_the_status_facts_project_no_amount() -> None:
    columns = [column.name for column in views.report_status_select().selected_columns]
    assert columns == ["fact", "subject", "count", "at"]
    branches = _status_branches().selects

    def projected(name: str) -> set[tuple[str, str]]:
        return _columns_read(branch.selected_columns[name] for branch in branches)

    assert projected("fact") == set()
    assert projected("subject") == {
        ("account", "label"),
        ("alembic_version_finance", "version_num"),
        ("archive_skip", "directory"),
        ("decision", "kind"),
        ("document", "issuer"),
        ("document", "status"),
        ("obligation", "arrangement_ref"),
        ("obligation", "unsettled_decision_id"),
        ("payee", "display_label"),
        ("source_discrepancy", "kind"),
    }
    assert projected("count") == {
        ("archive_skip", "count"),
        ("decision_orphan", "decision_id"),
    }
    assert projected("at") == {
        ("document", "imported_at"),
        ("job_run", "finished_at"),
    }
