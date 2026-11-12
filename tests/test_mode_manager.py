from tests.test_signals import NOW_MS, _ctx, _tracker
from tbrain.runtime_settings import DictSettings
from tbrain.signals.engine import EngineConfig
from tbrain.signals.modes import MODE_RULES, ModeManager


class Hub:
    def __init__(self):
        self.events = []
        self.steps = 0
        self.changes = []

    def on_step(self, now_ms, ctxs):
        self.steps += 1

    def on_mode_event(self, event, call, selected):
        self.events.append((call["mode"], event, selected))

    def mode_changed(self, mode):
        self.changes.append(mode)


def test_all_profiles_run_independently_and_publish_daily_comparisons(tmp_path):
    store = DictSettings({})
    modes = ModeManager(EngineConfig(cost_per_lot_inr=40), store, tmp_path, log=lambda *a: None)
    hub = Hub(); modes.hub = hub
    result = modes.step(NOW_MS, {"NIFTY": _ctx()}, _tracker())
    assert set(result["mode_comparison"]["modes"]) == set(MODE_RULES)
    assert all(name in modes.engines and "NIFTY" in modes.engines[name].diag for name in MODE_RULES)
    assert result["mode_comparison"]["selected"] == "balanced"
    assert hub.steps == 1


def test_conservative_profile_raises_the_score_bar_and_waits_for_a_regime(tmp_path):
    modes = ModeManager(EngineConfig(), DictSettings({}), tmp_path, log=lambda *a: None)
    cons, bal = modes.engines["conservative"].cfg, modes.engines["balanced"].cfg
    assert cons.min_score == 6 and cons.warmup_bars == 20          # the two tuned fields
    assert bal.min_score == 6 and bal.warmup_bars == 15            # balanced: same bar, standard warm-up
    assert modes.engines["aggressive"].cfg.min_score == 5          # aggressive keeps the volume
    assert cons.regime_gate is False and cons.vol_regime_filter is False   # the 0-call combination is gone
    assert cons.event_calendar_gate is True


def test_selected_mode_changes_immediately_and_only_selected_issue_is_routed(tmp_path):
    store = DictSettings({})
    modes = ModeManager(EngineConfig(), store, tmp_path, log=lambda *a: None)
    hub = Hub(); modes.hub = hub
    call = {"id": "x", "mode": "aggressive"}
    modes._event("aggressive", "issue", call)
    assert hub.events[-1] == ("aggressive", "issue", False)
    store.update({"mode": "aggressive", **MODE_RULES["aggressive"]})
    modes.refresh_settings()
    modes._event("aggressive", "issue", call)
    assert modes.selected_mode == "aggressive"
    assert hub.events[-1] == ("aggressive", "issue", True)
    assert hub.changes == ["aggressive"]


def test_manual_switch_change_marks_custom_mode(tmp_path):
    store = DictSettings({})
    modes = ModeManager(EngineConfig(), store, tmp_path, log=lambda *a: None)
    store.update({"allow_afternoon": True})
    modes.refresh_settings()
    assert modes.selected_mode == "custom"
    assert "custom" in modes.engines


def test_legacy_switch_settings_migrate_to_matching_named_mode(tmp_path):
    store = DictSettings({"settings": {
        "allow_afternoon": True, "dd_stop": True, "t1_direction_check": True,
        "vol_regime_filter": False, "regime_gate": False, "event_calendar_gate": False,
    }})
    modes = ModeManager(EngineConfig(), store, tmp_path, log=lambda *a: None)
    assert modes.selected_mode == "aggressive"
    assert store.get()["mode"] == "aggressive"
