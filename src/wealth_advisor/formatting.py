"""Human-friendly number formatting shared by recommender, narrative and reports."""

from __future__ import annotations


def money(amount: float | None, ccy: str = "IDR") -> str:
    """1_175_000_000 -> 'IDR 1.18B'; 325_000_000 -> 'IDR 325.0M'."""
    if amount is None:
        return "n/a"
    sign = "-" if amount < 0 else ""
    a = abs(amount)
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{sign}{ccy} {a / div:,.{2 if suffix in 'TB' else 1}f}{suffix}"
    return f"{sign}{ccy} {a:,.0f}"


def pct(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100:.{digits}f}%"


def signed_money(amount: float, ccy: str = "IDR") -> str:
    return ("+" if amount >= 0 else "") + money(amount, ccy)
