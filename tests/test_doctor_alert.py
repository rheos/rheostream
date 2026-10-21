"""The scheduled doctor pager: a check that starts failing sends one page, a paged
check that recovers sends one notice (issue #229).

`deploy/doctor/rheostream-doctor-alert.py` runs as a subprocess against a stub
`docker` on PATH and a stub page command. No real Docker and no real sender: the
stub saves each message it reads on stdin to its own file and exits with the code
in STUB_PAGE_EXIT, so each test checks exactly what would have been sent.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import platform
import re
import shlex
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "doctor" / "rheostream-doctor-alert.py"
PROJECT = "exampleproject"
HOST = platform.node()

OK_REPORT = """\
ok   cluster: reachable; server 16.15
ok   workspace 0000 (example): active
ok   evidence acceptance 0000 (example): 0 of 0 evidence units settled in the last 24 h
"""
FAIL_CHECK = "evidence acceptance 0000 (example)"
FAIL_LINE = (
    "FAIL evidence acceptance 0000 (example): 1 of 3 evidence units settled in the "
    "last 24 h ended gap/acceptance_failed; the evidence of each is lost"
)
FAIL_REPORT = (
    "ok   cluster: reachable; server 16.15\n"
    f"{FAIL_LINE}\n"
    "ok   workspace 0000 (example): active\n"
)
FAIL_SLUG = "evidence-acceptance-0000-(example)"
FAIL_PAGE = f"rheo.stream doctor FAIL on {HOST}: {FAIL_LINE}"
FAIL_RECOVERY = f"rheo.stream doctor recovered on {HOST}: {FAIL_CHECK}"


def fake_docker(tmp_path: Path, *, containers: str, report: str, code: int) -> Path:
    """A stub `docker`: `ps` prints `containers`; `exec` prints `report`, exits `code`,
    and leaves a marker, so a test can prove doctor never ran."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (tmp_path / "report.txt").write_text(report)
    script = bindir / "docker"
    script.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = ps ]; then printf "%s" "{containers}"; exit 0; fi\n'
        f'if [ "$1" = exec ]; then touch "{tmp_path}/exec-ran"; '
        f'cat "{tmp_path}/report.txt"; exit {code}; fi\n'
        "exit 64\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return bindir


def stub_page_command(tmp_path: Path) -> Path:
    """A stub page command: saves stdin to the next free page-N file, then exits
    with STUB_PAGE_EXIT (default 0). It records failed attempts too."""
    pagedir = tmp_path / "pages"
    pagedir.mkdir(exist_ok=True)
    script = tmp_path / "page-stub"
    script.write_text(
        "#!/bin/sh\n"
        "i=0\n"
        f'while [ -e "{pagedir}/page-$i" ]; do i=$((i+1)); done\n'
        f'cat > "{pagedir}/page-$i"\n'
        'exit "${STUB_PAGE_EXIT:-0}"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def pages(tmp_path: Path) -> list[bytes]:
    """Every message the stub read, in order."""
    files = (tmp_path / "pages").glob("page-*")
    ordered = sorted(files, key=lambda p: int(p.name.removeprefix("page-")))
    return [p.read_bytes() for p in ordered]


def texts(tmp_path: Path) -> list[str]:
    return [raw.decode("utf-8") for raw in pages(tmp_path)]


def state_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "state.json"
    path.parent.mkdir(exist_ok=True)
    return path


def write_state(tmp_path: Path, paged: dict[str, str]) -> Path:
    path = state_path(tmp_path)
    path.write_text(json.dumps({"version": 1, "paged": paged}))
    return path


def read_paged(tmp_path: Path) -> dict[str, str]:
    data = json.loads(state_path(tmp_path).read_text())
    assert data["version"] == 1
    paged: dict[str, str] = data["paged"]
    return paged


def run(
    tmp_path: Path,
    bindir: Path,
    *args: str,
    page_exit: int = 0,
    command: str | None = None,
    state_file: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    if command is None:
        # An argument after the path proves the line is split, not run whole.
        command = f"{shlex.quote(str(stub_page_command(tmp_path)))} --page"
    env = {k: v for k, v in os.environ.items() if not k.startswith("RHEO_DOCTOR_")}
    env.update(
        {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "RHEO_DOCTOR_COMPOSE_PROJECT": PROJECT,
            "RHEO_DOCTOR_PAGE_COMMAND": command,
            "RHEO_DOCTOR_STATE_FILE": str(state_file or state_path(tmp_path)),
            "RHEO_DOCTOR_LOG": "stderr",
            "STUB_PAGE_EXIT": str(page_exit),
            **(extra_env or {}),
        }
    )
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def load_script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("doctor_alert", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass resolves its annotations through sys.modules.
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_all_ok_pages_nothing_and_exits_0(tmp_path: Path) -> None:
    write_state(tmp_path, {})
    bindir = fake_docker(tmp_path, containers="core-1", report=OK_REPORT, code=0)
    result = run(tmp_path, bindir)
    assert result.returncode == 0, result.stderr
    assert pages(tmp_path) == []
    assert read_paged(tmp_path) == {}


# AC 18: transitions.


def test_first_fail_pages_once(tmp_path: Path) -> None:
    write_state(tmp_path, {})
    bindir = fake_docker(tmp_path, containers="core-1", report=FAIL_REPORT, code=1)
    result = run(tmp_path, bindir)
    assert result.returncode == 1, result.stderr
    # Only the FAIL line travels: the ok lines stay out of the page.
    assert texts(tmp_path) == [FAIL_PAGE]
    assert read_paged(tmp_path) == {FAIL_SLUG: FAIL_CHECK}
    assert stat.S_IMODE(state_path(tmp_path).stat().st_mode) == 0o600
    # No temp file is left beside the state file.
    assert [p.name for p in state_path(tmp_path).parent.iterdir()] == ["state.json"]


def test_second_consecutive_fail_pages_nothing(tmp_path: Path) -> None:
    write_state(tmp_path, {})
    bindir = fake_docker(tmp_path, containers="core-1", report=FAIL_REPORT, code=1)
    assert run(tmp_path, bindir).returncode == 1
    second = run(tmp_path, bindir)
    assert second.returncode == 1, second.stderr
    assert texts(tmp_path) == [FAIL_PAGE]
    assert read_paged(tmp_path) == {FAIL_SLUG: FAIL_CHECK}


def test_recovery_sends_one_notice(tmp_path: Path) -> None:
    write_state(tmp_path, {})
    failing = fake_docker(tmp_path, containers="core-1", report=FAIL_REPORT, code=1)
    assert run(tmp_path, failing).returncode == 1
    healthy = fake_docker(tmp_path, containers="core-1", report=OK_REPORT, code=0)
    result = run(tmp_path, healthy)
    assert result.returncode == 0, result.stderr
    assert texts(tmp_path) == [FAIL_PAGE, FAIL_RECOVERY]
    assert read_paged(tmp_path) == {}
    # A check that stays healthy sends nothing more.
    assert run(tmp_path, healthy).returncode == 0
    assert len(pages(tmp_path)) == 2


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        '{"version": 2, "paged": {}}',
        '{"version": 1, "paged": []}',
        '{"version": 1, "paged": {"cluster": 3}}',
        b"\xff\xfe".decode("latin-1"),
    ],
)
def test_unreadable_state_pages(tmp_path: Path, content: str) -> None:
    """A bad file is unknown, never all clear, even when it once held this check."""
    state_path(tmp_path).write_text(content, encoding="latin-1")
    bindir = fake_docker(tmp_path, containers="core-1", report=FAIL_REPORT, code=1)
    result = run(tmp_path, bindir)
    assert result.returncode == 1, result.stderr
    assert texts(tmp_path) == [FAIL_PAGE]
    assert "is unreadable" in result.stderr
    # The run leaves a good file behind.
    assert read_paged(tmp_path) == {FAIL_SLUG: FAIL_CHECK}


def test_missing_state_pages(tmp_path: Path) -> None:
    bindir = fake_docker(tmp_path, containers="core-1", report=FAIL_REPORT, code=1)
    result = run(tmp_path, bindir)
    assert result.returncode == 1, result.stderr
    assert texts(tmp_path) == [FAIL_PAGE]
    assert "is missing" in result.stderr
    assert read_paged(tmp_path) == {FAIL_SLUG: FAIL_CHECK}


# AC 19: a page that is not sent is not recorded.


def test_page_failure_leaves_check_unrecorded_and_retries(tmp_path: Path) -> None:
    write_state(tmp_path, {})
    bindir = fake_docker(tmp_path, containers="core-1", report=FAIL_REPORT, code=1)
    failed = run(tmp_path, bindir, page_exit=1)
    assert failed.returncode == 2, failed.stderr
    assert read_paged(tmp_path) == {}
    retried = run(tmp_path, bindir)
    assert retried.returncode == 1, retried.stderr
    # The stub saw the failed attempt and the retry.
    assert texts(tmp_path) == [FAIL_PAGE, FAIL_PAGE]
    assert read_paged(tmp_path) == {FAIL_SLUG: FAIL_CHECK}


@pytest.mark.parametrize("how", ["exit 1", "exit 75", "cannot start"])
def test_page_failure_exits_2(tmp_path: Path, how: str) -> None:
    write_state(tmp_path, {})
    bindir = fake_docker(tmp_path, containers="core-1", report=FAIL_REPORT, code=1)
    if how == "cannot start":
        result = run(tmp_path, bindir, command=str(tmp_path / "no-such-command"))
        assert "could not start" in result.stderr
    else:
        result = run(tmp_path, bindir, page_exit=int(how.split()[1]))
        assert f"page command exited {how.split()[1]}" in result.stderr
    # Exit 2 wins over the FAIL's exit 1.
    assert result.returncode == 2
    assert read_paged(tmp_path) == {}


def test_failed_recovery_notice_keeps_the_check_paged(tmp_path: Path) -> None:
    write_state(tmp_path, {FAIL_SLUG: FAIL_CHECK})
    bindir = fake_docker(tmp_path, containers="core-1", report=OK_REPORT, code=0)
    failed = run(tmp_path, bindir, page_exit=1)
    assert failed.returncode == 2
    assert read_paged(tmp_path) == {FAIL_SLUG: FAIL_CHECK}
    retried = run(tmp_path, bindir)
    assert retried.returncode == 0, retried.stderr
    assert texts(tmp_path) == [FAIL_RECOVERY, FAIL_RECOVERY]
    assert read_paged(tmp_path) == {}


def test_state_write_failure_exits_2(tmp_path: Path) -> None:
    missing_dir = tmp_path / "no-such-dir" / "state.json"
    bindir = fake_docker(tmp_path, containers="core-1", report=FAIL_REPORT, code=1)
    result = run(tmp_path, bindir, state_file=missing_dir)
    assert result.returncode == 2
    assert "cannot write state file" in result.stderr
    # The page itself still went out.
    assert texts(tmp_path) == [FAIL_PAGE]


def test_page_command_timeout_counts_as_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 60-second bound, shortened: a command that outlives it has not sent."""
    module = load_script(monkeypatch)
    module.PAGE_TIMEOUT = 1
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    assert module.page(sleeper, "x") == "timed out after 1 s"
    assert module.page([sys.executable, "-c", "pass"], "x") is None


@pytest.mark.parametrize(
    "unset, command",
    [
        ("RHEO_DOCTOR_COMPOSE_PROJECT", None),
        ("RHEO_DOCTOR_PAGE_COMMAND", ""),
        (None, "'unbalanced"),
    ],
)
def test_missing_configuration_exits_2_before_doctor_runs(
    tmp_path: Path, unset: str | None, command: str | None
) -> None:
    bindir = fake_docker(tmp_path, containers="core-1", report=FAIL_REPORT, code=1)
    extra = {unset: ""} if unset == "RHEO_DOCTOR_COMPOSE_PROJECT" else {}
    result = run(tmp_path, bindir, command=command, extra_env=extra)
    assert result.returncode == 2
    assert not (tmp_path / "exec-ran").exists()
    assert pages(tmp_path) == []


# AC 20 (doctor half): the line reaches the command byte for byte.


def test_markup_characters_arrive_byte_for_byte(tmp_path: Path) -> None:
    line = (
        "FAIL settings: identity.oauth.client_secret is *unset*; "
        "set RHEO__identity__oauth__client_secret and restart [core_1]"
    )
    write_state(tmp_path, {})
    bindir = fake_docker(tmp_path, containers="core-1", report=f"{line}\n", code=1)
    result = run(tmp_path, bindir)
    assert result.returncode == 1, result.stderr
    expected = f"rheo.stream doctor FAIL on {HOST}: {line}".encode()
    assert pages(tmp_path) == [expected]


# Kept rules.


def test_doctor_run_fail_evaluates_no_recoveries(tmp_path: Path) -> None:
    """A down or restarting container evaluates no check, so a real FAIL must stay
    paged rather than read as recovered and page again next run."""
    write_state(tmp_path, {FAIL_SLUG: FAIL_CHECK})
    down = fake_docker(tmp_path, containers="", report=OK_REPORT, code=0)
    result = run(tmp_path, down)
    assert result.returncode == 1, result.stderr
    [only] = texts(tmp_path)
    assert only.startswith(f"rheo.stream doctor FAIL on {HOST}: FAIL doctor run: ")
    assert read_paged(tmp_path) == {FAIL_SLUG: FAIL_CHECK, "doctor-run": "doctor run"}
    # Once doctor runs again and both checks pass, both recover.
    healthy = fake_docker(tmp_path, containers="core-1", report=OK_REPORT, code=0)
    assert run(tmp_path, healthy).returncode == 0
    assert sorted(texts(tmp_path)[1:]) == sorted(
        [FAIL_RECOVERY, f"rheo.stream doctor recovered on {HOST}: doctor run"]
    )
    assert read_paged(tmp_path) == {}


def test_dry_run_sends_and_writes_nothing(tmp_path: Path) -> None:
    path = write_state(tmp_path, {"cluster": "cluster"})
    before = path.read_bytes()
    bindir = fake_docker(tmp_path, containers="core-1", report=FAIL_REPORT, code=1)
    result = run(tmp_path, bindir, "--dry-run")
    assert result.returncode == 1, result.stderr
    assert f"state file {path}: ok" in result.stdout
    assert f"would page: {FAIL_PAGE}" in result.stdout
    assert (
        f"would send recovery: rheo.stream doctor recovered on {HOST}: cluster"
        in result.stdout
    )
    assert pages(tmp_path) == []
    assert path.read_bytes() == before


def test_dry_run_reports_a_missing_state_file_and_creates_none(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    bindir = fake_docker(tmp_path, containers="core-1", report=FAIL_REPORT, code=1)
    result = run(tmp_path, bindir, "--dry-run")
    assert result.returncode == 1, result.stderr
    assert f"state file {path}: missing" in result.stdout
    assert not path.exists()
    assert pages(tmp_path) == []


@pytest.mark.parametrize("containers", ["", "core-1 core-2"])
def test_no_single_core_container_is_a_fail(tmp_path: Path, containers: str) -> None:
    bindir = fake_docker(tmp_path, containers=containers, report=OK_REPORT, code=0)
    result = run(tmp_path, bindir)
    assert result.returncode == 1, result.stderr
    [only] = texts(tmp_path)
    assert "FAIL doctor run: expected one running core container" in only
    assert read_paged(tmp_path) == {"doctor-run": "doctor run"}


def test_doctor_exiting_nonzero_without_a_fail_line_is_a_fail(tmp_path: Path) -> None:
    bindir = fake_docker(tmp_path, containers="core-1", report="Traceback\n", code=3)
    result = run(tmp_path, bindir)
    assert result.returncode == 1, result.stderr
    assert texts(tmp_path) == [
        f"rheo.stream doctor FAIL on {HOST}: FAIL doctor run: exit 3"
    ]


# AC 22 and AC 54 (code half): the page command is the only transport.

NETWORK_MODULES = {
    "urllib",
    "http",
    "socket",
    "ssl",
    "requests",
    "httpx",
    "aiohttp",
    "smtplib",
    "ftplib",
    "xmlrpc",
}


def imported_modules(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_script_has_no_ticket_transport_token_or_current_client() -> None:
    source = SCRIPT.read_text()
    lowered = source.lower()
    for name in (
        "Tickets",
        "read_key",
        "Unavailable",
        "RHEO_DOCTOR_TICKETS_URL",
        "RHEO_DOCTOR_ENV_FILE",
        "RHEO_DOCTOR_TICKET_FIELDS",
        "CURRENT_ALERT_TOKEN",
        "CURRENT_API_URL",
        "alert_writer",
        "close_by_source",
        "/api/v1/operations",
    ):
        assert name not in source, name
    # No ticket service and no messaging service is named at all.
    assert "ticket" not in lowered
    assert "telegram" not in lowered
    # No bot-token shape and no workspace id.
    assert not re.search(r"\d{8,10}:[A-Za-z0-9_-]{30,}", source)
    assert not re.search(r"ws_[0-9a-f]{32}", source)
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-", source)
    assert imported_modules(source).isdisjoint(NETWORK_MODULES)


class RecordingHandler(BaseHTTPRequestHandler):
    seen: list[str] = []

    def log_message(self, *args: object) -> None:
        pass

    def _record(self) -> None:
        self.seen.append(f"{self.command} {self.path}")
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_PATCH = do_PUT = do_CONNECT = _record


@pytest.fixture
def listener() -> Iterator[tuple[list[str], str]]:
    seen: list[str] = []
    handler = type("Handler", (RecordingHandler,), {"seen": seen})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield seen, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


def test_failing_page_command_attempts_no_other_transport(
    tmp_path: Path, listener: tuple[list[str], str]
) -> None:
    """Every address a fallback could use points at a local listener, and the old
    and the Current settings are set: a failing page still contacts nothing."""
    seen, url = listener
    write_state(tmp_path, {})
    bindir = fake_docker(tmp_path, containers="core-1", report=FAIL_REPORT, code=1)
    result = run(
        tmp_path,
        bindir,
        page_exit=1,
        extra_env={
            "RHEO_DOCTOR_TICKETS_URL": url,
            "CURRENT_API_URL": url,
            "CURRENT_ALERT_TOKEN": "fake-token-for-test",
            "http_proxy": url,
            "https_proxy": url,
            "HTTP_PROXY": url,
            "HTTPS_PROXY": url,
        },
    )
    assert result.returncode == 2
    assert texts(tmp_path) == [FAIL_PAGE]
    assert seen == []
    assert imported_modules(SCRIPT.read_text()).isdisjoint(NETWORK_MODULES)
