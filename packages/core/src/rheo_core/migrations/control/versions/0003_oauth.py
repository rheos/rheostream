"""The control-plane OAuth tables and the widened token-issuer CHECK (issue #287).

Revision ID: 0003_oauth
Revises: 0002_work_index

Creates exactly the six tables in ``rheo_core.storage.oauth_tables`` (``oauth_client``,
``oauth_client_redirect_uri``, ``oauth_authorization``, ``oauth_grant``,
``oauth_refresh_token``, ``oauth_event``) from their own ``MetaData``, and rebuilds
``access_token_issued_from`` from ``TOKEN_ISSUERS_0003`` so a ``connector`` row is
accepted. No column changes; every existing token satisfies the new CHECK unchanged.
Downgrade is not supported in release one.
"""

from collections.abc import Sequence

from alembic import op
from rheo_core.storage.oauth_tables import (
    TOKEN_ISSUERS_0003,
    in_check,
    oauth_metadata,
)

revision: str = "0003_oauth"
down_revision: str | None = "0002_work_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``checkfirst=False``: a table already present means this chain is running
    # against a database it does not own, which must fail loudly, never skip.
    oauth_metadata.create_all(op.get_bind(), checkfirst=False)
    op.drop_constraint(
        "access_token_issued_from", "access_token", schema="control", type_="check"
    )
    op.create_check_constraint(
        "access_token_issued_from",
        "access_token",
        in_check("issued_from", TOKEN_ISSUERS_0003),
        schema="control",
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported in release one")
