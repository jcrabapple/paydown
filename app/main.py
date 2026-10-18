"""FastAPI app factory and routes."""
from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Path as PathParam, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import bills as bills_mod
from . import cardfx
from . import clock
from . import db
from . import goals as goals_mod
from . import insights as insights_mod
from . import paydays
from . import plaid as plaidlib
from . import sts as sts_mod
from . import subscriptions as subs_mod
from . import cash as cashx
from . import txns as txns_mod
from .auth import BasicAuthMiddleware
from .budget import compute_budget
from .engine import compare, this_month_plan
from .models import (
    AdjustmentIn,
    BillPaidIn,
    CardIn,
    ExpenseIn,
    GoalIn,
    GoalMoveIn,
    GoalPatch,
    IncomeIn,
    LabelIn,
    BulkLabelsIn,
    PaymentIn,
    PayScheduleDetectIn,
    PayScheduleIn,
    PaySchedulePatch,
    PlaidExchangeIn,
    PlaidIgnoreIn,
    PlaidLinkIn,
    PlaidLinkTokenIn,
    SettingsIn,
    SubscriptionCancelIn,
    SubscriptionDismissIn,
    SubscriptionIn,
    CashAccountIn,
    UseCashIn,
)

STATIC_DIR = Path(__file__).parent / "static"


def _static_version() -> str:
    """Short sha256 over static assets so /sw.js rotates cache on every change."""
    h = hashlib.sha256()
    if STATIC_DIR.exists():
        for p in sorted(STATIC_DIR.rglob("*")):
            if p.is_file() and p.name != "sw.js":
                h.update(str(p.relative_to(STATIC_DIR)).encode())
                h.update(p.read_bytes())
    return h.hexdigest()[:10]


def current_month() -> str:
    return clock.today().strftime("%Y-%m")


INTEREST_LOOKBACK_DAYS = 100


def _inferred_aprs(conn) -> dict[int, float]:
    """card_id -> APR estimated from recent interest-charge transactions
    (cardfx.infer_apr). Only cards with a linked Plaid account and charges."""
    since = (clock.today() - timedelta(days=INTEREST_LOOKBACK_DAYS)).isoformat()
    by_card: dict[int, list[int]] = {}
    for r in db.rows(
        conn,
        "SELECT pa.card_id AS card_id, t.amount_cents AS cents FROM transactions t "
        "JOIN plaid_accounts pa ON pa.id = t.plaid_account_id "
        "WHERE pa.card_id IS NOT NULL AND t.amount_cents > 0 AND t.date >= ? "
        "AND (t.pfc_detailed = 'BANK_FEES_INTEREST_CHARGE' OR UPPER(t.name) LIKE '%INTEREST CHARGE%') "
        "ORDER BY t.date DESC",
        (since,),
    ):
        lst = by_card.setdefault(r["card_id"], [])
        if len(lst) < 3:
            lst.append(r["cents"])
    out: dict[int, float] = {}
    for card_id, charges in by_card.items():
        card = db.get_row(conn, "cards", card_id)
        if not card:
            continue
        bal = card.get("statement_balance_cents") or card["balance_cents"]
        apr = cardfx.infer_apr(charges, bal)
        if apr is not None:
            out[card_id] = apr
    return out


def _apply_effective(card: dict, inferred: dict[int, float]) -> dict:
    """Copy of a card row with apr/min_payment_cents replaced by the effective
    terms (cardfx.effective) and the sources attached."""
    eff = cardfx.effective(card, inferred.get(card["id"]))
    out = dict(card)
    out["bank_apr"] = card.get("apr")
    out["bank_min_payment_cents"] = card.get("min_payment_cents")
    out["apr"] = eff["apr"]
    out["min_payment_cents"] = eff["min_payment_cents"]
    out["apr_effective_source"] = eff["apr_source"]
    out["min_source"] = eff["min_source"]
    out["apr_inferred"] = inferred.get(card["id"])
    return out


def _card_to_engine(card: dict) -> dict:
    return {
        "id": card["id"],
        "name": card["name"],
        "balance_cents": card["balance_cents"],
        "apr": card["apr"],
        "min_payment_cents": card["min_payment_cents"],
        "priority": card["priority"],
    }


def _card_view(card: dict, conn=None, inferred: dict[int, float] | None = None) -> dict:
    if inferred is None:
        inferred = _inferred_aprs(conn) if conn is not None else {}
    # apr / min_payment_cents are the EFFECTIVE terms; bank_* hold the stored values.
    out = _apply_effective(card, inferred)
    if card["credit_limit_cents"]:
        out["utilization_pct"] = round(card["balance_cents"] / card["credit_limit_cents"] * 100, 1)
    else:
        out["utilization_pct"] = None
    if card["starting_balance_cents"] > 0:
        paid = card["starting_balance_cents"] - card["balance_cents"]
        out["progress_pct"] = round(max(0, paid) / card["starting_balance_cents"] * 100, 1)
    else:
        out["progress_pct"] = 0.0
    out["sync"] = card.get("sync") or "manual"
    out["last_synced_at"] = card.get("last_synced_at")
    out["institution_name"] = None
    out["mask"] = None
    if conn is not None and out["sync"] == "plaid":
        linked = db.one(
            conn,
            "SELECT pi.institution_name AS inst, pa.mask AS mask FROM plaid_accounts pa "
            "JOIN plaid_items pi ON pi.id = pa.plaid_item_id WHERE pa.card_id = ?",
            (card["id"],),
        )
        if linked:
            out["institution_name"] = linked["inst"]
            out["mask"] = linked["mask"]
    return out


def _month_start_cards(conn, month: str) -> list[dict]:
    """Card rows with the balances used by the month plan:
    manual cards follow the B1 rule (balance + payments with month >= M);
    synced cards use the frozen plan_balance snapshot (see plaid.plan_balance).
    Live `cards` rows are not modified; closed cards are still included here
    (budget.py filters them; engine callers filter closed separately)."""
    out = []
    inferred = _inferred_aprs(conn)
    for c in db.list_rows(conn, "cards", "id"):
        c = dict(c)
        c["balance_cents"] = plaidlib.plan_balance(conn, c, month)
        out.append(_apply_effective(c, inferred))
    return out


def _engine_cards(conn, month: str) -> list[dict]:
    """Open (not closed) cards at start-of-month balances, for the engine."""
    return [
        _card_to_engine(c) for c in _month_start_cards(conn, month) if not c["closed"]
    ]


def _goal_target_date(g: dict):
    return date.fromisoformat(g["target_date"]) if g["target_date"] else None


def _subscriptions_monthly(conn) -> int:
    """Monthly-equivalent cost of every active subscription (feeds the plan)."""
    return sum(
        subs_mod.monthly_cents(r["amount_cents"], r["cycle"])
        for r in db.rows(conn, "SELECT amount_cents, cycle FROM subscriptions WHERE status = 'active'")
    )


def _subscription_view(row: dict, today: date) -> dict:
    out = dict(row)
    out["monthly_cents"] = subs_mod.monthly_cents(row["amount_cents"], row["cycle"])
    out["annual_cents"] = subs_mod.annual_cents(row["amount_cents"], row["cycle"])
    if row["status"] == "active":
        nxt = subs_mod.next_renewal(row["next_date"], row["cycle"], today)
        out["next_renewal"] = nxt.isoformat()
        out["days_until"] = (nxt - today).days
    else:
        out["next_renewal"] = None
        out["days_until"] = None
    return out


def _budget_dict(conn, month: str) -> dict:
    # Goals (SAFE.md): monthly need per active goal, using the set-aside
    # balance BEFORE the month so mid-month moves keep the budget stable.
    month_start = f"{month}-01"
    goals_monthly = 0
    for g in db.rows(
        conn, "SELECT * FROM goals WHERE active = 1 ORDER BY id"
    ):
        set_aside_before = _goal_set_aside(conn, g["id"], before=month_start)
        goals_monthly += goals_mod.monthly_need(
            g["target_cents"], set_aside_before, _goal_target_date(g), month
        )
    return compute_budget(
        _income_rows(conn),
        db.list_rows(conn, "expenses"),
        _month_start_cards(conn, month),
        db.rows(conn, "SELECT * FROM month_adjustments WHERE month = ?", (month,)),
        db.get_settings(conn),
        month,
        goals_monthly,
        _subscriptions_monthly(conn),
    )


def _goal_set_aside(conn, goal_id: int, before: str | None = None) -> int:
    """Sum of goal_moves; `before` (ISO date) counts moves strictly before it."""
    sql = "SELECT COALESCE(SUM(amount_cents), 0) AS t FROM goal_moves WHERE goal_id = ?"
    params: tuple = (goal_id,)
    if before is not None:
        sql += " AND moved_on < ?"
        params = (goal_id, before)
    return db.one(conn, sql, params)["t"]


def _goals_set_aside_total(conn) -> int:
    return sum(
        _goal_set_aside(conn, g["id"])
        for g in db.rows(conn, "SELECT id FROM goals WHERE active = 1")
    )


def _income_rows(conn) -> list[dict]:
    """Income rows; rows linked to an active pay schedule use the schedule's
    monthly equivalent as amount_cents (SAFE.md)."""
    out = []
    for r in db.list_rows(conn, "income", "sort, id"):
        r = dict(r)
        sid = r.get("pay_schedule_id")
        if sid:
            sched = db.get_row(conn, "pay_schedules", sid)
            if sched and sched["active"]:
                r["amount_cents"] = paydays.monthly_equivalent_cents(sched)
        out.append(r)
    return out


def _cash_as_of(conn) -> datetime | None:
    """Newest balance timestamp among included cash accounts (aware UTC)."""
    included = [
        a for a in db.list_rows(conn, "cash_accounts", "sort, id") if a["include_in_cash"]
    ]
    timestamps = [
        a["last_synced_at"] if (a.get("sync") or "manual") == "plaid" else a["balance_updated_at"]
        for a in included
    ]
    timestamps = [t for t in timestamps if t]
    if not timestamps:
        return None
    latest = max(timestamps)
    try:
        return datetime.strptime(latest, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.fromisoformat(latest.replace("Z", "+00:00"))
        except ValueError:
            return None


def _pending_out(conn, cash_as_of: datetime | None) -> int:
    """Payments logged after the balance snapshot, not yet at the bank.
    Payments with a transaction_id are already posted at the bank and are
    excluded (SAFE.md)."""
    if cash_as_of is None:
        return 0
    row = db.one(
        conn,
        "SELECT COALESCE(SUM(amount_cents), 0) AS t FROM payments "
        "WHERE created_at > ? AND transaction_id IS NULL",
        (cash_as_of.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    return row["t"]


def _plan_rows(conn, month: str) -> list[dict]:
    """Month plan rows (paid_cents, remaining_cents) plus the card's due_day."""
    budget = _budget_dict(conn, month)
    settings = db.get_settings(conn)
    engine_cards = _engine_cards(conn, month)
    plan = this_month_plan(engine_cards, budget["debt_budget_cents"], settings["strategy"], month)
    payments = db.rows(conn, "SELECT * FROM payments WHERE month = ? ORDER BY id", (month,))
    paid_by_card: dict[int, int] = {}
    for p in payments:
        paid_by_card[p["card_id"]] = paid_by_card.get(p["card_id"], 0) + p["amount_cents"]
    card_rows = {c["id"]: c for c in db.list_rows(conn, "cards", "id")}
    for row in plan:
        paid = paid_by_card.get(row["card_id"], 0)
        c = card_rows.get(row["card_id"]) or {}
        row["paid_cents"] = paid
        row["remaining_cents"] = max(0, row["recommended_cents"] - paid)
        # FIX5: minimum still owed this month (STS reserves this, not the extra).
        row["min_remaining_cents"] = max(0, row["minimum_cents"] - paid)
        row["due_day"] = c.get("due_day")
        row["next_due_date"] = c.get("next_due_date")
        row["bank_overdue"] = bool(c.get("bank_overdue"))
        row["sync"] = c.get("sync") or "manual"
        row["name"] = c.get("name", row["name"])
    return plan


def _rule_text(conn, rule_id: int | None) -> str | None:
    """The deposit rule's match_text for a rule-labelled row (R12)."""
    if rule_id is None:
        return None
    r = db.one(conn, "SELECT match_text FROM deposit_rules WHERE id = ?", (rule_id,))
    return r["match_text"] if r else None


def _active_schedules(conn) -> list[dict]:
    return db.rows(conn, "SELECT * FROM pay_schedules WHERE active = 1 ORDER BY id")


def _schedule_paychecks(conn, schedules: list[dict]) -> list[dict]:
    single = schedules[0]["id"] if len(schedules) == 1 else None
    return txns_mod.paychecks(conn, single)


def _pay_schedule_view(conn, s: dict, paychecks: list[dict] | None = None) -> dict:
    today = clock.today()
    sched = dict(s)
    upcoming = paydays.occurrences(sched, today, today + timedelta(days=90))
    if paychecks is None:
        # R9: same attribution as Safe-to-Spend - txns.paychecks() with the
        # only active schedule's id for NULL-schedule paychecks.
        active = [a for a in db.rows(
            conn, "SELECT * FROM pay_schedules WHERE active = 1 ORDER BY id"
        )]
        paychecks = _schedule_paychecks(conn, active)
    mine = [p for p in paychecks if p.get("pay_schedule_id") == s["id"]]
    confirmed = bool(mine)
    last_paycheck = None
    if confirmed:
        last = max(mine, key=lambda p: p["date"])
        last_paycheck = {"date": last["date"], "amount_cents": last["amount_cents"]}
    return {
        "id": s["id"],
        "name": s["name"],
        "frequency": s["frequency"],
        "anchor_date": s["anchor_date"],
        "semimonthly_day1": s["semimonthly_day1"],
        "semimonthly_day2": s["semimonthly_day2"],
        "weekend_rule": s["weekend_rule"],
        "amount_cents": s["amount_cents"],
        "cash_account_id": s["cash_account_id"],
        "active": bool(s["active"]),
        "monthly_equivalent_cents": paydays.monthly_equivalent_cents(sched),
        "next_dates": [d.isoformat() for d in upcoming[:3]],
        "deposit_confirmed": confirmed,
        "last_paycheck": last_paycheck,
    }


def create_app(db_path: str, user: str = "paydown", password: str = "", plaid=None) -> FastAPI:
    if not password:
        raise RuntimeError("BUDGET_PASS is required and cannot be empty")
    if not user:
        raise RuntimeError("BUDGET_USER cannot be empty")

    conn = db.init_db(db_path)
    # FIX5: backfill bank-reported card terms, then run the automation once
    # (auto-log only when the setting is on; salary auto-labelling always runs,
    # manual and rule labels are never overwritten). No failure here may block
    # boot (review F7): log it and serve the app anyway.
    for step in (
        lambda: plaidlib.refresh_card_bank_fields(conn),
        lambda: db.get_settings(conn).get("auto_log_card_payments", 1)
        and txns_mod.auto_log_card_payments(conn, clock.today()),
        lambda: txns_mod.auto_label_salary(conn),
    ):
        try:
            step()
        except Exception as e:  # noqa: BLE001
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            print(f"startup automation skipped: {type(e).__name__}: {e}", flush=True)

    # S5 (G2-05): a handler that raises mid-write can leave the worker thread's
    # connection inside an open transaction. This app-level yield dependency
    # rolls the current thread's connection back after EVERY route. It runs
    # where the handler ran (same threadpool thread, verified in
    # tests/test_fix4_2.py::test_s5_failed_handler_rolls_back_in_handler_thread)
    # because a middleware would run on the event loop thread and only see the
    # loop thread's own connection.
    def _db_cleanup():
        try:
            yield
        finally:
            c = app.state.db
            if getattr(c, "in_transaction", False):
                c.rollback()
            app.state.db_cleanup_thread = threading.get_ident()

    app = FastAPI(title="Paydown", dependencies=[Depends(_db_cleanup)])
    app.state.db = conn
    app.state.plaid = plaid
    app.add_middleware(BasicAuthMiddleware, user=user, password=password)

    # R8: every thread gets its own sqlite connection (db.ThreadLocalConnection),
    # so requests no longer need to be serialized; Plaid network calls run
    # outside any DB transaction and no longer block other requests.

    # --- health -------------------------------------------------------------
    @app.get("/api/health")
    def health():
        return {"ok": True}

    # --- settings -----------------------------------------------------------
    @app.get("/api/settings")
    def get_settings():
        s = db.get_settings(conn)
        s["auto_log_card_payments"] = bool(s.get("auto_log_card_payments", 1))
        return s

    @app.put("/api/settings")
    def put_settings(body: SettingsIn):
        fields = body.model_dump(exclude_unset=True, exclude_none=False)
        fields = {k: v for k, v in fields.items() if v is not None or k in ("extra_override_cents", "cash_floor_cents")}
        for k, v in list(fields.items()):
            if k in ("buffer_cents", "extra_override_cents", "cash_floor_cents") and v is not None:
                fields[k] = int(v)
            if k == "auto_log_card_payments" and v is not None:
                fields[k] = 1 if v else 0
        result = db.update_settings(conn, fields)
        result["auto_log_card_payments"] = bool(result.get("auto_log_card_payments", 1))
        return result

    # --- income -------------------------------------------------------------
    @app.get("/api/income")
    def list_income():
        return db.list_rows(conn, "income")

    @app.post("/api/income", status_code=201)
    def create_income(body: IncomeIn):
        return db.insert_row(conn, "income", body.model_dump())

    @app.put("/api/income/{item_id}")
    def update_income(item_id: int, body: IncomeIn):
        if not db.get_row(conn, "income", item_id):
            raise HTTPException(404)
        return db.update_row(conn, "income", item_id, body.model_dump())

    @app.delete("/api/income/{item_id}", status_code=204)
    def delete_income(item_id: int):
        if not db.delete_row(conn, "income", item_id):
            raise HTTPException(404)

    # --- expenses -----------------------------------------------------------
    @app.get("/api/expenses")
    def list_expenses():
        return db.list_rows(conn, "expenses")

    @app.post("/api/expenses", status_code=201)
    def create_expense(body: ExpenseIn):
        return db.insert_row(conn, "expenses", body.model_dump())

    @app.put("/api/expenses/{item_id}")
    def update_expense(item_id: int, body: ExpenseIn):
        if not db.get_row(conn, "expenses", item_id):
            raise HTTPException(404)
        return db.update_row(conn, "expenses", item_id, body.model_dump())

    @app.delete("/api/expenses/{item_id}", status_code=204)
    def delete_expense(item_id: int):
        if not db.delete_row(conn, "expenses", item_id):
            raise HTTPException(404)

    # --- cards --------------------------------------------------------------
    @app.get("/api/cards")
    def list_cards():
        inferred = _inferred_aprs(conn)
        return [_card_view(c, conn, inferred) for c in db.list_rows(conn, "cards", "id")]

    @app.post("/api/cards", status_code=201)
    def create_card(body: CardIn):
        if body.name is None or body.balance_cents is None or body.apr is None or body.min_payment_cents is None:
            raise HTTPException(422, "name, balance_cents, apr, min_payment_cents are required")
        return _card_view(
            db.create_card(
                conn,
                {
                    "name": body.name,
                    "balance_cents": body.balance_cents,
                    "apr": body.apr,
                    "min_payment_cents": body.min_payment_cents,
                    "credit_limit_cents": body.credit_limit_cents,
                    "due_day": body.due_day,
                    "priority": body.priority or 0,
                },
            )
        )

    @app.put("/api/cards/{card_id}")
    def update_card(card_id: int, body: CardIn):
        card = db.get_row(conn, "cards", card_id)
        if not card:
            raise HTTPException(404)
        fields = body.model_dump(exclude_unset=True, exclude_none=True)
        # Overrides accept an explicit null (= clear the override).
        for k in ("apr_override", "min_override_cents"):
            if k in body.model_fields_set:
                fields[k] = getattr(body, k)
        balance_changed = (
            "balance_cents" in fields and fields["balance_cents"] != card["balance_cents"]
        )
        updated = db.update_row(conn, "cards", card_id, fields)
        if balance_changed:
            updated = db.set_card_balance(conn, card_id, fields["balance_cents"])
        return _card_view(updated, conn)

    @app.delete("/api/cards/{card_id}", status_code=204)
    def delete_card(card_id: int):
        if not db.delete_row(conn, "cards", card_id):
            raise HTTPException(404)

    @app.get("/api/cards/{card_id}/history")
    def card_history(card_id: int):
        if not db.get_row(conn, "cards", card_id):
            raise HTTPException(404)
        return db.rows(
            conn,
            "SELECT * FROM balance_log WHERE card_id = ? ORDER BY id",
            (card_id,),
        )

    # --- payments -----------------------------------------------------------
    @app.get("/api/payments")
    def list_payments(month: str | None = Query(default=None)):
        if month is not None:
            return db.rows(conn, "SELECT * FROM payments WHERE month = ? ORDER BY id", (month,))
        return db.rows(conn, "SELECT * FROM payments ORDER BY id")

    @app.post("/api/payments", status_code=201)
    def create_payment(body: PaymentIn):
        card = db.get_row(conn, "cards", body.card_id)
        if not card:
            raise HTTPException(404, "card not found")
        month = body.month or body.paid_on[:7]
        payment = db.insert_row(
            conn,
            "payments",
            {
                "card_id": body.card_id,
                "month": month,
                "paid_on": body.paid_on,
                "amount_cents": body.amount_cents,
                "note": body.note,
                "created_at": plaidlib._now(),
            },
        )
        if (card.get("sync") or "manual") != "plaid":
            # Synced cards: the bank balance is authoritative; the payment row
            # is recorded but must not touch cards.balance_cents (it would
            # double count once the payment posts).
            db.apply_payment(conn, body.card_id, body.amount_cents)
        return payment

    @app.delete("/api/payments/{payment_id}", status_code=204)
    def delete_payment(payment_id: int):
        payment = db.get_row(conn, "payments", payment_id)
        if not payment:
            raise HTTPException(404)
        db.delete_row(conn, "payments", payment_id)
        if payment.get("transaction_id"):
            # FIX5: a deleted bank payment must not be auto-logged again on the
            # next sync/startup; dismiss its transaction (still loggable by hand).
            conn.execute(
                "UPDATE transactions SET label = 'dismissed', label_source = 'manual' "
                "WHERE transaction_id = ?",
                (payment["transaction_id"],),
            )
            conn.commit()
        card = db.get_row(conn, "cards", payment["card_id"])
        if not card or (card.get("sync") or "manual") != "plaid":
            db.revert_payment(conn, payment["card_id"], payment["amount_cents"])

    # --- adjustments --------------------------------------------------------
    @app.get("/api/adjustments")
    def list_adjustments(month: str | None = Query(default=None)):
        if month is not None:
            return db.rows(
                conn, "SELECT * FROM month_adjustments WHERE month = ? ORDER BY id", (month,)
            )
        return db.rows(conn, "SELECT * FROM month_adjustments ORDER BY id")

    @app.post("/api/adjustments", status_code=201)
    def create_adjustment(body: AdjustmentIn):
        return db.insert_row(
            conn,
            "month_adjustments",
            {
                "month": body.month,
                "name": body.name,
                "amount_cents": body.amount_cents,
            },
        )

    @app.delete("/api/adjustments/{item_id}", status_code=204)
    def delete_adjustment(item_id: int):
        if not db.delete_row(conn, "month_adjustments", item_id):
            raise HTTPException(404)

    # --- cash accounts -------------------------------------------------------
    def _cash_account_view(a: dict) -> dict:
        out = {
            "id": a["id"],
            "name": a["name"],
            "kind": a["kind"],
            "balance_cents": a["balance_cents"],
            "include_in_cash": bool(a["include_in_cash"]),
            "sync": a.get("sync") or "manual",
            "last_synced_at": a.get("last_synced_at"),
            "institution_name": None,
            "mask": None,
        }
        if out["sync"] == "plaid":
            linked = db.one(
                conn,
                "SELECT pi.institution_name AS inst, pa.mask AS mask FROM plaid_accounts pa "
                "JOIN plaid_items pi ON pi.id = pa.plaid_item_id WHERE pa.cash_account_id = ?",
                (a["id"],),
            )
            if linked:
                out["institution_name"] = linked["inst"]
                out["mask"] = linked["mask"]
        return out

    @app.get("/api/cash-accounts")
    def list_cash_accounts():
        return [_cash_account_view(a) for a in db.list_rows(conn, "cash_accounts", "sort, id")]

    @app.post("/api/cash-accounts", status_code=201)
    def create_cash_account(body: CashAccountIn):
        if body.name is None or body.balance_cents is None:
            raise HTTPException(422, "name and balance_cents are required")
        now = plaidlib._now()
        return _cash_account_view(
            db.insert_row(
                conn,
                "cash_accounts",
                {
                    "name": body.name,
                    "kind": body.kind or "checking",
                    "balance_cents": body.balance_cents,
                    "include_in_cash": 1 if (body.include_in_cash is None or body.include_in_cash) else 0,
                    "sync": "manual",
                    "balance_updated_at": now,
                    "created_at": now,
                    "sort": body.sort or 0,
                },
            )
        )

    @app.put("/api/cash-accounts/{account_id}")
    def update_cash_account(account_id: int, body: CashAccountIn):
        acct = db.get_row(conn, "cash_accounts", account_id)
        if not acct:
            raise HTTPException(404)
        fields = body.model_dump(exclude_unset=True, exclude_none=True)
        if "include_in_cash" in fields:
            fields["include_in_cash"] = 1 if fields["include_in_cash"] else 0
        if "balance_cents" in fields and fields["balance_cents"] != acct["balance_cents"]:
            if (acct.get("sync") or "manual") == "plaid":
                raise HTTPException(409, "Balance comes from your bank")
            fields["balance_updated_at"] = plaidlib._now()
        updated = db.update_row(conn, "cash_accounts", account_id, fields)
        return _cash_account_view(updated)

    @app.delete("/api/cash-accounts/{account_id}", status_code=204)
    def delete_cash_account(account_id: int):
        if not db.delete_row(conn, "cash_accounts", account_id):
            raise HTTPException(404)

    # --- cash bundle helper ---------------------------------------------------
    def _cash_bundle(month: str, budget: dict, plan: list[dict]) -> dict | None:
        accounts = db.list_rows(conn, "cash_accounts", "sort, id")
        if not accounts:
            return None
        included = [a for a in accounts if a["include_in_cash"]]
        cash_total = sum(a["balance_cents"] for a in included)
        cash_as_of = _cash_as_of(conn)
        pending_out = _pending_out(conn, cash_as_of)
        effective_cash = cash_total - pending_out
        goals_set_aside = _goals_set_aside_total(conn)
        pos = cashx.cash_position(
            accounts, db.get_settings(conn), budget["essentials_cents"], plan,
            effective_cash_cents=effective_cash,
            goals_set_aside_cents=goals_set_aside,
        )
        is_current = month == current_month()
        pos.update(
            {
                "is_current_month": is_current,
                "accounts": [_cash_account_view(a) for a in accounts],
            }
        )
        if not is_current:
            pos["spare_cents"] = None
            pos["months_covered"] = None
        pos["applied_cents"] = budget["cash_applied_cents"]
        return pos

    # --- month bundle -------------------------------------------------------
    @app.get("/api/month/{month}")
    def month_bundle(month: str = PathParam(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
        budget = _budget_dict(conn, month)
        settings = db.get_settings(conn)
        cards = db.list_rows(conn, "cards", "id")
        engine_cards = _engine_cards(conn, month)
        plan = _plan_rows(conn, month)
        # Projection: recurring budget every month; month 1 uses this month's
        # actual debt budget (one-off adjustments and cash sweeps, CASH.md).
        projection = compare(
            engine_cards, budget["recurring_debt_budget_cents"], month, settings["strategy"],
            first_month_budget_cents=budget["debt_budget_cents"],
        )
        projection["avalanche"]["schedule"] = projection["avalanche"]["schedule"][:120]
        projection["snowball"]["schedule"] = projection["snowball"]["schedule"][:120]
        projection["custom"]["schedule"] = projection["custom"]["schedule"][:120]
        projection["minimums_only"]["schedule"] = projection["minimums_only"]["schedule"][:120]
        return {
            "month": month,
            "budget": budget,
            "settings": settings,
            "plan": plan,
            "paid_total_cents": sum(r["paid_cents"] for r in plan),
            "recommended_total_cents": sum(r["recommended_cents"] for r in plan),
            "projection": projection,
            "cash": _cash_bundle(month, budget, plan),
            "cards": [_card_view(c, conn) for c in cards],
        }

    @app.post("/api/month/{month}/use-cash")
    def use_cash(month: str = PathParam(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), body: UseCashIn | None = None):
        if month != current_month():
            raise HTTPException(409, "use-cash applies to the current month only")
        budget = _budget_dict(conn, month)
        settings = db.get_settings(conn)
        cards = db.list_rows(conn, "cards", "id")
        engine_cards = _engine_cards(conn, month)
        plan = this_month_plan(engine_cards, budget["debt_budget_cents"], settings["strategy"], month)
        payments = db.rows(conn, "SELECT * FROM payments WHERE month = ? ORDER BY id", (month,))
        paid_by_card: dict[int, int] = {}
        for p in payments:
            paid_by_card[p["card_id"]] = paid_by_card.get(p["card_id"], 0) + p["amount_cents"]
        for row in plan:
            paid = paid_by_card.get(row["card_id"], 0)
            row["paid_cents"] = paid
            row["remaining_cents"] = max(0, row["recommended_cents"] - paid)
        cash = _cash_bundle(month, budget, plan)
        spare = cash["spare_cents"] if cash else 0
        amount = body.amount_cents if body is not None else None
        if amount is None:
            amount = spare
        if not (0 < amount <= spare):
            raise HTTPException(422, "amount must be between 1 and the spare cash")
        db.insert_row(
            conn,
            "month_adjustments",
            {"month": month, "name": "Spare cash from checking",
             "amount_cents": amount, "kind": "cash"},
        )
        return month_bundle(month)

    # --- projection ---------------------------------------------------------
    @app.get("/api/projection")
    def projection(
        extra: int | None = Query(default=None, ge=0, le=10**10),
        strategy: str | None = Query(default=None),
        lump: int = Query(default=0, ge=0, le=10**10),
        month: str | None = Query(default=None),
    ):
        m = month or current_month()
        budget = _budget_dict(conn, m)
        settings = db.get_settings(conn)
        strat = strategy or settings["strategy"]
        if strat not in ("avalanche", "snowball", "custom"):
            raise HTTPException(422, "invalid strategy")
        extra_cents = extra if extra is not None else None
        cards = _engine_cards(conn, m)
        # Recurring budget every month; month 1 = recurring budget + this
        # month's one-off delta (adjustments + sweeps). An explicit `extra`
        # replaces the recurring extra (CASH.md).
        one_off_delta = budget["debt_budget_cents"] - budget["recurring_debt_budget_cents"]
        if extra_cents is None:
            recurring_budget = budget["recurring_debt_budget_cents"]
            first_budget = budget["debt_budget_cents"]
        else:
            recurring_budget = budget["minimums_total_cents"] + extra_cents
            first_budget = recurring_budget + one_off_delta
        return compare(cards, recurring_budget, m, strat, lump,
                       first_month_budget_cents=first_budget)
    # --- plaid ---------------------------------------------------------------
    def _require_plaid():
        if plaid is None:
            raise HTTPException(503, "Plaid is not configured")

    def _item_out(it: dict) -> dict:
        n = db.one(
            conn, "SELECT COUNT(*) AS n FROM plaid_accounts WHERE plaid_item_id = ?", (it["id"],)
        )["n"]
        return {
            "id": it["id"],
            "institution_name": it["institution_name"],
            "status": it["status"],
            "error_code": it["error_code"],
            "last_synced_at": it["last_synced_at"],
            "accounts": n,
        }

    def _account_out(a: dict) -> dict:
        joined = db.one(
            conn,
            "SELECT pa.*, pi.institution_name FROM plaid_accounts pa "
            "JOIN plaid_items pi ON pi.id = pa.plaid_item_id WHERE pa.id = ?",
            (a["id"],),
        )
        a = joined or a
        card_name = None
        if a["card_id"]:
            card = db.get_row(conn, "cards", a["card_id"])
            card_name = card["name"] if card else None
        cash_account_name = None
        if a["cash_account_id"]:
            ca = db.get_row(conn, "cash_accounts", a["cash_account_id"])
            cash_account_name = ca["name"] if ca else None
        try:
            payload = json.loads(a["last_payload"] or "{}")
        except (ValueError, TypeError):
            payload = {}
        existing = db.get_row(conn, "cards", a["card_id"]) if a["card_id"] else None
        _, apr_source = plaidlib._pick_apr(payload.get("credit"), existing)
        return {
            "id": a["id"],
            "plaid_item_id": a["plaid_item_id"],
            "institution_name": a["institution_name"],
            "name": a["name"],
            "official_name": a["official_name"],
            "mask": a["mask"],
            "type": a.get("type"),
            "subtype": a.get("subtype"),
            "card_id": a["card_id"],
            "card_name": card_name,
            "cash_account_id": a.get("cash_account_id"),
            "cash_account_name": cash_account_name,
            "ignored": bool(a["ignored"]),
            "last_balance_cents": a["last_balance_cents"],
            "apr_source": apr_source,
        }

    @app.get("/api/plaid/status")
    def plaid_status():
        if plaid is None:
            return {"configured": False, "env": None, "items": []}
        return {
            "configured": True,
            "env": plaid.config.env,
            "items": [_item_out(it) for it in db.list_rows(conn, "plaid_items")],
        }

    @app.post("/api/plaid/link-token", dependencies=[Depends(_require_plaid)])
    def plaid_link_token(body: PlaidLinkTokenIn):
        _require_plaid()
        access_token = None
        if body.item_id is not None:
            it = db.get_row(conn, "plaid_items", body.item_id)
            if not it:
                raise HTTPException(404, "item not found")
            access_token = plaid.decrypt_token(it["access_token_enc"])
        try:
            out = plaid.link_token_create(access_token)
        except plaidlib.PlaidError as e:
            raise HTTPException(502, f"Plaid error: {e.error_code} {e.message}")
        return {"link_token": out["link_token"], "expiration": out.get("expiration")}

    @app.post("/api/plaid/exchange", dependencies=[Depends(_require_plaid)])
    def plaid_exchange(body: PlaidExchangeIn):
        _require_plaid()
        try:
            out = plaid.exchange(body.public_token)
        except plaidlib.PlaidError as e:
            raise HTTPException(502, f"Plaid error: {e.error_code} {e.message}")
        enc = plaid.encrypt_token(out["access_token"])
        existing = db.one(conn, "SELECT * FROM plaid_items WHERE item_id = ?", (out["item_id"],))
        if existing:
            it = db.update_row(conn, "plaid_items", existing["id"], {"access_token_enc": enc})
        else:
            it = db.insert_row(
                conn,
                "plaid_items",
                {
                    "item_id": out["item_id"],
                    "access_token_enc": enc,
                    "institution_name": body.institution_name,
                    "status": "ok",
                    "error_code": None,
                    "last_synced_at": None,
                    "created_at": plaidlib._now(),
                },
            )
        plaidlib.sync_item(conn, plaid, it)
        return _item_out(db.get_row(conn, "plaid_items", it["id"]))

    @app.post("/api/plaid/sync", dependencies=[Depends(_require_plaid)])
    def plaid_sync():
        _require_plaid()
        return plaidlib.sync_all(conn, plaid)

    @app.get("/api/plaid/accounts", dependencies=[Depends(_require_plaid)])
    def plaid_accounts():
        _require_plaid()
        rows = db.rows(
            conn,
            "SELECT pa.*, pi.institution_name FROM plaid_accounts pa "
            "JOIN plaid_items pi ON pi.id = pa.plaid_item_id ORDER BY pa.id",
        )
        return [_account_out(a) for a in rows]

    @app.post("/api/plaid/accounts/{account_id}/link", dependencies=[Depends(_require_plaid)])
    def plaid_link_account(account_id: int, body: PlaidLinkIn):
        _require_plaid()
        acct = db.get_row(conn, "plaid_accounts", account_id)
        if not acct:
            raise HTTPException(404, "account not found")
        acct_type = acct.get("type") or "credit"

        def _payload():
            try:
                return json.loads(acct["last_payload"] or "{}")
            except (ValueError, TypeError):
                return {}

        if body.card_id is not None or (body.create and acct_type == "credit"):
            if acct_type != "credit":
                raise HTTPException(422, "card pairing is only valid for credit accounts")
            if body.create:
                mapped = plaidlib.map_liability(_payload().get("account"), _payload().get("credit"), None)
                card = db.create_card(
                    conn,
                    {
                        "name": acct["official_name"] or acct["name"],
                        "balance_cents": acct["last_balance_cents"] or 0,
                        "apr": mapped.get("apr") or 0.0,
                        "min_payment_cents": mapped.get("min_payment_cents") or 0,
                        "credit_limit_cents": mapped.get("credit_limit_cents"),
                        "due_day": mapped.get("due_day"),
                    },
                )
                card_id = card["id"]
            else:
                if not db.get_row(conn, "cards", body.card_id):
                    raise HTTPException(404, "card not found")
                card_id = body.card_id
            # A card can be linked to at most one plaid account.
            conn.execute(
                "UPDATE plaid_accounts SET card_id = NULL WHERE card_id = ? AND id != ?",
                (card_id, account_id),
            )
            conn.commit()
            db.update_row(conn, "plaid_accounts", account_id, {"card_id": card_id})
            plaidlib._apply_account_to_card(conn, db.get_row(conn, "plaid_accounts", account_id), None)
        elif body.cash_account_id is not None or (body.create and acct_type == "depository"):
            if acct_type != "depository":
                raise HTTPException(422, "cash_account pairing is only valid for depository accounts")
            if body.create:
                subtype = acct.get("subtype") or "checking"
                now = plaidlib._now()
                ca = db.insert_row(
                    conn,
                    "cash_accounts",
                    {
                        "name": acct["official_name"] or acct["name"],
                        "kind": subtype if subtype in ("checking", "savings", "other") else "other",
                        "balance_cents": acct["last_balance_cents"] or 0,
                        "include_in_cash": 1 if subtype == "checking" else 0,
                        "sync": "plaid",
                        "last_synced_at": now,
                        "balance_updated_at": now,
                        "created_at": now,
                        "sort": 0,
                    },
                )
                cash_id = ca["id"]
            else:
                if not db.get_row(conn, "cash_accounts", body.cash_account_id):
                    raise HTTPException(404, "cash account not found")
                cash_id = body.cash_account_id
            # A cash account can be linked to at most one plaid account.
            conn.execute(
                "UPDATE plaid_accounts SET cash_account_id = NULL WHERE cash_account_id = ? AND id != ?",
                (cash_id, account_id),
            )
            conn.commit()
            db.update_row(conn, "plaid_accounts", account_id, {"cash_account_id": cash_id})
            plaidlib._apply_account_to_cash(conn, db.get_row(conn, "plaid_accounts", account_id))
        else:
            raise HTTPException(422, "card_id, cash_account_id or create is required")
        return _account_out(db.get_row(conn, "plaid_accounts", account_id))

    @app.post("/api/plaid/accounts/{account_id}/unlink", dependencies=[Depends(_require_plaid)])
    def plaid_unlink_account(account_id: int):
        _require_plaid()
        acct = db.get_row(conn, "plaid_accounts", account_id)
        if not acct:
            raise HTTPException(404, "account not found")
        if acct["card_id"]:
            db.update_row(conn, "cards", acct["card_id"], {"sync": "manual"})
        if acct.get("cash_account_id"):
            db.update_row(
                conn, "cash_accounts", acct["cash_account_id"],
                {"sync": "manual", "balance_updated_at": plaidlib._now()},
            )
        db.update_row(conn, "plaid_accounts", account_id, {"card_id": None, "cash_account_id": None})
        return _account_out(db.get_row(conn, "plaid_accounts", account_id))

    @app.post("/api/plaid/accounts/{account_id}/ignore", dependencies=[Depends(_require_plaid)])
    def plaid_ignore_account(account_id: int, body: PlaidIgnoreIn):
        _require_plaid()
        acct = db.get_row(conn, "plaid_accounts", account_id)
        if not acct:
            raise HTTPException(404, "account not found")
        db.update_row(conn, "plaid_accounts", account_id, {"ignored": 1 if body.ignored else 0})
        return _account_out(db.get_row(conn, "plaid_accounts", account_id))

    # --- combined accounts (frontend Today/Budget/Month) ----------------------
    @app.get("/api/accounts")
    def list_accounts():
        out = []
        for a in db.list_rows(conn, "cash_accounts", "sort, id"):
            out.append(
                {
                    "id": a["id"],
                    "kind": "cash",
                    "name": a["name"],
                    "balance_cents": a["balance_cents"],
                    "include_in_cash": bool(a["include_in_cash"]),
                    "sync": a.get("sync") or "manual",
                    "last_synced_at": a.get("last_synced_at"),
                }
            )
        for c in db.list_rows(conn, "cards", "id"):
            out.append(
                {
                    "id": c["id"],
                    "kind": "card",
                    "name": c["name"],
                    "balance_cents": c["balance_cents"],
                    "sync": c.get("sync") or "manual",
                    "last_synced_at": c.get("last_synced_at"),
                }
            )
        return out

    # --- pay schedules --------------------------------------------------------
    @app.get("/api/pay-schedules")
    def list_pay_schedules():
        active = [s for s in db.list_rows(conn, "pay_schedules", "id") if s["active"]]
        pcs = _schedule_paychecks(conn, active)
        return [
            _pay_schedule_view(conn, s, pcs)
            for s in db.list_rows(conn, "pay_schedules", "id")
        ]

    def _check_cash_account(account_id):
        if account_id is not None and not db.get_row(conn, "cash_accounts", account_id):
            raise HTTPException(422, "cash_account_id not found")

    @app.post("/api/pay-schedules", status_code=201)
    def create_pay_schedule(body: PayScheduleIn):
        try:
            body.check_semimonthly()
        except ValueError as e:
            raise HTTPException(422, str(e))
        _check_cash_account(body.cash_account_id)
        fields = body.model_dump()
        fields["active"] = 1 if body.active else 0
        fields["created_at"] = clock.today().isoformat()
        return _pay_schedule_view(conn, db.insert_row(conn, "pay_schedules", fields))

    @app.put("/api/pay-schedules/{schedule_id}")
    def update_pay_schedule(schedule_id: int, body: PaySchedulePatch):
        s = db.get_row(conn, "pay_schedules", schedule_id)
        if not s:
            raise HTTPException(404)
        fields = body.model_dump(exclude_unset=True, exclude_none=False)
        fields = {k: v for k, v in fields.items() if v is not None or k in ("semimonthly_day1", "semimonthly_day2", "cash_account_id")}
        merged = {**s, **{k: v for k, v in fields.items()}}
        if merged["frequency"] != "semimonthly":
            if "semimonthly_day1" in fields or "semimonthly_day2" in fields:
                raise HTTPException(422, "semimonthly days are only valid for semimonthly frequency")
            # R10: a frequency change away from semimonthly must not leave the
            # old day1/day2 values behind.
            if merged["semimonthly_day1"] is not None or merged["semimonthly_day2"] is not None:
                fields["semimonthly_day1"] = None
                fields["semimonthly_day2"] = None
                merged["semimonthly_day1"] = None
                merged["semimonthly_day2"] = None
        else:
            # R10: becoming semimonthly requires both days.
            if merged["semimonthly_day1"] is None or merged["semimonthly_day2"] is None:
                raise HTTPException(422, "semimonthly requires both days")
            if merged["semimonthly_day1"] >= merged["semimonthly_day2"]:
                raise HTTPException(422, "semimonthly requires day1 < day2")
        if "cash_account_id" in fields:
            _check_cash_account(fields["cash_account_id"])
        if "active" in fields:
            fields["active"] = 1 if fields["active"] else 0
        return _pay_schedule_view(conn, db.update_row(conn, "pay_schedules", schedule_id, fields))

    @app.delete("/api/pay-schedules/{schedule_id}", status_code=204)
    def delete_pay_schedule(schedule_id: int):
        if not db.delete_row(conn, "pay_schedules", schedule_id):
            raise HTTPException(404)

    @app.get("/api/pay-schedules/{schedule_id}/upcoming")
    def pay_schedule_upcoming(schedule_id: int, count: int = Query(default=6, ge=1, le=52)):
        s = db.get_row(conn, "pay_schedules", schedule_id)
        if not s:
            raise HTTPException(404)
        today = clock.today()
        dates = paydays.occurrences(dict(s), today, today + timedelta(days=370))
        return {"dates": [d.isoformat() for d in dates[:count]]}

    @app.post("/api/pay-schedules/detect")
    def pay_schedule_detect(body: PayScheduleDetectIn):
        today = clock.today()
        # R7: only linked, non-ignored cash accounts feed detection.
        linked = (" JOIN plaid_accounts pa ON pa.id = t.plaid_account_id"
                  " AND pa.type = 'depository' AND pa.cash_account_id IS NOT NULL"
                  " AND pa.ignored = 0")
        if body.transaction_ids is None:
            rows = db.rows(
                conn,
                "SELECT date, amount_cents FROM transactions t" + linked +
                " WHERE t.label = 'paycheck' AND t.pending = 0 AND t.amount_cents < 0 "
                "AND t.date >= ? ORDER BY date",
                ((today - timedelta(days=120)).isoformat(),),
            )
        else:
            if not body.transaction_ids:
                rows = []
            else:
                qs = ", ".join("?" for _ in body.transaction_ids)
                rows = db.rows(
                    conn,
                    "SELECT t.date, t.amount_cents FROM transactions t" + linked +
                    " WHERE t.label = 'paycheck' "
                    "AND t.pending = 0 AND t.amount_cents < 0 AND t.transaction_id IN (%s) "
                    "ORDER BY t.date" % qs,
                    tuple(body.transaction_ids),
                )
        count = len(rows)
        suggestion = paydays.infer_schedule(
            [r["date"] for r in rows], [-r["amount_cents"] for r in rows]
        )
        if suggestion is None:
            # R13: an honest reason, with the count always present.
            if count >= 2:
                reason = (
                    f"{count} paychecks don't follow a weekly, every-other-week, "
                    "twice-a-month or monthly pattern"
                )
            else:
                reason = "need at least 2 paychecks"
            return {"suggestion": None, "reason": reason, "count": count}
        suggestion["anchor_date"] = suggestion["anchor_date"].isoformat()
        suggestion["dates_used"] = [d.isoformat() for d in suggestion["dates_used"]]
        return {"suggestion": suggestion, "reason": None, "count": count}

    @app.post("/api/pay-schedules/{schedule_id}/use-as-income")
    def pay_schedule_use_as_income(schedule_id: int):
        s = db.get_row(conn, "pay_schedules", schedule_id)
        if not s:
            raise HTTPException(404)
        existing = db.one(conn, "SELECT * FROM income WHERE pay_schedule_id = ?", (schedule_id,))
        if existing:
            return existing
        return db.insert_row(
            conn,
            "income",
            {
                "name": s["name"],
                "amount_cents": s["amount_cents"],
                "active": 1,
                "sort": 0,
                "pay_schedule_id": schedule_id,
            },
        )

    # --- deposits, labels, rules ----------------------------------------------
    @app.get("/api/deposits")
    def list_deposits(
        days: int = Query(default=45, ge=1, le=365),
        include_transfers: bool = Query(default=False),
    ):
        rows = txns_mod.deposits(conn, clock.today(), days=days, include_transfers=include_transfers)
        rule_ids = {
            r["transaction_id"]: r["rid"]
            for r in db.rows(
                conn,
                "SELECT transaction_id, rule_id AS rid FROM transactions WHERE label_source = 'rule' "
                f"AND transaction_id IN ({', '.join('?' for _ in rows) or 'NULL'})",
                tuple(r["transaction_id"] for r in rows),
            )
        }
        for r in rows:
            r["pay_schedule_name"] = None
            if r["pay_schedule_id"]:
                s = db.get_row(conn, "pay_schedules", r["pay_schedule_id"])
                r["pay_schedule_name"] = s["name"] if s else None
            r["rule_text"] = _rule_text(conn, rule_ids.get(r["transaction_id"]))  # R12
        return rows

    @app.put("/api/transactions/{transaction_id}/label")
    def set_txn_label(transaction_id: str, body: LabelIn):
        row = db.one(
            conn,
            "SELECT t.*, pa.type AS account_type FROM transactions t "
            "JOIN plaid_accounts pa ON pa.id = t.plaid_account_id "
            "WHERE t.transaction_id = ?",
            (transaction_id,),
        )
        if not row:
            raise HTTPException(404, "transaction not found")
        if not (row["amount_cents"] < 0 and row["account_type"] == "depository"):
            raise HTTPException(422, "only deposits on linked cash accounts can be labelled")
        if body.pay_schedule_id is not None and not db.get_row(
            conn, "pay_schedules", body.pay_schedule_id
        ):
            raise HTTPException(422, "pay_schedule_id not found")
        try:
            txns_mod.set_label(
                conn, transaction_id, body.label, body.pay_schedule_id,
                create_rule=body.create_rule, rule_text=body.rule_text,
            )
        except ValueError as e:
            # R12: rule text below the 4-character minimum.
            raise HTTPException(422, str(e))
        r = db.one(
            conn,
            "SELECT t.transaction_id, t.date, t.amount_cents, t.name, t.merchant_name, "
            "t.pending, t.pfc_primary, t.pfc_detailed, t.label, t.label_source, "
            "t.pay_schedule_id, t.rule_id, pa.name AS account_name, pa.mask, pa.cash_account_id "
            "FROM transactions t JOIN plaid_accounts pa ON pa.id = t.plaid_account_id "
            "WHERE t.transaction_id = ?",
            (transaction_id,),
        )
        out = dict(r)
        out["amount_cents"] = -out["amount_cents"]
        out["pay_schedule_name"] = None
        if out["pay_schedule_id"]:
            s = db.get_row(conn, "pay_schedules", out["pay_schedule_id"])
            out["pay_schedule_name"] = s["name"] if s else None
        out["rule_text"] = _rule_text(conn, out.get("rule_id")) \
            if out.get("label_source") == "rule" else None
        return out

    @app.post("/api/transactions/labels")
    def bulk_set_txn_labels(body: BulkLabelsIn):
        # B3: set_label semantics (label_source manual, null clears) applied to
        # each id; unknown ids are skipped and no rule is created here.
        updated = 0
        for tid in body.transaction_ids:
            row = db.one(
                conn,
                "SELECT t.*, pa.type AS account_type FROM transactions t "
                "JOIN plaid_accounts pa ON pa.id = t.plaid_account_id "
                "WHERE t.transaction_id = ?",
                (tid,),
            )
            if not row:
                continue
            if not (row["amount_cents"] < 0 and row["account_type"] == "depository"):
                continue
            if body.pay_schedule_id is not None and not db.get_row(
                conn, "pay_schedules", body.pay_schedule_id
            ):
                raise HTTPException(422, "pay_schedule_id not found")
            txns_mod.set_label(conn, tid, body.label, body.pay_schedule_id)
            updated += 1
        return {"updated": updated}

    @app.get("/api/deposit-rules")
    def list_deposit_rules():
        out = []
        for r in db.rows(
            conn,
            "SELECT gr.*, (SELECT COUNT(*) FROM transactions t WHERE t.rule_id = gr.id "
            "AND t.label_source = 'rule') AS labelled_count "
            "FROM deposit_rules gr ORDER BY gr.id",
        ):
            name = None
            if r["pay_schedule_id"]:
                s = db.get_row(conn, "pay_schedules", r["pay_schedule_id"])
                name = s["name"] if s else None
            out.append(
                {
                    "id": r["id"],
                    "match_text": r["match_text"],
                    "label": r["label"],
                    "pay_schedule_id": r["pay_schedule_id"],
                    "pay_schedule_name": name,
                    "labelled_count": r["labelled_count"],  # R12
                }
            )
        return out

    @app.get("/api/deposit-rules/preview")
    def deposit_rules_preview(
        text: str = Query(...),
        exclude: str | None = Query(default=None),
    ):
        # R12: show what a rule WOULD label before it is created.
        match_text = txns_mod.normalize_match_text(text)
        if len(match_text) < 4:
            raise HTTPException(422, "rule text must be at least 4 characters")
        params: list = []
        if exclude:
            params.append(exclude)
        matches = []
        for r in db.rows(
            conn,
            "SELECT t.transaction_id, t.date, t.amount_cents, t.name, t.merchant_name "
            "FROM transactions t" + txns_mod._cash_account_join()
            + (" WHERE t.amount_cents < 0 AND t.label IS NULL"
               + (" AND t.transaction_id != ?" if exclude else ""))
            + " ORDER BY t.date DESC, t.transaction_id DESC",
            tuple(params),
        ):
            haystack = f"{(r['merchant_name'] or '').lower()} {(r['name'] or '').lower()}"
            if match_text in haystack:
                matches.append(r)
        samples = [
            {"date": r["date"], "amount_cents": -r["amount_cents"], "name": r["name"]}
            for r in matches[:5]
        ]
        return {"count": len(matches), "samples": samples}

    @app.delete("/api/deposit-rules/{rule_id}", status_code=204)
    def delete_deposit_rule(rule_id: int, clear_labels: bool = Query(default=False)):
        if clear_labels:
            # R12: unlabel everything this rule labelled, then delete the rule.
            with db.tx(conn):
                conn.execute(
                    "UPDATE transactions SET label = NULL, label_source = NULL, "
                    "pay_schedule_id = NULL, rule_id = NULL, updated_at = ? "
                    "WHERE rule_id = ? AND label_source = 'rule'",
                    (txns_mod._now_iso(), rule_id),
                )
        if not db.delete_row(conn, "deposit_rules", rule_id):
            raise HTTPException(404)
        return None

    # --- unlogged card payments ------------------------------------------------
    @app.get("/api/card-payments/unlogged")
    def list_unlogged_card_payments():
        return txns_mod.unlogged_card_payments(conn, clock.today())

    @app.post("/api/card-payments/log-all")
    def log_all_card_payments():
        return txns_mod.auto_log_card_payments(conn, clock.today())

    @app.post("/api/card-payments/{transaction_id}/log", status_code=201)
    def log_card_payment(transaction_id: str):
        row = db.one(
            conn,
            "SELECT t.*, pa.card_id FROM transactions t "
            "JOIN plaid_accounts pa ON pa.id = t.plaid_account_id "
            "WHERE t.transaction_id = ?",
            (transaction_id,),
        )
        if not row or not row["card_id"]:
            raise HTTPException(404, "card payment not found")
        if db.one(
            conn, "SELECT id FROM payments WHERE transaction_id = ?", (transaction_id,)
        ):
            raise HTTPException(409, "already logged")
        txns_mod.insert_bank_payment(conn, row)
        return db.one(conn, "SELECT * FROM payments WHERE transaction_id = ?", (transaction_id,))

    @app.post("/api/card-payments/{transaction_id}/dismiss", status_code=204)
    def dismiss_card_payment(transaction_id: str):
        row = db.one(conn, "SELECT * FROM transactions WHERE transaction_id = ?", (transaction_id,))
        if not row:
            raise HTTPException(404, "transaction not found")
        txns_mod.set_label(conn, transaction_id, "dismissed", None)
        return None

    # --- bill payments ---------------------------------------------------------
    @app.post("/api/bills/{expense_id}/paid", status_code=201)
    def mark_bill_paid(expense_id: int, body: BillPaidIn):
        exp = db.get_row(conn, "expenses", expense_id)
        if not exp:
            raise HTTPException(404, "expense not found")
        if exp["kind"] != "bill":
            raise HTTPException(422, "expense is not a bill")
        due_day = exp["due_day"]
        try:
            d = date.fromisoformat(body.due_date)
        except ValueError:
            raise HTTPException(422, "invalid due_date")
        if due_day is None or d.day != min(due_day, calendar.monthrange(d.year, d.month)[1]):
            raise HTTPException(422, "due_date is not a due date of this bill")
        if db.one(
            conn,
            "SELECT id FROM bill_payments WHERE expense_id = ? AND due_date = ?",
            (expense_id, body.due_date),
        ):
            raise HTTPException(409, "already paid")
        return db.insert_row(
            conn,
            "bill_payments",
            {
                "expense_id": expense_id,
                "due_date": body.due_date,
                "paid_on": clock.today().isoformat(),
                "source": "manual",
                "transaction_id": None,
            },
        )

    @app.delete("/api/bills/{expense_id}/paid/{due_date}", status_code=204)
    def unmark_bill_paid(expense_id: int, due_date: str):
        bp = db.one(
            conn,
            "SELECT * FROM bill_payments WHERE expense_id = ? AND due_date = ?",
            (expense_id, due_date),
        )
        if not bp:
            raise HTTPException(404, "bill payment not found")
        conn.execute("DELETE FROM bill_payments WHERE id = ?", (bp["id"],))
        if bp["source"] == "auto" and bp["transaction_id"]:
            # An auto-match removed by hand must not be re-created: dismiss the
            # transaction so run_bill_matching ignores it (SAFE.md).
            conn.execute(
                "UPDATE transactions SET label = 'dismissed', label_source = 'manual', "
                "updated_at = ? WHERE transaction_id = ?",
                (txns_mod._now_iso(), bp["transaction_id"]),
            )
        conn.commit()
        return None

    # --- goals -----------------------------------------------------------------
    def _goal_view(g: dict) -> dict:
        today = clock.today()
        set_aside = _goal_set_aside(conn, g["id"])
        target = g["target_cents"]
        moves = db.rows(
            conn, "SELECT * FROM goal_moves WHERE goal_id = ? ORDER BY id", (g["id"],)
        )
        payday_dates = []
        for s in _active_schedules(conn):
            payday_dates.extend(paydays.occurrences(dict(s), today, today + timedelta(days=120)))
        month_start = f"{current_month()}-01"
        set_aside_before = _goal_set_aside(conn, g["id"], before=month_start)
        return {
            "id": g["id"],
            "name": g["name"],
            "target_cents": target,
            "target_date": g["target_date"],
            "active": bool(g["active"]),
            "set_aside_cents": set_aside,
            "progress_pct": round(min(100.0, set_aside / target * 100), 1),
            "per_paycheck_cents": goals_mod.per_paycheck_suggestion(
                target, set_aside, _goal_target_date(g), payday_dates, today
            ),
            "monthly_need_cents": goals_mod.monthly_need(
                target, set_aside_before, _goal_target_date(g), current_month()
            ),
            "moves": moves,
        }

    @app.get("/api/goals")
    def list_goals():
        return [_goal_view(g) for g in db.list_rows(conn, "goals", "id")]

    @app.post("/api/goals", status_code=201)
    def create_goal(body: GoalIn):
        fields = body.model_dump()
        fields["active"] = 1 if body.active else 0
        fields["created_at"] = clock.today().isoformat()
        return _goal_view(db.insert_row(conn, "goals", fields))

    @app.put("/api/goals/{goal_id}")
    def update_goal(goal_id: int, body: GoalPatch):
        g = db.get_row(conn, "goals", goal_id)
        if not g:
            raise HTTPException(404)
        fields = body.model_dump(exclude_unset=True, exclude_none=False)
        fields = {k: v for k, v in fields.items() if v is not None or k == "target_date"}
        if "active" in fields:
            fields["active"] = 1 if fields["active"] else 0
        return _goal_view(db.update_row(conn, "goals", goal_id, fields))

    @app.delete("/api/goals/{goal_id}", status_code=204)
    def delete_goal(goal_id: int):
        if not db.delete_row(conn, "goals", goal_id):
            raise HTTPException(404)

    @app.post("/api/goals/{goal_id}/move")
    def move_goal(goal_id: int, body: GoalMoveIn):
        g = db.get_row(conn, "goals", goal_id)
        if not g:
            raise HTTPException(404)
        if body.amount_cents < 0:
            set_aside = _goal_set_aside(conn, goal_id)
            if set_aside + body.amount_cents < 0:
                raise HTTPException(422, "cannot use more than is set aside")
        db.insert_row(
            conn,
            "goal_moves",
            {
                "goal_id": goal_id,
                "amount_cents": body.amount_cents,
                "moved_on": clock.today().isoformat(),
                "note": body.note,
            },
        )
        return _goal_view(db.get_row(conn, "goals", goal_id))

    # --- safe-to-spend / upcoming ----------------------------------------------
    def _extra_warnings(today, unlabelled_income_count: int, has_any_schedule: bool,
                        has_active_schedule: bool) -> list[dict]:
        warnings = []
        if unlabelled_income_count > 0 and (has_active_schedule or not has_any_schedule):
            warnings.append(
                {
                    "code": "unlabelled_income",
                    "message": f"{unlabelled_income_count} deposit"
                    + ("s" if unlabelled_income_count != 1 else "")
                    + " in the last 30 days need review",
                }
            )
        unlogged = txns_mod.unlogged_card_payments(conn, today)
        if unlogged:
            warnings.append(
                {
                    "code": "unlogged_card_payments",
                    "message": f"{len(unlogged)} card payment"
                    + ("s" if len(unlogged) != 1 else "")
                    + " from your bank not logged yet",
                }
            )
        undated = [
            b for b in db.rows(
                conn, "SELECT * FROM expenses WHERE kind = 'bill' AND active = 1"
            )
            if (b.get("paid_from") or "cash") == "cash" and b["due_day"] is None
        ]
        if undated:
            warnings.append(
                {
                    "code": "undated_bills",
                    "message": f"{len(undated)} bill"
                    + ("s" if len(undated) != 1 else "")
                    + " without a due date",
                }
            )
        for it in db.rows(conn, "SELECT * FROM plaid_items WHERE status != 'ok'"):
            warnings.append(
                {
                    "code": "plaid_item_error",
                    "message": f"{it['institution_name'] or 'Your bank'} needs attention",
                }
            )
        return warnings

    @app.get("/api/safe-to-spend")
    def safe_to_spend_route():
        today = clock.today()
        now = clock.now()
        txns_mod.run_bill_matching(conn, today)
        accounts = db.list_rows(conn, "cash_accounts", "sort, id")
        included = [a for a in accounts if a["include_in_cash"]]
        cash_total = sum(a["balance_cents"] for a in included)
        cash_as_of = _cash_as_of(conn)
        pending_out = _pending_out(conn, cash_as_of)
        schedules = _active_schedules(conn)
        all_schedules = db.list_rows(conn, "pay_schedules", "id")
        payday_info = paydays.next_payday(today, schedules, _schedule_paychecks(conn, schedules))
        payday = payday_info["payday"]
        if payday:
            horizon = payday["date"] if isinstance(payday["date"], date) else date.fromisoformat(payday["date"])
        else:
            horizon = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
        # card_rows for exactly sts._month_list(today, horizon) months: today's
        # month through the horizon month, at least today's month even when a
        # late payday puts the horizon before today (R2).
        card_rows: dict[str, list[dict]] = {}
        for (y, m) in sts_mod._month_list(today, horizon):
            key = f"{y:04d}-{m:02d}"
            card_rows[key] = _plan_rows(conn, key)
        bills = db.rows(
            conn, "SELECT * FROM expenses WHERE kind = 'bill' AND active = 1 ORDER BY id"
        )
        bill_payments = {
            (r["expense_id"], r["due_date"])
            for r in db.rows(conn, "SELECT expense_id, due_date FROM bill_payments")
        }
        spending_monthly = sum(
            r["amount_cents"]
            for r in db.list_rows(conn, "expenses") if r["active"] and r["kind"] == "spending"
        )
        unlabelled_income_count = db.one(
            conn,
            "SELECT COUNT(*) AS n FROM transactions t "
            "JOIN plaid_accounts pa ON pa.id = t.plaid_account_id AND pa.type = 'depository' "
            "AND pa.cash_account_id IS NOT NULL AND pa.ignored = 0 "
            "WHERE t.amount_cents < 0 AND t.pending = 0 AND t.label IS NULL "
            "AND t.pfc_primary = 'INCOME' AND t.date >= ?",
            ((today - timedelta(days=30)).isoformat(),),
        )["n"]
        unlogged_count = len(txns_mod.unlogged_card_payments(conn, today))
        extra = _extra_warnings(
            today, unlabelled_income_count, bool(all_schedules), bool(schedules)
        )
        # S6: a deposit-confirmed schedule that has missed >= 2 paychecks in a
        # row has probably stopped - say so explicitly.
        extra = extra + paydays.stopped_warnings(payday_info)
        result = sts_mod.safe_to_spend(
            today=today,
            now=now,
            payday_info=payday_info,
            has_schedules=bool(all_schedules),
            cash_total_cents=cash_total,
            pending_out_cents=pending_out,
            cash_as_of=cash_as_of,
            cash_floor_cents=db.get_settings(conn)["cash_floor_cents"] or 0,
            goals_set_aside_cents=_goals_set_aside_total(conn),
            bills=bills,
            bill_payments=bill_payments,
            card_rows=card_rows,
            spending_monthly_cents=spending_monthly,
            extra_warnings=extra,
        )
        result["accounts"] = [
            {
                "id": a["id"],
                "name": a["name"],
                "balance_cents": a["balance_cents"],
                "sync": a.get("sync") or "manual",
                "last_synced_at": a.get("last_synced_at"),
            }
            for a in included
        ]
        result["unlabelled_income_count"] = unlabelled_income_count
        result["unlogged_card_payment_count"] = unlogged_count
        return result

    @app.get("/api/upcoming")
    def upcoming(days: int = Query(default=30, ge=1, le=365)):
        today = clock.today()
        end = today + timedelta(days=days)
        rows = []
        schedules = _active_schedules(conn)
        paychecks = _schedule_paychecks(conn, schedules)
        for s in schedules:
            for d in paydays.occurrences(dict(s), today, end):
                rows.append(
                    {"date": d.isoformat(), "kind": "payday", "id": s["id"],
                     "name": s["name"], "amount_cents": s["amount_cents"],
                     "status": "expected"}
                )
        bill_payments = {
            (r["expense_id"], r["due_date"])
            for r in db.rows(conn, "SELECT expense_id, due_date FROM bill_payments")
        }
        for b in db.rows(
            conn, "SELECT * FROM expenses WHERE kind = 'bill' AND active = 1 ORDER BY id"
        ):
            for d in bills_mod.due_dates(dict(b), today, end):
                key = (b["id"], d.isoformat())
                if key in bill_payments:
                    status = "paid"
                elif (b.get("paid_from") or "cash") == "card":
                    status = "on_card"
                else:
                    status = "due"
                rows.append(
                    {"date": d.isoformat(), "kind": "bill", "id": b["id"],
                     "name": b["name"], "amount_cents": b["amount_cents"], "status": status}
                )
        for month_key in sorted(
            {
                f"{d.year:04d}-{d.month:02d}"
                for d in [today + timedelta(days=i) for i in range(days + 1)]
            }
        ):
            plan_by_card = {r["card_id"]: r for r in _plan_rows(conn, month_key)}
            for c in db.list_rows(conn, "cards", "id"):
                if c["closed"]:
                    continue
                bank_due = (c.get("sync") or "manual") == "plaid" and c.get("next_due_date")
                if bank_due:
                    # FIX5: synced cards use the bank's next due date, once,
                    # in the month that date falls in.
                    if c["next_due_date"][:7] != month_key:
                        continue
                    due = date.fromisoformat(c["next_due_date"])
                elif c["due_day"] is None:
                    continue
                else:
                    due = date(
                        int(month_key[:4]), int(month_key[5:7]),
                        min(c["due_day"], calendar.monthrange(int(month_key[:4]), int(month_key[5:7]))[1]),
                    )
                if not (today <= due <= end):
                    continue
                row = plan_by_card.get(c["id"])
                remaining = row["remaining_cents"] if row else c["balance_cents"]
                if row is None and c["balance_cents"] > 0:
                    remaining = c["balance_cents"]
                min_rem = row["min_remaining_cents"] if row else remaining
                rows.append(
                    {"date": due.isoformat(), "kind": "card", "id": c["id"],
                     "name": c["name"], "amount_cents": remaining,
                     # FIX5: what is owed vs what the plan adds on top
                     "min_remaining_cents": min_rem,
                     "extra_remaining_cents": max(0, remaining - min_rem),
                     "status": "paid" if remaining == 0 else "due"}
                )
        for g in db.rows(conn, "SELECT * FROM goals WHERE active = 1 AND target_date IS NOT NULL"):
            d = date.fromisoformat(g["target_date"])
            if today <= d <= end:
                set_aside = _goal_set_aside(conn, g["id"])
                rows.append(
                    {"date": g["target_date"], "kind": "goal", "id": g["id"],
                     "name": g["name"], "amount_cents": max(0, g["target_cents"] - set_aside),
                     "status": "target"}
                )
        kind_order = {"payday": 0, "bill": 1, "card": 2, "goal": 3}
        rows.sort(key=lambda r: (r["date"], kind_order[r["kind"]], r["id"]))
        return rows

    # --- subscriptions --------------------------------------------------------
    @app.get("/api/subscriptions")
    def list_subscriptions():
        today = clock.today()
        rows = [_subscription_view(r, today) for r in db.list_rows(conn, "subscriptions", "id")]
        active = [r for r in rows if r["status"] == "active"]
        active.sort(key=lambda r: (r["next_renewal"], r["name"].lower(), r["id"]))
        cancelled = [r for r in rows if r["status"] == "cancelled"]
        cancelled.sort(key=lambda r: (r["cancelled_on"] or "", r["id"]), reverse=True)
        return {
            "active": active,
            "cancelled": cancelled,
            "totals": {
                "monthly_cents": sum(r["monthly_cents"] for r in active),
                "annual_cents": sum(r["annual_cents"] for r in active),
                "active_count": len(active),
                "cancelled_count": len(cancelled),
            },
        }

    @app.post("/api/subscriptions", status_code=201)
    def create_subscription(body: SubscriptionIn):
        fields = body.model_dump()
        fields["created_at"] = clock.now_iso()
        row = db.insert_row(conn, "subscriptions", fields)
        return _subscription_view(row, clock.today())

    # Static sub-paths first so they are never read as a {subscription_id}.
    @app.get("/api/subscriptions/suggestions")
    def subscription_suggestions():
        today = clock.today()
        txns = db.rows(
            conn,
            "SELECT t.transaction_id, t.date, t.amount_cents, t.name, t.merchant_name, "
            "t.pending, t.pfc_primary FROM transactions t "
            "JOIN plaid_accounts pa ON pa.id = t.plaid_account_id AND pa.ignored = 0 "
            "WHERE t.amount_cents > 0 AND t.date >= ?",
            ((today - timedelta(days=800)).isoformat(),),
        )
        subs = db.rows(conn, "SELECT name, match_text FROM subscriptions WHERE status = 'active'")
        known = [s["match_text"] or s["name"] for s in subs]
        cancelled = [
            {"text": s["match_text"] or s["name"], "cancelled_on": s["cancelled_on"]}
            for s in db.rows(
                conn,
                "SELECT name, match_text, cancelled_on FROM subscriptions "
                "WHERE status = 'cancelled' AND cancelled_on IS NOT NULL",
            )
        ]
        dismissed = {r["key"] for r in db.rows(conn, "SELECT key FROM subscription_dismissals")}
        bills = db.rows(
            conn,
            "SELECT id, name, match_text FROM expenses WHERE kind = 'bill' AND active = 1",
        )
        return subs_mod.detect_recurring(
            txns, today, known_texts=known, dismissed=dismissed, bills=bills,
            cancelled=cancelled,
        )

    @app.post("/api/subscriptions/suggestions/dismiss", status_code=204)
    def dismiss_subscription_suggestion(body: SubscriptionDismissIn):
        conn.execute(
            "INSERT OR IGNORE INTO subscription_dismissals (key, dismissed_at) VALUES (?, ?)",
            (body.key, clock.now_iso()),
        )
        conn.commit()

    @app.post("/api/subscriptions/from-expense/{expense_id}", status_code=201)
    def subscription_from_expense(expense_id: int):
        """Turn a monthly bill into a subscription and switch the bill off, so
        the plan counts it once (as a subscription), not twice."""
        exp = db.get_row(conn, "expenses", expense_id)
        if not exp:
            raise HTTPException(404)
        if exp["kind"] != "bill":
            raise HTTPException(409, "Only bills can become subscriptions")
        if not exp["active"]:
            raise HTTPException(409, "That bill is inactive")
        today = clock.today()
        if exp["due_day"] is not None:
            nxt = bills_mod.due_dates(exp, today, today + timedelta(days=62))[0]
        else:
            nxt = today
        try:
            conn.execute(
                "INSERT INTO subscriptions (name, amount_cents, cycle, next_date, match_text, "
                "source, created_at) VALUES (?, ?, 'monthly', ?, ?, 'converted', ?)",
                (exp["name"], exp["amount_cents"], nxt.isoformat(),
                 (exp.get("match_text") or "").strip().lower() or None, clock.now_iso()),
            )
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("UPDATE expenses SET active = 0 WHERE id = ?", (expense_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return _subscription_view(db.get_row(conn, "subscriptions", new_id), today)

    @app.put("/api/subscriptions/{subscription_id}")
    def update_subscription(subscription_id: int, body: SubscriptionIn):
        if not db.get_row(conn, "subscriptions", subscription_id):
            raise HTTPException(404)
        row = db.update_row(conn, "subscriptions", subscription_id, body.model_dump())
        return _subscription_view(row, clock.today())

    @app.post("/api/subscriptions/{subscription_id}/cancel")
    def cancel_subscription(subscription_id: int, body: SubscriptionCancelIn | None = None):
        if not db.get_row(conn, "subscriptions", subscription_id):
            raise HTTPException(404)
        on = (body.cancelled_on if body and body.cancelled_on else clock.today().isoformat())
        row = db.update_row(
            conn, "subscriptions", subscription_id, {"status": "cancelled", "cancelled_on": on}
        )
        return _subscription_view(row, clock.today())

    @app.post("/api/subscriptions/{subscription_id}/reactivate")
    def reactivate_subscription(subscription_id: int):
        if not db.get_row(conn, "subscriptions", subscription_id):
            raise HTTPException(404)
        row = db.update_row(
            conn, "subscriptions", subscription_id, {"status": "active", "cancelled_on": None}
        )
        return _subscription_view(row, clock.today())

    @app.delete("/api/subscriptions/{subscription_id}", status_code=204)
    def delete_subscription(subscription_id: int):
        if not db.delete_row(conn, "subscriptions", subscription_id):
            raise HTTPException(404)

    # --- export / import ----------------------------------------------------
    @app.get("/api/export")
    def export_all():
        return db.export_all(conn)

    @app.post("/api/import")
    def import_all(data: dict = Body(...)):
        # S7 (G2-07): a plain def - the sync DB work runs in the threadpool,
        # not on the event loop. FastAPI validates the JSON body and the dict
        # type, returning 422 for invalid JSON or non-object payloads.
        db.import_all(conn, data)
        return {"ok": True}

    # --- insights (FIX5 R3) ------------------------------------------------
    _TXN_COLS = (
        "t.transaction_id, t.date, t.amount_cents, t.name, t.merchant_name, "
        "t.pfc_primary, t.pfc_detailed, pa.card_id AS card_id, pa.type AS account_type"
    )

    def _linked_txns(types: tuple[str, ...]) -> list[dict]:
        qs = ", ".join("?" for _ in types)
        return db.rows(
            conn,
            f"SELECT {_TXN_COLS} FROM transactions t "
            "JOIN plaid_accounts pa ON pa.id = t.plaid_account_id "
            "WHERE t.pending = 0 AND pa.ignored = 0 "
            "AND (pa.card_id IS NOT NULL OR pa.cash_account_id IS NOT NULL) "
            f"AND pa.type IN ({qs}) ORDER BY t.date",
            types,
        )

    @app.get("/api/insights/debt-trend")
    def insights_debt_trend(months: int = Query(default=6, ge=1, le=24)):
        cards = [
            {"id": c["id"], "name": c["name"]}
            for c in db.list_rows(conn, "cards", "id")
            if not c["closed"] and (c.get("sync") or "manual") == "plaid"
        ]
        return insights_mod.debt_trend(_linked_txns(("credit",)), cards, clock.today(), months)

    @app.get("/api/insights/spending")
    def insights_spending(months: int = Query(default=3, ge=1, le=12)):
        excluded = {
            r["transaction_id"]
            for r in db.rows(conn, "SELECT transaction_id FROM bill_payments WHERE transaction_id IS NOT NULL")
        }
        match_texts = [
            r["match_text"].strip()
            for r in db.rows(
                conn,
                "SELECT match_text FROM expenses WHERE active = 1 AND kind = 'bill' AND match_text IS NOT NULL "
                "UNION ALL SELECT match_text FROM subscriptions WHERE status = 'active' AND match_text IS NOT NULL",
            )
            if (r["match_text"] or "").strip()
        ]
        budget_spending = sum(
            e["amount_cents"] for e in db.list_rows(conn, "expenses")
            if e["active"] and e["kind"] == "spending"
        )
        return insights_mod.spending_baseline(
            _linked_txns(("credit", "depository")), excluded, match_texts,
            budget_spending, clock.today(), months,
        )

    @app.get("/api/reminders")
    def reminders_endpoint(days: int = Query(default=3, ge=0, le=14)):
        today = clock.today()
        month = today.strftime("%Y-%m")
        cards = [{"id": c["id"], "name": c["name"], "closed": c["closed"]}
                 for c in db.list_rows(conn, "cards", "id")]
        bills = [e for e in db.list_rows(conn, "expenses") if e["active"] and e["kind"] == "bill"]
        paid_keys = {(r["expense_id"], r["due_date"])
                     for r in db.rows(conn, "SELECT expense_id, due_date FROM bill_payments")}
        since = (today - timedelta(days=90)).isoformat()
        late = {}
        for r in db.rows(
            conn,
            "SELECT pa.card_id AS card_id, COUNT(*) AS n FROM transactions t "
            "JOIN plaid_accounts pa ON pa.id = t.plaid_account_id "
            "WHERE pa.card_id IS NOT NULL AND t.amount_cents > 0 AND t.date >= ? "
            "AND (t.pfc_detailed = 'BANK_FEES_LATE_FEES' OR UPPER(t.name) LIKE '%LATE FEE%') "
            "GROUP BY pa.card_id",
            (since,),
        ):
            late[r["card_id"]] = r["n"]
        return insights_mod.reminders(_plan_rows(conn, month), cards, bills, paid_keys, late, today, days)

    # --- PWA / static -------------------------------------------------------
    @app.get("/sw.js")
    def service_worker():
        sw_path = STATIC_DIR / "sw.js"
        if not sw_path.exists():
            raise HTTPException(404)
        body = sw_path.read_text(encoding="utf-8").replace("__VERSION__", _static_version())
        return Response(
            content=body,
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    @app.get("/manifest.webmanifest")
    def webmanifest():
        mf_path = STATIC_DIR / "manifest.webmanifest"
        if not mf_path.exists():
            raise HTTPException(404)
        return FileResponse(mf_path, media_type="application/manifest+json")

    @app.get("/offline")
    def offline_page():
        off_path = STATIC_DIR / "offline.html"
        if not off_path.exists():
            raise HTTPException(404)
        return FileResponse(off_path, media_type="text/html")

    if STATIC_DIR.exists():
        app.mount(
            "/",
            StaticFiles(directory=str(STATIC_DIR), html=True, check_dir=False),
            name="static",
        )
    else:
        STATIC_DIR.mkdir(parents=True, exist_ok=True)
        app.mount(
            "/",
            StaticFiles(directory=str(STATIC_DIR), html=True, check_dir=False),
            name="static",
        )

    return app
