#!/usr/bin/env python3
"""Run `rheo doctor` against the flagship and page a person when a check starts or
stops failing (issue #229).

Usage:
  rheostream-doctor-alert.py [run]     run doctor, send the pages it needs
  rheostream-doctor-alert.py --dry-run run doctor and print what it would send

A host cron job calls this every hour. It runs `rheo doctor` inside the flagship's
core container. A check that starts failing sends one page. A check that keeps
failing sends nothing more. A paged check that stops failing sends one recovery
notice. The script keeps the paged checks in a state file on the host, so it needs
no outside service to know what it already sent.

Paging runs the page command: a command line split with shlex and run without a
shell. It reads one message on stdin and exits 0 once the message is delivered.
The script never holds or reads a messaging credential; the command does. A
command that exits nonzero, cannot start, or runs past 60 seconds has not sent, so
the check stays unrecorded and the next run tries again.

Each message is one plain line, handed to the command unchanged (the script never
picks a markup mode):
  rheo.stream doctor FAIL on <host>: <doctor's own FAIL line>
  rheo.stream doctor recovered on <host>: <check name>
Doctor's lines are content-free by design (counts, ids, slugs, settings keys).

State file: {"version": 1, "paged": {"<check slug>": "<check name>"}}, mode 0600,
replaced atomically (temp file in the same directory, fsync, rename). A missing,
unreadable or wrong-shape file is "unknown": no check counts as paged, so every
current FAIL pages. It never means all clear.

Configuration (environment; the cron file sets what differs from the defaults):
  RHEO_DOCTOR_COMPOSE_PROJECT  compose project label of the app      (required)
  RHEO_DOCTOR_SERVICE          compose service to run doctor in      (default core)
  RHEO_DOCTOR_PAGE_COMMAND     command that sends stdin as one page  (required)
  RHEO_DOCTOR_STATE_FILE       paged-check state                     (default
                               /var/lib/rheostream-doctor/state.json)
  RHEO_DOCTOR_LOG              syslog | stderr                       (default syslog)

Exit codes: 0 all ok; 1 at least one check FAILs, and every page it needs was sent
or already recorded; 2 the pager could not do its job: a required setting is
missing, a page command failed or timed out, or the state file could not be
written. Exit 2 wins over 1. An exit 2 is itself a problem the cron's mail or
syslog should surface: a pager that cannot page must not look healthy.

**No recovery on a failed run.** When doctor itself could not run (no single core
container, an exec failure, a non-zero exit with no FAIL line), the other checks
were never evaluated, so that run sends no recovery notice.

Standard library only, so the host needs nothing beyond python3 and docker.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass

TAG = "rheostream-doctor"
# Slug of the synthetic FAIL that run_doctor reports when doctor cannot run.
DOCTOR_RUN = "doctor-run"
STATE_VERSION = 1
DEFAULT_STATE_FILE = "/var/lib/rheostream-doctor/state.json"
PAGE_TIMEOUT = 60
# ``LEVEL`` padded to four characters, one space, then ``name: detail``.
LINE_RE = re.compile(r"^(ok|warn|FAIL)\s+(.+?): (.*)$")
SLUG_RE = re.compile(r"[^a-z0-9()._-]+")


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
    def slug(self) -> str:
        return SLUG_RE.sub("-", self.check.lower()).strip("-")


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


def load_state(path: str) -> tuple[dict[str, str], str]:
    """The paged checks and the file's status: ok, missing or unreadable.

    Anything but a well-formed file gives no paged checks, so the run pages."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}, "missing"
    except (OSError, ValueError):
        return {}, "unreadable"
    paged = data.get("paged") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("version") != STATE_VERSION
        or not isinstance(paged, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in paged.items())
    ):
        return {}, "unreadable"
    return dict(paged), "ok"


def write_state(path: str, paged: dict[str, str]) -> None:
    """Replace the state file atomically. mkstemp creates the temp file 0600."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {"version": STATE_VERSION, "paged": paged}, handle, sort_keys=True
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    # Sync the directory too, so the rename itself survives a reboot.
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def page(command: list[str], message: str) -> str | None:
    """Run the page command with `message` on stdin. None when it was sent,
    otherwise why not."""
    try:
        result = subprocess.run(
            command,
            input=message.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            check=False,
            timeout=PAGE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"timed out after {PAGE_TIMEOUT} s"
    except OSError as exc:
        return f"could not start: {type(exc).__name__}"
    if result.returncode != 0:
        return f"exited {result.returncode}"
    return None


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    project = os.environ.get("RHEO_DOCTOR_COMPOSE_PROJECT", "")
    if not project:
        log("error", "RHEO_DOCTOR_COMPOSE_PROJECT is not set")
        return 2
    service = os.environ.get("RHEO_DOCTOR_SERVICE", "core")
    try:
        command = shlex.split(os.environ.get("RHEO_DOCTOR_PAGE_COMMAND", ""))
    except ValueError:
        log("error", "RHEO_DOCTOR_PAGE_COMMAND is not a valid command line")
        return 2
    if not command:
        log("error", "RHEO_DOCTOR_PAGE_COMMAND is not set")
        return 2
    state_file = os.environ.get("RHEO_DOCTOR_STATE_FILE") or DEFAULT_STATE_FILE

    paged, state_status = load_state(state_file)
    if state_status != "ok":
        log(
            "error",
            f"state file {state_file} is {state_status}, so no check counts as paged",
        )
    if dry_run:
        print(f"state file {state_file}: {state_status}")

    failures = run_doctor(project, service)
    failing: dict[str, Failure] = {}
    for failure in failures:
        failing.setdefault(failure.slug, failure)
    host = platform.node()
    pager_failed = False

    for slug, failure in failing.items():
        if slug in paged:
            log("error", f"{slug} FAIL, already paged")
            continue
        message = f"rheo.stream doctor FAIL on {host}: {failure.line}"
        if dry_run:
            print(f"would page: {message}")
            continue
        reason = page(command, message)
        if reason is None:
            paged[slug] = failure.check
            log("error", f"{slug} FAIL, paged")
        else:
            pager_failed = True
            log("error", f"{slug} FAIL, page not sent: page command {reason}")

    # Doctor did not run, so no other check was evaluated: send no recovery.
    if DOCTOR_RUN not in failing:
        for slug, check in sorted(paged.items()):
            if slug in failing:
                continue
            message = f"rheo.stream doctor recovered on {host}: {check}"
            if dry_run:
                print(f"would send recovery: {message}")
                continue
            reason = page(command, message)
            if reason is None:
                del paged[slug]
                log("info", f"{slug} recovered, notice sent")
            else:
                pager_failed = True
                log(
                    "error", f"{slug} recovered, notice not sent: page command {reason}"
                )

    if not dry_run:
        try:
            write_state(state_file, paged)
        except OSError as exc:
            pager_failed = True
            log("error", f"cannot write state file {state_file}: {type(exc).__name__}")
    if pager_failed:
        return 2
    if not failures:
        log("info", "doctor all ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
