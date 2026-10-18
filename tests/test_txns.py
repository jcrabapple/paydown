"""Backend-data tests for phase 4: clock, migrations, transactions sync, txns helpers.

All Plaid HTTP is mocked with httpx.MockTransport; no network calls.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import types
from datetime import date, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet

from app import clock, db
from app.plaid import PlaidClient, PlaidConfig, encrypt_token, sync_item

TODAY = date.today()
TODAY_S = TODAY.isoformat()
MONTH = TODAY.strftime("%Y-%m")


def make_config() -> PlaidConfig:
    return PlaidConfig(client_id="cid", client_secret="cs", env="sandbox",
                       token_key=Fernet.generate_key(), redirect_uri=None)


_LAST_CONFIG: list = []


def make_client(routes: dict, calls: list | None = None):
    calls = calls if calls is not None else []
    config = make_config()
    _LAST_CONFIG.clear()
    _LAST_CONFIG.append(config)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls.append((request.url.path, body))
        r = routes.get(request.url.path)
        if r is None:
            return httpx.Response(404, json={
                "error_type": "INVALID_REQUEST", "error_code": "NOT_FOUND", "error_message": "nf"})
        status, data = r if isinstance(r, tuple) else (200, r)
        return httpx.Response(status, json=data)

    return PlaidClient(config, transport=httpx.MockTransport(handler)), config


def seed_item(conn, tx_cursor=None, item_id="item-1", token="access-t", key=None):
    # default to the key of the most recently created client so the item's
    # token is decryptable by that client
    key = key or (_LAST_CONFIG[0].token_key if _LAST_CONFIG else Fernet.generate_key())
    conn.execute(
        "INSERT INTO plaid_items (item_id, access_token_enc, institution_name, status, "
        "tx_cursor, created_at) VALUES (?, ?, ?, 'ok', ?, ?)",
        (item_id, encrypt_token(key, token), "Chase", tx_cursor, TODAY_S),
    )
    return key


def seed_account(conn, account_id="acc-dep", type="depository", item_id="item-1",
                 card_id=None, cash_account_id=None):
    conn.execute(
        "INSERT INTO plaid_accounts (plaid_item_id, account_id, name, mask, type, card_id, "
        "cash_account_id) VALUES ((SELECT id FROM plaid_items WHERE item_id = ?), ?, ?, "
        "'1111', ?, ?, ?)",
        (item_id, account_id, "Checking", type, card_id, cash_account_id),
    )


def txn(account_id="acc-dep", tid="t1", amount=-100.0, name="ACME PAYROLL",
        merchant="Acme", pending=False, pti=None, tdate=None):
    return {
        "transaction_id": tid, "account_id": account_id,
        "date": tdate or TODAY_S, "authorized_date": tdate or TODAY_S,
        "amount": amount, "name": name, "merchant_name": merchant,
        "pending": pending, "pending_transaction_id": pti,
        "personal_finance_category": {"primary": "INCOME", "detailed": "INCOME_SALARY"},
    }


DEP_ROUTES = {
    "/accounts/get": {"accounts": [
        {"account_id": "acc-dep", "type": "depository", "name": "Checking",
         "official_name": None, "mask": "1111", "balances": {"current": 100.0, "available": 100.0}},
    ], "item": {}},
}


def get_item_row(conn):
    return db.one(conn, "SELECT * FROM plaid_items WHERE item_id = 'item-1'")


def setup_item(conn, routes: dict, calls: list | None = None, tx_cursor=None):
    """Client + item whose token is encrypted with the client's own key."""
    client, config = make_client(routes, calls)
    seed_item(conn, tx_cursor=tx_cursor, key=config.token_key)
    return client, config



def link_cash_account(conn, account_id="acc-dep"):
    """R7: rules, paychecks, deposits and bill matching only consider LINKED
    cash accounts (cash_account_id set, not ignored). Tests that exercise
    those helpers must link the seeded depository account."""
    cur = conn.execute(
        "INSERT INTO cash_accounts (name, kind, balance_cents, created_at)"
        " VALUES ('Checking', 'checking', 0, ?)", (TODAY_S,))
    conn.execute("UPDATE plaid_accounts SET cash_account_id = ? WHERE account_id = ?",
                 (cur.lastrowid, account_id))
    conn.commit()

def txn_rows(conn, sql="SELECT * FROM transactions ORDER BY transaction_id"):
    return db.rows(conn, sql)


# --- sync: pagination -----------------------------------------------------------

def test_sync_pagination_with_has_more(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    calls: list = []
    routes = dict(DEP_ROUTES)
    routes["/transactions/sync"] = "PAGES"
    pages = [
        {"added": [txn(tid="t1"), txn(tid="t2", amount=-50.25)],
         "modified": [], "removed": [], "has_more": True, "next_cursor": "c1"},
        {"added": [txn(tid="t3", amount=12.34, name="COFFEE")],
         "modified": [], "removed": [], "has_more": False, "next_cursor": "c2"},
    ]
    state = {"page": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls.append((request.url.path, body))
        if request.url.path == "/transactions/sync":
            p = pages[state["page"]]
            state["page"] += 1
            return httpx.Response(200, json=p)
        r = routes.get(request.url.path)
        return httpx.Response(200, json=r)

    config = make_config()
    seed_item(conn, key=config.token_key)
    seed_account(conn)
    client = PlaidClient(config, transport=httpx.MockTransport(handler))
    item = get_item_row(conn)
    result = sync_item(conn, client, item)

    assert result["status"] == "ok"
    rows = txn_rows(conn)
    assert [r["transaction_id"] for r in rows] == ["t1", "t2", "t3"]
    t2 = [r for r in rows if r["transaction_id"] == "t2"][0]
    assert t2["amount_cents"] == -5025  # half-up cents, Plaid sign kept
    t3 = [r for r in rows if r["transaction_id"] == "t3"][0]
    assert t3["amount_cents"] == 1234
    assert t3["pfc_primary"] == "INCOME" and t3["pfc_detailed"] == "INCOME_SALARY"
    assert all(r["plaid_account_id"] for r in rows)
    # cursor saved; second request carries c1, first omits it entirely
    sync_bodies = [b for p, b in calls if p == "/transactions/sync"]
    assert "cursor" not in sync_bodies[0]
    assert sync_bodies[1]["cursor"] == "c1"
    assert sync_bodies[0]["count"] == 500
    assert db.one(conn, "SELECT tx_cursor FROM plaid_items WHERE item_id='item-1'")["tx_cursor"] == "c2"
    assert result["transactions_added"] == 3 and result["transactions_removed"] == 0


# --- sync: mutation during pagination restarts ----------------------------------

def test_sync_mutation_during_pagination_restarts(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    calls: list = []

    page1 = {"added": [txn(tid="t1")], "modified": [], "removed": [],
             "has_more": True, "next_cursor": "c1"}
    page_final = {"added": [txn(tid="t1"), txn(tid="t2")], "modified": [], "removed": [],
                  "has_more": False, "next_cursor": "c2"}
    state = {"sync_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls.append((request.url.path, body))
        if request.url.path == "/transactions/sync":
            state["sync_calls"] += 1
            if state["sync_calls"] == 1:
                return httpx.Response(200, json=page1)
            if state["sync_calls"] == 2:
                return httpx.Response(400, json={
                    "error_type": "API_ERROR",
                    "error_code": "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION",
                    "error_message": "mutation"})
            return httpx.Response(200, json=page_final)
        return httpx.Response(200, json=DEP_ROUTES[request.url.path])

    config = make_config()
    seed_item(conn, key=config.token_key)
    seed_account(conn)
    client = PlaidClient(config, transport=httpx.MockTransport(handler))
    result = sync_item(conn, client, get_item_row(conn))
    assert result["status"] == "ok"
    assert state["sync_calls"] == 3  # page, mutation error, full restart
    # restart went back to the saved cursor (None -> no cursor in body)
    sync_bodies = [b for p, b in calls if p == "/transactions/sync"]
    assert "cursor" not in sync_bodies[2]
    rows = txn_rows(conn)
    assert [r["transaction_id"] for r in rows] == ["t1", "t2"]
    assert db.one(conn, "SELECT tx_cursor FROM plaid_items WHERE item_id='item-1'")["tx_cursor"] == "c2"


def test_product_not_ready_leaves_item_ok(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    routes = dict(DEP_ROUTES)
    routes["/transactions/sync"] = (400, {
        "error_type": "PRODUCT_ERROR", "error_code": "PRODUCT_NOT_READY", "error_message": "not ready"})
    client, config = make_client(routes)
    seed_item(conn)
    seed_account(conn)
    result = sync_item(conn, client, get_item_row(conn))
    assert result["status"] == "ok"
    item = db.one(conn, "SELECT * FROM plaid_items WHERE item_id='item-1'")
    assert item["status"] == "ok" and item["error_code"] is None
    assert txn_rows(conn) == []
    assert item["tx_cursor"] is None


def test_other_transaction_error_marks_item_error(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    routes = dict(DEP_ROUTES)
    routes["/transactions/sync"] = (400, {
        "error_type": "API_ERROR", "error_code": "INVALID_ACCESS_TOKEN", "error_message": "bad"})
    client, config = make_client(routes)
    seed_item(conn)
    seed_account(conn)
    result = sync_item(conn, client, get_item_row(conn))
    assert result["status"] == "error"
    item = db.one(conn, "SELECT * FROM plaid_items WHERE item_id='item-1'")
    assert item["status"] == "error" and item["error_code"] == "INVALID_ACCESS_TOKEN"


# --- sync: removed / modified -----------------------------------------------------

def test_removed_deletes_row(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    routes = dict(DEP_ROUTES)
    routes["/transactions/sync"] = {"added": [], "modified": [], "removed": [
        {"transaction_id": "t1"}], "has_more": False, "next_cursor": "c1"}
    client, config = make_client(routes)
    seed_item(conn, tx_cursor="c0")
    seed_account(conn)
    conn.execute(
        "INSERT INTO transactions (transaction_id, plaid_account_id, date, amount_cents, "
        "name, pending, updated_at) VALUES ('t1', (SELECT id FROM plaid_accounts WHERE "
        "account_id='acc-dep'), ?, 5000, 'OLD', 0, ?)", (TODAY_S, TODAY_S))
    sync_item(conn, client, get_item_row(conn))
    assert txn_rows(conn) == []
    assert db.one(conn, "SELECT tx_cursor FROM plaid_items WHERE item_id='item-1'")["tx_cursor"] == "c1"


def test_modified_updates_but_keeps_label(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    routes = dict(DEP_ROUTES)
    routes["/transactions/sync"] = {"added": [], "modified": [
        txn(tid="t1", amount=-200.0, name="NEW NAME")], "removed": [],
        "has_more": False, "next_cursor": "c1"}
    client, config = make_client(routes)
    seed_item(conn, tx_cursor="c0")
    seed_account(conn)
    conn.execute(
        "INSERT INTO transactions (transaction_id, plaid_account_id, date, amount_cents, "
        "name, pending, label, label_source, updated_at) VALUES ('t1', (SELECT id FROM "
        "plaid_accounts WHERE account_id='acc-dep'), ?, 5000, 'OLD', 0, 'paycheck', "
        "'manual', ?)", (TODAY_S, TODAY_S))
    sync_item(conn, client, get_item_row(conn))
    row = db.one(conn, "SELECT * FROM transactions WHERE transaction_id='t1'")
    assert row["name"] == "NEW NAME" and row["amount_cents"] == -20000
    assert row["label"] == "paycheck" and row["label_source"] == "manual"


# --- sync: pending -> posted label carry-over (red before green) -----------------

def test_pending_to_posted_carries_manual_label(tmp_path):
    """A posted txn whose pending_transaction_id matches a stored, labelled pending
    row must inherit label, label_source and pay_schedule_id; the pending row is gone."""
    conn = db.init_db(str(tmp_path / "b.db"))
    routes = dict(DEP_ROUTES)
    routes["/transactions/sync"] = {
        "added": [txn(tid="posted-1", pti="pending-1", amount=-2149.88, pending=False)],
        "modified": [], "removed": [{"transaction_id": "pending-1"}],
        "has_more": False, "next_cursor": "c1"}
    client, config = make_client(routes)
    seed_item(conn, tx_cursor="c0")
    seed_account(conn)
    conn.execute(
        "INSERT INTO pay_schedules (name, frequency, anchor_date, created_at) "
        "VALUES ('Main paycheck', 'biweekly', ?, ?)", (TODAY_S, TODAY_S))
    conn.execute(
        "INSERT INTO transactions (transaction_id, plaid_account_id, date, amount_cents, "
        "name, pending, label, label_source, pay_schedule_id, updated_at) VALUES "
        "('pending-1', (SELECT id FROM plaid_accounts WHERE account_id='acc-dep'), "
        "?, -214988, 'ACME PAYROLL', 1, 'paycheck', 'manual', 1, ?)",
        (TODAY_S, TODAY_S))
    sync_item(conn, client, get_item_row(conn))
    posted = db.one(conn, "SELECT * FROM transactions WHERE transaction_id='posted-1'")
    assert posted is not None
    assert posted["label"] == "paycheck", f"label not carried: {posted['label']}"
    assert posted["label_source"] == "manual"
    assert posted["pay_schedule_id"] == 1
    assert posted["pending"] == 0
    assert db.one(conn, "SELECT * FROM transactions WHERE transaction_id='pending-1'") is None


# --- sync: atomicity (red before green) -------------------------------------------

def test_cursor_saved_only_when_whole_sync_commits(tmp_path, monkeypatch):
    """A failure injected mid-apply must leave the cursor AND the rows unchanged."""
    conn = db.init_db(str(tmp_path / "b.db"))
    routes = dict(DEP_ROUTES)
    routes["/transactions/sync"] = {
        "added": [txn(tid="t1"), txn(tid="t2")], "modified": [], "removed": [],
        "has_more": False, "next_cursor": "c2"}
    client, config = make_client(routes)
    seed_item(conn, tx_cursor=None)
    seed_account(conn)

    real = __import__("app.plaid", fromlist=["_upsert_txn"])._upsert_txn
    state = {"calls": 0}

    def failing_upsert(conn_, acct_map, t):
        state["calls"] += 1
        if state["calls"] == 2:
            raise RuntimeError("injected failure mid-apply")
        return real(conn_, acct_map, t)

    monkeypatch.setattr("app.plaid._upsert_txn", failing_upsert)
    with pytest.raises(RuntimeError, match="injected"):
        sync_item(conn, client, get_item_row(conn))
    monkeypatch.undo()

    item = db.one(conn, "SELECT * FROM plaid_items WHERE item_id='item-1'")
    assert item["tx_cursor"] is None, "cursor must not be saved when apply fails"
    assert txn_rows(conn) == [], "no transaction rows may be stored when apply fails"

    # a clean retry stores everything
    sync_item(conn, client, get_item_row(conn))
    assert [r["transaction_id"] for r in txn_rows(conn)] == ["t1", "t2"]
    assert db.one(conn, "SELECT tx_cursor FROM plaid_items WHERE item_id='item-1'")["tx_cursor"] == "c2"


# --- sync: account scoping --------------------------------------------------------

def test_txns_on_unstored_accounts_are_ignored(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    routes = dict(DEP_ROUTES)
    routes["/transactions/sync"] = {
        "added": [txn(tid="t1", account_id="acc-dep"),
                  txn(tid="t9", account_id="acc-brokerage", amount=-99.0)],
        "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"}
    client, config = make_client(routes)
    seed_item(conn)
    seed_account(conn)
    sync_item(conn, client, get_item_row(conn))
    rows = txn_rows(conn)
    assert [r["transaction_id"] for r in rows] == ["t1"]


# --- clock -------------------------------------------------------------------------

def test_clock_today_is_local_date(monkeypatch):
    monkeypatch.setenv("BUDGET_TZ", "Pacific/Kiritimati")  # UTC+14, always ahead
    monkeypatch.setattr(clock, "now", lambda: __import__("datetime").datetime(
        2026, 10, 2, 12, 0, tzinfo=__import__("datetime").timezone.utc))
    assert clock.today().isoformat() == "2026-10-03"  # 12:00 UTC + 14h = next day 02:00
    monkeypatch.setenv("BUDGET_TZ", "Pacific/Honolulu")  # UTC-10
    assert clock.today().isoformat() == "2026-10-02"  # 02:00 local same day


def test_clock_now_is_aware_utc():
    n = clock.now()
    assert n.tzinfo is not None and n.utcoffset().total_seconds() == 0


# --- apply_rules -------------------------------------------------------------------

def _seed_txn(conn, tid, amount=-2149.88, name="ACME PAYROLL", merchant="Acme",
              label=None, label_source=None, pending=0, date_=None):
    conn.execute(
        "INSERT INTO transactions (transaction_id, plaid_account_id, date, amount_cents, "
        "name, merchant_name, pending, label, label_source, updated_at) VALUES "
        "(?, (SELECT id FROM plaid_accounts WHERE account_id='acc-dep'), ?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, (date_ or TODAY_S), amount, name, merchant, pending, label, label_source, TODAY_S))


def test_apply_rules_labels_matching_inflows(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    seed_item(conn)
    seed_account(conn)
    link_cash_account(conn)  # R7: linked cash accounts only
    _seed_txn(conn, "t1", merchant="Acme Corp")
    conn.execute(
        "INSERT INTO deposit_rules (match_text, label, created_at) VALUES "
        "('acme', 'paycheck', ?)", (TODAY_S,))
    n = __import__("app.txns", fromlist=["apply_rules"]).apply_rules(conn)
    assert n == 1
    row = db.one(conn, "SELECT * FROM transactions WHERE transaction_id='t1'")
    assert row["label"] == "paycheck" and row["label_source"] == "rule"


def test_apply_rules_never_touches_manual_or_outflows(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    seed_item(conn)
    seed_account(conn)
    link_cash_account(conn)  # R7: linked cash accounts only
    _seed_txn(conn, "t1", merchant="Acme", label="not_paycheck", label_source="manual")
    _seed_txn(conn, "t2", amount=500.0, merchant="Acme")  # outflow, not an inflow
    conn.execute(
        "INSERT INTO deposit_rules (match_text, label, created_at) VALUES "
        "('acme', 'paycheck', ?)", (TODAY_S,))
    from app import txns
    n = txns.apply_rules(conn)
    assert n == 0
    assert db.one(conn, "SELECT * FROM transactions WHERE transaction_id='t1'")["label"] == "not_paycheck"
    assert db.one(conn, "SELECT * FROM transactions WHERE transaction_id='t2'")["label"] is None


def test_apply_rules_only_on_linked_cash_accounts(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    seed_item(conn)
    seed_account(conn)
    link_cash_account(conn)  # R7: linked cash accounts only
    seed_account(conn, account_id="acc-cc", type="credit")
    _seed_txn(conn, "t1", merchant="Acme")
    conn.execute(
        "INSERT INTO transactions (transaction_id, plaid_account_id, date, amount_cents, "
        "name, pending, updated_at) VALUES ('t2', (SELECT id FROM plaid_accounts WHERE "
        "account_id='acc-cc'), ?, -5000, 'REFUND ACME', 0, ?)", (TODAY_S, TODAY_S))
    conn.execute(
        "INSERT INTO deposit_rules (match_text, label, created_at) VALUES ('acme', 'paycheck', ?)",
        (TODAY_S,))
    from app import txns
    txns.apply_rules(conn)
    assert db.one(conn, "SELECT * FROM transactions WHERE transaction_id='t2'")["label"] is None
    assert db.one(conn, "SELECT * FROM transactions WHERE transaction_id='t1'")["label"] == "paycheck"


# --- set_label ----------------------------------------------------------------------

def test_set_label_manual_and_clear(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    seed_item(conn)
    seed_account(conn)
    link_cash_account(conn)  # R7: linked cash accounts only
    conn.execute(
        "INSERT INTO pay_schedules (name, frequency, anchor_date, created_at) "
        "VALUES ('S', 'weekly', ?, ?)", (TODAY_S, TODAY_S))
    _seed_txn(conn, "t1")
    from app import txns
    row = txns.set_label(conn, "t1", "paycheck", 1, create_rule=False)
    assert row["label"] == "paycheck" and row["label_source"] == "manual"
    assert row["pay_schedule_id"] == 1
    row = txns.set_label(conn, "t1", None, None)
    assert row["label"] is None and row["label_source"] is None
    assert row["pay_schedule_id"] is None
    assert txns.set_label(conn, "nope", "paycheck", None) is None


def test_set_label_with_create_rule_labels_other_matching_deposits(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    seed_item(conn)
    seed_account(conn)
    link_cash_account(conn)  # R7: linked cash accounts only
    _seed_txn(conn, "t1", merchant="Acme Corp")
    _seed_txn(conn, "t2", merchant="Acme Corp")
    _seed_txn(conn, "t3", merchant="Other Inc")
    from app import txns
    txns.set_label(conn, "t1", "paycheck", None, create_rule=True, rule_text=None)
    # rule created from the merchant name and applied to the other matching inflow
    rules = db.rows(conn, "SELECT * FROM deposit_rules")
    assert len(rules) == 1 and rules[0]["match_text"] == "acme corp"
    assert db.one(conn, "SELECT * FROM transactions WHERE transaction_id='t2'")["label"] == "paycheck"
    assert db.one(conn, "SELECT * FROM transactions WHERE transaction_id='t3'")["label"] is None
    # manual labels survive a later apply_rules
    txns.set_label(conn, "t3", "not_paycheck", None)
    txns.apply_rules(conn)
    assert db.one(conn, "SELECT * FROM transactions WHERE transaction_id='t3'")["label"] == "not_paycheck"


# --- paychecks / deposits ------------------------------------------------------------

def test_paychecks_and_deposits(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    seed_item(conn)
    seed_account(conn)
    link_cash_account(conn)  # R7: linked cash accounts only
    _seed_txn(conn, "t1", amount=-214988, merchant="Acme", label="paycheck")
    _seed_txn(conn, "t2", amount=-50000, name="TRANSFER IN", label=None)
    conn.execute(
        "UPDATE transactions SET pfc_primary = 'TRANSFER_IN' WHERE transaction_id = 't2'")
    conn.commit()
    _seed_txn(conn, "t3", amount=2500, name="COFFEE SHOP")
    _seed_txn(conn, "t4", amount=-99000, name="TRANSFER IN OTHER", label="paycheck")
    from app import txns
    pcs = txns.paychecks(conn)
    assert [p["transaction_id"] for p in pcs] == ["t1", "t4"]
    assert all(p["amount_cents"] > 0 for p in pcs) and pcs[0]["amount_cents"] == 214988

    deps = txns.deposits(conn, TODAY, days=45)
    assert [d["transaction_id"] for d in deps] == ["t4", "t1"]  # newest first
    assert all(d["amount_cents"] > 0 for d in deps)
    assert deps[0]["account_name"] == "Checking" and deps[0]["mask"] == "1111"
    # TRANSFER_IN without a label is hidden unless include_transfers
    deps_all = txns.deposits(conn, TODAY, days=45, include_transfers=True)
    assert [d["transaction_id"] for d in deps_all] == ["t4", "t2", "t1"]


# --- unlogged card payments -----------------------------------------------------------

def test_unlogged_card_payments_excludes_logged_and_dismissed(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    seed_item(conn)
    seed_account(conn, account_id="acc-cc", type="credit", card_id=None)
    conn.execute("INSERT INTO cards (name, balance_cents, apr, min_payment_cents, "
                 "starting_balance_cents, created_at) VALUES ('V', 0, 0.0, 0, 0, ?)", (TODAY_S,))
    conn.execute("UPDATE plaid_accounts SET card_id = 1 WHERE account_id='acc-cc'")
    old = (TODAY - timedelta(days=50)).isoformat()
    # R14: only payments since the first day of the current month are listed.
    recent = TODAY.replace(day=1).isoformat()
    for tid, amount, d, label in (("t-old", -500.0, old, None), ("t-log", -400.0, recent, None),
                                  ("t-dismiss", -300.0, recent, "dismissed"),
                                  ("t-keep", -20000, recent, None), ("t-out", 100.0, recent, None)):
        conn.execute(
            "INSERT INTO transactions (transaction_id, plaid_account_id, date, amount_cents, "
            "name, pending, label, updated_at) VALUES (?, (SELECT id FROM plaid_accounts WHERE "
            "account_id='acc-cc'), ?, ?, 'CC PAYMENT', 0, ?, ?)", (tid, d, amount, label, TODAY_S))
    conn.execute("INSERT INTO payments (card_id, month, paid_on, amount_cents, transaction_id) "
                 "VALUES (1, ?, ?, 40000, 't-log')", (MONTH, recent))
    from app import txns
    rows = txns.unlogged_card_payments(conn, TODAY)
    ids = [r["transaction_id"] for r in rows]
    assert ids == ["t-keep"], ids  # logged and dismissed excluded; too-old excluded; outflow excluded
    assert rows[0]["amount_cents"] == 20000 and rows[0]["card_name"] == "V"


# --- run_bill_matching -----------------------------------------------------------------

class _StubBills(types.ModuleType):
    """Standalone stub of app/bills.py per SAFE.md, used until the real module lands."""

    @staticmethod
    def match_bills(bills, txns_, paid, used_txn_ids, today, lookback_days=45):
        out = []
        for b in bills:
            if not b.get("match_text") or b.get("due_day") is None:
                continue
            text = b["match_text"].lower()
            for d in [date(TODAY.year, TODAY.month, min(b["due_day"], 28))]:
                due_s = d.isoformat()
                if (b["id"], due_s) in paid:
                    continue
                best = None
                for t in txns_:
                    if t["transaction_id"] in used_txn_ids or t["amount_cents"] <= 0:
                        continue
                    hay = ((t.get("merchant_name") or "") + " " + (t.get("name") or "")).lower()
                    if text not in hay:
                        continue
                    if abs((date.fromisoformat(t["date"]) - d).days) > 7:
                        continue
                    best = t
                    break
                if best:
                    out.append({"expense_id": b["id"], "due_date": due_s,
                                "paid_on": best["date"], "transaction_id": best["transaction_id"]})
                    used_txn_ids = used_txn_ids | {best["transaction_id"]}
        return out


@pytest.fixture
def stub_bills(monkeypatch):
    try:
        import app.bills  # noqa: F401
        yield None  # real module exists; use it
    except ImportError:
        monkeypatch.setitem(sys.modules, "app.bills", _StubBills("app.bills"))
        yield "stub"


@pytest.mark.parametrize(
    "fake_today",
    # S4: prove the test is day-of-month independent by also freezing the
    # clock at the 1st, 14th, 28th and 31st (repo clock patching).
    [None, "2026-03-01", "2026-03-14", "2026-03-28", "2026-03-31"],
)
def test_run_bill_matching_inserts_and_is_idempotent(tmp_path, stub_bills, monkeypatch,
                                                     fake_today):
    if fake_today is not None:
        frozen = date.fromisoformat(fake_today)
        monkeypatch.setattr(clock, "today", lambda: frozen)
        today = frozen
    else:
        today = clock.today()
    conn = db.init_db(str(tmp_path / "b.db"))
    seed_item(conn)
    seed_account(conn)
    link_cash_account(conn)  # R7: linked cash accounts only
    # S4: the bill's due day is picked relative to TODAY so the +-7-day match
    # window always contains the occurrence: the txn is seeded on TODAY itself
    # and due_day = today's day-of-month (a real calendar day, so due_dates'
    # clamping never moves it away from the txn).
    due_day = today.day
    conn.execute(
        "INSERT INTO expenses (name, amount_cents, kind, due_day, match_text) "
        "VALUES ('Electric', 14000, 'bill', ?, 'city power')", (due_day,), )
    recent = today.isoformat()
    _seed_txn(conn, "t1", amount=140.00, name="CITY POWER CO", merchant="City Power",
              date_=recent)
    from app import txns
    n = txns.run_bill_matching(conn, today)
    assert n == 1
    bp = db.rows(conn, "SELECT * FROM bill_payments")
    assert len(bp) == 1 and bp[0]["source"] == "auto"
    assert bp[0]["transaction_id"] == "t1" and bp[0]["expense_id"] == 1
    # idempotent: second run inserts nothing
    assert txns.run_bill_matching(conn, today) == 0
    assert len(db.rows(conn, "SELECT * FROM bill_payments")) == 1
    # manual payments are not duplicated by matching
    conn.execute("DELETE FROM bill_payments")
    expected_due = date(today.year, today.month, due_day).isoformat()
    conn.execute("INSERT INTO bill_payments (expense_id, due_date, paid_on, source) "
                 "VALUES (1, ?, ?, 'manual')", (expected_due, recent))
    assert txns.run_bill_matching(conn, today) == 0


# --- migrations --------------------------------------------------------------------------

PHASE3_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK (id = 1), strategy TEXT NOT NULL DEFAULT 'avalanche', buffer_cents INTEGER NOT NULL DEFAULT 0, debt_share_pct INTEGER NOT NULL DEFAULT 100, extra_override_cents INTEGER);
CREATE TABLE IF NOT EXISTS income (id INTEGER PRIMARY KEY, name TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0), active INTEGER NOT NULL DEFAULT 1, sort INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS expenses (id INTEGER PRIMARY KEY, name TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0), kind TEXT NOT NULL DEFAULT 'bill', due_day INTEGER CHECK (due_day BETWEEN 1 AND 31), active INTEGER NOT NULL DEFAULT 1, sort INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS cards (id INTEGER PRIMARY KEY, name TEXT NOT NULL, balance_cents INTEGER NOT NULL CHECK (balance_cents >= 0), apr REAL NOT NULL CHECK (apr >= 0 AND apr < 100), min_payment_cents INTEGER NOT NULL CHECK (min_payment_cents >= 0), credit_limit_cents INTEGER, due_day INTEGER CHECK (due_day BETWEEN 1 AND 31), priority INTEGER NOT NULL DEFAULT 0, starting_balance_cents INTEGER NOT NULL, closed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE, month TEXT NOT NULL, paid_on TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK (amount_cents > 0), note TEXT);
CREATE TABLE IF NOT EXISTS balance_log (id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE, logged_on TEXT NOT NULL, balance_cents INTEGER NOT NULL, source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS month_adjustments (id INTEGER PRIMARY KEY, month TEXT NOT NULL, name TEXT NOT NULL, amount_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS cash_accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'checking', balance_cents INTEGER NOT NULL, include_in_cash INTEGER NOT NULL DEFAULT 1, sync TEXT NOT NULL DEFAULT 'manual', last_synced_at TEXT, balance_updated_at TEXT, created_at TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS plaid_items (id INTEGER PRIMARY KEY, item_id TEXT NOT NULL UNIQUE, access_token_enc TEXT NOT NULL, institution_name TEXT, status TEXT NOT NULL DEFAULT 'ok', error_code TEXT, last_synced_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plaid_accounts (id INTEGER PRIMARY KEY, plaid_item_id INTEGER NOT NULL REFERENCES plaid_items(id) ON DELETE CASCADE, account_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL, official_name TEXT, mask TEXT, card_id INTEGER UNIQUE REFERENCES cards(id) ON DELETE SET NULL, ignored INTEGER NOT NULL DEFAULT 0, last_balance_cents INTEGER, last_payload TEXT);
"""


def test_migrations_run_twice_on_phase3_shaped_db(tmp_path):
    db_path = str(tmp_path / "live.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(PHASE3_SCHEMA)
    conn.execute("INSERT INTO settings (id) VALUES (1)")
    conn.execute("INSERT INTO income (name, amount_cents) VALUES ('Pay', 300000)")
    conn.execute("INSERT INTO expenses (name, amount_cents, kind, due_day) VALUES ('Rent', 120000, 'bill', 1)")
    conn.execute("INSERT INTO cash_accounts (name, balance_cents, created_at) VALUES ('Checking', 50000, ?)", (TODAY_S,))
    conn.commit()
    conn.close()

    conn1 = db.init_db(db_path)
    conn1.close()
    conn2 = db.init_db(db_path)  # second run must not error
    cols = lambda t: {r[1] for r in conn2.execute(f"PRAGMA table_info({t})")}  # noqa: E731
    for t in ("pay_schedules", "transactions", "deposit_rules", "bill_payments", "goals", "goal_moves"):
        assert t in {r[0] for r in conn2.execute("SELECT name FROM sqlite_master WHERE type='table'")}, t
    assert "paid_from" in cols("expenses") and "match_text" in cols("expenses")
    assert "pay_schedule_id" in cols("income")
    assert "transaction_id" in cols("payments")
    assert "tx_cursor" in cols("plaid_items")
    assert [tuple(r) for r in conn2.execute("SELECT name, amount_cents FROM income")] == [("Pay", 300000)]
    assert conn2.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 1
    conn2.close()


def test_export_import_includes_new_tables(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    seed_item(conn)
    seed_account(conn)
    conn.execute(
        "INSERT INTO pay_schedules (name, frequency, anchor_date, created_at) "
        "VALUES ('Main paycheck', 'biweekly', ?, ?)", (TODAY_S, TODAY_S))
    conn.execute("INSERT INTO goals (name, target_cents, created_at) VALUES ('Car', 90000, ?)", (TODAY_S,))
    conn.execute("INSERT INTO goal_moves (goal_id, amount_cents, moved_on) VALUES (1, 15000, ?)", (TODAY_S,))
    conn.execute("INSERT INTO deposit_rules (match_text, label, created_at) VALUES ('acme', 'paycheck', ?)", (TODAY_S,))
    conn.execute("INSERT INTO expenses (name, amount_cents, kind, due_day) VALUES ('E', 100, 'bill', 1)")
    conn.execute("INSERT INTO bill_payments (expense_id, due_date, paid_on, source, transaction_id) VALUES (1, ?, ?, 'auto', 't9')", (TODAY_S, TODAY_S))
    conn.execute(
        "INSERT INTO transactions (transaction_id, plaid_account_id, date, amount_cents, "
        "name, pending, label, pay_schedule_id, updated_at) VALUES ('t9', (SELECT id FROM "
        "plaid_accounts WHERE account_id='acc-dep'), ?, -1000, 'X', 0, 'paycheck', 1, ?)",
        (TODAY_S, TODAY_S))
    conn.commit()

    dump = db.export_all(conn)
    for t in ("pay_schedules", "deposit_rules", "bill_payments", "goals", "goal_moves"):
        assert t in dump, t
    assert "transactions" not in dump

    db.import_all(conn, dump)
    # transactions untouched by import; dangling schedule reference cleared
    row = db.one(conn, "SELECT * FROM transactions WHERE transaction_id='t9'")
    assert row["label"] == "paycheck"  # not touched
    # delete the schedule, re-import, pay_schedule_id must be nulled
    conn.execute("DELETE FROM pay_schedules")
    conn.commit()
    db.import_all(conn, dump)
    row = db.one(conn, "SELECT * FROM transactions WHERE transaction_id='t9'")
    assert row["pay_schedule_id"] is None