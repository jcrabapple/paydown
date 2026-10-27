"""Tests for app.bills (pure). Expected values hand-computed."""
from datetime import date

from app.bills import due_dates, match_bills

TODAY = date(2026, 10, 2)


def bill(id=1, amount=14000, due_day: int | None = 5, match_text: str | None = "electric"):
    return {"id": id, "amount_cents": amount, "due_day": due_day, "match_text": match_text}


def txn(txn_id, d, amount=14000, merchant=None, name=None, pending=False):
    return {
        "transaction_id": txn_id,
        "date": d,
        "amount_cents": amount,
        "merchant_name": merchant,
        "name": name,
        "pending": pending,
    }


def test_due_dates_basic():
    b = bill(due_day=5)
    assert due_dates(b, date(2026, 9, 25), date(2026, 11, 9)) == [
        date(2026, 10, 5),
        date(2026, 11, 5),
    ]  # Sep 5 is before the window start


def test_due_dates_none_day():
    assert due_dates(bill(due_day=None), date(2026, 10, 1), date(2026, 10, 31)) == []


def test_due_dates_clamped():
    # due_day 31 -> Feb 28 2027 (non-leap) and Apr 30 2027.
    b = bill(due_day=31)
    assert due_dates(b, date(2027, 2, 1), date(2027, 2, 28)) == [date(2027, 2, 28)]
    assert due_dates(b, date(2027, 4, 1), date(2027, 4, 30)) == [date(2027, 4, 30)]


def test_match_substring_on_merchant():
    r = match_bills(
        [bill()], [txn("t1", "2026-10-05", merchant="ACME Electric Co")], set(), set(), TODAY
    )
    assert r == [
        {"expense_id": 1, "due_date": "2026-10-05", "paid_on": "2026-10-05", "transaction_id": "t1"}
    ]


def test_match_substring_on_name():
    r = match_bills(
        [bill()], [txn("t1", "2026-10-05", name="ACME Electric bill payment")], set(), set(), TODAY
    )
    assert r and r[0]["transaction_id"] == "t1"


def test_match_case_insensitive():
    r = match_bills(
        [bill(match_text="ELECTRIC")], [txn("t1", "2026-10-05", merchant="acme ELECTRIC co")], set(), set(), TODAY
    )
    assert r and r[0]["transaction_id"] == "t1"


def test_seven_day_window_inclusive_exclusive():
    # Due Oct 5: a txn on Oct 12 (delta 7) matches; Oct 13 (delta 8) does not.
    r = match_bills([bill()], [txn("t1", "2026-10-12", merchant="Electric")], set(), set(), TODAY)
    assert r and r[0]["paid_on"] == "2026-10-12"
    r = match_bills([bill()], [txn("t1", "2026-10-13", merchant="Electric")], set(), set(), TODAY)
    assert r == []


def test_closest_txn_wins():
    # Due Oct 5: deltas 1 (Oct 4) and 0 (Oct 5) -> closest is Oct 5.
    r = match_bills(
        [bill()],
        [txn("t1", "2026-10-04", merchant="Electric"), txn("t2", "2026-10-05", merchant="Electric")],
        set(), set(), TODAY,
    )
    assert r[0]["transaction_id"] == "t2"


def test_tie_amount_then_transaction_id():
    # Both Oct 4 and Oct 6 are delta 1 from due Oct 5; lower amount difference
    # wins (14000 vs 14050), then transaction_id.
    r = match_bills(
        [bill()],
        [txn("t9", "2026-10-06", amount=14050, merchant="Electric"),
         txn("t2", "2026-10-04", amount=14000, merchant="Electric")],
        set(), set(), TODAY,
    )
    assert r[0]["transaction_id"] == "t2"


def test_txn_used_once_across_bills():
    # Two identical bills with the same due day: the single txn matches only
    # the first occurrence (lower bill id at the same due date).
    r = match_bills(
        [bill(id=1), bill(id=2)],
        [txn("t1", "2026-10-05", merchant="Electric")],
        set(), set(), TODAY,
    )
    assert [(x["expense_id"], x["transaction_id"]) for x in r] == [(1, "t1")]


def test_each_occurrence_once():
    # Two due dates in the window need two txns; one txn serves only one.
    b = bill(due_day=5)
    r = match_bills(
        [b],
        [txn("t1", "2026-09-05", merchant="Electric"), txn("t2", "2026-10-05", merchant="Electric")],
        set(), set(), TODAY,
    )
    assert sorted(x["due_date"] for x in r) == ["2026-09-05", "2026-10-05"]
    assert {x["transaction_id"] for x in r} == {"t1", "t2"}


def test_already_paid_skipped():
    r = match_bills(
        [bill()], [txn("t1", "2026-10-05", merchant="Electric")],
        {(1, "2026-10-05")}, set(), TODAY,
    )
    assert r == []


def test_used_txn_ids_respected():
    r = match_bills(
        [bill()], [txn("t1", "2026-10-05", merchant="Electric")],
        set(), {"t1"}, TODAY,
    )
    assert r == []


def test_inflow_not_matched():
    # amount_cents <= 0 is money arriving, not a bill outflow.
    r = match_bills(
        [bill()], [txn("t1", "2026-10-05", amount=-14000, merchant="Electric")],
        set(), set(), TODAY,
    )
    assert r == []


def test_pending_txn_allowed():
    r = match_bills(
        [bill()], [txn("t1", "2026-10-05", merchant="Electric", pending=True)],
        set(), set(), TODAY,
    )
    assert r and r[0]["transaction_id"] == "t1"


def test_card_account_outflows_allowed():
    # match_bills never sees accounts; a card-account outflow is just a txn.
    r = match_bills(
        [bill()], [txn("t1", "2026-10-05", merchant="Electric")], set(), set(), TODAY
    )
    assert r and r[0]["transaction_id"] == "t1"


def test_no_match_text_or_due_day_skipped():
    assert match_bills([bill(match_text=None)], [txn("t1", "2026-10-05")], set(), set(), TODAY) == []
    assert match_bills([bill(match_text="  ")], [txn("t1", "2026-10-05")], set(), set(), TODAY) == []
    assert match_bills([bill(due_day=None)], [txn("t1", "2026-10-05")], set(), set(), TODAY) == []


def test_lookback_window():
    # Default lookback 45 days: today - 45 = 2026-08-18, so an Aug 1 due date
    # is outside; a txn far outside +/-7 days of any occurrence never matches.
    r = match_bills([bill()], [txn("t1", "2026-08-01", merchant="Electric")], set(), set(), TODAY)
    assert r == []


def test_no_match_without_text_overlap():
    r = match_bills(
        [bill()], [txn("t1", "2026-10-05", merchant="Water Utility")], set(), set(), TODAY
    )
    assert r == []