"""Feed health: message rate, per-instrument coverage, feed-level gaps during market hours."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from tbrain.timeutil import in_market_hours

GAP_S = 10.0  # a silence longer than this during market hours counts as a recorder gap


@dataclass
class KeyStat:
    count: int = 0
    first_ns: int = 0
    last_ns: int = 0


@dataclass
class Health:
    msgs: int = 0
    rows: int = 0
    drops: int = 0
    per_key: dict[str, KeyStat] = field(default_factory=dict)
    gaps: list[tuple[int, float]] = field(default_factory=list)  # (gap_start_ns, seconds)
    _last_ns: int = 0
    _last_in_hours: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def observe(self, rows: list[dict], recv_ns: int, in_hours: bool | None = None) -> None:
        in_hours = in_market_hours() if in_hours is None else in_hours
        with self._lock:
            self.msgs += 1
            self.rows += len(rows)
            if self._last_ns and in_hours and self._last_in_hours:
                gap = (recv_ns - self._last_ns) / 1e9
                if gap > GAP_S:
                    self.gaps.append((self._last_ns, gap))
            self._last_ns, self._last_in_hours = recv_ns, in_hours
            for r in rows:
                st = self.per_key.get(r["instrument_key"])
                if st is None:
                    st = self.per_key[r["instrument_key"]] = KeyStat(first_ns=recv_ns)
                st.count += 1
                st.last_ns = recv_ns

    def summary(self) -> dict:
        with self._lock:
            return {"msgs": self.msgs, "rows": self.rows, "drops": self.drops, "keys_seen": len(self.per_key),
                    "gaps_over_10s": len(self.gaps), "max_gap_s": round(max((g for _, g in self.gaps), default=0.0), 1),
                    "total_gap_s": round(sum(g for _, g in self.gaps), 1)}
