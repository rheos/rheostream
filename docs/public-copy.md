# Public copy

## Repository description

Self-hosted agent workflows with durable jobs, workspace isolation and persistent memory.

## Post

rheoStream gives agents a persistent workspace with memory, background jobs and an audit trail. Web and MCP requests share a service boundary; server-side identity routes each operation to its workspace's Postgres database. Recallatron supports memory correction and supersession, with hybrid search using local embeddings, while private workspace data stays outside the public checkout. The core works today, but Leads, Current and Relationships are stubs, and the architecture tour documents the non-atomic enqueue path and its recovery limits. Follow a request through the code and tests: [rheos/rheostream](https://github.com/rheos/rheostream).
