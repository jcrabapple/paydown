"""API1 (docs/API1.md): mobile-readiness round.

- CSRF guard must keep allowing origin-less mutations (Flutter http sends no
  Origin header; D4) while blocking cross-site ones.
- POST /api/plaid/link-token passes android_package_name through to Plaid.
- GET /api/mobile/home composes the five home-screen payloads with identical
  semantics to the individual endpoints.
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.test_plaid import AUTH, LINK_ROUTES, make_client

MONTH = date.today().strftime("%Y-%m")


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "budget.db"), "user", "test")
    with TestClient(app) as c:
        yield c


def _seed(client):
    client.post("/api/income", json={"name": "Salary", "amount_cents": 400000}, auth=AUTH)
    client.post("/api/cards", json={"name": "Card A", "balance_cents": 500000, "apr": 24.99,
                                    "min_payment_cents": 15000}, auth=AUTH)
    client.post("/api/expenses", json={"name": "Rent", "amount_cents": 120000,
                                       "kind": "bill", "due_day": 1}, auth=AUTH)


# --- C3: CSRF guard pins ------------------------------------------------------

def test_mutation_without_origin_passes(client):
    """Non-browser clients (mobile) send no Origin and must keep working."""
    r = client.post("/api/expenses", json={"name": "Net", "amount_cents": 5000},
                    auth=AUTH)
    assert r.status_code == 201
    r = client.post("/api/expenses", json={"name": "Net2", "amount_cents": 5000},
                    auth=AUTH, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


# --- C1: android_package_name passthrough --------------------------------------

def test_link_token_android_package_passthrough(tmp_path):
    calls = []
    c = make_client(tmp_path, LINK_ROUTES, calls)
    r = c.post("/api/plaid/link-token",
               json={"android_package_name": "com.example.paydown"}, auth=AUTH)
    assert r.status_code == 200
    _, body = calls[-1]
    assert body["android_package_name"] == "com.example.paydown"


def test_link_token_omitted_package_sends_none(tmp_path):
    calls = []
    c = make_client(tmp_path, LINK_ROUTES, calls)
    r = c.post("/api/plaid/link-token", json={}, auth=AUTH)
    assert r.status_code == 200
    _, body = calls[-1]
    assert "android_package_name" not in body


def test_link_token_android_wins_over_web_redirect(tmp_path):
    """Plaid rejects tokens carrying both redirect_uri and android_package_name;
    a mobile request must therefore drop the web redirect_uri (D5)."""
    from app.plaid import PlaidConfig
    from cryptography.fernet import Fernet
    config = PlaidConfig(client_id="cid", client_secret="cs", env="sandbox",
                         token_key=Fernet.generate_key(),
                         redirect_uri="https://budget.example.com/plaid-oauth.html")
    calls = []
    c = make_client(tmp_path, LINK_ROUTES, calls, config=config)
    r = c.post("/api/plaid/link-token",
               json={"android_package_name": "com.example.paydown"}, auth=AUTH)
    assert r.status_code == 200
    _, body = calls[-1]
    assert body["android_package_name"] == "com.example.paydown"
    assert "redirect_uri" not in body
    # Web flow keeps its redirect.
    r = c.post("/api/plaid/link-token", json={}, auth=AUTH)
    assert r.status_code == 200
    _, body = calls[-1]
    assert body["redirect_uri"] == "https://budget.example.com/plaid-oauth.html"
    assert "android_package_name" not in body


def test_link_token_rejects_invalid_package_name(tmp_path):
    calls = []
    c = make_client(tmp_path, LINK_ROUTES, calls)
    for bad in ("not a package", "Us.Bad", "a" * 64 + ".x", "..", ""):
        r = c.post("/api/plaid/link-token",
                   json={"android_package_name": bad}, auth=AUTH)
        assert r.status_code == 422, bad


# --- C2: /api/mobile/home -------------------------------------------------------

def test_mobile_home_shape_matches_individual_endpoints(client):
    _seed(client)
    r = client.get("/api/mobile/home", auth=AUTH)
    assert r.status_code == 200
    home = r.json()
    assert home["month"] == MONTH
    assert set(home) >= {"month", "today", "budget", "settings", "plan",
                         "paid_total_cents", "recommended_total_cents",
                         "projection", "cash", "cards", "safe_to_spend",
                         "upcoming", "reminders"}

    bundle = client.get(f"/api/month/{MONTH}", auth=AUTH).json()
    assert home["budget"] == bundle["budget"]
    assert home["settings"] == bundle["settings"]
    assert home["plan"] == bundle["plan"]
    assert home["projection"] == bundle["projection"]
    assert home["cash"] == bundle["cash"]
    assert home["cards"] == bundle["cards"]
    assert home["paid_total_cents"] == bundle["paid_total_cents"]
    assert home["recommended_total_cents"] == bundle["recommended_total_cents"]

    assert home["safe_to_spend"] == client.get("/api/safe-to-spend", auth=AUTH).json()
    assert home["upcoming"] == client.get("/api/upcoming?days=30", auth=AUTH).json()
    assert home["reminders"] == client.get("/api/reminders?days=14", auth=AUTH).json()


def test_mobile_home_requires_auth(client):
    assert client.get("/api/mobile/home").status_code == 401


def test_mobile_home_settings_match_settings_endpoint(client):
    """API1 C2: the aggregate's settings are identical to GET /api/settings.

    The month bundle carried the raw sqlite int for auto_log_card_payments
    (1) while /api/settings returns a bool (true); a client comparing it to
    true read the toggle as off.
    """
    _seed(client)
    home = client.get("/api/mobile/home", auth=AUTH).json()
    settings = client.get("/api/settings", auth=AUTH).json()
    assert home["settings"] == settings
    assert home["settings"]["auto_log_card_payments"] is True
