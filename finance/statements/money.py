"""Exact decimal money helpers."""
from decimal import Decimal, ROUND_HALF_UP

TWOPLACES = Decimal("0.01")


def D(val):
    if val is None or val == "":
        return Decimal("0")
    if isinstance(val, Decimal):
        return val
    if isinstance(val, (int, float)):
        return Decimal(str(val))
    s = str(val).strip()
    dr = s.upper().endswith("DR")
    neg = s.startswith("-") or dr
    s_clean = s.upper().replace("DR", "").replace("CR", "").replace("$", "").replace(",", "").lstrip("+-").strip()
    if not s_clean:
        return Decimal("0")
    d = Decimal(s_clean)
    return -d if neg else d


def cents(val):
    return D(val).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def fmt(val, dash="-"):
    if val is None:
        return dash
    d = cents(val)
    if d < 0:
        return f"-${abs(d):,.2f}"
    return f"${d:,.2f}"
