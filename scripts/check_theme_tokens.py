#!/usr/bin/env python3
"""Theme-token lint over apps/web/src and modules/*/web (AC 4, the "check flags a
hard-coded color/spacing value outside the contract" gate).

`docs/architecture/theme-contract.md` and packages/web-contract/theme/contract-v1.json
are the only source of visual values this repository's shell and module screens may use
— see `apps/web/src/app/globals.css`'s own banner comment: "Values come only from the
theme's --rs-* custom properties or CSS keywords". This script is the enforcement: a
static scan (colors and lengths do not throw at build time the way a bad `var()` on a
truly undeclared custom property silently resolves to `unset` rather than erroring), not
a runtime assertion.

Five rules, each independently self-tested:

1. No color literal: `#hex`, a color function (`rgb(`/`rgba(`/`hsl(`/`hsla(`/`hwb(`/
   `oklch(`/`oklab(`/`lab(`/`lch(`/`color(`, any case), or a CSS named color, other
   than the keywords `transparent`/`currentColor`/`inherit`. The rule looks only where
   a value is a color, so status words and prose are never mistaken for one:
   - `.css`: every declaration value (quoted strings blanked first, so
     `content: "gold"` is text). Hex and color functions are flagged in any
     property; a named color only in a color-bearing property (`color`,
     `background`, `border`, `outline`, `fill`, `stroke`, `shadow`, `decoration`,
     `caret`, `accent`, `column-rule`, `emphasis`, `flood`, `lighting`, `stop` in the
     name) or a custom property — so `font-family: Tan` is a font. Selectors are
     never scanned, so `#abc { … }` is an id.
   - `.ts`/`.tsx`/`.js`/`.jsx`/`.mjs`/`.cjs`: a string value under a style-shaped
     key or JSX attribute (the same color-bearing names: `{ border: "1px solid
     red" }`, `<path fill="red" />`), and a string whose whole content is a hex or
     color-function value (`const accent = "#fff"`) unless it is the value of a
     URL-shaped key or attribute (`href="#abc"`); a variable name counts as the
     key for an assignment. `status: "green"`, `tone: "red"`
     and `"see #108"` are not colors.
   - every file: a named color, hex or color function in a `var()` fallback
     (`var(--rs-color-ground, red)`), and a named color inside a gradient function
     in any property or string (`mask-image: linear-gradient(red, transparent)`).
2. In `.css` files only, no bare `px`/`rem`/`em`/`pt`/`pc`/`in`/`cm`/`mm`/`Q`
   length (any case, a leading-dot or exponent number included: `16PX`, `.5rem`,
   `1e2px`) outside an `@media`/`@container` prelude (`0`, `%`, `ch`, `fr`, `vh`,
   `vw` are always allowed). The CSS keyword widths `thin`/`medium`/`thick`
   (`ALLOWED_WIDTH_KEYWORDS`) are explicitly allowed too: contract v1 has no
   ring/border-width token, so `globals.css`'s `outline: medium solid
   var(--rs-color-control-focus)` has no token to spend instead.
3. No inline style: no JSX `style=` attribute in a `.tsx`/`.jsx` file (a `const style
   =` variable is not one); no `style:` key with a non-string value in an object
   literal outside a type body in a `.tsx`/`.jsx`/`.js`/`.mjs`/`.cjs` file (the props
   behind `{...{ style: … }}`; `Intl.NumberFormat(…, { style: "percent" })` and
   `style?: CSSProperties` are not styles); no `createElement(…, { style: … })` in any
   script file; and no imperative style write in any script file (`el.style.color =
   …`, `el.style.setProperty(…)`, `el.style.cssText = …`, `el.style[…] = …`,
   `Object.assign(el.style, …)`).
4. Every `var(--rs-…)` reference names an actual token on the v1 contract (or the
   `--rs-font-brand` font-loader variable) — catches a typo'd custom property that would
   otherwise silently resolve to nothing at runtime.
5. No `--rs-chrome-*` reference anywhere yet: contract-v1.json reserves the confirmation
   chrome's token subset (AC 5), but no chrome-consuming component exists in this run
   (spec.md: the rule "loosens to a `chrome/` directory allowance when the first one
   lands", which is not this run).

Exemptions are exact paths from spec.md, anchored at the repository root, never "a
directory of that name anywhere" — a module package cannot hide literals by growing its
own `theme/themes/` or `generated/` folder:

- `apps/web/src/theme/themes/` — theme *data* files carry literal values by design,
  and `apps/web/src/theme/builtins.ts`'s own docstring explains why they cannot promote
  themselves into anything else;
- `apps/web/src/generated/` — the shell's machine-written codegen output;
- test files (`*.test.*`/`*.spec.*`) — this codebase pins hex/rgba values in
  `contrast.test.ts`, `seed-dark.test.ts`, `built-in-css.test.ts` and
  `layout.test.tsx` on purpose, to assert what the *token pipeline* produces; the same
  reasoning `check_routing_literals.py`'s module docstring gives for its own test-file
  exclusion.

`apps/web/src` is the one required root: when it is missing the scan fails rather than
passing over nothing (the same shape as `check_web_platform.py`'s `_resolve_roots`).
Every `modules/*/web` package is optional and scanned when present. The shell's
tsconfig sets `allowJs`, so `.js`/`.jsx`/`.mjs`/`.cjs` sources are scanned like TS.

Comments are stripped before every check (CSS `/* */`; script `//` and `/* */`,
quote-aware) so prose mentioning a color or a length in passing — this module's own
docstring included — is never mistaken for a real declaration.

Runs under the system python3 (3.9-compatible, no third-party deps). `check()` is a
pure function over (path, text) pairs plus the contract's token names; the walk is
`_scan_pairs(repo_root)`, which the self-test also runs over a scratch repository tree,
so root discovery and the path exemptions are proven along with the rules.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "packages" / "web-contract" / "theme" / "contract-v1.json"

_SHELL_WEB_SRC = "apps/web/src"
# The exact exempt directories (POSIX, repository-relative, trailing slash).
_EXEMPT_PREFIXES = ("apps/web/src/theme/themes/", "apps/web/src/generated/")

_SCRIPT_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
_JSX_SUFFIXES = frozenset({".tsx", ".jsx"})
SUFFIXES = _SCRIPT_SUFFIXES | {".css"}
_TEST_FILE = re.compile(r"\.(?:test|spec)\.(?:ts|tsx|js|jsx|mjs|cjs)$")
_SKIP_DIRS = frozenset({"__pycache__", "node_modules", ".next"})

ALLOWED_COLOR_KEYWORDS = frozenset({"transparent", "currentcolor", "inherit"})

# `apps/web/src/app/globals.css` sets `outline: medium solid var(--rs-color-…)` on
# purpose (its own banner comment says so): contract v1 has no ring/border-width
# token, so a CSS keyword width is the only option until one exists. None of these
# words is a named color and none has a leading digit, so no rule matches them; the
# constant makes the exemption a stated decision rather than an artifact of what the
# regexes happen to miss, and `_self_test()` pins it.
ALLOWED_WIDTH_KEYWORDS = frozenset({"thin", "medium", "thick"})

# The CSS Color Module Level 4 extended named-color keywords (147, plus
# rebeccapurple), lower-cased. `transparent`/`currentcolor` are deliberately not in
# this set — they are the always-allowed keywords.
NAMED_COLORS = frozenset(
    """aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond
    blue blueviolet brown burlywood cadetblue chartreuse chocolate coral
    cornflowerblue cornsilk crimson cyan darkblue darkcyan darkgoldenrod darkgray
    darkgreen darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid
    darkred darksalmon darkseagreen darkslateblue darkslategray darkslategrey
    darkturquoise darkviolet deeppink deepskyblue dimgray dimgrey dodgerblue
    firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite gold goldenrod
    gray grey green greenyellow honeydew hotpink indianred indigo ivory khaki
    lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan
    lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon
    lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue
    lightyellow lime limegreen linen magenta maroon mediumaquamarine mediumblue
    mediumorchid mediumpurple mediumseagreen mediumslateblue mediumspringgreen
    mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin
    navajowhite navy oldlace olive olivedrab orange orangered orchid palegoldenrod
    palegreen paleturquoise palevioletred papayawhip peachpuff peru pink plum
    powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon
    sandybrown seagreen seashell sienna silver skyblue slateblue slategray
    slategrey snow springgreen steelblue tan teal thistle tomato turquoise violet
    wheat white whitesmoke yellow yellowgreen""".split()
)

# Property / key / attribute names whose value is a color.
_COLOR_BEARING = (
    r"color|background|border|outline|fill|stroke|shadow|decoration|caret|accent"
    r"|column-rule|emphasis|flood|lighting|stop"
)
_COLOR_BEARING_NAME = re.compile(rf"(?:{_COLOR_BEARING})", re.IGNORECASE)

_HEX_COLOR = re.compile(
    r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})(?![\w-])"
)
_COLOR_FUNC = re.compile(
    r"(?<![\w$.-])(?:rgba?|hsla?|hwb|oklch|oklab|lab|lch|color)\(", re.IGNORECASE
)
_WHOLE_COLOR = re.compile(
    r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})"
    r"|(?:rgba?|hsla?|hwb|oklch|oklab|lab|lch|color)\([^)]*\)",
    re.IGNORECASE,
)

# A standalone word in a value: not part of an identifier (`--rs-color-red`,
# `.red-flag`, `#red`), not a function name (`rgb(`), not a file name (`red.png`).
_VALUE_WORD = re.compile(r"(?<![\w#.-])([A-Za-z]+)(?![\w(.-])")
# A CSS statement: text up to `;`, `{` or `}`. One ending in `;` or `}` is a
# declaration (or an at-statement); one ending in `{` is a selector or prelude.
_CSS_STATEMENT = re.compile(r"([^;{}]*)([;{}])")
_CSS_STRING = re.compile(r"""(["'])(?:\\.|(?!\1)[^\\\n])*\1""")
# Every `var()` fallback: the text after the first comma.
_VAR_FALLBACK = re.compile(r"var\(\s*--[\w-]+\s*,([^)]*)\)")
# Any script string literal (single line for '/", multi-line for backticks).
_SCRIPT_STRING = re.compile(
    r"""(?P<q>["'])(?P<v>(?:\\.|(?!(?P=q))[^\\\n])*)(?P=q)"""
    r"""|`(?P<t>(?:\\.|[^\\`])*)`"""
)
# The key or JSX attribute a string literal is the value of, read off the text just
# before the literal: `key: `, `"key": `, `attr=`, `attr={`.
_KEY_BEFORE = re.compile(r"""['"]?([A-Za-z_$][\w$-]*)['"]?\s*(?::|=\s*\{?)\s*$""")
_URL_KEYS = frozenset(
    {"href", "to", "src", "action", "id", "hash", "anchor", "url", "link", "path"}
)

_LENGTH_UNITS = r"(?:rem|em|px|pt|pc|in|cm|mm|q)"
_BARE_LENGTH = re.compile(
    r"(?<![\w.])(?:\d+(?:\.\d+)?|\.\d+)(?:e[+-]?\d+)?" + _LENGTH_UNITS + r"(?![\w-])",
    re.IGNORECASE,
)
_AT_RULE_PRELUDE = re.compile(r"@(?:media|container)\b[^{]*\{")

# A JSX `style=` attribute: `style` preceded by whitespace (inside a tag) and followed
# by `=` then `{` or a quote — never a `const style =` / `let style =` declaration.
_INLINE_STYLE = re.compile(r"(?<![\w$.-])style\s*=\s*[{\"']")
_DECLARATION_BEFORE = re.compile(r"\b(?:const|let|var)\s+$")
_IMPERATIVE_STYLE = re.compile(
    r"\.style\s*(?:\.\s*[A-Za-z]+\s*=(?!=)|\.\s*setProperty\s*\(|\[[^\]]*\]\s*=(?!=))"
    r"|\bassign\(\s*[\w$.]+\.style\b"
)
# A `style:` key whose value is not a string literal, in a JSX-bearing or plain JS
# file — the props object behind `{...{ style: … }}` or a `createElement(…, { style:
# … })` call. A string value is an unrelated option (`Intl.NumberFormat(…, { style:
# "percent" })`), and `style?:` is an optional type member. `.ts` files are left out:
# they carry types and plain data, never rendered props.
_STYLE_KEY = re.compile(r"""(?<![\w$.-])['"]?style['"]?\s*:(?!:)(?!\s*["'`])""")
_STYLE_KEY_SUFFIXES = frozenset({".tsx", ".jsx", ".js", ".mjs", ".cjs"})
# `createElement(tag, { … style: … })` in any script file, `.ts` included.
_CREATE_ELEMENT_STYLE = re.compile(
    r"\bcreateElement\(\s*[^,()]+,\s*\{[^}]*(?<![\w$.-])['\"]?style['\"]?\s*:"
)
# What opens a type body rather than an object literal, read off the text just
# before a `{`: a type alias, interface, `extends`, a single `&`/`|` type operator
# (not the `&&`/`||` of a conditional spread), `satisfies`, or an annotation after
# `)`, `}` or `]`.
_TYPE_OPENER = re.compile(
    r"(?:\btype\s+[\w$]+(?:\s*<[^=]*>)?\s*=|\binterface\s+[\w$][^{]*"
    r"|\bextends\s+[^{]*|(?<![&|])[&|]|\bsatisfies\s*|[)}\]]\s*:)\s*$"
)
# The CSS gradient functions, whose arguments carry colors in any property
# (`mask-image: linear-gradient(red, transparent)`).
_GRADIENT_OPEN = re.compile(
    r"(?<![\w-])(?:repeating-)?(?:linear|radial|conic)-gradient\(", re.IGNORECASE
)

_VAR_REF = re.compile(r"var\(\s*(--rs-[A-Za-z0-9-]+)\s*(?:,[^)]*)?\)")
_CHROME_REF = re.compile(r"--rs-chrome-[A-Za-z0-9-]*")

FONT_BRAND_VAR = "--rs-font-brand"


def _strip_css_comments(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i : i + 2] == "/*":
            close = text.find("*/", i + 2)
            end = n if close == -1 else close + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in text[i:end]))
            i = end
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _strip_ts_comments(text: str) -> str:
    """Quote-aware `//` and `/* */` stripper — a local copy of
    `check_routing_literals.py`'s `_strip_ts_comments`, kept per script on purpose
    (see scripts/README.md): each gate is a standalone stdlib-only script with no
    in-repo import.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        pair = text[i : i + 2]
        if pair == "//":
            end = text.find("\n", i)
            end = n if end == -1 else end
            out.append(" " * (end - i))
            i = end
            continue
        if pair == "/*":
            close = text.find("*/", i + 2)
            end = n if close == -1 else close + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in text[i:end]))
            i = end
            continue
        char = text[i]
        if char in "\"'`":
            quote = char
            j = i + 1
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == quote:
                    j += 1
                    break
                if text[j] == "\n" and quote != "`":
                    break
                j += 1
            out.append(text[i:j])
            i = j
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _blank_css_strings(text: str) -> str:
    return _CSS_STRING.sub(
        lambda m: "".join("\n" if ch == "\n" else " " for ch in m.group(0)), text
    )


def _is_exempt(relative: str) -> bool:
    if _TEST_FILE.search(relative):
        return True
    return relative.startswith(_EXEMPT_PREFIXES)


def _named_colors_in(value: str) -> list[str]:
    return [
        word
        for word in _VALUE_WORD.findall(value)
        if word.lower() in NAMED_COLORS and word.lower() not in ALLOWED_COLOR_KEYWORDS
    ]


def _gradient_colors_in(path: str, value: str) -> set[str]:
    """Named colors inside every gradient function's (balanced) argument list."""
    findings: set[str] = set()
    for match in _GRADIENT_OPEN.finditer(value):
        depth, i = 1, match.end()
        while i < len(value) and depth:
            depth += {"(": 1, ")": -1}.get(value[i], 0)
            i += 1
        for word in _named_colors_in(value[match.end() : i - 1 if not depth else i]):
            findings.add(f"{path}: named color literal '{word}' in a gradient")
    return findings


def _in_type_body(text: str, position: int) -> bool:
    """True when `position` sits inside a brace body that a type construct opens
    (`_TYPE_OPENER`), at any nesting depth — a local copy of the same idea in
    `check_search_boundary.py`, kept per script on purpose (see scripts/README.md)."""
    depth = 0
    for i in range(position - 1, -1, -1):
        char = text[i]
        if char == "}":
            depth += 1
        elif char == "{":
            if depth:
                depth -= 1
                continue
            if _TYPE_OPENER.search(text, max(0, i - 200), i):
                return True
    return False


def _literal_colors_in(path: str, value: str) -> set[str]:
    """Hex, color-function and named-color findings for one color-position value."""
    findings: set[str] = set()
    for match in _HEX_COLOR.finditer(value):
        findings.add(f"{path}: hex color literal {match.group(0)}")
    for match in _COLOR_FUNC.finditer(value):
        findings.add(f"{path}: color function literal {match.group(0)}")
    for word in _named_colors_in(value):
        findings.add(f"{path}: named color literal '{word}'")
    return findings


def _var_fallback_findings(path: str, stripped: str) -> set[str]:
    findings: set[str] = set()
    for match in _VAR_FALLBACK.finditer(stripped):
        findings |= _literal_colors_in(path, match.group(1))
    return findings


def _css_color_findings(path: str, stripped: str) -> set[str]:
    findings = _var_fallback_findings(path, stripped)
    for match in _CSS_STATEMENT.finditer(_blank_css_strings(stripped)):
        statement, terminator = match.groups()
        if terminator == "{" or ":" not in statement:
            continue
        prop, value = statement.split(":", 1)
        prop = prop.strip()
        for hit in _HEX_COLOR.finditer(value):
            findings.add(f"{path}: hex color literal {hit.group(0)}")
        for hit in _COLOR_FUNC.finditer(value):
            findings.add(f"{path}: color function literal {hit.group(0)}")
        if prop.startswith("--") or _COLOR_BEARING_NAME.search(prop):
            for word in _named_colors_in(value):
                findings.add(f"{path}: named color literal '{word}'")
        findings |= _gradient_colors_in(path, value)
    return findings


def _script_color_findings(path: str, stripped: str) -> set[str]:
    findings = _var_fallback_findings(path, stripped)
    for match in _SCRIPT_STRING.finditer(stripped):
        value = match.group("v") if match.group("q") else match.group("t")
        findings |= _gradient_colors_in(path, value)
        before = _KEY_BEFORE.search(
            stripped, max(0, match.start() - 120), match.start()
        )
        key = before.group(1) if before else None
        if key and _COLOR_BEARING_NAME.search(key):
            findings |= _literal_colors_in(path, value)
            continue
        if _WHOLE_COLOR.fullmatch(value.strip()) and (
            key is None or key.lower() not in _URL_KEYS
        ):
            findings.add(f"{path}: color literal string '{value.strip()}'")
    return findings


def _length_findings(path: str, stripped: str) -> list[str]:
    findings = []
    text = _blank_css_strings(stripped)
    prelude_spans = [m.span() for m in _AT_RULE_PRELUDE.finditer(text)]
    for match in _BARE_LENGTH.finditer(text):
        pos = match.start()
        if any(start <= pos < end for start, end in prelude_spans):
            continue
        findings.append(f"{path}: bare length literal '{match.group(0)}'")
    return findings


def _inline_style_findings(path: str, stripped: str, *, suffix: str) -> list[str]:
    findings = []
    if suffix in _STYLE_KEY_SUFFIXES:
        for match in _STYLE_KEY.finditer(stripped):
            if not _in_type_body(stripped, match.start()):
                line = stripped.count("\n", 0, match.start()) + 1
                findings.append(f"{path}: inline style object key (line {line})")
                break
    if _CREATE_ELEMENT_STYLE.search(stripped):
        findings.append(f"{path}: inline style in a createElement props object")
    if suffix in _JSX_SUFFIXES:
        for match in _INLINE_STYLE.finditer(stripped):
            if _DECLARATION_BEFORE.search(
                stripped, max(0, match.start() - 40), match.start()
            ):
                continue
            findings.append(f"{path}: inline style= attribute")
            break
    if _IMPERATIVE_STYLE.search(stripped):
        findings.append(f"{path}: imperative style write")
    return findings


def _token_findings(
    path: str, stripped: str, contract_var_names: frozenset[str]
) -> list[str]:
    findings = []
    for match in _CHROME_REF.finditer(stripped):
        findings.append(f"{path}: forbidden chrome-token reference {match.group(0)}")
    for match in _VAR_REF.finditer(stripped):
        name = match.group(1)
        if name.startswith("--rs-chrome-"):
            continue  # already reported by the chrome-reference scan above
        if name != FONT_BRAND_VAR and name not in contract_var_names:
            findings.append(f"{path}: var({name}) does not name a contract token")
    return findings


def check(
    pairs: Iterable[tuple[str, str]], *, contract_var_names: frozenset[str]
) -> list[str]:
    """`pairs` is (repository-relative POSIX path, text). Pure — no filesystem."""
    findings: list[str] = []
    for path, raw_text in pairs:
        if _is_exempt(path):
            continue
        suffix = Path(path).suffix
        if suffix == ".css":
            stripped = _strip_css_comments(raw_text)
            findings.extend(sorted(_css_color_findings(path, stripped)))
            findings.extend(_length_findings(path, stripped))
        elif suffix in _SCRIPT_SUFFIXES:
            stripped = _strip_ts_comments(raw_text)
            findings.extend(sorted(_script_color_findings(path, stripped)))
            findings.extend(_inline_style_findings(path, stripped, suffix=suffix))
        else:
            continue
        findings.extend(_token_findings(path, stripped, contract_var_names))
    return findings


# --- real-tree wiring -----------------------------------------------------------------


def _resolve_roots(repo_root: Path) -> list[Path]:
    """`apps/web/src` is always returned, even when missing, so `_scan_pairs()` can
    report it; every `modules/*/web` is included only when it exists."""
    optional: list[Path] = []
    modules_root = repo_root / "modules"
    if modules_root.is_dir():
        optional.extend(r for r in sorted(modules_root.glob("*/web")) if r.is_dir())
    return [repo_root / _SHELL_WEB_SRC] + optional


def _scan_pairs(repo_root: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """Every scanned-suffix file under the resolved roots as (relative path, text),
    plus an error for a missing required root. Exemptions are applied by `check()`,
    not here, so they are decided by the same repository-relative path in the real
    scan and in the self-test."""
    pairs: list[tuple[str, str]] = []
    errors: list[str] = []
    for root in _resolve_roots(repo_root):
        if not root.is_dir():
            errors.append(f"web root not found at {root}")
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                file_path = Path(dirpath) / name
                if file_path.suffix not in SUFFIXES:
                    continue
                relative = file_path.relative_to(repo_root).as_posix()
                try:
                    text = file_path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                pairs.append((relative, text))
    return pairs, errors


def _contract_var_names() -> frozenset[str]:
    data = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    return frozenset("--rs-" + entry["name"].replace(".", "-") for entry in data)


# --- self-test ------------------------------------------------------------------------


def _findings_by_path(findings: Iterable[str]) -> dict[str, list[str]]:
    by_path: dict[str, list[str]] = {}
    for finding in findings:
        by_path.setdefault(finding.split(": ", 1)[0], []).append(finding)
    return by_path


_PLANTED = {
    # Rule 1, CSS.
    "planted/color.module.css": (".x { color: #fff; }\n", "hex color"),
    "planted/color-fn.module.css": (
        ".y { background: rgba(0,0,0,.5); }\n",
        "color function",
    ),
    "planted/color-fn-upper.module.css": (
        ".y { background: RGB(0 0 0); }\n",
        "color function",
    ),
    "planted/color-fn-hsla-upper.module.css": (
        ".y { color: HSLA(0, 0%, 0%, 1); }\n",
        "color function",
    ),
    "planted/color-fn-hwb.module.css": (
        ".y { color: hwb(0 0% 0%); }\n",
        "color function",
    ),
    "planted/color-name.module.css": (".z { color: salmon; }\n", "named color"),
    "planted/color-shorthand.module.css": (
        ".z { border: var(--rs-size-hairline) solid red; }\n",
        "named color",
    ),
    "planted/color-important.module.css": (
        ".z { color: red !important; }\n",
        "named color",
    ),
    "planted/color-last-declaration.module.css": (
        ".z { padding: var(--rs-space-3); color: navy }\n",
        "named color",
    ),
    "planted/color-custom-property.module.css": (
        ".z { --local-accent: tomato; }\n",
        "named color",
    ),
    "planted/color-var-fallback.module.css": (
        ".z { color: var(--rs-color-ground, red); }\n",
        "named color",
    ),
    # Rule 1, scripts.
    "planted/color-var-fallback.ts": (
        'export const c = "var(--rs-color-ground, tomato)";\n',
        "named color",
    ),
    "planted/color-style-key.ts": (
        'export const s = { border: "var(--rs-size-hairline) solid red" };\n',
        "named color",
    ),
    "planted/color-style-key-lone.ts": (
        'export const s = { color: "red" };\n',
        "named color",
    ),
    "planted/color-jsx-attribute.tsx": (
        'export const I = () => <path fill="red" />;\n',
        "named color",
    ),
    "planted/color-whole-string.ts": (
        'export const brand = "#fff";\n',
        "color literal string",
    ),
    "planted/color-whole-string.js": (
        "export const brand = 'rgb(0, 0, 0)';\n",
        "color literal string",
    ),
    "planted/color-style-key.mjs": (
        'export const s = { backgroundColor: "#000" };\n',
        "hex color",
    ),
    # Rule 2.
    "planted/length.module.css": (".w { margin: 16px; }\n", "bare length"),
    "planted/length-leading-dot.module.css": (
        ".w { margin: .5rem; }\n",
        "bare length",
    ),
    "planted/length-upper.module.css": (".w { padding: 16PX; }\n", "bare length"),
    "planted/length-exponent.module.css": (".w { width: 1e2px; }\n", "bare length"),
    "planted/length-in.module.css": (".w { width: 1in; }\n", "bare length"),
    "planted/length-cm.module.css": (".w { width: 2cm; }\n", "bare length"),
    "planted/length-mm.module.css": (".w { width: 3mm; }\n", "bare length"),
    "planted/length-pc.module.css": (".w { width: 1pc; }\n", "bare length"),
    "planted/length-q.module.css": (".w { width: 4Q; }\n", "bare length"),
    "planted/media-body-length.module.css": (
        "@media (min-width: 600px) { .a { padding: 4px; } }\n",
        "bare length",
    ),
    # Rule 3.
    "planted/component.tsx": (
        '<div style={{ color: "var(--rs-color-ground)" }} />;\n',
        "inline style",
    ),
    "planted/component.jsx": (
        '<div style="padding: var(--rs-space-3)" />;\n',
        "inline style",
    ),
    "planted/imperative-assign.ts": (
        'el.style.color = "var(--rs-color-ground)";\n',
        "imperative style write",
    ),
    "planted/imperative-set-property.ts": (
        'el.style.setProperty("--rs-space-3", "1");\n',
        "imperative style write",
    ),
    "planted/imperative-index.js": (
        'el.style["color"] = "var(--rs-color-ground)";\n',
        "imperative style write",
    ),
    "planted/imperative-assign-object.ts": (
        "Object.assign(el.style, overrides);\n",
        "imperative style write",
    ),
    "planted/spread-style.tsx": (
        "export const P = () => <p {...{ style: { gap: 0 } }} />;\n",
        "inline style object key",
    ),
    "planted/conditional-spread-style.jsx": (
        "export const P = (on) => <p {...(on && { style: tight })} />;\n",
        "inline style object key",
    ),
    "planted/props-object-style.js": (
        "const props = {\n  id: 'pill',\n  style: tight,\n};\n",
        "inline style object key",
    ),
    "planted/create-element-style.ts": (
        'createElement("div", { className: pill, style: tight });\n',
        "createElement props object",
    ),
    # Rule 1, gradients in a non-color property.
    "planted/gradient-mask.module.css": (
        ".m { mask-image: linear-gradient(red, transparent); }\n",
        "in a gradient",
    ),
    "planted/gradient-nested.module.css": (
        ".m { list-style-image: radial-gradient(circle at 50% 50%, "
        "var(--rs-color-ground), navy); }\n",
        "in a gradient",
    ),
    "planted/gradient-string.tsx": (
        'export const s = { maskImage: "conic-gradient(gold, transparent)" };\n',
        "in a gradient",
    ),
    # Rules 4 and 5.
    "planted/unknown-token.module.css": (
        ".v { color: var(--rs-color-nope); }\n",
        "does not name a contract token",
    ),
    "planted/chrome-ref.module.css": (
        ".u { color: var(--rs-chrome-surface); }\n",
        "chrome-token",
    ),
    # The exemptions are exact shell paths: a module package cannot borrow them.
    "modules/scratch/web/src/theme/themes/planted.css": (
        ".x { color: #fff; }\n",
        "hex color",
    ),
    "modules/scratch/web/src/generated/planted.ts": (
        'export const c = "#fff";\n',
        "color literal string",
    ),
}

_CLEAN = {
    "clean/component.module.css": (
        ".a { color: var(--rs-color-ground); padding: var(--rs-space-3); "
        "border: var(--rs-size-hairline) solid var(--rs-color-ground); "
        "outline: medium solid var(--rs-color-control-focus); }\n"
        "@media (prefers-color-scheme: dark) and (min-width: 40rem) "
        "{ .a { padding: var(--rs-space-3); } }\n"
        ".red-flag:hover { color: currentColor; background: transparent; }\n"
        "#abc { color: inherit; }\n"
    ),
    "clean/comment.module.css": (
        "/* #fff and 16px and red are not real declarations */\n"
        ".a { color: inherit; }\n"
    ),
    "clean/text-values.module.css": (
        '.a::after { content: "gold"; }\n'
        ".b { font-family: Tan, var(--rs-font-brand), serif; }\n"
        '.c { content: "16px"; }\n'
    ),
    "clean/width-keywords.module.css": "".join(
        f".w-{keyword} {{ outline: {keyword} solid "
        "var(--rs-color-control-focus); }}\n"
        for keyword in sorted(ALLOWED_WIDTH_KEYWORDS)
    ),
    "clean/status.tsx": (
        'export const pill = { status: "green", tone: "red", label: "Gold" };\n'
        'export const Link = () => <a href="#abc">{"see #108"}</a>;\n'
        'export const Banner = () => <p title="Red flags">{"Green light"}</p>;\n'
    ),
    "clean/style-variable.tsx": (
        "const style = { gap: 0 };\n"
        'function pick() { let style = "compact"; return style; }\n'
        "export const P = () => <p className={cx(style, pick())} />;\n"
    ),
    "clean/style-not-props.tsx": (
        'export const pct = new Intl.NumberFormat("en", { style: "percent" });\n'
        "type Props = { style: CSSProperties; tone: string };\n"
        "interface Frame { style: CSSProperties }\n"
        "export function C({ style }: { style: CSSProperties }) { return null; }\n"
        "export const D = (p: { style?: CSSProperties }) => <p />;\n"
    ),
    "clean/style-key-data.ts": "export const option = { style: base, id: 1 };\n",
    "clean/gradient-tokens.module.css": (
        ".g { mask-image: linear-gradient(var(--rs-color-ground), transparent); }\n"
        ".h { grid-area: red; animation-name: tomato; }\n"
    ),
    "clean/style-read.ts": (
        "const current = el.style.color === expected;\n"
        "export const x = getComputedStyle(el).color;\n"
    ),
    "clean/functions.ts": (
        "export const lab = (x: number) => x;\n"
        "export const y = model.color(1) + lab(2);\n"
    ),
    "apps/web/src/theme/themes/seed.ts": 'export const ground = "#101010";\n',
    "apps/web/src/generated/api-types.ts": 'export const c = "#fff";\n',
    "apps/web/src/components/pinned.test.tsx": 'expect(c).toBe("#fff");\n',
    "apps/web/src/components/pinned.test.js": 'expect(c).toBe("#fff");\n',
}


def _self_test() -> str | None:
    contract = frozenset(
        {
            "--rs-color-ground",
            "--rs-space-3",
            "--rs-size-hairline",
            "--rs-color-control-focus",
        }
    )
    by_path = _findings_by_path(
        check(
            [
                *((path, text) for path, (text, _) in _PLANTED.items()),
                *_CLEAN.items(),
            ],
            contract_var_names=contract,
        )
    )
    for path, (_, expect_substring) in _PLANTED.items():
        if not any(expect_substring in hit for hit in by_path.get(path, [])):
            return (
                f"self-test FAILED: {path} did not produce a '{expect_substring}' "
                f"finding (got {by_path.get(path, [])})"
            )
    for path in _CLEAN:
        if path in by_path:
            return f"self-test FAILED: clean content was flagged: {by_path[path]}"

    # Root discovery and the missing-required-root failure, over a scratch tree.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        module_file = repo / "modules" / "scratch" / "web" / "src" / "planted.css"
        module_file.parent.mkdir(parents=True)
        module_file.write_text(".x { color: #fff; }\n", encoding="utf-8")
        pairs, errors = _scan_pairs(repo)
        if not any("apps/web/src" in error for error in errors):
            return (
                "self-test FAILED: a missing apps/web/src was not reported (the "
                "required root was dropped before the scan)"
            )
        (repo / "apps" / "web" / "src").mkdir(parents=True)
        (repo / "apps" / "web" / "src" / "legacy.jsx").write_text(
            'export const a = "#fff";\n', encoding="utf-8"
        )
        pairs, errors = _scan_pairs(repo)
        if errors:
            return f"self-test FAILED: a present required root was reported: {errors}"
        found = _findings_by_path(check(pairs, contract_var_names=contract))
        if "modules/scratch/web/src/planted.css" not in found:
            return (
                "self-test FAILED: a literal planted in a scratch modules/<x>/web "
                "package went uncaught (module root not discovered)"
            )
        if "apps/web/src/legacy.jsx" not in found:
            return "self-test FAILED: a .jsx source file was not scanned"
    return None


def main() -> int:
    self_test_failure = _self_test()
    if self_test_failure is not None:
        print(self_test_failure, file=sys.stderr)
        return 1
    try:
        contract_var_names = _contract_var_names()
        pairs, errors = _scan_pairs(ROOT)
    except (OSError, RuntimeError, UnicodeError, ValueError) as error:
        print(f"Theme-token check failed: {error}", file=sys.stderr)
        return 1
    findings = errors + check(pairs, contract_var_names=contract_var_names)
    for finding in findings:
        print(finding, file=sys.stderr)
    if not findings:
        print(
            "Theme-token checks passed (self-test verified all five rules still "
            f"catch a planted violation): {len(pairs)} file(s) found across "
            f"{len(_resolve_roots(ROOT))} web root(s), every value traces to the "
            "contract outside apps/web/src/theme/themes/, apps/web/src/generated/ "
            "and test files."
        )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
