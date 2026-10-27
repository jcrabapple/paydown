"""Plaid sync for the systemd timer. Reads env like run.py (BUDGET_DB, PLAID_*),
opens the DB, runs sync_all, prints one summary line per item (institution,
status, accounts_updated - never tokens or secrets). Exit 0 even when an item
needs re-login; exit 1 only on config/DB failure."""
from __future__ import annotations

import os
import sys

from app import db
from app.envfile import load_env_file
from app.plaid import PlaidClient, PlaidConfig, sync_all


def main() -> int:
    # Same at-rest posture as run.py: owner-only files (Opus review #6).
    os.umask(0o077)
    load_env_file()  # ./.env if present; real env vars win
    config = PlaidConfig.from_env()
    if config is None:
        print("Plaid is not configured; nothing to sync", file=sys.stderr)
        return 1
    db_path = os.environ.get("BUDGET_DB", "data/budget.db")
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    try:
        conn = db.init_db(db_path)
    except Exception as e:
        print(f"DB failure: {e}", file=sys.stderr)
        return 1
    result = sync_all(conn, PlaidClient(config))
    for item in result["items"]:
        line = f"{item['institution_name']}: {item['status']} accounts_updated={item['accounts_updated']} transactions_added={item.get('transactions_added', 0)} modified={item.get('transactions_modified', 0)} removed={item.get('transactions_removed', 0)}"
        if item["error_code"]:
            line += f" error_code={item['error_code']}"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
