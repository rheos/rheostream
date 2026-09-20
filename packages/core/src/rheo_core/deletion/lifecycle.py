"""The workspace lifecycle lock: one transaction-scoped advisory lock per workspace.

**Keyed on the workspace by construction rather than by hashing one into the key.**
A workspace is a database (``docs/architecture/storage-and-workspaces.md``), and a
Postgres advisory lock is scoped to the database the connection is on, so a fixed key
taken on a workspace's own connection is already a per-workspace lock. Mixing the
workspace id into the key would be a second, weaker way of saying the same thing —
weaker because a uuid does not fit a bigint and would have to be folded, which
introduces collisions a single fixed key cannot have.

Transaction-scoped (``pg_advisory_xact_lock``, through
``rheo_core.storage.postgres.advisory_lock``) for the reason that function's own
docstring gives: a session-level lock's unlock statement cannot run once a failed
statement has aborted the transaction, which is exactly the case a deletion has to
survive.

**What it serialises, and what it does not.** It is taken immediately before a
destructive lifecycle step — the physical delete in ``deletion/operations.py``, and
the provenance and closure writers a later run adds — so two such steps against one
workspace run one after the other rather than interleaving their authorisation and
their effect. It is *not* a lock over the whole workspace: ordinary reads and writes
never take it, so a deletion blocks another deletion and nothing else.
"""

from typing import Final

from sqlalchemy import Connection

from rheo_core.storage.postgres import advisory_lock

WORKSPACE_LIFECYCLE_LOCK_KEY: Final = 0x5248454F4C494645
"""``RHEOLIFE`` in ASCII.

Distinct from the migration chain's ``ADVISORY_LOCK_KEY`` (``RHEOMIGR``) and the
first-account lock's (``RHEOACCT``), the same way those two are distinct from each
other, so no two locks in this tree can collide over one key.
"""


def lock_workspace_lifecycle(conn: Connection) -> None:
    """Take the workspace lifecycle lock on ``conn``; blocks until it is held.

    Released when ``conn``'s current transaction ends, which for every caller in this
    tree is the dispatcher's single transaction.
    """
    advisory_lock(conn, WORKSPACE_LIFECYCLE_LOCK_KEY)
