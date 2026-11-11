"""Deterministic Yew parser: the answer key for the model's output.

Regexes for one bill layout. The labels and the usage line's shape below are the
synthetic fixture's invented ones; the owner's identities file gives his own bills'
(identities.real and identities.layout). It is exact on that layout and breaks on any
other, which is the trade-off the model-based extractor is there to cover.
"""
import re
from datetime import datetime
from rheo_finance.identities import layout, real as _real, real_re as _real_re

MONEY = r"\$?(-?[\d,]+\.\d\d)"


def _num(s):
    return float(s.replace(",", ""))


def _money(pattern, text, default=0.0):
    m = re.search(pattern + r"\s+" + MONEY, text)
    return _num(m.group(1)) if m else default


def _date(s):
    return datetime.strptime(" ".join(s.split()), "%B %d, %Y").date().isoformat()


def usage_lines(text):
    """Non-zero lines from the usage block of the current-charges page."""
    m = re.search(_real_re(r"\nYew use list\n(.*?)\n(?:Yew levies|YEW NOW SET|Yew fresh sum)"), text, re.S)
    line_shape = layout(
        "yew.bill.usage_line",
        r"^- (.+?) = ([\d,]+\.\d\d)$",
    )
    if not m:
        return []
    items = []
    for line in m.group(1).splitlines():
        hit = re.match(line_shape, line)
        if hit and _num(hit.group(2)) > 0:
            items.append({"description": hit.group(1).strip(), "amount": _num(hit.group(2))})
    return items


def parse(text: str) -> dict:
    summary = text.split(_real("Yew cut line"))[0]
    due = re.search(_real_re(r"Yew settle by\s+(\w+ \d+, \d{4})"), summary)
    if not due:
        due = re.search(_real_re(r"Yew Settle By.*\n\s+\S+\s+(\w+ \d+, \d{4})"), text)
    data = re.search(_real_re(r"Yew bytes sum\s+([\d,]+\.\d+)"), text)
    short_due = re.search(_real_re(r"Yew due sum\s+Yew settle by (\w{3}) (\d+), (\d{4})\s+") + MONEY, summary)
    return {
        "bill_date": _date(re.search(_real_re(r"Yew dated\s+(\w+ \d+, \d{4})"), text).group(1)),
        "due_date": (
            datetime.strptime(" ".join(short_due.group(1, 2, 3)), "%b %d %Y").date().isoformat()
            if short_due else _date(due.group(1))
        ),
        "total_due": _num(short_due.group(4)) if short_due else None,
        "unpaid_balance": _money(_real_re(r"(?m)^\s*(?:Yew prior left|Yew prior moved)"), summary, default=None),
        "current_charges": _money(_real_re(r"Yew fresh sum\s+Yew with levies"), summary),
        "late_payment_charge": _money(_real("Yew slow fee"), summary),
        "device_payment": _money(_real_re(r"Yew handset part"), summary),
        "adjustments": _money(_real_re(r"Yew fixups"), summary),
        "usage_charges": usage_lines(text),
        "data_used_mb": _num(data.group(1)) if data else None,
    }
