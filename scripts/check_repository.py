#!/usr/bin/env python3
"""Check repository paths and local docs; this is not a content/secret scanner."""

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]

PRIVATE_PATHS = (
    "AGENTS.md",
    "AGENTS.override.md",
    "CLAUDE.md",
    "CLAUDE.local.md",
    "modules/example/AGENTS.md",
    "modules/example/AGENTS.override.md",
    "modules/example/CLAUDE.md",
    "modules/example/CLAUDE.local.md",
    ".codex/config.toml",
    ".claude/settings.json",
    ".claude/rules/private.md",
    "modules/example/.codex/config.toml",
    "modules/example/.claude/settings.json",
    ".mcp.json",
    "modules/example/.mcp.json",
    ".rheo-local/workspaces/demo/profile.json",
    ".rheo-local/workspaces/demo/private-prompt.md",
    ".rheo-local/uploads/evidence.pdf",
    ".rheo-local/transcripts/session.jsonl",
    ".rheo-local/exports/workspace.csv",
    ".rheo-local/backups/database.sql",
    ".rheo-local/memory/index.bin",
    ".secrets/token.json",
    "rheo.local.yaml",
    ".env",
    ".env.local",
    ".env.production",
    ".envrc",
    ".envrc.local",
    "apps/example/.env",
    "apps/example/.env.production",
    "workspace.db",
    "workspace.db-wal",
    "workspace.db-shm",
    "workspace.db-journal",
    "work.sqlite",
    "work.sqlite-wal",
    "work.sqlite-shm",
    "work.sqlite-journal",
    "work.sqlite3",
    "work.sqlite3-wal",
    "work.sqlite3-shm",
    "work.sqlite3-journal",
    "modules/example/cache/workspace.db",
    "service.log",
    "runtime/agent.log",
    ".bureau/runs/example/state.json",
    # Build, dependency, and test caches the 0a tree generates.
    ".venv/pyvenv.cfg",
    "node_modules/.bin/pnpm",
    "apps/web/.next/BUILD_ID",
    "apps/web/next-env.d.ts",
    ".ruff_cache/CACHEDIR.TAG",
    ".mypy_cache/CACHEDIR.TAG",
    ".pytest_cache/CACHEDIR.TAG",
    # 0b1: checkout-local operator config and secret files under .rheo-local/.
    ".rheo-local/config/deployment.toml",
    ".rheo-local/secrets/cluster/primary-dsn",
)

PUBLIC_PATHS = (
    "examples/AGENTS.example.md",
    "examples/CLAUDE.example.md",
    ".env.example",
    ".env.production.example",
    "apps/example/.env.example",
    "apps/example/.env.production.example",
    "packages/core/workspaces/index.ts",
    "modules/leads/schema.json",
    "modules/leads/migrations/001.sql",
    "packs/freelance-software/defaults.yaml",
    "tests/fixtures/synthetic-person.json",
    "tests/fixtures/synthetic-leads.csv",
    # 0b2: the shared routing fixture pytest and vitest are both proven against
    # (C6 creates the files; declaring them public is C10's, the same reasoning
    # 0b1's C5 used for not claiming a file 0b1 never creates).
    "tests/fixtures/routing/path-mode.json",
    "tests/fixtures/routing/subdomain-mode.json",
    "tests/fixtures/routing/expected-urls.json",
    "docs/example.pdf",
    "README.md",
    # Public build and config files 0a's harness adds (asserted not ignored).
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "Makefile",
    "packages/contracts/pyproject.toml",
    "packages/core/pyproject.toml",
    "apps/core/pyproject.toml",
    "apps/worker/pyproject.toml",
    "apps/cli/pyproject.toml",
    "apps/mcp/pyproject.toml",
    "pnpm-workspace.yaml",
    "package.json",
    "pnpm-lock.yaml",
    ".nvmrc",
    "apps/web/package.json",
    "deploy/compose.yaml",
    "Dockerfile",
    # 0b1: the packaged settings defaults and the core Alembic chain's first
    # revision are public source, not runtime data.
    "packages/core/src/rheo_core/config/defaults.toml",
    "packages/core/src/rheo_core/migrations/core/versions/0001_core_schema.py",
    # 0c0: each chain's second revision, public source by the same reasoning.
    "packages/core/src/rheo_core/migrations/core/versions/0002_durable_work.py",
    "packages/core/src/rheo_core/migrations/control/versions/0002_work_index.py",
)


def git(*args, input_text=None):
    result = subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", *args],
        cwd=ROOT,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip() or "Git command failed")
    return result.stdout


def ignored(paths):
    if not paths:
        return set()
    output = git(
        "check-ignore",
        "--no-index",
        "-z",
        "--stdin",
        input_text="\0".join(paths) + "\0",
    )
    return set(output.rstrip("\0").split("\0")) if output else set()


def check():
    errors = []
    tracked = git("ls-files", "-z").rstrip("\0").split("\0")
    tracked = [path for path in tracked if path]
    if not tracked:
        return ["No tracked files found; run this check in the repository."]

    for path in sorted(set(PRIVATE_PATHS) - ignored(PRIVATE_PATHS)):
        errors.append(f"Private path is not ignored: {path}")
    for path in sorted(ignored(PUBLIC_PATHS)):
        errors.append(f"Public source/example path is ignored: {path}")
    for path in sorted(ignored(tracked)):
        errors.append(f"Ignored artifact is already tracked: {path}")

    tracked_set = set(tracked)
    for relative in tracked:
        if not relative.endswith(".md"):
            continue
        source = ROOT / relative
        if not source.is_file():
            errors.append(f"Tracked Markdown file is missing: {relative}")
            continue
        text = source.read_text(encoding="utf-8")
        # Repository docs currently use inline links. Ignore fenced examples.
        text = re.sub(r"(?ms)^```[^\n]*\n.*?^```\s*$", "", text)
        for link in re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", text):
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = (source.parent / unquote(parsed.path)).resolve()
            if not target.is_relative_to(ROOT):
                errors.append(f"Link escapes repository: {relative} -> {link}")
                continue
            target_relative = target.relative_to(ROOT).as_posix()
            # A local file existing only on the author's machine is not a valid
            # public link. Directories must contain at least one tracked file.
            present = target_relative in tracked_set or (
                target.is_dir()
                and any(
                    p.startswith(target_relative.rstrip("/") + "/") for p in tracked
                )
            )
            if not present:
                errors.append(f"Local link is not tracked: {relative} -> {link}")

    if not errors:
        print(
            f"Repository checks passed: {len(tracked)} tracked files; "
            f"{len(PRIVATE_PATHS)} private and {len(PUBLIC_PATHS)} public path cases; "
            "local Markdown file targets resolve."
        )
        print("Review content separately for secrets and personal/client data.")
    return errors


if __name__ == "__main__":
    try:
        findings = check()
    except (OSError, RuntimeError, UnicodeError, ValueError) as error:
        print(f"Repository check failed: {error}", file=sys.stderr)
        sys.exit(1)
    for finding in findings:
        print(finding, file=sys.stderr)
    sys.exit(1 if findings else 0)
