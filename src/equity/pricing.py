"""市场价格与汇率簿:as-of 查询,取不晚于时点的最近一条记录。

结算单记录实际使用的价格/汇率快照(含时点),员工可据此核对,
避免"用了哪天的价"说不清的问题。找不到记录时显式报错,不猜测。
"""

from __future__ import annotations

from datetime import datetime

from .errors import ValidationError
from .models import FxRatePoint, PricePoint
from .timeutil import ensure_utc


class PriceBook:
    def __init__(self) -> None:
        self._points: list[PricePoint] = []

    def add(self, point: PricePoint) -> None:
        self._points.append(point)
        self._points.sort(key=lambda p: ensure_utc(p.at))

    def as_of(self, at: datetime, currency: str) -> PricePoint:
        at = ensure_utc(at)
        candidates = [
            p for p in self._points
            if p.currency == currency and ensure_utc(p.at) <= at
        ]
        if not candidates:
            raise ValidationError(
                f"缺少 {currency} 在 {at.isoformat()} 之前的市场价格",
                code="price_missing",
            )
        return candidates[-1]

    def points(self) -> list[PricePoint]:
        return list(self._points)


class FxBook:
    def __init__(self) -> None:
        self._rates: list[FxRatePoint] = []

    def add(self, rate: FxRatePoint) -> None:
        self._rates.append(rate)
        self._rates.sort(key=lambda r: ensure_utc(r.at))

    def as_of(self, at: datetime, base: str, quote: str) -> FxRatePoint:
        at = ensure_utc(at)
        candidates = [
            r for r in self._rates
            if r.base == base and r.quote == quote and ensure_utc(r.at) <= at
        ]
        if not candidates:
            raise ValidationError(
                f"缺少 {base}->{quote} 在 {at.isoformat()} 之前的汇率",
                code="fx_missing",
            )
        return candidates[-1]

    def rates(self) -> list[FxRatePoint]:
        return list(self._rates)
