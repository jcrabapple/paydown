"""Engine tests. Hand-checked cases per AGENTS.md."""
from app.engine import (
    compare,
    minimums_only,
    monthly_interest_cents,
    simulate,
    this_month_plan,
)


def card(id, name, balance, apr, min_pay, priority=0):
    return {
        "id": id,
        "name": name,
        "balance_cents": balance,
        "apr": apr,
        "min_payment_cents": min_pay,
        "priority": priority,
    }


def test_1_single_card_basic_math():
    # $1,000.00 at 12% APR -> monthly interest = 100000 * 12/1200 = 1000 = $10.00.
    # Min $100, budget $100 -> balance 100000 + 1000 - 10000 = 91000 ($910.00) after month 1.
    # Payoff in 11 months (fixed $100 against ~1% growth).
    c = card(1, "Visa", 100000, 12.0, 10000)
    res = simulate([c], 10000, "avalanche", "2026-01")
    m1 = res["schedule"][0]
    assert m1["interest"]["1"] == 1000          # $10.00
    assert m1["payments"]["1"] == 10000         # $100.00
    assert m1["balances"]["1"] == 91000         # $910.00
    assert res["months"] == 11
    assert res["debt_free_month"] == "2026-11"
    assert res["total_interest_cents"] == sum(
        row["interest"]["1"] for row in res["schedule"]
    )
    assert res["total_paid_cents"] - res["total_interest_cents"] == 100000


def test_2_zero_apr_exactly_10_months():
    # $500 at 0%, $50/mo budget -> exactly 10 months, zero interest.
    c = card(1, "Zero", 50000, 0.0, 5000)
    res = simulate([c], 5000, "avalanche", "2026-01")
    assert res["months"] == 10
    assert res["total_interest_cents"] == 0
    assert res["debt_free_month"] == "2026-10"


def test_3_rollover():
    # A $200 0% min $100; B $1000 0% min $50; budget $250, snowball (balance asc -> A first).
    # Month 1: A gets 100 min + 100 extra = 200 (paid off), B gets 50.
    # Month 2: A is gone; B receives the full 250 (min 50 + rolled-over 200).
    a = card(1, "A", 20000, 0.0, 10000)
    b = card(2, "B", 100000, 0.0, 5000)
    res = simulate([a, b], 25000, "snowball", "2026-01")
    m1 = res["schedule"][0]
    assert m1["payments"]["1"] == 20000  # A: 100 min + 100 extra
    assert m1["payments"]["2"] == 5000   # B: min only
    assert m1["balances"]["1"] == 0
    assert m1["balances"]["2"] == 95000
    m2 = res["schedule"][1]
    assert m2["payments"]["2"] == 25000  # B gets the whole budget
    assert m2["balances"]["2"] == 70000
    assert res["cards"][0]["payoff_month"] == "2026-01"
    assert res["cards"][1]["payoff_month"] == "2026-05"
    assert res["months"] == 5


def test_4_avalanche_vs_snowball():
    # small/low APR card vs large/high APR card.
    low = card(1, "low", 10000, 5.0, 5000)     # $100 at 5%
    high = card(2, "high", 100000, 25.0, 5000)  # $1,000 at 25%
    budget = 12000
    av = simulate([dict(low), dict(high)], budget, "avalanche", "2026-01")
    sb = simulate([dict(low), dict(high)], budget, "snowball", "2026-01")
    # Avalanche targets the 25% card first (apr desc); snowball the $100 card (balance asc).
    assert sum(av["schedule"][0]["payments"].values())
    assert av["schedule"][0]["payments"]["2"] > 0
    assert sb["schedule"][0]["payments"]["1"] > 0
    assert av["schedule"][0]["payments"]["2"] > sb["schedule"][0]["payments"]["2"]
    assert av["total_interest_cents"] <= sb["total_interest_cents"]


def test_5_minimum_below_interest_never():
    # $10,000 at 30% APR: monthly interest = 1,000,000 * 30/1200 = 25,000 ($250).
    # Min $100 < $250 -> minimums_only marks it never.
    c = card(1, "trap", 1000000, 30.0, 10000)
    res = minimums_only([c], "2026-01")
    assert res["cards"][0]["never"] is True
    assert res["cards"][0]["payoff_month"] is None
    assert res["months"] is None


def test_6_underfunded():
    c = card(1, "c", 50000, 0.0, 20000)
    res = simulate([c], 10000, "avalanche", "2026-01")
    assert res["underfunded"] is True
    # Budget exhausted on minimums: pays what it can in strategy order.
    assert res["schedule"][0]["payments"]["1"] == 10000


def test_7_lump_sum_helps():
    c = card(1, "c", 100000, 12.0, 10000)
    without = simulate([dict(c)], 15000, "avalanche", "2026-01")
    with_lump = simulate([dict(c)], 15000, "avalanche", "2026-01", lump_sum_cents=50000)
    assert with_lump["months"] < without["months"]
    assert with_lump["total_interest_cents"] < without["total_interest_cents"]


def test_8_round_half_up():
    # 12,345 cents at 10% APR: 12345 * 10 / 1200 = 102.875 -> 103 (round half up).
    assert monthly_interest_cents(12345, 10.0) == 103
    # 10300/1200... exact tie check: 12300 * 10/1200 = 102.5 -> 103.
    assert monthly_interest_cents(12300, 10.0) == 103
    # Just below the tie: 102.374... -> 102.
    assert monthly_interest_cents(12270, 10.0) == 102


def test_max_months_never_flags():
    # Budget far below interest: never pays off within 600 months.
    c = card(1, "c", 1000000, 30.0, 10000)
    res = simulate([c], 10000, "avalanche", "2026-01", max_months=600)
    assert res["months"] is None
    assert res["debt_free_month"] is None
    assert res["cards"][0]["never"] is True


def test_compare_shape():
    cards = [card(1, "A", 20000, 0.0, 10000), card(2, "B", 100000, 12.0, 5000)]
    out = compare(cards, 25000, "2026-01", "avalanche")
    for key in ("avalanche", "snowball", "custom", "minimums_only"):
        assert key in out
    assert "interest_saved_cents" in out and "months_saved" in out
    assert out["interest_saved_cents"] >= 0


def test_this_month_plan():
    a = card(1, "A", 20000, 0.0, 10000)
    b = card(2, "B", 100000, 0.0, 5000)
    plan = this_month_plan([dict(a), dict(b)], 25000, "snowball", "2026-01")
    by_id = {p["card_id"]: p for p in plan}
    assert by_id[1]["recommended_cents"] == 20000
    assert by_id[1]["minimum_cents"] == 10000 and by_id[1]["extra_cents"] == 10000
    assert by_id[2]["recommended_cents"] == 5000
    assert by_id[2]["minimum_cents"] == 5000 and by_id[2]["extra_cents"] == 0
