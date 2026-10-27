"""Insights: debt trend, spending baseline, reminders (FIX5 Round 3, Lane I).

PURE module: no I/O, no clock calls, no app.db imports. Today and all dates
come in as arguments; transactions are plain dict rows shaped like the Plaid
sync (Plaid sign: amount_cents > 0 = money out of the account / charge on the
card, amount_cents < 0 = money back).
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from app.engine import round_half_up_cents

REMINDER_DAYS_MIN = 0
REMINDER_DAYS_MAX = 14

# Fixed labels for the Plaid primaries we care about; anything else is
# title-cased with underscores turned into spaces.
CATEGORY_LABELS = {
    "FOOD_AND_DRINK": "Food & drink",
    "GENERAL_MERCHANDISE": "Shopping",
    "GENERAL_SERVICES": "Services",
    "TRANSPORTATION": "Transportation",
    "ENTERTAINMENT": "Entertainment",
    "MEDICAL": "Medical",
    "PERSONAL_CARE": "Personal care",
    "HOME_IMPROVEMENT": "Home improvement",
    "RENT_AND_UTILITIES": "Rent & utilities",
    "TRAVEL": "Travel",
    "GOVERNMENT_AND_NON_PROFIT": "Government & donations",
}

BILL_NOTE = "Likely already in your bills"


def _as_date(v) -> date:
    if isinstance(v, date):
        return v
    return date.fromisoformat(v)


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _last_months(today: date, n: int) -> list[str]:
    """The last n calendar months including the current one, oldest first."""
    t = today.year * 12 + today.month - 1
    out = []
    for i in range(n - 1, -1, -1):
        k = t - i
        out.append(f"{k // 12:04d}-{k % 12 + 1:02d}")
    return out


def _contains(haystack: str | None, needle: str) -> bool:
    return bool(haystack) and needle in haystack.upper()


def _is_interest(txn: dict) -> bool:
    return (
        txn["amount_cents"] > 0
        and (
            txn.get("pfc_detailed") == "BANK_FEES_INTEREST_CHARGE"
            or _contains(txn.get("name"), "INTEREST CHARGE")
        )
    )


def _is_late_fee(txn: dict) -> bool:
    return txn.get("pfc_detailed") == "BANK_FEES_LATE_FEES" or _contains(
        txn.get("name"), "LATE FEE"
    )


def _classify(txn: dict) -> str:
    """One of: interest | fees | charges | payments | refunds."""
    amt = txn["amount_cents"]
    if amt > 0:
        if _is_interest(txn):
            return "interest"
        if txn.get("pfc_primary") == "BANK_FEES":
            return "fees"
        return "charges"
    # amount < 0: money back into the account
    if txn.get("pfc_primary") in ("LOAN_PAYMENTS", "TRANSFER_IN") or txn.get(
        "pfc_primary"
    ) is None:
        return "payments"
    return "refunds"


def debt_trend(
    txns: list[dict], cards: list[dict], today: date, months: int = 6
) -> dict:
    """Monthly card-debt reality check over linked CREDIT accounts.

    txns: linked, non-ignored, non-pending credit rows (see module docstring).
    cards: open linked cards [{"id", "name"}].
    """
    today = _as_date(today)
    month_keys = _last_months(today, months)
    buckets: dict[str, dict[str, int]] = {
        mk: {"charges": 0, "payments": 0, "refunds": 0, "interest": 0, "fees": 0}
        for mk in month_keys
    }
    coverage_start: str | None = None
    for t in txns:
        if coverage_start is None or t["date"] < coverage_start:
            coverage_start = t["date"]
        mk = t["date"][:7]
        if mk in buckets:
            buckets[mk][_classify(t)] += abs(t["amount_cents"])

    start30 = today - timedelta(days=30)
    start90 = today - timedelta(days=90)
    interest_30 = 0
    for t in txns:
        d = date.fromisoformat(t["date"])
        if not (start30 <= d <= today):
            continue
        if _classify(t) == "interest":
            interest_30 += t["amount_cents"]

    out_months = []
    for mk in month_keys:
        b = buckets[mk]
        partial = mk == today.isoformat()[:7]
        if coverage_start is not None and mk == coverage_start[:7]:
            partial = partial or date.fromisoformat(coverage_start).day != 1
        out_months.append(
            {
                "month": mk,
                "partial": partial,
                "charges_cents": b["charges"],
                "payments_cents": b["payments"],
                "refunds_cents": b["refunds"],
                "interest_cents": b["interest"],
                "fees_cents": b["fees"],
                # net_change = charges + interest + fees - payments - refunds
                "net_change_cents": (
                    b["charges"] + b["interest"] + b["fees"]
                    - b["payments"] - b["refunds"]
                ),
            }
        )

    return {
        "coverage_start": coverage_start,
        "months": out_months,
        "interest_last_30_cents": interest_30,
        "by_card": [
            {
                "card_id": c["id"],
                "name": c["name"],
                "interest_last_30_cents": _card_sum(txns, c["id"], "interest", start30, today),
                "fees_last_90_cents": _card_fee_sum(txns, c["id"], start90, today, late=False),
                "late_fees_last_90_cents": _card_fee_sum(txns, c["id"], start90, today, late=True),
            }
            for c in cards
        ],
    }


def _card_sum(txns, card_id, cls, start, end) -> int:
    return sum(
        t["amount_cents"]
        for t in txns
        if t.get("card_id") == card_id
        and start <= date.fromisoformat(t["date"]) <= end
        and _classify(t) == cls
    )


def _card_fee_sum(txns, card_id, start, end, late: bool) -> int:
    """All bank fees in the window; with late=True, only the late ones."""
    return sum(
        t["amount_cents"]
        for t in txns
        if t.get("card_id") == card_id
        and start <= date.fromisoformat(t["date"]) <= end
        and t["amount_cents"] > 0
        and _classify(t) == "fees"
        and (not late or _is_late_fee(t))
    )


def _category_label(primary: str | None) -> str:
    if primary and primary in CATEGORY_LABELS:
        return CATEGORY_LABELS[primary]
    return (primary or "Other").replace("_", " ").title()


def _is_excluded_by_match(txn: dict, match_texts: list[str]) -> bool:
    name = (txn.get("name") or "")
    merchant = (txn.get("merchant_name") or "")
    for mt in match_texts:
        up = mt.upper()
        if up and (up in name.upper() or up in merchant.upper()):
            return True
    return False


def spending_baseline(
    txns: list[dict],
    excluded_ids: set[str],
    match_texts: list[str],
    budget_spending_cents: int,
    today: date,
    months: int = 3,
) -> dict:
    """Average monthly everyday spending by Plaid primary category.

    Averages over the last `months` COMPLETE calendar months that the
    transaction history fully covers (never the current month).
    """
    today = _as_date(today)
    if not txns:
        return {
            "months_used": [],
            "categories": [],
            "total_monthly_avg_cents": 0,
            "budget_spending_cents": int(budget_spending_cents),
            "gap_cents": max(0, -int(budget_spending_cents)),
        }

    coverage_start = min(t["date"] for t in txns)
    cov_d = date.fromisoformat(coverage_start)
    cov_month_full = cov_d.day == 1

    # Candidates: the last `months` complete months (ending with last month).
    candidates = _last_months(today, months + 1)[:-1][-months:]
    months_used = [
        mk
        for mk in candidates
        if mk > cov_d.isoformat()[:7] or (mk == cov_d.isoformat()[:7] and cov_month_full)
    ]

    sums: dict[str, int] = {}
    for t in txns:
        if t["date"][:7] not in months_used:
            continue
        if t["transaction_id"] in excluded_ids:
            continue
        if _is_excluded_by_match(t, match_texts):
            continue
        if t["amount_cents"] <= 0:
            continue
        acct = t.get("account_type") or "credit"
        primary = t.get("pfc_primary")
        if acct == "credit":
            if _is_interest(t) or primary == "BANK_FEES":
                continue
        else:
            if primary in ("TRANSFER_OUT", "LOAN_PAYMENTS", "BANK_FEES"):
                continue
        sums[primary] = sums.get(primary, 0) + t["amount_cents"]

    n = len(months_used)
    categories = []
    total = 0
    for primary, s in sorted(sums.items(), key=lambda kv: (-kv[1], kv[0])):
        avg = round_half_up_cents(s / n) if n else 0
        total += avg
        suggest = primary != "RENT_AND_UTILITIES"
        categories.append(
            {
                "category": primary,
                "label": _category_label(primary),
                "monthly_avg_cents": avg,
                "suggest": suggest,
                "note": None if suggest else BILL_NOTE,
            }
        )

    return {
        "months_used": months_used,
        "categories": categories,
        "total_monthly_avg_cents": total,
        "budget_spending_cents": int(budget_spending_cents),
        "gap_cents": max(0, total - int(budget_spending_cents)),
    }


def _bill_due_dates(bill: dict, start: date, end: date) -> list[date]:
    day = bill.get("due_day")
    if day is None:
        return []
    out: list[date] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        d = date(y, m, min(day, calendar.monthrange(y, m)[1]))
        if start <= d <= end:
            out.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def reminders(
    plan_rows: list[dict],
    cards: list[dict],
    bills: list[dict],
    paid_keys: set,
    late_fee_counts: dict[int, int],
    today: date,
    days: int = 3,
) -> dict:
    """Upcoming payment reminders: due in [today, today+days] plus overdue.

    Cards follow the Safe-to-Spend S2 rules (synced cards use the bank's
    next_due_date + bank_overdue; manual cards use due_day). Bills follow the
    bill due-date rules (monthly, due_day clamped to the month end).
    """
    today = _as_date(today)
    days = max(REMINDER_DAYS_MIN, min(REMINDER_DAYS_MAX, int(days)))
    horizon = today + timedelta(days=days)
    open_ids = {c["id"] for c in cards if not c.get("closed")}

    items: list[dict] = []

    def add(kind, id_, name, d, amount, overdue, note):
        items.append(
            {
                "kind": kind,
                "id": id_,
                "name": name,
                "date": d.isoformat(),
                "days_until": (d - today).days,
                "amount_cents": amount,
                "overdue": overdue,
                "note": note,
            }
        )

    names = {c["id"]: c["name"] for c in cards}
    seen: set[int] = set()
    for row in plan_rows:
        cid = row["card_id"]
        if cid in open_ids and cid not in seen:
            seen.add(cid)
        else:
            continue
        min_rem = row.get("min_remaining_cents")
        min_rem = 0 if min_rem is None else max(0, int(min_rem))
        if min_rem == 0:
            continue
        sync = row.get("sync") or "manual"
        next_due = row.get("next_due_date")
        note = None
        n_late = int(late_fee_counts.get(cid, 0) or 0) if isinstance(cid, int) else 0
        if n_late > 0:
            note = f"late fee{'s' if n_late != 1 else ''} charged {n_late} time{'s' if n_late != 1 else ''} in the last 90 days"
        if sync == "plaid" and next_due:
            d = _as_date(next_due)
            overdue = bool(row.get("bank_overdue"))
            # Same rule as STS S2: a past next_due_date with bank_overdue
            # false means the bank considers it handled - skip it.
            if not (overdue or today <= d <= horizon):
                continue
            add("card", cid, row.get("name") or names.get(cid), d, min_rem, overdue, note)
            continue
        # Manual cards (and synced cards without a bank due date): due_day in
        # the current and next month.
        due_day = row.get("due_day")
        if due_day is None:
            continue
        nxt = (today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1)
        for (y, m) in [(today.year, today.month), nxt]:
            d = date(y, m, min(due_day, calendar.monthrange(y, m)[1]))
            if today <= d <= horizon:
                add("card", cid, row.get("name") or names.get(cid), d, min_rem, False, note)
            elif d < today and (y, m) == (today.year, today.month):
                add("card", cid, row.get("name") or names.get(cid), d, min_rem, True, note)

    # Bills: due dates in [today, horizon], cash- or card-paid, not yet marked
    # paid for that due date. Past bill dates are NOT reported as overdue: most
    # bills are never marked paid (no match_text), so "overdue" would be noise
    # that repeats every day (orchestrator decision, FIX5 R3).
    for b in bills:
        if b.get("paid_from") not in ("cash", "card", None) or b.get("due_day") is None:
            continue
        for d in _bill_due_dates(b, today, horizon):
            if (b["id"], d.isoformat()) in paid_keys:
                continue
            add("bill", b["id"], b["name"], d, b["amount_cents"], False, None)

    items.sort(key=lambda it: (it["date"], it["kind"]))
    return {"today": today.isoformat(), "items": items}
