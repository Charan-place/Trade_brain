from datetime import date, time as dtime

from tbrain.signals.event_calendar import RBI_MPC_DATES, is_event_blackout


def test_ordinary_day_is_never_a_blackout():
    assert is_event_blackout(date(2026, 10, 19), dtime(10, 0)) is None


def test_rbi_mpc_day_blocks_around_the_announcement_time():
    d = RBI_MPC_DATES[0]
    assert is_event_blackout(d, dtime(10, 0)) is not None          # right at announcement
    assert is_event_blackout(d, dtime(9, 45)) is not None          # 15 min before: inside the 30-min pre-window
    assert is_event_blackout(d, dtime(10, 45)) is not None         # 45 min after: inside the 60-min post-window
    assert is_event_blackout(d, dtime(9, 15)) is None              # well before the window opens
    assert is_event_blackout(d, dtime(13, 0)) is None              # well after the window closes


def test_fomc_reaction_morning_blocks_the_whole_entry_window():
    from datetime import timedelta
    from tbrain.signals.event_calendar import FOMC_DATES
    next_day = FOMC_DATES[0] + timedelta(days=1)
    assert is_event_blackout(next_day, dtime(9, 20)) is not None
    assert is_event_blackout(next_day, dtime(14, 0)) is not None
    assert "FOMC" in is_event_blackout(next_day, dtime(9, 20))


def test_union_budget_day_blocks_around_its_own_time():
    from tbrain.signals.event_calendar import UNION_BUDGET_DATES
    d = UNION_BUDGET_DATES[0]
    assert is_event_blackout(d, dtime(11, 0)) is not None
    assert "Budget" in is_event_blackout(d, dtime(11, 0))
    assert is_event_blackout(d, dtime(13, 30)) is None
