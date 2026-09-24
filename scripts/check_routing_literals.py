#!/usr/bin/env python3
"""Routing-literal denylist scan of every web-src root and apps/core/src
(criterion 22, B10).

`docs/architecture/identity-and-topology.md` § The routing configuration calls this
"a lint failure in both codebases": every link, redirect, callback URL and MCP
configuration file must go through `url_for`/`identity_path` (Python) or
`urlFor`/`identityPath` (TypeScript) rather than spelling a host or a
topology-specific prefix out directly. A route string is correct in exactly one of
the two topologies (`path` or `subdomain`) and silently wrong in the other, which
is why this is a static scan rather than a runtime assertion — a wrong hard-coded
literal would still return 200 in whichever mode happened to match it.

Runs under the system python3 (3.9-compatible, no third-party deps), mirroring
`scripts/check_web_platform.py`. Scans every TypeScript web-src root —
`apps/web/src/**`, every `modules/*/web/src/**`, and `packages/web-contract/src/**`
(`.ts`/`.tsx`) — plus `apps/core/src/**` (`.py`) — for a literal `http://`,
`https://`, `/auth/`, `/api/`, or `/mcp` route string. spec.md Technical Risk 6:
without this widening, criterion 22 would be vacuous for module screens —
`apps/web/src` alone says nothing about a route literal hard-coded inside a
module's own web package.

Three allowlist mechanisms, not one, and they are deliberately different in kind:

1. **Content allowlist** (the run's own family of route-literal owners): the
   `apps/web/src/lib/routing/` package and `apps/core/src/rheo_app_core/
   {auth_routes,api_routes}.py`. These are excluded whole-file, not line-by-line —
   the routing package is where the literal is defined so `url_for`/`urlFor` has
   something to return, and the two FastAPI modules attach the literal path to a
   route *decorator* (`@router.get("/auth/login")`), which serves the path rather
   than linking to it. Narrow on purpose: a wider content allowlist (e.g. all of
   `apps/core/src/rheo_app_core/`) would let a real hard-coded link hide in the
   next file added beside these two, which is the vacuous-gate failure mode this
   script exists to avoid.
2. **Structural exclusion** (`*.test.ts`/`*.test.tsx`/`*.spec.ts`/`*.spec.tsx`):
   test files legitimately hard-code fixture URLs to pin what the routing
   functions under test *produce* — that is what a test is for, and scanning test
   fixtures for the same rule the implementation is tested against would make
   every test file its own permanent allowlist entry in practice. This mirrors
   `check_web_platform.py`'s `SKIP_DIRS` in spirit (both exclude something that is
   not "application code producing a link"), even though the mechanism here is a
   filename suffix rather than a directory.
3. **Structural exclusion, by directory** (`apps/web/src/generated/`): the
   generated OpenAPI document and the TypeScript types derived from it. Every
   operation path key in that document is the literal string
   `/api/v1/operations/<name>`, so `openapi-typescript` emits those literals as
   object keys — and unlike every other hit this scan can produce, **a generated
   file cannot be hand-fixed to satisfy the lint**: the fix would be reverted by
   the next `make codegen`. Deliberately a third mechanism rather than a new
   entry in `_ALLOWLISTED_DIRS`: the content allowlist means "this file is a
   legitimate owner of route literals", which is a claim about authorship that is
   false of a file nobody wrote.

   The exclusion is paired with `make codegen`'s own byte-exact CI check (run 0v,
   AC 3: `git diff --exit-code -- apps/web/src/generated` after regenerating), so
   the directory cannot quietly become a place to hide hand-written code — a
   hand-edit there fails that gate instead of this one. Neither gate alone would
   be enough; together they cover the whole directory. That pairing is why the
   exclusion is exactly `apps/web/src/generated/` and nothing else: a module
   package's own `web/src/generated/` has no byte-exact regeneration check behind
   it, so it is scanned like any other source directory.

A third piece keeps both scans honest without either allowlist mechanism becoming
a place to hide a real link: comments, JSDoc, and Python docstrings are stripped
(quote-aware, so an actual code string that happens to *start* with "http://" is
never mistaken for a `//` comment) before the pattern is matched. Without this,
ordinary prose — "see `/auth/continue`" in a comment, or this module's own
docstring — would fail the gate on documentation, which is a different and much
noisier failure mode than the one this scan is for.

Self-test (anti-vacuity, `spec.md` Technical Risk 10 / criterion 22): `main()`
always builds a scratch repository tree — `apps/web/src`, a `modules/<x>/web/src`,
`packages/web-contract/src`, `apps/core/src` — plants literals in each root and
beside each exclusion, and runs `check()` against it through the same root resolver
the real scan uses, before scanning the real tree. A scan that stopped being able to
catch anything (a broken regex, an allowlist that grew too wide, a root the resolver
no longer discovers) fails loudly on every invocation rather than silently passing
forever.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Every location below is relative to a repository root, so `check()` and the
# self-test resolve the same layout against the real tree and a scratch tree alike.
_SHELL_WEB_SRC = Path("apps") / "web" / "src"
_CORE_SRC = Path("apps") / "core" / "src"
_WEB_CONTRACT_SRC = Path("packages") / "web-contract" / "src"


def _resolve_web_roots(repo_root: Path) -> list[Path]:
    """Every TypeScript web-src root this gate scans: the shell, every module's own
    web package, and the shared web-contract package — widened from `apps/web/src`
    alone (spec.md Technical Risk 6).

    `apps/web/src` is the one REQUIRED root and is always returned, even when it is
    missing, so `check()` reports it rather than scanning nothing (the same shape as
    `check_web_platform.py`'s `_resolve_roots`). A module web package and the
    web-contract package are optional and included only when they exist.
    """
    optional: list[Path] = []
    modules_root = repo_root / "modules"
    if modules_root.is_dir():
        optional.extend(sorted(modules_root.glob("*/web/src")))
    optional.append(repo_root / _WEB_CONTRACT_SRC)
    return [repo_root / _SHELL_WEB_SRC] + [r for r in optional if r.is_dir()]


TS_SUFFIXES = frozenset({".ts", ".tsx"})
PY_SUFFIXES = frozenset({".py"})

# Structural exclusion (see module docstring, point 2): file-suffix based, not
# content-based, and applied only on the web side, where tests live beside the
# code they test. Python tests for this run live under the top-level tests/,
# which is outside both scanned roots already.
_TEST_SUFFIXES = ("test.ts", "test.tsx", "spec.ts", "spec.tsx")

# Structural exclusion by directory (see module docstring, point 3): exactly
# `apps/web/src/generated/`, the shell's codegen output that `make codegen`'s
# byte-exact CI check covers. A module package's own `web/src/generated/` gets no
# such pairing, so it is scanned like any other source directory.
_GENERATED_DIR = _SHELL_WEB_SRC / "generated"

# Generated/vendored dirs a walk of a src/ root should never meet in this repo,
# kept anyway so this scan degrades the same way check_web_platform.py's does if
# one ever appears (e.g. a stray __pycache__ from a local interpreter run).
_SKIP_DIRS = frozenset({"__pycache__", "node_modules", ".next"})

# Content allowlist (see module docstring, point 1): whole files, not lines within
# them — deliberately not "any file under rheo_app_core" or "any file under
# lib/", which would silently cover a future file that has no business holding a
# route literal.
_ALLOWLISTED_DIRS = (_SHELL_WEB_SRC / "lib" / "routing",)
_ALLOWLISTED_FILES = frozenset(
    {
        _CORE_SRC / "rheo_app_core" / "auth_routes.py",
        _CORE_SRC / "rheo_app_core" / "api_routes.py",
    }
)

# http(s):// as a scheme prefix; /auth/, /api/ as path prefixes; /mcp with no
# trailing slash required (the mcp surface's own path is exactly "/mcp", per
# RoutingConfig's package defaults — a trailing-slash requirement would miss it).
_LITERAL = re.compile(r"https?://|/auth/|/api/|/mcp")


def _is_test_file(path: Path) -> bool:
    return any(path.name.endswith(suffix) for suffix in _TEST_SUFFIXES)


def _repo_relative(path: Path, repo_root: Path) -> Path | None:
    try:
        return path.relative_to(repo_root)
    except ValueError:
        return None


def _is_generated(path: Path, repo_root: Path) -> bool:
    """True for anything under `apps/web/src/generated/` — the shell's
    machine-written codegen output, and nothing else.

    One exact directory, not "a `generated` segment anywhere": a module package's
    `web/src/generated/`, or a `lib/spike/generated/` invented later, would have to
    be added here deliberately rather than inheriting the exemption by name.
    """
    relative = _repo_relative(path, repo_root)
    if relative is None:
        return False
    return relative.parts[: len(_GENERATED_DIR.parts)] == _GENERATED_DIR.parts


def _is_allowlisted(path: Path, repo_root: Path) -> bool:
    relative = _repo_relative(path, repo_root)
    if relative is None:
        return False
    if relative in _ALLOWLISTED_FILES:
        return True
    return any(
        relative.parts[: len(directory.parts)] == directory.parts
        for directory in _ALLOWLISTED_DIRS
    )


def _strip_ts_comments(text: str) -> str:
    """Blank `//` and `/* */` comments (JSDoc included), preserving every string
    and template literal's own content untouched and every newline in place (so a
    caller that wanted line numbers could still recover them; this scan does not).

    Quote-aware by construction: the scan walks the text once, tracking whether it
    is inside a string/template literal, and only treats `//`/`/*` as a comment
    start outside one. Without this, a real code literal like `"http://evil"`
    would have its own `//` mistaken for a line-comment start and the rest of the
    string silently dropped from the scan — hiding exactly the thing this script
    exists to catch, which would be a worse failure than the false positives this
    function exists to avoid.
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
                    # Unterminated single/double-quoted literal (invalid TS) —
                    # stop the string here rather than running off the line.
                    break
                j += 1
            out.append(text[i:j])
            i = j
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _strip_py_prose(text: str) -> str:
    """Blank `#` comments and triple-quoted strings (this codebase's own
    docstring convention; see every module in `apps/core/src` for the pattern),
    quote-aware for the same reason `_strip_ts_comments` is.

    Every route literal this repo's Python side ever needs is a plain single- or
    double-quoted string (route decorators, `url_for` calls) — never triple-quoted
    — so treating a triple-quoted string as prose rather than code is a
    convention this scan relies on, not a guess; a triple-quoted string used as an
    actual route literal would be invisible to this scan, which is a real (if
    currently theoretical) limitation, named here rather than left implicit.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == "#":
            end = text.find("\n", i)
            end = n if end == -1 else end
            out.append(" " * (end - i))
            i = end
            continue
        triple = text[i : i + 3]
        if triple in ('"""', "'''"):
            close = text.find(triple, i + 3)
            end = n if close == -1 else close + 3
            out.append("".join("\n" if ch == "\n" else " " for ch in text[i:end]))
            i = end
            continue
        char = text[i]
        if char in "\"'":
            quote = char
            j = i + 1
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == quote:
                    j += 1
                    break
                if text[j] == "\n":
                    break
                j += 1
            out.append(text[i:j])
            i = j
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _iter_files(root: Path, suffixes: frozenset[str]) -> Iterator[Path]:
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix in suffixes:
                yield path


def _relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def check(*, repo_root: Path = ROOT) -> list[str]:
    """Scan every web-src root `_resolve_web_roots(repo_root)` discovers, plus
    `apps/core/src`, under `repo_root` — the real tree by default, a scratch tree in
    the self-test, through the same resolver either way."""
    errors: list[str] = []
    for web_root in _resolve_web_roots(repo_root):
        if not web_root.is_dir():
            errors.append(f"web root not found at {web_root}")
            continue
        for path in sorted(_iter_files(web_root, TS_SUFFIXES)):
            if (
                _is_allowlisted(path, repo_root)
                or _is_test_file(path)
                or _is_generated(path, repo_root)
            ):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if _LITERAL.search(_strip_ts_comments(text)):
                errors.append(f"Hard-coded route literal: {_relative(path)}")
    for path in sorted(_iter_files(repo_root / _CORE_SRC, PY_SUFFIXES)):
        if _is_allowlisted(path, repo_root):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if _LITERAL.search(_strip_py_prose(text)):
            errors.append(f"Hard-coded route literal: {_relative(path)}")
    return errors


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _self_test() -> str | None:
    """Build a scratch repository tree, plant literals in every root the resolver
    must discover and beside every exclusion, and confirm `check()` tells them
    apart.

    Returns `None` on success, or a diagnostic string naming the failure. The
    scratch tree is laid out like the real one (`apps/web/src`,
    `modules/<x>/web/src`, `packages/web-contract/src`, `apps/core/src`) and
    `check()` finds its roots through `_resolve_web_roots()`, so breaking module-
    or contract-root discovery turns this red, not only breaking the pattern.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        literal = 'export const plantedHref = "/api/v1/planted";\n'
        must_catch = {
            "shell": repo / "apps/web/src/components/planted.tsx",
            "module web-src root": repo / "modules/scratch/web/src/screens/planted.tsx",
            "web-contract src root": repo / "packages/web-contract/src/planted.ts",
            "module generated/ dir (exemption is shell-only)": (
                repo / "modules/scratch/web/src/generated/planted.ts"
            ),
        }
        for path in must_catch.values():
            _write(path, literal)
        _write(
            repo / "apps/core/src/rheo_app_core/planted.py",
            'LINK = "/auth/login"\n',
        )
        must_pass = [
            # The shell's codegen output (exempt, paired with make codegen's check).
            repo / "apps/web/src/generated/openapi.ts",
            # The routing package owns the literals url_for returns.
            repo / "apps/web/src/lib/routing/links.ts",
            # Test files pin what the routing functions produce.
            repo / "modules/scratch/web/src/screens/planted.test.tsx",
        ]
        for path in must_pass:
            _write(path, literal)
        _write(
            repo / "apps/core/src/rheo_app_core/auth_routes.py",
            '@router.get("/auth/login")\n',
        )
        # A comment-only occurrence must NOT be flagged — proves the stripper
        # does not just make the scan more trigger-happy than the real gate is.
        quiet = repo / "apps/web/src/components/quiet.tsx"
        _write(quiet, "// mentions /api/ only in prose, never in code\n")

        findings = check(repo_root=repo)
        joined = "\n".join(findings)
        for label, path in must_catch.items():
            if str(path) not in joined:
                return (
                    f"self-test FAILED: a literal planted in the {label} went "
                    "uncaught (root not discovered, or exclusion too wide)"
                )
        if "planted.py" not in joined:
            return "self-test FAILED: a literal planted in apps/core/src went uncaught"
        allowed_route = repo / "apps/core/src/rheo_app_core/auth_routes.py"
        for path in [*must_pass, quiet, allowed_route]:
            if str(path) in joined:
                return (
                    f"self-test FAILED: an exempt or comment-only file was flagged: "
                    f"{path.relative_to(repo)}"
                )

        # The required shell root is reported when missing, never scanned as empty.
        with tempfile.TemporaryDirectory() as empty:
            missing = check(repo_root=Path(empty))
            if not any("web root not found" in f for f in missing):
                return (
                    "self-test FAILED: a missing apps/web/src was not reported "
                    "(the required root was dropped before check() saw it)"
                )
        return None


def main() -> int:
    self_test_failure = _self_test()
    if self_test_failure is not None:
        print(self_test_failure, file=sys.stderr)
        return 1
    try:
        findings = check()
    except (OSError, RuntimeError, UnicodeError, ValueError) as error:
        print(f"Routing literal check failed: {error}", file=sys.stderr)
        return 1
    for finding in findings:
        print(finding, file=sys.stderr)
    if not findings:
        print(
            "Routing literal checks passed (self-test verified the scan still "
            "catches a planted literal): no hard-coded route string outside "
            "apps/web/src/lib/routing/, apps/web/src/generated/, or the two "
            "FastAPI route modules."
        )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
