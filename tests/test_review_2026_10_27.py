"""Regression tests for the 2026-10-27 pre-open-source review (Gemini 3.8).

Triage: docs/reviews/2026-10-27-gemini-triage.md
"""

from app import db
from app.clock import today as clock_today
from tests.test_api1 import AUTH, client  # noqa: F401  (pytest fixture)
from tests.test_fix4_1 import seed_account, seed_txn


def _card(client, **kw):
    body = {"name": "Card A", "balance_cents": 100000, "apr": 20.0,
            "min_payment_cents": 5000, "due_day": 28}
    body.update(kw)
    r = client.post("/api/cards", json=body, auth=AUTH)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# --- 1.1 (rejected as unreachable; kept as an invariant guard) -------------

def test_zero_income_month_still_funds_and_reserves_card_minimum(client):
    """Review 1.1 claimed an underfunded month zeroes a card's minimum, so
    Safe-to-Spend / reminders / upcoming would drop it. It can't happen:
    compute_budget's debt budget is minimums_total + extra, so even with no
    income the plan funds every minimum (the shortfall is reported
    separately). Pin that, end to end."""
    cid = _card(client)
    client.post("/api/cash-accounts", json={"name": "Checking", "balance_cents": 500000},
                auth=AUTH)
    home = client.get("/api/mobile/home", auth=AUTH).json()
    row = next(r for r in home["plan"] if r["card_id"] == cid)
    assert row["minimum_cents"] == 5000
    assert row["recommended_cents"] >= 5000
    assert row["min_remaining_cents"] == 5000
    assert home["budget"]["debt_budget_cents"] >= 5000

    up = [u for u in client.get("/api/upcoming?days=62", auth=AUTH).json()
          if u["kind"] == "card" and u["id"] == cid]
    assert up and up[0]["status"] == "due"
    assert up[0]["min_remaining_cents"] == 5000


# --- 1.2 deleting an over-payment restores the real prior balance -----------

def test_delete_overpayment_restores_prior_balance(client):
    cid = _card(client, balance_cents=5000, min_payment_cents=2500)
    today = clock_today().isoformat()
    p = client.post("/api/payments", json={"card_id": cid, "amount_cents": 10000,
                                           "paid_on": today}, auth=AUTH).json()
    assert client.get("/api/cards", auth=AUTH).json()[0]["balance_cents"] == 0
    assert client.delete(f"/api/payments/{p['id']}", auth=AUTH).status_code == 204
    # Was 15000 (phantom $50 of debt): revert must undo only what was applied.
    assert client.get("/api/cards", auth=AUTH).json()[0]["balance_cents"] == 5000


def test_legacy_payment_without_applied_record_still_reverts(client):
    cid = _card(client, balance_cents=50000)
    today = clock_today().isoformat()
    p = client.post("/api/payments", json={"card_id": cid, "amount_cents": 10000,
                                           "paid_on": today}, auth=AUTH).json()
    conn = client.app.state.db
    conn.execute("UPDATE payments SET applied_cents = NULL WHERE id = ?", (p["id"],))
    conn.commit()
    client.delete(f"/api/payments/{p['id']}", auth=AUTH)
    assert client.get("/api/cards", auth=AUTH).json()[0]["balance_cents"] == 50000


# --- 2.1 removing a bank connection demotes its cards/cash to manual ---------

def test_delete_plaid_item_demotes_linked_card_and_cash(tmp_path):
    from tests.test_security_hardening import _plaid_app
    from app.plaid import encrypt_token

    c, config = _plaid_app(tmp_path, [])
    conn = c.app.state.db
    card = c.post("/api/cards", json={"name": "Card", "balance_cents": 1000, "apr": 10.0,
                                      "min_payment_cents": 100}).json()
    cash = c.post("/api/cash-accounts", json={"name": "Chk", "balance_cents": 1000}).json()
    conn.execute(
        "INSERT INTO plaid_items (item_id, access_token_enc, institution_name, status, created_at) "
        "VALUES ('item-1', ?, 'Bank', 'ok', '2026-10-01')",
        (encrypt_token(config.token_key, "tok"),))
    conn.commit()
    seed_account(conn, 1, "acc-card", "credit", card_id=card["id"])
    seed_account(conn, 1, "acc-cash", "depository", cash_account_id=cash["id"])
    db.update_row(conn, "cards", card["id"], {"sync": "plaid"})
    db.update_row(conn, "cash_accounts", cash["id"], {"sync": "plaid"})

    assert c.delete("/api/plaid/items/1").status_code == 204
    assert db.get_row(conn, "cards", card["id"])["sync"] == "manual"
    assert db.get_row(conn, "cash_accounts", cash["id"])["sync"] == "manual"
    # The user can edit the cash balance again (was 409 "comes from your bank").
    r = c.put(f"/api/cash-accounts/{cash['id']}", json={"balance_cents": 2500})
    assert r.status_code == 200, r.text


# --- 2.2 / 2.3 deposit-rule links survive import and pending->posted ---------

def _rule_setup(client):
    conn = client.app.state.db
    conn.execute(
        "INSERT INTO plaid_items (item_id, access_token_enc, institution_name, status, created_at) "
        "VALUES ('item-1', 'x', 'Bank', 'ok', '2026-10-01')")
    conn.commit()
    acct = seed_account(conn, 1, "acc-1", "depository")
    rule = db.insert_row(conn, "deposit_rules",
                         {"match_text": "ACME PAYROLL", "label": "paycheck",
                          "created_at": "2026-10-01"})
    return conn, acct, rule["id"]


def test_import_preserves_transaction_rule_links(client):
    conn, acct, rule_id = _rule_setup(client)
    seed_txn(conn, acct, "t1", "2026-10-01", -100000, "ACME PAYROLL", label="paycheck",
             label_source="rule")
    conn.execute("UPDATE transactions SET rule_id = ? WHERE transaction_id = 't1'", (rule_id,))
    conn.commit()
    dump = client.get("/api/export", auth=AUTH).json()
    assert client.post("/api/import", json=dump, auth=AUTH).status_code == 200
    row = db.one(conn, "SELECT rule_id FROM transactions WHERE transaction_id = 't1'")
    assert row["rule_id"] == rule_id


def test_pending_to_posted_carries_rule_id(client):
    from app.plaid import _carry_pending_labels

    conn, acct, rule_id = _rule_setup(client)
    seed_txn(conn, acct, "pend-1", "2026-10-01", -100000, "ACME PAYROLL", pending=1,
             label="paycheck", label_source="rule")
    conn.execute("UPDATE transactions SET rule_id = ? WHERE transaction_id = 'pend-1'",
                 (rule_id,))
    seed_txn(conn, acct, "post-1", "2026-10-02", -100000, "ACME PAYROLL")
    conn.execute("UPDATE transactions SET pending_transaction_id = 'pend-1' "
                 "WHERE transaction_id = 'post-1'")
    conn.commit()
    _carry_pending_labels(conn)
    row = db.one(conn, "SELECT label, rule_id FROM transactions WHERE transaction_id = 'post-1'")
    assert row["label"] == "paycheck"
    assert row["rule_id"] == rule_id


# --- PWA metadata must stay public (regressed by the 2026-10-18 hardening) ---

def test_pwa_metadata_routes_are_public_and_exact(tmp_path):
    """Chromium's background service-worker update check fetches /sw.js with
    NO credentials. Behind auth it 401s, the update never lands, and an
    installed PWA keeps serving the old cached app forever. The exemption is
    exact-path only, so it adds no traversal surface."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    anon = TestClient(create_app(str(tmp_path / "p.db"), "paydown", "test"))
    for path in ("/sw.js", "/manifest.webmanifest"):
        assert anon.get(path).status_code == 200, path
    # still exact: nothing beneath or beside them is public
    for path in ("/sw.js/x", "/sw.jsx", "/manifest.webmanifest/../index.html",
                 "/", "/api/settings", "/app.js"):
        # 401, or 429 once these anonymous probes trip the lockout: both
        # mean "not public", which is the property under test.
        assert anon.get(path).status_code in (401, 429), path
