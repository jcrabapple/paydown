"""Tests for app.sts (pure). Expected values hand-computed.

Doc-example facts: Oct 2026 has 31 days (dim=31); horizon 2026-10-09 gives
days_until=7; spending_monthly 82667 -> planned = round_half_up(82667*7/31)
= round_half_up(18666.7...) = 18667; per_day = 170300 // 7 = 24328.

Spec note: the JSON example in docs/SAFE.md shows a paycheck_late warning
while its payday status is "upcoming". SAFE.md's rule text says paycheck_late
fires only when status == "late", which is contradictory; we follow the rule
text (see test_paycheck_late_warning_exact_message for the exact message).
"""
from datetime import date, datetime, timezone

from app.sts import safe_to_spend

TODAY = date(2026, 10, 2)
NOW = datetime(2026, 10, 2, 10, 30, 0, tzinfo=timezone.utc)
CASH_AS_OF = NOW


def payday_info(status="upcoming", payday_date="2026-10-09", missed=None, last="2026-09-25"):
    return {
        "payday": {
            "date": payday_date,
            "schedule_id": 1,
            "name": "Main paycheck",
            "expected_cents": 215000,
            "status": status,
        }
        if payday_date
        else None,
        "missed": missed or [],
        "last_payday": last,
    }


def bill(id=3, name="Electric", amount=14000, due_day: int | None = 5, paid_from="cash"):
    return {
        "id": id,
        "name": name,
        "amount_cents": amount,
        "due_day": due_day,
        "paid_from": paid_from,
    }


def card_row(card_id=2, name="Capital One", due_day: int | None = 7, remaining=183000, recommended=183000):
    return {
        "card_id": card_id,
        "name": name,
        "due_day": due_day,
        "remaining_cents": remaining,
        "recommended_cents": recommended,
    }


def sts(**kw):
    base: dict = dict(
        today=TODAY,
        now=NOW,
        payday_info=payday_info(),
        has_schedules=True,
        cash_total_cents=412300,
        pending_out_cents=0,
        cash_as_of=CASH_AS_OF,
        cash_floor_cents=0,
        goals_set_aside_cents=45000,
        bills=[bill()],
        bill_payments=set(),
        card_rows={"2026-10": [card_row()]},
        spending_monthly_cents=82667,
        extra_warnings=[],
    )
    base.update(kw)
    return safe_to_spend(**base)


# ------------------------------------------------------- doc JSON example

def test_doc_json_example_reproduced():
    # 412300 - 0 - (14000 + 183000) - 45000 - 0 = 170300
    # per_day = 170300 // 7 = 24328; planned = round_half_up(82667 * 7/31) = 18667
    r = sts()
    assert r == {
        "today": "2026-10-02",
        "horizon": "2026-10-09",
        "days_until": 7,
        "payday": {
            "date": "2026-10-09",
            "schedule_id": 1,
            "name": "Main paycheck",
            "expected_cents": 215000,
            "status": "upcoming",
        },
        "period_start": "2026-09-25",
        "cash_total_cents": 412300,
        "pending_out_cents": 0,
        "cash_as_of": "2026-10-02T10:30:00Z",
        "obligations": [
            {"kind": "bill", "id": 3, "name": "Electric", "date": "2026-10-05",
             "amount_cents": 14000, "status": "due", "reserved": True, "undated": False},
            {"kind": "card", "id": 2, "name": "Capital One", "date": "2026-10-07",
             "amount_cents": 183000, "status": "due", "reserved": True, "undated": False,
             "extra_remaining_cents": 0},
        ],
        "obligations_total_cents": 197000,
        # S1: planned extra across card obligations (informational, not subtracted).
        # This fixture's row has no min_remaining_cents, so the whole remaining
        # counts as owed and the planned extra is 0.
        "planned_extra_cents": 0,
        "goals_set_aside_cents": 45000,
        "cash_floor_cents": 0,
        "safe_to_spend_cents": 170300,
        "per_day_cents": 24328,
        "planned_spending_cents": 18667,
        "warnings": [],
    }


def test_paycheck_late_warning_exact_message():
    # With the payday 3 days past (still within grace) the status is "late"
    # and the warning matches the doc example's warning verbatim.
    r = sts(
        today=date(2026, 10, 12),
        payday_info=payday_info(status="late", payday_date="2026-10-09", last="2026-09-25"),
        cash_as_of=datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc),
    )
    assert r["warnings"][0] == {
        "code": "paycheck_late",
        "message": "Main paycheck expected Oct 9 hasn't arrived yet",
        "date": "2026-10-09",
    }


# ------------------------------------------------------------- bill statuses

def test_overdue_bill_since_period_start_reserved():
    # Bill due on the 28th: Sep 28 falls in [period_start Sep 25, horizon] and
    # is before today -> overdue and reserved. A due_day 20 bill's Sep 20 date
    # is before period_start and does not appear at all.
    r = sts(bills=[bill(id=3, due_day=28), bill(id=4, due_day=20)])
    by_id = {o["id"]: o for o in r["obligations"]}
    assert by_id[3]["status"] == "overdue"
    assert by_id[3]["date"] == "2026-09-28"
    assert by_id[3]["reserved"] is True
    assert 4 not in by_id
    assert r["obligations_total_cents"] == 14000 + 183000  # bill + default card row


def test_paid_bill_listed_not_reserved():
    r = sts(bill_payments={(3, "2026-10-05")}, card_rows={"2026-10": []})
    o = r["obligations"][0]
    assert o["status"] == "paid"
    assert o["reserved"] is False
    assert r["obligations_total_cents"] == 0
    # Without the payment, the full 14000 is reserved.
    assert sts(card_rows={"2026-10": []})["obligations_total_cents"] == 14000


def test_card_paid_bill_not_reserved():
    r = sts(bills=[bill(id=5, name="Internet", paid_from="card")], card_rows={"2026-10": []})
    o = r["obligations"][0]
    assert o["kind"] == "bill"
    assert o["status"] == "on_card"
    assert o["date"] == "2026-10-05"
    assert o["reserved"] is False
    assert r["obligations_total_cents"] == 0


def test_undated_bill_spread_amount():
    # 31000 cents * min(7, 31)/31 = 31000 * 7/31 = 7000 exactly.
    r = sts(bills=[bill(id=6, name="Misc", amount=31000, due_day=None)], card_rows={"2026-10": []})
    o = r["obligations"][0]
    assert o["status"] == "spread"
    assert o["amount_cents"] == 7000
    assert o["reserved"] is True
    assert o["date"] == "2026-10-09"
    assert o["undated"] is True


# -------------------------------------------------------------- card rows

def test_card_due_next_month_inside_horizon_uses_next_months_row():
    # Horizon 2026-11-09: months Oct and Nov. The Oct row (due Oct 20) is due
    # inside the horizon; the Nov row supplies the Nov 5 obligation.
    rows = {
        "2026-10": [card_row(card_id=2, due_day=20, remaining=50000)],
        "2026-11": [card_row(card_id=2, due_day=5, remaining=183000)],
    }
    r = sts(payday_info=payday_info(payday_date="2026-11-09"), card_rows=rows)
    cards = [o for o in r["obligations"] if o["kind"] == "card"]
    assert [(o["date"], o["amount_cents"]) for o in cards] == [
        ("2026-10-20", 50000),
        ("2026-11-05", 183000),
    ]
    assert r["horizon"] == "2026-11-09"


def test_current_month_card_due_before_period_start_overdue_reserved():
    # Card due Oct 1: before today, but this month's row is still in scope ->
    # overdue and reserved (not silently dropped because it precedes
    # period_start Sep 25... it doesn't; it precedes today).
    r = sts(card_rows={"2026-10": [card_row(due_day=1)]})
    o = r["obligations"][0]
    assert o["status"] == "overdue"
    assert o["date"] == "2026-10-01"
    assert o["reserved"] is True


def test_card_remaining_zero_listed_paid_not_reserved():
    r = sts(bills=[], card_rows={"2026-10": [card_row(remaining=0, recommended=120000)]})
    o = r["obligations"][0]
    assert o["status"] == "paid"
    assert o["reserved"] is False
    assert o["amount_cents"] == 120000
    assert r["obligations_total_cents"] == 0


def test_card_undated_row_due_at_horizon():
    r = sts(bills=[], card_rows={"2026-10": [card_row(due_day=None, remaining=99000)]})
    o = r["obligations"][0]
    assert o["kind"] == "card"
    assert o["status"] == "due"
    assert o["date"] == "2026-10-09"
    assert o["undated"] is True
    assert o["reserved"] is True


def test_card_due_day_31_clamped():
    # Horizon in September (payday Sep 30... horizon Sep 28): due_day 31 in
    # September clamps to Sep 30 which is past the horizon -> not listed;
    # with horizon Oct 9 it clamps to Sep 30 < today -> overdue.
    late = sts(
        payday_info=payday_info(payday_date="2026-09-28", status="late"),
        card_rows={"2026-09": [card_row(due_day=31)]},
    )
    assert [o for o in late["obligations"] if o["kind"] == "card"] == []
    # Past-horizon row is skipped even though the month is in range.
    r = sts(bills=[], card_rows={"2026-09": [card_row(due_day=31)], "2026-10": []})
    assert r["obligations"] == []


def test_obligation_sort_order():
    # Same date: bill before card; earlier dates first.
    r = sts(
        bills=[bill(id=9, due_day=5)],
        card_rows={"2026-10": [card_row(card_id=2, due_day=5), card_row(card_id=7, due_day=3)]},
    )
    assert [(o["kind"], o["id"]) for o in r["obligations"]] == [
        ("card", 7),   # Oct 3
        ("bill", 9),   # Oct 5 (bill before card)
        ("card", 2),   # Oct 5
    ]


# ------------------------------------------------- totals, per-day, planned

def test_goals_and_floor_subtracted():
    r = sts(goals_set_aside_cents=10000, cash_floor_cents=5000)
    # 412300 - 197000 - 10000 - 5000 = 200300
    assert r["safe_to_spend_cents"] == 200300
    assert r["per_day_cents"] == 200300 // 7  # 28614


def test_pending_out_subtracted():
    r = sts(pending_out_cents=1000)
    assert r["safe_to_spend_cents"] == 170300 - 1000


def test_negative_short_warning_per_day_zero():
    r = sts(cash_total_cents=100000)
    # 100000 - 197000 - 45000 = -142000
    assert r["safe_to_spend_cents"] == -142000
    assert r["per_day_cents"] == 0
    short = [w for w in r["warnings"] if w["code"] == "short"]
    assert short == [{"code": "short", "message": "Short by $1,420.00"}]


def test_planned_spending_window_larger_than_month_fraction():
    # Horizon 2026-11-09 -> window_days = 38; min(38, 31) = 31 ->
    # planned = round_half_up(82667 * 31/31) = 82667.
    r = sts(payday_info=payday_info(payday_date="2026-11-09"))
    assert r["planned_spending_cents"] == 82667
    assert r["days_until"] == 38


# --------------------------------------------------------------- warnings

def test_no_pay_schedule_uses_month_end():
    r = sts(payday_info=payday_info(payday_date=None), has_schedules=False)
    assert r["horizon"] == "2026-10-31"
    assert r["days_until"] == 29
    assert r["payday"] is None
    assert any(w["code"] == "no_pay_schedule" for w in r["warnings"])


def test_no_upcoming_payday_warning_when_schedules_exist():
    r = sts(payday_info=payday_info(payday_date=None), has_schedules=True)
    assert r["horizon"] == "2026-10-31"
    assert any(w["code"] == "no_upcoming_payday" for w in r["warnings"])
    assert not any(w["code"] == "no_pay_schedule" for w in r["warnings"])


def test_stale_balance_none_and_old():
    r = sts(cash_as_of=None)
    assert any(w["code"] == "stale_balance" for w in r["warnings"])
    # now - cash_as_of = 4 days > 3 -> stale.
    r = sts(cash_as_of=datetime(2026, 9, 28, 10, 30, tzinfo=timezone.utc))
    stale = [w for w in r["warnings"] if w["code"] == "stale_balance"]
    assert stale and "4 days" in stale[0]["message"]
    # Exactly 3 days old is not stale.
    r = sts(cash_as_of=datetime(2026, 9, 29, 10, 30, tzinfo=timezone.utc))
    assert not any(w["code"] == "stale_balance" for w in r["warnings"])


def test_paycheck_missed_warnings():
    missed = [
        {"date": "2026-09-25", "schedule_id": 1, "name": "Main paycheck"},
        {"date": "2026-10-09", "schedule_id": 1, "name": "Main paycheck"},
    ]
    r = sts(payday_info=payday_info(missed=missed))
    codes = [w["code"] for w in r["warnings"]]
    assert codes.count("paycheck_missed") == 2
    msgs = [w["message"] for w in r["warnings"] if w["code"] == "paycheck_missed"]
    assert "Main paycheck expected Sep 25 never arrived" in msgs


def test_extra_warnings_appended_unchanged():
    extra = [{"code": "unlabelled_income", "message": "3 deposits to review", "count": 3}]
    r = sts(extra_warnings=extra)
    assert r["warnings"][-1] == extra[0]


def test_warning_order():
    r = sts(
        cash_total_cents=100000,
        cash_as_of=None,
        payday_info=payday_info(status="late", last="2026-09-25"),
    )
    assert [w["code"] for w in r["warnings"]] == [
        "paycheck_late",
        "stale_balance",
        "short",
    ]