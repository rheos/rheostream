"""AC 23 / AC 24: the mechanical guard over the finished phase-one matrix.

``parse`` reads ``docs/acceptance/phase-1-matrix.md``. ``validate`` asserts exactly
four things, and no more: completeness both ways over ``{1..23} ∪ {69}``; every
non-``deferred`` demonstrator still resolves (``pytest:`` against
``pytest --collect-only``, ``ci:`` as a bound job/step pair); every
non-``deferred`` mutation hunk still applies (``git apply --check``); every
non-``deferred`` row names a performer.

A ``deferred`` row (15, 16) is routed past checks 2 and 3 the moment ``State``
reads ``deferred``. Its ``Demonstrator`` and ``Mutation`` fields hold the literal
``none`` by grammar; they are never resolved and never passed to ``git apply``.
A ``partial`` row (17) is exempt from nothing.

``vitest:`` stays in the schema and is not resolved here: no matrix row uses it,
so ``validate`` takes no ``known_vitest_ids``. The positive control and the three
direct demonstrations mutate an in-memory copy; the committed matrix is never
written wrong.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MATRIX = _REPO_ROOT / "docs" / "acceptance" / "phase-1-matrix.md"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "repository-checks.yml"

EXPECTED_CRITERIA = frozenset(range(1, 24)) | {69}

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
    rows: list[Row],
    *,
    known_pytest_ids: set[str],
    known_ci_steps: dict[str, set[str]],
) -> list[str]:
    """Return errors; empty means the in-memory matrix is clean."""
    errors: list[str] = []
    errors.extend(_completeness_errors(rows))
    for row in rows:
        if row.state == "deferred":
            continue
        errors.extend(_demonstrator_errors(row, known_pytest_ids, known_ci_steps))
        errors.extend(_mutation_errors(row))
        errors.extend(_performer_errors(row))
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


def _completeness_errors(rows: list[Row]) -> list[str]:
    numbers = [row.number for row in rows]
    got = set(numbers)
    errors: list[str] = []
    missing = sorted(EXPECTED_CRITERIA - got)
    extra = sorted(got - EXPECTED_CRITERIA)
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
    """One-row fixture whose hunk cannot apply. Never written to the matrix file."""
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


_KNOWN: tuple[list[Row], set[str], dict[str, set[str]]] | None = None


def _known() -> tuple[list[Row], set[str], dict[str, set[str]]]:
    global _KNOWN
    if _KNOWN is None:
        _KNOWN = (parse(_MATRIX), _collect_pytest_ids(), parse_ci_steps(_WORKFLOW))
    return _KNOWN


def test_the_live_matrix_is_clean() -> None:
    rows, known_pytest_ids, known_ci_steps = _known()
    assert {row.number for row in rows} == EXPECTED_CRITERIA
    assert not any(
        demo.startswith("vitest:") for row in rows for demo in row.demonstrators
    )
    deferred = {row.number: row for row in rows if row.state == "deferred"}
    assert set(deferred) == {15, 16}
    assert all(
        row.demonstrators == () and row.mutation == "none" for row in deferred.values()
    )
    assert (
        validate(rows, known_pytest_ids=known_pytest_ids, known_ci_steps=known_ci_steps)
        == []
    )


def test_a_malformed_demonstrator_line_is_rejected() -> None:
    with pytest.raises(ValueError, match="malformed demonstrator line"):
        _demonstrators("- `pytest:tests/foo.py::test_ok`\nnot-a-bullet")


def test_the_positive_control_is_live() -> None:
    """Check 3 is a real ``git apply --check``, not a stub that always returns clean."""
    _, known_pytest_ids, known_ci_steps = _known()
    control = _broken_control_row()
    errors = validate(
        [control], known_pytest_ids=known_pytest_ids, known_ci_steps=known_ci_steps
    )
    assert errors
    assert any(
        f"criterion {control.number}" in error
        and "mutation hunk does not apply" in error
        for error in errors
    ), errors


def test_a_renamed_demonstrator_is_rejected_naming_the_row() -> None:
    rows, known_pytest_ids, known_ci_steps = _known()
    target = next(row for row in rows if row.state != "deferred" and row.demonstrators)
    broken = replace(
        target,
        demonstrators=(f"{target.demonstrators[0]}x", *target.demonstrators[1:]),
    )
    copied = [broken if row.number == target.number else row for row in rows]
    errors = validate(
        copied, known_pytest_ids=known_pytest_ids, known_ci_steps=known_ci_steps
    )
    assert errors
    assert any(f"criterion {target.number}" in error for error in errors), errors


def test_a_deleted_row_fails_completeness() -> None:
    rows, known_pytest_ids, known_ci_steps = _known()
    removed = rows[0]
    errors = validate(
        [row for row in rows if row.number != removed.number],
        known_pytest_ids=known_pytest_ids,
        known_ci_steps=known_ci_steps,
    )
    assert errors
    assert any("completeness: missing" in error for error in errors), errors


def test_a_fabricated_25th_row_fails_completeness() -> None:
    rows, known_pytest_ids, known_ci_steps = _known()
    extra = replace(rows[0], number=25)
    errors = validate(
        [*rows, extra],
        known_pytest_ids=known_pytest_ids,
        known_ci_steps=known_ci_steps,
    )
    assert errors
    assert any("completeness: unexpected" in error for error in errors), errors
