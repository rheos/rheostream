"""Every source kind FR-8 names has a registered parser, a listed synthetic fixture
and a passing parse (AC-8, parser half).

``FR8_KINDS`` is FR-8's own list, written out here, and ``SOURCES`` maps each kind to
the (issuer, format, parser id, version) that reads it, the fixture it is tested on
and the accounts that fixture names. The tests fail when a kind has no source, when
its parser is not registered (a module missing from ``PARSER_MODULES`` included),
when its fixture is not in ``MANIFEST.toml``, or when ``parse_document`` does not
parse the fixture with that parser, tied out where its control kind checks one. An
issuer directory that gains a parser version without a fixture fails too.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import tomllib
from pathlib import Path, PurePosixPath

import pytest
from sqlalchemy import Connection, select

from rheo_finance.context import UnitOfWork
from rheo_finance.documents.issuers import place
from rheo_finance.documents.registry import document_id, file_archive
from rheo_finance.parsers.registry import parse_document, parsers_for
from rheo_finance.storage.schema import document, statement_period

from .conftest import ManualAccount, register_accounts

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "synthetic"
MANIFEST = FIXTURES / "MANIFEST.toml"
NOW = dt.datetime(2045, 4, 1, 12, 0, tzinfo=dt.UTC)

# FR-8's list of on-disk source kinds, in its order.
FR8_KINDS = (
    "Alder Neem PDF statement",
    "Alder Neem CSV export",
    "Alder Neem PDF transaction export",
    "Sumac statement",
    "Fig transaction history PDF",
    "Olive statement (version 1)",
    "Olive statement (version 2)",
    "Hazel activity statement PDF",
    "Alder chequing CSV export",
    "Boxwood bill PDF",
    "Invoices received",
    "Receipts",
)


A = ManualAccount
NEEM = A("neem", "Alder", "credit_card", ("XXXX-9991",), "masked_card")
SUMAC = A("sumac-card", "Sumac", "credit_card", ("**** **** **** 9992",), "masked_card")
FIG = A("fig", "Fig", "wallet", ("W-0001-ZZ",), currencies="CAD;USD")
OLIVE_NAME = "Olive"
OLIVE_CHEQUING = A("olive-chequing", OLIVE_NAME, "chequing", ("999000000101",))
OLIVE_NEST = A("olive-nest", OLIVE_NAME, "savings", ("999000000102",))
HAZEL = A("hazel", "Hazel", "payment_processor", ("AB12CD34",), currencies="CAD;USD")
CHEQUING = A("chequing", "Alder", "chequing", ("999.07.77",))
UTILITY = A("utility", "Boxwood", "bill_vendor", ("9990001234",))


@dataclasses.dataclass(frozen=True)
class Source:
    path: str  # where the fixture is filed in the archive
    fixture: str
    parser_id: str
    version: str
    accounts: tuple[ManualAccount, ...] = ()


SOURCES: dict[str, Source] = {
    "Alder Neem PDF statement": Source(
        "alder-neem/2050-01-24.pdf", "alder_neem_statement.txt",
        "alder_neem.statement", "1", (NEEM,),
    ),
    "Alder Neem CSV export": Source(
        "alder-neem/2048-02-22.csv", "alder_neem_export.csv",
        "alder_neem.csv", "1", (NEEM,),
    ),
    "Alder Neem PDF transaction export": Source(
        "alder-neem/2049-10-28-download.pdf", "alder_neem_transactions.txt",
        "alder_neem.transactions", "transactions-1", (NEEM,),
    ),
    "Sumac statement": Source(
        "sumac/2042-10-22.pdf", "sumac_statement.txt", "sumac.statement", "1", (SUMAC,),
    ),
    "Fig transaction history PDF": Source(
        "fig/2045-08-history.pdf", "fig_history.txt",
        "fig.history", "1", (FIG,),
    ),
    "Olive statement (version 1)": Source(
        "olive/2054-10-30.pdf", "olive_statement_current.txt",
        "olive.statement", "1", (OLIVE_CHEQUING, OLIVE_NEST),
    ),
    "Olive statement (version 2)": Source(
        "olive/2039-10-15.pdf", "olive_statement_older.txt",
        "olive.statement_older", "2", (OLIVE_CHEQUING, OLIVE_NEST),
    ),
    "Hazel activity statement PDF": Source(
        "hazel/2030-11-report.pdf", "hazel_activity.txt",
        "hazel.activity", "1", (HAZEL,),
    ),
    "Alder chequing CSV export": Source(
        "alder/transactions-2036-05.csv", "alder_chequing_hints.csv",
        "alder.chequing_csv", "2", (CHEQUING,),
    ),
    "Boxwood bill PDF": Source(
        "boxwood/2029-01-26.pdf", "boxwood_bill.txt", "boxwood.bill", "3", (UTILITY,),
    ),
    "Invoices received": Source(
        "invoices-received/sample-vendor/2047-06-03.pdf", "invoice_received.txt",
        "invoices_received.invoice", "3",
    ),
    "Receipts": Source(
        "receipts/sample-clinic/2036-05-24.pdf", "patient_receipt.txt",
        "receipts.patient_receipt", "2",
    ),
}  # fmt: skip


def issuer_and_format(source: Source) -> tuple[str, str]:
    placed = place(PurePosixPath(source.path))
    assert placed is not None, source.path
    return placed.issuer, placed.format


def test_every_fr8_kind_has_a_source() -> None:
    assert tuple(SOURCES) == FR8_KINDS


@pytest.mark.parametrize("kind", FR8_KINDS)
def test_the_kind_has_a_registered_parser(kind: str) -> None:
    source = SOURCES[kind]
    registered = {
        (each.parser_id, each.version)
        for each in parsers_for(*issuer_and_format(source))
    }
    assert (source.parser_id, source.version) in registered


@pytest.mark.parametrize("kind", FR8_KINDS)
def test_the_kind_has_a_listed_synthetic_fixture(kind: str) -> None:
    listed = {
        entry["path"]: entry
        for entry in tomllib.loads(MANIFEST.read_text(encoding="utf-8"))["fixture"]
    }
    path = f"tests/fixtures/synthetic/{SOURCES[kind].fixture}"
    assert (ROOT / path).is_file()
    assert listed[path]["synthetic"] is True


def test_every_parser_of_an_fr8_directory_has_a_source() -> None:
    """A parser version added for one of these directories needs its own source
    here, and so a fixture and a parse."""
    wanted = {issuer_and_format(source) for source in SOURCES.values()}
    registered = {
        each.parser_id for issuer, fmt in wanted for each in parsers_for(issuer, fmt)
    }
    assert registered == {source.parser_id for source in SOURCES.values()}


@pytest.mark.parametrize("kind", FR8_KINDS)
def test_the_kind_parses_its_fixture(
    kind: str, owner_conn: Connection, tmp_path: Path
) -> None:
    """``parse_document`` parses the fixture with the kind's parser, and every period
    whose control kind checks one (``summary``, ``running_balance``,
    ``bill_identity``) is tied out."""
    source = SOURCES[kind]
    data, archive = tmp_path / "data", tmp_path / "archive"
    archive.mkdir()
    uow = UnitOfWork(owner_conn)
    register_accounts(uow, data, *source.accounts)
    raw = (FIXTURES / source.fixture).read_bytes()
    target = archive / source.path
    target.parent.mkdir(parents=True)
    target.write_bytes(raw)
    file_archive(uow, archive, data, NOW)
    doc = document_id(hashlib.sha256(raw).hexdigest())

    parsed = parse_document(
        uow, doc, archive, extract_pdf=lambda path: path.read_text(encoding="utf-8")
    )

    assert (parsed.status, parsed.reason) == ("parsed", None)
    assert parsed.lines + parsed.periods > 0
    row = owner_conn.execute(select(document).where(document.c.id == doc)).one()
    assert (row.parser_id, row.parser_version) == (source.parser_id, source.version)
    periods = owner_conn.execute(
        select(statement_period.c.control_kind, statement_period.c.tied_out).where(
            statement_period.c.document_id == doc
        )
    ).all()
    assert all(tied for control, tied in periods if control != "none")
