"""Subscription math and recurring-charge detection.

Pure module: no DB, no I/O, no clock calls (callers pass `today`).

Plan semantics (docs/SUBSCRIPTIONS.md): every active subscription feeds the
monthly plan at its monthly-equivalent cost, so a yearly charge accrues 1/12
per month and a weekly one 52/12.
"""
from __future__ import annotations

import calendar
import re
import statistics
from datetime import date, timedelta
from collections.abc import Collection, Sequence
from fractions import Fraction

from .engine import round_half_up_cents

CYCLES = ("weekly", "monthly", "quarterly", "yearly")
_PER_YEAR = {"weekly": 52, "monthly": 12, "quarterly": 4, "yearly": 1}
_MONTH_STEP = {"monthly": 1, "quarterly": 3, "yearly": 12}


def _as_date(v: date | str) -> date:
    return v if isinstance(v, date) else date.fromisoformat(v)


def annual_cents(amount_cents: int, cycle: str) -> int:
    """What one year of charges costs."""
    return amount_cents * _PER_YEAR[cycle]


def monthly_cents(amount_cents: int, cycle: str) -> int:
    """Monthly-equivalent cost in whole cents, halves rounding up."""
    return round_half_up_cents(Fraction(annual_cents(amount_cents, cycle), 12))


def advance(anchor: date | str, cycle: str, n: int) -> date:
    """The anchor date moved forward by n billing cycles. Month-based cycles
    keep the anchor's day of month, clamped (31st -> Feb 28th, then back)."""
    anchor = _as_date(anchor)
    if cycle == "weekly":
        return anchor + timedelta(days=7 * n)
    months = anchor.year * 12 + anchor.month - 1 + _MONTH_STEP[cycle] * n
    y, m = divmod(months, 12)
    m += 1
    return date(y, m, min(anchor.day, calendar.monthrange(y, m)[1]))


def next_renewal(anchor: date | str, cycle: str, today: date | str) -> date:
    """First charge date on or after today, walking the cycle from anchor.
    An anchor in the future is itself the next renewal."""
    anchor = _as_date(anchor)
    today = _as_date(today)
    if anchor >= today:
        return anchor
    if cycle == "weekly":
        n = -(-(today - anchor).days // 7)
    else:
        diff = (today.year - anchor.year) * 12 + today.month - anchor.month
        n = max(0, diff // _MONTH_STEP[cycle] - 1)
    while advance(anchor, cycle, n) < today:
        n += 1
    return advance(anchor, cycle, n)


# --- detection ---------------------------------------------------------------

# cycle -> (lo, hi) acceptable interval in days, minimum charges seen,
# and the longest silence after which the subscription counts as lapsed.
_WINDOWS = {
    "weekly": (6, 8, 4, 11),
    "monthly": (25, 35, 3, 48),
    "quarterly": (82, 100, 3, 146),
    "yearly": (350, 380, 2, 584),
}
_SKIP_PFC = {"TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS", "INCOME", "BANK_FEES"}


def merchant_key(t: dict) -> str:
    """Normalised merchant: lowercase words, no store numbers or reference
    codes (any token containing a digit), at most the first three words."""
    raw = (t.get("merchant_name") or t.get("name") or "").lower()
    tokens = [w.strip(".") for w in re.sub(r"[^a-z0-9. ]", " ", raw).split()]
    words = [w for w in tokens if w and not any(c.isdigit() for c in w)]
    return " ".join(words[:3])


def _overlaps(a: str, b: str) -> bool:
    a, b = a.strip().lower(), b.strip().lower()
    return len(a) >= 3 and len(b) >= 3 and (a in b or b in a)


def detect_recurring(
    txns: list[dict],
    today: date | str,
    *,
    known_texts: Sequence[str] = (),
    dismissed: Collection[str] = (),
    bills: Sequence[dict] = (),
    cancelled: Sequence[dict] = (),
) -> list[dict]:
    """Suggest subscriptions from bank transactions.

    txns: {transaction_id, date, amount_cents (>0 = outflow), name,
    merchant_name, pending, pfc_primary}. A merchant qualifies when its
    charges land on a steady cadence (weekly/monthly/quarterly/yearly) with
    a stable amount and the latest charge is recent enough that it has not
    lapsed. Merchants matching `known_texts` (existing subscriptions) or a
    dismissed key are dropped; ones matching an active bill in `bills`
    ({id, name, match_text}) are kept and carry that bill's id. A merchant
    matching a cancelled subscription ({text, cancelled_on}) stays hidden
    until it charges again AFTER the cancel date (still being billed).
    """
    today = _as_date(today)
    groups: dict[str, dict[date, dict]] = {}
    for t in txns:
        if t["amount_cents"] <= 0 or t.get("pending"):
            continue
        if (t.get("pfc_primary") or "") in _SKIP_PFC:
            continue
        key = merchant_key(t)
        if not key:
            continue
        # one charge per day per merchant: a duplicate same-day row is noise
        groups.setdefault(key, {})[_as_date(t["date"])] = t

    out: list[dict] = []
    for key, by_day in groups.items():
        if key in dismissed or any(_overlaps(key, k) for k in known_texts):
            continue
        days = sorted(by_day)
        if len(days) < 2:
            continue
        gaps = [(b - a).days for a, b in zip(days, days[1:])]
        med = statistics.median(gaps)
        cycle = next(
            (c for c, (lo, hi, _, _) in _WINDOWS.items() if lo <= med <= hi), None
        )
        if cycle is None:
            continue
        lo, hi, min_charges, max_silence = _WINDOWS[cycle]
        if len(days) < min_charges:
            continue
        if sum(1 for g in gaps if lo <= g <= hi) / len(gaps) < 0.75:
            continue
        amounts = [by_day[d]["amount_cents"] for d in days]
        mid = statistics.median(amounts)
        if sum(1 for a in amounts if abs(a - mid) <= 0.20 * mid) / len(amounts) < 0.75:
            continue
        last = days[-1]
        if (today - last).days > max_silence:
            continue
        if any(
            _overlaps(key, c["text"]) and last <= _as_date(c["cancelled_on"])
            for c in cancelled
            if c.get("cancelled_on")
        ):
            continue
        latest = by_day[last]
        match_bill = next(
            (b for b in bills if (b.get("match_text") or "").strip()
             and _overlaps(key, b["match_text"])),
            None,
        )
        out.append(
            {
                "key": key,
                "name": (latest.get("merchant_name") or latest.get("name") or key).strip(),
                "amount_cents": latest["amount_cents"],
                "cycle": cycle,
                "next_date": next_renewal(last, cycle, today).isoformat(),
                "match_text": key,
                "last_date": last.isoformat(),
                "occurrences": len(days),
                "bill_id": match_bill["id"] if match_bill else None,
                "bill_name": match_bill["name"] if match_bill else None,
            }
        )
    out.sort(key=lambda s: (s["next_date"], s["key"]))
    return out
