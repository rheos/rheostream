# Canonical command surface, started in run 0a. Each target is a thin alias over
# the underlying uv/pnpm/docker commands (plan.md, "Canonical commands"); the tool
# commands are the stable contract E0c freezes, the Makefile is the convenience.
# `test` needs a reachable Postgres (`make up`, or `RHEO_TEST_CLUSTER_DSN`
# pointing at one); `check` stays Docker-free.
#
# This deliberately reverses 0a's original rule ("only up/down/demo need Docker
# running; test/check stay Docker-free"), from run 0b1 on: `core` now makes a
# real database call at startup, and tests/conftest.py bridges the suite into the
# production settings->secret-scope path against a real cluster rather than an
# injected DSN. When the cluster is unreachable, `uv run pytest` fails fast
# (tests/conftest.py, pytest.exit) with a message naming the remedy — it never
# skips, because a skipped `postgres` marker would pass this gate vacuously.

.PHONY: install test lint typecheck build up down demo check migrate codegen spike

install:
	uv sync --frozen
	pnpm install --frozen-lockfile

test:
	uv run pytest
	pnpm -C apps/web test

lint:
	uv run ruff check .
	uv run ruff format --check .
	pnpm -C apps/web lint
	python3 scripts/check_routing_literals.py

typecheck:
	uv run mypy
	pnpm -C apps/web exec tsc --noEmit

build:
	pnpm -C apps/web build
	docker build -f Dockerfile .

up:
	docker compose -f deploy/compose.yaml up -d

down:
	docker compose -f deploy/compose.yaml down

migrate:
	uv run rheo migrate

# End-to-end proof that `core` (in compose) and the web shell (under `next start`,
# outside compose — 0a does not containerize web, spec.md W1) compose without a
# shared container. Starts postgres + core, polls (not a fixed sleep) for core to
# answer and fails loudly if it never does, builds and starts web pointed at the
# published core, then asserts both seams: core answers /healthz with a body only
# our service returns (a bare 200, or a foreign body that happens to contain the
# key, could come from an unrelated process already holding the port) and the web
# page renders the *healthy* core seam ("contract v...") rather than merely
# returning 200 (the shell 200s even when it can't reach core). Host ports are all
# overridable (RHEO_PG_PORT/RHEO_CORE_PORT/RHEO_WEB_PORT) for a machine where a
# default is already taken by something else; running this target requires those
# ports free, so stop `make up`'s shared stack first if it is using the same ones.
#
# Isolated, not merely hermetic: runs in its OWN compose project
# (`-p rheo-stream-demo`), so its containers, network, and the two named volumes
# it creates are namespaced apart from the shared `rheo-stream` project `make
# up`/`make down` manage — `down -v` at teardown can only ever remove volumes this
# target itself created, never a developer's own data. The operator sequence's
# data root is a fresh `mktemp -d` (never `.rheo-local`), removed at teardown
# because this target created it; RHEO__storage__cluster_dsn_ref is pinned so a
# stray, pre-existing deployment.toml elsewhere cannot redirect the operator
# sequence at a different cluster. Every step's failure is checked directly (`||`
# / `if !`) so `set -e` can never make a FAIL line unreachable.
#
# Isolation means this target cannot COEXIST on the same host ports as a running
# `make up` (or anything else already using them) — both would try to bind the
# same host port, and Docker doesn't namespace host ports by compose project. A
# preflight probes the three host ports this target needs before creating
# anything, so that collision fails fast with a message naming the remedy
# (`make down`, or override the port) instead of a raw "port is already
# allocated" Docker error that sends a developer to debug networking instead of
# stopping the shared stack. The preflight only reads (a TCP connect attempt via
# bash's /dev/tcp); it never stops or removes anything itself.
demo:
	@set -e; \
	demo_project="rheo-stream-demo"; \
	pg_port="$${RHEO_PG_PORT:-5432}"; \
	core_port="$${RHEO_CORE_PORT:-8000}"; \
	web_port="$${RHEO_WEB_PORT:-3000}"; \
	port_in_use() { \
		(exec 3<>"/dev/tcp/127.0.0.1/$$1") 2>/dev/null; \
		in_use_rc=$$?; \
		exec 3>&- 2>/dev/null || true; \
		exec 3<&- 2>/dev/null || true; \
		return $$in_use_rc; \
	}; \
	preflight_failed=0; \
	for portspec in "$$pg_port:postgres:RHEO_PG_PORT" "$$core_port:core:RHEO_CORE_PORT" "$$web_port:web:RHEO_WEB_PORT"; do \
		p="$${portspec%%:*}"; \
		rest="$${portspec#*:}"; \
		label="$${rest%%:*}"; \
		varname="$${rest#*:}"; \
		if port_in_use "$$p"; then \
			echo "FAIL  port $$p ($$label) is already in use — if that is this project's 'make up' dev stack, run 'make down' first; otherwise set $$varname to a free port"; \
			preflight_failed=1; \
		fi; \
	done; \
	if [ "$$preflight_failed" != 0 ]; then \
		exit 1; \
	fi; \
	demo_data_root="$$(mktemp -d)"; \
	WEB_PID=""; \
	cleanup() { \
		if [ -n "$$WEB_PID" ]; then kill "$$WEB_PID" 2>/dev/null || true; fi; \
		docker compose -p "$$demo_project" -f deploy/compose.yaml down -v || true; \
		rm -rf "$$demo_data_root"; \
	}; \
	trap cleanup EXIT; \
	docker compose -p "$$demo_project" -f deploy/compose.yaml up -d; \
	echo "Waiting for postgres to accept connections..."; \
	for _ in $$(seq 1 60); do \
		if docker compose -p "$$demo_project" -f deploy/compose.yaml exec -T postgres pg_isready -U rheo -d rheo >/dev/null 2>&1; then break; fi; \
		sleep 1; \
	done; \
	demo_dsn="postgresql://rheo:rheo_dev_only@localhost:$$pg_port/postgres"; \
	echo "Running rheo migrate..."; \
	RHEO_CLUSTER_DSN="$$demo_dsn" RHEO__storage__cluster_dsn_ref="secret://env/RHEO_CLUSTER_DSN" \
		RHEO_PROFILE=development RHEO_DATA_ROOT="$$demo_data_root" uv run rheo migrate \
		|| { echo "FAIL  rheo migrate"; exit 1; }; \
	echo "PASS  rheo migrate"; \
	echo "Running rheo account create..."; \
	account_id="$$(RHEO_CLUSTER_DSN="$$demo_dsn" RHEO__storage__cluster_dsn_ref="secret://env/RHEO_CLUSTER_DSN" \
		RHEO_PROFILE=development RHEO_DATA_ROOT="$$demo_data_root" \
		uv run rheo account create --provider github --subject demo-owner --display-name "Demo owner")" \
		|| { echo "FAIL  rheo account create"; exit 1; }; \
	if [ -z "$$account_id" ]; then \
		echo "FAIL  rheo account create  produced no account id on stdout"; exit 1; \
	fi; \
	echo "PASS  rheo account create  $$account_id"; \
	echo "Running rheo workspace create --owner $$account_id..."; \
	RHEO_CLUSTER_DSN="$$demo_dsn" RHEO__storage__cluster_dsn_ref="secret://env/RHEO_CLUSTER_DSN" \
		RHEO_PROFILE=development RHEO_DATA_ROOT="$$demo_data_root" \
		uv run rheo workspace create --owner "$$account_id" >/dev/null \
		|| { echo "FAIL  rheo workspace create --owner $$account_id"; exit 1; }; \
	echo "PASS  rheo workspace create --owner $$account_id"; \
	echo "Waiting for core /healthz..."; \
	core_up=0; \
	for _ in $$(seq 1 60); do \
		if curl -sf "http://localhost:$$core_port/healthz" 2>/dev/null | grep -q '"status":"ok","contract_version":'; then core_up=1; break; fi; \
		sleep 1; \
	done; \
	if [ "$$core_up" != 1 ]; then \
		echo "FAIL  core  http://localhost:$$core_port/healthz did not become ready within 60s"; \
		exit 1; \
	fi; \
	rc=0; \
	internal_secret="rheo_dev_only_internal_secret"; \
	echo "Checking the internal listener (8100) is unreachable from the host..."; \
	internal_http_code="$$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:8100/internal/v1/routing" 2>/dev/null || true)"; \
	if [ -n "$$internal_http_code" ] && [ "$$internal_http_code" != "000" ]; then \
		echo "FAIL  internal  http://localhost:8100/internal/v1/routing was reachable from the host (http $$internal_http_code) — compose must never publish 8100"; rc=1; \
	else \
		echo "PASS  internal  http://localhost:8100/internal/v1/routing is unreachable from the host"; \
	fi; \
	echo "Checking the internal listener answers inside the container network..."; \
	if docker compose -p "$$demo_project" -f deploy/compose.yaml exec -T core \
		curl -sf -H "X-Rheo-Internal: $$internal_secret" http://localhost:8100/internal/v1/routing >/dev/null 2>&1; then \
		echo "PASS  internal  container-network curl to :8100/internal/v1/routing succeeded"; \
	else \
		echo "FAIL  internal  container-network curl to :8100/internal/v1/routing failed (serve() may not have started internal_app, or the secret drifted from compose.yaml's)"; rc=1; \
	fi; \
	RHEO_CORE_INTERNAL_URL="http://localhost:$$core_port" PORT="$$web_port" pnpm -C apps/web build; \
	RHEO_CORE_INTERNAL_URL="http://localhost:$$core_port" PORT="$$web_port" pnpm -C apps/web start & \
	WEB_PID=$$!; \
	if curl -sf "http://localhost:$$core_port/healthz" 2>/dev/null | grep -q '"status":"ok","contract_version":'; then \
		echo "PASS  core  http://localhost:$$core_port/healthz"; \
	else \
		echo "FAIL  core  http://localhost:$$core_port/healthz"; rc=1; \
	fi; \
	web_ok=0; \
	for _ in $$(seq 1 30); do \
		if curl -s "http://localhost:$$web_port/" | grep -q 'contract v'; then web_ok=1; break; fi; \
		sleep 1; \
	done; \
	if [ "$$web_ok" = 1 ]; then \
		echo "PASS  web   http://localhost:$$web_port/ (core seam healthy: contract v present)"; \
	else \
		echo "FAIL  web   http://localhost:$$web_port/ never rendered the healthy core seam (contract v)"; rc=1; \
	fi; \
	exit $$rc

check:
	python3 scripts/check_repository.py

# Regenerate the two committed artifacts under apps/web/src/generated/ from the
# operation registry: `rheo openapi` builds the document, `openapi-typescript`
# derives the TypeScript `paths` the spike's typed client is indexed by. Both are
# generated files with "do not edit" banners (tests/test_generated_artifacts.py);
# a wrong-looking output is fixed in the generator or the source model and
# regenerated here, never hand-patched.
#
# `RHEO_MODULES=spike` and NO other RHEO_* variable: that is the canonical
# generation environment AC 3 polices, and it is why the emitted bytes depend on
# exactly two things — the installed distributions and RHEO_MODULES — and on
# nothing about the machine. `rheo openapi` is the one subcommand that does not
# bootstrap, so it needs no settings, data root or database; the same command
# therefore produces the same bytes on a developer's laptop and in the `web` CI
# job, which is what makes the `git diff --exit-code` gate meaningful.
#
# **The second command's paths are relative to `apps/web`, not to the repository
# root, and that is not a style choice.** `pnpm -C apps/web` sets the working
# directory before `exec` runs, so a root-relative path would be resolved under
# it and doubled into `apps/web/apps/web/src/generated/openapi.json` — ENOENT,
# exit 1, on the first CI run. Dropping `-C apps/web` is not the alternative:
# `openapi-typescript` is linked into `apps/web/node_modules/.bin`, and the root
# `node_modules/.bin` does not carry it.
#
# This target is permanent. It outlives run 0v's spike module and generates
# whatever the then-current registry holds; only the `spike` value of
# RHEO_MODULES goes at 0c0's branch cut, and the env read itself goes when the
# real `modules.installed` settings key lands.
codegen:
	RHEO_MODULES=spike uv run rheo openapi --out apps/web/src/generated/openapi.json
	pnpm -C apps/web exec openapi-typescript src/generated/openapi.json -o src/generated/api-types.ts

# Run 0v's end-to-end driver — DELETED at 0c0's branch cut, along with
# `modules/spike/**`, the spike web surface, and the two spike-only variables
# RHEO_SPIKE_WORKSPACE_IDS and RHEO_MODULES' env read. `codegen` above stays.
#
# Twelve steps, in `make demo`'s shape: a port preflight that only reads, an
# isolated compose project, a `mktemp -d` data root, polling rather than fixed
# sleeps, one PASS/FAIL line per assertion, `trap cleanup EXIT`, and every
# assertion `||`-guarded or inside an `if` so `set -e` can never make a FAIL line
# unreachable.
#
# **Only Postgres is in compose; `core` and `web` both run on the host (D-4).**
# The internal listener (8100) is container-network-only by construction —
# compose never publishes it and `make demo` asserts it is unreachable from the
# host — so a host-run `web` cannot reach a compose-run `core`'s internal
# listener at all. Publishing 8100 would contradict the invariant `make demo`
# guards, and containerizing `web` is deferred work, so the driver starts `core`
# with `python -m rheo_app_core.serve`, which serves both listeners in one
# process. That one process is load-bearing beyond convenience: `load_modules()`
# runs in `public_app`'s lifespan, and the internal listener reads the module
# surfaces that lifespan registered, so two separate processes would serve an
# empty `surfaces.modules` and the spike page would have no path to render at.
#
# **`core`'s two ports are fixed at 8000 and 8100 and this target cannot move
# them**, unlike `make demo`'s, where RHEO_CORE_PORT moves only a compose
# host-side publish. `apps/core/src/rheo_app_core/serve.py` hardcodes both in its
# `uvicorn.Config` calls and reads no environment, so a RHEO_CORE_PORT here would
# be a knob that changes nothing — the preflight names the real constraint
# instead. Only RHEO_PG_PORT and RHEO_WEB_PORT are overridable.
#
# **The routing topology is set explicitly, all three variables (D-37):** mode
# `path`, scheme `http`, and a port-free `base_host=localhost`.
# `RHEO__routing__scheme` is the one of the three that is not already the
# packaged default (`config/defaults.toml` ships `https`), so leaving it unset
# would silently change the routing config the web tier reads. A port-free
# `base_host` is what makes `Host: localhost:3000` and
# `Origin: http://localhost:3000` pass `normalize_host` -> `is_application_host`;
# `deploy/compose.yaml`'s own `localhost:3000` is a separate open issue and is
# deliberately not edited here.
#
# **RHEO_CORE_PUBLIC_URL is spelled `localhost`, never `127.0.0.1`.** The spike's
# workspace route handler forwards server-side to `POST /auth/session/workspace`,
# and the forwarded request's `Host` header is this URL's own authority; the
# session secret is host-bound, so a secret minted for `localhost` would refuse
# `session_missing` under `127.0.0.1`. RHEO_CORE_INTERNAL_API_URL is `127.0.0.1`
# because the internal listener reads `X-Rheo-Host`, not the authority.
#
# Step 4 invokes the sibling target as `$${MAKE}` rather than `$(MAKE)`. A recipe
# line carrying the literal `$(MAKE)` is executed even under `make -n`, and this
# whole recipe is a single backslash-continued line, so the `$(MAKE)` spelling
# would make `make -n spike` bring up docker and drive the entire slice instead
# of printing it. `MAKE` is exported into every recipe's environment, so the
# variable resolves to the same binary and MAKEFLAGS still propagates.
spike:
	@set -e; \
	spike_project="rheo-stream-spike"; \
	pg_port="$${RHEO_PG_PORT:-5432}"; \
	web_port="$${RHEO_WEB_PORT:-3000}"; \
	core_port=8000; \
	internal_port=8100; \
	spike_secret="rheo_dev_only_internal_secret"; \
	spike_page="/spike"; \
	note_body="hello from the spike driver"; \
	port_in_use() { \
		(exec 3<>"/dev/tcp/127.0.0.1/$$1") 2>/dev/null; \
		in_use_rc=$$?; \
		exec 3>&- 2>/dev/null || true; \
		exec 3<&- 2>/dev/null || true; \
		return $$in_use_rc; \
	}; \
	preflight_failed=0; \
	for portspec in \
		"$$pg_port|postgres|set RHEO_PG_PORT to a free port" \
		"$$web_port|web|set RHEO_WEB_PORT to a free port" \
		"$$core_port|core public listener|free the port: rheo_app_core.serve binds it unconditionally, so it is not overridable here" \
		"$$internal_port|core internal listener|free the port: rheo_app_core.serve binds it unconditionally, so it is not overridable here"; do \
		p="$${portspec%%|*}"; \
		rest="$${portspec#*|}"; \
		label="$${rest%%|*}"; \
		remedy="$${rest#*|}"; \
		if port_in_use "$$p"; then \
			echo "FAIL  port $$p ($$label) is already in use — if that is this project's 'make up' stack or a 'make demo' run, stop it first; otherwise $$remedy"; \
			preflight_failed=1; \
		fi; \
	done; \
	if [ "$$preflight_failed" != 0 ]; then \
		exit 1; \
	fi; \
	spike_data_root="$$(mktemp -d)"; \
	headers="$$spike_data_root/response-headers.txt"; \
	CORE_PID=""; \
	WEB_PID=""; \
	cleanup() { \
		if [ -n "$$WEB_PID" ]; then kill "$$WEB_PID" 2>/dev/null || true; wait "$$WEB_PID" 2>/dev/null || true; fi; \
		if [ -n "$$CORE_PID" ]; then kill "$$CORE_PID" 2>/dev/null || true; wait "$$CORE_PID" 2>/dev/null || true; fi; \
		for _ in $$(seq 1 20); do \
			if ! port_in_use "$$web_port" && ! port_in_use "$$core_port"; then break; fi; \
			sleep 1; \
		done; \
		docker compose -p "$$spike_project" -f deploy/compose.yaml down -v || true; \
		rm -rf "$$spike_data_root"; \
	}; \
	trap cleanup EXIT; \
	rc=0; \
	assert_match() { \
		if printf '%s' "$$3" | grep -qE -- "$$2"; then \
			echo "PASS  $$1"; \
		else \
			echo "FAIL  $$1  (nothing matched /$$2/)"; \
			rc=1; \
		fi; \
	}; \
	assert_no_match() { \
		if printf '%s' "$$3" | grep -qE -- "$$2"; then \
			echo "FAIL  $$1  (matched /$$2/, which must not appear)"; \
			rc=1; \
		else \
			echo "PASS  $$1"; \
		fi; \
	}; \
	spike_get() { \
		curl -s -L -H "Cookie: rheo_session=$$session_cookie" "http://localhost:$$web_port$$1" || true; \
	}; \
	spike_post() { \
		sp_path="$$1"; \
		shift; \
		: > "$$headers"; \
		curl -s -o /dev/null -D "$$headers" -X POST \
			-H "Cookie: rheo_session=$$session_cookie" \
			-H "Origin: http://localhost:$$web_port" \
			"$$@" "http://localhost:$$web_port$$sp_path" >/dev/null 2>&1 || true; \
		awk 'tolower($$1) == "location:" { print $$2 }' "$$headers" | tr -d '\r'; \
	}; \
	echo "== step 1: postgres (compose project $$spike_project, host port $$pg_port)"; \
	docker compose -p "$$spike_project" -f deploy/compose.yaml up -d postgres \
		|| { echo "FAIL  docker compose up -d postgres"; exit 1; }; \
	pg_ready=0; \
	for _ in $$(seq 1 60); do \
		if docker compose -p "$$spike_project" -f deploy/compose.yaml exec -T postgres pg_isready -U rheo -d rheo >/dev/null 2>&1; then pg_ready=1; break; fi; \
		sleep 1; \
	done; \
	if [ "$$pg_ready" != 1 ]; then \
		echo "FAIL  postgres never accepted connections within 60s"; exit 1; \
	fi; \
	echo "PASS  postgres accepting connections on $$pg_port"; \
	export RHEO_CLUSTER_DSN="postgresql://rheo:rheo_dev_only@localhost:$$pg_port/postgres"; \
	export RHEO__storage__cluster_dsn_ref="secret://env/RHEO_CLUSTER_DSN"; \
	export RHEO_PROFILE=development; \
	export RHEO_DATA_ROOT="$$spike_data_root"; \
	echo "== step 2: operator sequence (one account, two workspaces)"; \
	uv run rheo migrate || { echo "FAIL  rheo migrate"; exit 1; }; \
	echo "PASS  rheo migrate"; \
	account_id="$$(uv run rheo account create --provider github --subject spike-owner --display-name 'Spike owner')" \
		|| { echo "FAIL  rheo account create"; exit 1; }; \
	if [ -z "$$account_id" ]; then \
		echo "FAIL  rheo account create  produced no account id on stdout"; exit 1; \
	fi; \
	echo "PASS  rheo account create  $$account_id"; \
	workspace_a="$$(uv run rheo workspace create --owner "$$account_id")" \
		|| { echo "FAIL  rheo workspace create (A)"; exit 1; }; \
	workspace_b="$$(uv run rheo workspace create --owner "$$account_id")" \
		|| { echo "FAIL  rheo workspace create (B)"; exit 1; }; \
	if [ -z "$$workspace_a" ] || [ -z "$$workspace_b" ] || [ "$$workspace_a" = "$$workspace_b" ]; then \
		echo "FAIL  rheo workspace create  did not produce two distinct workspace ids (A='$$workspace_a' B='$$workspace_b')"; exit 1; \
	fi; \
	echo "PASS  rheo workspace create  A=$$workspace_a  B=$$workspace_b  (both owned by $$account_id, which AC 7's switch needs)"; \
	echo "== step 3: rheo-spike install into both workspaces"; \
	RHEO_MODULES=spike uv run rheo-spike install --workspace "$$workspace_a" \
		|| { echo "FAIL  rheo-spike install --workspace A"; exit 1; }; \
	RHEO_MODULES=spike uv run rheo-spike install --workspace "$$workspace_b" \
		|| { echo "FAIL  rheo-spike install --workspace B"; exit 1; }; \
	echo "PASS  rheo-spike install  schema spike + enabled core.module_state row in A and B"; \
	echo "== step 4: make codegen against the live registry"; \
	"$${MAKE:-make}" codegen || { echo "FAIL  make codegen"; exit 1; }; \
	if git diff --quiet -- apps/web/src/generated; then \
		echo "PASS  codegen  both artifacts regenerate byte-identically from the live registry"; \
	else \
		echo "FAIL  codegen  regenerating changed apps/web/src/generated — the committed artifacts are stale"; \
		rc=1; \
	fi; \
	echo "== step 5: core on the host, both listeners in one process"; \
	RHEO_MODULES=spike \
	RHEO_INTERNAL_SECRET="$$spike_secret" \
	RHEO__internal__secret_ref="secret://env/RHEO_INTERNAL_SECRET" \
	RHEO__routing__mode=path \
	RHEO__routing__scheme=http \
	RHEO__routing__base_host=localhost \
		uv run python -m rheo_app_core.serve & \
	CORE_PID=$$!; \
	core_up=0; \
	for _ in $$(seq 1 60); do \
		if curl -s "http://localhost:$$core_port/healthz" 2>/dev/null | grep -q '"status":"ok","contract_version":'; then core_up=1; break; fi; \
		sleep 1; \
	done; \
	if [ "$$core_up" != 1 ]; then \
		echo "FAIL  core  http://localhost:$$core_port/healthz never answered our own body within 60s"; exit 1; \
	fi; \
	echo "PASS  core  http://localhost:$$core_port/healthz"; \
	routing_document="$$(curl -s -H "X-Rheo-Internal: $$spike_secret" "http://localhost:$$internal_port/internal/v1/routing" || true)"; \
	assert_match "internal  the internal listener serves the routing document with the spike module surface (D-6: surfaces come from the loaded manifests, at runtime)" \
		'"modules":[{]"spike":[{]"host":"spike","path":"/spike"' "$$routing_document"; \
	echo "== step 6: web on the host, pointed at both listeners"; \
	RHEO_CORE_INTERNAL_API_URL="http://127.0.0.1:$$internal_port" \
	RHEO_CORE_PUBLIC_URL="http://localhost:$$core_port" \
	RHEO_INTERNAL_SECRET="$$spike_secret" \
	RHEO_SPIKE_WORKSPACE_IDS="$$workspace_a,$$workspace_b" \
	PORT="$$web_port" \
		pnpm -C apps/web build || { echo "FAIL  pnpm -C apps/web build"; exit 1; }; \
	RHEO_CORE_INTERNAL_API_URL="http://127.0.0.1:$$internal_port" \
	RHEO_CORE_PUBLIC_URL="http://localhost:$$core_port" \
	RHEO_INTERNAL_SECRET="$$spike_secret" \
	RHEO_SPIKE_WORKSPACE_IDS="$$workspace_a,$$workspace_b" \
	PORT="$$web_port" \
		pnpm -C apps/web start & \
	WEB_PID=$$!; \
	web_up=0; \
	for _ in $$(seq 1 60); do \
		if curl -s -o /dev/null -w '%{http_code}' "http://localhost:$$web_port$$spike_page" 2>/dev/null | grep -qE '^[1-5][0-9][0-9]$$'; then web_up=1; break; fi; \
		sleep 1; \
	done; \
	if [ "$$web_up" != 1 ]; then \
		echo "FAIL  web  http://localhost:$$web_port$$spike_page never answered within 60s"; exit 1; \
	fi; \
	echo "      web  http://localhost:$$web_port$$spike_page is answering (readiness, not an assertion: without a cookie the middleware 307s, so the first page assertion is step 8's)"; \
	echo "== step 7: mint a session (no OAuth provider in the driver)"; \
	session_cookie="$$(uv run rheo-spike session --account "$$account_id" --host localhost)" \
		|| { echo "FAIL  rheo-spike session"; exit 1; }; \
	if [ -z "$$session_cookie" ]; then \
		echo "FAIL  rheo-spike session  printed no cookie value on stdout"; exit 1; \
	fi; \
	echo "PASS  rheo-spike session  host-bound cookie minted for $$account_id on localhost"; \
	echo "== step 8: first run, then select workspace A through the spike's own form"; \
	page="$$(spike_get "$$spike_page")"; \
	assert_match "first-run  the page renders the workspace_unselected state" \
		'session: no workspace selected' "$$page"; \
	assert_match "first-run  the page renders the spike's own workspace form" \
		'name="target_workspace_id"' "$$page"; \
	location="$$(spike_post "$$spike_page/workspace" --data-urlencode "target_workspace_id=$$workspace_a")"; \
	assert_match "switch-a  the 303 carries the outcome, not merely a redirect (a refused switch redirects too)" \
		'workspace=selected' "$$location"; \
	page="$$(spike_get "$${location:-$$spike_page}")"; \
	assert_match "switch-a  the page now renders a session with workspace A active" \
		"workspace: (<!-- -->)?$$workspace_a" "$$page"; \
	echo "== step 9: add a note, then prove the outcome assertion reads the outcome"; \
	location="$$(spike_post "$$spike_page/add" --data-urlencode "body=$$note_body")"; \
	assert_match "add  the 303 carries add=succeeded" 'add=succeeded' "$$location"; \
	page="$$(spike_get "$${location:-$$spike_page}")"; \
	assert_match "add  the page renders the note body (AC 5, AC 6)" \
		"<span>$$note_body</span>" "$$page"; \
	location="$$(spike_post "$$spike_page/add" --data-urlencode "body=")"; \
	assert_match "add-refused  an empty body redirects with add=input_invalid, not add=succeeded" \
		'add=input_invalid' "$$location"; \
	page="$$(spike_get "$${location:-$$spike_page}")"; \
	assert_match "add-refused  the page renders its error state for that outcome" \
		'add refused: input_invalid' "$$page"; \
	echo "== step 10: switch to workspace B through the shipped /auth/session/workspace"; \
	location="$$(spike_post "$$spike_page/workspace" --data-urlencode "target_workspace_id=$$workspace_b")"; \
	assert_match "switch-b  the 303 carries the outcome of the forwarded switch" \
		'workspace=selected' "$$location"; \
	page="$$(spike_get "$${location:-$$spike_page}")"; \
	assert_match "isolation  the page now renders a session with workspace B active" \
		"workspace: (<!-- -->)?$$workspace_b" "$$page"; \
	assert_match "isolation  workspace B lists zero notes (an empty list is a success, not a refusal)" \
		'notes: none yet' "$$page"; \
	assert_no_match "isolation  workspace A's note is not visible from workspace B" \
		"<span>$$note_body</span>" "$$page"; \
	echo "== step 11: a module that is not named does not load"; \
	unnamed_document="$$(RHEO_MODULES= uv run rheo openapi --out - || true)"; \
	assert_no_match "not-activated  with RHEO_MODULES unset the document carries no spike path" \
		'"/api/v1/operations/spike\.' "$$unnamed_document"; \
	assert_match "not-activated  and it is a real document, not an empty or errored one" \
		'"/api/v1/operations/core\.workspace\.status"' "$$unnamed_document"; \
	echo "== step 12: teardown"; \
	exit $$rc
