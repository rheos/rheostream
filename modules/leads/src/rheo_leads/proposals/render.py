"""The whole-draft text form and its sections, both ways.

A draft is stored as sections (the cover letter, then one answer per screening
question), because an application form takes each separately. The whole-draft form is
what the model writes and what a person edits in one box: the letter, then a ``---``
divider line, then one ``**question**`` heading line per answer. :func:`parse_draft`
splits it, :func:`render_draft` builds it, and a well-formed draft round-trips.

A body that itself holds a bare ``---`` line or a line-leading ``**`` would read back as
structure, so :func:`render_draft` escapes both with NUL-delimited sentinels (which no
prose or JSON text carries) and :func:`parse_draft` reverses them.
"""

import re
from dataclasses import dataclass
from typing import Final

DIVIDER: Final = "\n---\n"
# The heading is ``**question**`` straight before a newline; the answer runs to the
# next line-leading heading or the end. ``[\s\S]`` spans newlines without flags.
_QA_RE: Final = re.compile(r"\*\*(.+?)\*\*\n([\s\S]*?)(?=\n\s*\*\*|$)")
_ESC_DASH: Final = "\x00ESC\x00DASH\x00"
_ESC_BOLD: Final = "\x00ESC\x00BOLD\x00"
# A tail with a divider but no headings is kept whole under this label.
UNLABELLED: Final = "Answers"
_COVER_LETTER: Final = re.compile(r"^cover letter:?$", re.IGNORECASE)


@dataclass(frozen=True)
class Answer:
    question: str
    answer: str


@dataclass(frozen=True)
class Draft:
    letter: str
    answers: tuple[Answer, ...] = ()


def _escape(text: str) -> str:
    text = re.sub(r"^---$", _ESC_DASH, text, flags=re.MULTILINE)
    return re.sub(r"^\*\*", _ESC_BOLD, text, flags=re.MULTILINE)


def _unescape(text: str) -> str:
    return text.replace(_ESC_DASH, "---").replace(_ESC_BOLD, "**")


def parse_draft(blob: str) -> Draft:
    """Split a whole draft into the letter and its answers."""
    divider = blob.find(DIVIDER)
    if divider == -1:
        return Draft(letter=_unescape(blob.strip()))
    letter = _unescape(blob[:divider].strip())
    rest = blob[divider + len(DIVIDER) :]
    answers = [
        Answer(question=m.group(1).strip(), answer=_unescape(m.group(2).strip()))
        for m in _QA_RE.finditer(rest)
    ]
    if not answers and rest.strip():
        answers.append(Answer(question=UNLABELLED, answer=_unescape(rest.strip())))
    # "Cover Letter" is a form's field label, not a question: the letter answers it.
    if answers and _COVER_LETTER.match(answers[0].question):
        folded = answers.pop(0)
        if not letter:
            letter = folded.answer
    return Draft(letter=letter, answers=tuple(answers))


def render_draft(draft: Draft) -> str:
    """Build the whole-draft form, escaping bodies, never the structure."""
    if not draft.answers:
        return _escape(draft.letter)
    blocks = "\n\n".join(
        f"**{a.question}**\n{_escape(a.answer)}" for a in draft.answers
    )
    return _escape(draft.letter) + "\n\n---\n\n" + blocks


def strip_model_chatter(text: str, *, expect_questions: bool) -> str:
    """Drop what a model says about the letter that is not the letter.

    - a leading line of at most eight words ending in ``:`` and followed by a bare
      ``---`` line goes with that divider ("Here's the proposal:"); a bare leading
      ``---`` goes on its own;
    - a trailing divider with nothing after it goes, since an empty tail is never
      answers;
    - with no screening questions asked, a trailing ``---`` section without a
      ``**question**`` heading goes ("A couple of notes on choices I made").

    With questions asked, a non-empty heading-less tail stays: it may be real answers,
    and :func:`parse_draft` keeps those visible.
    """
    t = (text or "").strip()
    lines = t.split("\n")
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is not None:
        head = lines[first].strip()
        nxt = next((i for i in range(first + 1, len(lines)) if lines[i].strip()), None)
        if (
            head.endswith(":")
            and len(head.split()) <= 8
            and nxt is not None
            and lines[nxt].strip() == "---"
        ):
            t = "\n".join(lines[nxt + 1 :]).strip()
        elif head == "---":
            t = "\n".join(lines[first + 1 :]).strip()
    t = re.sub(r"(?:\n\s*---[ \t]*)+$", "", t).strip()
    if not expect_questions:
        idx = t.rfind(DIVIDER)
        if idx != -1 and "**" not in t[idx + len(DIVIDER) :]:
            t = t[:idx].strip()
    return t
