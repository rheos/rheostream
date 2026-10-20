"""Short IDs (#345): the tail parsing and the shortest-unique-tail rule, no storage."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from rheo_contracts import RecordRef
from rheo_core.refs.short import short_ids, shortest_tail

# Two ids from the same millisecond: a UUIDv7 head is its timestamp, so they share it.
SAME_MINUTE_A = UUID("01a126c1-02cc-7d5b-a113-4d0fef1c1302")
SAME_MINUTE_B = UUID("01a126c1-02cc-7e40-9c07-b2a95d3e88f1")


def test_the_short_id_is_the_tail_not_the_timestamp_head() -> None:
    assert SAME_MINUTE_A.hex[:12] == SAME_MINUTE_B.hex[:12]
    assert RecordRef.short_id(SAME_MINUTE_A) == "ef1c1302"
    assert RecordRef.short_id(SAME_MINUTE_B) == "5d3e88f1"


def test_tail_parsing_takes_typed_and_bare_forms() -> None:
    tail = RecordRef.tail
    assert tail("ef1c1302", "leads", "opportunity") == "ef1c1302"
    assert tail("EF1C1302", "leads", "opportunity") == "ef1c1302"
    assert tail("leads.opportunity:ef1c1302", "leads", "opportunity") == "ef1c1302"
    assert tail("a113-4d0fef1c1302", "leads", "opportunity") == "a1134d0fef1c1302"
    # Too short, not hex, or typed as something else.
    assert tail("ef1c130", "leads", "opportunity") is None
    assert tail("ef1c13zz", "leads", "opportunity") is None
    assert tail("leads.observation:ef1c1302", "leads", "opportunity") is None
    assert RecordRef.short("leads.opportunity:ef1c1302") == (
        "leads",
        "opportunity",
        "ef1c1302",
    )
    assert RecordRef.short("ef1c1302") is None


def test_a_shared_tail_lengthens_like_a_git_short_hash() -> None:
    a = UUID("01a126c1-02cc-7d5b-a113-4d0fef1c1302")
    clash = UUID("01a126c1-02cc-7000-8000-00aaef1c1302")  # shares 8, not 10
    deeper = UUID("01a126c1-02cc-7000-8000-000fef1c1302")  # shares 10, not 12
    assert shortest_tail(a, [a]) == "ef1c1302"
    assert shortest_tail(a, [a, clash]) == "0fef1c1302"
    assert shortest_tail(clash, [a, clash]) == "aaef1c1302"
    assert shortest_tail(a, [a, clash, deeper]) == "4d0fef1c1302"
    assert shortest_tail(deeper, [a, clash, deeper]) == "000fef1c1302"


def test_short_ids_asks_the_lookup_once_for_every_possible_rival() -> None:
    a = UUID("01a126c1-02cc-7d5b-a113-4d0fef1c1302")
    other = UUID("01a126c1-02cc-7e40-9c07-b2a95d3e88f1")
    rival = UUID("01a126c1-02cc-7000-8000-00aaef1c1302")  # not on the page
    asked: list[tuple[int, list[str]]] = []

    def lookup(uow: Any, length: int, tails: Sequence[str]) -> list[UUID]:
        asked.append((length, list(tails)))
        known = [a, other, rival]
        return [i for i in known if i.hex[-length:] in tails]

    shown = short_ids(None, lookup, [a, other, a])  # type: ignore[arg-type]
    assert shown == {a: "0fef1c1302", other: "5d3e88f1"}
    assert asked == [(8, ["5d3e88f1", "ef1c1302"])]
    assert short_ids(None, lookup, []) == {}  # type: ignore[arg-type]
