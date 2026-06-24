"""Shared helpers for built-in checks."""
from __future__ import annotations


def fmt_num(n: float) -> str:
    """Compact human number: 1234567 -> '1.23M'."""
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.2f}{unit}"
    return f"{n:.0f}"


def fmt_rate(n: float, suffix: str) -> str:
    return f"{fmt_num(n)} {suffix}"
