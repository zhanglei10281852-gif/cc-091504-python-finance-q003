"""累进预扣税计算,全 Decimal,结果确定。"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .constants import MONEY_QUANT
from .models import TaxBracket


def progressive_tax(income: Decimal, brackets: tuple[TaxBracket, ...]) -> Decimal:
    """按档累进:每档区间 [lower, 下一档 lower) 乘以该档税率后求和。"""
    if income <= 0 or not brackets:
        return Decimal("0")
    ordered = sorted(brackets, key=lambda b: b.lower)
    tax = Decimal("0")
    for i, bracket in enumerate(ordered):
        upper = ordered[i + 1].lower if i + 1 < len(ordered) else None
        if income <= bracket.lower:
            continue
        top = income if upper is None else min(income, upper)
        tax += (top - bracket.lower) * bracket.rate
    return tax


def money(amount: Decimal) -> Decimal:
    """金额统一 2 位小数、四舍五入。"""
    return amount.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
