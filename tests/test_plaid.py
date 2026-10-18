"""Plaid backend tests. All Plaid HTTP is mocked with httpx.MockTransport;
no network calls are ever made."""
from __future__ import annotations

import json
import sqlite3
from datetime import date

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.main import create_app
from app.plaid import PlaidClient, PlaidConfig, encrypt_token, map_liability

AUTH = ("user", "test")
TODAY = date.today().isoformat()
MONTH = date.today().strftime("%Y-%m")


def make_config() -> PlaidConfig:
    # throwaway Fernet key generated inside the test process
    return PlaidConfig(
        client_id="cid", client_secret="cs", env="sandbox",
        token_key=Fernet.generate_key(), redirect_uri=None,
    )


def make_client(tmp_path, routes: dict, calls: list | None = None, config: PlaidConfig | None = None):
    calls = calls if calls is not None else []
    config = config or make_config()
    app = create_app(str(tmp_path / "budget.db"), "user", "test",
                     plaid=PlaidClient(config, transport=httpx.MockTransport(_handler(routes, calls))))
    return TestClient(app)


def _handler(routes: dict, calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls.append((request.url.path, body))
        r = routes.get(request.url.path)
        if r is None and request.url.path == "/transactions/sync":
            # default mock: no transaction data (phase-4 sync step)
            r = {"added": [], "modified": [], "removed": [], "has_more": False,
                 "next_cursor": None}
        if r is None and request.url.path == "/transactions/sync":
            # default mock: no transaction data (phase-4 sync step)
            r = {"added": [], "modified": [], "removed": [], "has_more": False,
                 "next_cursor": None}
        if r is None:
            return httpx.Response(404, json={"error_type": "INVALID_ROUTE", "error_code": "NOT_FOUND", "error_message": "nf"})
        status, data = r if isinstance(r, tuple) else (200, r)
        return httpx.Response(status, json=data)
    return handler


def item_key(c):
    """The Fernet key of the PlaidClient backing the TestClient."""
    return c.app.state.plaid.config.token_key


LINK_ROUTES = {
    "/link/token/create": {"link_token": "link-sandbox-tok", "expiration": "2026-10-02T20:00:00Z"},
    "/item/public_token/exchange": {"access_token": "access-sandbox-abc123", "item_id": "item-1"},
}


def accounts_and_liab_routes(**kw) -> dict:
    """Phase-3 sync calls /accounts/get first, then /liabilities/get only when a
    credit account is present. Build both mock routes from one response."""
    liab = liab_response(**kw)
    return {
        "/accounts/get": {"accounts": liab["accounts"], "item": {}},
        "/liabilities/get": liab,
    }


def liab_response(current=1000.00, limit=5000.0, aprs=None, min_pay=25.0,
                  due="2026-10-15", account_id="acc1", extra_accounts=()):
    account = {
        "account_id": account_id, "type": "credit", "name": "Freedom",
        "official_name": "Chase Freedom Unlimited", "mask": "4321",
        "balances": {"current": current, "limit": limit},
    }
    credit = {
        "account_id": account_id,
        "aprs": aprs if aprs is not None else [{"apr_percentage": 24.99, "balance_subject_to_apr": 900.0}],
        "minimum_payment_amount": min_pay,
        "next_payment_due_date": due,
    }
    return {
        "accounts": [account, *extra_accounts],
        "item": {},
        "liabilities": {"credit": [credit]},
    }


def add_item(db_path, item_id="item-1", token="access-sandbox-abc123", status="ok", institution="Chase", key=None):
    key = key or Fernet.generate_key()
    from app.db import init_db
    conn = init_db(db_path)
    conn.execute(
        "INSERT INTO plaid_items (item_id, access_token_enc, institution_name, status, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (item_id, encrypt_token(key, token), institution, status, TODAY),
    )
    conn.commit()
    conn.close()


def link_first_account(client, card_id):
    accounts = client.get("/api/plaid/accounts", auth=AUTH).json()
    r = client.post(f"/api/plaid/accounts/{accounts[0]['id']}/link", json={"card_id": card_id}, auth=AUTH)
    assert r.status_code == 200
    return accounts[0]["id"]


# --- map_liability (pure) ----------------------------------------------------

def test_map_liability_weighted_apr():
    credit = {"aprs": [
        {"apr_percentage": 29.99, "balance_subject_to_apr": 100.0},
        {"apr_percentage": 20.00, "balance_subject_to_apr": 300.0},
    ]}
    out = map_liability({"balances": {"current": 400.0}}, credit, {"apr": 9.9})
    assert out["apr"] == 22.5  # (29.99*100 + 20*300)/400 = 22.4975 -> 22.5
    assert out["apr_source"] == "weighted"


def test_map_liability_purchase_fallback():
    credit = {"aprs": [{"name": "balance_transfer", "apr_percentage": 3.0, "balance_subject_to_apr": 0},
                       {"name": "purchase", "apr_percentage": 21.74, "balance_subject_to_apr": 0}]}
    out = map_liability({"balances": {}}, credit, None)
    assert out["apr"] == 21.74
    assert out["apr_source"] == "purchase"


def test_map_liability_empty_aprs_keeps_existing():
    out = map_liability({"balances": {"current": 10.0}}, {"aprs": []}, {"apr": 12.5})
    assert "apr" not in out
    assert out["apr_source"] == "manual"


def test_map_liability_null_min_keeps_existing():
    credit = {"minimum_payment_amount": None}
    out = map_liability({"balances": {}}, credit, {"min_payment_cents": 1234})
    assert "min_payment_cents" not in out


def test_map_liability_negative_balance_clamps():
    out = map_liability({"balances": {"current": -25.5}}, None, None)
    assert out["balance_cents"] == 0  # -2550 clamped


def test_map_liability_credit_none_only_balance_and_limit():
    out = map_liability({"balances": {"current": 12.345, "limit": 100.005}}, None, None)
    assert out["balance_cents"] == 1235  # round half-up
    assert out["credit_limit_cents"] == 10001  # 100.005 * 100 = 10000.5 -> half-up 10001
    assert "apr" not in out and "min_payment_cents" not in out and "due_day" not in out


def test_map_liability_due_day_and_min():
    credit = {"minimum_payment_amount": 35.0, "next_payment_due_date": "2026-11-07"}
    out = map_liability({"balances": {}}, credit, {"due_day": 15})
    assert out["min_payment_cents"] == 3500
    assert out["due_day"] == 7


# --- unconfigured app --------------------------------------------------------

def test_unconfigured_503(tmp_path):
    app = create_app(str(tmp_path / "b.db"), "user", "test")
    c = TestClient(app)
    assert c.get("/api/plaid/status", auth=AUTH).json() == {"configured": False, "env": None, "items": []}
    for method, path in [("post", "/api/plaid/link-token"), ("post", "/api/plaid/sync")]:
        r = getattr(c, method)(path, auth=AUTH, json={})
        assert r.status_code == 503, path
        assert r.json()["detail"] == "Plaid is not configured"
    r = c.get("/api/plaid/accounts", auth=AUTH)
    assert r.status_code == 503 and r.json()["detail"] == "Plaid is not configured"
    r = c.post("/api/plaid/exchange", auth=AUTH, json={"public_token": "x"})
    assert r.status_code == 503 and r.json()["detail"] == "Plaid is not configured"


def test_csp_allows_plaid_cdn(tmp_path):
    c = make_client(tmp_path, {})
    r = c.get("/api/health")
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self' https://cdn.plaid.com" in csp
    assert "frame-src https://cdn.plaid.com" in csp
    assert "connect-src 'self' https://production.plaid.com https://sandbox.plaid.com" in csp
    assert "frame-ancestors 'none'" in csp


# --- exchange + token encryption --------------------------------------------

def test_exchange_stores_encrypted_token_and_syncs(tmp_path):
    calls: list = []
    config = make_config()
    routes = dict(LINK_ROUTES)
    routes["/liabilities/get"] = liab_response()
    routes["/accounts/get"] = {"accounts": liab_response()["accounts"], "item": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content or b"{}")))
        r = routes.get(request.url.path)
        if r is None and request.url.path == "/transactions/sync":
            # default mock: no transaction data (phase-4 sync step)
            r = {"added": [], "modified": [], "removed": [], "has_more": False,
                 "next_cursor": None}
        return httpx.Response(200, json=r)

    app = create_app(str(tmp_path / "budget.db"), "user", "test",
                     plaid=PlaidClient(config, transport=httpx.MockTransport(handler)))
    c = TestClient(app)
    r = c.post("/api/plaid/exchange", json={"public_token": "pub-1", "institution_name": "Chase"}, auth=AUTH)
    assert r.status_code == 200
    item = r.json()
    assert set(item) == {"id", "institution_name", "status", "error_code", "last_synced_at", "accounts"}
    assert item["institution_name"] == "Chase" and item["status"] == "ok" and item["accounts"] == 1

    # the plaintext token appears nowhere: DB file bytes, API responses, export
    db_bytes = (tmp_path / "budget.db").read_bytes()
    for name in ("-wal", "-shm"):
        p = tmp_path / f"budget.db{name}"
        if p.exists():
            db_bytes += p.read_bytes()
    assert b"access-sandbox-abc123" not in db_bytes
    assert b"access-sandbox-abc123" not in json.dumps(c.get("/api/plaid/status", auth=AUTH).json()).encode()
    assert b"access-sandbox-abc123" not in json.dumps(c.get("/api/plaid/accounts", auth=AUTH).json()).encode()
    export_text = c.get("/api/export", auth=AUTH).text
    assert "access-sandbox-abc123" not in export_text
    # the stored ciphertext decrypts back to the token with the app's key
    conn = sqlite3.connect(tmp_path / "budget.db")
    enc = conn.execute("SELECT access_token_enc FROM plaid_items").fetchone()[0]
    conn.close()
    assert Fernet(config.token_key).decrypt(enc.encode()).decode() == "access-sandbox-abc123"


def test_exchange_twice_updates_token_not_insert(tmp_path):
    calls: list = []
    routes = dict(LINK_ROUTES)
    routes["/liabilities/get"] = liab_response()
    routes["/accounts/get"] = {"accounts": liab_response()["accounts"], "item": {}}
    c = make_client(tmp_path, routes, calls)
    c.post("/api/plaid/exchange", json={"public_token": "pub-1"}, auth=AUTH)
    # second exchange returns the same item_id -> updates the existing row
    r = c.post("/api/plaid/exchange", json={"public_token": "pub-1", "institution_name": "Chase"}, auth=AUTH)
    items = c.get("/api/plaid/status", auth=AUTH).json()["items"]
    assert len(items) == 1 and items[0]["id"] == r.json()["id"]


def test_link_token_new_and_update_mode(tmp_path):
    calls: list = []
    config = make_config()
    routes = dict(LINK_ROUTES)
    routes["/liabilities/get"] = liab_response()
    routes["/accounts/get"] = {"accounts": liab_response()["accounts"], "item": {}}
    c = make_client(tmp_path, routes, calls, config=config)
    r = c.post("/api/plaid/link-token", json={}, auth=AUTH)
    assert r.status_code == 200
    assert r.json() == {"link_token": "link-sandbox-tok", "expiration": "2026-10-02T20:00:00Z"}
    path, body = calls[0]
    assert path == "/link/token/create"
    assert body["client_name"] == "Paydown" and body["language"] == "en"
    assert body["country_codes"] == ["US"] and body["user"] == {"client_user_id": "paydown-user"}
    assert body["products"] == ["transactions"]
    assert body["required_if_supported_products"] == ["liabilities"]
    assert body["account_filters"] == {
        "credit": {"account_subtypes": ["credit card"]},
        "depository": {"account_subtypes": ["checking", "savings"]},
    }
    assert "access_token" not in body and "redirect_uri" not in body

    # update mode: sends the item's decrypted access token, no products/filters
    add_item(str(tmp_path / "budget.db"), key=config.token_key)
    r2 = c.post("/api/plaid/link-token", json={"item_id": 1}, auth=AUTH)
    assert r2.status_code == 200
    path2, body2 = calls[1]
    assert path2 == "/link/token/create"
    assert "products" not in body2 and "account_filters" not in body2
    assert body2["access_token"] == "access-sandbox-abc123"


# --- sync ---------------------------------------------------------------------

def test_sync_updates_linked_card_and_balance_log_only_on_change(tmp_path):
    routes = accounts_and_liab_routes(current=4200.12)
    c = make_client(tmp_path, routes)
    card = c.post("/api/cards", json={"name": "V", "balance_cents": 100000, "apr": 5.0, "min_payment_cents": 5000}, auth=AUTH).json()
    add_item(str(tmp_path / "budget.db"), key=item_key(c))
    c.post("/api/plaid/sync", json={}, auth=AUTH)  # accounts appear after a first sync
    link_first_account(c, card["id"])

    r = c.post("/api/plaid/sync", json={}, auth=AUTH)
    assert r.status_code == 200
    out = r.json()
    assert out["items"][0]["status"] == "ok" and out["items"][0]["accounts_updated"] == 1
    assert "synced_at" in out
    cards = c.get("/api/cards", auth=AUTH).json()
    assert cards[0]["balance_cents"] == 420012  # 4200.12 -> round half-up
    assert cards[0]["sync"] == "plaid"
    assert cards[0]["institution_name"] == "Chase" and cards[0]["mask"] == "4321"
    assert cards[0]["apr"] == 24.99
    assert cards[0]["min_payment_cents"] == 2500
    assert cards[0]["credit_limit_cents"] == 500000
    assert cards[0]["due_day"] == 15
    assert cards[0]["last_synced_at"] is not None
    logs = c.get(f"/api/cards/{card['id']}/history", auth=AUTH).json()
    assert [l for l in logs if l["source"] == "plaid"][-1]["balance_cents"] == 420012

    # sync again with unchanged balance -> no new plaid balance_log row
    c.post("/api/plaid/sync", json={}, auth=AUTH)
    logs2 = c.get(f"/api/cards/{card['id']}/history", auth=AUTH).json()
    assert len([l for l in logs2 if l["source"] == "plaid"]) == 1

    # balance changed -> new row
    routes["/liabilities/get"] = liab_response(current=500.00)
    routes["/accounts/get"] = {"accounts": liab_response(current=500.00)["accounts"], "item": {}}
    c.post("/api/plaid/sync", json={}, auth=AUTH)
    logs3 = c.get(f"/api/cards/{card['id']}/history", auth=AUTH).json()
    assert len([l for l in logs3 if l["source"] == "plaid"]) == 2
    assert c.get("/api/cards", auth=AUTH).json()[0]["balance_cents"] == 50000


def test_item_login_required_does_not_stop_other_items(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("access_token") == "access-a":
            return httpx.Response(400, json={"error_type": "ITEM_ERROR", "error_code": "ITEM_LOGIN_REQUIRED", "error_message": "re-login"})
        return httpx.Response(200, json=liab_response(current=100.00))

    client = PlaidClient(make_config(), transport=httpx.MockTransport(handler))
    app = create_app(str(tmp_path / "b.db"), "user", "test", plaid=client)
    c = TestClient(app)
    cfg = make_config()
    client = PlaidClient(cfg, transport=httpx.MockTransport(handler))
    app = create_app(str(tmp_path / "b.db"), "user", "test", plaid=client)
    c = TestClient(app)
    add_item(str(tmp_path / "b.db"), item_id="item-a", token="access-a", institution="BankA", key=cfg.token_key)
    add_item(str(tmp_path / "b.db"), item_id="item-b", token="access-b", institution="BankB", key=cfg.token_key)
    card = c.post("/api/cards", json={"name": "B", "balance_cents": 0, "apr": 0.0, "min_payment_cents": 0}, auth=AUTH).json()
    c.post("/api/plaid/sync", json={}, auth=AUTH)  # BankA fails (login required), BankB syncs
    accounts = c.get("/api/plaid/accounts", auth=AUTH).json()
    # not synced yet; link the BankB account to the card, then sync
    b = [a for a in accounts if a["institution_name"] == "BankB"][0]["id"]
    c.post(f"/api/plaid/accounts/{b}/link", json={"card_id": card["id"]}, auth=AUTH)

    r = c.post("/api/plaid/sync", json={}, auth=AUTH)
    items = {i["institution_name"]: i for i in r.json()["items"]}
    assert items["BankA"]["status"] == "login_required"
    assert items["BankA"]["error_code"] == "ITEM_LOGIN_REQUIRED"
    assert items["BankB"]["status"] == "ok"
    status = {i["institution_name"]: i for i in c.get("/api/plaid/status", auth=AUTH).json()["items"]}
    assert status["BankA"]["status"] == "login_required"
    # BankB's card got the synced balance
    assert c.get("/api/cards", auth=AUTH).json()[0]["balance_cents"] == 10000


def test_other_plaid_error_marks_item_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error_type": "INVALID_REQUEST", "error_code": "INVALID_ACCESS_TOKEN", "error_message": "bad token"})
    cfg = make_config()
    client = PlaidClient(cfg, transport=httpx.MockTransport(handler))
    app = create_app(str(tmp_path / "b.db"), "user", "test", plaid=client)
    c = TestClient(app)
    add_item(str(tmp_path / "b.db"), key=cfg.token_key)
    r = c.post("/api/plaid/sync", json={}, auth=AUTH)
    assert r.json()["items"][0]["status"] == "error"
    assert r.json()["items"][0]["error_code"] == "INVALID_ACCESS_TOKEN"


# --- payments on synced cards (red-before-green) -------------------------------

def _synced_card(tmp_path):
    c = make_client(tmp_path, {})
    card = c.post("/api/cards", json={"name": "V", "balance_cents": 100000, "apr": 0.0, "min_payment_cents": 5000}, auth=AUTH).json()
    conn = sqlite3.connect(tmp_path / "budget.db")
    conn.execute("UPDATE cards SET sync = 'plaid' WHERE id = ?", (card["id"],))
    conn.commit()
    conn.close()
    return c, card["id"]


def test_payment_on_synced_card_does_not_change_balance(tmp_path):
    c, card_id = _synced_card(tmp_path)
    r = c.post("/api/payments", json={"card_id": card_id, "amount_cents": 40000, "paid_on": f"{MONTH}-15", "month": MONTH}, auth=AUTH)
    assert r.status_code == 201
    assert c.get("/api/cards", auth=AUTH).json()[0]["balance_cents"] == 100000
    logs = c.get(f"/api/cards/{card_id}/history", auth=AUTH).json()
    assert [l for l in logs if l["source"] in ("payment", "payment_delete")] == []
    # deleting the payment also must not touch the balance
    pid = r.json()["id"]
    assert c.delete(f"/api/payments/{pid}", auth=AUTH).status_code == 204
    assert c.get("/api/cards", auth=AUTH).json()[0]["balance_cents"] == 100000


def test_manual_card_payment_still_adjusts_balance(tmp_path):
    c, card_id = _synced_card(tmp_path)
    conn = sqlite3.connect(tmp_path / "budget.db")
    conn.execute("UPDATE cards SET sync = 'manual' WHERE id = ?", (card_id,))
    conn.commit()
    conn.close()
    r = c.post("/api/payments", json={"card_id": card_id, "amount_cents": 40000, "paid_on": f"{MONTH}-15", "month": MONTH}, auth=AUTH)
    assert c.get("/api/cards", auth=AUTH).json()[0]["balance_cents"] == 60000
    assert c.delete(f"/api/payments/{r.json()['id']}", auth=AUTH).status_code == 204
    assert c.get("/api/cards", auth=AUTH).json()[0]["balance_cents"] == 100000


# --- plan_balance snapshot (red-before-green) -----------------------------------

def test_plan_balance_frozen_snapshot_for_synced_card(tmp_path):
    """First sync of the month 100000; then a 40000 payment is logged and a later
    sync sees 90000 (bank balance moved up due to new spending). The month plan
    must still use 100000, show paid 40000, and remaining against the original
    recommendation."""
    c = make_client(tmp_path, {})
    card = c.post("/api/cards", json={"name": "V", "balance_cents": 100000, "apr": 0.0, "min_payment_cents": 10000}, auth=AUTH).json()
    c.post("/api/income", json={"name": "Pay", "amount_cents": 300000}, auth=AUTH)
    conn = sqlite3.connect(tmp_path / "budget.db")
    conn.execute("UPDATE cards SET sync = 'plaid' WHERE id = ?", (card["id"],))
    # first sync of the month: plaid balance_log row 100000 (card balance already 100000)
    conn.execute("INSERT INTO balance_log (card_id, logged_on, balance_cents, source) VALUES (?, ?, ?, 'plaid')", (card["id"], TODAY, 100000))
    conn.commit()
    conn.close()

    # a payment is logged (synced card: row only, no balance change) ...
    c.post("/api/payments", json={"card_id": card["id"], "amount_cents": 40000, "paid_on": f"{MONTH}-15", "month": MONTH}, auth=AUTH)
    # ... and a later sync sees 90000 because of new charges
    conn = sqlite3.connect(tmp_path / "budget.db")
    conn.execute("UPDATE cards SET balance_cents = 90000 WHERE id = ?", (card["id"],))
    conn.execute("INSERT INTO balance_log (card_id, logged_on, balance_cents, source) VALUES (?, ?, ?, 'plaid')", (card["id"], TODAY, 90000))
    conn.commit()
    conn.close()

    m = c.get(f"/api/month/{MONTH}", auth=AUTH).json()
    row = {p["card_id"]: p for p in m["plan"]}[card["id"]]
    assert row["recommended_cents"] == 100000, f"expected snapshot 100000, got {row['recommended_cents']}"
    assert row["paid_cents"] == 40000
    assert row["remaining_cents"] == 60000
    # the waterfall uses the snapshot too
    assert m["budget"]["minimums_total_cents"] == 10000


def test_plan_balance_manual_card_keeps_b1_rule(tmp_path):
    c = make_client(tmp_path, {})
    card = c.post("/api/cards", json={"name": "V", "balance_cents": 100000, "apr": 0.0, "min_payment_cents": 10000}, auth=AUTH).json()
    c.post("/api/income", json={"name": "Pay", "amount_cents": 300000}, auth=AUTH)
    c.post("/api/payments", json={"card_id": card["id"], "amount_cents": 40000, "paid_on": f"{MONTH}-15", "month": MONTH}, auth=AUTH)
    m = c.get(f"/api/month/{MONTH}", auth=AUTH).json()
    row = {p["card_id"]: p for p in m["plan"]}[card["id"]]
    assert row["recommended_cents"] == 100000  # B1: balance + payments >= M
    assert row["remaining_cents"] == 60000


def test_plan_balance_synced_card_no_snapshot_uses_live(tmp_path):
    c, card_id = _synced_card(tmp_path)
    c.post("/api/income", json={"name": "Pay", "amount_cents": 300000}, auth=AUTH)
    m = c.get(f"/api/month/{MONTH}", auth=AUTH).json()
    row = {p["card_id"]: p for p in m["plan"]}[card_id]
    assert row["recommended_cents"] == 100000  # no balance_log in M -> live balance


# --- link / unlink / ignore ------------------------------------------------------

def test_link_unlink_ignore_flow(tmp_path):
    routes = accounts_and_liab_routes(current=4200.12)
    c = make_client(tmp_path, routes)
    card = c.post("/api/cards", json={"name": "My Visa", "balance_cents": 100000, "apr": 5.0, "min_payment_cents": 5000}, auth=AUTH).json()
    add_item(str(tmp_path / "budget.db"), key=item_key(c))
    c.post("/api/plaid/sync", json={}, auth=AUTH)
    accounts = c.get("/api/plaid/accounts", auth=AUTH).json()
    a = accounts[0]
    assert a["card_id"] is None and a["card_name"] is None and a["ignored"] is False
    assert a["institution_name"] == "Chase" and a["mask"] == "4321"
    assert a["last_balance_cents"] == 420012

    r = c.post(f"/api/plaid/accounts/{a['id']}/link", json={"card_id": card["id"]}, auth=AUTH)
    assert r.status_code == 200
    linked = r.json()
    assert linked["card_id"] == card["id"] and linked["card_name"] == "My Visa"
    assert linked["apr_source"] == "weighted"
    card_now = c.get("/api/cards", auth=AUTH).json()[0]
    assert card_now["sync"] == "plaid" and card_now["balance_cents"] == 420012

    r = c.post(f"/api/plaid/accounts/{a['id']}/unlink", json={}, auth=AUTH)
    assert r.json()["card_id"] is None and r.json()["card_name"] is None
    card_now = c.get("/api/cards", auth=AUTH).json()[0]
    assert card_now["sync"] == "manual"
    assert card_now["balance_cents"] == 420012  # keeps last values

    r = c.post(f"/api/plaid/accounts/{a['id']}/ignore", json={"ignored": True}, auth=AUTH)
    assert r.json()["ignored"] is True
    r = c.post(f"/api/plaid/accounts/{a['id']}/ignore", json={"ignored": False}, auth=AUTH)
    assert r.json()["ignored"] is False


def test_link_create_new_card(tmp_path):
    routes = accounts_and_liab_routes(current=4200.12)
    c = make_client(tmp_path, routes)
    add_item(str(tmp_path / "budget.db"), key=item_key(c))
    c.post("/api/plaid/sync", json={}, auth=AUTH)
    a = c.get("/api/plaid/accounts", auth=AUTH).json()[0]
    r = c.post(f"/api/plaid/accounts/{a['id']}/link", json={"create": True}, auth=AUTH)
    assert r.status_code == 200
    cards = c.get("/api/cards", auth=AUTH).json()
    assert len(cards) == 1
    card = cards[0]
    assert card["name"] == "Chase Freedom Unlimited"  # official_name first
    assert card["balance_cents"] == 420012 and card["apr"] == 24.99
    assert card["min_payment_cents"] == 2500 and card["credit_limit_cents"] == 500000
    assert card["sync"] == "plaid"


def test_link_create_falls_back_to_zeroes_when_plaid_lacks_data(tmp_path):
    routes = {"/accounts/get": {"accounts": [{"account_id": "acc1", "type": "credit", "name": "Card", "official_name": None, "mask": "1111", "balances": {"current": None, "limit": None}}], "item": {}}, "/liabilities/get": {"accounts": [], "liabilities": {"credit": []}}}
    c = make_client(tmp_path, routes)
    add_item(str(tmp_path / "budget.db"), key=item_key(c))
    c.post("/api/plaid/sync", json={}, auth=AUTH)
    a = c.get("/api/plaid/accounts", auth=AUTH).json()[0]
    r = c.post(f"/api/plaid/accounts/{a['id']}/link", json={"create": True}, auth=AUTH)
    assert r.status_code == 200
    card = c.get("/api/cards", auth=AUTH).json()[0]
    assert card["name"] == "Card"
    assert card["balance_cents"] == 0 and card["apr"] == 0.0 and card["min_payment_cents"] == 0


# --- export / import fixups --------------------------------------------------------

def test_export_excludes_plaid_and_import_repairs_links(tmp_path):
    routes = accounts_and_liab_routes()
    c = make_client(tmp_path, routes)
    card = c.post("/api/cards", json={"name": "V", "balance_cents": 100000, "apr": 5.0, "min_payment_cents": 5000}, auth=AUTH).json()
    add_item(str(tmp_path / "budget.db"), key=item_key(c))
    c.post("/api/plaid/sync", json={}, auth=AUTH)
    link_first_account(c, card["id"])

    dump = c.get("/api/export", auth=AUTH).json()
    assert "plaid_items" not in dump and "plaid_accounts" not in dump

    # import the same dump: plaid tables survive untouched
    c.post("/api/import", json=dump, auth=AUTH)
    # now delete the card out from under the plaid account, re-import, and check repair
    c.delete(f"/api/cards/{card['id']}", auth=AUTH)
    dump2 = c.get("/api/export", auth=AUTH).json()
    dump2["cards"] = []  # card gone after import
    c.post("/api/import", json=dump2, auth=AUTH)
    conn = sqlite3.connect(tmp_path / "budget.db")
    assert conn.execute("SELECT COUNT(*) FROM plaid_accounts WHERE card_id IS NOT NULL").fetchone()[0] == 0
    conn.close()


# --- sync_cli ---------------------------------------------------------------------

def test_sync_cli_not_configured_exits_1(monkeypatch):
    import sync_cli
    for var in ("PLAID_CLIENT_ID", "PLAID_CLIENT_SECRET", "PLAID_TOKEN_KEY", "PLAID_ENV"):
        monkeypatch.delenv(var, raising=False)
    assert sync_cli.main() == 1


def test_sync_cli_prints_summary_and_exits_0(tmp_path, monkeypatch, capsys):
    import sync_cli
    monkeypatch.setenv("BUDGET_DB", str(tmp_path / "b.db"))
    routes = accounts_and_liab_routes()
    monkeypatch.setattr(
        sync_cli, "PlaidClient",
        lambda config: PlaidClient(config, transport=httpx.MockTransport(_handler(routes, []))),
    )
    from cryptography.fernet import Fernet as F
    env_key = F.generate_key()
    monkeypatch.setenv("PLAID_CLIENT_ID", "cid")
    monkeypatch.setenv("PLAID_CLIENT_SECRET", "cs")
    monkeypatch.setenv("PLAID_TOKEN_KEY", env_key.decode())
    add_item(str(tmp_path / "b.db"), key=env_key)
    assert sync_cli.main() == 0
    out = capsys.readouterr().out
    assert "Chase: ok accounts_updated=1" in out
    assert "access-sandbox" not in out


# --- orchestrator review regressions ---------------------------------------

def test_map_liability_purchase_uses_real_plaid_apr_type():
    """Plaid's real shape (docs /liabilities/get): {"apr_type": "purchase_apr", ...}."""
    credit = {"aprs": [
        {"apr_type": "cash_apr", "apr_percentage": 27.95, "balance_subject_to_apr": 0, "interest_charge_amount": 0},
        {"apr_type": "purchase_apr", "apr_percentage": 21.74, "balance_subject_to_apr": 0, "interest_charge_amount": 0},
    ]}
    out = map_liability({"balances": {"current": 0}}, credit, {"apr": 9.9})
    assert out["apr"] == 21.74
    assert out["apr_source"] == "purchase"


def test_map_liability_plaid_docs_example_is_weighted():
    """The literal credit object from Plaid's docs: four buckets, three carrying balance."""
    credit = {"aprs": [
        {"apr_percentage": 15.24, "apr_type": "balance_transfer_apr", "balance_subject_to_apr": 1562.32, "interest_charge_amount": 130.22},
        {"apr_percentage": 27.95, "apr_type": "cash_apr", "balance_subject_to_apr": 56.22, "interest_charge_amount": 14.81},
        {"apr_percentage": 12.5, "apr_type": "purchase_apr", "balance_subject_to_apr": 157.01, "interest_charge_amount": 25.66},
        {"apr_percentage": 0, "apr_type": "special", "balance_subject_to_apr": 1000, "interest_charge_amount": 0},
    ], "minimum_payment_amount": 20, "next_payment_due_date": "2020-05-28"}
    out = map_liability({"balances": {"current": 410, "limit": 2000}}, credit, None)
    # (23809.76 + 1571.35 + 1962.63 + 0) / 2775.55 = 9.8516 -> 9.85
    assert out["apr"] == 9.85
    assert out["min_payment_cents"] == 2000
    assert out["due_day"] == 28
    assert out["balance_cents"] == 41000
    assert out["credit_limit_cents"] == 200000


def test_sync_survives_network_error_on_one_item(tmp_path):
    """A transport failure (timeout/DNS) on one bank must not abort the others."""
    from app import db as dbm
    from app.plaid import sync_all
    key = Fernet.generate_key().decode()
    cfg = PlaidConfig(client_id="c", client_secret="s", env="sandbox", token_key=key.encode(), redirect_uri=None)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["access_token"] == "access-bad":
            raise httpx.ConnectTimeout("boom", request=request)
        return httpx.Response(200, json={"accounts": [], "liabilities": {"credit": []}, "item": {"item_id": "good"}})

    client = PlaidClient(cfg, transport=httpx.MockTransport(handler))
    conn = dbm.init_db(str(tmp_path / "t.db"))
    for item_id, tok in (("bad", "access-bad"), ("good", "access-good")):
        dbm.insert_row(conn, "plaid_items", {"item_id": item_id, "access_token_enc": encrypt_token(key.encode(), tok),
                                             "institution_name": item_id, "created_at": "2026-10-02"})
    result = sync_all(conn, client)
    by = {i["institution_name"]: i for i in result["items"]}
    assert by["bad"]["status"] == "error"
    assert by["bad"]["error_code"] == "NETWORK_ERROR"
    assert by["good"]["status"] == "ok"
