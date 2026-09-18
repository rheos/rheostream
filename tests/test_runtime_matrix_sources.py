"""AC 10: matrix demonstrators for criteria 15–17 never invoke a live ``claude``."""

from __future__ import annotations

import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_DEMONSTRATORS = (
    _ROOT / "tests" / "postgres" / "test_runtime_matrix.py",
    _ROOT / "tests" / "postgres" / "test_runtime_continuation_binding.py",
    _ROOT / "tests" / "harness" / "runtime_matrix.py",
)

_FORBIDDEN = (
    'which("claude")',
    "shutil.which('claude')",
    'shutil.which("claude")',
    "/usr/bin/claude",
    "PATH claude",
    'executable", "claude"',
    "time.sleep",
)

_AC8_LEAK = _ROOT / "tests" / "fixtures" / "ac8-credential-leak.diff"


def test_new_matrix_demonstrators_do_not_invoke_a_live_claude_binary() -> None:
    for path in _DEMONSTRATORS:
        text = path.read_text(encoding="utf-8")
        for token in _FORBIDDEN:
            assert token not in text, f"{path.name} contains {token!r}"
        assert "ClaudeCliRuntime" not in text
        assert "rheo_app_worker" not in text


def test_ac8_leak_hunk_still_applies() -> None:
    completed = subprocess.run(
        ["git", "apply", "--check", str(_AC8_LEAK)],
        cwd=_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
