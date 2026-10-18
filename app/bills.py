"""Bill due dates and auto-matching of bill occurrences to bank outflows.

Pure module: no DB, no I/O, no clock calls.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta


def _as_date(v: date | str) -> date:
    if isinstance(v, date):
        return v
    return date.fromisoformat(v)


def due_dates(bill: dict, start: date, end: date) -> list[date]:
    """Monthly due dates of a bill in [start, end], due_day clamped per month
    (31 -> last day). No weekend shift. Empty if due_day is None."""
    day = bill.get("due_day")
    if day is None:
        return []
    start = _as_date(start)
    end = _as_date(end)
    out: list[date] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        d = date(y, m, min(day, calendar.monthrange(y, m)[1]))
        if start <= d <= end:
            out.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def match_bills(
    bills: list[dict],
    txns: list[dict],
    paid: set[tuple[int, str]],
    used_txn_ids: set[str],
    today: date,
    lookback_days: int = 45,
) -> list[dict]:
    """Auto-match bill occurrences to bank outflows.

    bills: active kind='bill' expenses {id, amount_cents, due_day, match_text}.
    Only bills with non-empty match_text and a due_day are considered.
    Occurrences: due_dates(bill, today - lookback_days, today + 10).
    A txn matches when amount_cents > 0 (outflow), match_text is a
    case-insensitive substring of merchant_name or name,
    |txn.date - due| <= 7, txn id not in used_txn_ids, and
    (bill.id, due) not in paid. Pending txns allowed. Each txn used once,
    each occurrence once; prefer the txn closest to the due date
    (ties -> lower amount difference vs bill.amount_cents, then transaction_id).
    txns may include card-account outflows (bills paid by card).
    """
    today = _as_date(today)
    start = today - timedelta(days=lookback_days)
    end = today + timedelta(days=10)

    occurrences: list[tuple[date, int, dict, str]] = []
    for bill in bills:
        match_text = (bill.get("match_text") or "").strip().lower()
        if not match_text or bill.get("due_day") is None:
            continue
        for due in due_dates(bill, start, end):
            if (bill["id"], due.isoformat()) in paid:
                continue
            occurrences.append((due, bill["id"], bill, match_text))
    occurrences.sort(key=lambda o: (o[0], o[1]))

    prepared = [
        {
            "id": t["transaction_id"],
            "date": _as_date(t["date"]),
            "amount_cents": t["amount_cents"],
            "merchant": (t.get("merchant_name") or "").lower(),
            "name": (t.get("name") or "").lower(),
        }
        for t in txns
    ]
    used = set(used_txn_ids)
    results: list[dict] = []
    for due, expense_id, bill, match_text in occurrences:
        best = None
        best_key = None
        for t in prepared:
            if t["id"] in used or t["amount_cents"] <= 0:
                continue
            if match_text not in t["merchant"] and match_text not in t["name"]:
                continue
            delta = abs((t["date"] - due).days)
            if delta > 7:
                continue
            key = (delta, abs(t["amount_cents"] - bill["amount_cents"]), t["id"])
            if best_key is None or key < best_key:
                best_key = key
                best = t
        if best is not None:
            used.add(best["id"])
            results.append(
                {
                    "expense_id": expense_id,
                    "due_date": due.isoformat(),
                    "paid_on": best["date"].isoformat(),
                    "transaction_id": best["id"],
                }
            )
    return results