"""Time helpers. Every "today" in the app goes through here so tests can
monkeypatch app.clock.today / app.clock.now."""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

_DEFAULT_TZ = "America/New_York"


def local_tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("BUDGET_TZ", _DEFAULT_TZ))


def today() -> date:
    """Local calendar date in BUDGET_TZ (env, default America/New_York)."""
    return now().astimezone(local_tz()).date()


def now() -> datetime:
    """Aware UTC datetime."""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """Aware UTC datetime as an ISO string with a Z suffix."""
    return now().strftime("%Y-%m-%dT%H:%M:%SZ")