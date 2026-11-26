"""The synthetic CSV fixtures are built from the parsers' public layouts.

``tests.csv_layouts`` writes every CSV fixture a codename parser reads, from rows
keyed by that parser's own column names, in the order of its registered public
layout. Each file must be byte for byte what it writes, so a fixture cannot drift
into a layout of its own. The card exports read by the test-only card parser
(``tests/derive/support.py``) are no codename parser's layout and are left out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ..csv_layouts import FIXTURES, LAYOUTS, csv_text, fixture_text

SYNTHETIC = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
TEST_PARSER_FILES = frozenset(
    {"card_2038_export_a.csv", "card_2038_export_b.csv", "card_2039_export.csv"}
)


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_each_csv_fixture_is_what_the_builder_writes(name: str) -> None:
    assert (SYNTHETIC / name).read_text(encoding="utf-8") == fixture_text(name)


def test_every_csv_fixture_comes_from_the_builder() -> None:
    on_disk = {path.name for path in SYNTHETIC.glob("*.csv")}
    assert on_disk - TEST_PARSER_FILES == set(FIXTURES)


def test_a_row_naming_a_column_outside_the_layout_is_refused() -> None:
    with pytest.raises(KeyError):
        csv_text("fig", [{"Fig Elsewhere": "1"}])


def test_each_layout_is_a_parsers_registered_public_default() -> None:
    from rheo_finance.parsers import (
        alder_chequing_csv,
        alder_neem_csv,
        fig_statement_csv,
        hazel_activity_csv,
        juniper_balance_csv,
    )

    assert {name: shape.columns for name, shape in LAYOUTS.items()} == {
        "fig": fig_statement_csv.COLUMNS,
        "hazel": hazel_activity_csv.COLUMNS,
        "juniper": juniper_balance_csv.COLUMNS,
        "alder_chequing": alder_chequing_csv.ROLES,
        "alder_neem": alder_neem_csv.ROLES,
    }
