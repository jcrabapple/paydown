"""Phase 4 fix round 2 regression tests (docs/FIX4-2.md, S1-S7).

Each test failed against the pre-fix code; AGENTS.md + FIX4-1 rules apply.
"""
from __future__ import annotations

import inspect
import sqlite3
import threading
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import clock, db, paydays
from app.main import create_app
from app.sts import safe_to_spend

AUTH = ("user", "test")
TODAY = date(2026, 10, 2)
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _payday_info(payday=None, missed=None, last_payday=None):
    return {
        "payday": payday,
        "missed": missed or [],
        "last_payday": last_payday,
    }


def _payday(date_, schedule_id=1, name="P", expected=100000, status="upcoming"):
    return {
        "date": date_,
        "schedule_id": schedule_id,
        "name": name,
        "expected_cents": expected,
        "status": status,
    }


def _card_row(card_id=1, name="Capital One", due_day=None, remaining=0,
              recommended=0, paid=0):
    return {
        "card_id": card_id,
        "name": name,
        "due_day": due_day,
        "remaining_cents": remaining,
        "recommended_cents": recommended,
        "paid_cents": paid,
    }


def _sts(today, payday_info, card_rows, **kw):
    defaults = dict(
        today=today,
        now=NOW,
        payday_info=payday_info,
        has_schedules=True,
        cash_total_cents=1000000,
        pending_out_cents=0,
        cash_as_of=NOW,
        cash_floor_cents=0,
        goals_set_aside_cents=0,
        bills=[],
        bill_payments=set(),
        card_rows=card_rows,
        spending_monthly_cents=0,
        extra_warnings=[],
    )
    defaults.update(kw)
    return safe_to_spend(**defaults)


# --- S1 (G2-01): card in TODAY's month due before today is overdue/reserved ----

def test_s1_card_in_current_month_before_today_reserved_when_payday_late():
    # Late payday 2026-10-30 (status late), today 2026-11-05. Card due day 3 ->
    # D = 2026-11-03 < today, in today's month, remaining 50000: overdue and
    # reserved (gate scenario family; 0 reserved was the bug).
    res = _sts(
        date(2026, 11, 5),
        _payday_info(payday=_payday("2026-10-30", status="late")),
        {"2026-11": [_card_row(due_day=3, remaining=50000, recommended=50000)]},
    )
    card_obs = [o for o in res["obligations"] if o["kind"] == "card"]
    assert len(card_obs) == 1
    assert card_obs[0]["status"] == "overdue"
    assert card_obs[0]["amount_cents"] == 50000
    assert res["obligations_total_cents"] == 50000


# --- S2 (G2-04): undated card dedupe ignores fully-paid months ---------------

def test_s2_undated_card_paid_month_does_not_suppress_later_month():
    # 2-month horizon. Month 1 row is fully paid (remaining 0), month 3 row has
    # remaining 50000: the undated card must be reserved from the earliest
    # month with remaining > 0 (50000), not suppressed by the paid month.
    payday_info = _payday_info(payday=_payday("2026-12-01"))
    card_rows = {
        "2026-10": [_card_row(remaining=0, recommended=40000, paid=40000)],
        "2026-11": [_card_row(remaining=0, recommended=40000, paid=40000)],
        "2026-12": [_card_row(remaining=50000, recommended=50000)],
    }
    res = _sts(date(2026, 10, 2), payday_info, card_rows)
    card_obs = [o for o in res["obligations"] if o["kind"] == "card"]
    assert len(card_obs) == 1
    assert card_obs[0]["undated"] is True
    assert card_obs[0]["amount_cents"] == 50000
    assert res["obligations_total_cents"] == 50000


# --- S3 (G2-02): import keeps plaid sync + links ------------------------------

def _plaid_item(conn, item_id="item-1", institution="Bank"):
    return db.insert_row(
        conn, "plaid_items",
        {"item_id": item_id, "access_token_enc": "x", "institution_name": institution,
         "status": "ok", "error_code": None, "last_synced_at": None,
         "created_at": "2026-09-01T00:00:00Z"},
    )["id"]


def _plaid_account(conn, item_id, account_id, type_, card_id=None, cash_account_id=None):
    return db.insert_row(
        conn, "plaid_accounts",
        {"plaid_item_id": item_id, "account_id": account_id, "name": "Acct",
         "official_name": None, "mask": "1111", "card_id": card_id,
         "cash_account_id": cash_account_id, "ignored": 0, "type": type_,
         "subtype": None, "last_balance_cents": None, "last_payload": "{}"},
    )["id"]


def test_s3_import_preserves_plaid_links_and_sync(tmp_path):
    # In-place round trip, as the import endpoint is used in production (the
    # same DB keeps its plaid_items/plaid_accounts rows across the import).
    a = db.init_db(str(tmp_path / "a.db"))
    card = db.create_card(
        a, {"name": "Visa", "balance_cents": 100000, "apr": 0.0,
            "min_payment_cents": 2500, "due_day": 15, "priority": 0}
    )
    a.execute("UPDATE cards SET sync = 'plaid' WHERE id = ?", (card["id"],))
    cash = db.insert_row(
        a, "cash_accounts",
        {"name": "Checking", "kind": "checking", "balance_cents": 50000,
         "include_in_cash": 1, "sync": "manual", "created_at": "2026-01-01"},
    )["id"]
    a.execute("UPDATE cash_accounts SET sync = 'plaid' WHERE id = ?", (cash,))
    item = _plaid_item(a)
    _plaid_account(a, item, "acc-card", "credit", card_id=card["id"])
    _plaid_account(a, item, "acc-cash", "depository", cash_account_id=cash)
    a.commit()
    data = db.export_all(a)

    db.import_all(a, data)

    assert db.get_row(a, "cards", card["id"])["sync"] == "plaid"
    pa = db.one(a, "SELECT * FROM plaid_accounts WHERE account_id = 'acc-card'")
    assert pa["card_id"] == card["id"]
    assert db.get_row(a, "cash_accounts", cash)["sync"] == "plaid"
    pa2 = db.one(a, "SELECT * FROM plaid_accounts WHERE account_id = 'acc-cash'")
    assert pa2["cash_account_id"] == cash


# --- S4 (G2-03): import order derived from FKs; every-table round trip --------

def _populate_every_table(conn) -> None:
    """One row in EVERY exported table, every FK column filled."""
    cash = db.insert_row(
        conn, "cash_accounts",
        {"name": "Checking", "kind": "checking", "balance_cents": 123400,
         "include_in_cash": 1, "sync": "manual", "created_at": "2026-01-01"},
    )["id"]
    card = db.create_card(
        conn, {"name": "Visa", "balance_cents": 90000, "apr": 12.0,
               "min_payment_cents": 2500, "due_day": 15, "priority": 0}
    )["id"]
    sched = db.insert_row(
        conn, "pay_schedules",
        {"name": "Main paycheck", "frequency": "monthly", "anchor_date": "2026-01-25",
         "semimonthly_day1": None, "semimonthly_day2": None, "weekend_rule": "previous",
         "amount_cents": 1000, "cash_account_id": cash, "active": 1,
         "created_at": "2026-01-01T00:00:00Z"},
    )["id"]
    db.insert_row(
        conn, "deposit_rules",
        {"match_text": "acme payroll", "label": "paycheck", "pay_schedule_id": sched,
         "created_at": "2026-01-01T00:00:00Z"},
    )
    db.insert_row(
        conn, "income",
        {"name": "Job", "amount_cents": 200000, "active": 1, "sort": 0,
         "pay_schedule_id": sched},
    )
    expense = db.insert_row(
        conn, "expenses",
        {"name": "Electric", "amount_cents": 14000, "kind": "bill", "due_day": 5,
         "active": 1, "sort": 0, "paid_from": "cash", "match_text": "electric co"},
    )["id"]
    goal = db.insert_row(
        conn, "goals",
        {"name": "Car insurance", "target_cents": 90000, "target_date": "2027-03-01",
         "active": 1, "sort": 0, "created_at": "2026-01-01T00:00:00Z"},
    )["id"]
    db.insert_row(
        conn, "goal_moves",
        {"goal_id": goal, "amount_cents": 15000, "moved_on": "2026-10-01", "note": None},
    )
    db.insert_row(
        conn, "bill_payments",
        {"expense_id": expense, "due_date": "2026-10-05", "paid_on": "2026-10-03",
         "source": "manual", "transaction_id": None},
    )
    db.insert_row(
        conn, "payments",
        {"card_id": card, "month": "2026-10", "paid_on": "2026-10-03",
         "amount_cents": 25000, "note": None, "transaction_id": None,
         "created_at": "2026-10-03T00:00:00Z"},
    )
    db.insert_row(
        conn, "balance_log",
        {"card_id": card, "logged_on": "2026-10-01", "balance_cents": 90000,
         "source": "manual"},
    )
    db.insert_row(
        conn, "month_adjustments",
        {"month": "2026-10", "name": "Extra", "amount_cents": 5000, "kind": "manual"},
    )
    db.update_settings(
        conn, {"strategy": "avalanche", "buffer_cents": 10000,
               "debt_share_pct": 100, "extra_override_cents": None,
               "cash_floor_cents": 5000, "cash_reserve_months": 1.0}
    )


def test_s4_every_table_export_import_round_trip(tmp_path):
    a = db.init_db(str(tmp_path / "a.db"))
    _populate_every_table(a)
    data = db.export_all(a)

    b = db.init_db(str(tmp_path / "b.db"))
    # Must not raise IntegrityError (FK constraint failed was the bug).
    db.import_all(b, data)

    after = db.export_all(b)
    for t in db.TABLES:
        assert after[t] == data[t], f"table {t} did not round-trip"


# --- S5 (G2-05): failed handler leaves no open transaction --------------------

def _new_app(monkeypatch, tmp_path):
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    monkeypatch.setattr(clock, "now", lambda: NOW)
    db_path = str(tmp_path / "s5.db")
    app = create_app(db_path, "user", "test")
    return app, db_path


def test_s5_failed_handler_rolls_back_in_handler_thread(monkeypatch, tmp_path):
    app, db_path = _new_app(monkeypatch, tmp_path)
    threads = {}

    def boom():
        app.state.db.execute(
            "INSERT INTO month_adjustments (month, name, amount_cents, kind)"
            " VALUES ('2026-10', 'x', 100, 'manual')"
        )
        threads["handler"] = threading.get_ident()
        raise RuntimeError("boom")

    app.add_api_route("/api/__s5_boom", boom, methods=["GET"], include_in_schema=False)
    # create_app mounts StaticFiles at "/" last; a route appended after the
    # mount would be shadowed by it. Re-insert the test route before the mount.
    app.router.routes.insert(-1, app.router.routes.pop())

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/api/__s5_boom", auth=AUTH)
    assert r.status_code == 500

    # The rollback cleanup ran in the SAME thread as the handler.
    cleanup_thread = getattr(app.state, "db_cleanup_thread", None)
    assert cleanup_thread is not None, "DB cleanup dependency never ran"
    assert cleanup_thread == threads["handler"]

    # An independent connection can take the write lock immediately: the failed
    # handler left no open transaction behind.
    probe = sqlite3.connect(db_path, timeout=0)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("ROLLBACK")
    finally:
        probe.close()


# --- S6 (G2-06): missed occurrences across the whole paycheck history ---------

def _biweekly_paychecks():
    dates = ["2026-06-05", "2026-06-19", "2026-07-03", "2026-07-17", "2026-07-31"]
    return [
        {"transaction_id": f"t{i}", "date": d, "amount_cents": 200000,
         "pay_schedule_id": 1}
        for i, d in enumerate(dates)
    ]


def test_s6_missed_occurrences_not_limited_to_first_received_in_window():
    sched = {"id": 1, "name": "P", "frequency": "biweekly",
             "anchor_date": "2026-06-05", "semimonthly_day1": None,
             "semimonthly_day2": None, "weekend_rule": "none",
             "amount_cents": 200000, "active": 1}
    # Paychecks ended Jul 31; today Oct 2: Aug 14, Aug 28, Sep 11, Sep 25 are
    # all missed (before the fix, first_recv came from the 62-day window only
    # and missed came back []).
    info = paydays.next_payday(date(2026, 10, 2), [sched], _biweekly_paychecks())
    missed = [m["date"] for m in info["missed"]]
    assert [d.isoformat() for d in missed] == [
        "2026-08-14", "2026-08-28", "2026-09-11", "2026-09-25"
    ]
    # Current candidate unchanged: Oct 9 is still the upcoming payday.
    assert info["payday"]["date"].isoformat() == "2026-10-09"
    assert info["payday"]["status"] == "upcoming"


def test_s6_paychecks_stopped_warning_after_2_missed():
    sched = {"id": 1, "name": "P", "frequency": "biweekly",
             "anchor_date": "2026-06-05", "semimonthly_day1": None,
             "semimonthly_day2": None, "weekend_rule": "none",
             "amount_cents": 200000, "active": 1}
    info = paydays.next_payday(date(2026, 10, 2), [sched], _biweekly_paychecks())
    warnings = paydays.stopped_warnings(info)
    assert len(warnings) == 1
    w = warnings[0]
    assert w["code"] == "paychecks_stopped"
    # Orchestrator contract: "since" names the last paycheck that actually arrived (Jul 31),
    # with the count of consecutive misses after it (Aug 14, Aug 28, Sep 11, Sep 25).
    assert w["message"] == "No P paycheck since Jul 31 (4 missed). Is this schedule still active?"
    assert w["date"] == "2026-07-31"


def test_s6_no_stopped_warning_for_fewer_than_2_missed():
    info = _payday_info(missed=[{"date": "2026-09-25", "schedule_id": 1, "name": "P"}])
    assert paydays.stopped_warnings(info) == []


# --- S7 (G2-07): /api/import is a sync def -----------------------------------

def test_s7_import_is_sync_def_and_keeps_422s(monkeypatch, tmp_path):
    monkeypatch.setattr(clock, "today", lambda: TODAY)
    monkeypatch.setattr(clock, "now", lambda: NOW)
    app = create_app(str(tmp_path / "s7.db"), "user", "test")
    route = next(r for r in app.routes if getattr(r, "path", None) == "/api/import")
    # Sync DB work must not run on the event loop: the endpoint is a plain def.
    assert not inspect.iscoroutinefunction(route.endpoint)

    with TestClient(app) as c:
        r = c.post("/api/import", content=b"not json", auth=AUTH)
        assert r.status_code == 422
        r = c.post("/api/import", json=[1, 2], auth=AUTH)
        assert r.status_code == 422
        # And a valid export still imports.
        r = c.post("/api/import", json=db.export_all(app.state.db), auth=AUTH)
        assert r.status_code == 200
        assert r.json() == {"ok": True}


# --- orchestrator review: "stopped" means consecutive misses since the last receipt ---------

def test_paychecks_stopped_requires_consecutive_misses_after_last_receipt():
    from datetime import date
    from app import paydays
    S = {"id": 1, "name": "P", "frequency": "biweekly", "anchor_date": "2026-07-03",
         "semimonthly_day1": None, "semimonthly_day2": None, "weekend_rule": "none",
         "amount_cents": 1, "active": 1}
    pc = lambda d: {"transaction_id": d, "date": date.fromisoformat(d), "amount_cents": 1, "pay_schedule_id": 1}
    # Biweekly Jul 3 .. Oct 2. Aug 14 and Sep 11 missed, but Aug 28, Sep 25 and Oct 2 arrived:
    # two isolated misses, pay has NOT stopped.
    got = [pc(d) for d in ("2026-07-03", "2026-07-17", "2026-07-31", "2026-08-28", "2026-09-25")]
    info = paydays.next_payday(date(2026, 10, 2), [S], got)
    assert [m["date"].isoformat() for m in info["missed"]] == ["2026-08-14", "2026-09-11"]
    assert paydays.stopped_warnings(info) == []
    # Last receipt Jul 31; Aug 14, Aug 28, Sep 11, Sep 25 all missed -> stopped, message names
    # the last paycheck that actually arrived.
    info2 = paydays.next_payday(date(2026, 10, 2), [S], [pc("2026-07-17"), pc("2026-07-31")])
    w = paydays.stopped_warnings(info2)
    assert len(w) == 1
    assert w[0]["message"] == "No P paycheck since Jul 31 (4 missed). Is this schedule still active?"


def test_g3_01_card_due_after_payday_in_current_month_not_reserved():
    """Gate 3 G3-01: today Oct 2, payday Oct 9; a card due Oct 25 belongs to the next pay
    period and must not be reserved. An overdue card due Oct 1 still is."""
    from datetime import date, datetime, timezone
    from app import sts
    rows = {"2026-10": [
        {"card_id": 1, "name": "Later", "due_day": 25, "remaining_cents": 35000, "recommended_cents": 35000, "paid_cents": 0},
        {"card_id": 2, "name": "Overdue", "due_day": 1, "remaining_cents": 10000, "recommended_cents": 10000, "paid_cents": 0},
        {"card_id": 3, "name": "OnPayday", "due_day": 9, "remaining_cents": 5000, "recommended_cents": 5000, "paid_cents": 0},
    ]}
    r = sts.safe_to_spend(
        today=date(2026, 10, 2), now=datetime(2026, 10, 2, 12, tzinfo=timezone.utc),
        payday_info={"payday": {"date": date(2026, 10, 9), "schedule_id": 1, "name": "P",
                                "expected_cents": 0, "status": "upcoming"},
                     "missed": [], "last_payday": date(2026, 9, 25)},
        has_schedules=True, cash_total_cents=100000, pending_out_cents=0, cash_as_of=None,
        cash_floor_cents=0, goals_set_aside_cents=0, bills=[], bill_payments=set(),
        card_rows=rows, spending_monthly_cents=0, extra_warnings=[])
    by = {o["id"]: o for o in r["obligations"] if o["kind"] == "card"}
    assert 1 not in by                       # due Oct 25, after payday
    assert by[2]["status"] == "overdue"      # 10000
    assert by[3]["status"] == "due"          # due on payday: 5000
    assert r["obligations_total_cents"] == 15000


def test_late_payday_bills_match_cards():
    """Late payday (Oct 30, today Nov 1): bills get the same window as cards.
    Unpaid bill due Oct 31 (between the late payday and today) is overdue and reserved;
    bill due Nov 5 (rest of today's month, next payday unknown) is reserved like cards are."""
    from datetime import date, datetime, timezone
    from app import sts
    bills = [{"id": 1, "name": "Phone", "amount_cents": 8000, "due_day": 31, "paid_from": "cash"},
             {"id": 2, "name": "Water", "amount_cents": 4000, "due_day": 5, "paid_from": "cash"}]
    r = sts.safe_to_spend(
        today=date(2026, 11, 1), now=datetime(2026, 11, 1, 12, tzinfo=timezone.utc),
        payday_info={"payday": {"date": date(2026, 10, 30), "schedule_id": 1, "name": "P",
                                "expected_cents": 0, "status": "late"},
                     "missed": [], "last_payday": date(2026, 10, 16)},
        has_schedules=True, cash_total_cents=100000, pending_out_cents=0, cash_as_of=None,
        cash_floor_cents=0, goals_set_aside_cents=0, bills=bills, bill_payments=set(),
        card_rows={"2026-11": []}, spending_monthly_cents=0, extra_warnings=[])
    got = {(o["id"], o["date"]): (o["status"], o["reserved"]) for o in r["obligations"] if o["kind"] == "bill"}
    assert got[(1, "2026-10-31")] == ("overdue", True)
    assert got[(2, "2026-11-05")] == ("due", True)
    # Gate 4: the window runs to Nov 30, so the day-31 bill also has a Nov 30 occurrence.
    assert got[(1, "2026-11-30")] == ("due", True)
    assert set(got) == {(1, "2026-10-31"), (1, "2026-11-30"), (2, "2026-11-05")}
    assert r["obligations_total_cents"] == 8000 + 8000 + 4000


def test_g4_01_undated_bill_on_late_payday_covers_rest_of_month():
    """Gate 4 G4-01: late payday Oct 30, today Nov 1 (30-day month): an undated $300 bill
    reserves the rest of the month through bill_end (30 days -> $300), dated Nov 30."""
    from datetime import date, datetime, timezone
    from app import sts
    r = sts.safe_to_spend(
        today=date(2026, 11, 1), now=datetime(2026, 11, 1, 12, tzinfo=timezone.utc),
        payday_info={"payday": {"date": date(2026, 10, 30), "schedule_id": 1, "name": "P",
                                "expected_cents": 0, "status": "late"},
                     "missed": [], "last_payday": date(2026, 10, 16)},
        has_schedules=True, cash_total_cents=100000, pending_out_cents=0, cash_as_of=None,
        cash_floor_cents=0, goals_set_aside_cents=0,
        bills=[{"id": 9, "name": "Gas", "amount_cents": 30000, "due_day": None, "paid_from": "cash"}],
        bill_payments=set(), card_rows={"2026-11": []}, spending_monthly_cents=0, extra_warnings=[])
    (o,) = [o for o in r["obligations"] if o["id"] == 9]
    assert o["amount_cents"] == 30000
    assert o["date"] == "2026-11-30"
