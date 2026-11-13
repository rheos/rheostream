"""Four parsing modules taken from ``doc-extraction-api``, pinned by their bytes.

``alder``, ``reference``, ``pdftext`` and ``schema`` come from that project's
``docextract`` package at commit ``0ad7253f6586eea62cabc32d7d69823e3987e67b``.
``pdftext`` is the upstream file byte for byte. The other three carry recorded
patches. The first put the module's neutral codenames where upstream named an
institution, in names, docstrings and comments. The later ones took dates, figures and
account history out of comments and docstrings, and route the layout wording and
shapes that would identify an issuer through ``identities.real`` and
``identities.layout``, so each pattern matches the owner's own wording at run time while
these files hold only invented stand-ins and defaults. With the owner's identities file
the rules read what upstream's read. The patches, with the upstream paths and blob ids,
are kept with the owner's private records. The files arrive under this
repository's AGPL-3.0 license.

Rules for this package:

- ``rheo_finance.parsers.docextract_adapter`` is the only module that imports it.
- No file here is edited in place. ``tests/guards/test_vendored_bytes.py`` pins each
  file's sha256 and Git blob id; a further change is another recorded patch whose
  result gets a new pin there, with the reason beside it.
- Upstream's tests, fixtures, ``validate``, ``extractor``, ``api`` and ``monitor``
  modules are not copied.
- The files keep upstream's style, so the repository's lint and type checks skip this
  directory (root ``pyproject.toml``); the adapter types everything that leaves it.
"""

#: The upstream project, its version and the commit the four files were taken from.
UPSTREAM_PROJECT = "doc-extraction-api"
UPSTREAM_VERSION = "0.1.0"
UPSTREAM_COMMIT = "0ad7253f6586eea62cabc32d7d69823e3987e67b"
