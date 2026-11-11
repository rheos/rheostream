"""Model steps: the ``ModelClient`` seam (``client``), the prompt guard (``guard``) and
the test stub (``stub``). Nothing here imports a provider SDK, reads an API key or
starts a process: a production client reaches a model through rheoStream's runtime
contract."""
