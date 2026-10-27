# Coder rules for this repo

Read `docs/PLAN.md` fully before writing code. It is the source of truth for the data model,
API contract, engine algorithm, and UI. If the plan is ambiguous, pick the simplest reading and
list the decision in your final report; do not invent new features.

## Hard rules

- Do not commit, branch, stash, or push. The orchestrator commits.
- Only edit files your task owns (see Tasks in PLAN.md). If you need a change elsewhere, report it.
- No secrets in files. Never read `~/.config/budget.env` or any `.env`.
- Do not touch systemd units, cloudflared, or anything outside this repo.
- Test servers: use port 8486-8489 only, bind 127.0.0.1, and kill them before you finish.
- Dependencies: only those allowlisted in PLAN.md. Frontend: no CDN, no npm, no build step.
- Python env: `uv sync` / `uv run ...` with Python 3.12 (`uv python pin 3.12` is already set in pyproject).
- Do not delete, skip, weaken, or narrow tests to make them pass.

## Engine tests you must include (hand-check the numbers in comments)

1. Single card $1,000.00 at 12% APR, min $100, budget $100: month 1 interest is $10.00,
   payment $100, end balance $910.00. Payoff in 11 months; assert total interest equals the
   sum of the schedule's interest and that `total_paid - total_interest == 100000`.
2. Zero APR card $500, min $50, budget $50: exactly 10 months, zero interest.
3. Rollover: two cards, A $200 0% min $100, B $1000 0% min $50, budget $250, snowball:
   month 1 pays A 200 (100 min + 100 extra), B 50; month 2 A is gone and B receives all 250.
4. Avalanche vs snowball pick different first targets for cards (small balance/low APR) vs
   (large balance/high APR), and avalanche total interest <= snowball total interest.
5. Minimum below interest: $10,000 at 30% APR with $100 min -> `minimums_only` marks it `never`.
6. Underfunded budget sets `underfunded = true`.
7. Lump sum reduces months and interest vs no lump sum.
8. Rounding: interest uses round-half-up on cents (e.g. balance 12345 cents at 10% -> 102.875 -> 103).

For load-bearing logic, deliberately break it (e.g. disable rollover) and confirm the specific
test catches it. Compile/import errors are not evidence. Report the test names that failed
during that check.

## Final report must include

- Files created/changed.
- Exact verification commands you ran and their real output tail (pytest summary line, curl codes).
- Any deviations from PLAN.md and why.
