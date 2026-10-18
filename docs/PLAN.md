# Paydown: monthly credit card payoff budget

Single-user, self-hosted web app. Enter your monthly income, regular bills and
spending, and your credit cards (balance, APR, minimum). The app builds a monthly
budget, suggests how much you can pay beyond the minimums, recommends exactly what
to pay on each card this month, tracks the payments you actually make, and
projects a payoff month for every card.

Designed to run behind HTTPS (e.g. a reverse proxy or Cloudflare Tunnel), with HTTP Basic auth enforced
by the app itself.

## Inspiration (open source, studied before design)

| Project | Idea borrowed |
|---|---|
| noderaven/debt-tracker | "This month" action card telling you what to pay and where; windfall allocator; basic-auth single-file self-hosting; utilization per card |
| pelzade127/bloom-app | One pure deterministic simulation engine reused by every view; one slider splitting surplus between debt and cushion |
| tryonlinux/dave | Auto-rollover: a paid-off card's minimum rolls into the next target |
| vinnymicale/moolah | Debt-free date + interest saved vs paying minimums only |
| Actual Budget (carrying-debt guide) | Fund real expenses before extra debt payments; stop adding new charges to cards being paid off |
| Billzzz | Don't double-count card minimums as bills |

## Stack (fixed, do not change)

- Python 3.12, FastAPI, uvicorn, stdlib `sqlite3` (no ORM). pytest + httpx for tests.
- Frontend: vanilla JS (ES modules) + hand-written CSS + inline SVG charts, served
  as static files by FastAPI. **No build step, no CDN, no npm, no external fonts.**
- Money is **integer cents** everywhere in the backend and the API. APR is a float
  percent (e.g. `24.99`). Months are `"YYYY-MM"` strings.
- Dependencies allowlist: fastapi, uvicorn[standard], pydantic (via fastapi),
  pytest, httpx. Nothing else.

## Layout

```
app/
  __init__.py
  main.py        # FastAPI app factory create_app(db_path, user, password), routes, static mount
  auth.py        # Basic auth middleware
  db.py          # schema + connection + CRUD helpers
  engine.py      # PURE payoff math, no I/O, no imports from app.db
  budget.py      # monthly budget summary (pure function of rows -> dict)
  models.py      # pydantic request/response models
  static/
    index.html
    app.css
    app.js       # entry, router
    api.js       # fetch wrapper
    views/*.js   # one module per view
    charts.js    # SVG chart helpers
tests/
  test_engine.py
  test_budget.py
  test_api.py
run.py           # uvicorn entry: reads env, binds 127.0.0.1:$BUDGET_PORT
pyproject.toml
```

## Data model (SQLite, file at `$BUDGET_DB`, default `data/budget.db`)

```sql
CREATE TABLE settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  strategy TEXT NOT NULL DEFAULT 'avalanche',  -- avalanche | snowball | custom
  buffer_cents INTEGER NOT NULL DEFAULT 0,      -- cushion kept out of the debt budget each month
  debt_share_pct INTEGER NOT NULL DEFAULT 100,  -- % of remaining surplus suggested as extra (0-100)
  extra_override_cents INTEGER                   -- NULL = use suggestion; else fixed extra per month
);
CREATE TABLE income (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
  active INTEGER NOT NULL DEFAULT 1, sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE expenses (                          -- recurring monthly outflows that are NOT card minimums
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
  kind TEXT NOT NULL DEFAULT 'bill',             -- bill (fixed: rent, utilities) | spending (variable: groceries, gas)
  due_day INTEGER CHECK (due_day BETWEEN 1 AND 31),
  active INTEGER NOT NULL DEFAULT 1, sort INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE cards (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  balance_cents INTEGER NOT NULL CHECK (balance_cents >= 0),
  apr REAL NOT NULL CHECK (apr >= 0 AND apr < 100),
  min_payment_cents INTEGER NOT NULL CHECK (min_payment_cents >= 0),
  credit_limit_cents INTEGER,                    -- optional, for utilization
  due_day INTEGER CHECK (due_day BETWEEN 1 AND 31),
  priority INTEGER NOT NULL DEFAULT 0,           -- custom-strategy order, lower first
  starting_balance_cents INTEGER NOT NULL,       -- balance when first added (progress bar)
  closed INTEGER NOT NULL DEFAULT 0,             -- archived/paid-off cards hidden from plan
  created_at TEXT NOT NULL
);
CREATE TABLE payments (
  id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  month TEXT NOT NULL,                           -- YYYY-MM it counts toward
  paid_on TEXT NOT NULL,                         -- YYYY-MM-DD
  amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
  note TEXT
);
CREATE TABLE balance_log (                       -- statement/balance snapshots for the history chart
  id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  logged_on TEXT NOT NULL, balance_cents INTEGER NOT NULL, source TEXT NOT NULL  -- 'create'|'manual'|'payment'|'payment_delete'
);
CREATE TABLE month_adjustments (                 -- one-off items for a single month (car repair, bonus)
  id INTEGER PRIMARY KEY, month TEXT NOT NULL, name TEXT NOT NULL,
  amount_cents INTEGER NOT NULL                  -- negative = extra expense, positive = extra income
);
```

Rules:
- `PRAGMA foreign_keys=ON`, WAL mode. Schema created on startup (idempotent).
- Creating a card writes `starting_balance_cents = balance_cents` and a `balance_log` row (`create`).
- Logging a payment **subtracts** it from `cards.balance_cents` (floored at 0) and writes a
  `balance_log` row (`payment`). Deleting a payment adds it back (`payment_delete`).
- Editing a card's balance directly (statement update) writes a `balance_log` row (`manual`).
- The current month is computed from the server's local date unless a request passes `month`.

## Monthly budget (budget.py, pure)

For month M:

```
income_total      = sum(active income) + sum(positive adjustments for M)
bills_total       = sum(active expenses kind=bill)
spending_total    = sum(active expenses kind=spending)
adjust_out        = -sum(negative adjustments for M)
minimums_total    = sum(min(card.min_payment, card.balance) for open cards with balance > 0)
available_after_min = income_total - bills_total - spending_total - adjust_out - minimums_total
surplus           = available_after_min - buffer
suggested_extra   = max(0, floor(surplus * debt_share_pct / 100))   # floor to whole dollars (multiple of 100 cents)
extra             = extra_override if not null else suggested_extra
debt_budget       = minimums_total + extra
shortfall         = max(0, -available_after_min)    # > 0 means minimums cannot be covered: big red warning
leftover          = available_after_min - extra     # what stays unassigned (includes buffer)
```

`suggested_extra` is floored to a whole dollar so the suggestion reads like a human number.

## Payoff engine (engine.py, pure, the load-bearing part)

`simulate(cards, monthly_budget_cents, strategy, start_month, lump_sum_cents=0, max_months=600) -> Result`

Inputs per card: id, name, balance_cents, apr, min_payment_cents, priority.
Only cards with balance > 0 participate.

Each simulated month, in this order:
1. **Interest**: for each open card, `interest = round_half_up(balance * apr / 100 / 12)`;
   `balance += interest`. (Monthly-rate approximation of daily compounding; documented.)
2. **Lump sum** (month 1 only, if > 0): applied to target order like extra below, before minimums.
3. **Minimums**: each open card pays `min(min_payment, balance)`.
4. **Extra/rollover**: `pool = monthly_budget - sum(minimums paid this month)`; walk cards in
   strategy order and pay `min(pool, balance)` until pool is 0. This automatically rolls a
   paid-off card's minimum into the next target (the total budget stays constant).
5. A card whose balance hits 0 records `payoff_month` = this month. Record per-card payment,
   interest, and end balance for the month.

Strategy order (ties broken by `id` ascending):
- `avalanche`: apr desc, then balance asc
- `snowball`: balance asc (balance at the start of that month), then apr desc
- `custom`: priority asc

Termination: stop when all balances are 0, or `max_months` reached (then `debt_free_month`
is null and every unpaid card gets `payoff_month = null`, `never = true`).
If `monthly_budget < sum(minimums)` the engine still runs (pays what it can in strategy order
for minimums, i.e. minimums are paid in strategy order until the budget is exhausted) and
reports `underfunded = true`.

Result:
```
{
  months: int | null,               # months until debt free (null if not within max)
  debt_free_month: "YYYY-MM" | null,# start_month is month 1
  total_interest_cents, total_paid_cents,
  underfunded: bool,
  cards: [{id, name, payoff_month, months_to_payoff, interest_cents, never: bool}],
  schedule: [{month, payments: {card_id: cents}, interest: {card_id: cents},
              balances: {card_id: cents}, total_balance_cents}]
}
```

`minimums_only(cards, start_month)`: baseline where each card pays only its own minimum
forever, no rollover (budget = sum of minimums, but freed minimums are NOT reassigned).
A card whose minimum <= its first month's interest never pays off -> `never = true`.

`compare(cards, budget, start_month)` returns avalanche, snowball, custom, and minimums_only
results side by side plus `interest_saved_cents` and `months_saved` of the chosen strategy
vs minimums_only.

`this_month_plan(cards, budget, strategy, month)` = the first row of `simulate` expressed as
`[{card_id, name, minimum_cents, extra_cents, recommended_cents}]`.

## API (JSON, all under /api, all money in cents)

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | /api/health | (no auth) | `{"ok": true}` |
| GET/PUT | /api/settings | Settings | Settings |
| GET/POST | /api/income | IncomeIn | list / created |
| PUT/DELETE | /api/income/{id} | IncomeIn | updated / 204 |
| GET/POST | /api/expenses | ExpenseIn | list / created |
| PUT/DELETE | /api/expenses/{id} | ExpenseIn | updated / 204 |
| GET/POST | /api/cards | CardIn | list (incl. `utilization_pct`, `progress_pct`) / created |
| PUT/DELETE | /api/cards/{id} | CardIn (partial ok) | updated / 204 |
| GET | /api/cards/{id}/history | | balance_log rows |
| GET/POST | /api/payments | `?month=` filter; PaymentIn {card_id, amount_cents, paid_on, month?, note?} | list / created |
| DELETE | /api/payments/{id} | | 204 |
| GET/POST | /api/adjustments | `?month=`; {month, name, amount_cents} | list / created |
| DELETE | /api/adjustments/{id} | | 204 |
| GET | /api/month/{YYYY-MM} | | dashboard bundle, below |
| GET | /api/projection | `?extra=<cents>&strategy=&lump=<cents>` | compare() output for what-if |
| GET | /api/export | | full JSON dump of every table |
| POST | /api/import | export JSON | replaces all data (transaction) |

`GET /api/month/{m}` returns:
```
{ month, budget: <budget.py dict>, settings,
  plan: this_month_plan rows each extended with paid_cents (payments logged for that card in m)
        and remaining_cents = max(0, recommended - paid),
  paid_total_cents, recommended_total_cents,
  projection: compare() using debt_budget and settings.strategy (schedule trimmed to 120 months),
  cards: list }
```

Validation: pydantic models, 422 on bad input; month regex `^\d{4}-(0[1-9]|1[0-2])$`;
names 1-80 chars; amounts 0..10^10 cents.

## Auth

Middleware on every route except `/api/health`: HTTP Basic, user/pass from env
`BUDGET_USER` / `BUDGET_PASS`, compared with `secrets.compare_digest` on bytes. Missing or
wrong -> 401 with `WWW-Authenticate: Basic realm="budget"`. App refuses to start if
`BUDGET_PASS` is empty. Responses also carry `X-Robots-Tag: noindex`,
`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and a CSP of
`default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'`.
No inline scripts or inline style attributes that CSP would block (use classes; SVG
attributes like fill/stroke/width are fine).

## UI

Mobile-first (360px up), light/dark via `prefers-color-scheme`, system font stack.
Hash router, top nav: **Month · Cards · Budget · Plan · Settings**.

1. **Month** (default, `#/month/YYYY-MM`, prev/next arrows):
   - Header strip: Income, Bills+Spending, Card minimums, Suggested extra, Debt budget, Leftover.
     Shortfall shows a red banner ("Minimums exceed what's left by $X").
   - "This month" action card: per-card row with recommended (min + extra, extra highlighted),
     paid so far, remaining, a progress bar, and a **Log payment** button (amount defaults to remaining,
     date defaults to today). Logged payments listed under each card with delete.
   - One-off adjustments for the month (add/remove).
   - Small summary: debt-free month, total interest, interest saved vs minimums only.
2. **Cards**: list with balance, APR, minimum, utilization, progress since added, projected
   payoff month under the current strategy. Add/edit/delete form, "Update balance" quick action,
   per-card balance history sparkline (SVG).
3. **Budget**: income sources and expenses (bills vs spending) with inline add/edit/delete, totals,
   and the waterfall math from budget.py shown as a readable breakdown.
4. **Plan**: strategy picker (avalanche/snowball/custom with up/down reorder for custom), extra-payment
   slider (0..2x suggested, defaults to current) that re-queries /api/projection (debounced 250ms),
   lump-sum (windfall) input, comparison table of all strategies vs minimums-only
   (debt-free month, total interest, months), per-card payoff timeline (horizontal bars by month),
   and a stacked/total balance-over-time SVG line chart.
5. **Settings**: buffer, debt share %, extra override (blank = use suggestion), export/import JSON.

Money is formatted with `Intl.NumberFormat('en-US', {style:'currency', currency:'USD'})`; inputs
accept dollars ("1,234.56") and the client converts to cents.
Empty state on first load walks through: add income -> add bills -> add cards.

## Tasks

- **T1 backend** (owner: coder A): `app/db.py, engine.py, budget.py, models.py, auth.py, main.py`,
  `run.py`, `pyproject.toml`, `tests/**`. Must not touch `app/static/**`.
- **T2 frontend** (owner: coder B): everything under `app/static/**`. Must not touch Python files.
  Builds against the API contract above.
- **T3 integration** (orchestrator): run both together, browser-check, fix, deploy.

## Acceptance

- `uv run pytest -q` green. Engine tests include hand-computed cases (see AGENTS.md).
- `uv run python run.py` with `BUDGET_PASS=x BUDGET_PORT=8485` serves the UI; every view works
  against real data with no console errors; 401 without creds.

## Deployment

- `budget.service` (systemd user): `.venv/bin/python run.py`, env from `~/.config/budget.env`
  (mode 600: BUDGET_USER, BUDGET_PASS, BUDGET_PORT=8485, BUDGET_DB=data/budget.db).
- Reverse proxy / Cloudflare Tunnel: point your domain -> http://127.0.0.1:8485.
- Deploy a change: `uv run pytest -q && systemctl --user restart budget.service`.
- Back up: copy `data/budget.db` (WAL; use `sqlite3 data/budget.db ".backup x.db"`) or GET /api/export.
- Plaid sync (docs/PLAID.md): `budget-plaid-sync.timer` runs `sync_cli.py` daily ~06:30.
  Env adds PLAID_CLIENT_ID, PLAID_CLIENT_SECRET, PLAID_ENV=production, PLAID_TOKEN_KEY (Fernet),
  PLAID_REDIRECT_URI.
- Phase 3 (docs/CASH.md): cash accounts + spare-cash sweep; Plaid links use
  products=[transactions] + required_if_supported=[liabilities] with credit-card and
  checking/savings filters. Cash never enters the plan automatically (would double count
  income); only via an explicit "use spare cash" sweep for the current month.
- Phase 4 (docs/SAFE.md): Safe-to-Spend (Today view, default route), pay
  schedules, paycheck labels + deposit rules (with preview/undo), per-due-date bill tracking,
  goals, unlogged card payments, upcoming list; Plaid /transactions/sync with cursors.
  Per-thread sqlite connections (`db.ThreadLocalConnection`) + app-level yield dependency rollback;
  `tests/conftest.py` forbids opening `data/budget.db`.
