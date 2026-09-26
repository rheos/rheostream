"""The worker's liveness signal (#156): a file the loop touches, and the probe that
reads it.

``worker_loop`` calls the heartbeat once at the top of every iteration, so the file's
mtime says when the loop last came round. The container health check runs
``python -m rheo_app_worker.heartbeat``, which exits 0 while the file is younger than
:data:`MAX_AGE_SECONDS` and 1 otherwise, including when the file does not exist yet.

**What it proves, and what it does not.** A fresh file means the loop is iterating. It
does not mean Postgres is reachable: a pass that fails on the database still comes back
round the loop (after a back-off capped at 60 s), because the loop never exits on a
failed pass. That is deliberate. A worker waiting out a database outage is alive, and
the ``postgres`` service has its own health check.

**The bound.** Between two beats the loop runs one pass and then waits at most 60 s.
A pass runs job handlers inline, so one handler that runs for longer than
:data:`MAX_AGE_SECONDS` makes the container report unhealthy until it finishes. Docker
only reports that; it does not restart the container. Ten minutes is far past any
handler shipped today.

The heartbeat is off unless :data:`HEARTBEAT_FILE_VARIABLE` names a path, so tests and
a bare ``python -m rheo_app_worker.main`` on a laptop write nothing. The probe imports
only the standard library, so running it every 30 s costs one interpreter start.
"""

import logging
import os
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

logger = logging.getLogger("rheo_app_worker.heartbeat")

HEARTBEAT_FILE_VARIABLE: Final = "RHEO_WORKER_HEARTBEAT_FILE"
MAX_AGE_SECONDS: Final = 600.0


def heartbeat_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """The configured heartbeat file, or ``None`` when the heartbeat is off."""
    env = os.environ if environ is None else environ
    value = env.get(HEARTBEAT_FILE_VARIABLE, "").strip()
    return Path(value) if value else None


def touch(path: Path) -> None:
    """Set ``path``'s mtime to now, creating it if needed.

    Never raises: a full disk or a read-only mount must not stop the worker. The
    health check then goes stale and reports the problem, which is the right place.
    """
    try:
        path.touch()
    except OSError:
        logger.warning("worker heartbeat: could not touch %s", path, exc_info=True)


def heartbeat_from_env(
    environ: Mapping[str, str] | None = None,
) -> Callable[[], None] | None:
    """The callable ``worker_loop`` takes as ``heartbeat``, or ``None`` when off."""
    path = heartbeat_path(environ)
    if path is None:
        return None
    return lambda: touch(path)


def is_fresh(
    path: Path, *, max_age_seconds: float = MAX_AGE_SECONDS, now: float | None = None
) -> bool:
    """Whether ``path`` exists and was touched less than ``max_age_seconds`` ago."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    current = time.time() if now is None else now
    return current - mtime < max_age_seconds


def main(environ: Mapping[str, str] | None = None) -> int:
    """The health-check probe: 0 when the heartbeat is fresh, 1 otherwise."""
    path = heartbeat_path(environ)
    if path is None:
        print(f"{HEARTBEAT_FILE_VARIABLE} is not set", file=sys.stderr)
        return 1
    if not is_fresh(path):
        print(f"worker heartbeat {path} is missing or stale", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
