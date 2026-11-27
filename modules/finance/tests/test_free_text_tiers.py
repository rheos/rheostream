"""The public free text a finance read hands a token carries no figure (#568, ADR-0031).

ADR-0031 keeps the status lines and the open item and coverage reasons public, on
the ground that they are built from codes, counts, dates and labels alone. These tests
pin that ground: the columns each reason expression reads, subqueries included, and
the columns of the status facts view. A new column in either is a deliberate change
to this file, made with a look at whether it can carry an amount. The status lines
themselves are checked on a fixture ledger in ``test_status_ops``. No database here.
"""

from __future__ import annotations

from sqlalchemy import Column
from sqlalchemy.sql import visitors

from rheo_finance.reports import views


def _columns_read(select_fn: object, name: str) -> set[tuple[str, str]]:
    column = select_fn().selected_columns[name]  # type: ignore[operator]
    return {
        (element.table.name, element.name)
        for element in visitors.iterate(column)
        if isinstance(element, Column) and element.table is not None
    }


def test_the_open_item_reason_reads_no_amount() -> None:
    assert _columns_read(views.report_open_item_select, "reason") == {
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
    assert _columns_read(views.report_coverage_select, "reason") == {
        ("coverage", "reason_code")
    }


def test_the_status_lines_are_built_from_facts_with_no_amount_column() -> None:
    columns = [column.name for column in views.report_status_select().selected_columns]
    assert columns == ["fact", "subject", "count", "at"]
