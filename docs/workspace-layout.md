# Local workspace layout

Open the parent folder in your editor so public code and private working material
are available together. The public repository is one child of that folder.

```text
rheo-stream-workspace/                 No .git directory here
  README.md                           Local orientation
  AGENTS.md                           Local Codex entry instructions
  CLAUDE.md                           Local Claude Code entry instructions
  .gitignore                          Ignore everything if Git is initialized here by mistake
  .dockerignore                       Exclude everything from an accidental parent build context
  workspace.paths.json                Local path map; no credentials
  rheo-stream/                        Public checkout; owns its own .git directory
    AGENTS.md                         Ignored local entry file, for sessions started here
    CLAUDE.md                         Ignored local entry file, for sessions started here
  private/
    config/                           Private deployment settings and overrides
    documents/                        Source documents and working material
    agent-context/                    Internal build procedures and current session handoff
    modules/                          Private extension source, explicitly registered when supported
  workspaces/                         Private databases, uploads, indexes, logs, and exports
  backups/                            Private backup destinations
```

The parent files and private directories are local to each installation. They are
not part of this repository. Use a fresh parent outside any existing Git repository;
clone or move the public checkout into it without copying any legacy Git history.
Create private directories with owner-only permissions, or equivalent service-account
permissions where collaboration is intended. The layout itself creates no real client
data or credentials.

From the parent, scope commands explicitly:

```sh
git -C rheo-stream status
python3 rheo-stream/scripts/check_repository.py
```

Use `rheo-stream/` as the context for container builds and package publishing.
Do not publish the parent as an archive or initialize Git there. A parent ignore-all
file is only a guard against accidental staging; it can be bypassed. Private siblings
are excluded from the child repository by location, not by the child's `.gitignore`.

## Connecting the locations

Keep a local path map at the parent, with relative paths resolved against the file's
directory. For example:

```json
{
  "public_repository": "./rheo-stream",
  "private_configuration": "./private/config",
  "private_documents": "./private/documents",
  "private_agent_context": "./private/agent-context",
  "private_modules": "./private/modules",
  "runtime_workspaces": "./workspaces",
  "backups": "./backups"
}
```

This is an operator path map, not a versioned runtime API, and the application does
not read it. The application uses one data root instead: the directory
`RHEO_DATA_ROOT` names, or a container or platform default. Its layout is fixed:
`config/deployment.toml` for private deployment settings, `secrets/` for file secrets,
`workspaces/<workspace id>/` for each workspace's uploads, exports and runs, `logs/` and
`models/` (`packages/core/src/rheo_core/storage/data_root.py`). Startup refuses a data
root inside the source checkout (except `.rheo-local/`, and only when `RHEO_DATA_ROOT`
names it) and a symlink whose target escapes its parent, so a data root under the
parent's `workspaces/` works. Modules get a directory only by asking for a purpose
(uploads, exports, scratch); they cannot name a path. Do not make the source tree
writable to store personalization. An incoming event, model argument, or tenant setting
cannot select a host directory. Credentials belong in the selected secret store, not in
this map.

The editor workspace root is a filesystem convenience. It is not an authenticated
rheoStream workspace and does not replace workspace or member authorization. One data
root holds a directory for each application workspace (`workspaces/<workspace id>/`),
managed by the host. Avoid deriving authorization from folder names.

Private modules can use the same contracts as public modules, but their presence
on disk must not cause automatic discovery or execution. Core loads only the module ids
named in the deployment's `modules.installed` setting, found through the `rheo.modules`
entry-point group, and a workspace still has to install and enable each one.
Production packaging must deliberately include authorized private extensions without
publishing their source or bundling their configuration and customer data into public
artifacts.

## Private instructions and new sessions

Keep internal build procedures and the current handoff under
`private/agent-context/`. Put small local entry files named `AGENTS.md` and
`CLAUDE.md` at the parent workspace root and, when starting sessions inside the
checkout, at the checkout root too. The repository ignores those names, their
local/override variants, and local `.codex/`, `.claude/`, and `.mcp.json` configuration.
General public contributor guidance lives in [CONTRIBUTING.md](../CONTRIBUTING.md).

Each `AGENTS.md` entry should tell the agent to read the shared private build notes
and handoff, then the public idea and contribution guide. Resolve its paths from
the entry file's directory. Each `CLAUDE.md` can import the adjacent entry with:

```text
@AGENTS.md
```

This imports the small local entry; the entry then instructs the agent to read the
private context files. No symlinks or public links to private files are needed.
[Codex discovers project instructions from its project root toward the working directory](https://learn.chatgpt.com/docs/agent-configuration/agents-md#how-codex-discovers-guidance),
so a file above the Git root is insufficient for a session launched in the checkout.
[Claude Code reads `CLAUDE.md` and supports imports](https://code.claude.com/docs/en/memory).
Keep entry files small and name the context files explicitly; the full conversation
is not automatically carried into a new session.

The handoff should record the current branch/PR, completed decisions, outstanding
questions, checks performed, and next useful work. Refresh it at meaningful stopping
points and verify live Git/PR state when resuming. These files are guidance, not
access controls or authorization to perform actions beyond the user's request.

Fresh clones, separate worktrees, and cloud sessions do not receive ignored local
entry files or private sibling context from Git. Provision that context explicitly
before relying on continuity there. Local rules in Git's `info/exclude` can also
protect entry files on branches that predate the shared ignore rules; ignore rules
still do not protect a file already tracked on an older branch.

## Working boundaries

- Keep private content out of public issues, PRs, fixtures, logs, and build assets.
  Editor visibility and local paths do not authorize disclosure to external tools.
- Do not add public-tree symlinks to the private siblings. Use explicit configuration
  references and trusted storage access instead.
- Keep test inputs synthetic. Test jobs must not discover and load a developer's
  local path map or live data automatically.
- Scope indexing, file watching, and tooling to the directories they need. The
  directory arrangement is not a sandbox for an agent or process with filesystem access.
- Backups remain private and need their own access, retention, and restore policy.
  Git does not back up these sibling directories, and this layout schedules no backups.
  The flagship's nightly backups are described in the [deployment guide](../deploy/README.md#backups).

Installed applications may use an OS application-data directory instead. Hosted
deployments use operator-selected private databases and file/object storage; they
do not need this editor folder structure. All modes preserve the same
[private-data boundary](ideas/rheo-stream-idea.md#private-data-placement-in-each-deployment-mode).
