"""The evidence sanitizer: what of a turn's text may be stored or shown to a model.

FR 4. ``sanitize(text) -> str | None``. Pure and deterministic: no database, no
settings, no clock, and no core import beyond
:func:`~rheo_core.redaction.masking.mask_secret_references`, so a future laptop bridge
can import and run the identical function before egress.

**Input over** :data:`MAX_INPUT_CHARS` **is dropped whole** (``None``), before any
step runs. It fails closed on purpose: a human turn that long is a paste, not a
memory, and the Rheo-owned producer records the task text on its own. The cap also
bounds the work: ``sanitize`` runs inside the recording transaction, before the byte
budget, so its cost must be bounded by the input, not by what an adversary puts in
it. Under the cap every scan is linear or capped: the JSON rules make at most
:data:`_MAX_JSON_ATTEMPTS` failed parses per segment and treat a segment that needs
more as a payload, and JSON nested deeper than the parser can recurse is a payload
too.

The steps, in this order (spec Architecture, System Components item 1):

0. **Normalise line breaks and spaces.** Every line boundary :meth:`str.splitlines`
   knows, ``\\r\\n`` and a lone ``\\r`` included, becomes ``\\n`` before anything else,
   so CRLF text segments exactly like LF text. Then every other whitespace character
   (no-break space, the U+2000 to U+200A spaces, the ideographic space U+3000 and the
   rest of :data:`_ODD_SPACES`, plus the U+FEFF byte-order mark) becomes a plain
   space. The line-start rules (role lines, fences, indented code, blank-line breaks)
   read spaces and tabs only, while the final collapse reads every ``\\s``; left
   unmapped, a no-break space before ``system:`` would pass the role marker once and
   trip it on a second pass. A tab stays a tab: the indented-code rule counts it to
   the next multiple of four columns, and the fence and role rules already accept it.
1. **Mask secret references**, exactly as the runtime masks a task (#149). Contact
   values are *not* masked: the text is the person's own, and #149 decided that a
   contact value they typed is theirs to have remembered.
2. **Remove tool and file payloads.** Removing payloads is a privacy floor, so where a
   rule has to guess it drops more, never less. Fenced code blocks go first, over the
   whole text, because a fence may hold blank lines; each becomes a segment break. The
   rest is split into blank-line-separated segments, and each is worked in three
   passes:

   a. *Whole segment.* A line that is only a filesystem path is removed, after a
      leading list bullet and one layer of matching quotes or backticks come off
      (``"C:\\Users\\...\\tax 2025.pdf"`` from "Copy as path"). A path may
      hold spaces and nothing marks where it ends, so a line that starts at a root is
      a path to its end: a Windows drive root (``C:\\`` or ``C:/``), a UNC
      ``\\\\server\\``, ``~/``, ``./``, ``../``, or a POSIX root holding a
      directory (``/Users/example/My Documents/...``). What is left is removed whole
      when it is a unified-diff hunk (with any directly following segments made only
      of diff body lines), an XML/HTML-tag block spanning the whole segment, a JSON
      object or array, part of a stack trace, a PEM header (``-----BEGIN ...
      KEY-----``), or a shell or REPL transcript (a line opening with ``$ cmd``,
      ``user@host:~$ cmd``, zsh's ``user@host dir % cmd``, ``>>>`` and the like; the
      lines after a prompt are its output). A stack frame counts mid-sentence when
      its shape is distinctive: ``Caused by: java.io.IOException: ...``,
      ``PermissionError: [Errno 13] ...`` or ``File "/app/x.py", line 3`` inside a
      sentence takes the segment with it.
   b. *Line by line*, the way the path rule works, so a prose line sharing the segment
      survives. A JSON object or array that starts a line and ends one is removed with
      every line it spans. So is a tag block: from a line that opens with a tag to the
      first line ending in ``>`` once the opening tag's closer has been seen. A line
      holding nothing but an opening tag that never closes runs to the end of the
      segment, as an unterminated fence runs to the end of the text. A line indented
      by four or more columns is indented code and goes. Markdown would read such a
      line after a prose line as a lazy continuation of the paragraph, but that is
      the shape of a command pasted under "run this:", so the rule drops it. An
      indented continuation of a list item goes with it, and so does a tab-indented
      list: "Groceries" / tab "milk" / tab "eggs" keeps only "Groceries". That is a
      missing memory, never a wrong one, and an accepted cost. What is left meets
      (a)'s whole-segment tests again: removing a line can make two others adjacent,
      such as a ``---`` and ``+++`` header pair.
   c. *Inline payload.* What is left goes whole when it holds markup mid-sentence (a
      closing tag, a self-closing tag, an opening tag with a quoted attribute, a
      comment, declaration or processing instruction), an inline JSON object, a
      Python, JSON or JavaScript literal (a keyed dict in either quote, bare-key
      objects, or a list or set with a quoted element, ``["rm","-rf","/"]``), an
      encoded blob (40 or more base64 characters with a digit and both cases; see
      :data:`_BASE64_RUN` for the length), a compact JWT, or base64 wrapped across
      lines. The whole segment goes, not the span:
      cutting ``<tool>ls</tool>`` out of "Please inspect <tool>ls</tool> when
      ready." leaves a sentence about a payload that is no longer there, and a span
      rule that misjudges one boundary leaks the payload body. Prose that merely
      uses angle brackets or braces is kept and pinned by negative controls:
      ``a < b``, ``x<y and y>z``, ``List<String>``, a bare placeholder such as
      ``<target>``, an address in ``<...>``, and ``{name}``.
      Rich-text markup is markup all the same, an accepted cost: "I <em>really</em>
      liked it" carries a closing tag, and a turn that is only that sentence is
      dropped. So is a quoted list in prose: "I said ["yes", "no"] earlier" has the
      shape of ``["rm","-rf","/"]``, nothing tells them apart, and the privacy floor
      drops it. Kept and pinned: ``$5``, "costs $20 a month", "see ~/notes",
      ``v1.2.3``, "Python 3.12", a short git SHA, an ordinary URL (see the accepted
      costs for one that carries a long id), a citation or list of numbers
      (``[1]``, ``[2, 3]``), a task box ``[ ]``, a Markdown link, "the C: drive" and a
      ``>>>`` mentioned mid-sentence.

   Every segment is classified again once its whitespace has collapsed, and goes
   whole when the collapsed form is a payload (a hunk header or a stack frame split
   across two lines is one only once joined). No step may expose a payload an earlier
   step passed over, so one pass removes everything a second pass would:
   ``sanitize(sanitize(x)) == sanitize(x)``.

   A recognized shell prompt also taints the next non-empty segment, because terminal
   output can continue after a blank line. This is bounded to one segment: later
   paragraphs are still judged normally. A ``$`` prompt requires a known command or
   an explicit executable path, so prose such as ``$ sign is weird`` stays prose.

   **Kept on purpose:** a diff body with no hunk header before it. Its line grammar
   (``+``, ``-``, a leading space) is also the grammar of a Markdown list and of a
   pros-and-cons note, and without a header the two cannot be told apart. A body that
   follows a header is removed with it. A test pins both halves. Also kept, each
   pinned (#225):

   - a path inside a sentence ("See src/app/main.py for the entry point"), as
     before; only a line that *starts* at a root goes;
   - a bare ``/name`` followed by words ("/review the PR"): the shape of a slash
     command, and it holds no directory;
   - an ``at`` frame whose location has no file extension or holds a space, and the
     bare ``at file:N:N`` form, when mid-sentence ("meet at noon (room 4:30)", "at
     10:30:00"); a line that is nothing but such a frame still goes;
   - a qualified exception name with no message colon after it ("we saw a
     java.lang.NullPointerException today");
   - a lowercase-only hex run such as a full 40-character SHA-1, and a run with a
     separator more often than one character in ten (a mixed-case URL path, a slug,
     a ``snake_case`` name).

   **Accepted costs (#225):** a line opening with a multi-directory path loses the
   words after it ("/srv/app is where it lives" on its own line), the twin of "C:\\ is
   nearly full"; a triple-nested e-mail quote (``>>> I agree``) reads as a REPL line;
   ``$ 5`` with a space at the start of a line reads as a prompt, and so does a line
   opening with an e-mail address, a word and then ``%`` or ``#`` ("dana@example.com
   said % is odd"), which has the zsh prompt's shape. A line opening at a
   one-level ``~/x``, ``./x`` or ``../`` is a path to its end too ("~/notes is where I
   keep stuff", "./configure then make", "../ is the parent"), because ``~/My
   Documents/x`` holds its space in the first level. A prose mention of "Traceback
   (most recent call last):" takes its segment, since the header is matched
   mid-sentence. A mixed-case identifier of 40 or more characters with a digit and
   few separators reads as a blob. So does the id in a URL: a Google Docs, Drive or
   Sheets link holds a 44-character id, and its segment goes. That one is arguably a
   gain rather than a cost, because a link-shared document's id works like a
   password, so there is no URL exemption. Each is a missing memory, never a leaked
   payload.
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
from bisect import bisect_left
from typing import Final

from rheo_core.redaction.masking import mask_secret_references

# --- the input cap and step 0 ---------------------------------------------------------

MAX_INPUT_CHARS: Final = 65_536
"""Longer input is dropped whole, before any step runs (see the module docstring)."""

_ODD_SPACES: Final = (
    "\x1f\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009"
    "\u200a\u202f\u205f\u3000\ufeff"
)
"""Every character :meth:`str.isspace` accepts that :meth:`str.splitlines` does not
already turn into a line break, less the plain space and the tab, plus U+FEFF, which
is no ``\\s`` but is invisible in front of a role word all the same. A test derives the
set from the running Python and compares."""
_TO_PLAIN_SPACE: Final = str.maketrans(dict.fromkeys(_ODD_SPACES, " "))

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
    re.compile(r"Traceback \(most recent call last\):"),
    re.compile(r'\bFile "[^"\n]+", line \d+'),
    re.compile(r"\bError at /[^:\s]+\.[A-Za-z0-9]+:\d+:\d+\b"),
    re.compile(r"\b[\w./-]+\.go:\d+\s+\+0x[0-9a-fA-F]+\b"),
    re.compile(r"\bpanicked at [^:\s]+(?:/[^:\s]+)*\.rs:\d+:\d+\b"),
    re.compile(r"^\s*at [^\s(]+ ?\([^()\n]*:\d+(?::\d+)?\)\s*$", re.MULTILINE),
    re.compile(r"\bat [^\s(]+ ?\([^()\s]*\.\w+:\d+(?::\d+)?\)"),
    re.compile(r"^\s*at \S+:\d+:\d+\s*$", re.MULTILINE),
    re.compile(
        r"\bCaused by: (?:[\w$]+(?:\.[\w$]+)+|[A-Z][\w$]*(?:Exception|Error)\b)"
    ),
    re.compile(r"(?<![\w$.])(?:[a-z_][\w$]*+\.)++[A-Z][\w$]*(?:Exception|Error):"),
    re.compile(
        r"""(?<![\w$.])(?:[A-Z][A-Za-z0-9]*)?(?:Exception|Error):[ \t]*
        (?:\[Errno[ ]\d+\]                          # PermissionError: [Errno 13]
          | E[A-Z]{2,}:                              # Error: ENOENT:
          | ['"]?(?:[~.]{0,2}/|[A-Za-z]:[\\/]|\\\\)  # a path, quoted or not
        )""",
        re.VERBOSE,
    ),
)
"""Python traceback headers and frames, JavaScript/JVM ``at ...(file:line)`` frames,
Go ``file.go:line +0x...`` frames, Rust panics, ``Error at /file.ext:line:column``
locations, and exception lines. A segment carrying any of them is part of a trace and
goes whole. Since #225 the distinctive
shapes match mid-sentence too: a traceback header, a Python ``File "...", line N``
frame, an ``at name(file.ext:N)`` frame whose location names a file, a ``Caused by:``
chain naming a dotted class, and a qualified exception class followed by its message
colon (``java.io.IOException:``), and a bare exception name (``PermissionError:``,
``Error:``) when what follows its colon is an ``[Errno N]``, a Node code such as
``ENOENT:`` or a path, quoted or not. A bare name with anything else after it is prose
("it raised a ValueError, oddly", "Error: something broke"). Two stay whole-line only,
because mid-sentence they are also ordinary prose: an ``at`` frame whose location has
no file extension or holds a space ("meet at noon (room 4:30)"), and the bare
``at file:N:N`` form ("at 10:30:00"). The qualified-class pattern starts only at the
head of a dotted run (the lookbehind) and its inner quantifiers are possessive, so a
64 KB ``a.a.a...`` run is one linear scan, not one per position."""

_PROMPT_LEAD: Final = r"^[ \t]*(?:(?:[-*+]|\d+[.)])[ \t]+)?`?"
"""Where a prompt may start: the line start, after an optional list bullet and an
optional opening backtick (``- $ make``, ```$ make```)."""

_TRANSCRIPT_LINES: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(_PROMPT_LEAD + prompt, re.MULTILINE)
    for prompt in (
        r"\$ (?:(?:cat|cd|chmod|chown|cp|curl|docker|echo|env|find|git|go|grep|head|"
        r"ls|make|mkdir|mv|node|npm|npx|perl|pnpm|python(?:3)?|pytest|pwd|rm|ruby|"
        r"rustc|cargo|sed|sh|sort|ssh|sudo|tail|tar|touch|uv|wget|which|xargs)\b"
        r"|\d|(?:/|\.\.?/|~/)\S+)",  # $ cmd or explicit executable path
        r">>>(?: |$)",  # the Python REPL
        r"[\w.-]+@[\w.-]+:[^\s$#]*[$#] \S",  # bash: user@host:~$ cmd
        r"[\w.-]+@[\w.-]+ [^\s%#]+ [%#] \S",  # zsh: user@host dir % cmd
        r"\[[\w.-]+@[\w.-]+ [^\]\n]*\][$#] \S",  # [user@host dir]$ cmd
        r"PS [A-Za-z]:\\[^>\n]*> \S",  # PowerShell: PS C:\x> cmd
        r"In \[\d+\]: \S",  # IPython
    )
)
"""A line that opens with a shell prompt (a known ``$ cmd``, ``user@host:~$ cmd``) or
the Python REPL's ``>>>``. The segment goes whole, like a trace, and the next segment
goes too because output can continue after a blank line. ``$5`` and "costs $20" are no
prompt (no space after a line-start ``$``), while ``$ 5`` remains an accepted cost;
``$ sign is weird`` is prose. A ``>>>`` in the middle of a sentence is not at a line
start. The zsh default (``robin@mac app % make``), the bracketed
``[user@host dir]$``, PowerShell's ``PS C:\\x>`` and IPython's ``In [1]:`` count too;
a ``%`` is a prompt only after ``user@host dir``, so "50 % of users", "a 5 % fee" and
``100%`` are prose. Every pattern is anchored at a line start and each variable run
stops at a character the next token needs, so each line costs one scan. A triple-nested
e-mail quote (``>>> I agree``) reads as a REPL line and goes, an accepted cost."""

_XML_BLOCK: Final = re.compile(r"<[A-Za-z_!?/][^>]*>.*", re.DOTALL)
"""Opens with a tag (or a comment, declaration or closing tag); the segment must also
end with ``>``, so ``<tool_use>...</tool_use>``-style markup is spanned end to end."""

_PEM_HEADER: Final = re.compile(r"-----BEGIN (?:[A-Z ]*KEY|CERTIFICATE)-----")
"""A PEM armour header (``-----BEGIN RSA PRIVATE KEY-----``, ``-----BEGIN
CERTIFICATE-----``). Its segment goes whole even when the body lines are too short
for the blob rule. The scan starts only at the literal ``-----BEGIN``."""

_PATH_LINE: Final = re.compile(
    r"""^\s*(?:
        (?:~|\.{1,2})?(?:/[^\s/]+)+/?\s*$       # /abs, ~/home, ./rel, ../up
      | [\w.-]+(?:/[\w.@+-]+)+\.\w+\s*$         # rel/path/file.ext
      | [\w.-]+(?:/[\w.@+-]+)+/\s*$             # rel/dir/
        # From here on, a path to the end of the line, spaces and all (#225):
      | [A-Za-z]:[\\/]                          # C:\windows\path, C:/forward/slash
      | \\\\[^\\\s]+\\                          # \\server\share UNC
      | (?:~|\.{1,2})/                          # ~/My Documents/x, ./a b, ../a b
      | /{1,2}[^\s/]+/                          # /Users/x/My Documents, //srv/share
    )""",
    re.VERBOSE,
)
"""A line that is only a filesystem path. A URL is not one (its scheme's colon matches
no branch), and a line with words around the path is kept. A path may hold spaces
(``C:\\Program Files\\...``, ``/Users/example/My Documents/...``) and nothing marks
where it ends, so a line that starts at a root is a path to its end: a drive root
(either slash), a UNC ``\\\\server\\``, the home or working directory (``~/``,
``./``, ``../``), or a POSIX root holding at least one directory (``/name/``). So
"C:\\ is nearly full" and "/srv/app is where it lives" on their own lines go too, as a
missing memory rather than a leaked path. A bare ``/name`` with words after it is
kept: it is the shape of a slash command (``/review the PR``), and it cannot hold a
directory's space-separated name without a second slash."""

_LIST_BULLET: Final = re.compile(r"^(?:[-*+]|\d+[.)])[ \t]+")
"""A Markdown list bullet, taken off a line before the path test (#225 review)."""

_TAG_LINE: Final = re.compile(
    r"^\s*(?:<[A-Za-z_][\w:.-]*(?:\s[^<>]*)?/?>|</[A-Za-z_]|<!|<\?)"
)
"""A line that opens with a tag: an opening or self-closing tag, a closing tag, or a
comment, declaration or processing instruction. ``<dana@example.com>`` is not a tag
(``@`` is no name character), so a line opening with an address is not markup."""
_OPENING_TAG: Final = re.compile(r"^\s*<([A-Za-z_][\w:.-]*)(?:\s[^<>]*?)?(/?)>")
_CLOSING_TAG_NAME: Final = re.compile(r"</([A-Za-z_][\w:.-]*)")
_BARE_TAG_LINE: Final = re.compile(r"^\s*<[A-Za-z_][\w:.-]*(?:\s[^<>]*)?>\s*$")
"""A line holding nothing but one opening tag."""

_INLINE_MARKUP: Final = re.compile(
    r"""
        </[A-Za-z_][\w:.-]*\s*>                   # a closing tag
      | <[A-Za-z_][\w:.-]*(?:\s[^<>]*)?/>         # a self-closing tag
      | <[A-Za-z_][\w:.-]*\s                     # an opening tag, quoted attribute:
        (?=[^<>]*=\s*["'])[^<>]*+>               #   atomic, so linear on no ``>``
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

_INLINE_LITERALS: Final = re.compile(
    r"""
        \{\s*(?:
            '(?:\\[^\n]|[^'\\\n])*+' | "(?:\\[^\n]|[^"\\\n])*+"
          | “[^”\n]*” | ‘[^’\n]*’
          | [A-Za-z_$][A-Za-z0-9_$]*
        )\s*:                                      # quoted or JavaScript bare key
      | [\[{]\s*(?:(?:-?\d[\d.eE+-]*+|true|false|null)\s*+,\s*+)*+
        (?:'(?:\\[^\n]|[^'\\\n])*+' | "(?:\\[^\n]|[^"\\\n])*+"
          | “[^”\n]*” | ‘[^’\n]*’) \s*[,\]}]      # string element after literals
    """,
    re.VERBOSE,
)
"""A Python, JSON or JavaScript literal mid-sentence (#225, #232): a brace, quoted or
bare key and a colon (``{'tool': 'bash'}``, ``{tool: 'bash'}``), or a bracket or brace
whose elements run to a quoted string (``["rm","-rf","/"]``, ``[true, "x"]``,
``{'a', 'b'}``). Escaped quotes and smart quotes are included. Each quoted span stops
at the next unescaped quote of its kind, within its line. The run of leading numbers is
possessive as hardening, not for correctness: giving an element back lands on a digit,
where no quote can start, so it never changes a match, and a failed ``[1,1,1,...`` at
the cap no longer backtracks through every element (measured about 15 ms without it
and 2 ms with it; linear either way).

A quoted list in prose has the same shape, and the privacy floor decides it: "I said
["yes", "no"] earlier" goes whole, as a missing memory, because ``["rm","-rf","/"]``
is what the rule is for and nothing tells the two apart. What is kept, pinned by
negative controls: a citation or list of numbers (``[1]``, ``[1, 2]``), a Markdown
task box ``[ ]``, a link ``[text](url)``, ``{name}`` and ``{}``: none holds a quoted
element."""

_BASE64_RUN: Final = re.compile(r"(?<![\w+/=-])[\w+/=-]{40,}", re.ASCII)
_BASE64_LINE: Final = re.compile(r"[A-Za-z0-9+/=_-]+", re.ASCII)
_JWT_RUN: Final = re.compile(
    r"(?<![\w-])eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*(?![\w-])",
    re.ASCII,
)
"""A candidate encoded blob: 40 or more characters of the base64 alphabet, the
URL-safe ``-`` and ``_`` included. ASCII only: unspaced CJK prose with a Latin word
and a number in it must not read as one run. :func:`_is_blob` then asks for a digit,
an upper- and a lowercase letter, and at most one separator (``/``, ``+``, ``-``,
``_``) in every :data:`_BASE64_CHARS_PER_SEPARATOR` characters. The lookbehind lets
a match start only at a run's head, so the scan is linear.

Why 40: it is 30 encoded bytes, past anything prose makes. A short git SHA (7 to 12
hex), a UUID (36 with hyphens), ``v1.2.3``, a camelCase name are all shorter or lack
a character class, and a full 40-hex SHA-1 is lowercase only, so each is kept. The
common credential shapes are at or over it: an AWS secret key is 40, a GitHub token
40, a JWT segment or a PEM line longer.

Why the separator limit: random base64 has one of ``+/`` (or ``-_``) in 32
characters, so a 40-character blob meets the limit about 99 times in 100. A
mixed-case URL path (``/Novadiem-Studio/bureau/blob/main/Docs2``), a hyphenated slug
and a ``snake_case`` name have a separator every few characters, and are kept."""
_BASE64_CHARS_PER_SEPARATOR: Final = 10

_JSON_DECODER: Final = json.JSONDecoder(strict=False)
"""``strict=False`` accepts raw line breaks inside strings, so a payload that only
becomes valid JSON once its whitespace collapses is caught first time."""

_MAX_JSON_ATTEMPTS: Final = 64
"""Parses per segment, per rule, that may fail or fail to end a line. A failed parse
costs time in proportion to its offset (the error locates itself), so an unbounded
count is quadratic: ``'{"' * 32768`` is 32,768 failed parses. A segment that needs
more attempts than this is treated as a payload."""

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
    except RecursionError:
        return True  # nested past the parser's depth: a payload, never prose
    except ValueError:
        return False
    return isinstance(parsed, dict | list)


def _is_stack_trace(segment: str) -> bool:
    return any(pattern.search(segment) for pattern in _STACK_FRAME_LINES)


def _is_transcript(segment: str) -> bool:
    return any(pattern.search(segment) for pattern in _TRANSCRIPT_LINES)


def _is_pem(segment: str) -> bool:
    return _PEM_HEADER.search(segment) is not None


def _is_whole_payload(segment: str) -> bool:
    return (
        _is_diff(segment)
        or _is_xml_block(segment)
        or _is_json(segment)
        or _is_stack_trace(segment)
        or _is_transcript(segment)
        or _is_pem(segment)
    )


def _collapsed(segment: str) -> str:
    return _WHITESPACE.sub(" ", segment).strip()


def _path_candidate(line: str) -> str:
    """``line`` without a leading list bullet and one layer of matching quotes or
    backticks, the way a copied path arrives: Windows "Copy as path" wraps it in
    double quotes, and Markdown wraps it in backticks or lists it."""
    body = _LIST_BULLET.sub("", line.strip(), count=1)
    if len(body) >= 2 and body[0] in "\"'`" and body[-1] == body[0]:
        body = body[1:-1]
    return body


def _is_path_line(line: str) -> bool:
    return _PATH_LINE.match(_path_candidate(line)) is not None


def _without_path_lines(segment: str) -> str:
    return "\n".join(line for line in segment.split("\n") if not _is_path_line(line))


class _UnboundedPayload(Exception):
    """A segment the JSON rules cannot finish judging within their bounds: nested past
    the parser's recursion depth, or past :data:`_MAX_JSON_ATTEMPTS`. It is dropped as
    a payload."""


class _JsonAttempts:
    """The parse budget one JSON rule spends on one segment."""

    def __init__(self) -> None:
        self._left = _MAX_JSON_ATTEMPTS

    def decode(self, text: str, index: int) -> int | None:
        """Where the JSON value at ``index`` ends, or ``None`` when there is none."""
        try:
            _, end = _JSON_DECODER.raw_decode(text, index)
        except RecursionError as error:
            raise _UnboundedPayload from error
        except ValueError:
            self.spend()
            return None
        return end

    def spend(self) -> None:
        self._left -= 1
        if self._left < 0:
            raise _UnboundedPayload


class _SegmentLines:
    """One segment's lines, with what the block rules look up precomputed, so each
    line costs time in proportion to itself rather than to the rest of the segment."""

    def __init__(self, segment: str) -> None:
        self.text = segment
        self.lines = segment.split("\n")
        self.starts: list[int] = []
        offset = 0
        for line in self.lines:
            self.starts.append(offset)
            offset += len(line) + 1
        # next_gt[i]: the first line at or after i ending in ``>``, or -1.
        self.next_gt = [-1] * (len(self.lines) + 1)
        for index in range(len(self.lines) - 1, -1, -1):
            ends = self.lines[index].rstrip().endswith(">")
            self.next_gt[index] = index if ends else self.next_gt[index + 1]
        # closers[name]: the lines holding ``</name``, ascending.
        self.closers: dict[str, list[int]] = {}
        for index, line in enumerate(self.lines):
            for match in _CLOSING_TAG_NAME.finditer(line):
                self.closers.setdefault(match.group(1), []).append(index)
        self.json = _JsonAttempts()


def _json_block_end(seg: _SegmentLines, start: int) -> int | None:
    """The last line of a JSON object or array that starts line ``start`` and ends a
    line, or ``None`` when there is none."""
    line = seg.lines[start]
    offset = len(line) - len(line.lstrip())
    if line[offset : offset + 1] not in ("{", "["):
        return None
    end = seg.json.decode(seg.text, seg.starts[start] + offset)
    if end is None:
        return None
    line_end = seg.text.find("\n", end)
    if seg.text[end : len(seg.text) if line_end < 0 else line_end].strip():
        seg.json.spend()
        return None  # prose follows on the line: left to the inline rule
    return bisect_left(seg.starts, end + 1) - 1


def _markup_block_end(seg: _SegmentLines, start: int) -> int | None:
    """The last line of a tag block that opens line ``start``, or ``None``.

    The block ends on the first line ending in ``>`` once the opening tag's closer
    (``</`` and the same name) has been seen, so ``<tool_use>`` / ``ls -la`` /
    ``</tool_use>`` goes whole rather than stopping at the first line. A line that is
    only an opening tag and never closes runs to the end of the segment. A line that
    opens with a tag, runs on into prose and never closes is not a block; the inline
    rule judges it.
    """
    line = seg.lines[start]
    if not _TAG_LINE.match(line):
        return None
    opener = _OPENING_TAG.match(line)
    from_line: int | None = start
    if opener and not opener.group(2):
        closing = seg.closers.get(opener.group(1), [])
        found = bisect_left(closing, start)
        from_line = closing[found] if found < len(closing) else None
    if from_line is not None and seg.next_gt[from_line] >= 0:
        return seg.next_gt[from_line]
    if _BARE_TAG_LINE.match(line):
        return len(seg.lines) - 1
    return None


def _is_indented_code(line: str) -> bool:
    body = line.lstrip(" \t")
    indent = line[: len(line) - len(body)].expandtabs(_INDENT_COLUMNS)
    return bool(body) and len(indent) >= _INDENT_COLUMNS


def _without_payload_lines(segment: str) -> str:
    """Pass (b): ``segment`` without its JSON and tag blocks and indented code lines.

    Raises :class:`_UnboundedPayload` when the JSON rule runs out of bounds.
    """
    seg = _SegmentLines(segment)
    kept: list[str] = []
    index = 0
    while index < len(seg.lines):
        end = _json_block_end(seg, index)
        if end is None:
            end = _markup_block_end(seg, index)
        if end is not None:
            index = end + 1
            continue
        if not _is_indented_code(seg.lines[index]):
            kept.append(seg.lines[index])
        index += 1
    return "\n".join(kept)


def _carries_inline_json(segment: str) -> bool:
    attempts = _JsonAttempts()
    try:
        for start in _INLINE_JSON_START.finditer(segment):
            if attempts.decode(segment, start.start()) is not None:
                return True  # a brace and a quoted key only ever decode to an object
    except _UnboundedPayload:
        return True
    return False


def _is_blob(run: str) -> bool:
    return (
        any(char.isascii() and char.isdigit() for char in run)
        and any(char.isascii() and char.isupper() for char in run)
        and any(char.isascii() and char.islower() for char in run)
        and sum(run.count(sep) for sep in "/+-_") * _BASE64_CHARS_PER_SEPARATOR
        <= len(run)
    )


def _carries_blob(segment: str) -> bool:
    return any(_is_blob(match.group()) for match in _BASE64_RUN.finditer(segment))


def _carries_wrapped_blob(segment: str) -> bool:
    lines: list[str] = []

    def is_blob() -> bool:
        if len(lines) < 2:
            return False
        run = "".join(lines)
        return len(run) >= 40 and _is_blob(run)

    for line in segment.split("\n"):
        candidate = line.strip()
        if candidate and _BASE64_LINE.fullmatch(candidate):
            lines.append(candidate)
        else:
            if is_blob():
                return True
            lines.clear()
    return is_blob()


def _carries_inline_payload(segment: str) -> bool:
    return (
        _INLINE_MARKUP.search(segment) is not None
        or _INLINE_LITERALS.search(segment) is not None
        or _JWT_RUN.search(segment) is not None
        or _carries_blob(segment)
        or _carries_wrapped_blob(segment)
        or _carries_inline_json(segment)
    )


def _prose_of(segment: str) -> str | None:
    """Passes (a) and (b): what of ``segment`` is left as prose, or ``None``."""
    segment = _without_path_lines(segment)
    if not segment.strip() or _is_whole_payload(segment):
        return None
    try:
        prose = _without_payload_lines(segment)
    except _UnboundedPayload:
        return None
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
    prompt_taints_next = False
    for raw in _SEGMENT_BREAK.split(_without_fenced_blocks(text)):
        segment = _without_path_lines(raw)
        if not segment.strip():
            in_diff = False
            continue
        prompt = _is_transcript(segment)
        if prompt_taints_next:
            prompt_taints_next = prompt
            in_diff = False
            continue
        if prompt:
            prompt_taints_next = True
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
    if len(text) > MAX_INPUT_CHARS:
        return None
    normalised = "\n".join(text.splitlines()).translate(_TO_PLAIN_SPACE)
    masked = mask_secret_references(normalised)
    screened, kept = _screened_segments(masked)
    if any(_carries_injection_marker(segment) for segment in screened):
        return None
    result = "\n\n".join(_collapsed(segment) for segment in kept)
    return result or None
