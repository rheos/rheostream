# Agent runtimes

Replaceable adapters that manage model conversation mechanics. Runtime
implementations do not own domain data, permissions, or durable work state. One
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
