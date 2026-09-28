"""Read-only access to the predecessor's private snapshot and ontology directory.

:mod:`.sqlite_source` opens the SQLite snapshot through the read-only immutable URI
only; :mod:`.graph_source` parses the ontology patch log. Both are pure readers: no
code path here writes beside the snapshot or reaches the predecessor's host.
"""
