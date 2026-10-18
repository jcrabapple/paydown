"""Regression tests for fix round 1 (docs/FIX4-1.md, items R1-R15).

One test per item; each failed before its fix (red evidence recorded in the
fix-round report).
"""
from __future__ import annotations

import threading
import time
import unittest.mock as mock
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import clock, db, paydays, sts as sts_mod, txns as txns_mod
from app.main import create_app
from app.plaid import PlaidClient, PlaidConfig

AUTH = ("user", "test")
TODAY = date(2026, 10, 2)
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _make_env(tmp_path, monkeypatch, today=TODAY, now=NOW):
    monkeypatch.setattr(clock, "today", lambda: today)
    monkeypatch.setattr(clock, "now", lambda: now)
    app = create_app(str(tmp_path / "budget.db"), "user", "test")
    conn = app.state.db
    with TestClient(app) as c:
        yield c, conn


@pytest.fixture()
def env(tmp_path, monkeypatch):
    yield from _make_env(tmp_path, monkeypatch)


@pytest.fixture()
def late_env(tmp_path, monkeypatch):
    """Frozen clock at 2026-11-01 (R2: late payday across a month boundary)."""
    yield from _make_env(
        tmp_path, monkeypatch, date(2026, 11, 1), datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    )


# --- seeding helpers -----------------------------------------------------------------

def seed_item(conn, institution: str = "Bank") -> int:
    return db.insert_row(
        conn, "plaid_items",
        {"item_id": f"item-{institution}", "access_token_enc": "x",
         "institution_name": institution, "status": "ok", "error_code": None,
         "last_synced_at": None, "created_at": "2026-09-01T00:00:00Z"},
    )["id"]


def seed_account(conn, item_id: int, account_id: str, type_: str,
                 card_id: int | None = None, cash_account_id: int | None = None,
                 name: str = "Checking", mask: str = "1111", ignored: int = 0) -> int:
    return db.insert_row(
        conn, "plaid_accounts",
        {"plaid_item_id": item_id, "account_id": account_id, "name": name,
         "official_name": None, "mask": mask, "type": type_, "subtype": None,
         "card_id": card_id, "cash_account_id": cash_account_id, "ignored": ignored,
         "last_balance_cents": None, "last_payload": "{}"},
    )["id"]


def seed_txn(conn, plaid_account_id: int, transaction_id: str, date_: str,
             amount_cents: int, name: str, *, pending: int = 0, label: str | None = None,
             label_source: str | None = None, pay_schedule_id: int | None = None,
             pfc_primary: str | None = None, pfc_detailed: str | None = None,
             merchant_name: str | None = None) -> str:
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
    db.update_row(c.app.state.db, "cash_accounts", acct["id"],
                  {"balance_updated_at": "2026-10-02T10:30:00Z"})
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


def _seed_bank_with_card(c, conn):
    cash_id = make_cash_account(c, 412300)
    card_id = make_card(c)
    item = seed_item(conn)
    pa_cash = seed_account(conn, item, "acct-cash", "depository", cash_account_id=cash_id)
    pa_card = seed_account(conn, item, "acct-card", "credit", card_id=card_id, name="Card")
    return cash_id, card_id, pa_cash, pa_card


# --- R1: the live DB guard ----------------------------------------------------------

def test_r1_init_db_on_live_path_raises(tmp_path, monkeypatch):
    """R1: db.connect must refuse any path resolving to <repo>/data/budget.db.

    Without the tests/conftest.py guard this call would open (and migrate) the
    production DB - the bug being guarded against.
    """
    from pathlib import Path
    live = Path(__file__).resolve().parent.parent / "data" / "budget.db"
    with pytest.raises(RuntimeError, match="tests must not open the live DB"):
        db.init_db(str(live))
    conn = db.init_db(str(tmp_path / "ok.db"))
    assert conn is not None


def test_r1_sync_cli_creates_db_dir(tmp_path, monkeypatch, capsys):
    """R1 (agy F-07): sync_cli.main creates the DB's parent directory."""
    import contextlib
    import io
    import types
    import sync_cli as cli
    db_path = tmp_path / "nested" / "deeper" / "b.db"
    monkeypatch.setenv("BUDGET_DB", str(db_path))
    result = {"items": [{"institution_name": "B", "status": "ok", "error_code": None,
                         "accounts_updated": 0, "transactions_added": 1,
                         "transactions_modified": 2, "transactions_removed": 3}]}
    # monkeypatch restores these so later sync_cli tests see the real module.
    monkeypatch.setattr(cli, "PlaidConfig", types.SimpleNamespace(from_env=lambda: True))
    monkeypatch.setattr(cli, "PlaidClient", lambda cfg: None)
    monkeypatch.setattr(cli, "sync_all", lambda conn_, client_: result)
    assert not db_path.parent.exists()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main()
    assert code == 0
    assert db_path.exists()
    assert "transactions_added=1 modified=2 removed=3" in buf.getvalue()


# --- R2: card rows across a month boundary with a late payday -----------------------

def test_r2_card_reserved_when_late_payday_crosses_month(late_env):
    c, conn = late_env
    cash_id, card_id, pa_cash, pa_card = _seed_bank_with_card(c, conn)
    s = make_schedule(c, anchor_date="2026-10-30")
    # A labelled paycheck on Oct 16 makes the schedule deposit-confirmed, so
    # the not-yet-received Oct 30 payday is a LATE candidate (horizon in the
    # past) instead of rolling to Nov 13.
    seed_txn(conn, pa_cash, "pc0", "2026-10-16", -214988, "ACME PAYROLL",
             label="paycheck", pay_schedule_id=s["id"])
    # today 2026-11-01; the Oct 30 payday is late (within grace) -> horizon
    # 2026-10-30 is BEFORE today. The card due Nov 10 must still be reserved.
    c.put(f"/api/cards/{card_id}", json={"due_day": 10}, auth=AUTH)
    r = c.get("/api/safe-to-spend", auth=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["horizon"] == "2026-10-30"
    card_obs = [o for o in body["obligations"] if o["kind"] == "card" and o["id"] == card_id]
    assert card_obs, "card obligation missing entirely (R2 bug)"
    assert any(o["date"] == "2026-11-10" and o["status"] in ("due", "overdue") and o["reserved"]
               for o in card_obs), card_obs


# --- R3: undated cards reserved once per month ---------------------------------------

def test_r3_undated_card_reserved_once_over_two_month_horizon():
    row1 = {"card_id": 2, "name": "C", "due_day": None, "remaining_cents": 99000,
            "recommended_cents": 99000, "paid_cents": 0}
    rows = {
        "2026-10": [dict(row1)],
        "2026-11": [dict(row1)],
        "2026-12": [dict(row1)],
    }
    info = {
        "payday": {"date": date(2026, 12, 15), "schedule_id": 1, "name": "P",
                   "expected_cents": 0, "status": "upcoming"},
        "missed": [], "last_payday": date(2026, 9, 25),
    }
    r = sts_mod.safe_to_spend(
        today=date(2026, 10, 2), now=NOW, payday_info=info, has_schedules=True,
        cash_total_cents=500000, pending_out_cents=0, cash_as_of=NOW,
        cash_floor_cents=0, goals_set_aside_cents=0, bills=[], bill_payments=set(),
        card_rows=rows, spending_monthly_cents=0, extra_warnings=[],
    )
    undated = [o for o in r["obligations"] if o["kind"] == "card" and o["undated"]]
    assert len(undated) == 1, f"undated card reserved {len(undated)} times (R3 bug)"
    assert r["obligations_total_cents"] == 99000


# --- R4: missed paychecks between received ones --------------------------------------

def _sched(**over):
    s = {"id": 1, "name": "S", "frequency": "biweekly", "anchor_date": "2026-09-11",
         "semimonthly_day1": None, "semimonthly_day2": None, "weekend_rule": "none",
         "amount_cents": 100000, "active": 1}
    s.update(over)
    return s


def _pc(tid, d, sid=1):
    return {"transaction_id": tid, "date": d, "amount_cents": 100000, "pay_schedule_id": sid}


def test_r4_missed_between_received_are_reported():
    # Paychecks Sep 11 and Oct 23 arrived; Sep 25 and Oct 9 never did.
    s = _sched()
    pcs = [_pc("a", date(2026, 9, 11)), _pc("b", date(2026, 10, 23))]
    r = paydays.next_payday(date(2026, 11, 2), [s], pcs)
    missed = [m["date"] for m in r["missed"]]
    assert missed == [date(2026, 9, 25), date(2026, 10, 9)], f"missed={missed} (R4 bug)"


def test_r4_nothing_missed_before_first_received_paycheck():
    # First labelled paycheck is Oct 23: nothing before it may be flagged.
    s = _sched()
    pcs = [_pc("b", date(2026, 10, 23))]
    r = paydays.next_payday(date(2026, 11, 2), [s], pcs)
    assert r["missed"] == [], f"missed={r['missed']} (R4 bug)"


# --- R5: receipts assigns by nearest occurrence ---------------------------------------

def test_r5_paycheck_picks_nearest_occurrence():
    s = {"id": 1, "name": "S", "frequency": "semimonthly",
         "anchor_date": "2026-01-01", "semimonthly_day1": 1, "semimonthly_day2": 5,
         "weekend_rule": "none", "amount_cents": 0, "active": 1}
    pcs = [_pc("p1", date(2026, 1, 4))]
    r = paydays.receipts(s, pcs, date(2026, 1, 1), date(2026, 1, 31))
    assert r[date(2026, 1, 5)] is not None, "Jan 4 paycheck should map to Jan 5 (R5 bug)"
    assert r[date(2026, 1, 1)] is None
    assert r[date(2026, 1, 5)]["transaction_id"] == "p1"


def test_r5_occurrence_keeps_nearest_when_two_paychecks_compete():
    s = {"id": 1, "name": "S", "frequency": "semimonthly",
         "anchor_date": "2026-01-01", "semimonthly_day1": 1, "semimonthly_day2": 5,
         "weekend_rule": "none", "amount_cents": 0, "active": 1}
    # Jan 6 is 1 day from Jan 5 and 5 from Jan 1; Jan 4 is 3 from Jan 1 and 1 from Jan 5.
    # Both paychecks pick Jan 5; the occurrence keeps the nearest, ties -> earlier date.
    pcs = [_pc("early", date(2026, 1, 4)), _pc("late", date(2026, 1, 6))]
    r = paydays.receipts(s, pcs, date(2026, 1, 1), date(2026, 1, 31))
    assert r[date(2026, 1, 5)]["transaction_id"] == "early"
    assert r[date(2026, 1, 1)] is None


# --- R6: NULL category deposits are not hidden ----------------------------------------

def test_r6_null_category_deposit_visible(env):
    c, conn = env
    cash_id = make_cash_account(c, 100000)
    item = seed_item(conn)
    pa = seed_account(conn, item, "acct-cash", "depository", cash_account_id=cash_id)
    # pfc_primary NULL: the old `NOT (pfc_primary = 'TRANSFER_IN' AND label IS NULL)`
    # predicate evaluates to NULL and hides the row.
    seed_txn(conn, pa, "nullcat", "2026-09-25", -50000, "MYSTERY DEPOSIT", pfc_primary=None)
    rows = txns_mod.deposits(conn, TODAY, days=45)
    assert [d["transaction_id"] for d in rows] == ["nullcat"], \
        "NULL-category deposit hidden (R6 bug)"
    api = c.get("/api/deposits?days=45", auth=AUTH)
    assert [d["transaction_id"] for d in api.json()] == ["nullcat"]


# --- R7: unlinked / ignored accounts are excluded --------------------------------------

def test_r7_apply_rules_ignores_unlinked_and_ignored_accounts(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    item = seed_item(conn)
    unlinked = seed_account(conn, item, "acc-unlinked", "depository")
    ignored = seed_account(conn, item, "acc-ignored", "depository", ignored=1)
    for tid, pa in (("t1", unlinked), ("t2", ignored)):
        conn.execute(
            "INSERT INTO transactions (transaction_id, plaid_account_id, date, amount_cents,"
            " name, pending, updated_at) VALUES (?, ?, ?, -5000, 'ACME PAYROLL', 0, ?)",
            (tid, pa, "2026-10-01", "2026-10-01T00:00:00Z"))
    conn.execute(
        "INSERT INTO deposit_rules (match_text, label, created_at) VALUES ('acme', 'paycheck', ?)",
        ("2026-10-02",))
    conn.commit()
    assert txns_mod.apply_rules(conn) == 0, "unlinked/ignored accounts labelled (R7 bug)"


def test_r7_unlabelled_income_count_and_detect_ignores_unlinked(env):
    c, conn = env
    make_cash_account(c, 100000)
    item = seed_item(conn)
    pa = seed_account(conn, item, "acct-unlinked", "depository")  # no cash_account_id
    seed_txn(conn, pa, "inc1", "2026-09-20", -30000, "MYSTERY", pfc_primary="INCOME")
    seed_txn(conn, pa, "pc1", "2026-08-28", -214988, "ACME PAYROLL", label="paycheck")
    seed_txn(conn, pa, "pc2", "2026-09-11", -214988, "ACME PAYROLL", label="paycheck")
    sts = c.get("/api/safe-to-spend", auth=AUTH).json()
    assert sts["unlabelled_income_count"] == 0, "unlinked account counted (R7 bug)"
    out = c.post("/api/pay-schedules/detect", json={"transaction_ids": None}, auth=AUTH).json()
    assert out["suggestion"] is None, "unlinked paychecks used by detect (R7 bug)"
    assert out["count"] == 0


def test_r7_bill_matching_ignores_unlinked_and_ignored_accounts(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    item = seed_item(conn)
    pa_unlinked = seed_account(conn, item, "acc-un", "credit")  # no card link
    pa_ignored = seed_account(conn, item, "acc-ig", "depository", ignored=1)
    conn.execute(
        "INSERT INTO expenses (name, amount_cents, kind, due_day, match_text)"
        " VALUES ('Electric', 14000, 'bill', 5, 'city power')")
    for tid, pa in (("t1", pa_unlinked), ("t2", pa_ignored)):
        conn.execute(
            "INSERT INTO transactions (transaction_id, plaid_account_id, date, amount_cents,"
            " name, merchant_name, pending, updated_at) VALUES (?, ?, ?, 14000,"
            " 'CITY POWER CO', 'City Power', 0, ?)",
            (tid, pa, f"2026-10-0{5 if tid == 't1' else 4}", "2026-10-01T00:00:00Z"))
    conn.commit()
    inserted = txns_mod.run_bill_matching(conn, TODAY)
    assert inserted == 0, "unlinked/ignored accounts matched (R7 bug)"
    assert db.rows(conn, "SELECT * FROM bill_payments") == []


# --- R8: per-thread connections, no global request lock -------------------------------

def test_r8_threadlocal_connections_are_per_thread(tmp_path):
    proxy = db.init_db(str(tmp_path / "b.db"))
    seen = []

    def grab():
        seen.append(proxy._conn())

    t = threading.Thread(target=grab)
    t.start()
    t.join()
    main_conn = proxy._conn()
    assert main_conn is not seen[0], "connections are not per-thread (R8 bug)"
    # db helpers keep working through the proxy.
    db.insert_row(proxy, "income", {"name": "Pay", "amount_cents": 1000})
    assert db.one(proxy, "SELECT COUNT(*) AS n FROM income")["n"] == 1
    with db.tx(proxy):
        proxy.execute("INSERT INTO income (name, amount_cents) VALUES ('X', 1)")
    assert db.one(proxy, "SELECT COUNT(*) AS n FROM income")["n"] == 2


def test_r8_slow_plaid_does_not_block_other_requests(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet as _F
    from app import plaid as plaidlib
    config = PlaidConfig(client_id="cid", client_secret="cs", env="sandbox",
                         token_key=_F.generate_key(), redirect_uri=None)

    def slow_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/get":
            time.sleep(2)
        return httpx.Response(200, json={"accounts": [], "item": {}})

    client = PlaidClient(config, transport=httpx.MockTransport(slow_handler))
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    monkeypatch.setattr(clock, "now", lambda: NOW)
    app = create_app(str(tmp_path / "budget.db"), "user", "test", plaid=client)
    conn = app.state.db
    with TestClient(app) as c:
        item = seed_item(conn, "SlowBank")
        from app.plaid import encrypt_token
        conn.execute(
            "UPDATE plaid_items SET access_token_enc = ? WHERE id = ?",
            (encrypt_token(config.token_key, "tok"), item))
        conn.commit()
        results = {}

        def do_sync():
            r = c.post("/api/plaid/sync", auth=AUTH)
            results["sync"] = r.status_code

        def timed(path, key):
            start = time.monotonic()
            r = c.get(path, auth=AUTH)
            results[key] = (time.monotonic() - start, r.status_code)

        t1 = threading.Thread(target=do_sync)
        t1.start()
        time.sleep(0.3)  # let the sync enter the slow Plaid call
        t2 = threading.Thread(target=timed, args=("/api/health", "health"))
        t3 = threading.Thread(target=timed, args=("/api/safe-to-spend", "sts"))
        t2.start()
        t3.start()
        t1.join()
        t2.join()
        t3.join()
        assert results["sync"] == 200
        health_dt, health_code = results["health"]
        sts_dt, sts_code = results["sts"]
        assert health_code == 200 and sts_code == 200
        assert health_dt < 1.0, f"/api/health took {health_dt:.2f}s behind the slow sync (R8 bug)"
        assert sts_dt < 1.0, f"/api/safe-to-spend took {sts_dt:.2f}s behind the slow sync (R8 bug)"


# --- R9: pay-schedule listing uses the shared paycheck attribution ----------------------

def test_r9_null_schedule_paycheck_confirms_only_active_schedule(env):
    c, conn = env
    cash_id = make_cash_account(c, 412300)
    item = seed_item(conn)
    pa = seed_account(conn, item, "acct-cash", "depository", cash_account_id=cash_id)
    # NULL pay_schedule_id: attributed to the only active schedule.
    seed_txn(conn, pa, "pc1", "2026-09-25", -214988, "ACME PAYROLL", label="paycheck")
    s = make_schedule(c)
    rows = c.get("/api/pay-schedules", auth=AUTH).json()
    assert rows[0]["deposit_confirmed"] is True, "NULL-schedule paycheck not attributed (R9 bug)"
    assert rows[0]["last_paycheck"] == {"date": "2026-09-25", "amount_cents": 214988}
    # With two active schedules a NULL-schedule paycheck confirms neither.
    make_schedule(c, name="Other", anchor_date="2026-09-26")
    rows = c.get("/api/pay-schedules", auth=AUTH).json()
    assert all(r["deposit_confirmed"] is False for r in rows), \
        "ambiguous NULL-schedule paycheck attributed (R9 bug)"


# --- R10: stale semimonthly days after a frequency change ------------------------------

def test_r10_frequency_change_clears_semimonthly_days(env):
    c, conn = env
    s = make_schedule(c, frequency="semimonthly", anchor_date="2026-10-01",
                      semimonthly_day1=1, semimonthly_day2=15)
    # Switching away: both days are cleared.
    r = c.put(f"/api/pay-schedules/{s['id']}", json={"frequency": "monthly"}, auth=AUTH)
    assert r.status_code == 200
    row = db.get_row(conn, "pay_schedules", s["id"])
    assert (row["semimonthly_day1"], row["semimonthly_day2"]) == (None, None), \
        f"stale days after frequency change (R10 bug): {row}"
    # Switching to semimonthly without both days: 422.
    r = c.put(f"/api/pay-schedules/{s['id']}", json={"frequency": "semimonthly"}, auth=AUTH)
    assert r.status_code == 422, f"expected 422, got {r.status_code} (R10 bug)"


# --- R11: plaid._now uses the clock ------------------------------------------------------

def test_r11_plaid_now_uses_clock(monkeypatch):
    from app import plaid as plaidlib
    monkeypatch.setattr(clock, "now", lambda: datetime(2026, 10, 2, 12, 34, 56, tzinfo=timezone.utc))
    assert plaidlib._now() == "2026-10-02T12:34:56Z", "plaid._now bypasses the clock (R11 bug)"


# --- R12: rule_id, min length, preview, labelled_count, clear_labels ---------------------

def _rule_env(c, conn):
    cash_id = make_cash_account(c, 100000)
    item = seed_item(conn)
    pa = seed_account(conn, item, "acct-cash", "depository", cash_account_id=cash_id)
    return cash_id, pa


def test_r12_rule_id_set_by_rules_cleared_by_manual(env):
    c, conn = env
    cash_id, pa = _rule_env(c, conn)
    t1 = seed_txn(conn, pa, "t1", "2026-09-25", -214988, "ACME PAYROLL", merchant_name="Acme")
    r = c.put(f"/api/transactions/{t1}/label",
              json={"label": "paycheck", "pay_schedule_id": None, "create_rule": True,
                    "rule_text": "acme payroll"}, auth=AUTH)
    assert r.status_code == 200
    manual = db.one(conn, "SELECT * FROM transactions WHERE transaction_id = 't1'")
    assert manual["rule_id"] is None, "manual label did not clear rule_id (R12 bug)"
    t2 = seed_txn(conn, pa, "t2", "2026-09-26", -214988, "ACME PAYROLL", merchant_name="Acme")
    txns_mod.apply_rules(conn)
    ruled = db.one(conn, "SELECT * FROM transactions WHERE transaction_id = 't2'")
    assert ruled["label_source"] == "rule" and ruled["rule_id"] is not None, \
        "apply_rules did not set rule_id (R12 bug)"


def test_r12_rule_text_minimum_four_chars(env):
    c, conn = env
    cash_id, pa = _rule_env(c, conn)
    t1 = seed_txn(conn, pa, "t1", "2026-09-25", -214988, "ACME PAYROLL", merchant_name="Acme")
    r = c.put(f"/api/transactions/{t1}/label",
              json={"label": "paycheck", "pay_schedule_id": None, "create_rule": True,
                    "rule_text": "abc"}, auth=AUTH)
    assert r.status_code == 422, f"expected 422 for short rule text, got {r.status_code} (R12 bug)"
    assert db.rows(conn, "SELECT * FROM deposit_rules") == []


def test_r12_preview_route(env):
    c, conn = env
    cash_id, pa = _rule_env(c, conn)
    seed_txn(conn, pa, "t1", "2026-09-25", -214988, "ACME PAYROLL", merchant_name="Acme")
    seed_txn(conn, pa, "t2", "2026-09-20", -214988, "ACME PAYROLL", merchant_name="Acme")
    seed_txn(conn, pa, "t3", "2026-09-15", -214988, "ACME PAYROLL", merchant_name="Acme")
    seed_txn(conn, pa, "t4", "2026-09-10", -214988, "ACME PAYROLL",
             merchant_name="Acme", label="paycheck")  # already labelled
    r = c.get("/api/deposit-rules/preview", params={"text": "acme", "exclude": "t3"}, auth=AUTH)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["count"] == 2, f"count={out.get('count')} (R12 bug)"
    assert [s["name"] for s in out["samples"]] == ["ACME PAYROLL", "ACME PAYROLL"]  # newest first
    assert out["samples"][0]["date"] == "2026-09-25"
    assert out["samples"][0]["amount_cents"] == 214988
    r = c.get("/api/deposit-rules/preview", params={"text": "abc"}, auth=AUTH)
    assert r.status_code == 422, f"short preview text -> {r.status_code} (R12 bug)"


def test_r12_rules_list_labelled_count_and_clear_labels(env):
    c, conn = env
    cash_id, pa = _rule_env(c, conn)
    t1 = seed_txn(conn, pa, "t1", "2026-09-25", -214988, "ACME PAYROLL", merchant_name="Acme")
    c.put(f"/api/transactions/{t1}/label",
          json={"label": "paycheck", "pay_schedule_id": None, "create_rule": True,
                "rule_text": "acme payroll"}, auth=AUTH)
    t2 = seed_txn(conn, pa, "t2", "2026-09-26", -214988, "ACME PAYROLL", merchant_name="Acme")
    txns_mod.apply_rules(conn)
    rules = c.get("/api/deposit-rules", auth=AUTH).json()
    assert rules[0]["labelled_count"] == 1, f"labelled_count={rules[0].get('labelled_count')} (R12 bug)"
    # Deposit rows carry rule_text when labelled by the rule.
    deps = c.get("/api/deposits?days=45", auth=AUTH).json()
    by_id = {d["transaction_id"]: d for d in deps}
    assert by_id["t2"]["rule_text"] == "acme payroll", \
        f"rule_text={by_id['t2'].get('rule_text')} (R12 bug)"
    assert by_id["t1"]["rule_text"] is None  # manual label
    rid = rules[0]["id"]
    assert c.delete(f"/api/deposit-rules/{rid}", params={"clear_labels": "true"},
                    auth=AUTH).status_code == 204
    t1row = db.one(conn, "SELECT * FROM transactions WHERE transaction_id = 't1'")
    t2row = db.one(conn, "SELECT * FROM transactions WHERE transaction_id = 't2'")
    # t1 was labelled MANUALLY: clear_labels must not touch it (only this
    # rule's own label_source='rule' rows).
    assert (t1row["label"], t1row["label_source"]) == ("paycheck", "manual")
    assert (t2row["label"], t2row["label_source"], t2row["pay_schedule_id"], t2row["rule_id"]) == \
        (None, None, None, None), f"clear_labels left {dict(t2row)} (R12 bug)"
    assert c.get("/api/deposit-rules", auth=AUTH).json() == []


# --- R13: detect count and honest no-pattern reason --------------------------------------

def test_r13_detect_count_and_no_pattern_reason(env):
    c, conn = env
    cash_id = make_cash_account(c, 100000)
    item = seed_item(conn)
    pa = seed_account(conn, item, "acct-cash", "depository", cash_account_id=cash_id)
    seed_txn(conn, pa, "pc1", "2026-08-03", -214988, "ACME PAYROLL", label="paycheck")
    seed_txn(conn, pa, "pc2", "2026-08-27", -214988, "ACME PAYROLL", label="paycheck")
    out = c.post("/api/pay-schedules/detect", json={"transaction_ids": None}, auth=AUTH).json()
    assert out["count"] == 2, f"count missing/wrong: {out} (R13 bug)"
    assert out["suggestion"] is None
    assert out["reason"] == ("2 paychecks don't follow a weekly, every-other-week, "
                             "twice-a-month or monthly pattern"), f"reason={out['reason']} (R13 bug)"
    # One paycheck: count still present, generic reason.
    out = c.post("/api/pay-schedules/detect", json={"transaction_ids": ["pc1"]}, auth=AUTH).json()
    assert out["count"] == 1 and out["reason"] == "need at least 2 paychecks", out


# --- R14: unlogged card payments scoped to the current month ------------------------------

def test_r14_unlogged_card_payments_scoped_and_filtered(env):
    c, conn = env
    cash_id, card_id, pa_cash, pa_card = _seed_bank_with_card(c, conn)
    # Before the current month: flooded the list before the fix.
    seed_txn(conn, pa_card, "old", "2026-09-28", -50000, "AUTOPAY")
    # In-month but an ordinary purchase posted negative is impossible; use
    # category filters: LOAN_PAYMENTS / TRANSFER_IN / NULL qualify.
    seed_txn(conn, pa_card, "keep1", "2026-10-01", -20000, "AUTOPAY")
    seed_txn(conn, pa_card, "keep2", "2026-10-02", -15000, "AUTOPAY", pfc_primary="TRANSFER_IN")
    seed_txn(conn, pa_card, "keep3", "2026-10-02", -10000, "AUTOPAY", pfc_primary="LOAN_PAYMENTS")
    seed_txn(conn, pa_card, "skip1", "2026-10-02", -12000, "REFUND", pfc_primary="FOOD_AND_DRINK")
    rows = c.get("/api/card-payments/unlogged", auth=AUTH).json()
    ids = [r["transaction_id"] for r in rows]
    assert ids == ["keep2", "keep3", "keep1"], f"ids={ids} (R14 bug)"


# --- R15: zero cards are not listed as paid ------------------------------------------------

def test_r15_zero_card_rows_are_skipped():
    zero = {"card_id": 2, "name": "C", "due_day": 7, "remaining_cents": 0,
            "recommended_cents": 0, "paid_cents": 0}
    info = {
        "payday": {"date": date(2026, 10, 9), "schedule_id": 1, "name": "P",
                   "expected_cents": 0, "status": "upcoming"},
        "missed": [], "last_payday": date(2026, 9, 25),
    }
    r = sts_mod.safe_to_spend(
        today=date(2026, 10, 2), now=NOW, payday_info=info, has_schedules=True,
        cash_total_cents=500000, pending_out_cents=0, cash_as_of=NOW,
        cash_floor_cents=0, goals_set_aside_cents=0, bills=[], bill_payments=set(),
        card_rows={"2026-10": [zero]}, spending_monthly_cents=0, extra_warnings=[],
    )
    assert not [o for o in r["obligations"] if o["kind"] == "card"], \
        "zero card listed as paid (R15 bug)"
