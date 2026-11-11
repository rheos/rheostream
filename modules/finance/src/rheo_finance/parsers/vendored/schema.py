"""The agreed fields for a Yew mobile bill.

Every field is nullable in the extraction schema so the model can say "not found" instead of
guessing. validate.py turns nulls and bad values into flags.
"""
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator
from rheo_finance.identities import real as _real, real_re as _real_re

DOCUMENT_TYPE = "yew_bill"

MONEY_FIELDS = (
    "total_due", "unpaid_balance", "current_charges",
    "late_payment_charge", "device_payment", "adjustments",
)


def to_money(v: object) -> Decimal | None:
    if v is None:
        return None
    return Decimal(str(v)).quantize(Decimal("0.01"))


class UsageCharge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str
    amount: Decimal

    _money = field_validator("amount", mode="before")(to_money)


class YewBill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bill_date: date | None
    due_date: date | None
    total_due: Decimal | None
    unpaid_balance: Decimal | None
    current_charges: Decimal | None
    late_payment_charge: Decimal | None
    device_payment: Decimal | None
    adjustments: Decimal | None
    usage_charges: list[UsageCharge] | None
    data_used_mb: Decimal | None

    _money = field_validator(*MONEY_FIELDS, mode="before")(to_money)


FIELD_GUIDE = {
    "bill_date": _real("The bill date ('Yew dated')."),
    "due_date": _real("The date the total due is payable by ('Yew settle by')."),
    "total_due": _real("The total due ('Yew due sum'): the unpaid balance plus the current charges."),
    "unpaid_balance": _real("The unpaid balance from earlier bills ('Yew prior left')."),
    "current_charges": _real("This bill's current charges ('Yew fresh sum Yew with levies')."),
    "late_payment_charge": _real("A charge for paying late ('Yew slow fee'). 0 if there is none."),
    "device_payment": _real("A device instalment ('Yew handset part'). 0 if there is none."),
    "adjustments": _real("Account adjustments ('Yew fixups'), as a negative number. 0 if there are none. "
                   "Recurring credits ('Yew offsets') are not adjustments."),
    "usage_charges": _real("Each usage line ('Yew use list') with a non-zero amount, as {description, amount}. "
                           "Empty list if every usage line is 0.00."),
    "data_used_mb": _real("Data used in the period, in MB ('Yew bytes sum')."),
}

_NUM = {"type": ["number", "null"]}
_DATE = {"type": ["string", "null"], "format": "date"}

JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "bill_date": _DATE,
        "due_date": _DATE,
        **{f: _NUM for f in MONEY_FIELDS},
        "usage_charges": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "properties": {"description": {"type": "string"}, "amount": {"type": "number"}},
                "required": ["description", "amount"],
                "additionalProperties": False,
            },
        },
        "data_used_mb": _NUM,
    },
    "required": list(FIELD_GUIDE),
    "additionalProperties": False,
}
