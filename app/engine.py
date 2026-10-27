"""Pure payoff math. No I/O, no imports from app.db."""
from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

from .cardfx import estimate_min_cents

MAX_MONTHS = 600

ENGINE_DOC = (
    "Interest is a monthly-rate approximation of daily compounding: "
    "interest = round_half_up(balance_cents * apr_pct / 100 / 12)."
)


def round_half_up_cents(x: Fraction | float) -> int:
    """Round a Fraction/float to the nearest int, halves rounding up (toward +inf)."""
    f = Fraction(x)
    if f >= 0:
        n = f.numerator // f.denominator
        if (f - n) * 2 >= 1:
            n += 1
        return n
    return -round_half_up_cents(-f)


def monthly_interest_cents(balance_cents: int, apr: float) -> int:
    """round_half_up(balance * apr / 100 / 12) using exact rational arithmetic."""
    if balance_cents <= 0 or apr <= 0:
        return 0
    return round_half_up_cents(
        Fraction(balance_cents) * Fraction(Decimal(str(apr))) / 1200
    )


def _add_month(month: str, n: int = 1) -> str:
    y, m = int(month[:4]), int(month[5:7])
    t = (y * 12 + (m - 1)) + n
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def _month_diff(a: str, b: str) -> int:
    return (int(a[:4]) * 12 + int(a[5:7])) - (int(b[:4]) * 12 + int(b[5:7]))


def _effective_minimum(c: dict, month_index: int) -> int:
    """E1: a synced card reporting min 0 (cycle already satisfied) pays 0 in
    month 1 only; from month 2 its minimum is the issuer-formula estimate on
    that month's post-interest starting balance."""
    if c["min_payment_cents"] or month_index <= 1:
        return c["min_payment_cents"]
    return estimate_min_cents(c["balance_cents"], c["apr"])


def _strategy_order(cards, strategy: str):
    """Cards in payment order; ties broken by id ascending."""
    if strategy == "snowball":
        key = lambda c: (c["balance_cents"], -c["apr"], c["id"])
    elif strategy == "custom":
        key = lambda c: (c.get("priority", 0), c["id"])
    else:  # avalanche (default)
        key = lambda c: (-c["apr"], c["balance_cents"], c["id"])
    return sorted(cards, key=key)


def _blank_card_rows(cards):
    return [
        {
            "id": c["id"],
            "name": c["name"],
            "payoff_month": None,
            "months_to_payoff": None,
            "interest_cents": 0,
            "never": False,
        }
        for c in cards
    ]


def simulate(
    cards: list[dict],
    monthly_budget_cents: int,
    strategy: str = "avalanche",
    start_month: str | None = None,
    lump_sum_cents: int = 0,
    max_months: int = MAX_MONTHS,
    first_month_budget_cents: int | None = None,
) -> dict:
    """Simulate month-by-month payoff. Pure; see docs/PLAN.md for the algorithm.

    first_month_budget_cents: when set, month 1 uses it instead of
    monthly_budget_cents (one-off adjustments and sweeps apply to month 1 only).
    """
    start_month = start_month or "2026-01"
    open_cards = [dict(c) for c in cards if c["balance_cents"] > 0]
    rows = {c["id"]: r for c, r in zip(open_cards, _blank_card_rows(open_cards))}
    result = {
        "months": None,
        "debt_free_month": None,
        "total_interest_cents": 0,
        "total_paid_cents": 0,
        "underfunded": False,
        "cards": [rows[c["id"]] for c in open_cards],
        "schedule": [],
    }
    if not open_cards:
        result["months"] = 0
        result["debt_free_month"] = _add_month(start_month, -1)
        return result

    min_sum = sum(min(c["min_payment_cents"], c["balance_cents"]) for c in open_cards)
    if monthly_budget_cents < min_sum or (
        first_month_budget_cents is not None and first_month_budget_cents < min_sum
    ):
        result["underfunded"] = True

    lump_left = max(0, lump_sum_cents)
    month = start_month

    for i in range(1, max_months + 1):
        month_budget = (
            first_month_budget_cents if i == 1 and first_month_budget_cents is not None
            else monthly_budget_cents
        )
        balances_at_start = {c["id"]: c["balance_cents"] for c in open_cards}
        # 1. Interest
        interest = {}
        for c in open_cards:
            it = monthly_interest_cents(c["balance_cents"], c["apr"])
            c["balance_cents"] += it
            interest[c["id"]] = it
            rows[c["id"]]["interest_cents"] += it
        order = _strategy_order(open_cards, strategy)
        payments = {c["id"]: 0 for c in open_cards}

        # 2. Lump sum (month 1 only), applied like extra, before minimums
        if i == 1 and lump_left > 0:
            for c in order:
                if lump_left <= 0:
                    break
                pay = min(lump_left, c["balance_cents"])
                c["balance_cents"] -= pay
                lump_left -= pay
                payments[c["id"]] += pay

        # 3. Minimums (paid in strategy order; if underfunded, budget runs out)
        budget_left = month_budget
        min_paid = 0
        for c in order:
            if budget_left <= 0:
                break
            pay = min(_effective_minimum(c, i), c["balance_cents"], budget_left)
            c["balance_cents"] -= pay
            budget_left -= pay
            min_paid += pay
            payments[c["id"]] += pay

        # 4. Extra / rollover: pool = budget - minimums actually paid
        pool = month_budget - min_paid
        for c in order:
            if pool <= 0:
                break
            pay = min(pool, c["balance_cents"])
            c["balance_cents"] -= pay
            pool -= pay
            payments[c["id"]] += pay

        # 5. Record
        total_paid_month = sum(payments.values())
        result["total_interest_cents"] += sum(interest.values())
        result["total_paid_cents"] += total_paid_month
        sched_balances = {}
        for c in open_cards:
            if balances_at_start[c["id"]] > 0 and c["balance_cents"] <= 0:
                rows[c["id"]]["payoff_month"] = month
                rows[c["id"]]["months_to_payoff"] = i
            sched_balances[str(c["id"])] = c["balance_cents"]
        result["schedule"].append(
            {
                "month": month,
                "payments": {str(k): v for k, v in payments.items()},
                "interest": {str(k): v for k, v in interest.items()},
                "balances": sched_balances,
                "total_balance_cents": sum(sched_balances.values()),
            }
        )
        if all(c["balance_cents"] <= 0 for c in open_cards):
            result["months"] = i
            result["debt_free_month"] = month
            break
        month = _add_month(month)

    if result["months"] is None:
        for r in result["cards"]:
            if r["payoff_month"] is None:
                r["never"] = True
    return result


def minimums_only(cards: list[dict], start_month: str | None = None, max_months: int = MAX_MONTHS) -> dict:
    """Baseline: each card pays only its own minimum, no rollover."""
    start_month = start_month or "2026-01"
    open_cards = [dict(c) for c in cards if c["balance_cents"] > 0]
    rows = {c["id"]: r for c, r in zip(open_cards, _blank_card_rows(open_cards))}
    result = {
        "months": None,
        "debt_free_month": None,
        "total_interest_cents": 0,
        "total_paid_cents": 0,
        "underfunded": False,
        "cards": [rows[c["id"]] for c in open_cards],
        "schedule": [],
    }
    if not open_cards:
        result["months"] = 0
        result["debt_free_month"] = _add_month(start_month, -1)
        return result

    month = start_month
    for i in range(1, max_months + 1):
        payments, interest = {}, {}
        month_interest = 0
        month_paid = 0
        balances = {}
        for c in open_cards:
            if c["balance_cents"] <= 0:
                payments[c["id"]], interest[c["id"]], balances[c["id"]] = 0, 0, 0
                continue
            it = monthly_interest_cents(c["balance_cents"], c["apr"])
            c["balance_cents"] += it
            interest[c["id"]] = it
            rows[c["id"]]["interest_cents"] += it
            pay = min(_effective_minimum(c, i), c["balance_cents"])
            c["balance_cents"] -= pay
            payments[c["id"]] = pay
            month_paid += pay
            month_interest += it
            balances[c["id"]] = c["balance_cents"]
            if balances[c["id"]] <= 0:
                rows[c["id"]]["payoff_month"] = month
                rows[c["id"]]["months_to_payoff"] = i
        result["total_interest_cents"] += month_interest
        result["total_paid_cents"] += month_paid
        result["schedule"].append(
            {
                "month": month,
                "payments": {str(k): v for k, v in payments.items()},
                "interest": {str(k): v for k, v in interest.items()},
                "balances": {str(k): v for k, v in balances.items()},
                "total_balance_cents": sum(balances.values()),
            }
        )
        if all(c["balance_cents"] <= 0 for c in open_cards):
            result["months"] = i
            result["debt_free_month"] = month
            break
        month = _add_month(month)

    # A card with a payoff month paid off; `never` iff it has no payoff month.
    for c in open_cards:
        r = rows[c["id"]]
        r["never"] = r["payoff_month"] is None
    return result


def compare(
    cards: list[dict],
    budget: int,
    start_month: str | None = None,
    strategy: str = "avalanche",
    lump_sum_cents: int = 0,
    first_month_budget_cents: int | None = None,
) -> dict:
    """All strategies side by side + savings of the chosen strategy vs minimums-only."""
    min_only = minimums_only(cards, start_month)
    out = {
        "avalanche": simulate(cards, budget, "avalanche", start_month, lump_sum_cents,
                              first_month_budget_cents=first_month_budget_cents),
        "snowball": simulate(cards, budget, "snowball", start_month, lump_sum_cents,
                             first_month_budget_cents=first_month_budget_cents),
        "custom": simulate(cards, budget, "custom", start_month, lump_sum_cents,
                           first_month_budget_cents=first_month_budget_cents),
        "minimums_only": min_only,
    }
    chosen = out[strategy] if strategy in ("avalanche", "snowball", "custom") else out["avalanche"]
    if chosen["months"] is not None and min_only["months"] is not None:
        months_saved = min_only["months"] - chosen["months"]
        interest_saved = min_only["total_interest_cents"] - chosen["total_interest_cents"]
    else:
        months_saved = None
        interest_saved = None
    out["strategy"] = strategy
    out["interest_saved_cents"] = interest_saved
    out["months_saved"] = months_saved
    # E2: when minimums_only never finishes, savings must stay null (never 0
    # because the baseline stopped) and we flag the baseline explicitly.
    out["minimums_only_never"] = min_only["months"] is None
    return out


def this_month_plan(
    cards: list[dict],
    budget: int,
    strategy: str = "avalanche",
    month: str | None = None,
    lump_sum_cents: int = 0,
) -> list[dict]:
    """First row of simulate() as recommended per-card payments."""
    month = month or "2026-01"
    res = simulate(cards, budget, strategy, month, lump_sum_cents=lump_sum_cents, max_months=1)
    if not res["schedule"]:
        return []
    row = res["schedule"][0]
    by_id = {c["id"]: c for c in cards}
    order = _strategy_order([dict(c) for c in cards if c["balance_cents"] > 0], strategy)
    plan = []
    for c in order:
        pay = row["payments"].get(str(c["id"]), 0)
        minimum = min(min(c["min_payment_cents"], c["balance_cents"]), pay)
        plan.append(
            {
                "card_id": c["id"],
                "name": c["name"],
                "minimum_cents": minimum,
                "extra_cents": pay - minimum,
                "recommended_cents": pay,
            }
        )
    # include cards that received nothing this month (e.g. underfunded) at the end
    for c in by_id.values():
        if c["balance_cents"] > 0 and all(p["card_id"] != c["id"] for p in plan):
            plan.append(
                {
                    "card_id": c["id"],
                    "name": c["name"],
                    "minimum_cents": 0,
                    "extra_cents": 0,
                    "recommended_cents": 0,
                }
            )
    return plan
