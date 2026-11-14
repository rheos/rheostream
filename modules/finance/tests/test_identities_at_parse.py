"""The parsers read the identities file in force when they parse, not at import.

``finance rebuild`` and the tick import the derive (and with it every pattern the
vendored Alder rules build) before they bind the workspace layout, so a pattern built
at import would hold the codename stand-ins for the life of the process. Seams: a
synthetic identities file that rewords the Alder, Sumac and Alder Neem forms, the
synthetic fixtures reworded through it, ``identities.per_identities``,
``identities.digest`` and ``derive.stage1.parse_inputs``. Every wording here is
invented; none is an institution's.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from rheo_finance import identities
from rheo_finance.derive import stage1
from rheo_finance.parsers import (
    alder_neem_transactions,
    docextract_adapter,
    sumac_statement,
)
from rheo_finance.parsers.base import AccountLookup, NotRecognised, Rejected

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "synthetic"
ALDER = FIXTURES / "alder_statement_older.txt"
SUMAC = FIXTURES / "sumac_statement.txt"
NEEM = FIXTURES / "alder_neem_transactions.txt"

# Invented wording for the codename forms, and the Alder period line in that wording.
PERIOD = (
    r"ZQBANK CYCLE\s+(\d{1,2} (?:@MONTHS@) \d{4})"
    r" UNTIL (\d{1,2} (?:@MONTHS@) \d{4})"
)
REWORDED = (
    "[forms]\n"
    '"ALDER" = "ZQBANK"\n'
    '"Sumac" = "Zqcard"\n'
    '"SUMAC" = "ZQCARD"\n'
    '"Neem" = "Zqnest"\n'
    "\n"
    "[layout]\n"
    f"\"alder.statement.period\" = '{PERIOD}'\n"
)


@pytest.fixture
def owner_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / identities.FILE_NAME
    monkeypatch.setattr(identities, "identities_path", lambda: path)
    return path


def _alder(text: str) -> bool:
    try:
        docextract_adapter.alder_statement(text)
    except NotRecognised:
        return False
    return True


def _with_lookup(
    parse: Callable[[str, AccountLookup], object],
) -> Callable[[str], bool]:
    def recognised(text: str) -> bool:
        try:
            parse(text, AccountLookup(()))
        except NotRecognised:
            return False
        except Rejected:
            return True  # read as its layout, then refused for the empty registry
        return True

    return recognised


READERS: dict[str, tuple[Path, Callable[[str], bool]]] = {
    "alder statement": (ALDER, _alder),
    "sumac statement": (SUMAC, _with_lookup(sumac_statement.parse)),
    "alder neem transactions": (NEEM, _with_lookup(alder_neem_transactions.parse)),
}


@pytest.mark.parametrize("name", sorted(READERS))
def test_a_parser_follows_the_file_in_force_when_it_parses(
    owner_file: Path, name: str
) -> None:
    fixture, recognised = READERS[name]
    original = fixture.read_text(encoding="utf-8")
    assert recognised(original)

    owner_file.write_text(REWORDED, encoding="utf-8")
    reworded = identities.real(original)
    assert reworded != original
    assert recognised(reworded)
    assert not recognised(original)

    owner_file.unlink()
    assert recognised(original)
    assert not recognised(reworded)


def test_an_import_before_the_layout_is_bound_still_reads_the_file(
    tmp_path: Path,
) -> None:
    """The order ``finance rebuild`` and the tick use: the derive is imported with no
    identities file in force, and the data directory is set only afterwards."""
    empty = tmp_path / "empty"
    data = tmp_path / "data"
    empty.mkdir()
    data.mkdir()
    (data / identities.FILE_NAME).write_text(REWORDED, encoding="utf-8")
    program = textwrap.dedent(
        f"""
        import os, sys
        import rheo_finance.derive.ops  # imports the vendored Alder rules
        from rheo_finance import identities
        from rheo_finance.parsers import docextract_adapter
        from rheo_finance.parsers.base import NotRecognised
        os.environ["FINANCE_DATA_DIR"] = {str(data)!r}
        text = identities.real(open({str(ALDER)!r}, encoding="utf-8").read())
        try:
            docextract_adapter.alder_statement(text)
        except NotRecognised:
            sys.exit("not recognised")
        print("recognised")
        """
    )
    env = {**os.environ, "FINANCE_DATA_DIR": str(empty), "HOME": str(empty)}
    done = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert (done.returncode, done.stdout.strip()) == (0, "recognised"), done.stderr


def test_per_identities_builds_once_per_file(owner_file: Path) -> None:
    built: list[str] = []

    @identities.per_identities
    def value() -> str:
        built.append(identities.real("ALDER"))
        return built[-1]

    assert (value(), value()) == ("ALDER", "ALDER")
    owner_file.write_text('[forms]\n"ALDER" = "ZQBANK"\n', encoding="utf-8")
    assert (value(), value()) == ("ZQBANK", "ZQBANK")
    owner_file.write_text('[forms]\n"ALDER" = "ZQVAULT"\n', encoding="utf-8")
    stamp = owner_file.stat().st_mtime_ns + 1_000_000
    os.utime(owner_file, ns=(stamp, stamp))
    assert value() == "ZQVAULT"
    assert built == ["ALDER", "ZQBANK", "ZQVAULT"]


def test_the_digest_names_the_files_bytes(owner_file: Path) -> None:
    assert identities.digest() is None
    owner_file.write_text(REWORDED, encoding="utf-8")
    assert identities.digest() == hashlib.sha256(REWORDED.encode("utf-8")).hexdigest()


def test_the_parse_inputs_change_with_the_identities_file() -> None:
    def inputs(wording: str | None) -> str:
        return stage1.parse_inputs("alder", "pdf", "r" * 64, "x+y", "h" * 64, wording)

    assert inputs(None) == inputs(None)
    assert inputs(None) != inputs("a" * 64)
    assert inputs("a" * 64) != inputs("b" * 64)
