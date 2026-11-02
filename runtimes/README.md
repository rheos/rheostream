# Agent runtimes

Replaceable adapters that manage model conversation mechanics. Runtime
implementations do not own domain data, permissions, or durable work state. One conversational
adapter is built: `rheo_runtimes.claude_cli.ClaudeCliRuntime`, which spawns the
configured Claude CLI (`claude -p`).

The [idea document](../docs/ideas/rheo-stream-idea.md#rheo-agent-and-interaction-model)
requires configurable Claude CLI (`claude -p`) and OpenRouter execution, and identifies
Codex CLI (`codex exec`) as an additional adapter to validate. Direct provider APIs
and SDKs can use the same boundary.

CLI agents supply an agent loop. The OpenRouter adapter must supply a loop that
routes tool requests through rheo's permissions and returns results to the model.
Runtime selection is separate from model selection. Each adapter must declare its
capabilities and preserve scoped sessions, action approval, cancellation/failure
states, and durable operation identity; native sessions are not interchangeable.

Private configuration selects adapters, models, provider policies, and credential
references. No credentials or subscription access ship in this directory. The
OpenRouter and Codex CLI adapters are not built yet, and a documented CLI capability
does not establish that its rheo adapter is suitable for every hosted workflow.

## Jev typed judgments

`rheo_runtimes.jev.JevProvider` calls TypeSafe System One at the fixed HTTPS endpoint
`https://api.typesafe.ai/v1/systemone`. This is a separate, tool-free typed inference
protocol (`rheo_contracts.judgment`) composed through `JudgmentRunner` in the worker;
it is not a conversational agent runtime or a replacement for Claude's full scorer.
The adapter validates every answer and token count, bounds request/response sizes,
refuses redirects, ignores ambient HTTP proxies and exposes only fixed failure codes.
The [System One API](https://docs.typesafe.ai/api/system-one) and
[Score primitive](https://docs.typesafe.ai/primitives/score) define the wire contract.

Activation requires both an operator and the workspace owner:

1. Provision a TypeSafe API key privately at
   `<data_root>/secrets/runtime/jev/api-key`, owned/readable by the worker with mode
   `0600` or stricter. Keep it out of Git and container builds. The existing shared
   data-root volume supports this file reference without Compose environment changes.
2. Add this to `<data_root>/config/deployment.toml` (merge with existing settings),
   then restart the worker:

   ```toml
   [runtime.jev]
   enabled = true
   credential_ref = "secret://file/runtime/jev/api-key"
   model_id = "jev-latest"
   ```

   The only alternative environment reference permitted is
   `secret://env/RHEO_TYPESAFE_API_KEY`; explicitly supply that variable to the worker
   and any service performing deployment-secret startup validation. Setting a host
   variable alone does not forward it through Compose. A model can be pinned instead
   of using the provider's moving `jev-latest` alias.
3. As the workspace owner, invoke `core.settings.set` with
   `{"key":"leads.scoring.prefilter_enabled","value":true}`. This authorizes sending
   that workspace's posting and profile/policy data to TypeSafe and using its metered
   API. Deployment/environment defaults alone cannot opt a workspace in.
4. Optionally set `leads.scoring.prefilter_threshold` with the same operation to an
   integer from 1 to 100 (default 50). A lower threshold holds fewer jobs. On the next
   scoring sweep, check the Scoring call log (`prefilter`) and Review queue. Promote
   a held job to bypass the gate and request full scoring.

To roll back, set the workspace flag to false or deployment `runtime.jev.enabled`
to false. The next sweep releases previous Jev holds into the normal scorer; it does
not rewrite final scores. An unavailable key/provider also falls through to full
scoring, with a content-free failed-call record. No live provider or production data
is needed by the synthetic tests in `tests/test_jev.py`, `tests/test_job_prefilter.py`
and `tests/postgres/test_job_prefilter.py`. Those tests establish integration behavior,
not the provider's judgment quality; review representative held jobs during rollout.
