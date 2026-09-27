# Behavioral tests

Contract, integration, migration, and acceptance tests for the core, the modules
and the deployment files. Use synthetic isolated workspaces and exercise
absent/disabled modules as well as normal operation. Never load real user data or a
production database. Module-owned tests live under each module (for example
`modules/recallatron/tests`), and the web packages carry their own vitest suites.

`tests/test_acceptance_matrix.py` guards the [acceptance matrices](../docs/acceptance/README.md):
every expected criterion has a row, every demonstrator still resolves, and every captured
mutation hunk still applies.

From run 0b1 on, `uv run pytest` needs a reachable Postgres cluster (`make up`, or
point `RHEO_TEST_CLUSTER_DSN` at one); the `postgres` marker selects the
database-touching tests, and the suite fails fast — never a silent skip — when the
cluster is unreachable.

## A fast, throwaway test cluster

`make up`'s Postgres is also the dev and demo database, so it runs at full
durability on a Docker volume, and on a laptop it is often shared by several
sessions at once. For local test runs, use the test-only cluster in
`deploy/compose.test.yaml` instead:

```sh
make test-fast                 # start the test cluster if needed, then run make test against it
make test-fast PYTEST_ARGS=tests/postgres/test_provisioning.py   # narrow the pytest half
make test-pg-up                # just start it (idempotent)
make test-pg-down              # throw it away
```

It runs the same image as CI (`pgvector/pgvector:pg16`) with the same credentials,
but with `fsync`, `synchronous_commit` and `full_page_writes` off and its data dir on
tmpfs. Nothing survives `make test-pg-down` or a Docker restart, which is the point.
Never use it for anything but tests.

It listens on host port 5434 by default. Set `RHEO_TEST_PG_PORT` to run your own on
another port; the compose project is named after the port
(`rheo-stream-test-<port>`), so parallel sessions on different ports never share or
stop each other's cluster, and none of them touches the `rheo-stream` project that
`make up` and `make down` manage. To point a plain `uv run pytest` at it, set
`RHEO_TEST_CLUSTER_DSN=postgresql://rheo:rheo_dev_only@localhost:5434/postgres`.

`make test` and every default are unchanged: `tests/conftest.py`'s
`DEFAULT_TEST_CLUSTER_DSN` and `.env.example` still name port 5432, the compose
dev stack's default `RHEO_PG_PORT`. If your dev stack runs on another port (5432 is
often taken by another project), either set `RHEO_TEST_CLUSTER_DSN` to match or use
`make test-fast`.
