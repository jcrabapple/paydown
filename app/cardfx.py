"""Effective card terms (FIX5 R1). PURE: no I/O, no app.db imports.

Synced cards can arrive with no APR (Plaid `aprs: []`) or a $0 minimum
(issuer not reporting, or the cycle's minimum already satisfied). Every
consumer of a card's APR/minimum (budget, engine, plan, STS) must go through
`effective()` so the numbers agree everywhere.

Precedence
  APR:     apr_override > bank/manual apr > inferred-from-interest (only when
           apr_source == 'unknown') > stored apr (0.0 placeholder, 'unknown')
  Minimum: min_override_cents > estimated (synced, bank min 0, balance > 0 and
           no statement balance reported: the bank is not reporting terms)
           > min_payment_cents
"""
from __future__ import annotations

from fractions import Fraction

MIN_FLOOR_CENTS = 2500          # typical issuer floor for an estimated minimum
INFER_MAX_APR = 60.0            # discard nonsense estimates above this
INFER_MIN_CHARGE_CENTS = 100    # ignore sub-$1 interest charges (residual interest)
ASSUMED_APR_FOR_ESTIMATE = 25.0  # when the APR is unknown, estimate minimums at a typical card APR


def _round_half_up(x: Fraction) -> int:
    return int(x + Fraction(1, 2)) if x >= 0 else -int(-x + Fraction(1, 2))


def estimate_min_cents(balance_cents: int, apr: float) -> int:
    """Common issuer formula: 1% of balance + one month's interest, at least
    $25, never more than the balance."""
    if balance_cents <= 0:
        return 0
    interest = Fraction(balance_cents) * Fraction(str(apr or 0)) / 100 / 12
    est = _round_half_up(Fraction(balance_cents) / 100 + interest)
    return min(balance_cents, max(MIN_FLOOR_CENTS, est))


def infer_apr(charge_cents: list[int], balance_cents: int | None) -> float | None:
    """Estimate APR from recent monthly interest charges against a balance
    (the last statement balance when known, else the current balance).

    apr ~= mean(charge) / balance * 12 * 100. Returns None when there is
    nothing usable or the estimate is implausible."""
    charges = [c for c in charge_cents if c and c >= INFER_MIN_CHARGE_CENTS]
    if not charges or not balance_cents or balance_cents <= 0:
        return None
    mean = Fraction(sum(charges), len(charges))
    apr = float(mean / balance_cents * 1200)
    if not (0 < apr <= INFER_MAX_APR):
        return None
    return round(apr, 2)


def effective(card: dict, inferred_apr: float | None = None) -> dict:
    """Effective terms for one card row (dict with the cards columns).

    Returns {apr, apr_source, min_payment_cents, min_source} where
      apr_source in override | bank | manual | inferred | unknown
      min_source in override | bank | manual | estimated
    """
    synced = (card.get("sync") or "manual") == "plaid"
    stored_src = card.get("apr_source") or "manual"

    if card.get("apr_override") is not None:
        apr, apr_src = float(card["apr_override"]), "override"
    elif stored_src == "unknown":
        if inferred_apr is not None:
            apr, apr_src = float(inferred_apr), "inferred"
        else:
            apr, apr_src = float(card.get("apr") or 0.0), "unknown"
    else:
        apr, apr_src = float(card.get("apr") or 0.0), stored_src

    balance = card.get("balance_cents") or 0
    bank_min = card.get("min_payment_cents") or 0
    if card.get("min_override_cents") is not None:
        minimum, min_src = int(card["min_override_cents"]), "override"
    elif synced and bank_min == 0 and balance > 0 and card.get("statement_balance_cents") is None:
        est_apr = apr if apr > 0 else ASSUMED_APR_FOR_ESTIMATE
        minimum, min_src = estimate_min_cents(balance, est_apr), "estimated"
    else:
        minimum, min_src = bank_min, ("bank" if synced else "manual")

    return {
        "apr": apr,
        "apr_source": apr_src,
        "min_payment_cents": minimum,
        "min_source": min_src,
    }
