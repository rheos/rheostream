"""The archive walk and the Downloads filer beat once per entry (#546, ADR-0027).

- L2: ``registry.walk`` calls ``progress`` first for every listed entry, so each
  hash comes right after a beat; the default no-op leaves the walk's result as it was.
- L3: ``filer.file_downloads`` calls ``progress`` first for every listed entry, the
  monitor file included, so each text read comes right after a beat; the counts
  equal a run without a beat.

No database: the filer's unit of work is a stand-in that knows no document. Every
file and identifier is synthetic.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import cast

import pytest

from rheo_finance.context import UnitOfWork
from rheo_finance.documents import filer, registry
from rheo_finance.path_check import PathCheck

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
STATEMENT = (FIXTURES / "alder_statement_newer.txt").read_bytes()
BILL = (FIXTURES / "yew_bill.txt").read_bytes()
NOW = dt.datetime(2063, 1, 18, 12, 0, tzinfo=dt.UTC)
IDENTIFIER = "samplewallet"


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# --- L2: the archive walk ----------------------------------------------------------


def _archive(root: Path) -> None:
    _write(root / "alder" / "2055-06-12.pdf", STATEMENT)
    _write(root / "alder" / "2055-07-12.pdf", STATEMENT + b" ")
    _write(root / "alder" / "2055-08-12.pdf", STATEMENT + b"  ")
    _write(root / "unmappedsample" / "notes.pdf", b"sample notes")
    _write(root / "alder" / f"{IDENTIFIER}-notes.pdf", b"sample notes")


def test_the_walk_beats_before_every_entry_and_every_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _archive(tmp_path)
    check = PathCheck([IDENTIFIER])
    plain = registry.walk(tmp_path, check)

    events: list[str] = []
    hashed = registry._sha256

    def sha256(path: Path) -> str:
        events.append("hash")
        return hashed(path)

    monkeypatch.setattr(registry, "_sha256", sha256)
    beaten = registry.walk(tmp_path, check, progress=lambda: events.append("beat"))

    assert events.count("beat") == 5
    assert events.count("hash") == 3
    for at, event in enumerate(events):
        if event == "hash":
            assert events[at - 1] == "beat"
    # The beat changes nothing the walk finds.
    assert beaten == plain
    assert len(plain.found) == 3
    assert sum(plain.skipped.values()) == 2


# --- L3: the Downloads filer -------------------------------------------------------


class _NoDocuments:
    """A unit of work that knows no document."""

    def scalar(self, _statement: object, _parameters: object = None) -> None:
        return None


def _scan(
    downloads: Path, archive: Path, events: list[str], *, beat: bool
) -> filer.DownloadsFiled:
    def extract(path: Path) -> str:
        events.append(f"read {path.name}")
        return path.read_bytes().decode("utf-8")

    archive.mkdir(exist_ok=True)
    if beat:
        return filer.file_downloads(
            cast(UnitOfWork, _NoDocuments()),
            downloads,
            archive,
            PathCheck([]),
            NOW,
            extract_pdf=extract,
            progress=lambda: events.append("beat"),
        )
    return filer.file_downloads(
        cast(UnitOfWork, _NoDocuments()),
        downloads,
        archive,
        PathCheck([]),
        NOW,
        extract_pdf=extract,
    )


def test_the_filer_beats_before_every_entry_and_every_read(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    _write(downloads / "a.pdf", STATEMENT)
    _write(downloads / "b.pdf", BILL)
    _write(downloads / "c.pdf", b"Sample notes with no date\n")
    _write(downloads / "monitor-sample.pdf", STATEMENT)

    plain_events: list[str] = []
    plain = _scan(downloads, tmp_path / "archive-plain", plain_events, beat=False)
    events: list[str] = []
    beaten = _scan(downloads, tmp_path / "archive-beaten", events, beat=True)

    assert events == [
        "beat",
        "read a.pdf",
        "beat",
        "read b.pdf",
        "beat",
        "read c.pdf",
        "beat",
    ]
    assert plain_events == [e for e in events if e != "beat"]
    assert beaten == plain
    assert (plain.filed, plain.monitor_skipped, plain.unrecognised) == (
        2,
        1,
        ("c.pdf",),
    )
