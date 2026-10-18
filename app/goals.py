"""Goal funding math: per-paycheck and monthly suggestions.

Pure module: no DB, no I/O, no clock calls.
"""
from __future__ import annotations


def _ceil_to_dollar_cents(total_cents: int, parts: int) -> int:
    """ceil-to-dollar of total_cents / parts, in cents."""
    return -(-total_cents // (parts * 100)) * 100


def _month_index(month: str) -> int:
    return int(month[:4]) * 12 + int(month[5:7]) - 1


def per_paycheck_suggestion(
    target: int,
    set_aside: int,
    target_date,  # date | None
    payday_dates: list,  # list[date]
    today,  # date
) -> int | None:
    """Suggested set-aside per paycheck, in cents, or None when not applicable."""
    if target_date is None or set_aside >= target:
        return None
    n = sum(1 for d in payday_dates if today <= d <= target_date)
    remaining = target - set_aside
    if n == 0:
        return remaining
    return _ceil_to_dollar_cents(remaining, n)


def monthly_need(
    target: int,
    set_aside_before_month: int,
    target_date,  # date | None
    month: str,
) -> int:
    """Monthly budget need for a goal in cents (0 when not applicable).

    Uses the balance before the month so the budget stays stable while money
    is set aside mid-month.
    """
    if target_date is None:
        return 0
    target_month = f"{target_date.year:04d}-{target_date.month:02d}"
    if target_month < month or set_aside_before_month >= target:
        return 0
    months_left = _month_index(target_month) - _month_index(month) + 1
    return _ceil_to_dollar_cents(target - set_aside_before_month, months_left)