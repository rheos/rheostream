#!/usr/bin/env python3
"""Every web workspace package declares `lint`, `typecheck` and `test` scripts.

`make lint`, `make typecheck` and `make test` (and the CI web job) run `pnpm -r lint`,
`pnpm -r typecheck` and `pnpm -r test`. `pnpm -r <script>` silently skips a package
that does not declare `<script>` and still exits 0, so a new module web package
without a `test` script would never have its tests run, and nothing would say so.
This gate turns that silence into a failure.

Two checks over every package directory that holds a `package.json` under `apps/*`,
`packages/*` or `modules/*/web`:

1. It declares all three scripts (`REQUIRED_SCRIPTS`) as non-empty strings.
2. It is matched by a `packages:` glob in `pnpm-workspace.yaml`. `pnpm -r` never
   visits a directory outside the workspace, so a package missing from that list is
   skipped just as silently as a missing script.

Every literal (non-wildcard) `pnpm-workspace.yaml` entry is also expected to name an
existing package, so a stale entry is reported rather than left to mislead. A wildcard
such as `modules/*/web` may match nothing yet: a module's web package arrives later.

Runs under the system python3 (3.9-compatible, no third-party deps), mirroring the
other gate scripts: the `pnpm-workspace.yaml` reader handles only the flat `packages:`
list this repository uses (plain and `!`-negated globs), not general YAML.
Self-test: `main()` always builds a scratch repository tree with one complete package,
one missing a script, one outside the workspace globs, and one stale glob, and
confirms each fault is reported, before checking the real tree.
"""

from __future__ import annotations

import fnmatch
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SCRIPTS = ("lint", "typecheck", "test")
_PACKAGE_GLOBS = ("apps/*", "packages/*", "modules/*/web")


def _workspace_globs(repo_root: Path) -> tuple[list[str], list[str]]:
    """(include globs, exclude globs) from `pnpm-workspace.yaml`'s `packages:` list."""
    path = repo_root / "pnpm-workspace.yaml"
    include: list[str] = []
    exclude: list[str] = []
    in_packages = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith((" ", "\t", "-")):
            in_packages = line.strip() == "packages:"
            continue
        stripped = line.strip()
        if in_packages and stripped.startswith("-"):
            glob = stripped[1:].strip().strip("'\"").rstrip("/")
            if glob.startswith("!"):
                exclude.append(glob[1:])
            else:
                include.append(glob)
    return include, exclude


def _candidate_packages(repo_root: Path) -> list[str]:
    found: set[str] = set()
    for glob in _PACKAGE_GLOBS:
        for directory in repo_root.glob(glob):
            if (directory / "package.json").is_file():
                found.add(directory.relative_to(repo_root).as_posix())
    return sorted(found)


def _matches(relative: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(relative, glob) for glob in globs)


def check(repo_root: Path = ROOT) -> list[str]:
    findings: list[str] = []
    try:
        include, exclude = _workspace_globs(repo_root)
    except OSError as error:
        return [f"pnpm-workspace.yaml could not be read: {error}"]
    if not include:
        findings.append("pnpm-workspace.yaml declares no packages: globs")
    candidates = _candidate_packages(repo_root)
    for relative in candidates:
        if not _matches(relative, include) or _matches(relative, exclude):
            findings.append(
                f"{relative}: package.json is not in pnpm-workspace.yaml, so "
                "`pnpm -r` never runs it"
            )
        try:
            manifest = json.loads(
                (repo_root / relative / "package.json").read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            findings.append(f"{relative}/package.json could not be read: {error}")
            continue
        scripts = manifest.get("scripts") if isinstance(manifest, dict) else None
        scripts = scripts if isinstance(scripts, dict) else {}
        for name in REQUIRED_SCRIPTS:
            value = scripts.get(name)
            if not isinstance(value, str) or not value.strip():
                findings.append(
                    f"{relative}/package.json: no '{name}' script, so "
                    f"`pnpm -r {name}` silently skips this package"
                )
    for glob in include:
        if any(char in glob for char in "*?["):
            continue  # a wildcard may legitimately match nothing yet
        if not any(fnmatch.fnmatchcase(c, glob) for c in candidates):
            findings.append(
                f"pnpm-workspace.yaml: glob '{glob}' matches no package with a "
                "package.json under apps/*, packages/* or modules/*/web"
            )
    return findings


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _self_test() -> str | None:
    full = json.dumps({"scripts": {name: "true" for name in REQUIRED_SCRIPTS}})
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        _write(
            repo / "pnpm-workspace.yaml",
            "packages:\n  - apps/web\n  - modules/*/web\n  - packages/web-contract\n"
            "  - packages/retired\n  - tools/*\n",
        )
        _write(repo / "apps/web/package.json", full)
        _write(repo / "packages/web-contract/package.json", full)
        _write(
            repo / "modules/scratch/web/package.json",
            json.dumps({"scripts": {"lint": "eslint .", "typecheck": "tsc"}}),
        )
        _write(repo / "packages/stray/package.json", full)
        findings = check(repo)
        joined = "\n".join(findings)
        expectations = {
            "missing test script": "modules/scratch/web/package.json: no 'test'",
            "package outside the workspace": "packages/stray: package.json is not in",
            "stale workspace glob": "glob 'packages/retired' matches no package",
        }
        for label, needle in expectations.items():
            if needle not in joined:
                return f"self-test FAILED: {label} went unreported (got {findings})"
        for clean in ("apps/web", "packages/web-contract"):
            if any(f.startswith((clean + "/", clean + ":")) for f in findings):
                return f"self-test FAILED: complete package {clean} was flagged"
        if "glob 'tools/*'" in joined:
            return "self-test FAILED: a wildcard glob was reported as stale"
    return None


def main() -> int:
    self_test_failure = _self_test()
    if self_test_failure is not None:
        print(self_test_failure, file=sys.stderr)
        return 1
    findings = check()
    for finding in findings:
        print(finding, file=sys.stderr)
    if not findings:
        packages = _candidate_packages(ROOT)
        print(
            "Workspace-script checks passed (self-test verified a missing script, an "
            f"out-of-workspace package and a stale glob are caught): {len(packages)} "
            f"package(s) ({', '.join(packages)}) declare "
            f"{'/'.join(REQUIRED_SCRIPTS)} and are in pnpm-workspace.yaml."
        )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
