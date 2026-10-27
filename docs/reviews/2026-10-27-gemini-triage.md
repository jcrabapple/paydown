# Web/server review triage — Gemini 3.8 Flash high, 2026-10-27

Reviewed snapshot: `711fdad`. 10 findings. Each checked against source; every
accepted fix has a regression test shown red on the pre-fix code
(`tests/test_review_2026_10_27.py`) or a browser red/green pass.

## Accepted
| # | Finding | Fix |
|---|---|---|
| 1.2 | Deleting an over-payment on a manual card re-added the full amount (balance floors at 0 on apply) → phantom debt | `payments.applied_cents` (migration) records what was actually applied; revert uses it; NULL legacy rows revert the full amount as before |
| 2.1 | `DELETE /api/plaid/items/{id}` left linked cards/cash at `sync='plaid'`: payments stopped reducing card balances, cash balances 409'd forever | demote linked cards/cash to manual inside the same transaction before the cascade |
| 2.2 | Import wiped `transactions.rule_id` (FK SET NULL when deposit_rules are replaced) | captured and restored like `pay_schedule_id` |
| 2.3 | Pending→posted carried label but not `rule_id` | carried |
| 5.1 | settings.js import errors called undefined `showErr` | `errorBanner` |
| 5.2 | Custom-order arrows were a no-op when priorities were equal (every card starts at 0, so this hit any real install) | renumber the list 0..n-1 in the new order; browser-verified |

## Accepted-narrowed
| # | Finding | Decision |
|---|---|---|
| 4.1 | `delete_payment` condition `not card or …` would crash on a missing card | unreachable (`PRAGMA foreign_keys=ON`, payments cascade with their card) but the condition is backwards; flipped to `card and …` |

## Rejected
| # | Reason |
|---|---|
| 1.1 | "Underfunded month zeroes a card's minimum, so STS/reminders/upcoming drop it." `compute_budget` sets `debt_budget = minimums_total + extra` from the same effective-minimum cards the engine uses, so the plan always funds every minimum; with $0 income it still recommends the full minimum and reports the shortfall separately. Kept as an invariant guard test (`test_zero_income_month_still_funds_and_reserves_card_minimum`). |

## Found during verification (not in the review)
- **budget.js called `showError` at 7 sites but never defined it.** Every Budget-page error (pay-schedule/goal delete, subscription actions…) threw a ReferenceError and the user saw nothing. Browser red/green: old code → `showError is not defined`, 0 banners; fixed → banner. Found by a mechanical undefined-call sweep over app/static.
- **PWA metadata routes behind auth** (regressed by `4d1d408`, the icon-traversal fix narrowed the public list to icons only). The background service-worker update check fetches `/sw.js` without credentials, so installed PWAs stopped receiving new versions on 2026-10-18. Restored as an exact-path allowlist (`/sw.js`, `/manifest.webmanifest`, `/offline`, `/api/health`).
- **Test isolation:** the auth lockout counter is process-global and nothing reset it between tests; added an autouse reset in conftest.
- **`/api/mobile/home` settings** returned the raw sqlite `1` for `auto_log_card_payments` where `/api/settings` returns `true` (API1 promises identical semantics). The existing equality test missed it because Python's `1 == True`.
