"""Rebuild today's in-memory chart history from the parquet already recorded, so restarting `record`
mid-session keeps the candles (index, futures, today's call legs) and the VWAP curve."""
from __future__ import annotations

from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq

COLS = ["recv_ns", "instrument_key", "ltp", "ltt_ms", "vtt", "atp"]


def backfill_today(brain, ticks_dir: Path, day: str, keys: set[str], fut_keys: dict[str, str], log=print) -> int:
    d = Path(ticks_dir) / f"date={day}"
    files = sorted(d.glob("*.parquet")) if d.exists() else []   # *.parquet only: skips in-flight .tmp files
    if not files or not keys:
        return 0
    parts = []
    for f in files:
        try:
            t = pq.read_table(f, columns=COLS)
        except Exception as e:  # one bad file must not block startup
            log(f"[backfill] skip {f.name}: {e}")
            continue
        parts.append(t.filter(pc.is_in(t["instrument_key"], value_set=__import__("pyarrow").array(sorted(keys)))))
    if not parts:
        return 0
    import pyarrow as pa
    tbl = pa.concat_tables(parts).sort_by("recv_ns")
    cols = {c: tbl[c].to_pylist() for c in COLS}
    idx_to_und = {v: k for k, v in brain.index_keys.items()}
    fut_to_und = {v: k for k, v in fut_keys.items()}
    last_vtt: dict[str, int] = {}
    spot: dict[str, float] = {}
    n = 0
    for i in range(tbl.num_rows):
        k, px = cols["instrument_key"][i], cols["ltp"][i]
        if not px:
            continue
        ts = cols["ltt_ms"][i] or cols["recv_ns"][i] // 1_000_000
        v = cols["vtt"][i]
        dv = float(v - last_vtt[k]) if v is not None and k in last_vtt and v > last_vtt[k] else 0.0
        if v is not None:
            last_vtt[k] = v
        brain.candles.update(k, ts, px, dv)
        n += 1
        if k in idx_to_und:
            spot[idx_to_und[k]] = px
        elif k in fut_to_und and cols["atp"][i]:
            u = fut_to_und[k]
            if u in spot:  # index-equivalent VWAP = futures VWAP - current basis
                brain.record_vwap(u, cols["recv_ns"][i] / 1e6, cols["atp"][i] - (px - spot[u]))
    log(f"[backfill] rebuilt chart history from {len(files)} files, {n} updates")
    return n
