"""The manual-entry files (FR-10): file name to its ordered header columns.

``finance init-data`` writes each file header-only from this table, and the loaders
that later prompts add read their columns from it, so the header the owner edits and
the header the code expects cannot drift. Column names come from SPEC "Files, paths
and settings" > Manual-entry files. A line ref column holds a ``finance.txn:<uuid>``
reference.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

MANUAL_DIR_NAME: Final = "manual"
# The utility-shares file goes by this name unless the setting names another.
UTILITY_SHARES_KEY: Final = "finance.manual.utility_shares_file"
UTILITY_SHARES_FILE: Final = "utility_shares.csv"

MANUAL_FILES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "accounts.csv": (
            "slug", "label", "institution", "kind", "currencies", "cadence", "owner",
            "opened", "closed", "sourced_by", "mail_senders", "payee_patterns",
            "placeholder", "missing_before", "missing_reason",
        ),
        "account_identifiers.csv": (
            "account", "identifier", "kind", "valid_from", "valid_to",
        ),
        "transfer_hints.csv": ("pattern", "direction", "counterpart_account"),
        "obligations.csv": (
            "payee", "kind", "period_label", "invoice_number", "amount_due", "minimum",
            "due_date", "archive_path", "issued_on",
        ),
        "arrangements.csv": (
            "arrangement", "payee", "agreed_on", "instalment", "amount", "due_date",
            "replaces_periods",
        ),
        "transactions.csv": (
            "account", "date", "amount", "currency", "description", "note",
        ),
        "business_use.csv": ("vendor", "category", "percent", "note"),
        UTILITY_SHARES_FILE: ("bill_date", "amount", "currency", "note"),
        "tax_slips.csv": ("year", "slip", "issuer", "box", "amount", "note"),
        "filed_years.csv": ("year", "filed_on", "note"),
        "document_periods.csv": (
            "archive_path", "account", "period_start", "period_end", "note",
        ),
        "merchant_rules.csv": (
            "merchant_key", "category", "tag", "business_unit", "percent",
        ),
        "payees.csv": ("slug", "label", "kind", "patterns", "party", "paid_from"),
        "classify_golden.csv": ("merchant_key", "category", "tag", "business_unit"),
        "parties.csv": ("slug", "label", "roles"),
        "party_aliases.csv": ("party", "kind", "alias"),
        "rent_schedule.csv": ("party", "amount", "currency", "effective_from"),
        "splits.csv": ("line_ref", "part_label", "amount", "covers", "note"),
        "loans.csv": (
            "party", "asset", "principal", "date_out", "out_line_ref", "term_days",
        ),
        "own_addresses.csv": ("account", "address"),
        "crypto_labels.csv": ("line_ref", "label", "note"),
        "balance_estimates.csv": ("account", "as_of", "asset", "quantity", "note"),
    }
)  # fmt: skip


def manual_files(
    utility_shares_file: str = UTILITY_SHARES_FILE,
) -> Mapping[str, tuple[str, ...]]:
    """``MANUAL_FILES`` with the utility-shares file under its configured name.

    The name is a plain ``.csv`` file name that no other manual file uses.
    """
    if utility_shares_file == UTILITY_SHARES_FILE:
        return MANUAL_FILES
    if (
        not utility_shares_file.endswith(".csv")
        or "/" in utility_shares_file
        or "\\" in utility_shares_file
        or utility_shares_file.startswith(".")
        or utility_shares_file in MANUAL_FILES
    ):
        raise ValueError(
            f"{UTILITY_SHARES_KEY}: not a free .csv file name: {utility_shares_file!r}"
        )
    return MappingProxyType(
        {
            (utility_shares_file if name == UTILITY_SHARES_FILE else name): columns
            for name, columns in MANUAL_FILES.items()
        }
    )
