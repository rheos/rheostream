"""Migration-run private-output guards (FR 18, AC 17).

This package is the mechanical half of the real-data isolation rule every later
migration-groundwork phase depends on before it may write anything:
:mod:`rheo_recallatron.migration.private_paths` refuses a write outside the one
operator-configured private root, and refuses a database DSN that is not loopback.

Nothing else lives here. The batch state machine, the harvest/verify passes and the
cutover mechanism belong wholly to later runs; this package implements none of them.
"""

from rheo_recallatron.migration.private_paths import (
    PrivateOutputRefusal,
    require_loopback_dsn,
    require_private_output,
)

__all__ = [
    "PrivateOutputRefusal",
    "require_loopback_dsn",
    "require_private_output",
]
