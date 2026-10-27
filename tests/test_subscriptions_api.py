"""Subscription routes + plan wiring (docs/SUBSCRIPTIONS.md). Frozen clock 2026-10-07."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import clock, db
from app.main import create_app

AUTH = ("user", "test")
TODAY = date(2026, 10, 7)
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
MONTH = "2026-10"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    monkeypatch.setattr(clock, "now", lambda: NOW)
    app = create_app(str(tmp_path / "budget.db"), "user", "test")
    with TestClient(app) as c:
        yield c, app.state.db


def sub(**kw):
    return {"name": "Netflix", "amount_cents": 1599, "cycle": "monthly",
            "next_date": "2026-10-20", **kw}


def post_sub(c, **kw):
    r = c.post("/api/subscriptions", json=sub(**kw), auth=AUTH)
    assert r.status_code == 201, r.text
    return r.json()


def month(c):
    return c.get(f"/api/month/{MONTH}", auth=AUTH).json()["budget"]


def seed_income(c, cents=500000):
    c.post("/api/income", json={"name": "Pay", "amount_cents": cents}, auth=AUTH)


# --- CRUD / validation ---------------------------------------------------------

def test_crud_roundtrip_and_views(env):
    c, _ = env
    s = post_sub(c, match_text="  NETFLIX ", note=" family plan ")
    assert s["match_text"] == "netflix" and s["note"] == "family plan"
    assert s["monthly_cents"] == 1599 and s["annual_cents"] == 19188
    assert s["next_renewal"] == "2026-10-20" and s["days_until"] == 13
    r = c.put(f"/api/subscriptions/{s['id']}",
              json=sub(amount_cents=1799, cycle="yearly", next_date="2026-03-01"), auth=AUTH)
    assert r.status_code == 200
    assert r.json()["monthly_cents"] == 150           # 1799 / 12 = 149.9 -> 150
    assert r.json()["next_renewal"] == "2027-03-01"    # anchor in the past rolls forward
    assert c.delete(f"/api/subscriptions/{s['id']}", auth=AUTH).status_code == 204
    assert c.delete(f"/api/subscriptions/{s['id']}", auth=AUTH).status_code == 404
    assert c.put("/api/subscriptions/999", json=sub(), auth=AUTH).status_code == 404


@pytest.mark.parametrize("bad", [
    {"cycle": "daily"}, {"amount_cents": -1}, {"name": ""}, {"next_date": "2026-13-40"},
    {"next_date": "soon"}, {"note": "x" * 201}, {"match_text": "y" * 81},
])
def test_validation(env, bad):
    c, _ = env
    assert c.post("/api/subscriptions", json=sub(**bad), auth=AUTH).status_code == 422


def test_auth_required(env):
    c, _ = env
    assert c.get("/api/subscriptions").status_code == 401


def test_list_sorted_with_totals_and_cancelled_split(env):
    c, _ = env
    a = post_sub(c, name="Later", next_date="2026-10-25")
    b = post_sub(c, name="Sooner", amount_cents=12000, cycle="yearly", next_date="2026-10-09")
    z = post_sub(c, name="Gone", amount_cents=500, next_date="2026-10-10")
    c.post(f"/api/subscriptions/{z['id']}/cancel", auth=AUTH)
    d = c.get("/api/subscriptions", auth=AUTH).json()
    assert [s["name"] for s in d["active"]] == ["Sooner", "Later"]
    assert [s["name"] for s in d["cancelled"]] == ["Gone"]
    assert d["cancelled"][0]["cancelled_on"] == "2026-10-07"
    assert d["cancelled"][0]["next_renewal"] is None
    assert d["totals"] == {"monthly_cents": 1599 + 1000, "annual_cents": 19188 + 12000,
                           "active_count": 2, "cancelled_count": 1}
    assert a["id"] != b["id"]


def test_cancel_with_date_and_reactivate(env):
    c, _ = env
    s = post_sub(c)
    r = c.post(f"/api/subscriptions/{s['id']}/cancel", json={"cancelled_on": "2026-10-01"}, auth=AUTH)
    assert r.json()["status"] == "cancelled" and r.json()["cancelled_on"] == "2026-10-01"
    r = c.post(f"/api/subscriptions/{s['id']}/reactivate", auth=AUTH)
    assert r.json()["status"] == "active" and r.json()["cancelled_on"] is None
    assert c.post("/api/subscriptions/99/cancel", auth=AUTH).status_code == 404
    assert c.post("/api/subscriptions/99/reactivate", auth=AUTH).status_code == 404


# --- plan wiring ---------------------------------------------------------------

def test_active_subscriptions_feed_the_plan_and_cancel_removes_them(env):
    c, _ = env
    seed_income(c)
    before = month(c)
    s1 = post_sub(c)                                                    # 15.99/mo
    s2 = post_sub(c, name="Prime", amount_cents=13900, cycle="yearly")   # 139.00/yr -> 11.58/mo
    b = month(c)
    assert b["subscriptions_total_cents"] == 1599 + 1158                 # 11.583 -> 1158
    assert b["bills_total_cents"] == before["bills_total_cents"]
    assert b["available_after_min_cents"] == before["available_after_min_cents"] - 2757
    assert b["essentials_cents"] == before["essentials_cents"] + 2757
    c.post(f"/api/subscriptions/{s2['id']}/cancel", auth=AUTH)
    assert month(c)["subscriptions_total_cents"] == 1599
    c.delete(f"/api/subscriptions/{s1['id']}", auth=AUTH)
    assert month(c) == before


def test_projection_budget_sees_subscriptions(env):
    c, _ = env
    seed_income(c, 400000)
    c.post("/api/cards", json={"name": "Visa", "balance_cents": 900000, "apr": 20.0,
                               "min_payment_cents": 20000}, auth=AUTH)
    base = month(c)["recurring_debt_budget_cents"]
    post_sub(c, amount_cents=10000)
    assert month(c)["recurring_debt_budget_cents"] == base - 10000


# --- convert a bill ----------------------------------------------------------------

def test_from_expense_moves_bill_without_double_counting(env):
    c, _ = env
    seed_income(c)
    bill = c.post("/api/expenses", json={"name": "Hulu", "amount_cents": 1299, "kind": "bill",
                                          "due_day": 5, "match_text": "Hulu"}, auth=AUTH).json()
    before = month(c)
    assert before["bills_total_cents"] == 1299
    r = c.post(f"/api/subscriptions/from-expense/{bill['id']}", auth=AUTH)
    assert r.status_code == 201, r.text
    s = r.json()
    assert (s["name"], s["amount_cents"], s["cycle"], s["source"]) == ("Hulu", 1299, "monthly", "converted")
    assert s["match_text"] == "hulu" and s["next_date"] == "2026-11-05"   # the 5th already passed
    after = month(c)
    assert after["bills_total_cents"] == 0 and after["subscriptions_total_cents"] == 1299
    assert after["available_after_min_cents"] == before["available_after_min_cents"]  # same cost, counted once
    assert c.get("/api/expenses", auth=AUTH).json()[0]["active"] == 0
    # converting again: the bill is inactive now
    assert c.post(f"/api/subscriptions/from-expense/{bill['id']}", auth=AUTH).status_code == 409


def test_from_expense_rejects_spending_missing_and_undated_uses_today(env):
    c, _ = env
    sp = c.post("/api/expenses", json={"name": "Food", "amount_cents": 40000, "kind": "spending"},
                auth=AUTH).json()
    assert c.post(f"/api/subscriptions/from-expense/{sp['id']}", auth=AUTH).status_code == 409
    assert c.post("/api/subscriptions/from-expense/404", auth=AUTH).status_code == 404
    b = c.post("/api/expenses", json={"name": "Undated", "amount_cents": 500, "kind": "bill"},
               auth=AUTH).json()
    assert c.post(f"/api/subscriptions/from-expense/{b['id']}", auth=AUTH).json()["next_date"] == "2026-10-07"


# --- suggestions ---------------------------------------------------------------------

def seed_account(conn):
    item = db.insert_row(conn, "plaid_items", {
        "item_id": "i1", "access_token_enc": "x", "institution_name": "Bank", "status": "ok",
        "error_code": None, "last_synced_at": None, "created_at": "2026-09-01T00:00:00Z"})["id"]
    return db.insert_row(conn, "plaid_accounts", {
        "plaid_item_id": item, "account_id": "a1", "name": "Card", "official_name": None,
        "mask": "1", "type": "credit", "subtype": None, "card_id": None, "cash_account_id": None,
        "ignored": 0, "last_balance_cents": None, "last_payload": "{}"})["id"]


def seed_series(conn, acct, name, cents, first, step, n, tag):
    for i in range(n):
        conn.execute(
            "INSERT INTO transactions (transaction_id, plaid_account_id, date, amount_cents, name,"
            " merchant_name, pending, updated_at) VALUES (?, ?, ?, ?, ?, ?, 0, '2026-09-01T00:00:00Z')",
            (f"{tag}-{i}", acct, (first + timedelta(days=step * i)).isoformat(), cents, name, name))
    conn.commit()


def test_suggestions_confirm_dismiss_flow(env):
    c, conn = env
    acct = seed_account(conn)
    seed_series(conn, acct, "Spotify", 1099, date(2026, 6, 20), 30, 4, "sp")
    seed_series(conn, acct, "Netflix", 1599, date(2026, 6, 21), 30, 4, "nf")
    seed_series(conn, acct, "Corner Cafe", 850, date(2026, 8, 1), 3, 6, "cf")   # too frequent / not a cycle
    s = c.get("/api/subscriptions/suggestions", auth=AUTH).json()
    assert {x["key"] for x in s} == {"spotify", "netflix"}
    assert all(x["cycle"] == "monthly" for x in s)
    # dismiss one: it is gone and stays gone
    assert c.post("/api/subscriptions/suggestions/dismiss", json={"key": "Spotify "}, auth=AUTH).status_code == 204
    assert c.post("/api/subscriptions/suggestions/dismiss", json={"key": "spotify"}, auth=AUTH).status_code == 204  # idempotent
    assert [x["key"] for x in c.get("/api/subscriptions/suggestions", auth=AUTH).json()] == ["netflix"]
    # confirm the other (what the UI does: POST the suggestion's fields)
    nf = c.get("/api/subscriptions/suggestions", auth=AUTH).json()[0]
    post_sub(c, name=nf["name"], amount_cents=nf["amount_cents"], cycle=nf["cycle"],
             next_date=nf["next_date"], match_text=nf["match_text"])
    assert c.get("/api/subscriptions/suggestions", auth=AUTH).json() == []


def test_suggestion_for_existing_bill_carries_bill_id_and_ignored_accounts_are_skipped(env):
    c, conn = env
    acct = seed_account(conn)
    seed_series(conn, acct, "Netflix", 1599, date(2026, 6, 21), 30, 4, "nf")
    bill = c.post("/api/expenses", json={"name": "Streaming", "amount_cents": 1599, "kind": "bill",
                                          "match_text": "netflix"}, auth=AUTH).json()
    [s] = c.get("/api/subscriptions/suggestions", auth=AUTH).json()
    assert s["bill_id"] == bill["id"] and s["bill_name"] == "Streaming"
    conn.execute("UPDATE plaid_accounts SET ignored = 1")
    conn.commit()
    assert c.get("/api/subscriptions/suggestions", auth=AUTH).json() == []


def test_cancelled_subscription_only_resurfaces_if_charged_after_cancelling(env):
    c, conn = env
    acct = seed_account(conn)
    seed_series(conn, acct, "Netflix", 1599, date(2026, 6, 21), 30, 4, "nf")   # last charge 2026-09-19
    s = post_sub(c, match_text="netflix")
    assert c.get("/api/subscriptions/suggestions", auth=AUTH).json() == []     # tracked
    c.post(f"/api/subscriptions/{s['id']}/cancel", json={"cancelled_on": "2026-09-25"}, auth=AUTH)
    # cancelled after the last charge: quiet, not re-suggested right after cancelling
    assert c.get("/api/subscriptions/suggestions", auth=AUTH).json() == []
    # a charge lands AFTER the cancel date: the company is still billing, so say so
    seed_series(conn, acct, "Netflix", 1599, date(2026, 10, 5), 30, 1, "nf-late")
    [sug] = c.get("/api/subscriptions/suggestions", auth=AUTH).json()
    assert sug["key"] == "netflix" and sug["last_date"] == "2026-10-05"


# --- export / import ------------------------------------------------------------------

def test_export_import_roundtrip(env):
    c, _ = env
    s = post_sub(c, match_text="netflix")
    c.post(f"/api/subscriptions/{s['id']}/cancel", auth=AUTH)
    c.post("/api/subscriptions/suggestions/dismiss", json={"key": "hulu"}, auth=AUTH)
    dump = c.get("/api/export", auth=AUTH).json()
    assert len(dump["subscriptions"]) == 1 and dump["subscription_dismissals"][0]["key"] == "hulu"
    c.delete(f"/api/subscriptions/{s['id']}", auth=AUTH)
    assert c.post("/api/import", json=dump, auth=AUTH).status_code == 200
    d = c.get("/api/subscriptions", auth=AUTH).json()
    assert [x["name"] for x in d["cancelled"]] == ["Netflix"]
    again = c.get("/api/export", auth=AUTH).json()
    assert again["subscriptions"] == dump["subscriptions"]
    assert again["subscription_dismissals"] == dump["subscription_dismissals"]


def test_migration_adds_tables_to_an_existing_db(tmp_path):
    p = str(tmp_path / "old.db")
    conn = db.init_db(p)
    conn.execute("DROP TABLE subscriptions")
    conn.execute("DROP TABLE subscription_dismissals")
    conn.commit()
    conn2 = db.init_db(p)   # what a service restart does
    assert conn2.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 0
    assert conn2.execute("SELECT COUNT(*) FROM subscription_dismissals").fetchone()[0] == 0
