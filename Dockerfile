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

# Claude Code CLI, pinned to one exact version with checksum verification
# (issue #258, gate B): `runtime.claude_cli.executable` (deploy/README.md,
# docs/architecture/runtime-and-mcp.md) needs a stable absolute path to a real
# binary in the image, and gate B never installs or updates one at container
# start (#152; this image is offline-safe by policy).
#
# Installed from Anthropic's own release bucket rather than the `curl … |
# bash` native installer: that installer self-updates the binary it puts down
# in the background, which conflicts with pinning a single version for the
# life of the image, and it is not written to also verify a checksum for you
# in one non-interactive step. Anthropic instead publishes a signed release
# manifest per version (code.claude.com/docs/en/setup, "Binary integrity and
# code signing") with a SHA256 checksum for every platform binary, the
# manifest itself detached-signed with Anthropic's release GPG key
# (fingerprint 31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE, key at
# https://downloads.claude.ai/keys/claude-code.asc). This layer fetches the
# platform binary directly and checks it against that manifest's checksum,
# which is the exact value copied into CLAUDE_CLI_SHA256_<ARCH> below.
#
# CLAUDE_CLI_VERSION is pinned to the version the local CLI ran on 2026-09-29
# (`claude --version` => 2.1.257 (Claude Code)), confirmed available for both
# Linux platforms this base image needs. TARGETARCH is mapped rather than
# restricted to amd64: the manifest already publishes a linux-arm64 build
# alongside linux-x64, so multi-arch costs two extra `case` lines and one more
# pinned checksum, not a second install path. python:3.12-slim is
# glibc/Debian, so this uses the manifest's linux-x64/linux-arm64 builds, not
# the musl ones.
#
# Checksums verified 2026-09-29 against
# https://downloads.claude.ai/claude-code-releases/2.1.257/manifest.json
# (signature checked per the docs page above before trusting its contents).
ARG CLAUDE_CLI_VERSION=2.1.257
ARG CLAUDE_CLI_SHA256_AMD64=9a64bda9d8722a1fa05bef9a5961d07e0331b99597eda9e2f6a732f3a0ff7f05
ARG CLAUDE_CLI_SHA256_ARM64=22f7d48f17193952c3c2d0b8bf2f31db2cd08fd5fb09a374fa321496b711d017
ARG TARGETARCH
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) claude_platform=linux-x64; claude_sha256="$CLAUDE_CLI_SHA256_AMD64" ;; \
      arm64) claude_platform=linux-arm64; claude_sha256="$CLAUDE_CLI_SHA256_ARM64" ;; \
      *) echo "unsupported TARGETARCH for the pinned Claude CLI: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/claude \
      "https://downloads.claude.ai/claude-code-releases/${CLAUDE_CLI_VERSION}/${claude_platform}/claude"; \
    echo "${claude_sha256}  /tmp/claude" | sha256sum -c -; \
    install -d -m 0755 /opt/claude/bin; \
    install -m 0755 /tmp/claude /opt/claude/bin/claude; \
    rm -f /tmp/claude
# Stable absolute path; this is the value for runtime.claude_cli.executable.
# DISABLE_AUTOUPDATER=1 is the documented env var (code.claude.com/docs/en/setup
# "Disable auto-updates") that keeps a running `claude` from checking for or
# installing an update on its own, matching this image's no-install-at-start
# policy even though a raw binary install (no launcher, no `versions/` dir)
# has no updater of its own to begin with.
ENV PATH="/opt/claude/bin:$PATH" \
    DISABLE_AUTOUPDATER=1

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
COPY apps/bridge ./apps/bridge
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

# Start from the venv exactly as built above, with no network (#152). A plain
# `uv run` re-syncs every workspace member at each container start, and once uv's
# cached index ages that rebuild fetches hatchling from PyPI, so an offline or
# egress-restricted host could not start the image. The default command and the
# compose `command:` lines call the venv's python directly; UV_NO_SYNC=1 covers
# anyone who still types `uv run` inside the container; and the venv's bin on
# PATH makes `rheo …` work as-is in `docker compose exec core`. No sync at start
# also means the alembic script removed above never comes back.
ENV UV_NO_SYNC=1 \
    PATH="/app/.venv/bin:$PATH"

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
CMD ["/app/.venv/bin/python", "-m", "rheo_app_core.serve"]
