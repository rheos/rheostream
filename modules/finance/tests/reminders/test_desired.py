"""Public reminder seams over invented rows in rolled-back finance_test transactions."""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Callable
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, insert, select

from rheo_finance import catalogue, operations
from rheo_finance.clock import FakeClock
from rheo_finance.context import FinanceContext
from rheo_finance.contracts import Role, SafetyClass
from rheo_finance.operations import OperationError, RoleRefused, dispatch
from rheo_finance.reminders.desired import DesiredOut
from rheo_finance.reminders.policy import PayloadPolicy, PayloadRefused, build_payload
from rheo_finance.settings import parse_settings
from rheo_finance.storage.ids import uuid7
from rheo_finance.storage.schema import obligation, payee
from tests.derive.support import Ledger
from tests.derive.test_payment_application import (
    N1,
    N,
    card_export,
    paid_card,
    statement_n,
    statement_n1,
    with_settings,
)

D = dt.date
M = Decimal
NOW = dt.datetime(2059, 5, 1, 12, tzinfo=dt.UTC)
DUE = D(2059, 5, 21)
FLOOR = '[finance.reminders]\nmin_due_date = "2059-01-01"\n'


def context(
    build: Callable[[Role], FinanceContext],
    *,
    role: Role = Role.OWNER,
    settings: str = FLOOR,
    when: dt.datetime = NOW,
) -> FinanceContext:
    return dataclasses.replace(
        build(role), settings=parse_settings(settings), clock=FakeClock(when)
    )


def desired(ctx: FinanceContext) -> DesiredOut:
    catalogue.load_all()
    out = dispatch("finance.reminder.desired", {}, ctx.role, ctx=ctx)
    assert isinstance(out, DesiredOut)
    return out


def add(
    conn: Connection,
    *,
    period: str = "bill-2059-05",
    due: dt.date | None = DUE,
    status: str = "open",
    before_floor: bool = False,
    threshold: str = "33.27",
    kind: str = "bill",
    label: str = "Sample Utility",
    slug: str = "sample-utility",
) -> None:
    pid = conn.scalar(select(payee.c.id).where(payee.c.slug == slug))
    if pid is None:
        pid = uuid7(NOW)
        conn.execute(
            insert(payee).values(id=pid, slug=slug, display_label=label, kind="vendor")
        )
    conn.execute(
        insert(obligation).values(
            id=uuid7(NOW),
            payee_id=pid,
            kind=kind,
            period_label=period,
            amount_due=M("848.29"),
            minimum_due=M("21.54"),
            threshold_due=M(threshold),
            paid=M("0.00"),
            remaining=M("848.29"),
            threshold_remaining=M(threshold),
            due_date=due,
            status=status,
            primary_source="sample-bills/synthetic.pdf",
            before_floor=before_floor,
        )
    )


def test_floor_unset_refuses_even_without_obligations(
    rolled_back_context: Callable[[Role], FinanceContext],
) -> None:
    with pytest.raises(
        OperationError, match="^set finance.reminders.min_due_date first$"
    ):
        desired(context(rolled_back_context, settings=""))


def test_read_uses_owner_connection_and_refuses_member(
    rolled_back_context: Callable[[Role], FinanceContext],
    owner_conn: Connection,
) -> None:
    add(owner_conn)
    catalogue.load_all()
    spec = operations.SPECS["finance.reminder.desired"]
    assert spec.safety_class is SafetyClass.READ
    assert spec.connection == "owner"
    assert spec.tool_name is None
    assert spec.roles == {Role.OWNER, Role.SERVICE}
    for role in (Role.OWNER, Role.SERVICE):
        assert len(desired(context(rolled_back_context, role=role)).tickets) == 1
    with pytest.raises(RoleRefused):
        desired(context(rolled_back_context, role=Role.MEMBER))


@pytest.mark.parametrize(
    "due,status,before_floor",
    [
        (None, "open", False),
        (D(2058, 12, 31), "open", False),
        (D(2059, 5, 21), "open", True),
        (D(2059, 5, 21), "presumed_paid", False),
        (D(2059, 5, 21), "disputed", False),
    ],
    ids=[
        "undated",
        "before-date-floor",
        "stored-floor-marker",
        "presumed-paid",
        "disputed",
    ],
)
def test_ineligible_obligations_emit_nothing(
    rolled_back_context: Callable[[Role], FinanceContext],
    owner_conn: Connection,
    due: dt.date | None,
    status: str,
    before_floor: bool,
) -> None:
    add(owner_conn, due=due, status=status, before_floor=before_floor)
    assert desired(context(rolled_back_context)).tickets == []


@pytest.mark.parametrize(
    "status,threshold", [("partial", "0.00"), ("paid", "0.00"), ("superseded", "33.27")]
)
def test_settled_eligible_periods_return_done_for_reconciliation(
    rolled_back_context: Callable[[Role], FinanceContext],
    owner_conn: Connection,
    status: str,
    threshold: str,
) -> None:
    add(owner_conn, status=status, threshold=threshold)
    (ticket,) = desired(context(rolled_back_context)).tickets
    assert ticket.status == "done"
    assert ticket.severity == "normal"


def test_floor_is_inclusive_and_contract_contains_only_policy_content(
    rolled_back_context: Callable[[Role], FinanceContext],
    owner_conn: Connection,
) -> None:
    add(owner_conn, due=D(2059, 1, 1), status="partial", threshold="10.77")
    (ticket,) = desired(context(rolled_back_context)).tickets
    assert ticket.model_dump() == {
        "payee_label": "Sample Utility",
        "amount": M("10.77"),
        "due_date": D(2059, 1, 1),
        "period": "bill-2059-05",
        "source_ref": "finance:sample-utility:bill-2059-05",
        "title": "Sample Utility: 10.77 CAD due 2059-01-01",
        "body": "bill-2059-05: 10.77 CAD",
        "status": "open",
        "wake": None,
        "area": "bills",
        "ticket_type": "finance-bill",
        "severity": "high",
        "provenance": "status-poll",
    }


@pytest.mark.parametrize(
    "zone,due,when,wake",
    [
        (
            "Europe/Berlin",
            D(2059, 4, 3),
            "2059-03-01T12:00:00+00:00",
            "2059-03-29T07:00:00+00:00",
        ),
        (
            "Europe/Berlin",
            D(2059, 4, 4),
            "2059-03-01T12:00:00+00:00",
            "2059-03-30T06:00:00+00:00",
        ),
        (
            "Etc/GMT-9",
            D(2059, 5, 21),
            "2059-05-15T23:00:00+00:00",
            "2059-05-15T23:00:00+00:00",
        ),
        ("Etc/GMT-9", D(2059, 5, 21), "2059-05-15T23:00:01+00:00", None),
    ],
    ids=["winter-offset", "spring-offset", "wake-now", "wake-passed"],
)
def test_local_wake_converts_to_utc_and_passed_wake_is_absent(
    rolled_back_context: Callable[[Role], FinanceContext],
    owner_conn: Connection,
    zone: str,
    due: dt.date,
    when: str,
    wake: str | None,
) -> None:
    add(owner_conn, due=due)
    settings = FLOOR + f'[finance.locale]\ntimezone = "{zone}"\n'
    (ticket,) = desired(
        context(
            rolled_back_context, settings=settings, when=dt.datetime.fromisoformat(when)
        )
    ).tickets
    assert ticket.wake == (dt.datetime.fromisoformat(wake) if wake else None)
    assert ticket.severity == "normal"


def test_wake_settings_and_overdue_use_local_date(
    rolled_back_context: Callable[[Role], FinanceContext],
    owner_conn: Connection,
) -> None:
    add(owner_conn, due=D(2059, 5, 21))
    settings = (
        FLOOR + 'wake_days_before = 2\nwake_time = "09:30"\n'
        '[finance.locale]\ntimezone = "Europe/Berlin"\n'
    )
    (ticket,) = desired(context(rolled_back_context, settings=settings)).tickets
    assert ticket.wake == dt.datetime(2059, 5, 19, 7, 30, tzinfo=dt.UTC)
    for when, expected in [
        ("2059-05-21T21:59:59+00:00", "normal"),
        ("2059-05-21T22:00:00+00:00", "high"),
    ]:
        (ticket,) = desired(
            context(
                rolled_back_context,
                settings=settings,
                when=dt.datetime.fromisoformat(when),
            )
        ).tickets
        assert ticket.severity == expected


def test_card_minimum_stays_on_its_statement_when_next_statement_loads(
    own: Ledger,
) -> None:
    minimum = paid_card(D(2058, 3, 26), "21.54")
    with_settings(own, '[finance.reminders]\nmin_due_date = "2058-01-01"\n')
    own.ctx = dataclasses.replace(
        own.ctx, clock=FakeClock(dt.datetime(2058, 3, 30, 12, tzinfo=dt.UTC))
    )
    statement_n(own)
    card_export(own, minimum)
    own.derive()
    first = {t.source_ref.rsplit(":", 1)[1]: t for t in desired(own.ctx).tickets}
    assert first[N].status == "done"
    assert ": 0.00 CAD due " in first[N].title
    statement_n1(own, minimum)
    own.derive()
    after = {t.source_ref.rsplit(":", 1)[1]: t for t in desired(own.ctx).tickets}
    assert after[N].status == "done"
    assert after[N1].status == "open"
    assert ": 24.73 CAD due " in after[N1].title
    assert after[N1].wake == dt.datetime(2058, 4, 21, 15, tzinfo=dt.UTC)


def test_builder_accepts_only_the_typed_four_field_policy() -> None:
    policy = PayloadPolicy(
        payee_label="Sample Utility",
        amount=M("10.77"),
        due_date=D(2059, 5, 21),
        period="bill-2059-05",
    )
    payload = build_payload(policy, registered_payees={"Sample Utility"})
    assert payload.title == "Sample Utility: 10.77 CAD due 2059-05-21"
    assert payload.body == "bill-2059-05: 10.77 CAD"
    with pytest.raises(PayloadRefused):
        build_payload(policy.model_dump(), registered_payees={"Sample Utility"})  # type: ignore[arg-type]  # Deliberate untyped boundary probe.


@pytest.mark.parametrize("extra", ["memo", "balance", "account_number", "counterparty"])
def test_typed_policy_refuses_restricted_extra_fields(extra: str) -> None:
    with pytest.raises(ValidationError):
        PayloadPolicy.model_validate(
            {
                "payee_label": "Sample Utility",
                "amount": M("10.77"),
                "due_date": D(2059, 5, 21),
                "period": "bill-2059-05",
                extra: "private free text",
            }
        )


@pytest.mark.parametrize(
    "label,period,amount,registered",
    [
        ("Sample 999999", "bill-2059-05", "10.77", True),
        ("Sample 9999 9999 9999 9991", "bill-2059-05", "10.77", True),
        ("Sample 9999-99**-****-9991", "bill-2059-05", "10.77", True),
        ("Sample Utility", "999999", "10.77", True),
        ("Sample Utility", "memo with private text", "10.77", True),
        ("Sample Utility", "bill:2059-05", "10.77", True),
        ("Sample Utility", "bill-2059-05", "829694.19", True),
        ("Sample Counterparty", "bill-2059-05", "10.77", False),
    ],
    ids=[
        "digit-run",
        "grouped-card",
        "masked-card",
        "period-digits",
        "memo-period",
        "colon-period",
        "amount-digits",
        "unregistered-counterparty",
    ],
)
def test_builder_rejects_restricted_shapes_even_when_label_is_registered(
    label: str,
    period: str,
    amount: str,
    registered: bool,
) -> None:
    policy = PayloadPolicy(
        payee_label=label, amount=M(amount), due_date=D(2059, 5, 21), period=period
    )
    with pytest.raises(PayloadRefused):
        build_payload(
            policy, registered_payees={label} if registered else {"Sample Utility"}
        )


@pytest.mark.parametrize(
    "shape",
    [
        "9999 9999 9999 9991",
        "xxxx-xxxx-xxxx-9991",
        "XXXX XXXX XXXX 9991",
        "****-****-****-9991",
        "9999 9999 9999 1",
        "9999 9999 9999 9999 991",
    ],
    ids=[
        "grouped",
        "masked-x",
        "masked-upper-x",
        "masked-star",
        "13-digits",
        "19-digits",
    ],
)
@pytest.mark.parametrize(
    "prefix,suffix",
    [("Sample", ""), ("", "Sample"), ("Sample", "Vendor")],
    ids=["prefix", "suffix", "both-sides"],
)
def test_builder_refuses_card_shapes_attached_to_registered_label_words(
    shape: str, prefix: str, suffix: str
) -> None:
    label = f"{prefix}{shape}{suffix}"
    policy = PayloadPolicy(
        payee_label=label,
        amount=M("10.77"),
        due_date=D(2059, 5, 21),
        period="bill-2059-05",
    )
    with pytest.raises(PayloadRefused, match="^restricted shape in reminder payload$"):
        build_payload(policy, registered_payees={label})


@pytest.mark.parametrize(
    "shape",
    [
        "９９９９ ９９９９ ９９９９ ９９９１",
        "٩٩٩٩-٩٩٩٩-٩٩٩٩-٩٩٩١",
        "9999 ９９９９ ٩٩٩٩ 9991",
        "xxxx-xxxx-xxxx-９９９１",
        "9999\u00a09999\u00a09999\u00a09991",
        "9999\u20099999\u20099999\u20099991",
        "9999\u202f9999\u202f9999\u202f9991",
        "••••-••••-••••-9991",
        "••••\u00a0••••\u00a0••••\u00a09991",
        "９９９９\u2009٩٩٩٩\u00a09999\u202f9991",
    ],
    ids=[
        "fullwidth",
        "arabic-decimal",
        "mixed-decimal",
        "masked-decimal",
        "nbsp-groups",
        "thin-space-groups",
        "narrow-nbsp-groups",
        "bullet-mask",
        "bullet-nbsp-groups",
        "mixed-decimal-space-groups",
    ],
)
@pytest.mark.parametrize(
    "prefix,suffix",
    [("Sample", ""), ("", "Sample"), ("Sample", "Vendor")],
    ids=["prefix", "suffix", "both-sides"],
)
def test_builder_refuses_unicode_decimal_card_shapes_in_registered_labels(
    shape: str, prefix: str, suffix: str
) -> None:
    label = f"{prefix}{shape}{suffix}"
    policy = PayloadPolicy(
        payee_label=label,
        amount=M("10.77"),
        due_date=D(2059, 5, 21),
        period="bill-2059-05",
    )
    with pytest.raises(PayloadRefused, match="^restricted shape in reminder payload$"):
        build_payload(policy, registered_payees={label})


@pytest.mark.parametrize("amount", ["-0.01", "NaN", "Infinity", "0.001"])
def test_policy_refuses_invalid_amounts(amount: str) -> None:
    with pytest.raises(ValidationError):
        PayloadPolicy(
            payee_label="Sample Utility",
            amount=M(amount),
            due_date=D(2059, 5, 21),
            period="bill-2059-05",
        )


@pytest.mark.parametrize(
    "label",
    [
        "Sample 999999",
        "Sample9999 9999 9999 9991",
        "Samplexxxx-xxxx-xxxx-9991",
        "Sample９９９９ ９９９９ ９９９９ ９９９１",
    ],
    ids=[
        "digit-run",
        "attached-grouped-card",
        "attached-masked-card",
        "attached-unicode-card",
    ],
)
def test_operation_applies_payload_policy_without_leaking_bad_input(
    rolled_back_context: Callable[[Role], FinanceContext],
    owner_conn: Connection,
    label: str,
) -> None:
    add(owner_conn, label=label)
    with pytest.raises(PayloadRefused) as caught:
        desired(context(rolled_back_context))
    assert label not in str(caught.value)


@pytest.mark.parametrize("slug", ["999999", "9999-9999-9999-9991"])
def test_operation_refuses_unsafe_source_keys(
    rolled_back_context: Callable[[Role], FinanceContext],
    owner_conn: Connection,
    slug: str,
) -> None:
    add(owner_conn, slug=slug)
    with pytest.raises(PayloadRefused):
        desired(context(rolled_back_context))


def test_builder_revalidates_a_constructed_policy_without_echoing_input() -> None:
    policy = PayloadPolicy.model_construct(
        payee_label="Sample Utility",
        amount=M("-0.01"),
        due_date=D(2059, 5, 21),
        period="bill-2059-05",
    )
    with pytest.raises(PayloadRefused, match="^invalid reminder payload policy$"):
        build_payload(policy, registered_payees={"Sample Utility"})


def test_policy_refuses_float_money() -> None:
    with pytest.raises(ValidationError):
        PayloadPolicy.model_validate(
            {
                "payee_label": "Sample Utility",
                "amount": 12.5,
                "due_date": D(2059, 5, 21),
                "period": "bill-2059-05",
            }
        )
