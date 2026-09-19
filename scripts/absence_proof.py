#!/usr/bin/env python3
"""Compare phase-one acceptance outcomes with Recallatron present and absent."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MATRIX = _REPO_ROOT / "docs" / "acceptance" / "phase-1-matrix.md"
_PARSER = _REPO_ROOT / "tests" / "test_acceptance_matrix.py"
_MODULE_DIR = Path("modules") / "recallatron"

Summary = dict[str, object]


def _load_matrix_parser(root: Path) -> ModuleType:
    path = root / "tests" / "test_acceptance_matrix.py"
    name = f"_rheo_acceptance_matrix_{abs(hash(path))}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load acceptance-matrix parser from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _matrix_selection(root: Path) -> tuple[set[str], set[str]]:
    parser = _load_matrix_parser(root)
    rows = parser.parse(root / "docs" / "acceptance" / "phase-1-matrix.md")
    selected = {
        demo.removeprefix("pytest:")
        for row in rows
        for demo in row.demonstrators
        if demo.startswith("pytest:")
    }
    ci_excluded = {
        demo for row in rows for demo in row.demonstrators if demo.startswith("ci:")
    }
    return selected, ci_excluded


def _resolves(selected_id: str, collected_id: str) -> bool:
    return collected_id == selected_id or collected_id.startswith(f"{selected_id}[")


class _Recorder:
    def __init__(self, selected: set[str]) -> None:
        self.selected = selected
        self.collected: set[str] = set()
        self.outcomes: dict[str, str] = {}

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.collected = {
            item.nodeid
            for item in session.items
            if any(_resolves(base, item.nodeid) for base in self.selected)
        }

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.nodeid not in self.collected:
            return
        if report.failed:
            self.outcomes[report.nodeid] = (
                "failed" if report.when == "call" else "error"
            )
        elif report.skipped:
            self.outcomes[report.nodeid] = "skipped"
        elif report.when == "call" and report.passed:
            self.outcomes[report.nodeid] = "passed"


def _string_set(summary: Mapping[str, object], key: str) -> set[str]:
    value = summary.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return set()
    return set(value)


def _outcome_map(summary: Mapping[str, object]) -> dict[str, str]:
    value = summary.get("outcomes")
    if not isinstance(value, dict):
        return {}
    return {
        key: outcome
        for key, outcome in value.items()
        if isinstance(key, str) and isinstance(outcome, str)
    }


def summary_errors(summary: Mapping[str, object]) -> list[str]:
    selected = _string_set(summary, "selected")
    collected = _string_set(summary, "collected")
    outcomes = _outcome_map(summary)
    errors: list[str] = []
    if not selected:
        errors.append("selected is empty")
    unresolved = sorted(
        base
        for base in selected
        if not any(_resolves(base, node_id) for node_id in collected)
    )
    unexpected = sorted(
        node_id
        for node_id in collected
        if not any(_resolves(base, node_id) for base in selected)
    )
    if unresolved:
        errors.append(f"selected ids not collected: {unresolved}")
    if unexpected:
        errors.append(f"collected ids not selected: {unexpected}")
    missing_outcomes = sorted(collected - set(outcomes))
    extra_outcomes = sorted(set(outcomes) - collected)
    if missing_outcomes:
        errors.append(f"collected ids without outcomes: {missing_outcomes}")
    if extra_outcomes:
        errors.append(f"outcomes for uncollected ids: {extra_outcomes}")
    skipped = sorted(
        node_id for node_id, outcome in outcomes.items() if outcome == "skipped"
    )
    if skipped:
        errors.append(f"skipped outcomes: {skipped}")
    unknown = sorted(
        f"{node_id}={outcome}"
        for node_id, outcome in outcomes.items()
        if outcome not in {"passed", "failed", "error", "skipped"}
    )
    if unknown:
        errors.append(f"unknown outcomes: {unknown}")
    return errors


def comparison_errors(
    first: Mapping[str, object],
    second: Mapping[str, object],
    *,
    expected_selected: set[str],
    expected_ci_excluded: set[str],
) -> list[str]:
    errors = [f"first: {error}" for error in summary_errors(first)]
    errors.extend(f"second: {error}" for error in summary_errors(second))
    first_selected = _string_set(first, "selected")
    second_selected = _string_set(second, "selected")
    if first_selected != second_selected:
        errors.append("selected sets differ between configurations")
    if first_selected != expected_selected:
        errors.append("first selected set differs from the acceptance matrix")
    if second_selected != expected_selected:
        errors.append("second selected set differs from the acceptance matrix")
    first_ci = _string_set(first, "ci_excluded")
    second_ci = _string_set(second, "ci_excluded")
    if first_ci != expected_ci_excluded or second_ci != expected_ci_excluded:
        errors.append("ci_excluded differs from the acceptance matrix")
    if _string_set(first, "collected") != _string_set(second, "collected"):
        errors.append("collected node ids differ between configurations")
    first_outcomes = _outcome_map(first)
    second_outcomes = _outcome_map(second)
    differing = sorted(
        node_id
        for node_id in set(first_outcomes) | set(second_outcomes)
        if first_outcomes.get(node_id) != second_outcomes.get(node_id)
    )
    if differing:
        errors.append(f"outcomes differ for node ids: {differing}")
    return errors


def checkout_status_errors(status: str) -> list[str]:
    paths = [line for line in status.splitlines() if line.strip()]
    if not paths:
        return []
    return [*paths, "commit before running the absence proof"]


def summary_exit_code(summary: Mapping[str, object]) -> int:
    return int(bool(summary_errors(summary)))


def comparison_exit_code(
    first: Mapping[str, object],
    second: Mapping[str, object],
    *,
    expected_selected: set[str],
    expected_ci_excluded: set[str],
) -> int:
    return int(
        bool(
            comparison_errors(
                first,
                second,
                expected_selected=expected_selected,
                expected_ci_excluded=expected_ci_excluded,
            )
        )
    )


def checkout_status_exit_code(status: str) -> int:
    return int(bool(checkout_status_errors(status)))


def _write_summary(path: Path, summary: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _run(root: Path, out: Path) -> int:
    selected, ci_excluded = _matrix_selection(root)
    if not selected:
        summary: Summary = {
            "selected": [],
            "collected": [],
            "outcomes": {},
            "ci_excluded": sorted(ci_excluded),
        }
        _write_summary(out, summary)
        print("selected is empty", file=sys.stderr)
        return 1

    recorder = _Recorder(selected)
    pytest.main(["-q", *sorted(selected)], plugins=[recorder])
    summary = {
        "selected": sorted(selected),
        "collected": sorted(recorder.collected),
        "outcomes": dict(sorted(recorder.outcomes.items())),
        "ci_excluded": sorted(ci_excluded),
    }
    _write_summary(out, summary)
    errors = summary_errors(summary)
    for error in errors:
        print(error, file=sys.stderr)
    print(f"wrote {out}: {len(selected)} selected, {len(recorder.collected)} collected")
    return int(bool(errors))


def _configured_run(out: Path) -> int:
    previous_installed = os.environ.get("RHEO__modules__installed")
    previous_data_root = os.environ.get("RHEO_DATA_ROOT")
    try:
        os.environ["RHEO__modules__installed"] = ""
        with tempfile.TemporaryDirectory(prefix="rheo-absence-config-") as data_root:
            os.environ["RHEO_DATA_ROOT"] = data_root
            return _run(_REPO_ROOT, out)
    finally:
        if previous_installed is None:
            os.environ.pop("RHEO__modules__installed", None)
        else:
            os.environ["RHEO__modules__installed"] = previous_installed
        if previous_data_root is None:
            os.environ.pop("RHEO_DATA_ROOT", None)
        else:
            os.environ["RHEO_DATA_ROOT"] = previous_data_root


def _completed(
    command: Sequence[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, check=False)


def _checkout_run(out: Path) -> int:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=_REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if status.returncode != 0:
        print(status.stderr, file=sys.stderr, end="")
        return status.returncode
    dirty = checkout_status_errors(status.stdout)
    if dirty:
        print("working tree is not clean:", file=sys.stderr)
        for line in dirty:
            print(line, file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="rheo-absence-checkout-") as parent:
        checkout = Path(parent) / "checkout"
        added = False
        result = 1
        try:
            added_result = _completed(
                ["git", "worktree", "add", "--detach", str(checkout), "HEAD"],
                cwd=_REPO_ROOT,
            )
            if added_result.returncode != 0:
                return added_result.returncode
            added = True
            shutil.rmtree(checkout / _MODULE_DIR)
            synced = _completed(["uv", "sync"], cwd=checkout)
            if synced.returncode != 0:
                return synced.returncode
            environment = os.environ.copy()
            environment["RHEO__modules__installed"] = ""
            with tempfile.TemporaryDirectory(prefix="rheo-absence-data-") as data_root:
                environment["RHEO_DATA_ROOT"] = data_root
                ran = subprocess.run(
                    [
                        "uv",
                        "run",
                        "--directory",
                        str(checkout),
                        "python",
                        "scripts/absence_proof.py",
                        "--mode",
                        "run",
                        "--out",
                        str(out.resolve()),
                    ],
                    cwd=checkout,
                    env=environment,
                    check=False,
                )
            result = ran.returncode
        finally:
            if added:
                removed = _completed(
                    ["git", "worktree", "remove", "--force", str(checkout)],
                    cwd=_REPO_ROOT,
                )
                if removed.returncode != 0:
                    result = removed.returncode
        return result


def _read_summary(path: Path) -> Summary:
    value: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"summary at {path} is not an object")
    return value


def _compare(paths: Sequence[Path]) -> int:
    if len(paths) != 2:
        print("--mode compare requires exactly two summary paths", file=sys.stderr)
        return 2
    first = _read_summary(paths[0])
    second = _read_summary(paths[1])
    expected_selected, expected_ci = _matrix_selection(_REPO_ROOT)
    errors = comparison_errors(
        first,
        second,
        expected_selected=expected_selected,
        expected_ci_excluded=expected_ci,
    )
    for node_id, outcome in sorted(_outcome_map(first).items()):
        if outcome != "passed":
            print(f"non-passed outcome: {node_id} = {outcome}")
    for error in errors:
        print(error, file=sys.stderr)
    if not errors:
        print("absence proof summaries are identical")
    return int(bool(errors))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode", required=True, choices=("run", "config", "checkout", "compare")
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument("summaries", nargs="*", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "compare":
        return _compare(args.summaries)
    if args.summaries:
        parser.error("summary paths are accepted only by --mode compare")
    if args.out is None:
        parser.error(f"--mode {args.mode} requires --out")
    if args.mode == "run":
        return _run(_REPO_ROOT, args.out)
    if args.mode == "config":
        return _configured_run(args.out)
    return _checkout_run(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
