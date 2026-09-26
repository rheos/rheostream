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

.PHONY: install test lint typecheck build up down demo check migrate codegen absence-proof criterion-31 legacy-names fixture-provenance theme-tokens search-boundary workspace-scripts flagship-config flagship-up flagship-down

ABSENCE_PROOF_CONFIG := $(shell git rev-parse --git-path rheo-absence-config.json)
ABSENCE_PROOF_CHECKOUT := $(shell git rev-parse --git-path rheo-absence-checkout.json)

install:
	uv sync --frozen --extra local-embeddings
	pnpm install --frozen-lockfile

test:
	uv run pytest
	pnpm -r test

lint:
	uv run ruff check .
	uv run ruff format --check .
	pnpm -r lint
	python3 scripts/check_routing_literals.py
	python3 scripts/check_theme_tokens.py
	python3 scripts/check_search_boundary.py

typecheck:
	uv run mypy
	pnpm -r typecheck

build:
	pnpm -C apps/web build
	docker build -f Dockerfile .

up:
	docker compose -f deploy/compose.yaml up -d

down:
	docker compose -f deploy/compose.yaml down

# Prompt 5: the flagship deployment overlay (deploy/compose.flagship.yaml), separate
# from the dev stack above. `flagship-config` renders the resolved config against the
# committed placeholder env file — a syntax/interpolation proof, not a real deploy.
flagship-config:
	docker compose -f deploy/compose.flagship.yaml --env-file deploy/.env.flagship.example config --quiet

# Refuses instead of silently falling back to the placeholder example: a real
# deploy env carries real secrets, and defaulting to the committed example would
# either fail loudly on FQDN mismatches or, worse, run with placeholder values.
flagship-up:
	@if [ -z "$$FLAGSHIP_ENV" ] || [ ! -f "$$FLAGSHIP_ENV" ]; then \
		echo "FAIL  FLAGSHIP_ENV must name an existing env file (see deploy/.env.flagship.example) — e.g. make flagship-up FLAGSHIP_ENV=/path/to/real.env"; \
		exit 1; \
	fi
	docker compose -p rheo-stream-flagship -f deploy/compose.flagship.yaml --env-file "$$FLAGSHIP_ENV" up -d --build

# Never `-v`: this stack's named volumes hold real data and must survive a
# routine stop/restart.
flagship-down:
	docker compose -p rheo-stream-flagship -f deploy/compose.flagship.yaml down

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
	python3 scripts/check_legacy_names.py
	python3 scripts/check_fixture_provenance.py
	python3 scripts/check_search_boundary.py
	python3 scripts/check_workspace_scripts.py

# Criterion 34: no predecessor-product name or generalized source-product-prefix
# compound identifier in any tracked path, or in any tracked file's text outside
# the historical-docs exception list `check_legacy_names.py` declares (see that
# script's own docstring for the exact denylist). Self-tests itself before
# scanning the real tree, same convention as check_routing_literals.py.
legacy-names:
	python3 scripts/check_legacy_names.py

# Criterion 35: every fixtures/-segment file has a provenance.json entry, and
# content heuristics over fixtures/tests/web source catch a non-reserved email,
# a non-documentation IPv4, a phone-shaped string, or an hourly-rate-shaped
# string. Self-tests itself before scanning the real tree.
fixture-provenance:
	python3 scripts/check_fixture_provenance.py

# AC 4: no hard-coded color/spacing value or inline style outside the theme
# token contract, over apps/web/src and modules/*/web (theme data files under
# apps/web/src/theme/themes/ and codegen output under apps/web/src/generated/ are
# exempt, those exact paths only). Self-tests itself before scanning the real
# tree.
theme-tokens:
	python3 scripts/check_theme_tokens.py

# AC 10: no search input anywhere in the application shell outside
# Recallatron's own screen components. Part of both `lint` and `check`; CI runs
# it as its own step. Self-tests itself before scanning the real tree.
search-boundary:
	python3 scripts/check_search_boundary.py

# Every web workspace package (apps/*, packages/*, modules/*/web) declares lint,
# typecheck and test scripts and is listed in pnpm-workspace.yaml, because
# `pnpm -r <script>` silently skips a package without one. Part of `check`.
# Self-tests itself before checking the real tree.
workspace-scripts:
	python3 scripts/check_workspace_scripts.py

# The tree must be committed first because checkout builds its comparison from HEAD.
absence-proof:
	uv run python scripts/absence_proof.py --mode config --out $(ABSENCE_PROOF_CONFIG)
	uv run python scripts/absence_proof.py --mode checkout --out $(ABSENCE_PROOF_CHECKOUT)
	uv run python scripts/absence_proof.py --mode compare $(ABSENCE_PROOF_CONFIG) $(ABSENCE_PROOF_CHECKOUT)

# Project criterion 31: Recallatron's behavioural suite passes under each retrieval
# strategy. The same file runs twice, with every workspace pinned to `lexical`, then
# to `dense` against the real local embedding model at the shipped relevance floor.
# The two variables are read by that file's own fixtures, deliberately not `RHEO__*`
# ones, which tests/conftest.py refuses at import. The lexical pass clears the
# provider variable so an exported one cannot leak into it. One pass per recipe line,
# so either pass going red fails the target and the second cannot mask the first.
#
# The dense pass embeds every live memory synchronously after each commit point, so
# it proves the suite passes in the steady state, with the embed job's lag collapsed
# to zero. It does not prove a memory written a moment ago is dense-findable before
# its embed job runs.
criterion-31:
	RHEO_TEST_RETRIEVAL_STRATEGY=lexical RHEO_TEST_EMBEDDING_PROVIDER= uv run pytest tests/postgres/test_memory_records.py
	RHEO_TEST_RETRIEVAL_STRATEGY=dense RHEO_TEST_EMBEDDING_PROVIDER=local uv run pytest tests/postgres/test_memory_records.py

# Regenerate the two committed artifacts under apps/web/src/generated/ from the
# operation registry: `rheo openapi` builds the document, `openapi-typescript`
# derives the TypeScript `paths` apps/web's typed client is indexed by. Both are
# generated files with "do not edit" banners (tests/test_generated_artifacts.py);
# a wrong-looking output is fixed in the generator or the source model and
# regenerated here, never hand-patched.
#
# The third command writes apps/web/src/modules.generated.ts, the composed module
# list, from every installed distribution's manifest. It reads no setting (not even
# `modules.installed`, see `rheo_app_cli.web_compose`), so it needs neither pin
# below. It does check apps/web/package.json first and refuses, by name, a module
# whose web package is not a dependency there.
#
# A canonical generation environment is what AC 3 depends on: the emitted bytes
# must follow from the installed distributions and from nothing about the machine
# that ran the command. The module allowlist is the deployment-scope setting
# `modules.installed`, and TWO ambient sources can set it — the
# `RHEO__modules__installed` variable, and a `deployment.toml` under the resolved
# data root. `rheo openapi` still bootstraps nothing: it resolves settings and
# reads no database.
#
# **Pinned here, from the commit that ships the first real `rheo.modules` entry
# point.** The recipe used to only *depend* on that environment, which cost nothing
# while `entry_points(group="rheo.modules")` returned nothing: with no module
# discoverable, neither source had anything to activate. `modules/recallatron`
# publishes one now, so both are pinned:
#
#   - `RHEO__modules__installed=` — the empty string decodes to the empty list
#     (`settings/schema.py`'s `decode_text`: a `list[str]` drops empty items), and
#     the environment layer is applied over `deployment.toml`, so this is a
#     positive "no module is installed", not merely an absent variable.
#   - `RHEO_DATA_ROOT` at a fresh `mktemp -d`, removed by a `trap` on exit, so the
#     `<data_root>/config/deployment.toml` that data-root resolution would
#     otherwise find on a developer's machine does not exist for this command.
#
# **What the pin does and does not cover, stated exactly rather than as "no `RHEO_*`
# variable".** It pins ONE key by name and relocates the file layer; it does not
# scrub the environment, any other `RHEO__*` variable still reaches `rheo openapi`,
# and this recipe itself sets two `RHEO_*` variables. That is sufficient because
# `modules.installed` is the only resolved setting the emitted document is built
# from — registration reaches `current_profile()` only for the `test_harness`
# origin, which neither the core operations nor a real module use. A later setting
# that moved the emitted bytes would need its own pin on this line; none does today.
#
# **The drift it guards is not yet byte-visible, and the honest form of the claim
# matters more than the stronger one.** The shipped Recallatron skeleton declares no
# operation, tool or web surface, so naming it through either source yields a
# byte-identical document. What the pin stops *today* is a malformed
# `deployment.toml`, which aborts `rheo openapi` outright rather than silently. A
# developer regenerating a genuinely *different* `openapi.json` than the one
# `repository-checks.yml` diffs becomes reachable the moment a module declares
# something the document carries — so the recipe is pinned before that rather than
# after it. CI needs neither pin: a runner carries no `RHEO__*` and no
# `deployment.toml`.
#
# The `trap` and the command it protects share one logical line on purpose: without
# `.ONESHELL` make runs each recipe line in its own shell, so a `trap` set on an
# earlier line would fire at that line's end and delete the data root before
# `rheo openapi` ran.
#
# **The second command's paths are relative to `apps/web`, not to the repository
# root, and that is not a style choice.** `pnpm -C apps/web` sets the working
# directory before `exec` runs, so a root-relative path would be resolved under
# it and doubled into `apps/web/apps/web/src/generated/openapi.json` — ENOENT,
# exit 1, on the first CI run. Dropping `-C apps/web` is not the alternative:
# `openapi-typescript` is linked into `apps/web/node_modules/.bin`, and the root
# `node_modules/.bin` does not carry it.
#
# This target is permanent. It generates whatever the then-current registry holds.
# Run 0v's throwaway module used to be the last distribution publishing a
# `rheo.modules` entry point, and while that held there was nothing for this recipe
# to pass either way; `modules/recallatron` publishes one again, which is why the
# recipe now pins the setting rather than passing nothing.
codegen:
	codegen_data_root="$$(mktemp -d)" || exit 1; \
		trap 'rm -rf "$$codegen_data_root"' EXIT; \
		RHEO__modules__installed= RHEO_DATA_ROOT="$$codegen_data_root" \
		uv run rheo openapi --out apps/web/src/generated/openapi.json
	pnpm -C apps/web exec openapi-typescript src/generated/openapi.json -o src/generated/api-types.ts
	uv run rheo web compose --out apps/web/src/modules.generated.ts
