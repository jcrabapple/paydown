"""Create a demo Paydown database filled with a realistic, entirely fictional
household. Useful for trying the app, screenshots, and UI work without
touching real data.

    uv run python scripts/seed_demo.py data/demo.db
    BUDGET_DB=data/demo.db BUDGET_PASS=demo uv run python run.py

Every row goes through the real API (validation included), and dates are
relative to today, so the Today / Plan / Upcoming views always look current.
Refuses to touch an existing file.
"""
from __future__ import annotations

import argparse
import base64
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import clock  # noqa: E402
from app.main import create_app  # noqa: E402

USER, PASSWORD = "demo", "demo-seed-only"


def _ok(r, what):
    if r.status_code >= 300:
        raise SystemExit(f"seed failed at {what}: {r.status_code} {r.text}")
    return r.json() if r.content else None


def seed(db_path: str) -> dict:
    today = clock.today()
    month_start = today.replace(day=1)
    token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    c = TestClient(create_app(db_path, USER, PASSWORD),
                   headers={"Authorization": f"Basic {token}"})

    def post(path, body, what=None):
        return _ok(c.post(path, json=body), what or path)

    _ok(c.put("/api/settings", json={
        "strategy": "avalanche", "buffer_cents": 20000, "debt_share_pct": 100,
        "cash_floor_cents": 100000, "cash_reserve_months": 1,
    }), "settings")

    post("/api/income", {"name": "Salary", "amount_cents": 520000})
    post("/api/income", {"name": "Freelance design", "amount_cents": 60000})

    for name, cents, due in [("Rent", 165000, 1), ("Car insurance", 14200, 9),
                             ("Internet", 6500, 12), ("Electric", 11000, 18),
                             ("Phone", 8500, 22)]:
        post("/api/expenses", {"name": name, "amount_cents": cents, "kind": "bill",
                               "due_day": due})
    for name, cents in [("Groceries", 60000), ("Gas", 18000), ("Dining out", 20000)]:
        post("/api/expenses", {"name": name, "amount_cents": cents, "kind": "spending"})

    checking = post("/api/cash-accounts", {"name": "Checking", "kind": "checking",
                                           "balance_cents": 412000})
    post("/api/cash-accounts", {"name": "Emergency fund", "kind": "savings",
                                "balance_cents": 950000, "include_in_cash": False})

    cards = {}
    for name, bal, apr, minimum, due, limit in [
        ("Everyday Rewards Visa", 482000, 24.99, 13500, 14, 800000),
        ("Travel Card", 1240000, 21.49, 31000, 5, 1500000),
        ("Store Card", 86000, 29.99, 3500, 21, 150000),
        ("Balance Transfer", 350000, 0.0, 10000, 26, 500000),
    ]:
        cards[name] = post("/api/cards", {
            "name": name, "balance_cents": bal, "apr": apr, "min_payment_cents": minimum,
            "due_day": due, "credit_limit_cents": limit,
        })["id"]

    # Like a real user: every card whose due day already passed this month got
    # its minimum on time (otherwise the demo opens on a wall of "overdue").
    # The planned extra stays outstanding, so the plan shows work left to do.
    ym = month_start.strftime("%Y-%m")
    for name, minimum, due in [("Everyday Rewards Visa", 13500, 14),
                               ("Travel Card", 31000, 5),
                               ("Store Card", 3500, 21),
                               ("Balance Transfer", 10000, 26)]:
        if due < today.day:
            paid = month_start + timedelta(days=max(0, due - 3))
            post("/api/payments", {"card_id": cards[name], "amount_cents": minimum,
                                   "paid_on": paid.isoformat(), "month": ym})

    # Biweekly paycheck anchored on the most recent Friday.
    last_friday = today - timedelta(days=(today.weekday() - 4) % 7)
    post("/api/pay-schedules", {"name": "Paycheck", "frequency": "biweekly",
                                "anchor_date": last_friday.isoformat(),
                                "amount_cents": 240000,
                                "cash_account_id": checking["id"]})

    def months_ahead(n: int) -> date:
        y, m = divmod(month_start.month - 1 + n, 12)
        return date(month_start.year + y, m + 1, 15)

    car = post("/api/goals", {"name": "Car repairs", "target_cents": 120000,
                              "target_date": months_ahead(5).isoformat()})
    gifts = post("/api/goals", {"name": "Holiday gifts", "target_cents": 80000,
                                "target_date": months_ahead(2).isoformat()})
    post(f"/api/goals/{car['id']}/move", {"amount_cents": 30000, "note": "Started"})
    post(f"/api/goals/{gifts['id']}/move", {"amount_cents": 25000})

    for name, cents, cycle, days in [("Streaming", 1599, "monthly", 6),
                                     ("Music", 1199, "monthly", 11),
                                     ("Cloud storage", 299, "monthly", 17),
                                     ("Gym", 4500, "monthly", 3),
                                     ("Domain renewal", 1500, "yearly", 140)]:
        post("/api/subscriptions", {"name": name, "amount_cents": cents, "cycle": cycle,
                                    "next_date": (today + timedelta(days=days)).isoformat()})

    return {"cards": len(cards), "user": USER}


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed a fictional demo household.")
    ap.add_argument("db", help="path for the NEW demo database (must not exist)")
    args = ap.parse_args()
    p = Path(args.db)
    if p.exists():
        print(f"Refusing to seed: {p} already exists", file=sys.stderr)
        return 1
    p.parent.mkdir(parents=True, exist_ok=True)
    seed(str(p))
    print(f"Seeded demo household into {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
