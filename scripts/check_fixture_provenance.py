#!/usr/bin/env python3
"""Fixture-provenance and synthetic-content scan (criterion 35).

This is a public repository (`rheos/rheostream`); the run's own preamble requires
every fixture, test name, comment, commit message and doc edit to use synthetic data
only. This script is the CI-enforced half of that promise, in three independent
checks:

1. **Manifest completeness.** Every tracked file under a `fixtures/` path segment
   (`Path(relative).parts` contains `"fixtures"`, anywhere in the path — not only
   `tests/fixtures/`) has a matching entry in `tests/fixtures/provenance.json`:
   `{"path": ..., "origin": "synthetic", "how": "<one line>"}`. Every listed entry's
   `path` must correspond to a file that still exists (an orphaned entry is a finding
   too — it means the manifest is lying about what is on disk). No `origin` other than
   `"synthetic"` is accepted. The manifest's own path is exempt from needing an entry
   about itself — it is the ledger, not a fixture — the same way
   `check_legacy_names.py` exempts its own path from its self-scan.
2. **Content heuristics**, scanned over every fixture file plus `tests/**`,
   `modules/*/tests/**`, `apps/web/src/**`, `modules/*/web/**` and
   `packages/web-contract/**`: an email address must resolve to an RFC 2606/6761
   reserved domain (`example.com`/`.org`/`.net`, anything under the reserved TLDs
   `.example`/`.test`/`.invalid`, or `localhost`); an IPv4 literal must be loopback
   (`127.0.0.0/8`), `0.0.0.0`, or an RFC 5737 documentation range (`192.0.2.0/24`,
   `198.51.100.0/24`, `203.0.113.0/24`); a phone-number-shaped string (grouped
   3-3-4 or `(3) 3-4` digits, compact E.164 `+…`, or a bare 10-digit North American
   number — see `_PHONE`) is flagged outright, as is a rate-shaped string (a currency
   amount, by symbol or by code on either side, followed by `/h`, `/hr`, `/hour`,
   `per hour`, `an hour`, or `hourly` — see `_RATE`) — this repository is not the
   place for either, synthetic or not.
3. **Optional private denylist.** When the environment variable
   `RHEO_PRIVATE_DENYLIST` names a file outside the repository, each of that file's
   non-blank lines is also a forbidden substring, compared case-insensitively,
   anywhere the scan in (2) covers. CI
   never sets this variable — it exists so a real client or personal name can be
   checked for locally without ever publishing the names themselves in this script or
   its history.

Runs under the system python3 (3.9-compatible, no third-party deps), mirroring
`scripts/check_routing_literals.py`. Every check function is pure (operates on given
paths/text/manifest data, not on the filesystem), so the self-test exercises the real
logic without touching git or the tree; only `_tracked_fixture_relatives()`,
`_read_manifest()`, `_tracked_content_pairs()` and `_private_denylist_lines()` do I/O,
and only `main()` wires them together.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = "tests/fixtures/provenance.json"

# --- check 1: manifest completeness -----------------------------------------------


def check_manifest(fixture_relatives: Iterable[str], manifest: list[Any]) -> list[str]:
    findings: list[str] = []
    fixture_set = {p for p in fixture_relatives if p != MANIFEST_PATH}
    manifest_by_path: dict[str, Any] = {}
    for entry in manifest:
        if not isinstance(entry, dict):
            findings.append(f"{MANIFEST_PATH}: entry is not an object: {entry!r}")
            continue
        path = entry.get("path")
        origin = entry.get("origin")
        how = entry.get("how")
        if not isinstance(path, str) or not path:
            findings.append(f"{MANIFEST_PATH}: entry missing a valid 'path': {entry!r}")
            continue
        if path in manifest_by_path:
            findings.append(f"{MANIFEST_PATH}: duplicate entry for {path}")
        manifest_by_path[path] = entry
        if origin != "synthetic":
            findings.append(
                f"{MANIFEST_PATH}: {path} has origin {origin!r}, only "
                "'synthetic' is accepted"
            )
        if not isinstance(how, str) or not how.strip() or "\n" in how:
            findings.append(f"{MANIFEST_PATH}: {path} is missing a one-line 'how'")

    for path in sorted(fixture_set - manifest_by_path.keys()):
        findings.append(f"{path}: fixture file has no {MANIFEST_PATH} entry")
    for path in sorted(manifest_by_path.keys() - fixture_set):
        findings.append(
            f"{MANIFEST_PATH}: entry for {path}, but that file no longer exists"
        )
    return findings


# --- check 2: content heuristics ----------------------------------------------------

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_RESERVED_EXACT_DOMAINS = frozenset(
    {"example.com", "example.org", "example.net", "localhost"}
)
_RESERVED_TLDS = ("example", "test", "invalid")


def _domain_is_reserved(domain: str) -> bool:
    low = domain.lower().rstrip(".")
    if low in _RESERVED_EXACT_DOMAINS:
        return True
    tld = low.rsplit(".", 1)[-1]
    return tld in _RESERVED_TLDS


# A run of digits not adjacent to another digit (so this never matches inside a longer
# number or a UUID segment — see module docstring / handoff for the worked-through
# reasoning on why e.g. a UUID's 8-4-4-4-12 hex groups cannot satisfy this).
_IPV4 = re.compile(
    r"(?<!\d)(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?!\d)"
)
_DOC_RANGE_PREFIXES = frozenset({(192, 0, 2), (198, 51, 100), (203, 0, 113)})


def _ipv4_is_reserved(literal: str) -> bool:
    octets = tuple(int(part) for part in literal.split("."))
    if octets == (0, 0, 0, 0):
        return True
    if octets[0] == 127:
        return True
    return octets[:3] in _DOC_RANGE_PREFIXES


# Three phone shapes:
# - grouped 3-3-4 or (3) 3-4 digits joined by `-`/`.`/space, with an optional
#   country-code prefix (`+1 555-555-0100`, `(555) 555-0100`);
# - compact E.164: `+` then 8-15 digits (`+15555550100`);
# - a bare 10-digit North American number whose area code and exchange both start
#   2-9 (`5555550100`). That NANP rule is what keeps a 10-digit Unix timestamp
#   (which starts with 1 until 2286) out of it, and the `[\w.-]` guards keep a digit
#   run inside a UUID, a hash, a decimal or a longer identifier out of it too.
_PHONE = re.compile(
    r"(?<!\d)(?:\+?\d{1,3}[-.\s])?(?:\(\d{3}\)\s?|\d{3}[-.\s])\d{3}[-.\s]\d{4}(?!\d)"
    r"|(?<![\w+])\+[1-9]\d{7,14}(?!\d)"
    r"|(?<![\w.+-])[2-9]\d{2}[2-9]\d{6}(?![\w.-])"
)

# An amount — a currency symbol before it, or a currency code on either side — then
# a per-hour unit: `/h`, `/hr`, `/hour`, `per hour`/`per hr`, `an hour`, `hourly`.
_CURRENCY_CODE = r"(?:USD|CAD|EUR|GBP|AUD|NZD)"
_AMOUNT = r"\d[\d,]*(?:\.\d+)?"
_RATE_AMOUNT = (
    rf"(?:[$€£]\s?{_AMOUNT}|{_AMOUNT}\s?{_CURRENCY_CODE}|{_CURRENCY_CODE}\s?{_AMOUNT})"
    rf"(?:\s?{_CURRENCY_CODE})?"
)
_RATE_UNIT = r"(?:/\s?(?:hr|h|hour)\b|per\s+(?:hour|hr)\b|an\s+hour\b|hourly\b)"
_RATE = re.compile(rf"{_RATE_AMOUNT}\s?{_RATE_UNIT}", re.IGNORECASE)


def check_content(pairs: Iterable[tuple[str, str]]) -> list[str]:
    findings: list[str] = []
    for path, text in pairs:
        for match in _EMAIL.finditer(text):
            if not _domain_is_reserved(match.group(1)):
                findings.append(
                    f"{path}: email does not use a reserved domain: {match.group(0)}"
                )
        for match in _IPV4.finditer(text):
            if not _ipv4_is_reserved(match.group(0)):
                findings.append(
                    f"{path}: IPv4 literal is not loopback/0.0.0.0/a documentation "
                    f"range: {match.group(0)}"
                )
        if _PHONE.search(text):
            findings.append(f"{path}: phone-number-shaped string found")
        if _RATE.search(text):
            findings.append(f"{path}: hourly-rate-shaped string found")
    return findings


# --- check 3: optional private denylist ---------------------------------------------


def check_private_denylist(
    pairs: Iterable[tuple[str, str]], denylist_lines: Iterable[str]
) -> list[str]:
    """Case-insensitive: a real name spelled in a different case in a fixture is
    still that name."""
    findings: list[str] = []
    lines = [line.strip().casefold() for line in denylist_lines if line.strip()]
    if not lines:
        return findings
    for path, text in pairs:
        folded = text.casefold()
        for line in lines:
            if line in folded:
                findings.append(
                    f"{path}: contains a forbidden RHEO_PRIVATE_DENYLIST substring"
                )
    return findings


# --- real-tree wiring -----------------------------------------------------------------


def _is_fixture_segment(relative: str) -> bool:
    return "fixtures" in Path(relative).parts


def _in_content_roots(relative: str) -> bool:
    if _is_fixture_segment(relative):
        return True
    parts = Path(relative).parts
    if relative.startswith("tests/") or relative.startswith("apps/web/src/"):
        return True
    if relative.startswith("packages/web-contract/"):
        return True
    if len(parts) >= 3 and parts[0] == "modules" and parts[2] in ("tests", "web"):
        return True
    return False


def _git_ls_files() -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [p for p in output.rstrip("\0").split("\0") if p]


def _tracked_fixture_relatives(all_files: list[str]) -> list[str]:
    return [p for p in all_files if _is_fixture_segment(p)]


def _read_manifest() -> list[Any]:
    manifest_path = ROOT / MANIFEST_PATH
    if not manifest_path.is_file():
        return []
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{MANIFEST_PATH} is not valid JSON: {error}") from error
    if not isinstance(data, list):
        raise RuntimeError(f"{MANIFEST_PATH} must be a JSON array of entries")
    return data


def _tracked_content_pairs(all_files: list[str]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for relative in all_files:
        if relative == MANIFEST_PATH or not _in_content_roots(relative):
            continue
        path = ROOT / relative
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        pairs.append((relative, text))
    return pairs


def _private_denylist_lines() -> list[str]:
    denylist_path = os.environ.get("RHEO_PRIVATE_DENYLIST")
    if not denylist_path:
        return []
    path = Path(denylist_path).expanduser()
    try:
        if path.resolve().is_relative_to(ROOT):
            raise RuntimeError(
                "RHEO_PRIVATE_DENYLIST must name a file outside the repository"
            )
        return path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RuntimeError(
            f"could not read RHEO_PRIVATE_DENYLIST={path}: {error}"
        ) from error


# --- self-test ------------------------------------------------------------------------


def _self_test() -> str | None:
    # Check 1: manifest completeness — one file with no entry, one entry whose file
    # is gone, one entry with a disallowed origin, one clean pair.
    fixture_files = {
        "tests/fixtures/planted-no-entry.json",
        "tests/fixtures/planted-known.json",
        "tests/fixtures/planted-bad-origin.json",
    }
    manifest = [
        {
            "path": "tests/fixtures/planted-known.json",
            "origin": "synthetic",
            "how": "seed data",
        },
        {
            "path": "tests/fixtures/planted-stale.json",
            "origin": "synthetic",
            "how": "removed",
        },
        {
            "path": "tests/fixtures/planted-bad-origin.json",
            "origin": "real",
            "how": "wrong",
        },
    ]
    manifest_findings = " ".join(check_manifest(fixture_files, manifest))
    if "planted-no-entry.json" not in manifest_findings:
        return (
            "self-test FAILED: a fixture file missing a provenance entry went uncaught"
        )
    if "planted-stale.json" not in manifest_findings:
        return "self-test FAILED: an orphaned provenance entry went uncaught"
    if "planted-bad-origin.json" not in manifest_findings:
        return "self-test FAILED: a non-'synthetic' origin went uncaught"
    if "planted-known.json" in manifest_findings:
        return "self-test FAILED: a clean fixture/entry pair was flagged"

    # Check 2: content heuristics — violations of each rule, and clean near-misses.
    # Every planted phone number is in the fictional 555-01xx range.
    planted_content = {
        "tests/fixtures/planted-email.json": '{"contact": "person@gmail.com"}',
        "tests/fixtures/planted-ip.json": '{"host": "8.8.8.8"}',
        "tests/fixtures/planted-phone.json": '{"phone": "555-555-0100"}',
        "tests/fixtures/planted-phone-paren.json": '{"phone": "(555) 555-0101"}',
        "tests/fixtures/planted-phone-e164.json": '{"phone": "+15555550102"}',
        "tests/fixtures/planted-phone-e164-spaced.json": (
            '{"phone": "+1 555 555 0103"}'
        ),
        "tests/fixtures/planted-phone-bare.json": '{"phone": "5555550104"}',
        "tests/fixtures/planted-rate.json": '{"rate": "$85/hr"}',
        "tests/fixtures/planted-rate-hour.json": '{"rate": "$85/hour"}',
        "tests/fixtures/planted-rate-an-hour.json": '{"rate": "$85 an hour"}',
        "tests/fixtures/planted-rate-code.json": '{"rate": "85 USD/hr"}',
        "tests/fixtures/planted-rate-code-first.json": '{"rate": "USD 85 per hour"}',
        "tests/fixtures/planted-rate-hourly.json": '{"rate": "€60 hourly"}',
    }
    clean_content = {
        "tests/fixtures/clean.json": (
            '{"contact": "person@example.com", "host": "192.0.2.10", '
            '"note": "no phone or rate shape here"}'
        ),
        "tests/fixtures/clean-numbers.json": (
            '{"epoch": 1727136000, "id": "3f2b1c4d-5e6f-4a7b-8c9d-5555550100ab", '
            '"ratio": 0.5555550100, "budget": "$85 total", "count": 12345678901234}'
        ),
    }
    content_findings = check_content([*planted_content.items(), *clean_content.items()])
    by_path: dict[str, list[str]] = {}
    for finding in content_findings:
        path = finding.split(":", 1)[0]
        by_path.setdefault(path, []).append(finding)
    for planted in planted_content:
        if planted not in by_path:
            return f"self-test FAILED: content heuristic did not catch {planted}"
    for clean in clean_content:
        if clean in by_path:
            return f"self-test FAILED: clean content was flagged: {by_path[clean]}"

    # Check 3: the optional private denylist.
    denylist_pairs = [
        ("tests/fixtures/planted-secret.json", '{"client": "Acme Regional Hospital"}'),
        (
            "tests/fixtures/planted-secret-case.json",
            '{"client": "ACME regional HOSPITAL"}',
        ),
        ("tests/fixtures/clean-secret.json", '{"client": "a synthetic customer"}'),
    ]
    denylist_findings = check_private_denylist(
        denylist_pairs, ["Acme Regional Hospital"]
    )
    flagged = {f.split(":", 1)[0] for f in denylist_findings}
    if "tests/fixtures/planted-secret.json" not in flagged:
        return (
            "self-test FAILED: a planted RHEO_PRIVATE_DENYLIST substring went uncaught"
        )
    if "tests/fixtures/planted-secret-case.json" not in flagged:
        return (
            "self-test FAILED: a RHEO_PRIVATE_DENYLIST substring in a different "
            "case went uncaught (comparison is not case-insensitive)"
        )
    if "tests/fixtures/clean-secret.json" in flagged:
        return "self-test FAILED: text without the denylisted substring was flagged"
    if check_private_denylist(denylist_pairs, []):
        return "self-test FAILED: an empty denylist still produced findings"

    return None


def main() -> int:
    self_test_failure = _self_test()
    if self_test_failure is not None:
        print(self_test_failure, file=sys.stderr)
        return 1
    try:
        all_files = _git_ls_files()
        fixture_relatives = _tracked_fixture_relatives(all_files)
        manifest = _read_manifest()
        content_pairs = _tracked_content_pairs(all_files)
        denylist_lines = _private_denylist_lines()
    except (
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        UnicodeError,
        ValueError,
    ) as error:
        print(f"Fixture-provenance check failed: {error}", file=sys.stderr)
        return 1

    findings = [
        *check_manifest(fixture_relatives, manifest),
        *check_content(content_pairs),
        *check_private_denylist(content_pairs, denylist_lines),
    ]
    for finding in findings:
        print(finding, file=sys.stderr)
    if not findings:
        print(
            "Fixture-provenance checks passed (self-test verified all three rules "
            f"still catch a planted violation): {len(fixture_relatives)} fixture "
            "file(s) accounted for, no non-synthetic content found."
        )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
