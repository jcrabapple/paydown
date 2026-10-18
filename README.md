# Paydown

A fast, self-hosted monthly credit-card payoff and **Safe-to-Spend** budget app.

Paydown answers two questions that most budgeting apps muddle together:
1. **Today (`#/today`)**: *How much cash can I safely spend right now without missing a bill or card minimum before my next paycheck?*
2. **This Month & Plan (`#/month`, `#/plan`)**: *How much extra can my monthly budget put toward my credit cards, which card gets it first (Avalanche, Snowball, or Custom), and when will every card be paid off?*

Built with **Python 3.12 (FastAPI + SQLite)** and **vanilla ES-module JavaScript + CSS** (zero build step, zero npm dependencies, installable as a PWA, light/dark mode out of the box). Works 100% manually or with optional **Plaid** bank sync.

---

## Features

- **Safe-to-Spend Today View**: Calculates what you can spend between today and your next payday (`checking & savings − pending payments − bills due before payday − card minimums due before payday − goals set aside − cash floor`), with a daily allowance and full breakdown.
- **Deterministic Payoff Engine**: Simulates Avalanche (highest APR first), Snowball (lowest balance first), and Custom card order side-by-side against a minimums-only baseline, with automatic minimum rollover as each card is paid off.
- **Monthly Action Plan**: Recommends exact minimum + extra payments per card for the month, tracks logged payments (manual or auto-logged from your bank), and freezes start-of-month plan snapshots so recommendations don't shift mid-month as you pay them.
- **Spare Cash Sweep**: Keeps a configurable cash reserve (`months of essentials + cash floor`) and lets you sweep excess checking cash into the current month's card payments without double-counting monthly income.
- **Subscriptions & Sinking-Fund Goals**: Tracks weekly/monthly/quarterly/yearly subscriptions at their monthly-equivalent cost, detects recurring charges from bank history, and manages sinking-fund goals inside checking.
- **Reality Check & Spending Baseline**: Analyzes synced credit card transactions to show month-over-month net debt change (`charges + interest + fees − payments − refunds`) and suggests realistic everyday spending budget lines from historical averages.
- **Optional Plaid Integration**: Syncs credit card balances, APRs, statement balances, due dates, checking/savings balances, and transactions. Infers effective APRs from interest-charge transactions when issuers omit APR data, and encrypts all Plaid access tokens at rest using Fernet (`cryptography`).
- **Installable PWA**: Includes a web app manifest, adaptive/maskable icons, and a root service worker with offline read caching.

---

## Quick Start

### Prerequisites
- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) (recommended) or standard `venv` + `pip`

### 1. Clone and install
```bash
git clone https://github.com/jcrabapple/paydown.git
cd paydown
uv sync
```

### 2. Configure environment
Copy `.env.example` and set a strong password:
```bash
cp .env.example .env
```

Or pass environment variables directly when starting:
```bash
BUDGET_USER=paydown BUDGET_PASS="your-strong-password" BUDGET_PORT=8485 uv run python run.py
```

Open `http://127.0.0.1:8485` and sign in with your `BUDGET_USER` and `BUDGET_PASS`.

---

## Optional: Bank Sync with Plaid

Paydown works completely offline/manually without Plaid. If you have a Plaid developer account (e.g. the free Plaid Trial plan) and want automatic balance, liability, and transaction syncing:

1. Generate a Fernet encryption key for storing access tokens at rest:
   ```bash
   uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
2. Set the Plaid variables in your environment file (`~/.config/budget.env` or `.env`):
   ```ini
   PLAID_CLIENT_ID=your_client_id
   PLAID_CLIENT_SECRET=your_client_secret
   PLAID_ENV=production
   PLAID_TOKEN_KEY=your_fernet_key
   # Optional, required by OAuth banks:
   PLAID_REDIRECT_URI=https://your-domain.example.com/plaid-oauth.html
   ```
3. To sync daily in the background, run `uv run python sync_cli.py` via cron or a systemd timer.

---

## Running with systemd

Example user service (`~/.config/systemd/user/budget.service`):

```ini
[Unit]
Description=Paydown Budget App
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/projects/budget
EnvironmentFile=%h/.config/budget.env
ExecStart=%h/projects/budget/.venv/bin/python run.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
```

---

## Running Tests

The test suite mocks all external network calls and includes a safety guard in `tests/conftest.py` that prevents tests from ever opening `data/budget.db`:

```bash
uv run pytest -q
```

---

## License

[MIT](LICENSE)
