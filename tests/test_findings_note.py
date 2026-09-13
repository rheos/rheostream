"""AC 16's mechanical half: the findings note exists, parses, and is complete.

Run 0v's deliverable is ``docs/notes/0v-vertical-slice-findings.md``, not the code
it was produced by — ``modules/spike/**``, the spike web surface and the driver are
deleted at 0c0's branch cut and the note is what survives. So the note gets the same
treatment as any other artifact this repository depends on: a test that fails when
it is wrong, rather than a convention nobody checks.

**What this asserts, and what it deliberately does not.** It asserts structure: the
file parses into ``### F<n> — <title>`` findings, the numbering runs from F1 with no
gap and no duplicate, every finding carries exactly one disposition line, and every
disposition is one of the three the specification allows. It asserts **F1–F22
specifically**, because those are the design-pass findings the note carries verbatim
and dropping one in transcription is the failure mode with no other guard.

It does **not** assert the prose is right — no test can — and it does not check for
the trust-boundary section, which is deliberately not a numbered finding and carries
no disposition.

**One assertion this cannot make.** Most findings carry ``file:line`` evidence, but
not all of them can: F7 names an open issue's documentation half, F44 and F49 are
about ``make`` and CI behaviour with no source line to cite. A "every finding cites a
line" assertion would either be false or would force a fake citation, so the shape is
checked instead — a finding whose body is empty or near-empty fails
:func:`test_every_finding_has_a_body`.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

NOTE = _REPO_ROOT / "docs" / "notes" / "0v-vertical-slice-findings.md"

#: The three dispositions `spec.md` allows, and the only three this note may use.
DISPOSITIONS = ("amend the spec", "amend the plan", "accept")

#: The design-pass findings, carried into the note verbatim. AC 16 names them.
REQUIRED = tuple(f"F{n}" for n in range(1, 23))

#: ``### F13 — step 2's `operation_set` check is a no-op …``
_HEADING = re.compile(r"(?m)^### (F(\d+)) — (\S.*)$")

#: The bolded disposition sentence inside a finding's body. ``\b`` rather than a
#: trailing ``.``: F7 writes ``**Disposition: accept** for this run``, with the
#: sentence continuing outside the bold, while most write ``**Disposition: amend
#: the plan.**``. Both are legal and both must match.
_DISPOSITION = re.compile(r"\*\*Disposition: (" + "|".join(DISPOSITIONS) + r")\b")

#: Any use of the word at all, bolded or not, so an unbolded or invented
#: disposition ("Disposition: defer") is caught rather than silently ignored by
#: the stricter pattern above.
_ANY_DISPOSITION = re.compile(r"Disposition:\s*([^\n*.,]+)")

#: Short enough that a heading with nothing under it fails, long enough that a real
#: finding never trips it. The shortest finding in the note as written is ~400
#: characters.
_MINIMUM_BODY = 120


def _findings() -> "dict[str, str]":
    """Every ``### F<n>`` heading in the note mapped to the body beneath it."""
    text = NOTE.read_text(encoding="utf-8")
    matches = list(_HEADING.finditer(text))
    bodies: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        bodies[match.group(1)] = text[match.end() : end].strip()
    return bodies


def test_the_note_exists_and_is_not_empty() -> None:
    assert NOTE.is_file(), (
        f"{NOTE.relative_to(_REPO_ROOT)} is run 0v's deliverable and is missing"
    )
    assert NOTE.read_text(encoding="utf-8").strip(), (
        f"{NOTE.relative_to(_REPO_ROOT)} is empty"
    )


def test_every_design_pass_finding_is_present() -> None:
    """F1–F22 each appear as a heading.

    These are transcribed rather than written, so the failure this guards is a
    finding dropped between the source list and the note — which nothing else in
    the repository would notice.
    """
    found = _findings()
    missing = [name for name in REQUIRED if name not in found]
    assert not missing, (
        f"{NOTE.relative_to(_REPO_ROOT)} is missing {missing}; F1-F22 are carried "
        "into the note verbatim and none may be dropped in transcription"
    )


def test_the_numbering_is_contiguous_from_one() -> None:
    numbers = sorted(int(name[1:]) for name in _findings())
    assert numbers, "the note parsed to no findings at all"
    expected = list(range(1, numbers[-1] + 1))
    assert numbers == expected, (
        "finding numbers are not contiguous from F1: "
        f"missing {sorted(set(expected) - set(numbers))}, "
        f"duplicated or out of range {sorted(set(numbers) - set(expected))}"
    )


def test_every_finding_carries_exactly_one_disposition() -> None:
    problems = {}
    for name, body in _findings().items():
        matched = _DISPOSITION.findall(body)
        if len(matched) != 1:
            problems[name] = matched
    assert not problems, (
        "every finding carries exactly one disposition line, one of "
        f"{list(DISPOSITIONS)}; these do not: {problems}"
    )


def test_no_disposition_is_outside_the_allowed_three() -> None:
    """Catches an unbolded or invented disposition the stricter pattern skips.

    Without this, `Disposition: defer to 1a` would simply not match
    `_DISPOSITION`, and the finding would fail the count test with a confusing
    message about having zero rather than about having an illegal one.
    """
    text = NOTE.read_text(encoding="utf-8")
    used = {match.strip() for match in _ANY_DISPOSITION.findall(text)}
    illegal = sorted(used - set(DISPOSITIONS))
    assert not illegal, (
        f"{NOTE.relative_to(_REPO_ROOT)} uses dispositions outside the allowed "
        f"three {list(DISPOSITIONS)}: {illegal}"
    )


def test_every_finding_has_a_body() -> None:
    """A heading with nothing under it is not a finding."""
    thin = {
        name: len(body)
        for name, body in _findings().items()
        if len(body) < _MINIMUM_BODY
    }
    assert not thin, (
        "a finding must carry its evidence, not only a heading and a disposition; "
        f"these bodies are under {_MINIMUM_BODY} characters: {thin}"
    )
