"""Regression tests for FIX1 backend items B1-B3 (docs/FIX1.md)."""
import pytest
from fastapi.testclient import TestClient

from app.engine import minimums_only
from app.main import create_app

AUTH = ("user", "test")


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "budget.db"), "user", "test")
    with TestClient(app) as c:
        yield c


def _engine_card(card_id, name, balance, apr, min_pay, priority=0):
    return {
        "id": card_id,
        "name": name,
        "balance_cents": balance,
        "apr": apr,
        "min_payment_cents": min_pay,
        "priority": priority,
    }


# ---------------------------------------------------------------------------
# B1: month plan / budget waterfall must use start-of-month balances
# ---------------------------------------------------------------------------

def test_b1_payoff_payment_keeps_card_in_plan_and_budget_unchanged(client):
    """Logging the payment that pays a card off must not remove it from the
    plan, must not drop its minimum from minimums_total, and must not move
    suggested_extra."""
    client.post("/api/income", json={"name": "Pay", "amount_cents": 300000}, auth=AUTH)
    card = client.post(
        "/api/cards",
        json={"name": "Discover", "balance_cents": 95000, "apr": 0.0, "min_payment_cents": 5000},
        auth=AUTH,
    ).json()

    before = client.get("/api/month/2026-03", auth=AUTH).json()
    assert before["budget"]["minimums_total_cents"] == 5000
    row = {p["card_id"]: p for p in before["plan"]}[card["id"]]
    assert row["recommended_cents"] == 95000
    min_total_before = before["budget"]["minimums_total_cents"]
    extra_before = before["budget"]["suggested_extra_cents"]

    # Pay the card off entirely within month 2026-03.
    r = client.post(
        "/api/payments",
        json={"card_id": card["id"], "amount_cents": 95000, "paid_on": "2026-03-15", "month": "2026-03"},
        auth=AUTH,
    )
    assert r.status_code == 201
    # Live balance is now 0...
    assert client.get("/api/cards", auth=AUTH).json()[0]["balance_cents"] == 0

    after = client.get("/api/month/2026-03", auth=AUTH).json()
    # ...but the card must still be in the month plan with the payment visible.
    plan = {p["card_id"]: p for p in after["plan"]}
    assert card["id"] in plan, "paid-off card vanished from the month plan"
    row2 = plan[card["id"]]
    assert row2["paid_cents"] == 95000
    assert row2["remaining_cents"] == 0
    assert row2["recommended_cents"] == 95000
    assert after["budget"]["minimums_total_cents"] == min_total_before == 5000
    assert after["budget"]["suggested_extra_cents"] == extra_before


def test_b1_partial_payment_leaves_recommendations_unchanged(client):
    """A partial payment on one card must not change recommended_cents for any
    card in that month (start-of-month balances drive the plan)."""
    client.post("/api/income", json={"name": "Pay", "amount_cents": 300000}, auth=AUTH)
    a = client.post(
        "/api/cards",
        json={"name": "A", "balance_cents": 50000, "apr": 0.0, "min_payment_cents": 5000},
        auth=AUTH,
    ).json()
    b = client.post(
        "/api/cards",
        json={"name": "B", "balance_cents": 80000, "apr": 0.0, "min_payment_cents": 5000},
        auth=AUTH,
    ).json()

    before = client.get("/api/month/2026-04", auth=AUTH).json()
    rec_before = {p["card_id"]: p["recommended_cents"] for p in before["plan"]}

    client.post(
        "/api/payments",
        json={"card_id": b["id"], "amount_cents": 20000, "paid_on": "2026-04-10", "month": "2026-04"},
        auth=AUTH,
    )

    after = client.get("/api/month/2026-04", auth=AUTH).json()
    rec_after = {p["card_id"]: p["recommended_cents"] for p in after["plan"]}
    assert rec_after == rec_before, "recommendations moved because a payment was logged"
    plan = {p["card_id"]: p for p in after["plan"]}
    assert plan[b["id"]]["paid_cents"] == 20000
    assert plan[b["id"]]["remaining_cents"] == max(0, rec_before[b["id"]] - 20000)


def test_b1_projection_uses_start_of_month_balances(client):
    """/api/projection?month= must ignore payments already logged in that month."""
    client.post(
        "/api/cards",
        json={"name": "V", "balance_cents": 100000, "apr": 12.0, "min_payment_cents": 10000},
        auth=AUTH,
    )
    before = client.get("/api/projection", params={"month": "2026-05"}, auth=AUTH).json()
    client.post(
        "/api/payments",
        json={"card_id": 1, "amount_cents": 40000, "paid_on": "2026-05-05", "month": "2026-05"},
        auth=AUTH,
    )
    after = client.get("/api/projection", params={"month": "2026-05"}, auth=AUTH).json()
    assert (
        after["avalanche"]["total_interest_cents"] == before["avalanche"]["total_interest_cents"]
    ), "projection changed because a payment was already logged for the month"


# ---------------------------------------------------------------------------
# B2: closed cards must not leak into the engine
# ---------------------------------------------------------------------------

def test_b2_closed_card_never_appears_in_plan_or_projection(client):
    client.post("/api/income", json={"name": "Pay", "amount_cents": 300000}, auth=AUTH)
    open_card = client.post(
        "/api/cards",
        json={"name": "Open", "balance_cents": 50000, "apr": 0.0, "min_payment_cents": 5000},
        auth=AUTH,
    ).json()
    closed_card = client.post(
        "/api/cards",
        json={"name": "Old", "balance_cents": 999999, "apr": 19.99, "min_payment_cents": 90000},
        auth=AUTH,
    ).json()
    r = client.put(f"/api/cards/{closed_card['id']}", json={"closed": True}, auth=AUTH)
    assert r.status_code == 200

    m = client.get("/api/month/2026-06", auth=AUTH).json()
    plan_ids = {p["card_id"] for p in m["plan"]}
    assert closed_card["id"] not in plan_ids
    assert open_card["id"] in plan_ids
    assert m["budget"]["minimums_total_cents"] == 5000

    proj = m["projection"]
    for key in ("avalanche", "snowball", "custom", "minimums_only"):
        ids = {c["id"] for c in proj[key]["cards"]}
        assert closed_card["id"] not in ids, f"closed card leaks into projection.{key}"
        for row in proj[key]["schedule"]:
            assert str(closed_card["id"]) not in row["payments"]
            assert str(closed_card["id"]) not in row["balances"]

    p = client.get("/api/projection", params={"month": "2026-06"}, auth=AUTH).json()
    for key in ("avalanche", "snowball", "custom", "minimums_only"):
        ids = {c["id"] for c in p[key]["cards"]}
        assert closed_card["id"] not in ids


# ---------------------------------------------------------------------------
# B3: a card with a payoff month must never be flagged never
# ---------------------------------------------------------------------------

def test_b3_minimums_only_never_iff_no_payoff_month():
    """In minimums_only, `never` must be exactly `payoff_month is None`.
    A card that pays off must never carry never=True, even alongside a
    minimum-below-interest card."""
    trap = _engine_card(1, "trap", 1000000, 30.0, 10000)   # min $100 < $250 interest -> never
    payer = _engine_card(2, "payer", 10000, 0.0, 5000)     # $100 at 0%, min $50 -> pays off month 2
    res = minimums_only([trap, payer], "2026-01")
    by_id = {c["id"]: c for c in res["cards"]}
    assert by_id[1]["payoff_month"] is None
    assert by_id[1]["never"] is True          # keeps AGENTS.md test 5 semantics
    assert by_id[2]["payoff_month"] == "2026-02"
    assert by_id[2]["never"] is False, "paid-off card marked never"
    for c in res["cards"]:
        assert c["never"] == (c["payoff_month"] is None)