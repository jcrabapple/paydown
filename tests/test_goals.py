"""Tests for app.goals (pure). Expected values hand-computed."""
from datetime import date

from app.goals import monthly_need, per_paycheck_suggestion

# Goal example from docs/SAFE.md: target 90000, set_aside 15000, target 2027-03-01.

PAYDAYS = [
    date(2026, 10, 9),
    date(2026, 10, 23),
    date(2026, 11, 6),
    date(2026, 11, 20),
    date(2026, 12, 4),
    date(2026, 12, 18),
]


def test_no_target_date_returns_none():
    assert per_paycheck_suggestion(90000, 15000, None, PAYDAYS, date(2026, 10, 2)) is None


def test_set_aside_ge_target_returns_none():
    assert per_paycheck_suggestion(90000, 90000, date(2027, 3, 1), PAYDAYS, date(2026, 10, 2)) is None
    assert per_paycheck_suggestion(90000, 95000, date(2027, 3, 1), PAYDAYS, date(2026, 10, 2)) is None


def test_zero_paydays_gives_remaining():
    # No paydays in [today, target_date]: need the whole remaining amount.
    assert per_paycheck_suggestion(90000, 15000, date(2026, 10, 3), [], date(2026, 10, 2)) == 75000
    # Paydays exist but all before today.
    assert per_paycheck_suggestion(
        90000, 15000, date(2026, 10, 3), [date(2026, 10, 1)], date(2026, 10, 2)
    ) == 75000


def test_one_payday_ceil_to_dollar():
    # 75000 cents over 1 payday = 75000 -> already whole dollars.
    assert per_paycheck_suggestion(
        90000, 15000, date(2026, 10, 9), [date(2026, 10, 9)], date(2026, 10, 2)
    ) == 75000
    # 75001 cents over 1 payday -> ceil to dollar -> 75100.
    assert per_paycheck_suggestion(
        90001, 15000, date(2026, 10, 9), [date(2026, 10, 9)], date(2026, 10, 2)
    ) == 75100


def test_several_paydays_ceil_to_dollar():
    # 75000 over 6 paydays = 12500 exactly.
    assert per_paycheck_suggestion(90000, 15000, date(2026, 12, 18), PAYDAYS, date(2026, 10, 2)) == 12500
    # 90001 over 3 paydays = 30000.33 cents -> ceil to dollar -> 30100.
    assert per_paycheck_suggestion(
        90001, 0, date(2026, 10, 23), [date(2026, 10, 9), date(2026, 10, 16), date(2026, 10, 23)],
        date(2026, 10, 2),
    ) == 30100


def test_paydays_outside_target_date_not_counted():
    # A payday after the target date doesn't count: 2 of the 3 listed.
    assert per_paycheck_suggestion(
        90000, 30000, date(2026, 10, 16),
        [date(2026, 10, 9), date(2026, 10, 16), date(2026, 10, 23)],
        date(2026, 10, 2),
    ) == 30000  # 60000 / 2


def test_monthly_need_basic():
    # target 2027-03, month 2026-10: months_left = 5 + 1 = 6; 75000/6 = 12500.
    assert monthly_need(90000, 15000, date(2027, 3, 1), "2026-10") == 12500


def test_monthly_need_target_month():
    # month == target month: months_left = 1; 75000 -> 75000.
    assert monthly_need(90000, 15000, date(2027, 3, 1), "2027-03") == 75000


def test_monthly_need_month_before_start():
    # target month 2027-03 < month 2027-04 -> 0.
    assert monthly_need(90000, 15000, date(2027, 3, 1), "2027-04") == 0


def test_monthly_need_fully_funded():
    assert monthly_need(90000, 90000, date(2027, 3, 1), "2026-10") == 0
    assert monthly_need(90000, 95000, date(2027, 3, 1), "2026-10") == 0


def test_monthly_need_no_target_date():
    assert monthly_need(90000, 0, None, "2026-10") == 0


def test_monthly_need_ceil_to_dollar():
    # 75001 over 6 months = 12500.17 cents -> ceil to dollar -> 12501? No:
    # ceil-to-dollar means whole dollars -> 12501 cents is $125.01 -> $126 = 12600.
    assert monthly_need(90001, 15000, date(2027, 3, 1), "2026-10") == 12600


def test_monthly_need_cross_year():
    # target 2027-01, month 2026-11: months_left = 2 + 1 = 3; 90000/3 = 30000.
    assert monthly_need(90000, 0, date(2027, 1, 15), "2026-11") == 30000