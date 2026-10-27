"""The README quick start (cp .env.example .env) must actually work."""

import os
import subprocess
import sys
from pathlib import Path

from app.envfile import load_env_file

ROOT = Path(__file__).resolve().parent.parent


def test_parses_comments_quotes_export_and_trailing_comments(tmp_path, monkeypatch):
    for k in ("A1", "B1", "C1", "D1", "E1"):
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / ".env"
    f.write_text(
        "# comment\n\nA1=plain\nexport B1=\"quoted value\"\nC1='single # not a comment'\n"
        "D1=value # trailing comment\nnot a line\nE1=\n",
        encoding="utf-8",
    )
    loaded = load_env_file(f)
    assert os.environ["A1"] == "plain"
    assert os.environ["B1"] == "quoted value"
    assert os.environ["C1"] == "single # not a comment"
    assert os.environ["D1"] == "value"
    assert os.environ["E1"] == ""
    assert sorted(loaded) == ["A1", "B1", "C1", "D1", "E1"]


def test_real_environment_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDGET_PASS", "from-env")
    f = tmp_path / ".env"
    f.write_text("BUDGET_PASS=from-file\n", encoding="utf-8")
    assert load_env_file(f) == []
    assert os.environ["BUDGET_PASS"] == "from-env"


def test_missing_file_is_a_no_op(tmp_path):
    assert load_env_file(tmp_path / "nope.env") == []


def test_example_file_parses_and_sets_required_keys(tmp_path, monkeypatch):
    for k in ("BUDGET_USER", "BUDGET_PASS", "BUDGET_PORT", "PLAID_CLIENT_ID"):
        monkeypatch.delenv(k, raising=False)
    load_env_file(ROOT / ".env.example")
    assert os.environ["BUDGET_PASS"]
    assert os.environ["BUDGET_USER"] == "paydown"
    assert "PLAID_CLIENT_ID" not in os.environ  # commented out in the example


def test_run_py_picks_up_dot_env_from_cwd(tmp_path):
    """End to end: with only ./.env providing BUDGET_PASS, run.py gets past
    its 'BUDGET_PASS is required' guard (we stop it before it binds)."""
    (tmp_path / ".env").write_text("BUDGET_PASS=from-dot-env\nBUDGET_PORT=1\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("BUDGET_", "PLAID_"))}
    env["BUDGET_DB"] = str(tmp_path / "x.db")
    env["PYTHONPATH"] = str(ROOT)
    code = (
        "import uvicorn, run; uvicorn.run = lambda *a, **k: print('WOULD_SERVE'); run.main()"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=60)
    assert "WOULD_SERVE" in r.stdout, r.stderr
    assert "BUDGET_PASS is required" not in r.stderr
