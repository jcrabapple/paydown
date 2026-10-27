# Phase 4: Safe-to-Spend, pay schedules, paycheck labels, goals

PLAN.md, PLAID.md, CASH.md and AGENTS.md still apply. This file wins where they conflict.

## What we're building and why

Simple Bank's Safe-to-Spend: your balance minus pending transactions, minus bills due before
your next paycheck, minus money set aside in Goals. It answers "what can I spend today
without hurting anything", and it only works if the app knows **when the next paycheck
lands**. So this phase adds:

1. **Pay schedules** (weekly, biweekly, semimonthly, monthly) that predict paydays.
2. **Bank transactions** stored locally (Plaid `/transactions/sync`, already consented).
3. **Paycheck labels** on checking deposits, with "always label deposits like this" rules.
   A schedule with labelled paychecks is *deposit-confirmed*: a payday only counts once the
   paycheck actually shows up.
4. **Safe-to-Spend** until the next payday, with a per-day allowance and a full breakdown.
5. **Bill tracking per due date**: auto-matched from transactions or marked paid by hand.
6. **Goals** (sinking funds for irregular bills): money set aside inside checking, removed
   from Safe-to-Spend and spare cash, and funded through the monthly budget.
7. **Unlogged card payments**: card payments seen at the bank but not logged in the app,
   with one-click logging (otherwise Safe-to-Spend double counts them).

Note: Plaid's category alone cannot reliably distinguish paychecks from other transfers or
deposits; user labels and deposit rules are the source of truth.

## Conventions (all coders)

- Money: integer cents. `transactions.amount_cents` keeps **Plaid's sign: positive = money
  leaving the account, negative = money arriving**. Deposits are `amount_cents < 0`.
- Dates are local calendar dates in `BUDGET_TZ` (env, default `America/New_York`) as ISO
  `YYYY-MM-DD`; timestamps are ISO UTC with `Z`.
- `app/clock.py` (backend-data coder creates it): `today() -> date` (local date in BUDGET_TZ)
  and `now() -> datetime` (aware UTC). Every "today" in the app goes through it so tests can
  monkeypatch `app.clock.today` / `app.clock.now`.
- `round_half_up_cents` from `app.engine` for every fractional cent. "Ceil to a dollar" means
  `-(-x // 100) * 100`.
- No new dependencies. stdlib `datetime`, `zoneinfo`, `calendar`, `statistics` are fine.

## Data model (owned by the backend-data coder, migrations in `app/db.py`)

All idempotent (`CREATE TABLE IF NOT EXISTS`, PRAGMA table_info guards), safe on the live DB.

```sql
CREATE TABLE IF NOT EXISTS pay_schedules (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  frequency TEXT NOT NULL,                -- weekly | biweekly | semimonthly | monthly
  anchor_date TEXT NOT NULL,              -- a known payday (YYYY-MM-DD); weekly/biweekly phase, monthly day
  semimonthly_day1 INTEGER, semimonthly_day2 INTEGER,  -- 1..31, 31 = last day of month; semimonthly only
  weekend_rule TEXT NOT NULL DEFAULT 'previous',       -- previous | next | none
  amount_cents INTEGER NOT NULL DEFAULT 0 CHECK (amount_cents >= 0),  -- expected net paycheck
  cash_account_id INTEGER REFERENCES cash_accounts(id) ON DELETE SET NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
  transaction_id TEXT PRIMARY KEY,
  plaid_account_id INTEGER NOT NULL REFERENCES plaid_accounts(id) ON DELETE CASCADE,
  date TEXT NOT NULL, authorized_date TEXT,
  amount_cents INTEGER NOT NULL,          -- Plaid sign: + out, - in
  name TEXT NOT NULL, merchant_name TEXT,
  pending INTEGER NOT NULL DEFAULT 0, pending_transaction_id TEXT,
  pfc_primary TEXT, pfc_detailed TEXT,
  label TEXT,                             -- paycheck | not_paycheck | dismissed | NULL
  label_source TEXT,                      -- manual | rule | NULL
  pay_schedule_id INTEGER REFERENCES pay_schedules(id) ON DELETE SET NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transactions_account_date ON transactions(plaid_account_id, date);
CREATE TABLE IF NOT EXISTS deposit_rules (
  id INTEGER PRIMARY KEY,
  match_text TEXT NOT NULL,               -- stored lowercased, trimmed, 2..80 chars
  label TEXT NOT NULL,                    -- paycheck | not_paycheck
  pay_schedule_id INTEGER REFERENCES pay_schedules(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL,
  UNIQUE(match_text)
);
CREATE TABLE IF NOT EXISTS bill_payments (
  id INTEGER PRIMARY KEY,
  expense_id INTEGER NOT NULL REFERENCES expenses(id) ON DELETE CASCADE,
  due_date TEXT NOT NULL,
  paid_on TEXT NOT NULL,
  source TEXT NOT NULL,                   -- manual | auto
  transaction_id TEXT,                    -- set when source = auto
  UNIQUE(expense_id, due_date)
);
CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  target_cents INTEGER NOT NULL CHECK (target_cents > 0),
  target_date TEXT,                       -- optional
  active INTEGER NOT NULL DEFAULT 1, sort INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goal_moves (
  id INTEGER PRIMARY KEY,
  goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
  amount_cents INTEGER NOT NULL CHECK (amount_cents != 0),  -- + set aside, - used/released
  moved_on TEXT NOT NULL, note TEXT
);
```
Column additions:
- `expenses.paid_from TEXT NOT NULL DEFAULT 'cash'` (cash | card) and `expenses.match_text TEXT`.
- `income.pay_schedule_id INTEGER REFERENCES pay_schedules(id) ON DELETE SET NULL`.
- `payments.transaction_id TEXT` (+ partial unique index where not null).
- `plaid_items.tx_cursor TEXT`.

Export/import: include `pay_schedules`, `deposit_rules`, `bill_payments`, `goals`,
`goal_moves`. Exclude `transactions` (bank data; re-synced) and leave it untouched on import;
after import set `transactions.pay_schedule_id = NULL` where the schedule no longer exists.

## Pure modules (owned by the backend-pure coder; no DB, no I/O, no clock calls)

### `app/paydays.py`

```python
GRACE_DAYS = 3

def occurrences(schedule: dict, start: date, end: date) -> list[date]
```
Paydays in `[start, end]` inclusive, sorted, after the weekend rule. `schedule` keys:
`frequency, anchor_date (date or ISO str), semimonthly_day1, semimonthly_day2, weekend_rule`.
- weekly / biweekly: `anchor + k*7` / `anchor + k*14` for every integer k (negative too).
- monthly: day-of-month of `anchor_date`, clamped to month length (31 -> Feb 28/29).
- semimonthly: `day1` and `day2` each month, clamped; 31 means last day.
- weekend rule on the nominal date: Saturday/Sunday -> previous Friday (`previous`) or next
  Monday (`next`); `none` leaves it. Generate nominal dates from `start - 7` to `end + 7` and
  filter **after** adjustment, so a payday adjusted into the window is never lost. De-duplicate.

```python
def receipts(schedule: dict, paychecks: list[dict], start: date, end: date) -> dict[date, dict | None]
```
`paychecks`: labelled, **posted** paycheck deposits `{transaction_id, date, amount_cents (positive),
pay_schedule_id}`. A schedule is **deposit-confirmed** iff at least one paycheck has its
`pay_schedule_id`. (Callers attribute paychecks with a NULL schedule to the only active
schedule when exactly one exists; this function only sees `pay_schedule_id` values.)
Returns each occurrence in `[start, end]` mapped to the paycheck that received it, or None.
Matching: each paycheck matches at most one occurrence, the nearest one within
`±GRACE_DAYS` (ties -> earlier occurrence); each occurrence takes at most one paycheck
(extra paychecks near the same occurrence are ignored for receipt).

```python
def next_payday(today: date, schedules: list[dict], paychecks: list[dict]) -> dict
```
Over active schedules. Per schedule, scan occurrences in `[today - GRACE_DAYS, today + 62]`:
- deposit-confirmed schedule: an occurrence is *received* iff `receipts` maps it to a paycheck.
  Not received and `today > date + GRACE_DAYS` -> *missed*. Not received otherwise -> candidate.
- not deposit-confirmed: an occurrence is received iff `date <= today`; else candidate.
The schedule's candidate is its earliest candidate. Returns:
```python
{"payday": {"date": date, "schedule_id": int, "name": str, "expected_cents": int,
            "status": "upcoming" | "today" | "late"} | None,     # late = date < today, within grace
 "missed": [{"date": date, "schedule_id": int, "name": str}],     # within the scan window
 "last_payday": date | None}   # latest received occurrence <= today across schedules (scan back 62 days)
```
`payday` is the earliest candidate across schedules (ties -> lowest schedule id).

```python
def infer_schedule(dates: list[date], amounts: list[int]) -> dict | None
```
Suggest a schedule from labelled paychecks (needs >= 2 distinct dates, else None). Sort dates,
intervals in days.
1. If >= 75% of dates share a weekday (use each date's weekday as-is; weekend-adjusted
   paydays land on Friday anyway): median interval 6..8 -> weekly; 12..16 -> biweekly.
2. Else if days-of-month fall into <= 2 clusters (a cluster = days within 3 of each other;
   any day >= 28 belongs to a "31" cluster) and median interval 12..18 -> semimonthly with
   `day1, day2` = each cluster's most common day (31 for the end-of-month cluster), day1 < day2.
3. Else median interval 26..33 -> monthly.
4. Else None.
Returns `{"frequency", "anchor_date": latest date, "semimonthly_day1", "semimonthly_day2",
"weekend_rule": "previous", "amount_cents": median(amounts) rounded half-up,
"confidence": "high" if len(dates) >= 3 else "low", "dates_used": [...]}`.

```python
def monthly_equivalent_cents(schedule: dict) -> int
```
weekly `amount*52/12`, biweekly `amount*26/12`, semimonthly `amount*2`, monthly `amount`,
half-up.

### `app/bills.py`

```python
def due_dates(bill: dict, start: date, end: date) -> list[date]
```
`due_day` clamped per month (31 -> last day). No weekend shift. Empty if `due_day` is None.

```python
def match_bills(bills: list[dict], txns: list[dict], paid: set[tuple[int, str]],
                used_txn_ids: set[str], today: date, lookback_days: int = 45) -> list[dict]
```
Auto-match bill occurrences to bank outflows. `bills`: active `kind='bill'` expenses with
`id, amount_cents, due_day, match_text`. Only bills with non-empty `match_text` and a
`due_day`. Occurrences: `due_dates(bill, today - lookback_days, today + 10)`. A txn matches
when `amount_cents > 0` (outflow), `match_text` is a case-insensitive substring of
`merchant_name` or `name`, `|txn.date - due| <= 7`, txn id not in `used_txn_ids`, and
`(bill.id, due.isoformat())` not in `paid`. Pending txns allowed. Each txn used once, each
occurrence once; prefer the txn closest to the due date (ties -> lower amount difference vs
`bill.amount_cents`, then transaction_id). Returns `[{expense_id, due_date (ISO), paid_on
(txn date ISO), transaction_id}]`. `txns` may include card-account outflows (bills paid by card).

### `app/goals.py`

```python
def per_paycheck_suggestion(target: int, set_aside: int, target_date: date | None,
                            payday_dates: list[date], today: date) -> int | None
```
None if no `target_date` or `set_aside >= target`. `n` = paydays in `[today, target_date]`.
n == 0 -> `target - set_aside`; else ceil-to-dollar of `(target - set_aside) / n`.

```python
def monthly_need(target: int, set_aside_before_month: int, target_date: date | None, month: str) -> int
```
0 if no `target_date`, or target month < `month`, or `set_aside_before_month >= target`.
Else `months_left = month_diff(target month, month) + 1`; ceil-to-dollar of
`(target - set_aside_before_month) / months_left`. (Using the balance *before* the month keeps
the budget stable while setting money aside mid-month.)

### `app/sts.py`

```python
def safe_to_spend(*, today: date, now: datetime,
                  payday_info: dict,               # next_payday() result
                  has_schedules: bool,
                  cash_total_cents: int, pending_out_cents: int, cash_as_of: datetime | None,
                  cash_floor_cents: int, goals_set_aside_cents: int,
                  bills: list[dict],               # active kind='bill': id, name, amount_cents, due_day, paid_from
                  bill_payments: set[tuple[int, str]],   # (expense_id, due_date ISO)
                  card_rows: dict[str, list[dict]],      # 'YYYY-MM' -> rows {card_id, name, due_day, remaining_cents, recommended_cents}
                  spending_monthly_cents: int,
                  extra_warnings: list[dict]) -> dict
```
Definitions:
- `H` (horizon) = `payday_info["payday"]["date"]`, or the last day of today's month when there is
  no payday (warning `no_pay_schedule` if `not has_schedules`, else `no_upcoming_payday`).
- `period_start` = `payday_info["last_payday"]` or `today`.
- `days_until` = `max(0, (H - today).days)`; `window_days` = `max(1, days_until)`;
  `dim` = days in today's month.
- Bills (`paid_from == 'cash'`, `due_day` set): every due date `D` in `[period_start, H]`.
  `(id, D)` in `bill_payments` -> status `paid` (listed, not reserved); `D < today` ->
  `overdue` (reserved); else `due` (reserved).
- Bills paid by card (`paid_from == 'card'`): due dates in `[today, H]`, status `on_card`, not reserved.
- Undated cash bills: one line, status `spread`, amount
  `round_half_up(amount * min(window_days, dim) / dim)`, reserved, date = H.
- Cards: for each month M from today's month through H's month (callers must supply
  `card_rows` for exactly those months), for each row: `D` = `due_day` clamped in M, or None.
  - `D is None` and `remaining > 0` -> `due`, reserved, date = H, `undated: true`.
  - `D <= H` and (M is today's month or `D >= today`):
    `remaining > 0` -> `overdue` if `D < today` else `due` (reserved, amount = remaining);
    `remaining == 0` -> `paid` (listed, amount = recommended).
- `obligations_total` = sum of reserved amounts.
- `safe_to_spend` = `cash_total - pending_out - obligations_total - goals_set_aside - cash_floor`.
- `per_day` = `max(0, safe_to_spend) // window_days`.
- `planned_spending` = `round_half_up(spending_monthly * min(window_days, dim) / dim)`.
- Warnings: `paycheck_late` (payday status late), one `paycheck_missed` per missed occurrence,
  `stale_balance` when `cash_as_of` is None or `now - cash_as_of > 3 days`, `short` when
  safe_to_spend < 0, plus `extra_warnings` appended unchanged.

Returns (exact shape; dates ISO strings):
```json
{
  "today": "2026-10-02", "horizon": "2026-10-09", "days_until": 7,
  "payday": {"date": "2026-10-09", "schedule_id": 1, "name": "Main paycheck",
             "expected_cents": 215000, "status": "upcoming"},
  "period_start": "2026-09-25",
  "cash_total_cents": 412300, "pending_out_cents": 0, "cash_as_of": "2026-10-02T10:30:00Z",
  "obligations": [
    {"kind": "bill", "id": 3, "name": "Electric", "date": "2026-10-05", "amount_cents": 14000,
     "status": "due", "reserved": true, "undated": false},
    {"kind": "card", "id": 2, "name": "Example Bank", "date": "2026-10-07", "amount_cents": 183000,
     "status": "due", "reserved": true, "undated": false}
  ],
  "obligations_total_cents": 197000,
  "goals_set_aside_cents": 45000, "cash_floor_cents": 0,
  "safe_to_spend_cents": 170300, "per_day_cents": 24328,
  "planned_spending_cents": 18667,
  "warnings": [{"code": "paycheck_late", "message": "Main paycheck expected Oct 9 hasn't arrived yet",
                "date": "2026-10-09"}]
}
```
Obligations sorted by date, then kind (bill before card), then id. `message` strings are
plain English, built in sts.py for every code it emits (extra_warnings arrive with messages).

## Bank transactions (owned by the backend-data coder: `app/plaid.py`, `app/txns.py`)

### Sync

`sync_item` gains a transactions step after accounts/liabilities:
- `POST /transactions/sync` with `{access_token, cursor (omit when NULL), count: 500}`; loop while
  `has_more`, accumulating `added`, `modified`, `removed`. On `PlaidError` with code
  `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION`, restart from the item's saved cursor (max 3
  attempts). `PRODUCT_NOT_READY` / `PRODUCTS_NOT_SUPPORTED` / `PRODUCT_NOT_ENABLED` -> skip,
  item stays `ok`. Other errors -> existing item error handling.
- Apply all changes **and** save `next_cursor` in one DB transaction (`db.tx`).
- Store only txns whose `account_id` is a stored `plaid_accounts` row with type `depository`
  or `credit`. Upsert on `transaction_id`. `amount_cents` = half-up cents of Plaid `amount`.
  `pfc_primary/pfc_detailed` from `personal_finance_category`. `removed` -> delete row.
- Pending -> posted: when an added txn's `pending_transaction_id` matches a stored row that has
  a label, copy `label, label_source, pay_schedule_id` onto the new row (then the pending row
  is removed by Plaid's `removed` list, or by us if still present).
- After storing: apply deposit rules to unlabelled inflows (below), then run bill auto-matching
  (`txns.run_bill_matching(conn, today)`), which inserts `bill_payments` with `source='auto'`.
- Never log access tokens, request bodies, or transaction names.

### `app/txns.py` (DB helpers, unit tested against a tmp DB)

- `apply_rules(conn, only_ids=None)`: for inflows (`amount_cents < 0`) on **linked cash accounts**
  with `label IS NULL`, the first rule (lowest id) whose `match_text` is a substring of
  `lower(merchant_name or '') || ' ' || lower(name)` sets `label`, `pay_schedule_id`,
  `label_source='rule'`. Never touches `manual` labels.
- `set_label(conn, transaction_id, label, pay_schedule_id, create_rule, rule_text)`:
  manual label (`label_source='manual'`; `label=None` clears and sets source NULL).
  `create_rule=True` upserts a rule (match_text = `rule_text` or the txn's merchant_name or
  name, lowercased/trimmed) and re-applies rules to unlabelled inflows.
- `paychecks(conn, single_schedule_id)`: posted inflows labelled `paycheck` -> list of dicts for
  `paydays`, `amount_cents` positive; NULL `pay_schedule_id` becomes `single_schedule_id`.
- `run_bill_matching(conn, today)`: loads bills, outflows from linked cash **and** card accounts
  in the last 60 days, existing `bill_payments`, calls `bills.match_bills`, inserts results.
- `unlogged_card_payments(conn, today)`: posted txns on **linked card** accounts with
  `amount_cents < 0` (a payment to the card), date within the last 45 days, `label` not
  `dismissed`, and no `payments.transaction_id` referencing them.
- `deposits(conn, today, days=45, include_transfers=False)`: inflows on linked cash accounts,
  newest first, with account name/mask; `include_transfers=False` hides `pfc_primary =
  'TRANSFER_IN'` rows unless labelled.

## Wiring (owned by the wiring coder, runs after the two backend coders land)

`app/main.py`, `app/models.py`, `app/budget.py`, `app/cash.py`, `run.py`, `sync_cli.py`.

### Shared helpers in main.py
- `_pending_out(conn)` / `_cash_as_of(conn)`: move the existing phase-3 logic into helpers used by
  both the month bundle and safe-to-spend. **Change:** payments with `transaction_id` are
  excluded from pending_out (they are already posted at the bank).
- `_plan_rows(conn, month)`: the month bundle's plan rows (with `paid_cents`,
  `remaining_cents`) **plus `due_day`** from the card. Used for `card_rows` (current month and
  every month through the horizon month).
- `_income_rows(conn)`: income rows where `pay_schedule_id` is set and the schedule is active
  use `monthly_equivalent_cents(schedule)` as `amount_cents` (UI shows "from pay schedule").
  Budget and everything else use these rows.
- Goals: `set_aside(goal, before=None)` from `goal_moves`; `goals_set_aside_total` for active goals.

### Budget and cash changes
- `budget.py`: new input `goals_monthly_cents` (sum of `monthly_need` for active goals for month M,
  computed by main.py with set-aside before M). Waterfall:
  `available_after_min = income - bills - spending - adjust_out - minimums - goals_monthly`.
  Same subtraction in the recurring path. New key `goals_cents`. Nothing else changes.
- `cash.py` spare cash: `reserve += goals_set_aside_total` (money earmarked in checking).
  New bundle key `cash.goals_set_aside_cents`.

### Routes (all behind basic auth; exact shapes)

Pay schedules
- `GET /api/pay-schedules` -> `[PayScheduleOut]`
- `POST /api/pay-schedules`, `PUT /api/pay-schedules/{id}` (partial), `DELETE /api/pay-schedules/{id}`
- `GET /api/pay-schedules/{id}/upcoming?count=6` -> `{"dates": ["2026-10-09", ...]}`
- `POST /api/pay-schedules/detect` body `{"transaction_ids": [str] | null}` (null = all posted
  `paycheck`-labelled deposits from the last 120 days) -> `infer_schedule` result or
  `{"suggestion": null, "reason": "need at least 2 paychecks"}`. Shape:
  `{"suggestion": {...infer_schedule...} | null, "reason": str | null}`.
- `POST /api/pay-schedules/{id}/use-as-income` -> creates (or returns the existing) income row with
  `pay_schedule_id` set and name = schedule name; returns the income row.

`PayScheduleOut`:
```json
{"id": 1, "name": "Main paycheck", "frequency": "biweekly", "anchor_date": "2026-09-25",
 "semimonthly_day1": null, "semimonthly_day2": null, "weekend_rule": "previous",
 "amount_cents": 215000, "cash_account_id": 4, "active": true,
 "monthly_equivalent_cents": 465833, "next_dates": ["2026-10-09", "2026-10-23", "2026-11-06"],
 "deposit_confirmed": true, "last_paycheck": {"date": "2026-09-25", "amount_cents": 214988} }
```
Validation: frequency enum; semimonthly requires both days (1..31, day1 < day2), other
frequencies must not send them (422); weekend_rule enum; name 1..80.

Transactions and labels
- `GET /api/deposits?days=45&include_transfers=false` -> `[DepositOut]`
- `PUT /api/transactions/{transaction_id}/label` body
  `{"label": "paycheck" | "not_paycheck" | null, "pay_schedule_id": int | null,
    "create_rule": bool, "rule_text": str | null}` -> `DepositOut`. 404 unknown id; 422 when the
  txn is not an inflow on a linked cash account.
- `GET /api/deposit-rules` -> `[{"id", "match_text", "label", "pay_schedule_id", "pay_schedule_name"}]`;
  `DELETE /api/deposit-rules/{id}` (does not change existing labels).
- `GET /api/card-payments/unlogged` -> `[{"transaction_id", "date", "amount_cents" (positive),
  "card_id", "card_name", "name"}]`
- `POST /api/card-payments/{transaction_id}/log` -> creates a payment (card from the account link,
  `month` = txn month, `paid_on` = txn date, `amount_cents` = -txn amount, `transaction_id`,
  note "From your bank"); returns the payment. 409 if already logged.
- `POST /api/card-payments/{transaction_id}/dismiss` -> sets label `dismissed`; 204.

`DepositOut`:
```json
{"transaction_id": "abc", "date": "2026-09-25", "amount_cents": 214988, "name": "ACME PAYROLL",
 "merchant_name": "Acme", "pending": false, "pfc_primary": "INCOME", "pfc_detailed": "INCOME_SALARY",
 "account_name": "Checking", "mask": "1111", "cash_account_id": 4,
 "label": "paycheck", "label_source": "rule", "pay_schedule_id": 1, "pay_schedule_name": "Main paycheck"}
```
(`amount_cents` here is **positive**: the deposit amount.)

Bills
- `ExpenseIn` gains `paid_from` (cash|card, default cash) and `match_text`
  (nullable, 2..80 chars, stored trimmed). Expense responses include both.
- `POST /api/bills/{expense_id}/paid` body `{"due_date": "YYYY-MM-DD"}` -> manual bill_payment
  (`paid_on` = today); 409 if already paid; 422 if `due_date` is not a due date of that bill
  or the expense is not `kind='bill'`.
- `DELETE /api/bills/{expense_id}/paid/{due_date}` -> 204 (works for auto too; an auto-match
  removed by hand must not be re-created: delete the bill_payment and set the matched
  transaction's label to `dismissed` so `run_bill_matching` ignores it).

Goals
- `GET /api/goals` -> `[GoalOut]`; `POST`, `PUT` (partial), `DELETE /api/goals/{id}`.
- `POST /api/goals/{id}/move` body `{"amount_cents": int != 0, "note": str | null}` -> GoalOut.
  Negative moves cannot take `set_aside` below 0 (422).
```json
GoalOut: {"id": 1, "name": "Car insurance", "target_cents": 90000, "target_date": "2027-03-01",
          "active": true, "set_aside_cents": 15000, "progress_pct": 16.7,
          "per_paycheck_cents": 6000, "monthly_need_cents": 15000,
          "moves": [{"id": 1, "amount_cents": 15000, "moved_on": "2026-10-02", "note": null}]}
```
`monthly_need_cents` is for the current month; `per_paycheck_cents` uses the next 120 days of
paydays from all active schedules (`occurrences` with `date >= today`).

Safe-to-Spend and upcoming
- `GET /api/safe-to-spend` -> the `sts.safe_to_spend` dict plus
  `"accounts": [{"id","name","balance_cents","sync","last_synced_at"}]` (included cash accounts)
  and `"unlabelled_income_count"`, `"unlogged_card_payment_count"`. Before computing, run
  `txns.run_bill_matching(conn, today)`. `extra_warnings` include:
  `unlabelled_income` (count of posted INCOME_* inflows in the last 30 days with no label,
  only when there is at least one active schedule or no schedule at all),
  `unlogged_card_payments`, `undated_bills` (cash bills without a due day),
  `plaid_item_error` (any item status != ok, message names the institution).
- `GET /api/upcoming?days=30` -> `[{"date", "kind": "payday"|"bill"|"card"|"goal",
  "id", "name", "amount_cents", "status"}]` over `[today, today+days]`: paydays from
  `occurrences` (status `expected`), bills via `due_dates` (status `paid`/`due`, card-paid
  bills `on_card`), cards by due day using that month's plan row remaining (`paid` when 0),
  goal target dates (amount = target - set_aside). Sorted by date then kind order
  payday, bill, card, goal.

`/api/month/{m}`: plan rows gain `due_day`; `cash` gains `goals_set_aside_cents`; budget gains
`goals_cents`.

### Sync CLI / timer
`sync_cli.py` output line per item adds `transactions_added=N modified=N removed=N`.

## Frontend (owned by the frontend coder, `app/static/**` only)

Navigation: **Today · Month · Accounts · Budget · Plan · Settings**. `#/today` is the new
default route (empty hash -> today). At 375px the nav scrolls horizontally inside `#nav`
(no document overflow).

### Today view (`views/today.js`)
1. **Hero**: "Safe to spend" + big amount (red + "Short by $X" when negative), then
   "about $Y a day until <Weekday Mon D> (N days)" or "Payday is today" / "Paycheck expected
   <date>, not seen yet" from payday status; with no payday: "Until <month end> · add your pay
   schedule for a more accurate number" linking to `#/budget`.
2. **Breakdown** (`<details>` open by default on desktop, closed on mobile is fine):
   Cash in checking & savings; − card payments logged, not yet at the bank (if > 0);
   − bills before payday; − card payments before payday; − set aside for goals (if > 0);
   − cash floor (if > 0); = Safe to spend. Sum lines from `obligations` by kind (reserved only).
   Below: "Planned spending until payday: $P" with "covered" / "$Q short" vs safe_to_spend.
3. **Warnings** as banners (message text from the API, escaped).
4. **Before payday** list: every obligation (date, name, amount, status pill:
   due / overdue / paid / on card / spread). Bills with status due/overdue get **Mark paid**
   (POST bills paid with that `date`), paid bills get **Undo** (DELETE). Cards with
   due/overdue link "Log payment" -> `#/month/<YYYY-MM of that date>`.
5. **Deposits to review**: from `GET /api/deposits` (last 45 days): unlabelled ones first.
   Each row: date, name, account ••mask, amount, buttons **Paycheck** and **Not a paycheck**.
   Paycheck opens a small dialog: schedule select (active schedules + "No schedule yet"),
   checkbox "Always treat deposits matching" + editable text prefilled with merchant_name or
   name, Save -> PUT label. Labelled rows show a pill (Paycheck · <schedule> / Not a paycheck,
   "rule" suffix when label_source = rule) and **Change**. "Show transfers" toggle refetches
   with `include_transfers=true`. After labelling >= 2 paychecks and no schedule exists, show
   "Set up your pay schedule from these paychecks" -> calls detect and opens the schedule
   dialog (Budget's) prefilled.
6. **Card payments from your bank not logged yet**: from `/api/card-payments/unlogged`; each with
   **Log it** and **Dismiss**.
7. **Coming up (30 days)**: `/api/upcoming?days=30` grouped by date, paydays highlighted.

### Budget view additions
- **Pay schedule** panel (top of Budget): list schedules (name, frequency in words e.g.
  "Every other Friday", "1st and 15th", "Monthly on the 25th", amount, "≈ $X/month", next 3
  dates, "Confirmed by deposits" pill when deposit_confirmed, last paycheck). Add/Edit dialog:
  name, frequency, a known payday (date input), semimonthly days (shown only for semimonthly;
  "Last day" option = 31), weekend rule ("If payday is on a weekend: Friday before / Monday
  after / No change"), expected amount, deposit account (cash accounts select, optional),
  active. **Detect from labelled paychecks** button (calls detect with null, prefills dialog,
  shows confidence + dates used). **Use as income** button per schedule (POST use-as-income);
  income rows with `pay_schedule_id` show "from pay schedule" and their amount is read-only
  in the income dialog.
- Expense dialog: for kind=bill add **Paid from** (Checking / Credit card) and **Bank
  description contains** (match_text, optional, hint "Lets Paydown mark this bill paid
  automatically").
- **Goals** panel: list (name, progress bar set_aside/target, target date, "Set aside $X each
  paycheck" when per_paycheck_cents, "$Y/month in your budget" when monthly_need_cents > 0),
  buttons **Set aside** and **Use** (move dialogs; Use sends a negative amount), Edit, Delete.
  Add goal dialog: name, target ($), target date (optional).
- Waterfall: "Set aside for goals" line after card minimums when `goals_cents > 0`.

### Month view
- Cash panel shows "+ $G set aside for goals" in the reserve sentence when > 0.

### Settings
- **Deposit rules** section: list (match text, label, schedule) with Delete.

All user and bank strings through `esc()`. No inline scripts/styles. Reuse existing helpers.

## Tests (each coder for their files; mocked Plaid only)

Backend-pure (`tests/test_paydays.py`, `tests/test_bills.py`, `tests/test_goals.py`, `tests/test_sts.py`):
- occurrences: weekly; biweekly with negative k (anchor after start); monthly 31 in Feb (leap and
  non-leap) and April; semimonthly 15/31; weekend previous and next, including a nominal Sunday
  payday on the 1st moving to the previous month's last Friday and still returned when the
  window covers it; `none`.
- receipts/next_payday: unconfirmed schedule rolls at midnight of payday; confirmed schedule on
  payday morning (status today), paycheck 2 days early counts, 2 days late (status late),
  4 days late (missed, next occurrence becomes the candidate); two schedules pick the earliest;
  last_payday.
- infer_schedule: weekly; biweekly with one weekend-shifted payday; semimonthly 1/15 and 15/last
  (incl. Feb 28); monthly; 2 dates -> low confidence; random dates -> None.
- match_bills: substring match on merchant or name, ±7 day window, closest wins, a txn never
  matches two bills, already-paid skipped, card-account outflows allowed.
- goals: suggestion with 0/1/several paydays, ceil to dollar; monthly_need across months and
  after target month.
- safe_to_spend: the JSON example above reproduced exactly (Oct has 31 days; spending_monthly
  82667 gives planned 18667); overdue bill since period start is
  reserved but one before period start is not; paid bill listed not reserved; card due next
  month inside the horizon uses next month's row; current-month card due before period start
  with remaining is overdue and reserved; card-paid bill not reserved; undated bill spread
  amount; goals and floor subtracted; negative -> `short` warning and per_day 0; no schedules
  -> horizon month end + `no_pay_schedule`; stale balance.

Backend-data (`tests/test_txns.py`): sync pagination with has_more; mutation-during-pagination
restart; PRODUCT_NOT_READY leaves item ok; removed deletes; pending->posted carries a manual
label; cursor saved only when the whole sync commits (inject a failure mid-apply and assert the
cursor and rows are unchanged); unlinked accounts' txns ignored; apply_rules never overrides
manual; set_label with create_rule labels other matching deposits; unlogged_card_payments
excludes logged and dismissed; run_bill_matching inserts and is idempotent; migrations run twice
on a phase-3-shaped DB without error.

Wiring (`tests/test_sts_api.py`): every route above incl. validation errors; pending_out
excludes payments with transaction_id; goals reduce the budget waterfall and spare cash;
income from pay schedule uses the monthly equivalent; deleting an auto bill payment stays
deleted after the next matching run; `/api/safe-to-spend` end-to-end with a frozen clock
(monkeypatch `app.clock.today`/`now`) and a seeded DB; export/import round-trip of new tables.

## Ownership summary

| Coder | Files | Starts |
|---|---|---|
| backend-pure | `app/paydays.py`, `app/bills.py`, `app/goals.py`, `app/sts.py`, their tests | now |
| backend-data | `app/clock.py`, `app/db.py`, `app/plaid.py`, `app/txns.py`, `tests/test_txns.py`, mock updates in `tests/test_plaid.py` | now |
| frontend | `app/static/**` | now (mock first, real backend before reporting) |
| wiring | `app/main.py`, `app/models.py`, `app/budget.py`, `app/cash.py`, `run.py`, `sync_cli.py`, `tests/test_sts_api.py`, expectation updates in existing tests | after both backend coders land |

`txns.py` imports `bills.match_bills` from backend-pure's module; backend-data codes against
the signature above and may stub it locally in tests until it lands, but must not create
`app/bills.py`.
