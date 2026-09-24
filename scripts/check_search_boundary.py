#!/usr/bin/env python3
"""Search-input boundary scan (AC 10): "no search input exists anywhere in the
application shell outside Recallatron's own screen components."

Scans `apps/web/src/**`, `modules/*/web/**`, and `packages/web-contract/**` (`.tsx`,
`.ts`, and — because the shell's tsconfig sets `allowJs` — `.jsx`/`.js`/`.mjs`/`.cjs`)
for any statically visible search input, excluding exactly
`modules/recallatron/web/src/screens/search/**` — the one screen spec.md explicitly
allows to carry one (build Prompt 8 creates that directory; the exclusion is written by
path pattern rather than "does the directory exist", so it is correct before and after
Prompt 8 lands).

What counts as a search input, every form case-insensitive in its value:

- a `type`, `inputMode` or `enterKeyHint` attribute whose value is `search`, or a
  `role` attribute of `search`/`searchbox` (also inside a space-separated fallback
  list), in any quoting JSX allows: `"search"`, `'search'`, `{"search"}`,
  `{'search'}`, or a backtick template, with any casing (`type="Search"`);
- the HTML `<search>` element (lower-case: a capitalised `<Search>` is a React
  component, which this scan cannot see into by name);
- `createElement("search")`, and `createElement("input", { type: "search" })`;
- in `.tsx`/`.jsx` only, a props-shaped object literal carrying one of those keys
  with a search value — the statically visible half of a spread (`const props = {
  type: "search" }; <input {...props} />`). The value must end the property, so a
  union such as `type: "search" | "nav"` or an interface member ending in `;` is not
  props; neither is anything inside a `type X = {…}`/`interface X {…}` body. `.ts`
  files carry contract types (a future `searchProviders` shape), not rendered props,
  so this rule does not scan them.

A match anywhere else fails the scan, naming the file **and line** — AC 10 is meant to
be actionable at review time, and "some file in apps/web/src" is not.

`apps/web/src` is the one required root: when it is missing the scan fails rather than
passing over nothing (the same shape as `check_web_platform.py`'s `_resolve_roots`).
Every `modules/*/web` and `packages/web-contract` is optional and scanned when present.

Runs under the system python3 (3.9-compatible, no third-party deps), mirroring
`scripts/check_routing_literals.py`. Comments are stripped first (quote-aware, a local
copy of the same stripper — see scripts/README.md), so a code comment that happens to
mention `type="search"` in prose is never mistaken for a real attribute.

**Test files (`*.test.*`/`*.spec.*`) are excluded**, the same structural exclusion
`check_routing_literals.py`'s module docstring explains for its own scan: a companion
regression test legitimately asserts the ABSENCE of a search input by matching the same
literal pattern this scanner looks for — `ShellFrame.test.tsx` pins
`/type="search"|role="search"/` as a regex literal precisely so the shell frame can
never grow one.

Self-test (anti-vacuity): `main()` always checks every planted form against `check()`
directly — each OUTSIDE the excluded path (must be caught) and one at the excluded
path pattern itself (must NOT be caught), plus near-misses — and runs the walk over a
scratch repository tree, proving root discovery and the missing-root failure, before
scanning for real.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_SHELL_WEB_SRC = Path("apps") / "web" / "src"
_SKIP_DIRS = frozenset({"__pycache__", "node_modules", ".next"})
_SUFFIXES = (".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs")
_JSX_SUFFIXES = (".tsx", ".jsx")
_TEST_FILE = re.compile(r"\.(?:test|spec)\.(?:ts|tsx|js|jsx|mjs|cjs)$")

# The one screen this rule exempts (spec.md, AC 10). Written as a path-prefix check
# against the POSIX-relative path, not a filesystem existence check — Prompt 8, not
# this prompt, creates the directory.
_EXCLUDED_PREFIX = "modules/recallatron/web/src/screens/search/"

_QUOTED = r"""(?P<q>["'`])"""
_SEARCH_PATTERNS = (
    # JSX attribute: type/inputMode/enterKeyHint="search", in any JSX quoting.
    re.compile(
        r"\b(?:type|inputMode|enterKeyHint)\s*=\s*(?:\{\s*)?"
        + _QUOTED
        + r"\s*search\s*(?P=q)",
        re.IGNORECASE,
    ),
    # JSX attribute: role="search" / "searchbox", or either in a fallback list.
    re.compile(
        r"\brole\s*=\s*(?:\{\s*)?"
        + _QUOTED
        + r"(?:(?!(?P=q)).)*?\bsearch(?:box)?\b(?:(?!(?P=q)).)*(?P=q)",
        re.IGNORECASE,
    ),
    # The HTML <search> element.
    re.compile(r"<search(?=[\s>/])"),
    # createElement("search"), and createElement("input", { type: "search" }).
    re.compile(r"\bcreateElement\(\s*" + _QUOTED + r"search(?P=q)", re.IGNORECASE),
    re.compile(
        r"""\bcreateElement\(\s*(["'`])input\1\s*,\s*\{[^}]*"""
        r"""\b(?:type|role|inputMode|enterKeyHint)['"]?\s*:\s*(["'`])"""
        r"""\s*search(?:box)?\s*\2""",
        re.IGNORECASE,
    ),
)

# JSX files only: a props-shaped object literal carrying a search value (see the
# module docstring). The lookahead requires the value to end the property.
_PROPS_OBJECT = re.compile(
    r"""(?<![\w$])['"]?(?:type|role|inputMode|enterKeyHint)['"]?\s*:\s*"""
    + _QUOTED
    + r"""\s*search(?:box)?\s*(?P=q)(?=\s*(?:[,})]|$))""",
    re.IGNORECASE | re.MULTILINE,
)
# What opens a type body rather than an object literal, read off the text just
# before a `{`.
_TYPE_OPENER = re.compile(
    r"(?:\btype\s+[\w$]+(?:\s*<[^=]*>)?\s*=|\binterface\s+[\w$][^{]*"
    r"|\bextends\s+[^{]*|[&|]|\bsatisfies\s+[^{]*)\s*$"
)


def _in_type_body(text: str, position: int) -> bool:
    """True when `position` sits inside a brace body that a type alias, interface,
    `extends`, a `&`/`|` type operator, or `satisfies` opens, at any nesting depth."""
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


def _strip_ts_comments(text: str) -> str:
    """Quote-aware `//`/`/* */` stripper — a local copy of
    `check_routing_literals.py`'s original, kept per script on purpose (see
    scripts/README.md): each gate is a standalone stdlib-only script with no in-repo
    import."""
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


def _is_excluded(relative: str) -> bool:
    return relative.startswith(_EXCLUDED_PREFIX)


def _is_test_file(relative: str) -> bool:
    return bool(_TEST_FILE.search(relative))


def check(pairs: Iterable[tuple[str, str]]) -> list[str]:
    """`pairs` is (relative path, file text). Pure — no filesystem access — so the
    self-test exercises exactly this logic against synthetic paths.
    """
    findings: list[str] = []
    for relative, text in pairs:
        if _is_excluded(relative) or _is_test_file(relative):
            continue
        stripped = _strip_ts_comments(text)
        hits: dict[int, str] = {}
        for pattern in _SEARCH_PATTERNS:
            for match in pattern.finditer(stripped):
                hits.setdefault(match.start(), match.group(0))
        if relative.endswith(_JSX_SUFFIXES):
            for match in _PROPS_OBJECT.finditer(stripped):
                if not _in_type_body(stripped, match.start()):
                    hits.setdefault(match.start(), match.group(0))
        for start in sorted(hits):
            line = stripped.count("\n", 0, start) + 1
            findings.append(
                f"{relative}:{line}: search input outside Recallatron's search "
                f"screen ({hits[start]})"
            )
    return findings


# --- real-tree wiring -----------------------------------------------------------------


def _resolve_roots(repo_root: Path) -> list[Path]:
    """`apps/web/src` is always returned, even when missing, so the walk can report
    it; every `modules/*/web` and `packages/web-contract` only when it exists."""
    optional: list[Path] = []
    modules_root = repo_root / "modules"
    if modules_root.is_dir():
        optional.extend(sorted(modules_root.glob("*/web")))
    optional.append(repo_root / "packages" / "web-contract")
    return [repo_root / _SHELL_WEB_SRC] + [r for r in optional if r.is_dir()]


def _scan_pairs(repo_root: Path) -> tuple[list[tuple[str, str]], list[str]]:
    pairs: list[tuple[str, str]] = []
    errors: list[str] = []
    for root in _resolve_roots(repo_root):
        if not root.is_dir():
            errors.append(f"web root not found at {root}")
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                if not name.endswith(_SUFFIXES):
                    continue
                file_path = Path(dirpath) / name
                relative = file_path.relative_to(repo_root).as_posix()
                try:
                    text = file_path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                pairs.append((relative, text))
    return pairs, errors


# --- self-test ------------------------------------------------------------------------

_OUTSIDE = "apps/web/src/components/"

_PLANTED = {
    "type-double.tsx": '<input type="search" placeholder="find anything" />;\n',
    "type-braced-double.tsx": '<input type={"search"} />;\n',
    "type-braced-single.tsx": "<input type={'search'} />;\n",
    "type-backtick.tsx": "<input type={`search`} />;\n",
    "type-case.tsx": '<input type="Search" />;\n',
    "input-mode.tsx": '<input type="text" inputMode="search" />;\n',
    "enter-key-hint.tsx": "<input type=\"text\" enterKeyHint={'search'} />;\n",
    "role-double.tsx": '<div role="search"><input /></div>;\n',
    "role-braced-double.tsx": '<div role={"search"}><input /></div>;\n',
    "role-braced-single.tsx": "<div role={'search'}><input /></div>;\n",
    "role-searchbox.tsx": '<div role="searchbox" contentEditable />;\n',
    "role-fallback-list.tsx": '<div role="search navigation" />;\n',
    "search-element.tsx": "<search><input /></search>;\n",
    "spread-props.tsx": (
        'const props = { type: "search" } as const;\n<input {...props} />;\n'
    ),
    "spread-inline.tsx": "<input {...{ type: 'search' }} />;\n",
    "spread-multiline.jsx": (
        "const props = {\n  id: 'find',\n  inputMode: 'search'\n};\n"
        "<input {...props} />;\n"
    ),
    "legacy.jsx": '<input type="search" />;\n',
    "legacy.mjs": "export const html = '<input type=\"search\">';\n",
    "create-element.ts": 'createElement("input", { type: "search" });\n',
    "create-search-element.js": 'createElement("search", null);\n',
}
_NEAR_MISSES = {
    "quiet.tsx": (
        '// a search input would use type="search" here, but this one is a link\n'
        '<a href="/find">Find</a>;\n'
    ),
    "text-input.tsx": '<input type="text" name="search" aria-label="Find" />;\n',
    "component.tsx": "<Search results={results} /><Researcher />;\n",
    "search-provider.ts": 'export const provider = { kind: "search" };\n',
    "contract-types.ts": (
        'export type SearchProvider = { type: "search" | "nav"; id: string };\n'
        'export const providers = [{ type: "search", id: "recall" }];\n'
    ),
    "union-in-jsx.tsx": (
        'type Mode = { type: "search" | "nav" };\n'
        'export type Only = { type: "search" };\n'
        'interface Props { role: "search"; nested: { type: "search" } }\n'
        'type Merged = Base & { type: "search" };\n'
        "export const Nav = (p: Mode) => <nav />;\n"
    ),
    "prose.tsx": '<p>{"Use the search screen to find a memory."}</p>;\n',
    # Inline annotations: the brace is not a type-alias body, so only the
    # end-of-property rule keeps these out.
    "inline-annotations.tsx": (
        'export const Nav = (p: { type: "search" | "nav" }) => <nav />;\n'
        'declare const mode: { type: "search"; id: string };\n'
    ),
}


def _self_test() -> str | None:
    pairs: list[tuple[str, str]] = [
        *((_OUTSIDE + name, text) for name, text in _PLANTED.items()),
        *((_OUTSIDE + name, text) for name, text in _NEAR_MISSES.items()),
        (
            "modules/recallatron/web/src/screens/search/index.tsx",
            '<input type="search" placeholder="recall" />;\n',
        ),
        (
            "apps/web/src/shell/planted.test.tsx",
            'const SEARCH_INPUT = /type="search"|role="search"/;\n'
            "expect(rendered).not.toMatch(SEARCH_INPUT);\n",
        ),
    ]
    findings = check(pairs)
    flagged = {finding.split(":", 1)[0] for finding in findings}

    missing = sorted(name for name in _PLANTED if _OUTSIDE + name not in flagged)
    if missing:
        return f"self-test FAILED: planted search inputs went uncaught: {missing}"
    wrong = sorted(name for name in _NEAR_MISSES if _OUTSIDE + name in flagged)
    if wrong:
        return f"self-test FAILED: near-miss text was flagged: {wrong}"
    if "modules/recallatron/web/src/screens/search/index.tsx" in flagged:
        return (
            "self-test FAILED: the excluded Search-screen path was flagged "
            "(exclusion is not real)"
        )
    if "apps/web/src/shell/planted.test.tsx" in flagged:
        return (
            "self-test FAILED: a companion regression test's own assertion "
            "text was flagged"
        )
    if not any(f.startswith(_OUTSIDE + "spread-props.tsx:1:") for f in findings):
        return (
            "self-test FAILED: the planted violation's line number was not "
            "reported as line 1"
        )

    # Root discovery and the missing-required-root failure, over a scratch tree.
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        module_file = repo / "modules" / "scratch" / "web" / "src" / "Find.tsx"
        contract_file = repo / "packages" / "web-contract" / "src" / "find.jsx"
        for path, text in (
            (module_file, '<input type="search" />;\n'),
            (contract_file, '<div role="search" />;\n'),
        ):
            path.parent.mkdir(parents=True)
            path.write_text(text, encoding="utf-8")
        _, errors = _scan_pairs(repo)
        if not any("apps/web/src" in error for error in errors):
            return (
                "self-test FAILED: a missing apps/web/src was not reported (the "
                "required root was dropped before the scan)"
            )
        (repo / _SHELL_WEB_SRC).mkdir(parents=True)
        pairs_found, errors = _scan_pairs(repo)
        if errors:
            return f"self-test FAILED: a present required root was reported: {errors}"
        found = {f.split(":", 1)[0] for f in check(pairs_found)}
        for expected in (
            "modules/scratch/web/src/Find.tsx",
            "packages/web-contract/src/find.jsx",
        ):
            if expected not in found:
                return (
                    f"self-test FAILED: {expected} planted in a scratch tree went "
                    "uncaught (root not discovered, or suffix not scanned)"
                )
    return None


def main() -> int:
    self_test_failure = _self_test()
    if self_test_failure is not None:
        print(self_test_failure, file=sys.stderr)
        return 1
    try:
        pairs, errors = _scan_pairs(ROOT)
    except (OSError, RuntimeError, UnicodeError, ValueError) as error:
        print(f"Search-boundary check failed: {error}", file=sys.stderr)
        return 1
    findings = errors + check(pairs)
    for finding in findings:
        print(finding, file=sys.stderr)
    if not findings:
        print(
            "Search-boundary checks passed (self-test verified every planted form is "
            "caught and the inside/outside exclusion pair still behaves correctly): "
            f"{len(pairs)} file(s), no search input outside {_EXCLUDED_PREFIX}."
        )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
