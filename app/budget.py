"""Monthly budget waterfall. Pure function of DB rows -> dict."""
from __future__ import annotations


def compute_budget(
    income_rows: list[dict],
    expense_rows: list[dict],
    card_rows: list[dict],
    adjustment_rows: list[dict],
    settings: dict,
    month: str,
    goals_monthly_cents: int = 0,
    subscriptions_monthly_cents: int = 0,
) -> dict:
    """Waterfall per docs/PLAN.md + docs/CASH.md + docs/SAFE.md.

    adjustment_rows must already be filtered to `month`. Rows with
    kind == 'cash' are cash sweeps: always positive, they add straight to the
    extra (bypassing buffer and debt_share_pct) and never count as income.
    goals_monthly_cents is set aside for goals this month (SAFE.md); it is
    subtracted from the waterfall like an essential.
    subscriptions_monthly_cents is the monthly-equivalent cost of every active
    subscription (docs/SUBSCRIPTIONS.md); also an essential, kept separate from
    bills so a bill converted to a subscription is never counted twice.
    """
    income_total = sum(r["amount_cents"] for r in income_rows if r["active"]) + sum(
        a["amount_cents"] for a in adjustment_rows
        if a["amount_cents"] > 0 and a.get("kind", "manual") == "manual"
    )
    bills_total = sum(
        r["amount_cents"] for r in expense_rows if r["active"] and r["kind"] == "bill"
    )
    spending_total = sum(
        r["amount_cents"] for r in expense_rows if r["active"] and r["kind"] == "spending"
    )
    adjust_out = -sum(
        a["amount_cents"] for a in adjustment_rows
        if a["amount_cents"] < 0 and a.get("kind", "manual") == "manual"
    )
    cash_applied = sum(
        a["amount_cents"] for a in adjustment_rows
        if a["amount_cents"] > 0 and a.get("kind") == "cash"
    )
    open_cards = [c for c in card_rows if not c["closed"] and c["balance_cents"] > 0]
    minimums_total = sum(min(c["min_payment_cents"], c["balance_cents"]) for c in open_cards)
    available_after_min = (
        income_total - bills_total - spending_total - adjust_out - minimums_total
        - goals_monthly_cents - subscriptions_monthly_cents
    )
    buffer = settings.get("buffer_cents", 0) or 0
    surplus = available_after_min - buffer
    debt_share_pct = settings.get("debt_share_pct", 100)
    suggested_extra = max(0, (surplus * debt_share_pct // 100) // 100 * 100)
    override = settings.get("extra_override_cents")

    def _base_extra(recurring: bool) -> int:
        """override-or-suggestion. `recurring` computes the suggestion from a
        waterfall with no one-off adjustments (the recurring debt budget)."""
        if override is not None:
            return override
        if recurring:
            rec_available = (
                sum(r["amount_cents"] for r in income_rows if r["active"])
                - bills_total - spending_total - minimums_total - goals_monthly_cents
                - subscriptions_monthly_cents
            )
            rec_surplus = rec_available - buffer
            return max(0, (rec_surplus * debt_share_pct // 100) // 100 * 100)
        return suggested_extra

    base_extra = _base_extra(False)
    extra = base_extra + cash_applied
    debt_budget = minimums_total + extra
    recurring_debt_budget = minimums_total + _base_extra(True)
    shortfall = max(0, -available_after_min)
    # Leftover is unchanged by sweeps: the sweep comes out of checking, not income.
    leftover = available_after_min - base_extra
    return {
        "month": month,
        "income_total_cents": income_total,
        "bills_total_cents": bills_total,
        "spending_total_cents": spending_total,
        "adjust_out_cents": adjust_out,
        "minimums_total_cents": minimums_total,
        "goals_cents": goals_monthly_cents,
        "subscriptions_total_cents": subscriptions_monthly_cents,
        "essentials_cents": (
            bills_total + spending_total + minimums_total + goals_monthly_cents
            + subscriptions_monthly_cents
        ),
        "available_after_min_cents": available_after_min,
        "buffer_cents": buffer,
        "surplus_cents": surplus,
        "debt_share_pct": debt_share_pct,
        "suggested_extra_cents": suggested_extra,
        "extra_override_cents": override,
        "extra_cents": extra,
        "cash_applied_cents": cash_applied,
        "debt_budget_cents": debt_budget,
        "recurring_debt_budget_cents": recurring_debt_budget,
        "shortfall_cents": shortfall,
        "leftover_cents": leftover,
    }
