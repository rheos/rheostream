# Requires BuildKit: only its per-Dockerfile ignore file keeps this context to the web trees; the legacy builder would apply the root .dockerignore and `COPY modules ./modules` would pull in every Python module.
#
# The web shell's image. `apps/web` has never been containerized before this;
# `make demo` still runs it on the host via `next start`. This image adds no
# change under `apps/web`'s own source — `next.config.ts` is untouched and no
# `output: "standalone"` is added — so the runtime image carries the full pnpm
# workspace install rather than a trimmed standalone bundle.
#
# Build context is the repository root (`docker build -f apps/web/Dockerfile
# .`), matching the root Dockerfile's convention, because the workspace install
# needs the root manifests plus the three `pnpm-workspace.yaml` package globs:
# `apps/web`, `packages/web-contract`, and `modules/*/web` (today only
# `modules/recallatron/web`). `apps/web/Dockerfile.dockerignore` binds to this
# Dockerfile's own name and is the only ignore file BuildKit applies to this
# build; the root `.dockerignore` does not apply here.
FROM node:22.23.3-slim

# corepack ships with Node and pins pnpm to package.json's `packageManager`
# field (pnpm@9.15.9), so the version here can never drift from the one CI's
# `web` job and every contributor's host already use.
RUN corepack enable && corepack prepare pnpm@9.15.9 --activate

WORKDIR /app

# Root workspace manifests first, so `pnpm install` below is cached
# independently of the source copied after it.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./

# The three pnpm-workspace.yaml package globs. `modules` is copied whole
# because the ignore file admits only `modules/*/web` from it, so every
# module's web package arrives without being named here. The committed codegen
# outputs under apps/web/src/generated/ and apps/web/src/modules.generated.ts
# are used as checked in; there is no codegen step in this build.
COPY apps/web ./apps/web
COPY packages/web-contract ./packages/web-contract
COPY modules ./modules

# `--filter @rheo-stream/web...` installs the web app and every workspace
# dependency it needs (web-contract, recallatron-web) and nothing else the
# lockfile resolves for the Python side.
RUN pnpm install --frozen-lockfile --filter @rheo-stream/web...

ENV NEXT_TELEMETRY_DISABLED=1
ENV NODE_ENV=production

RUN pnpm -C apps/web build

EXPOSE 3000

# `next start` looks for the production build in `./.next`, so it runs from
# the app's own directory (the build output is /app/apps/web/.next). pnpm is
# build-time only: the runtime command calls the app's installed `next`
# binary directly rather than going back through `pnpm start`.
WORKDIR /app/apps/web
CMD ["node_modules/.bin/next", "start", "-p", "3000"]
