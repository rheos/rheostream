"""The module's wiring into core: the manifest, the ``finance.tick`` job, the
composition over core sessions, the runtime model client and the in-process Current
client the reminder sync uses under core.

The only package under ``rheo_finance`` that imports ``rheo_core`` or
``rheo_contracts`` (``tests/guards/test_no_cross_module.py``).
"""
