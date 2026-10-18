"""Wiring tests: Safe-to-Spend API routes, helpers, budget/cash changes and the
sync CLI output line (docs/SAFE.md 'Wiring'). Bank data is seeded directly into
sqlite; Plaid is never called. All tests run on a frozen clock (2026-10-02).
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import clock, db, paydays
from app.main import create_app

AUTH = ("user", "test")
TODAY = date(2026, 10, 2)
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    monkeypatch.setattr(clock, "now", lambda: NOW)
    app = create_app(str(tmp_path / "budget.db"), "user", "test")
    conn = app.state.db
    with TestClient(app) as c:
        yield c, conn


# --- seeding helpers (direct DB inserts, no Plaid) -------------------------------

def seed_item(conn, institution: str = "Bank") -> int:
    return db.insert_row(
        conn, "plaid_items",
        {"item_id": f"item-{institution}", "access_token_enc": "x",
         "institution_name": institution, "status": "ok", "error_code": None,
         "last_synced_at": None, "created_at": "2026-09-01T00:00:00Z"},
    )["id"]


def seed_account(conn, item_id: int, account_id: str, type_: str,
                 card_id: int | None = None, cash_account_id: int | None = None,
                 name: str = "Checking", mask: str = "1111") -> int:
    return db.insert_row(
        conn, "plaid_accounts",
        {"plaid_item_id": item_id, "account_id": account_id, "name": name,
         "official_name": None, "mask": mask, "type": type_, "subtype": None,
         "card_id": card_id, "cash_account_id": cash_account_id, "ignored": 0,
         "last_balance_cents": None, "last_payload": "{}"},
    )["id"]


def seed_txn(conn, plaid_account_id: int, transaction_id: str, date_: str,
             amount_cents: int, name: str, *, pending: int = 0, label: str | None = None,
             label_source: str | None = None, pay_schedule_id: int | None = None,
             pfc_primary: str | None = None, pfc_detailed: str | None = None,
             merchant_name: str | None = None) -> str:
    # transactions has no `id` column, so insert and read back directly.
    conn.execute(
        "INSERT INTO transactions (transaction_id, plaid_account_id, date, authorized_date,"
        " amount_cents, name, merchant_name, pending, pending_transaction_id, pfc_primary,"
        " pfc_detailed, label, label_source, pay_schedule_id, updated_at)"
        " VALUES (?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
        (transaction_id, plaid_account_id, date_, amount_cents, name, merchant_name,
         pending, pfc_primary, pfc_detailed, label, label_source, pay_schedule_id,
         "2026-09-01T00:00:00Z"),
    )
    conn.commit()
    return transaction_id


def make_cash_account(c, balance_cents: int, name: str = "Checking") -> int:
    r = c.post("/api/cash-accounts", json={"name": name, "balance_cents": balance_cents}, auth=AUTH)
    assert r.status_code == 201, r.text
    acct = r.json()
    # Timestamp the balance so it is fresh on the frozen clock.
    db.update_row(
        c.app.state.db, "cash_accounts", acct["id"],
        {"balance_updated_at": "2026-10-02T10:30:00Z"},
    )
    return acct["id"]


def make_card(c, name="Capital One", balance_cents=183000, due_day=7, min_cents=10000) -> int:
    r = c.post("/api/cards", json={"name": name, "balance_cents": balance_cents,
                                   "apr": 0.0, "min_payment_cents": min_cents,
                                   "due_day": due_day}, auth=AUTH)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_schedule(c, **overrides) -> dict:
    body = {"name": "Main paycheck", "frequency": "biweekly", "anchor_date": "2026-09-25",
            "amount_cents": 215000}
    body.update(overrides)
    r = c.post("/api/pay-schedules", json=body, auth=AUTH)
    assert r.status_code == 201, r.text
    return r.json()


# --- pay schedules ----------------------------------------------------------------

def test_pay_schedule_crud_and_validation(env):
    c, conn = env
    s = make_schedule(c)
    assert s["monthly_equivalent_cents"] == paydays.monthly_equivalent_cents(
        {"frequency": "biweekly", "amount_cents": 215000}
    )
    assert s["next_dates"] == ["2026-10-09", "2026-10-23", "2026-11-06"]
    assert s["deposit_confirmed"] is False and s["last_paycheck"] is None

    r = c.get("/api/pay-schedules", auth=AUTH)
    assert r.status_code == 200 and len(r.json()) == 1

    # Partial update
    r = c.put(f"/api/pay-schedules/{s['id']}", json={"amount_cents": 220000}, auth=AUTH)
    assert r.status_code == 200 and r.json()["amount_cents"] == 220000
    # Full update keeps unset semimonthly fields
    r = c.put(f"/api/pay-schedules/{s['id']}",
              json={"name": "Pay", "frequency": "biweekly", "anchor_date": "2026-09-25",
                    "amount_cents": 215000}, auth=AUTH)
    assert r.status_code == 200 and r.json()["semimonthly_day1"] is None

    # Validation
    r = c.post("/api/pay-schedules", json={"name": "X", "frequency": "daily",
                                           "anchor_date": "2026-09-25"}, auth=AUTH)
    assert r.status_code == 422
    r = c.post("/api/pay-schedules", json={"name": "X", "frequency": "semimonthly",
                                           "anchor_date": "2026-09-25"}, auth=AUTH)
    assert r.status_code == 422  # semimonthly requires both days
    r = c.post("/api/pay-schedules", json={"name": "X", "frequency": "semimonthly",
                                           "anchor_date": "2026-09-25",
                                           "semimonthly_day1": 20, "semimonthly_day2": 5}, auth=AUTH)
    assert r.status_code == 422  # day1 < day2
    r = c.post("/api/pay-schedules", json={"name": "X", "frequency": "biweekly",
                                           "anchor_date": "2026-09-25",
                                           "semimonthly_day1": 1, "semimonthly_day2": 15}, auth=AUTH)
    assert r.status_code == 422  # semimonthly days only for semimonthly
    r = c.post("/api/pay-schedules", json={"name": "X", "frequency": "biweekly",
                                           "anchor_date": "2026-09-25",
                                           "weekend_rule": "sometimes"}, auth=AUTH)
    assert r.status_code == 422
    r = c.post("/api/pay-schedules", json={"name": "", "frequency": "biweekly",
                                           "anchor_date": "2026-09-25"}, auth=AUTH)
    assert r.status_code == 422
    r = c.put("/api/pay-schedules/9999", json={"amount_cents": 1}, auth=AUTH)
    assert r.status_code == 404
    r = c.delete(f"/api/pay-schedules/{s['id']}", auth=AUTH)
    assert r.status_code == 204
    assert c.get("/api/pay-schedules", auth=AUTH).json() == []


def test_pay_schedule_upcoming(env):
    c, conn = env
    s = make_schedule(c)
    r = c.get(f"/api/pay-schedules/{s['id']}/upcoming?count=4", auth=AUTH)
    assert r.status_code == 200
    assert r.json() == {"dates": ["2026-10-09", "2026-10-23", "2026-11-06", "2026-11-20"]}
    r = c.get("/api/pay-schedules/9999/upcoming", auth=AUTH)
    assert r.status_code == 404


def test_pay_schedule_deposit_confirmed_and_last_paycheck(env):
    c, conn = env
    cash_id = make_cash_account(c, 412300)
    s = make_schedule(c)
    item = seed_item(conn)
    pa = seed_account(conn, item, "acct-cash", "depository", cash_account_id=cash_id)
    seed_txn(conn, pa, "pc1", "2026-09-25", -214988, "ACME PAYROLL",
             label="paycheck", pay_schedule_id=s["id"])
    rows = c.get("/api/pay-schedules", auth=AUTH).json()
    assert rows[0]["deposit_confirmed"] is True
    assert rows[0]["last_paycheck"] == {"date": "2026-09-25", "amount_cents": 214988}


def test_pay_schedule_detect(env):
    c, conn = env
    cash_id = make_cash_account(c, 100000)
    item = seed_item(conn)
    pa = seed_account(conn, item, "acct-cash", "depository", cash_account_id=cash_id)
    ids = []
    for i, (d, amt) in enumerate([("2026-08-28", 214988), ("2026-09-11", 215012),
                                  ("2026-09-25", 214988)]):
        ids.append(seed_txn(conn, pa, f"pc{i}", d, -amt, "ACME PAYROLL", label="paycheck"))
    # null = all posted paychecks from the last 120 days
    r = c.post("/api/pay-schedules/detect", json={"transaction_ids": None}, auth=AUTH)
    assert r.status_code == 200
    out = r.json()
    assert out["reason"] is None
    sug = out["suggestion"]
    assert sug["frequency"] == "biweekly"
    assert sug["anchor_date"] == "2026-09-25"
    assert sug["confidence"] == "high"
    assert sug["dates_used"] == ["2026-08-28", "2026-09-11", "2026-09-25"]
    assert sug["amount_cents"] == 214988
    # explicit transaction_ids subset
    r = c.post("/api/pay-schedules/detect", json={"transaction_ids": ids[:2]}, auth=AUTH)
    sug = r.json()["suggestion"]
    assert sug["frequency"] == "biweekly" and sug["confidence"] == "low"
    # one paycheck -> no suggestion (R13: response always carries the count)
    r = c.post("/api/pay-schedules/detect", json={"transaction_ids": [ids[0]]}, auth=AUTH)
    assert r.json() == {"suggestion": None, "reason": "need at least 2 paychecks", "count": 1}
    r = c.post("/api/pay-schedules/detect", json={"transaction_ids": []}, auth=AUTH)
    assert r.json()["suggestion"] is None


def test_use_as_income_and_monthly_equivalent(env):
    c, conn = env
    s = make_schedule(c)
    r = c.post(f"/api/pay-schedules/{s['id']}/use-as-income", auth=AUTH)
    assert r.status_code == 200
    row = r.json()
    assert row["name"] == "Main paycheck" and row["pay_schedule_id"] == s["id"]
    # Second call returns the existing row (no duplicate).
    r2 = c.post(f"/api/pay-schedules/{s['id']}/use-as-income", auth=AUTH)
    assert r2.json()["id"] == row["id"]
    assert c.get("/api/income", auth=AUTH).json()[0]["id"] == row["id"]
    # The budget uses the monthly equivalent (215000 * 26 / 12 = 465833).
    r = c.get("/api/month/2026-10", auth=AUTH)
    assert r.json()["budget"]["income_total_cents"] == 465833
    r = c.post("/api/pay-schedules/9999/use-as-income", auth=AUTH)
    assert r.status_code == 404


# --- deposits, labels, rules --------------------------------------------------------

def test_deposits_and_label(env):
    c, conn = env
    cash_id = make_cash_account(c, 100000)
    item = seed_item(conn)
    pa = seed_account(conn, item, "acct-cash", "depository", cash_account_id=cash_id)
    t1 = seed_txn(conn, pa, "d1", "2026-09-25", -214988, "ACME PAYROLL",
                  pfc_primary="INCOME", pfc_detailed="INCOME_SALARY", merchant_name="Acme")
    t2 = seed_txn(conn, pa, "d2", "2026-09-20", -50000, "TRANSFER FROM SAVINGS",
                  pfc_primary="TRANSFER_IN")
    r = c.get("/api/deposits?days=45", auth=AUTH)
    assert r.status_code == 200
    rows = r.json()
    assert [x["transaction_id"] for x in rows] == ["d1"]  # unlabelled transfer hidden
    r = c.get("/api/deposits?days=45&include_transfers=true", auth=AUTH)
    assert [x["transaction_id"] for x in r.json()] == ["d1", "d2"]
    row = rows[0]
    assert row["amount_cents"] == 214988 and row["account_name"] == "Checking"
    assert row["mask"] == "1111" and row["cash_account_id"] == cash_id

    # 404 unknown transaction
    r = c.put("/api/transactions/nope/label", json={"label": "paycheck"}, auth=AUTH)
    assert r.status_code == 404
    # 422: outflows can't be labelled
    seed_txn(conn, pa, "out1", "2026-09-26", 2000, "COFFEE")
    r = c.put("/api/transactions/out1/label", json={"label": "paycheck"}, auth=AUTH)
    assert r.status_code == 422
    # 422: bad label value
    r = c.put(f"/api/transactions/{t1}/label", json={"label": "whatever"}, auth=AUTH)
    assert r.status_code == 422

    t3 = seed_txn(conn, pa, "d3", "2026-10-01", -214988, "ACME PAYROLL DEPOSIT")
    # Label as paycheck with a rule; apply_rules labels d3 too.
    r = c.put(f"/api/transactions/{t1}/label",
              json={"label": "paycheck", "pay_schedule_id": None,
                    "create_rule": True, "rule_text": "acme payroll"}, auth=AUTH)
    assert r.status_code == 200
    out = r.json()
    assert out["label"] == "paycheck" and out["label_source"] == "manual"
    # The rule labelled the other matching deposit automatically.
    labelled = db.one(conn, "SELECT * FROM transactions WHERE transaction_id = ?", (t3,))
    assert labelled["label"] == "paycheck" and labelled["label_source"] == "rule"
    assert out["pay_schedule_name"] is None

    # deposit rules list + delete
    r = c.get("/api/deposit-rules", auth=AUTH)
    assert r.status_code == 200
    rules = r.json()
    assert len(rules) == 1 and rules[0]["match_text"] == "acme payroll"
    assert rules[0]["label"] == "paycheck" and rules[0]["pay_schedule_name"] is None
    assert c.delete(f"/api/deposit-rules/{rules[0]['id']}", auth=AUTH).status_code == 204
    assert c.get("/api/deposit-rules", auth=AUTH).json() == []
    assert c.delete("/api/deposit-rules/9999", auth=AUTH).status_code == 404
    # Deleting the rule does not change existing labels.
    assert db.one(conn, "SELECT label FROM transactions WHERE transaction_id = ?", (t3,))["label"] == "paycheck"

    # Clearing the label
    r = c.put(f"/api/transactions/{t1}/label",
              json={"label": None, "pay_schedule_id": None, "create_rule": False,
                    "rule_text": None}, auth=AUTH)
    assert r.json()["label"] is None


def test_label_with_schedule_name(env):
    c, conn = env
    cash_id = make_cash_account(c, 100000)
    s = make_schedule(c)
    item = seed_item(conn)
    pa = seed_account(conn, item, "acct-cash", "depository", cash_account_id=cash_id)
    t1 = seed_txn(conn, pa, "d1", "2026-09-25", -214988, "ACME PAYROLL")
    r = c.put(f"/api/transactions/{t1}/label",
              json={"label": "paycheck", "pay_schedule_id": s["id"], "create_rule": False,
                    "rule_text": None}, auth=AUTH)
    assert r.json()["pay_schedule_name"] == "Main paycheck"
    assert r.json()["pay_schedule_id"] == s["id"]
    # 422: unknown schedule
    t2 = seed_txn(conn, pa, "d2", "2026-09-26", -100, "X")
    r = c.put(f"/api/transactions/{t2}/label",
              json={"label": "paycheck", "pay_schedule_id": 999}, auth=AUTH)
    assert r.status_code == 422


# --- card payments / pending_out ---------------------------------------------------

def _seed_bank_with_card(c, conn):
    cash_id = make_cash_account(c, 412300)
    card_id = make_card(c)
    item = seed_item(conn)
    pa_cash = seed_account(conn, item, "acct-cash", "depository", cash_account_id=cash_id)
    pa_card = seed_account(conn, item, "acct-card", "credit", card_id=card_id, name="Card")
    return cash_id, card_id, pa_cash, pa_card


def test_card_payments_log_and_dismiss(env):
    c, conn = env
    cash_id, card_id, pa_cash, pa_card = _seed_bank_with_card(c, conn)
    # R14: only payments since the first day of the current month are listed.
    t_pay = seed_txn(conn, pa_card, "pay1", "2026-10-01", -50000, "AUTOPAY")
    t_pay2 = seed_txn(conn, pa_card, "pay2", "2026-10-02", -20000, "AUTOPAY")
    r = c.get("/api/card-payments/unlogged", auth=AUTH)
    assert r.status_code == 200
    rows = r.json()
    assert [x["transaction_id"] for x in rows] == ["pay2", "pay1"]
    assert rows[0]["amount_cents"] == 20000
    assert rows[0]["card_id"] == card_id and rows[0]["card_name"] == "Capital One"

    r = c.post("/api/card-payments/pay1/log", auth=AUTH)
    assert r.status_code == 201
    p = r.json()
    assert p["card_id"] == card_id and p["amount_cents"] == 50000
    assert p["month"] == "2026-10" and p["paid_on"] == "2026-10-01"
    assert p["note"] == "From your bank" and p["transaction_id"] == "pay1"
    # 409 if already logged
    assert c.post("/api/card-payments/pay1/log", auth=AUTH).status_code == 409
    # logged one disappears from the list
    rows = c.get("/api/card-payments/unlogged", auth=AUTH).json()
    assert [x["transaction_id"] for x in rows] == ["pay2"]
    # 404: unknown transaction, or a txn not on a linked card
    assert c.post("/api/card-payments/nope/log", auth=AUTH).status_code == 404
    assert c.post("/api/card-payments/pay2/dismiss", auth=AUTH).status_code == 204
    assert db.one(conn, "SELECT label FROM transactions WHERE transaction_id = 'pay2'")["label"] == "dismissed"
    assert c.get("/api/card-payments/unlogged", auth=AUTH).json() == []


def test_pending_out_excludes_payments_with_transaction_id(env):
    c, conn = env
    cash_id, card_id, pa_cash, pa_card = _seed_bank_with_card(c, conn)
    # Manual payment without a bank transaction: still pending at the bank.
    r = c.post("/api/payments", json={"card_id": card_id, "amount_cents": 5000,
                                      "paid_on": "2026-10-02", "month": "2026-10"}, auth=AUTH)
    assert r.status_code == 201
    # Payment linked to a bank transaction: already posted, must NOT count.
    t_pay = seed_txn(conn, pa_card, "pay1", "2026-09-28", -30000, "AUTOPAY")
    r = c.post("/api/card-payments/pay1/log", auth=AUTH)
    assert r.status_code == 201
    r = c.get("/api/safe-to-spend", auth=AUTH)
    assert r.json()["pending_out_cents"] == 5000


# --- bills -------------------------------------------------------------------------

def test_bills_paid_validation(env):
    c, conn = env
    r = c.post("/api/expenses", json={"name": "Electric", "amount_cents": 14000,
                                      "kind": "bill", "due_day": 5}, auth=AUTH)
    eid = r.json()["id"]
    # paid_from / match_text round-trip
    r = c.put(f"/api/expenses/{eid}",
              json={"name": "Electric", "amount_cents": 14000, "kind": "bill",
                    "due_day": 5, "paid_from": "card", "match_text": "electric co"}, auth=AUTH)
    assert r.status_code == 200
    assert r.json()["paid_from"] == "card" and r.json()["match_text"] == "electric co"
    r = c.put(f"/api/expenses/{eid}",
              json={"name": "Electric", "amount_cents": 14000, "kind": "bill",
                    "due_day": 5, "paid_from": "cash", "match_text": "electric"}, auth=AUTH)
    assert r.json()["paid_from"] == "cash"
    # match_text validation
    r = c.post("/api/expenses", json={"name": "X", "amount_cents": 1, "match_text": "a"},
               auth=AUTH)
    assert r.status_code == 422
    r = c.post("/api/expenses", json={"name": "X", "amount_cents": 1, "paid_from": "bitcoin"},
               auth=AUTH)
    assert r.status_code == 422

    # mark paid: 422 when not a due date of the bill
    r = c.post(f"/api/bills/{eid}/paid", json={"due_date": "2026-10-06"}, auth=AUTH)
    assert r.status_code == 422
    # 404 unknown expense
    r = c.post("/api/bills/9999/paid", json={"due_date": "2026-10-05"}, auth=AUTH)
    assert r.status_code == 404
    # not a bill
    r = c.post("/api/expenses", json={"name": "Food", "amount_cents": 5000,
                                      "kind": "spending"}, auth=AUTH)
    sid = r.json()["id"]
    r = c.post(f"/api/bills/{sid}/paid", json={"due_date": "2026-10-05"}, auth=AUTH)
    assert r.status_code == 422
    # valid
    r = c.post(f"/api/bills/{eid}/paid", json={"due_date": "2026-10-05"}, auth=AUTH)
    assert r.status_code == 201
    bp = r.json()
    assert bp["paid_on"] == "2026-10-02" and bp["source"] == "manual"
    # 409 duplicate
    r = c.post(f"/api/bills/{eid}/paid", json={"due_date": "2026-10-05"}, auth=AUTH)
    assert r.status_code == 409
    # undo
    r = c.delete(f"/api/bills/{eid}/paid/2026-10-05", auth=AUTH)
    assert r.status_code == 204
    assert c.delete(f"/api/bills/{eid}/paid/2026-10-05", auth=AUTH).status_code == 404


def test_auto_match_and_delete_stays_deleted(env):
    c, conn = env
    cash_id, card_id, pa_cash, pa_card = _seed_bank_with_card(c, conn)
    r = c.post("/api/expenses", json={"name": "Electric", "amount_cents": 14000,
                                      "kind": "bill", "due_day": 5,
                                      "match_text": "electric co"}, auth=AUTH)
    eid = r.json()["id"]
    seed_txn(conn, pa_cash, "el1", "2026-10-04", 14000, "ELECTRIC CO PAYMENT")
    # safe-to-spend runs bill matching first
    r = c.get("/api/safe-to-spend", auth=AUTH)
    bp = db.one(conn, "SELECT * FROM bill_payments WHERE expense_id = ?", (eid,))
    assert bp is not None and bp["source"] == "auto" and bp["transaction_id"] == "el1"
    assert bp["due_date"] == "2026-10-05" and bp["paid_on"] == "2026-10-04"
    paid_rows = [o for o in r.json()["obligations"] if o["status"] == "paid"]
    assert any(o["id"] == eid for o in paid_rows)
    # undo the auto match: the txn is dismissed so matching must not re-create it
    assert c.delete(f"/api/bills/{eid}/paid/2026-10-05", auth=AUTH).status_code == 204
    assert db.one(conn, "SELECT label FROM transactions WHERE transaction_id = 'el1'")["label"] == "dismissed"
    c.get("/api/safe-to-spend", auth=AUTH)  # another matching run
    assert db.one(conn, "SELECT COUNT(*) AS n FROM bill_payments WHERE expense_id = ?", (eid,))["n"] == 0
    # the bill shows as due (reserved) again
    obs = c.get("/api/safe-to-spend", auth=AUTH).json()["obligations"]
    due_rows = [o for o in obs if o["status"] == "due" and o["id"] == eid and o["kind"] == "bill"]
    assert len(due_rows) == 1


# --- goals -------------------------------------------------------------------------

def test_goals_crud_move_and_effects(env):
    c, conn = env
    # CRUD + validation
    r = c.post("/api/goals", json={"name": "Car insurance", "target_cents": 90000,
                                   "target_date": "2027-03-01"}, auth=AUTH)
    assert r.status_code == 201
    g = r.json()
    assert g["set_aside_cents"] == 0 and g["progress_pct"] == 0.0
    assert g["monthly_need_cents"] == 15000  # ceil($900 / 6 months) = $150
    assert g["per_paycheck_cents"] == 90000  # no paydays -> all remaining at once
    r = c.post("/api/goals", json={"name": "X", "target_cents": 0}, auth=AUTH)
    assert r.status_code == 422
    r = c.post("/api/goals", json={"name": "X", "target_cents": 100, "target_date": "bad"},
               auth=AUTH)
    assert r.status_code == 422

    # move: set aside 15000
    r = c.post(f"/api/goals/{g['id']}/move", json={"amount_cents": 15000, "note": "start"},
               auth=AUTH)
    assert r.status_code == 200
    g2 = r.json()
    assert g2["set_aside_cents"] == 15000 and g2["progress_pct"] == 16.7
    assert len(g2["moves"]) == 1 and g2["moves"][0]["moved_on"] == "2026-10-02"
    assert g2["monthly_need_cents"] == 15000  # balance BEFORE the month: the move
    # happened on 2026-10-02, so this month's need is unchanged (SAFE.md)
    # negative move below zero -> 422
    r = c.post(f"/api/goals/{g['id']}/move", json={"amount_cents": -20000}, auth=AUTH)
    assert r.status_code == 422
    # zero -> 422
    r = c.post(f"/api/goals/{g['id']}/move", json={"amount_cents": 0}, auth=AUTH)
    assert r.status_code == 422
    r = c.post("/api/goals/9999/move", json={"amount_cents": 1}, auth=AUTH)
    assert r.status_code == 404

    # Goals reduce the budget waterfall and the spare cash.
    make_card(c)
    make_cash_account(c, 300000)
    r = c.post("/api/income", json={"name": "Job", "amount_cents": 500000}, auth=AUTH)
    before = c.get("/api/month/2026-10", auth=AUTH).json()
    assert before["budget"]["goals_cents"] == 15000
    assert before["budget"]["available_after_min_cents"] == 500000 - 10000 - 15000
    assert before["cash"]["goals_set_aside_cents"] == 15000
    assert before["cash"]["reserve_cents"] >= 15000

    r = c.put(f"/api/goals/{g['id']}", json={"target_cents": 60000}, auth=AUTH)
    assert r.status_code == 200 and r.json()["target_cents"] == 60000
    assert c.delete(f"/api/goals/{g['id']}", auth=AUTH).status_code == 204
    assert c.get("/api/goals", auth=AUTH).json() == []
    assert c.delete("/api/goals/9999", auth=AUTH).status_code == 404


def test_goal_per_paycheck_uses_schedules(env):
    c, conn = env
    make_schedule(c)  # biweekly, 2026-10-09, 10-23, 11-06, 11-20 within 120 days
    r = c.post("/api/goals", json={"name": "Trip", "target_cents": 40000,
                                   "target_date": "2026-11-15"}, auth=AUTH)
    g = r.json()
    # 3 paydays in [today, target]: ceil(40000/3 -> $133.34) = 13400
    assert g["per_paycheck_cents"] == 13400
    assert g["monthly_need_cents"] == 20000  # ceil($400 / 2 months)


# --- safe-to-spend end-to-end -------------------------------------------------------

def test_safe_to_spend_end_to_end(env):
    c, conn = env
    # Seed:
    #   pay schedule 1: biweekly anchor 2026-09-25, $2,150.00
    #   checking (manual balance) $4,123.00, updated 2026-10-02T10:30:00Z
    #   linked depository account with a posted paycheck 2026-09-25 -$2,149.88
    #     labelled paycheck for schedule 1 (deposit-confirms the schedule)
    #   card "Capital One" $1,830.00 due on the 7th, linked credit account
    #   bills: Electric $140.00 due the 5th (cash); Internet $60.00 due the 20th
    #     (after the payday -> not reserved); Streaming $10.00 with no due day
    #   spending $826.67/month; goal Car insurance $900 with $150 set aside
    #
    # Hand computation (today = 2026-10-02, horizon = 2026-10-09,
    # period_start = 2026-09-25, 7 days in the window, 31 days in October):
    #   obligations: Electric 2026-10-05 due (reserved) 14000
    #                Capital One 2026-10-07 due (reserved) 10000 (S1: the
    #                  MINIMUM still owed is reserved, not min + extra)
    #                Streaming 2026-10-09 spread round_half_up(1000*7/31)=226
    #   Internet 2026-10-20 is after the horizon -> not listed
    #   obligations_total = 14000 + 10000 + 226 = 24226
    #   safe = 412300 - 0 (pending) - 24226 - 15000 (goal) - 0 (floor) = 373074
    #   per_day = 373074 // 7 = 53296
    #   planned = round_half_up(82667 * 7 / 31) = 18667
    #   planned_extra (S1, informational) = 183000 - 10000 = 173000
    cash_id = make_cash_account(c, 412300)
    card_id = make_card(c)
    s = make_schedule(c)
    item = seed_item(conn)
    pa_cash = seed_account(conn, item, "acct-cash", "depository", cash_account_id=cash_id)
    seed_account(conn, item, "acct-card", "credit", card_id=card_id, name="Card")
    seed_txn(conn, pa_cash, "pc1", "2026-09-25", -214988, "ACME PAYROLL",
             label="paycheck", pay_schedule_id=s["id"], pfc_primary="INCOME")
    r = c.post("/api/expenses", json={"name": "Electric", "amount_cents": 14000,
                                      "kind": "bill", "due_day": 5}, auth=AUTH)
    r = c.post("/api/expenses", json={"name": "Internet", "amount_cents": 6000,
                                      "kind": "bill", "due_day": 20}, auth=AUTH)
    r = c.post("/api/expenses", json={"name": "Streaming", "amount_cents": 1000,
                                      "kind": "bill"}, auth=AUTH)
    r = c.post("/api/expenses", json={"name": "Fun", "amount_cents": 82667,
                                      "kind": "spending"}, auth=AUTH)
    r = c.post("/api/goals", json={"name": "Car insurance", "target_cents": 90000,
                                   "target_date": "2027-03-01"}, auth=AUTH)
    gid = r.json()["id"]
    r = c.post(f"/api/goals/{gid}/move", json={"amount_cents": 15000}, auth=AUTH)
    assert r.status_code == 200
    # Income from the pay schedule: the monthly equivalent (465833) funds the
    # card plan, so the card's remaining before payday is its full balance.
    c.post(f"/api/pay-schedules/{s['id']}/use-as-income", auth=AUTH)

    resp = c.get("/api/safe-to-spend", auth=AUTH)
    assert resp.status_code == 200
    sts = resp.json()
    assert sts["today"] == "2026-10-02" and sts["horizon"] == "2026-10-09"
    assert sts["days_until"] == 7
    assert sts["payday"] == {"date": "2026-10-09", "schedule_id": s["id"],
                             "name": "Main paycheck", "expected_cents": 215000,
                             "status": "upcoming"}
    assert sts["period_start"] == "2026-09-25"
    assert sts["cash_total_cents"] == 412300
    assert sts["pending_out_cents"] == 0
    assert sts["cash_as_of"] == "2026-10-02T10:30:00Z"
    assert sts["obligations_total_cents"] == 24226
    assert sts["planned_extra_cents"] == 173000
    assert sts["goals_set_aside_cents"] == 15000
    assert sts["cash_floor_cents"] == 0
    assert sts["safe_to_spend_cents"] == 373074
    assert sts["per_day_cents"] == 53296
    assert sts["planned_spending_cents"] == 18667
    obs = {(o["kind"], o["id"], o["status"]): o for o in sts["obligations"]}
    assert obs[("bill", 1, "due")]["date"] == "2026-10-05"
    assert obs[("bill", 1, "due")]["amount_cents"] == 14000
    assert obs[("bill", 3, "spread")]["undated"] is True
    assert obs[("card", 1, "due")]["date"] == "2026-10-07"
    assert obs[("card", 1, "due")]["amount_cents"] == 10000
    assert obs[("card", 1, "due")]["extra_remaining_cents"] == 173000
    assert not any(o["id"] == 2 for o in sts["obligations"])  # Internet after payday
    # warnings: undated bill (Streaming); no unlabelled income; no stale balance
    codes = [w["code"] for w in sts["warnings"]]
    assert codes == ["undated_bills"]
    assert sts["accounts"] == [{"id": cash_id, "name": "Checking",
                                "balance_cents": 412300, "sync": "manual",
                                "last_synced_at": None}]
    assert sts["unlabelled_income_count"] == 0
    assert sts["unlogged_card_payment_count"] == 0

    # Marking Electric paid reduces safe-to-spend by exactly the bill amount.
    r = c.post("/api/bills/1/paid", json={"due_date": "2026-10-05"}, auth=AUTH)
    assert r.status_code == 201
    sts2 = c.get("/api/safe-to-spend", auth=AUTH).json()
    assert sts2["safe_to_spend_cents"] == 373074 + 14000
    paid = [o for o in sts2["obligations"] if o["status"] == "paid"]
    assert len(paid) == 1 and paid[0]["id"] == 1
    # Undo restores the number.
    c.delete("/api/bills/1/paid/2026-10-05", auth=AUTH)
    assert c.get("/api/safe-to-spend", auth=AUTH).json()["safe_to_spend_cents"] == 373074


def test_safe_to_spend_no_schedule_and_short(env):
    c, conn = env
    make_cash_account(c, 5000)
    r = c.post("/api/expenses", json={"name": "Rent", "amount_cents": 150000,
                                      "kind": "bill", "due_day": 5}, auth=AUTH)
    sts = c.get("/api/safe-to-spend", auth=AUTH).json()
    # No payday -> horizon is month end 2026-10-31
    assert sts["horizon"] == "2026-10-31"
    codes = [w["code"] for w in sts["warnings"]]
    assert "no_pay_schedule" in codes
    # Rent due 2026-10-05 reserved; safe = 5000 - 150000 = -145000 -> short
    assert sts["obligations_total_cents"] == 150000
    assert sts["safe_to_spend_cents"] == -145000
    assert sts["per_day_cents"] == 0
    assert "short" in codes


def test_safe_to_spend_unlabelled_income_and_unlogged(env):
    c, conn = env
    cash_id, card_id, pa_cash, pa_card = _seed_bank_with_card(c, conn)
    make_schedule(c)
    seed_txn(conn, pa_cash, "inc1", "2026-09-20", -30000, "MYSTERY DEPOSIT",
             pfc_primary="INCOME")
    seed_txn(conn, pa_card, "pay1", "2026-10-02", -25000, "AUTOPAY")  # R14: in current month
    sts = c.get("/api/safe-to-spend", auth=AUTH).json()
    assert sts["unlabelled_income_count"] == 1
    assert sts["unlogged_card_payment_count"] == 1
    codes = [w["code"] for w in sts["warnings"]]
    assert "unlabelled_income" in codes and "unlogged_card_payments" in codes
    # With a schedule present but INACTIVE, unlabelled_income is suppressed.
    c.put("/api/pay-schedules/1", json={"active": False}, auth=AUTH)
    sts = c.get("/api/safe-to-spend", auth=AUTH).json()
    assert "unlabelled_income" not in [w["code"] for w in sts["warnings"]]
    assert "no_upcoming_payday" in [w["code"] for w in sts["warnings"]]


def test_safe_to_spend_plaid_item_error(env):
    c, conn = env
    make_cash_account(c, 100000)
    item_id = seed_item(conn, "BrokenBank")
    db.update_row(conn, "plaid_items", item_id, {"status": "login_required"})
    sts = c.get("/api/safe-to-spend", auth=AUTH).json()
    warning = [w for w in sts["warnings"] if w["code"] == "plaid_item_error"]
    assert warning and "BrokenBank" in warning[0]["message"]


def test_safe_to_spend_stale_balance(env):
    c, conn = env
    acct_id = make_cash_account(c, 100000)
    db.update_row(conn, "cash_accounts", acct_id,
                  {"balance_updated_at": "2026-09-20T10:30:00Z"})
    sts = c.get("/api/safe-to-spend", auth=AUTH).json()
    assert "stale_balance" in [w["code"] for w in sts["warnings"]]


# --- upcoming ----------------------------------------------------------------------

def test_upcoming_route(env):
    c, conn = env
    cash_id, card_id, pa_cash, pa_card = _seed_bank_with_card(c, conn)
    make_schedule(c)
    c.post("/api/expenses", json={"name": "Electric", "amount_cents": 14000,
                                  "kind": "bill", "due_day": 5}, auth=AUTH)
    c.post("/api/expenses", json={"name": "Streaming", "amount_cents": 1000,
                                  "kind": "bill", "due_day": 6, "paid_from": "card"},
           auth=AUTH)
    r = c.post("/api/goals", json={"name": "Trip", "target_cents": 40000,
                                   "target_date": "2026-10-25"}, auth=AUTH)
    rows = c.get("/api/upcoming?days=30", auth=AUTH).json()
    kinds = [(r0["date"], r0["kind"]) for r0 in rows]
    assert ("2026-10-05", "bill") in kinds
    assert ("2026-10-06", "bill") in kinds
    assert ("2026-10-07", "card") in kinds
    assert ("2026-10-09", "payday") in kinds
    assert ("2026-10-23", "payday") in kinds
    assert ("2026-10-25", "goal") in kinds
    goal_row = next(r0 for r0 in rows if r0["kind"] == "goal")
    assert goal_row["amount_cents"] == 40000
    payday_row = next(r0 for r0 in rows if r0["kind"] == "payday")
    assert payday_row["status"] == "expected" and payday_row["amount_cents"] == 215000
    streaming = next(r0 for r0 in rows if r0["name"] == "Streaming")
    assert streaming["status"] == "on_card"
    electric = next(r0 for r0 in rows if r0["name"] == "Electric")
    assert electric["status"] == "due"
    # sorted by date then kind order payday, bill, card, goal
    order = {"payday": 0, "bill": 1, "card": 2, "goal": 3}
    keys = [(r0["date"], order[r0["kind"]]) for r0 in rows]
    assert keys == sorted(keys)
    # mark the electric bill paid -> status paid
    c.post("/api/bills/1/paid", json={"due_date": "2026-10-05"}, auth=AUTH)
    rows = c.get("/api/upcoming?days=30", auth=AUTH).json()
    electric = next(r0 for r0 in rows if r0["name"] == "Electric")
    assert electric["status"] == "paid"


def test_concurrent_requests_all_succeed(env):
    """Regression: the shared sqlite connection is used from uvicorn's
    threadpool; parallel requests used to corrupt each other (None rows /
    'bad parameter' misuse errors). Request handling is serialized in
    create_app, so hammering the parallel-fetch endpoints must always 200."""
    c, conn = env
    _seed_bank_with_card(c, conn)
    make_schedule(c)
    r = c.post("/api/goals", json={"name": "G", "target_cents": 40000,
                                   "target_date": "2026-10-25"}, auth=AUTH)
    assert r.status_code == 201
    paths = ["/api/safe-to-spend", "/api/upcoming?days=30", "/api/month/2026-10",
             "/api/goals", "/api/pay-schedules"]
    results = []
    errors = []

    def worker():
        try:
            for _ in range(10):
                for p in paths:
                    results.append(c.get(p, auth=AUTH).status_code)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert set(results) == {200}


# --- export / import ---------------------------------------------------------------

def test_export_import_roundtrip_new_tables(env):
    c, conn = env
    s = make_schedule(c)
    c.post(f"/api/pay-schedules/{s['id']}/use-as-income", auth=AUTH)
    c.post("/api/deposit-rules", auth=AUTH)  # 422 (no body) -> ignore; create via label route
    cash_id, card_id, pa_cash, pa_card = _seed_bank_with_card(c, conn)
    t = seed_txn(conn, pa_cash, "d1", "2026-09-25", -214988, "ACME PAYROLL")
    c.put(f"/api/transactions/{t}/label",
          json={"label": "paycheck", "pay_schedule_id": s["id"], "create_rule": True,
                "rule_text": "acme payroll"}, auth=AUTH)
    g = c.post("/api/goals", json={"name": "Trip", "target_cents": 40000,
                                   "target_date": "2026-10-25"}, auth=AUTH).json()
    c.post(f"/api/goals/{g['id']}/move", json={"amount_cents": 5000}, auth=AUTH)
    c.post("/api/expenses", json={"name": "Electric", "amount_cents": 14000,
                                  "kind": "bill", "due_day": 5}, auth=AUTH)
    c.post("/api/bills/1/paid", json={"due_date": "2026-10-05"}, auth=AUTH)

    export = c.get("/api/export", auth=AUTH).json()
    for table in ("pay_schedules", "deposit_rules", "bill_payments", "goals", "goal_moves"):
        assert export[table], table
    # Re-import the same payload; the new tables survive.
    r = c.post("/api/import", json=export, auth=AUTH)
    assert r.status_code == 200
    export2 = c.get("/api/export", auth=AUTH).json()
    assert export2["pay_schedules"] == export["pay_schedules"]
    assert export2["goals"] == export["goals"]
    assert export2["goal_moves"] == export["goal_moves"]
    assert export2["bill_payments"] == export["bill_payments"]
    assert export2["deposit_rules"] == export["deposit_rules"]
    # Transactions are bank data: untouched, and schedule links resolve.
    assert len(c.get("/api/deposits?days=45", auth=AUTH).json()) == 1
    assert db.one(conn, "SELECT pay_schedule_id FROM transactions WHERE transaction_id = 'd1'")["pay_schedule_id"] == s["id"]


# --- sync CLI output ---------------------------------------------------------------

def test_sync_cli_line_includes_txn_counts(env, capsys, tmp_path, monkeypatch):
    c, conn = env
    # R1: this test used to call sync_cli.main() with no BUDGET_DB, which ran
    # db.init_db("data/budget.db") and migrated the LIVE database on every test
    # run. Point it at a tmp DB (FIX4-1 R1).
    monkeypatch.setenv("BUDGET_DB", str(tmp_path / "cli" / "budget.db"))
    import io
    import contextlib
    import os
    from app.plaid import PlaidClient, PlaidConfig, sync_all
    # Fake client: no Plaid network calls; exercises the sync_item result shape.
    class FakeClient:
        def decrypt_token(self, enc):
            return "tok"
        def accounts_get(self, token):
            return {"accounts": []}
        def transactions_sync(self, token, cursor):
            return {"added": [], "modified": [], "removed": [], "has_more": False,
                    "next_cursor": "c1"}
    item_id = seed_item(conn, "CliBank")
    result = sync_all(conn, FakeClient())
    assert result["items"][0]["transactions_added"] == 0
    assert "transactions_added" in result["items"][0]
    # The CLI line format (sync_cli.main) prints the new fields.
    import sync_cli as cli
    import types
    cli.PlaidConfig = types.SimpleNamespace(from_env=lambda: True)
    cli.PlaidClient = lambda cfg: None
    cli.sync_all = lambda conn_, client_: result
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main()
        assert code == 0
        line = buf.getvalue().strip()
        assert "transactions_added=0 modified=0 removed=0" in line
        assert "CliBank" in line
    finally:
        pass