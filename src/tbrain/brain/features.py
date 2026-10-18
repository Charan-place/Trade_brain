"""Rolling per-underlying feature history (sampled ~1/s) so rules can ask "how did X change in the last N seconds"."""
from __future__ import annotations

from bisect import bisect_right


class FeatureTracker:
    def __init__(self, keep_s: float = 1800.0, every_s: float = 1.0):
        self.keep_s, self.every_s = keep_s, every_s
        self._t: dict[tuple[str, str], list[float]] = {}
        self._v: dict[tuple[str, str], list[float]] = {}
        self._last: dict[str, float] = {}

    def sample(self, und: str, t: float, values: dict[str, float | None]) -> None:
        if t - self._last.get(und, -1e18) < self.every_s:
            return
        self._last[und] = t
        for name, v in values.items():
            if v is None:
                continue
            k = (und, name)
            ts, vs = self._t.setdefault(k, []), self._v.setdefault(k, [])
            ts.append(t); vs.append(float(v))
            if ts[0] < t - self.keep_s - 60:
                cut = bisect_right(ts, t - self.keep_s)
                del ts[:cut]; del vs[:cut]

    def _at(self, und: str, name: str, t: float) -> float | None:
        ts = self._t.get((und, name))
        if not ts:
            return None
        i = bisect_right(ts, t) - 1
        return self._v[(und, name)][i] if i >= 0 else None

    def now(self, und: str, name: str) -> float | None:
        vs = self._v.get((und, name))
        return vs[-1] if vs else None

    def delta(self, und: str, name: str, secs: float) -> float | None:
        ts = self._t.get((und, name))
        if not ts or ts[-1] - ts[0] < secs * 0.8:  # not enough history yet
            return None
        past = self._at(und, name, ts[-1] - secs)
        return None if past is None else self._v[(und, name)][-1] - past

    def min_since(self, und: str, name: str, secs: float) -> float | None:
        ts = self._t.get((und, name))
        if not ts:
            return None
        i = bisect_right(ts, ts[-1] - secs)
        vals = self._v[(und, name)][i:]
        return min(vals) if vals else None

    def max_since(self, und: str, name: str, secs: float) -> float | None:
        ts = self._t.get((und, name))
        if not ts:
            return None
        i = bisect_right(ts, ts[-1] - secs)
        vals = self._v[(und, name)][i:]
        return max(vals) if vals else None
