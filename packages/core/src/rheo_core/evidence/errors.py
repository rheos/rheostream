"""A content-free stand-in for a database error raised on an evidence path (#215).

A psycopg error's message quotes the failing row, and SQLAlchemy's ``DBAPIError`` adds
the statement and its bound parameters. On an evidence path those are the human's own
words, and the worker stores ``str(failure)`` as ``core.job.last_error``, outside
evidence retention. :class:`EvidenceDatabaseError` keeps what a reader needs to act on
(the original class name and the SQLSTATE) and nothing else.

Not in ``rheo_core.evidence.__all__``: the runtime hook raises it and nothing outside
core catches it by type. Recallatron keeps its own equivalent for the drain.
"""

from sqlalchemy.exc import DBAPIError


class EvidenceDatabaseError(Exception):
    """A database error with its message, statement and parameters dropped.

    ``str()`` and ``repr()`` carry only the original class name and SQLSTATE.
    ``connection_invalidated`` is kept, so a caller can still tell a dropped backend
    from a refused statement. Raise it ``from None`` and outside the ``except`` block
    that caught the original, or the original stays reachable on ``__context__``.
    """

    def __init__(self, error: DBAPIError) -> None:
        raw = getattr(error.orig, "sqlstate", None)
        # Only a string is kept: anything else could be an object with its own text.
        sqlstate = raw if isinstance(raw, str) else None
        super().__init__(f"{type(error).__name__} (SQLSTATE {sqlstate})")
        self.error_type: str = type(error).__name__
        self.sqlstate: str | None = sqlstate
        self.connection_invalidated: bool = bool(error.connection_invalidated)
