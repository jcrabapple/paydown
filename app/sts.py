"""Safe-to-Spend: balance minus pending, minus obligations before next payday.

Pure module: no DB, no I/O, no clock calls (today/now come in as arguments).
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone

from app.engine import round_half_up_cents
from app.bills import due_dates

STALE_BALANCE_DAYS = 3


def _as_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(v)


def _iso(v: datetime | None) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mon_d(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def _dollars(cents: int) -> str:
    return f"${abs(cents) / 100:,.2f}"


def _month_list(today: date, horizon: date) -> list[tuple[int, int]]:
    """(year, month) from today's month through the horizon's month (inclusive);
    at least today's month even when the horizon is before today (late payday)."""
    end = horizon if horizon >= today else today
    out = []
    y, m = today.year, today.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def safe_to_spend(
    *,
    today,
    now,  # datetime
    payday_info: dict,
    has_schedules: bool,
    cash_total_cents: int,
    pending_out_cents: int,
    cash_as_of,  # datetime | None
    cash_floor_cents: int,
    goals_set_aside_cents: int,
    bills: list[dict],
    bill_payments: set,  # set[tuple[int, str]]
    card_rows: dict,  # 'YYYY-MM' -> rows
    spending_monthly_cents: int,
    extra_warnings: list[dict],
) -> dict:
    today = _as_date(today)
    payday = payday_info.get("payday")
    warnings: list[dict] = []

    if payday:
        horizon = _as_date(payday["date"])
    else:
        horizon = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
        warnings.append(
            {
                "code": "no_pay_schedule" if not has_schedules else "no_upcoming_payday",
                "message": (
                    "Add your pay schedule for a more accurate number"
                    if not has_schedules
                    else "No upcoming payday in your schedule"
                ),
            }
        )

    last_payday = payday_info.get("last_payday")
    period_start = _as_date(last_payday) if last_payday else today

    days_until = max(0, (horizon - today).days)
    window_days = max(1, days_until)
    dim = calendar.monthrange(today.year, today.month)[1]

    obligations: list[dict] = []

    # Bill window. Normally [period_start, horizon]. On a late payday (horizon < today)
    # the next payday is unknown until the paycheck lands, so - like cards - reserve
    # through the end of today's month; otherwise every bill would drop out.
    bill_end = horizon
    if horizon < today:
        bill_end = date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])

    # Cash bills (paid from checking), every due date in [period_start, bill_end].
    for b in bills:
        if b.get("paid_from", "cash") != "cash":
            continue
        if b.get("due_day") is None:
            # Undated cash bill: spread across the window. On a late payday the window
            # is today..bill_end (end of month), same as dated bills (Gate 4 G4-01).
            spread_days = window_days if horizon >= today else (bill_end - today).days + 1
            amt = round_half_up_cents(
                b["amount_cents"] * min(spread_days, dim) / dim
            )
            obligations.append(
                {
                    "kind": "bill",
                    "id": b["id"],
                    "name": b["name"],
                    "date": bill_end.isoformat(),
                    "amount_cents": amt,
                    "status": "spread",
                    "reserved": True,
                    "undated": True,
                }
            )
            continue
        for d in due_dates(b, period_start, bill_end):
            key = (b["id"], d.isoformat())
            if key in bill_payments:
                status, reserved = "paid", False
            elif d < today:
                status, reserved = "overdue", True
            else:
                status, reserved = "due", True
            obligations.append(
                {
                    "kind": "bill",
                    "id": b["id"],
                    "name": b["name"],
                    "date": d.isoformat(),
                    "amount_cents": b["amount_cents"],
                    "status": status,
                    "reserved": reserved,
                    "undated": False,
                }
            )

    # Card-paid bills: due dates in [today, horizon], not reserved.
    for b in bills:
        if b.get("paid_from") != "card" or b.get("due_day") is None:
            continue
        for d in due_dates(b, today, bill_end):
            obligations.append(
                {
                    "kind": "bill",
                    "id": b["id"],
                    "name": b["name"],
                    "date": d.isoformat(),
                    "amount_cents": b["amount_cents"],
                    "status": "on_card",
                    "reserved": False,
                    "undated": False,
                }
            )

    # Card payments, per month from today's month through the horizon's month.
    # S1: a card obligation reserves the MINIMUM still owed (min_remaining_cents),
    # never remaining_cents (min + extra). Extra is planned, not owed: it is
    # reported per obligation as extra_remaining_cents and summed at the top
    # level as planned_extra_cents (informational, NOT subtracted).
    # Rows without min_remaining_cents (legacy callers) reserve everything.
    undated_seen: set[int] = set()
    bank_dated_seen: set[int] = set()
    for (y, m) in _month_list(today, horizon):
        month_key = f"{y:04d}-{m:02d}"
        for row in card_rows.get(month_key, []):
            # R15: a card with no recommendation and no payments is dead weight
            # - listing it as "paid" with amount 0 just adds noise.
            if row.get("recommended_cents", 0) == 0 and row.get("paid_cents", 0) == 0:
                continue
            remaining = row.get("remaining_cents", 0)
            min_rem = row.get("min_remaining_cents")
            min_rem = remaining if min_rem is None else max(0, int(min_rem))
            reserve_amt = min(min_rem, remaining)
            extra_rem = remaining - reserve_amt
            sync = row.get("sync") or "manual"
            next_due = row.get("next_due_date")
            # S2: a synced card with a bank-reported due date uses that date.
            # One obligation per card, using today's-month plan row amounts
            # (months iterate from today's month, so the first sighting wins).
            if sync == "plaid" and next_due:
                if row["card_id"] in bank_dated_seen:
                    continue
                bank_dated_seen.add(row["card_id"])
                d = _as_date(next_due)
                bank_overdue = bool(row.get("bank_overdue"))
                eom = date(
                    today.year, today.month, calendar.monthrange(today.year, today.month)[1]
                )
                # In scope: due between today and the payday; a late payday
                # (horizon < today) keeps reserving through end of today's
                # month, same as the due_day rule (R2). A past next_due_date
                # with bank_overdue false means the bank considers it handled:
                # skip it. bank_overdue true is always in scope, as overdue.
                in_scope = (
                    bank_overdue
                    or today <= d <= horizon
                    or (horizon < today and today <= d <= eom)
                )
                if not in_scope:
                    continue
                if reserve_amt > 0:
                    obligations.append(
                        {
                            "kind": "card",
                            "id": row["card_id"],
                            "name": row["name"],
                            "date": d.isoformat(),
                            "amount_cents": reserve_amt,
                            "status": "overdue" if bank_overdue else "due",
                            "reserved": True,
                            "undated": False,
                            "extra_remaining_cents": extra_rem,
                        }
                    )
                elif remaining > 0:
                    # S1: minimum satisfied; extra may remain but is not owed.
                    obligations.append(
                        {
                            "kind": "card",
                            "id": row["card_id"],
                            "name": row["name"],
                            "date": d.isoformat(),
                            "amount_cents": extra_rem,
                            "status": "covered",
                            "reserved": False,
                            "undated": False,
                            "extra_remaining_cents": extra_rem,
                        }
                    )
                else:
                    obligations.append(
                        {
                            "kind": "card",
                            "id": row["card_id"],
                            "name": row["name"],
                            "date": d.isoformat(),
                            "amount_cents": row.get("recommended_cents", 0),
                            "status": "paid",
                            "reserved": False,
                            "undated": False,
                            "extra_remaining_cents": 0,
                        }
                    )
                continue
            due_day = row.get("due_day")
            d = (
                date(y, m, min(due_day, calendar.monthrange(y, m)[1]))
                if due_day is not None
                else None
            )
            if d is None:
                # R3/S2: an undated card (no due_day) contributes once, from the
                # earliest month whose row still has remaining > 0. A fully
                # paid month must not suppress a later month's balance.
                if remaining <= 0:
                    continue
                if row["card_id"] in undated_seen:
                    continue
                undated_seen.add(row["card_id"])
                obligations.append(
                    {
                        "kind": "card",
                        "id": row["card_id"],
                        "name": row["name"],
                        "date": horizon.isoformat(),
                        "amount_cents": reserve_amt,
                        "status": "due",
                        "reserved": True,
                        "undated": True,
                        "extra_remaining_cents": extra_rem,
                    }
                )
                continue
            # S1: one scope rule for both branches (horizon before/after today).
            # Overdue: due earlier in today's month and unpaid. Upcoming: due from
            # today through the payday. Late payday (horizon < today, at most
            # GRACE_DAYS): the next payday is unknown until the late paycheck lands,
            # so stay conservative and keep reserving the rest of today's month (R2).
            # Gate 3 G3-01: with a normal horizon the current-month clause must not
            # pull in cards due after payday.
            same_month = (y, m) == (today.year, today.month)
            in_scope = (
                (d < today and same_month)
                or (today <= d <= horizon)
                or (horizon < today and d >= today and same_month)
            )
            if not in_scope:
                continue
            if remaining > 0 and reserve_amt > 0:
                status = "overdue" if d < today else "due"
                obligations.append(
                    {
                        "kind": "card",
                        "id": row["card_id"],
                        "name": row["name"],
                        "date": d.isoformat(),
                        "amount_cents": reserve_amt,
                        "status": status,
                        "reserved": True,
                        "undated": False,
                        "extra_remaining_cents": extra_rem,
                    }
                )
            elif remaining > 0:
                # S1: minimum satisfied; extra may remain but is not owed.
                obligations.append(
                    {
                        "kind": "card",
                        "id": row["card_id"],
                        "name": row["name"],
                        "date": d.isoformat(),
                        "amount_cents": extra_rem,
                        "status": "covered",
                        "reserved": False,
                        "undated": False,
                        "extra_remaining_cents": extra_rem,
                    }
                )
            else:
                status = "paid"
                amount = row.get("recommended_cents", 0)
                obligations.append(
                    {
                        "kind": "card",
                        "id": row["card_id"],
                        "name": row["name"],
                        "date": d.isoformat(),
                        "amount_cents": amount,
                        "status": status,
                        "reserved": False,
                        "undated": False,
                        "extra_remaining_cents": 0,
                    }
                )

    obligations.sort(
        key=lambda o: (o["date"], 0 if o["kind"] == "bill" else 1, o["id"])
    )
    obligations_total = sum(o["amount_cents"] for o in obligations if o["reserved"])
    # S1: planned extra across card obligations in the window. Informational
    # only - it is NOT subtracted from safe-to-spend (extra is planned, the
    # debt budget already funds it).
    planned_extra_cents = sum(
        o.get("extra_remaining_cents", 0) for o in obligations if o["kind"] == "card"
    )

    safe = (
        cash_total_cents
        - pending_out_cents
        - obligations_total
        - goals_set_aside_cents
        - cash_floor_cents
    )
    per_day = max(0, safe) // window_days
    planned = round_half_up_cents(
        spending_monthly_cents * min(window_days, dim) / dim
    )

    # Warnings, in a stable order.
    if payday and payday.get("status") == "late":
        pd = _as_date(payday["date"])
        warnings.append(
            {
                "code": "paycheck_late",
                "message": f"{payday['name']} expected {_mon_d(pd)} hasn't arrived yet",
                "date": pd.isoformat(),
            }
        )
    for miss in payday_info.get("missed", []):
        d = _as_date(miss["date"])
        warnings.append(
            {
                "code": "paycheck_missed",
                "message": f"{miss['name']} expected {_mon_d(d)} never arrived",
                "date": d.isoformat(),
            }
        )
    if cash_as_of is None:
        warnings.append(
            {"code": "stale_balance", "message": "Checking balance hasn't synced yet"}
        )
    elif (now - cash_as_of) > timedelta(days=STALE_BALANCE_DAYS):
        warnings.append(
            {
                "code": "stale_balance",
                "message": f"Checking balance is {(now - cash_as_of).days} days old",
            }
        )
    if safe < 0:
        warnings.append(
            {"code": "short", "message": f"Short by {_dollars(safe)}"}
        )
    warnings.extend(extra_warnings)

    out_payday = None
    if payday:
        out_payday = dict(payday)
        out_payday["date"] = _as_date(payday["date"]).isoformat()

    return {
        "today": today.isoformat(),
        "horizon": horizon.isoformat(),
        "days_until": days_until,
        "payday": out_payday,
        "period_start": period_start.isoformat(),
        "cash_total_cents": cash_total_cents,
        "pending_out_cents": pending_out_cents,
        "cash_as_of": _iso(cash_as_of),
        "obligations": obligations,
        "obligations_total_cents": obligations_total,
        "planned_extra_cents": planned_extra_cents,
        "goals_set_aside_cents": goals_set_aside_cents,
        "cash_floor_cents": cash_floor_cents,
        "safe_to_spend_cents": safe,
        "per_day_cents": per_day,
        "planned_spending_cents": planned,
        "warnings": warnings,
    }