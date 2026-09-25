"""The embedding seam: what a memory's vector is made from, who makes it, and when.

- ``protocol.py`` — the :class:`~rheo_recallatron.embedding.protocol.EmbeddingProvider`
  shape, ``embed(texts) -> vectors`` plus ``model_id`` and ``dimensions``.
- ``local.py`` / ``fake.py`` — the one real provider (MiniLM-L6-v2 in-process) and the
  deterministic one the test profile registers.
- ``registry.py`` — the registered providers and the one reader of the configured name.
- ``enqueue.py`` — the one enqueue helper both memory writers call, and the rebuild's.
- ``job.py`` / ``rebuild.py`` — the after-commit embed job, and the one-job rebuild.
- ``operations.py`` — ``recallatron.embedding.rebuild`` and ``.coverage``.

**This file imports nothing; the four behavioural submodules are imported by path.**
``enqueue.py`` reaches the retrieval dispatcher, and the dispatcher will reach the
dense strategy, which reaches this package's registry: a package ``__init__`` that
pulled in ``enqueue`` would close that loop. Only :func:`embed_input` lives here,
because it is the one thing every side of the seam shares.
"""


def embed_input(title: str, body: str) -> str:
    """The text a memory's vector is made from: the title, one newline, the body.

    The one place ``EMBED_INPUT_VERSION = 1``'s composition is written. The embed job,
    the rebuild and every test embed through it, and a query is embedded as its bare
    string. Any change to what this returns bumps that version, because the relevance
    floor was measured over this representation and the rebuild's version prune is what
    makes a bump safe.
    """
    return title + "\n" + body
