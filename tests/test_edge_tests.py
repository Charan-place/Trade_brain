from datetime import date, timedelta

import numpy as np
import pytest

from tbrain.research import edge_tests as E


def _day(d: str, rng, first_sign=0, last_drift=0.0, base=20000.0, vol=1.0) -> E.Day:
    step = rng.normal(0, vol, E.N_BARS)
    step[0:30] += first_sign * 0.5
    step[E.idx(15, 0):E.idx(15, 30)] += first_sign * last_drift
    c = base + np.cumsum(step)
    o = np.concatenate(([base], c[:-1]))
    return E.Day(d, o, np.maximum(o, c) + 0.2, np.minimum(o, c) - 0.2, c)


def _days(n, rng, **kw):
    out, dt = [], date(2023, 1, 2)
    while len(out) < n:
        if dt.weekday() < 5:
            out.append(_day(dt.isoformat(), rng, first_sign=int(rng.choice([-1, 1])), **kw))
        dt += timedelta(days=1)
    return out


def test_planted_first30_to_last30_momentum_is_detected_and_noise_is_not():
    rng = np.random.default_rng(1)
    planted = E.evaluate("planted", E.h1_trades(_days(250, rng, last_drift=0.25)), n_boot=2000, n_null=2000)
    assert planted.mean_bp > 1 and planted.p_two_sided < 0.01 and planted.ci_bp[0] > 0
    noise = E.evaluate("noise", E.h1_trades(_days(250, np.random.default_rng(2))), n_boot=2000, n_null=2000)
    assert noise.p_two_sided > 0.01 and noise.ci_bp[0] < 0 < noise.ci_bp[1]


def test_loader_never_reads_the_sealed_final_period(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    folder = tmp_path / "NIFTY"
    folder.mkdir()
    for month, day in (("2025-06", "2025-06-30"), ("2025-07", "2025-07-01"), ("2026-01", "2026-01-02")):
        ts = [f"{day}T{(555 + k) // 60:02d}:{(555 + k) % 60:02d}:00+05:30" for k in range(E.N_BARS)]
        px = [100.0 + k * 0.01 for k in range(E.N_BARS)]
        pq.write_table(pa.table({"ts": ts, "open": px, "high": px, "low": px, "close": px, "volume": [0] * E.N_BARS,
                                 "oi": [0] * E.N_BARS}), folder / f"{month}.parquet")
    days, info = E.load_days(tmp_path, "NIFTY")
    assert [d.date for d in days] == ["2025-06-30"]                      # last validate day kept, FINAL days never read
    with pytest.raises(PermissionError):
        E.load_days(tmp_path, "NIFTY", splits=("train", "validate", "final"))
    days2, _ = E.load_days(tmp_path, "NIFTY", splits=("final",), open_final=True)
    assert [d.date for d in days2] == ["2025-07-01", "2026-01-02"]


def _flat(d: str, close: float) -> E.Day:
    a = np.full(E.N_BARS, close)
    return E.Day(d, a, a, a, a)


def test_high_vol_regime_uses_only_information_available_the_day_before():
    # Daily moves shrink steadily, so no day is "high vol" by chance; then one violent day appears at index 70.
    days, px, dt = [], 20000.0, date(2023, 1, 2)
    for k in range(120):
        while dt.weekday() >= 5:
            dt += timedelta(days=1)
        size = 0.05 if k == 70 else 0.01 * (1 - k / 200)
        px *= 1 + (-1) ** k * size
        days.append(_flat(dt.isoformat(), px))
        dt += timedelta(days=1)
    hi = E.high_vol_days(days)
    assert days[70].date not in hi            # its own move is not known when the day is classified
    assert days[71].date in hi                # but it is known the morning after
    assert not any(days[k].date in hi for k in range(0, 71))


def test_expiry_calendar_moves_to_the_previous_trading_day_on_a_holiday():
    rng = np.random.default_rng(4)
    mon, tue_holiday, wed = date(2025, 11, 10), date(2025, 11, 11), date(2025, 11, 12)
    days = [_day(x.isoformat(), rng) for x in (mon, wed)]               # Tuesday missing = holiday
    assert E.expiry_days(days, "NIFTY") == {mon.isoformat()}


def test_holm_is_monotone_and_never_below_the_raw_p():
    rs = [E.Result("a", 10, 10, 0, 0, (0, 0), p) for p in (0.001, 0.02, 0.04, 0.5)]
    E.holm(rs)
    adj = [r.p_holm for r in rs]
    assert adj == sorted(adj) and all(a >= r.p_two_sided for a, r in zip(adj, rs))
    assert adj[0] == pytest.approx(0.004)
