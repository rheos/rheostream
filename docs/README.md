# Documentation

- [Idea document](ideas/rheo-stream-idea.md): product thesis, boundaries, decisions,
  and open architecture questions.
- [Workspace layout](workspace-layout.md): public checkout and connected private
  siblings under a local, unversioned parent.
- [Requirements](requirements/README.md): scope, decisions, functional requirements,
  and the phased build plan with its acceptance criteria. Accepted (PR #5).
- [Architecture](architecture/README.md): contracts, designs, and decision records
  for release one, with one page per later phase. Accepted (PR #7).
- [Acceptance matrix](acceptance/README.md): per phase, the test that demonstrates each
  recorded criterion and the captured mutation that proves the test bites.
- [Notes](notes/0v-vertical-slice-findings.md): findings from build spikes, each
  with its evidence and a proposed disposition against the documents above.
- [Substrate boundary](notes/0c0-substrate-boundary.md): what the durable-work
  substrate provides, what each later run builds on top of it, and the tokens that
  guard the line between them.
- [Deployment](../deploy/README.md): the local compose stack and the flagship
  (subdomain mode) deployment, with rollback, the connection budget, and backups.

The idea document is the starting point, and the requirements documents close the
decisions it left open. Directory placeholders that still say "reserved" are not
approved specifications. Record accepted decisions explicitly and keep private user
or deployment details out of public documentation.
