#!/usr/bin/env python3
"""Migration-output path gate and private-denylist leak scan (FR 18, AC 17).

The 1b migration-groundwork run's own preamble requires every real predecessor
artifact (a harvested ledger, an entity-pair extract, a comparison, a forecast, a
measurement, a hand-written report) to live only under the private root
(``rheo-stream-workspace/private/``), never in this public repository. This script
is the mechanical, build-time-checkable half of that rule, in two independent
pieces:

1. **Path gate** (always active; the only part CI runs). Fails when a tracked
   path's basename matches a migration-output filename pattern — the shapes real
   predecessor artifacts take (``*ledger*.csv``, ``inventory*.json``,
   ``ground-truth*``, and so on; see ``_PATH_GATE_PATTERNS`` for the exact list),
   plus a handful of exact hand-written report names a wayward relative path could
   otherwise land in the tracked tree.
2. **Leak scan** (only runs when the environment variable
   ``RHEO_PRIVATE_DENYLIST`` names a file outside the repository; CI never sets
   it). Case-insensitive, whitespace-normalized substring match of each denylist
   line against tracked content, in one of five modes selected by CLI flag:

   - default (no flag): every tracked text file at ``HEAD``.
   - ``--diff <range>`` (two-dot range, e.g. ``origin/main..HEAD``): the *added*
     lines of every commit in the range, scanned per commit (never only the net
     diff between the range's endpoints, so a leak one commit introduces and a
     later one reverts is still caught), plus the staged diff. A merge commit is
     scanned by its first-parent added lines only.
   - ``--commits <range>``: every commit message in the range.
   - ``--text <file>...``: arbitrary files (used for a gated issue/PR title or
     body file before it reaches GitHub).
   - ``--triage-exempt <ref>`` (with a required ``--out <dir>``): the identical
     matcher as the default mode, run over ``<ref>`` instead of ``HEAD``, but
     reporting which denylist *lines* already match something public at that ref
     (written to ``<dir>/denylist-exempt.txt``) rather than which files matched.
     This is Phase 1R's triage step and Phase 9's re-triage; it shares
     :func:`match_denylist` with every other mode so the exemption check can never
     silently diverge from the leak scan it is exempting against.

   **Output discipline (load-bearing):** on a match, only
   ``<path or commit sha>:<line number>: denylist line <n>`` is printed. The
   matched text and the denylist line's own content are never printed — printing
   either would defeat the mechanism by leaking the thing it caught into a
   terminal or log capture that this very script would then have to catch again.

Runs under the system python3 (3.9-compatible, no third-party dependency at
import time; ``--triage-exempt`` lazily imports
``rheo_recallatron.migration.private_paths`` only when that mode is used, since
only Phase 1R/9 invoke it, always through the project's own virtualenv). Mirrors
``scripts/check_fixture_provenance.py``'s shape: every check function is pure
(operates on given paths/text/needles, not on the filesystem) so the self-test
exercises the real logic without touching git; only the ``_git_*`` and
``_tracked_text_at_ref`` helpers and ``main()`` do I/O.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DENYLIST_VARIABLE = "RHEO_PRIVATE_DENYLIST"

# --- check 1: the path gate --------------------------------------------------------

_PATH_GATE_PATTERNS: tuple[str, ...] = (
    "*ledger*.csv",
    "*pairs*.csv",
    "inventory*.json",
    "*extract*.jsonl",
    "comparison*.json",
    "ground-truth*",
    "*forecast*.md",
    "measurement*",
    "reading-*.json",
    "migration-report*",
    "dry-run-*.json",
    "denylist*.txt",
    # w2: the hand-written private reports later prompts write, exact names only.
    "report.md",
    "compat-manifest.md",
    "producer-register.md",
    "REHARVEST.md",
    "SCRATCH.md",
)


def check_path_gate(tracked_relatives: Iterable[str]) -> list[str]:
    findings: list[str] = []
    for relative in tracked_relatives:
        basename = Path(relative).name
        for pattern in _PATH_GATE_PATTERNS:
            if fnmatch.fnmatchcase(basename, pattern):
                findings.append(
                    f"{relative}: tracked path matches migration-output pattern "
                    f"{pattern!r}"
                )
                break
    return findings


# --- check 2: the shared denylist matcher --------------------------------------------
#
# Every leak-scan mode below (default, --diff, --commits, --text, --triage-exempt)
# calls this one function and only this function. That is deliberate: it is what
# lets the self-test's "--triage-exempt agrees with the default mode" assertion
# prove the two modes share a matcher, rather than merely happening to agree today.


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).casefold()


def _denylist_needles(lines: Iterable[str]) -> list[str]:
    """Normalized, non-blank denylist lines, in file order (1-based externally)."""
    return [_normalize(line) for line in lines if line.strip()]


def match_denylist(text: str, needles: Sequence[str]) -> list[tuple[int, int]]:
    """Every ``(1-based line number in text, 1-based denylist line index)`` pair
    where a needle is a substring of that line, case-insensitively and with runs of
    whitespace collapsed on both sides."""
    hits: list[tuple[int, int]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        normalized = _normalize(line)
        if not normalized:
            continue
        for index, needle in enumerate(needles, start=1):
            if needle and needle in normalized:
                hits.append((lineno, index))
    return hits


def _format_hits(source_id: str, hits: Iterable[tuple[int, int]]) -> list[str]:
    return [f"{source_id}:{lineno}: denylist line {index}" for lineno, index in hits]


def compute_triage_exemptions(
    denylist_lines: Sequence[str], pairs: Iterable[tuple[str, str]]
) -> tuple[list[str], int, int]:
    """The pure half of ``--triage-exempt``: which denylist *lines* (not which
    files) already match something in ``pairs``, plus the exempted/remaining
    counts ``--triage-exempt`` prints. ``pairs`` is ``(source_id, text)``, exactly
    what every other mode's per-source scan takes — this is what makes "agrees
    with the default mode over the same fixture" a meaningful assertion rather
    than a coincidence.
    """
    needles = _denylist_needles(denylist_lines)
    nonblank = [line for line in denylist_lines if line.strip()]
    matched_indices: set[int] = set()
    for _source_id, text in pairs:
        for _lineno, index in match_denylist(text, needles):
            matched_indices.add(index)
    exempt_lines = [
        line for index, line in enumerate(nonblank, start=1) if index in matched_indices
    ]
    return exempt_lines, len(matched_indices), len(nonblank) - len(matched_indices)


# --- git plumbing (the only I/O in this script besides main()'s wiring) --------------


def _run_git(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


def _git_ls_files(cwd: Path = ROOT) -> list[str]:
    output = _run_git(["ls-files", "-z"], cwd=cwd).stdout
    return [p for p in output.rstrip("\0").split("\0") if p]


def _tracked_text_at_ref(ref: str, cwd: Path = ROOT) -> list[tuple[str, str]]:
    names = _run_git(["ls-tree", "-r", "--name-only", "-z", ref], cwd=cwd).stdout
    pairs: list[tuple[str, str]] = []
    for relative in (p for p in names.rstrip("\0").split("\0") if p):
        blob = subprocess.run(
            ["git", "show", f"{ref}:{relative}"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if blob.returncode != 0:
            continue
        pairs.append((relative, blob.stdout))
    return pairs


def _commit_shas(range_spec: str, cwd: Path) -> list[str]:
    output = _run_git(["log", "--format=%H", range_spec], cwd=cwd).stdout
    return [line for line in output.splitlines() if line]


def _added_lines(diff_text: str) -> str:
    added = [
        line[1:]
        for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    return "\n".join(added)


def scan_default(
    needles: Sequence[str], ref: str = "HEAD", cwd: Path = ROOT
) -> list[str]:
    findings: list[str] = []
    for relative, text in _tracked_text_at_ref(ref, cwd=cwd):
        findings.extend(_format_hits(relative, match_denylist(text, needles)))
    return findings


def scan_diff(needles: Sequence[str], range_spec: str, cwd: Path = ROOT) -> list[str]:
    findings: list[str] = []
    for sha in _commit_shas(range_spec, cwd=cwd):
        diff_text = _run_git(
            ["show", "--no-color", "--first-parent", sha], cwd=cwd
        ).stdout
        added = _added_lines(diff_text)
        findings.extend(_format_hits(sha, match_denylist(added, needles)))
    staged = _run_git(["diff", "--no-color", "--cached"], cwd=cwd).stdout
    staged_added = _added_lines(staged)
    if staged_added:
        findings.extend(_format_hits("STAGED", match_denylist(staged_added, needles)))
    return findings


def scan_commits(
    needles: Sequence[str], range_spec: str, cwd: Path = ROOT
) -> list[str]:
    findings: list[str] = []
    for sha in _commit_shas(range_spec, cwd=cwd):
        message = _run_git(["show", "-s", "--format=%B", sha], cwd=cwd).stdout
        findings.extend(_format_hits(sha, match_denylist(message, needles)))
    return findings


def scan_text_files(needles: Sequence[str], paths: Sequence[str]) -> list[str]:
    findings: list[str] = []
    for path_value in paths:
        text = Path(path_value).read_text(encoding="utf-8", errors="replace")
        findings.extend(_format_hits(path_value, match_denylist(text, needles)))
    return findings


def run_triage_exempt(
    denylist_lines: Sequence[str], ref: str, out_dir: Path, cwd: Path = ROOT
) -> tuple[int, int]:
    """The I/O wrapper around :func:`compute_triage_exemptions`: reads the tracked
    tree at ``ref``, writes the exempted denylist lines to
    ``<out_dir>/denylist-exempt.txt`` through the private-output allowlist guard
    (creating it fresh), and returns the printed counts."""
    from rheo_recallatron.migration.private_paths import require_private_output

    pairs = _tracked_text_at_ref(ref, cwd=cwd)
    exempt_lines, exempted, remaining = compute_triage_exemptions(denylist_lines, pairs)
    out_path = require_private_output(Path(out_dir) / "denylist-exempt.txt")
    out_path.write_text("".join(f"{line}\n" for line in exempt_lines), encoding="utf-8")
    return exempted, remaining


def _read_denylist(path_value: str) -> list[str]:
    path = Path(path_value).expanduser()
    if path.resolve().is_relative_to(ROOT):
        raise RuntimeError(
            f"{DENYLIST_VARIABLE} must name a file outside the repository"
        )
    return path.read_text(encoding="utf-8").splitlines()


# --- self-test ------------------------------------------------------------------------


def _self_test() -> str | None:
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        repo = tmp / "scratch-repo"
        repo.mkdir()
        _run_git(["init", "-q"], cwd=repo)
        _run_git(["config", "user.email", "scratch@example.com"], cwd=repo)
        _run_git(["config", "user.name", "Scratch"], cwd=repo)

        # --- path gate: a clean file, a planted pattern match, a planted exact
        # hand-written-report name, and a clean near-miss.
        (repo / "clean.txt").write_text("nothing interesting here\n")
        (repo / "inventory-2026.json").write_text("{}\n")
        nested = repo / "nested"
        nested.mkdir()
        (nested / "report.md").write_text("# report\n")
        (repo / "inventory.py").write_text("# not json, should not match\n")
        _run_git(["add", "-A"], cwd=repo)
        _run_git(["commit", "-q", "-m", "seed"], cwd=repo)

        gate_findings = check_path_gate(_git_ls_files(cwd=repo))
        flagged = {f.split(":", 1)[0] for f in gate_findings}
        if "inventory-2026.json" not in flagged:
            return "self-test FAILED: path gate missed a planted inventory*.json file"
        if "nested/report.md" not in flagged:
            return "self-test FAILED: path gate missed a planted exact-name report.md"
        if "clean.txt" in flagged:
            return "self-test FAILED: path gate flagged an innocuous tracked file"
        if "inventory.py" in flagged:
            return "self-test FAILED: path gate flagged a near-miss extension"

        # --- the shared matcher: output discipline (never print matched text).
        needles = _denylist_needles(["Acme Regional Hospital"])
        hits = match_denylist(
            "line one\nAcme Regional Hospital appears here\n", needles
        )
        formatted = _format_hits("planted.txt", hits)
        if formatted != ["planted.txt:2: denylist line 1"]:
            return f"self-test FAILED: unexpected finding shape: {formatted!r}"
        if any("Acme Regional Hospital" in line for line in formatted):
            return "self-test FAILED: a finding printed the matched denylist text"

        # --- mode 1: default (tracked text at HEAD).
        (repo / "public.txt").write_text("Acme Regional Hospital signed on\n")
        _run_git(["add", "-A"], cwd=repo)
        _run_git(["commit", "-q", "-m", "add a public leak"], cwd=repo)
        default_findings = scan_default(needles, cwd=repo)
        if not any(f.startswith("public.txt:") for f in default_findings):
            return "self-test FAILED: default-mode leak scan missed a planted leak"

        # --- mode 2: --diff, over a range whose leak is introduced then reverted
        # by a later commit — proving the per-commit scan, not the net diff.
        (repo / "diffed.txt").write_text("before\n")
        _run_git(["add", "-A"], cwd=repo)
        _run_git(["commit", "-q", "-m", "add a file"], cwd=repo)
        base = _run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
        (repo / "diffed.txt").write_text("Acme Regional Hospital, briefly\n")
        _run_git(["commit", "-aq", "-m", "leak the name"], cwd=repo)
        (repo / "diffed.txt").write_text("redacted\n")
        _run_git(["commit", "-aq", "-m", "revert the name"], cwd=repo)
        diff_findings = scan_diff(needles, f"{base}..HEAD", cwd=repo)
        if not any("denylist line 1" in f for f in diff_findings):
            return "self-test FAILED: --diff mode missed a leak a later commit reverted"

        # --- mode 3: --commits, a leak in a commit message.
        (repo / "trivial.txt").write_text("x\n")
        _run_git(["add", "-A"], cwd=repo)
        commit_base = _run_git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
        _run_git(
            ["commit", "-q", "-m", "mentions Acme Regional Hospital in the message"],
            cwd=repo,
        )
        commits_findings = scan_commits(needles, f"{commit_base}..HEAD", cwd=repo)
        if not commits_findings:
            return "self-test FAILED: --commits mode missed a leak in a commit message"

        # --- mode 4: --text, an arbitrary file (used for a gated issue/PR body).
        text_file = tmp / "candidate-body.txt"
        text_file.write_text("Acme Regional Hospital is the client\n")
        text_findings = scan_text_files(needles, [str(text_file)])
        if not text_findings:
            return "self-test FAILED: --text mode missed a leak in an arbitrary file"

        # --- mode 5: --triage-exempt, over a synthetic fixture with synthetic
        # tokens only. A mixed-case, differently-spaced match is exempted, and
        # `compute_triage_exemptions` agrees with the default-mode matcher on the
        # same fixture because both call `match_denylist` and nothing else.
        exempt_denylist = ["Foo   Bar", "Never Matches Anything Real"]
        exempt_needles = _denylist_needles(exempt_denylist)
        fixture_pairs = [
            ("tests/fixtures/public.txt", "the contact is foo bar, publicly known"),
            ("tests/fixtures/clean.txt", "nothing sensitive in here"),
        ]
        default_like_indices = {
            index
            for _source, text in fixture_pairs
            for _lineno, index in match_denylist(text, exempt_needles)
        }
        exempt_lines, exempted, remaining = compute_triage_exemptions(
            exempt_denylist, fixture_pairs
        )
        if exempt_lines != ["Foo   Bar"]:
            return (
                "self-test FAILED: --triage-exempt did not exempt the mixed-case, "
                f"differently-spaced match: {exempt_lines!r}"
            )
        if exempted != len(default_like_indices) or default_like_indices != {1}:
            return (
                "self-test FAILED: --triage-exempt's matched-index set disagrees "
                "with the default-mode matcher over the same fixture"
            )
        if remaining != 1:
            return (
                f"self-test FAILED: expected one remaining denylist line, got "
                f"{remaining}"
            )

    return None


# --- CLI wiring -----------------------------------------------------------------------


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--diff", metavar="RANGE", help="two-dot commit range")
    group.add_argument("--commits", metavar="RANGE", help="two-dot commit range")
    group.add_argument("--text", nargs="+", metavar="FILE", help="arbitrary files")
    group.add_argument(
        "--triage-exempt", metavar="REF", dest="triage_exempt", help="a git ref"
    )
    parser.add_argument("--out", metavar="DIR", help="required with --triage-exempt")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    self_test_failure = _self_test()
    if self_test_failure is not None:
        print(self_test_failure, file=sys.stderr)
        return 1

    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.triage_exempt is not None and not args.out:
        print("--triage-exempt requires --out", file=sys.stderr)
        return 1
    if args.out and args.triage_exempt is None:
        print("--out is only valid together with --triage-exempt", file=sys.stderr)
        return 1

    try:
        tracked = _git_ls_files()
    except subprocess.CalledProcessError as error:
        print(f"migration-output check failed: {error}", file=sys.stderr)
        return 1

    findings = check_path_gate(tracked)
    exempt_summary: tuple[int, int] | None = None

    denylist_path = os.environ.get(DENYLIST_VARIABLE)
    if denylist_path:
        try:
            denylist_lines = _read_denylist(denylist_path)
        except (OSError, RuntimeError) as error:
            print(f"migration-output check failed: {error}", file=sys.stderr)
            return 1
        needles = _denylist_needles(denylist_lines)

        try:
            if args.triage_exempt is not None:
                exempt_summary = run_triage_exempt(
                    denylist_lines, args.triage_exempt, Path(args.out)
                )
            elif args.diff is not None:
                findings.extend(scan_diff(needles, args.diff))
            elif args.commits is not None:
                findings.extend(scan_commits(needles, args.commits))
            elif args.text is not None:
                findings.extend(scan_text_files(needles, args.text))
            else:
                findings.extend(scan_default(needles))
        except subprocess.CalledProcessError as error:
            print(f"migration-output check failed: {error}", file=sys.stderr)
            return 1
    elif args.triage_exempt is not None:
        print(
            f"--triage-exempt needs {DENYLIST_VARIABLE} set; nothing to exempt against",
            file=sys.stderr,
        )
        return 1

    for finding in findings:
        print(finding, file=sys.stderr)

    if exempt_summary is not None:
        exempted, remaining = exempt_summary
        print(f"exempted={exempted} remaining={remaining}")

    if findings:
        return 1
    print(
        f"Migration-output checks passed: {len(tracked)} tracked path(s) clear of "
        "the path gate."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
