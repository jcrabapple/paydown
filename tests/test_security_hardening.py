"""Security hardening (Opus review 2026-10-18).

Covers: CSRF origin checks on mutating requests (#1), import column
allow-list (#2), auth rate limiting (#3), service-worker cache narrowing is
static (sw.js, not tested here), Plaid item removal (#5), icon-exemption
traversal (#7), import size cap (#10).
"""
from __future__ import annotations

import json

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import auth as auth_mod
from app.main import create_app
from app.plaid import PlaidClient, PlaidConfig

AUTH = ("paydown", "test")


@pytest.fixture()
def client(tmp_path):
    import base64
    app = create_app(str(tmp_path / "budget.db"), "paydown", "test")
    token = base64.b64encode(b"paydown:test").decode()
    with TestClient(app, headers={"Authorization": f"Basic {token}"}) as c:
        yield c


# --- #1 CSRF: mutating requests with a foreign Origin are rejected ------------

def test_csrf_foreign_origin_rejected(client):
    r = client.post(
        "/api/card-payments/log-all",
        headers={"Origin": "https://evil.example",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 403


def test_csrf_matching_origin_allowed(client):
    r = client.post("/api/card-payments/log-all", headers={"Origin": "http://testserver"})
    assert r.status_code == 200


def test_csrf_no_origin_api_client_allowed(client):
    # Non-browser API clients send no Origin: they keep working.
    r = client.post("/api/card-payments/log-all")
    assert r.status_code == 200


def test_csrf_cross_site_fetch_site_rejected(client):
    r = client.post("/api/card-payments/log-all", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_csrf_get_with_foreign_origin_allowed(client):
    # Only state-changing methods are protected; GET stays readable (no CORS anyway).
    r = client.get("/api/cards", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200


def test_csrf_tunnel_host_origin_allowed(client):
    # Behind a tunnel the browser Origin host equals the Host header (port-less https).
    r = client.post(
        "/api/card-payments/log-all",
        headers={"Origin": "https://budget.example.com", "Host": "budget.example.com"},
    )
    assert r.status_code == 200


# --- #2 import column allow-list ---------------------------------------------

def test_import_sqli_column_injection_is_neutralized(client):
    payload = {"expenses": [{"name, amount_cents) SELECT sqlite_version()||?, 1 --": "-x"}]}
    assert client.post("/api/import", json=payload).status_code == 200
    names = [e["name"] for e in client.get("/api/expenses").json()]
    assert not any("3." in n for n in names)  # no sqlite_version() exfiltrated


def test_import_still_roundtrips_valid_rows(client):
    client.post("/api/income", json={"name": "Pay", "amount_cents": 100000})
    dump = client.get("/api/export").json()
    assert client.post("/api/import", json=dump).status_code == 200
    assert client.get("/api/income").json()[0]["name"] == "Pay"


# --- #3 auth rate limiting -----------------------------------------------------

def test_rate_limit_locks_out_after_failures(client, monkeypatch):
    monkeypatch.setattr(auth_mod, "FAIL_THRESHOLD", 3)
    monkeypatch.setattr(auth_mod, "COOLDOWN_BASE_S", 0.05)
    codes = []
    for _ in range(5):
        r = client.get("/api/cards", auth=("paydown", "wrong"), headers={"Authorization": ""})
        codes.append(r.status_code)
    assert 429 in codes
    # the 429 responses come after the threshold is hit
    assert codes[:3] == [401, 401, 401]
    assert codes[3] == 429
    # a successful auth (after the short cooldown) resets the counter
    import time
    time.sleep(0.3)
    assert client.get("/api/cards").status_code == 200


# --- #5 Plaid item removal -----------------------------------------------------

def _plaid_app(tmp_path, calls):
    import base64
    config = PlaidConfig(client_id="c", client_secret="s", env="sandbox",
                         token_key=Fernet.generate_key(), redirect_uri=None)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content or b"{}")))
        if request.url.path == "/item/remove":
            return httpx.Response(200, json={"removed": True})
        return httpx.Response(200, json={"accounts": [], "item": {}})

    client = PlaidClient(config, transport=httpx.MockTransport(handler))
    app = create_app(str(tmp_path / "b.db"), "paydown", "test", plaid=client)
    token = base64.b64encode(b"paydown:test").decode()
    return TestClient(app, headers={"Authorization": f"Basic {token}"}), config


def test_delete_plaid_item_removes_at_plaid_and_locally(tmp_path):
    calls: list = []
    c, config = _plaid_app(tmp_path, calls)
    conn = c.app.state.db
    from cryptography.fernet import Fernet as F
    from app.plaid import encrypt_token
    conn.execute(
        "INSERT INTO plaid_items (item_id, access_token_enc, institution_name, status, created_at) "
        "VALUES ('item-1', ?, 'Chase', 'ok', '2026-10-01')",
        (encrypt_token(config.token_key, "access-tok"),))
    conn.execute(
        "INSERT INTO plaid_accounts (plaid_item_id, account_id, name, type, last_payload) "
        "VALUES (1, 'acc-1', 'Checking', 'depository', '{}')")
    conn.execute(
        "INSERT INTO transactions (transaction_id, plaid_account_id, date, amount_cents, name, "
        "pending, updated_at) VALUES ('t1', 1, '2026-10-01', 100, 'X', 0, '2026-10-01')")
    conn.commit()

    r = c.delete("/api/plaid/items/1")
    assert r.status_code == 204
    assert ("/item/remove", {"client_id": "c", "secret": "s", "access_token": "access-tok"}) in calls
    assert conn.execute("SELECT COUNT(*) FROM plaid_items").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM plaid_accounts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_delete_plaid_item_404_unknown(tmp_path):
    c, _ = _plaid_app(tmp_path, [])
    assert c.delete("/api/plaid/items/999").status_code == 404


# --- #7 icon exemption cannot traverse ------------------------------------------

def test_icon_traversal_requires_auth(tmp_path):
    # Fresh client WITHOUT default auth: public-by-accident would show as 200.
    import base64
    app = create_app(str(tmp_path / "t.db"), "paydown", "test")
    anon = TestClient(app)
    # %2e%2e survives httpx URL normalization and decodes to '..' server-side.
    r = anon.get("/icons/%2e%2e/index.html")
    assert r.status_code == 401
    r = anon.get("/icons/%2f../index.html")
    assert r.status_code == 401


def test_public_path_regex_rejects_dot_segments():
    from app.auth import _PUBLIC_PATH_RE
    assert _PUBLIC_PATH_RE.fullmatch("/icons/icon.svg")
    assert not _PUBLIC_PATH_RE.fullmatch("/icons/../index.html")
    assert not _PUBLIC_PATH_RE.fullmatch("/icons//index.html")
    assert not _PUBLIC_PATH_RE.fullmatch("/icons/sub/icon.png")


def test_icon_files_stay_public(client):
    assert client.get("/icons/icon.svg").status_code == 200


# --- #10 import size cap ---------------------------------------------------------

def test_import_rejects_oversized_body(client, monkeypatch):
    monkeypatch.setattr(auth_mod, "MAX_BODY_BYTES", 1000)
    big = {"expenses": [{"name": "x" * 200, "amount_cents": 1} for _ in range(50)]}
    r = client.post("/api/import", json=big)
    assert r.status_code == 413


# --- headers (#8) ----------------------------------------------------------------

def test_security_headers_present(client):
    r = client.get("/api/health")
    csp = r.headers["Content-Security-Policy"]
    assert "form-action 'self'" in csp
    assert "base-uri 'none'" in csp
    assert r.headers["Strict-Transport-Security"] == "max-age=31536000"
    assert "camera=()" in r.headers["Permissions-Policy"]
