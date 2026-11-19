"""The marker tag job feed descriptions can arrive wrapped in (#392).

The Upwork detail read delivers a posting's description inside one
``<untrusted_participant_content>`` wrapper. :func:`unwrapped` drops exactly that
outer pair, and nothing else: markup inside the text, or a tag that is not at the
very start or end, is the client's own text and stays.

New postings are stored without it (``jobfeed.normalize.apply_detail``). Rows
stored before that still carry it, and a posting already stored is not read again
(#377), so the readers that hand the text to a person or a model unwrap it too. The
scoring fingerprint reads the stored text as it is, so unwrapping never makes a
scored job look changed.
"""

from typing import Final

WRAPPER_OPEN: Final = "<untrusted_participant_content>"
WRAPPER_CLOSE: Final = "</untrusted_participant_content>"


def unwrapped(text: str) -> str:
    """``text`` without one leading opening tag and one trailing closing tag."""
    text = text.strip()
    if text.startswith(WRAPPER_OPEN):
        text = text[len(WRAPPER_OPEN) :].lstrip()
    if text.endswith(WRAPPER_CLOSE):
        text = text[: -len(WRAPPER_CLOSE)].rstrip()
    return text
