"""FIX5 Round 2 Lane B tests: B1 auto-log card payments (link vs insert,
7/8-day boundary, previous-month window, idempotence, setting gate),
B2 auto-label salary deposits, B3 bulk labels endpoint."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

from app import clock, db, txns
from app.main import create_app
from app.plaid import PlaidClient, PlaidConfig, encrypt_token, sync_item

TODAY = date(2026, 10, 14)
TODAY_S = TODAY.isoformat()
NOW = datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc)
NOW_S = "2026-10-14T14:00:00Z"


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: NOW)
    monkeypatch.setattr(clock, "today", lambda: TODAY)


def init(tmp_path):
    p = Path(str(tmp_path))
    if p.suffix != ".db":
        p.mkdir(parents=True, exist_ok=True)
        p = p / "b.db"
    return db.init_db(str(p))


def seed_card_account(conn, account_id="acc-cc", card_id=1, type="credit",
                      item_id="item-1"):
    conn.execute(
        "INSERT OR IGNORE INTO plaid_items (item_id, access_token_enc, institution_name, status, "
        "tx_cursor, created_at) VALUES (?, ?, 'Chase', 'ok', NULL, ?)",
        (item_id, "x", TODAY_S),
    )
    conn.execute(
        "INSERT INTO plaid_accounts (plaid_item_id, account_id, name, mask, type, card_id) "
        "VALUES ((SELECT id FROM plaid_items WHERE item_id = ?), ?, 'Card', '1111', ?, ?)",
        (item_id, account_id, type, card_id),
    )


def seed_cash_account(conn, account_id="acc-dep", cash_account_id=1, item_id="item-1"):
    conn.execute(
        "INSERT OR IGNORE INTO plaid_items (item_id, access_token_enc, institution_name, status, "
        "tx_cursor, created_at) VALUES (?, ?, 'Chase', 'ok', NULL, ?)",
        (item_id, "x", TODAY_S),
    )
    conn.execute(
        "INSERT INTO cash_accounts (name, kind, balance_cents, created_at) "
        "VALUES ('Checking', 'checking', 0, ?)", (TODAY_S,))
    conn.execute(
        "INSERT INTO plaid_accounts (plaid_item_id, account_id, name, mask, type, card_id, "
        "cash_account_id, ignored) VALUES ((SELECT id FROM plaid_items WHERE item_id = ?), "
        "?, 'Checking', '1111', 'depository', NULL, ?, 0)",
        (item_id, account_id, cash_account_id),
    )


def seed_card(conn, card_id=1, name="Visa"):
    conn.execute(
        "INSERT INTO cards (id, name, balance_cents, apr, min_payment_cents, "
        "starting_balance_cents, created_at) VALUES (?, ?, 0, 0.0, 0, 0, ?)",
        (card_id, name, TODAY_S),
    )


def seed_txn(conn, tid, account_id, amount_cents, tdate=TODAY_S, label=None,
             label_source=None, pending=0, pfc_primary=None, pfc_detailed=None,
             name="TXN"):
    conn.execute(
        "INSERT INTO transactions (transaction_id, plaid_account_id, date, amount_cents, "
        "name, pending, label, label_source, pfc_primary, pfc_detailed, updated_at) "
        "VALUES (?, (SELECT id FROM plaid_accounts WHERE account_id = ?), ?, ?, ?, ?, "
        "?, ?, ?, ?, ?)",
        (tid, account_id, tdate, amount_cents, name, pending, label, label_source,
         pfc_primary, pfc_detailed, NOW_S),
    )


# --- B1: auto_log_card_payments -------------------------------------------------------

def test_b1_links_matching_manual_payment(tmp_path):
    conn = init(tmp_path)
    seed_card(conn)
    seed_card_account(conn)
    txn_date = TODAY - timedelta(days=3)
    seed_txn(conn, "t1", "acc-cc", -50000, txn_date.isoformat(),
             pfc_primary="LOAN_PAYMENTS")
    manual_date = txn_date - timedelta(days=2)  # within 7 days
    conn.execute(
        "INSERT INTO payments (card_id, month, paid_on, amount_cents, note, created_at) "
        "VALUES (1, ?, ?, 50000, 'Manual', ?)", ("2026-09", manual_date.isoformat(), NOW_S))
    result = txns.auto_log_card_payments(conn, TODAY)
    assert result == {"logged": 0, "linked": 1}
    row = db.one(conn, "SELECT * FROM payments WHERE card_id = 1")
    assert row["transaction_id"] == "t1"
    assert db.one(conn, "SELECT COUNT(*) AS n FROM payments")["n"] == 1  # no duplicate


def test_b1_inserts_when_no_manual_payment_matches(tmp_path):
    conn = init(tmp_path)
    seed_card(conn)
    seed_card_account(conn)
    seed_txn(conn, "t1", "acc-cc", -50000, TODAY_S, pfc_primary="LOAN_PAYMENTS")
    result = txns.auto_log_card_payments(conn, TODAY)
    assert result == {"logged": 1, "linked": 0}
    p = db.one(conn, "SELECT * FROM payments WHERE transaction_id = 't1'")
    assert p["card_id"] == 1
    assert p["month"] == TODAY_S[:7]
    assert p["paid_on"] == TODAY_S
    assert p["amount_cents"] == 50000
    assert p["note"] == "From your bank"
    assert p["created_at"] == NOW_S


def test_b1_seven_day_boundary(tmp_path):
    # 7 days apart links; 8 days apart inserts (deliberate-break target: <= vs <).
    for gap, expect_linked in ((7, 1), (8, 0)):
        conn = init(tmp_path / f"g{gap}")
        seed_card(conn)
        seed_card_account(conn)
        txn_date = TODAY - timedelta(days=10)
        seed_txn(conn, "t1", "acc-cc", -50000, txn_date.isoformat(),
                 pfc_primary="LOAN_PAYMENTS")
        manual_date = txn_date + timedelta(days=gap)
        conn.execute(
            "INSERT INTO payments (card_id, month, paid_on, amount_cents, created_at) "
            "VALUES (1, ?, ?, 50000, ?)", ("2026-09", manual_date.isoformat(), NOW_S))
        result = txns.auto_log_card_payments(conn, TODAY)
        assert result["linked"] == expect_linked, (gap, result)
        assert result["logged"] == (1 - expect_linked), (gap, result)
        total = db.one(conn, "SELECT COUNT(*) AS n FROM payments")["n"]
        # linked: the manual row is reused (1 row); inserted: a new row is
        # added next to the too-far manual row (2 rows).
        assert total == (1 if expect_linked else 2), (gap, total)


def test_b1_previous_month_window(tmp_path):
    # A payment dated in the PREVIOUS month is a candidate for auto-logging
    # (the unlogged list only shows the current month).
    conn = init(tmp_path)
    seed_card(conn)
    seed_card_account(conn)
    prev_month = (TODAY.replace(day=1) - timedelta(days=1)).replace(day=5)
    seed_txn(conn, "t-prev", "acc-cc", -50000, prev_month.isoformat(),
             pfc_primary="LOAN_PAYMENTS")
    assert txns.unlogged_card_payments(conn, TODAY) == []  # not in the UI list
    result = txns.auto_log_card_payments(conn, TODAY)
    assert result == {"logged": 1, "linked": 0}
    assert db.one(conn, "SELECT COUNT(*) AS n FROM payments WHERE transaction_id='t-prev'")["n"] == 1
    # Older than the previous month is out of scope.
    conn2 = init(tmp_path / "old")
    seed_card(conn2, 2, "Old")
    seed_card_account(conn2, "acc-old", card_id=2, item_id="item-2")
    seed_txn(conn2, "t-ancient", "acc-old", -50000, (TODAY - timedelta(days=90)).isoformat(),
             pfc_primary="LOAN_PAYMENTS")
    assert txns.auto_log_card_payments(conn2, TODAY) == {"logged": 0, "linked": 0}


def test_b1_idempotent_second_run(tmp_path):
    conn = init(tmp_path)
    seed_card(conn)
    seed_card_account(conn)
    seed_txn(conn, "t1", "acc-cc", -50000, TODAY_S, pfc_primary="LOAN_PAYMENTS")
    seed_txn(conn, "t2", "acc-cc", -30000, (TODAY - timedelta(days=2)).isoformat(),
             pfc_primary="LOAN_PAYMENTS")
    conn.execute(
        "INSERT INTO payments (card_id, month, paid_on, amount_cents, created_at) "
        "VALUES (1, ?, ?, 30000, ?)", ("2026-10", (TODAY - timedelta(days=3)).isoformat(), NOW_S))
    first = txns.auto_log_card_payments(conn, TODAY)
    assert first == {"logged": 1, "linked": 1}
    second = txns.auto_log_card_payments(conn, TODAY)
    assert second == {"logged": 0, "linked": 0}
    assert db.one(conn, "SELECT COUNT(*) AS n FROM payments")["n"] == 2


def test_b1_skips_wrong_card_or_amount(tmp_path):
    conn = init(tmp_path)
    seed_card(conn, 1, "A")
    seed_card(conn, 2, "B")
    seed_card_account(conn, "acc-a", card_id=1, item_id="item-1")
    seed_card_account(conn, "acc-b", card_id=2, item_id="item-2")
    seed_txn(conn, "t1", "acc-a", -50000, TODAY_S, pfc_primary="LOAN_PAYMENTS")
    # Same amount but on the wrong card -> no link, insert.
    conn.execute(
        "INSERT INTO payments (card_id, month, paid_on, amount_cents, created_at) "
        "VALUES (2, ?, ?, 50000, ?)", ("2026-10", TODAY_S, NOW_S))
    result = txns.auto_log_card_payments(conn, TODAY)
    assert result == {"logged": 1, "linked": 0}
    # Different amount on the right card -> no link.
    conn3 = init(tmp_path / "amt")
    seed_card(conn3, 1, "A")
    seed_card_account(conn3, "acc-a", card_id=1, item_id="item-1")
    seed_txn(conn3, "t1", "acc-a", -50000, TODAY_S, pfc_primary="LOAN_PAYMENTS")
    conn3.execute(
        "INSERT INTO payments (card_id, month, paid_on, amount_cents, created_at) "
        "VALUES (1, ?, ?, 49999, ?)", ("2026-10", TODAY_S, NOW_S))
    assert txns.auto_log_card_payments(conn3, TODAY) == {"logged": 1, "linked": 0}


# --- B1 via sync / endpoints ----------------------------------------------------------

def make_client(tmp_path):
    app = create_app(str(tmp_path / "budget.db"), "user", "test")
    return TestClient(app)


def test_b1_startup_hook_logs_and_labels(tmp_path):
    conn = db.init_db(str(tmp_path / "budget.db"))
    seed_card(conn)
    seed_card_account(conn)
    seed_cash_account(conn)
    seed_txn(conn, "t-pay", "acc-cc", -50000, TODAY_S, pfc_primary="LOAN_PAYMENTS")
    seed_txn(conn, "t-sal", "acc-dep", -250000, TODAY_S,
             pfc_primary="INCOME", pfc_detailed="INCOME_SALARY")
    conn.commit()
    client = make_client(tmp_path)
    r = client.get("/api/card-payments/unlogged", auth=("user", "test"))
    assert r.status_code == 200
    assert r.json() == []  # startup already logged it
    assert db.one(conn, "SELECT COUNT(*) AS n FROM payments WHERE transaction_id='t-pay'")["n"] == 1
    r = client.get("/api/deposits?days=45", auth=("user", "test"))
    row = [d for d in r.json() if d["transaction_id"] == "t-sal"][0]
    assert row["label"] == "paycheck" and row["label_source"] == "auto"


def test_b1_log_all_endpoint(tmp_path):
    client = make_client(tmp_path)
    conn = db.init_db(str(tmp_path / "budget.db"))  # same db the app uses
    seed_card(conn)
    seed_card_account(conn)
    seed_txn(conn, "t1", "acc-cc", -50000, TODAY_S, pfc_primary="LOAN_PAYMENTS")
    conn.commit()
    r = client.post("/api/card-payments/log-all", auth=("user", "test"))
    assert r.status_code == 200
    assert r.json() == {"logged": 1, "linked": 0}
    # Second run: already logged, idempotent.
    r2 = client.post("/api/card-payments/log-all", auth=("user", "test"))
    assert r2.json() == {"logged": 0, "linked": 0}
    # Manual single-log endpoint still works and shares the helper.
    seed_txn(conn, "t2", "acc-cc", -10000, TODAY_S, pfc_primary="LOAN_PAYMENTS")
    conn.commit()
    r3 = client.post("/api/card-payments/t2/log", auth=("user", "test"))
    assert r3.status_code == 201
    assert r3.json()["note"] == "From your bank" and r3.json()["amount_cents"] == 10000
    r4 = client.post("/api/card-payments/t2/log", auth=("user", "test"))
    assert r4.status_code == 409


def test_b1_setting_gates_sync_but_not_log_all(tmp_path):
    conn = init(tmp_path)
    conn.execute("UPDATE settings SET auto_log_card_payments = 0 WHERE id = 1")
    conn.commit()
    seed_card(conn)
    seed_card_account(conn)
    routes = {
        "/accounts/get": {"accounts": [
            {"account_id": "acc-cc", "type": "credit", "name": "Card",
             "official_name": None, "mask": "1111",
             "balances": {"current": 100.0, "available": 100.0}}],
            "item": {}},
        "/transactions/sync": {"added": [
             {"transaction_id": "t1", "account_id": "acc-cc", "date": TODAY_S,
             "authorized_date": TODAY_S, "amount": -500.0, "name": "CC PAYMENT",
             "merchant_name": None, "pending": False, "pending_transaction_id": None,
             "personal_finance_category": {"primary": "LOAN_PAYMENTS", "detailed": None}}],
            "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"},
    }
    config = PlaidConfig(client_id="cid", client_secret="cs", env="sandbox",
                         token_key=Fernet.generate_key(), redirect_uri=None)
    conn.execute("UPDATE plaid_items SET access_token_enc = ? WHERE item_id = 'item-1'",
                 (encrypt_token(config.token_key, "access-t"),))
    conn.commit()
    client = PlaidClient(config, transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=routes.get(req.url.path))))
    item = db.one(conn, "SELECT * FROM plaid_items WHERE item_id = 'item-1'")
    result = sync_item(conn, client, item)
    assert result["status"] == "ok"
    # Setting is off -> sync must NOT log the payment...
    assert db.one(conn, "SELECT COUNT(*) AS n FROM payments WHERE transaction_id='t1'")["n"] == 0
    # ...but log-all always logs.
    assert txns.auto_log_card_payments(conn, TODAY) == {"logged": 1, "linked": 0}


def test_b1_sync_logs_when_setting_on(tmp_path):
    conn = init(tmp_path)
    seed_card(conn)
    seed_card_account(conn)
    seed_cash_account(conn, item_id="item-2")
    routes = {
        "/accounts/get": {"accounts": [
            {"account_id": "acc-cc", "type": "credit", "name": "Card",
             "official_name": None, "mask": "1111",
             "balances": {"current": 100.0, "available": 100.0}}],
            "item": {}},
        "/transactions/sync": {"added": [
            {"transaction_id": "t1", "account_id": "acc-cc", "date": TODAY_S,
             "authorized_date": TODAY_S, "amount": -500.0, "name": "CC PAYMENT",
             "merchant_name": None, "pending": False, "pending_transaction_id": None,
             "personal_finance_category": {"primary": "LOAN_PAYMENTS", "detailed": None}},
            {"transaction_id": "t-sal", "account_id": "acc-dep", "date": TODAY_S,
             "authorized_date": TODAY_S, "amount": -1000.0, "name": "ACME PAYROLL",
             "merchant_name": "Acme", "pending": False, "pending_transaction_id": None,
             "personal_finance_category": {"primary": "INCOME", "detailed": "INCOME_SALARY"}}],
            "modified": [], "removed": [], "has_more": False, "next_cursor": "c1"},
    }
    config = PlaidConfig(client_id="cid", client_secret="cs", env="sandbox",
                         token_key=Fernet.generate_key(), redirect_uri=None)
    conn.execute("UPDATE plaid_items SET access_token_enc = ? WHERE item_id = 'item-1'",
                 (encrypt_token(config.token_key, "access-t"),))
    conn.commit()
    client = PlaidClient(config, transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=routes.get(req.url.path))))
    item = db.one(conn, "SELECT * FROM plaid_items WHERE item_id = 'item-1'")
    assert sync_item(conn, client, item)["status"] == "ok"
    assert db.one(conn, "SELECT COUNT(*) AS n FROM payments WHERE transaction_id='t1'")["n"] == 1
    sal = db.one(conn, "SELECT * FROM transactions WHERE transaction_id='t-sal'")
    assert sal["label"] == "paycheck" and sal["label_source"] == "auto"


# --- B2: auto_label_salary ------------------------------------------------------------

def test_b2_labels_unlabelled_salary(tmp_path):
    conn = init(tmp_path)
    seed_cash_account(conn)
    seed_txn(conn, "t1", "acc-dep", -250000, TODAY_S,
             pfc_primary="INCOME", pfc_detailed="INCOME_SALARY")
    assert txns.auto_label_salary(conn) == 1
    r = db.one(conn, "SELECT * FROM transactions WHERE transaction_id = 't1'")
    assert r["label"] == "paycheck" and r["label_source"] == "auto"
    # Second run: already labelled, not touched again.
    assert txns.auto_label_salary(conn) == 0


def test_b2_never_overwrites_manual_or_rule_labels(tmp_path):
    conn = init(tmp_path)
    seed_cash_account(conn)
    seed_txn(conn, "t-manual", "acc-dep", -250000, TODAY_S, label="not_paycheck",
             label_source="manual", pfc_primary="INCOME", pfc_detailed="INCOME_SALARY")
    seed_txn(conn, "t-rule", "acc-dep", -250000, TODAY_S, label="not_paycheck",
             label_source="rule", pfc_primary="INCOME", pfc_detailed="INCOME_SALARY")
    assert txns.auto_label_salary(conn) == 0
    for tid in ("t-manual", "t-rule"):
        r = db.one(conn, "SELECT * FROM transactions WHERE transaction_id = ?", (tid,))
        assert r["label"] == "not_paycheck" and r["label_source"] in ("manual", "rule")


def test_b2_skips_pending_other_accounts_and_categories(tmp_path):
    conn = init(tmp_path)
    seed_cash_account(conn)
    seed_card(conn)
    seed_card_account(conn)
    seed_txn(conn, "t-pending", "acc-dep", -250000, TODAY_S, pending=1,
             pfc_primary="INCOME", pfc_detailed="INCOME_SALARY")
    seed_txn(conn, "t-other", "acc-dep", -250000, TODAY_S,
             pfc_primary="INCOME", pfc_detailed="INCOME_OTHER")
    seed_txn(conn, "t-outflow", "acc-dep", 250000, TODAY_S,
             pfc_primary="INCOME", pfc_detailed="INCOME_SALARY")
    seed_txn(conn, "t-card", "acc-cc", -250000, TODAY_S,
             pfc_primary="INCOME", pfc_detailed="INCOME_SALARY")  # credit account
    # an ignored depository account
    conn.execute(
        "INSERT INTO cash_accounts (name, kind, balance_cents, created_at) "
        "VALUES ('Ignored', 'checking', 0, ?)", (TODAY_S,))
    conn.execute(
        "INSERT INTO plaid_accounts (plaid_item_id, account_id, name, mask, type, "
        "cash_account_id, ignored) VALUES ((SELECT id FROM plaid_items WHERE item_id='item-1'), "
        "'acc-ign', 'Ignored', '2222', 'depository', 2, 1)")
    seed_txn(conn, "t-ignored", "acc-ign", -250000, TODAY_S,
             pfc_primary="INCOME", pfc_detailed="INCOME_SALARY")
    assert txns.auto_label_salary(conn) == 0
    assert db.one(conn, "SELECT COUNT(*) AS n FROM transactions WHERE label = 'paycheck'")["n"] == 0


# --- B3: bulk labels endpoint ---------------------------------------------------------

def test_b3_bulk_labels_endpoint(tmp_path):
    client = make_client(tmp_path)
    conn = db.init_db(str(tmp_path / "budget.db"))
    seed_cash_account(conn)
    seed_txn(conn, "t1", "acc-dep", -1000, TODAY_S)
    seed_txn(conn, "t2", "acc-dep", -2000, TODAY_S)
    seed_txn(conn, "t3", "acc-dep", -3000, TODAY_S, label="paycheck", label_source="manual")
    conn.commit()
    r = client.post("/api/transactions/labels", auth=("user", "test"),
                    json={"transaction_ids": ["t1", "t2", "t-missing"], "label": "not_paycheck",
                          "pay_schedule_id": None})
    assert r.status_code == 200
    assert r.json() == {"updated": 2}  # unknown ids skipped
    for tid in ("t1", "t2"):
        row = db.one(conn, "SELECT * FROM transactions WHERE transaction_id = ?", (tid,))
        assert row["label"] == "not_paycheck" and row["label_source"] == "manual"
    # clearing with null
    r2 = client.post("/api/transactions/labels", auth=("user", "test"),
                     json={"transaction_ids": ["t1"], "label": None, "pay_schedule_id": None})
    assert r2.json() == {"updated": 1}
    row = db.one(conn, "SELECT * FROM transactions WHERE transaction_id = 't1'")
    assert row["label"] is None and row["label_source"] is None
    # >500 ids -> 422
    r3 = client.post("/api/transactions/labels", auth=("user", "test"),
                     json={"transaction_ids": [f"x{i}" for i in range(501)],
                           "label": "paycheck", "pay_schedule_id": None})
    assert r3.status_code == 422


# --- settings field -------------------------------------------------------------------

def test_settings_auto_log_roundtrip(tmp_path):
    client = make_client(tmp_path)
    r = client.get("/api/settings", auth=("user", "test"))
    assert r.status_code == 200
    assert r.json()["auto_log_card_payments"] is True  # default
    r2 = client.put("/api/settings", auth=("user", "test"),
                    json={"auto_log_card_payments": False})
    assert r2.status_code == 200
    assert r2.json()["auto_log_card_payments"] is False
    # other fields survive
    assert client.get("/api/settings", auth=("user", "test")).json()[
        "auto_log_card_payments"] is False
    client.put("/api/settings", auth=("user", "test"), json={"auto_log_card_payments": True})
    assert client.get("/api/settings", auth=("user", "test")).json()[
        "auto_log_card_payments"] is True


def test_settings_auto_log_off_disables_startup_autolog(tmp_path):
    conn = db.init_db(str(tmp_path / "budget.db"))
    seed_card(conn)
    seed_card_account(conn)
    seed_txn(conn, "t1", "acc-cc", -50000, TODAY_S, pfc_primary="LOAN_PAYMENTS")
    conn.execute("UPDATE settings SET auto_log_card_payments = 0 WHERE id = 1")
    conn.commit()
    make_client(tmp_path)  # startup runs with the setting off
    assert db.one(conn, "SELECT COUNT(*) AS n FROM payments")["n"] == 0


def test_deleting_bank_payment_is_not_relogged(tmp_path):
    """Orchestrator review: deleting an auto-logged bank payment must stick;
    otherwise the next sync/startup/log-all silently logs it again."""
    client = make_client(tmp_path)
    conn = db.init_db(str(tmp_path / "budget.db"))
    seed_card(conn)
    seed_card_account(conn)
    seed_txn(conn, "t1", "acc-cc", -50000, TODAY_S, pfc_primary="LOAN_PAYMENTS")
    conn.commit()
    assert client.post("/api/card-payments/log-all", auth=("user", "test")).json()["logged"] == 1
    pid = db.one(conn, "SELECT id FROM payments WHERE transaction_id='t1'")["id"]
    assert client.delete(f"/api/payments/{pid}", auth=("user", "test")).status_code == 204
    assert client.post("/api/card-payments/log-all", auth=("user", "test")).json() == {"logged": 0, "linked": 0}
    assert client.get("/api/card-payments/unlogged", auth=("user", "test")).json() == []
    # still loggable by hand from the transaction id
    assert client.post("/api/card-payments/t1/log", auth=("user", "test")).status_code == 201


def test_bulk_labels_rejects_bad_schedule_before_applying_any(tmp_path):
    # Review F6: a bad pay_schedule_id must not partially apply the batch.
    client = make_client(tmp_path)
    conn = db.init_db(str(tmp_path / "budget.db"))
    seed_cash_account(conn)
    seed_txn(conn, "d1", "acc-dep", -1000, TODAY_S, pfc_primary="INCOME")
    conn.commit()
    r = client.post("/api/transactions/labels", auth=("user", "test"),
                    json={"transaction_ids": ["d1"], "label": "paycheck", "pay_schedule_id": 999})
    assert r.status_code == 422
    assert db.one(conn, "SELECT label FROM transactions WHERE transaction_id='d1'")["label"] is None
