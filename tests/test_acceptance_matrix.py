"""AC 23 / AC 24: the mechanical guard over the finished acceptance matrices.

``parse`` reads one matrix file. ``validate`` asserts exactly four things about
it, and no more: completeness both ways over that file's own expected set; every
non-``deferred`` demonstrator still resolves (``pytest:`` against
``pytest --collect-only``, ``ci:`` as a bound job/step pair); every
non-``deferred`` mutation hunk still applies (``git apply --check``); every
non-``deferred`` row names a performer.

**Two files, validated independently, plus one check between them.**
:data:`MATRICES` is an ordered list of ``(path, expected_criteria)`` pairs —
``phase-1-matrix.md`` over ``{1..23} ∪ {69}`` and ``phase-2-matrix.md`` over
``{24, 25, 26, 27, 28, 29, 30, 33, 36}``. Each is parsed and validated on its own,
so a stale row in one cannot be masked by the other, and
:func:`cross_file_errors` then asserts that **no criterion number appears in more
than one matrix**. That last check is the only thing the two files share: they are
not merged, not concatenated, and not reconciled into one set. A criterion has
exactly one home and the pair is what proves it, because a number that drifted
into both would otherwise satisfy both files' completeness checks at once.

The list is deliberately a small tuple rather than a directory scan. A scan would
make "which matrices exist" an incidental property of the filesystem; naming them
is what makes adding a third a reviewed decision with an expected set attached.

A ``deferred`` row is routed past checks 2 and 3 the moment ``State`` reads
``deferred``. Its ``Demonstrator`` and ``Mutation`` fields hold the literal
``none`` by grammar; they are never resolved and never passed to ``git apply``.
Neither live matrix holds one today; phase one's rows 15-17 are ``complete``, and
phase two's one held-back half — criterion 24's interface contributions, which are
run 1a3's — is a ``partial`` row carrying real evidence for the half it does
demonstrate. Criterion 30 was the second such row until the public retention
amendment landed and its evidence covered the amended text.

``vitest:`` stays in the schema and is not resolved here: no matrix row uses it,
so ``validate`` takes no ``known_vitest_ids``. The positive control and the
direct demonstrations mutate an in-memory copy; the committed matrices are never
written wrong.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ACCEPTANCE = _REPO_ROOT / "docs" / "acceptance"
_PHASE_ONE = _ACCEPTANCE / "phase-1-matrix.md"
_PHASE_TWO = _ACCEPTANCE / "phase-2-matrix.md"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "repository-checks.yml"

EXPECTED_CRITERIA = frozenset(range(1, 24)) | {69}
"""Phase one's set, unchanged: criteria 1-23 and criterion 69."""

PHASE_TWO_CRITERIA = frozenset({24, 25, 26, 27, 28, 29, 30, 33, 36})
"""Phase two's seeded set.

Nine of phase two's fourteen criteria. 31, 32, 34, 35 and 37 are absent because no
run has closed them, and the completeness check is what keeps them absent: a row
for one of them fails here as ``unexpected`` rather than quietly widening what the
repository claims.
"""

MATRICES: Final[tuple[tuple[Path, frozenset[int]], ...]] = (
    (_PHASE_ONE, EXPECTED_CRITERIA),
    (_PHASE_TWO, PHASE_TWO_CRITERIA),
)

_MATRIX_IDS: Final = tuple(path.name for path, _ in MATRICES)

_HEADING = re.compile(r"^### Criterion (\d+)\s*$", re.MULTILINE)
_FIELD = re.compile(r"^\*\*(.+?):\*\*\s*(.*)$")
_DEMO_BULLET = re.compile(r"^- `([^`]+)`\s*$")
_DIFF_FENCE = re.compile(r"```diff\n(.*?)```", re.DOTALL)
_JOB_KEY = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
_STEP_NAME = re.compile(r"^      - name:\s+(.+)$")


@dataclass(frozen=True, slots=True)
class Row:
    number: int
    state: str
    demonstrators: tuple[str, ...]
    mutation: str
    performed_by: str


def parse(path: Path) -> list[Row]:
    """Split the matrix on ``### Criterion N`` and read fields by bolded label."""
    text = path.read_text(encoding="utf-8")
    headings = list(_HEADING.finditer(text))
    if not headings:
        return []
    rows: list[Row] = []
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        rows.append(
            _row_from_fields(int(match.group(1)), _fields(text[match.end() : end]))
        )
    return rows


def parse_ci_steps(path: Path) -> dict[str, set[str]]:
    """Map each workflow *job key* to the ``- name:`` steps inside that job.

    Nested on purpose: a step name that exists in another job must not satisfy
    a ``ci:<job> / <step>`` pair. Four jobs share ``Check out source``.
    """
    jobs: dict[str, set[str]] = {}
    current: str | None = None
    in_jobs = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("jobs:"):
            in_jobs = True
            current = None
            continue
        if not in_jobs:
            continue
        job = _JOB_KEY.match(line)
        if job:
            current = job.group(1)
            jobs.setdefault(current, set())
            continue
        step = _STEP_NAME.match(line)
        if step is not None and current is not None:
            jobs[current].add(step.group(1).strip())
    return jobs


def validate(
    rows: Sequence[Row],
    *,
    expected: frozenset[int],
    known_pytest_ids: set[str],
    known_ci_steps: dict[str, set[str]],
) -> list[str]:
    """Return errors; empty means this in-memory matrix is clean.

    ``expected`` is the file's own criterion set and is required rather than
    defaulted: one matrix's set is never the right answer for another, and a
    default here is how a second file would come to be checked against the first
    file's numbers.
    """
    errors: list[str] = []
    errors.extend(_completeness_errors(rows, expected))
    for row in rows:
        if row.state == "deferred":
            continue
        errors.extend(_demonstrator_errors(row, known_pytest_ids, known_ci_steps))
        errors.extend(_mutation_errors(row))
        errors.extend(_performer_errors(row))
    return errors


def cross_file_errors(parsed: Sequence[tuple[Path, Sequence[Row]]]) -> list[str]:
    """No criterion number may appear in more than one matrix.

    The one check that spans files. Each file's own completeness check is blind to
    the others by construction — it compares that file's rows against that file's
    expected set — so a number written into two matrices satisfies both and is
    reported by neither. The error names **both** files, because "criterion 24 is
    duplicated" without saying where is not an actionable report when the whole
    point is that it is in two places.
    """
    errors: list[str] = []
    for index, (left_path, left_rows) in enumerate(parsed):
        left = {row.number for row in left_rows}
        for right_path, right_rows in parsed[index + 1 :]:
            shared = sorted(left & {row.number for row in right_rows})
            if shared:
                errors.append(
                    f"criteria {shared} appear in both "
                    f"{left_path.name} and {right_path.name}"
                )
    return errors


def _fields(section: str) -> dict[str, str]:
    """A field owns every line from its ``**Label:**`` to the next bolded label."""
    collected: dict[str, list[str]] = {}
    current: str | None = None
    for line in section.splitlines():
        match = _FIELD.match(line)
        if match:
            current = match.group(1)
            collected[current] = [match.group(2)]
            continue
        if current is not None:
            collected[current].append(line)
    return {name: "\n".join(parts).strip() for name, parts in collected.items()}


def _row_from_fields(number: int, fields: dict[str, str]) -> Row:
    return Row(
        number=number,
        state=fields.get("State", "").splitlines()[0].strip(),
        demonstrators=_demonstrators(fields.get("Demonstrator", "")),
        mutation=_mutation(fields.get("Mutation", "")),
        performed_by=fields.get("Performed by", "").splitlines()[0].strip(),
    )


def _demonstrators(body: str) -> tuple[str, ...]:
    if body == "none":
        return ()
    found: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _DEMO_BULLET.match(stripped)
        if match is None:
            raise ValueError(f"malformed demonstrator line: {stripped!r}")
        found.append(match.group(1))
    return tuple(found)


def _mutation(body: str) -> str:
    if body == "none":
        return "none"
    fence = _DIFF_FENCE.search(body)
    if fence is None:
        return body
    hunk = fence.group(1)
    return hunk if hunk.endswith("\n") else hunk + "\n"


def _completeness_errors(rows: Sequence[Row], expected: frozenset[int]) -> list[str]:
    numbers = [row.number for row in rows]
    got = set(numbers)
    errors: list[str] = []
    missing = sorted(expected - got)
    extra = sorted(got - expected)
    if missing:
        errors.append(f"completeness: missing {missing}")
    if extra:
        errors.append(f"completeness: unexpected {extra}")
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    if duplicates:
        errors.append(f"completeness: duplicate {duplicates}")
    return errors


def _demonstrator_errors(
    row: Row,
    known_pytest_ids: set[str],
    known_ci_steps: dict[str, set[str]],
) -> list[str]:
    if not row.demonstrators:
        return [f"criterion {row.number}: no demonstrator"]
    errors: list[str] = []
    for demo in row.demonstrators:
        if demo.startswith("pytest:"):
            if not _pytest_resolves(demo.removeprefix("pytest:"), known_pytest_ids):
                errors.append(
                    f"criterion {row.number}: pytest demonstrator does not "
                    f"resolve: {demo}"
                )
            continue
        if demo.startswith("ci:"):
            if not _ci_resolves(demo, known_ci_steps):
                errors.append(
                    f"criterion {row.number}: ci demonstrator does not resolve as a "
                    f"job/step pair: {demo}"
                )
            continue
        errors.append(
            f"criterion {row.number}: demonstrator kind is not resolved: {demo}"
        )
    return errors


def _pytest_resolves(node_id: str, known: set[str]) -> bool:
    if node_id in known:
        return True
    prefix = f"{node_id}["
    return any(item.startswith(prefix) for item in known)


def _ci_resolves(demo: str, known: dict[str, set[str]]) -> bool:
    rest = demo.removeprefix("ci:")
    if " / " not in rest:
        return False
    job, step = rest.split(" / ", 1)
    return step in known.get(job, set())


def _mutation_errors(row: Row) -> list[str]:
    if row.mutation == "none" or not row.mutation.strip():
        return [f"criterion {row.number}: mutation hunk does not apply"]
    completed = subprocess.run(
        ["git", "apply", "--check", "-"],
        input=row.mutation if row.mutation.endswith("\n") else f"{row.mutation}\n",
        cwd=_REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return [f"criterion {row.number}: mutation hunk does not apply"]
    return []


def _performer_errors(row: Row) -> list[str]:
    if row.performed_by and row.performed_by != "none":
        return []
    return [f"criterion {row.number}: no performer"]


def _collect_pytest_ids() -> set[str]:
    completed = subprocess.run(
        ["uv", "run", "pytest", "--collect-only", "-q"],
        cwd=_REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        line.split()[0]
        for line in completed.stdout.splitlines()
        if line.startswith("tests/") and "::" in line
    }


def _broken_control_row() -> Row:
    """One-row fixture whose hunk cannot apply. Never written to a matrix file."""
    return Row(
        number=1,
        state="complete",
        demonstrators=(
            "pytest:tests/test_git_clean.py::test_startup_writes_nothing_new_to_tracked_tree",
        ),
        mutation=(
            "diff --git a/README.md b/README.md\n"
            "--- a/README.md\n"
            "+++ b/README.md\n"
            "@@ -1,3 +1,2 @@\n"
            "-this-line-is-not-in-the-file-and-must-not-apply\n"
            " # rheoStream\n"
            "\n"
        ),
        performed_by="C10 (positive control)",
    )


_Known = tuple[tuple[tuple[Path, list[Row]], ...], set[str], dict[str, set[str]]]

_KNOWN: _Known | None = None


def _known() -> _Known:
    global _KNOWN
    if _KNOWN is None:
        _KNOWN = (
            tuple((path, parse(path)) for path, _ in MATRICES),
            _collect_pytest_ids(),
            parse_ci_steps(_WORKFLOW),
        )
    return _KNOWN


def _rows_of(path: Path) -> list[Row]:
    parsed, _, _ = _known()
    return next(rows for candidate, rows in parsed if candidate == path)


@pytest.mark.parametrize(("path", "expected"), MATRICES, ids=_MATRIX_IDS)
def test_each_live_matrix_is_clean(path: Path, expected: frozenset[int]) -> None:
    _, known_pytest_ids, known_ci_steps = _known()
    rows = _rows_of(path)
    assert {row.number for row in rows} == expected
    assert not any(
        demo.startswith("vitest:") for row in rows for demo in row.demonstrators
    )
    deferred = {row.number: row for row in rows if row.state == "deferred"}
    assert set(deferred) == set()
    assert all(
        row.demonstrators == () and row.mutation == "none" for row in deferred.values()
    )
    assert (
        validate(
            rows,
            expected=expected,
            known_pytest_ids=known_pytest_ids,
            known_ci_steps=known_ci_steps,
        )
        == []
    )


def test_every_state_is_one_of_the_three_the_grammar_defines() -> None:
    """``complete | partial | deferred`` and nothing else, across both files.

    Written when phase two seeded its second ``partial`` row: the temptation at
    that point was a fourth token for "held pending a public amendment", and this
    is what makes inventing one a test failure rather than a quiet precedent.
    """
    parsed, _, _ = _known()
    states = {row.state for _, rows in parsed for row in rows}
    assert states <= {"complete", "partial", "deferred"}, states


def test_a_malformed_demonstrator_line_is_rejected() -> None:
    with pytest.raises(ValueError, match="malformed demonstrator line"):
        _demonstrators("- `pytest:tests/foo.py::test_ok`\nnot-a-bullet")


def test_the_positive_control_is_live() -> None:
    """Check 3 is a real ``git apply --check``, not a stub that always returns clean."""
    _, known_pytest_ids, known_ci_steps = _known()
    control = _broken_control_row()
    errors = validate(
        [control],
        expected=EXPECTED_CRITERIA,
        known_pytest_ids=known_pytest_ids,
        known_ci_steps=known_ci_steps,
    )
    assert errors
    assert any(
        f"criterion {control.number}" in error
        and "mutation hunk does not apply" in error
        for error in errors
    ), errors


@pytest.mark.parametrize(("path", "expected"), MATRICES, ids=_MATRIX_IDS)
def test_a_renamed_demonstrator_is_rejected_naming_the_row(
    path: Path, expected: frozenset[int]
) -> None:
    _, known_pytest_ids, known_ci_steps = _known()
    rows = _rows_of(path)
    target = next(row for row in rows if row.state != "deferred" and row.demonstrators)
    broken = replace(
        target,
        demonstrators=(f"{target.demonstrators[0]}x", *target.demonstrators[1:]),
    )
    copied = [broken if row.number == target.number else row for row in rows]
    errors = validate(
        copied,
        expected=expected,
        known_pytest_ids=known_pytest_ids,
        known_ci_steps=known_ci_steps,
    )
    assert errors
    assert any(f"criterion {target.number}" in error for error in errors), errors


@pytest.mark.parametrize(("path", "expected"), MATRICES, ids=_MATRIX_IDS)
def test_a_deleted_row_fails_completeness(path: Path, expected: frozenset[int]) -> None:
    _, known_pytest_ids, known_ci_steps = _known()
    rows = _rows_of(path)
    removed = rows[0]
    errors = validate(
        [row for row in rows if row.number != removed.number],
        expected=expected,
        known_pytest_ids=known_pytest_ids,
        known_ci_steps=known_ci_steps,
    )
    assert errors
    assert any("completeness: missing" in error for error in errors), errors


@pytest.mark.parametrize(("path", "expected"), MATRICES, ids=_MATRIX_IDS)
def test_a_fabricated_extra_row_fails_completeness(
    path: Path, expected: frozenset[int]
) -> None:
    """The number is outside **both** sets, which phase two made load-bearing.

    Phase one's own version of this fixture used 25, on the reasoning that a
    twenty-fifth row could only be a fabrication. 25 is now criterion 25's real
    home in ``phase-2-matrix.md``, so the same number would prove nothing against
    that file's expected set.
    """
    _, known_pytest_ids, known_ci_steps = _known()
    rows = _rows_of(path)
    unclaimed = 9999
    assert not any(unclaimed in members for _, members in MATRICES)
    extra = replace(rows[0], number=unclaimed)
    errors = validate(
        [*rows, extra],
        expected=expected,
        known_pytest_ids=known_pytest_ids,
        known_ci_steps=known_ci_steps,
    )
    assert errors
    assert any("completeness: unexpected" in error for error in errors), errors


def test_no_criterion_number_appears_in_more_than_one_matrix() -> None:
    parsed, _, _ = _known()
    assert cross_file_errors(parsed) == []


def test_a_number_written_into_both_matrices_is_reported_naming_both() -> None:
    """The cross-file check's positive control.

    Neither file's own completeness check can see this: phase two's set holds 24,
    so a 24 row there is expected, and a 24 row copied into phase one is
    ``unexpected`` only there. Duplicating a number each file **does** claim — the
    case a copy-paste actually produces — is invisible to both, which is why this
    check exists and why its control duplicates a legitimately-owned number rather
    than an invented one.
    """
    parsed, _, _ = _known()
    (phase_one_path, phase_one_rows), (phase_two_path, phase_two_rows) = parsed
    intruder = replace(phase_one_rows[0], number=24)
    errors = cross_file_errors(
        [
            (phase_one_path, [*phase_one_rows, intruder]),
            (phase_two_path, phase_two_rows),
        ]
    )
    assert errors
    assert any(
        "24" in error and phase_one_path.name in error and phase_two_path.name in error
        for error in errors
    ), errors
