"""DB helpers for bank transactions, paycheck labels, deposit rules, bill
matching, unlogged card payments and deposits (docs/SAFE.md).

Money convention: transactions.amount_cents keeps Plaid's sign -
positive = money leaving the account, negative = money arriving.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from . import clock
from . import db

LABELS = ("paycheck", "not_paycheck", "dismissed")


def _now_iso() -> str:
    return clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_match_text(text: str | None) -> str:
    """Rules store match_text lowercased and trimmed."""
    return (text or "").strip().lower()


def _cash_account_join() -> str:
    """Inflows live on LINKED cash (depository) accounts: linked to a cash
    account and not ignored (R7, SAFE.md)."""
    return (
        " JOIN plaid_accounts pa ON pa.id = t.plaid_account_id"
        " AND pa.type = 'depository'"
        " AND pa.cash_account_id IS NOT NULL"
        " AND pa.ignored = 0"
    )


# --- deposit rules --------------------------------------------------------------

def apply_rules(conn: sqlite3.Connection, only_ids: list[str] | None = None) -> int:
    """Apply deposit_rules to unlabelled inflows on linked cash accounts.

    The first rule (lowest id) whose match_text is a substring of
    'merchant_name name' (lowercased) wins. Never touches manual labels.
    Returns the number of rows labelled.
    """
    rules = db.rows(conn, "SELECT * FROM deposit_rules ORDER BY id")
    if not rules:
        return 0
    sql = (
        "SELECT t.transaction_id, t.merchant_name, t.name FROM transactions t"
        + _cash_account_join()
        + " WHERE t.amount_cents < 0 AND t.label IS NULL"
    )
    params: tuple = ()
    if only_ids is not None:
        if not only_ids:
            return 0
        sql += " AND t.transaction_id IN (%s)" % ", ".join("?" for _ in only_ids)
        params = tuple(only_ids)
    candidates = db.rows(conn, sql, params)
    labelled = 0
    for t in candidates:
        haystack = f"{(t['merchant_name'] or '').lower()} {(t['name'] or '').lower()}"
        for rule in rules:
            if rule["match_text"] and rule["match_text"] in haystack:
                conn.execute(
                    "UPDATE transactions SET label = ?, label_source = 'rule', "
                    "pay_schedule_id = ?, rule_id = ?, updated_at = ? "
                    "WHERE transaction_id = ?",
                    (rule["label"], rule["pay_schedule_id"], rule["id"],
                     _now_iso(), t["transaction_id"]),
                )
                labelled += 1
                break
    conn.commit()
    return labelled


def _upsert_rule(conn, match_text: str, label: str, pay_schedule_id: int | None) -> None:
    conn.execute(
        "INSERT INTO deposit_rules (match_text, label, pay_schedule_id, created_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(match_text) DO UPDATE SET "
        "label = excluded.label, pay_schedule_id = excluded.pay_schedule_id",
        (match_text, label, pay_schedule_id, clock.today().isoformat()),
    )


def set_label(conn: sqlite3.Connection, transaction_id: str, label: str | None,
              pay_schedule_id: int | None, create_rule: bool = False,
              rule_text: str | None = None) -> dict | None:
    """Manually label a transaction (label_source='manual').

    label=None clears the label and the source. create_rule upserts a
    deposit rule (match_text = rule_text, else the txn's merchant_name or
    name) and re-applies rules to unlabelled inflows.
    """
    row = db.one(conn, "SELECT * FROM transactions WHERE transaction_id = ?", (transaction_id,))
    if not row:
        return None
    if label is not None and create_rule:
        # R12: validate the rule text BEFORE touching the row - a 3-char
        # fragment would label dozens of deposits.
        base = rule_text or row["merchant_name"] or row["name"]
        match_text = normalize_match_text(base)
        if len(match_text) < 4:
            raise ValueError("rule text must be at least 4 characters")
    fields: dict = {"updated_at": _now_iso()}
    if label is None:
        fields |= {"label": None, "label_source": None, "pay_schedule_id": None, "rule_id": None}
    else:
        # Manual labels clear rule_id (R12): the link is only meaningful for
        # rule-applied labels.
        fields |= {"label": label, "label_source": "manual", "pay_schedule_id": pay_schedule_id,
                   "rule_id": None}
    conn.execute(
        "UPDATE transactions SET %s WHERE transaction_id = ?" % ", ".join(f"{k} = ?" for k in fields),
        (*fields.values(), transaction_id),
    )
    conn.commit()
    if label is not None and create_rule:
        _upsert_rule(conn, match_text, label, pay_schedule_id)
        conn.commit()
        apply_rules(conn)
    return db.one(conn, "SELECT * FROM transactions WHERE transaction_id = ?", (transaction_id,))


# --- paychecks / deposits ---------------------------------------------------------

def paychecks(conn: sqlite3.Connection, single_schedule_id: int | None = None) -> list[dict]:
    """Posted paycheck deposits (for app.paydays.receipts).

    amount_cents is positive (the deposit amount); a NULL pay_schedule_id
    is attributed to single_schedule_id.
    """
    rows = db.rows(
        conn,
        "SELECT t.transaction_id, t.date, t.amount_cents, t.pay_schedule_id FROM transactions t"
        + _cash_account_join()
        + " WHERE t.amount_cents < 0 AND t.pending = 0 AND t.label = 'paycheck' ORDER BY t.date, t.transaction_id",
    )
    return [
        {
            "transaction_id": r["transaction_id"],
            "date": r["date"],
            "amount_cents": -r["amount_cents"],
            "pay_schedule_id": single_schedule_id if r["pay_schedule_id"] is None else r["pay_schedule_id"],
        }
        for r in rows
    ]


def deposits(conn: sqlite3.Connection, today: date, days: int = 45,
             include_transfers: bool = False) -> list[dict]:
    """Inflows on linked cash accounts, newest first, with account name/mask.

    include_transfers=False hides pfc_primary='TRANSFER_IN' rows unless they
    carry a label. amount_cents is positive (the deposit amount).
    """
    sql = (
        "SELECT t.transaction_id, t.date, t.amount_cents, t.name, t.merchant_name, t.pending, "
        "t.pfc_primary, t.pfc_detailed, t.label, t.label_source, t.pay_schedule_id, "
        "pa.name AS account_name, pa.mask, pa.cash_account_id FROM transactions t"
        + _cash_account_join()
        + " WHERE t.amount_cents < 0 AND t.date >= ?"
    )
    if not include_transfers:
        # COALESCE: a NULL pfc_primary must not make NOT (...=...) evaluate to
        # NULL and hide the row (R6).
        sql += " AND NOT (COALESCE(t.pfc_primary, '') = 'TRANSFER_IN' AND t.label IS NULL)"
    sql += " ORDER BY t.date DESC, t.transaction_id DESC"
    rows = db.rows(conn, sql, ((today - timedelta(days=days)).isoformat(),))
    return [
        {
            "transaction_id": r["transaction_id"],
            "date": r["date"],
            "amount_cents": -r["amount_cents"],
            "name": r["name"],
            "merchant_name": r["merchant_name"],
            "pending": bool(r["pending"]),
            "pfc_primary": r["pfc_primary"],
            "pfc_detailed": r["pfc_detailed"],
            "label": r["label"],
            "label_source": r["label_source"],
            "pay_schedule_id": r["pay_schedule_id"],
            "account_name": r["account_name"],
            "mask": r["mask"],
            "cash_account_id": r["cash_account_id"],
        }
        for r in rows
    ]


# --- bill matching ------------------------------------------------------------------

def run_bill_matching(conn: sqlite3.Connection, today: date) -> int:
    """Auto-match bill occurrences to bank outflows, inserting bill_payments
    with source='auto'. Idempotent. Returns the number of rows inserted."""
    try:
        from . import bills as bills_mod
    except ImportError:
        return 0  # app/bills.py not landed yet; nothing to match against
    bills = db.rows(
        conn,
        "SELECT id, name, amount_cents, due_day, match_text FROM expenses "
        "WHERE kind = 'bill' AND active = 1 AND match_text IS NOT NULL "
        "AND TRIM(match_text) != '' AND due_day IS NOT NULL ORDER BY id",
    )
    if not bills:
        return 0
    txns = db.rows(
        conn,
        "SELECT t.transaction_id, t.date, t.amount_cents, t.name, t.merchant_name "
        "FROM transactions t JOIN plaid_accounts pa ON pa.id = t.plaid_account_id "
        "WHERE t.amount_cents > 0 AND t.date >= ? AND (t.label IS NULL OR t.label != 'dismissed') "
        "AND pa.ignored = 0 AND ((pa.type = 'depository' AND pa.cash_account_id IS NOT NULL) "
        "OR (pa.type = 'credit' AND pa.card_id IS NOT NULL))",
        ((today - timedelta(days=60)).isoformat(),),
    )
    paid = {(r["expense_id"], r["due_date"]) for r in db.rows(conn, "SELECT expense_id, due_date FROM bill_payments")}
    used_txn_ids = {
        r["transaction_id"]
        for r in db.rows(conn, "SELECT transaction_id FROM bill_payments WHERE transaction_id IS NOT NULL")
    }
    matches = bills_mod.match_bills(bills, txns, paid, used_txn_ids, today)
    inserted = 0
    for m in matches:
        cur = conn.execute(
            "INSERT OR IGNORE INTO bill_payments (expense_id, due_date, paid_on, source, "
            "transaction_id) VALUES (?, ?, ?, 'auto', ?)",
            (m["expense_id"], m["due_date"], m["paid_on"], m["transaction_id"]),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


# --- unlogged card payments -----------------------------------------------------------

def _card_payment_candidates(conn: sqlite3.Connection, since: str) -> list[dict]:
    """Posted payments TO linked cards seen at the bank, dated on/after
    `since`, not yet referenced by a payment row (shared by the unlogged list
    and auto-logging)."""
    rows = db.rows(
        conn,
        "SELECT t.transaction_id, t.date, t.amount_cents, t.name, pa.card_id, c.name AS card_name "
        "FROM transactions t JOIN plaid_accounts pa ON pa.id = t.plaid_account_id "
        "LEFT JOIN cards c ON c.id = pa.card_id "
        "WHERE pa.type = 'credit' AND pa.card_id IS NOT NULL AND pa.ignored = 0 "
        "AND t.amount_cents < 0 AND t.pending = 0 AND t.date >= ? "
        "AND (COALESCE(t.pfc_primary, '') IN ('LOAN_PAYMENTS', 'TRANSFER_IN') "
        "     OR t.pfc_primary IS NULL) "
        "AND (t.label IS NULL OR t.label != 'dismissed') "
        "AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.transaction_id = t.transaction_id) "
        "ORDER BY t.date DESC, t.transaction_id",
        (since,),
    )
    return rows


def unlogged_card_payments(conn: sqlite3.Connection, today: date) -> list[dict]:
    """Posted payments TO linked cards seen at the bank but not logged in the app.

    A card payment is a negative (inflow) amount on a linked credit account,
    dated since the first day of the current local month (R14: older ones
    flooded the list), whose category is a card payment (LOAN_PAYMENTS,
    TRANSFER_IN) or unknown (NULL), not dismissed, and not referenced by a
    payment row. amount_cents is positive (the payment amount).
    """
    rows = _card_payment_candidates(conn, today.replace(day=1).isoformat())
    return [
        {
            "transaction_id": r["transaction_id"],
            "date": r["date"],
            "amount_cents": -r["amount_cents"],
            "card_id": r["card_id"],
            "card_name": r["card_name"],
            "name": r["name"],
        }
        for r in rows
    ]


def insert_bank_payment(conn: sqlite3.Connection, row: sqlite3.Row | dict) -> int:
    """Insert a payments row for a bank-posted card payment (shared by
    POST /api/card-payments/{id}/log and auto_log_card_payments).

    `row` needs transaction_id, date, amount_cents (Plaid sign: negative for
    an inflow) and card_id. month = txn month, paid_on = txn date, note
    'From your bank'. Payments with a transaction_id are already at the bank,
    so pending_out ignores them (SAFE.md); synced card balances are not
    modified by logging. Idempotent via the partial unique index on
    payments.transaction_id.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO payments (card_id, month, paid_on, amount_cents, "
        "note, transaction_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            row["card_id"],
            row["date"][:7],
            row["date"],
            -row["amount_cents"],
            "From your bank",
            row["transaction_id"],
            _now_iso(),
        ),
    )
    conn.commit()
    return cur.rowcount


def auto_log_card_payments(conn: sqlite3.Connection, today: date) -> dict:
    """Log or link bank-posted card payments since the first day of the
    PREVIOUS month (B1).

    For each candidate: if a manual payment row (transaction_id IS NULL)
    exists on the same card with the same amount and |paid_on - txn date|
    <= 7 days, link it; otherwise insert what the single-log endpoint
    inserts. Returns {"logged": n, "linked": n}. Idempotent.
    """
    prev = today.replace(day=1) - timedelta(days=1)
    since = prev.replace(day=1).isoformat()
    logged = 0
    linked = 0
    for t in _card_payment_candidates(conn, since):
        match = db.one(
            conn,
            "SELECT id FROM payments WHERE card_id = ? AND transaction_id IS NULL "
            "AND amount_cents = ? AND ABS(julianday(paid_on) - julianday(?)) <= 7 "
            "ORDER BY ABS(julianday(paid_on) - julianday(?)), id LIMIT 1",
            (t["card_id"], -t["amount_cents"], t["date"], t["date"]),
        )
        if match:
            try:
                cur = conn.execute(
                    "UPDATE payments SET transaction_id = ? WHERE id = ?",
                    (t["transaction_id"], match["id"]),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()  # another candidate already linked this payment
                continue
            linked += cur.rowcount
        else:
            logged += insert_bank_payment(conn, t)
    return {"logged": logged, "linked": linked}


def auto_label_salary(conn: sqlite3.Connection) -> int:
    """Auto-label unlabelled salary deposits as paychecks (B2).

    Unlabelled, non-pending inflows on linked, non-ignored depository
    accounts with pfc_detailed = 'INCOME_SALARY' get label='paycheck',
    label_source='auto'. Manual and rule labels are never overwritten.
    Returns the number of rows labelled.
    """
    rows = db.rows(
        conn,
        "SELECT t.transaction_id FROM transactions t" + _cash_account_join()
        + " WHERE t.amount_cents < 0 AND t.pending = 0 AND t.label IS NULL "
        "AND t.pfc_detailed = 'INCOME_SALARY'",
    )
    if not rows:
        return 0
    now = _now_iso()
    for r in rows:
        conn.execute(
            "UPDATE transactions SET label = 'paycheck', label_source = 'auto', "
            "updated_at = ? WHERE transaction_id = ?",
            (now, r["transaction_id"]),
        )
    conn.commit()
    return len(rows)