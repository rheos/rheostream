"""Every synthetic CSV the finance tests use, built from the parsers' public layouts.

Each CSV parser registers one invented public layout: its ``COLUMNS`` (a header the
file prints) or ``ROLES`` (a headerless file). ``csv_text`` writes rows, given as
mappings from that parser's own column names to cell values, in that layout's order,
with its header when it prints one. No test spells out a header, a column order or a
row of its own: it takes the rows below (``fixture_rows``), changes a value, and
writes them again.

``FIXTURES`` holds the rows of the synthetic CSV fixture files, oldest first. The
files are this module's output: ``python -m tests.csv_layouts`` rewrites them, and
``tests/guards/test_csv_layouts.py`` checks each file is exactly what it would write.
"""

from __future__ import annotations

import csv
import io
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from rheo_finance.parsers import (
    alder_chequing_csv,
    alder_neem_csv,
    fig_statement_csv,
    hazel_activity_csv,
    juniper_balance_csv,
)

Row = Mapping[str, str]


@dataclass(frozen=True)
class Layout:
    columns: tuple[str, ...]
    header: bool  # a header row, every cell quoted; else headerless, quoted as needed


LAYOUTS: dict[str, Layout] = {
    "fig": Layout(fig_statement_csv.COLUMNS, header=True),
    "hazel": Layout(hazel_activity_csv.COLUMNS, header=True),
    "juniper": Layout(juniper_balance_csv.COLUMNS, header=True),
    "alder_chequing": Layout(alder_chequing_csv.ROLES, header=False),
    "alder_neem": Layout(alder_neem_csv.ROLES, header=False),
}


def csv_text(
    layout: str,
    rows: Iterable[Row],
    *,
    renamed: Mapping[str, str] | None = None,
    dropped: Iterable[str] = (),
) -> str:
    """``rows`` in ``layout``'s public column order. ``renamed`` prints a column under
    another header (a header the parser must refuse); ``dropped`` leaves columns out."""
    shape = LAYOUTS[layout]
    gone = set(dropped)
    columns = [each for each in shape.columns if each not in gone]
    out = io.StringIO()
    if shape.header:
        writer = csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\n")
    else:
        writer = csv.writer(out, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    if shape.header:
        writer.writerow([(renamed or {}).get(each, each) for each in columns])
    for row in rows:
        unknown = set(row) - set(shape.columns)
        if unknown:
            raise KeyError(f"{layout}: no column {sorted(unknown)}")
        writer.writerow([row.get(each, "") for each in columns])
    return out.getvalue()


# --- the fixture files' rows ---------------------------------------------------------

FIG_ROWS: tuple[Row, ...] = (
    # A card check: nothing moves, so it gives no line.
    {"Fig Day": "2045/08/05", "Fig Kind": "FG-K", "Fig Unit": "USD",
     "Fig Text": "SPEND Sample Service 0.00 USD", "Fig Amount": "0.00",
     "Fig Fee": "0.00", "Fig Left": "1008.02"},
    {"Fig Day": "2045/08/08", "Fig Kind": "FG-A", "Fig Unit": "CAD",
     "Fig Text": "OUT Sample Payee Two", "Fig Amount": "-5.30", "Fig Fee": "4.30",
     "Fig Left": "1597.06", "Fig Party": "Sample Payee Two"},
    {"Fig Day": "2045/08/10", "Fig Kind": "FG-A", "Fig Unit": "CAD",
     "Fig Text": "OUT Sample Payee", "Fig Amount": "-504.01", "Fig Fee": "0.40",
     "Fig Left": "1093.05", "Fig Party": "Sample Payee"},
    {"Fig Day": "2045/08/12", "Fig Kind": "FG-K", "Fig Unit": "CAD",
     "Fig Text": "SPEND Sample Web Shop 4.00 USD", "Fig Amount": "-84.09",
     "Fig Fee": "0.10", "Fig Left": "1008.96", "Fig Other Unit": "USD",
     "Fig Other Sum": "4.00"},
    {"Fig Day": "2045/08/15", "Fig Kind": "FG-K", "Fig Unit": "CAD",
     "Fig Text": "SPEND Sample Grocer 15.15 CAD", "Fig Amount": "-15.15",
     "Fig Fee": "0.00", "Fig Left": "993.81"},
    {"Fig Day": "2045/08/20", "Fig Kind": "FG-X", "Fig Unit": "USD",
     "Fig Text": "Turned 504.01 USD to 687.54 CAD", "Fig Amount": "-504.54",
     "Fig Fee": "0.53", "Fig Left": "503.48", "Fig Other Unit": "CAD",
     "Fig Other Sum": "687.54"},
    {"Fig Day": "2045/08/20", "Fig Kind": "FG-X", "Fig Unit": "CAD",
     "Fig Text": "Turned 504.01 USD to 687.54 CAD", "Fig Amount": "687.54",
     "Fig Fee": "0.00", "Fig Left": "1681.35", "Fig Other Unit": "USD",
     "Fig Other Sum": "504.01"},
    {"Fig Day": "2045/08/27", "Fig Kind": "FG-A", "Fig Unit": "USD",
     "Fig Text": "IN JUNIPER PAYMENTS", "Fig Amount": "257.05", "Fig Fee": "0.00",
     "Fig Left": "760.53", "Fig Party": "JUNIPER PAYMENTS"},
)  # fmt: skip

HAZEL_ROWS: tuple[Row, ...] = (
    {"Hz Day": "2030-10-28", "Hz Kind": "HZ SPEND", "Hz Unit": "CAD",
     "Hz Amount": "-73.21", "Hz Fee": "0.00", "Hz Balance": "11.16",
     "Hz Party": "Sample Shop"},
    {"Hz Day": "2030-11-03", "Hz Kind": "HZ LOAD", "Hz Unit": "CAD",
     "Hz Amount": "33.48", "Hz Fee": "0.00", "Hz Balance": "44.64"},
    {"Hz Day": "2030-11-03", "Hz Kind": "HZ RATE", "Hz Unit": "CAD",
     "Hz Amount": "-44.64", "Hz Fee": "0.00", "Hz Balance": "0.00"},
    {"Hz Day": "2030-11-03", "Hz Kind": "HZ RATE", "Hz Unit": "USD",
     "Hz Amount": "32.13", "Hz Fee": "0.00", "Hz Balance": "37.71"},
    # Held: the invented layout's skip kind, so it moves no balance and gives no line.
    {"Hz Day": "2030-11-04", "Hz Kind": "HZ HELD", "Hz Unit": "USD",
     "Hz Amount": "-37.71", "Hz Fee": "0.00", "Hz Balance": "37.71",
     "Hz Party": "Sample Streaming Co"},
    {"Hz Day": "2030-11-04", "Hz Kind": "HZ PLAN", "Hz Unit": "USD",
     "Hz Amount": "-37.71", "Hz Fee": "0.00", "Hz Balance": "0.00",
     "Hz Party": "Sample Streaming Co"},
    {"Hz Day": "2030-11-20", "Hz Kind": "HZ INFLOW", "Hz Unit": "USD",
     "Hz Amount": "22.32", "Hz Fee": "-0.43", "Hz Balance": "21.89",
     "Hz Party": "Sample Client Inc."},
)  # fmt: skip

JUNIPER_ROWS: tuple[Row, ...] = (
    {"Jn Day": "2045-08-03", "Jn Kind": "TAKE", "Jn Unit": "USD",
     "Jn Amount": "57.15", "Jn Fee": "1.95", "Jn Group": "grp501"},
    {"Jn Day": "2045-08-11", "Jn Kind": "UNDO", "Jn Unit": "USD",
     "Jn Amount": "-11.43", "Jn Fee": "0.00", "Jn Group": "grp501"},
    {"Jn Day": "2045-08-12", "Jn Kind": "BANKED", "Jn Unit": "USD",
     "Jn Amount": "-40.78", "Jn Fee": "2.99", "Jn Group": "grp501"},
    {"Jn Day": "2045-08-14", "Jn Kind": "TAKE", "Jn Unit": "USD",
     "Jn Amount": "114.30", "Jn Fee": "1.09", "Jn Group": "grp502"},
    {"Jn Day": "2045-08-17", "Jn Kind": "TAKE", "Jn Unit": "USD",
     "Jn Amount": "137.16", "Jn Fee": "3.90", "Jn Group": "grp502"},
    {"Jn Day": "2045-08-20", "Jn Kind": "TAKE", "Jn Unit": "USD",
     "Jn Amount": "13.08", "Jn Fee": "2.50", "Jn Group": "grp502"},
    {"Jn Day": "2045-08-26", "Jn Kind": "BANKED", "Jn Unit": "USD",
     "Jn Amount": "-257.05", "Jn Fee": "0.00", "Jn Group": "grp502"},
    # Not paid out yet: no group.
    {"Jn Day": "2045-08-27", "Jn Kind": "TAKE", "Jn Unit": "USD",
     "Jn Amount": "34.29", "Jn Fee": "9.31", "Jn Group": ""},
)  # fmt: skip

_FIELDS = "{}  {}  {}"  # a description's sub-fields, two spaces apart

CHEQUING_JUMP_ROWS: tuple[Row, ...] = (
    {"description": _FIELDS.format("DEPOSIT", "SAMPLE PAYROLL", "99906"),
     "balance": "768.80", "date": "2038/10/04", "deposit": "614.68"},
    {"description": _FIELDS.format("WITHDRAWAL", "SAMPLE ATM", "99905"),
     "balance": "744.00", "date": "2038/10/05", "withdrawal": "24.80"},
    {"description": _FIELDS.format("POS PURCHASE", "SAMPLE HARDWARE", "99904"),
     "balance": "654.43", "date": "2038/10/07", "withdrawal": "89.57"},
    # Rows are missing here: the balance jumps.
    {"description": _FIELDS.format("SETTLE TO", "SAMPLE UTILITY", "99903"),
     "balance": "472.05", "date": "2038/10/14", "withdrawal": "81.67"},
    {"description": _FIELDS.format("DEPOSIT", "SAMPLE TRANSFER IN", "99902"),
     "balance": "565.05", "date": "2038/10/14", "deposit": "93.00"},
    {"description": _FIELDS.format("POS PURCHASE", "SAMPLE GROCER", "99901"),
     "balance": "561.36", "date": "2038/10/18", "withdrawal": "3.69"},
)  # fmt: skip

CHEQUING_HINT_ROWS: tuple[Row, ...] = (
    {"description": "e-Send credit  Sample  9990001  ; sender = SAMPLE FRIEND",
     "balance": "713.00", "date": "2056/02/13", "deposit": "93.00"},
    {"description": "Settle to  ALDER NEEM  9991  Ref 999101",
     "balance": "465.00", "date": "2056/02/16", "withdrawal": "248.00"},
    {"description": _FIELDS.format("POS PURCHASE", "SAMPLE GROCER", "99907"),
     "balance": "449.50", "date": "2056/02/17", "withdrawal": "15.50"},
)  # fmt: skip

NEEM_ROWS: tuple[Row, ...] = (
    {"txn_date": "2048-02-04", "posted": "2048-02-04", "debit": "67.60",
     "description": "PURCHASE NEEM CARRY CHARGE"},
    {"txn_date": "2048-02-10", "posted": "2048-02-12", "debit": "56.58",
     "description": "SAMPLE GROCER"},
    {"txn_date": "2048-02-13", "posted": "2048-02-15", "credit": "24.43",
     "description": "SAMPLE BOOKSHOP"},
    {"txn_date": "2048-02-19", "posted": "2048-02-19", "credit": "488.60",
     "description": "CARD PAID IN -- NOTED"},
    {"txn_date": "2048-02-21", "posted": "2048-02-22", "debit": "6.21",
     "description": "SAMPLE CAFE"},
)  # fmt: skip

CHEQUING_ACCOUNT = "999.07.77"
NEEM_CARD = "XXXX-9991"


def _with(rows: tuple[Row, ...], **fixed: str) -> tuple[Row, ...]:
    return tuple({**fixed, **row} for row in rows)


# File name -> (layout, rows in the order the file lists them).
FIXTURES: dict[str, tuple[str, tuple[Row, ...]]] = {
    "fig_statement.csv": ("fig", FIG_ROWS),
    "hazel_activity.csv": ("hazel", HAZEL_ROWS),
    "juniper_balance.csv": ("juniper", JUNIPER_ROWS),
    # Rows newest first in this file.
    "alder_chequing_jump.csv": (
        "alder_chequing",
        _with(CHEQUING_JUMP_ROWS, account=CHEQUING_ACCOUNT)[::-1],
    ),
    "alder_chequing_hints.csv": (
        "alder_chequing",
        _with(CHEQUING_HINT_ROWS, account=CHEQUING_ACCOUNT),
    ),
    # Rows newest first in this file.
    "alder_neem_export.csv": ("alder_neem", _with(NEEM_ROWS, card=NEEM_CARD)[::-1]),
}


def fixture_rows(name: str) -> list[dict[str, str]]:
    """A fixture file's rows, as fresh dicts a test may change."""
    return [dict(row) for row in FIXTURES[name][1]]


def fixture_text(name: str) -> str:
    layout, rows = FIXTURES[name]
    return csv_text(layout, rows)


def write_fixtures(directory: Path) -> None:
    for name in FIXTURES:
        (directory / name).write_text(fixture_text(name), encoding="utf-8")


if __name__ == "__main__":
    write_fixtures(Path(sys.argv[1]) if len(sys.argv) > 1 else
                   Path(__file__).parent / "fixtures" / "synthetic")  # fmt: skip
