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
