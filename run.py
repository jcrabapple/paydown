"""Uvicorn entry point. Env: BUDGET_DB, BUDGET_USER, BUDGET_PASS, BUDGET_PORT,
plus PLAID_* (see app.plaid.PlaidConfig.from_env). Never logs secret values."""
from __future__ import annotations

import os
import sys

import uvicorn

from app.main import create_app
from app.plaid import PlaidClient, PlaidConfig


def main() -> None:
    db_path = os.environ.get("BUDGET_DB", "data/budget.db")
    user = os.environ.get("BUDGET_USER", "paydown")
    password = os.environ.get("BUDGET_PASS", "")
    if not password:
        print("Refusing to start: BUDGET_PASS is required and cannot be empty", file=sys.stderr)
        sys.exit(1)
    port = int(os.environ.get("BUDGET_PORT", "8485"))
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    config = PlaidConfig.from_env()
    plaid = PlaidClient(config) if config else None
    app = create_app(db_path, user, password, plaid=plaid)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
