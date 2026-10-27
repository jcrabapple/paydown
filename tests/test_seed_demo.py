"""scripts/seed_demo.py must keep producing a coherent demo household
(README + screenshots depend on it) and must never touch an existing DB."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import seed_demo  # noqa: E402


def test_seed_produces_a_working_household(tmp_path):
    db = tmp_path / "demo.db"
    seed_demo.seed(str(db))
    from fastapi.testclient import TestClient

    from app.main import create_app
    import base64

    tok = base64.b64encode(f"{seed_demo.USER}:{seed_demo.PASSWORD}".encode()).decode()
    c = TestClient(create_app(str(db), seed_demo.USER, seed_demo.PASSWORD),
                   headers={"Authorization": f"Basic {tok}"})
    home = c.get("/api/mobile/home").json()
    assert len(home["plan"]) == 4
    assert home["budget"]["debt_budget_cents"] > 0
    assert len(c.get("/api/subscriptions").json()["active"]) == 5
    assert len(c.get("/api/goals").json()) == 2
    assert len(c.get("/api/pay-schedules").json()) == 1
    assert c.get("/api/safe-to-spend").status_code == 200


def test_refuses_existing_file(tmp_path):
    db = tmp_path / "existing.db"
    db.write_bytes(b"precious")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "seed_demo.py"), str(db)],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 1
    assert db.read_bytes() == b"precious"
