from datetime import date

import pytest

from tbrain.research.history import candle_url, month_windows, parse_candles


def test_month_windows_cover_the_range_without_gaps_or_overlap():
    w = month_windows(date(2022, 1, 1), date(2022, 4, 15))
    assert w == [(date(2022, 1, 1), date(2022, 1, 31)), (date(2022, 2, 1), date(2022, 2, 28)),
                 (date(2022, 3, 1), date(2022, 3, 31)), (date(2022, 4, 1), date(2022, 4, 15))]
    assert month_windows(date(2022, 12, 20), date(2023, 1, 5)) == [
        (date(2022, 12, 20), date(2022, 12, 31)), (date(2023, 1, 1), date(2023, 1, 5))]


def test_url_puts_to_date_before_from_date_and_encodes_the_key():
    u = candle_url("NSE_INDEX|Nifty 50", date(2025, 1, 1), date(2025, 1, 31))
    assert u.endswith("/minutes/1/2025-01-31/2025-01-01")
    assert "NSE_INDEX%7CNifty%2050" in u and "|" not in u and " " not in u


def test_parse_orders_oldest_first_and_pads_a_missing_oi_column():
    p = {"status": "success", "data": {"candles": [["2025-01-02T09:16:00+05:30", 2, 3, 1, 2, 10],
                                                    ["2025-01-02T09:15:00+05:30", 1, 2, 1, 2, 5, 7]]}}
    rows = parse_candles(p)
    assert [r[0][11:16] for r in rows] == ["09:15", "09:16"]
    assert rows[1][6] == 0 and rows[0][6] == 7


def test_parse_rejects_bad_status_and_bad_shape():
    with pytest.raises(ValueError):
        parse_candles({"status": "error"})
    with pytest.raises(ValueError):
        parse_candles({"status": "success", "data": {"candles": [[1, 2, 3]]}})
