"""FIX5 Lane E tests (E1 zero-minimum, E2 non-terminating baseline)."""
from app.cardfx import estimate_min_cents
from app.engine import compare, minimums_only, monthly_interest_cents, simulate


def card(id, name, balance, apr, min_pay, priority=0):
    return {
        "id": id,
        "name": name,
        "balance_cents": balance,
        "apr": apr,
        "min_payment_cents": min_pay,
        "priority": priority,
    }


def rh_card():
    # Synced card with current cycle satisfied (min reported as $0)
    return card(1, "Card A", 400000, 24.99, 0)


def test_e1_minimums_only_month1_zero_then_estimates():
    c = rh_card()
    res = minimums_only([c], "2026-10")
    # Month 1: minimum is the reported $0 -> payment 0, full balance carries.
    assert res["schedule"][0]["payments"]["1"] == 0
    # Month 2 onward: estimated minimum, so it terminates (never flagged False).
    assert res["months"] is not None
    assert res["cards"][0]["never"] is False
    assert res["cards"][0]["payoff_month"] is not None
    # Payments after month 1 are all > 0.
    for row in res["schedule"][1:]:
        assert row["payments"]["1"] > 0


def test_e1_minimums_only_month2_equals_estimate():
    c = rh_card()
    res = minimums_only([c], "2026-10")
    row = res["schedule"][1]
    # Month-2 balance at start, after month 2's interest has been applied:
    # 400000 + m1 interest + m2 interest.
    b1 = 400000 + monthly_interest_cents(400000, 24.99)
    b2 = b1 + monthly_interest_cents(b1, 24.99)
    assert row["payments"]["1"] == estimate_min_cents(b2, 24.99)


def test_e1_simulate_budget_zero_extra_month2_equals_estimate():
    # Budget with 0 extra: budget equals the month-2 estimated minimum, so the
    # month-2 payment is exactly the estimate (all of it minimum).
    c = rh_card()
    b1 = 400000 + monthly_interest_cents(400000, 24.99)
    b2 = b1 + monthly_interest_cents(b1, 24.99)
    est = estimate_min_cents(b2, 24.99)
    res = simulate([dict(c)], est, "avalanche", "2026-10", first_month_budget_cents=0)
    assert res["schedule"][0]["payments"]["1"] == 0
    assert res["schedule"][1]["payments"]["1"] == est


def test_e1_nonzero_min_cards_unchanged():
    # AGENTS.md engine test 5: $10,000 at 30% with $100 min marks never.
    c = card(1, "trap", 1000000, 30.0, 10000)
    res = minimums_only([c], "2026-01")
    assert res["cards"][0]["never"] is True
    assert res["months"] is None
    # Non-zero minimum also unchanged under simulate: month 1 pays min.
    c2 = card(1, "v", 100000, 12.0, 10000)
    res2 = simulate([c2], 10000, "avalanche", "2026-10")
    assert res2["schedule"][0]["payments"]["1"] == 10000


def test_e1_zero_min_month1_only_in_compare_avalanche():
    # Under compare(), month 1 for the zero-min card is still 0.
    cards = [rh_card(), card(2, "Card B", 800000, 22.0, 20000)]
    out = compare(cards, 20000, "2026-10", "avalanche")
    assert out["avalanche"]["schedule"][0]["payments"]["1"] == 0
    assert out["avalanche"]["schedule"][1]["payments"]["1"] > 0


def test_e2_minimums_only_never_flag_true():
    # $10,000 at 30% with $100 min can never be paid by its minimum.
    c = card(1, "trap", 1000000, 30.0, 10000)
    out = compare([c], 10000, "2026-01", "avalanche")
    assert out["minimums_only_never"] is True
    assert out["interest_saved_cents"] is None
    assert out["months_saved"] is None


def test_e2_minimums_only_never_flag_false_when_baseline_finishes():
    c = card(1, "c", 100000, 12.0, 10000)
    out = compare([c], 10000, "2026-01", "avalanche")
    assert out["minimums_only_never"] is False
    assert out["interest_saved_cents"] is not None
