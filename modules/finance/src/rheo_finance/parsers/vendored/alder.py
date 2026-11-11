"""Deterministic parser for Alder monthly account statements (pdftotext -layout output).

The parser never trusts column position. Each transaction line ends with an amount and the
running balance; whether the amount is a withdrawal or a deposit is read off the balance change.
Every account is then checked against its summary (opening, total withdrawals, total deposits,
closing). A failed check is reported in `checks`, never silently passed along.

A blank balance is zero, so some lines carry one value or none: an amount that empties the
account, or the balance on a notice that moves no money. One value equal to the balance before it
could be either; the next line's arithmetic, or the summary's closing balance, settles which.

On a layout it does not read it either raises AlderParseError or reports failed checks.
The period line, the number mark, the opening line and the overdraft marker below are
the synthetic fixture's invented ones; the owner's identities file gives his
own (identities.layout and identities.real).
"""
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from rheo_finance.identities import layout, real as _real, real_re as _real_re

MONTHS = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
OD = layout("alder.statement.overdraft", "neg")
MARK = re.escape(layout("alder.statement.number_mark", " No "))
# 9,999.99 / 99.99- neg / 99.99- / -99.99
MONEY = rf"-?\d{{1,3}}(?:,\d{{3}})*\.\d\d(?:- ?(?:{re.escape(OD)})?)?"

PERIOD = re.compile(layout(
    "alder.statement.period",
    r"ALDER CYCLE\s+(\d{1,2} (?:@MONTHS@) \d{4}) UNTIL (\d{1,2} (?:@MONTHS@) \d{4})",
).replace("@MONTHS@", MONTHS))
PRINTED_DATE = re.compile(r"^\s*([A-Z]+ \d{1,2}, \d{4})\s*$", re.M)
# An account can carry a nickname after its number, "ALDER BASIC CHEQUING No ... (SPARE JAR)",
# and its continuation header adds a marker in brackets.
SUMMARY_LINE = re.compile(rf"^([A-Z][A-Z0-9 &'/-]*?){MARK}(\d{{6,}})(?: \([^)]*\))?((?:\s+{MONEY}){{4}})\s*$")
ACCOUNT_HEADER = re.compile(rf"^\s*([A-Z][A-Z0-9 &'/-]*?){MARK}(\d{{6,}})(?: \([^)]*\))*\s*$")
OPENING = re.compile(_real_re(rf"^\s*ALDER START FIGURE\s+({MONEY})\s*$"))
TXN_START = re.compile(rf"^\s{{0,3}}(\d{{2}}) ({MONTHS})\s+(\S.*)$")
AMOUNT_TAIL = re.compile(rf"(?:\s{{2,}}{MONEY})+\s*$")

# The fee types this layout names. Anything else is new and worth a look.
# ALDER UNNAMED CHARGE names its fee in brackets after it.
KNOWN_FEES = {_real("ALDER MONTH FEE"), _real("ALDER COUNT FEES"), "OVERDRAFT FEE", _real("ALDER MACHINE FEE"),
              "OVERDRAFT INTEREST", _real("ALDER UNNAMED CHARGE")}
NSF = re.compile(r"\bNSF\b|NON[- ]?SUFFICIENT|RETURNED ITEM|ITEM RETURNED|DISHONOU?RED")


class AlderParseError(ValueError):
    """The text is not an Alder statement this parser can read."""


def money(s):
    s = s.strip()
    bare = s.removesuffix(OD).rstrip()
    negative = s.startswith("-") or bare.endswith("-")
    value = Decimal(bare.replace(",", "").strip(" -"))
    return -value if negative else value


def _day(s):
    return datetime.strptime(s, "%d %b %Y").date()


def label(name):
    """Account name without its number, for anything that leaves this machine."""
    return name.title()


def classify(description, withdrawal):
    """(transaction type, fee type or None, nsf flag) for one transaction."""
    kind = description.split(" (")[0].strip()
    inner = re.search(r"\(([^)]*)\)", description)
    fee = None
    if kind == _real("ALDER UNNAMED CHARGE"):
        fee = inner.group(1).strip() if inner else kind
    elif re.search(r"\bFEES?\b", kind):
        fee = kind
    elif kind.startswith(_real("ALDER RATE ADDED")) and withdrawal:
        # Interest charged for overdrawn days.
        fee = "OVERDRAFT INTEREST"
    nsf = bool(NSF.search(description)) or bool(fee and re.search(r"\bRETURN", fee))
    return kind, fee, nsf


def movement(prev, values):
    """(withdrawal, deposit, balance, ambiguous) for one line, read off the balance change, or None
    when the numbers do not add up. A blank balance is zero."""
    if prev is None:
        return None
    if len(values) == 3:
        w, dep, balance = values
        return (w, dep, balance, False) if prev - w + dep == balance else None
    if len(values) == 2:
        amount, balance = values
        if prev - amount == balance:
            return amount, None, balance, False
        if prev + amount == balance:
            return None, amount, balance, False
        return None
    if len(values) == 1:
        (v,) = values
        if v == prev:
            # a notice showing the balance, or a withdrawal that empties the account
            return None, None, v, prev > 0
        if prev < 0 and prev + v == 0:
            return None, v, Decimal("0.00"), False  # a deposit that clears an overdraft exactly
        return None
    return (None, None, prev, False) if prev == 0 else None


def parse(text: str) -> dict:
    period = PERIOD.search(text)
    if not period:
        raise AlderParseError("no period line")
    start, end = _day(period.group(1)), _day(period.group(2))
    printed = PRINTED_DATE.search(text)
    statement_date = (datetime.strptime(printed.group(1).title(), "%B %d, %Y").date()
                      if printed else end + timedelta(days=1))

    def full_date(dd, mon):
        d = _day(f"{dd} {mon} {start.year}")
        return d if d >= start else _day(f"{dd} {mon} {end.year}")

    summary = {}
    accounts = {}  # number -> account dict, in statement order
    current = None
    pending = None  # date line whose amounts are on a following line
    checks = []

    def open_account(name, number):
        return accounts.setdefault(number, {
            "name": name, "ref": number[-4:], "opening": None, "transactions": [],
        })

    def empty_out(t):
        """Settle an ambiguous line as the withdrawal that took the account to zero."""
        t["withdrawal"], t["balance"] = t["balance"], Decimal("0.00")
        t["type"], t["fee_type"], t["nsf"] = classify(t["description"], withdrawal=True)

    def add_transaction(d, description, amounts, raw):
        acct = accounts[current]
        txns = acct["transactions"]
        prev = txns[-1]["balance"] if txns else acct["opening"]
        values = [money(a) for a in amounts]
        got = movement(prev, values)
        # an ambiguous line before this one: did it take the account to zero?
        ambiguous_before = bool(txns) and txns[-1].pop("ambiguous", False)
        if ambiguous_before and got is None and (alt := movement(Decimal("0.00"), values)) is not None:
            empty_out(txns[-1])
            got = alt
        if got is None:
            checks.append(f"{label(acct['name'])} {d}: running balance does not add up ({raw.strip()[:60]})")
            got = (None, None, values[-1] if len(values) > 1 else None, False)
        w, dep, balance, ambiguous = got
        kind, fee, nsf = classify(description, withdrawal=w is not None)
        txns.append({
            "balance": balance, "description": description, "deposit": dep,
            "date": d.isoformat(), "withdrawal": w, "type": kind, "fee_type": fee, "nsf": nsf,
        } | ({"ambiguous": True} if ambiguous else {}))

    def close_pending():
        """A line that never showed an amount: nothing moved, and the balance is zero."""
        add_transaction(pending["date"], " ".join(p for p in pending["parts"] if p), [], pending["raw"])

    for line in text.splitlines():
        # continuation lines are indented; a new date or anything at the margin ends the line
        if pending is not None and line.strip() and (TXN_START.match(line) or not line[0].isspace()):
            close_pending()
            pending = None
        if pending is None and (m := SUMMARY_LINE.match(line)):
            o, wd, dp, c = (money(v) for v in re.findall(MONEY, m.group(3)))
            summary[m.group(2)] = {"name": m.group(1).strip(), "opening": o, "withdrawals": wd,
                                   "deposits": dp, "closing": c}
            continue
        if pending is None and (m := ACCOUNT_HEADER.match(line)):
            current = m.group(2)
            open_account(m.group(1).strip(), current)
            continue
        if current is None:
            continue
        if pending is None and (m := OPENING.match(line)):
            accounts[current]["opening"] = money(m.group(1))
            continue
        if pending is None:
            m = TXN_START.match(line)
            if not m:
                continue
            pending = {"date": full_date(m.group(1), m.group(2)), "parts": [], "raw": line}
            rest = m.group(3)
        else:
            if not line.strip():
                continue
            rest = line
            pending["raw"] += "\n" + line
        tail = AMOUNT_TAIL.search(rest)
        head = rest[:tail.start()] if tail else rest
        pending["parts"].append(" ".join(head.split()))
        if tail:
            description = " ".join(p for p in pending["parts"] if p)
            add_transaction(pending["date"], description, re.findall(MONEY, tail.group()), pending["raw"])
            pending = None
        elif len(pending["parts"]) > 3:
            checks.append(f"unfinished transaction: {pending['raw'].strip()[:60]}")
            pending = None

    if pending is not None:
        close_pending()
    if not accounts:
        raise AlderParseError("no account sections found")

    out_accounts = []
    fees = []
    for number, acct in accounts.items():
        txns = acct["transactions"]
        s = summary.get(number)
        if txns and txns[-1].pop("ambiguous", False) and s and s["closing"] == 0:
            empty_out(txns[-1])
        opening = acct["opening"] if acct["opening"] is not None else Decimal("0.00")
        closing = txns[-1]["balance"] if txns and txns[-1]["balance"] is not None else opening
        withdrawals = sum((t["withdrawal"] or 0 for t in txns), Decimal("0.00"))
        deposits = sum((t["deposit"] or 0 for t in txns), Decimal("0.00"))
        name = label(acct["name"])
        if s:
            for field, got in (("opening", opening), ("withdrawals", withdrawals),
                               ("deposits", deposits), ("closing", closing)):
                if got != s[field]:
                    checks.append(f"{name}: {field} {got} does not match the summary {s[field]}")
        elif opening + deposits - withdrawals != closing:
            checks.append(f"{name}: opening + deposits - withdrawals != closing")

        overdrawn, spell = [], None
        for t in txns:
            if t["balance"] is None:
                continue
            if t["balance"] < 0 and spell is None:
                spell = {"from": t["date"], "to": None}
            elif t["balance"] >= 0 and spell is not None:
                spell["to"] = t["date"]
                overdrawn.append(spell)
                spell = None
        if spell is not None:
            overdrawn.append(spell)  # still overdrawn at the end of the period

        balances = [t["balance"] for t in txns if t["balance"] is not None] + [opening]
        for t in txns:
            if t["fee_type"]:
                amount = t["withdrawal"] if t["withdrawal"] is not None else -(t["deposit"] or 0)
                fees.append({"date": t["date"], "account": name, "fee_type": t["fee_type"],
                             "amount": amount, "nsf": t["nsf"]})
        out_accounts.append({
            "name": name, "ref": acct["ref"], "opening": opening, "withdrawals": withdrawals,
            "deposits": deposits, "closing": closing, "lowest": min(balances),
            "overdrawn": overdrawn, "in_summary": bool(s), "transactions": txns,
        })

    for number, s in summary.items():
        if number not in accounts:
            checks.append(f"{label(s['name'])}: in the summary but no transaction section")

    fees_by_type = {}
    for f in fees:
        fees_by_type[f["fee_type"]] = fees_by_type.get(f["fee_type"], Decimal("0.00")) + f["amount"]
    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "statement_date": statement_date.isoformat(),
        "accounts": out_accounts,
        "fees": fees,
        "fees_total": sum((f["amount"] for f in fees), Decimal("0.00")),
        "fees_by_type": fees_by_type,
        "nsf": [t | {"account": a["name"]} for a in out_accounts for t in a["transactions"] if t["nsf"]],
        "checks": checks,
    }


def error_report_deadline(statement: dict) -> date:
    """The statement date plus a fixed window of 30 days (no caller reads it)."""
    return date.fromisoformat(statement["statement_date"]) + timedelta(days=30)
