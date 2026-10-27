"""Pure subscription math + detection. Expected values hand-computed."""
from datetime import date, timedelta

from app.subscriptions import (
    advance, annual_cents, detect_recurring, merchant_key, monthly_cents, next_renewal,
)

TODAY = date(2026, 10, 7)


def test_monthly_equivalents():
    assert monthly_cents(1599, "monthly") == 1599
    assert monthly_cents(12000, "yearly") == 1000          # 120.00 / 12
    assert monthly_cents(9999, "yearly") == 833            # 833.25 -> 833
    assert monthly_cents(3000, "quarterly") == 1000        # 4 * 30.00 / 12
    assert monthly_cents(1000, "weekly") == 4333           # 52 * 10.00 / 12 = 43.333
    assert monthly_cents(1050, "weekly") == 4550           # 52 * 10.50 / 12 = 45.50 exactly
    assert monthly_cents(2501, "yearly") == 208            # 208.416 -> 208
    assert monthly_cents(1230, "yearly") == 103            # 102.5 -> half rounds up


def test_annual_cents():
    assert annual_cents(1599, "monthly") == 19188
    assert annual_cents(500, "weekly") == 26000
    assert annual_cents(9900, "yearly") == 9900
    assert annual_cents(2500, "quarterly") == 10000


def test_advance_clamps_day_and_recovers():
    a = date(2026, 1, 31)
    assert advance(a, "monthly", 1) == date(2026, 2, 28)
    assert advance(a, "monthly", 2) == date(2026, 3, 31)   # anchor day, not the clamped one
    assert advance(date(2024, 2, 29), "yearly", 1) == date(2025, 2, 28)
    assert advance(date(2024, 2, 29), "yearly", 4) == date(2028, 2, 29)
    assert advance(date(2026, 11, 15), "quarterly", 1) == date(2027, 2, 15)
    assert advance(date(2026, 10, 7), "weekly", 3) == date(2026, 10, 28)


def test_next_renewal_future_anchor_and_today():
    assert next_renewal(date(2026, 10, 20), "monthly", TODAY) == date(2026, 10, 20)
    assert next_renewal(date(2026, 10, 7), "yearly", TODAY) == TODAY  # charges today


def test_next_renewal_rolls_forward():
    assert next_renewal(date(2026, 7, 5), "monthly", TODAY) == date(2026, 11, 5)
    assert next_renewal(date(2025, 3, 10), "yearly", TODAY) == date(2027, 3, 10)
    assert next_renewal(date(2026, 9, 28), "weekly", TODAY) == date(2026, 10, 12)  # 28th,5th,12th
    assert next_renewal(date(2026, 1, 31), "monthly", date(2026, 3, 1)) == date(2026, 3, 31)
    assert next_renewal("2026-06-01", "quarterly", "2026-10-07") == date(2026, 12, 1)


def test_merchant_key_strips_numbers_and_noise():
    assert merchant_key({"merchant_name": "Netflix", "name": "NETFLIX.COM 866-579-7172"}) == "netflix"
    assert merchant_key({"merchant_name": None, "name": "SPOTIFY P1A2B3C4D5"}) == "spotify"
    assert merchant_key({"name": "AMZN Mktp US*2X4Y9 Amzn.com/bill WA"}) == "amzn mktp us"
    assert merchant_key({"name": "1234567"}) == ""


def _t(i, d, cents, name="Netflix", **kw):
    return {"transaction_id": f"t{i}-{name}", "date": d.isoformat(), "amount_cents": cents,
            "name": name, "merchant_name": name, "pending": 0, "pfc_primary": None, **kw}


def _series(name, cents, first, cycle_days, n, **kw):
    return [_t(i, first + timedelta(days=cycle_days * i), cents, name, **kw) for i in range(n)]


def test_detects_monthly_with_jitter_and_price_rise():
    ds = [date(2026, 5, 3), date(2026, 6, 3), date(2026, 7, 6), date(2026, 8, 3), date(2026, 9, 3)]
    txns = [_t(i, d, 1549 if i < 4 else 1799) for i, d in enumerate(ds)]
    [s] = detect_recurring(txns, TODAY)
    assert (s["key"], s["cycle"], s["amount_cents"]) == ("netflix", "monthly", 1799)
    assert s["next_date"] == "2026-11-03"  # Oct 3 already passed relative to today
    assert s["occurrences"] == 5 and s["last_date"] == "2026-09-03"
    assert s["bill_id"] is None


def test_detects_weekly_quarterly_yearly():
    weekly = _series("Gym Pass", 1000, date(2026, 8, 12), 7, 8)            # last 2026-09-30
    quarterly = _series("Cloud Backup", 3000, date(2026, 1, 15), 91, 3)    # Jan 15, Apr 16, Jul 16
    yearly = [_t(0, date(2024, 11, 2), 1500, "Domain Reg"),
              _t(1, date(2025, 11, 2), 1500, "Domain Reg")]
    got = {s["key"]: s for s in detect_recurring(weekly + quarterly + yearly, TODAY)}
    assert got["gym pass"]["cycle"] == "weekly"
    assert got["gym pass"]["next_date"] == "2026-10-07"  # 09-30 + 7 = today
    assert got["cloud backup"]["cycle"] == "quarterly"
    assert got["cloud backup"]["next_date"] == "2026-10-16"
    assert got["domain reg"]["cycle"] == "yearly"
    assert got["domain reg"]["next_date"] == "2026-11-02"


def test_rejects_irregular_unstable_and_too_few():
    irregular = [_t(i, date(2026, 5, 1) + timedelta(days=d), 1500, "Corner Cafe")
                 for i, d in enumerate([0, 9, 41, 44, 90])]
    wild_amounts = [_t(i, date(2026, 6, 1) + timedelta(days=30 * i), c, "Power Co")
                    for i, c in enumerate([5000, 12000, 3000, 9000])]
    two_monthly = _series("Newish", 999, date(2026, 8, 5), 31, 2)
    assert detect_recurring(irregular + wild_amounts + two_monthly, TODAY) == []


def test_lapsed_subscription_is_dropped():
    old = _series("Old Magazine", 1200, date(2026, 1, 5), 30, 4)  # last charge Apr 5
    assert detect_recurring(old, TODAY) == []


def test_skips_inflows_pending_transfers_and_loans():
    refunds = _series("Netflix", -1599, date(2026, 6, 1), 30, 4)
    pend = _series("Hulu", 999, date(2026, 6, 1), 30, 4, pending=1)
    xfer = _series("Zelle", 5000, date(2026, 6, 1), 30, 4, pfc_primary="TRANSFER_OUT")
    loan = _series("Car Loan", 40000, date(2026, 6, 1), 30, 4, pfc_primary="LOAN_PAYMENTS")
    assert detect_recurring(refunds + pend + xfer + loan, TODAY) == []


def test_known_and_dismissed_are_filtered():
    txns = _series("Netflix", 1599, date(2026, 6, 20), 30, 4) + \
        _series("Spotify", 1099, date(2026, 6, 22), 30, 4)
    assert {s["key"] for s in detect_recurring(txns, TODAY)} == {"netflix", "spotify"}
    assert {s["key"] for s in detect_recurring(txns, TODAY, known_texts=["NETFLIX"])} == {"spotify"}
    assert {s["key"] for s in detect_recurring(txns, TODAY, dismissed={"spotify"})} == {"netflix"}
    # "ne" is too short to count as a match for anything
    assert len(detect_recurring(txns, TODAY, known_texts=["ne"])) == 2


def test_matching_bill_is_flagged_not_hidden():
    txns = _series("Netflix", 1599, date(2026, 6, 20), 30, 4)
    bills = [{"id": 7, "name": "Streaming", "match_text": "netflix"},
             {"id": 8, "name": "Other", "match_text": ""}]
    [s] = detect_recurring(txns, TODAY, bills=bills)
    assert (s["bill_id"], s["bill_name"]) == (7, "Streaming")


def test_duplicate_same_day_rows_do_not_break_cadence():
    txns = _series("Netflix", 1599, date(2026, 6, 20), 30, 4)
    txns.append({**txns[1], "transaction_id": "dupe"})
    [s] = detect_recurring(txns, TODAY)
    assert s["occurrences"] == 4


def test_cancelled_merchant_hidden_until_charged_after_cancel_date():
    txns = _series("Netflix", 1599, date(2026, 6, 20), 30, 4)       # last charge 2026-09-18
    c = [{"text": "netflix", "cancelled_on": "2026-09-20"}]
    assert detect_recurring(txns, TODAY, cancelled=c) == []
    assert detect_recurring(txns, TODAY, cancelled=[{"text": "netflix", "cancelled_on": "2026-09-18"}]) == []  # same day = still the old charge
    assert len(detect_recurring(txns, TODAY, cancelled=[{"text": "netflix", "cancelled_on": "2026-09-17"}])) == 1
    assert len(detect_recurring(txns, TODAY, cancelled=[{"text": "hulu", "cancelled_on": "2026-09-20"}])) == 1
