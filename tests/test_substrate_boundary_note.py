"""AC 20, AC 21 and AC 23: the E0c record exists, is complete, and is safe to publish.

`docs/notes/0c0-substrate-boundary.md` is this run's deliverable to whoever freezes the
0c1 cohort. Like run 0v's findings note before it, it gets a test rather than a
convention: a document nothing checks is a document that quietly goes out of step with
the tree it describes.

**What this asserts, and what it deliberately does not.** It asserts structure and
required content — the seven sections, the two tables agreeing in **both** directions,
and the specific sentences the record would be misleading without. It does not assert
the prose is right; no test can.

**It carries no cut-symbol token of its own.** The twelve tokens are parsed out of the
note and compared against each other, never written here. That is not only tidiness:
the boundary guard substring-matches those tokens over raw diff text, and the note is
the single file declared to carry them, so a test that hardcoded a token would itself
be the boundary crossing it was written to help police. The count is asserted as a
number instead.
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]

NOTE = _REPO_ROOT / "docs" / "notes" / "0c0-substrate-boundary.md"

#: The seven sections `spec.md` § The E0c record fixes, in order. Matched on the
#: numbered heading rather than on the exact title, so rewording a heading is allowed
#: and dropping a section is not.
REQUIRED_SECTIONS = 7

#: ``## 3. 0c1's task boundary``
_SECTION = re.compile(r"(?m)^## (\d+)\. (\S.*)$")

#: A cut-symbol row: ``| 1 | `<token>` | … | … |`` — four cells, the first an index
#: and the second a backticked token. The example is a placeholder on purpose: a real
#: token written here would put one in a second file and be the crossing this file
#: helps police, which is the same reason the module docstring gives above.
_TOKEN_ROW = re.compile(r"(?m)^\|\s*(\d+)\s*\|\s*`([^`]+)`\s*\|[^|]*\|[^|]*\|\s*$")

#: A coverage row: ``| leasing | 0c1 / 12 | 1, 2 |`` — three cells, the last a
#: comma-separated list of token indices.
_COVERAGE_ROW = re.compile(
    r"(?m)^\|\s*([^|]+?)\s*\|\s*(0c[12]\s*/\s*\d+)\s*\|\s*([\d,\s]+)\s*\|\s*$"
)

#: Private locations that must never appear in a public note. Run 0v's first draft of
#: its own findings note block-quoted a private planning document into this repository
#: and caught it only just before committing; AC 23 makes the check mechanical rather
#: than a habit. A full text-similarity comparison is not possible and not required —
#: this run cannot read those documents at all — so the check is on the names and paths
#: through which private material would arrive.
FORBIDDEN = (
    "private/",
    "build-plan",
    ".bureau",
    "benchmark-schedule",
    "/Users/",
    "agent-context",
    "HANDOFF",
)

#: Sentences the record would be actively misleading without, each reduced to one
#: distinguishing substring. A required statement that nothing checks is a statement
#: that quietly goes missing on the next edit.
REQUIRED_CONTENT = {
    "the guard's mechanism, cited": "integration-gate.sh:430-435",
    "the raw-diff mechanism named as such": "raw diff text",
    "the added-lines claim marked withdrawn": "withdrawn as incorrect",
    "the retained audit protocol's issue": "#45",
    "the due-work index's issue": "#46",
    "the verb_noun limitation": "verb_noun",
    "the exemption count, stated explicitly": "TWO",
    "the narrowed waiver, named": "A-NARROWED",
    "rename rejection 1 — the name is pinned twice": "pinned twice",
    "rename rejection 2 — the sibling probes": "sibling probes",
    "rename rejection 3 — the hit is a false positive": "false positive",
}


def _text() -> str:
    return NOTE.read_text(encoding="utf-8")


def test_the_note_exists_and_is_not_empty() -> None:
    assert NOTE.is_file(), (
        f"{NOTE.relative_to(_REPO_ROOT)} is run 0c0's deliverable and is missing"
    )
    assert _text().strip(), f"{NOTE.relative_to(_REPO_ROOT)} is empty"


def test_the_seven_sections_are_present_and_numbered_from_one() -> None:
    """All seven, contiguous, in order.

    The sections are not interchangeable: section 5 is the one a cohort freeze weighs
    and section 6 is the one the boundary guard's own honesty rests on. Dropping either
    leaves a note that still reads as complete.
    """
    numbers = [int(match.group(1)) for match in _SECTION.finditer(_text())]
    assert numbers == list(range(1, REQUIRED_SECTIONS + 1)), (
        f"expected sections 1-{REQUIRED_SECTIONS} in order; found {numbers}"
    )


def test_every_section_has_a_body() -> None:
    """A heading with nothing under it is not a section."""
    text = _text()
    matches = list(_SECTION.finditer(text))
    thin = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        if len(body) < 200:
            thin[match.group(2)] = len(body)
    assert not thin, f"these sections carry no real body: {thin}"


def test_the_cut_symbol_table_declares_exactly_twelve_tokens() -> None:
    """Twelve rows, numbered 1-12, each naming a distinct token.

    A thirteenth token fails here. So does a duplicate, and so does a gap in the
    numbering, which the coverage table's references depend on.
    """
    rows = _TOKEN_ROW.findall(_text())
    indices = [int(index) for index, _ in rows]
    tokens = [token for _, token in rows]

    assert indices == list(range(1, 13)), (
        f"the cut-symbol table must carry tokens 1-12 in order; found {indices}"
    )
    assert len(set(tokens)) == 12, f"the twelve tokens are not distinct: {tokens}"


def test_the_two_tables_agree_in_both_directions() -> None:
    """Every token guards a behaviour, and every behaviour is guarded by a token.

    This is run 0v's F56 addressed to this run by name. One direction alone is not
    enough: a token with no behaviour is decoration that reads as coverage, and a
    behaviour with no token is a scored deliverable the guard would not notice
    crossing the boundary.
    """
    declared = {int(index) for index, _ in _TOKEN_ROW.findall(_text())}

    coverage = _COVERAGE_ROW.findall(_text())
    assert coverage, "the behaviour-coverage table parsed to no rows at all"

    referenced: set[int] = set()
    for behaviour, _cohort, cell in coverage:
        cited = {int(part) for part in cell.replace(" ", "").split(",") if part}
        assert cited, f"behaviour {behaviour!r} cites no token"
        referenced |= cited

    assert not referenced - declared, (
        "the coverage table cites tokens the cut-symbol table does not declare: "
        f"{sorted(referenced - declared)}"
    )
    assert not declared - referenced, (
        "these declared tokens guard no behaviour in the coverage table: "
        f"{sorted(declared - referenced)}; a token with no row reads as coverage "
        "while providing none"
    )


def test_the_note_carries_the_statements_that_keep_the_guard_honest() -> None:
    """The specific sentences a cohort freeze would be misled without.

    A structure test over required content, not a semantic one: each required
    statement is reduced to one distinguishing substring. The point is that these
    cannot silently disappear, not that the surrounding prose is graded.
    """
    text = _text()
    missing = sorted(
        f"{what} ({marker!r})"
        for what, marker in REQUIRED_CONTENT.items()
        if marker not in text
    )
    assert not missing, (
        f"{NOTE.relative_to(_REPO_ROOT)} is missing required statements: {missing}"
    )


def test_the_note_names_no_private_location() -> None:
    """Publication safety, the half that is mechanically checkable."""
    text = _text()
    found = sorted(marker for marker in FORBIDDEN if marker in text)
    assert not found, (
        f"{NOTE.relative_to(_REPO_ROOT)} is public and names private locations: {found}"
    )
