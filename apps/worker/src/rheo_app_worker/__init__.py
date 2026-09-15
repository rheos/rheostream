"""The worker process. Its composition root is main.py, and it is runnable.

main.py holds the process-wide job-kind registry, installs the stop signals, and runs
rheo_core.work.loop.worker_loop until the container is asked to stop. This package
shares the core image and is started with a command override
(python -m rheo_app_worker.main); the loop it runs is specified in
docs/architecture/intake-and-events.md.
"""
