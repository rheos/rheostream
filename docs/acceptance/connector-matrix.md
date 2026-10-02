# Connector sign-in acceptance matrix

This is the acceptance record for the connector sign-in change (issue #287): one row for
each of criteria 70-93 in `build-plan.md`'s "Connector sign-in (issue #287)" block. Read
[README.md](README.md) first. Its per-row grammar, its three parser rules, and above all its
"what this is not" section govern every row here: the matrix records which test demonstrates
a criterion and which mutation proves that test bites, and it never claims a criterion is
demonstrated merely because a row exists.

**Criterion numbers map to the change's own acceptance criteria.** Criteria 70-90 are its
AC-1 to AC-21 in order (70 is AC-1, 90 is AC-21), 91 is AC-24, 92 is AC-25 and 93 is AC-26.
AC-22 and AC-23 are documentation criteria about this record and the decision ledger and get
no number of their own. Criterion 91 is the one manual check, performed once by the
maintainer after deploy, so its row is `deferred` and claims no test.

**Rows 70-81 are written; rows 82-93 follow.** This file is not yet registered in
`tests/test_acceptance_matrix.py`, because the guard's completeness check would fail on the
missing half. It is registered in the same change that adds the last row, with its expected
set attached, so until then nothing mechanical checks it.

Every mutation below was applied to this working tree on 2026-10-02, its row's demonstrators
were run, watched go red with the failure the `Cost` line quotes, and the mutation was
reverted and the demonstrators re-run green. Each fenced block is the `git diff` captured
while it was applied, never a hand-typed hunk, and each one passes `git apply --check`
against the committed tree. Where a row lists more demonstrators than its mutation reddens,
the rest were run under the same mutation and stayed green; the README's third parser rule
allows that, and the row's `Note:` says why it matters when it does.

---

### Criterion 70

**Text:** "With the feature configured, an unauthenticated `POST` to the `mcp` surface answers `401` whose `WWW-Authenticate` header is `Bearer` with a `resource_metadata` parameter equal to `url_for` of the metadata document, in both subdomain and path mode; a test asserts the header string. (FR 1, 21)" (`build-plan.md:388-391`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_mount.py::test_unauthenticated_post_carries_the_resource_metadata_pointer`

**Mutation:**
```diff
diff --git a/apps/mcp/src/rheo_app_mcp/transport.py b/apps/mcp/src/rheo_app_mcp/transport.py
index 94489b1..eb4c547 100644
--- a/apps/mcp/src/rheo_app_mcp/transport.py
+++ b/apps/mcp/src/rheo_app_mcp/transport.py
@@ -218,9 +218,7 @@ class _BearerGate:
             return
         bearer = _bearer_from(scope)
         if bearer is None or not bearer:
-            await _refuse(
-                send, TOKEN_MALFORMED, NO_BEARER_DETAIL, challenge=self.challenge
-            )
+            await _refuse(send, TOKEN_MALFORMED, NO_BEARER_DETAIL)
             return
         resolved = resolve_context(bearer)
         if isinstance(resolved, Refusal):
```

**Cost:** `pytest:tests/postgres/test_oauth_mount.py::test_unauthenticated_post_carries_the_resource_metadata_pointer[subdomain]` — first observed failure line: `E       assert 'Bearer' == 'Bearer resou...ted-resource"'`; the `[path]` case failed the same way against its own pointer, 2 failed

**Performed by:** mcpoauth-P13 (2026-10-02)

**Note:** the mutation drops the pointer from the no-bearer refusal only. The refusal for a
presented-but-bad bearer keeps it, so this row and criterion 72's are reddened by different
lines of the same gate.

### Criterion 71

**Text:** "With the feature configured, `GET /.well-known/oauth-protected-resource` on the `mcp` host answers 200 JSON whose `resource` equals the canonical connector identifier and whose `authorization_servers` has length 1 and equals the issuer; `GET /.well-known/oauth-authorization-server` answers 200 with the FR 4 document; a non-`GET` on either answers 405; `GET /` on the same host still answers 405 with `Allow: POST`; and any other path on the `mcp` host still answers 404. With the feature unconfigured both well-known paths answer 404. (FR 2, 21)" (`build-plan.md:392-398`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_mount.py::test_configured_mount_serves_the_metadata_documents`
- `pytest:tests/postgres/test_oauth_mount.py::test_other_mcp_host_paths_stay_404_when_configured`
- `pytest:tests/postgres/test_oauth_mount.py::test_unconfigured_mount_is_unchanged`
- `pytest:tests/test_oauth_surface.py::test_protected_resource_metadata`

**Mutation:**
```diff
diff --git a/apps/core/src/rheo_app_core/mcp_mount.py b/apps/core/src/rheo_app_core/mcp_mount.py
index 86ea933..83b51e9 100644
--- a/apps/core/src/rheo_app_core/mcp_mount.py
+++ b/apps/core/src/rheo_app_core/mcp_mount.py
@@ -333,7 +333,7 @@ async def _answer_metadata(
     document: dict[str, object], scope: Scope, receive: Receive, send: Send
 ) -> None:
     """``GET``: the document, uncached. Anything else: ``405`` with ``Allow: GET``."""
-    if scope.get("method") != METADATA_METHOD:
+    if scope.get("method") not in (METADATA_METHOD, "POST"):
         await JSONResponse(
             {"detail": "Method Not Allowed"},
             status_code=405,
```

**Cost:** `pytest:tests/postgres/test_oauth_mount.py::test_configured_mount_serves_the_metadata_documents[subdomain]` — first observed failure line: `E           AssertionError: {"resource":"https://mcp.example.test/","authorization_servers":["https://mcp.example.test"],"bearer_methods_supported":["header"]}`, the message of `assert 200 == 405` on a `POST` to the protected-resource path; the `[path]` case failed the same way, 2 failed

**Performed by:** mcpoauth-P13 (2026-10-02)

**Note:** the mutation lets a `POST` to a metadata path read the document instead of being
refused. The other three demonstrators cover the 404 clauses (a near-miss path on the `mcp`
host, another host, and the feature off) and the document's own fields; they stayed green
under this mutation, as they should.

### Criterion 72

**Text:** "With the feature configured, a presented token that is malformed, expired or revoked still answers `401` with the same distinct `state` values as before this change, now also carrying `resource_metadata`; a valid token that lacks the operation answers the unchanged refusal. (FR 3)" (`build-plan.md:399-402`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_mount.py::test_bad_tokens_keep_their_states_and_carry_the_pointer`
- `pytest:tests/postgres/test_oauth_mount.py::test_a_token_lacking_the_operation_gets_the_unchanged_refusal`

**Mutation:**
```diff
diff --git a/apps/mcp/src/rheo_app_mcp/transport.py b/apps/mcp/src/rheo_app_mcp/transport.py
index 94489b1..00aa180 100644
--- a/apps/mcp/src/rheo_app_mcp/transport.py
+++ b/apps/mcp/src/rheo_app_mcp/transport.py
@@ -224,9 +224,7 @@ class _BearerGate:
             return
         resolved = resolve_context(bearer)
         if isinstance(resolved, Refusal):
-            await _refuse(
-                send, resolved.state, resolved.detail, challenge=self.challenge
-            )
+            await _refuse(send, resolved.state, resolved.detail)
             return
         state = scope.setdefault("state", {})
         state[CONTEXT_STATE_KEY] = resolved
```

**Cost:** `pytest:tests/postgres/test_oauth_mount.py::test_bad_tokens_keep_their_states_and_carry_the_pointer[subdomain]` — first observed failure line: `E           assert 'Bearer' == 'Bearer resou...ted-resource"'`; the `[path]` case failed the same way, 2 failed

**Performed by:** mcpoauth-P13 (2026-10-02)

**Note:** criterion 70's demonstrator was run under this mutation too and stayed green: the
no-bearer refusal still carried the pointer, so each row's mutation is caught only by its own
row's test. "The unchanged refusal" for a valid token lacking the operation is the shipped
MCP facade result (a `200` tool result with `isError` and state `not_found`), which is what
the second demonstrator asserts; the mutation does not touch that path.

### Criterion 73

**Text:** "The authorization-server metadata document contains every field FR 4 lists with those values, and contains neither `client_id_metadata_document_supported: true` nor `scopes_supported`. A schema test compares the field set. (FR 4, 5)" (`build-plan.md:403-405`)

**State:** complete

**Demonstrator:**
- `pytest:tests/test_oauth_surface.py::test_authorization_server_metadata_field_set`
- `pytest:tests/postgres/test_oauth_mount.py::test_configured_mount_serves_the_metadata_documents`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/oauth/surface.py b/packages/core/src/rheo_core/oauth/surface.py
index a21b17f..b28d1d0 100644
--- a/packages/core/src/rheo_core/oauth/surface.py
+++ b/packages/core/src/rheo_core/oauth/surface.py
@@ -128,6 +128,7 @@ class OAuthSurface:
             "code_challenge_methods_supported": ["S256"],
             "token_endpoint_auth_methods_supported": ["none"],
             "authorization_response_iss_parameter_supported": True,
+            "scopes_supported": [],
         }
 
     def resource_matches(self, value: str) -> bool:
```

**Cost:** `pytest:tests/test_oauth_surface.py::test_authorization_server_metadata_field_set[subdomain]` — first observed failure line: `E       AssertionError: assert {'authorizati...ndpoint', ...} == {'authorizati...ndpoint', ...}`, with `Extra items in the left set: 'scopes_supported'`; both modes of both demonstrators failed, 4 failed

**Performed by:** mcpoauth-P13 (2026-10-02)

### Criterion 74

**Text:** "DCR succeeds for the allowlisted callback and returns a public client with no secret; it is refused `invalid_redirect_uri` for each of: a different host, a trailing-slash or path-suffix variant, a changed scheme, a port, a userinfo, a fragment, and an empty list; a non-object body is refused `invalid_client_metadata`. A registration with an allowlisted URI that requests an unsupported `token_endpoint_auth_method`, `grant_types` or `response_types` succeeds and returns the overridden values (`none`; `authorization_code` and `refresh_token`; `code`). (FR 6, edge case 5)" (`build-plan.md:406-412`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_service.py::test_registration_returns_a_public_client`
- `pytest:tests/postgres/test_oauth_service.py::test_redirect_uri_refusals_commit_a_refused_event`
- `pytest:tests/postgres/test_oauth_service.py::test_non_object_body_is_invalid_client_metadata`
- `pytest:tests/postgres/test_oauth_service.py::test_unsupported_metadata_is_overridden_not_refused`
- `pytest:tests/postgres/test_oauth_token_http.py::test_registration_returns_the_effective_public_client`
- `pytest:tests/postgres/test_oauth_token_http.py::test_registration_refuses_a_redirect_uri_off_the_allowlist`
- `pytest:tests/postgres/test_oauth_token_http.py::test_registration_refuses_a_body_that_is_not_a_json_object`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/oauth/service.py b/packages/core/src/rheo_core/oauth/service.py
index e09111f..678055d 100644
--- a/packages/core/src/rheo_core/oauth/service.py
+++ b/packages/core/src/rheo_core/oauth/service.py
@@ -217,7 +217,7 @@ def register_client(
         if (
             not isinstance(requested, list)
             or not requested
-            or not all(isinstance(uri, str) and uri in allowed for uri in requested)
+            or not all(isinstance(uri, str) and uri.rstrip("/") in allowed for uri in requested)
         ):
             return _refuse_registration(conn, now, INVALID_REDIRECT_URI, 400)
         client = oauth_store.insert_client(
```

**Cost:** `pytest:tests/postgres/test_oauth_service.py::test_redirect_uri_refusals_commit_a_refused_event[trailing-slash]` — first observed failure line: `E       AssertionError: assert {'client_id': 'CaOgME6CpW6iNdFRG7qCSQ', 'client_id_issued_at': 1790934887, 'client_name': 'unnamed client', 'redirect_uris': ['https://claude.ai/api/mcp/auth_callback/'], ...} == OAuthError(error='invalid_redirect_uri', status=400, redirect=False, redirect_uri=None, state=None, iss=None)`; `pytest:tests/postgres/test_oauth_token_http.py::test_registration_refuses_a_redirect_uri_off_the_allowlist[redirect_uris0]` also failed, `E       assert 201 == 400`, 2 failed of 29

**Performed by:** mcpoauth-P13 (2026-10-02)

**Note:** the mutation is the near-miss the allowlist exists to refuse: comparing the URI
with its trailing slash stripped. Exactly the trailing-slash case went red in each file, and
every other refusal case stayed green, which is the evidence that the parametrised cases are
independent rather than one check repeated.

### Criterion 75

**Text:** "Registration beyond the per-source rate limit, and beyond the counted-client cap, is refused; a client with no grant, older than the configured age and with no sign-in in flight is removed by cleanup. Three cases survive cleanup: (a) a client whose grant's access token expired hours ago survives and its refresh still succeeds; (b) a client whose only grant is revoked or past its absolute end survives, is not counted toward the cap, and still lists with its client name; (c) a client with an authorization in flight survives. (FR 7, edge case 6)" (`build-plan.md:413-419`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_service.py::test_per_source_limit_refuses_and_commits_the_refusal`
- `pytest:tests/postgres/test_oauth_service.py::test_per_source_limit_counts_before_cleanup_deletes`
- `pytest:tests/postgres/test_oauth_service.py::test_counted_client_cap_refuses_and_commits_the_refusal`
- `pytest:tests/postgres/test_oauth_service.py::test_registration_cleans_up_abandoned_clients_only`
- `pytest:tests/postgres/test_oauth_service.py::test_granted_clients_survive_cleanup_and_dead_ones_are_not_counted`
- `pytest:tests/postgres/test_oauth_exchange.py::test_an_idle_connector_survives_cleanup_and_refreshes`
- `pytest:tests/postgres/test_oauth_token_http.py::test_registration_past_the_per_source_limit_is_429`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/oauth/service.py b/packages/core/src/rheo_core/oauth/service.py
index e09111f..d452543 100644
--- a/packages/core/src/rheo_core/oauth/service.py
+++ b/packages/core/src/rheo_core/oauth/service.py
@@ -208,7 +208,7 @@ def register_client(
         counted = oauth_store.count_counted_clients(
             conn, now=now, abandoned_before=abandoned_before
         )
-        if counted >= lifetimes.max_clients:
+        if counted > lifetimes.max_clients:
             return _refuse_registration(conn, now, TEMPORARILY_UNAVAILABLE, 429)
         if not isinstance(metadata, Mapping):
             return _refuse_registration(conn, now, INVALID_CLIENT_METADATA, 400)
```

**Cost:** `pytest:tests/postgres/test_oauth_service.py::test_counted_client_cap_refuses_and_commits_the_refusal` — first observed failure line: `E       AssertionError: assert {'client_id': 'ERx-OjvMdSzhn735aPgDJA', 'client_id_issued_at': 1790934911, 'client_name': 'unnamed client', 'redirect_uris': ['https://claude.ai/api/mcp/auth_callback'], ...} == OAuthError(error='temporarily_unavailable', status=429, redirect=False, redirect_uri=None, state=None, iss=None)`, 1 failed of 7

**Performed by:** mcpoauth-P13 (2026-10-02)

**Note:** the mutation is an off-by-one on the cap, letting one registration past it. The
per-source limit and the three survive-cleanup cases have their own demonstrators, which
stayed green under this mutation; it bites the cap clause only.

### Criterion 76

**Text:** "An authorization request is refused (page, no redirect to the client) for each of: unknown `client_id`, `redirect_uri` not exactly equal to a registered one; and refused with a redirected error for: `code_challenge_method` absent or `plain`, missing `code_challenge`, and missing or wrong `resource` (`invalid_target`). (FR 8)" (`build-plan.md:420-423`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_service.py::test_untrusted_redirect_errors_are_pages`
- `pytest:tests/postgres/test_oauth_service.py::test_redirectable_errors_carry_state_and_iss`
- `pytest:tests/postgres/test_oauth_http.py::test_an_unknown_client_or_unregistered_redirect_uri_is_a_page`
- `pytest:tests/postgres/test_oauth_http.py::test_a_bad_pkce_or_resource_redirects_with_error_state_and_iss`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/oauth/service.py b/packages/core/src/rheo_core/oauth/service.py
index e09111f..3c5063a 100644
--- a/packages/core/src/rheo_core/oauth/service.py
+++ b/packages/core/src/rheo_core/oauth/service.py
@@ -282,7 +282,7 @@ def begin_authorization(
         if (
             challenge is None
             or _CODE_CHALLENGE.fullmatch(challenge) is None
-            or params.get("code_challenge_method") != "S256"
+            or params.get("code_challenge_method") not in ("S256", "plain")
         ):
             return redirected(INVALID_REQUEST)
         resource = params.get("resource")
```

**Cost:** `pytest:tests/postgres/test_oauth_service.py::test_redirectable_errors_carry_state_and_iss[overrides2-invalid_request]` — first observed failure line: `E       AssertionError: assert 'dde122b14bfc63f9842ced229adbe279697a5fdfe6e99df5aac862170d114e85' == OAuthError(error='invalid_request', status=302, redirect=True, redirect_uri='https://claude.ai/api/mcp/auth_callback', state='opaque-state', iss='https://mcp.example.test')`, the `plain` case returning a request nonce instead of the redirected error; `pytest:tests/postgres/test_oauth_http.py::test_a_bad_pkce_or_resource_redirects_with_error_state_and_iss[subdomain-overrides1-invalid_request]` and its `[path-…]` twin also failed, `E       assert 303 == 302` (the browser was sent on to consent), 3 failed of 28

**Performed by:** mcpoauth-P13 (2026-10-02)

### Criterion 77

**Text:** "With no session cookie, an authorization request sends the browser through `/auth/login` and, after the test identity provider completes, lands on the consent screen; the GitHub OAuth callback URL used is the unchanged `url_for(IDENTITY, "/callback")`; a closed-signup unknown identity gets the existing refusal and no code is issued. (FR 9, edge case 7)" (`build-plan.md:424-427`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_http.py::test_no_session_goes_through_login_and_lands_on_consent`
- `pytest:tests/postgres/test_oauth_http.py::test_a_closed_signup_unknown_identity_gets_the_existing_refusal`

**Mutation:**
```diff
diff --git a/apps/core/src/rheo_app_core/oauth_routes.py b/apps/core/src/rheo_app_core/oauth_routes.py
index 2740b80..4389a8b 100644
--- a/apps/core/src/rheo_app_core/oauth_routes.py
+++ b/apps/core/src/rheo_app_core/oauth_routes.py
@@ -515,7 +515,7 @@ def consent(request: Request) -> Response:
         return _error_page(AUTHORIZATION_EXPIRED, 400)
     session = _session_row_from_cookie(request, _current_host(request))
     if session is None:
-        return_target = url_for(config, IDENTITY, _CONSENT_SUFFIX)
+        return_target = url_for(config, IDENTITY, "/")
         login = (
             f"{identity_path(config, _LOGIN_SUFFIX)}?"
             f"{urlencode({'return': return_target})}"
```

**Cost:** `pytest:tests/postgres/test_oauth_http.py::test_no_session_goes_through_login_and_lands_on_consent[subdomain]` — first observed failure line: `    | AssertionError: assert {'return': 'h...e.test/auth/'} == {'return': 'h...auth/consent'}` (inside the async test's exception group, which is why the line carries pytest's `|` gutter rather than `E`); the `[path]` case failed the same way, 2 failed

**Performed by:** mcpoauth-P13 (2026-10-02)

### Criterion 78

**Text:** "The consent screen shows the client name, the redirect URI's hostname, the account, the workspace and the operation set; it appears on a second authorization by the same client; Deny redirects with `access_denied` and issues nothing; an Allow `POST` with a bad `Origin` is refused `origin_not_allowed`. (FR 10)" (`build-plan.md:428-431`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_http.py::test_the_consent_page_shows_who_what_where_and_is_shown_every_time`
- `pytest:tests/postgres/test_oauth_http.py::test_deny_redirects_access_denied_and_issues_nothing`
- `pytest:tests/postgres/test_oauth_http.py::test_allow_with_a_bad_origin_is_refused_and_writes_nothing`

**Mutation:**
```diff
diff --git a/apps/core/src/rheo_app_core/oauth_routes.py b/apps/core/src/rheo_app_core/oauth_routes.py
index 2740b80..68a63c2 100644
--- a/apps/core/src/rheo_app_core/oauth_routes.py
+++ b/apps/core/src/rheo_app_core/oauth_routes.py
@@ -604,7 +604,7 @@ async def consent_decision(request: Request) -> Response:
         return _not_found_page()
     surface, config = resolved
     refused_origin = _check_origin(request, config)
-    if refused_origin is not None:
+    if refused_origin is not None and False:
         return refused_origin
     if not _is_form(request):
         return _error_page(INVALID_REQUEST, 400)
```

**Cost:** `pytest:tests/postgres/test_oauth_http.py::test_allow_with_a_bad_origin_is_refused_and_writes_nothing[None]` — first observed failure line: `E       assert 302 == 403`, an Allow with no `Origin` header redirecting with a code instead of being refused; the `[https://evil.example.net]` case failed the same way, 2 failed of 6

**Performed by:** mcpoauth-P13 (2026-10-02)

### Criterion 79

**Text:** "For an account with one active workspace the grant binds to it; for an account with two the screen offers exactly those two and binds the chosen one; an account with none issues nothing and answers `workspace_unselected`; a workspace id the account is not a member of, submitted in the Allow `POST`, is refused. (FR 11, edge case 9)" (`build-plan.md:432-435`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_service.py::test_one_workspace_binds`
- `pytest:tests/postgres/test_oauth_service.py::test_two_workspaces_offer_both_and_bind_the_chosen`
- `pytest:tests/postgres/test_oauth_service.py::test_no_workspace_is_workspace_unselected_and_decides_the_row`
- `pytest:tests/postgres/test_oauth_service.py::test_non_member_workspace_is_refused_and_writes_nothing`
- `pytest:tests/postgres/test_oauth_http.py::test_two_workspaces_offer_exactly_those_and_bind_the_chosen_one`
- `pytest:tests/postgres/test_oauth_http.py::test_an_account_with_no_workspace_is_workspace_unselected`
- `pytest:tests/postgres/test_oauth_http.py::test_a_workspace_the_account_is_not_a_member_of_is_refused`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/oauth/service.py b/packages/core/src/rheo_core/oauth/service.py
index e09111f..b1dcce4 100644
--- a/packages/core/src/rheo_core/oauth/service.py
+++ b/packages/core/src/rheo_core/oauth/service.py
@@ -477,7 +477,7 @@ def approve(
                 account_id=account_id,
             )
             return _page(WORKSPACE_UNSELECTED, 403)
-        if workspace_id not in {choice.workspace_id for choice in choices.workspaces}:
+        if workspace_id is None:
             return _page(NOT_A_MEMBER, 403)
         code = secrets.token_urlsafe(32)
         oauth_store.record_authorization_decision(
```

**Cost:** `pytest:tests/postgres/test_oauth_service.py::test_two_workspaces_offer_both_and_bind_the_chosen` — first observed failure line: `E       AssertionError: assert Approval(code='eCoJAIEozcJ2z93o-rYZMDAV1KeKo3B05KgYvbwuJco', redirect_uri='https://claude.ai/api/mcp/auth_callback', state='opaque-state') == OAuthError(error='not_a_member', status=403, redirect=False, redirect_uri=None, state=None, iss=None)`; `test_non_member_workspace_is_refused_and_writes_nothing` and `test_a_workspace_the_account_is_not_a_member_of_is_refused` (`E       assert 302 == 403`) also failed, 3 failed of 7

**Performed by:** mcpoauth-P13 (2026-10-02)

**Note:** the mutation keeps the refusal for an absent workspace id and drops the membership
test, so any submitted workspace id is approved. `test_non_member_workspace_is_refused_and_writes_nothing`
submits an id that names no workspace at all, and under the mutation it failed one step later,
on the `oauth_authorization` row's foreign key; the first observed failure above is the
service test that submits a real workspace the account does not belong to.

### Criterion 80

**Text:** "The token exchange returns an access token with prefix `rheo_mcp_`, whose `access_token` row has kind `mcp`, one account, one workspace, a non-null `expires_at`, an issuing-authority value distinct from `session`, `operator` and `runtime`, and snapshot rows equal to the `agent_default` expansion intersected with the member role's permitted set, minus the non-token-issuable set (for an owner, the full expansion minus that set); a test asserts none of the eight non-token-issuable operations is in the snapshot. (FR 12)" (`build-plan.md:436-441`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_issuance.py::test_owner_connector_token_row_and_full_snapshot`
- `pytest:tests/postgres/test_oauth_issuance.py::test_member_snapshot_is_agent_default_for_the_role`
- `pytest:tests/postgres/test_oauth_issuance.py::test_snapshot_strips_non_issuable_and_discover_from_the_expansion`
- `pytest:tests/postgres/test_oauth_exchange.py::test_redemption_issues_an_mcp_token_its_grant_and_a_refresh_token`
- `pytest:tests/postgres/test_oauth_migration.py::test_connector_issuer_inserts`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/tokens/issue.py b/packages/core/src/rheo_core/tokens/issue.py
index 2adc80f..169d32a 100644
--- a/packages/core/src/rheo_core/tokens/issue.py
+++ b/packages/core/src/rheo_core/tokens/issue.py
@@ -470,7 +470,6 @@ def connector_operations(role: Role) -> frozenset[str]:
     shown is what :func:`issue_connector_token` grants."""
     return (
         (agent_default() & _role_permitted_set(role))
-        - NON_TOKEN_ISSUABLE
         - {DISCOVER_THEN_CALL_OPERATION}
     )
 
```

**Cost:** `pytest:tests/postgres/test_oauth_issuance.py::test_snapshot_strips_non_issuable_and_discover_from_the_expansion` — first observed failure line: `E       AssertionError: assert 'core.token.issue' not in frozenset({'core.audit.list', 'core.operation.get', 'core.operation.list', 'core.token.issue', 'core.workspace.status', 'harness.note.get'})`, 1 failed of 5

**Performed by:** mcpoauth-P13 (2026-10-02)

**Note:** the shipped `agent_default` expansion contains none of the non-token-issuable
operations today, so dropping the subtraction changes nothing for a real owner or member
token, and the four other demonstrators stayed green. The third demonstrator widens the
expansion with a non-token-issuable operation and is the one that pins the subtraction; it
is the reason the clause cannot regress silently when `agent_default` grows.

### Criterion 81

**Text:** "The access token's lifetime equals the configured value and never exceeds `identity.token_max_days.mcp`; the refresh grant's absolute end never exceeds it either; a refresh at the cap answers `invalid_grant`. (FR 13, edge case 14)" (`build-plan.md:442-444`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_exchange.py::test_grant_days_above_the_mcp_cap_end_at_the_cap`
- `pytest:tests/postgres/test_oauth_exchange.py::test_access_expiry_never_passes_the_grant_end`
- `pytest:tests/postgres/test_oauth_exchange.py::test_refresh_at_the_grant_cap_is_refused_and_just_before_is_cut_to_it`
- `pytest:tests/postgres/test_oauth_issuance.py::test_expiry_beyond_the_mcp_cap_is_clamped`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/oauth/service.py b/packages/core/src/rheo_core/oauth/service.py
index e09111f..7597ffe 100644
--- a/packages/core/src/rheo_core/oauth/service.py
+++ b/packages/core/src/rheo_core/oauth/service.py
@@ -562,7 +562,7 @@ def grant_days(surface: OAuthSurface, workspace_id: UUID) -> int:
     workspace under its floor, as ``issue_handler`` reads it."""
     settings = resolve(workspace_id=workspace_id, source=PostgresOverrideSource())
     max_days = settings.get_int(TOKEN_MAX_DAYS_MCP_KEY)
-    return min(surface.lifetimes.grant_days, max_days)
+    return max(surface.lifetimes.grant_days, max_days)
 
 
 def _grant_end(surface: OAuthSurface, now: datetime, workspace_id: UUID) -> datetime:
```

**Cost:** `pytest:tests/postgres/test_oauth_exchange.py::test_grant_days_above_the_mcp_cap_end_at_the_cap` — first observed failure line: `E       AssertionError: assert datetime.datetime(2026, 11, 16, 9, 57, 57, 196776, tzinfo=zoneinfo.ZoneInfo(key='Etc/UTC')) == (datetime.datetime(2026, 10, 2, 9, 57, 57, 196776, tzinfo=datetime.timezone.utc) + datetime.timedelta(days=30))`, a 45-day grant against a 30-day cap; `test_access_expiry_never_passes_the_grant_end` also failed, 2 failed of 4

**Performed by:** mcpoauth-P13 (2026-10-02)

**Note:** the mutation inverts the cap from a minimum to a maximum in `grant_days`, the one
place the grant's lifetime is resolved. The refresh-at-the-cap demonstrator compares against
the grant end that was actually stored, so it stayed green; it pins the refusal at the end,
and this mutation moves the end.
