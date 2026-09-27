# Repository tooling

`python3 scripts/check_routing_literals.py` fails on a hard-coded route literal
(`http(s)://`, `/auth/`, `/api/`, `/mcp`) anywhere under `apps/web/src`, every
`modules/*/web/src`, `packages/web-contract/src`, `packages/web-contract/theme`, or
`apps/core/src` outside `apps/web/src/lib/routing/`, `apps/web/src/generated/` and
the two FastAPI route modules that serve those paths — every link must go through
`url_for`/`urlFor` instead. Self-tests itself on every run (plants literals in a
scratch repository tree whose roots it finds through the same resolver as the real
scan) before scanning the real tree.

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
value, a `var()` fallback, a gradient's arguments in any property) or a length literal
in CSS, an inline style (a JSX `style=` attribute, a `style:` props key behind a spread,
a `createElement(…, { style: … })` call) or an imperative style write, an unrecognised
`var(--rs-…)` reference, or any
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

Known limit: the scan reads literals, not values, so it cannot see a search input
whose value is only known at runtime. It misses a props object written in a `.ts`
file and spread into JSX elsewhere, a conditional such as
`role={cond ? "search" : undefined}`, and a constant such as `type={SEARCH_TYPE}`.
Every literal form the spec names is caught. The runtime backstop for the shell is
`apps/web/src/shell/ShellFrame.test.tsx`, whose "renders no search input when …" cases
render the frame in each account state and assert the output has no
`type="search"`/`role="search"`.

`python3 scripts/check_workspace_scripts.py` fails when a web workspace package under
`apps/*`, `packages/*` or `modules/*/web` does not declare `lint`, `typecheck` and
`test` scripts, or is not listed in `pnpm-workspace.yaml` — `pnpm -r` skips either
silently. Runs in `make check`. Self-tests itself on every run before checking the real
tree.

`python3 scripts/check_module_web_boundary.py` fails when a module web package can
reach core without the `ShellApi` its screens are handed (#183). It follows every
`modules/*/web` package's entry points through their imports, into
`packages/web-contract` too, and refuses any import other than a relative file inside
the package, `react`, or a web-contract export. That shuts out the `@/` alias into
apps/web, `next/headers`, `next/server`, Node built-ins and every other package. It
also refuses a free `fetch` or other network API, `process`, `globalThis`, `eval`,
`Function`, `document`, `import.meta`, a non-member use of a global-object name
(`window`, `self`, `global`, `top`, `parent`, `frames`, `opener`, spreads included),
the `.cookie`, `.constructor`, `.__proto__`, `.prototype`, `.defaultView` and
`.ownerDocument` members (and those names as string keys), `with`, string timers,
`<script>`/`<iframe>`/`<embed>`, `dangerouslySetInnerHTML`, `javascript:` URLs, a
non-literal dynamic `import()`, `require(`, and `"use server"`. Every target of a
conditional web-contract export is followed. Test files the entry
points never import are out of scope. It fails when no module web package exists, or
when one has no `package.json` or entry point. Runs in `make check`. Self-tests itself
on every run before scanning the real tree. Known limits: it reads source text, so a
name assembled at runtime and passed through an allowed object is out of its sight, and
markup that makes the browser send a cookie-bearing request (`<img src>`,
`<form action>`) is left to the routing-literal gate.

`python3 scripts/check_migration_outputs.py` fails when a tracked path's basename
matches a migration-output filename pattern (a harvested ledger, an entity-pair
extract, a comparison, a forecast, a measurement, or one of a handful of exact
hand-written report names) — the path gate, always active and the only part CI
runs (`make migration-output-gate`). An optional local `RHEO_PRIVATE_DENYLIST`
environment variable, naming a file outside the repository, additionally enables a
case-insensitive, whitespace-normalized leak scan of tracked content in one of five
modes selected by CLI flag (`--diff <range>`, `--commits <range>`, `--text
<file>...`, `--triage-exempt <ref> --out <dir>`, or no flag for tracked text at
`HEAD`); CI never sets this variable. On a match, only
`<path or commit sha>:<line number>: denylist line <n>` is printed — never the
matched text or the denylist line's own content. Self-tests itself on every run
before scanning the real tree.

Each gate is a standalone, stdlib-only `python3` script with no import from
anywhere else in the repository, so it runs from a bare checkout before any
dependency is installed — the one exception is `check_migration_outputs.py`'s
`--triage-exempt` mode, which lazily imports
`rheo_recallatron.migration.private_paths` (only when that mode is invoked, always
through the project's own virtualenv via `uv run`; the path gate CI runs needs no
import at all). That is why the quote-aware `_strip_ts_comments` helper, and
the `_in_type_body` helper the search and theme gates share, are copied into each
script that needs them rather than shared: the copies are deliberate, and a fix to one
belongs in all of them.

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
