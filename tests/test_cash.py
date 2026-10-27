"""Phase 3 (docs/CASH.md) tests. Projection-bug tests written red-first."""
from __future__ import annotations

import json
import sqlite3
from datetime import date

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import create_app
from app.plaid import PlaidClient, PlaidConfig
from app import cash as cashx
from app import db

from test_plaid import (  # shared mocked-Plaid helpers
    add_item, item_key, liab_response, accounts_and_liab_routes,
)

AUTH = ("user", "test")
TODAY = date.today().isoformat()
MONTH = date.today().strftime("%Y-%m")


def make_config() -> PlaidConfig:
    return PlaidConfig(
        client_id="cid", client_secret="cs", env="sandbox",
        token_key=Fernet.generate_key(), redirect_uri=None,
    )


def make_client(tmp_path, routes: dict | None = None, calls: list | None = None):
    routes = routes or {}
    calls = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls.append((request.url.path, body))
        r = routes.get(request.url.path)
        if r is None and request.url.path == "/transactions/sync":
            # default mock: no transaction data (phase-4 sync step)
            r = {"added": [], "modified": [], "removed": [], "has_more": False,
                 "next_cursor": None}
        if r is None:
            return httpx.Response(404, json={
                "error_type": "INVALID_ROUTE", "error_code": "NOT_FOUND", "error_message": "nf"})
        status, data = r if isinstance(r, tuple) else (200, r)
        return httpx.Response(status, json=data)

    app = create_app(
        str(tmp_path / "budget.db"), "user", "test",
        plaid=PlaidClient(make_config(), transport=httpx.MockTransport(handler)),
    )
    return TestClient(app)


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "budget.db"), "user", "test")
    with TestClient(app) as c:
        yield c


def add_card(c, **kw):
    body = {"name": "V", "balance_cents": 100000, "apr": 0.0, "min_payment_cents": 10000}
    body.update(kw)
    return c.post("/api/cards", json=body, auth=AUTH).json()


def add_income(c, cents):
    c.post("/api/income", json={"name": "Pay", "amount_cents": cents}, auth=AUTH)


# --- projection bug (red before green) --------------------------------------

def test_projection_bonus_does_not_recur_forever(client):
    """A +$2,000 one-off adjustment in month M must change the debt-free month by
    at most what a $2,000 lump sum would, and month 2 of the schedule must equal
    the recurring budget."""
    add_income(client, 300000)
    add_card(client, balance_cents=1000000)  # 0% APR, min 10000 -> ~4 month payoff
    base = client.get("/api/month/" + MONTH, auth=AUTH).json()
    # One-off bonus of $2,000 this month.
    client.post("/api/adjustments",
                json={"month": MONTH, "name": "Bonus", "amount_cents": 200000}, auth=AUTH)
    with_bonus = client.get("/api/month/" + MONTH, auth=AUTH).json()
    rec = with_bonus["budget"]["recurring_debt_budget_cents"]
    assert rec == base["budget"]["debt_budget_cents"]  # recurring budget ignores adjustments
    proj = with_bonus["projection"]["avalanche"]
    # Month 1 uses this month's (boosted) budget; month 2 is back to recurring.
    assert proj["schedule"][0]["payments"] != proj["schedule"][1]["payments"]
    m1_total = sum(proj["schedule"][0]["payments"].values())
    m2_total = sum(proj["schedule"][1]["payments"].values())
    assert m2_total == rec
    # The debt-free month with the bonus is at least as early as base, and no
    # earlier than base with a $2,000 lump sum.
    lump = client.get("/api/projection", params={"lump": 200000}, auth=AUTH).json()
    assert base["projection"]["avalanche"]["debt_free_month"] >= proj["debt_free_month"]
    assert proj["debt_free_month"] >= lump["avalanche"]["debt_free_month"]


def test_projection_route_uses_recurring_month1_delta(client):
    """/api/projection: month 1 = recurring budget + this month's one-off delta."""
    add_income(client, 300000)
    add_card(client, balance_cents=1000000)
    client.post("/api/adjustments",
                json={"month": MONTH, "name": "Bonus", "amount_cents": 200000}, auth=AUTH)
    proj = client.get("/api/projection", auth=AUTH).json()["avalanche"]
    rec = client.get("/api/month/" + MONTH, auth=AUTH).json()["budget"]["recurring_debt_budget_cents"]
    m1 = sum(proj["schedule"][0]["payments"].values())
    m2 = sum(proj["schedule"][1]["payments"].values())
    assert m2 == rec
    assert m1 > m2  # month 1 keeps the bonus
    # explicit extra replaces the recurring extra
    p2 = client.get("/api/projection", params={"extra": 50000}, auth=AUTH).json()["avalanche"]
    m1b = sum(p2["schedule"][0]["payments"].values())
    m2b = sum(p2["schedule"][1]["payments"].values())
    assert m2b == 10000 + 50000  # minimums + replaced extra
    assert m1b == m2b + 200000   # + one-off delta


# --- cash_position pure cases -------------------------------------------------

def _acct(balance, include=True, kind="checking"):
    return {"id": 1, "name": "A", "kind": kind, "balance_cents": balance,
            "include_in_cash": 1 if include else 0, "sync": "manual",
            "last_synced_at": None, "balance_updated_at": None}


_SETTINGS = {"cash_floor_cents": 0, "cash_reserve_months": 1.0}


def test_cash_position_spare_and_months_covered():
    pos = cashx.cash_position([_acct(412300)], _SETTINGS, 333500, [])
    assert pos["cash_total_cents"] == 412300
    assert pos["essentials_cents"] == 333500
    assert pos["reserve_cents"] == 333500
    assert pos["spare_cents"] == 78800
    assert pos["months_covered"] == round(412300 / 333500, 1)  # (cash - floor - extra)/essentials


def test_cash_position_negative_balance_and_excluded_account():
    pos = cashx.cash_position([_acct(-5000), _acct(100000, include=False)], _SETTINGS, 50000, [])
    assert pos["cash_total_cents"] == -5000
    assert pos["spare_cents"] == 0
    # months_covered counts every included account only
    # S3: clamped at 0.0 - a negative cash position is 0.0 months, not -0.1.
    assert pos["months_covered"] == 0.0


def test_cash_position_essentials_zero():
    pos = cashx.cash_position([_acct(100000)], _SETTINGS, 0, [])
    assert pos["months_covered"] is None
    assert pos["spare_cents"] == 100000


def test_cash_position_reserve_months_zero_and_one_and_half():
    rows = [_acct(200000)]
    p0 = cashx.cash_position(rows, {"cash_floor_cents": 0, "cash_reserve_months": 0}, 100000, [])
    assert p0["reserve_cents"] == 0 and p0["spare_cents"] == 200000
    p15 = cashx.cash_position(rows, {"cash_floor_cents": 0, "cash_reserve_months": 1.5}, 100000, [])
    assert p15["reserve_cents"] == 150000 and p15["spare_cents"] == 50000


def test_cash_position_floor_and_extra_remaining():
    # extra unpaid in the plan reserves against spare
    plan = [{"minimum_cents": 10000, "paid_cents": 40000,
             "remaining_cents": 60000, "recommended_cents": 100000}]
    pos = cashx.cash_position([_acct(300000)], _SETTINGS, 50000, plan)
    # extra_remaining = max(0, 60000 - max(0, 10000-40000)) = 60000
    assert pos["extra_remaining_cents"] == 60000
    assert pos["reserve_cents"] == 110000
    assert pos["spare_cents"] == 190000  # floored whole dollar
    # spare is floored to a whole dollar: 300000-110000=190000 exact; try odd cents
    pos2 = cashx.cash_position([_acct(300099)], _SETTINGS, 50000, plan)
    assert pos2["spare_cents"] == 190000  # 190099 -> floored


def test_extra_remaining_ignores_unpaid_minimums():
    plan = [{"minimum_cents": 10000, "paid_cents": 0,
             "remaining_cents": 10000, "recommended_cents": 10000}]
    assert cashx.extra_remaining_cents(plan) == 0


# --- stability invariant (real sweep path) -------------------------------------

def test_stability_invariant_sweep_then_payments(tmp_path):
    """Apply a sweep X, then log card payments totalling X: spare and every
    recommended_cents stay unchanged. Then edit the cash balance down by X on a
    later date: still unchanged."""
    c = make_client(tmp_path)
    card = c.post("/api/cards", json={"name": "V", "balance_cents": 200000, "apr": 0.0,
                                      "min_payment_cents": 0}, auth=AUTH).json()
    c.post("/api/expenses", json={"name": "Rent", "amount_cents": 100000, "kind": "bill"}, auth=AUTH)
    c.post("/api/expenses", json={"name": "Food", "amount_cents": 50000, "kind": "spending"}, auth=AUTH)
    c.post("/api/income", json={"name": "Pay", "amount_cents": 300000}, auth=AUTH)
    ca = c.post("/api/cash-accounts", json={"name": "Checking", "balance_cents": 400000}, auth=AUTH).json()
    # the account's balance timestamp predates everything that follows
    conn = sqlite3.connect(tmp_path / "budget.db")
    conn.execute("UPDATE cash_accounts SET balance_updated_at = '2026-01-01T00:00:00Z' WHERE id = ?", (ca["id"],))
    conn.commit(); conn.close()

    X = 50000
    before = c.get(f"/api/month/{MONTH}", auth=AUTH).json()
    spare0 = before["cash"]["spare_cents"]
    rec0 = {p["card_id"]: p["recommended_cents"] for p in before["plan"]}
    assert spare0 == 100000  # 400000 - (150000 essentials + 150000 extra_remaining)
    assert rec0[card["id"]] == 150000

    # step 1: sweep X
    r = c.post(f"/api/month/{MONTH}/use-cash", json={"amount_cents": X}, auth=AUTH)
    assert r.status_code == 200
    after1 = r.json()
    spare1 = after1["cash"]["spare_cents"]
    rec1 = {p["card_id"]: p["recommended_cents"] for p in after1["plan"]}
    assert rec1[card["id"]] == rec0[card["id"]] + X  # recommendations grew by the sweep
    assert spare1 == spare0 - X

    # step 2: log card payments totalling X
    c.post("/api/payments", json={"card_id": card["id"], "amount_cents": X,
                                  "paid_on": f"{MONTH}-15", "month": MONTH}, auth=AUTH)
    after2 = c.get(f"/api/month/{MONTH}", auth=AUTH).json()
    rec2 = {p["card_id"]: p["recommended_cents"] for p in after2["plan"]}
    assert rec2 == rec1  # recommendations unchanged
    assert after2["cash"]["spare_cents"] == spare1  # spare unchanged

    # step 3: the cash balance is edited down by X on a later date
    conn = sqlite3.connect(tmp_path / "budget.db")
    conn.execute("UPDATE payments SET created_at = '2026-01-02T00:00:00Z'")
    conn.commit(); conn.close()
    c.put(f"/api/cash-accounts/{ca['id']}", json={"balance_cents": 350000}, auth=AUTH)
    after3 = c.get(f"/api/month/{MONTH}", auth=AUTH).json()
    rec3 = {p["card_id"]: p["recommended_cents"] for p in after3["plan"]}
    assert rec3 == rec2
    assert after3["cash"]["spare_cents"] == spare1


# --- sweep bypasses buffer and debt_share_pct -----------------------------------

def test_sweep_bypasses_buffer_and_debt_share(tmp_path):
    c = make_client(tmp_path)
    card = c.post("/api/cards", json={"name": "V", "balance_cents": 200000, "apr": 0.0,
                                      "min_payment_cents": 10000}, auth=AUTH).json()
    c.post("/api/income", json={"name": "Pay", "amount_cents": 300000}, auth=AUTH)
    c.put("/api/settings", json={"buffer_cents": 100000, "debt_share_pct": 10}, auth=AUTH)
    c.post("/api/cash-accounts", json={"name": "Checking", "balance_cents": 200000}, auth=AUTH)
    b = c.get(f"/api/month/{MONTH}", auth=AUTH).json()["budget"]
    # surplus = 300000-10000-100000 = 190000 -> 10% = 19000
    assert b["extra_cents"] == 19000
    X = 30000
    r = c.post(f"/api/month/{MONTH}/use-cash", json={"amount_cents": X}, auth=AUTH)
    b2 = r.json()["budget"]
    assert b2["extra_cents"] == 19000 + X  # bypasses buffer and share
    assert b2["debt_budget_cents"] == 10000 + 19000 + X
    assert b2["cash_applied_cents"] == X
    assert b2["recurring_debt_budget_cents"] == 10000 + 19000
    assert b2["leftover_cents"] == b["leftover_cents"]  # unchanged by sweeps
    # the sweep row exists and is deletable via the adjustments route
    rows = c.get("/api/adjustments", params={"month": MONTH}, auth=AUTH).json()
    assert [(x["kind"], x["amount_cents"]) for x in rows] == [("cash", X)]
    assert c.delete(f"/api/adjustments/{rows[0]['id']}", auth=AUTH).status_code == 204


# --- use-cash validation ---------------------------------------------------------

def test_use_cash_409_outside_current_month(tmp_path):
    c = make_client(tmp_path)
    c.post("/api/cash-accounts", json={"name": "C", "balance_cents": 100000}, auth=AUTH)
    r = c.post("/api/month/2026-01/use-cash", json={"amount_cents": 100}, auth=AUTH)
    assert r.status_code == 409


def test_use_cash_422_above_spare_and_null_means_all_spare(tmp_path):
    c = make_client(tmp_path)
    card = c.post("/api/cards", json={"name": "V", "balance_cents": 300000, "apr": 0.0,
                                      "min_payment_cents": 10000}, auth=AUTH).json()
    c.post("/api/cash-accounts", json={"name": "C", "balance_cents": 100000}, auth=AUTH)
    c.post("/api/expenses", json={"name": "Rent", "amount_cents": 50000, "kind": "bill"}, auth=AUTH)
    # essentials 60000, extra_remaining 0 (no income -> underfunded, extra 0)
    r = c.post(f"/api/month/{MONTH}/use-cash", json={"amount_cents": 50000}, auth=AUTH)
    assert r.status_code == 422  # spare = 100000 - 60000 = 40000 < 50000
    r = c.post(f"/api/month/{MONTH}/use-cash", json={"amount_cents": None}, auth=AUTH)
    assert r.status_code == 200
    assert r.json()["budget"]["cash_applied_cents"] == 40000
    assert r.json()["cash"]["spare_cents"] == 0


# --- cash account CRUD -----------------------------------------------------------

def test_cash_account_crud(tmp_path):
    c = make_client(tmp_path)
    r = c.post("/api/cash-accounts", json={"name": "Checking", "kind": "checking",
                                           "balance_cents": 100000, "include_in_cash": True}, auth=AUTH)
    assert r.status_code == 201
    a = r.json()
    assert a["institution_name"] is None and a["mask"] is None and a["sync"] == "manual"
    conn = sqlite3.connect(tmp_path / "budget.db")
    assert conn.execute("SELECT balance_updated_at FROM cash_accounts WHERE id = ?", (a["id"],)).fetchone()[0]
    conn.close()
    r = c.put(f"/api/cash-accounts/{a['id']}", json={"name": "Main", "balance_cents": 90000}, auth=AUTH)
    assert r.json()["balance_cents"] == 90000 and r.json()["name"] == "Main"
    assert len(c.get("/api/cash-accounts", auth=AUTH).json()) == 1
    assert c.delete(f"/api/cash-accounts/{a['id']}", auth=AUTH).status_code == 204
    assert c.get("/api/cash-accounts", auth=AUTH).json() == []
    # negative balances are allowed (overdrawn)
    r = c.post("/api/cash-accounts", json={"name": "OD", "balance_cents": -2500}, auth=AUTH)
    assert r.json()["balance_cents"] == -2500
    # validation: bad kind, bad name
    assert c.post("/api/cash-accounts", json={"name": "X", "kind": "brokerage"}, auth=AUTH).status_code == 422
    assert c.post("/api/cash-accounts", json={"name": "", "balance_cents": 1}, auth=AUTH).status_code == 422


def test_cash_month_bundle_null_without_accounts_and_noncurrent(tmp_path):
    c = make_client(tmp_path)
    assert c.get(f"/api/month/{MONTH}", auth=AUTH).json()["cash"] is None
    c.post("/api/cash-accounts", json={"name": "C", "balance_cents": 100000}, auth=AUTH)
    cur = c.get(f"/api/month/{MONTH}", auth=AUTH).json()["cash"]
    assert cur["is_current_month"] is True and cur["spare_cents"] == 100000  # essentials 0
    other = c.get("/api/month/2026-01", auth=AUTH).json()["cash"]
    assert other["is_current_month"] is False
    assert other["cash_total_cents"] == 100000 and other["spare_cents"] is None
    assert other["months_covered"] is None


# --- migrations / backfill ----------------------------------------------------------

def test_payments_created_at_backfill(tmp_path):
    dbp = str(tmp_path / "b.db")
    conn = db.init_db(dbp)
    conn.execute("INSERT INTO cards (id, name, balance_cents, apr, min_payment_cents, starting_balance_cents, created_at) VALUES (1, 'V', 0, 0, 0, 0, '2026-01-01')")
    conn.execute("INSERT INTO payments (card_id, month, paid_on, amount_cents) VALUES (1, '2026-01', '2026-01-05', 100)")
    conn.commit(); conn.close()
    conn = db.init_db(dbp)  # re-run migration: backfills created_at
    created = conn.execute("SELECT created_at FROM payments").fetchone()[0]
    conn.close()
    assert created == "2026-01-05T00:00:00Z"


def test_new_payment_has_created_at(tmp_path):
    c = make_client(tmp_path)
    card = c.post("/api/cards", json={"name": "V", "balance_cents": 1000, "apr": 0.0,
                                      "min_payment_cents": 100}, auth=AUTH).json()
    c.post("/api/payments", json={"card_id": card["id"], "amount_cents": 100,
                                  "paid_on": f"{MONTH}-01"}, auth=AUTH)
    p = c.get("/api/payments", auth=AUTH).json()[0]
    assert p["created_at"] and p["created_at"].endswith("Z")


# --- Plaid depository sync (mocked) ---------------------------------------------------

def _dep(account_id="dep1", name="Everyday", subtype="checking", available=1500.25, current=None, **kw):
    acct = {"account_id": account_id, "type": "depository", "subtype": subtype, "name": name,
            "official_name": None, "mask": "9999",
            "balances": {"available": available, "current": current}}
    return acct


def test_sync_depository_only_item_no_liabilities_call(tmp_path):
    calls: list = []
    routes = {
        "/accounts/get": {"accounts": [_dep()], "item": {}},
        "/liabilities/get": {"accounts": [], "liabilities": {"credit": []}},
    }
    c = make_client(tmp_path, routes, calls)
    add_item(str(tmp_path / "budget.db"), key=item_key(c))
    r = c.post("/api/plaid/sync", json={}, auth=AUTH)
    assert r.status_code == 200
    assert r.json()["items"][0]["status"] == "ok"
    paths = [p for p, _ in calls]
    assert "/accounts/get" in paths and "/liabilities/get" not in paths  # no credit -> skipped
    a = c.get("/api/plaid/accounts", auth=AUTH).json()[0]
    assert a["type"] == "depository" and a["subtype"] == "checking"
    assert a["last_balance_cents"] == 150025  # 1500.25 available, round half-up


def test_sync_mixed_item_updates_card_and_cash(tmp_path):
    routes = accounts_and_liab_routes()
    routes["/accounts/get"]["accounts"] = liab_response()["accounts"] + [_dep(), _dep(account_id="dep2", subtype="savings", available=200.0, current=250.0)]
    c = make_client(tmp_path, routes)
    add_item(str(tmp_path / "budget.db"), key=item_key(c))
    r = c.post("/api/plaid/sync", json={}, auth=AUTH)
    assert r.json()["items"][0]["accounts_updated"] == 3
    accs = c.get("/api/plaid/accounts", auth=AUTH).json()
    deps = [a for a in accs if a["type"] == "depository"]
    assert len(deps) == 2
    # link one depository via create: kind from subtype, include only for checking
    r = c.post(f"/api/plaid/accounts/{deps[0]['id']}/link", json={"create": True}, auth=AUTH)
    assert r.status_code == 200
    cas = c.get("/api/cash-accounts", auth=AUTH).json()
    assert len(cas) == 1 and cas[0]["kind"] == "checking" and cas[0]["include_in_cash"] is True
    assert cas[0]["balance_cents"] == 150025 and cas[0]["sync"] == "plaid"
    assert cas[0]["institution_name"] == "Chase" and cas[0]["mask"] == "9999"
    # link the savings one to the created account? No: a fresh create keeps them separate.
    r2 = c.post(f"/api/plaid/accounts/{deps[1]['id']}/link", json={"create": True}, auth=AUTH)
    cas = c.get("/api/cash-accounts", auth=AUTH).json()
    sv = [a for a in cas if a["kind"] == "savings"][0]
    assert sv["include_in_cash"] is False and sv["balance_cents"] == 20000


def test_sync_no_liability_accounts_item_still_ok(tmp_path):
    calls: list = []
    routes = {
        "/accounts/get": {"accounts": [liab_response()["accounts"][0]], "item": {}},
        # liabilities errors with a "no credit details" code -> item stays ok
        "/liabilities/get": (400, {"error_type": "INVALID_REQUEST", "error_code": "NO_LIABILITY_ACCOUNTS", "error_message": "none"}),
    }
    c = make_client(tmp_path, routes, calls)
    add_item(str(tmp_path / "budget.db"), key=item_key(c))
    r = c.post("/api/plaid/sync", json={}, auth=AUTH)
    item = r.json()["items"][0]
    assert item["status"] == "ok" and item["error_code"] is None
    a = c.get("/api/plaid/accounts", auth=AUTH).json()[0]
    assert a["last_balance_cents"] == 100000  # balance from /accounts/get


def test_depository_balance_uses_available_then_current(tmp_path):
    routes = {
        "/accounts/get": {"accounts": [_dep(available=None, current=123.45)], "item": {}},
    }
    c = make_client(tmp_path, routes)
    add_item(str(tmp_path / "budget.db"), key=item_key(c))
    c.post("/api/plaid/sync", json={}, auth=AUTH)
    a = c.get("/api/plaid/accounts", auth=AUTH).json()[0]
    assert a["last_balance_cents"] == 12345  # available null -> current


def test_link_pairing_422(tmp_path):
    routes = accounts_and_liab_routes()
    routes["/accounts/get"]["accounts"] = liab_response()["accounts"] + [_dep()]
    c = make_client(tmp_path, routes)
    add_item(str(tmp_path / "budget.db"), key=item_key(c))
    c.post("/api/plaid/sync", json={}, auth=AUTH)
    accs = c.get("/api/plaid/accounts", auth=AUTH).json()
    card = c.post("/api/cards", json={"name": "V", "balance_cents": 0, "apr": 0.0, "min_payment_cents": 0}, auth=AUTH).json()
    dep = [a for a in accs if a["type"] == "depository"][0]
    crd = [a for a in accs if a["type"] == "credit"][0]
    # card pairing on a depository account
    r = c.post(f"/api/plaid/accounts/{dep['id']}/link", json={"card_id": card["id"]}, auth=AUTH)
    assert r.status_code == 422
    # cash pairing on a credit account
    r = c.post(f"/api/plaid/accounts/{crd['id']}/link", json={"cash_account_id": 1}, auth=AUTH)
    assert r.status_code == 422
    # linking an existing cash account works
    ca = c.post("/api/cash-accounts", json={"name": "Main", "balance_cents": 0}, auth=AUTH).json()
    r = c.post(f"/api/plaid/accounts/{dep['id']}/link", json={"cash_account_id": ca["id"]}, auth=AUTH)
    assert r.status_code == 200 and r.json()["cash_account_id"] == ca["id"]
    # unlink: the cash account goes back to manual
    r = c.post(f"/api/plaid/accounts/{dep['id']}/unlink", json={}, auth=AUTH)
    cas = c.get("/api/cash-accounts", auth=AUTH).json()
    assert cas[0]["sync"] == "manual"
    # now balance edits are allowed again
    r = c.put(f"/api/cash-accounts/{ca['id']}", json={"balance_cents": 500}, auth=AUTH)
    assert r.status_code == 200


def test_synced_cash_account_balance_edit_409(tmp_path):
    routes = accounts_and_liab_routes()
    routes["/accounts/get"]["accounts"] = [_dep()]
    c = make_client(tmp_path, routes)
    add_item(str(tmp_path / "budget.db"), key=item_key(c))
    c.post("/api/plaid/sync", json={}, auth=AUTH)
    dep = c.get("/api/plaid/accounts", auth=AUTH).json()[0]
    r = c.post(f"/api/plaid/accounts/{dep['id']}/link", json={"create": True}, auth=AUTH)
    ca = c.get("/api/cash-accounts", auth=AUTH).json()[0]
    r = c.put(f"/api/cash-accounts/{ca['id']}", json={"balance_cents": 1}, auth=AUTH)
    assert r.status_code == 409 and "bank" in r.json()["detail"].lower()
    # non-balance edits are fine
    r = c.put(f"/api/cash-accounts/{ca['id']}", json={"name": "Renamed"}, auth=AUTH)
    assert r.status_code == 200 and r.json()["name"] == "Renamed"


# --- export / import round-trip -------------------------------------------------------

def test_export_import_roundtrip_includes_cash_accounts(tmp_path):
    c = make_client(tmp_path, accounts_and_liab_routes())
    c.post("/api/cash-accounts", json={"name": "Checking", "balance_cents": 100000}, auth=AUTH)
    dump = c.get("/api/export", auth=AUTH).json()
    assert "cash_accounts" in dump and dump["cash_accounts"][0]["name"] == "Checking"
    empty = {k: [] for k in dump}
    empty["settings"] = [{"id": 1, "strategy": "avalanche", "buffer_cents": 0,
                          "debt_share_pct": 100, "extra_override_cents": None}]
    c.post("/api/import", json=empty, auth=AUTH)
    assert c.get("/api/cash-accounts", auth=AUTH).json() == []
    c.post("/api/import", json=dump, auth=AUTH)
    cas = c.get("/api/cash-accounts", auth=AUTH).json()
    assert cas[0]["name"] == "Checking" and cas[0]["balance_cents"] == 100000


def test_cash_settings_routes(tmp_path):
    c = make_client(tmp_path)
    r = c.put("/api/settings", json={"cash_floor_cents": 25000, "cash_reserve_months": 1.5}, auth=AUTH)
    assert r.status_code == 200
    s = c.get("/api/settings", auth=AUTH).json()
    assert s["cash_floor_cents"] == 25000 and s["cash_reserve_months"] == 1.5
    c.post("/api/cash-accounts", json={"name": "C", "balance_cents": 200000}, auth=AUTH)
    pos = c.get(f"/api/month/{MONTH}", auth=AUTH).json()["cash"]
    assert pos["cash_floor_cents"] == 25000 and pos["reserve_months"] == 1.5


def test_cash_account_out_shape_in_bundle(tmp_path):
    c = make_client(tmp_path)
    c.post("/api/cash-accounts", json={"name": "Checking", "kind": "checking",
                                       "balance_cents": 412300, "include_in_cash": True}, auth=AUTH)
    acct = c.get(f"/api/month/{MONTH}", auth=AUTH).json()["cash"]["accounts"][0]
    assert set(acct) == {"id", "name", "kind", "balance_cents", "include_in_cash", "sync",
                         "last_synced_at", "institution_name", "mask"}
    assert acct["include_in_cash"] is True and acct["sync"] == "manual"


# --- orchestrator review --------------------------------------------------------

def test_paying_minimum_plus_sweep_drops_spare_by_minimum_only(tmp_path):
    """With a real minimum: sweep X, then pay the whole recommendation (min + X).
    The swept X stays neutral; the minimum reduces spare exactly like paying a bill,
    because the reserve is a static month of essentials and no income is assumed."""
    c = make_client(tmp_path)
    card = c.post("/api/cards", json={"name": "V", "balance_cents": 500000, "apr": 0.0,
                                      "min_payment_cents": 10000}, auth=AUTH).json()
    c.post("/api/expenses", json={"name": "Rent", "amount_cents": 100000, "kind": "bill"}, auth=AUTH)
    c.post("/api/income", json={"name": "Pay", "amount_cents": 200000}, auth=AUTH)
    ca = c.post("/api/cash-accounts", json={"name": "Checking", "balance_cents": 600000}, auth=AUTH).json()
    conn = sqlite3.connect(tmp_path / "budget.db")
    conn.execute("UPDATE cash_accounts SET balance_updated_at = '2026-01-01T00:00:00Z' WHERE id = ?", (ca["id"],))
    conn.commit(); conn.close()

    X = 30000
    after_sweep = c.post(f"/api/month/{MONTH}/use-cash", json={"amount_cents": X}, auth=AUTH).json()
    spare1 = after_sweep["cash"]["spare_cents"]
    rec = {p["card_id"]: p for p in after_sweep["plan"]}[card["id"]]
    assert rec["minimum_cents"] == 10000
    c.post("/api/payments", json={"card_id": card["id"], "amount_cents": rec["recommended_cents"],
                                  "paid_on": date.today().isoformat(), "month": MONTH}, auth=AUTH)
    after_pay = c.get(f"/api/month/{MONTH}", auth=AUTH).json()
    assert after_pay["cash"]["spare_cents"] == spare1 - 10000
    rec2 = {p["card_id"]: p for p in after_pay["plan"]}[card["id"]]
    assert rec2["recommended_cents"] == rec["recommended_cents"]


def test_months_covered_uses_effective_cash(tmp_path):
    """A logged-but-not-yet-reflected payment lowers months_covered too, not just spare."""
    c = make_client(tmp_path)
    card = c.post("/api/cards", json={"name": "V", "balance_cents": 500000, "apr": 0.0,
                                      "min_payment_cents": 0}, auth=AUTH).json()
    c.post("/api/expenses", json={"name": "Rent", "amount_cents": 100000, "kind": "bill"}, auth=AUTH)
    ca = c.post("/api/cash-accounts", json={"name": "Checking", "balance_cents": 300000}, auth=AUTH).json()
    conn = sqlite3.connect(tmp_path / "budget.db")
    conn.execute("UPDATE cash_accounts SET balance_updated_at = '2026-01-01T00:00:00Z' WHERE id = ?", (ca["id"],))
    conn.commit(); conn.close()
    before = c.get(f"/api/month/{MONTH}", auth=AUTH).json()["cash"]["months_covered"]
    c.post("/api/payments", json={"card_id": card["id"], "amount_cents": 100000,
                                  "paid_on": date.today().isoformat(), "month": MONTH}, auth=AUTH)
    after = c.get(f"/api/month/{MONTH}", auth=AUTH).json()["cash"]["months_covered"]
    assert before == 3.0
    assert after == 2.0
