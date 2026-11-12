"""Known high-volatility scheduled events (RBI MPC, Union Budget, US FOMC): dates only, hardcoded and
refreshed once a year from each body's own official announcement. No free machine-readable calendar API
exists for any of these (confirmed: RBI's own "Advance Release Calendar" covers SDDS data, not MPC dates) -
this is the standard practice, not a gap. Keep going on a stale list silently blocks the wrong day or misses
the right one, so each entry carries its source and REFRESH THIS FILE once the next cycle's dates are announced.

Why dates only, not a live news feed: this engine only ever BUYS premium (CE/PE), never sells it. On a
scheduled policy day, implied volatility typically runs up pre-announcement and crushes after - an option
buyer can be right on direction and still lose to that IV crush. A known-date blackout window sidesteps the
exact mechanism this system is specifically exposed to; it does not predict the announcement's outcome.
"""
from __future__ import annotations

from datetime import date, time as dtime

# RBI Monetary Policy Committee decision dates (last day of each 3-day meeting; announced ~10:00 IST).
# Source: RBI's own FY27 schedule press release (announced March 2026).
RBI_MPC_DATES: tuple[date, ...] = (
    date(2026, 4, 8), date(2026, 6, 5), date(2026, 8, 6),
    date(2026, 10, 1), date(2026, 12, 4), date(2027, 2, 5),
)

# Union Budget day (presented ~11:00 IST on 1 Feb unless it falls on a weekend).
UNION_BUDGET_DATES: tuple[date, ...] = (date(2027, 2, 1),)

# US FOMC decision days (announced ~2:30pm ET = ~midnight-1am IST next calendar day in India, but Indian
# markets react the FOLLOWING morning to the overnight move) - blackout applies to the Indian trading day
# right after each date below.
FOMC_DATES: tuple[date, ...] = (
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
    date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9),
)

BLACKOUT_BEFORE_MIN = 30   # minutes before the event's own time to start blocking new calls
BLACKOUT_AFTER_MIN = 60    # minutes after to keep blocking (IV crush + repricing settles down)

RBI_MPC_TIME = dtime(10, 0)
UNION_BUDGET_TIME = dtime(11, 0)


def blackout_window(d: date) -> tuple[dtime, dtime] | None:
    """If `d` is a known event day, the (start, end) IST time window new calls should be blocked.
    FOMC blocks the WHOLE entry window the next morning (the overnight move is already priced in by open)."""
    if d in RBI_MPC_DATES:
        start_min = RBI_MPC_TIME.hour * 60 + RBI_MPC_TIME.minute - BLACKOUT_BEFORE_MIN
        end_min = RBI_MPC_TIME.hour * 60 + RBI_MPC_TIME.minute + BLACKOUT_AFTER_MIN
        return dtime(start_min // 60, start_min % 60), dtime(end_min // 60, end_min % 60)
    if d in UNION_BUDGET_DATES:
        start_min = UNION_BUDGET_TIME.hour * 60 + UNION_BUDGET_TIME.minute - BLACKOUT_BEFORE_MIN
        end_min = UNION_BUDGET_TIME.hour * 60 + UNION_BUDGET_TIME.minute + BLACKOUT_AFTER_MIN
        return dtime(start_min // 60, start_min % 60), dtime(end_min // 60, end_min % 60)
    from datetime import timedelta
    if (d - timedelta(days=1)) in FOMC_DATES:
        return dtime(9, 15), dtime(9, 15)  # degenerate "whole morning" marker; caller treats start==end specially
    return None


def is_event_blackout(d: date, t: dtime) -> str | None:
    """Returns a human reason string if `(d, t)` falls inside a known event blackout window, else None."""
    win = blackout_window(d)
    if win is None:
        return None
    start, end = win
    if start == end:  # FOMC "whole morning" marker
        return f"FOMC reaction day ({d.isoformat()}): overnight move already priced in, blocked all morning"
    if start <= t <= end:
        label = "RBI MPC" if d in RBI_MPC_DATES else "Union Budget"
        return f"{label} day ({d.isoformat()}): blocked {start:%H:%M}-{end:%H:%M} (IV crush risk for option buyers)"
    return None
