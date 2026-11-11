"""Rough foreign-currency to AUD conversion, for cross-checking foreign invoices against bank lines.

Rates are DATA, not config: they live in <records>/fx_rates.json (see config.load_records), e.g.

    {"JPY": {"2022-09-16": {"aud": 0.010412, "source": "public daily rate"},
             "2022-10": 0.0099}}

A key is a day (YYYY-MM-DD) or a month (YYYY-MM); the value is AUD per 1 unit of the currency,
either a number or {"aud": number, "source": text}. A day key wins over a month key. Nothing is
fetched automatically and nothing depends on this file: without a rate the step is skipped.
"""
from decimal import Decimal
from typing import Any, Dict, Optional

from . import config


def _rates() -> Dict[str, Any]:
    return (config.load().get("personal") or {}).get("fx_rates") or {}


def rate(currency: str, day: str, rates: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """{"rate": Decimal, "key": "...", "source": text} for currency on YYYY-MM-DD, or None."""
    table = (rates if rates is not None else _rates()).get((currency or "").upper()) or {}
    for key in (day, day[:7]):
        if key in table:
            v = table[key]
            val = v.get("aud") if isinstance(v, dict) else v
            if val:
                return {"rate": Decimal(str(val)), "key": key, "source": v.get("source", "") if isinstance(v, dict) else ""}
    return None


def to_aud(amount, currency: str, day: str, rates: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Estimate of amount in AUD (rounded to cents) with the rate used, or None if no rate is known."""
    if (currency or "AUD").upper() == "AUD":
        return {"aud": Decimal(str(amount)).quantize(Decimal("0.01")), "rate": Decimal(1), "key": "AUD", "source": ""}
    r = rate(currency, day, rates)
    if not r:
        return None
    return {"aud": (Decimal(str(amount)) * r["rate"]).quantize(Decimal("0.01")), **r}
