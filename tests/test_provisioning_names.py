"""``workspace_id_for_database``: the pure inverse of ``database_name_for`` (spec R14).

The drain resolves its workspace from ``current_database()``, so this inverse is what
stands between a database name and a workspace id. It round-trips every id and
refuses every malformed shape, each by its own case. No Postgres.
"""

from uuid import UUID

import pytest
from rheo_core.refs import uuid7
from rheo_core.storage.provisioning import (
    DATABASE_NAME_PREFIX,
    database_name_for,
    workspace_id_for_database,
)

_DIGITS = "0123456789abcdef" * 2  # 32 valid lowercase hex digits


@pytest.mark.parametrize("workspace_id", [uuid7(), UUID(int=0)], ids=["uuid7", "zero"])
def test_the_inverse_round_trips(workspace_id: UUID) -> None:
    assert workspace_id_for_database(database_name_for(workspace_id)) == workspace_id


def test_the_shared_postgres_database_is_not_a_workspace() -> None:
    assert workspace_id_for_database("postgres") is None


def test_thirty_one_digits_is_not_a_workspace() -> None:
    assert workspace_id_for_database(f"{DATABASE_NAME_PREFIX}{_DIGITS[:31]}") is None


def test_thirty_three_digits_is_not_a_workspace() -> None:
    assert workspace_id_for_database(f"{DATABASE_NAME_PREFIX}{_DIGITS}a") is None


def test_an_uppercase_hex_digit_is_not_a_workspace() -> None:
    # UUID(hex=...) would accept it, so only the lowercase rule and the round trip
    # keep "ws_...A..." from naming the same workspace as its lowercase spelling.
    name = f"{DATABASE_NAME_PREFIX}{_DIGITS[:31]}A"
    assert workspace_id_for_database(name) is None


def test_another_prefix_with_valid_digits_is_not_a_workspace() -> None:
    assert workspace_id_for_database(f"db_{_DIGITS}") is None


def test_a_non_hex_ws_name_is_not_a_workspace() -> None:
    # The name tests/test_worker_pass_cache_order.py already uses for a database
    # that is not a workspace's.
    assert workspace_id_for_database("ws_unrelated") is None
