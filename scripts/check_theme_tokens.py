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

1. No color literal: `#hex` or a color function (`rgb(`/`rgba(`/`hsl(`/`hsla(`/
   `oklch(`/`oklab(`/`lab(`/`lch(`/`color(`) anywhere in code, and no CSS named color
   in a value, other than the keywords `transparent`/`currentColor`/`inherit`. A named
   color is found wherever it sits in the value: alone (`color: red`), inside a
   shorthand (`border: 1px solid red`), before `!important`, or as a `var()` fallback
   (`var(--rs-color-ground, red)`). In `.css` every declaration value is scanned word
   by word; in `.ts`/`.tsx` three places are: a lone word after a `:` (`{ color:
   "red" }`), a string value under a style-shaped key (`color`, `background`, `border`,
   `outline`, `fill`, `stroke`, `shadow`, `decoration` in the key — `{ border: "1px
   solid red" }`), and every `var()` fallback. Other TS strings are UI copy, where
   "Red flags" is prose, not a color.
2. In `.css` files only, no bare `px`/`rem`/`em`/`pt` length (any case, with or
   without a leading zero: `16px`, `16PX`, `.5rem`) outside an `@media`/`@container`
   prelude (`0`, `%`, `ch`, `fr`, `vh`, `vw` are always allowed — they carry no
   token-vs-literal ambiguity the way a length unit does). The CSS keyword widths
   `thin`/`medium`/`thick` (`ALLOWED_WIDTH_KEYWORDS`) are explicitly allowed too:
   contract v1 has no ring/border-width token, so `globals.css`'s
   `outline: medium solid var(--rs-color-control-focus)` has no token to spend instead.
3. No inline `style=` attribute in any `.tsx` file.
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
- test files (`*.test.ts(x)`/`*.spec.ts(x)`) — this codebase pins hex/rgba values in
  `contrast.test.ts`, `seed-dark.test.ts`, `built-in-css.test.ts` and
  `layout.test.tsx` on purpose, to assert what the *token pipeline* produces; the same
  reasoning `check_routing_literals.py`'s module docstring gives for its own test-file
  exclusion.

`apps/web/src` is the one required root: when it is missing the scan fails rather than
passing over nothing (the same shape as `check_web_platform.py`'s `_resolve_roots`).
Every `modules/*/web` package is optional and scanned when present.

Comments are stripped before every check (CSS `/* */`; TS/TSX `//` and `/* */`,
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

SUFFIXES = frozenset({".css", ".ts", ".tsx"})
_TEST_SUFFIXES = ("test.ts", "test.tsx", "spec.ts", "spec.tsx")
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

_HEX_COLOR = re.compile(
    r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})\b"
)
_COLOR_FUNC = re.compile(r"\b(?:rgba?|hsla?|oklch|oklab|lab|lch|color)\(")

# A standalone word in a CSS value: not part of an identifier (`--rs-color-red`,
# `.red-flag`, `#red`), not a function name (`rgb(`), not a file name (`red.png`).
_VALUE_WORD = re.compile(r"(?<![\w#.-])([A-Za-z]+)(?![\w(.-])")
# A CSS statement: text up to `;`, `{` or `}`. One ending in `;` or `}` is a
# declaration (or an at-statement); one ending in `{` is a selector or prelude.
_CSS_STATEMENT = re.compile(r"([^;{}]*)([;{}])")
# Every `var()` fallback, in any file type: the text after the first comma.
_VAR_FALLBACK = re.compile(r"var\(\s*--[\w-]+\s*,([^)]*)\)")
# TS/TSX: a lone word after a `:` (a JS object-literal key), optionally quoted,
# followed by a statement-ending character.
_NAMED_COLOR_VALUE = re.compile(r":\s*['\"`]?([A-Za-z]{3,20})['\"`]?\s*(?=[;,}\n)]|$)")
# TS/TSX: a string value under a style-shaped key, e.g. `border: "1px solid red"`.
_STYLE_KEY_STRING = re.compile(
    r"""['"]?[A-Za-z-]*(?:[Cc]olor|[Bb]ackground|[Bb]order|[Oo]utline|[Ff]ill"""
    r"""|[Ss]troke|[Ss]hadow|[Dd]ecoration)[A-Za-z-]*['"]?\s*:\s*"""
    r"""(['"`])((?:\\.|(?!\1)[^\\\n])*)\1"""
)

_BARE_LENGTH = re.compile(
    r"(?<![\w.])(?:\d+(?:\.\d+)?|\.\d+)(?:px|rem|em|pt)(?![A-Za-z0-9])", re.IGNORECASE
)
_AT_RULE_PRELUDE = re.compile(r"@(?:media|container)\b[^{]*\{")

_INLINE_STYLE = re.compile(r"(?<![\w-])style\s*=")

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


def _is_exempt(relative: str) -> bool:
    if any(relative.endswith(suffix) for suffix in _TEST_SUFFIXES):
        return True
    return relative.startswith(_EXEMPT_PREFIXES)


def _named_colors_in(value: str) -> list[str]:
    return [
        word
        for word in _VALUE_WORD.findall(value)
        if word.lower() in NAMED_COLORS and word.lower() not in ALLOWED_COLOR_KEYWORDS
    ]


def _color_findings(path: str, stripped: str, *, is_css: bool) -> set[str]:
    findings: set[str] = set()
    for match in _HEX_COLOR.finditer(stripped):
        findings.add(f"{path}: hex color literal {match.group(0)}")
    for match in _COLOR_FUNC.finditer(stripped):
        findings.add(f"{path}: color function literal {match.group(0)}")
    named: list[str] = []
    for match in _VAR_FALLBACK.finditer(stripped):
        named.extend(_named_colors_in(match.group(1)))
    if is_css:
        for match in _CSS_STATEMENT.finditer(stripped):
            statement, terminator = match.groups()
            if terminator == "{" or ":" not in statement:
                continue
            named.extend(_named_colors_in(statement.split(":", 1)[1]))
    else:
        for match in _NAMED_COLOR_VALUE.finditer(stripped):
            word = match.group(1)
            if word.lower() in NAMED_COLORS:
                named.append(word)
        for match in _STYLE_KEY_STRING.finditer(stripped):
            named.extend(_named_colors_in(match.group(2)))
    for word in named:
        if word.lower() not in ALLOWED_COLOR_KEYWORDS:
            findings.add(f"{path}: named color literal '{word}'")
    return findings


def _length_findings(path: str, stripped: str) -> list[str]:
    findings = []
    prelude_spans = [m.span() for m in _AT_RULE_PRELUDE.finditer(stripped)]
    for match in _BARE_LENGTH.finditer(stripped):
        pos = match.start()
        if any(start <= pos < end for start, end in prelude_spans):
            continue
        findings.append(f"{path}: bare length literal '{match.group(0)}'")
    return findings


def _inline_style_findings(path: str, stripped: str) -> list[str]:
    if _INLINE_STYLE.search(stripped):
        return [f"{path}: inline style= attribute"]
    return []


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
        is_css = suffix == ".css"
        stripped = (
            _strip_css_comments(raw_text) if is_css else _strip_ts_comments(raw_text)
        )
        findings.extend(sorted(_color_findings(path, stripped, is_css=is_css)))
        if is_css:
            findings.extend(_length_findings(path, stripped))
        if suffix == ".tsx":
            findings.extend(_inline_style_findings(path, stripped))
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
    """Every `.css`/`.ts`/`.tsx` file under the resolved roots as (relative path,
    text), plus an error for a missing required root. Exemptions are applied by
    `check()`, not here, so they are decided by the same repository-relative path in
    the real scan and in the self-test."""
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


def _self_test() -> str | None:
    contract = frozenset(
        {
            "--rs-color-ground",
            "--rs-space-3",
            "--rs-size-hairline",
            "--rs-color-control-focus",
        }
    )
    planted = {
        "planted/color.module.css": (".x { color: #fff; }\n", "hex color"),
        "planted/color-fn.module.css": (
            ".y { background: rgba(0,0,0,.5); }\n",
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
        "planted/color-var-fallback.module.css": (
            ".z { color: var(--rs-color-ground, red); }\n",
            "named color",
        ),
        "planted/color-var-fallback.ts": (
            'export const c = "var(--rs-color-ground, tomato)";\n',
            "named color",
        ),
        "planted/color-style-key.ts": (
            'export const s = { border: "var(--rs-size-hairline) solid red" };\n',
            "named color",
        ),
        "planted/color-lone-value.ts": (
            'export const s = { color: "red" };\n',
            "named color",
        ),
        "planted/length.module.css": (".w { margin: 16px; }\n", "bare length"),
        "planted/length-leading-dot.module.css": (
            ".w { margin: .5rem; }\n",
            "bare length",
        ),
        "planted/length-upper.module.css": (".w { padding: 16PX; }\n", "bare length"),
        "planted/media-body-length.module.css": (
            "@media (min-width: 600px) { .a { padding: 4px; } }\n",
            "bare length",
        ),
        "planted/component.tsx": (
            '<div style={{ color: "var(--rs-color-ground)" }} />;\n',
            "inline style",
        ),
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
            "hex color",
        ),
    }
    clean = {
        "clean/component.module.css": (
            ".a { color: var(--rs-color-ground); padding: var(--rs-space-3); "
            "border: var(--rs-size-hairline) solid var(--rs-color-ground); "
            "outline: medium solid var(--rs-color-control-focus); }\n"
            "@media (prefers-color-scheme: dark) and (min-width: 40rem) "
            "{ .a { padding: var(--rs-space-3); } }\n"
            ".red-flag:hover { color: currentColor; background: transparent; }\n"
        ),
        "clean/comment.module.css": (
            "/* #fff and 16px and red are not real declarations */\n"
            ".a { color: inherit; }\n"
        ),
        "clean/width-keywords.module.css": "".join(
            f".w-{keyword} {{ outline: {keyword} solid "
            "var(--rs-color-control-focus); }}\n"
            for keyword in sorted(ALLOWED_WIDTH_KEYWORDS)
        ),
        "clean/copy.tsx": (
            'export const Banner = () => <p title="Red flags">{"Green light"}</p>;\n'
            'export const label = { title: "Gold members", kind: "neutral" };\n'
        ),
        "apps/web/src/theme/themes/seed.ts": 'export const ground = "#101010";\n',
        "apps/web/src/generated/api-types.ts": 'export const c = "#fff";\n',
        "apps/web/src/components/pinned.test.tsx": 'expect(c).toBe("#fff");\n',
    }

    by_path = _findings_by_path(
        check(
            [
                *((path, text) for path, (text, _) in planted.items()),
                *clean.items(),
            ],
            contract_var_names=contract,
        )
    )
    for path, (_, expect_substring) in planted.items():
        if not any(expect_substring in hit for hit in by_path.get(path, [])):
            return (
                f"self-test FAILED: {path} did not produce a '{expect_substring}' "
                f"finding (got {by_path.get(path, [])})"
            )
    for path in clean:
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
        pairs, errors = _scan_pairs(repo)
        if errors:
            return f"self-test FAILED: a present required root was reported: {errors}"
        found = _findings_by_path(check(pairs, contract_var_names=contract))
        if "modules/scratch/web/src/planted.css" not in found:
            return (
                "self-test FAILED: a literal planted in a scratch modules/<x>/web "
                "package went uncaught (module root not discovered)"
            )
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
