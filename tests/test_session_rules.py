"""v2.2 session rules: 13:00 afternoon cutoff (dashboard switch) and drawdown stop, both shadow-tracked."""
import json
from datetime import datetime, time as dtime

import pytest

from tbrain.runtime_settings import DictSettings, FileSettings
from tbrain.signals.engine import EngineConfig, SignalEngine
from tbrain.timeutil import IST
from tests import test_signals as T


def _at(h, m):
    return datetime(2026, 10, 20, h, m, tzinfo=IST).timestamp() * 1000


def _step(eng, now_ms):
    old = T.NOW_MS
    try:
        T.NOW_MS = now_ms           # the helpers build bars/tape relative to NOW_MS
        return eng.step(now_ms, {"NIFTY": T._ctx()}, T._tracker())
    finally:
        T.NOW_MS = old


def test_no_new_calls_after_13_by_default_but_blocked_one_is_a_shadow():
    eng = SignalEngine(EngineConfig(), store_dir=None, log=lambda *a: None)
    out = _step(eng, _at(13, 5))
    assert not out["calls"]
    assert "afternoon calls are off after 13:00" in (out["diagnostics"]["NIFTY"]["blocked"] or "")
    (sh,) = eng.shadows.values()
    assert "afternoon cutoff" in {SignalEngine.blocker_key(b) for b in sh["blocked_by"]}


def test_switch_on_allows_afternoon_calls_and_morning_is_unaffected():
    eng = SignalEngine(EngineConfig(), store_dir=None, log=lambda *a: None)
    assert eng.apply_settings({"allow_afternoon": True}) == {"allow_afternoon": True}
    assert len(_step(eng, _at(13, 5))["calls"]) == 1
    eng2 = SignalEngine(EngineConfig(), store_dir=None, log=lambda *a: None)
    assert len(_step(eng2, _at(10, 30))["calls"]) == 1                     # before 13:00: unchanged


def test_apply_settings_whitelists_booleans_only():
    eng = SignalEngine(EngineConfig(), store_dir=None, log=lambda *a: None)
    assert eng.apply_settings({"min_score": 1, "allow_afternoon": "yes", "regime_gate": True}) == {}
    assert eng.cfg.min_score == 5 and eng.cfg.allow_afternoon is False and eng.cfg.regime_gate is False


def _nifty(out):
    return [c for c in out["calls"] if c["und"] == "NIFTY"]


def _closed(eng, cid, issued, exit_, r):
    eng.calls[cid] = {"id": cid, "und": "SENSEX", "dir": 1, "status": "closed", "outcome": "T2" if r > 0 else "SL",
                      "issued_ms": issued, "exit_ms": exit_, "r_now": r, "pnl_per_lot": 1000 * r, "t1_hit": r > 0}


def test_drawdown_stop_uses_only_calls_closed_before_now():
    eng = SignalEngine(EngineConfig(), store_dir=None, log=lambda *a: None)
    _closed(eng, "a", _at(9, 40), _at(10, 0), 3.0)     # peak +3R
    _closed(eng, "b", _at(10, 5), _at(10, 20), -1.1)
    _closed(eng, "c", _at(10, 22), _at(11, 0), -1.0)   # closes AFTER the 10:30 decision -> must not count yet
    assert eng.realized_path(_at(10, 30)) == (3.0, pytest.approx(1.9))
    out = _step(eng, _at(10, 30))
    assert len(_nifty(out)) == 1                           # gave back 1.1R < 2R: still trading
    eng2 = SignalEngine(EngineConfig(), store_dir=None, log=lambda *a: None)
    for args in (("a", _at(9, 40), _at(10, 0), 3.0), ("b", _at(10, 5), _at(10, 20), -1.1), ("c", _at(10, 22), _at(10, 25), -1.0)):
        _closed(eng2, *args)
    out2 = _step(eng2, _at(10, 30))
    assert not _nifty(out2) and "drawdown stop" in out2["diagnostics"]["NIFTY"]["blocked"]
    assert any("drawdown stop" == SignalEngine.blocker_key(b) for s in eng2.shadows.values() for b in s["blocked_by"])
    eng2.apply_settings({"dd_stop": False})
    assert len(_nifty(_step(eng2, _at(10, 31)))) == 1      # switch off -> trades again


def test_drawdown_stop_ignores_yesterday_and_small_peaks():
    eng = SignalEngine(EngineConfig(), store_dir=None, log=lambda *a: None)
    y = datetime(2026, 10, 19, 10, 0, tzinfo=IST).timestamp() * 1000
    _closed(eng, "y1", y, y + 60_000, 5.0); _closed(eng, "y2", y + 120_000, y + 180_000, -4.0)   # yesterday
    _closed(eng, "t1", _at(9, 40), _at(10, 0), 1.5); _closed(eng, "t2", _at(10, 1), _at(10, 10), -2.0)  # peak < 2R
    assert eng.realized_path(_at(10, 30)) == (1.5, pytest.approx(-0.5))
    assert len(_nifty(_step(eng, _at(10, 30)))) == 1


def test_rules_block_reports_switch_state():
    eng = SignalEngine(EngineConfig(), store_dir=None, log=lambda *a: None)
    r = _step(eng, _at(10, 30))["rules"]
    assert r["afternoon_cutoff"] == "13:00" and r["allow_afternoon"] is False and r["dd_stop"] is True


@pytest.mark.parametrize("store_factory", [lambda tmp: FileSettings(tmp / "live" / "settings.json"),
                                           lambda tmp: DictSettings({})])
def test_settings_stores_whitelist_and_persist(tmp_path, store_factory):
    st = store_factory(tmp_path)
    assert st.get() == {}
    assert st.update({"allow_afternoon": True, "min_score": 1, "dd_stop": "no"}) == {"allow_afternoon": True}
    assert st.update({"dd_stop": False}) == {"allow_afternoon": True, "dd_stop": False}
    assert st.get() == {"allow_afternoon": True, "dd_stop": False}


def test_backtest_presets_keep_old_behaviour_reproducible():
    from tbrain.backtest.replay import preset
    assert preset("v2.1").allow_afternoon is True and preset("v2.1").dd_stop is False      # live 19-23 Oct rules
    assert preset("v2.2").allow_afternoon is False and preset("v2.2").dd_stop is True
    assert preset("v2.2").afternoon_cutoff == dtime(13, 0)
    assert preset("v2.2+afternoon").allow_afternoon is True


def test_local_ui_settings_endpoint_with_csrf_guards(tmp_path):
    import urllib.error
    import urllib.request
    from tbrain.ui.server import Broadcaster, UIServer
    st = FileSettings(tmp_path / "s.json")
    srv = UIServer(Broadcaster(), 8791, st).start()
    try:
        base = "http://127.0.0.1:8791/api/settings"
        assert json.loads(urllib.request.urlopen(base).read())["can_edit"] is True

        def post(body, ctype="application/json", origin=None):
            req = urllib.request.Request(base, data=body, method="POST", headers={"Content-Type": ctype})
            if origin:
                req.add_header("Origin", origin)
            try:
                return urllib.request.urlopen(req).status
            except urllib.error.HTTPError as e:
                return e.code
        assert post(b"allow_afternoon=true", "application/x-www-form-urlencoded") == 415   # form post refused
        assert post(b'{"allow_afternoon": true}', origin="https://evil.example") == 403      # foreign site refused
        assert post(b'{"allow_afternoon": true}', origin="http://127.0.0.1:8791") == 200
        assert st.get() == {"allow_afternoon": True}
    finally:
        srv.stop()


def test_regime_layer_shows_labelling_with_current_reading():
    from types import SimpleNamespace
    from tbrain.brain.state import BrainState
    sig = {"diagnostics": {"NIFTY": {"regime": {"label": "range"}}, "SENSEX": {"regime": {"label": "trend"}}}}
    off = SimpleNamespace(engine=SimpleNamespace(cfg=EngineConfig()))
    lay = BrainState._regime_layer(off, sig)
    assert lay["status"] == "labelling" and "NIFTY range · SENSEX trend" in lay["detail"] and "not blocking" in lay["detail"]
    assert BrainState._regime_layer(off, {})["status"] == "off"
    on = SimpleNamespace(engine=SimpleNamespace(cfg=EngineConfig(regime_gate=True)))
    assert BrainState._regime_layer(on, sig)["status"] == "on"
