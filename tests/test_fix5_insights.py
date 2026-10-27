"""FIX5 Lane I: pure insights module (debt_trend, spending_baseline, reminders)."""
from datetime import date

import pytest

from app.insights import debt_trend, spending_baseline, reminders

TODAY = date(2026, 10, 14)


def txn(tid, d, cents, name, primary=None, detailed=None, card_id=3, acct=None):
    t = {
        "transaction_id": tid,
        "date": d,
        "amount_cents": cents,
        "name": name,
        "merchant_name": None,
        "pfc_primary": primary,
        "pfc_detailed": detailed,
        "card_id": card_id,
    }
    if acct is not None:
        t["account_type"] = acct
    return t


# ---------------------------------------------------------------- debt_trend


def test_debt_trend_classifies_every_class():
    txns = [
        # interest by pfc_detailed (within the last 30 days of 2026-10-14)
        txn("t1", "2026-09-20", 63348, "INTEREST CHARGE:PURCHASES",
            "BANK_FEES", "BANK_FEES_INTEREST_CHARGE"),
        # interest by name only, different month
        txn("t2", "2026-08-05", 1000, "interest charge on purchases", "BANK_FEES"),
        # late fee (fees, and specifically a late fee)
        txn("t3", "2026-09-20", 4100, "LATE FEE - PAYMENT DUE ON 09/15",
            "BANK_FEES", "BANK_FEES_LATE_FEES"),
        # plain fee, not late
        txn("t4", "2026-09-21", 300, "ANNUAL FEE", "BANK_FEES", "BANK_FEES_OTHER"),
        # a charge
        txn("t5", "2026-09-01", 18425, "SHELL OIL", "TRANSPORTATION",
            "GAS_TRANSPORTATION_FUEL"),
        # a payment (negative, LOAN_PAYMENTS)
        txn("t6", "2026-09-15", -603560, "MOBILE PURCHASE BILL PAYMENT",
            "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD"),
        # a payment with NULL primary
        txn("t7", "2026-09-16", -500, "CHECK PAYMENT", None, None),
        # a refund (negative, other primary)
        txn("t8", "2026-09-25", -1200, "AMAZON REFUND", "GENERAL_MERCHANDISE",
            "GENERAL_MERCHANDISE"),
        # outside the 6-month window: ignored
        txn("t9", "2026-01-01", 999999, "OLD CHARGE", "GENERAL_MERCHANDISE"),
    ]
    out = debt_trend(txns, [{"id": 3, "name": "Card C"}], TODAY, months=6)
    assert [m["month"] for m in out["months"]] == [
        "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    sep = out["months"][-2]
    assert sep["month"] == "2026-09"
    # interest 63348 + fees 4100+300 + charge 18425 - payments 603560+500 - refund 1200
    assert sep["net_change_cents"] == 63348 + 4100 + 300 + 18425 - 603560 - 500 - 1200
    assert sep["interest_cents"] == 63348
    assert sep["fees_cents"] == 4400
    assert sep["charges_cents"] == 18425
    assert sep["payments_cents"] == 604060
    assert sep["refunds_cents"] == 1200
    aug = out["months"][3]
    assert aug["interest_cents"] == 1000 and aug["net_change_cents"] == 1000
    # coverage_start = earliest txn date that is in scope? No: earliest overall.
    assert out["coverage_start"] == "2026-01-01"
    assert out["interest_last_30_cents"] == 63348


def test_debt_trend_partial_flags():
    txns = [
        txn("t1", "2026-07-04", 500, "KROGER", "FOOD_AND_DRINK"),
        txn("t2", "2026-09-01", 500, "KROGER", "FOOD_AND_DRINK"),
    ]
    out = debt_trend(txns, [], TODAY, months=6)
    p = {m["month"]: m["partial"] for m in out["months"]}
    # coverage starts 2026-07-04 (not the 1st) -> July partial; October is current
    assert p["2026-07"] is True
    assert p["2026-08"] is False
    assert p["2026-09"] is False
    assert p["2026-10"] is True


def test_debt_trend_coverage_on_first_is_not_partial():
    txns = [txn("t1", "2026-07-01", 500, "KROGER", "FOOD_AND_DRINK")]
    out = debt_trend(txns, [], TODAY, months=6)
    p = {m["month"]: m["partial"] for m in out["months"]}
    assert p["2026-07"] is False


def test_debt_trend_by_card_windows_and_late_fees():
    txns = [
        txn("t1", "2026-10-01", 18965, "INTEREST CHARGE:PURCHASES", "BANK_FEES",
            "BANK_FEES_INTEREST_CHARGE", card_id=3),
        txn("t2", "2026-06-01", 5000, "INTEREST CHARGE:PURCHASES", "BANK_FEES",
            "BANK_FEES_INTEREST_CHARGE", card_id=3),  # older than 30 days
        txn("t3", "2026-09-20", 4100, "LATE FEE - PAYMENT DUE ON 09/15", "BANK_FEES",
            "BANK_FEES_LATE_FEES", card_id=4),
        txn("t4", "2026-09-21", 300, "ANNUAL FEE", "BANK_FEES", "BANK_FEES_OTHER",
            card_id=4),  # fee but not late
        txn("t5", "2026-09-22", 700, "LATE FEE", "BANK_FEES", "BANK_FEES_LATE_FEES",
            card_id=4),  # older than 90? no: within
    ]
    cards = [{"id": 3, "name": "Card C"}, {"id": 4, "name": "Credit Card"}]
    out = debt_trend(txns, cards, TODAY, months=6)
    by_card = {r["card_id"]: r for r in out["by_card"]}
    assert by_card[3]["interest_last_30_cents"] == 18965
    assert by_card[4]["interest_last_30_cents"] == 0
    assert by_card[4]["fees_last_90_cents"] == 4100 + 300 + 700
    assert by_card[4]["late_fees_last_90_cents"] == 4100 + 700
    assert by_card[3]["fees_last_90_cents"] == 0


def test_debt_trend_empty_history():
    out = debt_trend([], [], TODAY, months=6)
    assert out["coverage_start"] is None
    assert len(out["months"]) == 6
    assert all(m["net_change_cents"] == 0 for m in out["months"])
    assert out["interest_last_30_cents"] == 0
    assert out["by_card"] == []


# --------------------------------------------------------- spending_baseline


def _base_txns():
    return [
        txn("t1", "2026-08-01", 139800, "WHOLE FOODS MARKET", "FOOD_AND_DRINK",
            acct="credit"),
        txn("t2", "2026-09-01", 139800, "WHOLE FOODS MARKET", "FOOD_AND_DRINK",
            acct="credit"),
        txn("t3", "2026-09-11", 372500, "KROGER #442", "GENERAL_MERCHANDISE",
            acct="credit"),
        txn("t4", "2026-08-11", 0, "ZERO AMOUNT", "FOOD_AND_DRINK", acct="credit"),
    ]


def test_spending_baseline_averages_complete_months():
    out = spending_baseline(_base_txns(), set(), [], 0, TODAY, months=3)
    # today 2026-10-14: last 3 complete months = 2026-07..09; history only
    # covers 2026-08+ -> months_used 2026-08, 2026-09
    assert out["months_used"] == ["2026-08", "2026-09"]
    cats = {c["category"]: c for c in out["categories"]}
    assert cats["FOOD_AND_DRINK"]["monthly_avg_cents"] == 139800
    assert cats["FOOD_AND_DRINK"]["label"] == "Food & drink"
    assert cats["FOOD_AND_DRINK"]["suggest"] is True
    assert cats["FOOD_AND_DRINK"]["note"] is None
    # zero-amount txn ignored
    assert "FOOD_AND_DRINK" in cats and len(out["categories"]) == 2
    assert out["total_monthly_avg_cents"] == 139800 + cats["GENERAL_MERCHANDISE"]["monthly_avg_cents"]
    assert out["gap_cents"] == out["total_monthly_avg_cents"]


def test_spending_baseline_skips_partial_coverage_month():
    txns = [
        txn("t1", "2026-07-04", 10000, "KROGER", "GENERAL_MERCHANDISE", acct="credit"),
        txn("t2", "2026-08-02", 20000, "KROGER", "GENERAL_MERCHANDISE", acct="credit"),
        txn("t3", "2026-09-02", 30000, "KROGER", "GENERAL_MERCHANDISE", acct="credit"),
    ]
    out = spending_baseline(txns, set(), [], 0, TODAY, months=3)
    assert out["months_used"] == ["2026-08", "2026-09"]
    assert out["categories"][0]["monthly_avg_cents"] == 25000


def test_spending_baseline_full_coverage_month_counts():
    txns = [
        txn("t1", "2026-07-01", 10000, "KROGER", "GENERAL_MERCHANDISE", acct="credit"),
        txn("t2", "2026-08-02", 20000, "KROGER", "GENERAL_MERCHANDISE", acct="credit"),
        txn("t3", "2026-09-02", 30000, "KROGER", "GENERAL_MERCHANDISE", acct="credit"),
    ]
    out = spending_baseline(txns, set(), [], 0, TODAY, months=3)
    assert out["months_used"] == ["2026-07", "2026-08", "2026-09"]
    assert out["categories"][0]["monthly_avg_cents"] == 20000


def test_spending_baseline_never_uses_current_month():
    txns = [txn("t1", "2026-10-01", 99999, "KROGER", "GENERAL_MERCHANDISE", acct="credit")]
    out = spending_baseline(txns, set(), [], 0, TODAY, months=3)
    assert out["months_used"] == []
    assert out["categories"] == []


def test_spending_baseline_exclusions():
    def pair(tid, d1, d2, cents, name, primary, acct="credit", detailed=None):
        return [
            txn(tid + "a", d1, cents, name, primary, detailed, acct=acct),
            txn(tid + "b", d2, cents, name, primary, detailed, acct=acct),
        ]

    txns = (
        pair("count", "2026-08-09", "2026-09-09", 4500, "CVS PHARMACY #1",
             "GENERAL_MERCHANDISE", acct="depository")
        # bill payment excluded by id
        + pair("bill1", "2026-08-05", "2026-09-05", 5000, "COMCAST",
               "RENT_AND_UTILITIES")
        # match_text hit on name
        + pair("m1", "2026-08-06", "2026-09-06", 6000, "NETFLIX.COM monthly",
               "ENTERTAINMENT")
        # match_text hit on merchant
        + pair("m2", "2026-08-06", "2026-09-06", 6000, "SPOTIFY USA",
               "ENTERTAINMENT")
        # interest and fees excluded on credit
        + pair("i1", "2026-08-07", "2026-09-07", 63348, "INTEREST CHARGE:PURCHASES",
               "BANK_FEES", detailed="BANK_FEES_INTEREST_CHARGE")
        + pair("f1", "2026-08-07", "2026-09-07", 4100, "LATE FEE", "BANK_FEES",
               detailed="BANK_FEES_LATE_FEES")
        # depository transfers excluded
        + pair("x1", "2026-08-08", "2026-09-08", 250000, "TRANSFER TO SAVINGS",
               "TRANSFER_OUT", acct="depository")
        + pair("x2", "2026-08-08", "2026-09-08", 26400, "CREDIT CARD PAYMENT",
               "LOAN_PAYMENTS", acct="depository")
    )
    out = spending_baseline(txns, {"bill1a", "bill1b"}, ["NETFLIX", "spotify"],
                            10000, TODAY, months=3)
    cats = {c["category"]: c for c in out["categories"]}
    assert set(cats) == {"GENERAL_MERCHANDISE"}
    assert cats["GENERAL_MERCHANDISE"]["monthly_avg_cents"] == 4500
    assert out["total_monthly_avg_cents"] == 4500
    assert out["budget_spending_cents"] == 10000
    assert out["gap_cents"] == 0  # max(0, 4500 - 10000)


def test_spending_baseline_rent_note_and_labels_and_sort():
    txns = [
        txn("r1", "2026-08-01", 180000, "SUNRIDGE APARTMENTS", "RENT_AND_UTILITIES",
            acct="credit"),
        txn("r2", "2026-09-01", 180000, "SUNRIDGE APARTMENTS", "RENT_AND_UTILITIES",
            acct="credit"),
        txn("g1", "2026-08-15", 40000, "IRS PAYMENT", "GOVERNMENT_AND_NON_PROFIT",
            acct="credit"),
        txn("g2", "2026-09-15", 40000, "IRS PAYMENT", "GOVERNMENT_AND_NON_PROFIT",
            acct="credit"),
        txn("o1", "2026-08-20", 1000, "MISC", "SOMETHING_ELSE", acct="credit"),
        txn("o2", "2026-09-20", 1000, "MISC", "SOMETHING_ELSE", acct="credit"),
    ]
    out = spending_baseline(txns, set(), [], 0, TODAY, months=3)
    cats = {c["category"]: c for c in out["categories"]}
    assert [c["category"] for c in out["categories"]] == [
        "RENT_AND_UTILITIES", "GOVERNMENT_AND_NON_PROFIT", "SOMETHING_ELSE"]
    rent = cats["RENT_AND_UTILITIES"]
    assert rent["label"] == "Rent & utilities"
    assert rent["suggest"] is False
    assert rent["note"] == "Likely already in your bills"
    assert cats["GOVERNMENT_AND_NON_PROFIT"]["label"] == "Government & donations"
    assert cats["SOMETHING_ELSE"]["label"] == "Something Else"


def test_spending_baseline_rounding_half_up():
    # 2 months, total 6667 -> 3333.5 -> 3334 (half-up); 3-month case 10001/3
    txns = [
        txn("a", "2026-08-01", 3334, "KROGER", "GENERAL_MERCHANDISE", acct="credit"),
        txn("b", "2026-09-01", 3333, "KROGER", "GENERAL_MERCHANDISE", acct="credit"),
    ]
    out = spending_baseline(txns, set(), [], 0, TODAY, months=3)
    assert out["categories"][0]["monthly_avg_cents"] == 3334
    txns3 = [
        txn("a", "2026-08-05", 3334, "KROGER", "GENERAL_MERCHANDISE", acct="credit"),
        txn("b", "2026-09-05", 3334, "KROGER", "GENERAL_MERCHANDISE", acct="credit"),
        txn("c", "2026-07-01", 3333, "KROGER", "GENERAL_MERCHANDISE", acct="credit"),
    ]
    out3 = spending_baseline(txns3, set(), [], 0, TODAY, months=3)
    assert out3["categories"][0]["monthly_avg_cents"] == 3334


def test_spending_baseline_empty_history():
    out = spending_baseline([], set(), [], 50000, TODAY, months=3)
    assert out == {
        "months_used": [],
        "categories": [],
        "total_monthly_avg_cents": 0,
        "budget_spending_cents": 50000,
        "gap_cents": 0,
    }


# ----------------------------------------------------------------- reminders


def card_row(cid, name, min_rem, *, sync="plaid", next_due=None, bank_overdue=False,
             due_day=None):
    return {
        "card_id": cid,
        "name": name,
        "min_remaining_cents": min_rem,
        "due_day": due_day,
        "next_due_date": next_due,
        "bank_overdue": bank_overdue,
        "sync": sync,
    }


def test_reminders_synced_due_in_window():
    rows = [card_row(4, "Credit Card", 13543, next_due="2026-10-15")]
    out = reminders(rows, [{"id": 4, "name": "Credit Card", "closed": 0}], [],
                    set(), {}, TODAY, days=3)
    assert out["today"] == "2026-10-14"
    assert out["items"] == [{
        "kind": "card", "id": 4, "name": "Credit Card", "date": "2026-10-15",
        "days_until": 1, "amount_cents": 13543, "overdue": False, "note": None,
    }]


def test_reminders_synced_outside_window_skipped():
    rows = [
        card_row(1, "Card A", 0, next_due="2026-11-02"),
        card_row(3, "Card C", 20900, next_due="2026-11-06"),
    ]
    out = reminders(rows, [{"id": 1, "name": "Card A", "closed": 0},
                           {"id": 3, "name": "Card C", "closed": 0}],
                    [], set(), {}, TODAY, days=3)
    assert out["items"] == []


def test_reminders_bank_overdue_and_past_not_overdue_skipped():
    rows = [
        card_row(7, "Overdue Card", 5000, next_due="2026-10-01", bank_overdue=True),
        card_row(8, "Handled Card", 5000, next_due="2026-10-01", bank_overdue=False),
    ]
    out = reminders(rows, [{"id": 7, "name": "O", "closed": 0},
                           {"id": 8, "name": "H", "closed": 0}],
                    [], set(), {}, TODAY, days=3)
    assert len(out["items"]) == 1
    it = out["items"][0]
    assert it["id"] == 7 and it["overdue"] is True and it["days_until"] == -13
    assert it["date"] == "2026-10-01"


def test_reminders_manual_due_day_overdue_and_current_month():
    rows = [
        # due day 5 of October: already past -> overdue
        card_row(9, "Manual A", 3000, sync="manual", due_day=5),
        # due day 16 of October: inside the window
        card_row(10, "Manual B", 2000, sync="manual", due_day=16),
    ]
    cards = [{"id": 9, "name": "Manual A", "closed": 0},
             {"id": 10, "name": "Manual B", "closed": 0}]
    out = reminders(rows, cards, [], set(), {}, TODAY, days=3)
    by_id = {i["id"]: i for i in out["items"]}
    assert by_id[9]["date"] == "2026-10-05" and by_id[9]["overdue"] is True
    assert by_id[10]["date"] == "2026-10-16" and by_id[10]["overdue"] is False


def test_reminders_manual_next_month_and_month_end_clamp():
    # Today near month end: the 2nd of next month is inside a 7-day window.
    today = date(2026, 10, 28)
    rows = [card_row(11, "Manual C", 1000, sync="manual", due_day=2)]
    out = reminders(rows, [{"id": 11, "name": "Manual C", "closed": 0}], [],
                    set(), {}, today, days=7)
    # both the missed Oct 2 occurrence (overdue) and the Nov 2 one are listed
    assert [i["date"] for i in out["items"]] == ["2026-10-02", "2026-11-02"]
    assert out["items"][0]["overdue"] is True
    assert out["items"][1] == {"kind": "card", "id": 11, "name": "Manual C",
                               "date": "2026-11-02", "days_until": 5,
                               "amount_cents": 1000, "overdue": False, "note": None}
    # due_day 31 clamped to Feb 28 in February
    rows2 = [card_row(12, "Manual D", 4000, sync="manual", due_day=31)]
    out2 = reminders(rows2, [{"id": 12, "name": "Manual D", "closed": 0}], [],
                     set(), {}, date(2027, 2, 25), days=7)
    assert out2["items"][0]["date"] == "2027-02-28"


def test_reminders_min_zero_and_paid_cards_skipped():
    rows = [
        card_row(1, "Card A", 0, next_due="2026-10-16"),
        card_row(5, "Paid Off", 0, sync="manual", due_day=16),
    ]
    out = reminders(rows, [{"id": 1, "name": "R", "closed": 0},
                           {"id": 5, "name": "P", "closed": 0}],
                    [], set(), {}, TODAY, days=3)
    assert out["items"] == []


def test_reminders_closed_card_skipped():
    rows = [card_row(4, "Old Card", 13543, next_due="2026-10-15")]
    out = reminders(rows, [{"id": 4, "name": "Old Card", "closed": 1}], [],
                    set(), {}, TODAY, days=3)
    assert out["items"] == []


def test_reminders_late_fee_note():
    rows = [card_row(4, "Credit Card", 13543, next_due="2026-10-15")]
    out = reminders(rows, [{"id": 4, "name": "Credit Card", "closed": 0}], [],
                    set(), {4: 2}, TODAY, days=3)
    assert out["items"][0]["note"] == "late fees charged 2 times in the last 90 days"
    out0 = reminders(rows, [{"id": 4, "name": "Credit Card", "closed": 0}], [],
                     set(), {4: 0}, TODAY, days=3)
    assert out0["items"][0]["note"] is None


def test_reminders_bills_due_paid_and_overdue():
    bills = [
        {"id": 1, "name": "Electric", "amount_cents": 9500, "due_day": 20, "paid_from": "cash"},
        {"id": 2, "name": "Internet", "amount_cents": 6000, "due_day": 16, "paid_from": "card"},
        {"id": 3, "name": "Rent", "amount_cents": 180000, "due_day": 1, "paid_from": "cash"},
        {"id": 4, "name": "Old Utility", "amount_cents": 7000, "due_day": 14, "paid_from": "cash"},
        {"id": 5, "name": "No due day", "amount_cents": 1000, "due_day": None, "paid_from": "cash"},
    ]
    out = reminders([], [], bills, set(), {}, TODAY, days=7)
    by_id = {i["id"]: i for i in out["items"]}
    assert by_id[1]["date"] == "2026-10-20" and by_id[1]["overdue"] is False
    # card-paid bills are also reminders
    assert by_id[2]["date"] == "2026-10-16"
    # due day 1 of October is in the past: bills are never reported overdue
    # (unmatched bills are rarely marked paid; it would repeat daily as noise)
    assert 3 not in by_id
    # due on today: days_until 0, due not overdue
    assert by_id[4]["date"] == "2026-10-14" and by_id[4]["overdue"] is False
    assert 5 not in by_id
    # paid occurrences drop out
    out2 = reminders([], [], bills, {(4, "2026-10-14")}, {}, TODAY, days=7)
    assert {i["id"] for i in out2["items"]} == {1, 2}


def test_reminders_sorted_by_date_then_kind():
    rows = [card_row(4, "Credit Card", 13543, next_due="2026-10-15")]
    bills = [{"id": 1, "name": "Electric", "amount_cents": 9500, "due_day": 15,
              "paid_from": "cash"}]
    out = reminders(rows, [{"id": 4, "name": "Credit Card", "closed": 0}], bills,
                    set(), {}, TODAY, days=3)
    assert [(i["date"], i["kind"]) for i in out["items"]] == [
        ("2026-10-15", "bill"), ("2026-10-15", "card")]


def test_reminders_days_clamped():
    rows = [card_row(3, "Card C", 20900, next_due="2026-10-20")]
    out = reminders(rows, [{"id": 3, "name": "D", "closed": 0}], [], set(), {},
                    TODAY, days=99)
    # clamped to 14 -> Oct 20 (6 days) still included
    assert [i["id"] for i in out["items"]] == [3]
    rows2 = [card_row(3, "Card C", 20900, next_due="2026-10-31")]
    out2 = reminders(rows2, [{"id": 3, "name": "D", "closed": 0}], [], set(), {},
                     TODAY, days=99)
    assert out2["items"] == []


@pytest.mark.parametrize("fn,args", [
    (debt_trend, ([], [], TODAY)),
    (spending_baseline, ([], set(), [], 0, TODAY)),
    (reminders, ([], [], [], set(), {}, TODAY)),
])
def test_pure_signatures_accept_positional_defaults(fn, args):
    assert fn(*args) is not None


# --- endpoint wiring (orchestrator) -------------------------------------------------

def test_insight_endpoints_wired(tmp_path, monkeypatch):
    import json as _json
    from datetime import date as _d
    from fastapi.testclient import TestClient
    from app import clock as _clock, db as _db
    from app.main import create_app
    monkeypatch.setattr(_clock, "today", lambda: _d(2026, 10, 14))
    path = str(tmp_path / "w.db")
    conn = _db.init_db(path)
    card = _db.create_card(conn, {"name": "Rewards Card", "balance_cents": 500000, "apr": 0.0,
                                  "min_payment_cents": 0, "due_day": 15})
    conn.execute("UPDATE cards SET sync='plaid', next_due_date='2026-10-15' WHERE id=?", (card["id"],))
    conn.execute("INSERT INTO plaid_items (item_id, access_token_enc, institution_name, status, created_at) "
                 "VALUES ('i', 'x', 'Example Bank', 'ok', '2026-10-01')")
    conn.execute("INSERT INTO plaid_accounts (plaid_item_id, account_id, name, mask, type, card_id, last_payload) "
                 "VALUES (1, 'a', 'Card', '1234', 'credit', ?, ?)", (card["id"], _json.dumps({})))
    rows = [("l1", "2026-08-17", 4100, "LATE FEE - PAYMENT DUE ON 08/15", "BANK_FEES", "BANK_FEES_LATE_FEES"),
            ("l2", "2026-09-15", 4100, "LATE FEE - PAYMENT DUE ON 09/15", "BANK_FEES", "BANK_FEES_LATE_FEES"),
            ("i1", "2026-09-17", 8000, "INTEREST CHARGE ON PURCHASES", "BANK_FEES", "BANK_FEES_INTEREST_CHARGE"),
            ("c1", "2026-09-03", 12000, "GROCER", "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES"),
            ("c0", "2026-07-01", 100, "SEED", "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES")]
    for tid, d, amt, name, p1, p2 in rows:
        conn.execute("INSERT INTO transactions (transaction_id, plaid_account_id, date, amount_cents, name, "
                     "pending, pfc_primary, pfc_detailed, updated_at) VALUES (?, 1, ?, ?, ?, 0, ?, ?, ?)",
                     (tid, d, amt, name, p1, p2, d))
    conn.commit()
    c = TestClient(create_app(path, "u", "p"), headers={"Authorization": "Basic dTpw"})
    trend = c.get("/api/insights/debt-trend?months=3").json()
    sep = next(m for m in trend["months"] if m["month"] == "2026-09")
    assert (sep["charges_cents"], sep["interest_cents"], sep["fees_cents"]) == (12000, 8000, 4100)
    sp = c.get("/api/insights/spending?months=3").json()
    assert sp["months_used"] == ["2026-07", "2026-08", "2026-09"]
    assert sp["categories"][0]["label"] == "Food & drink"
    rem = c.get("/api/reminders?days=3").json()
    item = next(i for i in rem["items"] if i["kind"] == "card")
    assert item["date"] == "2026-10-15" and item["amount_cents"] > 0
    assert item["note"] == "late fees charged 2 times in the last 90 days"
