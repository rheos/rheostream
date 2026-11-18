"""Text helpers shared by the parsers that read extracted PDF text.

A text extractor can break a word with stray spaces or run two words together, and
how it does so depends on the PDF and the extractor, not on the issuer. A parser
therefore compares a label with every space removed (``squeezed``), so one label
reads the same however the extractor spaced it.
"""

from __future__ import annotations


def squeezed(line: str) -> str:
    """``line`` with every whitespace character removed."""
    return "".join(line.split())
