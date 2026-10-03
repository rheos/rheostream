"""The stdlib-only first-message recall hook (issue #292).

Every test runs against a temporary ``bridge_home`` with a fake fetch; no request
leaves the process. Tokens, refs and memories are invented.
"""

from __future__ import annotations

import ast
import io
import json
import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from rheo_bridge import recall_hook

HOOK_SOURCE = Path(recall_hook.__file__)
API_URL = "https://api.example.org"
TOKEN = "rbt_" + "x" * 43
SALT = "5a17" * 8
SESSION = "11111111-2222-4333-8444-555555555555"
INTERACTIVE = {"CLAUDE_CODE_ENTRYPOINT": "cli"}
NOW = 1_790_000_000.0


def item(n: int, score: float | None, **extra: Any) -> dict[str, Any]:
    return {
        "ref": f"recallatron.memory:example-{n}",
        "kind": "fact",
        "title": f"Example memory {n}",
        "body": f"Body of example memory {n}.",
        "recorded_at": "2026-09-30T12:00:00Z",
        "invalidated_at": None,
        "rerank_score": score,
        **extra,
    }


def answer(*items: dict[str, Any]) -> bytes:
    return json.dumps({"state": "succeeded", "result": {"items": list(items)}}).encode()


class FakeFetch:
    def __init__(self, body: bytes | Exception) -> None:
        self.body = body
        self.calls: list[tuple[str, dict[str, Any], dict[str, str], float]] = []

    def __call__(
        self, url: str, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> bytes:
        self.calls.append((url, json.loads(body), dict(headers), timeout))
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


@pytest.fixture
def home(tmp_path: Path) -> Path:
    bridge_home = tmp_path / ".rheo-bridge"
    bridge_home.mkdir(mode=0o700)
    write_config(bridge_home)
    token = bridge_home / "recall-token"
    token.write_text(TOKEN)
    os.chmod(token, 0o600)
    return bridge_home


def write_config(bridge_home: Path, **overrides: Any) -> None:
    config = {
        "api_url": API_URL,
        "install_salt": SALT,
        "recall_enabled": True,
        **overrides,
    }
    (bridge_home / "config.json").write_text(json.dumps(config))


def run(
    bridge_home: Path,
    fetch: FakeFetch,
    *,
    prompt: str = "how do we deploy the flagship",
    session: str = SESSION,
    environ: Mapping[str, str] = INTERACTIVE,
    raw: bytes | None = None,
) -> str:
    stdin = io.BytesIO(
        raw
        if raw is not None
        else json.dumps({"prompt": prompt, "session_id": session}).encode()
    )
    stdout = io.StringIO()
    code = recall_hook.main(
        stdin=stdin,
        stdout=stdout,
        bridge_home=bridge_home,
        environ=environ,
        fetch_fn=fetch,
        now=NOW,
    )
    assert code == 0
    return stdout.getvalue()


def context_of(output: str) -> str:
    document = json.loads(output)
    assert document["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    context: str = document["hookSpecificOutput"]["additionalContext"]
    return context


# --- what is injected --------------------------------------------------------------


def test_relevant_items_are_injected_with_kind_date_title_body_and_ref(
    home: Path,
) -> None:
    fetch = FakeFetch(answer(item(1, 3.2), item(2, 0.0)))
    context = context_of(run(home, fetch))
    assert context.startswith(recall_hook.HEADER)
    assert (
        "- [fact, recorded 2026-09-30] Example memory 1: Body of example memory 1. "
        "(recallatron.memory:example-1)"
    ) in context
    assert "Example memory 2" in context


def test_the_request_carries_the_query_k_and_the_recall_token(home: Path) -> None:
    fetch = FakeFetch(answer(item(1, 1.0)))
    run(home, fetch, prompt="  what did we decide about backups?  ")
    ((url, body, headers, timeout),) = fetch.calls
    assert url == API_URL + "/api/v1/operations/recallatron.memory.recall"
    assert body == {"query": "what did we decide about backups?", "k": 5}
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert timeout == 3.0


def test_the_query_is_cut_to_its_maximum(home: Path) -> None:
    fetch = FakeFetch(answer())
    run(home, fetch, prompt="word " * 1000)
    ((_, body, _, _),) = fetch.calls
    assert len(body["query"]) == recall_hook.QUERY_MAX_CHARS


def test_items_below_the_score_floor_are_dropped(home: Path) -> None:
    fetch = FakeFetch(answer(item(1, 2.0), item(2, -0.01), item(3, -8.6)))
    context = context_of(run(home, fetch))
    assert "Example memory 1" in context
    assert "Example memory 2" not in context and "Example memory 3" not in context


def test_a_configured_floor_is_honoured(home: Path) -> None:
    write_config(home, recall_min_score=2.5)
    fetch = FakeFetch(answer(item(1, 3.0), item(2, 2.0)))
    context = context_of(run(home, fetch))
    assert "Example memory 1" in context and "Example memory 2" not in context


@pytest.mark.parametrize(
    "items",
    [
        (item(1, None),),
        (item(1, -0.5), item(2, -3.0)),
        (item(1, 5.0, invalidated_at="2026-09-30T00:00:00Z"),),
        (item(1, True),),
        (),
    ],
    ids=["no-reranker", "all-below-floor", "invalidated", "bool-score", "empty"],
)
def test_nothing_relevant_injects_nothing(home: Path, items: tuple[Any, ...]) -> None:
    assert run(home, FakeFetch(answer(*items))) == ""


def test_bodies_are_trimmed_and_the_whole_block_respects_max_chars(home: Path) -> None:
    write_config(home, recall_max_chars=700)
    long = " ".join(["detail"] * 400)
    fetch = FakeFetch(answer(*(item(n, 1.0, body=long) for n in range(1, 6))))
    context = context_of(run(home, fetch))
    assert len(context) <= 700
    first = context.splitlines()[1]
    assert "..." in first
    assert len(first) < recall_hook.BODY_MAX_CHARS + 120


def test_whitespace_in_titles_and_bodies_is_flattened(home: Path) -> None:
    fetch = FakeFetch(answer(item(1, 1.0, title="Two\nlines", body="a\n\n  b\tc")))
    context = context_of(run(home, fetch))
    assert "Two lines: a b c" in context
    assert len(context.splitlines()) == 2


# --- once per session --------------------------------------------------------------


def test_only_the_first_prompt_of_a_session_recalls(home: Path) -> None:
    fetch = FakeFetch(answer(item(1, 1.0)))
    assert run(home, fetch) != ""
    assert run(home, fetch, prompt="and another thing") == ""
    assert len(fetch.calls) == 1
    assert run(home, fetch, session="99999999-2222-4333-8444-555555555555") != ""
    assert len(fetch.calls) == 2


def test_a_failed_lookup_still_uses_up_the_session(home: Path) -> None:
    fetch = FakeFetch(TimeoutError("timed out"))
    assert run(home, fetch) == ""
    assert run(home, fetch, prompt="second") == ""
    assert len(fetch.calls) == 1


def test_a_slash_command_does_not_use_up_the_session(home: Path) -> None:
    fetch = FakeFetch(answer(item(1, 1.0)))
    assert run(home, fetch, prompt="/model") == ""
    assert fetch.calls == []
    assert run(home, fetch, prompt="now the real question") != ""


def test_the_marker_is_private_and_names_no_session(home: Path) -> None:
    run(home, FakeFetch(answer()))
    markers = home / recall_hook.MARKER_DIR
    assert stat.S_IMODE(markers.stat().st_mode) == 0o700
    (marker,) = list(markers.iterdir())
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert SESSION not in marker.name
    assert marker.read_bytes() == b""


def test_old_markers_are_pruned(home: Path) -> None:
    markers = home / recall_hook.MARKER_DIR
    markers.mkdir(mode=0o700)
    old = markers / ("0" * 32)
    old.write_bytes(b"")
    stale = NOW - recall_hook.MARKER_MAX_AGE_SECONDS - 60
    os.utime(old, (stale, stale))
    run(home, FakeFetch(answer()))
    assert not old.exists()
    assert len(list(markers.iterdir())) == 1


# --- gates -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"},
        {"CLAUDE_CODE_ENTRYPOINT": "cli", "RHEO_RECALL_DISABLE": "1"},
    ],
    ids=["no-entrypoint", "headless", "opted-out"],
)
def test_non_interactive_or_opted_out_sessions_get_nothing(
    home: Path, environ: dict[str, str]
) -> None:
    fetch = FakeFetch(answer(item(1, 1.0)))
    assert run(home, fetch, environ=environ) == ""
    assert fetch.calls == []
    assert not (home / recall_hook.MARKER_DIR).exists()


def test_desktop_sessions_recall(home: Path) -> None:
    fetch = FakeFetch(answer(item(1, 1.0)))
    assert run(home, fetch, environ={"CLAUDE_CODE_ENTRYPOINT": "claude-desktop"})


@pytest.mark.parametrize(
    "config",
    [
        {"recall_enabled": False},
        {"recall_enabled": "true"},
        {"api_url": "http://api.example.org"},
        {"install_salt": ""},
    ],
    ids=["off", "not-a-bool", "plain-http", "no-salt"],
)
def test_an_unusable_config_does_nothing(home: Path, config: dict[str, Any]) -> None:
    write_config(home, **config)
    fetch = FakeFetch(answer(item(1, 1.0)))
    assert run(home, fetch) == ""
    assert fetch.calls == []


def test_no_config_or_no_token_does_nothing(home: Path) -> None:
    (home / "recall-token").unlink()
    fetch = FakeFetch(answer(item(1, 1.0)))
    assert run(home, fetch) == ""
    (home / "config.json").unlink()
    assert run(home, fetch) == ""
    assert fetch.calls == []


@pytest.mark.parametrize(
    "token", ["", "two words", "tab\tinside", "x" * (16 * 1024 + 1)], ids=repr
)
def test_a_malformed_token_does_nothing(home: Path, token: str) -> None:
    (home / "recall-token").write_text(token)
    fetch = FakeFetch(answer(item(1, 1.0)))
    assert run(home, fetch) == ""
    assert fetch.calls == []


def test_a_symlinked_token_is_refused(home: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text(TOKEN)
    (home / "recall-token").unlink()
    (home / "recall-token").symlink_to(elsewhere)
    fetch = FakeFetch(answer(item(1, 1.0)))
    assert run(home, fetch) == ""
    assert fetch.calls == []


# --- failures are silent -----------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        OSError("connection refused"),
        TimeoutError("timed out"),
        b"not json",
        json.dumps({"state": "refused"}).encode(),
        json.dumps({"state": "succeeded", "result": {"items": "nope"}}).encode(),
        json.dumps([1, 2]).encode(),
    ],
    ids=["refused-connection", "timeout", "not-json", "refusal", "bad-items", "list"],
)
def test_a_failed_or_malformed_answer_prints_nothing(
    home: Path, body: bytes | Exception
) -> None:
    assert run(home, FakeFetch(body)) == ""


@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b"[1]", json.dumps({"prompt": "hi"}).encode()],
    ids=["empty", "not-json", "list", "no-session"],
)
def test_a_malformed_payload_prints_nothing(home: Path, raw: bytes) -> None:
    fetch = FakeFetch(answer(item(1, 1.0)))
    assert run(home, fetch, raw=raw) == ""
    assert fetch.calls == []


def test_an_oversize_payload_prints_nothing(home: Path) -> None:
    fetch = FakeFetch(answer(item(1, 1.0)))
    raw = b" " * (recall_hook.STDIN_CAP + 1)
    assert run(home, fetch, raw=raw) == ""
    assert fetch.calls == []


# --- the script as Claude Code runs it ------------------------------------------------


def test_the_script_exits_0_and_prints_nothing_when_unconfigured(
    tmp_path: Path,
) -> None:
    import subprocess

    bridge_home = tmp_path / ".rheo-bridge"
    bridge_home.mkdir()
    script = bridge_home / "recall_hook.py"
    script.write_bytes(HOOK_SOURCE.read_bytes())
    done = subprocess.run(
        [sys.executable, str(script), "UserPromptSubmit"],
        input=json.dumps({"prompt": "hi", "session_id": SESSION}).encode(),
        capture_output=True,
        env={"CLAUDE_CODE_ENTRYPOINT": "cli", "PATH": os.environ.get("PATH", "")},
        timeout=60,
        check=False,
    )
    assert done.returncode == 0
    assert done.stdout == b"" and done.stderr == b""


def test_the_hook_imports_only_the_standard_library() -> None:
    tree = ast.parse(HOOK_SOURCE.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module is not None
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names) | {"__future__"}, roots - set(
        sys.stdlib_module_names
    )


# --- the total deadline --------------------------------------------------------------


def test_a_slow_lookup_is_abandoned_at_the_deadline(home: Path) -> None:
    import time as _time

    write_config(home, recall_timeout_seconds=1)

    class Slow(FakeFetch):
        def __call__(
            self, url: str, body: bytes, headers: Mapping[str, str], timeout: float
        ) -> bytes:
            _time.sleep(5)
            return answer(item(1, 1.0))

    started = _time.monotonic()
    assert run(home, Slow(b"")) == ""
    assert _time.monotonic() - started < 3


def test_the_configured_timeout_is_capped(home: Path) -> None:
    write_config(home, recall_timeout_seconds=60)
    fetch = FakeFetch(answer(item(1, 1.0)))
    run(home, fetch)
    ((_, _, _, timeout),) = fetch.calls
    assert timeout == recall_hook.MAX_TIMEOUT_SECONDS
