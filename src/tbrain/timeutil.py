"""IST time helpers. NSE cash/F&O regular session: 09:15-15:30 IST (verify against NSE circulars)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def now_ist() -> datetime:
    return datetime.now(IST)


def ist_date() -> date:
    return now_ist().date()


def epoch_ms_to_ist_date(ms: int | float) -> date:
    return datetime.fromtimestamp(ms / 1000.0, IST).date()


def in_market_hours(dt: datetime | None = None) -> bool:
    """Weekday + 09:15-15:30 IST. Does NOT know exchange holidays (feed simply stays silent on those)."""
    dt = (dt or now_ist()).astimezone(IST)
    return dt.weekday() < 5 and MARKET_OPEN <= dt.time() <= MARKET_CLOSE


def at_ist(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=IST)
