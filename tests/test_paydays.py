"""Tests for app.paydays (pure). All expected values hand-computed.

Calendar facts used (verified against the 2026/2027 calendar):
- 2026-10-02 is a Friday; Fridays in Oct 2026: 2, 9, 16, 23, 30.
- Saturdays in Oct 2026: 3, 10, 17, 24, 31. Sundays: 4, 11, 18, 25.
- 2026-11-01 is a Sunday; its previous Friday is 2026-10-30 (last Friday of Oct).
- 2026-11-15 is a Sunday -> previous-Friday shift to 2026-11-13.
- 2027 is not a leap year (Feb 28); 2028 is (Feb 29). April always has 30 days.
"""
from datetime import date

import pytest

from app.paydays import (
    GRACE_DAYS,
    infer_schedule,
    monthly_equivalent_cents,
    next_payday,
    occurrences,
    receipts,
)


def sched(**kw):
    s = {
        "id": kw.pop("id", 1),
        "name": kw.pop("name", "Paycheck"),
        "frequency": kw.pop("frequency", "weekly"),
        "anchor_date": kw.pop("anchor_date"),
        "weekend_rule": kw.pop("weekend_rule", "none"),
        "amount_cents": kw.pop("amount_cents", 100000),
        "active": kw.pop("active", 1),
    }
    s.update(kw)
    return s


def pc(txn_id, d, amount=215000, schedule_id=1):
    return {
        "transaction_id": txn_id,
        "date": d,
        "amount_cents": amount,
        "pay_schedule_id": schedule_id,
    }


# ---------------------------------------------------------------- occurrences

def test_weekly():
    s = sched(anchor_date="2026-10-02")  # Friday
    assert occurrences(s, date(2026, 10, 1), date(2026, 10, 20)) == [
        date(2026, 10, 2),
        date(2026, 10, 9),
        date(2026, 10, 16),
    ]


def test_biweekly_negative_k_anchor_after_start():
    # anchor 2026-10-09; k = -1 gives 2026-09-25 (Oct 9 - 14 days), inside the window.
    s = sched(frequency="biweekly", anchor_date="2026-10-09")
    assert occurrences(s, date(2026, 9, 20), date(2026, 9, 30)) == [date(2026, 9, 25)]


def test_monthly_31_feb_nonleap():
    # Monthly on the 31st anchored 2027-01-31: Feb 2027 has 28 days.
    s = sched(frequency="monthly", anchor_date="2027-01-31")
    assert occurrences(s, date(2027, 2, 1), date(2027, 2, 28)) == [date(2027, 2, 28)]


def test_monthly_31_feb_leap():
    s = sched(frequency="monthly", anchor_date="2027-01-31")
    assert occurrences(s, date(2028, 2, 1), date(2028, 2, 29)) == [date(2028, 2, 29)]


def test_monthly_31_april():
    # April has 30 days: 31 clamps to 2027-04-30.
    s = sched(frequency="monthly", anchor_date="2027-01-31")
    assert occurrences(s, date(2027, 4, 1), date(2027, 4, 30)) == [date(2027, 4, 30)]


def test_semimonthly_15_31():
    s = sched(
        frequency="semimonthly",
        anchor_date="2026-10-15",
        semimonthly_day1=15,
        semimonthly_day2=31,
    )
    assert occurrences(s, date(2026, 10, 1), date(2026, 10, 31)) == [
        date(2026, 10, 15),
        date(2026, 10, 31),
    ]


def test_weekend_previous():
    # Saturdays 3/10/17/24/31 shift back to Fridays 2/9/16/23/30.
    s = sched(anchor_date="2026-10-03", weekend_rule="previous")
    assert occurrences(s, date(2026, 10, 1), date(2026, 10, 31)) == [
        date(2026, 10, 2),
        date(2026, 10, 9),
        date(2026, 10, 16),
        date(2026, 10, 23),
        date(2026, 10, 30),
    ]


def test_weekend_next():
    # Saturdays shift forward to Mondays 5/12/19/26; Oct 31 -> Nov 2 falls
    # outside the window end and is filtered out after adjustment.
    s = sched(anchor_date="2026-10-03", weekend_rule="next")
    assert occurrences(s, date(2026, 10, 1), date(2026, 10, 31)) == [
        date(2026, 10, 5),
        date(2026, 10, 12),
        date(2026, 10, 19),
        date(2026, 10, 26),
    ]


def test_sunday_first_shifts_to_previous_month_last_friday():
    # Monthly on the 1st anchored 2026-11-01 (a Sunday): 'previous' moves it to
    # 2026-10-30, the last Friday of October, which is before the nominal month
    # and before a window starting 2026-10-25 -- still returned because the
    # nominal date 2026-11-01 is generated (start-7 covers it) and filtering
    # happens after adjustment.
    s = sched(frequency="monthly", anchor_date="2026-11-01", weekend_rule="previous")
    assert occurrences(s, date(2026, 10, 25), date(2026, 11, 8)) == [date(2026, 10, 30)]


def test_weekend_none_keeps_saturday():
    s = sched(anchor_date="2026-10-03", weekend_rule="none")
    assert occurrences(s, date(2026, 10, 1), date(2026, 10, 31)) == [
        date(2026, 10, 3),
        date(2026, 10, 10),
        date(2026, 10, 17),
        date(2026, 10, 24),
        date(2026, 10, 31),
    ]


def test_adjusted_into_window_semimonthly():
    # Semimonthly 15/31 with 'previous': nominal 2026-10-31 (Saturday) shifts to
    # 2026-10-30 == window start; nominal 2026-11-15 (Sunday) shifts to 11-13;
    # 2026-11-30 (Monday) stays.
    s = sched(
        frequency="semimonthly",
        anchor_date="2026-10-15",
        semimonthly_day1=15,
        semimonthly_day2=31,
        weekend_rule="previous",
    )
    assert occurrences(s, date(2026, 10, 30), date(2026, 11, 30)) == [
        date(2026, 10, 30),
        date(2026, 11, 13),
        date(2026, 11, 30),
    ]


def test_dedup():
    # Semimonthly with day1 == day2 would double-generate; result must dedupe.
    s = sched(
        frequency="semimonthly",
        anchor_date="2026-10-15",
        semimonthly_day1=15,
        semimonthly_day2=15,
    )
    assert occurrences(s, date(2026, 10, 1), date(2026, 10, 31)) == [date(2026, 10, 15)]


# ------------------------------------------------------- receipts / next_payday

BIWEEKLY = sched(
    id=1,
    name="Main paycheck",
    frequency="biweekly",
    anchor_date="2026-10-09",
    amount_cents=215000,
)


def test_unconfirmed_rolls_at_midnight():
    # No paychecks at all: on Oct 8 the Oct 9 occurrence is a future candidate;
    # at midnight of Oct 9 the occurrence is "received" (date <= today) and the
    # next payday rolls to Oct 23.
    assert next_payday(date(2026, 10, 8), [BIWEEKLY], [])["payday"] == {
        "date": date(2026, 10, 9),
        "schedule_id": 1,
        "name": "Main paycheck",
        "expected_cents": 215000,
        "status": "upcoming",
    }
    r = next_payday(date(2026, 10, 9), [BIWEEKLY], [])
    assert r["payday"]["date"] == date(2026, 10, 23)
    assert r["missed"] == []


def test_confirmed_payday_morning_status_today():
    # Deposit-confirmed (Sep 25 paycheck carries schedule id 1). On the morning
    # of payday Oct 9 the paycheck hasn't posted yet, so the occurrence is an
    # unreceived candidate with status "today".
    r = next_payday(date(2026, 10, 9), [BIWEEKLY], [pc("t1", "2026-09-25")])
    assert r["payday"]["status"] == "today"
    assert r["payday"]["date"] == date(2026, 10, 9)
    assert r["missed"] == []


def test_paycheck_two_days_early_counts():
    # Paycheck posts Oct 7, two days before the Oct 9 occurrence: within
    # +/-GRACE_DAYS it maps to Oct 9, so that payday counts as received and the
    # candidate rolls to Oct 23.
    receipts_map = receipts(BIWEEKLY, [pc("t0", "2026-09-25"), pc("t1", "2026-10-07")],
                            date(2026, 9, 20), date(2026, 10, 31))
    assert set(receipts_map) == {date(2026, 9, 25), date(2026, 10, 9), date(2026, 10, 23)}
    assert receipts_map[date(2026, 10, 9)]["transaction_id"] == "t1"
    assert receipts_map[date(2026, 9, 25)]["transaction_id"] == "t0"
    assert receipts_map[date(2026, 10, 23)] is None
    r = next_payday(date(2026, 10, 7), [BIWEEKLY], [pc("t0", "2026-09-25"), pc("t1", "2026-10-07")])
    assert r["payday"]["date"] == date(2026, 10, 23)
    assert r["last_payday"] == date(2026, 9, 25)


def test_paycheck_two_days_late_status_late():
    # Today Oct 11, the Oct 9 paycheck hasn't posted; Oct 9 is still within
    # grace (today <= occ + 3), so it stays a candidate with status "late".
    r = next_payday(date(2026, 10, 11), [BIWEEKLY], [pc("t0", "2026-09-25")])
    assert r["payday"] == {
        "date": date(2026, 10, 9),
        "schedule_id": 1,
        "name": "Main paycheck",
        "expected_cents": 215000,
        "status": "late",
    }
    assert r["missed"] == []
    assert r["last_payday"] == date(2026, 9, 25)


def test_paycheck_four_days_late_missed_next_is_candidate():
    # Today Oct 13: today (13) > occ (9) + GRACE_DAYS (3), so Oct 9 is missed
    # and the next occurrence Oct 23 becomes the candidate.
    r = next_payday(date(2026, 10, 13), [BIWEEKLY], [pc("t0", "2026-09-25")])
    assert r["missed"] == [
        {"date": date(2026, 10, 9), "schedule_id": 1, "name": "Main paycheck"}
    ]
    assert r["payday"]["date"] == date(2026, 10, 23)
    assert r["payday"]["status"] == "upcoming"


def test_two_schedules_pick_earliest():
    s1 = sched(id=1, name="Monthly", frequency="monthly", anchor_date="2026-10-25")
    s2 = sched(id=2, name="Weekly", frequency="weekly", anchor_date="2026-10-16")
    # Today Oct 3: monthly candidate Oct 25, weekly candidates Oct 9, 16, ...
    r = next_payday(date(2026, 10, 3), [s1, s2], [])
    assert r["payday"]["date"] == date(2026, 10, 9)
    assert r["payday"]["schedule_id"] == 2
    assert r["last_payday"] == date(2026, 10, 2)  # weekly Oct 2 <= today counts


def test_two_schedules_tie_lowest_id():
    s1 = sched(id=1, name="A", frequency="biweekly", anchor_date="2026-10-09")
    s2 = sched(id=2, name="B", frequency="weekly", anchor_date="2026-10-09")
    # Both unconfirmed with candidate Oct 9 (today Oct 8): tie -> lowest id.
    r = next_payday(date(2026, 10, 8), [s1, s2], [])
    assert r["payday"]["schedule_id"] == 1


def test_inactive_schedule_skipped():
    s = sched(active=0, anchor_date="2026-10-09")
    r = next_payday(date(2026, 10, 8), [s], [])
    assert r["payday"] is None


def test_last_payday_confirmed():
    r = next_payday(
        date(2026, 10, 9),
        [BIWEEKLY],
        [pc("t0", "2026-09-25"), pc("t1", "2026-10-09")],
    )
    assert r["last_payday"] == date(2026, 10, 9)


def test_each_paycheck_matches_one_occurrence():
    # Two paychecks near the same occurrence: the occurrence takes only one.
    m = receipts(
        BIWEEKLY,
        [pc("t1", "2026-10-08"), pc("t2", "2026-10-10")],
        date(2026, 10, 1),
        date(2026, 10, 31),
    )
    assert m[date(2026, 10, 9)]["transaction_id"] == "t1"  # closest (delta 1)


def test_no_schedule_no_candidates():
    r = next_payday(date(2026, 10, 2), [], [])
    assert r == {"payday": None, "missed": [], "last_payday": None, "last_paycheck_by_schedule": {}}


# ------------------------------------------------------------- infer_schedule

def test_infer_weekly():
    dates = ["2026-10-02", "2026-10-09", "2026-10-16", "2026-10-23"]  # all Fridays
    amounts = [100000, 100000, 100000, 100000]
    r = infer_schedule(dates, amounts)
    assert r["frequency"] == "weekly"
    assert r["anchor_date"] == date(2026, 10, 23)
    assert r["semimonthly_day1"] is None and r["semimonthly_day2"] is None
    assert r["weekend_rule"] == "previous"
    assert r["amount_cents"] == 100000
    assert r["confidence"] == "high"
    assert r["dates_used"] == [date(2026, 10, d) for d in (2, 9, 16, 23)]


def test_infer_biweekly_with_weekend_shift():
    # Sep 25, Oct 9, Oct 23 are Fridays; Nov 5 is a Thursday (a shifted payday).
    # Intervals: 14, 14, 13 -> median 14; 3 of 4 dates are Fridays (75%).
    r = infer_schedule(
        ["2026-09-25", "2026-10-09", "2026-10-23", "2026-11-05"],
        [215000, 215000, 214000, 215000],
    )
    assert r["frequency"] == "biweekly"
    # Median amount of [214000, 215000, 215000, 215000] = 215000.
    assert r["amount_cents"] == 215000
    assert r["confidence"] == "high"


def test_infer_semimonthly_1_15():
    # Days 1, 15, 1, 15, 2 -> clusters {1,2} (within 3) and {15}; intervals
    # 14,16,14,18 -> median 15; no dominant weekday (max 2/5).
    r = infer_schedule(
        ["2026-09-01", "2026-09-15", "2026-10-01", "2026-10-15", "2026-11-02"],
        [100000] * 5,
    )
    assert r["frequency"] == "semimonthly"
    assert r["semimonthly_day1"] == 1  # day 1 appears twice vs day 2 once
    assert r["semimonthly_day2"] == 15
    assert r["amount_cents"] == 100000


def test_infer_semimonthly_15_last_incl_feb():
    # 15th + last banking day: Nov 15 (Sun), Nov 30 (Mon), Dec 15 (Tue),
    # Dec 31 (Thu), Jan 15 2027 (Fri), Jan 29 2027 (Fri; Jan 31 is a Sunday).
    # Days-of-month: 15,30,15,31,15,29 -> clusters {15} and {29,30,31} = "31".
    # Intervals: 15,15,16,15,14 -> median 15.
    r = infer_schedule(
        ["2026-11-15", "2026-11-30", "2026-12-15", "2026-12-31", "2027-01-15", "2027-01-29"],
        [90000] * 6,
    )
    assert r["frequency"] == "semimonthly"
    assert r["semimonthly_day1"] == 15
    assert r["semimonthly_day2"] == 31
    assert r["anchor_date"] == date(2027, 1, 29)


def test_infer_monthly():
    # Jan 25, Feb 25, Mar 25, Apr 25 2027: intervals 31,28,31 -> median 31;
    # weekdays Mon/Thu/Thu/Sun (50% < 75%); one cluster but median not 12..18.
    r = infer_schedule(
        ["2027-01-25", "2027-02-25", "2027-03-25", "2027-04-25"], [100000] * 4
    )
    assert r["frequency"] == "monthly"
    assert r["anchor_date"] == date(2027, 4, 25)


def test_infer_two_dates_low_confidence():
    r = infer_schedule(["2026-10-02", "2026-10-16"], [100000, 100000])
    assert r["frequency"] == "biweekly"
    assert r["confidence"] == "low"


def test_infer_needs_two_dates():
    assert infer_schedule(["2026-10-02"], [100000]) is None
    assert infer_schedule([], []) is None


def test_infer_random_dates_none():
    # Intervals 3,24,15 -> median 15; weekdays spread; 3 day-clusters; median
    # not 26..33 -> no rule matches.
    assert (
        infer_schedule(["2026-10-02", "2026-10-05", "2026-10-29", "2026-11-13"], [100] * 4)
        is None
    )


def test_infer_amount_median_half_up():
    # Median of [100001, 100002] = 100001.5 -> half-up -> 100002.
    r = infer_schedule(["2026-10-02", "2026-10-09"], [100001, 100002])
    assert r["amount_cents"] == 100002


# -------------------------------------------------- monthly_equivalent_cents

def test_monthly_equivalent():
    # weekly 100000*52/12 = 433333.33 -> 433333
    assert monthly_equivalent_cents({"frequency": "weekly", "amount_cents": 100000}) == 433333
    # biweekly 215000*26/12 = 465833.33 -> 465833 (doc example)
    assert monthly_equivalent_cents({"frequency": "biweekly", "amount_cents": 215000}) == 465833
    assert monthly_equivalent_cents({"frequency": "semimonthly", "amount_cents": 100000}) == 200000
    assert monthly_equivalent_cents({"frequency": "monthly", "amount_cents": 100000}) == 100000
    # weekly 1 cent: 52/12 = 4.33 -> 4
    assert monthly_equivalent_cents({"frequency": "weekly", "amount_cents": 1}) == 4


def test_grace_days_value():
    assert GRACE_DAYS == 3


def test_unknown_frequency_raises():
    with pytest.raises(ValueError):
        occurrences({"frequency": "daily", "anchor_date": "2026-10-02"}, date(2026, 10, 1), date(2026, 10, 2))