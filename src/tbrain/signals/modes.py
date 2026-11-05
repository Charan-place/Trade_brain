"""Run the three dashboard strategy profiles independently over the same live snapshot."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from tbrain.signals.engine import EngineConfig, SignalEngine
from tbrain.timeutil import IST, now_ist

MODE_RULES = {
    "conservative": {"allow_afternoon": False, "dd_stop": True, "t1_direction_check": False,
                     "vol_regime_filter": True, "regime_gate": True, "event_calendar_gate": True},
    "balanced": {"allow_afternoon": False, "dd_stop": True, "t1_direction_check": False,
                 "vol_regime_filter": False, "regime_gate": False, "event_calendar_gate": False},
    "aggressive": {"allow_afternoon": True, "dd_stop": True, "t1_direction_check": True,
                   "vol_regime_filter": False, "regime_gate": False, "event_calendar_gate": False},
}


class ModeManager:
    """Own one persisted signal engine per profile; only the selected profile reaches real execution."""

    def __init__(self, base: EngineConfig, store, store_dir: Path, log=print):
        self.store, self.store_dir, self.log = store, Path(store_dir), log
        self.hub = None
        self._selected_at_step = "balanced"
        self.engines: dict[str, SignalEngine] = {}
        for name, rules in MODE_RULES.items():
            path = self.store_dir if name == "balanced" else self.store_dir / "modes" / name
            self._add(name, replace(base, mode_id=name, **rules), path)
        self._settings = {}
        self.refresh_settings()

    def _add(self, name: str, cfg: EngineConfig, path: Path) -> None:
        engine = SignalEngine(cfg, store_dir=path, log=self.log)
        engine.on_event = lambda event, call, mode=name: self._event(mode, event, call)
        self.engines[name] = engine

    def _event(self, mode: str, event: str, call: dict) -> None:
        if self.hub is not None:
            self.hub.on_mode_event(event, call, selected=(mode == self._selected_at_step))

    def refresh_settings(self) -> None:
        try:
            settings = self.store.get() if self.store else {}
        except Exception as e:  # dashboard state must not stop the market loop
            self.log(f"[settings] {e!r}")
            return
        previous = self._selected_at_step
        selected = settings.get("mode")
        if selected is None:
            defaults = MODE_RULES["balanced"]
            effective = {k: settings.get(k, defaults[k]) for k in defaults}
            selected = next((name for name, rules in MODE_RULES.items() if rules == effective), "custom")
            settings = {**effective, "mode": selected}
            if self.store:
                try:
                    settings = self.store.update(settings)
                except Exception as e:  # noqa: BLE001
                    self.log(f"[settings] mode migration failed: {e!r}")
        if selected not in (*MODE_RULES, "custom"):
            selected = "balanced"
        if selected == "custom":
            if "custom" not in self.engines:
                custom = dict(MODE_RULES["balanced"])
                custom.update({k: settings[k] for k in custom if isinstance(settings.get(k), bool)})
                self._add("custom", replace(self.engines["balanced"].cfg, mode_id="custom", **custom),
                          self.store_dir / "modes" / "custom")
            else:
                self.engines["custom"].apply_settings(settings)
        self._settings = settings
        self._selected_at_step = selected
        if self.hub is not None and selected != previous:
            self.hub.mode_changed(selected)

    @property
    def selected_mode(self) -> str:
        return self._selected_at_step

    @property
    def cfg(self) -> EngineConfig:
        return self.engines[self.selected_mode].cfg

    def display_keys(self) -> list[str]:
        return self.engines[self.selected_mode].display_keys()

    def today_calls(self, now_ms: float | None = None) -> list[dict]:
        return self.engines[self.selected_mode].today_calls(now_ms)

    def apply_settings(self, settings: dict | None) -> dict:
        before = self.selected_mode
        self.refresh_settings()
        return {"mode": self.selected_mode} if before != self.selected_mode else {}

    def step(self, now_ms: float, ctxs: dict[str, dict], tracker) -> dict:
        self.refresh_settings()
        if self.hub is not None:
            self.hub.on_step(now_ms, ctxs)
        results = {}
        # Fixed profile engines always run, even when another profile is selected.
        for name in MODE_RULES:
            results[name] = self.engines[name].step(now_ms, ctxs, tracker)
        if "custom" in self.engines:
            results["custom"] = self.engines["custom"].step(now_ms, ctxs, tracker)
        result = results[self.selected_mode]
        result["mode"] = self.selected_mode
        names = list(MODE_RULES) + (["custom"] if "custom" in self.engines else [])
        result["mode_comparison"] = {
            "selected": self.selected_mode,
            "modes": {name: {
                "selected": name == self.selected_mode,
                "summary": self.engines[name].summarize(self.engines[name].today_calls(now_ms)),
                "calls": self.engines[name].today_calls(now_ms),
                "rules": {"version": 1, **(MODE_RULES.get(name) or {
                    k: getattr(self.engines[name].cfg, k) for k in
                    ("allow_afternoon", "dd_stop", "t1_direction_check", "vol_regime_filter", "regime_gate", "event_calendar_gate")})},
                "return_basis": "signal model: entry at ask, exits marked at bid, estimated costs",
            } for name in names},
        }
        return result

    def archive_history(self, days: int = 365) -> list[tuple[str, dict]]:
        """Rebuild per-day modeled comparisons from each profile engine's durable call log."""
        cutoff = now_ist().date() - timedelta(days=max(1, days) - 1)
        grouped: dict[str, dict[str, list[dict]]] = {}
        for name, engine in self.engines.items():
            for call in engine.calls.values():
                # Legacy balanced calls without a mode marker cannot safely be attributed to a profile.
                if call.get("mode") != name or not call.get("issued_ms"):
                    continue
                day = datetime.fromtimestamp(call["issued_ms"] / 1000, IST).date()
                if day < cutoff:
                    continue
                grouped.setdefault(day.isoformat(), {}).setdefault(name, []).append(call)
        sessions = self.store_dir.parent / "sessions"
        if sessions.exists():
            for path in sessions.glob("session_*.json"):
                try:
                    day = date.fromisoformat(path.name[len("session_"):len("session_") + 10])
                except ValueError:
                    continue
                if day >= cutoff:
                    grouped.setdefault(day.isoformat(), {})

        out = []
        for day in sorted(grouped):
            modes = {}
            for name, engine in self.engines.items():
                calls = sorted(grouped[day].get(name, []), key=lambda c: c["issued_ms"], reverse=True)
                rules = MODE_RULES.get(name) or {k: getattr(engine.cfg, k) for k in
                    ("allow_afternoon", "dd_stop", "t1_direction_check", "vol_regime_filter", "regime_gate", "event_calendar_gate")}
                modes[name] = {"selected": None, "summary": engine.summarize(calls), "calls": calls,
                               "rules": {"version": 1, **rules},
                               "return_basis": "signal model: entry at ask, exits marked at bid, estimated costs"}
            out.append((day, {"selected": None, "modes": modes}))
        return out
