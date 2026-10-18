"""SQLite schema, connection, and CRUD helpers. stdlib sqlite3, no ORM."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  strategy TEXT NOT NULL DEFAULT 'avalanche',
  buffer_cents INTEGER NOT NULL DEFAULT 0,
  debt_share_pct INTEGER NOT NULL DEFAULT 100,
  extra_override_cents INTEGER,
  auto_log_card_payments INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS income (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
  active INTEGER NOT NULL DEFAULT 1, sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
  kind TEXT NOT NULL DEFAULT 'bill',
  due_day INTEGER CHECK (due_day BETWEEN 1 AND 31),
  active INTEGER NOT NULL DEFAULT 1, sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cards (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  balance_cents INTEGER NOT NULL CHECK (balance_cents >= 0),
  apr REAL NOT NULL CHECK (apr >= 0 AND apr < 100),
  min_payment_cents INTEGER NOT NULL CHECK (min_payment_cents >= 0),
  credit_limit_cents INTEGER,
  due_day INTEGER CHECK (due_day BETWEEN 1 AND 31),
  priority INTEGER NOT NULL DEFAULT 0,
  starting_balance_cents INTEGER NOT NULL,
  closed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  month TEXT NOT NULL,
  paid_on TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
  note TEXT
);
CREATE TABLE IF NOT EXISTS balance_log (
  id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  logged_on TEXT NOT NULL, balance_cents INTEGER NOT NULL, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS month_adjustments (
  id INTEGER PRIMARY KEY, month TEXT NOT NULL, name TEXT NOT NULL,
  amount_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cash_accounts (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'checking',
  balance_cents INTEGER NOT NULL,
  include_in_cash INTEGER NOT NULL DEFAULT 1,
  sync TEXT NOT NULL DEFAULT 'manual',
  last_synced_at TEXT, balance_updated_at TEXT, created_at TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0
);
"""

PLAID_SCHEMA = """
CREATE TABLE IF NOT EXISTS plaid_items (
  id INTEGER PRIMARY KEY,
  item_id TEXT NOT NULL UNIQUE,
  access_token_enc TEXT NOT NULL,
  institution_name TEXT,
  status TEXT NOT NULL DEFAULT 'ok',
  error_code TEXT,
  last_synced_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plaid_accounts (
  id INTEGER PRIMARY KEY,
  plaid_item_id INTEGER NOT NULL REFERENCES plaid_items(id) ON DELETE CASCADE,
  account_id TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL, official_name TEXT, mask TEXT,
  card_id INTEGER UNIQUE REFERENCES cards(id) ON DELETE SET NULL,
  ignored INTEGER NOT NULL DEFAULT 0,
  last_balance_cents INTEGER,
  last_payload TEXT
);
"""

SAFE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pay_schedules (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  frequency TEXT NOT NULL,
  anchor_date TEXT NOT NULL,
  semimonthly_day1 INTEGER, semimonthly_day2 INTEGER,
  weekend_rule TEXT NOT NULL DEFAULT 'previous',
  amount_cents INTEGER NOT NULL DEFAULT 0 CHECK (amount_cents >= 0),
  cash_account_id INTEGER REFERENCES cash_accounts(id) ON DELETE SET NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
  transaction_id TEXT PRIMARY KEY,
  plaid_account_id INTEGER NOT NULL REFERENCES plaid_accounts(id) ON DELETE CASCADE,
  date TEXT NOT NULL, authorized_date TEXT,
  amount_cents INTEGER NOT NULL,
  name TEXT NOT NULL, merchant_name TEXT,
  pending INTEGER NOT NULL DEFAULT 0, pending_transaction_id TEXT,
  pfc_primary TEXT, pfc_detailed TEXT,
  label TEXT,
  label_source TEXT,
  pay_schedule_id INTEGER REFERENCES pay_schedules(id) ON DELETE SET NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transactions_account_date
  ON transactions(plaid_account_id, date);
CREATE TABLE IF NOT EXISTS deposit_rules (
  id INTEGER PRIMARY KEY,
  match_text TEXT NOT NULL,
  label TEXT NOT NULL,
  pay_schedule_id INTEGER REFERENCES pay_schedules(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  UNIQUE(match_text)
);
CREATE TABLE IF NOT EXISTS bill_payments (
  id INTEGER PRIMARY KEY,
  expense_id INTEGER NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
  due_date TEXT NOT NULL,
  paid_on TEXT NOT NULL,
  source TEXT NOT NULL,
  transaction_id TEXT,
  UNIQUE(expense_id, due_date)
);
CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  target_cents INTEGER NOT NULL CHECK (target_cents > 0),
  target_date TEXT,
  active INTEGER NOT NULL DEFAULT 1, sort INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goal_moves (
  id INTEGER PRIMARY KEY,
  goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
  amount_cents INTEGER NOT NULL CHECK (amount_cents != 0),
  moved_on TEXT NOT NULL, note TEXT
);
"""


SUBSCRIPTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
  cycle TEXT NOT NULL CHECK (cycle IN ('weekly','monthly','quarterly','yearly')),
  next_date TEXT NOT NULL,
  match_text TEXT,
  note TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','cancelled')),
  cancelled_on TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscription_dismissals (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  dismissed_at TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    # R8: concurrent per-thread connections must wait for each other's locks
    # instead of failing immediately with 'database is locked'.
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


class ThreadLocalConnection:
    """R8: proxy that gives every thread its own sqlite3.Connection on first
    use, so requests no longer share one connection and the global request
    lock is unnecessary. Attribute access (execute, commit, ...) is delegated
    to the calling thread's connection; db.* helpers work unchanged."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._local = threading.local()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self._db_path)
            self._local.conn = conn
        return conn

    def __getattr__(self, name):
        return getattr(self._conn(), name)


def _migrate(conn) -> None:
    """Idempotent migrations: plaid tables + cards.sync / cards.last_synced_at,
    phase-3 columns (CASH.md)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cards)")}
    if cols and "sync" not in cols:
        conn.execute("ALTER TABLE cards ADD COLUMN sync TEXT NOT NULL DEFAULT 'manual'")
    if cols and "last_synced_at" not in cols:
        conn.execute("ALTER TABLE cards ADD COLUMN last_synced_at TEXT")
    conn.executescript(PLAID_SCHEMA)
    # settings: cash floor / reserve months
    scols = {r[1] for r in conn.execute("PRAGMA table_info(settings)")}
    if scols and "cash_floor_cents" not in scols:
        conn.execute("ALTER TABLE settings ADD COLUMN cash_floor_cents INTEGER NOT NULL DEFAULT 0")
    if scols and "cash_reserve_months" not in scols:
        conn.execute("ALTER TABLE settings ADD COLUMN cash_reserve_months REAL NOT NULL DEFAULT 1.0")
    if scols and "auto_log_card_payments" not in scols:
        conn.execute("ALTER TABLE settings ADD COLUMN auto_log_card_payments INTEGER NOT NULL DEFAULT 1")
    # month_adjustments.kind (manual | cash)
    acols = {r[1] for r in conn.execute("PRAGMA table_info(month_adjustments)")}
    if acols and "kind" not in acols:
        conn.execute("ALTER TABLE month_adjustments ADD COLUMN kind TEXT NOT NULL DEFAULT 'manual'")
    # payments.created_at with backfill
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(payments)")}
    if pcols and "created_at" not in pcols:
        conn.execute("ALTER TABLE payments ADD COLUMN created_at TEXT")
    conn.execute(
        "UPDATE payments SET created_at = paid_on || 'T00:00:00Z' WHERE created_at IS NULL"
    )
    # plaid_accounts: type/subtype/cash_account_id (UNIQUE not allowed in ADD
    # COLUMN on SQLite, so use a partial unique index).
    pacols = {r[1] for r in conn.execute("PRAGMA table_info(plaid_accounts)")}
    if pacols and "type" not in pacols:
        conn.execute("ALTER TABLE plaid_accounts ADD COLUMN type TEXT")
    if pacols and "subtype" not in pacols:
        conn.execute("ALTER TABLE plaid_accounts ADD COLUMN subtype TEXT")
    if pacols and "cash_account_id" not in pacols:
        conn.execute(
            "ALTER TABLE plaid_accounts ADD COLUMN cash_account_id INTEGER REFERENCES cash_accounts(id) ON DELETE SET NULL"
        )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_plaid_accounts_cash_account "
        "ON plaid_accounts(cash_account_id) WHERE cash_account_id IS NOT NULL"
    )
    # phase 4 (SAFE.md): pay schedules, transactions, deposit rules, bills, goals
    conn.executescript(SAFE_SCHEMA)
    conn.executescript(SUBSCRIPTIONS_SCHEMA)
    ecols = {r[1] for r in conn.execute("PRAGMA table_info(expenses)")}
    if ecols and "paid_from" not in ecols:
        conn.execute("ALTER TABLE expenses ADD COLUMN paid_from TEXT NOT NULL DEFAULT 'cash'")
    if ecols and "match_text" not in ecols:
        conn.execute("ALTER TABLE expenses ADD COLUMN match_text TEXT")
    icols = {r[1] for r in conn.execute("PRAGMA table_info(income)")}
    if icols and "pay_schedule_id" not in icols:
        conn.execute(
            "ALTER TABLE income ADD COLUMN pay_schedule_id "
            "INTEGER REFERENCES pay_schedules(id) ON DELETE SET NULL"
        )
    pcols2 = {r[1] for r in conn.execute("PRAGMA table_info(payments)")}
    if pcols2 and "transaction_id" not in pcols2:
        conn.execute("ALTER TABLE payments ADD COLUMN transaction_id TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_transaction "
        "ON payments(transaction_id) WHERE transaction_id IS NOT NULL"
    )
    itcols = {r[1] for r in conn.execute("PRAGMA table_info(plaid_items)")}
    if itcols and "tx_cursor" not in itcols:
        conn.execute("ALTER TABLE plaid_items ADD COLUMN tx_cursor TEXT")
    # FIX5 R1: bank-reported card fields + user overrides that survive syncs.
    #   apr_source: bank | manual | unknown (synced card whose bank sends no APR)
    #   apr_override / min_override_cents: NULL = no override
    #   next_due_date / bank_overdue / statement_balance_cents: from Plaid liabilities
    ccols2 = {r[1] for r in conn.execute("PRAGMA table_info(cards)")}
    for col, ddl in (
        ("apr_source", "TEXT NOT NULL DEFAULT 'manual'"),
        ("apr_override", "REAL CHECK (apr_override IS NULL OR (apr_override >= 0 AND apr_override < 100))"),
        ("min_override_cents", "INTEGER CHECK (min_override_cents IS NULL OR min_override_cents >= 0)"),
        ("next_due_date", "TEXT"),
        ("bank_overdue", "INTEGER NOT NULL DEFAULT 0"),
        ("statement_balance_cents", "INTEGER"),
    ):
        if ccols2 and col not in ccols2:
            conn.execute(f"ALTER TABLE cards ADD COLUMN {col} {ddl}")
    # R12: rule_id links a rule-labelled transaction back to its deposit rule.
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
    if tcols and "rule_id" not in tcols:
        conn.execute(
            "ALTER TABLE transactions ADD COLUMN rule_id "
            "INTEGER REFERENCES deposit_rules(id) ON DELETE SET NULL"
        )


def init_db(db_path: str) -> sqlite3.Connection:
    """Run schema + migrations once on a direct connection, then return a
    ThreadLocalConnection proxy (R8): each thread gets its own connection."""
    direct = connect(db_path)
    try:
        direct.executescript(SCHEMA)
        direct.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")
        _migrate(direct)
        direct.commit()
    finally:
        direct.close()
    return ThreadLocalConnection(db_path)


@contextmanager
def tx(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


# --- settings ---------------------------------------------------------------

def get_settings(conn) -> dict:
    return one(conn, "SELECT * FROM settings WHERE id = 1")


def update_settings(conn, fields: dict) -> dict:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE settings SET {cols} WHERE id = 1", tuple(fields.values()))
    conn.commit()
    return get_settings(conn)


# --- generic CRUD -----------------------------------------------------------

_DEFAULT_ORDER = {
    "settings": "id",
    "income": "sort, id",
    "expenses": "sort, id",
    "cards": "id",
    "payments": "id",
    "balance_log": "id",
    "month_adjustments": "id",
    "subscriptions": "id",
}


def list_rows(conn, table: str, order: str | None = None) -> list[dict]:
    order = order or _DEFAULT_ORDER.get(table, "id")
    return rows(conn, f"SELECT * FROM {table} ORDER BY {order}")


def get_row(conn, table: str, row_id: int) -> dict | None:
    return one(conn, f"SELECT * FROM {table} WHERE id = ?", (row_id,))


def insert_row(conn, table: str, fields: dict) -> dict:
    cols = ", ".join(fields)
    qs = ", ".join("?" for _ in fields)
    cur = conn.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(fields.values())
    )
    conn.commit()
    return get_row(conn, table, cur.lastrowid)


def update_row(conn, table: str, row_id: int, fields: dict) -> dict | None:
    if fields:
        cols = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE {table} SET {cols} WHERE id = ?", (*fields.values(), row_id)
        )
        conn.commit()
    return get_row(conn, table, row_id)


def delete_row(conn, table: str, row_id: int) -> bool:
    cur = conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    conn.commit()
    return cur.rowcount > 0


# --- card-specific ----------------------------------------------------------

from datetime import date  # noqa: E402


def _today() -> str:
    return date.today().isoformat()


def create_card(conn, fields: dict) -> dict:
    fields = dict(fields)
    fields["starting_balance_cents"] = fields["balance_cents"]
    fields["created_at"] = _today()
    card = insert_row(conn, "cards", fields)
    log_balance(conn, card["id"], card["balance_cents"], "create")
    return card


def set_card_balance(conn, card_id: int, balance_cents: int) -> dict | None:
    card = update_row(conn, "cards", card_id, {"balance_cents": balance_cents})
    if card:
        log_balance(conn, card_id, balance_cents, "manual")
    return card


def log_balance(conn, card_id: int, balance_cents: int, source: str) -> None:
    conn.execute(
        "INSERT INTO balance_log (card_id, logged_on, balance_cents, source) VALUES (?, ?, ?, ?)",
        (card_id, _today(), balance_cents, source),
    )
    conn.commit()


def apply_payment(conn, card_id: int, amount_cents: int) -> None:
    conn.execute(
        "UPDATE cards SET balance_cents = MAX(0, balance_cents - ?) WHERE id = ?",
        (amount_cents, card_id),
    )
    log_balance(conn, card_id, get_row(conn, "cards", card_id)["balance_cents"], "payment")


def revert_payment(conn, card_id: int, amount_cents: int) -> None:
    conn.execute(
        "UPDATE cards SET balance_cents = balance_cents + ? WHERE id = ?",
        (amount_cents, card_id),
    )
    log_balance(conn, card_id, get_row(conn, "cards", card_id)["balance_cents"], "payment_delete")


# --- export / import --------------------------------------------------------
#
# S4: import order is derived from the actual foreign keys at import time
# (topological order: referenced tables first, delete in reverse), so adding a
# new FK to the schema cannot silently break import.

TABLES = [
    "settings", "pay_schedules", "deposit_rules", "income", "expenses", "goals",
    "goal_moves", "bill_payments", "cards", "payments", "balance_log",
    "month_adjustments", "cash_accounts", "subscriptions", "subscription_dismissals",
]


def _fk_order(conn, tables: list[str]) -> list[str]:
    """Tables ordered so every referenced table precedes its referrers."""
    deps: dict[str, set[str]] = {t: set() for t in tables}
    tset = set(tables)
    for t in tables:
        for fk in conn.execute(f"PRAGMA foreign_key_list({t})").fetchall():
            ref = fk["table"]
            if ref in tset and ref != t:
                deps[t].add(ref)
    out: list[str] = []
    while len(out) < len(tables):
        ready = [t for t in tables if t not in out and deps[t] <= set(out)]
        if not ready:  # cycle fallback: keep remaining in declared order
            out.extend(t for t in tables if t not in out)
            break
        out.extend(ready)
    return out


def export_all(conn) -> dict:
    return {t: rows(conn, f"SELECT * FROM {t} ORDER BY id") for t in TABLES}


def _table_columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def import_all(conn, data: dict) -> None:
    with tx(conn):
        tables = _fk_order(conn, TABLES)
        # Security (Opus review #2): import column names must never be
        # interpolated into SQL. Allow-list every key against PRAGMA
        # table_info and DROP keys that are not real columns — a crafted
        # backup file otherwise executes arbitrary INSERT ... SELECT.
        allowed_cols = {t: _table_columns(conn, t) for t in tables}
        # Deleting pay_schedules cascades ON DELETE SET NULL onto
        # transactions.pay_schedule_id (transactions are not part of the
        # import). Capture the links and restore them after import when the
        # schedule still exists (SAFE.md: transactions are left untouched).
        txn_links = rows(
            conn,
            "SELECT transaction_id, pay_schedule_id FROM transactions "
            "WHERE pay_schedule_id IS NOT NULL",
        )
        # R7: the DELETE FROM cash_accounts / cards below fires the FK
        # ON DELETE SET NULL on plaid_accounts, wiping the bank-account
        # links that the linked-cash-accounts joins depend on. Capture and
        # restore them after the import.
        pa_links = rows(
            conn,
            "SELECT id, card_id, cash_account_id FROM plaid_accounts "
            "WHERE card_id IS NOT NULL OR cash_account_id IS NOT NULL",
        )
        for t in reversed(tables):
            conn.execute(f"DELETE FROM {t}")
        for t in tables:
            allowed = allowed_cols[t]
            for r in data.get(t, []):
                if not isinstance(r, dict):
                    continue
                cols = [c for c in r.keys() if c in allowed]
                if not cols:
                    continue
                qs = ", ".join("?" for _ in cols)
                conn.execute(
                    f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({qs})",
                    tuple(r[c] for c in cols),
                )
        conn.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")
        # plaid_items / plaid_accounts are excluded from export and untouched by
        # import. S3: restore the card/cash links FIRST, before the
        # sync='manual' demotions below, so a plaid-synced card/cash account
        # whose link resolves against the imported rows keeps sync='plaid'.
        for r in pa_links:
            if r["card_id"] and one(conn, "SELECT id FROM cards WHERE id = ?", (r["card_id"],)):
                conn.execute("UPDATE plaid_accounts SET card_id = ? WHERE id = ?",
                             (r["card_id"], r["id"]))
            if r["cash_account_id"] and one(
                conn, "SELECT id FROM cash_accounts WHERE id = ?", (r["cash_account_id"],)
            ):
                conn.execute("UPDATE plaid_accounts SET cash_account_id = ? WHERE id = ?",
                             (r["cash_account_id"], r["id"]))
        # Then repair links that no longer match the imported rows.
        conn.execute(
            "UPDATE plaid_accounts SET card_id = NULL WHERE card_id IS NOT NULL "
            "AND card_id NOT IN (SELECT id FROM cards)"
        )
        conn.execute(
            "UPDATE cards SET sync = 'manual' WHERE sync = 'plaid' "
            "AND id NOT IN (SELECT card_id FROM plaid_accounts WHERE card_id IS NOT NULL)"
        )
        conn.execute(
            "UPDATE plaid_accounts SET cash_account_id = NULL WHERE cash_account_id IS NOT NULL "
            "AND cash_account_id NOT IN (SELECT id FROM cash_accounts)"
        )
        conn.execute(
            "UPDATE cash_accounts SET sync = 'manual' WHERE sync = 'plaid' "
            "AND id NOT IN (SELECT cash_account_id FROM plaid_accounts WHERE cash_account_id IS NOT NULL)"
        )
        # phase 4 (SAFE.md): transactions are bank data - excluded from export,
        # untouched by import. Clear references that no longer resolve.
        for r in txn_links:
            if one(conn, "SELECT id FROM pay_schedules WHERE id = ?", (r["pay_schedule_id"],)):
                conn.execute(
                    "UPDATE transactions SET pay_schedule_id = ? WHERE transaction_id = ?",
                    (r["pay_schedule_id"], r["transaction_id"]),
                )
        conn.execute(
            "UPDATE transactions SET pay_schedule_id = NULL WHERE pay_schedule_id IS NOT NULL "
            "AND pay_schedule_id NOT IN (SELECT id FROM pay_schedules)"
        )
        conn.execute(
            "UPDATE pay_schedules SET cash_account_id = NULL WHERE cash_account_id IS NOT NULL "
            "AND cash_account_id NOT IN (SELECT id FROM cash_accounts)"
        )
        conn.execute(
            "UPDATE deposit_rules SET pay_schedule_id = NULL WHERE pay_schedule_id IS NOT NULL "
            "AND pay_schedule_id NOT IN (SELECT id FROM pay_schedules)"
        )
        conn.execute(
            "UPDATE income SET pay_schedule_id = NULL WHERE pay_schedule_id IS NOT NULL "
            "AND pay_schedule_id NOT IN (SELECT id FROM pay_schedules)"
        )
