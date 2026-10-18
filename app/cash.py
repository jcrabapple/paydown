"""Pure cash-position math (docs/CASH.md). No I/O.

Timestamps (pending payments vs the cash balance age) are resolved by the
caller; this module receives `effective_cash_cents` (cash_total - pending_out).
"""
from __future__ import annotations

from fractions import Fraction

from .engine import round_half_up_cents


def _floor_dollar(cents: int) -> int:
    return (cents // 100) * 100


def extra_remaining_cents(plan_rows: list[dict]) -> int:
    """Unpaid EXTRA in this month's plan: for each row,
    max(0, remaining - max(0, minimum - paid)). Unpaid minimums are inside
    essentials and must not count here."""
    total = 0
    for r in plan_rows:
        unpaid_min = max(0, r["minimum_cents"] - r["paid_cents"])
        total += max(0, r["remaining_cents"] - unpaid_min)
    return total


def cash_position(
    account_rows: list[dict],
    settings: dict,
    essentials_cents: int,
    plan_rows: list[dict],
    effective_cash_cents: int | None = None,
    goals_set_aside_cents: int = 0,
) -> dict:
    """Spare-cash math for one month.

    account_rows: cash_accounts rows (only include_in_cash counts).
    effective_cash_cents: cash_total - pending_out; defaults to cash_total.
    goals_set_aside_cents: money earmarked in checking for goals (SAFE.md) -
    added to the reserve so it never shows as spare.
    Returns the `cash` bundle keys (minus `accounts`, `is_current_month`).
    """
    included = [a for a in account_rows if a.get("include_in_cash")]
    cash_total = sum(a["balance_cents"] for a in included)
    if effective_cash_cents is None:
        effective_cash_cents = cash_total
    cash_floor = settings.get("cash_floor_cents") or 0
    reserve_months = settings.get("cash_reserve_months")
    if reserve_months is None:
        reserve_months = 1.0
    extra_remaining = extra_remaining_cents(plan_rows)
    reserve = (
        cash_floor
        + round_half_up_cents(Fraction(essentials_cents) * Fraction(str(reserve_months)))
        + extra_remaining
        + goals_set_aside_cents
    )
    spare = max(0, _floor_dollar(effective_cash_cents - reserve))
    if essentials_cents == 0:
        months_covered = None
    else:
        # S3: never negative - cash below the floor is 0.0 months covered.
        months_covered = max(
            0.0,
            round((effective_cash_cents - cash_floor - extra_remaining) / essentials_cents, 1),
        )
    return {
        "cash_total_cents": cash_total,
        "cash_floor_cents": cash_floor,
        "reserve_months": reserve_months,
        "essentials_cents": essentials_cents,
        "extra_remaining_cents": extra_remaining,
        "goals_set_aside_cents": goals_set_aside_cents,
        "reserve_cents": reserve,
        "spare_cents": spare,
        "months_covered": months_covered,
    }
