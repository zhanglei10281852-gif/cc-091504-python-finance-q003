"""跨时区时间换算。

平台内部一律使用 UTC aware datetime;业务日期(归属日、离职日、
行权截止日)按授予约定时区解释,"当日截止"指该时区 23:59:59.999999,
换算为 UTC 时刻后参与比较,保证跨时区截止点结果确定。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def ensure_utc(dt: datetime) -> datetime:
    """归一化为 UTC aware;naive 输入视为 UTC(显式优于猜测)。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def end_of_day_utc(d: date, tz_name: str) -> datetime:
    """业务日期 d 在 tz_name 时区的日末时刻,返回 UTC。"""
    local_end = datetime.combine(d, time.max, tzinfo=ZoneInfo(tz_name))
    return local_end.astimezone(UTC)


def local_date(dt: datetime, tz_name: str) -> date:
    """UTC 时刻在 tz_name 时区对应的业务日期。"""
    return ensure_utc(dt).astimezone(ZoneInfo(tz_name)).date()


def utcnow() -> datetime:
    return datetime.now(UTC)


def add_days(d: date, days: int) -> date:
    return d + timedelta(days=days)
