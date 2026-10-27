"""Autouse guards shared by the whole suite.

R1 (docs/FIX4-1.md): no test may ever open the live database
`<repo>/data/budget.db`. Every test gets BUDGET_DB pointed at a tmp path AND
`app.db.connect` is wrapped so that any connection attempt whose path resolves
to the live DB raises RuntimeError (chdir is NOT used: it breaks imports).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import db

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_DB = REPO_ROOT / "data" / "budget.db"


@pytest.fixture(autouse=True)
def _never_touch_live_db(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDGET_DB", str(tmp_path / "guard.db"))
    real_connect = db.connect

    def guarded_connect(db_path):
        p = Path(str(db_path)).resolve()
        if p == LIVE_DB.resolve():
            raise RuntimeError("tests must not open the live DB")
        return real_connect(db_path)

    monkeypatch.setattr(db, "connect", guarded_connect)
    yield


@pytest.fixture(autouse=True)
def _reset_auth_throttle():
    """The brute-force lockout counter is process-global (app.auth._throttle).
    Reset it around every test so one test's deliberate 401s can't lock the
    next test out with 429s (order-dependent failures)."""
    from app import auth

    def reset():
        with auth._throttle_lock:
            auth._throttle.update(count=0, last_fail=0.0)

    reset()
    yield
    reset()
