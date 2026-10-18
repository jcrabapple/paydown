"""Pydantic request models."""
from __future__ import annotations

import re
from datetime import date

from pydantic import BaseModel, Field, field_validator

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MAX_CENTS = 10**10


def _check_name(v: str | None) -> str | None:
    if v is None:
        return v
    if not (1 <= len(v) <= 80):
        raise ValueError("name must be 1-80 chars")
    return v


def _check_amount(v: int | None) -> int | None:
    if v is None:
        return v
    if not (0 <= v <= MAX_CENTS):
        raise ValueError("amount out of range")
    return v


def _check_month(v: str | None) -> str | None:
    if v is None:
        return v
    if not MONTH_RE.match(v):
        raise ValueError("month must be YYYY-MM")
    return v


def _check_day(v: int | None) -> int | None:
    if v is None or (1 <= v <= 31):
        return v
    raise ValueError("due_day must be 1-31")


class SettingsIn(BaseModel):
    strategy: str | None = None
    buffer_cents: int | None = None
    debt_share_pct: int | None = Field(default=None, ge=0, le=100)
    extra_override_cents: int | None = None
    cash_floor_cents: int | None = None
    cash_reserve_months: float | None = Field(default=None, ge=0, le=6)
    auto_log_card_payments: bool | None = None

    @field_validator("strategy")
    @classmethod
    def _strategy(cls, v):
        if v is not None and v not in ("avalanche", "snowball", "custom"):
            raise ValueError("strategy must be avalanche, snowball or custom")
        return v

    @field_validator("buffer_cents")
    @classmethod
    def _buffer(cls, v):
        return _check_amount(v)

    @field_validator("extra_override_cents")
    @classmethod
    def _override(cls, v):
        return _check_amount(v)


class IncomeIn(BaseModel):
    name: str
    amount_cents: int
    active: bool = True
    sort: int = 0

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return _check_name(v)

    @field_validator("amount_cents")
    @classmethod
    def _amt(cls, v):
        return _check_amount(v)


class ExpenseIn(BaseModel):
    name: str
    amount_cents: int
    kind: str = "bill"
    due_day: int | None = None
    active: bool = True
    sort: int = 0
    paid_from: str = "cash"
    match_text: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return _check_name(v)

    @field_validator("amount_cents")
    @classmethod
    def _amt(cls, v):
        return _check_amount(v)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        if v not in ("bill", "spending"):
            raise ValueError("kind must be bill or spending")
        return v

    @field_validator("due_day")
    @classmethod
    def _day(cls, v):
        return _check_day(v)

    @field_validator("paid_from")
    @classmethod
    def _paid_from(cls, v):
        if v not in ("cash", "card"):
            raise ValueError("paid_from must be cash or card")
        return v

    @field_validator("match_text")
    @classmethod
    def _match_text(cls, v):
        if v is None:
            return v
        v = v.strip()
        if not (2 <= len(v) <= 80):
            raise ValueError("match_text must be 2-80 chars")
        return v


class CardIn(BaseModel):
    name: str | None = None
    balance_cents: int | None = None
    apr: float | None = None
    min_payment_cents: int | None = None
    credit_limit_cents: int | None = None
    due_day: int | None = None
    priority: int | None = None
    closed: bool | None = None
    # FIX5: user overrides for synced cards (explicit null clears)
    apr_override: float | None = None
    min_override_cents: int | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return _check_name(v)

    @field_validator("apr_override")
    @classmethod
    def _apr_ovr(cls, v):
        if v is not None and not (0 <= v < 100):
            raise ValueError("apr_override must be 0 <= apr < 100")
        return v

    @field_validator("min_override_cents")
    @classmethod
    def _min_ovr(cls, v):
        return _check_amount(v)

    @field_validator("balance_cents")
    @classmethod
    def _bal(cls, v):
        return _check_amount(v)

    @field_validator("apr")
    @classmethod
    def _apr(cls, v):
        if v is not None and not (0 <= v < 100):
            raise ValueError("apr must be 0 <= apr < 100")
        return v

    @field_validator("min_payment_cents")
    @classmethod
    def _min(cls, v):
        return _check_amount(v)

    @field_validator("credit_limit_cents")
    @classmethod
    def _limit(cls, v):
        return _check_amount(v)

    @field_validator("due_day")
    @classmethod
    def _day(cls, v):
        return _check_day(v)


class PaymentIn(BaseModel):
    card_id: int
    amount_cents: int
    paid_on: str
    month: str | None = None
    note: str | None = None

    @field_validator("amount_cents")
    @classmethod
    def _amt(cls, v):
        if v is None or not (0 < v <= MAX_CENTS):
            raise ValueError("amount must be positive")
        return v

    @field_validator("paid_on")
    @classmethod
    def _date(cls, v):
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError("paid_on must be YYYY-MM-DD")
        return v

    @field_validator("month")
    @classmethod
    def _month(cls, v):
        return _check_month(v)


class AdjustmentIn(BaseModel):
    month: str
    name: str
    amount_cents: int

    @field_validator("month")
    @classmethod
    def _month(cls, v):
        return _check_month(v)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return _check_name(v)

    @field_validator("amount_cents")
    @classmethod
    def _amt(cls, v):
        if v is None or abs(v) > MAX_CENTS:
            raise ValueError("amount out of range")
        return v


class ImportIn(BaseModel):
    data: dict


class PlaidLinkTokenIn(BaseModel):
    item_id: int | None = None


class PlaidExchangeIn(BaseModel):
    public_token: str
    institution_name: str | None = None


class PlaidLinkIn(BaseModel):
    card_id: int | None = None
    cash_account_id: int | None = None
    create: bool | None = None


class PlaidIgnoreIn(BaseModel):
    ignored: bool


class CashAccountIn(BaseModel):
    name: str | None = None
    kind: str | None = None
    balance_cents: int | None = None
    include_in_cash: bool | None = None
    sort: int | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return _check_name(v)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        if v is not None and v not in ("checking", "savings", "other"):
            raise ValueError("kind must be checking, savings or other")
        return v

    @field_validator("balance_cents")
    @classmethod
    def _bal(cls, v):
        if v is None or abs(v) > MAX_CENTS:
            raise ValueError("balance out of range")
        return v


FREQUENCIES = ("weekly", "biweekly", "semimonthly", "monthly")
WEEKEND_RULES = ("previous", "next", "none")


def _check_date(v: str | None) -> str | None:
    if v is None:
        return v
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        date.fromisoformat(v)
    except ValueError:
        raise ValueError("invalid date")
    return v


class PayScheduleIn(BaseModel):
    name: str
    frequency: str
    anchor_date: str
    semimonthly_day1: int | None = None
    semimonthly_day2: int | None = None
    weekend_rule: str = "previous"
    amount_cents: int = 0
    cash_account_id: int | None = None
    active: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return _check_name(v)

    @field_validator("frequency")
    @classmethod
    def _freq(cls, v):
        if v not in FREQUENCIES:
            raise ValueError("frequency must be weekly, biweekly, semimonthly or monthly")
        return v

    @field_validator("anchor_date")
    @classmethod
    def _anchor(cls, v):
        return _check_date(v)

    @field_validator("weekend_rule")
    @classmethod
    def _wr(cls, v):
        if v not in WEEKEND_RULES:
            raise ValueError("weekend_rule must be previous, next or none")
        return v

    @field_validator("amount_cents")
    @classmethod
    def _amt(cls, v):
        return _check_amount(v)

    @field_validator("semimonthly_day1", "semimonthly_day2")
    @classmethod
    def _sm_day(cls, v):
        if v is None or (1 <= v <= 31):
            return v
        raise ValueError("semimonthly days must be 1-31")

    def check_semimonthly(self):
        if self.frequency == "semimonthly":
            if (
                self.semimonthly_day1 is None or self.semimonthly_day2 is None
                or not (self.semimonthly_day1 < self.semimonthly_day2)
            ):
                raise ValueError("semimonthly requires day1 < day2 (both 1-31)")
        elif self.semimonthly_day1 is not None or self.semimonthly_day2 is not None:
            raise ValueError("semimonthly days are only valid for semimonthly frequency")
        return self


class PaySchedulePatch(BaseModel):
    name: str | None = None
    frequency: str | None = None
    anchor_date: str | None = None
    semimonthly_day1: int | None = None
    semimonthly_day2: int | None = None
    weekend_rule: str | None = None
    amount_cents: int | None = None
    cash_account_id: int | None = None
    active: bool | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return _check_name(v)

    @field_validator("frequency")
    @classmethod
    def _freq(cls, v):
        if v is not None and v not in FREQUENCIES:
            raise ValueError("frequency must be weekly, biweekly, semimonthly or monthly")
        return v

    @field_validator("anchor_date")
    @classmethod
    def _anchor(cls, v):
        return _check_date(v)

    @field_validator("weekend_rule")
    @classmethod
    def _wr(cls, v):
        if v is not None and v not in WEEKEND_RULES:
            raise ValueError("weekend_rule must be previous, next or none")
        return v

    @field_validator("amount_cents")
    @classmethod
    def _amt(cls, v):
        return _check_amount(v)

    @field_validator("semimonthly_day1", "semimonthly_day2")
    @classmethod
    def _sm_day(cls, v):
        if v is None or (1 <= v <= 31):
            return v
        raise ValueError("semimonthly days must be 1-31")


class PayScheduleDetectIn(BaseModel):
    transaction_ids: list[str] | None = None


class GoalIn(BaseModel):
    name: str
    target_cents: int
    target_date: str | None = None
    active: bool = True
    sort: int = 0

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return _check_name(v)

    @field_validator("target_cents")
    @classmethod
    def _target(cls, v):
        if v is None or v <= 0:
            raise ValueError("target_cents must be positive")
        return v

    @field_validator("target_date")
    @classmethod
    def _td(cls, v):
        return _check_date(v)


class GoalPatch(BaseModel):
    name: str | None = None
    target_cents: int | None = None
    target_date: str | None = None
    active: bool | None = None
    sort: int | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return _check_name(v)

    @field_validator("target_cents")
    @classmethod
    def _target(cls, v):
        if v is not None and v <= 0:
            raise ValueError("target_cents must be positive")
        return v

    @field_validator("target_date")
    @classmethod
    def _td(cls, v):
        return _check_date(v)


class GoalMoveIn(BaseModel):
    amount_cents: int
    note: str | None = None

    @field_validator("amount_cents")
    @classmethod
    def _amt(cls, v):
        if v == 0 or abs(v) > MAX_CENTS:
            raise ValueError("amount_cents must be non-zero")
        return v


class LabelIn(BaseModel):
    label: str | None = None
    pay_schedule_id: int | None = None
    create_rule: bool = False
    rule_text: str | None = None

    @field_validator("label")
    @classmethod
    def _label(cls, v):
        if v is not None and v not in ("paycheck", "not_paycheck"):
            raise ValueError("label must be paycheck or not_paycheck")
        return v


class BulkLabelsIn(BaseModel):
    transaction_ids: list[str]
    label: str | None = None
    pay_schedule_id: int | None = None

    @field_validator("transaction_ids")
    @classmethod
    def _ids(cls, v):
        if len(v) > 500:
            raise ValueError("at most 500 transaction_ids per request")
        return v

    @field_validator("label")
    @classmethod
    def _label(cls, v):
        if v is not None and v not in ("paycheck", "not_paycheck"):
            raise ValueError("label must be paycheck or not_paycheck")
        return v


class BillPaidIn(BaseModel):
    due_date: str

    @field_validator("due_date")
    @classmethod
    def _date(cls, v):
        return _check_date(v)


class UseCashIn(BaseModel):
    amount_cents: int | None = None

    @field_validator("amount_cents")
    @classmethod
    def _amt(cls, v):
        if v is None:
            return v
        if not (0 < v <= MAX_CENTS):
            raise ValueError("amount must be positive")
        return v


class PlaidLinkIn(BaseModel):
    card_id: int | None = None
    cash_account_id: int | None = None
    create: bool | None = None


class SubscriptionIn(BaseModel):
    name: str
    amount_cents: int
    cycle: str = "monthly"
    next_date: str
    match_text: str | None = None
    note: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        return _check_name(v)

    @field_validator("amount_cents")
    @classmethod
    def _amt(cls, v):
        return _check_amount(v)

    @field_validator("cycle")
    @classmethod
    def _cycle(cls, v):
        if v not in ("weekly", "monthly", "quarterly", "yearly"):
            raise ValueError("cycle must be weekly, monthly, quarterly or yearly")
        return v

    @field_validator("next_date")
    @classmethod
    def _next(cls, v):
        return _check_date(v)

    @field_validator("match_text")
    @classmethod
    def _match(cls, v):
        if v is None:
            return None
        v = v.strip().lower()
        if len(v) > 80:
            raise ValueError("match_text must be at most 80 chars")
        return v or None

    @field_validator("note")
    @classmethod
    def _note(cls, v):
        if v is None:
            return None
        v = v.strip()
        if len(v) > 200:
            raise ValueError("note must be at most 200 chars")
        return v or None


class SubscriptionCancelIn(BaseModel):
    cancelled_on: str | None = None

    @field_validator("cancelled_on")
    @classmethod
    def _on(cls, v):
        return _check_date(v)


class SubscriptionDismissIn(BaseModel):
    key: str

    @field_validator("key")
    @classmethod
    def _key(cls, v):
        v = v.strip().lower()
        if not (1 <= len(v) <= 80):
            raise ValueError("key must be 1-80 chars")
        return v
