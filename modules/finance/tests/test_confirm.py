"""Confirm's public operation and CLI seams over rolled-back synthetic ledgers."""

from __future__ import annotations

import dataclasses
import io
from collections.abc import Callable
from decimal import Decimal

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select, update

from rheo_finance import catalogue, composition, operations, queue
from rheo_finance.cli.main import main
from rheo_finance.context import FinanceContext, LineSnapshot, UnitOfWork
from rheo_finance.contracts import Role, SafetyClass
from rheo_finance.derive.snapshot import txn_snapshot
from rheo_finance.operations import OperationError
from rheo_finance.parsers import docextract_adapter
from rheo_finance.storage.schema import (
    decision,
    decision_orphan,
    document_line,
    obligation,
    pair_edge,
    payment,
    payment_application,
    payment_evidence,
    queue_count,
    txn,
)
from tests.derive.support import D, Ledger, read_as_text
from tests.derive.test_orphans import parser_change, record, ref, setup
from tests.derive.test_payment_application import bills, neem_statement, with_settings
from tests.derive.test_payments import bank, name_card, notice, vendors
from tests.derive.test_presumed_paid import history
from tests.derive.test_transfer_edges import (
    BILL_PAYMENT,
    PAYMENT_RECEIVED,
    file_card,
)

M = Decimal
Run = Callable[..., tuple[int, str, str]]


@pytest.fixture
def prepared(own: Ledger, notice_parser: None) -> Ledger:
    vendors(own)
    name_card(own)
    own.ctx = dataclasses.replace(own.ctx, snapshot_provider=txn_snapshot)
    catalogue.load_all()
    return own


@pytest.fixture
def cli(
    prepared: Ledger,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[Run, list[str]]:
    calls: list[str] = []
    monkeypatch.setattr(composition, "build_context", lambda role: prepared.ctx)
    monkeypatch.setattr(docextract_adapter, "pdf_text", read_as_text)
    original = operations.SPECS["finance.ledger.derive"]

    def derive(ctx: FinanceContext, uow: UnitOfWork, data: BaseModel) -> BaseModel:
        calls.append("derive")
        return original.handler(ctx, uow, data)

    monkeypatch.setitem(
        operations.SPECS,
        "finance.ledger.derive",
        original.model_copy(update={"handler": derive}),
    )

    def run(*args: str, answers: str = "") -> tuple[int, str, str]:
        monkeypatch.setattr("sys.stdin", io.StringIO(answers))
        code = main(["confirm", *args])
        if code == 0 and args[:1] == ("answer",):
            snapshot_matches(prepared)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run, calls


def pending(ledger: Ledger, **payload: object) -> queue.ListOut:
    out = operations.dispatch("finance.queue.list", payload, Role.OWNER, ctx=ledger.ctx)
    assert isinstance(out, queue.ListOut)
    return out


def snapshot_matches(ledger: Ledger) -> None:
    complete = pending(ledger, counts_only=True)
    snapshot = {
        kind: count
        for kind, count in ledger.conn.execute(
            select(queue_count.c.kind, queue_count.c.count)
        )
    }
    assert set(snapshot) == set(queue.KINDS)
    assert snapshot == {row.kind: row.count for row in complete.counts}
    assert sum(snapshot.values()) == complete.total


def ambiguous(ledger: Ledger) -> None:
    bank(ledger, (D(2031, 6, 14), BILL_PAYMENT, M("-216.40")))
    file_card(
        ledger,
        (D(2031, 6, 15), PAYMENT_RECEIVED, M("216.40")),
        (D(2031, 6, 16), PAYMENT_RECEIVED, M("216.40")),
    )
    ledger.derive()


def pick(ledger: Ledger, kind: str) -> queue.Item:
    snapshot_matches(ledger)
    return next(each for each in pending(ledger).items if each.kind == kind)


@pytest.mark.parametrize("answer,kind", [("yes", "pair"), ("no", "unpair")])
def test_pair_answer_records_decision_and_derives_exactly_once(
    prepared: Ledger, cli: tuple[Run, list[str]], answer: str, kind: str
) -> None:
    ambiguous(prepared)
    item = pick(prepared, "pairing")
    run, calls = cli
    code, _, err = run("answer", item.code, answer)
    assert (code, err) == (0, "")
    assert calls == ["derive"]
    assert prepared.conn.scalar(select(func.count()).select_from(decision)) == 1
    assert prepared.conn.scalar(select(decision.c.kind)) == kind
    assert (
        prepared.conn.scalar(
            select(func.count()).select_from(pair_edge).where(pair_edge.c.applied)
        )
        == 1
    )
    assert item.code not in {each.code for each in pending(prepared).items}


def test_changed_evidence_refuses_stale_code_with_no_write_or_derive(
    prepared: Ledger, cli: tuple[Run, list[str]]
) -> None:
    ambiguous(prepared)
    item = pick(prepared, "pairing")
    prepared.conn.execute(update(txn).values(display_label="Corrected display label"))
    run, calls = cli
    code, _, err = run("answer", item.code, "yes")
    assert code == 1
    assert "item changed, run confirm again" in err
    assert not calls
    assert prepared.conn.scalar(select(func.count()).select_from(decision)) == 0


def test_second_competing_answer_becomes_stale_after_first_derives(
    prepared: Ledger, cli: tuple[Run, list[str]]
) -> None:
    ambiguous(prepared)
    choices = [each for each in pending(prepared).items if each.kind == "pairing"]
    assert len(choices) == 2
    run, calls = cli
    assert run("answer", choices[0].code, "yes")[0] == 0
    assert run("answer", choices[1].code, "yes")[0] == 1
    assert calls == ["derive"]
    assert prepared.conn.scalar(select(func.count()).select_from(decision)) == 1


def test_invalid_answer_refuses_without_deriving(
    prepared: Ledger, cli: tuple[Run, list[str]]
) -> None:
    ambiguous(prepared)
    run, calls = cli
    assert run("answer", pick(prepared, "pairing").code, "invalid")[0] == 1
    assert calls == []
    assert prepared.conn.scalar(select(func.count()).select_from(decision)) == 0


def transfers(ledger: Ledger, count: int = 12) -> None:
    bank(
        ledger,
        *(
            (
                D(2031, 6, n + 1),
                f"CAROB E-SEND  receiver = SAMPLE PARTY {n}",
                -M(n + 1),
            )
            for n in range(count)
        ),
    )
    ledger.derive()


def test_amount_order_and_ten_item_interactive_cap(
    prepared: Ledger, cli: tuple[Run, list[str]]
) -> None:
    with_settings(prepared, "[finance.classify]\nbatch_size = 25\n")
    transfers(prepared)
    snapshot_matches(prepared)
    out = pending(prepared)
    assert out.total == 12
    assert out.batch_size == 10
    assert [each.amount_at_stake for each in out.items] == list(
        map(M, range(12, 0, -1))
    )
    assert all(each.recommendation is None and not each.choices for each in out.items)
    run, calls = cli
    code, text, err = run(answers="skip\n" * 12)
    assert (code, err) == (0, "")
    assert text.count("Category for this e-Transfer line?") == 10
    assert text.count("Recommendation:") == 0
    assert calls == []


def test_line_keyed_category_has_no_recommendation_and_survives_derive(
    prepared: Ledger, cli: tuple[Run, list[str]]
) -> None:
    transfers(prepared, 1)
    item = pick(prepared, "category")
    assert item.recommendation is None
    run, calls = cli
    assert run("answer", item.code, "groceries")[0] == 0
    row = prepared.conn.execute(select(decision)).one()
    assert row.kind == "category" and row.merchant_key is None
    assert row.target_ref.startswith("finance.txn:")
    assert prepared.conn.scalar(select(txn.c.category)) == "groceries"
    assert calls == ["derive"]
    assert pending(prepared).total == 0


def test_counts_only_returns_no_item_text_and_does_not_read_stdin(
    prepared: Ledger, cli: tuple[Run, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    transfers(prepared, 1)
    assert pending(prepared, counts_only=True).items == []
    run, calls = cli
    code, text, err = run("--counts")
    assert (code, err) == (0, "")
    assert "category: 1" in text
    assert "SAMPLE" not in text and "finance.txn:" not in text
    assert "Recommendation:" not in text and " at stake" not in text
    assert calls == []


def test_queue_read_permissions_and_no_tool_export(prepared: Ledger) -> None:
    spec = operations.SPECS["finance.queue.list"]
    assert spec.safety_class is SafetyClass.READ
    assert spec.connection == "owner" and spec.tool_name is None
    assert spec.roles == {Role.OWNER, Role.SERVICE}
    for role in (Role.MEMBER, Role.SERVICE):
        ctx = dataclasses.replace(prepared.ctx, role=role)
        name = "finance.queue.list" if role is Role.MEMBER else "finance.confirm.answer"
        with pytest.raises(operations.RoleRefused):
            operations.dispatch(name, {}, role, ctx=ctx)


@pytest.mark.parametrize("answer", ["yes", "no"])
def test_held_notice_pair_answer_reuses_notice_and_money_contract(
    prepared: Ledger, cli: tuple[Run, list[str]], answer: str
) -> None:
    bank(prepared, (D(2031, 6, 22), "SAMPLE ISP", M("-216.40")))
    notice(prepared, D(2031, 6, 14))
    prepared.derive()
    item = pick(prepared, "held_notice")
    run, calls = cli
    assert run("answer", item.code, answer)[0] == 0
    assert calls == ["derive"]
    assert prepared.conn.scalar(select(func.count()).select_from(payment)) == (
        1 if answer == "yes" else 2
    )
    assert not any(each.kind == "held_notice" for each in pending(prepared).items)


def test_card_answer_changes_owed_after_exactly_one_derive(
    prepared: Ledger, cli: tuple[Run, list[str]]
) -> None:
    prepared.file(
        "alder-neem/2031-06-01.pdf",
        neem_statement(
            D(2031, 5, 6), D(2031, 6, 1), M("432.80"), (), M("21.64"), D(2031, 6, 29)
        ),
    )
    bank(prepared, (D(2031, 6, 14), BILL_PAYMENT, M("-216.40")))
    prepared.derive()
    assert prepared.conn.scalar(select(payment.c.state)) == "confirm_only"
    item = pick(prepared, "card_debit")
    run, calls = cli
    assert run("answer", item.code, "yes")[0] == 0
    assert calls == ["derive"]
    assert prepared.conn.scalar(select(decision.c.kind)) == "payment_apply"
    assert prepared.conn.scalar(select(payment.c.state)) == "active"
    assert prepared.conn.scalar(select(obligation.c.remaining)) == M("216.40")
    assert prepared.conn.scalar(select(obligation.c.threshold_remaining)) == 0


def test_payment_answer_chooses_between_current_live_targets(
    prepared: Ledger, cli: tuple[Run, list[str]]
) -> None:
    bills(
        prepared,
        ("isp", "216.40", D(2031, 6, 1), D(2031, 6, 24)),
        ("isp", "171.92", D(2031, 7, 1), D(2031, 7, 24)),
    )
    bank(prepared, (D(2031, 7, 14), "SAMPLE ISP", M("-201.75")))
    prepared.derive()
    item = pick(prepared, "payment")
    assert [each.answer for each in item.choices] == ["1", "2", "no"]
    assert item.recommendation is None
    run, calls = cli
    assert run("answer", item.code, "1")[0] == 0
    assert calls == ["derive"]
    assert prepared.conn.scalar(select(func.sum(obligation.c.paid))) == M("201.75")
    assert prepared.conn.scalar(select(payment_application.c.auto)) is False


def test_presumed_paid_queue_uses_existing_coverage_candidates(
    prepared: Ledger, cli: tuple[Run, list[str]]
) -> None:
    history(prepared)
    prepared.derive()
    item = pick(prepared, "presumed_paid")
    run, calls = cli
    assert run("answer", item.code, "yes")[0] == 0
    assert calls == ["derive"]
    assert (
        prepared.conn.scalar(
            select(func.count())
            .select_from(obligation)
            .where(obligation.c.status == "presumed_paid")
        )
        == 1
    )


@pytest.mark.parametrize("answer", ["yes", "no"])
def test_orphan_category_and_pair_offer_stored_fields_and_complete_replacements(
    prepared: Ledger,
    cli: tuple[Run, list[str]],
    card_parsers: None,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    setup(prepared)
    category = record(
        prepared,
        kind="category",
        target_ref=ref(prepared, "-9.28"),
        category="groceries",
        tag="personal",
    )
    paired = record(
        prepared,
        kind="pair",
        target_ref=ref(prepared, "-21.64"),
        counterpart_ref=ref(prepared, "21.64"),
    )
    prepared.derive()
    parser_change(monkeypatch)
    prepared.derive()
    out = pending(prepared, orphans=True)
    snapshot_matches(prepared)
    assert len(out.items) == 2
    assert {each.kind for each in out.items} == {"reattach"}
    assert all(each.recommendation == "yes" for each in out.items)
    assert any("groceries" in line for each in out.items for line in each.evidence)
    outputs: list[queue.AnswerOut] = []
    original_handler = operations.SPECS["finance.confirm.answer"]

    def observe(
        ctx: FinanceContext, uow: UnitOfWork, data: queue.AnswerIn
    ) -> queue.AnswerOut:
        result = original_handler.handler(ctx, uow, data)
        assert isinstance(result, queue.AnswerOut)
        outputs.append(result)
        return result

    monkeypatch.setitem(
        operations.SPECS,
        "finance.confirm.answer",
        original_handler.model_copy(update={"handler": observe}),
    )
    run, calls = cli
    for item in out.items:
        assert run("answer", item.code, answer)[0] == 0
    assert calls == ["derive", "derive"]
    assert sorted(each.recorded for each in outputs) == (
        [1, 1] if answer == "yes" else [1, 2]
    )
    assert all(len(each.decision_ids) == each.recorded for each in outputs)
    assert pending(prepared, orphans=True).items == []
    assert set(prepared.conn.scalars(select(txn.c.orphan_candidate))) == {False}
    rows = prepared.conn.execute(
        select(decision).where(decision.c.about_decision_id.in_((category, paired)))
    ).all()
    assert len(rows) == (2 if answer == "yes" else 3)
    if answer == "yes":
        assert {row.kind for row in rows} == {"category", "pair"}
        assert (
            prepared.conn.scalar(select(func.count()).select_from(decision_orphan)) == 0
        )
    else:
        assert {row.kind for row in rows} == {"reattach_decline"}
        assert {
            row.candidate_txn_id
            for row in prepared.conn.execute(select(decision_orphan))
        } == {None}
        assert (
            prepared.conn.scalar(select(func.count()).select_from(decision_orphan)) == 2
        )


def test_answer_recomputes_after_preparation_and_rolls_back_invalid_category(
    prepared: Ledger, cli: tuple[Run, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    transfers(prepared, 1)
    item = pick(prepared, "category")
    # A new decision between listing and answering is visible in answer's own UoW,
    # even before derive has had a chance to replace the derived table.
    operations.dispatch(
        "finance.decision.record",
        {
            "kind": "category",
            "target_ref": item.evidence[1].split(" ", 1)[0],
            "category": "transport",
        },
        Role.OWNER,
        ctx=prepared.ctx,
    )
    run, calls = cli
    assert run("answer", item.code, "groceries")[0] == 1
    assert calls == []
    assert prepared.conn.scalar(select(func.count()).select_from(decision)) == 1


def test_same_evidence_has_same_codes_and_setting_can_reduce_batch(
    prepared: Ledger,
) -> None:
    transfers(prepared, 4)
    first = pending(prepared)
    assert first == pending(prepared)
    with_settings(prepared, "[finance.classify]\nbatch_size = 3\n")
    assert pending(prepared).batch_size == 3


def test_interactive_answer_refreshes_the_next_item(
    prepared: Ledger, cli: tuple[Run, list[str]]
) -> None:
    ambiguous(prepared)
    run, calls = cli
    code, text, err = run(answers="yes\nskip\n")
    assert (code, err) == (0, "")
    assert calls == ["derive"]
    assert text.count("Pair these lines?") == 1


def test_card_decline_keeps_the_obligation_unpaid(
    prepared: Ledger, cli: tuple[Run, list[str]]
) -> None:
    prepared.file(
        "alder-neem/2031-06-01.pdf",
        neem_statement(
            D(2031, 5, 6), D(2031, 6, 1), M("432.80"), (), M("21.64"), D(2031, 6, 29)
        ),
    )
    bank(prepared, (D(2031, 6, 14), BILL_PAYMENT, M("-216.40")))
    prepared.derive()
    run, calls = cli
    assert run("answer", pick(prepared, "card_debit").code, "no")[0] == 0
    assert calls == ["derive"]
    assert prepared.conn.scalar(select(decision.c.kind)) == "payment_unapply"
    assert prepared.conn.scalar(select(obligation.c.remaining)) == M("432.80")
    assert (
        prepared.conn.scalar(select(payment_application.c.reason)) == "payment_unapply"
    )


def test_category_yes_is_refused_without_a_recommendation(
    prepared: Ledger, cli: tuple[Run, list[str]]
) -> None:
    transfers(prepared, 1)
    run, calls = cli
    assert run("answer", pick(prepared, "category").code, "yes")[0] == 1
    assert not calls
    assert prepared.conn.scalar(select(func.count()).select_from(decision)) == 0


def test_payment_changed_target_evidence_refuses_old_code(
    prepared: Ledger, cli: tuple[Run, list[str]]
) -> None:
    bills(
        prepared,
        ("isp", "216.40", D(2031, 6, 1), D(2031, 6, 24)),
        ("isp", "171.92", D(2031, 7, 1), D(2031, 7, 24)),
    )
    bank(prepared, (D(2031, 7, 14), "SAMPLE ISP", M("-201.75")))
    prepared.derive()
    item = pick(prepared, "payment")
    prepared.conn.execute(update(obligation).values(remaining=M("10.64")))
    run, calls = cli
    assert run("answer", item.code, "1")[0] == 1
    assert not calls
    assert prepared.conn.scalar(select(func.count()).select_from(decision)) == 0


def test_multi_line_decline_failure_rolls_back_all_rows(
    prepared: Ledger,
    card_parsers: None,
    monkeypatch: pytest.MonkeyPatch,
    cli: tuple[Run, list[str]],
) -> None:
    setup(prepared)
    record(
        prepared,
        kind="pair",
        target_ref=ref(prepared, "-21.64"),
        counterpart_ref=ref(prepared, "21.64"),
    )
    prepared.derive()
    parser_change(monkeypatch)
    prepared.derive()
    item = pending(prepared, orphans=True).items[0]
    snapshots = 0

    def refuse_second(uow: UnitOfWork, target: str) -> LineSnapshot | None:
        nonlocal snapshots
        snapshots += 1
        if snapshots == 2:
            raise OperationError("snapshot unavailable")
        return txn_snapshot(uow, target)

    # The provider is the public decision seam; failure after the first row must
    # roll back the entire answer transaction and must not dispatch derive.
    ctx = dataclasses.replace(prepared.ctx, snapshot_provider=refuse_second)
    monkeypatch.setattr(composition, "build_context", lambda role: ctx)
    run, calls = cli
    assert run("answer", item.code, "no")[0] == 1
    assert snapshots == 2
    assert not calls
    assert prepared.conn.scalar(select(func.count()).select_from(decision)) == 1


@pytest.mark.parametrize(
    "printed", ["Sample Sumac", "sAmPlE   oLiVe", "SAMPLE\u00a0HENNA"]
)
def test_confirm_and_queue_withhold_source_transfer_names(
    prepared: Ledger, cli: tuple[Run, list[str]], printed: str
) -> None:
    bank(
        prepared,
        (D(2031, 6, 14), f"CAROB E-SEND  receiver = {printed}", M("-14.65")),
    )
    prepared.derive()
    out = pending(prepared)
    assert out.total == 1 and out.items[0].kind == "category"
    safe = out.model_dump_json().casefold()
    assert "e-transfer (name withheld)" in safe
    assert not any(name in safe for name in ("sumac", "olive", "henna"))
    assert "description_norm" not in safe and "merchant_key" not in safe
    run, calls = cli
    code, text, err = run(answers="skip\n")
    assert (code, err) == (0, "")
    assert "e-Transfer (name withheld)" in text
    assert not any(name in text.casefold() for name in ("sumac", "olive", "henna"))
    assert run("--counts")[0] == 0
    assert not calls


def test_notice_evidence_withholds_source_names_and_sender_streams(
    prepared: Ledger, cli: tuple[Run, list[str]]
) -> None:
    bank(prepared, (D(2031, 6, 22), "SAMPLE ISP", M("-216.40")))
    notice(prepared, D(2031, 6, 14), printed="SAMPLE ISP sAmPlE   SuMaC")
    prepared.derive()
    before = pending(prepared)
    assert any(each.kind == "held_notice" for each in before.items)
    prepared.conn.execute(
        update(document_line)
        .where(document_line.c.line_kind == "payment_notice")
        .values(
            description_raw="PAYMENT FROM sAmPlE   SuMaC",
            description_norm="payment from sample sumac",
        )
    )
    prepared.conn.execute(
        update(payment_evidence).values(
            stream_key="notice:SaMpLe.OlIvE@example.invalid"
        )
    )
    out = pending(prepared)
    # Restricted wording and sender streams are not displayed evidence, so their
    # edits cannot change the displayed digest while safe refs and dates are fixed.
    assert [each.code for each in out.items] == [each.code for each in before.items]
    safe = out.model_dump_json().casefold()
    assert not any(name in safe for name in ("sumac", "olive", "example.invalid"))
    assert "description_norm" not in safe and "stream_key" not in safe
    assert "Our net" in out.model_dump_json()
    run, calls = cli
    code, text, err = run(answers="skip\n")
    assert (code, err) == (0, "")
    assert not any(
        name in text.casefold() for name in ("sumac", "olive", "example.invalid")
    )
    assert "stream_key" not in text
    assert not calls


def test_orphan_evidence_does_not_reprint_legacy_merchant_keys_or_notes(
    prepared: Ledger,
    cli: tuple[Run, list[str]],
    card_parsers: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup(prepared)
    original = record(
        prepared,
        kind="category",
        merchant_key="SaMpLe   SuMaC legacy key",
        newest_line_ref=ref(prepared, "-9.28"),
        category="groceries",
        tag="personal",
        note="sAmPlE   SuMaC private note",
    )
    prepared.derive()
    parser_change(monkeypatch)
    prepared.derive()
    # Legacy text stays in the immutable record, while re-attachment shows the
    # required owner answer fields, safe references and the candidate display label.
    out = pending(prepared, orphans=True)
    assert len(out.items) == 1
    safe = out.model_dump_json().casefold()
    assert "sumac" not in safe and "private note" not in safe
    assert "groceries" in safe and "personal" in safe and "display_label" in safe
    assert "merchant_digest" in safe and "merchant_key" not in safe
    assert str(original) in safe
    run, calls = cli
    code, text, err = run("--orphans", answers="skip\n")
    assert (code, err) == (0, "")
    assert "sumac" not in text.casefold()
    assert not calls
