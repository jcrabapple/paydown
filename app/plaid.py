"""Plaid integration: config, HTTP client, token encryption, liability mapping, sync.

Secrets rules:
- Access tokens are Fernet-encrypted before storage and decrypted only where a
  Plaid call needs them. Never returned by any route, never exported.
- Request bodies contain the client secret: nothing here logs them.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

import httpx
from cryptography.fernet import Fernet, InvalidToken

from . import clock
from . import db

CLIENT_NAME = "Paydown"
USER_ID = os.environ.get("PLAID_USER_ID", "paydown-user")


class PlaidError(Exception):
    def __init__(self, error_type: str, error_code: str, message: str, status: int):
        super().__init__(message)
        self.error_type = error_type
        self.error_code = error_code
        self.message = message
        self.status = status


@dataclass
class PlaidConfig:
    client_id: str
    client_secret: str
    env: str  # sandbox | production
    token_key: bytes
    redirect_uri: str | None = None

    @classmethod
    def from_env(cls) -> "PlaidConfig | None":
        client_id = os.environ.get("PLAID_CLIENT_ID")
        client_secret = os.environ.get("PLAID_CLIENT_SECRET")
        token_key = os.environ.get("PLAID_TOKEN_KEY")
        if not client_id or not client_secret or not token_key:
            return None
        env = os.environ.get("PLAID_ENV", "production")
        if env not in ("sandbox", "production"):
            env = "production"
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            env=env,
            token_key=token_key.encode(),
            redirect_uri=os.environ.get("PLAID_REDIRECT_URI") or None,
        )


def _cents(value) -> int | None:
    """Plaid float dollars -> integer cents, round half-up."""
    if value is None:
        return None
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _now() -> str:
    # R11: go through app.clock so tests can freeze time.
    return clock.now_iso()


def _today() -> str:
    return date.today().isoformat()


# --- token encryption -------------------------------------------------------

def encrypt_token(key: bytes, token: str) -> str:
    return Fernet(key).encrypt(token.encode()).decode()


def decrypt_token(key: bytes, enc: str) -> str:
    try:
        return Fernet(key).decrypt(enc.encode()).decode()
    except InvalidToken as e:  # pragma: no cover - corrupt DB, nothing to recover
        raise PlaidError("API_ERROR", "TOKEN_DECRYPT", "stored access token unreadable", 0) from e


# --- HTTP client ------------------------------------------------------------

class PlaidClient:
    def __init__(self, config: PlaidConfig, transport: httpx.BaseTransport | None = None):
        self.config = config
        self.base = f"https://{config.env}.plaid.com"
        self._client = httpx.Client(timeout=30, transport=transport)

    def _post(self, path: str, payload: dict) -> dict:
        try:
            resp = self._client.post(f"{self.base}{path}", json=payload)
        except httpx.HTTPError as e:
            # Timeouts/DNS/TLS failures: surface as a PlaidError so callers
            # (sync_all, routes) handle them like any other Plaid failure.
            raise PlaidError("NETWORK_ERROR", "NETWORK_ERROR", type(e).__name__, 0) from None
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code != 200 or data.get("error_code"):
            raise PlaidError(
                data.get("error_type", "API_ERROR"),
                data.get("error_code", "UNKNOWN"),
                data.get("error_message", ""),
                resp.status_code,
            )
        return data

    def _auth(self) -> dict:
        return {"client_id": self.config.client_id, "secret": self.config.client_secret}

    def link_token_create(self, access_token: str | None = None) -> dict:
        body = self._auth() | {
            "client_name": CLIENT_NAME,
            "language": "en",
            "country_codes": ["US"],
            "user": {"client_user_id": USER_ID},
        }
        if self.config.redirect_uri:
            body["redirect_uri"] = self.config.redirect_uri
        if access_token:
            body["access_token"] = access_token
        else:
            # transactions keeps depository balances refreshed daily; liabilities
            # is added whenever the bank has a card (docs/CASH.md).
            body["products"] = ["transactions"]
            body["required_if_supported_products"] = ["liabilities"]
            body["account_filters"] = {
                "credit": {"account_subtypes": ["credit card"]},
                "depository": {"account_subtypes": ["checking", "savings"]},
            }
        return self._post("/link/token/create", body)

    def exchange(self, public_token: str) -> dict:
        return self._post("/item/public_token/exchange", self._auth() | {"public_token": public_token})

    def accounts_get(self, access_token: str) -> dict:
        return self._post("/accounts/get", self._auth() | {"access_token": access_token})

    def liabilities_get(self, access_token: str) -> dict:
        return self._post("/liabilities/get", self._auth() | {"access_token": access_token})

    def transactions_sync(self, access_token: str, cursor: str | None = None, count: int = 500) -> dict:
        body = self._auth() | {"access_token": access_token, "count": count}
        if cursor is not None:
            body["cursor"] = cursor
        return self._post("/transactions/sync", body)

    def encrypt_token(self, token: str) -> str:
        return encrypt_token(self.config.token_key, token)

    def decrypt_token(self, enc: str) -> str:
        return decrypt_token(self.config.token_key, enc)


# --- mapping (pure) ---------------------------------------------------------

def _pick_apr(credit: dict | None, existing: dict | None) -> tuple[float | None, str]:
    """Returns (apr, source). weighted | purchase | manual."""
    aprs = (credit or {}).get("aprs") or []
    weighted = [
        (a.get("apr_percentage"), a.get("balance_subject_to_apr"))
        for a in aprs
        if (a.get("balance_subject_to_apr") or 0) > 0 and a.get("apr_percentage") is not None
    ]
    if weighted:
        total = sum(b for _, b in weighted)
        apr = round(sum(p * b for p, b in weighted) / total, 2)
        return apr, "weighted"
    for a in aprs:
        is_purchase = (
            (a.get("name") or "").lower() == "purchase"
            or a.get("type") == "purchase"
            or a.get("apr_type") == "purchase_apr"  # Plaid's actual field
            or "purchase_apr" in a
        )
        if is_purchase and a.get("apr_percentage") is not None:
            return a["apr_percentage"], "purchase"
    return None, "manual"  # keep the existing APR


def map_liability(account: dict | None, credit: dict | None, existing: dict | None = None) -> dict:
    """Plaid account (+ credit liability) -> fields to apply to a card row.

    Keys absent from the result mean "keep the existing value".
    """
    out: dict = {}
    balances = (account or {}).get("balances") or {}
    current = _cents(balances.get("current"))
    if current is not None:
        out["balance_cents"] = max(0, current)
    limit = _cents(balances.get("limit"))
    if limit is not None:
        out["credit_limit_cents"] = limit
    if not credit:
        return out
    min_pay = credit.get("minimum_payment_amount")
    if min_pay is not None:
        out["min_payment_cents"] = _cents(min_pay) or 0
    due = credit.get("next_payment_due_date")
    # The bank's current view replaces the stored one: a due date or overdue
    # flag the bank stopped reporting must not linger (review F1).
    out["next_due_date"] = None
    if due:
        try:
            out["due_day"] = int(due[8:10])
            out["next_due_date"] = str(due)[:10]
        except (ValueError, IndexError):
            pass
    out["bank_overdue"] = 1 if credit.get("is_overdue") else 0
    stmt = _cents(credit.get("last_statement_balance"))
    if stmt is not None:
        out["statement_balance_cents"] = max(0, stmt)
    apr, source = _pick_apr(credit, existing)
    out["apr_source"] = source
    if apr is not None:
        out["apr"] = apr
    return out


# --- sync -------------------------------------------------------------------

def _apply_account_to_card(conn, acct_row: dict, item_row: dict) -> bool:
    """Apply an account's stored last_payload to its linked card. Returns True
    if the card was touched. Used by sync_item and the link route."""
    card_id = acct_row.get("card_id")
    if not card_id:
        return False
    card = db.get_row(conn, "cards", card_id)
    if not card:
        return False
    try:
        payload = json.loads(acct_row["last_payload"] or "{}")
    except (ValueError, TypeError):
        payload = {}
    mapped = map_liability(payload.get("account"), payload.get("credit"), card)
    _apr_source_column(mapped, card)
    mapped["last_synced_at"] = _now()
    mapped["sync"] = "plaid"
    new_balance = mapped.get("balance_cents")
    changed = new_balance is not None and new_balance != card["balance_cents"]
    has_today = db.one(
        conn,
        "SELECT id FROM balance_log WHERE card_id = ? AND source = 'plaid' AND logged_on = ?",
        (card_id, _today()),
    )
    db.update_row(conn, "cards", card_id, mapped)
    if changed or not has_today:
        db.log_balance(conn, card_id, new_balance if new_balance is not None else card["balance_cents"], "plaid")
    return True


def _apr_source_column(mapped: dict, card: dict | None) -> None:
    """Translate map_liability's apr_source (weighted|purchase|manual) into the
    cards.apr_source column (bank|manual|unknown), in place.

    The bank supplied an APR -> 'bank'. No APR from the bank -> keep the card's
    current source, except a synced card that never had a real APR (still the
    0.0 placeholder from link-create, source 'manual' by migration default)
    becomes 'unknown' so the UI asks for it and inference can fill in."""
    src = mapped.pop("apr_source", None)
    if src in ("weighted", "purchase"):
        mapped["apr_source"] = "bank"
        return
    current = (card or {}).get("apr_source") or "manual"
    if current == "bank":
        return  # bank stopped sending it; last bank value stays authoritative
    if not (card or {}).get("apr"):
        mapped["apr_source"] = "unknown"


def refresh_card_bank_fields(conn) -> int:
    """Re-derive the FIX5 bank columns (apr_source, next_due_date, bank_overdue,
    statement_balance_cents) for every linked card from its stored last_payload,
    without touching balances or balance_log. Idempotent; returns cards updated."""
    n = 0
    for acct in db.rows(
        conn, "SELECT * FROM plaid_accounts WHERE card_id IS NOT NULL AND last_payload IS NOT NULL"
    ):
        card = db.get_row(conn, "cards", acct["card_id"])
        if not card:
            continue
        try:
            payload = json.loads(acct["last_payload"] or "{}")
        except (ValueError, TypeError):
            continue
        mapped = map_liability(payload.get("account"), payload.get("credit"), card)
        _apr_source_column(mapped, card)
        fields = {
            k: mapped[k]
            for k in ("apr_source", "next_due_date", "bank_overdue", "statement_balance_cents")
            if k in mapped and mapped[k] != card.get(k)
        }
        if fields:
            db.update_row(conn, "cards", card["id"], fields)
            n += 1
    return n


NO_CREDIT_DETAIL_CODES = {"PRODUCTS_NOT_SUPPORTED", "NO_LIABILITY_ACCOUNTS", "PRODUCT_NOT_ENABLED"}


def _depository_balance_cents(acct: dict) -> int | None:
    b = acct.get("balances") or {}
    cents = _cents(b.get("available"))
    if cents is None:
        cents = _cents(b.get("current"))
    return cents


def _apply_account_to_cash(conn, acct_row: dict) -> bool:
    """Push a depository account's synced balance onto its linked cash account."""
    cash_account_id = acct_row.get("cash_account_id")
    if not cash_account_id or (acct_row.get("type") or "") != "depository":
        return False
    if acct_row.get("last_balance_cents") is None:
        return False
    now = _now()
    db.update_row(
        conn, "cash_accounts", cash_account_id,
        {
            "balance_cents": acct_row["last_balance_cents"],
            "sync": "plaid",
            "last_synced_at": now,
            "balance_updated_at": now,
        },
    )
    return True


# /transactions/sync errors that mean "no transaction data yet" - skip the
# step entirely and leave the item ok.
TXN_SKIP_CODES = {"PRODUCT_NOT_READY", "PRODUCTS_NOT_SUPPORTED", "PRODUCT_NOT_ENABLED"}
TXN_MUTATION_CODE = "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"
TXN_MAX_ATTEMPTS = 3


def _txn_account_map(conn) -> dict[str, dict]:
    """Stored plaid_accounts allowed to hold transactions, keyed by account_id."""
    return {
        r["account_id"]: r
        for r in db.rows(
            conn,
            "SELECT id, account_id, type FROM plaid_accounts WHERE type IN ('depository', 'credit')",
        )
    }


def _txn_row_fields(t: dict, plaid_account_id: int) -> dict:
    pfc = t.get("personal_finance_category") or {}
    return {
        "plaid_account_id": plaid_account_id,
        "date": t.get("date"),
        "authorized_date": t.get("authorized_date"),
        "amount_cents": _cents(t.get("amount")) or 0,
        "name": t.get("name") or "",
        "merchant_name": t.get("merchant_name"),
        "pending": 1 if t.get("pending") else 0,
        "pending_transaction_id": t.get("pending_transaction_id"),
        "pfc_primary": pfc.get("primary"),
        "pfc_detailed": pfc.get("detailed"),
        "updated_at": _now(),
    }


def _upsert_txn(conn, acct_map: dict, t: dict) -> None:
    """Store one added transaction (ignores accounts we don't track)."""
    acct = acct_map.get(t.get("account_id"))
    if not acct:
        return
    fields = _txn_row_fields(t, acct["id"])
    existing = db.one(conn, "SELECT transaction_id FROM transactions WHERE transaction_id = ?", (t["transaction_id"],))
    if existing:
        conn.execute(
            "UPDATE transactions SET %s WHERE transaction_id = ?" % ", ".join(f"{k} = ?" for k in fields),
            (*fields.values(), t["transaction_id"]),
        )
    else:
        fields["transaction_id"] = t["transaction_id"]
        cols = ", ".join(fields)
        conn.execute(
            f"INSERT INTO transactions ({cols}) VALUES ({', '.join('?' for _ in fields)})",
            tuple(fields.values()),
        )


def _modify_txn(conn, acct_map: dict, t: dict) -> None:
    """Apply a modified transaction; label fields are preserved."""
    acct = acct_map.get(t.get("account_id"))
    if not acct:
        return
    fields = _txn_row_fields(t, acct["id"])
    conn.execute(
        "UPDATE transactions SET %s WHERE transaction_id = ?" % ", ".join(f"{k} = ?" for k in fields),
        (*fields.values(), t["transaction_id"]),
    )


def _apply_txn_changes(conn, added: list, modified: list, removed: list,
                       next_cursor: str | None, item_id: int) -> dict:
    """Store all changes and the next cursor in ONE transaction: the cursor
    must only be saved when everything commits."""
    acct_map = _txn_account_map(conn)
    with db.tx(conn):
        for t in added:
            _upsert_txn(conn, acct_map, t)
        for t in modified:
            _modify_txn(conn, acct_map, t)
        # carry labels from pending rows onto their posted versions BEFORE the
        # removed list (which normally contains the pending row) is applied
        _carry_pending_labels(conn)
        for t in removed:
            tid = t.get("transaction_id")
            if tid:
                conn.execute("DELETE FROM transactions WHERE transaction_id = ?", (tid,))
        conn.execute("UPDATE plaid_items SET tx_cursor = ? WHERE id = ?", (next_cursor, item_id))
    return {"added": len(added), "modified": len(modified), "removed": len(removed)}


def _carry_pending_labels(conn) -> None:
    """Pending -> posted: a posted txn whose pending_transaction_id matches a
    stored labelled row inherits label, label_source and pay_schedule_id.
    Any leftover stored pending row with that id is removed."""
    for row in db.rows(
        conn,
        "SELECT transaction_id, pending_transaction_id FROM transactions "
        "WHERE pending = 0 AND pending_transaction_id IS NOT NULL",
    ):
        src = db.one(
            conn, "SELECT label, label_source, pay_schedule_id FROM transactions "
            "WHERE transaction_id = ? AND label IS NOT NULL",
            (row["pending_transaction_id"],),
        )
        if src:
            conn.execute(
                "UPDATE transactions SET label = ?, label_source = ?, pay_schedule_id = ? "
                "WHERE transaction_id = ?",
                (src["label"], src["label_source"], src["pay_schedule_id"], row["transaction_id"]),
            )
            conn.execute(
                "DELETE FROM transactions WHERE transaction_id = ?",
                (row["pending_transaction_id"],),
            )


def _sync_transactions(conn, client: PlaidClient, item_row: dict, token: str) -> dict | None:
    """Sync bank transactions for one item. Returns counts, or None to skip."""
    attempts = 0
    while True:
        attempts += 1
        cursor = item_row["tx_cursor"]
        added: list = []
        modified: list = []
        removed: list = []
        next_cursor: str | None = None
        try:
            while True:
                data = client.transactions_sync(token, cursor)
                added += data.get("added") or []
                modified += data.get("modified") or []
                removed += data.get("removed") or []
                if not data.get("has_more"):
                    next_cursor = data.get("next_cursor")
                    break
                cursor = data.get("next_cursor")
        except PlaidError as e:
            if e.error_code == TXN_MUTATION_CODE and attempts < TXN_MAX_ATTEMPTS:
                # Something changed server-side mid-pagination: restart from
                # the item's saved cursor.
                item_row = db.get_row(conn, "plaid_items", item_row["id"])
                continue
            if e.error_code in TXN_SKIP_CODES:
                return None
            raise
        return _apply_txn_changes(conn, added, modified, removed, next_cursor, item_row["id"])


def sync_item(conn, client: PlaidClient, item_row: dict) -> dict:
    item_id = item_row["id"]

    def finish(status: str, error_code: str | None, accounts_updated: int = 0,
               transactions_added: int = 0, transactions_modified: int = 0,
               transactions_removed: int = 0) -> dict:
        db.update_row(
            conn, "plaid_items", item_id,
            {"status": status, "error_code": error_code, "last_synced_at": _now()},
        )
        return {
            "id": item_id,
            "institution_name": item_row["institution_name"],
            "status": status,
            "error_code": error_code,
            "accounts_updated": accounts_updated,
            "transactions_added": transactions_added,
            "transactions_modified": transactions_modified,
            "transactions_removed": transactions_removed,
        }

    try:
        token = client.decrypt_token(item_row["access_token_enc"])
        data = client.accounts_get(token)
    except PlaidError as e:
        if e.error_code == "ITEM_LOGIN_REQUIRED":
            return finish("login_required", e.error_code)
        return finish("error", e.error_code)

    all_accounts = data.get("accounts") or []
    # Liabilities details only when the bank returned a credit account.
    credits: dict[str, dict] = {}
    if any(a.get("type") == "credit" for a in all_accounts):
        try:
            ldata = client.liabilities_get(token)
        except PlaidError as e:
            if e.error_code == "ITEM_LOGIN_REQUIRED":
                return finish("login_required", e.error_code)
            if e.error_code not in NO_CREDIT_DETAIL_CODES:
                return finish("error", e.error_code)
            ldata = {}  # no credit details; cards still get balance/limit
        credits = {
            c.get("account_id"): c for c in (ldata.get("liabilities") or {}).get("credit") or []
        }

    accounts_updated = 0
    for acct in all_accounts:
        if acct.get("type") not in ("credit", "depository"):
            continue
        payload = json.dumps({"account": acct, "credit": credits.get(acct.get("account_id"))})
        if acct.get("type") == "credit":
            last_balance = _cents((acct.get("balances") or {}).get("current"))
        else:
            last_balance = _depository_balance_cents(acct)
        row = db.one(conn, "SELECT * FROM plaid_accounts WHERE account_id = ?", (acct.get("account_id"),))
        fields = {
            "name": acct.get("name") or "",
            "official_name": acct.get("official_name"),
            "mask": acct.get("mask"),
            "type": acct.get("type"),
            "subtype": acct.get("subtype"),
            "last_balance_cents": last_balance,
            "last_payload": payload,
        }
        if row:
            db.update_row(conn, "plaid_accounts", row["id"], fields)
            row = db.get_row(conn, "plaid_accounts", row["id"])  # fresh payload
        else:
            fields.update(
                {"plaid_item_id": item_id, "account_id": acct.get("account_id"),
                 "card_id": None, "cash_account_id": None, "ignored": 0}
            )
            row = db.insert_row(conn, "plaid_accounts", fields)
        accounts_updated += 1
        _apply_account_to_card(conn, row, item_row)
        _apply_account_to_cash(conn, row)

    txn_counts: dict | None = None
    try:
        txn_counts = _sync_transactions(conn, client, item_row, token)
    except PlaidError as e:
        if e.error_code == "ITEM_LOGIN_REQUIRED":
            return finish("login_required", e.error_code, accounts_updated)
        return finish("error", e.error_code, accounts_updated)
    if txn_counts is not None:
        from . import clock as _clock
        from . import txns as _txns
        _txns.apply_rules(conn)
        _txns.run_bill_matching(conn, _clock.today())
        # B2 (FIX5 Round 2): auto-label salary deposits after apply_rules.
        _txns.auto_label_salary(conn)
        # B1 (FIX5 Round 2): auto-log/link card payments when the setting is on.
        if db.get_settings(conn).get("auto_log_card_payments", 1):
            _txns.auto_log_card_payments(conn, _clock.today())
    return finish("ok", None, accounts_updated,
                  transactions_added=(txn_counts or {}).get("added", 0),
                  transactions_modified=(txn_counts or {}).get("modified", 0),
                  transactions_removed=(txn_counts or {}).get("removed", 0))


def sync_all(conn, client: PlaidClient) -> dict:
    results = []
    for item in db.list_rows(conn, "plaid_items"):
        try:
            results.append(sync_item(conn, client, item))
        except PlaidError as e:
            # One item failing must not stop the others.
            db.update_row(conn, "plaid_items", item["id"], {"status": "error", "error_code": e.error_code, "last_synced_at": _now()})
            results.append({"id": item["id"], "institution_name": item["institution_name"],
                            "status": "error", "error_code": e.error_code, "accounts_updated": 0})
    return {"items": results, "synced_at": _now()}


# --- plan balance -----------------------------------------------------------

def plan_balance(conn, card: dict, month: str) -> int:
    """Balance a card contributes to the month plan.

    Manual cards: B1 rule - live balance plus payments logged with month >= M.
    Synced cards: frozen snapshot - the balance of the EARLIEST balance_log row
    whose logged_on falls in month M (any source), else the live balance. The
    bank balance is authoritative and may move mid-month; recommendations must
    not.
    """
    if card.get("sync") == "plaid":
        row = db.one(
            conn,
            "SELECT balance_cents FROM balance_log WHERE card_id = ? AND substr(logged_on, 1, 7) = ? "
            "ORDER BY id LIMIT 1",
            (card["id"], month),
        )
        return row["balance_cents"] if row else card["balance_cents"]
    row = db.one(
        conn,
        "SELECT COALESCE(SUM(amount_cents), 0) AS t FROM payments WHERE card_id = ? AND month >= ?",
        (card["id"], month),
    )
    return (row["t"] if row else 0) + card["balance_cents"]