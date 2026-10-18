"""Append-only Parquet store: <root>/<dataset>/date=YYYY-MM-DD/part-HHMMSS-<n>.parquet (atomic rename)."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from tbrain.brokers.upstox.normalize import DEPTH
from tbrain.timeutil import now_ist


def _tick_schema() -> pa.Schema:
    f64, i64, s = pa.float64(), pa.int64(), pa.string()
    fields = [("recv_ns", i64), ("cur_ts_ms", i64), ("instrument_key", s), ("src", s),
              ("ltp", f64), ("ltt_ms", i64), ("ltq", i64), ("cp", f64), ("atp", f64), ("vtt", i64),
              ("oi", f64), ("iv", f64), ("tbq", f64), ("tsq", f64),
              ("delta", f64), ("gamma", f64), ("theta", f64), ("vega", f64), ("rho", f64), ("rp", f64),
              ("i1_ts_ms", i64), ("i1_open", f64), ("i1_high", f64), ("i1_low", f64), ("i1_close", f64), ("i1_vol", i64)]
    for n in range(1, DEPTH + 1):
        fields += [(f"bp{n}", f64), (f"bq{n}", i64), (f"ap{n}", f64), (f"aq{n}", i64)]
    return pa.schema(fields)


TICK_SCHEMA = _tick_schema()
CHAIN_SCHEMA = pa.schema([
    ("recv_ns", pa.int64()), ("underlying", pa.string()), ("expiry", pa.string()), ("strike", pa.float64()),
    ("underlying_spot", pa.float64()), ("pcr", pa.float64()), ("side", pa.string()), ("instrument_key", pa.string()),
    ("ltp", pa.float64()), ("close_price", pa.float64()), ("volume", pa.float64()), ("oi", pa.float64()),
    ("prev_oi", pa.float64()), ("bid_price", pa.float64()), ("bid_qty", pa.float64()), ("ask_price", pa.float64()),
    ("ask_qty", pa.float64()), ("delta", pa.float64()), ("gamma", pa.float64()), ("theta", pa.float64()),
    ("vega", pa.float64()), ("iv", pa.float64()), ("pop", pa.float64()),
])


def _coerce(rows: list[dict], schema: pa.Schema) -> list[dict]:
    """Make chain REST values (possibly int/str) fit the float schema; leave None."""
    floats = {f.name for f in schema if pa.types.is_floating(f.type)}
    out = []
    for r in rows:
        r = dict(r)
        for k in floats:
            v = r.get(k)
            if v is not None and not isinstance(v, float):
                try:
                    r[k] = float(v)
                except (TypeError, ValueError):
                    r[k] = None
        out.append(r)
    return out


class ParquetStore:
    def __init__(self, root: Path, schema: pa.Schema, flush_s: float = 30, flush_rows: int = 200_000):
        self.root, self.schema = Path(root), schema
        self.flush_s, self.flush_rows = flush_s, flush_rows
        self._buf: list[dict] = []
        self._last = time.monotonic()
        self._n = 0
        self.rows_written = 0
        self.files_written = 0

    def add(self, rows: list[dict]) -> None:
        self._buf.extend(rows)
        if len(self._buf) >= self.flush_rows:
            self.flush()

    def maybe_flush(self) -> None:
        if self._buf and time.monotonic() - self._last >= self.flush_s:
            self.flush()

    def flush(self) -> Path | None:
        self._last = time.monotonic()
        if not self._buf:
            return None
        rows, self._buf = self._buf, []
        table = pa.Table.from_pylist(_coerce(rows, self.schema), schema=self.schema)
        now = now_ist()
        d = self.root / f"date={now.date().isoformat()}"
        d.mkdir(parents=True, exist_ok=True)
        self._n += 1
        final = d / f"part-{now.strftime('%H%M%S')}-{os.getpid()}-{self._n:05d}.parquet"
        tmp = final.with_suffix(".parquet.tmp")
        pq.write_table(table, tmp, compression="zstd")
        os.replace(tmp, final)
        self.rows_written += table.num_rows
        self.files_written += 1
        return final
