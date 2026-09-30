#!/usr/bin/env python3
"""Run `rheo doctor` against the flagship and page on FAIL through a ticket service
(issue #229).

Usage:
  rheostream-doctor-alert.py [run]     run doctor, file or close tickets
  rheostream-doctor-alert.py --dry-run run doctor and print what it would do

A host cron job calls this. It runs `rheo doctor` inside the flagship's core
container and turns each FAIL line into a critical ticket in the operator's ticket
service, which is expected to page on a critical ticket's *creation* and to update
the open ticket, not add one, when a ticket with the same `source_ref` and
`ticket_type` arrives again. When a check recovers, the script closes its ticket, so
the next failure pages afresh. A pager that never closes its tickets pages once and
then goes quiet for good.

The ticket service contract: `GET {url}/tickets?status=open&page=N&per_page=M`
returns `{"tickets": [...], "total": T, "per_page": M}`; `POST {url}/tickets`
creates (201) or dedupes onto the open ticket (200); `PATCH {url}/tickets/{id}`
with `{"status": "done"}` closes one. Bearer-token auth. Any field the service
needs beyond the ones this script sets goes in `RHEO_DOCTOR_TICKET_FIELDS`.

Doctor's lines are content-free by design (counts, ids, slugs, settings keys),
so a FAIL line is copied into the ticket body as it is.

Configuration (environment; the cron file sets what differs from the defaults):
  RHEO_DOCTOR_COMPOSE_PROJECT  compose project label of the app (required)
  RHEO_DOCTOR_SERVICE          compose service to run doctor in (default core)
  RHEO_DOCTOR_TICKETS_URL      ticket service API base            (required)
  RHEO_DOCTOR_ENV_FILE         file holding TICKETS_API_KEY=...   (default
                               /root/.config/rheostream-doctor.env, mode 0600)
  RHEO_DOCTOR_TICKET_FIELDS    JSON object of extra ticket fields (default {})
  RHEO_DOCTOR_LOG              syslog | stderr                   (default syslog)

Exit codes: 0 all ok (tickets closed as needed); 1 at least one FAIL was paged;
2 the run could not do its job (no key, the ticket service unreachable, a ticket
write failed). An exit 2 is itself a problem the cron's mail or syslog should
surface: a pager that cannot reach its ticket service must not look healthy.

**No recovery on a failed run.** When doctor itself could not run (no single core
container, an exec failure, a non-zero exit with no FAIL line), the other checks
were never evaluated, so no open ticket is closed on that run.

Standard library only, so the host needs nothing beyond python3 and docker.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

TAG = "rheostream-doctor"
SOURCE_PREFIX = "rheostream-doctor:"
TICKET_TYPE = "infra-alert"
DOCTOR_RUN_REF = SOURCE_PREFIX + "doctor-run"
# ``LEVEL`` padded to four characters, one space, then ``name: detail``.
LINE_RE = re.compile(r"^(ok|warn|FAIL)\s+(.+?): (.*)$")
SLUG_RE = re.compile(r"[^a-z0-9()._-]+")


class Unavailable(Exception):
    """The run cannot do its job; exit 2."""


def log(level: str, message: str) -> None:
    line = f"{level}: {message}"
    if os.environ.get("RHEO_DOCTOR_LOG", "syslog") == "syslog":
        prio = "user.err" if level == "error" else "user.info"
        try:
            subprocess.run(
                ["logger", "-t", TAG, "-p", prio, "--", line],
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if level == "error" or sys.stderr.isatty():
            print(f"{TAG} {line}", file=sys.stderr)
    else:
        print(f"{TAG} {line}", file=sys.stderr)


@dataclass(frozen=True)
class Failure:
    check: str
    line: str

    @property
    def source_ref(self) -> str:
        return SOURCE_PREFIX + SLUG_RE.sub("-", self.check.lower()).strip("-")


def parse_failures(output: str) -> list[Failure]:
    failures = []
    for raw in output.splitlines():
        match = LINE_RE.match(raw.rstrip())
        if match and match.group(1) == "FAIL":
            failures.append(Failure(check=match.group(2), line=raw.rstrip()))
    return failures


def find_container(project: str, service: str) -> str:
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    names = [n for n in result.stdout.split() if n]
    if result.returncode != 0 or len(names) != 1:
        raise LookupError(
            f"expected one running {service} container, found {len(names)}"
        )
    return names[0]


def run_doctor(project: str, service: str) -> list[Failure]:
    """FAIL lines from doctor, or one synthetic FAIL when doctor cannot run."""
    try:
        container = find_container(project, service)
    except (LookupError, OSError, subprocess.TimeoutExpired) as exc:
        return [Failure("doctor run", f"FAIL doctor run: {exc}")]
    try:
        result = subprocess.run(
            ["docker", "exec", container, "rheo", "doctor"],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [Failure("doctor run", f"FAIL doctor run: {type(exc).__name__}")]
    failures = parse_failures(result.stdout)
    if result.returncode != 0 and not failures:
        # Doctor exits non-zero on a FAIL line; a non-zero exit with none means
        # it did not get as far as reporting, which is a failure of its own.
        failures = [Failure("doctor run", f"FAIL doctor run: exit {result.returncode}")]
    return failures


class Tickets:
    def __init__(self, base: str, key: str, extra: dict[str, Any]) -> None:
        self.base = base.rstrip("/")
        self.key = key
        self.extra = extra

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            return exc.code, {}
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise Unavailable(
                f"tickets {method} {path}: {type(exc).__name__}"
            ) from None

    def open_doctor_tickets(self) -> list[dict[str, Any]]:
        """Every open ticket this pager owns. Also the preflight: an unreachable
        service or a rejected key fails the run before doctor's result counts."""
        tickets: list[dict[str, Any]] = []
        page = 1
        while True:
            status, body = self._request(
                "GET", f"/tickets?status=open&page={page}&per_page=100"
            )
            if status != 200:
                raise Unavailable(f"tickets open-ticket list answered {status}")
            batch = body.get("tickets", [])
            tickets.extend(
                t
                for t in batch
                if str(t.get("source_ref") or "").startswith(SOURCE_PREFIX)
                and t.get("ticket_type") == TICKET_TYPE
            )
            total = int(body.get("total", 0))
            if not batch or page * int(body.get("per_page", 100) or 100) >= total:
                return tickets
            page += 1

    def file(self, failure: Failure, host: str) -> int:
        status, _ = self._request(
            "POST",
            "/tickets",
            {
                **self.extra,
                "title": f"rheo.stream doctor FAIL: {failure.check}"[:200],
                "ticket_type": TICKET_TYPE,
                "severity": "critical",
                "provenance": "status-poll",
                "source_ref": failure.source_ref,
                "body": f"{failure.line}\n\nFrom `rheo doctor` on {host}, via {TAG}.",
            },
        )
        if status not in (200, 201):
            raise Unavailable(f"tickets write answered {status}")
        return status

    def close(self, ticket_id: object) -> None:
        status, _ = self._request(
            "PATCH",
            f"/tickets/{urllib.parse.quote(str(ticket_id))}",
            {"status": "done"},
        )
        if status != 200:
            raise Unavailable(f"tickets close answered {status}")


def read_key(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                name, _, value = line.strip().partition("=")
                if name == "TICKETS_API_KEY" and value:
                    return value.strip().strip('"').strip("'")
    except OSError as exc:
        raise Unavailable(f"cannot read {path}: {type(exc).__name__}") from None
    raise Unavailable(f"no TICKETS_API_KEY in {path}")


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    project = os.environ.get("RHEO_DOCTOR_COMPOSE_PROJECT", "")
    if not project:
        log("error", "RHEO_DOCTOR_COMPOSE_PROJECT is not set")
        return 2
    service = os.environ.get("RHEO_DOCTOR_SERVICE", "core")
    base = os.environ.get("RHEO_DOCTOR_TICKETS_URL", "")
    if not base:
        log("error", "RHEO_DOCTOR_TICKETS_URL is not set")
        return 2
    try:
        extra = json.loads(os.environ.get("RHEO_DOCTOR_TICKET_FIELDS") or "{}")
    except ValueError:
        extra = None
    if not isinstance(extra, dict):
        log("error", "RHEO_DOCTOR_TICKET_FIELDS is not a JSON object")
        return 2
    try:
        key = read_key(
            os.environ.get(
                "RHEO_DOCTOR_ENV_FILE", "/root/.config/rheostream-doctor.env"
            )
        )
        tickets = Tickets(base, key, extra)
        open_tickets = tickets.open_doctor_tickets()
        failures = run_doctor(project, service)
        failing = {f.source_ref for f in failures}
        host = socket.gethostname()
        for failure in failures:
            if dry_run:
                print(f"would file {failure.source_ref}: {failure.line}")
                continue
            status = tickets.file(failure, host)
            action = "created" if status == 201 else "updated"
            log("error", f"{failure.source_ref} FAIL, ticket {action}")
        # Doctor did not run, so no other check was evaluated: close nothing.
        evaluated = DOCTOR_RUN_REF not in failing
        for ticket in open_tickets if evaluated else []:
            if ticket.get("source_ref") in failing:
                continue
            ref, ticket_id = ticket.get("source_ref"), ticket.get("id")
            if dry_run:
                print(f"would close {ref} (ticket {ticket_id})")
                continue
            tickets.close(ticket_id)
            log("info", f"{ref} recovered, ticket {ticket_id} closed")
    except Unavailable as exc:
        log("error", str(exc))
        return 2
    if not failures:
        log("info", "doctor all ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
