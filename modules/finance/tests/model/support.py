"""What the model tests share: a reply schema and synthetic manual files.

The fake ``claude`` executable that tested the direct ``claude -p`` client went with
that client, which was not imported into rheoStream.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from rheo_finance.init_data import header_line
from rheo_finance.manual.headers import MANUAL_DIR_NAME, MANUAL_FILES


class Verdict(BaseModel):
    """A reply schema for the tests."""

    label: str
    confidence: float


def write_manual(data_dir: Path, name: str, *rows: str) -> None:
    """A synthetic manual file: its header, then ``rows`` as written."""
    manual = data_dir / MANUAL_DIR_NAME
    manual.mkdir(parents=True, exist_ok=True)
    body = header_line(MANUAL_FILES[name]) + "".join(row + "\n" for row in rows)
    (manual / name).write_text(body, encoding="utf-8")
