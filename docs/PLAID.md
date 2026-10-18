# Phase 2: card sync via Plaid (build spec)

PLAN.md and AGENTS.md still apply.

## Facts already verified by the orchestrator (do not re-verify against production)

- `~/.config/budget.env` holds `PLAID_CLIENT_ID`, `PLAID_CLIENT_SECRET` (note the name:
  CLIENT_SECRET), `PLAID_ENV=production`, `PLAID_TOKEN_KEY` (Fernet key). Production keys are
  valid; sandbox rejects them. **Never read or print that file.** The service loads it via systemd.
- `/link/token/create` on production with `products=["liabilities"]` and
  `account_filters={"credit":{"account_subtypes":["credit card"]}}` succeeds.
- Passing `redirect_uri` fails until it is registered in the Plaid dashboard, so it is
  **optional**: include it only when env `PLAID_REDIRECT_URI` is set. Web Link uses a popup
  (new tab on mobile) without it.
- Trial plan: 10 Items lifetime, removing an Item does not free a slot. **Coders must never
  complete a Link flow or call `/item/public_token/exchange` against production.** All
  tests mock Plaid HTTP. Coders make NO production Plaid calls at all; the orchestrator
  does the live check.

## Dependencies

Added by orchestrator already: `httpx` (runtime), `cryptography` (Fernet). Nothing else.

## Backend

### Config (`app/plaid.py`)

`PlaidConfig.from_env()` reads PLAID_CLIENT_ID, PLAID_CLIENT_SECRET, PLAID_ENV
(`sandbox`|`production`, default production), PLAID_TOKEN_KEY, PLAID_REDIRECT_URI (optional).
`create_app(..., plaid=PlaidClient|None)`; run.py builds it from env. If any required var
is missing, `plaid=None` and every `/api/plaid/*` route except `/api/plaid/status` returns
503 `{"detail": "Plaid is not configured"}`. The app must keep working without Plaid.

`PlaidClient(config, transport: httpx.BaseTransport | None = None)` wraps
`https://{env}.plaid.com` with `httpx.Client(timeout=30, transport=transport)`. Tests inject
`httpx.MockTransport`. Methods: `link_token_create(access_token=None)`,
`exchange(public_token)`, `liabilities_get(access_token)`. Plaid errors become
`PlaidError(error_type, error_code, message, status)`. Never log request bodies (they contain
the secret) or access tokens.

Access tokens: `Fernet(PLAID_TOKEN_KEY).encrypt(...)` before storing; decrypted only where a
Plaid call needs it. Never returned by any API route, never in export.

### Schema (idempotent migrations in db.init_db)

```sql
CREATE TABLE IF NOT EXISTS plaid_items (
  id INTEGER PRIMARY KEY,
  item_id TEXT NOT NULL UNIQUE,
  access_token_enc TEXT NOT NULL,
  institution_name TEXT,
  status TEXT NOT NULL DEFAULT 'ok',        -- ok | login_required | error
  error_code TEXT,
  last_synced_at TEXT,                       -- ISO datetime UTC, "Z" suffix
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plaid_accounts (
  id INTEGER PRIMARY KEY,
  plaid_item_id INTEGER NOT NULL REFERENCES plaid_items(id) ON DELETE CASCADE,
  account_id TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL, official_name TEXT, mask TEXT,
  card_id INTEGER UNIQUE REFERENCES cards(id) ON DELETE SET NULL,
  ignored INTEGER NOT NULL DEFAULT 0,
  last_balance_cents INTEGER,
  last_payload TEXT          -- {"account": {...}, "credit": {...}} JSON from the last liabilities call
);
```
`cards` gains `sync TEXT NOT NULL DEFAULT 'manual'` (manual | plaid) and
`last_synced_at TEXT` via `ALTER TABLE ... ADD COLUMN` guarded by a PRAGMA table_info check.
Export/import: `plaid_items` and `plaid_accounts` are excluded from export and untouched by
import; after import, set `plaid_accounts.card_id = NULL` where the card id no longer exists,
and set `sync='manual'` on any card not linked by a plaid_accounts row.

### Mapping Plaid -> card (pure function `map_liability(account, credit, existing) -> dict`, unit tested)

Plaid amounts are float dollars; convert with `Decimal(str(x)) * 100` rounded half-up to int.
- `balance_cents` = `account.balances.current` (positive = owed; clamp negatives to 0).
- `credit_limit_cents` = `account.balances.limit` (None stays None).
- `min_payment_cents` = `credit.minimum_payment_amount`; if null/absent, keep `existing`.
- `due_day` = day of `credit.next_payment_due_date` if present, else keep existing.
- `apr`: from `credit.aprs`. If any entries have `balance_subject_to_apr > 0`, use the
  balance-weighted average of `apr_percentage` over those, rounded to 2 decimals. Else use the
  `purchase_apr` entry. Else keep the existing APR. `apr_source`: weighted | purchase | manual.
`credit` may be None (account returned without liabilities data): only balance and limit apply.

### Sync (`sync_all(conn, client)` and `sync_item(conn, client, item_row)`)

For each item: `liabilities_get`; upsert `plaid_accounts` (name, official_name, mask,
last_balance_cents, last_payload) for every account with type `credit` returned; for each linked
account (card_id not null) apply `map_liability` to the card, set `cards.last_synced_at`, and
write a `balance_log` row with source `plaid` **only when the balance changed or no `plaid`
row exists for that card today**. Set item `status='ok'`, `error_code=NULL`,
`last_synced_at`. On `PlaidError` with `error_code == "ITEM_LOGIN_REQUIRED"` set
`status='login_required'`; other Plaid errors `status='error'` with `error_code` stored. One
item failing must not stop the others. Returns
`{"items": [{"id", "institution_name", "status", "error_code", "accounts_updated"}], "synced_at"}`.

### Payments on synced cards

For cards with `sync='plaid'`, logging or deleting a payment records/removes the payment row
but **does not change `cards.balance_cents`** (the bank balance is authoritative; changing
it would double count once the payment posts). No `payment`/`payment_delete` balance_log
rows for those cards.

### Plan balance for synced cards (replaces the B1 rule for sync='plaid' only)

Manual cards keep `balance + sum(payments with month >= M)`. Synced cards use a frozen
snapshot so recommendations do not move mid-month as the bank balance changes:
`plan_balance(M)` = `balance_cents` of the EARLIEST `balance_log` row for that card whose
`logged_on` falls in month M (any source). If none exists in M, use the live `balance_cents`.
Implement as one function `plan_balance(conn, card, month)` used by every path that today
uses `_month_start_cards`. Unit test (red before green): synced card, first sync of the month
100000, later sync 60000 after a 40000 payment logged -> the month plan still uses 100000,
shows paid 40000, and remaining is computed against the original recommendation.

### Routes (all behind basic auth)

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | /api/plaid/status | | `{"configured": bool, "env": "production", "items": [ItemOut]}` |
| POST | /api/plaid/link-token | `{"item_id": int}` optional (update mode) | `{"link_token": str, "expiration": str}` |
| POST | /api/plaid/exchange | `{"public_token": str, "institution_name": str or null}` | `ItemOut` (runs sync_item for the new item so accounts appear) |
| POST | /api/plaid/sync | | sync result above |
| GET | /api/plaid/accounts | | `[AccountOut]` |
| POST | /api/plaid/accounts/{id}/link | `{"card_id": int}` or `{"create": true}` | `AccountOut` (links, sets card sync='plaid', applies last_payload mapping now; create uses official_name or name, falls back to 0 APR/0 min if Plaid lacks them) |
| POST | /api/plaid/accounts/{id}/unlink | | `AccountOut` (card back to sync='manual', keeps last values) |
| POST | /api/plaid/accounts/{id}/ignore | `{"ignored": bool}` | `AccountOut` |

Link token create body: `client_name "Paydown"`, `language "en"`, `country_codes ["US"]`,
`user.client_user_id "paydown-user"`, `redirect_uri` only if configured. New item:
`products ["liabilities"]` + `account_filters {"credit": {"account_subtypes": ["credit card"]}}`.
Update mode (`item_id` given): pass that item's decrypted `access_token`, no `products`, no
filters. After a successful update-mode Link the frontend calls `/api/plaid/sync` (no exchange).
Exchange of an item_id that already exists updates the token instead of inserting.

Exact response shapes (the frontend codes against these literally):
```json
ItemOut:    {"id": 1, "institution_name": "Chase", "status": "ok", "error_code": null,
             "last_synced_at": "2026-10-02T14:05:11Z", "accounts": 2}
AccountOut: {"id": 3, "plaid_item_id": 1, "institution_name": "Chase", "name": "Freedom",
             "official_name": "Chase Freedom Unlimited", "mask": "4321",
             "card_id": 7, "card_name": "Chase Freedom", "ignored": false,
             "last_balance_cents": 420012, "apr_source": "weighted"}
```
Card objects (GET /api/cards, month bundle `cards`) gain `"sync": "manual"|"plaid"`,
`"last_synced_at": str|null`, `"institution_name": str|null`, `"mask": str|null`.

### Security headers

CSP becomes:
`default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self' https://cdn.plaid.com; frame-src https://cdn.plaid.com; connect-src 'self' https://production.plaid.com https://sandbox.plaid.com; frame-ancestors 'none'`.

### CLI for the timer

`sync_cli.py` at repo root: reads env like run.py, opens the DB, runs `sync_all`, prints one
summary line per item (institution, status, accounts_updated; never tokens), exits 0 even
when an item needs re-login, exits 1 only on config/DB failure.

### Tests (mock Plaid with httpx.MockTransport; no network)

map_liability cases (weighted APR with two buckets, purchase fallback, empty aprs keeps
existing, null min keeps existing, negative balance clamps, credit None); exchange stores an
encrypted token (assert the plaintext token appears in neither the DB file bytes, any API
response, nor export); sync updates a linked card and writes balance_log only on change;
ITEM_LOGIN_REQUIRED sets status and other items still sync; payments on a synced card do not
touch the balance; plan_balance snapshot test; 503 when not configured; link-token update mode
sends the access token and no products (inspect the mocked request body); CSP header contents.
Red-before-green per AGENTS.md for plan_balance and the payment rule.

## Frontend (Cards view + Month dialog + one static page)

- Cards view gets a **Bank sync** panel at the top, driven by GET /api/plaid/status and
  GET /api/plaid/accounts:
  - `configured: false` -> panel hidden.
  - "Connect a bank" button: POST link-token, load
    `https://cdn.plaid.com/link/v2/stable/link-initialize.js` once via a created `<script>`
    element (no inline script), `Plaid.create({token, onSuccess, onExit}).open()`.
    Before opening, store the link token in `sessionStorage` (`paydown.linkToken`) and the mode
    (`new` or `update:<item_id>`) for the OAuth page. onSuccess(public_token, metadata) ->
    POST exchange with `metadata.institution && metadata.institution.name`, then refresh.
    onExit with an error -> show `error.display_message || error.error_message` inline.
  - Each item: institution, status pill (Connected / Needs re-login / Error <code>), last
    synced in relative words ("2 hours ago"), **Reconnect** when status is login_required
    (update-mode link-token with item_id; onSuccess -> POST /api/plaid/sync), and one
    **Sync now** button for all items (POST /api/plaid/sync, then refresh; show per-item results).
  - Unlinked, non-ignored accounts: "<name> ••<mask> · <balance>" with a select
    [Create new card | each manual card by name] + Link button, and an Ignore button.
    Ignored accounts are collapsed under "Ignored (n)" with Unignore.
- Card items: a "Synced · <institution> ••<mask> · <relative time>" line for sync='plaid'.
  The edit dialog disables balance, APR, minimum and credit-limit fields for synced cards with
  the hint "Updated from your bank daily", and offers an "Unlink from bank" button (find the
  plaid account by card_id from /api/plaid/accounts). "Update balance" is hidden for synced cards.
- Month view: Log payment dialog for a synced card shows the hint
  "Your bank balance updates when the payment posts." and the delete confirmation for a synced
  card's payment says the balance won't change.
- `app/static/plaid-oauth.html` + `app/static/plaid-oauth.js`: re-initialize Link with
  `{token: sessionStorage['paydown.linkToken'], receivedRedirectUri: window.location.href}`;
  onSuccess performs the same exchange (mode new) or sync (mode update), then
  `location.href = "/#/cards"`. No inline scripts, no inline styles.
- Escape every Plaid-provided string (institution, account names, masks) with `esc()`.
- Use the existing `api.js` wrapper, dialog helpers and CSS conventions.

## Ownership

- Backend coder: `app/*.py`, `run.py`, `sync_cli.py`, `tests/**`. Not `app/static/**`.
- Frontend coder: `app/static/**` only. Build against the exact shapes above.
- Orchestrator: systemd timer, env, browser verification of CSP + Link loading, live link-token check.
