"""Time helpers.

Rule: never subtract or add to aware datetimes that share a ZoneInfo directly —
Python does wall-clock arithmetic in that case, which is wrong across DST.
Everything goes through UTC here.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
ROME = ZoneInfo("Europe/Rome")

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def parse_hhmm(value: str) -> time:
    hh, mm = str(value).strip().split(":")
    return time(int(hh), int(mm))


def at(day: date, t: time, tz: ZoneInfo) -> datetime:
    """Wall-clock time `t` on `day` in `tz` (fold=0 on ambiguous times)."""
    return datetime.combine(day, t, tzinfo=tz)


def shift(dt: datetime, minutes: float) -> datetime:
    """Add real elapsed minutes, keeping dt's zone."""
    tz = dt.tzinfo
    return (dt.astimezone(UTC) + timedelta(minutes=minutes)).astimezone(tz)


def minutes_between(a: datetime, b: datetime) -> int:
    """Real elapsed minutes from a to b."""
    return int(round((b.astimezone(UTC) - a.astimezone(UTC)).total_seconds() / 60))


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start.astimezone(UTC) < b_end.astimezone(UTC) and b_start.astimezone(UTC) < a_end.astimezone(UTC)


def within(t: datetime, start: datetime, end: datetime) -> bool:
    u = t.astimezone(UTC)
    return start.astimezone(UTC) <= u < end.astimezone(UTC)


def local(dt: datetime, tz: ZoneInfo = ROME) -> datetime:
    return dt.astimezone(tz)


def hhmm(dt: datetime, tz: ZoneInfo = ROME) -> str:
    return local(dt, tz).strftime("%H:%M")


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def day_label(day: date) -> str:
    return f"{DAY_NAMES[day.weekday()]} {day.day} {MONTHS[day.month - 1]}"


def fmt_minutes(m: int | None) -> str:
    m = int(m or 0)
    h, r = divmod(m, 60)
    if h and r:
        return f"{h}h{r:02d}"
    if h:
        return f"{h}h"
    return f"{r}m"


def utcnow() -> datetime:
    return datetime.now(UTC)
