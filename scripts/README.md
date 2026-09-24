# Repository tooling

`python3 scripts/check_routing_literals.py` fails on a hard-coded route literal
(`http(s)://`, `/auth/`, `/api/`, `/mcp`) anywhere under `apps/web/src`, every
`modules/*/web/src`, `packages/web-contract/src`, or `apps/core/src` outside
`apps/web/src/lib/routing/`, `apps/web/src/generated/` and the two FastAPI route
modules that serve those paths — every link must go through `url_for`/`urlFor`
instead. Self-tests itself on every run (plants literals in a scratch repository tree
whose roots it finds through the same resolver as the real scan) before scanning the
real tree.

`python3 scripts/check_web_platform.py` fails on a platform-only `@vercel/*` import
or an edge-runtime declaration anywhere under `apps/web`, every `modules/*/web`, or
`packages/web-contract`, outside generated build output. Self-tests itself on every
run, over a scratch repository tree, before scanning the real tree.

`python3 scripts/check_legacy_names.py` fails on a predecessor-product name, with or
without a plural or version suffix (see the script's own docstring for the exact
denylist), or a generalized source-product-prefix compound identifier, in any tracked
path or in tracked text outside an explicit historical-docs exception list.
Self-tests itself on every run before scanning the real tree.

`python3 scripts/check_fixture_provenance.py` fails when a tracked file under a
`fixtures/` path segment has no entry in `tests/fixtures/provenance.json`, when an
entry names a file that no longer exists or an `origin` other than `"synthetic"`, or
when fixture/test/web-source content looks like a non-reserved email domain, a
non-documentation IPv4 literal, a phone number, or an hourly rate. An optional local
`RHEO_PRIVATE_DENYLIST` environment variable can point at a file outside the
repository whose lines are also checked for, case-insensitively. Self-tests itself on
every run before scanning the real tree.

`python3 scripts/check_theme_tokens.py` fails on a hard-coded color in a color
position (a CSS declaration, a style-shaped key or JSX attribute, a whole-string color
value, a `var()` fallback) or a length literal in CSS, a JSX `style=` attribute or an
imperative style write, an unrecognised `var(--rs-…)` reference, or any
`--rs-chrome-*` reference, anywhere under `apps/web/src` or `modules/*/web` (TS and JS
sources alike) outside test files and the two exact exempt directories
`apps/web/src/theme/themes/` and `apps/web/src/generated/`. A missing `apps/web/src`
fails it. Self-tests itself on every run before scanning the real tree.

`python3 scripts/check_search_boundary.py` fails on a search input — a `search`
`type`, `inputMode`, `enterKeyHint` or `role` in any JSX quoting, the `<search>`
element, a `createElement` call making one, or a props object in a JSX file carrying
one — anywhere under `apps/web/src`, `modules/*/web`, or `packages/web-contract`, outside
Recallatron's own search screen (`modules/recallatron/web/src/screens/search/`). Type
declarations are not props. A missing `apps/web/src` fails it. Runs in both `make lint`
and `make check`. Self-tests itself on every run before scanning the real tree.

`python3 scripts/check_workspace_scripts.py` fails when a web workspace package under
`apps/*`, `packages/*` or `modules/*/web` does not declare `lint`, `typecheck` and
`test` scripts, or is not listed in `pnpm-workspace.yaml` — `pnpm -r` skips either
silently. Runs in `make check`. Self-tests itself on every run before checking the real
tree.

Each gate is a standalone, stdlib-only `python3` script with no import from
anywhere else in the repository, so it runs from a bare checkout before any
dependency is installed. That is why the quote-aware `_strip_ts_comments` helper is
copied into each script that needs it rather than shared: the copies are deliberate,
and a fix to one belongs in all of them.

Run `python3 scripts/check_repository.py` from any directory in a checkout.
Git and Python 3.9 or newer are the only dependencies.

The check verifies that private runtime and local agent-instruction paths are
ignored, public source/example paths remain trackable, no ignored artifacts are
already tracked, and local inline
Markdown links resolve to tracked files or directories. Stage newly added public
files before running it. It does not scan file contents for secrets, validate
external URLs or Markdown anchors, or inspect release image/package contents.

Content review remains necessary before publishing. Future application tooling
must use synthetic isolated data and the agreed private-storage boundary.
