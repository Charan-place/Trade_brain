"""Build a brain snapshot offline from the latest `tbrain probe` file (no network)."""
from __future__ import annotations

import json
from pathlib import Path

from tbrain.brain.state import BrainState, count_recorded_sessions
from tbrain.brokers.upstox.instruments import Contract
from tbrain.brokers.upstox.normalize import normalize


def latest_probe(data_dir: Path) -> Path | None:
    files = sorted((data_dir / "probe").glob("probe_*.json"))
    return files[-1] if files else None


def snapshot_from_probe(path: Path, data_dir: Path, risk_free: float = 0.065, vix_key: str = "NSE_INDEX|India VIX") -> dict:
    d = json.loads(path.read_text())
    man = d["manifest"]
    contracts = [Contract.from_json(c) for c in man.get("subscribed_detail", []) if "kind" in c]
    index_keys = {u: v["index_key"] for u, v in man.get("underlyings", {}).items() if v.get("index_key")}
    b = BrainState(contracts, index_keys, vix_key, risk_free, count_recorded_sessions(data_dir))
    for i, msg in enumerate(d.get("raw_samples", [])):
        b.ingest(normalize(msg, i))
    snap = b.snapshot({"connected": False, "market_status": man.get("market_status", {}),
                       "note": f"Replay of {path.name} (recorded {man.get('started', '?')})"}, mode="demo")
    return snap
