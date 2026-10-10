"""A routing hint only: the workspace connection remains authoritative."""

from sqlalchemy import Column, ForeignKey, MetaData, Table, Text, Uuid

from rheo_core.storage.control_tables import workspace

metadata = MetaData(schema="control")
locator = Table(
    "connector_locator",
    metadata,
    Column("connection_id", Uuid, primary_key=True),
    Column(
        "workspace_id",
        Uuid,
        ForeignKey(workspace.c.id, ondelete="CASCADE"),
        nullable=False,
    ),
    Column("module_id", Text, nullable=False),
    Column("transport", Text, nullable=False),
)
