"""Pull 1-minute index candles from Upstox Historical Candle Data V3 into local parquet, month by month.

Documented limits (upstox.com/developer/api-documentation/v3/get-historical-candle-data): 1-minute data starts
Jan 2022 and one request may span at most one month. Candle rows are [ts, open, high, low, close, volume, oi].
The access token comes from the existing token store and is never printed or logged.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

BASE = "https://api.upstox.com/v3/historical-candle"
FIRST_MONTH = date(2022, 1, 1)
COLUMNS = ("ts", "open", "high", "low", "close", "volume", "oi")


def month_windows(start: date, end: date) -> list[tuple[date, date]]:
    """Inclusive (from_date, to_date) calendar-month windows covering [start, end]."""
    out, cur = [], date(start.year, start.month, 1)
    while cur <= end:
        nxt = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
        out.append((max(cur, start), min(nxt - timedelta(days=1), end)))
        cur = nxt
    return out


def candle_url(instrument_key: str, frm: date, to: date) -> str:
    key = urllib.parse.quote(instrument_key, safe="")
    return f"{BASE}/{key}/minutes/1/{to.isoformat()}/{frm.isoformat()}"  # to_date comes before from_date


def parse_candles(payload: dict) -> list[list]:
    """Rows as [ts, o, h, l, c, v, oi], oldest first. Raises ValueError on an unexpected shape."""
    if payload.get("status") != "success":
        raise ValueError(f"status={payload.get('status')!r}")
    rows = (payload.get("data") or {}).get("candles") or []
    out = []
    for r in rows:
        if not isinstance(r, list) or len(r) < 6:
            raise ValueError(f"unexpected candle shape: {len(r) if isinstance(r, list) else type(r).__name__} fields")
        out.append(list(r[:7]) + [0] * (7 - len(r)))
    out.sort(key=lambda r: r[0])
    return out


def fetch_month(token: str, instrument_key: str, frm: date, to: date, retries: int = 4, pause_s: float = 0.4) -> list[list]:
    url = candle_url(instrument_key, frm, to)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                rows = parse_candles(json.loads(r.read()))
            time.sleep(pause_s)
            return rows
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise RuntimeError(f"HTTP {e.code} for {instrument_key} {frm}..{to}") from None  # never echo headers/URL auth
    raise RuntimeError("unreachable")


def pull_index(token: str, name: str, instrument_key: str, out_dir: Path, end: date, start: date = FIRST_MONTH,
               log=print) -> dict:
    """One parquet file per month under out_dir/name/. Months already on disk are skipped (resumable)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    folder = Path(out_dir) / name
    folder.mkdir(parents=True, exist_ok=True)
    gaps, rows_total = [], 0
    for frm, to in month_windows(start, end):
        f = folder / f"{frm:%Y-%m}.parquet"
        complete_month = to < end and to == (date(frm.year + (frm.month == 12), frm.month % 12 + 1, 1) - timedelta(days=1))
        if f.exists() and complete_month:
            continue
        try:
            rows = fetch_month(token, instrument_key, frm, to)
        except Exception as e:  # noqa: BLE001 - record and continue; a gap must be visible, not fatal
            gaps.append({"month": f"{frm:%Y-%m}", "error": str(e)})
            log(f"[{name}] {frm:%Y-%m}: FAILED ({e})")
            continue
        if not rows:
            gaps.append({"month": f"{frm:%Y-%m}", "error": "no candles returned"})
            log(f"[{name}] {frm:%Y-%m}: no candles")
            continue
        cols = list(zip(*rows))
        pq.write_table(pa.table({c: list(v) for c, v in zip(COLUMNS, cols)}), f)
        rows_total += len(rows)
        log(f"[{name}] {frm:%Y-%m}: {len(rows):,} candles")
    summary = {"name": name, "rows_written": rows_total, "gaps": gaps}
    (folder / "_manifest.json").write_text(json.dumps(summary, indent=1))
    return summary
