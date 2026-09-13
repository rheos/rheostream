"""``rheo_spike``: the throwaway vertical-slice module of run 0v, deleted at 0c0's
branch cut.

Nothing here is imported by ``rheo_core``; the only edge into this package is the
``rheo.modules`` entry point declared in ``modules/spike/pyproject.toml``, which the
loader follows to :data:`rheo_spike.manifest.MANIFEST` and only when ``RHEO_MODULES``
names ``spike``.

**Two shipped gates already reach this code, and neither may be edited.**
``tests/test_boundary_scans.py`` walks ``modules/`` for the secret scope's private
names, so nothing under this package may name one. ``tests/test_audit_sink.py``'s
scan roots include ``modules/``, so no file here may *call* ``.record(...)`` — the
sink's ``def record`` is a definition and not a hit, which is the whole point: the
dispatcher calls it, this module only supplies the writer.

``tests/test_boundary.py``'s own scan roots do **not** include ``modules/``, so the
"only ``rheo_core/boundary/`` constructs a ``WorkspaceContext``" rule is unenforced
here (run 0v's finding F18). This package obeys it anyway: it constructs no context
at all — ``devtools.py`` asks ``context_for_operator`` for one.

Importing this module registers nothing. Registration happens in the loader, from
the manifest, exactly as it would for a real distribution.
"""
