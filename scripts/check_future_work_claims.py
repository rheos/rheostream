#!/usr/bin/env python3
"""Find prose that says work does not exist yet, so a run can check it at close-out.

This repository is built in sequenced runs, and that style produces docstrings which
describe future work: "belongs to the run that builds X", "nothing here writes Y". Every
run that lands some of X silently falsifies them, and until this script nothing checked
it (issue #73).

Those sites are invisible to every other gate at once, for three independent reasons:
they are not in the landing run's diff, so no diff review sees them; they are not in its
declared paths, so its coders are correctly forbidden to edit them; and they are not in
its own artifacts, so the artifact sweeps do not reach them.

**This is a reporting tool, not a test.** It cannot know whether a claim is still true —
that needs a human who knows what the run just landed. It is deliberately not wired into
``make check``: a claim that is true today would fail CI tomorrow for reasons no commit
in that build caused. Run it at close-out, read each hit, and correct or record it.

**It keys on reference to a run, not on absence generally.** A sentence that defers to
a run is the one that expires, because runs happen. "Nothing in this package imports X"
is a structural invariant and is deliberately not matched; an earlier draft that matched
absence generally returned 27 hits of which about four were real, and a guard whose hits
are mostly false buries the real ones.

**The match is whitespace-flattened.** A line-oriented grep misses a claim that wraps
across a line break, which produced three confirmed false negatives in run 0c2 alone —
one sweep found two sites where the flattened form found four. Prose wraps; the check
must not care.

Usage:
    python3 scripts/check_future_work_claims.py [--json] [PATH ...]

Exit status is 0 whether or not it finds anything. It reports; it does not judge.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

#: Phrasings that defer a behaviour to a LATER RUN. This is deliberately narrow.
#:
#: The first draft matched absence generally — "nothing here", "future", "not yet" — and
#: returned 27 hits of which roughly four were real, because it could not tell a
#: *temporal* claim from a *structural* one. "Nothing in this package imports
#: ``rheo_core.operations``" is an invariant that is true by design and stays true;
#: "nothing in this run writes an outbox row" is a claim with an expiry date. A guard
#: whose hits are mostly false buries the real ones, which is exactly how run 0c2's
#: first event-clock check failed: 39 hits, all wrong.
#:
#: So the invariant this check keys on is **reference to a run**. A sentence that names
#: a run, or defers to one, is the sentence that goes stale, because runs happen. A
#: structural rule does not mention runs at all.
PATTERNS: tuple[tuple[str, str], ...] = (
    (r"belongs? to (?:the |a |later |future )*runs?\b", "defers to a later run"),
    (r"belongs? to the runs? that build", "defers to a later run"),
    (r"(?:nothing|none of this) in this run\b", "asserts this run does nothing"),
    (
        r"this run (?:does not|doesn'?t|never) (?:build|write|fan|deliver|ship|add)",
        "asserts this run does not",
    ),
    (r"until (?:a|the) later run", "defers to a later run"),
    (
        r"(?:left|deferred) to (?:a|the) (?:later|future|following) run",
        "defers to a later run",
    ),
    (r"the run that builds", "defers to a later run"),
    (r"a later run (?:will|adds|builds|ships)", "defers to a later run"),
)

DEFAULT_ROOTS = ("packages", "apps", "runtimes", "scripts", "docs", "README.md")
TEXT_SUFFIXES = {".py", ".md", ".ts", ".tsx", ".toml", ".yaml", ".yml"}

#: This file quotes the phrasings it hunts for, in its own docstring and in PATTERNS, so
#: scanning itself produces three guaranteed hits that are noise by construction. A
#: detector must not be able to match text its own author wrote — the same rule that
#: sank two watchers in run 0c2, arriving here a third time.
SELF = Path(__file__).resolve()


def tracked_files(roots: tuple[str, ...]) -> list[Path]:
    """Every tracked text file under ``roots``. Tracked only: untracked scratch and
    ignored build output are not claims the repository makes."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "--", *roots],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [
        path
        for p in out.split("\n")
        if p and (path := Path(p)).suffix in TEXT_SUFFIXES and path.resolve() != SELF
    ]


def flatten(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace, keeping a map from flattened offset back to line number."""
    flat_chars: list[str] = []
    line_of: list[int] = []
    line = 1
    prev_space = False
    for ch in text:
        if ch == "\n":
            line += 1
        if ch.isspace():
            if not prev_space:
                flat_chars.append(" ")
                line_of.append(line)
            prev_space = True
            continue
        prev_space = False
        flat_chars.append(ch)
        line_of.append(line)
    return "".join(flat_chars), line_of


def scan(path: Path) -> list[dict[str, object]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    flat, line_of = flatten(text)
    hits: list[dict[str, object]] = []
    for pattern, why in PATTERNS:
        for m in re.finditer(pattern, flat, re.IGNORECASE):
            start = m.start()
            hits.append(
                {
                    "file": str(path),
                    "line": line_of[start] if start < len(line_of) else 0,
                    "why": why,
                    "quote": flat[max(0, start - 60) : start + 110].strip(),
                }
            )
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("paths", nargs="*", default=None)
    args = ap.parse_args()

    roots = tuple(args.paths) if args.paths else DEFAULT_ROOTS
    seen: set[tuple[object, object]] = set()
    hits = []
    for f in tracked_files(roots):
        for h in scan(f):
            # Two patterns can match one sentence; report the sentence once.
            key = (h["file"], h["line"])
            if key in seen:
                continue
            seen.add(key)
            hits.append(h)
    hits.sort(key=lambda h: (h["file"], h["line"]))

    if args.json:
        json.dump(hits, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if not hits:
        print(
            "No future-work claims found. That is a real nil result: the match is "
            "whitespace-flattened, so wrapped prose was checked too."
        )
        return 0

    print(f"{len(hits)} future-work claim(s) to check against what this run landed.")
    print("Each may still be true. Read it, then correct or record it.\n")
    current = None
    for h in hits:
        if h["file"] != current:
            current = h["file"]
            print(f"  {current}")
        print(f"    :{h['line']}  ({h['why']})")
        print(f"      …{h['quote']}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
