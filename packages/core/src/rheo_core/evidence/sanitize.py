"""The evidence sanitizer: what of a turn's text may be stored or shown to a model.

FR 4. ``sanitize(text) -> str | None``. Pure and deterministic: no database, no
settings, no clock, and no core import beyond
:func:`~rheo_core.redaction.masking.mask_secret_references`, so a future laptop bridge
can import and run the identical function before egress.

The steps, in this order (spec Architecture, System Components item 1):

0. **Normalise line breaks.** Every line boundary :meth:`str.splitlines` knows,
   ``\\r\\n`` and a lone ``\\r`` included, becomes ``\\n`` before anything else, so
   CRLF text segments exactly like LF text.
1. **Mask secret references**, exactly as the runtime masks a task (#149). Contact
   values are *not* masked: the text is the person's own, and #149 decided that a
   contact value they typed is theirs to have remembered.
2. **Remove tool and file payloads.** Removing payloads is a privacy floor, so where a
   rule has to guess it drops more, never less. Fenced code blocks go first, over the
   whole text, because a fence may hold blank lines; each becomes a segment break. The
   rest is split into blank-line-separated segments, and each is worked in three
   passes:

   a. *Whole segment.* A line that is only a filesystem path is removed (a Windows
      path may hold spaces: a line that starts at a drive root is a path to its end).
      What is left is removed whole when it is a unified-diff hunk (with any directly
      following segments made only of diff body lines), an XML/HTML-tag block spanning
      the whole segment, a JSON object or array, or part of a stack trace.
   b. *Line by line*, the way the path rule works, so a prose line sharing the segment
      survives. A JSON object or array that starts a line and ends one is removed with
      every line it spans. So is a tag block: from a line that opens with a tag to the
      first line ending in ``>`` once the opening tag's closer has been seen. A line
      holding nothing but an opening tag that never closes runs to the end of the
      segment, as an unterminated fence runs to the end of the text. A line indented
      by four or more columns is indented code and goes. Markdown would read such a
      line after a prose line as a lazy continuation of the paragraph, but that is
      the shape of a command pasted under "run this:", so the rule drops it; an
      indented continuation of a list item goes with it, and that is a missing
      memory, never a wrong one.
   c. *Inline payload.* What is left goes whole when it holds markup mid-sentence (a
      closing tag, a self-closing tag, an opening tag with a quoted attribute, a
      comment, declaration or processing instruction) or an inline JSON object. The
      whole segment goes, not the span: cutting ``<tool>ls</tool>`` out of "Please
      inspect <tool>ls</tool> when ready." leaves a sentence about a payload that is
      no longer there, and a span rule that misjudges one boundary leaks the payload
      body. Prose that merely uses angle brackets or braces is kept and pinned by
      negative controls: ``a < b``, ``x<y and y>z``, ``List<String>``, a bare
      placeholder such as ``<target>``, an address in ``<...>``, and ``{name}``.

   Every segment is classified again once its whitespace has collapsed, and goes
   whole when the collapsed form is a payload (a hunk header or a stack frame split
   across two lines is one only once joined). No step may expose a payload an earlier
   step passed over, so one pass removes everything a second pass would:
   ``sanitize(sanitize(x)) == sanitize(x)``.

   **Kept on purpose:** a diff body with no hunk header before it. Its line grammar
   (``+``, ``-``, a leading space) is also the grammar of a Markdown list and of a
   pros-and-cons note, and without a header the two cannot be told apart. A body that
   follows a header is removed with it. A test pins both halves.
3. **Drop the whole unit** (``None``) when any prose segment carries an injection
   marker from the closed list below. A turn carrying an attempt to steer the model
   that reads it is not trustworthy evidence as a whole, so the unit goes, not the
   segment. The markers run after payload removal, as the spec orders it: a marker
   inside a removed payload never reaches a model either way. A segment dropped in
   pass (c) or on the collapsed re-check is still prose, though, and the markers read
   it: an inline ``</system>`` drops the unit, not only its segment.
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
      | [A-Za-z]:\\.*                           # C:\windows\path, spaces and all
      | [\w.-]+(?:/[\w.@+-]+)+\.\w+             # rel/path/file.ext
      | [\w.-]+(?:/[\w.@+-]+)+/                 # rel/dir/
    )\s*$""",
    re.VERBOSE,
)
"""A line that is only a filesystem path. A URL is not one (its scheme's colon matches
no branch), and a line with words around the path is kept. A Windows path may hold
spaces (``C:\\Program Files\\...``) and nothing marks where it ends, so a line that
starts at a drive root is a path to its end: "C:\\ is nearly full" on its own line goes
too, as a missing memory rather than a leaked path."""

_TAG_LINE: Final = re.compile(
    r"^\s*(?:<[A-Za-z_][\w:.-]*(?:\s[^<>]*)?/?>|</[A-Za-z_]|<!|<\?)"
)
"""A line that opens with a tag: an opening or self-closing tag, a closing tag, or a
comment, declaration or processing instruction. ``<dana@example.com>`` is not a tag
(``@`` is no name character), so a line opening with an address is not markup."""
_OPENING_TAG: Final = re.compile(r"^\s*<([A-Za-z_][\w:.-]*)(?:\s[^<>]*?)?(/?)>")
_BARE_TAG_LINE: Final = re.compile(r"^\s*<[A-Za-z_][\w:.-]*(?:\s[^<>]*)?>\s*$")
"""A line holding nothing but one opening tag."""

_INLINE_MARKUP: Final = re.compile(
    r"""
        </[A-Za-z_][\w:.-]*\s*>                   # a closing tag
      | <[A-Za-z_][\w:.-]*(?:\s[^<>]*)?/>         # a self-closing tag
      | <[A-Za-z_][\w:.-]*\s[^<>]*=\s*["'][^<>]*> # an opening tag, quoted attribute
      | <!(?:--|\[CDATA\[|[A-Za-z])               # a comment, CDATA or declaration
      | <\?[A-Za-z]                               # a processing instruction
    """,
    re.VERBOSE,
)
"""Markup mid-sentence. A bare opening tag with no attributes is deliberately absent:
``<target>`` in "replace <target> with the goal" is a placeholder, and neither
``x<y and y>z`` nor ``List<String>`` is markup. A tag carrying a payload is paired, so
its closing tag is what gives it away."""

_INLINE_JSON_START: Final = re.compile(r'\{\s*"')
"""Where an inline JSON object can start: a brace, then a quoted key. Not ``{name}``."""

_JSON_DECODER: Final = json.JSONDecoder(strict=False)
"""``strict=False`` accepts raw line breaks inside strings, so a payload that only
becomes valid JSON once its whitespace collapses is caught first time."""

_INDENT_COLUMNS: Final = 4
"""Markdown's indented code block: four columns of leading whitespace, a tab to the
next multiple of four."""

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
        parsed = _JSON_DECODER.decode(stripped)
    except ValueError:
        return False
    return isinstance(parsed, dict | list)


def _is_stack_trace(segment: str) -> bool:
    return any(pattern.search(segment) for pattern in _STACK_FRAME_LINES)


def _is_whole_payload(segment: str) -> bool:
    return (
        _is_diff(segment)
        or _is_xml_block(segment)
        or _is_json(segment)
        or _is_stack_trace(segment)
    )


def _collapsed(segment: str) -> str:
    return _WHITESPACE.sub(" ", segment).strip()


def _without_path_lines(segment: str) -> str:
    return "\n".join(line for line in segment.split("\n") if not _PATH_LINE.match(line))


def _json_block_end(lines: list[str], start: int) -> int | None:
    """The last line of a JSON object or array that starts line ``start`` and ends a
    line, or ``None`` when there is none."""
    line = lines[start]
    offset = len(line) - len(line.lstrip())
    if line[offset : offset + 1] not in ("{", "["):
        return None
    text = "\n".join(lines[start:])
    try:
        _, end = _JSON_DECODER.raw_decode(text, offset)
    except ValueError:
        return None
    if text[end:].split("\n", 1)[0].strip():
        return None  # prose follows on the line: left to the inline rule
    return start + text.count("\n", 0, end)


def _markup_block_end(lines: list[str], start: int) -> int | None:
    """The last line of a tag block that opens line ``start``, or ``None``.

    The block ends on the first line ending in ``>`` once the opening tag's closer has
    been seen, so ``<tool_use>`` / ``ls -la`` / ``</tool_use>`` goes whole rather than
    stopping at the first line. A line that is only an opening tag and never closes
    runs to the end of the segment. A line that opens with a tag, runs on into prose
    and never closes is not a block; the inline rule judges it.
    """
    if not _TAG_LINE.match(lines[start]):
        return None
    opener = _OPENING_TAG.match(lines[start])
    closer = f"</{opener.group(1)}" if opener and not opener.group(2) else None
    closed = closer is None
    for index in range(start, len(lines)):
        closed = closed or (closer is not None and closer in lines[index])
        if closed and lines[index].rstrip().endswith(">"):
            return index
    if _BARE_TAG_LINE.match(lines[start]):
        return len(lines) - 1
    return None


def _is_indented_code(line: str) -> bool:
    body = line.lstrip(" \t")
    indent = line[: len(line) - len(body)].expandtabs(_INDENT_COLUMNS)
    return bool(body) and len(indent) >= _INDENT_COLUMNS


def _without_payload_lines(segment: str) -> str:
    """Pass (b): ``segment`` without its JSON and tag blocks and indented code lines."""
    lines = segment.split("\n")
    kept: list[str] = []
    index = 0
    while index < len(lines):
        end = _json_block_end(lines, index)
        if end is None:
            end = _markup_block_end(lines, index)
        if end is not None:
            index = end + 1
            continue
        if not _is_indented_code(lines[index]):
            kept.append(lines[index])
        index += 1
    return "\n".join(kept)


def _carries_inline_json(segment: str) -> bool:
    for start in _INLINE_JSON_START.finditer(segment):
        try:
            _JSON_DECODER.raw_decode(segment, start.start())
        except ValueError:
            continue
        return True  # a brace and a quoted key only ever decode to an object
    return False


def _carries_inline_payload(segment: str) -> bool:
    return _INLINE_MARKUP.search(segment) is not None or _carries_inline_json(segment)


def _prose_of(segment: str) -> str | None:
    """Passes (a) and (b): what of ``segment`` is left as prose, or ``None``."""
    segment = _without_path_lines(segment)
    if not segment.strip() or _is_whole_payload(segment):
        return None
    prose = _without_payload_lines(segment)
    if not prose.strip() or _is_whole_payload(prose):
        return None
    return prose


def _survives_collapse(collapsed: str) -> bool:
    """Whether a kept segment, once its whitespace collapses, passes every rule
    unchanged. A segment that fails goes whole: a second pass would see only the
    collapsed form, and must find nothing left to remove."""
    prose = _prose_of(collapsed)
    return (
        prose is not None
        and _collapsed(prose) == collapsed
        and not _carries_inline_payload(prose)
    )


def _screened_segments(text: str) -> tuple[list[str], list[str]]:
    """Step 2: the prose segments the markers read, and the segments kept.

    Path-only lines go first, and every whole-segment test runs on what is left. The
    other order let a path line shield a payload behind it (``/srv/x.toml`` above a
    JSON object): the segment failed the JSON test, lost its path line, and the object
    came out as kept text, to be caught only by a second pass the runtime never runs.
    The collapsed re-check closes the same hole for whitespace.
    """
    screened: list[str] = []
    kept: list[str] = []
    in_diff = False
    for raw in _SEGMENT_BREAK.split(_without_fenced_blocks(text)):
        segment = _without_path_lines(raw)
        if not segment.strip():
            in_diff = False
            continue
        if _is_diff(segment) or _is_diff(_collapsed(segment)):
            in_diff = True
            continue
        if in_diff and _is_diff_body(segment):
            continue
        in_diff = False
        prose = _prose_of(segment)
        if prose is None:
            continue
        screened.append(prose)
        if _carries_inline_payload(prose) or not _survives_collapse(_collapsed(prose)):
            continue
        kept.append(prose)
    return screened, kept


def _carries_injection_marker(segment: str) -> bool:
    if any(pattern.search(segment) for pattern in _ROLE_LINE_MARKERS):
        return True
    collapsed = _WHITESPACE.sub(" ", segment)
    return any(pattern.search(collapsed) for pattern in _INJECTION_MARKERS)


def sanitize(text: str) -> str | None:
    """The text as it may be stored and shown to a model, or ``None`` to drop it."""
    masked = mask_secret_references("\n".join(text.splitlines()))
    screened, kept = _screened_segments(masked)
    if any(_carries_injection_marker(segment) for segment in screened):
        return None
    result = "\n\n".join(_collapsed(segment) for segment in kept)
    return result or None
