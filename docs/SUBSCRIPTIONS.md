# Subscriptions

Tracks recurring charges (streaming, hosting, software) that can be weekly, monthly,
quarterly or yearly, which the monthly-only `expenses` table cannot express.

## Plan semantics

- Every `active` subscription feeds the monthly plan at its **monthly-equivalent** cost:
  `amount * charges_per_year / 12`, rounded half-up to the cent (weekly = 52/yr,
  monthly 12, quarterly 4, yearly 1). A yearly charge therefore accrues 1/12 per month.
- `compute_budget(..., subscriptions_monthly_cents)` subtracts it like an essential. It is
  a separate waterfall line (`subscriptions_total_cents`), included in
  `available_after_min`, the recurring debt budget, and `essentials_cents`
  (so cash runway sees it). `bills_total_cents` is untouched.
- Cancelled subscriptions cost nothing. Reactivating restores them.
- **No double counting:** a bill that is really a subscription is converted with
  `POST /api/subscriptions/from-expense/{id}`, which creates the subscription and
  sets the bill `active = 0` in one transaction. Only `kind = 'bill'` converts.

## Not wired (deliberate, v1 is lean)

Subscription renewals do **not** appear in `/api/upcoming` or Safe-to-Spend. A converted
bill therefore stops reserving its due date in Safe-to-Spend. The UI says so before converting.

## Data

`subscriptions(id, name, amount_cents, cycle, next_date, match_text, note, status,
cancelled_on, source, created_at)`; `source` is `manual | converted`.
`next_date` is any known charge date; the API rolls it forward (`next_renewal`).
Month-based cycles keep the anchor's day of month, clamped (Jan 31 -> Feb 28 -> Mar 31).
`subscription_dismissals(id, key UNIQUE, dismissed_at)` remembers dismissed suggestions.
Both tables are in `db.TABLES`, so export/import carries them.

## API

| Route | Notes |
|---|---|
| `GET /api/subscriptions` | `{active (by next renewal), cancelled, totals}`; rows add `monthly_cents`, `annual_cents`, `next_renewal`, `days_until` |
| `POST /api/subscriptions`, `PUT /{id}`, `DELETE /{id}` | body: name, amount_cents, cycle, next_date, match_text?, note? |
| `POST /{id}/cancel` `{cancelled_on?}`, `POST /{id}/reactivate` | |
| `GET /suggestions` | detected recurring charges not already tracked or dismissed |
| `POST /suggestions/dismiss` `{key}` | idempotent, 204 |
| `POST /from-expense/{expense_id}` | 201; 409 for spending or inactive bills |

## Detection (`subscriptions.detect_recurring`, pure)

Input: synced `transactions` (outflows, not pending, account not ignored, last ~800 days),
excluding Plaid categories TRANSFER_IN/OUT, LOAN_PAYMENTS, INCOME, BANK_FEES.
Merchants group by a normalised key (lowercase, tokens with digits dropped, first 3 words).
A merchant qualifies when the median gap fits a cycle window (weekly 6-8d, monthly 25-35,
quarterly 82-100, yearly 350-380), at least 75% of gaps fit, enough charges exist
(4 weekly, 3 monthly/quarterly, 2 yearly), at least 75% of amounts are within 20% of the
median, and the last charge is not older than the lapse limit (11 / 48 / 146 / 584 days).
Suggested amount is the latest charge. Suggestions that match an active bill's
`match_text` carry `bill_id` and the UI offers "Convert bill" instead of "Add".
A merchant matching a cancelled subscription stays hidden until it charges again after
`cancelled_on`. Expect false positives (a weekly pub tab); dismiss them. Yearly charges
need 13+ months of synced history before they can be detected.

## UI

`#/subscriptions` view (`views/subscriptions.js`); "make subscription" link on Budget bills;
Subscriptions line in the Budget waterfall; month tile is Bills+Spending+Subs.
