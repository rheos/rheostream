# Documentation

- [Idea document](ideas/rheo-stream-idea.md): product thesis, boundaries, decisions,
  and open architecture questions.
- [Workspace layout](workspace-layout.md): public checkout and connected private
  siblings under a local, unversioned parent.
- [Requirements](requirements/README.md): proposed scope, decisions, functional
  requirements, and the phased build plan with its acceptance criteria. Awaiting
  the maintainer's ratification; not yet accepted.
- [Architecture](architecture/README.md): future contracts, designs, and decisions.
- [Notes](notes/0v-vertical-slice-findings.md): findings from build spikes, each
  with its evidence and a proposed disposition against the documents above.
- [Substrate boundary](notes/0c0-substrate-boundary.md): what the durable-work
  substrate provides, what each later run builds on top of it, and the tokens that
  guard the line between them.

The idea document is the starting point and the requirements documents propose
closures for the decisions it left open. Neither requirements document is
accepted yet: the maintainer's review of the pull request that carries them is
the ratification step. Remaining directory placeholders are not approved
specifications. Record accepted decisions explicitly and keep private user or
deployment details out of public documentation.
