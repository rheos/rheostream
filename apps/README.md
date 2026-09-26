# Application entry points

Application composition and interface adapters. Five entry points exist: `core` (the
FastAPI app, with its public and internal listeners), `worker` (the durable-work loop),
`cli` (the `rheo` operator command), `mcp` (the MCP facade, mounted in the core process)
and `web` (the Next.js shell).

Entry points resolve trusted workspace context and call application services.
They must not duplicate domain rules or require a model session for deterministic
intake and ordinary operations. App boundaries do not imply separate deployments.
