"""Database fixtures: one migrated ``ws_*`` database per session on the repository's
test cluster, a rolled-back owner connection per test, and a reader engine.

Every fixture of the suite lives in this file. pytest 9 gives a directory's
``conftest.py`` fixtures only to the first collector of that directory, and a command
line that goes into a directory, out of it and back in makes a second one, whose tests
then report the fixture as not found (``tests/guards/test_test_layout.py``). So
``ledger`` and the fixtures built on it (``derive.support`` and
``derive.crypto_support`` hold what they build) and ``rowan_stub`` are here
(``rates.support`` holds its helpers), and a directory's own
``conftest.py`` defines no fixture.

Also the sink fixtures: a settings file under a temporary ``$FINANCE_DATA_DIR``, a
direct approval record, and a local stub HTTP server; and ``scratch_database``, a
throwaway database for the one kind of test that must commit rows (reading them back
under the reader role).

The cluster is the one the repository's own suite uses: ``RHEO_TEST_CLUSTER_DSN``
(``make test-fast`` starts a throwaway pg16 one on port 5434), else the compose
default. The harness logs in as that cluster's user and runs every connection as
``finance_owner`` or ``finance_reader`` (NOLOGIN roles it creates when absent), set at
connect, so no finance password exists. A cluster that cannot be reached fails every
test that needs it, loudly and with the command to run. It never skips: a skipped
database test passes vacuously.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import MappingProxyType

import pytest
from sqlalchemy import Connection, Engine, create_engine, insert, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from rheo_finance import db, identities, operations, settings
from rheo_finance.clock import FakeClock
from rheo_finance.context import (
    FinanceContext,
    FinancePaths,
    UnitOfWork,
)
from rheo_finance.contracts import Role
from rheo_finance.documents import issuers
from rheo_finance.parsers import registry
from rheo_finance.rates import rowan
from rheo_finance.settings import parse_settings
from rheo_finance.storage import schema
from rheo_finance.storage.ids import uuid7
from tests.derive.crypto_support import (
    CRYPTO_INSTITUTION,
    CRYPTO_ISSUER,
    CRYPTO_PARSERS,
    EXCHANGE_ID,
    WALLET_ID,
)
from tests.derive.support import (
    CARD_ISSUER,
    CARD_PARSERS,
    CSV_IDENTIFIER,
    FX2_IDENTIFIER,
    FX_IDENTIFIER,
    FX_INSTITUTION,
    FX_ISSUER,
    FX_PARSER,
    NOTICE_ISSUER,
    NOTICE_PARSER,
    SECOND,
    THIRD,
    Ledger,
)
from tests.finance_support import (
    SCRATCH_DATABASES,
    TEST_REMEDY,
    DatabaseUnderTest,
    StubServer,
    cluster_url,
    drop_database,
    no_snapshot,
    provision_database,
)
from tests.rates.support import stub_client
from tests.sinks.support import CurrentStub

# The exit code of the tripwire ``claude`` every test finds first on PATH.
CLAUDE_TRIPWIRE_EXIT = 97
CLAUDE_TRIPWIRE_SCRIPT = (
    "#!/bin/sh\n"
    'echo "the real claude is blocked in the test suite" >&2\n'
    f"exit {CLAUDE_TRIPWIRE_EXIT}\n"
)


@pytest.fixture(scope="session", autouse=True)
def claude_tripwire(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """No test can launch the owner's real ``claude``: a ``claude`` that fails loudly
    is first on PATH for the whole session. A model test replaces PATH with its own
    fake; anything else that looks ``claude`` up gets this one."""
    directory = tmp_path_factory.mktemp("claude-tripwire")
    script = directory / "claude"
    script.write_text(CLAUDE_TRIPWIRE_SCRIPT, encoding="utf-8")
    script.chmod(0o755)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")
        yield directory


@pytest.fixture(scope="session")
def test_database() -> Iterator[DatabaseUnderTest]:
    """One migrated ``ws_*`` database per test session (per xdist worker), dropped
    when the session ends. A killed session leaves it for the repository's reaper,
    which drops stale ``ws_<32 hex>`` databases nobody is connected to."""
    cluster = cluster_url()
    try:
        built = provision_database(cluster)
    except OperationalError as exc:
        pytest.fail(f"{TEST_REMEDY} ({type(exc.orig).__name__})", pytrace=False)
    except db.DbError as exc:
        pytest.fail(f"{TEST_REMEDY} ({exc})", pytrace=False)
    try:
        yield built
    finally:
        drop_database(cluster, built.owner_url.database or "")


@pytest.fixture(scope="module")
def scratch_database(test_database: DatabaseUnderTest) -> Iterator[DatabaseUnderTest]:
    """A throwaway database, set up and migrated like ``test_database``, dropped after
    the module.

    A reader connection cannot see rows inside another connection's rolled-back
    transaction, so a test that checks a view under the reader role commits its rows
    here. Values are synthetic.
    """
    cluster = cluster_url()
    try:
        built = provision_database(cluster)
    except (OperationalError, db.DbError) as exc:
        pytest.fail(f"{TEST_REMEDY} (scratch database: {exc})", pytrace=False)
    name = built.owner_url.database or ""
    SCRATCH_DATABASES.add(name)
    try:
        yield built
    finally:
        SCRATCH_DATABASES.discard(name)
        drop_database(cluster, name)


@pytest.fixture(scope="session")
def owner_engine(test_database: DatabaseUnderTest) -> Iterator[Engine]:
    engine = create_engine(test_database.owner_url, poolclass=NullPool)
    yield engine
    engine.dispose()


@pytest.fixture
def owner_conn(owner_engine: Engine) -> Iterator[Connection]:
    """An owner connection inside a transaction that is rolled back after the test."""
    with owner_engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture(scope="session")
def reader_engine(test_database: DatabaseUnderTest) -> Iterator[Engine]:
    engine = create_engine(test_database.reader_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.fail(f"{TEST_REMEDY} (reader: {exc.orig})", pytrace=False)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def no_owner_identities(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reads the owner's identities file: every codename stands for itself,
    as the synthetic documents are written."""
    missing = tmp_path / "no-owner-identities" / identities.FILE_NAME
    monkeypatch.setattr(identities, "identities_path", lambda: missing)


@pytest.fixture(autouse=True)
def isolated_current_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Derive loads every provider; none may discover a real Current credential."""
    from rheo_finance.sinks import current

    monkeypatch.setattr(
        current,
        "load_settings",
        lambda: settings.load_settings(tmp_path / settings.SETTINGS_FILE_NAME),
    )


@pytest.fixture
def current_stub() -> Iterator[CurrentStub]:
    stub = CurrentStub()
    try:
        yield stub
    finally:
        stub.stop()


@pytest.fixture
def current_settings(
    current_stub: CurrentStub,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> settings.Settings:
    from rheo_finance.http import Client
    from rheo_finance.sinks import current

    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    credential = tmp_path / "synthetic.env"
    credential.write_text(
        f'export CURRENT_API_URL="{current_stub.url}"\n'
        'export CURRENT_ALERT_TOKEN="synthetic-token"\n'
    )
    text_ = (
        f'[finance.reminders]\ncurrent_credential_file = "{credential}"\n'
        'min_due_date = "2063-01-01"\n[finance.sinks.current]\nenabled = true\n'
    )
    (tmp_path / settings.SETTINGS_FILE_NAME).write_text(text_)
    monkeypatch.setattr(
        current,
        "make_client",
        lambda u, s: Client(
            "current", s, current.ALLOWLIST, uow=u, allowed_schemes=("http",)
        ),
    )
    return parse_settings(text_)


@pytest.fixture
def current_context(
    current_settings: settings.Settings,
    rolled_back_context: Callable[[Role], FinanceContext],
    record_sink_approval: Callable[..., uuid.UUID],
    owner_conn: Connection,
) -> FinanceContext:
    from dataclasses import replace

    from rheo_finance.sinks import current
    from rheo_finance.sinks.spec import spec_digest

    ctx = replace(rolled_back_context(Role.SERVICE), settings=current_settings)
    record_sink_approval(
        "current",
        spec_digest(current.spec(UnitOfWork(owner_conn))),
        at=ctx.clock.now_utc(),
    )
    return ctx


@pytest.fixture
def scratch_current_engine(scratch_database: DatabaseUnderTest) -> Iterator[Engine]:
    from sqlalchemy import delete, select

    engine = create_engine(scratch_database.owner_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        # Per-case committed rows are removed before the shared scratch DB teardown.
        # This cleanup scope exists before any fixture/test can commit its setup.
        with engine.begin() as conn:
            slugs = (
                "sample-utility",
                "sample-renamed",
                "sample-commit",
                "sample-rollback",
            )
            ids = select(schema.payee.c.id).where(schema.payee.c.slug.in_(slugs))
            conn.execute(
                delete(schema.obligation).where(schema.obligation.c.payee_id.in_(ids))
            )
            conn.execute(delete(schema.payee).where(schema.payee.c.slug.in_(slugs)))
            conn.execute(
                delete(schema.sink_ticket).where(
                    schema.sink_ticket.c.source_ref.in_(
                        (
                            "finance:sample-utility:bill-2063-05",
                            "finance:sample-renamed:bill-2063-05",
                        )
                    )
                )
            )
        engine.dispose()


@pytest.fixture
def durable_current_context(
    scratch_database: DatabaseUnderTest,
    scratch_current_engine: Engine,
    current_settings: settings.Settings,
    make_context: Callable[[Role], FinanceContext],
) -> FinanceContext:
    from dataclasses import replace

    from sqlalchemy import func, select

    from rheo_finance.sinks import current
    from rheo_finance.sinks.spec import spec_digest

    # scratch_database registered its full teardown before this fixture can commit.
    ctx = operations.with_databases(
        replace(make_context(Role.SERVICE), settings=current_settings),
        owner_url=scratch_database.owner_url,
        reader_url=scratch_database.reader_url,
    )
    with operations.owner_transaction(ctx) as uow:
        newest = uow.scalar(select(func.max(schema.decision.c.decided_at)))
        at = (
            max(ctx.clock.now_utc(), newest + dt.timedelta(seconds=1))
            if newest
            else ctx.clock.now_utc()
        )
        uow.execute(
            insert(schema.decision).values(
                id=uuid7(ctx.clock.now_utc()),
                kind="sink_approve",
                sink="current",
                spec_digest=spec_digest(current.spec(uow)),
                preview_digest="9" * 64,
                decided_at=at,
            )
        )
    return ctx


@pytest.fixture
def make_context(
    test_database: DatabaseUnderTest, tmp_path: Path
) -> Callable[[Role], FinanceContext]:
    """A ``FinanceContext`` for a role over ``finance_test``, with a fake clock."""

    # The archive and Downloads point at paths that do not exist, so an operation
    # dispatched with these settings never reads or writes the real directories.
    unset = parse_settings(
        f'[finance.paths]\narchive = "{tmp_path / "archive-unset"}"\n'
        f'downloads = "{tmp_path / "downloads-unset"}"\n'
    )

    def build(role: Role) -> FinanceContext:
        ctx = FinanceContext(
            settings=unset,
            clock=FakeClock(dt.datetime(2063, 1, 18, 12, 0, tzinfo=dt.UTC)),
            paths=FinancePaths(data=tmp_path),
            role=role,
            snapshot_provider=no_snapshot,
        )
        return operations.with_databases(
            ctx,
            owner_url=test_database.owner_url,
            reader_url=test_database.reader_url,
        )

    return build


@pytest.fixture
def rolled_back_context(
    owner_conn: Connection,
    make_context: Callable[[Role], FinanceContext],
) -> Callable[[Role], FinanceContext]:
    """``make_context``, but ``dispatch`` runs owner operations on ``owner_conn``.

    Each operation's commit releases a savepoint inside the test's transaction, which
    is rolled back after the test. The append-only tables keep committed rows for good,
    so a test that writes decisions dispatches through this.
    """

    def build(role: Role) -> FinanceContext:
        return operations.with_owner_connection(make_context(role), owner_conn)

    return build


@pytest.fixture
def empty_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, operations.OperationSpec]:
    """Swap ``operations.SPECS`` for an empty dict; the live one is restored after."""
    fresh: dict[str, operations.OperationSpec] = {}
    monkeypatch.setattr(operations, "SPECS", fresh)
    return fresh


# --- sinks: settings, a direct approval record, a local stub HTTP server -------------


@pytest.fixture
def sink_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., None]:
    """Point ``$FINANCE_DATA_DIR`` at ``tmp_path`` (every sink setting false) and
    return ``set_enabled(sink=True, ...)``, which rewrites its ``settings.toml``.

    The gate reads settings from ``$FINANCE_DATA_DIR`` at each call, so without this
    a test would read the owner's own file. A sink setting is true only here.
    """
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))

    def set_enabled(**sinks: bool) -> None:
        lines = [
            f"[finance.sinks.{sink}]\nenabled = {str(on).lower()}\n"
            for sink, on in sinks.items()
        ]
        (tmp_path / settings.SETTINGS_FILE_NAME).write_text(
            "\n".join(lines), encoding="utf-8"
        )

    return set_enabled


@pytest.fixture
def insert_decision(owner_conn: Connection) -> Callable[..., uuid.UUID]:
    """Insert one decision row directly on ``owner_conn`` and return its id.

    ``finance.decision.record`` refuses ``sink_approve`` by design (only ``finance
    sink approve`` records one), so a test that needs an approval writes the row here,
    inside the test's rolled-back transaction. Pass ``at`` to order decisions.
    """

    def insert_row(kind: str, *, at: dt.datetime, **columns: object) -> uuid.UUID:
        decision_id = uuid7(at)
        owner_conn.execute(
            insert(schema.decision).values(
                id=decision_id, kind=kind, decided_at=at, **columns
            )
        )
        return decision_id

    return insert_row


@pytest.fixture
def record_sink_approval(
    insert_decision: Callable[..., uuid.UUID],
) -> Callable[..., uuid.UUID]:
    """Record a ``sink_approve`` for ``sink`` pinned to ``spec_digest``, at ``at``."""

    def approve(sink: str, spec_digest: str, *, at: dt.datetime) -> uuid.UUID:
        return insert_decision(
            "sink_approve",
            at=at,
            sink=sink,
            spec_digest=spec_digest,
            preview_digest="9" * 64,
        )

    return approve


@pytest.fixture
def make_stub_server() -> Iterator[Callable[[], StubServer]]:
    """Start as many stub servers as a test needs; all stop after it."""
    started: list[StubServer] = []

    def start() -> StubServer:
        server = StubServer()
        started.append(server)
        return server

    yield start
    for server in started:
        server.stop()


@pytest.fixture
def stub_server(make_stub_server: Callable[[], StubServer]) -> StubServer:
    return make_stub_server()


@pytest.fixture
def ledger(
    owner_conn: Connection,
    rolled_back_context: Callable[[Role], FinanceContext],
    tmp_path: Path,
) -> Ledger:
    return Ledger(owner_conn, rolled_back_context(Role.OWNER), tmp_path)


@pytest.fixture
def fx_ledger(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> Ledger:
    """``ledger`` with one chequing account at ``FX_INSTITUTION`` (slug ``fx``) and
    the ``FX_ISSUER`` directory mapped to ``parse_fx``, for this test only."""
    monkeypatch.setattr(
        issuers,
        "ISSUER_DIRS",
        MappingProxyType(
            {**issuers.ISSUER_DIRS, FX_ISSUER: issuers.IssuerDir("statement")}
        ),
    )
    registry.load_parsers()
    monkeypatch.setitem(registry.PARSERS, FX_PARSER.parser_id, FX_PARSER)
    ledger.accounts(("fx", FX_INSTITUTION, "chequing", FX_IDENTIFIER))
    return ledger


@pytest.fixture
def card_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Map ``CARD_ISSUER`` as a statement issuer (a CSV there is an export) and
    register the card parsers, for this test only."""
    monkeypatch.setattr(
        issuers,
        "ISSUER_DIRS",
        MappingProxyType(
            {
                **issuers.ISSUER_DIRS,
                CARD_ISSUER: issuers.IssuerDir("statement", {"csv": "export"}),
            }
        ),
    )
    registry.load_parsers()
    for parser in CARD_PARSERS:
        monkeypatch.setitem(registry.PARSERS, parser.parser_id, parser)


@pytest.fixture
def notice_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Map ``NOTICE_ISSUER`` as a directory of emails, one sub-directory per sender,
    and register ``parse_notice``, for this test only."""
    directories = MappingProxyType(
        {**issuers.ISSUER_DIRS, NOTICE_ISSUER: issuers.IssuerDir("email", nested=True)}
    )
    monkeypatch.setattr(issuers, "ISSUER_DIRS", directories)
    # The registry holds its own name for the map, to find a nested issuer's directory.
    monkeypatch.setattr(registry, "ISSUER_DIRS", directories)
    registry.load_parsers()
    monkeypatch.setitem(registry.PARSERS, NOTICE_PARSER.parser_id, NOTICE_PARSER)


@pytest.fixture
def own(fx_ledger: Ledger) -> Ledger:
    """Three chequing accounts and a Neem card at Alder, and two accounts at the
    synthetic FX institution (``FX_LINES`` documents under ``FX_ISSUER``)."""
    fx_ledger.accounts(
        ("chequing", "Alder", "chequing", CSV_IDENTIFIER),
        ("second", "Alder", "chequing", SECOND),
        ("third", "Alder", "chequing", THIRD),
        ("neem", "Alder", "credit_card", "999000009991"),
        ("fx", FX_INSTITUTION, "chequing", FX_IDENTIFIER),
        ("fx2", FX_INSTITUTION, "chequing", FX2_IDENTIFIER),
    )
    return fx_ledger


@pytest.fixture
def crypto_ledger(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> Ledger:
    """``ledger`` with two crypto accounts at ``CRYPTO_INSTITUTION`` (slugs ``wallet``,
    kind ``crypto_wallet``, and ``exchange``, kind ``exchange``) and the
    ``CRYPTO_ISSUER`` directory mapped to the crypto fixture parsers, for this test
    only. Tests that need other accounts write ``accounts.csv`` again."""
    monkeypatch.setattr(
        issuers,
        "ISSUER_DIRS",
        MappingProxyType(
            {**issuers.ISSUER_DIRS, CRYPTO_ISSUER: issuers.IssuerDir("export")}
        ),
    )
    registry.load_parsers()
    for parser in CRYPTO_PARSERS:
        monkeypatch.setitem(registry.PARSERS, parser.parser_id, parser)
    ledger.accounts(
        ("wallet", CRYPTO_INSTITUTION, "crypto_wallet", WALLET_ID),
        ("exchange", CRYPTO_INSTITUTION, "exchange", EXCHANGE_ID),
    )
    return ledger


@pytest.fixture
def rowan_stub(stub_server: StubServer, monkeypatch: pytest.MonkeyPatch) -> StubServer:
    """Rowan pointed at the local stub over plain http for one test; the provider then
    declares the stub's host, so an approval in the test is pinned to that spec.
    Nothing reaches the real Rowan."""
    monkeypatch.setattr(rowan, "ENDPOINT", rowan.Endpoint(stub_server.host, "http"))
    monkeypatch.setattr(rowan, "make_client", stub_client)
    return stub_server
