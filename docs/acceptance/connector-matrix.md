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

**Notes.** The file holds 24 rows, one for each of criteria 70-93, matching the spec's
AC-23 list. 23 of them have a demonstrator and a performed mutation. Criterion 91 is the
deliberate exception: a manual row, `deferred`, which the guard in
`tests/test_acceptance_matrix.py` allows by number and for no other criterion. AC-16 asks
that the existing `mcp` transport and token tests pass without edits. That was met with one
sanctioned additive line: `tests/postgres/test_tokens.py`'s `READ_ONLY_OPERATIONS` set gains
`core.oauth_event.list`, the read operation this change registers. In either file, no
existing case was edited or removed.

This file is registered in `tests/test_acceptance_matrix.py` with its expected set
`{70..93}`, so a missing, extra or duplicated row, a demonstrator that no longer resolves and
a hunk that no longer applies all fail the guard.

Every mutation below (criterion 91 has none) was applied to this working tree on
2026-10-02, its row's demonstrators
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
- `pytest:tests/postgres/test_oauth_boundary.py::test_every_advertised_url_answers_and_follows_url_for`

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

**Cost:** `pytest:tests/postgres/test_oauth_mount.py::test_unauthenticated_post_carries_the_resource_metadata_pointer[subdomain]` — first observed failure line: `E       assert 'Bearer' == 'Bearer resou...ted-resource"'`; the `[path]` case failed the same way against its own pointer, 2 failed. Re-run under the same mutation on adding the second demonstrator: `pytest:tests/postgres/test_oauth_boundary.py::test_every_advertised_url_answers_and_follows_url_for[subdomain]` — first observed failure line: `    | AssertionError: Headers({'content-type': 'application/json', 'content-length': '71', 'www-authenticate': 'Bearer'})` (the async exception group's `|` gutter), the pointer missing from the 401; the `[path]` case failed the same way, 2 failed

**Performed by:** mcpoauth-P13 (2026-10-02), mcpoauth-P14 (2026-10-02)

**Note:** the mutation drops the pointer from the no-bearer refusal only. The refusal for a
presented-but-bad bearer keeps it, so this row and criterion 72's are reddened by different
lines of the same gate. The first demonstrator compares the header with the surface's own
computed URL; the second derives the expected pointer from `url_for` itself (RFC 8414
insertion) and walks discovery from it, so the `url_for` clause does not rest on the
surface agreeing with itself.

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

### Criterion 82

**Text:** "Using a refresh token returns a new access token and a new refresh token and the old refresh token then answers `invalid_grant`; presenting the old one a second time (outside any recorded grace) revokes the grant so that the newest access token is refused `token_revoked`; presenting it inside the grace answers `invalid_grant` and changes nothing. The test re-reads the database after each refusal and asserts the written state (the revocation and the `refresh_reuse_revoked` row exist after the reuse; nothing changed after the grace-window refusal). (FR 13, edge case 4)" (`build-plan.md:445-451`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_exchange.py::test_refresh_rotates_both_values_in_place`
- `pytest:tests/postgres/test_oauth_exchange.py::test_reuse_inside_the_grace_changes_nothing`
- `pytest:tests/postgres/test_oauth_exchange.py::test_reuse_after_the_grace_revokes_the_grant`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/oauth/service.py b/packages/core/src/rheo_core/oauth/service.py
index d9fd1e0..b6430dd 100644
--- a/packages/core/src/rheo_core/oauth/service.py
+++ b/packages/core/src/rheo_core/oauth/service.py
@@ -743,7 +743,7 @@ def refresh(
             grace = timedelta(seconds=surface.lifetimes.refresh_grace_seconds)
             if now - row.rotated_at < grace:
                 return _token_error(INVALID_GRANT)
-            ended = _revoke_if_live(conn, row.token_id, now)
+            ended = False
             token = get_access_token(conn, row.token_id)
             # grant_revoked only when this reuse ended a live grant, the rule the
             # code replay and ``revoke_handler`` follow.
```

**Cost:** `pytest:tests/postgres/test_oauth_exchange.py::test_reuse_after_the_grace_revokes_the_grant` — first observed failure line: `E       AssertionError: assert None is not None`, the grant's access token still carrying a null `revoked_at` after the old refresh value was presented past the grace, 1 failed of 3 (re-captured and re-run against `f67cee4`, where the revocation moved behind `revoke_access_token_if_live`)

**Performed by:** mcpoauth-P14 (2026-10-02)

**Note:** the mutation keeps the `refresh_reuse_revoked` row and the `invalid_grant` answer
and drops only the revocation, which is the half of the clause a reader of the response alone
cannot see. That is why the demonstrator re-reads the `access_token` row. The rotation and the
inside-the-grace refusal have their own demonstrators, and both stayed green.

### Criterion 83

**Text:** "An authorization code redeems once; a second redemption answers `invalid_grant` and revokes what the first issued; redemption with a wrong or missing `code_verifier`, a different `client_id`, a different `redirect_uri`, or after 60 s answers `invalid_grant`/`invalid_client`, and with a present `resource` that is not the canonical identifier answers `invalid_target`, in each case issuing nothing; an absent `resource` redeems successfully; failed redemptions leave the code burned (a later correct redemption of the same code answers `invalid_grant`); the token endpoint accepts `application/x-www-form-urlencoded`. The test re-reads the database after each state-changing refusal and asserts the burn, and for a replay the revocation and its event row. (FR 14, edge case 3)" (`build-plan.md:452-461`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_exchange.py::test_replay_revokes_what_the_first_redemption_issued`
- `pytest:tests/postgres/test_oauth_exchange.py::test_a_failed_redemption_burns_the_code_and_issues_nothing`
- `pytest:tests/postgres/test_oauth_exchange.py::test_unknown_or_missing_code_is_invalid_grant`
- `pytest:tests/postgres/test_oauth_exchange.py::test_absent_or_bound_resource_redeems`
- `pytest:tests/postgres/test_oauth_exchange.py::test_code_at_59_seconds_redeems`
- `pytest:tests/postgres/test_oauth_token_http.py::test_another_client_redeeming_the_code_is_401_invalid_client`
- `pytest:tests/postgres/test_oauth_token_http.py::test_token_requires_a_form_content_type`
- `pytest:tests/postgres/test_oauth_token_http.py::test_register_redeem_call_refresh_over_http`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/oauth/service.py b/packages/core/src/rheo_core/oauth/service.py
index d9fd1e0..dd98ebc 100644
--- a/packages/core/src/rheo_core/oauth/service.py
+++ b/packages/core/src/rheo_core/oauth/service.py
@@ -601,7 +601,7 @@ def _refuse_code_replay(
 ) -> OAuthError:
     """A second redemption: revoke what the first issued, record the refusal and
     (when this ended the grant) ``grant_revoked``, all committed with the error."""
-    ended = row.token_id is not None and _revoke_if_live(conn, row.token_id, now)
+    ended = False
     events = [(CODE_REDEEMED, REFUSED)]
     if ended:
         events.append((GRANT_REVOKED, SUCCEEDED))
```

**Cost:** `pytest:tests/postgres/test_oauth_exchange.py::test_replay_revokes_what_the_first_redemption_issued` — first observed failure line: `E       AssertionError: assert None is not None`, the first redemption's access token left unrevoked by the replay, 1 failed of 22 (re-captured and re-run against `f67cee4`, where the revocation moved behind `revoke_access_token_if_live`)

**Performed by:** mcpoauth-P14 (2026-10-02)

**Note:** the replay still answers `invalid_grant` and still writes its refused
`code_redeemed` row under this mutation; only the revocation of what the first redemption
issued is gone. The burn, the verifier, client, redirect, expiry and `resource` refusals, the
absent-`resource` success and the form content type are pinned by the other demonstrators,
which stayed green.

### Criterion 84

**Text:** "A connector access token presented to the `api` surface answers `token_wrong_kind`; presented to the `mcp` surface it lists the `agent_default` tools and a call to a tool outside its snapshot answers `operation_not_permitted`; a call is dispatched through the same boundary function as a token minted by `rheo token issue` (a test patches the boundary and sees the one call). (FR 15)" (`build-plan.md:462-466`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_boundary.py::test_a_connector_token_is_mcp_only_and_dispatches_like_an_issued_one`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/tokens/issue.py b/packages/core/src/rheo_core/tokens/issue.py
index 2adc80f..05d67d2 100644
--- a/packages/core/src/rheo_core/tokens/issue.py
+++ b/packages/core/src/rheo_core/tokens/issue.py
@@ -523,7 +523,7 @@ def issue_connector_token(
         conn,
         account_id=account_id,
         workspace_id=workspace_id,
-        kind="mcp",
+        kind="cli",
         issued_from=CONNECTOR_ISSUED_FROM,
         token_hash=hashlib.sha256(raw).digest(),
         set_name="agent_default",
```

**Cost:** `pytest:tests/postgres/test_oauth_boundary.py::test_a_connector_token_is_mcp_only_and_dispatches_like_an_issued_one[subdomain]` — first observed failure line: `E           AssertionError: {"state":"succeeded","operation_id":null,"result":{"core_version":"0.1.0","core_contract_version":1,"modules":[]}}`, the message of `assert 200 == 401` at `tests/postgres/test_oauth_boundary.py:396`: the connector token was served on the `api` surface; the `[path]` case failed the same way, 2 failed

**Performed by:** mcpoauth-P14 (2026-10-02)

**Note:** the criterion's text says a call to a tool outside the snapshot answers
`operation_not_permitted`. Over MCP it does not, and the demonstrator asserts what ships: the
MCP facade answers a tool outside the token's listing with its own result (a `200` with
`isError` and state `not_found`) and dispatches nothing. `operation_not_permitted` is what the
boundary answers when an operation outside the snapshot is dispatched directly with the
connector token's own context, and the same test asserts that too. The boundary spy sees
exactly one dispatch per call, for the connector token and for a `rheo token issue` token
alike.

### Criterion 85

**Text:** "Three regression tests, one per existing caller, pass without edits to the existing tests: (a) a Claude-Code-style `mcp` token lists and calls Recallatron tools; (b) the bridge's `cli` token calls `core.evidence.ingest` on the `api` surface; (c) a dedicated `agent_default` `mcp` token lists and calls tools. Each compares the response to the pre-change golden response, normalizing only a named, closed list: the `date` and `content-length` headers, any MCP session-id header, and body values at keys `id`, `*_id` and `*_at`; everything else is compared exactly. The existing `mcp` transport and token test files are unmodified by the change except for added cases. (FR 16, edge case 12)" (`build-plan.md:467-474`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_existing_bearer_regression.py::test_a_claude_code_token_lists_and_recalls_as_before`
- `pytest:tests/postgres/test_existing_bearer_regression.py::test_a_bridge_token_ingests_as_before`
- `pytest:tests/postgres/test_existing_bearer_regression.py::test_a_box_bot_token_lists_and_calls_as_before`
- `pytest:tests/postgres/test_existing_bearer_regression.py::test_an_unauthenticated_post_is_a_bare_bearer_401_as_before`
- `pytest:tests/postgres/test_existing_bearer_regression.py::test_the_guard_admits_only_the_listed_values`
- `pytest:tests/postgres/test_existing_bearer_regression.py::test_an_object_under_a_listed_key_is_still_compared`

**Mutation:**
```diff
diff --git a/apps/mcp/src/rheo_app_mcp/transport.py b/apps/mcp/src/rheo_app_mcp/transport.py
index 94489b1..3cc0d20 100644
--- a/apps/mcp/src/rheo_app_mcp/transport.py
+++ b/apps/mcp/src/rheo_app_mcp/transport.py
@@ -132,7 +132,7 @@ def _bearer_from(scope: Scope) -> str | None:
     return None
 
 
-BARE_CHALLENGE: Final = b"Bearer"
+BARE_CHALLENGE: Final = b'Bearer realm="mcp"'
 """The ``WWW-Authenticate`` value when no OAuth surface is configured: exactly the
 header this gate answered with before issue #287."""
 
```

**Cost:** `pytest:tests/postgres/test_existing_bearer_regression.py::test_an_unauthenticated_post_is_a_bare_bearer_401_as_before` — first observed failure line: `E       assert {'exchanges':...presented'}}]} == {'exchanges':...presented'}}]}`, whose diff shows `['www-authenticate', 'Bearer realm="mcp"']` against the golden's `['www-authenticate', 'Bearer']`, 1 failed of 9

**Performed by:** mcpoauth-P14 (2026-10-02)

**Note:** the mutation changes one token of the one header this change touched on the
existing path, and the golden comparison catches it: everything outside the closed
normalisation list is compared exactly, and the two guard tests pin that list. The three
callers' own goldens stayed green under it, as they should, because a served request never
carries a refusal header. "Without edits to the existing tests" holds with one sanctioned
additive line: `tests/postgres/test_tokens.py` gains `core.oauth_event.list` in its
`READ_ONLY_OPERATIONS` set, because this change registers that read operation. No case in
that file or in `tests/postgres/test_mcp_transport.py` was edited or removed; the transport
file only gained cases.

### Criterion 86

**Text:** "A full registration, authorization, exchange, call and refresh flow is run against a test log sink, and a scan of every captured log line, every audit row, every `operation` row and every error body finds none of: the access token, the refresh token, the code, the verifier, the challenge. A test also asserts that presenting a bearer in a query string on the `mcp` and `api` surfaces is not accepted. (FR 17, edge case 13)" (`build-plan.md:475-479`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_boundary.py::test_a_full_flow_leaves_no_secret_in_logs_rows_or_error_bodies`
- `pytest:tests/test_log_redaction.py::test_the_query_is_stripped_and_the_path_kept`
- `pytest:tests/test_log_redaction.py::test_a_target_without_a_query_is_untouched`
- `pytest:tests/test_log_redaction.py::test_an_unrecognised_shape_fails_closed`
- `pytest:tests/test_log_redaction.py::test_a_preformatted_message_fails_closed`
- `pytest:tests/test_log_redaction.py::test_attaching_twice_adds_one_filter`
- `pytest:tests/test_log_redaction.py::test_serve_configs_leave_the_filter_on_the_access_logger`
- `pytest:tests/test_log_redaction.py::test_a_real_server_logs_the_request_without_its_query`
- `pytest:tests/postgres/test_oauth_token_http.py::test_a_code_in_the_query_string_is_never_read`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/log_config.py b/packages/core/src/rheo_core/log_config.py
index 07cd0ff..d6058b8 100644
--- a/packages/core/src/rheo_core/log_config.py
+++ b/packages/core/src/rheo_core/log_config.py
@@ -144,7 +144,7 @@ class RedactQueryFilter(logging.Filter):
             and isinstance(args[_TARGET_INDEX], str)
         ):
             target = args[_TARGET_INDEX]
-            if "?" in target:
+            if "?" in target and False:
                 record.args = (
                     *args[:_TARGET_INDEX],
                     target.split("?", 1)[0],
```

**Cost:** `pytest:tests/postgres/test_oauth_boundary.py::test_a_full_flow_leaves_no_secret_in_logs_rows_or_error_bodies[subdomain]` — first observed failure line: `E           AssertionError: /auth/oauth/authorize`, the message of `assert lines, path`: no access line for the authorize request was left without its query; the `[path]` case failed the same way, and `test_the_query_is_stripped_and_the_path_kept`, `test_serve_configs_leave_the_filter_on_the_access_logger` and `test_a_real_server_logs_the_request_without_its_query` failed in every case, 9 failed of 18. That mutation first trips the full-flow test's structural check on the access lines, not its secret scan, so a second mutation was performed and reverted that leaks the authorization code into an application log line during redemption (the hunk is in the Note): `pytest:tests/postgres/test_oauth_boundary.py::test_a_full_flow_leaves_no_secret_in_logs_rows_or_error_bodies[subdomain]` — first observed failure line: `E               AssertionError: a secret appears in a log line: {"timestamp": "2026-10-02 03:58:12,953", "level": "WARNING", "logger": "rheo_core.oauth.service", "message": "redeeming code UueaLbDQ-Z0uILKoBdQljZQpJpqOtr7uZgjAeM6S40M"}`, raised by the scan at `tests/postgres/test_oauth_boundary.py:532`; the `[path]` case failed the same way, 2 failed

**Performed by:** mcpoauth-P14 (2026-10-02)

**Note:** the query-string clause is asserted inside the full-flow test: a valid access
token sent as `access_token` or `token` in the query, on the `mcp` and on the `api` surface,
answers `401` with `token_malformed`, while the same value in the header is served. The
mutation leaves that path alone; it removes the access-log redaction, which is the place a
code, challenge or GitHub code would otherwise reach a log line. The second mutation,
captured the same way and checked with `git apply --check`, shows the secret scan itself
bites on a captured log line:

```diff
diff --git a/packages/core/src/rheo_core/oauth/service.py b/packages/core/src/rheo_core/oauth/service.py
index e09111f..4ad3926 100644
--- a/packages/core/src/rheo_core/oauth/service.py
+++ b/packages/core/src/rheo_core/oauth/service.py
@@ -643,6 +643,7 @@ def exchange_code(
     ``token_id`` and ``code_redeemed``. The access value expires at ``min(now +
     access_token_minutes, grant_expires_at)``."""
     code = form.get("code")
+    __import__("logging").getLogger(__name__).warning("redeeming code %s", code)
     with get_backend().control_engine.begin() as conn:
         row = (
             oauth_store.get_authorization_by_code_hash(
```

### Criterion 87

**Text:** "`rheo token list` shows every connector grant with the fields FR 18 names; `rheo token revoke <id>` makes the grant's next `mcp` call answer 401 with the pointer and makes its refresh answer `invalid_grant`; revoking one of two grants for one account leaves the other working. (FR 18, edge cases 10, 11)" (`build-plan.md:480-483`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_cli.py::test_list_shows_each_connector_grant_with_its_fields_and_no_secret`
- `pytest:tests/postgres/test_oauth_cli.py::test_list_narrows_by_workspace_and_shows_dashes_for_an_operator_token`
- `pytest:tests/postgres/test_oauth_cli.py::test_revoking_one_of_two_grants_kills_it_and_leaves_the_other`
- `pytest:tests/postgres/test_oauth_cli.py::test_a_revoked_grant_still_lists_with_its_name_after_cleanup`

**Mutation:**
```diff
diff --git a/apps/cli/src/rheo_app_cli/commands/token.py b/apps/cli/src/rheo_app_cli/commands/token.py
index f7120b9..6324985 100644
--- a/apps/cli/src/rheo_app_cli/commands/token.py
+++ b/apps/cli/src/rheo_app_cli/commands/token.py
@@ -219,7 +219,7 @@ def list_tokens(args: argparse.Namespace) -> int:
             row.created_at,
             row.expires_at,
             row.last_used_at,
-            row.revoked_at,
+            None,
             None
             if grant is None
             else sanitize_client_name(
```

**Cost:** `pytest:tests/postgres/test_oauth_cli.py::test_revoking_one_of_two_grants_kills_it_and_leaves_the_other` — first observed failure line: `E       AssertionError: assert '-' != '-'`, the revoked grant listing with no revocation time; `test_a_revoked_grant_still_lists_with_its_name_after_cleanup` failed the same way, 2 failed of 4

**Performed by:** mcpoauth-P14 (2026-10-02)

**Note:** in the revoke demonstrator the dead `mcp` call (401 with the pointer), the dead
refresh (`invalid_grant`) and the surviving second grant are asserted before the listing,
and they passed under this mutation: revocation itself is the shipped `core.token.revoke`.
The mutation bites the listing's `revoked` column, the field FR 18 adds the revocation to.

### Criterion 88

**Text:** "`rheo doctor` output includes connector-grant counts and the OAuth-configured line, contains no token, client name or other client-supplied text, and goes `FAIL` for the counted-client cap and missing-refresh-row cases. (FR 19)" (`build-plan.md:484-486`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_cli.py::test_the_oauth_surface_line_is_ok_disabled_ok_configured_or_fail`
- `pytest:tests/postgres/test_oauth_cli.py::test_connector_grants_fail_at_the_counted_client_cap`
- `pytest:tests/postgres/test_oauth_cli.py::test_revoked_or_expired_grants_keep_their_clients_below_the_cap`
- `pytest:tests/postgres/test_oauth_cli.py::test_a_live_grant_with_no_refresh_row_fails`
- `pytest:tests/postgres/test_oauth_cli.py::test_doctor_shows_both_lines_and_no_client_text`

**Mutation:**
```diff
diff --git a/apps/cli/src/rheo_app_cli/commands/doctor.py b/apps/cli/src/rheo_app_cli/commands/doctor.py
index c63ed01..7b057e6 100644
--- a/apps/cli/src/rheo_app_cli/commands/doctor.py
+++ b/apps/cli/src/rheo_app_cli/commands/doctor.py
@@ -424,7 +424,7 @@ def _connector_grants_check(counts: ConnectorGrantCounts, max_clients: int) -> C
         f"refresh row {counts.missing_refresh}"
     )
     problems = []
-    if counts.counted_clients >= max_clients:
+    if counts.counted_clients > max_clients:
         problems.append("client cap reached, new registrations are refused")
     if counts.missing_refresh:
         problems.append("a live grant cannot refresh")
```

**Cost:** `pytest:tests/postgres/test_oauth_cli.py::test_connector_grants_fail_at_the_counted_client_cap` — first observed failure line: `E       AssertionError: Check(name='connector grants', level='ok', detail='clients 1/1; grants active 0, expired 0, revoked 0, ending within 7 days 0; live grants with no live refresh row 0')`, the message of `assert 'ok' == 'FAIL'`, 1 failed of 5

**Performed by:** mcpoauth-P14 (2026-10-02)

### Criterion 89

**Text:** "Each audit event FR 20 lists is written exactly once for a scripted flow, each row carries account, workspace, client id, grant id and an outcome word, and a deny and a refresh-reuse each produce their own row. The test re-reads the database after each refusal (deny, replay, refresh-reuse, refused registration) and asserts the row exists, so a refused request still leaves its event. (FR 20)" (`build-plan.md:487-491`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_exchange.py::test_a_scripted_flow_writes_each_event_once`
- `pytest:tests/postgres/test_oauth_exchange.py::test_reuse_after_the_grace_revokes_the_grant`
- `pytest:tests/postgres/test_oauth_exchange.py::test_replay_revokes_what_the_first_redemption_issued`
- `pytest:tests/postgres/test_oauth_service.py::test_redirect_uri_refusals_commit_a_refused_event`
- `pytest:tests/postgres/test_oauth_http.py::test_deny_redirects_access_denied_and_issues_nothing`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/oauth/service.py b/packages/core/src/rheo_core/oauth/service.py
index e09111f..6025dc7 100644
--- a/packages/core/src/rheo_core/oauth/service.py
+++ b/packages/core/src/rheo_core/oauth/service.py
@@ -761,7 +761,7 @@ def refresh(
                     occurred_at=now,
                     client_id=None if grant is None else grant.client_id,
                     account_id=None if token is None else token.account_id,
-                    workspace_id=None if token is None else token.workspace_id,
+                    workspace_id=None,
                     token_id=row.token_id,
                 )
             return _token_error(INVALID_GRANT)
```

**Cost:** `pytest:tests/postgres/test_oauth_exchange.py::test_a_scripted_flow_writes_each_event_once` — first observed failure line: `E           AssertionError: refresh_reuse_revoked`, the message of the per-event `(account_id, workspace_id, token_id)` comparison, `At index 1 diff: None != UUID(...)`; `test_reuse_after_the_grace_revokes_the_grant` also failed, `E       AssertionError: assert None == UUID('01a0fc16-f23f-7e0a-b61a-db51858bdedd')`, 2 failed of 18

**Performed by:** mcpoauth-P14 (2026-10-02)

**Note:** the mutation keeps every event and drops one field from two of them (the
refresh-reuse refusal and the `grant_revoked` it writes). It is caught because the scripted
flow checks the fields of each row, not only that the row exists. The deny, replay and
refused-registration rows are re-read by their own demonstrators, which stayed green.

### Criterion 90

**Text:** "With the feature unconfigured (off, or enabled but incomplete) the `mcp` surface answers today's bare `Bearer` 401, the well-known paths and `/auth/oauth/*` answer 404, and doctor shows `FAIL` for the enabled-but-incomplete case; with it configured no advertised endpoint answers 404 or 500; the same suite passes in path mode with the endpoints built through `url_for`, and a route string naming a host or prefix outside `url_for` fails the existing lint. (FR 21)" (`build-plan.md:492-497`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_boundary.py::test_an_unconfigured_deployment_advertises_nothing`
- `pytest:tests/postgres/test_oauth_boundary.py::test_every_advertised_url_answers_and_follows_url_for`
- `pytest:tests/postgres/test_oauth_mount.py::test_unconfigured_mount_is_unchanged`
- `pytest:tests/postgres/test_oauth_http.py::test_unconfigured_the_browser_routes_are_404`
- `pytest:tests/postgres/test_oauth_token_http.py::test_unconfigured_routes_are_404`
- `pytest:tests/test_oauth_surface.py::test_unconfigured_reasons`
- `pytest:tests/postgres/test_oauth_cli.py::test_the_oauth_surface_line_is_ok_disabled_ok_configured_or_fail`
- `ci:web / Routing-literal gate (criterion 22)`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/oauth/surface.py b/packages/core/src/rheo_core/oauth/surface.py
index a21b17f..2c179f7 100644
--- a/packages/core/src/rheo_core/oauth/surface.py
+++ b/packages/core/src/rheo_core/oauth/surface.py
@@ -191,9 +191,7 @@ def oauth_surface(
     """The configured surface, or why there is none (module docstring)."""
     if not settings.get_bool(ENABLED_KEY):
         return OAuthUnconfigured("disabled")
-    if not settings.get_bool(_GITHUB_ENABLED_KEY) or not settings.get_str(
-        _GITHUB_CLIENT_ID_KEY
-    ):
+    if not settings.get_str(_GITHUB_CLIENT_ID_KEY):
         return OAuthUnconfigured("identity_provider_disabled")
     redirect_uris = settings.get_list(REDIRECT_URIS_KEY)
     if not redirect_uris:
```

**Cost:** `pytest:tests/test_oauth_surface.py::test_unconfigured_reasons[subdomain-overrides1-identity_provider_disabled]` — first observed failure line: `E       AssertionError: assert OAuthSurface(resource='https://mcp.example.test/', issuer='https://mcp.example.test', authorization_endpoint='https://...conds=10, max_clients=100, registrations_per_source_per_hour=30, abandoned_client_minutes=60), accepts_empty_path=True) == OAuthUnconfigured(reason='identity_provider_disabled')`; the `[path-…]` twin failed the same way, 2 failed of 32

**Performed by:** mcpoauth-P14 (2026-10-02)

**Note:** the mutation lets an enabled deployment with GitHub sign-in switched off count as
configured while a GitHub client id is set. The boundary demonstrator's incomplete case
stayed green under it, because that test's configuring context closes before the probes and
leaves no client id, so it reaches the same refusal through the clause the mutation kept;
the surface unit test sets the client id and isolates the GitHub switch. The lint clause is
the shipped routing-literal gate, which also scans the new routes, built from
`SERVED_IDENTITY_PATH` and `url_for` rather than literals.

### Criterion 91

**Text:** "**Manual, once, by the maintainer after deploy and on his go:** adding the connector `https://mcp.rheo.stream/` in claude.ai (desktop chat), then using it from claude.ai on the web and on the phone, each completes sign-in through GitHub on `auth.rheo.stream` (or reuses the account's connection, per S5), lists the Recallatron tools, and one `recallatron_recall` call returns a result from his own workspace. The result is recorded in the run's closeout and the matrix's manual row; it is not claimed by any CI test. (Outcome metric)" (`build-plan.md:498-503`)

**State:** deferred

**Demonstrator:** none

**Mutation:** none

**Cost:** none

**Performed by:** none

**Note:** this check is manual. The maintainer performs it once, after the merge and after
`RHEO__identity__oauth__enabled=true` is set on the deployment: he adds the connector
`https://mcp.rheo.stream/` in claude.ai desktop chat, then uses it from claude.ai on the web
and from the phone. The result is written into this row when it exists. No CI test claims
it, and the guard allows this one deferred row by number (`DEFERRED_CRITERIA` in
`tests/test_acceptance_matrix.py`).

### Criterion 92

**Text:** "The `oauth_event` reader (e.g. `rheo token events`), run after a scripted flow that includes a registration, a grant, a deny, a code redemption, a refresh and a refresh-reuse revocation, lists those events newest first; every row shows only the content-free fields (event, outcome word, time, account, workspace, client id, grant id); a scan of its output finds none of the access token, refresh token, code, verifier, challenge or any client-supplied free text (including the client name); and a caller who is not the operator/owner is refused. (FR 20)" (`build-plan.md:504-510`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_event_list.py::test_the_operator_reads_the_flow_newest_first_with_the_unbound_rows`
- `pytest:tests/postgres/test_oauth_event_list.py::test_each_record_carries_exactly_the_eight_content_free_fields`
- `pytest:tests/postgres/test_oauth_event_list.py::test_an_owner_session_sees_its_workspace_and_no_unbound_row`
- `pytest:tests/postgres/test_oauth_event_list.py::test_a_member_is_refused_role_not_permitted`
- `pytest:tests/postgres/test_oauth_event_list.py::test_another_workspace_s_rows_are_never_shown`
- `pytest:tests/postgres/test_oauth_cli.py::test_events_prints_seven_columns_newest_first_and_no_secret`
- `pytest:tests/postgres/test_oauth_cli.py::test_a_refused_events_dispatch_exits_one_with_the_state`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/storage/oauth_store.py b/packages/core/src/rheo_core/storage/oauth_store.py
index 4cfa204..bb4307a 100644
--- a/packages/core/src/rheo_core/storage/oauth_store.py
+++ b/packages/core/src/rheo_core/storage/oauth_store.py
@@ -573,7 +573,7 @@ def list_oauth_events(
     statement = (
         select(event)
         .where(scope)
-        .order_by(event.c.occurred_at.desc(), event.c.id.desc())
+        .order_by(event.c.occurred_at, event.c.id)
         .limit(limit)
     )
     return tuple(
```

**Cost:** `pytest:tests/postgres/test_oauth_event_list.py::test_the_operator_reads_the_flow_newest_first_with_the_unbound_rows` — first observed failure line: `E       AssertionError: assert [(datetime.da...e7dbb')), ...] == [(datetime.da...6eeed')), ...]`, the oldest event listed first; `test_an_owner_session_sees_its_workspace_and_no_unbound_row` and `test_events_prints_seven_columns_newest_first_and_no_secret` also failed, 3 failed of 10

**Performed by:** mcpoauth-P14 (2026-10-02)

**Note:** the run under this mutation also included the module's two `limit` tests, which
stayed green; they are not listed above because they pin the input bound, not this
criterion.

### Criterion 93

**Text:** "A pending authorization cannot be completed late, from another browser, or twice (edge case 8; FR 9, 10). A test drives each case and, after every refusal, re-reads the database and asserts that no code was issued (`code_hash` and `code_expires_at` null on the row), no `access_token` or `oauth_grant` row was created, and the row is unchanged. (a) **Expired:** with the clock at or after the `oauth_authorization` row's `expires_at` (created plus 10 minutes), `GET /auth/oauth/consent` with the correct `rheo_oauth_request` cookie answers the `authorization_expired` page (`data-state="authorization_expired"`), and an Allow `POST` answers the same page; nothing is issued. (b) **Another browser:** with a live row, `GET /auth/oauth/consent` with no `rheo_oauth_request` cookie, and with a cookie whose nonce hashes to no row, answers `authorization_expired` and issues nothing; a session that holds a different, live pending row of its own and posts the first row's `request_id` is refused `authorization_expired` (the cookie's row id must equal `request_id`); an Allow `POST` whose `request_id` is not the id of the row named by the cookie is refused `authorization_expired`. (c) **Already decided:** after Deny, and after an Allow that issued a code, replaying the same cookie value against `GET` and `POST /auth/oauth/consent` answers `authorization_expired` and issues no second code and no second approval or denial event; the replayed Allow does not change the code the first Allow issued. Boundary cases: at 1 second before `expires_at` the row is still live and consent succeeds; at `expires_at` exactly it is refused; the `Set-Cookie` for `rheo_oauth_request` carries `Max-Age=600` (the same 10 minutes), so a browser that has dropped the cookie at Max-Age is the absent-cookie case in (b). (FR 9, 10; edge case 8)" (`build-plan.md:511-531`)

**State:** complete

**Demonstrator:**
- `pytest:tests/postgres/test_oauth_http.py::test_at_expires_at_the_consent_is_authorization_expired`
- `pytest:tests/postgres/test_oauth_http.py::test_one_second_before_expires_at_consent_succeeds`
- `pytest:tests/postgres/test_oauth_http.py::test_another_browser_without_the_cookie_gets_authorization_expired`
- `pytest:tests/postgres/test_oauth_http.py::test_a_session_holding_its_own_row_cannot_approve_another_rows_id`
- `pytest:tests/postgres/test_oauth_http.py::test_a_denied_request_cannot_be_replayed`
- `pytest:tests/postgres/test_oauth_http.py::test_an_approved_request_cannot_be_replayed_and_keeps_its_code`
- `pytest:tests/postgres/test_oauth_service.py::test_pending_boundaries`
- `pytest:tests/postgres/test_oauth_service.py::test_expired_request_is_refused_at_expiry_and_live_one_second_before`
- `pytest:tests/postgres/test_oauth_service.py::test_missing_or_unknown_cookie_is_refused`
- `pytest:tests/postgres/test_oauth_service.py::test_another_rows_id_is_refused`
- `pytest:tests/postgres/test_oauth_service.py::test_denied_request_cannot_be_replayed`
- `pytest:tests/postgres/test_oauth_service.py::test_approved_request_cannot_be_replayed`
- `pytest:tests/postgres/test_oauth_migration.py::test_the_oauth_request_cookie_lives_ten_minutes`
- `pytest:tests/postgres/test_oauth_http.py::test_no_session_goes_through_login_and_lands_on_consent`

**Mutation:**
```diff
diff --git a/packages/core/src/rheo_core/oauth/service.py b/packages/core/src/rheo_core/oauth/service.py
index e09111f..7cce1f1 100644
--- a/packages/core/src/rheo_core/oauth/service.py
+++ b/packages/core/src/rheo_core/oauth/service.py
@@ -312,7 +312,7 @@ def _pending(
     row = oauth_store.get_authorization_by_request_hash(
         conn, _sha256(request_nonce), for_update=for_update
     )
-    if row is None or row.decided_at is not None or now >= row.expires_at:
+    if row is None or row.decided_at is not None or now > row.expires_at:
         return None
     return row
 
```

**Cost:** `pytest:tests/postgres/test_oauth_http.py::test_at_expires_at_the_consent_is_authorization_expired[subdomain-GET]` — first observed failure line: `E       AssertionError: <!doctype html>`, the message of `assert 200 == 400`: the consent page was served at `expires_at` exactly; the other three cases of that test, `test_pending_boundaries` and `test_expired_request_is_refused_at_expiry_and_live_one_second_before` also failed, 6 failed of 23. Additional mutation, performed and reverted: dropping the `request_id` == cookie-row-id check in the Allow and Deny `POST` (the hunk is in the Note) reddened `pytest:tests/postgres/test_oauth_http.py::test_a_session_holding_its_own_row_cannot_approve_another_rows_id[subdomain]` — first observed failure line: `E       assert 302 == 400`: a `POST` naming the first row's id approved the cookie's own row, because the mutated `_decide` passes `row.id` to `approve`; the `[path]` case failed the same way, 2 failed of 23

**Performed by:** mcpoauth-P14 (2026-10-02)

**Note:** the primary hunk is in `_pending`, the one rule `pending()`, `approve` and `deny`
all read, so moving the boundary by one tick is caught at the exact-boundary case on both
the page and the service; the one-second-before demonstrators stayed green. The second
mutation, captured the same way and checked with `git apply --check`:

```diff
diff --git a/apps/core/src/rheo_app_core/oauth_routes.py b/apps/core/src/rheo_app_core/oauth_routes.py
index 2740b80..7f9f978 100644
--- a/apps/core/src/rheo_app_core/oauth_routes.py
+++ b/apps/core/src/rheo_app_core/oauth_routes.py
@@ -552,7 +552,7 @@ def _decide(
     nonce = request.cookies.get(OAUTH_REQUEST_COOKIE)
     now = _now()
     row = pending(nonce, now)
-    if row is None or _uuid_or_none(form.get("request_id")) != row.id:
+    if row is None:
         return _error_page(AUTHORIZATION_EXPIRED, 400)
     session = _session_row_from_cookie(request, _current_host(request))
     if session is None:
```

The service-level `test_another_rows_id_is_refused` stayed green under it, because
`approve` keeps its own `row.id != row_id` check; the route's check is what stops a browser
holding its own pending row from posting another row's id.
`test_no_session_goes_through_login_and_lands_on_consent` is listed for the `Max-Age=600`
clause: it asserts the real `Set-Cookie` the authorize route sends, where the migration test
reads the constant. It was run under both mutations and stayed green, as it should.
