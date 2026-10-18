import gzip
import json
from datetime import datetime, time, timedelta

import pytest

from tbrain.timeutil import IST


def _exp_ms(d):
    return int(datetime.combine(d, time(23, 59, 59), tzinfo=IST).timestamp() * 1000)


@pytest.fixture
def today():
    return datetime(2026, 10, 19, tzinfo=IST).date()  # a Monday


@pytest.fixture
def rows(today):
    """Instrument-master rows shaped like Upstox's documented fields."""
    e1, e2, em = today + timedelta(days=1), today + timedelta(days=8), today + timedelta(days=9)
    out = []
    n = 0
    for und, seg, ukey, base, step, exps in (
        ("NIFTY", "NSE_FO", "NSE_INDEX|Nifty 50", 25000, 50, (e1, e2)),
        ("SENSEX", "BSE_FO", "BSE_INDEX|SENSEX", 82000, 100, (today + timedelta(days=3), today + timedelta(days=10))),
    ):
        for e in exps:
            for k in range(-15, 16):
                for side in ("CE", "PE"):
                    n += 1
                    out.append({"segment": seg, "underlying_symbol": und, "underlying_key": ukey, "instrument_type": side,
                                "instrument_key": f"{seg}|{n}", "trading_symbol": f"{und} {base + k*step} {side}",
                                "strike_price": float(base + k * step), "expiry": _exp_ms(e),
                                "lot_size": 65 if und == "NIFTY" else 20, "tick_size": 0.05, "weekly": True})
        n += 1
        out.append({"segment": seg, "underlying_symbol": und, "underlying_key": ukey, "instrument_type": "FUT",
                    "instrument_key": f"{seg}|{n}", "trading_symbol": f"{und} FUT", "strike_price": 0, "expiry": _exp_ms(em),
                    "lot_size": 65 if und == "NIFTY" else 20, "tick_size": 0.1, "weekly": False})
    # things that must be excluded
    out.append({"segment": "NSE_FO", "underlying_symbol": "BANKNIFTY", "underlying_key": "NSE_INDEX|Nifty Bank",
                "instrument_type": "CE", "instrument_key": "NSE_FO|999", "strike_price": 50000.0, "expiry": _exp_ms(e1)})
    out.append({"segment": "NSE_EQ", "underlying_symbol": "NIFTY", "instrument_type": "EQ", "instrument_key": "NSE_EQ|X",
                "expiry": _exp_ms(e1)})
    out.append({"segment": "NSE_FO", "underlying_symbol": "NIFTY", "underlying_key": "NSE_INDEX|Nifty 50",
                "instrument_type": "CE", "instrument_key": "NSE_FO|OLD", "strike_price": 25000.0,
                "expiry": _exp_ms(today - timedelta(days=1))})
    return out


@pytest.fixture
def gz_url(tmp_path, rows):
    p = tmp_path / "complete.json.gz"
    p.write_bytes(gzip.compress(json.dumps(rows).encode()))
    return p.as_uri()
