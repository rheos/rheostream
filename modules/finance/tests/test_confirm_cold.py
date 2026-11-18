"""A cold public confirm answer records and applies a synthetic payment."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.finance_support import DatabaseUnderTest


def test_cold_confirm_answer_records_and_applies_payment(
    test_database: DatabaseUnderTest, tmp_path: Path
) -> None:
    # The session's own test database outlives the child. Only the guarded test URL
    # crosses the process boundary, privately through the environment.
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path)],
        env={
            **os.environ,
            "CONFIRM_TEST_OWNER_DSN": test_database.owner_url.render_as_string(
                hide_password=False
            ),
        },
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _exercise_cold_answer(root: Path) -> None:
    import contextlib
    import io
    from decimal import Decimal

    import pytest
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    from rheo_finance import composition, operations, queue
    from rheo_finance.cli.main import main
    from rheo_finance.clock import FakeClock
    from rheo_finance.context import FinanceContext, FinancePaths
    from rheo_finance.contracts import Role
    from rheo_finance.derive.snapshot import txn_snapshot
    from rheo_finance.parsers import docextract_adapter
    from rheo_finance.settings import parse_settings
    from rheo_finance.storage.schema import (
        decision,
        obligation,
        payment,
        payment_application,
    )
    from tests.derive.support import CSV_IDENTIFIER, NOW, D, Ledger, read_as_text
    from tests.derive.test_payment_application import neem_statement
    from tests.derive.test_payments import bank, name_card
    from tests.derive.test_transfer_edges import BILL_PAYMENT

    url = make_url(os.environ.pop("CONFIRM_TEST_OWNER_DSN"))
    if not re.fullmatch(r"ws_[0-9a-f]{32}", url.database or ""):
        raise AssertionError("cold confirm requires the suite's ws_* test database")
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                ctx = operations.with_owner_connection(
                    operations.with_databases(
                        FinanceContext(
                            settings=parse_settings(""),
                            clock=FakeClock(NOW),
                            paths=FinancePaths(data=root / "data"),
                            role=Role.OWNER,
                            snapshot_provider=txn_snapshot,
                        ),
                        owner_url=url,
                        reader_url=url,
                    ),
                    connection,
                )
                ledger = Ledger(connection, ctx, root)
                ledger.accounts(
                    ("chequing", "Alder", "chequing", CSV_IDENTIFIER),
                    ("neem", "Alder", "credit_card", "999000009991"),
                )
                name_card(ledger)
                ledger.file(
                    "alder-neem/2043-06-01.pdf",
                    neem_statement(
                        D(2043, 5, 6),
                        D(2043, 6, 1),
                        Decimal("357.80"),
                        (),
                        Decimal("17.89"),
                        D(2043, 6, 29),
                    ),
                )
                bank(ledger, (D(2043, 6, 14), BILL_PAYMENT, Decimal("-178.90")))
                with pytest.MonkeyPatch.context() as patch:
                    patch.setenv("FINANCE_DATA_DIR", str(ledger.data))
                    patch.setattr(composition, "build_context", lambda role: ledger.ctx)
                    patch.setattr(docextract_adapter, "pdf_text", read_as_text)
                    ledger.derive()
                    pending = operations.dispatch(
                        "finance.queue.list", {}, Role.OWNER, ctx=ledger.ctx
                    )
                    assert isinstance(pending, queue.ListOut)
                    item = next(
                        row for row in pending.items if row.kind == "card_debit"
                    )
                    assert connection.scalar(select(payment.c.state)) == "confirm_only"
                    assert connection.scalar(select(obligation.c.remaining)) == Decimal(
                        "357.80"
                    )
                    assert (
                        connection.scalar(select(func.count()).select_from(decision))
                        == 0
                    )
                    # Setup uses derive.run, never the catalogue or derive.ops.
                    # This must still be a cold command when its public entry runs.
                    assert "rheo_finance.derive.ops" not in sys.modules
                    assert "finance.ledger.derive" not in operations.SPECS
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with (
                        contextlib.redirect_stdout(stdout),
                        contextlib.redirect_stderr(stderr),
                    ):
                        code = main(["confirm", "answer", item.code, "yes"])
                    assert (
                        connection.scalar(select(func.count()).select_from(decision))
                        == 1
                    )
                    assert connection.scalar(select(decision.c.kind)) == "payment_apply"
                    assert code == 0, (
                        f"public confirm failed: exit={code}; {stderr.getvalue()}"
                    )
                    assert stderr.getvalue() == ""
                    assert (
                        "1 decision(s) recorded; ledger derived." in stdout.getvalue()
                    )
                    assert connection.scalar(select(payment.c.state)) == "active"
                    assert connection.scalar(select(obligation.c.remaining)) == Decimal(
                        "178.90"
                    )
                    assert (
                        connection.scalar(select(obligation.c.threshold_remaining)) == 0
                    )
                    application = connection.execute(select(payment_application)).one()
                    assert application.obligation_id == connection.scalar(
                        select(obligation.c.id)
                    )
                    assert application.amount == Decimal("178.90")
                    after = operations.dispatch(
                        "finance.queue.list", {}, Role.OWNER, ctx=ledger.ctx
                    )
                    assert isinstance(after, queue.ListOut)
                    assert not any(row.kind == "card_debit" for row in after.items)
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


if __name__ == "__main__":
    # Run as a script, so the suite's package (``tests``) is found from its parent.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    _exercise_cold_answer(Path(sys.argv[1]))
