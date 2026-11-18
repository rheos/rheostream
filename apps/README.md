# Application entry points

Application composition and interface adapters. Six entry points exist: `core` (the
FastAPI app, with its public and internal listeners), `worker` (the durable-work loop),
`cli` (the `rheo` operator command), `mcp` (the MCP facade, mounted in the core process),
`web` (the Next.js shell) and `bridge` (`rheo-bridge`, which runs on a laptop and sends
Claude Code sessions, from one enrolled directory or the whole machine, to Recallatron's
automatic memory).

Entry points resolve trusted workspace context and call application services.
They must not duplicate domain rules or require a model session for deterministic
intake and ordinary operations. App boundaries do not imply separate deployments.
