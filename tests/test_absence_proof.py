"""Mutation controls for the two-configuration absence proof."""

from __future__ import annotations

import importlib.util
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "absence_proof.py"
_MATRIX_TEST = _REPO_ROOT / "tests" / "test_acceptance_matrix.py"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "repository-checks.yml"
_NODE = "tests/probe.py::test_one"
_CI = "ci:python / Pytest"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


absence = _load(_SCRIPT, "_absence_proof_under_test")
matrix = _load(_MATRIX_TEST, "_acceptance_matrix_for_absence_test")


def _summary() -> dict[str, object]:
    return {
        "selected": [_NODE],
        "collected": [_NODE],
        "outcomes": {_NODE: "passed"},
        "ci_excluded": [_CI],
    }


def test_compare_rejects_one_changed_outcome_and_accepts_the_identical_pair() -> None:
    first = _summary()
    good = deepcopy(first)
    bad = deepcopy(first)
    bad["outcomes"] = {_NODE: "failed"}

    kwargs = {"expected_selected": {_NODE}, "expected_ci_excluded": {_CI}}
    assert absence.comparison_exit_code(first, good, **kwargs) == 0
    assert absence.comparison_exit_code(first, bad, **kwargs) != 0

    both_failed = deepcopy(bad)
    assert absence.comparison_exit_code(bad, both_failed, **kwargs) == 0


def test_summary_rejects_empty_selected_and_accepts_nonempty_selected() -> None:
    good = _summary()
    bad = deepcopy(good)
    bad["selected"] = []
    bad["collected"] = []
    bad["outcomes"] = {}
    assert absence.summary_exit_code(good) == 0
    assert absence.summary_exit_code(bad) != 0


def test_summary_rejects_an_uncollected_selected_id() -> None:
    good = _summary()
    bad = deepcopy(good)
    bad["collected"] = []
    bad["outcomes"] = {}
    assert absence.summary_exit_code(good) == 0
    assert absence.summary_exit_code(bad) != 0


def test_summary_rejects_a_collected_id_without_an_outcome() -> None:
    good = _summary()
    bad = deepcopy(good)
    bad["outcomes"] = {}
    assert absence.summary_exit_code(good) == 0
    assert absence.summary_exit_code(bad) != 0


def test_summary_rejects_a_skipped_outcome() -> None:
    good = _summary()
    bad = deepcopy(good)
    bad["outcomes"] = {_NODE: "skipped"}
    assert absence.summary_exit_code(good) == 0
    assert absence.summary_exit_code(bad) != 0


def test_checkout_rejects_dirty_status_and_accepts_clean_status() -> None:
    assert absence.checkout_status_exit_code("") == 0
    assert absence.checkout_status_exit_code("?? uncommitted.py\n") != 0
    assert absence.checkout_status_errors("?? uncommitted.py\n")[-1] == (
        "commit before running the absence proof"
    )


def test_workflow_runs_the_absence_proof_beside_a_known_positive_control() -> None:
    jobs = matrix.parse_ci_steps(_WORKFLOW)
    assert "python" in jobs and "Pytest" in jobs["python"]
    assert "absence-proof" in jobs
    assert "Run make absence-proof" in jobs["absence-proof"]
    assert (
        "      - name: Run make absence-proof\n        run: make absence-proof"
    ) in _WORKFLOW.read_text(encoding="utf-8")
