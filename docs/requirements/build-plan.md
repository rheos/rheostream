# rheoStream — phased build plan

**Status:** Accepted. Ratified by the maintainer's review and merge of the pull request that
carried these documents (PR #5, 2026-09-09). Phases one to three are planned to implementable
depth; later phases are sketches that will be planned when their predecessor lands. R1 to R5 in
the companion requirements document are ratified by that merge.
**Companion:** [Requirements and scope](requirements-and-scope.md), which carries the decisions,
functional requirements (cited as FR *n*), and the release-one scope boundary.
**Decision citations:** D *n* is a settled decision and R *n* is a decision taken on the
maintainer's behalf. Both are defined in the companion requirements document: D *n* under
[Settled decisions](requirements-and-scope.md#settled-decisions), R *n* under
[Questions resolved here](requirements-and-scope.md#questions-resolved-here). No criterion in
this plan rests on a decision recorded anywhere else.
**Source of acceptance criteria:** the architecture acceptance-scenario table in the
[idea document](../ideas/rheo-stream-idea.md#architecture-acceptance-scenarios). Every criterion
below names the scenario it comes from.

Acceptance criteria are numbered continuously across the whole plan and are checkable by
inspection or by a test. None is a judgement about quality. A
[functional-requirement coverage table](#functional-requirement-coverage) at the end of this
document maps every FR from 1 to 53 to the criteria that test it.

Release one is phases one to three. Phases four onward follow it.

---

## Phase 1 — Walking skeleton

**Goal.** A running application with nothing in it: a workspace exists, its storage is
provisioned and versioned, background work survives a crash, an agent can reach the system
through MCP, a person can log in, and a fresh clone runs a synthetic demo. No domain module
ships in this phase.

**In scope**

- Repository bootstrap: the Python core service, the Next.js application shell, the container
  stack with a Postgres container, the development harness, and the publication checks wired
  into continuous integration. This phase owns the application shell; phase two unifies
  navigation and theme on top of it.
- Workspace and storage foundation: the control plane (accounts, workspace registry), the
  per-workspace database provisioning path, migration tracking per module, and the storage
  adapter seam.
- Identity: the identity-provider boundary with GitHub OAuth behind it, plus locally
  issued tokens for command-line and MCP clients, issued from an authenticated web session or by
  an operator-level command on a headless install.
- The operator-level command that adds a second member to a workspace against the control plane.
  R1 makes this the only way a second member exists in release one, and criterion 8 needs a
  `member` session to test the role check against.
- Three test fixtures, all test-only and none a shipped capability. Each is registered by the
  test harness under the test profile only, and criterion 18 asserts that the production
  registration set carries none of them:
  - A second identity-provider implementation that is not an OAuth provider, existing purely as
    a test double so that criterion 8 can substitute it behind the boundary. D6 ships exactly
    one real provider in release one, and this fixture does not become a second one.
  - A deduplicating consumer that records processed event identifiers, so criterion 11 has
    something to deliver to.
  - A confirmable fixture operation registered in the destructive safety class, which records
    the request it received and deletes nothing. It is the one operation above the read class
    in this phase, so criterion 19's approval-required state and criterion 21's restored pending
    approval are exercised against a real confirmation requirement rather than an empty set.
- Durable work: the transactional outbox, the worker with leases, bounded retries and
  cancellation, operation records with terminal status, and the audit record.
- The approval and confirmation records from R3: the approval binding tuple (actor, workspace,
  operation, recipient or destination, payload digest, purpose, execution window), the durable
  approval record carrying the payload digest, standing grants limited to the read, draft, and
  mutate classes and scoped to a workspace and a named operation set, the recheck of
  permissions and contact restrictions at execution time, and the approval-required state
  returned to a headless or channel-mediated caller.
- The MCP façade with the safety-class registration check and a small set of read-class tools
  sufficient to prove the boundary.
- `ClaudeCliRuntime` behind the runtime adapter contract, with capability discovery.
- Workspace export and restore.
- Routing configuration, through which every link the interface produces is generated.

**Out of scope**

- Any domain module. There are no leads, no work items, and no memories in this phase.
- The second runtime adapter, channels, and voice.
- Module disable, remove, purge, and restore (D5).
- Any interface beyond the shell, a login, and a workspace switcher.

**Acceptance criteria**

1. A clone of the public repository, with no private configuration and no credentials, builds
   and starts the synthetic demo, and the demo completes without reading any file outside the
   checkout and the synthetic fixture set. *(Scenario: Fresh public clone; FR 14.)*
2. Running the demo and normal startup writes no file inside the tracked source tree; `git
   status` is clean after a full run. *(Scenario: Local private workspace; FR 10, FR 14.)*
3. With the checkout-local fallback directory explicitly selected, every configuration file,
   upload, database, database sidecar, export, and log it produces is matched by the repository
   ignore rules, verified by a test that creates one of each and asserts it is ignored.
   *(Scenario: Checkout-local fallback; FR 10.)*
4. `python3 scripts/check_repository.py` exits zero in continuous integration on every branch,
   and the container build context excludes the private sibling directories by construction.
   *(Scenario: Publication review.)*
5. Each workspace's records are in a distinct Postgres database or schema, verified by a test
   that creates two workspaces, writes to one, and asserts the other's storage is unchanged.
   *(Scenario: Hosted workspace data; FR 7.)*
6. No code path accepts a database path, connection string, schema name, workspace identifier, or
   actor identity from a request body, query parameter, model argument, or workspace setting. A
   test supplies each of those and asserts the value is ignored and the authenticated context is
   used. *(Scenario: Wrong workspace or channel audience; FR 2, FR 23.)*
7. A request carrying a valid record or operation identifier belonging to another workspace is
   refused by the HTTP API, the MCP façade, and the job dispatcher alike. *(Scenario: Wrong
   workspace or channel audience; FR 1.)*
8. A person signs in through GitHub OAuth and the resulting session carries an
   actor, a workspace, and a role. The provider is reached only through the identity-provider
   boundary, verified two ways: the second identity-provider implementation, which is a test
   double and not a shipped provider, is substituted behind the same boundary and leaves the
   domain service suite passing unchanged, and a check asserts no domain module imports the
   provider package. A second member added by the operator-level command receives a `member`
   session, and an operation restricted to `owner` is refused to it. *(Scenario: Hosted workspace
   data; FR 3, FR 5.)*
9. A locally issued command-line or MCP token names exactly one actor, one workspace, and one
   permitted operation set. It is issued either from an already-authenticated web session or by
   the operator-level command against the control plane, and presenting it never requires a
   browser flow. A test presents a malformed token, an expired token, and a valid token whose
   operation set excludes the requested operation, and asserts each is refused with a distinct
   non-success state and leaves no partial effect. *(Scenario: Headless approval or failure;
   FR 4.)*
10. Every workspace row records the installed core version, each installed module's version, and
    each module's schema version, readable through a supported operation rather than by querying
    a migration table directly. No module exists in this phase, so the two module clauses pass
    on an empty set here; criterion 24 re-asserts them with the memory module installed, and
    only that re-assertion counts as their test. *(Scenario: Module reinstalled or upgraded;
    FR 9.)*
11. A state change and its outgoing event commit in one transaction. A test that kills the
    process between commit and delivery, then restarts, observes the event delivered exactly once
    to the deduplicating test consumer, which records processed identifiers. *(Scenario: Crash
    during handoff; FR 15.)*
12. A worker killed mid-task releases its lease and the task is retried within its bounded retry
    budget; a task exhausting its budget appears in a failure list with its error, and is not
    silently dropped. A queued task that is cancelled never runs, and a running task that is
    cancelled reaches a terminal cancelled status rather than a success or a further retry,
    verified by a test that cancels one of each and asserts the recorded terminal status and the
    absence of any later effect. *(Scenario: Crash during handoff; FR 16.)*
13. Every long-running operation returns an operation identifier before it completes, and that
    operation reports one of a fixed set of terminal statuses, including an explicit unresolved
    status. A test asserts no operation can report success without a recorded terminal check.
    *(Scenario: Crash during handoff; FR 17.)*
14. Every mutating operation writes an audit record naming actor, workspace, operation, and time,
    readable through a supported operation rather than by querying a table directly. A mutating
    operation registered without an audit path fails registration at startup with a named
    operation, so the record cannot be skipped by omission. A test performs one mutation of each
    registered kind and asserts a matching audit record exists for every one. *(Scenario: Crash
    during handoff; FR 18.)*
15. A workflow that declares a requirement the configured runtime cannot satisfy (structured
    output, streaming, or continuation) is rejected before the runtime is invoked, with a named
    unmet capability. *(Scenario: Runtime capability mismatch; FR 19, FR 20.)*
16. A headless run whose executable is missing, whose credential is expired, whose tool is
    denied, whose deadline elapses before the runtime answers, or whose output stream truncates
    returns a distinct non-success state within the configured deadline. A test induces each of
    the five, the timeout by a runtime double that never answers, and asserts no hang and no
    success report. *(Scenario: Headless approval or failure; FR 21.)*
17. A secret is held by reference and resolves only inside the component that presents it. A
    test configures a model credential and the OAuth client secret as references, drives one
    runtime request that needs the model credential and one sign-in that needs the client
    secret, and asserts that the runtime request as the adapter records it, the arguments of
    every tool call made during the run, the operation record, and the audit record contain
    neither a secret value nor a reference that resolves to one; a second check asserts the
    resolved value is never passed to a domain service. *(Scenario: Hosted workspace data, for
    its requirement that runtime configuration stay access-controlled and out of every artifact
    that does not need it; FR 13, guardrail 14.)*
18. Every registered MCP tool and every registered service operation declares exactly one safety
    class, and either one registered without a class fails registration at startup, naming what
    was registered. A test registers one tool and one service operation with no declared
    class and asserts startup fails naming each. A second check starts the application under the
    production profile and asserts that its registration set contains no tool, operation,
    consumer, or identity provider registered by the test harness and no operation in the
    external-side-effect or financial class; the check runs in continuous integration from this
    phase onward. *(Scenario: Headless approval or failure; FR 24.)*
19. A headless or channel-mediated request that reaches a confirmation requirement receives an
    explicit approval-required state and nothing else happens. A test invokes the
    destructive-class fixture operation through an MCP token session and asserts that the
    response is an approval-required state carrying an approval identifier, distinct from
    success and from failure, returned within the configured deadline; that the fixture recorded
    no request; and that no operation record reports success. The test then approves through the approval
    operation as an authenticated actor and asserts the fixture executes once and a durable
    approval record exists carrying the payload digest, readable through a supported operation;
    and it requests a standing grant naming the destructive class and asserts the grant is
    refused at grant time. *(Scenario: Headless approval or failure; FR 24, R3 items 5, 6, and
    7.)*
20. Every tool the MCP façade exposes is namespaced and goal-level and reaches storage only by
    calling an application service. A test enumerates the registered tools and asserts that none
    accepts SQL, a table name, or a query fragment as an argument, and a check asserts that no
    file in the MCP façade imports a database driver or a repository directly. *(Scenario: Wrong
    workspace or channel audience; FR 22, guardrail 5.)*
21. A workspace export produces an artifact that a restore reads back into an empty deployment,
    after which the restored workspace's composition, configuration versions, module schema
    versions, and records match the original, verified by comparison rather than by inspection.
    A pending approved action is restored in a state that requires a fresh approval before it can
    execute: the test approves the destructive-class fixture operation, exports before it runs,
    restores, and asserts the restored action is refused until approved again and that the
    fixture recorded nothing in between. *(Scenario: Export and restore; FR 52.)*
22. Every link the web interface produces is generated through routing configuration, with no
    route string hard-coded to one topology. This phase's surface is the application shell, the
    login, the workspace switcher, the post-login redirect, and the OAuth callback URL, and the
    last two are topology-sensitive, so they are in scope here rather than later. In subdomain
    mode that surface spans the shell host and the identity host the companion document names
    under its recorded changes of direction, and the callback resolves to the identity host in
    subdomain mode and to a path on the single origin in single-host path mode, from the same
    routing configuration. The interface test suite covering that surface passes in
    single-host path mode and in subdomain mode with no code change between the two runs, and
    both runs are a continuous-integration gate from this phase onward, over whatever interface
    surface exists when it runs. *(Scenario: Fresh public clone; FR 47.)*
23. The web interface builds and runs in the single-server container deployment with no
    hosting-platform-specific feature in its dependency set, enforced by a build that fails on a
    platform-only import or configuration key. That build is a continuous-integration gate from
    this phase onward, over whatever interface surface exists when it runs. *(Scenario: Fresh
    public clone; FR 48, guardrail 15.)*
69. Configuration resolves from exactly three sources in a fixed precedence — package
    defaults, then deployment settings, then workspace and member overrides — and a key that no
    settings schema declares is refused at every source. For a key the operator's policy floor
    governs, a workspace override that would relax the deployment value is refused at write time
    naming the key, and an override already stored is clamped at read time from the moment the
    deployment value tightens. A test sets one key at each source and asserts the resolved value
    follows the precedence; writes a floored key looser than the deployment value and asserts the
    refusal names the key; and tightens the deployment value beneath an existing looser row and
    asserts the resolved value is the deployment value while the row is left in place.
    *(Scenario: Hosted workspace data; FR 12, guardrail 25.)*

Criterion numbering is append-only: a criterion added after this plan was ratified takes the
next unused number across the whole document, which is why 69 sits in phase one after 23, and
nothing is renumbered, so every existing reference to a criterion number stays valid.

---

## Phase 2 — The memory module

**Goal.** Recallatron ships as the first real module: installed and enabled through the module
registration contract, storing and retrieving durable memory under workspace and record
permissions, with its retrieval layer rebuilt on Postgres and its data migrated out of the
predecessor. This phase proves module contract v1 on the smallest domain that can carry it (D11).

**In scope**

- The module manifest and the install and enable path, implementing the subset of the lifecycle
  contract that D5 places in release one.
- Release-one phase two supplies typed memory records with immutable provenance, entity
  identity, memory-to-entity mentions, memory-to-authoritative-record links,
  correction/supersession, readable non-erasure history and conservative ambient automatic
  creation from eligible explicit-statement evidence. It does not yet supply generic entity
  attributes, entity-to-entity relations, traversal, confirmation, merge, profile synthesis
  or a conversation archive. Those omissions are release-one scope boundaries; any permanent
  retirement or later mapping decision is recorded in the decision ledger and the named
  planning carrier before migration accepts data loss.
- The retrieval adapter, with the release-one implementation on pgvector for dense retrieval and
  `tsvector` or `pg_trgm` for lexical retrieval, or a hybrid of the two (D9).
- Permission-enforced retrieval, including inheritance of source access and purpose restrictions
  by derived memories.
- Conservative ambient automatic memory from eligible explicitly stated Rheo runtime evidence
  and an enrolled local Claude Code CLI/Desktop Code bridge, delivered after retrieval and
  before migration/cutover, with trusted receipts and durable
  sanitation/digestion/extraction; normal eligible units require neither a remember command
  nor per-note review.
- Retention policy per workspace.
- Migration and cutover from the predecessor's memory store: extraction, re-embedding under the
  new retrieval layer, verification, and switchover. The migration runs against the maintainer's
  real memories, so its verification report is written to private workspace storage alongside
  that data and is never committed to this repository (FR 53). The switchover itself is a step
  the maintainer performs deliberately once the report is satisfactory; the migration job never
  switches over on its own.
- Navigation and theme unification on the phase-one application shell: one token set, one theme,
  and one navigation contract that modules contribute to, plus the memory browse, search, and
  entity screens ported from the predecessor. *(Amended 2026-09-24 — issue #40: "one theme"
  becomes a themable contract. The one token set is the versioned
  [theme token contract](../architecture/theme-contract.md); a theme is a data file of values for
  it, whose confirmation-chrome subset only built-in themes set; this phase ships one seed
  built-in theme on it, which a later UI phase replaces or extends. See architecture decision
  A18.)*
- The naming and fixture-provenance checks, wired as gates in continuous integration. They bind
  from this phase onward because this phase carries the first ported interface code, which is
  where a legacy name, a hard-coded API path, or real data would enter the public repository. The
  routing-configuration and platform-dependency gates already bind from phase one and take this
  phase's ported surface into their scope.

**Out of scope**

- Module disable, remove, purge, and restore.
- Cross-module memory links to Leads records beyond the reference type, since Leads does not
  exist yet. The link type is defined here and exercised in phase three.

The architecture corrections these entries rest on are the accepted public ones in
[memory](../architecture/memory.md), [deletion, export and
migration](../architecture/deletion-export-migration.md) and [runtime and
MCP](../architecture/runtime-and-mcp.md). Architecture decision A0's named advanced-feature
deferrals are retained. Public criterion numbers are unchanged; ambient evidence is supplemental
until a separately ratified append-only criterion amendment says otherwise.

**Acceptance criteria**

24. The memory module is installed and enabled entirely through the registration contract: no
    core file changes to add it, verified by a test that enables it in a fresh workspace and
    asserts its records, tools, migrations, and interface contributions all appear, and that
    the workspace row now records the module's version and its schema version through the
    operation criterion 10 names. *(Scenario: A new specialist module; FR 26, FR 9.)*
25. The module's manifest declares its owned record types, its storage destination, its
    migrations, its configuration schema, its provided tools with their safety classes, and its
    export format. A manifest missing any of these fails validation at install with a named
    missing field. *(Scenario: A new specialist module; FR 52.)*
26. The module writes only to its own storage. A test asserts that no memory-module code path
    holds a handle to another module's tables, that the core's storage API is the only route to a
    connection, and that a cross-module change is effected only through a public operation or
    event. *(Scenario: A new specialist module; FR 11, guardrail 6.)*
27. A retrieval call by an actor without permission on a source record returns no content
    derived from that record, verified by a test that stores a memory under one permission
    scope and queries it under another. *(Scenario: Wrong workspace or channel audience; FR 27.)*
28. A memory derived from two sources carries the intersection of their audiences, not the
    union. A test combines a broadly readable source with a restricted one and asserts the
    result is restricted. *(Scenario: Wrong workspace or channel audience; FR 27.)*
29. Deleting or superseding a source record invalidates its derived summaries and its
    embeddings in the same operation, verified by querying the retrieval index afterward.
    *(Scenario: Permissions or contact purpose withdrawn; FR 28.)*
30. Every workspace has explicit memory retention settings rather than an inherited posture:
    both are written as rows when the module is enabled, so what a workspace does about age is
    always a stored, readable choice. The package default for the age-expiry gate is off, so
    nothing is deleted for age alone unless a workspace states that it wants that, and the
    retention window is a bounded positive number of days, read only while the gate is on. A
    missing or out-of-range window is refused rather than replaced by a default, and the window
    carries no indefinite sentinel value: unbounded retention is the gate's off state.
    *(Scenario: Permissions or contact purpose withdrawn; FR 29. Amended 2026-09-21 — see the
    recorded change of direction in
    [memory](../architecture/memory.md#retention-fr-29-criterion-30).)*
31. The retrieval strategy is selected through the adapter, and a test runs the module's full
    behavioural suite against both a dense-only and a lexical-only configuration, both passing.
    *(Scenario: Runtime choice, applied to retrieval rather than to a model runtime; FR 30.)*
32. The migration from the predecessor's memory store is verified before switchover by a
    count, identity, index and access check: every source record has a valid destination under the
    approved mapping; original identity, timestamps, lifecycle/status and available provenance
    are retained; a second import creates no duplicates and does not resurrect erased content;
    and searchable destinations are populated/indexed with intended access restrictions intact.
    Historical evidence remains distinguishable from curated memory. Planned legacy-memory
    queries, labelled expected records, old-memory relevance scores and legacy ranking agreement
    are not cutover gates. Random spot checks are optional/informal; partial or missing recall
    of preserved legacy material may be improved later. Before cutover, separately prove that
    new eligible memories are automatically saved and recalled through the intended client
    with correct access, using benign operator-chosen examples rather than an owner-authored
    benchmark. The verification result is written to private workspace storage next to the
    migrated data, and a check asserts no migration report is tracked in this repository.
    *(Scenario: Export and restore; FR 53, D9. Amended 2026-09-28 — see decision-ledger entry 16
    in the [idea document](../ideas/rheo-stream-idea.md#recorded-changes-of-direction).)*
33. Every phase-one path completes correctly in a workspace where the memory module was never
    installed and never enabled, verified by running the phase-one acceptance suite in such a
    workspace. Workspace-level disable is phase-seven work under D5, so release one proves the
    module boundary by absence rather than by disable. *(Scenario: A new specialist module, for
    the half requiring that the core carry no dependency on a module added through the public
    extension points; FR 26.)*
34. No route, package, table, MCP tool, service name, environment variable, configuration key, or
    user-facing string in the repository matches a legacy product name or a generalized
    source-product prefix. The check runs in continuous integration on every branch from this
    phase onward, and its scope includes this phase's ported surface: the navigation, the theme,
    and the memory browse, search, and entity screens. Documentation files recording historical
    migration notes are the single allowed exception, listed explicitly. *(Scenario: Publication
    review; FR 49, guardrail 1. Amended 2026-09-24 — issue #40: "the theme" is the theme token
    contract and every theme file on it, the built-in themes and the contract definition
    included, rather than one fixed theme; see
    [theme contract](../architecture/theme-contract.md).)*
35. Ported interface code contains no personal data, rate, client name, or private deployment
    detail, and every fixture in the repository is synthetic, asserted by a fixture provenance
    check that runs in continuous integration from this phase onward. *(Scenario: Publication
    review; FR 50.)*
36. Criterion 21 is re-asserted at the end of this phase against a workspace that has the memory
    module installed, enabled, and populated. The export carries the module's records through the
    export format its manifest declares (criterion 25), and after a restore into an empty
    deployment the restored workspace's composition, configuration versions, module schema
    versions, and memory records match the original by comparison. *(Scenario: Export and
    restore; FR 52.)*
37. Criteria 22 and 23 still pass with this phase's interface surface included: the unified
    navigation and theme, and the memory browse, search, and entity screens. This criterion adds
    no new check; it asserts that the two gates established in phase one run unchanged in
    continuous integration over the larger surface. *(Scenario: Fresh public clone; FR 47,
    FR 48. Amended 2026-09-24 — issue #40: "the theme" is now the theme token contract, its
    built-in theme files and the shell rendered from them, rather than one fixed theme. The same
    rules run over the larger surface; the amendment adds no new check, it only widens what the
    existing criterion-22 and criterion-23 gates scan. See
    [theme contract](../architecture/theme-contract.md).)*

**Connector sign-in (issue #287)**

These criteria come from the connector sign-in change (issue #287), which lets a hosted MCP
client such as claude.ai connect to the `mcp` surface through an OAuth sign-in instead of a
pasted token. Criteria 70-90 restate that change's acceptance criteria AC-1 to AC-21 in order,
91 restates AC-24 (the one manual check), 92 restates AC-25 and 93 restates AC-26. The FR and
edge-case numbers in parentheses are the issue's own specification numbers, not this
document's. The evidence for each is in the
[connector matrix](../acceptance/connector-matrix.md).

70. With the feature configured, an unauthenticated `POST` to the `mcp` surface answers `401`
    whose `WWW-Authenticate` header is `Bearer` with a `resource_metadata` parameter equal to
    `url_for` of the metadata document, in both subdomain and path mode; a test asserts the
    header string. (FR 1, 21)
71. With the feature configured, `GET /.well-known/oauth-protected-resource` on the `mcp` host
    answers 200 JSON whose `resource` equals the canonical connector identifier and whose
    `authorization_servers` has length 1 and equals the issuer;
    `GET /.well-known/oauth-authorization-server` answers 200 with the FR 4 document; a non-`GET`
    on either answers 405; `GET /` on the same host still answers 405 with `Allow: POST`; and any
    other path on the `mcp` host still answers 404. With the feature unconfigured both well-known
    paths answer 404. (FR 2, 21)
72. With the feature configured, a presented token that is malformed, expired or revoked still
    answers `401` with the same distinct `state` values as before this change, now also carrying
    `resource_metadata`; a valid token that lacks the operation answers the unchanged refusal.
    (FR 3)
73. The authorization-server metadata document contains every field FR 4 lists with those
    values, and contains neither `client_id_metadata_document_supported: true` nor
    `scopes_supported`. A schema test compares the field set. (FR 4, 5)
74. DCR succeeds for the allowlisted callback and returns a public client with no secret; it is
    refused `invalid_redirect_uri` for each of: a different host, a trailing-slash or path-suffix
    variant, a changed scheme, a port, a userinfo, a fragment, and an empty list; a non-object
    body is refused `invalid_client_metadata`. A registration with an allowlisted URI that
    requests an unsupported `token_endpoint_auth_method`, `grant_types` or `response_types`
    succeeds and returns the overridden values (`none`; `authorization_code` and
    `refresh_token`; `code`). (FR 6, edge case 5)
75. Registration beyond the per-source rate limit, and beyond the counted-client cap, is refused;
    a client with no grant, older than the configured age and with no sign-in in flight is
    removed by cleanup. Three cases survive cleanup: (a) a client whose grant's access token
    expired hours ago survives and its refresh still succeeds; (b) a client whose only grant is
    revoked or past its absolute end survives, is not counted toward the cap, and still lists
    with its client name; (c) a client with an authorization in flight survives. (FR 7, edge
    case 6)
76. An authorization request is refused (page, no redirect to the client) for each of: unknown
    `client_id`, `redirect_uri` not exactly equal to a registered one; and refused with a
    redirected error for: `code_challenge_method` absent or `plain`, missing `code_challenge`,
    and missing or wrong `resource` (`invalid_target`). (FR 8)
77. With no session cookie, an authorization request sends the browser through `/auth/login`
    and, after the test identity provider completes, lands on the consent screen; the GitHub
    OAuth callback URL used is the unchanged `url_for(IDENTITY, "/callback")`; a closed-signup
    unknown identity gets the existing refusal and no code is issued. (FR 9, edge case 7)
78. The consent screen shows the client name, the redirect URI's hostname, the account, the
    workspace and the operation set; it appears on a second authorization by the same client;
    Deny redirects with `access_denied` and issues nothing; an Allow `POST` with a bad `Origin`
    is refused `origin_not_allowed`. (FR 10)
79. For an account with one active workspace the grant binds to it; for an account with two the
    screen offers exactly those two and binds the chosen one; an account with none issues
    nothing and answers `workspace_unselected`; a workspace id the account is not a member of,
    submitted in the Allow `POST`, is refused. (FR 11, edge case 9)
80. The token exchange returns an access token with prefix `rheo_mcp_`, whose `access_token` row
    has kind `mcp`, one account, one workspace, a non-null `expires_at`, an issuing-authority
    value distinct from `session`, `operator` and `runtime`, and snapshot rows equal to the
    `agent_default` expansion intersected with the member role's permitted set, minus the
    non-token-issuable set (for an owner, the full expansion minus that set); a test asserts
    none of the eight non-token-issuable operations is in the snapshot. (FR 12)
81. The access token's lifetime equals the configured value and never exceeds
    `identity.token_max_days.mcp`; the refresh grant's absolute end never exceeds it either; a
    refresh at the cap answers `invalid_grant`. (FR 13, edge case 14)
82. Using a refresh token returns a new access token and a new refresh token and the old refresh
    token then answers `invalid_grant`; presenting the old one a second time (outside any
    recorded grace) revokes the grant so that the newest access token is refused
    `token_revoked`; presenting it inside the grace answers `invalid_grant` and changes nothing.
    The test re-reads the database after each refusal and asserts the written state (the
    revocation and the `refresh_reuse_revoked` row exist after the reuse; nothing changed after
    the grace-window refusal). (FR 13, edge case 4)
83. An authorization code redeems once; a second redemption answers `invalid_grant` and revokes
    what the first issued; redemption with a wrong or missing `code_verifier`, a different
    `client_id`, a different `redirect_uri`, or after 60 s answers
    `invalid_grant`/`invalid_client`, and with a present `resource` that is not the canonical
    identifier answers `invalid_target`, in each case issuing nothing; an absent `resource`
    redeems successfully; failed redemptions leave the code burned (a later correct redemption
    of the same code answers `invalid_grant`); the token endpoint accepts
    `application/x-www-form-urlencoded`. The test re-reads the database after each
    state-changing refusal and asserts the burn, and for a replay the revocation and its event
    row. (FR 14, edge case 3)
84. A connector access token presented to the `api` surface answers `token_wrong_kind`;
    presented to the `mcp` surface it lists the `agent_default` tools and a call to a tool
    outside its snapshot answers `operation_not_permitted`; a call is dispatched through the
    same boundary function as a token minted by `rheo token issue` (a test patches the boundary
    and sees the one call). (FR 15)
85. Three regression tests, one per existing caller, pass without edits to the existing tests:
    (a) a Claude-Code-style `mcp` token lists and calls Recallatron tools; (b) the bridge's
    `cli` token calls `core.evidence.ingest` on the `api` surface; (c) a dedicated
    `agent_default` `mcp` token lists and calls tools. Each compares the response to the
    pre-change golden response, normalizing only a named, closed list: the `date` and
    `content-length` headers, any MCP session-id header, and body values at keys `id`, `*_id`
    and `*_at`; everything else is compared exactly. The existing `mcp` transport and token test
    files are unmodified by the change except for added cases. (FR 16, edge case 12)
86. A full registration, authorization, exchange, call and refresh flow is run against a test
    log sink, and a scan of every captured log line, every audit row, every `operation` row and
    every error body finds none of: the access token, the refresh token, the code, the verifier,
    the challenge. A test also asserts that presenting a bearer in a query string on the `mcp`
    and `api` surfaces is not accepted. (FR 17, edge case 13)
87. `rheo token list` shows every connector grant with the fields FR 18 names;
    `rheo token revoke <id>` makes the grant's next `mcp` call answer 401 with the pointer and
    makes its refresh answer `invalid_grant`; revoking one of two grants for one account leaves
    the other working. (FR 18, edge cases 10, 11)
88. `rheo doctor` output includes connector-grant counts and the OAuth-configured line, contains
    no token, client name or other client-supplied text, and goes `FAIL` for the counted-client
    cap and missing-refresh-row cases. (FR 19)
89. Each audit event FR 20 lists is written exactly once for a scripted flow, each row carries
    account, workspace, client id, grant id and an outcome word, and a deny and a refresh-reuse
    each produce their own row. The test re-reads the database after each refusal (deny,
    replay, refresh-reuse, refused registration) and asserts the row exists, so a refused
    request still leaves its event. (FR 20)
90. With the feature unconfigured (off, or enabled but incomplete) the `mcp` surface answers
    today's bare `Bearer` 401, the well-known paths and `/auth/oauth/*` answer 404, and doctor
    shows `FAIL` for the enabled-but-incomplete case; with it configured no advertised endpoint
    answers 404 or 500; the same suite passes in path mode with the endpoints built through
    `url_for`, and a route string naming a host or prefix outside `url_for` fails the existing
    lint. (FR 21)
91. **Manual, once, by the maintainer after deploy and on his go:** adding the connector
    `https://mcp.rheo.stream/` in claude.ai (desktop chat), then using it from claude.ai on the
    web and on the phone, each completes sign-in through GitHub on `auth.rheo.stream` (or reuses
    the account's connection, per S5), lists the Recallatron tools, and one `recallatron_recall`
    call returns a result from his own workspace. The result is recorded in the run's closeout
    and the matrix's manual row; it is not claimed by any CI test. (Outcome metric)
92. The `oauth_event` reader (e.g. `rheo token events`), run after a scripted flow that includes
    a registration, a grant, a deny, a code redemption, a refresh and a refresh-reuse
    revocation, lists those events newest first; every row shows only the content-free fields
    (event, outcome word, time, account, workspace, client id, grant id); a scan of its output
    finds none of the access token, refresh token, code, verifier, challenge or any
    client-supplied free text (including the client name); and a caller who is not the
    operator/owner is refused. (FR 20)
93. A pending authorization cannot be completed late, from another browser, or twice (edge case
    8; FR 9, 10). A test drives each case and, after every refusal, re-reads the database and
    asserts that no code was issued (`code_hash` and `code_expires_at` null on the row), no
    `access_token` or `oauth_grant` row was created, and the row is unchanged. (a)
    **Expired:** with the clock at or after the `oauth_authorization` row's `expires_at`
    (created plus 10 minutes), `GET /auth/oauth/consent` with the correct `rheo_oauth_request`
    cookie answers the `authorization_expired` page (`data-state="authorization_expired"`), and
    an Allow `POST` answers the same page; nothing is issued. (b) **Another browser:** with a
    live row, `GET /auth/oauth/consent` with no `rheo_oauth_request` cookie, and with a cookie
    whose nonce hashes to no row, answers `authorization_expired` and issues nothing; a session
    that holds a different, live pending row of its own and posts the first row's `request_id`
    is refused `authorization_expired` (the cookie's row id must equal `request_id`); an Allow
    `POST` whose `request_id` is not the id of the row named by the cookie is refused
    `authorization_expired`. (c) **Already decided:** after Deny, and after an Allow that issued
    a code, replaying the same cookie value against `GET` and `POST /auth/oauth/consent` answers
    `authorization_expired` and issues no second code and no second approval or denial event;
    the replayed Allow does not change the code the first Allow issued. Boundary cases: at 1
    second before `expires_at` the row is still live and consent succeeds; at `expires_at`
    exactly it is refused; the `Set-Cookie` for `rheo_oauth_request` carries `Max-Age=600` (the
    same 10 minutes), so a browser that has dropped the cookie at Max-Age is the absent-cookie
    case in (b). (FR 9, 10; edge case 8)

---

## Phase 3 — The opportunity core

**Goal.** The first real funnel works end to end. An inbound inquiry from a site the maintainer
already operates arrives, becomes an observation with provenance, resolves a party, creates an
opportunity in a configured pipeline, and is worked to a terminal disposition in the web
interface and through rheo. This phase completes release one.

**In scope**

- The relationships module at the R4 contract: party identity, contact points, affiliations,
  automatic matching restricted to authenticated evidence, and merges reversible by
  construction.
- Both the relationships module and the opportunity core install and enable through the
  registration contract phase two established, with manifests that pass criterion 25's
  validation. Neither is wired into the core by a core file change, which is what lets a
  workspace hold relationships with Leads absent (criterion 54).
- Generic intake: the authenticated ingestion API, a signed webhook receiver, CSV/JSON import,
  manual capture, declarative versioned field mapping, durable receipts, and connection health.
- Observations with per-field provenance, occurrence and receipt time, bounded source payload,
  and acquisition attribution separate from delivery transport.
- Opportunities, one inbound-services pipeline preset with stable stage identifiers and pinned
  configuration versions, qualification records, and terminal dispositions.
- The record-level deletion path for observations, parties, and opportunities (R5).
- The handoff operation, defined with no destination.
- A recording sink: a test-only operation registered in the external-side-effect safety class
  that records what it would have sent and sends nothing. It is the only external-class operation
  in release one, and it exists so that the approval binding and the fail-closed recheck have
  something to execute against. It is a test fixture, not a shipped destination: R2's rule is
  unchanged, the handoff operation still has exactly one release-one destination, none, and the
  sink is not a handoff destination and does not appear in the release-one scope boundary as a
  product capability. Like the phase-one fixtures, it is registered only by the test harness
  under the test profile; the production-profile check in criterion 18 asserts that the shipped
  registration set contains no external-side-effect operation, so a production build carries no
  reachable operation in that class.
- Contact purpose and permission records, kept separate from observed interest.
- The first web interface: opportunity list and triage, opportunity detail with evidence,
  qualification view, and connection settings, ported per the UI port inventory.
- The first real funnel connected, plus a second unrelated synthetic event-referral funnel as a
  fixture. Both route into the one inbound-services preset through different declarative mappings
  and different routing rules. Pointing the live form at the deployment sends real inquiries from
  real people into the system, so it is a step the maintainer performs deliberately once the
  synthetic funnel passes; nothing connects it automatically.

**Out of scope**

- Any job-search field, source, rubric, or screen.
- Work creation. A handoff has no destination in this phase.
- External CRM connections. The field-ownership contract is written; no connector ships.
- Any external-side-effect operation other than the recording sink, and any financial operation
  at all.
- A second pipeline preset, including a dedicated referral preset.

**Acceptance criteria**

38. A valid signed webhook delivery is authenticated, stored, acknowledged, and processed to a
    created opportunity with no agent runtime process running anywhere in the deployment.
    *(Scenario: Intake without a model session; FR 35, FR 32.)*
39. A CSV import and a JSON import are accepted through the same declarative, versioned field
    mapping as the webhook receiver and produce normalized observations on the same path,
    verified by a test that maps one source record through all three transports and asserts the
    resulting observations are identical apart from their delivery transport. A file whose shape
    does not match its declared mapping version is refused with a named field rather than
    partially applied. *(Scenario: Two different custom funnels; FR 32.)*
40. A manually captured inquiry produces a normalized observation through the same declarative,
    versioned field mapping and the same processing path as a webhook delivery. Its delivery
    transport is recorded as manual capture, and its acquisition attribution, meaning which funnel
    and which campaign, is chosen by the operator at capture from the funnels the workspace has
    configured, so attribution is never empty and never holds a transport value. A test asserts
    the resulting observation is indistinguishable in shape from a delivered one, and that a
    capture submitted with no funnel selected is refused rather than stored with empty
    attribution. *(Scenario: Two different custom funnels; FR 32, FR 36.)*
41. The authenticated ingestion API resolves the workspace from the connection's own credential.
    A test posts a delivery whose payload claims a different tenant, a different destination
    workspace, and an elevated permission set, then asserts all three claims are ignored, the
    observation lands in the credential's workspace, and the claimed values survive only as
    evidence inside the stored payload. *(Scenario: Wrong workspace or channel audience; FR 31.)*
42. A webhook connection whose signing secret has been revoked or rotated refuses every delivery
    signed with the old secret from that moment on, including deliveries already accepted by the
    receiver but not yet processed. A test accepts a delivery, revokes the secret before the
    worker runs, and asserts the queued processing fails closed with no observation created and
    the receipt marked as refused rather than pending; a second delivery signed with the old
    secret is refused before any receipt is stored and no acknowledgement is returned; and both
    refusals are visible as unresolved failures on that connection's health. *(Scenario:
    Permissions or contact purpose withdrawn, for its requirement that queued effects recheck
    policy; FR 31, FR 38, R3 item 4.)*
43. The acknowledgement returned to the sender is emitted only after the receipt and its pending
    processing work are durably stored, verified by a test that fails the processing step and
    asserts the receipt survives. *(Scenario: Intake without a model session; FR 33.)*
44. Two concurrent deliveries carrying the same source event identifier produce exactly one
    observation and exactly one processing effect. *(Scenario: Same event retried concurrently;
    FR 34.)*
45. A delivery reusing an accepted source event identifier with different content is recorded as
    a conflict, is visible as one, and does not modify the existing observation. *(Scenario: Same
    event retried concurrently; FR 34.)*
46. Every observation records per-field provenance, the source occurrence time, the server receipt
    time, and a bounded copy of the source payload, with the bound enforced rather than advisory.
    Acquisition attribution, meaning which funnel and which campaign, is stored in fields distinct
    from the delivery transport, verified by a test that delivers the same funnel and campaign
    through two transports and asserts the attribution matches while the recorded transport
    differs. *(Scenario: Late evidence or scoring result; FR 36, guardrail 10.)*
47. An observation arriving with an older source occurrence time than the current field values
    does not overwrite them, and a thinner observation does not overwrite a richer one. A test
    submits both orderings and asserts identical resulting field values. *(Scenario: Late
    evidence or scoring result; FR 37.)*
48. A field absent from a payload and a field explicitly cleared by a payload produce
    distinguishable results. *(Scenario: Late evidence or scoring result; FR 37.)*
49. A user-set stage or note survives any later observation, verified by a test that sets a
    stage by hand and then submits a fuller observation. *(Scenario: Late evidence or scoring
    result; FR 37.)*
50. The same person completing two funnel steps produces two observations, and produces two
    opportunities only when a routing rule says so. The fixture supplies evidence of the grade R4
    requires for an automatic match, a source-verified email address from the same source
    connection on both steps, so exactly one party results; a test asserts one party, two
    observations, and the configured opportunity count. Without that evidence the expected result
    is a second party and a review candidate, which criterion 51 covers. *(Scenario: One person,
    several interactions; FR 39, R4.)*
51. An automatic party match occurs only on an authenticated external subject identifier within
    the connection's namespace or on a source-verified email address. A test presents a matching
    name, a matching company domain, and a matching phone number and asserts each produces a
    review candidate and no link. *(Scenario: One person, several interactions; FR 41, R4.)*
52. Party matching never considers a party in another workspace, verified by a test that creates
    identical parties in two workspaces and asserts no candidate crosses. *(Scenario: Wrong
    workspace or channel audience; FR 41.)*
53. After a merge, both original party identifiers still resolve, and an unmerge restores the
    pre-merge state including every record's party reference. *(Scenario: One person, several
    interactions; FR 41, R4.)*
54. A workspace with the Leads module absent creates parties, contact points, and affiliations
    through the relationships module and reads them back, verified by running the relationships
    behavioural suite in a workspace where the relationships module was installed and enabled
    through the registration contract and Leads was never installed. No relationships record carries
    an opportunity field, and no relationships code path references a Leads table. *(Scenario:
    Client work without Leads; FR 40, R4, guardrail 24.)*
55. Two unrelated funnels, the maintainer's inbound-inquiry form and a synthetic event-referral
    funnel, run through the same intake contract with different declarative mappings and different
    routing rules, and both route into the single inbound-services pipeline preset release one
    ships. Neither requires a new core field, a provider-specific column, a second preset, or a
    product name anywhere in the core. *(Scenario: Two different custom funnels; FR 44.)*
56. A workspace works a pipeline to a terminal disposition with no job board configured, no
    candidate profile present, and no application tool registered. A test asserts the
    job-search-related configuration surface is absent rather than merely hidden, and that the
    terminal disposition the opportunity reaches is stored with the comparable outcome the
    preset maps it to, readable through a supported operation. *(Scenario: Business-only
    installation; FR 42, FR 44.)*
57. Editing a pipeline preset does not change the stage meaning or field schema of opportunities
    created under an earlier configuration version, verified by a test that edits a preset with
    opportunities mid-stage and asserts their pinned version and stage semantics are unchanged.
    The same test renames a stage's label and asserts the stage identifier and every
    opportunity's stage reference are unchanged, so labels are editable and identifiers are
    stable. *(Scenario: Preset edited during active work; FR 43, FR 42.)*
58. Every opportunity and every qualification record stores the configuration version it was
    created under, readable through a supported operation, and a qualification record is a
    record of its own rather than a field on the opportunity. *(Scenario: Preset edited during
    active work; FR 42, FR 43, FR 39.)*
59. An inbound payload whose text instructs the agent to send a message, grant a tool, or change
    a policy results in no tool grant, no policy change, and no external effect. The text is
    retrievable as evidence. *(Scenario: Source text requests a tool action; FR 25.)*
60. An approval binds actor, workspace, operation, recipient or destination, payload digest,
    purpose, and execution window, exercised against the recording sink, which is release one's
    only external-side-effect-class operation. A test approves a payload addressed to the sink,
    alters one byte of it, and asserts execution is refused with an explicit invalid-approval
    state rather than executed against the altered payload; the test repeats with an altered
    recipient and with an elapsed execution window, and asserts in each refused case that the
    sink recorded no attempt. A further case revokes the acting actor's permission for the
    operation between approval and execution and asserts the execution rechecks and fails closed
    with nothing recorded, and a last case asserts that a standing grant does not satisfy the
    external class at all. *(Scenario: Headless approval or failure; FR 24, R3 items 3, 4, and
    5.)*
61. Withdrawing contact permission causes a queued action against that contact to fail closed at
    execution. A test approves and queues a recording-sink action against a contact, withdraws
    the contact permission, runs the queue, and asserts the action is refused at execution and
    the sink recorded nothing. The same test asserts that memory derived from that contact's data
    stops being retrievable for the withdrawn purpose. *(Scenario: Permissions or contact purpose
    withdrawn; FR 46, FR 27, FR 28, R3 item 4.)*
62. Observed interest and permission to contact are separate records. A test asserts that
    recording an inbound submission creates no contact permission by itself. *(Scenario:
    Permissions or contact purpose withdrawn; FR 46.)*
63. Connection health, last successful intake, lag, and unresolved failures are readable per
    connection through a supported operation and are visible in the connection settings screen.
    *(Scenario: Intake without a model session; FR 38.)*
64. A handoff operation records a durable identifier, source opportunity, purpose, destination,
    approved payload snapshot, and result. With no destination configured, requesting one yields
    an explicit unavailable state, not a silent success and not a created opportunity outcome.
    *(Scenario: Crash during handoff; FR 45, guardrail 20.)*
65. Deleting an observation, a party, or an opportunity requires an explicit per-action
    confirmation, removes the record together with its derived summaries and embeddings, removes
    every export artifact still held under the data root that carries it and marks that
    artifact's export record as removed by deletion, cancels any queued action that depends on
    it, and leaves a deletion record naming actor, time, record type, and record identifier. A
    test produces a workspace export, then deletes one record of each type and asserts: the
    record and every derivative are unreadable through every supported operation; the export
    artifact is gone from the data root and its export record says why; the dependent queued
    action is cancelled rather than left runnable; the deletion record exists and carries no
    field copied from the deleted record; a standing grant does not satisfy the confirmation;
    deleting a party leaves its observations in place as unlinked evidence; and deleting an
    observation leaves its delivery receipt in place holding the source event identifier, the
    content digest, and the timestamps and no payload. The test then re-delivers the deleted
    observation's source event and asserts it is recorded as a conflict against the surviving
    receipt and recreates no observation. *(Scenario: Permissions or contact purpose withdrawn;
    FR 51, FR 34, R5.)*
66. The phase-three acceptance suite passes in a workspace where the memory module was never
    installed and never enabled: intake, party resolution, opportunity creation, work through the
    pipeline, and the terminal disposition all complete. Two clauses are not exercised in this
    run, because no memory exists to derive from: criterion 61's clause that memory derived from
    the contact's data stops being retrievable, and criterion 65's embeddings clause. Both pass
    only in the memory-installed run of the same suite, and the suite records them as skipped,
    not passed, in the no-memory run. Every other clause of every phase-three criterion is
    exercised in both runs. *(Scenario: A new specialist module, for the half requiring that the
    core carry no dependency on a module added through the public extension points; FR 26.)*
67. Criterion 21 is re-asserted at the end of release one against a workspace with the
    relationships module and the opportunity core populated. The export carries parties, contact
    points, affiliations, observations with their per-field provenance, opportunities with their
    pinned configuration versions, and permission records, and after a restore into an empty
    deployment every one of them matches the original by comparison. A pending approved
    recording-sink action is restored in a state that requires a fresh approval before it can
    execute. *(Scenario: Export and restore; FR 52.)*
68. Criteria 22, 23, 34, and 35 still pass with this phase's interface surface included: the
    opportunity list and triage queue, the opportunity detail view, the qualification view, and
    the connection settings screen. This criterion adds no new check; it asserts that the four
    gates run unchanged in continuous integration over the larger surface and that none of them
    regressed. *(Scenario: Publication review; FR 47, FR 48, FR 49, FR 50.)*

---

## Phase 4 — The optional job-search workflow

**Goal.** Restore the enrichment and application-preparation depth of the opportunity-discovery
predecessor, entirely as an optional workflow that a business user never sees.

**Shape.** A job-search capability supplying pipeline presets, namespaced schema extensions for
candidate-fit and application questions, the job-source connectors, the multi-observation
enrichment behaviour where an email alert, a copied search result, and a full posting converge on
one opportunity, and the proposal drafting and answer library screens deferred from the UI port
inventory. The email transport becomes a deterministic client-side poller with a durable cursor,
not an agent routine.

**Key boundaries.** Nothing in this phase adds a field, a column, or a concept to the Leads core.
The job-search capability is a bundle of presets and namespaced schema extensions turned on and
off by workspace configuration, not a module that gets disabled: module disable in the lifecycle
sense is phase-seven work under D5. With the toggle off, its enrichment and polling workers stop
being scheduled, any run already in flight finishes or fails through the ordinary
operation-status path rather than being killed, its screens are hidden, and its records stay
readable and exportable. The acceptance scenario this phase must satisfy is **Job-search
regression**: email, search result, and full posting enrich one opportunity while preserving
proposals and user decisions. It must also re-satisfy **Business-only installation** with the
capability enabled but unconfigured.

**Answers on landing.** Idea-doc question 5 (which proposal-generation capabilities belong in the
initial job-search release).

---

## Phase 5 — The work-in-motion module

**Goal.** Current ships: commitments, projects, tasks, status, priority, due dates, and
dependencies, with work created from an opportunity or independently.

**Shape.** The module ported from the work-in-motion predecessor, including its queue, board, and
work-item screens, moved from `better-sqlite3` to the per-workspace Postgres database. The
handoff operation from phase three gains its first destination, with the destination
deduplicating retries so that a lost response after successful creation does not create a second
project. Leads displays a linked work item's state rather than keeping a second editable copy.

**Key boundaries.** Current does not become an invoicing or accounting product. Completing a task
never asserts a domain outcome that another module owns. The acceptance scenario this phase must
satisfy is **Crash during handoff**: a retry recovers the same created work, and an uncertain
external outcome stays visible until reconciled.

---

## Phase 6 — Channels and the second runtime

**Goal.** Prove that the authorization model is one model, by adding a second conversational
surface and a structurally different runtime adapter.

**Shape.** The OpenRouter adapter, which supplies its own agent loop and dispatches tools through
the authorized boundary, with an explicit model and provider allowlist, required-parameter
enforcement, and a fallback policy that cannot widen who receives private context. Telegram as a
second channel, entering the same session and authorization model, with per-audience visibility
so that a private conversation and a group conversation are distinct. Voice waits until this
phase's text interaction and permission model are stable.

**Key boundaries.** No channel becomes a privileged back door. Native session identifiers stay
private adapter handles bound to runtime, credential scope, actor, workspace, and audience, and no
adapter ever resumes a global latest session. The acceptance scenarios this phase must satisfy
are **Runtime choice**, **Runtime switch or retry**, and **Wrong workspace or channel audience**.

**Answers on landing.** Idea-doc question 15 (minimum useful voice experience and its timing) and
the hosted-default half of question 10.

---

## Phase 7 — Framework proof and a v0.1 release

**Goal.** Demonstrate that the extension contracts are real, then publish.

**Shape.** The remainder of the module lifecycle from D5: disable, remove, purge, and restore,
with dependency checks, in-flight effect reconciliation, retained exports, and detached-record
handling. A small synthetic specialist module, registering records, tools, interface
contributions, jobs, migrations, and exports through the public extension points with no change
to the core. Domain packs and their composition rules. The invitation flow and self-service
member management that R1 left out of release one, so that a workspace can gain a second member
without an operator command; with a second member present, FR 6 gains its test, that the first
member's personal preferences and personal credentials are not readable by the second. The
licence decision was planned for this point, to be made before accepting substantial outside
contributions and after the contracts it governs are real; it was settled early, on 2026-09-17,
as AGPL-3.0 (`LICENSE`, SPDX `AGPL-3.0-only`), so this phase inherits it and reviews only the
contribution terms.

**Key boundaries.** The synthetic module is an example, not a product for another profession. The
acceptance scenarios this phase must satisfy are **A new specialist module**, **Module disabled
during execution**, **Module removal with dependencies**, **Removal from one workspace**,
**Module reinstalled or upgraded**, **Combined domain packs**, and **Public pack export**.

**Answers on landing.** Idea-doc questions 14, 21, 22, and 23.

---

## Phase 8 — The hosted edition

**Goal.** Offer a managed workspace to a customer who does not want to operate infrastructure.

**Not planned in detail.** This phase is named so that release-one decisions do not foreclose it,
not because its shape is decided. What is already recorded and carried forward:

- Module subdomains compete with per-tenant subdomains for the same namespace level, and a
  wildcard certificate covers one level only. The hosted edition must choose its scheme
  deliberately rather than inheriting the reference deployment's.
- A hosted service uses service-owned model credentials through an API or agent SDK. It must not
  depend on customers sharing consumer CLI subscription logins.
- Per-workspace encryption and key management need a threat model and a recovery procedure, not
  one library option, and their design is a prerequisite for this phase rather than an addition to
  an earlier one.
- Billing metadata joins the control plane here. Release one holds none (D1, FR 7).
- Provider licensing and attribution for every job source must be reviewed before that source is
  enabled in a public hosted service.

**Answers on landing.** Idea-doc questions 3, 9, and the hosted half of question 2.

---

## Functional-requirement coverage

Every functional requirement in the companion document, from FR 1 to FR 53, appears below. A
requirement is either mapped to the acceptance criteria that test it, or recorded as deliberately
untested in release one with the phase or milestone that carries it and the reason. A
requirement with neither is a gap in this plan, and none is listed that way. Where a criterion
tests part of a requirement, the row says which part and names what is left untested.

| FR | Tested by | Note |
| --- | --- | --- |
| FR 1 | 7 | Cross-workspace refusal across all three entry surfaces is the boundary test. |
| FR 2 | 6 | Storage routing may not come from any caller-supplied source. |
| FR 3 | 8 | Substituting the second provider implementation behind the boundary is the check that domain code depends on the boundary. |
| FR 4 | 9 | Both issuance paths, the scope, and the three rejection cases. |
| FR 5 | 8 | An `owner`-only operation refused to a `member` session is the role check. |
| FR 6 | none in release one | Deliberate. Release one stores only the owner's material (R1), so no second member's private material exists to separate. Phase seven adds the invitation flow and with it the second member whose access is the test. |
| FR 7 | 5 | Distinct database or schema per workspace. |
| FR 8 | none in release one | Deliberate. D2 ships no second backend, so the adapter seam can only be proven by the later local-first edition that supplies one; release one writes the interface-level suite without a second implementation to run it against. |
| FR 9 | 10, 24 | Composition and schema versions as readable product data. Criterion 10 passes on an empty module set in phase one; criterion 24 is the test of the two module clauses. |
| FR 10 | 2, 3 | Runtime output outside the tracked tree, and the checkout-local opt-in fully ignored. |
| FR 11 | 26 | The core storage API is the only route to a connection, no module holds another module's tables, and cross-module change goes through public operations and events. |
| FR 12 | 69 | The three-source precedence and the operator floor, proved end-to-end through the settings write path: a floored override looser than the deployment value is refused naming the key, and a stored row is clamped on read once the deployment value tightens. Criterion 6 covers the separate rule that a workspace setting cannot supply storage routing or actor identity (FR 2 and FR 23). |
| FR 13 | 17 | A model credential and the OAuth client secret held by reference, resolved only in the presenting component, and absent from the runtime request, every tool argument, the operation record, and the audit record. The redaction contract for what reaches a model provider is still an architecture-specification deliverable (idea-doc question 12); criterion 17 tests the secret boundary, not redaction of workspace content. |
| FR 14 | 1, 2 | Fresh clone, synthetic demo, tracked source unchanged. |
| FR 15 | 11 | Outbox in one transaction, exactly-once delivery after a kill. |
| FR 16 | 12 | Leases, bounded retries, an inspectable failure list, and cancellation of a queued and a running task. |
| FR 17 | 13 | Operation identifier and a terminal status set including unresolved. |
| FR 18 | 14 | Written, readable, and impossible to skip by omission. |
| FR 19 | 15 | Partial. The adapter contract is exercised from the first adapter. That no domain module or channel depends on a specific runtime is fully proven only by the structurally different second adapter in phase six (**Runtime choice**). |
| FR 20 | 15 | Partial. Structured output, streaming, and continuation are the capabilities criterion 15 declares and rejects. Cancellation and usage reporting are defined in the contract but untested in release one; phase six tests them when the second adapter must satisfy the same discovery (**Runtime choice**, **Runtime switch or retry**). |
| FR 21 | 16 | Five induced failure modes, including the timeout, no hang, no false completion. |
| FR 22 | 20 | Namespaced goal-level tools, no raw database or privileged SQL path. |
| FR 23 | 6 | Workspace and actor come from the authenticated session; a model-supplied argument is ignored. |
| FR 24 | 18, 19, 60 | Declared class at registration for every MCP tool and every service operation, the production registration set free of test fixtures and of the external and financial classes, the approval-required state and the approval record exercised against the destructive-class fixture, and the approval binding exercised against the recording sink. |
| FR 25 | 59 | Inbound text stays evidence. |
| FR 26 | 24, 33, 66 | Installable and enableable through the contract; the phase-one and phase-three paths both complete where it was never enabled. |
| FR 27 | 27, 28, 61 | Permission-enforced retrieval, intersection of audiences, withdrawal honoured. |
| FR 28 | 29, 61, 65 | Invalidation of derived summaries and embeddings with the source, on supersession and on deletion. |
| FR 29 | 30 | Explicit retention, stored per workspace at enable. "Nothing is retained permanently by default" is read as forbidding an *inherited* retention posture, not an explicit permanent choice: both settings are written as rows, the age-expiry gate's package default is off, and the window is bounded 1 to 3650 days with no indefinite sentinel (amended 2026-09-21; the recorded change of direction is in [memory](../architecture/memory.md#retention-fr-29-criterion-30)). |
| FR 30 | 31 | The behavioural suite passes on dense-only and lexical-only configurations. |
| FR 31 | 41, 42 | Workspace resolved from the connection credential; claimed values never override it; a revoked signing secret fails closed for queued and for new deliveries. |
| FR 32 | 38, 39, 40 | All three transports through one declarative versioned mapping. |
| FR 33 | 43 | Acknowledgement only after durable receipt. |
| FR 34 | 44, 45, 65 | Concurrent duplicate, conflicting content under one identifier, and re-delivery of a deleted observation's source event recorded as a conflict against the surviving receipt. |
| FR 35 | 38 | Intake with no runtime process anywhere in the deployment. |
| FR 36 | 40, 46 | Provenance, both timestamps, the bounded payload copy, and attribution separate from transport including at manual capture. |
| FR 37 | 47, 48, 49 | Older and thinner evidence, absent versus cleared, user-owned state. |
| FR 38 | 42, 63 | Health, last intake, lag, unresolved failures, per connection, including a revoked signing secret's refusals. |
| FR 39 | 50, 58 | Partial. Criterion 50 tests two observations, one party, and a configured opportunity count, and criterion 58 tests that qualification records are distinct records with their own pinned version. Untested in release one and carried by phase four: an observation remaining unlinked, being linked later, or supporting several opportunities through recorded decisions, which the job-search workflow's multi-observation enrichment is the first path to exercise (**Job-search regression**). |
| FR 40 | 54 | Relationships installed through the registration contract and usable with Leads absent. |
| FR 41 | 51, 52, 53 | Two evidence classes only, no cross-workspace candidate, reversible merge. |
| FR 42 | 56, 57, 58 | Terminal disposition reached and stored with its mapped comparable outcome, stage identifiers stable under a label rename, and the pinned configuration version stored and readable. |
| FR 43 | 57, 58 | Preset edits do not reinterpret existing records. |
| FR 44 | 55, 56 | Partial. Two funnels through one core and one preset, and no job-shaped surface present, prove the core acquires no new field. The versioned, namespaced, schema-validated extension mechanism itself is not exercised in release one, because the first extension is phase four's job-search bundle; that phase carries it (**Job-search regression**). |
| FR 45 | 64 | The handoff operation exists and has no destination. |
| FR 46 | 61, 62 | Interest and permission are separate records; withdrawal fails closed. |
| FR 47 | 22, 37, 68 | Both topology modes with no code change, held from phase one onward, including the login redirect and the OAuth callback URL. |
| FR 48 | 23, 37, 68 | No hosting-platform-only dependency, held from phase one onward. |
| FR 49 | 34, 68 | No legacy name anywhere outside the listed historical notes, held from phase two onward, when the first ported code arrives. |
| FR 50 | 35, 68 | Ported code carries no real data; every fixture synthetic, held from phase two onward. |
| FR 51 | 65 | Confirmed per action, cascading to derivatives and to held export artifacts, cancelling dependents, leaving a content-free deletion record and a payload-free receipt. |
| FR 52 | 21, 25, 36, 67 | Export and restore on an empty workspace with a pending approved fixture action restored unapproved, then re-asserted with a module populated and again with the opportunity core populated; the module export format is declared in the manifest and exercised by the phase-two re-assertion. |
| FR 53 | 32 | Count-and-sample verification before switchover, with the report kept in private workspace storage. |
