"""Where the web app gets the brain's live state.

Local:  FileStateSource reads data/live/state.json (the recorder rewrites it every few seconds).
Modal:  the recorder publishes a zlib-compressed snapshot into a modal.Dict about once a second
        (make_dict_publisher); the web container reads it with DictStateSource.
"""
from __future__ import annotations

import json
import time
import zlib

STALE_S = 30


def encode(snap: dict) -> dict:
    return {"ts": time.time(), "z": zlib.compress(json.dumps(snap, default=str, separators=(",", ":")).encode(), 6)}


def decode(rec: dict | None) -> dict | None:
    if not rec or "z" not in rec:
        return None
    st = json.loads(zlib.decompress(rec["z"]))
    age = time.time() - float(rec.get("ts") or 0)
    if age > STALE_S:
        st["mode"] = "stale"
        st.setdefault("feed", {})["note"] = f"Brain not running right now; showing the state from {age / 60:.0f} min ago"
    return st


EMPTY = {"mode": "empty", "underlyings": {}, "brain": [],
         "feed": {"note": "No live data yet - the brain starts at 09:00 IST on trading days once Upstox is connected"},
         "signals": {"status": "observing", "calls": [], "reason": "No data yet", "scoreboard": {"calls": 0}}}


class FileStateSource:
    def __init__(self, settings):
        self.s = settings

    def latest(self) -> dict:
        from tbrain.ui.server import current_state
        return current_state(self.s)


class DictStateSource:
    def __init__(self, d, key: str = "state"):
        self.d, self.key = d, key

    def latest(self) -> dict:
        try:
            return decode(self.d.get(self.key)) or EMPTY
        except Exception:  # noqa: BLE001 - a read hiccup should show 'empty', not a 500
            return EMPTY


def make_dict_publisher(d, key: str = "state", every_s: float = 1.0, log=print):
    """Return a cheap snap -> None hook for Recorder(publish=...). Throttled; never raises."""
    last = [0.0]
    warned = [0.0]

    def publish(snap: dict) -> None:
        now = time.monotonic()
        if now - last[0] < every_s:
            return
        last[0] = now
        try:
            d[key] = encode(snap)
        except Exception as e:  # noqa: BLE001
            if time.time() - warned[0] > 60:
                warned[0] = time.time()
                log(f"[publish] state not shared with the web app: {e!r}")

    return publish
