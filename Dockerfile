# Shared Python core/worker image. The default command serves apps/core's two
# listeners: the public one (`/healthz`, `/auth/*`, the `api` surface) on 8000
# and the internal one (container-network only) on 8100, both under one process
# via `rheo_app_core.serve` (C10) — never `uvicorn ... --workers N`, which would
# break the single-loop, shared-process design both listeners depend on.
# `deploy/compose.yaml`'s `worker` service overrides this default via its own
# `command:`; the image itself is unchanged. The build context is the
# checkout root (deploy/compose.yaml sets `context: ..`), so the COPYs below
# are repo-root-relative.
FROM python:3.12-slim

# curl: not in the base image, and needed inside this container specifically —
# `make demo` (C10) proves the internal listener is container-network-only by
# curling it from the host (must fail to connect) and then from inside this
# same container via `docker compose ... exec` (must succeed), which is the
# only way to tell "correctly unpublished" apart from "never started". A
# `--no-install-recommends` apt install kept in its own early layer so it
# caches independently of source changes below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Pin uv to an exact version (spec.md Technical Risks, risk 1: uv is the newer
# pick, so it is pinned rather than floated). Copying the static binary from the
# official uv image is uv's recommended, reproducible install path.
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /uvx /bin/

WORKDIR /app

# Root build manifests first. The .dockerignore re-include (C4) is what lands
# these three in the build context; without it `uv sync --frozen` below has no
# manifest or lock and fails.
COPY pyproject.toml uv.lock .python-version ./

# Python workspace members the lock resolves (every member except apps/web,
# which is a pnpm app excluded from the uv workspace).
COPY packages/ ./packages/
COPY apps/core ./apps/core
COPY apps/worker ./apps/worker
COPY apps/mcp ./apps/mcp
COPY apps/cli ./apps/cli
COPY modules/ ./modules/
COPY connectors/ ./connectors/
COPY runtimes/ ./runtimes/
COPY channels/ ./channels/

# `uv sync --frozen` installs alembic's own console script (.venv/bin/alembic)
# as a side effect of resolving the alembic dependency; drop it so the image
# matches the ratified claim (storage-and-workspaces.md § Migrations): "Alembic's
# own command is not installed as a console script in the image." The real
# defence is each env.py refusing to run without the orchestrator's connection;
# this closes the doc/reality gap around it.
ARG UV_SYNC_ARGS=""
RUN uv sync --frozen $UV_SYNC_ARGS \
    && rm -f /app/.venv/bin/alembic

# The in-image data root (rheo_core.storage.data_root reads RHEO_IN_CONTAINER to
# pick it) and the flag that makes that branch live rather than dead code.
# Mode 0700, owner root (the process user; the image sets no USER), because the
# data root holds secrets/ and config/deployment.toml (#161). Docker copies this
# mode into a named volume the first time it mounts an empty one;
# deploy/core-entrypoint.sh tightens a volume created before this line existed.
RUN install -d -m 0700 /var/lib/rheo-stream
ENV RHEO_IN_CONTAINER=1

COPY --chmod=0755 deploy/core-entrypoint.sh /usr/local/bin/rheo-core-entrypoint

EXPOSE 8000
# The entrypoint only fixes the data root's mode and then execs its arguments, so
# `worker`'s `command:` override and a `docker run ... <cmd>` both still run as given.
ENTRYPOINT ["/usr/local/bin/rheo-core-entrypoint"]
CMD ["uv", "run", "python", "-m", "rheo_app_core.serve"]
