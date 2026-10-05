"""Seed one manual connection, mapping, and usable default funnel.

This is install-time seed data: enable has no module-specific hook. UUIDs are
workspace-local, and rerunning the migration chain does not generate another set.
"""

from datetime import UTC, datetime

from alembic import op
from rheo_core.refs import uuid7
from sqlalchemy import text

revision = "0003_seed_manual_capture"
down_revision = "0002_intake_tables"
branch_labels = None
depends_on = None

# Frozen release-one vocabulary, independent of later application configuration.
FACTS = (
    "person.name",
    "person.email",
    "person.phone",
    "organization.name",
    "organization.domain",
    "subject",
    "message",
    "interest",
    "source_url",
    "locale",
    "value.amount",
    "value.currency",
    "value.basis",
)


def upgrade() -> None:
    conn = op.get_bind()
    funnel, mapping, connection = uuid7(), uuid7(), uuid7()
    now = datetime.now(UTC)
    conn.execute(
        text(
            "INSERT INTO leads.funnel VALUES (:id, 'General inquiries', "
            "'Default intake funnel', :now, NULL)"
        ),
        {"id": funnel, "now": now},
    )
    conn.execute(
        text(
            "INSERT INTO leads.field_mapping VALUES "
            "(:id, 1, 'manual_capture', 'json', 'active')"
        ),
        {"id": mapping},
    )
    conn.execute(
        text(
            "INSERT INTO leads.field_mapping_identity VALUES "
            "(:id, 1, '/event_id', '/occurred_at', NULL, NULL)"
        ),
        {"id": mapping},
    )
    for fact in FACTS:
        transform = {
            "person.email": "email_normalize",
            "person.phone": "phone_normalize",
        }.get(fact, "trim")
        conn.execute(
            text(
                "INSERT INTO leads.field_mapping_rule VALUES "
                "(:id, 1, :fact, :path, :transform, NULL, false, false)"
            ),
            {"id": mapping, "fact": fact, "path": "/" + fact, "transform": transform},
        )
    conn.execute(
        text("""INSERT INTO leads.intake_connection
        (id,name,transport,source_namespace,mapping_id,mapping_version,
         funnel_id,campaign_id,priority,subject_authenticated,email_verified,state,created_at)
        VALUES (:id,'Manual capture','manual','manual_capture',:mapping,1,
                NULL,NULL,100,false,false,'active',:now)"""),
        {"id": connection, "mapping": mapping, "now": now},
    )
    conn.execute(
        text("INSERT INTO leads.connection_health (connection_id) VALUES (:id)"),
        {"id": connection},
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade is not supported")
