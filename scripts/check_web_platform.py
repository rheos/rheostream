#!/usr/bin/env python3
"""Platform-only denylist scan of apps/web, every modules/*/web, and
packages/web-contract (the criterion-23 gate).

`next build` does not fail on a `@vercel/*` import or an edge-runtime declaration,
so this static scan — not the build — is what keeps the web surface a plain,
self-hostable Next.js app with no platform-only feature. It is a foundation, not
exhaustive: later phases tighten it as the web surface grows (spec.md Technical Risks,
risk 5).

Widened from `apps/web` alone to also cover every `modules/*/web` and
`packages/web-contract` (spec.md Technical Risk 6: without this, criterion 23 would be
vacuous for a module's own web package or the shared contract package — a
platform-only import hard-coded there would be invisible to a scan that only ever
looked at `apps/web`).

`apps/web` is the one REQUIRED root; `modules/*/web` and `packages/web-contract` are
optional (they may not exist yet in every checkout state). `_resolve_roots()` keeps
that distinction explicit: the required root is always returned, even when it is
missing, so `check()` can still report it as an error, while an optional root is
included only when it exists (CodeRabbit f2 — an earlier revision filtered every
root, required or not, by `is_dir()` before `check()` ever saw the list, which
silently turned a missing `apps/web` into a clean scan of nothing instead of the
"web root not found" failure the pre-widening script always gave).

Runs under the system python3 (3.9-compatible, no third-party deps), mirroring
`scripts/check_repository.py`. Scans source only: `.next/`, `node_modules/`, and other
generated dirs carry `@vercel/*` and "edge" strings from Next.js/vendor internals, so
they are skipped — and the CI web job runs `next build` before this gate, so
`apps/web/.next` exists at scan time.

Self-test (new in this run — there was none before; `check_routing_literals.py`'s
`_self_test` is the convention this follows): `main()` always builds a scratch
repository tree with `apps/web/src`, `modules/<x>/web/src` and
`packages/web-contract/src`, plants one of the three violation forms in each, and scans
it through `_resolve_roots()` — the resolver the real scan uses — so the widened roots
are proven discovered, not only declared. It also confirms a near-miss (a comment
mentioning `@vercel/` in prose) is NOT caught and that a missing required web root is
still reported by `check()` rather than dropped before it, before scanning the real
tree.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Generated build output and vendored dependencies — skipped so their internals do
# not false-positive this scan.
SKIP_DIRS = frozenset({".next", "node_modules", "dist", "build", ".turbo"})

CODE_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})

# A JS/TS string literal quote: " or ' or ` (backtick as \x60 to avoid quoting noise).
_QUOTE = r"[\x22'\x60]"

# The two platform-only runtime values Next.js accepts for edge execution. Ordered
# so the longer literal is tried first (an alternation is left-biased).
_EDGE_VALUE = r"(?:experimental-edge|edge)"

# import/require/from ... "@vercel/..." — a platform-only package import.
_VERCEL_IMPORT = re.compile(r"(?:from|import|require)\s*\(?\s*" + _QUOTE + r"@vercel/")
# export const runtime = 'edge' | "edge" | `edge` (or 'experimental-edge'): the
# route-segment / config assignment form.
_EDGE_EXPORT = re.compile(
    r"export\s+const\s+runtime\s*=\s*(" + _QUOTE + r")" + _EDGE_VALUE + r"\1"
)
# An object-literal runtime: 'edge' | 'experimental-edge' key, e.g. inside
# `export const config = { runtime: 'edge' }`. Checked in every source file, not
# only next.config.*, so an edge runtime declared via a config object is caught too.
_EDGE_CONFIG_KEY = re.compile(
    r"\bruntime\s*:\s*(" + _QUOTE + r")" + _EDGE_VALUE + r"\1"
)


def _resolve_roots(repo_root: Path) -> list[Path]:
    """The roots under `repo_root` this gate scans. `apps/web` is always returned,
    even when it does not exist, so `check()` can still report a missing required
    web root (CodeRabbit f2: an earlier revision filtered every root — including the
    required one — by `is_dir()` here, before `check()` ever saw it, which silently
    turned a missing `apps/web` into a clean scan of nothing instead of the "web
    root not found" failure the pre-widening script always gave). The optional
    roots — every `modules/*/web` package, and `packages/web-contract` — are
    included only when they exist: neither is required to exist yet, unlike
    `apps/web`.
    """
    optional: list[Path] = []
    modules_root = repo_root / "modules"
    if modules_root.is_dir():
        optional.extend(sorted(modules_root.glob("*/web")))
    optional.append(repo_root / "packages" / "web-contract")
    return [repo_root / "apps" / "web"] + [r for r in optional if r.is_dir()]


def _source_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def _relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def check(*, roots: list[Path] | None = None) -> list[str]:
    if roots is None:
        roots = _resolve_roots(ROOT)
    errors = []
    for root in roots:
        if not root.is_dir():
            errors.append(f"web root not found at {root}")
            continue
        for path in sorted(_source_files(root)):
            if path.suffix not in CODE_SUFFIXES:
                continue
            relative = _relative(path)
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                # Non-text/unreadable file under a source tree: nothing to scan.
                continue
            if _VERCEL_IMPORT.search(text):
                errors.append(f"Platform-only @vercel/* import: {relative}")
            if _EDGE_EXPORT.search(text):
                errors.append(f"Edge runtime export declared: {relative}")
            if _EDGE_CONFIG_KEY.search(text):
                errors.append(f"Edge runtime configured: {relative}")
    return errors


def _self_test() -> str | None:
    """Build a scratch repository tree laid out like the real one — `apps/web/src`,
    `modules/<x>/web/src`, `packages/web-contract/src` — plant one violation form in
    each, and scan it through `_resolve_roots()`, the same resolver the real scan
    uses. Breaking module- or contract-root discovery therefore turns this red, not
    only breaking a pattern. A comment-only near-miss must never be caught. Never
    touches the real repo.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        planted = {
            "apps/web/src/planted-import.ts": (
                'import { geolocation } from "@vercel/functions";\n'
            ),
            "modules/scratch/web/src/planted-edge.ts": (
                'export const runtime = "edge";\n'
            ),
            "packages/web-contract/src/planted-config.ts": (
                'export const config = { runtime: "edge" };\n'
            ),
            "apps/web/src/quiet.ts": (
                "// this file intentionally avoids @vercel/ imports\n"
            ),
        }
        for relative, text in planted.items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

        findings = check(roots=_resolve_roots(repo))
        import_hit = any("apps/web/src/planted-import.ts" in f for f in findings)
        module_edge_hit = any(
            "modules/scratch/web/src/planted-edge.ts" in f for f in findings
        )
        config_hit = any(
            "packages/web-contract/src/planted-config.ts" in f for f in findings
        )
        quiet_hit = any("quiet.ts" in f for f in findings)

        if not import_hit:
            return "self-test FAILED: a planted @vercel/* import went uncaught"
        if not module_edge_hit:
            return (
                "self-test FAILED: an edge-runtime export planted inside a scratch "
                "modules/<x>/web/src went uncaught (module root not discovered)"
            )
        if not config_hit:
            return (
                "self-test FAILED: an edge-runtime config key planted inside a "
                "scratch packages/web-contract/src went uncaught (contract root not "
                "discovered)"
            )
        if quiet_hit:
            return "self-test FAILED: comment-only text was flagged"

    # CodeRabbit f2: a missing REQUIRED web root must still be reported by check() —
    # not silently filtered out of the root list before check() ever sees it.
    with tempfile.TemporaryDirectory() as empty:
        empty_repo = Path(empty)
        resolved = _resolve_roots(empty_repo)
        missing_web_root = empty_repo / "apps" / "web"
        if missing_web_root not in resolved:
            return (
                "self-test FAILED: _resolve_roots() dropped the required web root "
                "when it does not exist (check() can no longer report it)"
            )
        if not any(str(missing_web_root) in f for f in check(roots=resolved)):
            return (
                "self-test FAILED: check() did not report the missing required web root"
            )
    return None


def main():
    self_test_failure = _self_test()
    if self_test_failure is not None:
        print(self_test_failure, file=sys.stderr)
        return 1
    try:
        findings = check()
    except (OSError, RuntimeError, UnicodeError, ValueError) as error:
        print(f"Web platform check failed: {error}", file=sys.stderr)
        return 1
    for finding in findings:
        print(finding, file=sys.stderr)
    if not findings:
        print(
            "Web platform checks passed (self-test verified the scan still catches "
            "a planted violation, including inside a widened modules/*/web root): "
            "apps/web, every modules/*/web, and packages/web-contract are "
            "platform-neutral."
        )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
