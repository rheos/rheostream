# ADR-0021: Float runs as a rheoStream module in the main workspace; the standalone server runtime is dropped
Status: accepted
Date: 2026-11-10

## Context

ADR-0017 put the runtime on its own private Linux server: a foreground worker, a
dedicated PostgreSQL 17 service, an immutable private image and a release workflow
that fired from `main`. That path was specified and partly built, and nothing was ever
provisioned or released. It is a second deployment system to maintain for one
person's ledger. Float was always meant to sit beside the other modules in rheoStream,
which already has workspaces, a worker, scheduling, audit and its own deploy.

## Decision

Three owner decisions, recorded 2026-11-10.

Decision 42.F2: "Build it as a rheoStream module." Float becomes a rheoStream module.
There is no separate server runtime for it.

Decision 43.F1: "Live rheoStream, own space (Recommended)." Float runs on the live rheoStream. This
was first read as a separate finance workspace.

Decision 43.F2, the correction: "what do you mean it's own workspace? it will be part
of my current workspace". Float is a module in the existing main workspace, beside
Current, Leads, Recallatron and Relationships, on the live rheoStream, under
rheoStream's own deploy. It is not a dedicated workspace. The module must not be
reachable through existing connector grants until the owner opts in; how that is enforced
is decided in the module plan.

This supersedes ADR-0017. The standalone release path is removed from the repository:
the deploy workflow, the release and canary controllers, the remote CLI wrapper, the
server Compose files, the Dockerfile and its build-context allowlist, and the tests of
those files. Git history keeps them. Local standalone use stays for development and
tests: the CLI, `finance jobs serve`, local Compose and the existing tests. The
module work itself is planned and built separately, under rheoStream's own deploy.

ADR-0017's protection of ledger data (kept out of the public repo, out of image build
contexts and out of iCloud) and its role, approval and identity boundaries carry over
unchanged. ADR-0007's residency mechanics were already superseded by ADR-0017 and are
not revived.

## Consequences

No server or registry package was ever created. Two release-era credentials do exist
and no workflow uses either now: a repository secret and a read-only deploy key on the
parser repository, both named in the owner's private records. Whether to revoke them or
keep them for the module build is an open owner decision, to be recorded here when
made. The only CI left in this repo is the public job (ruff, `repo_check`,
compile); the full suite runs locally through the regression runner until the module
brings its own checks. Residency, database and role setup, and how the
module reaches the data are decided in the module's own ADRs. Until the module lands,
no deployed instance of Float exists, and no first-live-data or sink gate has been
cleared by this record.
