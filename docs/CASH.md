# Phase 3: cash accounts (checking/savings) in the plan

PLAN.md, PLAID.md and AGENTS.md still apply. This file wins where they conflict.

## Goal

The plan should know how much cash the user actually has. Checking/savings balances (manual or
synced from Plaid) are combined with bills, spending and card minimums to find **spare cash**:
money sitting in checking beyond what the next month needs. You can move spare cash into
this month's card payments with one click.

## The model (read this before coding)

Income-based extra (existing) answers "how much of each month's income can go to cards".
Cash answers a different question: "is there a pile of money in checking beyond what the
coming month needs?" Counting checking as income would double count the paycheck, so cash
never flows into the plan automatically. It flows in only when the user applies it, as a
**cash sweep**: a one-off record for the month that adds straight to that month's extra.

For the current month only:

```
cash_total        = sum(balance of cash accounts with include_in_cash = 1)
essentials        = bills_total + spending_total + minimums_total          # one month, from budget
extra_remaining   = sum over plan rows of max(0, remaining_cents - max(0, minimum_cents - paid_cents))
                    # unpaid EXTRA in this month's plan (unpaid minimums are inside essentials)
reserve           = cash_floor + round(essentials * cash_reserve_months) + extra_remaining
spare             = max(0, cash_total - reserve), floored to a whole dollar
months_covered    = (cash_total - cash_floor - extra_remaining) / essentials   (null if essentials == 0), 1 decimal
```

Why one month of essentials and not "bills left this month": a paycheck that lands on the
28th is for the rent due on the 1st. A calendar-month view would call it spare. A rolling
month of essentials with no future income assumed never does.

**Payments not yet reflected in the cash balance.** A card payment logged today hasn't left
checking until the bank posts it (Plaid) or the user edits the balance (manual). Without
handling this, logging a payment drops `extra_remaining` while cash stays put, and spare
jumps by the payment. So:

```
cash_as_of        = latest balance timestamp across included accounts
                    (last_synced_at for plaid, balance_updated_at for manual; ISO UTC datetimes)
pending_out       = sum(payments.amount_cents where payments.created_at > cash_as_of)   # all cards
effective_cash    = cash_total - pending_out
```
Timestamps, not dates: if a user logs a payment and fixes the checking balance on the same day,
date comparison can't tell which came first and would subtract the payment twice.
`payments` gains `created_at TEXT` (ISO UTC datetime, set on insert; migration backfills
existing rows with `paid_on || 'T00:00:00Z'`). `cash_accounts` gets `balance_updated_at TEXT`
(set on create and on every balance change, manual or synced).
and `effective_cash` replaces `cash_total` in `reserve`/`spare`/`months_covered`.
Plaid uses `balances.available`, which usually already reflects pending debits; a payment
pending across a sync can briefly overstate spare by that payment. Accepted.

Use-cash cap: `amount <= min(spare, sum over open cards of (plan balance - recommended))`
so a sweep can't exceed what the cards can absorb this month.

**Stability invariant (must be tested):** applying a sweep of X then logging card payments
totalling X leaves `spare` and every row's `recommended_cents` unchanged after the sweep.
(Apply X: extra +X, extra_remaining +X, spare -X. Log payments of X: pending_out +X,
extra_remaining -X, spare unchanged. Then sync/edit the cash balance down by X on a later
date: pending_out -X, cash -X, spare unchanged.) Test all three steps with a manual account.

## Pre-existing bug to fix first (red before green)

One-off month adjustments are projected as if they recur every month: `/api/month/{m}`
and `/api/projection` call `compare(cards, debt_budget_for_M, ...)`, so a $2,000 bonus in
October makes every future month look $2,000 richer. Fix:

- `budget.py` returns `recurring_debt_budget_cents`: the debt budget computed with no
  adjustments and no sweeps.
- `engine.simulate(..., first_month_budget_cents=None)`: when set, month 1 uses it instead of
  `monthly_budget_cents`. Plumb through `compare()`.
- Projections use `monthly_budget = recurring_debt_budget`, `first_month_budget =
  debt_budget_for_M`. `/api/projection?extra=` replaces the recurring extra (month 1 =
  recurring with that extra + this month's one-off delta).
- Test: a +$2,000 adjustment in month M changes the debt-free month by at most what a
  $2,000 lump would, and the schedule's month 2 total payment equals the recurring budget.

## Budget changes (`budget.py`)

- `month_adjustments` gains `kind TEXT NOT NULL DEFAULT 'manual'` (manual | cash).
- Manual adjustments behave exactly as now (positive = income through buffer and
  debt_share_pct, negative = expense).
- Cash sweeps (`kind='cash'`, always positive) **bypass buffer and share**:
  `extra = base_extra + cash_applied`, where `base_extra` is the existing override-or-
  suggestion. They also add to `debt_budget`. New keys: `cash_applied_cents`,
  `recurring_debt_budget_cents`. `leftover_cents` is unchanged by sweeps (the sweep comes
  out of checking, not income).

## Cash accounts

```sql
CREATE TABLE IF NOT EXISTS cash_accounts (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'checking',           -- checking | savings | other
  balance_cents INTEGER NOT NULL,                  -- may be negative (overdrawn)
  include_in_cash INTEGER NOT NULL DEFAULT 1,      -- savings you don't want touched: 0
  sync TEXT NOT NULL DEFAULT 'manual',             -- manual | plaid
  last_synced_at TEXT, balance_updated_at TEXT, created_at TEXT NOT NULL, sort INTEGER NOT NULL DEFAULT 0
);
```
Settings gain `cash_floor_cents INTEGER NOT NULL DEFAULT 0` and
`cash_reserve_months REAL NOT NULL DEFAULT 1.0` (0 to 6). Migrations idempotent (PRAGMA
table_info guard). Export/import include `cash_accounts`.

Routes: `GET/POST /api/cash-accounts`, `PUT/DELETE /api/cash-accounts/{id}` (PUT partial;
balance edits on `sync='plaid'` accounts return 409 "Balance comes from your bank").
`CashAccountIn`: name 1-80, kind enum, balance_cents -10^10..10^10, include_in_cash bool.
Response adds `institution_name`, `mask` (null when manual).

## Month bundle

`GET /api/month/{m}` gains:
```json
"cash": {
  "is_current_month": true,
  "accounts": [{"id": 1, "name": "Checking", "kind": "checking", "balance_cents": 412300,
                "include_in_cash": true, "sync": "plaid", "last_synced_at": "...",
                "institution_name": "Chase", "mask": "1111"}],
  "cash_total_cents": 412300, "cash_floor_cents": 0, "reserve_months": 1.0,
  "essentials_cents": 333500, "extra_remaining_cents": 0, "reserve_cents": 333500,
  "spare_cents": 78800, "months_covered": 1.2, "applied_cents": 0
}
```
`cash` is `null` when there are no cash accounts. For months other than the current one,
`spare_cents` and `months_covered` are null (`is_current_month: false`) but totals still show.
Implement the math as a pure function `cash_position(...)` in `app/cash.py`.

`POST /api/month/{m}/use-cash` body `{"amount_cents": int | null}` (null = all spare).
Only for the current month (else 409). `0 < amount <= spare` (else 422). Inserts a
`month_adjustments` row `{kind: "cash", name: "Spare cash from checking", amount_cents}`.
Returns the new month bundle. Removing a sweep uses the existing
`DELETE /api/adjustments/{id}`.

## Plaid changes

- New-item link token: `products: ["transactions"]`,
  `required_if_supported_products: ["liabilities"]`,
  `account_filters: {"credit": {"account_subtypes": ["credit card"]}, "depository": {"account_subtypes": ["checking", "savings"]}}`.
  (Verified accepted by production on 2026-10-02. Transactions keeps depository balances
  refreshed daily; liabilities is added whenever the bank has a card.)
- Sync per item: call `/accounts/get` (free, cached daily balances for every account) and
  `/liabilities/get` only if any returned account has `type == "credit"`. Treat a liabilities
  error with `error_code` in {`PRODUCTS_NOT_SUPPORTED`, `NO_LIABILITY_ACCOUNTS`,
  `PRODUCT_NOT_ENABLED`} as "no credit details" (cards still get balance/limit from
  `/accounts/get`), not an item error. Other errors keep the existing behavior.
- `plaid_accounts` gains `type TEXT`, `subtype TEXT`, `cash_account_id INTEGER UNIQUE REFERENCES cash_accounts(id) ON DELETE SET NULL`.
  Upsert every account returned whose type is `credit` or `depository`.
- Depository balance: `balances.available` if not null, else `balances.current`, to cents
  (half-up). Linked cash accounts get that balance and `last_synced_at` on each sync.
- `POST /api/plaid/accounts/{id}/link` accepts, by account type:
  credit: `{"card_id": int}` or `{"create": true}` (existing);
  depository: `{"cash_account_id": int}` or `{"create": true}` (creates a cash account named
  from official_name/name, kind from subtype, include_in_cash = subtype == "checking").
  Wrong pairing (card_id on a depository account, etc.) -> 422.
  Unlink sets the cash account back to `sync='manual'`.
- `AccountOut` gains `"type"`, `"subtype"`, `"cash_account_id"`, `"cash_account_name"`.

## Frontend

- Rename nav **Cards** to **Accounts** (route `#/accounts`; keep `#/cards` working as an
  alias). Page order: Bank sync panel, **Cash accounts** section, **Credit cards** section.
- Cash accounts section: list (name, kind pill, balance, "Not counted" pill when
  include_in_cash is false, Synced line like cards), Add/Edit/Delete dialogs (balance,
  kind, include toggle; balance disabled for synced with "Updated from your bank daily").
- Bank sync panel: unlinked accounts are grouped "Cards" and "Cash accounts"; the link
  select lists cards for credit accounts and cash accounts for depository accounts.
- Month view (current month, `cash` not null): a **Cash** panel under the summary tiles:
  "Checking & savings $X", "Keeps $Y for the next month of bills, spending and minimums"
  (+ floor and unpaid extra when non-zero), "Covers N months". If `spare_cents > 0`:
  "**$Z spare** — put it toward your cards this month" with a button that opens a dialog
  (amount prefilled with spare, editable) and POSTs use-cash, then re-renders. Applied
  sweeps appear in the adjustments list labelled "Cash from checking" and are deletable.
  The summary tiles add a "Cash" tile.
- Budget waterfall shows a "Cash from checking" line under Extra when `cash_applied_cents > 0`.
- Settings: "Cash floor ($)" and "Reserve (months of essentials)" inputs with one-line hints.
- Empty-state copy mentions cash accounts as optional.

## Tests (backend)

cash_position pure cases (spare, negative balance, include_in_cash false, essentials 0,
reserve months 0 and 1.5, floor); stability invariant above (red before green is not
possible for new code; write it first anyway and show it passing with the real sweep path);
the projection bug (red before green); sweep bypasses buffer and debt_share_pct; use-cash
409 outside current month and 422 above spare; cash account CRUD + 409 on synced balance
edit; Plaid link token body (products, required_if_supported_products, both filters);
sync with a depository-only item (no liabilities call made), with a mixed item, and with
NO_LIABILITY_ACCOUNTS (item status stays ok); depository uses `available` then `current`;
link pairing 422; export/import round-trip includes cash_accounts. Mock Plaid only.

## Ownership and rules

- Backend coder: `app/*.py` (new `app/cash.py`), `tests/**`, `sync_cli.py` if needed. Not `app/static/**`.
- Frontend coder: `app/static/**` only, coding against the exact JSON shapes above.
- No Plaid network calls of any kind (10-Item lifetime cap, real production credentials).
  Never read `~/.config/budget.env`. All Plaid behavior is tested with httpx.MockTransport.
- Existing 65 tests must stay green; do not delete, skip, weaken or narrow them. Where a
  test encodes the projection bug, change its expectation and say so in the report.
- Kill test servers by PID (never `pkill -f`). Ports: backend 8486, frontend 8489.
- Frontend must be verified against the REAL backend from this round once the backend coder
  has landed it: if backend routes aren't there yet, verify against a mock first, then poll
  `uv run pytest -q tests/test_cash.py` until it exists and passes, and re-verify on the
  real server (seed via curl, Basic auth header via CDP) before reporting.
