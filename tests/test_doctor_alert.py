"""The scheduled doctor pager: FAIL lines become tickets, recoveries close them
(issue #229).

`deploy/doctor/rheostream-doctor-alert.py` runs as a subprocess against a stub
`docker` on PATH and a local stand-in for the ticket service. No real Docker and no
real service: the stub records every request, so each test checks exactly what
would have been filed or closed.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "doctor" / "rheostream-doctor-alert.py"
PROJECT = "exampleproject"

OK_REPORT = """\
ok   cluster: reachable; server 16.15
ok   workspace 0000 (example): active
ok   evidence acceptance 0000 (example): 0 of 0 evidence units settled in the last 24 h
"""
FAIL_LINE = (
    "FAIL evidence acceptance 0000 (example): 1 of 3 evidence units settled in the "
    "last 24 h ended gap/acceptance_failed; the evidence of each is lost"
)
FAIL_REPORT = (
    "ok   cluster: reachable; server 16.15\n"
    f"{FAIL_LINE}\n"
    "ok   workspace 0000 (example): active\n"
)
FAIL_REF = "rheostream-doctor:evidence-acceptance-0000-(example)"


class TicketStub:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.open_tickets: list[dict[str, Any]] = []
        self.list_status = 200
        self.write_status = 201

    def handler(self) -> type[BaseHTTPRequestHandler]:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _reply(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length)) if length else {}

            def do_GET(self) -> None:
                stub.requests.append(("GET", self.path, {}))
                assert self.headers["Authorization"] == "Bearer test-key"
                self._reply(
                    stub.list_status,
                    {
                        "tickets": stub.open_tickets,
                        "total": len(stub.open_tickets),
                        "page": 1,
                        "per_page": 100,
                    },
                )

            def do_POST(self) -> None:
                stub.requests.append(("POST", self.path, self._body()))
                self._reply(stub.write_status, {"id": 1})

            def do_PATCH(self) -> None:
                stub.requests.append(("PATCH", self.path, self._body()))
                self._reply(200, {})

        return Handler

    def calls(self, method: str) -> list[tuple[str, dict[str, Any]]]:
        return [(path, body) for m, path, body in self.requests if m == method]


@pytest.fixture
def svc() -> Iterator[tuple[TicketStub, str]]:
    stub = TicketStub()
    server = HTTPServer(("127.0.0.1", 0), stub.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub, f"http://127.0.0.1:{server.server_port}/api"
    finally:
        server.shutdown()


def fake_docker(tmp_path: Path, *, containers: str, report: str, code: int) -> Path:
    """A stub `docker`: `ps` prints `containers`; `exec` prints `report`, exits `code`,
    and leaves a marker, so the preflight test can prove doctor never ran."""
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


def run(
    tmp_path: Path, url: str, bindir: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    env_file = tmp_path / "doctor.env"
    env_file.write_text("TICKETS_API_KEY=test-key\n")
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "RHEO_DOCTOR_COMPOSE_PROJECT": PROJECT,
        "RHEO_DOCTOR_ENV_FILE": str(env_file),
        "RHEO_DOCTOR_TICKETS_URL": url,
        "RHEO_DOCTOR_TICKET_FIELDS": '{"area": "infra"}',
        "RHEO_DOCTOR_LOG": "stderr",
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def open_ticket(
    ref: str, ticket_id: int, ticket_type: str = "infra-alert"
) -> dict[str, Any]:
    return {
        "id": ticket_id,
        "source_ref": ref,
        "ticket_type": ticket_type,
        "status": "open",
    }


def test_all_ok_files_nothing_and_exits_0(
    tmp_path: Path, svc: tuple[TicketStub, str]
) -> None:
    stub, url = svc
    bindir = fake_docker(tmp_path, containers="core-1", report=OK_REPORT, code=0)
    result = run(tmp_path, url, bindir)
    assert result.returncode == 0, result.stderr
    assert stub.calls("POST") == []
    assert stub.calls("PATCH") == []


def test_a_fail_line_files_one_critical_ticket(
    tmp_path: Path, svc: tuple[TicketStub, str]
) -> None:
    stub, url = svc
    bindir = fake_docker(tmp_path, containers="core-1", report=FAIL_REPORT, code=1)
    result = run(tmp_path, url, bindir)
    assert result.returncode == 1, result.stderr
    [(path, body)] = stub.calls("POST")
    assert path == "/api/tickets"
    assert body["source_ref"] == FAIL_REF
    assert body["severity"] == "critical"
    assert body["ticket_type"] == "infra-alert"
    # Service-specific fields ride through from RHEO_DOCTOR_TICKET_FIELDS.
    assert body["area"] == "infra"
    assert body["provenance"] == "status-poll"
    assert "gap/acceptance_failed" in body["body"]
    # Only the FAIL line travels: the ok lines stay out of the ticket.
    assert "cluster: reachable" not in body["body"]


def test_a_still_failing_check_keeps_its_ticket_open(
    tmp_path: Path, svc: tuple[TicketStub, str]
) -> None:
    stub, url = svc
    stub.open_tickets = [open_ticket(FAIL_REF, 7)]
    stub.write_status = (
        200  # the service dedupes: same source_ref + type updates, no new page
    )
    bindir = fake_docker(tmp_path, containers="core-1", report=FAIL_REPORT, code=1)
    result = run(tmp_path, url, bindir)
    assert result.returncode == 1, result.stderr
    assert len(stub.calls("POST")) == 1
    assert stub.calls("PATCH") == []


def test_a_recovered_check_closes_its_ticket(
    tmp_path: Path, svc: tuple[TicketStub, str]
) -> None:
    stub, url = svc
    stub.open_tickets = [
        open_ticket(FAIL_REF, 7),
        # Not this pager's: another source, and this prefix under another type.
        open_ticket("gmail:abc", 8),
        open_ticket(FAIL_REF, 9, ticket_type="deploy-failure"),
    ]
    bindir = fake_docker(tmp_path, containers="core-1", report=OK_REPORT, code=0)
    result = run(tmp_path, url, bindir)
    assert result.returncode == 0, result.stderr
    assert stub.calls("PATCH") == [("/api/tickets/7", {"status": "done"})]


def test_a_rejected_key_fails_before_doctor_runs(
    tmp_path: Path, svc: tuple[TicketStub, str]
) -> None:
    """The preflight: a pager that cannot reach its service must fail loudly."""
    stub, url = svc
    stub.list_status = 401
    bindir = fake_docker(tmp_path, containers="core-1", report=FAIL_REPORT, code=1)
    result = run(tmp_path, url, bindir)
    assert result.returncode == 2
    assert "answered 401" in result.stderr
    assert not (tmp_path / "exec-ran").exists()
    assert stub.calls("POST") == []


def test_an_unreachable_ticket_service_exits_2(tmp_path: Path) -> None:
    bindir = fake_docker(tmp_path, containers="core-1", report=OK_REPORT, code=0)
    result = run(tmp_path, "http://127.0.0.1:9/api", bindir)
    assert result.returncode == 2
    assert "tickets" in result.stderr


def test_a_failed_ticket_write_exits_2(
    tmp_path: Path, svc: tuple[TicketStub, str]
) -> None:
    stub, url = svc
    stub.write_status = 500
    bindir = fake_docker(tmp_path, containers="core-1", report=FAIL_REPORT, code=1)
    result = run(tmp_path, url, bindir)
    assert result.returncode == 2
    assert "answered 500" in result.stderr


@pytest.mark.parametrize("containers", ["", "core-1 core-2"])
def test_no_single_core_container_is_a_fail(
    tmp_path: Path, svc: tuple[TicketStub, str], containers: str
) -> None:
    stub, url = svc
    bindir = fake_docker(tmp_path, containers=containers, report=OK_REPORT, code=0)
    result = run(tmp_path, url, bindir)
    assert result.returncode == 1, result.stderr
    [(_, body)] = stub.calls("POST")
    assert body["source_ref"] == "rheostream-doctor:doctor-run"


def test_doctor_exiting_nonzero_without_a_fail_line_is_a_fail(
    tmp_path: Path, svc: tuple[TicketStub, str]
) -> None:
    stub, url = svc
    bindir = fake_docker(tmp_path, containers="core-1", report="Traceback\n", code=3)
    result = run(tmp_path, url, bindir)
    assert result.returncode == 1, result.stderr
    [(_, body)] = stub.calls("POST")
    assert body["source_ref"] == "rheostream-doctor:doctor-run"
    assert "exit 3" in body["body"]


def test_dry_run_writes_nothing(tmp_path: Path, svc: tuple[TicketStub, str]) -> None:
    stub, url = svc
    stub.open_tickets = [open_ticket("rheostream-doctor:cluster", 4)]
    bindir = fake_docker(tmp_path, containers="core-1", report=FAIL_REPORT, code=1)
    result = run(tmp_path, url, bindir, "--dry-run")
    assert result.returncode == 1, result.stderr
    assert f"would file {FAIL_REF}" in result.stdout
    assert "would close rheostream-doctor:cluster" in result.stdout
    assert stub.calls("POST") == []
    assert stub.calls("PATCH") == []


def test_no_ticket_closes_when_doctor_could_not_run(
    tmp_path: Path, svc: tuple[TicketStub, str]
) -> None:
    """A down or restarting container evaluates no check, so a real FAIL's ticket must
    stay open rather than read as recovered and page again next run."""
    stub, url = svc
    stub.open_tickets = [open_ticket(FAIL_REF, 7)]
    bindir = fake_docker(tmp_path, containers="", report=OK_REPORT, code=0)
    result = run(tmp_path, url, bindir)
    assert result.returncode == 1, result.stderr
    [(_, body)] = stub.calls("POST")
    assert body["source_ref"] == "rheostream-doctor:doctor-run"
    assert stub.calls("PATCH") == []
