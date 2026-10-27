"""API tests against a tmp_path sqlite db."""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app

AUTH = ("user", "test")


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "budget.db"), "user", "test")
    with TestClient(app) as c:
        yield c


def test_health_no_auth(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_auth_required(client):
    assert client.get("/api/cards").status_code == 401
    r = client.get("/api/cards", auth=AUTH)
    assert r.status_code == 200
    wrong = client.get("/api/cards", auth=("user", "nope"))
    assert wrong.status_code == 401
    assert wrong.headers["WWW-Authenticate"] == 'Basic realm="budget"'
    r = client.get("/api/cards", auth=AUTH)
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Robots-Tag"] == "noindex"
    assert r.headers["Referrer-Policy"] == "no-referrer"


def test_settings_roundtrip(client):
    r = client.put(
        "/api/settings",
        json={"strategy": "snowball", "buffer_cents": 10000, "debt_share_pct": 50},
        auth=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["strategy"] == "snowball"
    assert client.get("/api/settings", auth=AUTH).json()["buffer_cents"] == 10000


def test_income_crud(client):
    r = client.post("/api/income", json={"name": "Salary", "amount_cents": 400000}, auth=AUTH)
    assert r.status_code == 201
    iid = r.json()["id"]
    r = client.put(f"/api/income/{iid}", json={"name": "Salary", "amount_cents": 450000}, auth=AUTH)
    assert r.json()["amount_cents"] == 450000
    r = client.delete(f"/api/income/{iid}", auth=AUTH)
    assert r.status_code == 204
    assert client.get("/api/income", auth=AUTH).json() == []


def test_expenses_crud_and_validation(client):
    r = client.post(
        "/api/expenses", json={"name": "Rent", "amount_cents": 150000, "kind": "bill"}, auth=AUTH
    )
    assert r.status_code == 201
    r = client.post("/api/expenses", json={"name": "", "amount_cents": 1}, auth=AUTH)
    assert r.status_code == 422
    r = client.post("/api/expenses", json={"name": "x" * 81, "amount_cents": 1}, auth=AUTH)
    assert r.status_code == 422
    r = client.post(
        "/api/expenses", json={"name": "Gas", "amount_cents": 10**11}, auth=AUTH
    )
    assert r.status_code == 422


def test_card_create_and_history(client):
    r = client.post(
        "/api/cards",
        json={
            "name": "Visa",
            "balance_cents": 100000,
            "apr": 12.0,
            "min_payment_cents": 10000,
            "credit_limit_cents": 200000,
        },
        auth=AUTH,
    )
    assert r.status_code == 201
    card = r.json()
    assert card["starting_balance_cents"] == 100000
    assert card["utilization_pct"] == 50.0
    assert card["progress_pct"] == 0.0
    hist = client.get(f"/api/cards/{card['id']}/history", auth=AUTH)
    assert len(hist.json()) == 1
    assert hist.json()[0]["source"] == "create"


def test_payment_adjusts_balance_and_delete_restores(client):
    card = client.post(
        "/api/cards",
        json={"name": "Visa", "balance_cents": 100000, "apr": 0.0, "min_payment_cents": 5000},
        auth=AUTH,
    ).json()
    r = client.post(
        "/api/payments",
        json={"card_id": card["id"], "amount_cents": 30000, "paid_on": "2026-01-15", "month": "2026-01"},
        auth=AUTH,
    )
    assert r.status_code == 201
    pid = r.json()["id"]
    assert client.get("/api/cards", auth=AUTH).json()[0]["balance_cents"] == 70000
    logs = client.get(f"/api/cards/{card['id']}/history", auth=AUTH).json()
    assert logs[-1]["source"] == "payment"
    assert logs[-1]["balance_cents"] == 70000
    assert client.delete(f"/api/payments/{pid}", auth=AUTH).status_code == 204
    assert client.get("/api/cards", auth=AUTH).json()[0]["balance_cents"] == 100000
    logs = client.get(f"/api/cards/{card['id']}/history", auth=AUTH).json()
    assert logs[-1]["source"] == "payment_delete"


def test_manual_balance_update_logged(client):
    card = client.post(
        "/api/cards",
        json={"name": "V", "balance_cents": 100000, "apr": 0.0, "min_payment_cents": 5000},
        auth=AUTH,
    ).json()
    client.put(f"/api/cards/{card['id']}", json={"balance_cents": 90000}, auth=AUTH)
    logs = client.get(f"/api/cards/{card['id']}/history", auth=AUTH).json()
    assert logs[-1]["source"] == "manual"


def test_adjustments_crud(client):
    r = client.post(
        "/api/adjustments", json={"month": "2026-01", "name": "Bonus", "amount_cents": 50000}, auth=AUTH
    )
    assert r.status_code == 201
    aid = r.json()["id"]
    assert len(client.get("/api/adjustments", params={"month": "2026-01"}, auth=AUTH).json()) == 1
    assert client.delete(f"/api/adjustments/{aid}", auth=AUTH).status_code == 204


def test_month_bundle_shape(client):
    client.post("/api/income", json={"name": "Pay", "amount_cents": 300000}, auth=AUTH)
    client.post("/api/expenses", json={"name": "Rent", "amount_cents": 100000, "kind": "bill"}, auth=AUTH)
    card = client.post(
        "/api/cards",
        json={"name": "Visa", "balance_cents": 50000, "apr": 0.0, "min_payment_cents": 10000},
        auth=AUTH,
    ).json()
    m = client.get("/api/month/2026-01", auth=AUTH).json()
    assert m["month"] == "2026-01"
    assert m["budget"]["income_total_cents"] == 300000
    assert m["budget"]["minimums_total_cents"] == 10000
    assert m["budget"]["debt_budget_cents"] == 200000  # min + 100% surplus
    plan = {p["card_id"]: p for p in m["plan"]}
    row = plan[card["id"]]
    assert row["recommended_cents"] == 50000  # capped at the card's balance
    assert row["paid_cents"] == 0 and row["remaining_cents"] == 50000
    assert m["recommended_total_cents"] == 50000
    assert "projection" in m and "avalanche" in m["projection"]
    # Log a payment and check paid/remaining update.
    client.post(
        "/api/payments",
        json={"card_id": card["id"], "amount_cents": 40000, "paid_on": "2026-01-05", "month": "2026-01"},
        auth=AUTH,
    )
    m2 = client.get("/api/month/2026-01", auth=AUTH).json()
    row2 = {p["card_id"]: p for p in m2["plan"]}[card["id"]]
    assert row2["paid_cents"] == 40000
    # FIX1 B1: recommendations use start-of-month balances, so the recommended
    # amount does not shrink after logging a payment; only remaining does.
    assert row2["recommended_cents"] == 50000
    assert row2["remaining_cents"] == 10000  # max(0, recommended 50000 - paid 40000)
    assert m2["paid_total_cents"] == 40000


def test_month_bad_month_422(client):
    assert client.get("/api/month/2026-13", auth=AUTH).status_code == 422
    assert client.get("/api/month/junk", auth=AUTH).status_code == 422


def test_projection_with_extra(client):
    client.post(
        "/api/cards",
        json={"name": "V", "balance_cents": 100000, "apr": 12.0, "min_payment_cents": 10000},
        auth=AUTH,
    )
    base = client.get("/api/projection", auth=AUTH).json()
    bigger = client.get("/api/projection", params={"extra": 50000}, auth=AUTH).json()
    assert bigger["avalanche"]["total_interest_cents"] <= base["avalanche"]["total_interest_cents"]
    lump = client.get("/api/projection", params={"lump": 50000}, auth=AUTH).json()
    assert lump["avalanche"]["total_interest_cents"] < base["avalanche"]["total_interest_cents"]
    sb = client.get("/api/projection", params={"strategy": "snowball"}, auth=AUTH).json()
    assert sb["strategy"] == "snowball"


def test_export_import_roundtrip(client):
    client.post("/api/income", json={"name": "Pay", "amount_cents": 100000}, auth=AUTH)
    client.post(
        "/api/cards",
        json={"name": "V", "balance_cents": 10000, "apr": 5.0, "min_payment_cents": 1000},
        auth=AUTH,
    )
    dump = client.get("/api/export", auth=AUTH).json()
    # Wipe by importing an empty export, then re-import the dump.
    empty = {k: [] for k in dump}
    empty["settings"] = [{"id": 1, "strategy": "avalanche", "buffer_cents": 0, "debt_share_pct": 100, "extra_override_cents": None}]
    client.post("/api/import", json=empty, auth=AUTH)
    assert client.get("/api/income", auth=AUTH).json() == []
    client.post("/api/import", json=dump, auth=AUTH)
    assert client.get("/api/income", auth=AUTH).json()[0]["name"] == "Pay"
    assert client.get("/api/cards", auth=AUTH).json()[0]["name"] == "V"


def test_empty_password_refused(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(str(tmp_path / "x.db"), "user", "")


def test_static_index_served(client):
    r = client.get("/", auth=AUTH)
    # index.html may not exist yet (frontend built concurrently); must not 500.
    assert r.status_code in (200, 404)


def test_pwa_manifest_and_sw_precache_integrity(client):
    import re
    mf = client.get("/manifest.webmanifest", auth=AUTH)
    assert mf.status_code == 200
    assert "application/manifest+json" in mf.headers["content-type"]
    data = mf.json()
    assert data["name"] == "Paydown"
    assert data["display"] == "standalone"
    for icon in data["icons"]:
        ir = client.get(icon["src"], auth=AUTH)
        assert ir.status_code == 200, f"missing icon {icon['src']}"

    sw = client.get("/sw.js", auth=AUTH)
    assert sw.status_code == 200
    assert sw.headers.get("Service-Worker-Allowed") == "/"
    assert "__VERSION__" not in sw.text
    m = re.search(r"const PRECACHE_URLS = \[(.*?)\];", sw.text, re.S)
    assert m, "PRECACHE_URLS list not found in sw.js"
    urls = re.findall(r"'([^']+)'", m.group(1))
    assert len(urls) >= 15
    for u in urls:
        res = client.get(u, auth=AUTH)
        assert res.status_code == 200, f"precache URL {u} returned {res.status_code}"

    off = client.get("/offline", auth=AUTH)
    assert off.status_code == 200
    assert "offline" in off.text.lower()
