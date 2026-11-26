"""``docextract_adapter.docextract_version``: what a PDF's parse inputs and the input
fingerprint record of the ``docextract`` code that read it.

The four parsing modules are vendored, so the version is the upstream version and the
commit they were vendored at, the same string the pinned git install reported
(``<version>+<commit>``). Keeping that string keeps every stored input fingerprint, so
a rebuild over the same documents gives the same digest after the move.
"""

from __future__ import annotations

from rheo_finance.parsers import docextract_adapter, vendored

#: What the pinned git install of ``doc-extraction-api`` reported before the move.
PINNED_INSTALL = "0.1.0+0ad7253f6586eea62cabc32d7d69823e3987e67b"


def test_the_version_is_the_one_the_pinned_install_reported() -> None:
    assert docextract_adapter.docextract_version() == PINNED_INSTALL


def test_the_version_is_built_from_the_vendored_provenance() -> None:
    assert vendored.UPSTREAM_PROJECT == "doc-extraction-api"
    assert docextract_adapter.docextract_version() == (
        f"{vendored.UPSTREAM_VERSION}+{vendored.UPSTREAM_COMMIT}"
    )
