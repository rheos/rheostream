"""The collector's own state rows."""

from dataclasses import dataclass, fields
from datetime import datetime
from uuid import UUID

from sqlalchemy import Connection, select

from rheo_leads.storage import jobfeed as j


@dataclass(frozen=True)
class JobfeedConnection:
    """One ``jobfeed_connection`` row; ``singleton`` is constant and left out.

    The three ``*_ref`` values are secret-store references, never secret values.
    """

    connection_id: UUID
    pipeline_id: UUID
    authorization_state: str
    token_ref: str | None
    credential_generation: int
    resource_url: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    revocation_endpoint: str | None
    client_id: str | None
    client_auth_method: str | None
    client_redirect_uri: str | None
    client_secret_expires_at: datetime | None
    client_secret_ref: str | None
    registration_token_ref: str | None
    organization_id: str | None
    run_claimed_at: datetime | None
    last_completed_at: datetime | None
    consecutive_failures: int
    not_before: datetime | None
    fetched: int
    imported: int
    skipped_dup: int
    refreshed: int
    filtered: int
    skipped_untracked: int
    errors: int
    detail_reads: int
    pages: int
    created_at: datetime
    updated_at: datetime


_FIELDS = tuple(f.name for f in fields(JobfeedConnection))


def read_connection(conn: Connection) -> JobfeedConnection | None:
    """The workspace's collector connection, or ``None`` before the first connect."""
    row = conn.execute(select(j.jobfeed_connection)).mappings().one_or_none()
    if row is None:
        return None
    return JobfeedConnection(**{name: row[name] for name in _FIELDS})
