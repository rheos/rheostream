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
