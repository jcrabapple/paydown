"""Budget waterfall tests."""
from app.budget import compute_budget


def S(**kw):
    return {"strategy": "avalanche", "buffer_cents": 0, "debt_share_pct": 100, "extra_override_cents": None, **kw}


def test_basic_waterfall():
    income = [{"amount_cents": 500000, "active": 1}]
    expenses = [
        {"amount_cents": 150000, "kind": "bill", "active": 1},
        {"amount_cents": 50000, "kind": "spending", "active": 1},
    ]
    cards = [
        {"balance_cents": 100000, "min_payment_cents": 25000, "closed": 0},
        {"balance_cents": 0, "min_payment_cents": 25000, "closed": 0},
    ]
    b = compute_budget(income, expenses, cards, [], S(), "2026-01")
    assert b["income_total_cents"] == 500000
    assert b["bills_total_cents"] == 150000
    assert b["spending_total_cents"] == 50000
    assert b["minimums_total_cents"] == 25000  # paid-off card excluded
    assert b["available_after_min_cents"] == 275000
    assert b["surplus_cents"] == 275000
    assert b["suggested_extra_cents"] == 275000  # 100% share, already whole dollars
    assert b["extra_cents"] == 275000
    assert b["debt_budget_cents"] == 300000
    assert b["shortfall_cents"] == 0
    assert b["leftover_cents"] == 0


def test_suggested_extra_floors_to_whole_dollars():
    income = [{"amount_cents": 123456, "active": 1}]
    b = compute_budget(income, [], [], [], S(), "2026-01")
    # surplus 123456 * 100% = 123456 -> floor to 123400.
    assert b["suggested_extra_cents"] == 123400


def test_buffer_and_debt_share():
    income = [{"amount_cents": 100000, "active": 1}]
    b = compute_budget(income, [], [], [], S(buffer_cents=20000, debt_share_pct=50), "2026-01")
    assert b["surplus_cents"] == 80000
    assert b["suggested_extra_cents"] == 40000  # 50% of 80000 = 40000
    assert b["leftover_cents"] == 60000  # 100000 - 40000 (includes buffer)


def test_override_wins():
    income = [{"amount_cents": 100000, "active": 1}]
    b = compute_budget(income, [], [], [], S(extra_override_cents=12345), "2026-01")
    assert b["extra_cents"] == 12345


def test_negative_adjustment_and_shortfall():
    income = [{"amount_cents": 100000, "active": 1}]
    cards = [{"balance_cents": 50000, "min_payment_cents": 80000, "closed": 0}]
    adj = [{"amount_cents": -30000}]
    b = compute_budget(income, [], cards, adj, S(), "2026-01")
    assert b["income_total_cents"] == 100000
    assert b["adjust_out_cents"] == 30000
    assert b["minimums_total_cents"] == 50000  # min(min 80000, balance 50000)
    assert b["available_after_min_cents"] == 20000
    assert b["shortfall_cents"] == 0
    # Now income too low: shortfall.
    b2 = compute_budget([{"amount_cents": 50000, "active": 1}], [], cards, adj, S(), "2026-01")
    assert b2["available_after_min_cents"] == -30000
    assert b2["shortfall_cents"] == 30000
    assert b2["extra_cents"] == 0


def test_subscriptions_reduce_available_and_essentials():
    # income 5000.00, bills 1500.00, subscriptions 83.33/mo (a $1000 yearly accrual), min 250.00
    income = [{"amount_cents": 500000, "active": 1}]
    expenses = [{"amount_cents": 150000, "kind": "bill", "active": 1}]
    cards = [{"balance_cents": 100000, "min_payment_cents": 25000, "closed": 0}]
    base = compute_budget(income, expenses, cards, [], S(), "2026-01")
    b = compute_budget(income, expenses, cards, [], S(), "2026-01", 0, 8333)
    assert b["subscriptions_total_cents"] == 8333
    assert b["bills_total_cents"] == 150000                 # bills untouched: no double counting
    assert b["available_after_min_cents"] == base["available_after_min_cents"] - 8333
    assert b["essentials_cents"] == base["essentials_cents"] + 8333
    assert b["surplus_cents"] == 500000 - 150000 - 25000 - 8333  # 316667
    assert b["suggested_extra_cents"] == 316600             # floored to whole dollars
    # the recurring debt budget (what the projection uses) must see them too
    assert b["recurring_debt_budget_cents"] == 25000 + 316600
    assert base["subscriptions_total_cents"] == 0


def test_subscriptions_count_in_recurring_budget_when_adjustments_exist():
    income = [{"amount_cents": 400000, "active": 1}]
    adj = [{"amount_cents": 100000, "kind": "manual"}]       # bonus: month 1 only
    cards = [{"balance_cents": 900000, "min_payment_cents": 20000, "closed": 0}]
    b = compute_budget(income, [], cards, adj, S(), "2026-01", 0, 10000)
    # recurring: 4000.00 - 200.00 mins - 100.00 subs = 3700.00 extra; month 1 adds the bonus
    assert b["recurring_debt_budget_cents"] == 20000 + 370000
    assert b["debt_budget_cents"] == 20000 + 470000
