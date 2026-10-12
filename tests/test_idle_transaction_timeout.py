"""The deployment never cuts a long collector run's idle work transaction.

A job feed run keeps the worker's transaction open, and idle, for up to about
430 s while it waits on the network. Postgres ends a session idle in a transaction
for longer than ``idle_in_transaction_session_timeout``, so neither compose file
may set it (as a server option, a command flag or an environment value) below
480 s. Leaving it unset is fine: the server default is no timeout.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = (ROOT / "deploy/compose.yaml", ROOT / "deploy/compose.flagship.yaml")
MINIMUM_SECONDS = 480.0
SETTING = "idle_in_transaction_session_timeout"
_VALUE = re.compile(
    rf"{SETTING}\s*[=:]?\s*['\"]?(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|min|h|d)?",
    re.IGNORECASE,
)
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "min": 60.0, "h": 3600.0, "d": 86400.0}


def configured_seconds(text: str) -> list[float]:
    """Every value the text sets the setting to, in seconds (a bare number is
    milliseconds, as Postgres reads it). A mention with no readable value counts
    as zero, so it fails rather than passing unread."""
    values: list[float] = []
    for line in text.splitlines():
        if SETTING not in line.lower():
            continue
        matches = list(_VALUE.finditer(line))
        if not matches:
            values.append(0.0)
        for match in matches:
            unit = (match.group("unit") or "ms").lower()
            values.append(float(match.group("number")) * _UNIT_SECONDS[unit])
    return values


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_no_compose_file_sets_an_idle_transaction_timeout_below_480_seconds(
    path: Path,
) -> None:
    assert path.is_file(), path
    for seconds in configured_seconds(path.read_text()):
        assert seconds >= MINIMUM_SECONDS, (path.name, seconds)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("    command: postgres -c idle_in_transaction_session_timeout=60000", [60.0]),
        ('      - "idle_in_transaction_session_timeout=10min"', [600.0]),
        ("PGOPTIONS: -c idle_in_transaction_session_timeout=30s", [30.0]),
        ("idle_in_transaction_session_timeout: 8min", [480.0]),
        ("# idle_in_transaction_session_timeout is not set", [0.0]),
        ("command: postgres -c max_connections=100", []),
    ],
)
def test_every_spelling_of_the_setting_is_read(
    text: str, expected: list[float]
) -> None:
    """Positive controls: the scan reads a command flag, a list entry, an
    environment value and a mapping, and refuses a mention it cannot read."""
    assert configured_seconds(text) == expected
