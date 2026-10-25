"""The money side of a proposal, with no model and no storage: the hourly bridge, the
scope ladder, the budget's shape, and the figures a draft may state.

All rates come from the owner's pricing policy. A fixed price is the day rate per
honest working-day. When the posting asks for a fixed quote but the listing is typed
hourly, the apply form only takes an hourly rate, and that rate has to agree with the
letter's figure, so it is the day rate spread over the working hours of a day. A plain
hourly listing gets a rate from the scope ladder: the shorter the job, the closer to
the profile's default rate, never under the floor.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from rheo_leads.proposals.profile import PricingPolicy

# A project total at or below this reads as an hourly cap or a rate band.
HOURLY_CAP_CEILING: Final = Decimal(150)
# The scope-fit warning fires when the priced scope reaches this share of the budget.
SCOPE_FIT_RATIO: Final = Decimal("1.2")
SCOPE_FIT_WARNING: Final = (
    "This posting's scope, priced at honest working-days, runs past its stated "
    "budget: the job looks underfunded for what it needs, even on the client's terms."
)
SCOPE_WORDS: Final[Mapping[str, str]] = {
    "day": "a day or two of work",
    "week": "about a week of work",
    "weeks": "a multi-week engagement",
    "ongoing": "an open-ended engagement",
}


def money(value: Decimal | float) -> str:
    return f"${float(value):,.2f}"


def plain(value: Decimal | float) -> str:
    """A number without trailing zeros: 45, 112.5."""
    return f"{float(value):g}"


def budget_is_project_total(
    budget_min: Decimal | None, budget_max: Decimal | None, listing_type: str | None
) -> bool:
    """Whether the budget is a project total, not an hourly cap, a rate band or
    nothing. The listing's own type decides when it is known: a fixed-price
    listing's figure is a total (often "up to" one, with no minimum), an hourly
    listing's never is. Otherwise a single figure with no minimum, or a small one,
    reads as an hourly cap."""
    kind = (listing_type or "").strip().lower()
    if budget_max is None or kind == "hourly":
        return False
    if kind == "fixed":
        return True
    if budget_min is None:
        return False
    return budget_max > HOURLY_CAP_CEILING


def scope_fit_warning(
    amounts: Sequence[Decimal],
    budget_min: Decimal | None,
    budget_max: Decimal | None,
    listing_type: str | None,
) -> str | None:
    if not budget_is_project_total(budget_min, budget_max, listing_type):
        return None
    assert budget_max is not None
    return SCOPE_FIT_WARNING if sum(amounts) >= SCOPE_FIT_RATIO * budget_max else None


def day_rate_hourly(p: PricingPolicy) -> Decimal:
    return p.day_rate / p.hours_per_day


def weeks_rate(p: PricingPolicy) -> Decimal:
    return p.weeks_rate or (day_rate_hourly(p) + p.hourly_floor) / 2


def clamp(p: PricingPolicy, rate: Decimal) -> Decimal:
    return max(p.hourly_floor, min(p.hourly_default, rate))


def working_days(p: PricingPolicy, amounts: Sequence[Decimal]) -> Decimal:
    return round(sum(amounts, Decimal(0)) / p.day_rate, 2)


def ladder_rate(
    p: PricingPolicy, days: Decimal | None, scope_size: str
) -> tuple[Decimal, str]:
    """The rate for a plain hourly bid, from a day count when known, else from the
    analysis's reading of the scope."""
    if days is not None and days > 0:
        if days <= 2:
            return p.hourly_default, "a day or two of work, so the profile rate"
        if days <= 10:
            return (
                day_rate_hourly(p),
                "about a week or two of work, so the day-rate equivalent",
            )
        if days <= 20:
            return (
                weeks_rate(p),
                "a multi-week engagement, bid below the day rate for steady work",
            )
        return p.hourly_floor, "a long engagement, bid at the floor for steady work"
    ladder = {
        "day": p.hourly_default,
        "week": day_rate_hourly(p),
        "weeks": weeks_rate(p),
        "ongoing": p.hourly_floor,
    }
    key = scope_size.strip().lower()
    if key in ladder:
        return ladder[key], f"{SCOPE_WORDS[key]} (scope read from the posting)"
    return day_rate_hourly(p), "the day-rate equivalent (no scope read yet)"


@dataclass(frozen=True)
class Suggestion:
    rate: Decimal
    basis: str
    hours: Decimal | None
    matches_fixed_quote: bool
    working_days: Decimal | None
    fixed_total: Decimal | None
    listing_type: str | None
    asks_quote: bool
    scope_size: str | None
    note: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rate": str(self.rate),
            "basis": self.basis,
            "hours": None if self.hours is None else str(self.hours),
            "matches_fixed_quote": self.matches_fixed_quote,
            "working_days": None
            if self.working_days is None
            else str(self.working_days),
            "fixed_total": None if self.fixed_total is None else str(self.fixed_total),
            "listing_type": self.listing_type,
            "asks_quote": self.asks_quote,
            "scope_size": self.scope_size,
            "note": self.note,
        }


def suggest(
    p: PricingPolicy,
    *,
    listing_type: str | None,
    amounts: Sequence[Decimal],
    analysis: Mapping[str, Any],
) -> Suggestion:
    """The hourly rate for the apply form and why, from the milestones (if any)
    and the last draft's analysis (may be empty)."""
    total = round(sum(amounts, Decimal(0)), 2) if amounts else None
    days = working_days(p, amounts) if amounts else None
    scope_size = str(analysis.get("scope_size") or "")
    asks_quote = analysis.get("asks_quote") is True
    listing = (listing_type or "").strip().lower() or None
    if total is not None and total > 0:
        rate = clamp(p, day_rate_hourly(p))
        hours = round(total / rate, 1)
        basis = (
            f"matches the fixed quote: {money(total)} is about {plain(hours)} tracked "
            f"hours at ${plain(rate)}/hr, the day rate spread over "
            f"{plain(p.hours_per_day)}-hour days"
        )
        matches = True
    else:
        raw, basis = ladder_rate(p, days, scope_size)
        rate, hours, matches = clamp(p, raw), None, False
    if listing == "hourly" and total is not None:
        note: str | None = (
            "The listing is hourly, so the apply form takes this rate; the letter "
            "carries the fixed figure the posting asked for, and the two agree."
        )
    elif listing == "hourly" and asks_quote:
        note = (
            "The posting asks for a fixed quote but the listing is hourly. Suggest "
            "milestones so the letter has a figure and the form rate matches it."
        )
    elif listing == "fixed" and total is None:
        note = (
            "Fixed-price listing with no milestones yet; the form takes the total, "
            "not a rate."
        )
    else:
        note = None
    return Suggestion(
        rate=round(rate, 2),
        basis=basis,
        hours=hours,
        matches_fixed_quote=matches,
        working_days=days,
        fixed_total=total,
        listing_type=listing,
        asks_quote=asks_quote,
        scope_size=scope_size or None,
        note=note,
    )


def figures_block(
    p: PricingPolicy,
    rows: Sequence[tuple[str, Decimal]],
    listing_type: str | None,
    *,
    per_unit: str = "",
) -> str:
    """The milestones as the ONLY amounts a draft may state. The apply form's hourly
    rate is the owner's bookkeeping and never reaches the drafter; a per-unit ask
    makes the figures the first piece's price, never the per-unit rate."""
    amounts = [amount for _, amount in rows]
    total = round(sum(amounts, Decimal(0)), 2)
    days = working_days(p, amounts)
    weeks = math.ceil(days / 5) if days else 0
    lines = [
        "\n\nPRICING FIGURES (the developer's own numbers, computed from honest "
        "working-days at their day rate; these are the ONLY amounts you may state, "
        "and you must not invent, round, or restructure them):"
    ]
    for title, amount in rows:
        stage_days = round(amount / p.day_rate, 2)
        lines.append(f"- {title}: {money(amount)} ({plain(stage_days)} working-days)")
    lines.append(
        f"- Total: {money(total)} for {plain(days)} working-days, about {weeks} "
        f"calendar week{'s' if weeks != 1 else ''} of delivery."
    )
    lines.append(
        "HOW TO USE THEM: where the posting asks for a quote, estimate, price, or "
        "timeline, state the total and the working-day count plainly, name the stages "
        "in one sentence, and stop. Do not apologise for the figure, offer to lower "
        "it, or defer it to a later conversation. A stage that cannot be sized before "
        "the work starts (fixes an audit will find) is still quoted from these figures "
        "as the allowance it represents, with one clause saying that anything beyond "
        "it is quoted separately before it is touched."
    )
    if per_unit:
        lines.append(
            f"PER-{per_unit.upper()} ASK: the posting asks for a price per {per_unit}. "
            "These figures price the FIRST piece only, and that piece carries one-time "
            "setup (look development, style frames, the production pipeline) that "
            f"later {per_unit}s reuse. State the total as the price of that first "
            f"piece. Never call it, or anything derived from it, the per-{per_unit} "
            f"rate, and never say later {per_unit}s cost the same. Say in one plain "
            f"clause that the recurring per-{per_unit} rate leaves out the setup and "
            "is quoted once the first piece has set the look."
        )
    lines.append(
        "The application form's own rate or price field is the developer's to fill. "
        "Never mention the application form, an hourly rate or tracked hours derived "
        "from these figures, or that two figures agree or match."
    )
    if (listing_type or "").strip().lower() == "hourly":
        lines.append("Never present the stages as hourly-rate steps.")
    return "\n".join(lines)
