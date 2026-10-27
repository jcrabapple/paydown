"""Pay schedules: occurrence prediction, paycheck receipts, next payday, inference.

Pure module: no DB, no I/O, no clock calls. Dates in, dates out.
Money is integer cents; fractional cents use round_half_up_cents from app.engine.
"""
from __future__ import annotations

import calendar
import statistics
from collections import Counter
from datetime import date, timedelta
from fractions import Fraction

from app.engine import round_half_up_cents

GRACE_DAYS = 3

# Back-scan horizon for received/missed occurrences (days before today).
SCAN_BACK_DAYS = 62
# Forward horizon for candidate occurrences.
SCAN_FORWARD_DAYS = 62


def _as_date(v: date | str) -> date:
    if isinstance(v, date):
        return v
    return date.fromisoformat(v)


def _clamp_day(year: int, month: int, day: int) -> int:
    """Day-of-month clamped to month length; 31 -> last day (Feb 28/29, Apr 30...)."""
    return min(day, calendar.monthrange(year, month)[1])


def _weekend_adjust(d: date, rule: str) -> date:
    wd = d.weekday()  # Mon=0 .. Sun=6
    if rule == "previous":
        if wd == 5:  # Saturday -> previous Friday
            return d - timedelta(days=1)
        if wd == 6:  # Sunday -> previous Friday
            return d - timedelta(days=2)
    elif rule == "next":
        if wd == 5:  # Saturday -> next Monday
            return d + timedelta(days=2)
        if wd == 6:  # Sunday -> next Monday
            return d + timedelta(days=1)
    return d


def _nominal_dates(schedule: dict, start: date, end: date) -> list[date]:
    """Nominal (pre-weekend-adjustment) paydays in [start, end]."""
    freq = schedule["frequency"]
    anchor = _as_date(schedule["anchor_date"])
    out: list[date] = []
    if freq in ("weekly", "biweekly"):
        step = 7 if freq == "weekly" else 14
        k = (start - anchor).days // step - 1
        while anchor + timedelta(days=k * step) <= end:
            d = anchor + timedelta(days=k * step)
            if d >= start:
                out.append(d)
            k += 1
    elif freq == "monthly":
        day = anchor.day
        y, m = start.year, start.month
        while (y, m) <= (end.year, end.month):
            out.append(date(y, m, _clamp_day(y, m, day)))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    elif freq == "semimonthly":
        d1 = schedule.get("semimonthly_day1")
        d2 = schedule.get("semimonthly_day2")
        y, m = start.year, start.month
        while (y, m) <= (end.year, end.month):
            for day in (d1, d2):
                if day is not None:
                    out.append(date(y, m, _clamp_day(y, m, day)))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    else:
        raise ValueError(f"unknown frequency: {freq!r}")
    return out


def occurrences(schedule: dict, start: date, end: date) -> list[date]:
    """Paydays in [start, end] inclusive, sorted, after the weekend rule.

    Nominal dates are generated from start-7 to end+7 and filtered AFTER
    adjustment, so a payday adjusted into the window is never lost.
    """
    start = _as_date(start)
    end = _as_date(end)
    rule = schedule.get("weekend_rule") or "none"
    seen: set[date] = set()
    for nominal in _nominal_dates(schedule, start - timedelta(days=7), end + timedelta(days=7)):
        d = _weekend_adjust(nominal, rule)
        if start <= d <= end:
            seen.add(d)
    return sorted(seen)


def _confirmed(schedule: dict, paychecks: list[dict]) -> bool:
    sid = schedule.get("id")
    return any(p.get("pay_schedule_id") == sid for p in paychecks)


def receipts(
    schedule: dict, paychecks: list[dict], start: date, end: date
) -> dict[date, dict | None]:
    """Map each occurrence in [start, end] to the paycheck that received it, or None.

    paychecks: labelled posted paycheck deposits
    {transaction_id, date, amount_cents (positive), pay_schedule_id}.
    Deposit-confirmed iff at least one paycheck carries this schedule's id.
    Matching: each paycheck matches at most one occurrence (nearest within
    +/-GRACE_DAYS, ties -> earlier occurrence); each occurrence takes at most
    one paycheck (extras ignored for receipt).
    """
    start = _as_date(start)
    end = _as_date(end)
    result: dict[date, dict | None] = {o: None for o in occurrences(schedule, start, end)}
    if not _confirmed(schedule, paychecks):
        return result
    sid = schedule["id"]
    mine = [
        {
            "transaction_id": p["transaction_id"],
            "date": _as_date(p["date"]),
            "amount_cents": p["amount_cents"],
            "pay_schedule_id": p.get("pay_schedule_id"),
        }
        for p in paychecks
        if p.get("pay_schedule_id") == sid
    ]
    # Step 1: each paycheck picks its NEAREST occurrence within +/-GRACE_DAYS
    # (ties -> earlier occurrence); a paycheck with no occurrence in grace is
    # ignored for receipt.
    candidates: dict[date, list[dict]] = {}
    for p in mine:
        best_occ: date | None = None
        best_key = None
        for occ in result:
            delta = abs((p["date"] - occ).days)
            if delta > GRACE_DAYS:
                continue
            key = (delta, occ)  # nearest, ties -> earlier occurrence
            if best_key is None or key < best_key:
                best_key = key
                best_occ = occ
        if best_occ is not None:
            candidates.setdefault(best_occ, []).append(p)
    # Step 2: each occurrence keeps at most one paycheck - the nearest, ties
    # -> earlier paycheck date, then transaction_id (extras ignored).
    for occ, ps in candidates.items():
        ps.sort(key=lambda p: (abs((p["date"] - occ).days), p["date"], p["transaction_id"]))
        result[occ] = ps[0]
    return result


def next_payday(today: date, schedules: list[dict], paychecks: list[dict]) -> dict:
    """Next payday across active schedules, with missed occurrences and last payday."""
    today = _as_date(today)
    scan_start = today - timedelta(days=SCAN_BACK_DAYS)
    scan_end = today + timedelta(days=SCAN_FORWARD_DAYS)

    candidates: list[dict] = []
    missed: list[dict] = []
    received_dates: list[date] = []
    last_paycheck_by_schedule: dict[int, date | None] = {}

    for s in schedules:
        if not s.get("active", 1):
            continue
        confirmed = _confirmed(s, paychecks)
        recv_map = (
            receipts(s, paychecks, scan_start, scan_end) if confirmed else {}
        )
        if confirmed:
            received_dates_sched = [
                occ for occ, p in recv_map.items() if p is not None and occ <= today
            ]
            # R4/S6: only occurrences at/after the schedule's earliest
            # paycheck can be missed - before labelling started, absence is
            # not evidence of a missed paycheck. That bound must come from ALL
            # of the schedule's paychecks (they may be older than the 62-day
            # scan window), not just received occurrences inside the window.
            my_dates = [
                _as_date(p["date"])
                for p in paychecks
                if p.get("pay_schedule_id") == s["id"]
            ]
            first_recv = (
                min(my_dates) - timedelta(days=GRACE_DAYS) if my_dates else None
            )
        else:
            received_dates_sched = [o for o in occurrences(s, scan_start, today)]
            first_recv = None
        last_recv = max(received_dates_sched) if received_dates_sched else None
        if confirmed:
            # latest paycheck actually received for this schedule (any age), for
            # the "paychecks stopped" warning
            last_paycheck_by_schedule[s["id"]] = max(my_dates) if my_dates else None
        for occ in occurrences(s, scan_start, scan_end):
            if confirmed:
                received = recv_map.get(occ) is not None
            else:
                received = occ <= today  # unconfirmed: rolls at midnight of payday
            if received:
                if occ <= today:
                    received_dates.append(occ)
                continue
            if occ > today + timedelta(days=GRACE_DAYS):
                candidates.append({"date": occ, "schedule": s})
            elif occ >= today - timedelta(days=GRACE_DAYS):
                # not received, grace not fully elapsed -> candidate (today/late)
                candidates.append({"date": occ, "schedule": s})
            elif first_recv is not None and occ >= first_recv:
                # not received, today > occ + GRACE_DAYS, and not before the
                # first paycheck that ever arrived -> missed (R4)
                missed.append(
                    {"date": occ, "schedule_id": s["id"], "name": s["name"]}
                )

    candidates.sort(key=lambda c: (c["date"], c["schedule"]["id"]))
    payday = None
    if candidates:
        c = candidates[0]
        d = c["date"]
        status = "today" if d == today else ("late" if d < today else "upcoming")
        payday = {
            "date": d,
            "schedule_id": c["schedule"]["id"],
            "name": c["schedule"]["name"],
            "expected_cents": c["schedule"].get("amount_cents", 0),
            "status": status,
        }
    missed.sort(key=lambda m: (m["date"], m["schedule_id"]))
    return {
        "payday": payday,
        "missed": missed,
        "last_payday": max(received_dates) if received_dates else None,
        "last_paycheck_by_schedule": last_paycheck_by_schedule,
    }


def _mon_d(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def stopped_warnings(payday_info: dict) -> list[dict]:
    """S6: one `paychecks_stopped` warning per deposit-confirmed schedule with >= 2 missed
    occurrences in a row since its last received paycheck. Isolated misses between
    received paychecks are reported as `missed` but are not "stopped"."""
    last_by_sched = payday_info.get("last_paycheck_by_schedule") or {}
    by_schedule: dict[tuple[int, str], list[date]] = {}
    for m in payday_info.get("missed", []):
        by_schedule.setdefault((m["schedule_id"], m["name"]), []).append(_as_date(m["date"]))
    out: list[dict] = []
    for (sid, name), dates in sorted(by_schedule.items()):
        last = last_by_sched.get(sid)
        trailing = [d for d in dates if last is None or d > last]
        if len(trailing) < 2:
            continue
        since = _mon_d(last) if last else _mon_d(min(trailing))
        out.append(
            {
                "code": "paychecks_stopped",
                "message": f"No {name} paycheck since {since} ({len(trailing)} missed). "
                "Is this schedule still active?",
                "date": (last or min(trailing)).isoformat(),
            }
        )
    return out

def _day_clusters(days: Counter) -> list[tuple[int, bool]]:
    """Cluster days-of-month. A cluster = days within 3 of each other; any day
    >= 28 belongs to the '31' (end-of-month) cluster. Returns (rep_day, is_31)."""
    end_month = sorted(d for d in days if d >= 28)
    clusters: list[tuple[int, bool]] = []
    if end_month:
        clusters.append((31, True))
    rest = sorted(d for d in days if d < 28)
    group: list[int] = []
    for d in rest:
        if group and d - group[-1] > 3:
            clusters.append((group_rep(group, days), False))
            group = []
        group.append(d)
    if group:
        clusters.append((group_rep(group, days), False))
    return sorted(clusters)


def group_rep(group: list[int], days: Counter) -> int:
    """Most common day in the cluster (ties -> lower day)."""
    best = max(group, key=lambda d: (days[d], -d))
    return best


def infer_schedule(dates: list, amounts: list) -> dict | None:
    """Suggest a pay schedule from labelled paychecks (needs >= 2 distinct dates)."""
    pairs: dict[date, int] = {}
    for d, a in zip(dates, amounts):
        pairs.setdefault(_as_date(d), a)
    ds = sorted(pairs)
    if len(ds) < 2:
        return None
    amts = [pairs[d] for d in ds]
    intervals = [(b - a).days for a, b in zip(ds, ds[1:])]
    med = statistics.median(intervals)

    freq = None
    day1 = day2 = None
    weekday = Counter(d.weekday() for d in ds)
    top_share = weekday.most_common(1)[0][1] * 4 >= len(ds) * 3  # >= 75%
    if top_share:
        if 6 <= med <= 8:
            freq = "weekly"
        elif 12 <= med <= 16:
            freq = "biweekly"
    if freq is None:
        clusters = _day_clusters(Counter(d.day for d in ds))
        if len(clusters) <= 2 and 12 <= med <= 18:
            freq = "semimonthly"
            day1 = clusters[0][0]
            day2 = clusters[1][0] if len(clusters) > 1 else clusters[0][0]
    if freq is None and 26 <= med <= 33:
        freq = "monthly"
    if freq is None:
        return None

    med_amount = statistics.median(amts) if amts else 0
    return {
        "frequency": freq,
        "anchor_date": ds[-1],
        "semimonthly_day1": day1,
        "semimonthly_day2": day2,
        "weekend_rule": "previous",
        "amount_cents": round_half_up_cents(Fraction(med_amount)),
        "confidence": "high" if len(ds) >= 3 else "low",
        "dates_used": ds,
    }


_MONTHLY_EQUIV = {
    "weekly": (52, 12),
    "biweekly": (26, 12),
    "semimonthly": (2, 1),
    "monthly": (1, 1),
}


def monthly_equivalent_cents(schedule: dict) -> int:
    """weekly amount*52/12, biweekly amount*26/12, semimonthly amount*2, monthly amount."""
    num, den = _MONTHLY_EQUIV[schedule["frequency"]]
    return round_half_up_cents(
        Fraction(schedule.get("amount_cents", 0)) * Fraction(num, den)
    )