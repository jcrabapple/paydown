"""FIX5 R1 foundation: bank-reported card terms, overrides, effective APR/min.

Shapes mirror Plaid liabilities payloads where issuers omit `aprs` or report
`minimum_payment_amount: 0` with no statement.
"""
from __future__ import annotations

import json
from datetime import timedelta

from fastapi.testclient import TestClient

from app import cardfx, clock, db
from app.main import create_app
from app.plaid import map_liability, refresh_card_bank_fields


# --- cardfx (pure) -----------------------------------------------------------

def test_infer_apr_from_interest_charges():
    # ~$150/mo interest on a $6,500.00 statement -> ~27.7%
    apr = cardfx.infer_apr([15000, 14800, 15200], 650000)
    assert 26.0 < apr < 29.0


def test_infer_apr_ignores_tiny_and_missing():
    assert cardfx.infer_apr([90], 650000) is None          # sub-$1 residual interest
    assert cardfx.infer_apr([], 650000) is None
    assert cardfx.infer_apr([15000], 0) is None
    assert cardfx.infer_apr([500000], 1000) is None        # implausible (> 60%)


def test_estimate_min_formula_and_caps():
    # 1% of $5,000.00 + a month of 18% interest = $50.00 + $75.00 = $125.00
    assert cardfx.estimate_min_cents(500000, 18.0) == 12500
    assert cardfx.estimate_min_cents(1000, 20.0) == 1000    # never above balance
    assert cardfx.estimate_min_cents(100000, 0.0) == 2500   # $25 floor
    assert cardfx.estimate_min_cents(0, 25.0) == 0


def _card(**kw):
    base = {"id": 1, "apr": 0.0, "apr_source": "manual", "apr_override": None,
            "min_payment_cents": 0, "min_override_cents": None, "balance_cents": 500000,
            "statement_balance_cents": None, "sync": "plaid"}
    base.update(kw)
    return base


def test_effective_precedence():
    # unknown APR -> inferred
    e = cardfx.effective(_card(apr_source="unknown"), 18.2)
    assert (e["apr"], e["apr_source"]) == (18.2, "inferred")
    # unknown with no inference stays unknown
    e = cardfx.effective(_card(apr_source="unknown"), None)
    assert e["apr_source"] == "unknown"
    # override beats everything
    e = cardfx.effective(_card(apr_source="bank", apr=29.49, apr_override=21.0), 18.2)
    assert (e["apr"], e["apr_source"]) == (21.0, "override")
    # bank APR is not replaced by inference
    e = cardfx.effective(_card(apr_source="bank", apr=29.49), 18.2)
    assert (e["apr"], e["apr_source"]) == (29.49, "bank")


def test_effective_min_estimated_only_when_bank_silent():
    # synced, min 0, balance, no statement -> estimated
    e = cardfx.effective(_card(apr_source="unknown"), 18.0)
    assert e["min_source"] == "estimated" and e["min_payment_cents"] == 12500
    # min 0 but a statement exists (cycle already satisfied) -> keep bank 0
    e = cardfx.effective(_card(statement_balance_cents=400000, apr=24.99, apr_source="bank"))
    assert (e["min_payment_cents"], e["min_source"]) == (0, "bank")
    # override wins
    e = cardfx.effective(_card(min_override_cents=4100))
    assert (e["min_payment_cents"], e["min_source"]) == (4100, "override")
    # manual card with min 0 is left alone
    e = cardfx.effective(_card(sync="manual", min_payment_cents=0))
    assert (e["min_payment_cents"], e["min_source"]) == (0, "manual")


# --- plaid mapping -------------------------------------------------------------

def test_map_liability_captures_due_date_overdue_statement():
    credit = {"aprs": [], "minimum_payment_amount": 180, "next_payment_due_date": "2026-11-06",
              "is_overdue": False, "last_statement_balance": 6500.00}
    out = map_liability({"balances": {"current": 6500.00}}, credit, {"apr": 0.0})
    assert out["next_due_date"] == "2026-11-06"
    assert out["due_day"] == 6
    assert out["bank_overdue"] == 0
    assert out["statement_balance_cents"] == 650000
    assert "apr" not in out


def _seed_linked_card(conn, credit, apr=0.0, apr_source="manual"):
    today = clock.today().isoformat()
    card = db.create_card(conn, {"name": "Rewards Card", "balance_cents": 650000, "apr": apr,
                                 "min_payment_cents": 18000})
    conn.execute("UPDATE cards SET sync='plaid', apr_source=? WHERE id=?", (apr_source, card["id"]))
    conn.execute(
        "INSERT INTO plaid_items (item_id, access_token_enc, institution_name, status, created_at) "
        "VALUES ('item-1', 'x', 'Example Bank', 'ok', ?)", (today,))
    conn.execute(
        "INSERT INTO plaid_accounts (plaid_item_id, account_id, name, mask, type, card_id, last_payload) "
        "VALUES (1, 'acc-cc', 'Rewards Card', '1234', 'credit', ?, ?)",
        (card["id"], json.dumps({"account": {"balances": {"current": 6500.00}}, "credit": credit})))
    conn.commit()
    return card["id"]


def test_refresh_backfills_unknown_apr_and_due_date(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    cid = _seed_linked_card(conn, {"aprs": [], "minimum_payment_amount": 180,
                                   "next_payment_due_date": "2026-11-06", "is_overdue": False,
                                   "last_statement_balance": 6500.00})
    assert refresh_card_bank_fields(conn) == 1
    card = db.get_row(conn, "cards", cid)
    assert card["apr_source"] == "unknown"
    assert card["next_due_date"] == "2026-11-06"
    assert card["statement_balance_cents"] == 650000
    assert refresh_card_bank_fields(conn) == 0   # idempotent


def test_refresh_keeps_real_manual_apr(tmp_path):
    # A manual card with a real APR, later linked: bank sends no APR -> stays manual.
    conn = db.init_db(str(tmp_path / "b.db"))
    cid = _seed_linked_card(conn, {"aprs": []}, apr=24.99)
    refresh_card_bank_fields(conn)
    assert db.get_row(conn, "cards", cid)["apr_source"] == "manual"


def test_refresh_marks_bank_apr(tmp_path):
    conn = db.init_db(str(tmp_path / "b.db"))
    cid = _seed_linked_card(conn, {"aprs": [{"apr_percentage": 29.49, "apr_type": "purchase_apr",
                                             "balance_subject_to_apr": None}]})
    refresh_card_bank_fields(conn)
    assert db.get_row(conn, "cards", cid)["apr_source"] == "bank"


# --- API ------------------------------------------------------------------------

def _client(tmp_path):
    app = create_app(str(tmp_path / "api.db"), "u", "p")
    return TestClient(app, headers={"Authorization": "Basic dTpw"}), app


def test_api_infers_apr_and_feeds_engine(tmp_path):
    db_path = str(tmp_path / "api.db")
    conn = db.init_db(db_path)
    cid = _seed_linked_card(conn, {"aprs": [], "minimum_payment_amount": 180,
                                   "next_payment_due_date": "2026-11-06",
                                   "last_statement_balance": 6500.00})
    refresh_card_bank_fields(conn)
    acct_id = db.one(conn, "SELECT id FROM plaid_accounts WHERE account_id='acc-cc'")["id"]
    for i, cents in enumerate((15000, 14800, 15200)):
        d = (clock.today() - timedelta(days=5 + 30 * i)).isoformat()
        conn.execute(
            "INSERT INTO transactions (transaction_id, plaid_account_id, date, amount_cents, name, "
            "pending, pfc_detailed, updated_at) VALUES (?, ?, ?, ?, 'INTEREST CHARGE:PURCHASES', 0, "
            "'BANK_FEES_INTEREST_CHARGE', ?)", (f"int{i}", acct_id, d, cents, d))
    conn.commit()
    client = TestClient(create_app(db_path, "u", "p"), headers={"Authorization": "Basic dTpw"})
    card = next(c for c in client.get("/api/cards").json() if c["id"] == cid)
    assert card["apr_effective_source"] == "inferred"
    assert 26.0 < card["apr"] < 29.0
    assert card["bank_apr"] == 0.0
    assert card["apr_inferred"] == card["apr"]
    assert card["next_due_date"] == "2026-11-06"
    # The projection now charges interest on this card.
    proj = client.get("/api/projection?extra=0").json()
    assert proj["avalanche"]["total_interest_cents"] > 0


def test_api_override_set_and_clear(tmp_path):
    db_path = str(tmp_path / "api.db")
    conn = db.init_db(db_path)
    cid = _seed_linked_card(conn, {"aprs": [], "minimum_payment_amount": 0})
    refresh_card_bank_fields(conn)
    client = TestClient(create_app(db_path, "u", "p"), headers={"Authorization": "Basic dTpw"})
    r = client.put(f"/api/cards/{cid}", json={"apr_override": 27.24, "min_override_cents": 13500})
    assert r.status_code == 200, r.text
    c = r.json()
    assert (c["apr"], c["apr_effective_source"]) == (27.24, "override")
    assert (c["min_payment_cents"], c["min_source"]) == (13500, "override")
    # month plan uses the overridden minimum
    month = clock.today().strftime("%Y-%m")
    b = client.get(f"/api/month/{month}").json()
    assert b["budget"]["minimums_total_cents"] == 13500
    # explicit null clears
    c = client.put(f"/api/cards/{cid}", json={"apr_override": None, "min_override_cents": None}).json()
    assert c["apr_override"] is None and c["min_override_cents"] is None
    assert c["apr_effective_source"] == "unknown"
    # bad values rejected
    assert client.put(f"/api/cards/{cid}", json={"apr_override": 150}).status_code == 422
    assert client.put(f"/api/cards/{cid}", json={"min_override_cents": -1}).status_code == 422


def test_sync_does_not_clobber_overrides(tmp_path):
    from app.plaid import _apply_account_to_card
    conn = db.init_db(str(tmp_path / "b.db"))
    cid = _seed_linked_card(conn, {"aprs": [], "minimum_payment_amount": 0})
    conn.execute("UPDATE cards SET apr_override = 18.5, min_override_cents = 4100 WHERE id = ?", (cid,))
    conn.commit()
    acct = db.one(conn, "SELECT * FROM plaid_accounts WHERE card_id = ?", (cid,))
    _apply_account_to_card(conn, acct, None)
    card = db.get_row(conn, "cards", cid)
    assert card["apr_override"] == 18.5 and card["min_override_cents"] == 4100


def test_upcoming_uses_bank_due_date_and_splits_min_extra(tmp_path, monkeypatch):
    from datetime import date as _d
    monkeypatch.setattr(clock, "today", lambda: _d(2026, 10, 14))
    db_path = str(tmp_path / "api.db")
    conn = db.init_db(db_path)
    cid = _seed_linked_card(conn, {"aprs": [], "minimum_payment_amount": 180,
                                   "next_payment_due_date": "2026-11-06",
                                   "last_statement_balance": 6500.00})
    refresh_card_bank_fields(conn)
    conn.execute("UPDATE cards SET due_day = 6 WHERE id = ?", (cid,))
    conn.execute("INSERT INTO income (name, amount_cents) VALUES ('Pay', 500000)")
    conn.commit()
    client = TestClient(create_app(db_path, "u", "p"), headers={"Authorization": "Basic dTpw"})
    rows = [r for r in client.get("/api/upcoming?days=30").json() if r["kind"] == "card"]
    # due_day 6 would also produce a stale 2026-10-06 date; only the bank's Nov 6 counts
    assert [r["date"] for r in rows] == ["2026-11-06"]
    r = rows[0]
    assert r["min_remaining_cents"] == 18000
    assert r["extra_remaining_cents"] == r["amount_cents"] - 18000 > 0


def test_map_liability_clears_overdue_and_due_when_bank_stops_reporting():
    # Review F1: a stale bank_overdue=1 / old due date must not persist forever.
    out = map_liability({"balances": {"current": 10.0}}, {"aprs": [], "is_overdue": None}, {"apr": 12.5})
    assert out["bank_overdue"] == 0
    assert out["next_due_date"] is None


def test_estimated_min_assumes_typical_apr_when_unknown():
    # Review F4: unknown APR must not drop the interest part of the estimate.
    e = cardfx.effective(_card(apr_source="unknown", balance_cents=500000), None)
    assert e["min_source"] == "estimated"
    assert e["min_payment_cents"] > cardfx.estimate_min_cents(500000, 0.0)


def test_startup_survives_unexpected_automation_errors(tmp_path, monkeypatch):
    # Review F7: no automation failure may block boot.
    from app import txns as _txns
    def boom(*a, **k):
        raise ValueError("bad row")
    monkeypatch.setattr(_txns, "auto_log_card_payments", boom)
    import app.plaid as _plaid
    monkeypatch.setattr(_plaid, "refresh_card_bank_fields", boom)
    create_app(str(tmp_path / "boot.db"), "u", "p")  # must not raise
