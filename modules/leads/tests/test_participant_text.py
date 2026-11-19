"""The feed's wrapper tag comes off whole, and nothing else does (#392)."""

import pytest
from rheo_leads.participant_text import WRAPPER_CLOSE, WRAPPER_OPEN, unwrapped


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"{WRAPPER_OPEN}Hello{WRAPPER_CLOSE}", "Hello"),
        (f"  {WRAPPER_OPEN}\n Hello \n{WRAPPER_CLOSE}\n", "Hello"),
        (f"{WRAPPER_OPEN}Hello", "Hello"),
        (f"Hello{WRAPPER_CLOSE}", "Hello"),
        ("Plain text", "Plain text"),
        (f"{WRAPPER_OPEN}{WRAPPER_CLOSE}", ""),
    ],
)
def test_the_outer_wrapper_comes_off(text: str, expected: str) -> None:
    assert unwrapped(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Use <b>bold</b> and <untrusted_participant_content> as written.",
        f"Before {WRAPPER_OPEN} middle {WRAPPER_CLOSE} after",
        f"{WRAPPER_OPEN.upper()}Shouting{WRAPPER_CLOSE.upper()}",
    ],
)
def test_other_markup_and_inner_tags_stay(text: str) -> None:
    assert unwrapped(text) == text


def test_only_one_pair_comes_off() -> None:
    doubled = f"{WRAPPER_OPEN}{WRAPPER_OPEN}Hi{WRAPPER_CLOSE}{WRAPPER_CLOSE}"
    assert unwrapped(doubled) == f"{WRAPPER_OPEN}Hi{WRAPPER_CLOSE}"
