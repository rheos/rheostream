#!/usr/bin/env python3
"""Legacy-product-name denylist scan of every tracked file's path and text
(criterion 34).

This repository is a clean public rewrite of a family of internal tools (`docs/ideas/
rheo-stream-idea.md`). Nothing in the shipped surface — code, docs, fixtures, file
names, commit history going forward — should carry the predecessors' names: `MOL`,
`MOT`/`M.O.T.`, `Ministry`, or the generalized `Upwork`-prefixed identifier family the
idea document's guardrail 1 names. A name that leaks through a fixture, a code
comment, a doc, or a file path is not merely untidy in a public repo — it is the exact
kind of private-context leak `scripts/check_repository.py` cannot see.

Runs under the system python3 (3.9-compatible, no third-party deps), mirroring
`scripts/check_routing_literals.py`.

**Paths and contents.** Every tracked path is scanned with the same rules as file
text, so a directory or file named after a predecessor (`modules/mot/…`,
`upwork_jobs.json`) fails even when its content is clean. Paths are scanned for every
tracked file, including the historical-docs exceptions and declared binaries: the
exception list covers what a document says, not what the tree is called.

**Tokenization, not substring search.** A denylist word must be a whole identifier
or prose word, not a substring anywhere. The text is split into alphanumeric runs
(broken at every non-alphanumeric character), and within each run a denylist word
(`mol`, `mot`, `ministry`) matches only where a token begins and ends:

- lower-case (`mot`) at the run's start or after a digit, ending before anything
  but another lower-case letter (`mot`, `mot2`, `motView`; not `remote`, `motion`);
- title-case (`Mot`) wherever it starts a camelCase hump, same ending rule
  (`MotView`, `Mots`; not `Motion`);
- upper-case (`MOT`) at the run's start or after a lower-case letter or digit,
  ending at the run's end, a digit, or the start of the next camelCase hump
  (`MOT`, `MOTView`; not `EMOTION`, `MOTOR`, `MOLECULE`).

Each form may carry a plural or version suffix — `s`, or an optional `v` plus digits
(`MOTs`, `MOLs`, `MOTv2`, `mot2`). This is what keeps `DedupWorkspace` and prose such
as `remote`/`motion`/`molecule`/`emotion`/`model` out of the denylist while still
catching every suffixed form. A naive `re.search(r"mot", text, re.I)` would flag all
the near-misses; this scan does not.

**The dotted form `M.O.T.` is checked separately**, case-insensitively, with or
without the final dot — dots are run boundaries for the word scan above, so `M.O.T`
would otherwise tokenize to the single letters `m`, `o`, `t`.

**`upwork` is checked as a compound segment, not a bare word.** The prose "sourced
from Upwork" (`docs/eval/bureau-vs-langgraph.md`) must pass: it names the real
platform, which is a legitimate thing for this repository's docs to say. What must
NOT pass is `upwork` used as one segment of a compound — as the prefix
(`upwork_jobs`, `upwork-thing`, `upwork/jobs`, `upwork.jobs`, `UpworkX`) or as an
infix segment (`sync_upwork_data`, `handleUpworkPayload`). The rule: scan
compound runs (`[A-Za-z0-9_./-]+`), split each run into segments at `_`, `-`, `.`
and `/` and at camelCase humps (`_segments()`), and flag the run when `upwork` is one
of two or more segments. A bare `Upwork` run, including one ending a sentence
(`Upwork.`, whose trailing dot leaves an empty piece rather than a second segment),
is one segment and never flagged. Staying segment-based — never falling back to a
raw substring search — is what keeps `DedupWorkspace` a near-miss even though the
characters `u`,`p`,`w`,`o`,`r`,`k` appear contiguously inside it: hump-splitting
gives `["Dedup", "Workspace"]`, and neither segment equals `upwork`.

Exceptions are whole files, whose content is skipped (not line-by-line): the run's
own historical docs that legitimately discuss the predecessors by name. The script's
own content is also skipped, since its docstring and self-test necessarily spell out
every denylisted form.

**Undeclared binaries are a gate failure, not a silent skip.** Every tracked file
that fails to decode as UTF-8 is reported unless its path is declared in the shared
`scripts/tracked_binaries.json` (read by this script and by
`tests/test_absent_behaviour.py` + `tests/test_allowlist_variable_absent.py`, so the
one declaration and its honesty checks live in one place). A file that fails to
decode and is NOT on that list is invisible to every regex above.

Self-test (anti-vacuity, mirroring `check_routing_literals.py`'s `_self_test`):
`main()` always plants each positive form and each near-miss against `check()` and
`check_paths()` directly, and separately plants an undeclared non-UTF-8 file against
`_classify()` — no filesystem or git involved — before scanning the real tree, so a
scan that stopped being able to catch anything fails loudly on every invocation.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The one shared declaration of tracked-but-binary files, read by this script and by
# tests/test_absent_behaviour.py + tests/test_allowlist_variable_absent.py — a data
# file rather than a Python module, so a plain `python3 scripts/check_legacy_names.py`
# invocation and a pytest-collected test file can both resolve it by the same
# repo-root-relative path with no sys.path/import-mode coupling between the two.
TRACKED_BINARIES_PATH = ROOT / "scripts" / "tracked_binaries.json"

# Whole-file content exceptions (see module docstring): historical docs that
# legitimately name a predecessor. Relative, POSIX-style, matching `git ls-files`.
EXCEPT_FILES = frozenset(
    {
        "docs/ideas/rheo-stream-idea.md",
        "docs/requirements/requirements-and-scope.md",
        "docs/notes/1a0-module-contract/spec.md",
        "docs/notes/1a0-module-contract/plan.md",
    }
)

# The script's own path, relative to ROOT — its content is excluded because its
# docstring and self-test necessarily spell out every denylisted form. Its path is
# still scanned.
SELF_PATH = "scripts/check_legacy_names.py"

# Alphanumeric runs: broken at every non-alphanumeric character.
_IDENT_RUN = re.compile(r"[A-Za-z0-9]+")

# A denylist word inside one alphanumeric run, in each of the three case shapes an
# identifier or prose word takes (see module docstring for the boundary rules).
_LEGACY_IN_RUN = re.compile(
    r"(?<![A-Za-z])(?P<lower>mot|mol|ministry|ministries)(?:s|v?\d+)?(?![a-z])"
    r"|(?P<title>Mot|Mol|Ministry|Ministries)(?:s|v?\d+)?(?![a-z])"
    r"|(?<![A-Z])(?P<upper>MOT|MOL|MINISTRY|MINISTRIES)(?:[sS]|[vV]?\d+)?"
    r"(?=$|[^A-Za-z]|[A-Z][a-z])"
)

_DOTTED = re.compile(r"(?i)(?<![a-z0-9])m\.o\.t(?![a-z0-9])")

# camelCase hump split, used for the upwork compound segments.
_HUMP = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")

# Compound runs for the `upwork` check: `_`, `-`, `.` and `/` stay inside the run
# (unlike `_IDENT_RUN` above), because a compound is exactly what this check is for.
_COMPOUND_RUN = re.compile(r"[A-Za-z0-9_./-]+")
_SEPARATOR = re.compile(r"[_./-]")

_UPWORK = "upwork"


def _legacy_words(text: str) -> set[str]:
    words: set[str] = set()
    for run in _IDENT_RUN.findall(text):
        for match in _LEGACY_IN_RUN.finditer(run):
            words.add(match.group(0).lower())
    return words


def _segments(run: str) -> list[str]:
    """Split one compound run into its pieces: explicit `_`/`-`/`.`/`/` separators
    first, then camelCase humps within each piece."""
    segments: list[str] = []
    for piece in _SEPARATOR.split(run):
        if piece:
            segments.extend(_HUMP.findall(piece))
    return segments


def _upwork_compounds(text: str) -> set[str]:
    """Runs where `upwork` is one of two or more segments (see module docstring)."""
    hits: set[str] = set()
    for match in _COMPOUND_RUN.finditer(text):
        run = match.group(0)
        segments = _segments(run)
        if len(segments) < 2:
            continue
        if any(segment.lower() == _UPWORK for segment in segments):
            hits.add(run)
    return hits


def _text_findings(text: str) -> list[str]:
    """Descriptions of every legacy-name form in `text` (no path attached)."""
    findings = [f"legacy name '{word}'" for word in sorted(_legacy_words(text))]
    if _DOTTED.search(text):
        findings.append("legacy name 'M.O.T.'")
    for compound in sorted(_upwork_compounds(text)):
        findings.append(f"legacy source-product-prefix compound '{compound}'")
    return findings


def check(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """`pairs` is (relative path, file text). Returns (path, description) findings
    over the text only, skipping the historical-docs exceptions.

    A pure function over given text — no filesystem or git access — so the self-test
    exercises exactly this logic without touching the real tree.
    """
    findings: list[tuple[str, str]] = []
    for relative, text in pairs:
        if relative in EXCEPT_FILES or relative == SELF_PATH:
            continue
        for description in _text_findings(text):
            findings.append((relative, description))
    return findings


def check_paths(relatives: Iterable[str]) -> list[tuple[str, str]]:
    """The same rules over every tracked path itself (see module docstring)."""
    findings: list[tuple[str, str]] = []
    for relative in relatives:
        for description in _text_findings(relative):
            findings.append((relative, f"path contains {description}"))
    return findings


def _known_binary_paths() -> frozenset[str]:
    """The shared declaration's keys (see `TRACKED_BINARIES_PATH` above) — every
    tracked path this scan is allowed to skip because it cannot be UTF-8 text."""
    data = json.loads(TRACKED_BINARIES_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"{TRACKED_BINARIES_PATH} must be a JSON object")
    return frozenset(data.keys())


def _classify(
    relatives_and_bytes: Iterable[tuple[str, bytes]], known_binaries: frozenset[str]
) -> tuple[list[tuple[str, str]], list[str]]:
    """Split raw tracked-file bytes into decodable (path, text) pairs and the
    relative paths that fail to decode as UTF-8 and are not declared binaries.

    A pure function over given bytes — no filesystem access — so the self-test
    exercises exactly this logic; only `_tracked_files()` below reads files.
    """
    pairs: list[tuple[str, str]] = []
    undeclared_binaries: list[str] = []
    for relative, raw in relatives_and_bytes:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            if relative not in known_binaries:
                undeclared_binaries.append(relative)
            continue
        pairs.append((relative, text))
    return pairs, undeclared_binaries


def _tracked_files() -> tuple[list[str], list[tuple[str, str]], list[str]]:
    """Every tracked path, every tracked file's (relative path, text), and any
    undeclared binary paths (see `_classify()`)."""
    output = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    relatives = [p for p in output.rstrip("\0").split("\0") if p]
    known_binaries = _known_binary_paths()
    relatives_and_bytes: list[tuple[str, bytes]] = []
    for relative in relatives:
        if relative == SELF_PATH:
            continue
        try:
            raw = (ROOT / relative).read_bytes()
        except OSError:
            continue
        relatives_and_bytes.append((relative, raw))
    pairs, undeclared = _classify(relatives_and_bytes, known_binaries)
    return relatives, pairs, undeclared


def _self_test() -> str | None:
    """Plant every positive form and every near-miss in text and in paths, and
    confirm the scan tells them apart; separately plant an undeclared non-UTF-8 file
    and confirm `_classify()` reports it. Returns `None` on success, or a diagnostic.
    """
    positives = {
        "planted/mot-lowercase.txt": "identifier = mot_x\n",
        "planted/mot-caps.txt": "CONST = MOT\n",
        "planted/mot-dotted.txt": "Filed historically under M.O.T. review.\n",
        "planted/mot-dotted-no-final-dot.txt": "Filed under M.O.T review.\n",
        "planted/mot-camel.tsx": "export const MotView = () => null;\n",
        "planted/mot-caps-camel.tsx": "export const MOTView = () => null;\n",
        "planted/mot-path.txt": "route = '/mot/'\n",
        "planted/mot-plural.txt": "Both MOTs agreed.\n",
        "planted/mol-plural.txt": "Two MOLs ran side by side.\n",
        "planted/mot-version.txt": "The MOTv2 cutover.\n",
        "planted/mot-digit.txt": "service = mot2\n",
        "planted/ministry-plural.txt": "Every Ministries table.\n",
        "planted/upwork-compound.txt": "table = upwork_jobs\n",
        "planted/upwork-underscore-infix.txt": "queue = sync_upwork_data\n",
        "planted/upwork-camel-infix.tsx": "function handleUpworkPayload() {}\n",
        "planted/upwork-slash.txt": "route = '/upwork/jobs'\n",
        "planted/upwork-dot.txt": "key = settings.upwork.token\n",
    }
    near_misses = {
        "planted/near-miss-words.txt": (
            "remote motion molecule emotion model motor Molly moth.\n"
        ),
        "planted/near-miss-caps.txt": "REMOTE EMOTION MOTOR MOLECULE MOTH\n",
        "planted/near-miss-upwork-prose.txt": (
            "The maintainer's funnel is scored against Upwork job postings.\n"
        ),
        "planted/near-miss-upwork-sentence-end.txt": "Jobs were sourced from Upwork.\n",
        "planted/near-miss-upwork-substring.txt": "class DedupWorkspace:\n    pass\n",
        "planted/near-miss-dotted.txt": "see form.o.t and atom.o.tx\n",
    }
    findings = check([*positives.items(), *near_misses.items()])
    flagged = {path for path, _ in findings}

    missing = sorted(p for p in positives if p not in flagged)
    if missing:
        return f"self-test FAILED: planted positive forms went uncaught: {missing}"
    wrongly_flagged = sorted(p for p in near_misses if p in flagged)
    if wrongly_flagged:
        return (
            "self-test FAILED: near-miss text was flagged (tokenizer or compound "
            f"check broke): {wrongly_flagged}"
        )

    # The historical-docs exceptions skip content only.
    excepted = next(iter(sorted(EXCEPT_FILES)))
    if check([(excepted, "CONST = MOT\n")]):
        return "self-test FAILED: an exception-listed doc's content was flagged"

    # Paths are scanned with the same rules.
    planted_paths = [
        "modules/mot/src/handler.py",
        "docs/notes/MOTv2-cutover.md",
        "tests/fixtures/upwork_jobs.json",
        "apps/web/src/upwork/view.tsx",
    ]
    clean_paths = [
        "apps/web/src/remote/motion.tsx",
        "tests/postgres/test_memory_dedup.py",
        "docs/eval/bureau-vs-langgraph.md",
    ]
    path_flagged = {path for path, _ in check_paths([*planted_paths, *clean_paths])}
    missing_paths = sorted(p for p in planted_paths if p not in path_flagged)
    if missing_paths:
        return f"self-test FAILED: planted legacy paths went uncaught: {missing_paths}"
    wrong_paths = sorted(p for p in clean_paths if p in path_flagged)
    if wrong_paths:
        return f"self-test FAILED: clean paths were flagged: {wrong_paths}"

    # An undeclared file that fails to decode as UTF-8 must be reported.
    known_binaries = frozenset({"planted/declared-binary.bin"})
    classify_pairs, undeclared = _classify(
        [
            ("planted/text-file.txt", b"identifier = mot_x\n"),
            ("planted/declared-binary.bin", b"\xff\xfe\x00binary"),
            ("planted/undeclared-binary.bin", b"\xff\xfe\x00binary"),
        ],
        known_binaries,
    )
    if "planted/undeclared-binary.bin" not in undeclared:
        return (
            "self-test FAILED: an undeclared non-UTF-8 file went uncaught by "
            "_classify() (should be a gate failure, not a silent skip)"
        )
    if "planted/declared-binary.bin" in undeclared:
        return "self-test FAILED: a declared known binary was reported as undeclared"
    if not any(path == "planted/text-file.txt" for path, _ in classify_pairs):
        return "self-test FAILED: a genuine text file was dropped by _classify()"
    return None


def main() -> int:
    self_test_failure = _self_test()
    if self_test_failure is not None:
        print(self_test_failure, file=sys.stderr)
        return 1
    try:
        relatives, pairs, undeclared_binaries = _tracked_files()
        findings = check_paths(relatives) + check(pairs)
    except (
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        UnicodeError,
        ValueError,
    ) as error:
        print(f"Legacy-name check failed: {error}", file=sys.stderr)
        return 1
    for relative in undeclared_binaries:
        findings.append(
            (
                relative,
                "is not valid UTF-8 and is not declared in "
                "scripts/tracked_binaries.json — this scan cannot see it",
            )
        )
    for relative, message in sorted(findings):
        print(f"{relative}: {message}", file=sys.stderr)
    if not findings:
        print(
            "Legacy-name checks passed (self-test verified the scan still catches "
            f"planted forms): {len(relatives)} tracked path(s) and their text carry "
            "no legacy name or upwork compound outside the historical-docs "
            "exception list, and every tracked file either decoded as UTF-8 or is "
            "a declared binary."
        )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
