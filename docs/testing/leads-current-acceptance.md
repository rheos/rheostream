# Leads-to-Current acceptance stack

This is maintainer documentation for the synthetic acceptance stack behind the
Leads-to-Current handoff. It prepares a local, disposable copy of the product for a
manual browser review. It is not evidence that the review has passed.

## The authority

[`tests/fixtures/leads-current-handoff.json`](../../tests/fixtures/leads-current-handoff.json)
lists all 27 named states. Each state names its own automated tests and how a reviewer
reaches it: a seeded URL (generic and job-search Leads variants), a normal
interaction, or a scripted step on the disposable stack. Two asymmetric item/project
denial states use restricted-token authorization tests plus render tests instead of a
browser row, because an ordinary owner or member can always read both siblings.
`tests/test_prompt14_acceptance.py` keeps that file honest, and
`tests/postgres/test_prompt14_seed.py` runs the seed against disposable test databases.

The `prompt14` file, variable and project names are the harness's historical
identifiers. They name this stack and nothing else.

## The stack

`tests/harness/prompt14.sh` drives one Compose project per source commit, using
`deploy/compose.yaml` plus the test-only `tests/harness/compose.prompt14.yaml`:

```
tests/harness/prompt14.sh prepare-env /ABSOLUTE/PRIVATE/stack.env
tests/harness/prompt14.sh build /ABSOLUTE/PRIVATE/stack.env /ABSOLUTE/PROOF_DIR
tests/harness/prompt14.sh up /ABSOLUTE/PRIVATE/stack.env /ABSOLUTE/PROOF_DIR
tests/harness/prompt14.sh seed /ABSOLUTE/PRIVATE/stack.env /ABSOLUTE/PROOF_DIR
tests/harness/prompt14.sh urls /ABSOLUTE/PRIVATE/stack.env
tests/harness/prompt14.sh reset /ABSOLUTE/PRIVATE/stack.env /ABSOLUTE/PROOF_DIR
```

- The env file must live outside the checkout. The project name and image tags come
  from `HEAD`, and `build` refuses a working tree with uncommitted changes, so a tag
  never misstates what it contains.
- Every command first saves the fully resolved Compose configuration and refuses to
  go on unless every writable volume belongs to this project, only host ports 8000
  (Core) and 3000 (web) are published, and Core's internal 8100 stays private.
- `up` starts Postgres, Core and the web app, and never the worker. The seed runs
  every worker visit itself, so approved-and-queued and in-progress handoffs stay put.
- `seed` provisions a synthetic owner (Avery Chen at Northwind) with two workspaces,
  A and B, and one undecided approval for each review step that uses one up
  (`spare:<row>` in `urls`). Pending approvals stay pending for 900 seconds; when one
  lapses, run `reset`, `up` and `seed` again.
- `reset` removes this project's containers, volumes and network only.

Two scripted steps reach states that only exist while something is down:

```
tests/harness/prompt14.sh current-omitted /ABSOLUTE/PRIVATE/stack.env /ABSOLUTE/PROOF_DIR
tests/harness/prompt14.sh current-restored /ABSOLUTE/PRIVATE/stack.env /ABSOLUTE/PROOF_DIR
tests/harness/prompt14.sh core-stop /ABSOLUTE/PRIVATE/stack.env /ABSOLUTE/PROOF_DIR
tests/harness/prompt14.sh core-start /ABSOLUTE/PRIVATE/stack.env /ABSOLUTE/PROOF_DIR
```

`current-omitted` restarts this project's Core without the Current module, so Leads
shows both Current branches as temporarily unavailable. `core-stop` stops this
project's Core, so the shell shows an approval as temporarily unavailable.

## The review browser

The seeded owner has no sign-in provider. `browser-start` opens Google Chrome with a
new throwaway profile and gives it one ordinary session for the seeded owner:

```
tests/harness/prompt14.sh browser-start /ABSOLUTE/PRIVATE/stack.env /ABSOLUTE/PRIVATE/browser [--headless]
tests/harness/prompt14.sh browser-stop /ABSOLUTE/PRIVATE/browser
```

- Core's own `create_session` and `mint_host_secret` mint the session inside the
  project's Core container. The secret goes through a pipe straight into
  `tests/harness/audit_browser.mjs`, which installs it as the normal HttpOnly,
  SameSite=Lax `rheo_session` cookie for `http://localhost` through Chrome's DevTools
  cookie API. It is never printed, logged, written to a file, or put in a URL or a
  command line.
- The profile lives in a new temporary directory from `browser-start` until
  `browser-stop`, which closes Chrome and deletes the profile. If `browser-start`
  fails, it removes the profile itself. The browser directory holds only the Chrome
  process id, the profile path and the DevTools port.
- The reviewer uses that window normally: open any URL from `urls`, and use the
  ordinary workspace switcher to move between workspaces A and B.

No route, sign-in path, grant or permission is added for any of this.
