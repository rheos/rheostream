"""``identities.real``: institution wording stays out of the public source.

Seams: ``real`` and ``forms`` over a synthetic identities file; a parser that must
match an institution's own name (the energy bill's title) and a reference pattern that
names one (the processor's bank-deposit wording), each run with and without the file.
Every wording here is invented; none is an institution's.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from rheo_finance import identities
from rheo_finance.parsers import base, boxwood_bill
from rheo_finance.reference import patterns

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "synthetic" / "boxwood_bill.txt"
)
# The processor's wording for a deposit from the bank, as an owner's file would hold it.
DEPOSIT = '[forms]\n"hz load" = "bank deposit to zq account"\n'
ACCOUNT = base.RegisteredAccount(id=uuid.UUID(int=1), slug="test-energy")


class EveryAccount(base.AccountLookup):
    """A lookup that finds the one test account for any question."""

    def __init__(self) -> None:
        super().__init__(())

    def by_last_four(
        self, *_args: object, **_kwargs: object
    ) -> tuple[base.RegisteredAccount, ...]:
        return (ACCOUNT,)


@pytest.fixture
def owner_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / identities.FILE_NAME
    monkeypatch.setattr(identities, "identities_path", lambda: path)
    return path


def write(path: Path, **forms: str) -> None:
    body = "[forms]\n" + "".join(f'"{k}" = "{v}"\n' for k, v in forms.items())
    path.write_text(body, encoding="utf-8")


def test_without_a_file_every_form_stands_for_itself(owner_file: Path) -> None:
    assert not owner_file.exists()
    assert identities.real("Boxwood bill from BOXWOOD") == "Boxwood bill from BOXWOOD"
    assert dict(identities.forms()) == {}


def test_whole_forms_are_replaced_longest_first_and_case_as_written(
    owner_file: Path,
) -> None:
    owner_file.write_text(
        '[forms]\n"Fig" = "Sample Money"\n"Fig Entry" = "Sample Money Ref"\n'
        '"hz load" = "bank deposit to zq account"\n',
        encoding="utf-8",
    )
    assert identities.real("Fig Entry") == "Sample Money Ref"
    assert identities.real("Fig statement") == "Sample Money statement"
    # Never inside a longer word, and only the case the file names.
    assert identities.real("Figment FIG fig") == "Figment FIG fig"
    assert identities.real("hz load") == "bank deposit to zq account"


def test_an_unusable_file_is_refused_without_its_contents(owner_file: Path) -> None:
    owner_file.write_text('[forms]\n"Boxwood" = 7\n', encoding="utf-8")
    with pytest.raises(identities.IdentitiesError) as refused:
        identities.real("Boxwood")
    assert "Boxwood" not in str(refused.value)


def test_a_bill_titled_with_the_issuers_own_name_needs_the_file(
    owner_file: Path,
) -> None:
    codename = FIXTURE.read_text(encoding="utf-8")
    own_name = codename.replace("Boxwood", "Sample Power", 1)
    expected = boxwood_bill.parse(codename, EveryAccount())

    with pytest.raises(base.NotRecognised):
        boxwood_bill.parse(own_name, EveryAccount())
    write(owner_file, Boxwood="Sample Power")
    assert boxwood_bill.parse(own_name, EveryAccount()) == expected


def test_a_pattern_naming_an_institution_matches_its_own_word(
    owner_file: Path,
) -> None:
    def hint(text: str) -> str | None:
        return patterns.classify(
            text, account_kind="payment_processor", issuer="hazel"
        ).transfer_hint

    assert hint("hz load") == "hazel_preauthorized"
    assert hint("bank deposit to zq account") is None
    owner_file.write_text(DEPOSIT, encoding="utf-8")
    assert hint("bank deposit to zq account") == "hazel_preauthorized"


def test_the_patterns_digest_covers_the_owner_forms(owner_file: Path) -> None:
    patterns.patterns_digest.cache_clear()
    try:
        bare = patterns.patterns_digest()
        owner_file.write_text(DEPOSIT, encoding="utf-8")
        patterns.patterns_digest.cache_clear()
        assert patterns.patterns_digest() != bare
    finally:
        patterns.patterns_digest.cache_clear()
