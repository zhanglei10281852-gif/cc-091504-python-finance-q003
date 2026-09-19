"""跨时区截止点：协议约定时区的自然日边界统一换算为 UTC 时刻再比较，
保证同一事实在任何执行地点得到同一结论。"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo


def as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def day_start_utc(day: date, tz_name: str) -> datetime:
    return datetime.combine(day, time.min, ZoneInfo(tz_name)).astimezone(timezone.utc)


def day_end_utc(day: date, tz_name: str) -> datetime:
    return datetime.combine(day, time.max, ZoneInfo(tz_name)).astimezone(timezone.utc)


def reached(day: date, tz_name: str, now: datetime) -> bool:
    """协议时区该日 00:00:00 是否已到（归属、窗口起算）。"""
    return as_utc(now) >= day_start_utc(day, tz_name)


def expired(day: date, tz_name: str, now: datetime) -> bool:
    """协议时区该日 23:59:59 是否已过（截止、窗口届满）。"""
    return as_utc(now) > day_end_utc(day, tz_name)


def local_date(tz_name: str, now: datetime) -> date:
    """某时刻在协议时区对应的自然日。"""
    return as_utc(now).astimezone(ZoneInfo(tz_name)).date()
