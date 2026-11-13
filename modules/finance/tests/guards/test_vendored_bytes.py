"""The vendored ``docextract`` files are pinned, byte for byte.

``rheo_finance.parsers.vendored`` holds four files taken from ``doc-extraction-api``
at commit ``0ad7253f6586eea62cabc32d7d69823e3987e67b``: ``pdftext`` unchanged, the
other three with the recorded patches the package docstring describes (codenames
for institutions, layout wording and shapes read through ``identities``, no dates or
figures from documents); the patches themselves are kept privately. Each file is
pinned here by the sha256 of its bytes and by its Git blob id (``git hash-object``
gives the same id for the same bytes), so an edit in place fails this test. A change
that must be made is another recorded patch: its result gets a new pin here, with the
reason beside it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from rheo_finance.parsers import vendored

DIRECTORY = Path(vendored.__file__).resolve().parent
UPSTREAM_COMMIT = "0ad7253f6586eea62cabc32d7d69823e3987e67b"

# file name -> (Git blob id, sha256 of the bytes, how it relates to upstream)
PINS: dict[str, tuple[str, str, str]] = {
    "alder.py": (
        "df7e437cc29fe9a8eaa3e065d74a815793df1bb3",
        "bfbf441ca1852d2c46f1280a644365f1579217437a494e9c8ff56b3bf8567737",
        "recorded patches",
    ),
    "reference.py": (
        "7bb6e13d94065f972243f9eeaa27f4976c35faa4",
        "97c5589063dad2832975273c23b69e3dfc18580df63106ea87cc46f2771ff144",
        "recorded patches",
    ),
    "pdftext.py": (
        "5e814c4adcad042c53f4c5f03a1db70adee5720d",
        "f5bb0cfa1496cdf22906a98d40c807b38ecada7a6ead5430b683dc666c87660c",
        "upstream bytes",
    ),
    "schema.py": (
        "e2c776ca2ab51ab36a2de5891b4ac4a2dd1c7609",
        "38b54e7d3cad4c59d3d05013db6b2fab4dee419af0da6e6c4971468f2e278ad4",
        "recorded patches",
    ),
}


def git_blob_id(data: bytes) -> str:
    """What ``git hash-object`` names these bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def test_each_vendored_file_is_its_pinned_bytes() -> None:
    for name, (blob, sha256, _how) in PINS.items():
        data = (DIRECTORY / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == sha256, name
        assert git_blob_id(data) == blob, name


def test_the_package_holds_the_four_files_and_its_init_only() -> None:
    present = sorted(
        path.name
        for path in DIRECTORY.iterdir()
        if path.is_file() and path.suffix == ".py"
    )
    assert present == sorted([*PINS, "__init__.py"])


def test_the_pins_name_the_commit_the_package_declares() -> None:
    assert vendored.UPSTREAM_COMMIT == UPSTREAM_COMMIT
    assert {how for _, _, how in PINS.values()} == {
        "recorded patches",
        "upstream bytes",
    }


def test_a_one_byte_edit_changes_both_pins() -> None:
    """The control: the pins are computed over the bytes, so an edit is caught."""
    name = "pdftext.py"
    blob, sha256, _how = PINS[name]
    edited = (DIRECTORY / name).read_bytes() + b"\n"
    assert hashlib.sha256(edited).hexdigest() != sha256
    assert git_blob_id(edited) != blob
