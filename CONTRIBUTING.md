# Contributing

rheoStream is partway through release one: the framework core and the Recallatron
module are built, and a reference instance runs at `rheo.stream` (see the
[README](README.md#status)). Start with the
[idea document](docs/ideas/rheo-stream-idea.md), then the accepted
[requirements](docs/requirements/README.md) and [architecture](docs/architecture/README.md).
Discuss scope in an issue before adding implementations or introducing dependencies.

Create a dedicated branch and linked draft pull request before implementation.
Describe the resulting behavior, run relevant checks, and complete review before
merging through GitHub. Keep proposals distinguishable from accepted decisions.

Preserve the architecture boundaries when contributing:

- Keep profession-specific behavior in modules and domain packs. Core services
  and domain modules must remain independent of the selected AI runtime.
- Use service/event contracts instead of writing another module's tables. Treat
  dependencies, disablement, retained data, removal, and restoration as design work.
- Public defaults must be reusable. Use synthetic fixtures and never load real
  workspaces or production data in tests.
- Keep stack, storage, runtime rollout, and license decisions explicit. Directory
  placeholders do not settle the open decisions in the idea document.
- Import selected material only after privacy, provenance, and license review;
  do not copy legacy Git history, private documentation, or whole production trees.

For repository changes, run:

```sh
python3 scripts/check_repository.py
```

`make check` runs that plus the legacy-name, fixture-provenance, search-boundary,
workspace-scripts and module-web-boundary gates; `make lint`, `make typecheck` and `make test` cover code
changes. `uv run pytest` needs a reachable Postgres cluster: `make up` starts one, or
point `RHEO_TEST_CLUSTER_DSN` at your own. For local runs, `make test-fast` is
quicker: it starts a throwaway test-only cluster on port 5434 (durability off, its
own disposable data volume) and runs `make test` against it. [tests/README.md](tests/README.md) has
the details. [scripts/README.md](scripts/README.md) describes each gate.

Review staged content for private information as well as credentials. Use invented
people, organizations, and source payloads in examples. Never attach real client
data, production exports, or secrets to public issues, PRs, test results, or logs.
The path checks are deliberately limited and do not establish that arbitrary file
contents are safe to publish.

Use the [parent workspace layout](docs/workspace-layout.md) to keep the public
checkout alongside private configuration and runtime data. Open the parent in an
editor, but run Git, builds, and publication from the child checkout. Do not create
a Git repository at the parent or link private sibling content into the public
tree. The optional `/.rheo-local/` directory is ignored; it must not become the
source of committed fixtures. Public packs contain generic defaults, while
personal overrides remain private.

Keep project `AGENTS.md`, `AGENTS.override.md`, `CLAUDE.md`, `CLAUDE.local.md`,
and local coding-agent configuration out of Git. The repository ignores these
files at every depth and excludes them from its source-only container context.
Internal build procedures and handoffs belong in private agent context; only
general contributor guidance belongs here. See
[private instructions and new sessions](docs/workspace-layout.md#private-instructions-and-new-sessions).
Ignore rules do not remove already tracked files or erase earlier history.

The project is licensed under AGPL-3.0; see [LICENSE](LICENSE). Contributor
terms beyond that file are not a separate agreement. Discuss architectural
proposals through issues. Reused material needs source provenance and license
review.
