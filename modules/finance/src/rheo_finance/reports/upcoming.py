"""What is coming up: the bills due soon and the recurring charges the ledger predicts,
read at request time from the granted report views (#637).

Two kinds of row, never mixed:

- **due**: an open obligation from ``report_owed`` with something left to pay and a due
  date from today to the end of the window, read through ``owed.figures`` exactly as
  ``owed`` reads it, so a presumed-paid, paid or disputed obligation is not here.
- **expected**: a charge the ledger's own history predicts. Nothing here is stored, and
  a prediction never becomes an obligation, a notice or a reminder: a card charge from
  a subscription vendor is a spending line only (stage 14).

**A series.** The charges a prediction rests on are the outflows of ``report_txn``
(amount below zero) with a label of their own that are:

- not transfer-like (``transfer_state`` null: an own transfer, a conversion, a funding
  pull and an unpaired transfer-like line are left out);
- not excluded (``exclusion`` null) and not in a crypto currency
  (``report_crypto_movement``);
- not cited by an obligation's payment (``report_owed_trace``), and not a payment still
  unapplied to a payee that has bills (``report_open_item``, matched by account, date,
  amount and currency, since an unapplied credit names no line): ``due`` and Owed
  count those bills already. A payee with no bill, such as a landlord, keeps them.

One account, one currency and one label key make a group, where the label key is the
printed label casefolded, without each token that holds a digit and without trailing
two-letter tokens (a region code), so a label that changes its postal code, phone
number or province between statements stays one group. A key that starts with another
key of two or more tokens of the same account and currency, token for token, joins it;
an empty key joins nothing. Within a group, each charge in date order joins the run
whose latest amount is nearest, when it is within ``AMOUNT_TOLERANCE`` of it (or
``AMOUNT_FLOOR``), else it starts a run: one vendor with two plans is two runs, and a
charge that drifts a little each month, as interest or a foreign-currency price does,
stays one. Two runs of one
group whose median amounts are that near join when their charges, put together, are
never within ``COLLISION_DAYS`` of each other: interest that moves back and forth
splits into two runs that skip turns, and this puts it back together. And a run
carries on into a run that starts where its next charge was due, after its last
charge: a price rise starts a new run, which continues the old one.

**Regular.** A run counts when its latest ``RECENT`` charges number at least
``finance.waste.recurring_min_count``, every gap between them is a day or more, their
median gap falls inside one ``CADENCES`` band, and at least three quarters of the gaps
are within that band's tolerance of the median. Shopping fails this; a subscription,
a fee or interest passes.

**Still running.** The charge after a run's last one is due one cadence step later.
A run is stopped when that charge fell more than the cadence's grace before the end
of its account's loaded data (the account's latest posting date): the data covers the
day and no charge came. An account whose latest data is older than ``STALE_ACCOUNT``
days has no predictions at all. So an account whose recent statements are not loaded
keeps its charges, and a cancelled subscription on a current account drops out.

**The charges.** Each step from the last charge, up to the end of the window. A step
before today is ``likely_charged`` (the data does not reach it yet, so it has probably
posted), shown only from ``PAST_DAYS`` back and only past the account's data; a step
from today on is ``expected``. The amount is the run's latest, with the low and high of
its latest three.
"""

from __future__ import annotations

import datetime as dt
import re
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from rheo_finance.clock import local_date
from rheo_finance.context import FinanceContext, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass, Tiered
from rheo_finance.operations import OperationSpec, register
from rheo_finance.render import as_of
from rheo_finance.reports.owed import Facts, figures
from rheo_finance.storage.schema import (
    report_crypto_movement,
    report_open_item,
    report_owed,
    report_owed_trace,
    report_txn,
)
from rheo_finance.tier import Tier

P, R = Tier.PUBLIC, Tier.RESTRICTED
ZERO: Final = Decimal("0.00")
MIN_COUNT_KEY: Final = "finance.waste.recurring_min_count"
DEFAULT_DAYS: Final = 30
MAX_DAYS: Final = 92
RECENT: Final = 6
COLLISION_DAYS: Final = 15
AMOUNT_TOLERANCE: Final = Decimal("0.15")
AMOUNT_FLOOR: Final = Decimal("1.00")
STALE_ACCOUNT: Final = 120
PAST_DAYS: Final = 62
REGULAR_SHARE: Final = 0.75
JOIN_TOKENS: Final = 2
# What the reader view writes for a line with no label of its own.
NO_LABEL: Final = "Transaction"


@dataclass(frozen=True)
class Cadence:
    name: str
    low: int
    high: int
    tolerance: int
    grace: int
    months: int = 0
    days: int = 0

    def step(self, start: dt.date, n: int) -> dt.date:
        if self.days:
            return start + dt.timedelta(days=self.days * n)
        return add_months(start, self.months * n)


CADENCES: Final = (
    Cadence("weekly", 6, 8, 2, 3, days=7),
    Cadence("monthly", 26, 35, 7, 7, months=1),
    Cadence("yearly", 350, 380, 20, 31, months=12),
)


def add_months(day: dt.date, months: int) -> dt.date:
    """The same day ``months`` later, the month's last day when it is shorter."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    return dt.date(year, month, min(day.day, (nxt - dt.timedelta(days=1)).day))


_REGION: Final = re.compile(r"[a-z]{2}")
_ALNUM: Final = re.compile(r"\w")


def label_key(label: str) -> str:
    """A printed label without what changes between statements: tokens holding a
    digit, tokens with no letter or digit, and trailing two-letter tokens."""
    tokens = [
        t
        for t in label.casefold().split()
        if not any(c.isdigit() for c in t) and _ALNUM.search(t)
    ]
    while len(tokens) > 1 and _REGION.fullmatch(tokens[-1]):
        tokens.pop()
    return " ".join(tokens)


def joined_keys(keys: Iterable[str]) -> dict[str, str]:
    """Each key to the shortest key of ``JOIN_TOKENS`` or more tokens it starts with,
    token for token (itself if none). An empty key is never a root and maps to
    itself, so a label with no letters joins nothing."""
    ordered = sorted(set(keys), key=lambda k: (len(k.split()), k))
    roots: list[tuple[str, ...]] = []
    out: dict[str, str] = {}
    for key in ordered:
        tokens = tuple(key.split())
        if len(tokens) < JOIN_TOKENS:
            out[key] = key
            continue
        root = next((r for r in roots if tokens[: len(r)] == r), None)
        if root is None:
            roots.append(tokens)
            out[key] = key
        else:
            out[key] = " ".join(root)
    return out


@dataclass(frozen=True)
class Outflow:
    account: str
    account_label: str
    posted: dt.date
    amount: Decimal  # positive: what left the account
    currency: str
    label: str


@dataclass
class Run:
    account: str
    account_label: str
    currency: str
    charges: list[Outflow] = field(default_factory=list)

    @property
    def last(self) -> Outflow:
        return self.charges[-1]


def runs(outflows: Iterable[Outflow]) -> list[Run]:
    """The runs of every group, as the module docstring says."""
    groups: dict[tuple[str, str], list[Outflow]] = defaultdict(list)
    for each in outflows:
        groups[(each.account, each.currency)].append(each)
    found: list[Run] = []
    for (account, currency), items in sorted(groups.items()):
        keys = joined_keys(label_key(o.label) for o in items)
        by_key: dict[str, list[Outflow]] = defaultdict(list)
        for o in items:
            key = keys[label_key(o.label)]
            if key:
                by_key[key].append(o)
        for _, group in sorted(by_key.items()):
            open_runs: list[Run] = []
            for o in sorted(group, key=lambda o: (o.posted, o.amount, o.label)):
                near = [r for r in open_runs if _near(r.last.amount, o.amount)]
                if near:
                    min(
                        near, key=lambda r: abs(r.last.amount - o.amount)
                    ).charges.append(o)
                else:
                    open_runs.append(Run(account, o.account_label, currency, [o]))
            found.extend(chained(merged(open_runs)))
    return found


def _near(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(AMOUNT_FLOOR, max(a, b) * AMOUNT_TOLERANCE)


def merged(found: list[Run]) -> list[Run]:
    """Join two runs of one group whose median amounts are near and whose charges,
    put together, are never within ``COLLISION_DAYS`` of each other: a charge that
    moves back and forth, as interest does, splits into two runs that skip turns."""
    out = list(found)
    joined = True
    while joined:
        joined = False
        for i, a in enumerate(out):
            for b in out[i + 1 :]:
                if not _near(
                    statistics.median(o.amount for o in a.charges),
                    statistics.median(o.amount for o in b.charges),
                ):
                    continue
                both = sorted(
                    a.charges + b.charges, key=lambda o: (o.posted, o.amount, o.label)
                )
                if any(
                    (y.posted - x.posted).days < COLLISION_DAYS
                    for x, y in zip(both, both[1:], strict=False)
                ):
                    continue
                a.charges = both
                out.remove(b)
                joined = True
                break
            if joined:
                break
    return out


def chained(found: list[Run]) -> list[Run]:
    """Carry a run on into the run that starts where its next charge was due: a price
    change beyond ``AMOUNT_TOLERANCE`` starts a new run, which this continues. The
    later run must start after the earlier one's last charge, so two plans that run
    side by side stay two."""
    out = sorted(found, key=lambda r: (r.charges[0].posted, r.charges[0].amount))
    joined = True
    while joined:
        joined = False
        for a in out:
            every = cadence(a, 2)
            if every is None:
                continue
            due = every.step(a.last.posted, 1)
            b = next(
                (
                    b
                    for b in out
                    if b is not a
                    and b.charges[0].posted > a.last.posted
                    and abs((b.charges[0].posted - due).days) <= every.tolerance
                ),
                None,
            )
            if b is not None:
                a.charges.extend(b.charges)
                out.remove(b)
                joined = True
                break
    return out


def cadence(run: Run, min_count: int) -> Cadence | None:
    recent = run.charges[-RECENT:]
    if len(recent) < max(2, min_count):
        return None
    gaps = [
        (b.posted - a.posted).days for a, b in zip(recent, recent[1:], strict=False)
    ]
    if any(g < 1 for g in gaps):
        return None
    middle = statistics.median(gaps)
    for each in CADENCES:
        if each.low <= middle <= each.high:
            near = sum(abs(g - middle) <= each.tolerance for g in gaps)
            return each if near >= REGULAR_SHARE * len(gaps) else None
    return None


Kind = Literal["due", "expected"]
State = Literal["due", "expected", "likely_charged"]


class Charge(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Annotated[Kind, Tiered(P)]
    state: Annotated[State, Tiered(P)]
    on: Annotated[dt.date, Tiered(P)]
    days_away: Annotated[int, Tiered(P)]
    label: Annotated[str, Tiered(P)]
    account: Annotated[str | None, Tiered(P)]
    account_label: Annotated[str | None, Tiered(P)]
    currency: Annotated[str, Tiered(P)]
    amount: Annotated[Decimal, Tiered(R)]
    amount_low: Annotated[Decimal | None, Tiered(R)]
    amount_high: Annotated[Decimal | None, Tiered(R)]
    cadence: Annotated[str | None, Tiered(P)]
    seen: Annotated[int | None, Tiered(P)]
    last_seen: Annotated[dt.date | None, Tiered(P)]
    payee_slug: Annotated[str | None, Tiered(P)]
    obligation_ref: Annotated[str | None, Tiered(P)]
    obligation_kind: Annotated[str | None, Tiered(P)]
    period_label: Annotated[str | None, Tiered(P)]


def predicted(
    found: Sequence[Run],
    data_end: Mapping[str, dt.date],
    today: dt.date,
    days: int,
    min_count: int,
) -> list[Charge]:
    """The expected and likely-charged steps of every regular, still-running run."""
    end = today + dt.timedelta(days=days)
    out: list[Charge] = []
    for run in found:
        every = cadence(run, min_count)
        through = data_end.get(run.account)
        if every is None or through is None:
            continue
        if (today - through).days > STALE_ACCOUNT:
            continue
        last = run.last
        if every.step(last.posted, 1) < through - dt.timedelta(days=every.grace):
            continue
        tail = [o.amount for o in run.charges[-3:]]
        low, high = min(tail), max(tail)
        n = 1
        while (on := every.step(last.posted, n)) <= end:
            n += 1
            if on < today - dt.timedelta(days=PAST_DAYS):
                continue
            if on <= through:
                # Inside the loaded data, which shows no such charge yet: the grace
                # keeps the run alive, but this step is not one the data cannot see.
                continue
            out.append(
                Charge(
                    kind="expected",
                    state="likely_charged" if on < today else "expected",
                    on=on,
                    days_away=(on - today).days,
                    label=last.label,
                    account=run.account,
                    account_label=run.account_label,
                    currency=run.currency,
                    amount=last.amount,
                    amount_low=None if low == high else low,
                    amount_high=None if low == high else high,
                    cadence=every.name,
                    seen=len(run.charges),
                    last_seen=last.posted,
                    payee_slug=None,
                    obligation_ref=None,
                    obligation_kind=None,
                    period_label=None,
                )
            )
    return out


class ListIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    days: int = Field(default=DEFAULT_DAYS, ge=1, le=MAX_DAYS)


class Total(BaseModel):
    model_config = ConfigDict(frozen=True)
    currency: Annotated[str, Tiered(P)]
    amount: Annotated[Decimal, Tiered(R)]


class AccountData(BaseModel):
    model_config = ConfigDict(frozen=True)
    account: Annotated[str, Tiered(P)]
    account_label: Annotated[str, Tiered(P)]
    data_through: Annotated[dt.date, Tiered(P)]
    predicts: Annotated[bool, Tiered(P)]


class ListOut(BaseModel):
    model_config = ConfigDict(frozen=True)
    as_of: Annotated[str, Tiered(P)]
    today: Annotated[dt.date, Tiered(P)]
    window_days: Annotated[int, Tiered(P)]
    rows: Annotated[list[Charge], Tiered(P)]
    totals: Annotated[list[Total], Tiered(P)]
    overdue_count: Annotated[int, Tiered(P)]
    accounts: Annotated[list[AccountData], Tiered(P)]
    headline: Annotated[list[str], Tiered(R)]


def _due(uow: UnitOfWork, today: dt.date, days: int) -> tuple[list[Charge], int]:
    rows: list[Charge] = []
    overdue = 0
    for row in uow.execute(select(report_owed)).mappings():
        o = figures(Facts.model_validate(dict(row)), today, days)
        if o.days_until_due is None or o.threshold_remaining <= 0 or o.due_date is None:
            continue
        if o.days_until_due < 0:
            overdue += 1
            continue
        if o.days_until_due > days:
            continue
        rows.append(
            Charge(
                kind="due",
                state="due",
                on=o.due_date,
                days_away=o.days_until_due,
                label=o.payee_label,
                account=None,
                account_label=None,
                currency="CAD",
                amount=o.threshold_remaining,
                amount_low=None,
                amount_high=None,
                cadence=None,
                seen=None,
                last_seen=None,
                payee_slug=o.payee_slug,
                obligation_ref=o.obligation_ref,
                obligation_kind=o.kind,
                period_label=o.period_label,
            )
        )
    return rows, overdue


Paid = tuple[str | None, dt.date, Decimal, str | None]


def _unapplied_bill_payments(uow: UnitOfWork) -> set[Paid]:
    """The (account label, date, amount, currency) of each payment still unapplied to
    a payee that has bills: Owed counts that payee's bills, so its payments are not
    predicted too. An unapplied credit names no line, so the line is found by these
    four; a payee with no bill (rent) keeps its payments as predictions."""
    billers = set(
        uow.execute(select(report_owed_trace.c.payee_slug).distinct()).scalars()
    )
    o = report_open_item.c
    return {
        (row.account_label, row.occurred_on, abs(Decimal(row.amount)), row.currency)
        for row in uow.execute(
            select(
                o.account_label, o.occurred_on, o.amount, o.currency, o.payee_slug
            ).where(o.payment_ref.is_not(None), o.amount.is_not(None))
        )
        if row.payee_slug in billers
    }


def list_upcoming(ctx: FinanceContext, uow: UnitOfWork, data: ListIn) -> ListOut:
    today = local_date(ctx.clock.now_utc(), ctx.settings.timezone)
    min_count = ctx.settings.get_int(MIN_COUNT_KEY)
    cited = {
        ref
        for ref in uow.execute(
            select(report_owed_trace.c.txn_ref).where(
                report_owed_trace.c.txn_ref.is_not(None)
            )
        ).scalars()
    }
    paid_bills = _unapplied_bill_payments(uow)
    crypto = set(
        uow.execute(select(report_crypto_movement.c.currency).distinct()).scalars()
    )
    c = report_txn.c
    data_end: dict[str, dt.date] = {}
    labels: dict[str, str] = {}
    outflows: list[Outflow] = []
    for row in uow.execute(
        select(
            c.line_ref,
            c.account,
            c.account_label,
            c.posting_date,
            c.amount,
            c.currency,
            c.display_label,
            c.transfer_state,
            c.exclusion,
        )
    ).mappings():
        if row.account is None or row.posting_date is None:
            continue
        if row.account not in data_end or row.posting_date > data_end[row.account]:
            data_end[row.account] = row.posting_date
        labels[row.account] = row.account_label or row.account
        if (
            row.amount is None
            or row.amount >= 0
            or row.transfer_state is not None
            or row.exclusion is not None
            or row.line_ref in cited
            or (row.account_label, row.posting_date, abs(row.amount), row.currency)
            in paid_bills
            or row.currency in crypto
            or not row.display_label
            or row.display_label == NO_LABEL
        ):
            continue
        outflows.append(
            Outflow(
                account=row.account,
                account_label=row.account_label or row.account,
                posted=row.posting_date,
                amount=-Decimal(row.amount),
                currency=row.currency,
                label=row.display_label,
            )
        )
    expected = predicted(runs(outflows), data_end, today, data.days, min_count)
    due, overdue = _due(uow, today, data.days)
    rows = sorted(
        due + expected,
        key=lambda r: (
            r.on,
            r.kind != "due",
            r.account or "",
            r.label,
            r.currency,
            r.obligation_ref or "",
            r.amount,
        ),
    )
    sums: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for r in rows:
        if r.state != "likely_charged":
            sums[r.currency] += r.amount
    totals = [Total(currency=k, amount=v) for k, v in sorted(sums.items())]
    predicting = {r.account for r in expected}
    stamp = as_of(ctx.clock, ctx.settings.timezone)
    return ListOut(
        as_of=stamp,
        today=today,
        window_days=data.days,
        rows=rows,
        totals=totals,
        overdue_count=overdue,
        accounts=[
            AccountData(
                account=k,
                account_label=labels[k],
                data_through=v,
                predicts=k in predicting,
            )
            for k, v in sorted(data_end.items())
        ],
        headline=[
            stamp,
            f"next {data.days} days: "
            + ("; ".join(f"{t.amount:.2f} {t.currency}" for t in totals) or "nothing"),
            f"rows: {len(rows)}",
        ],
    )


register(
    OperationSpec(
        name="finance.upcoming.list",
        input_model=ListIn,
        output_model=ListOut,
        handler=list_upcoming,
        tool_name="finance_upcoming",
        description="Bills due and recurring charges expected in the next days.",
        safety_class=SafetyClass.READ,
        roles=frozenset(Role),
        connection="reader",
    )
)
