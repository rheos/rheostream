"""The evidence sanitizer: what of a turn's text may be stored or shown to a model.

FR 4. ``sanitize(text) -> str | None``. Pure and deterministic: no database, no
settings, no clock, and no core import beyond
:func:`~rheo_core.redaction.masking.mask_secret_references`, so a future laptop bridge
can import and run the identical function before egress.

The steps, in this order (spec Architecture, System Components item 1):

1. **Mask secret references**, exactly as the runtime masks a task (#149). Contact
   values are *not* masked: the text is the person's own, and #149 decided that a
   contact value they typed is theirs to have remembered.
2. **Remove tool and file payloads.** Fenced code blocks go first, over the whole text,
   because a fence may hold blank lines; each becomes a segment break. The rest is
   split into blank-line-separated segments. In each, a line that is only a filesystem
   path is removed first; what is left is then removed whole when it is a unified-diff
   hunk (with any directly following segments made only of diff body lines), an
   XML/HTML-tag block spanning the whole segment, a JSON object or array, or part of a
   stack trace. No step may expose a payload an earlier step passed over, so one pass
   removes everything a second pass would: ``sanitize(sanitize(x)) == sanitize(x)``.
3. **Drop the whole unit** (``None``) when any remaining segment carries an injection
   marker from the closed list below. A turn carrying an attempt to steer the model
   that reads it is not trustworthy evidence as a whole, so the unit goes, not the
   segment. The markers run after payload removal, as the spec orders it: a marker
   inside a removed payload never reaches a model either way.
4. **Collapse whitespace** inside each kept segment, and keep segments apart with one
   blank line. An empty result is ``None``.

**The marker list is closed, and override- or reader-shaped on purpose.** The person's
own task is an instruction to the runtime agent by nature and must stay extractable, so
ordinary task text is not a marker: "use the grep tool", "call the API", "update the
system prompt file", "ignore the lint warnings", "you are now on the release branch".
One accepted false positive is named and pinned by a test: "you are now in charge of
the release" matches the persona-reset shape and drops the unit. A dropped unit is a
missing memory, never a wrong one, so the list errs that way only where the shape is
also a classic injection opener. The patterns are the spec's, verbatim but for one
grouping fix in the ``disregard`` pattern (commented where it sits); the unanchored
ones are matched against the segment with its whitespace collapsed, so a line break
inside a phrase does not hide it. The list is a floor, not the guarantee: the
guarantees are structural (spec Technical Risks R2).
"""

import json
import re
from typing import Final

from rheo_core.redaction.masking import mask_secret_references

# --- step 3: the closed injection-marker list (spec component 1, verbatim) ------------

_INJECTION_MARKERS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # instruction override
        r"ignore (all |any |the )?(previous|prior|above|earlier) instructions",
        # The spec writes this group as ``(previous|prior|above|earlier )?``, where the
        # trailing space binds to ``earlier`` alone, so "disregard previous
        # instructions" would need "previousinstructions" to match. The optional word
        # and its space are grouped together here; that only ever drops more.
        r"disregard (all |any |the )?((previous|prior|above|earlier) )?instructions",
        r"forget (all |your )?(previous|prior) instructions",
        # prompt exfiltration or replacement
        r"(reveal|print|repeat|show) (your|the) system prompt",
        r"new system prompt:",
        # persona reset
        r"you are now (a|an|in) ",
        r"(enable|enter|activate) developer mode",
        # role and chat-template markup
        re.escape("<system>"),
        re.escape("</system>"),
        re.escape("<|im_start|>"),
        re.escape("[INST]"),
        # a tool instruction addressed to the reader of the text
        r"when you (read|process|summari[sz]e|extract)[^.]{0,80}"
        r"(call|invoke|run|use) ",
        r"(extractor|summari[sz]er|assistant reading this)[^.]{0,40}"
        r"(call|invoke|run|use) ",
    )
)
"""Matched against a segment with its whitespace collapsed to single spaces."""

_ROLE_LINE_MARKERS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^[ \t]*system:", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[ \t]*assistant:", re.IGNORECASE | re.MULTILINE),
)
""""A line starting ``system:`` or ``assistant:``": matched against the raw segment,
line by line, because collapsing whitespace would erase the line starts. Indentation
before the role word still counts as the line's start; otherwise an indented role line
would pass once and then, its indentation collapsed away, drop on a second pass."""

# --- step 2: the closed payload-removal rules -----------------------------------------

_FENCE: Final = re.compile(r"^[ \t]*(`{3,}|~{3,})")
"""A fence line opens or closes a fenced code block (Markdown: three or more backticks
or tildes). A block closes on a fence of the same character at least as long, with
nothing but whitespace after it on the line."""

_SEGMENT_BREAK: Final = re.compile(r"\n[ \t]*\n")

_DIFF_HEADER_LINES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^[ \t]*@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", re.MULTILINE),
    re.compile(r"^[ \t]*diff --git ", re.MULTILINE),
    re.compile(r"^[ \t]*--- \S.*\n[ \t]*\+\+\+ \S", re.MULTILINE),
)
"""Indentation allowed: the last step strips it, so a header matched only at column 0
would pass once indented and be caught on a second pass instead of the first."""
_DIFF_BODY_LINE: Final = re.compile(r"^(?:[+\- ]|\\ No newline)")
"""A line a hunk's body is made of: added, removed, context, or the no-newline note."""

_STACK_FRAME_LINES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^\s*Traceback \(most recent call last\):", re.MULTILINE),
    re.compile(r'^\s*File "[^"\n]+", line \d+', re.MULTILINE),
    re.compile(r"^\s*at [^\s(]+ ?\([^()\n]*:\d+(?::\d+)?\)\s*$", re.MULTILINE),
    re.compile(r"^\s*at \S+:\d+:\d+\s*$", re.MULTILINE),
)
"""Python traceback headers and frames, and JavaScript/JVM ``at ...(file:line)``
frames. A segment carrying any of them is part of a trace and goes whole."""

_XML_BLOCK: Final = re.compile(r"<[A-Za-z_!?/][^>]*>.*", re.DOTALL)
"""Opens with a tag (or a comment, declaration or closing tag); the segment must also
end with ``>``, so ``<tool_use>...</tool_use>``-style markup is spanned end to end."""

_PATH_LINE: Final = re.compile(
    r"""^\s*(?:
        (?:~|\.{1,2})?(?:/[^\s/]+)+/?           # /abs, ~/home, ./rel, ../up
      | [A-Za-z]:\\(?:[^\s\\]+\\?)*             # C:\windows\path
      | [\w.-]+(?:/[\w.@+-]+)+\.\w+             # rel/path/file.ext
      | [\w.-]+(?:/[\w.@+-]+)+/                 # rel/dir/
    )\s*$""",
    re.VERBOSE,
)
"""A line that is only a filesystem path. A URL is not one (its scheme's colon matches
no branch), and a line with words around the path is kept."""

_WHITESPACE: Final = re.compile(r"\s+")


def _without_fenced_blocks(text: str) -> str:
    """``text`` with every fenced code block replaced by a segment break.

    An unterminated fence runs to the end of the text: it is still a payload, and
    removing too much costs a memory, never a wrong one.
    """
    kept: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        opener = _FENCE.match(line)
        if fence is None:
            if opener is not None:
                fence = opener.group(1)
                kept.append("")
                continue
            kept.append(line)
        elif opener is not None and opener.group(1)[0] == fence[0]:
            # A closing fence carries nothing after it: a fence line with an info
            # string (a nested opener such as ```python) stays inside the block.
            closes = not line[opener.end() :].strip()
            if closes and len(opener.group(1)) >= len(fence):
                fence = None
                kept.append("")
    return "\n".join(kept)


def _is_diff(segment: str) -> bool:
    return any(pattern.search(segment) for pattern in _DIFF_HEADER_LINES)


def _is_diff_body(segment: str) -> bool:
    lines = [line for line in segment.split("\n") if line.strip()]
    return bool(lines) and all(_DIFF_BODY_LINE.match(line) for line in lines)


def _is_xml_block(segment: str) -> bool:
    stripped = segment.strip()
    return stripped.endswith(">") and _XML_BLOCK.fullmatch(stripped) is not None


def _is_json(segment: str) -> bool:
    stripped = segment.strip()
    if not stripped or stripped[0] not in "[{":
        return False
    try:
        # ``strict=False`` accepts raw line breaks inside strings, so a payload that
        # only becomes valid JSON once its whitespace collapses is caught first time.
        parsed = json.loads(stripped, strict=False)
    except ValueError:
        return False
    return isinstance(parsed, dict | list)


def _is_stack_trace(segment: str) -> bool:
    return any(pattern.search(segment) for pattern in _STACK_FRAME_LINES)


def _kept_segments(text: str) -> list[str]:
    """Step 2: the segments left once every tool and file payload is removed.

    Path-only lines go first, and every whole-segment test runs on what is left. The
    other order let a path line shield a payload behind it (``/srv/x.toml`` above a
    JSON object): the segment failed the JSON test, lost its path line, and the object
    came out as kept text, to be caught only by a second pass the runtime never runs.
    """
    kept: list[str] = []
    in_diff = False
    for raw in _SEGMENT_BREAK.split(_without_fenced_blocks(text)):
        lines = [line for line in raw.split("\n") if not _PATH_LINE.match(line)]
        segment = "\n".join(lines)
        if not segment.strip():
            in_diff = False
            continue
        if _is_diff(segment):
            in_diff = True
            continue
        if in_diff and _is_diff_body(segment):
            continue
        in_diff = False
        if _is_xml_block(segment) or _is_json(segment) or _is_stack_trace(segment):
            continue
        kept.append(segment)
    return kept


def _carries_injection_marker(segment: str) -> bool:
    if any(pattern.search(segment) for pattern in _ROLE_LINE_MARKERS):
        return True
    collapsed = _WHITESPACE.sub(" ", segment)
    return any(pattern.search(collapsed) for pattern in _INJECTION_MARKERS)


def sanitize(text: str) -> str | None:
    """The text as it may be stored and shown to a model, or ``None`` to drop it."""
    masked = mask_secret_references(text)
    segments = _kept_segments(masked)
    if any(_carries_injection_marker(segment) for segment in segments):
        return None
    collapsed = [_WHITESPACE.sub(" ", segment).strip() for segment in segments]
    result = "\n\n".join(segment for segment in collapsed if segment)
    return result or None
