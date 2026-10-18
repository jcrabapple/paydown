"""FIX5 Lane S tests: S1 (reserve minimums, not the extra), S2 (bank due dates),
S3 (months_covered clamped at 0.0). Pure sts/cash behaviour, frozen dates.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timezone

from app.sts import safe_to_spend
from app import cash as cashx

TODAY = date(2026, 10, 14)
NOW = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
CASH_AS_OF = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)


def payday_info():
    return {"payday": None, "missed": [], "last_payday": None}


def row(card_id, *, min_remaining, remaining, recommended=None,
        due_day: int | None = 20, sync="plaid", next_due=None, bank_overdue=False,
        paid=0, name=None):
    return {
        "card_id": card_id,
        "name": name or f"Card {card_id}",
        "due_day": due_day,
        "remaining_cents": remaining,
        "recommended_cents": recommended if recommended is not None else remaining,
        "paid_cents": paid,
        "min_remaining_cents": min_remaining,
        "sync": sync,
        "next_due_date": next_due,
        "bank_overdue": bank_overdue,
    }


def sts(card_rows, *, cash_total_cents=50000, bills=(), goals=0, floor=0):
    return safe_to_spend(
        today=TODAY,
        now=NOW,
        payday_info=payday_info(),
        has_schedules=False,
        cash_total_cents=cash_total_cents,
        pending_out_cents=0,
        cash_as_of=CASH_AS_OF,
        cash_floor_cents=floor,
        goals_set_aside_cents=goals,
        bills=list(bills),
        bill_payments=set(),
        card_rows=card_rows,
        spending_monthly_cents=0,
        extra_warnings=[],
    )


def bill(amount_cents, due_day, bill_id=1, name="Electric"):
    return {"id": bill_id, "name": name, "amount_cents": amount_cents,
            "due_day": due_day, "paid_from": "cash"}


# ---------------------------------------------------------------- S1

def test_s1_card_reserves_minimum_not_extra():
    r = sts({"2026-10": [row(6, min_remaining=2000, remaining=26550, due_day=22)]})
    cards = [o for o in r["obligations"] if o["kind"] == "card"]
    assert len(cards) == 1
    o = cards[0]
    assert o["status"] == "due" and o["reserved"] is True
    assert o["amount_cents"] == 2000
    assert o["extra_remaining_cents"] == 24550
    # Only the minimum is subtracted; the extra is informational.
    assert r["obligations_total_cents"] == 2000
    assert r["planned_extra_cents"] == 24550
    assert r["safe_to_spend_cents"] == 50000 - 2000


def test_s1_planned_extra_is_not_subtracted():
    rows = [row(2, min_remaining=20000, remaining=800000, due_day=14),
            row(4, min_remaining=12500, remaining=500000, due_day=15)]
    r = sts({"2026-10": rows})
    assert r["obligations_total_cents"] == 20000 + 12500
    assert r["planned_extra_cents"] == (800000 - 20000) + (500000 - 12500)
    assert r["safe_to_spend_cents"] == 50000 - 20000 - 12500


def test_s1_covered_when_minimum_satisfied():
    # Minimum already paid this month; only extra remains. Not reserved.
    r = sts({"2026-10": [row(9, min_remaining=0, remaining=45000, recommended=71450,
                             paid=26450, due_day=25)]})
    o = [x for x in r["obligations"] if x["kind"] == "card"][0]
    assert o["status"] == "covered"
    assert o["reserved"] is False
    assert o["amount_cents"] == 45000
    assert o["extra_remaining_cents"] == 45000
    assert r["obligations_total_cents"] == 0
    assert r["planned_extra_cents"] == 45000


def test_s1_paid_row_unchanged():
    r = sts({"2026-10": [row(9, min_remaining=0, remaining=0, recommended=120000,
                             paid=120000, due_day=25)]})
    o = [x for x in r["obligations"] if x["kind"] == "card"][0]
    assert o["status"] == "paid" and o["reserved"] is False
    assert o["amount_cents"] == 120000
    assert r["planned_extra_cents"] == 0


def test_s1_row_without_min_remaining_reserves_all_legacy():
    # Legacy rows (no min_remaining_cents key) keep reserving everything.
    legacy = {"card_id": 5, "name": "Old", "due_day": 20, "remaining_cents": 90000,
              "recommended_cents": 90000}
    r = sts({"2026-10": [legacy]})
    o = [x for x in r["obligations"] if x["kind"] == "card"][0]
    assert o["amount_cents"] == 90000 and o["extra_remaining_cents"] == 0
    assert r["obligations_total_cents"] == 90000


def test_s1_recommended_zero_and_unpaid_skipped():
    r = sts({"2026-10": [row(11, min_remaining=0, remaining=0, recommended=0, paid=0)]})
    assert not [o for o in r["obligations"] if o["kind"] == "card"]


def test_s1_undated_reserves_minimum():
    r = sts({"2026-10": [row(12, min_remaining=3000, remaining=120000, due_day=None)]})
    o = [x for x in r["obligations"] if x["kind"] == "card"][0]
    assert o["status"] == "due" and o["undated"] is True
    assert o["amount_cents"] == 3000 and o["extra_remaining_cents"] == 117000


# ---------------------------------------------------------------- S2

# The FIX5 S2 regression set from live data (today 2026-10-14, horizon
# 2026-10-31, no payday). Expected values are computed from these fixtures.
def _live_rows():
    return [
        # Card 1: next due Nov 2, min 0 -> not listed
        row(1, name="Card A", min_remaining=0, remaining=400000, recommended=400000,
            paid=0, due_day=2, next_due="2026-11-02"),
        # Card 3: next due Nov 6 -> not listed
        row(3, name="Card C", min_remaining=18000, remaining=650000, recommended=650000,
            paid=0, due_day=6, next_due="2026-11-06"),
        # Card 2: next due today (Oct 14), min unpaid -> due 20000
        row(2, name="Card B", min_remaining=20000, remaining=800000,
            recommended=800000, paid=0, due_day=14, next_due="2026-10-14"),
        # Card 4: next due Oct 15, effective min -> due 12500
        row(4, name="Card D", min_remaining=12500, remaining=500000,
            recommended=500000, paid=0, due_day=15, next_due="2026-10-15"),
        # Card 6: next due Oct 22, min 2000, recommended 26550
        row(6, name="Card F", min_remaining=2000, remaining=26550,
            recommended=26550, paid=0, due_day=22, next_due="2026-10-22"),
        # Overdue at the bank, past due date -> overdue
        row(7, name="Overdue Card", min_remaining=5100, remaining=5100,
            recommended=5100, paid=0, due_day=1, next_due="2026-10-01",
            bank_overdue=True),
        # Past due date but the bank says it is handled -> not listed
        row(8, name="Handled Card", min_remaining=8800, remaining=8800,
            recommended=8800, paid=0, due_day=1, next_due="2026-10-01"),
    ]


def test_s2_bank_due_date_regression_set():
    rows = _live_rows()
    bills_in_window = [bill(14000, 20)]
    bill_total = 14000
    r = sts({"2026-10": rows}, bills=bills_in_window)
    cards = {o["id"]: o for o in r["obligations"] if o["kind"] == "card"}
    # Not listed: next due outside the window, or past and handled by the bank.
    assert set(cards) == {2, 4, 6, 7}
    assert cards[2]["status"] == "due"
    assert cards[2]["date"] == "2026-10-14"
    assert cards[2]["amount_cents"] == 20000
    assert cards[4]["status"] == "due"
    assert cards[4]["date"] == "2026-10-15"
    assert cards[4]["amount_cents"] == 12500
    assert cards[6]["status"] == "due"
    assert cards[6]["date"] == "2026-10-22"
    assert cards[6]["amount_cents"] == 2000
    assert cards[6]["extra_remaining_cents"] == 24550
    assert cards[7]["status"] == "overdue"
    assert cards[7]["date"] == "2026-10-01"
    assert cards[7]["amount_cents"] == 5100
    # Safe-to-spend = cash - bills in the window - reserved minimums only.
    reserved_mins = 20000 + 12500 + 2000 + 5100
    assert r["obligations_total_cents"] == bill_total + reserved_mins
    assert r["safe_to_spend_cents"] == 50000 - bill_total - reserved_mins
    # planned_extra covers the listed cards' extras only, and is not subtracted.
    assert r["planned_extra_cents"] == (800000 - 20000) + (500000 - 12500) + 24550


def test_s2_bank_due_date_after_horizon_skipped():
    r = sts({"2026-10": [row(3, min_remaining=18000, remaining=650000, due_day=6,
                             next_due="2026-11-06")]})
    assert not [o for o in r["obligations"] if o["kind"] == "card"]
    assert r["planned_extra_cents"] == 0


def test_s2_bank_overdue_in_scope_even_beyond_horizon():
    # bank_overdue is always in scope, even with the due date after the payday.
    r = sts({"2026-10": [row(7, min_remaining=5100, remaining=5100, due_day=1,
                             next_due="2026-11-01", bank_overdue=True)]})
    o = [o for o in r["obligations"] if o["kind"] == "card"][0]
    assert o["status"] == "overdue" and o["reserved"] is True
    assert o["amount_cents"] == 5100


def test_s2_one_obligation_per_card_uses_todays_month_row():
    # The card appears in October and November rows; one obligation, October's.
    oct_row = row(6, min_remaining=2000, remaining=26550, due_day=22,
                  next_due="2026-10-22")
    nov_row = row(6, min_remaining=9000, remaining=95000, due_day=22,
                  next_due="2026-11-22")
    r = sts({"2026-10": [oct_row], "2026-11": [nov_row]})
    cards = [o for o in r["obligations"] if o["kind"] == "card"]
    assert len(cards) == 1
    assert cards[0]["date"] == "2026-10-22"
    assert cards[0]["amount_cents"] == 2000


def test_s2_late_payday_reserves_through_month_end():
    # Horizon before today (late payday): a bank due date still within
    # today's month stays in scope; one after month end does not.
    late = safe_to_spend(
        today=date(2026, 10, 20),
        now=NOW,
        payday_info={"payday": {"date": date(2026, 10, 18), "schedule_id": 1,
                                "name": "Pay", "expected_cents": 0,
                                "status": "late"},
                     "missed": [], "last_payday": date(2026, 10, 4)},
        has_schedules=True,
        cash_total_cents=100000,
        pending_out_cents=0,
        cash_as_of=CASH_AS_OF,
        cash_floor_cents=0,
        goals_set_aside_cents=0,
        bills=[],
        bill_payments=set(),
        card_rows={"2026-10": [row(6, min_remaining=2000, remaining=26550, due_day=22,
                                   next_due="2026-10-25"),
                               row(9, min_remaining=4000, remaining=4000, due_day=9,
                                   next_due="2026-11-05")]},
        spending_monthly_cents=0,
        extra_warnings=[],
    )
    cards = {o["id"]: o for o in late["obligations"] if o["kind"] == "card"}
    assert set(cards) == {6}
    assert cards[6]["status"] == "due" and cards[6]["amount_cents"] == 2000


def test_s2_manual_card_keeps_due_day_rules():
    r = sts({"2026-10": [row(5, min_remaining=7000, remaining=70000, due_day=18,
                             sync="manual", next_due=None)]})
    o = [o for o in r["obligations"] if o["kind"] == "card"][0]
    assert o["date"] == "2026-10-18"
    assert o["amount_cents"] == 7000 and o["status"] == "due"


# ---------------------------------------------------------------- S3

def test_s3_months_covered_clamped_at_zero():
    pos = cashx.cash_position(
        [{"name": "Checking", "balance_cents": -5000, "include_in_cash": 1}],
        {"cash_floor_cents": 0, "cash_reserve_months": 1.0},
        50000, [],
    )
    assert pos["months_covered"] == 0.0


def test_s3_months_covered_positive_unchanged():
    pos = cashx.cash_position(
        [{"name": "Checking", "balance_cents": 100000, "include_in_cash": 1}],
        {"cash_floor_cents": 0, "cash_reserve_months": 1.0},
        50000, [],
    )
    assert pos["months_covered"] == 2.0
