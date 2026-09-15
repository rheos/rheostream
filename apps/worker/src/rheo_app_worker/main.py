"""The worker image's entry point: ``python -m rheo_app_worker.main``.

Mirrors ``apps/core/src/rheo_app_core/serve.py``'s shape — a ``main()``, an
``if __name__ == "__main__":``, nothing else — with the stop wiring pulled out as its
own function so a test can drive it without launching a process.

``get_backend()`` is called **inside** ``main()``, never at module level. A
module-level call would make the bare ``import rheo_app_worker.main`` resolve the
cluster DSN, and that import is exactly what the deploy topology's smoke check and this
module's own signal test run with no environment configured.
``apps/core/src/rheo_app_core/main.py`` places its equivalent call inside ``lifespan``
for the same reason.
"""

import signal
import threading
from types import FrameType

from rheo_core.events import ConsumerRegistry
from rheo_core.storage.postgres import get_backend, reset_backend
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import worker_loop

JOB_KINDS = JobKindRegistry()
"""The process-wide job-kind registry.

This run registers no kinds of its own, so it starts empty and that is correct here: it
exists so ``main()`` has a registry to hand ``worker_loop``, and so the kinds a later
run adds have one obvious place to be registered.
"""

CONSUMERS = ConsumerRegistry()
"""The process-wide consumer registry, beside ``JOB_KINDS`` and empty for the same
reason: release one ships no consumer, and this is the one obvious place a later run's
consumer gets registered.

Built here rather than in ``rheo_core.events`` deliberately — that package holds no
module-level instance, so a test builds its own registry and there is no global to
reset between cases. This module is the composition root, which is the only place a
process-wide one belongs.
"""


def install_stop_signals(stop: threading.Event) -> None:
    """Make ``SIGTERM`` and ``SIGINT`` set ``stop``.

    Its own function rather than three lines inside :func:`main` so that it can be
    driven directly by a test: with the wiring inlined, nothing short of launching the
    process could exercise it, and a container's stop signal reaching ``worker_loop``
    would be proved by nothing.

    The handlers are installed for the life of the process, which is what a container
    wants. A test that calls this has to save and restore the process's prior handlers
    itself.
    """

    def _request_stop(signum: int, frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)


def main() -> None:
    backend = get_backend()
    stop = threading.Event()
    install_stop_signals(stop)
    try:
        worker_loop(kinds=JOB_KINDS, consumers=CONSUMERS, backend=backend, stop=stop)
    finally:
        # In a ``finally`` so a loop that exits on its stop signal and one that raises
        # both dispose the backend's engines on the way out.
        reset_backend()


if __name__ == "__main__":
    main()
